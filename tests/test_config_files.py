"""Explicit staging with synthetic security; no native ACL changes or installs."""

import base64
import hashlib
import io
import json
import os
import tempfile
import unittest
from collections.abc import Callable
from pathlib import Path
from typing import cast
from unittest.mock import patch
from uuid import uuid4

from graphql_desk import config_files
from graphql_desk.config_files import (
    MAX_CONFIG_BYTES,
    ConfigFileStage,
    ConfigSnapshot,
    capture_config_directories,
    same_output,
)
from graphql_desk.editor_worker import EditorWorker, serve


class ConfigFileTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="issue356-unit-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.target = self.root / "pyrightconfig.json"
        self.original = b'{"extraPaths": ["PRIVATE_CONFIG"]}\n'
        self.target.write_bytes(self.original)
        self.text = '{"extraPaths": ["PRIVATE_CONFIG", "generated"]}\n'
        self.security: dict[tuple[int, int], bytes] = {}

        def security(descriptor: int) -> bytes:
            current = os.fstat(descriptor)
            return self.security.get(
                (current.st_dev, current.st_ino), b"PRIVATE_SECURITY"
            )

        def check_acl(descriptor: int) -> None:
            pass

        def same_owner(original: bytes | None, staged: bytes | None) -> bool:
            return True

        def apply_security(descriptor: int, original: bytes | None) -> None:
            pass

        replacements: tuple[tuple[str, object], ...] = (
            ("check_config_acl", check_acl),
            ("config_dacl", security),
            ("_same_config_owner", same_owner),
            ("apply_config_dacl", apply_security),
        )
        for name, replacement in replacements:
            fixture = patch(f"graphql_desk.config_files.{name}", replacement)
            fixture.start()
            self.addCleanup(fixture.stop)
        self.worker = EditorWorker(config_workspace=self.root)

    def request(self, **changes: object) -> dict[str, object]:
        request: dict[str, object] = {
            "op": "config_file",
            "workspace": str(self.root),
            "action": "stage",
            "file": self.target.name,
            "temporary": str(self.root / f".graphql-desk-{uuid4()}.tmp"),
            "expected_hash": hashlib.sha256(self.original).hexdigest(),
            "text": self.text,
        }
        request.update(changes)
        return request

    def phase(self, action: str, token: object) -> dict[str, object]:
        return {
            "op": "config_file",
            "workspace": str(self.root),
            "action": action,
            "token": token,
        }

    def staged(self) -> tuple[dict[str, object], Path]:
        request = self.request()
        reply = self.worker.handle(request)
        self.assertTrue(reply["ok"], reply)
        return reply, Path(cast(str, request["temporary"]))

    def caller_discard(
        self, token: object, temporary: Path, *, worker: EditorWorker | None = None
    ) -> None:
        selected = self.worker if worker is None else worker
        self.assertTrue(selected.handle(self.phase("verify_discard", token))["ok"])
        temporary.unlink()
        self.assertTrue(selected.handle(self.phase("discard", token))["ok"])

    def test_stage_verification_and_caller_commit_are_separate(self) -> None:
        reply, temporary = self.staged()
        self.assertEqual(self.target.read_bytes(), self.original)
        self.assertEqual(temporary.read_text(), self.text)
        self.assertTrue(self.worker.handle(self.phase("verify", reply["token"]))["ok"])
        os.replace(temporary, self.target)
        self.assertTrue(self.worker.handle(self.phase("finish", reply["token"]))["ok"])
        self.assertFalse(temporary.exists())
        self.assertEqual(self.target.read_text(), self.text)
        self.assertFalse(self.worker.handle(self.phase("verify", reply["token"]))["ok"])

    def test_discard_confirmation_never_unlinks_a_present_stage(self) -> None:
        reply, temporary = self.staged()
        before = temporary.read_bytes()
        self.assertTrue(
            self.worker.handle(self.phase("verify_discard", reply["token"]))["ok"]
        )
        self.assertEqual(temporary.read_bytes(), before)
        self.assertFalse(
            self.worker.handle(self.phase("discard", reply["token"]))["ok"]
        )
        self.assertEqual(temporary.read_bytes(), before)
        self.assertFalse(self.worker.handle(self.request())["ok"])
        self.caller_discard(reply["token"], temporary)
        self.staged()

    def test_verify_discard_requires_the_exact_token_and_request_shape(self) -> None:
        reply, temporary = self.staged()
        for request in (
            self.phase("verify_discard", "0" * 32),
            {**self.phase("verify_discard", reply["token"]), "text": "PRIVATE_CONFIG"},
        ):
            self.assertEqual(
                self.worker.handle(request),
                {"ok": False, "error": "editor-request-failed"},
            )
            self.assertEqual(temporary.read_text(), self.text)
        self.caller_discard(reply["token"], temporary)

    def test_discard_confirmation_preserves_a_file_recreated_after_caller_unlink(
        self,
    ) -> None:
        reply, temporary = self.staged()
        self.assertTrue(
            self.worker.handle(self.phase("verify_discard", reply["token"]))["ok"]
        )
        temporary.unlink()
        temporary.write_text("foreign replacement", encoding="utf8")
        self.assertFalse(
            self.worker.handle(self.phase("discard", reply["token"]))["ok"]
        )
        self.assertFalse(
            self.worker.handle(self.phase("verify_discard", reply["token"]))["ok"]
        )
        self.assertEqual(temporary.read_text(), "foreign replacement")
        self.assertEqual(self.target.read_bytes(), self.original)
        self.assertFalse(self.worker.handle(self.request())["ok"])

    def test_absent_stage_can_be_confirmed_after_revocation_without_source_freshness(
        self,
    ) -> None:
        reply, temporary = self.staged()
        self.worker.invalidate()
        self.target.write_text("user configuration", encoding="utf8")
        self.assertTrue(
            self.worker.handle(self.phase("verify_discard", reply["token"]))["ok"]
        )
        temporary.unlink()
        self.assertTrue(
            self.worker.handle(self.phase("verify_discard", reply["token"]))["ok"]
        )
        self.assertTrue(self.worker.handle(self.phase("discard", reply["token"]))["ok"])
        self.assertEqual(self.target.read_text(), "user configuration")

    def test_restart_between_caller_unlink_and_confirmation_resumes_only_known_target(
        self,
    ) -> None:
        for changed_target in (False, True):
            with self.subTest(changed_target=changed_target):
                self.worker = EditorWorker(config_workspace=self.root)
                self.target.write_bytes(self.original)
                reply, temporary = self.staged()
                self.assertTrue(
                    self.worker.handle(self.phase("verify_discard", reply["token"]))[
                        "ok"
                    ]
                )
                temporary.unlink()
                if changed_target:
                    self.target.write_text(
                        "foreign user configuration", encoding="utf8"
                    )
                restarted = EditorWorker(config_workspace=self.root)
                resumed = restarted.handle(
                    {
                        "op": "config_file",
                        "workspace": str(self.root),
                        "action": "resume",
                        "receipt": reply["receipt"],
                    }
                )
                self.assertEqual(resumed["ok"], not changed_target)
                if not changed_target:
                    self.assertFalse(
                        restarted.handle(self.phase("verify", resumed["token"]))["ok"]
                    )
                    self.assertTrue(
                        restarted.handle(
                            self.phase("verify_discard", resumed["token"])
                        )["ok"]
                    )
                    self.assertTrue(
                        restarted.handle(self.phase("discard", resumed["token"]))["ok"]
                    )
                self.assertFalse(temporary.exists())
                self.assertEqual(
                    self.target.read_bytes(),
                    b"foreign user configuration" if changed_target else self.original,
                )

    def test_default_worker_and_wrong_workspace_cannot_stage(self) -> None:
        request = self.request()
        self.assertFalse(EditorWorker().handle(request)["ok"])
        self.assertFalse(
            self.worker.handle({**request, "workspace": str(self.root.parent)})["ok"]
        )
        self.assertFalse(Path(cast(str, request["temporary"])).exists())
        self.assertEqual(self.target.read_bytes(), self.original)

    def test_only_allowed_targets_reserved_names_and_bounded_content(self) -> None:
        for change in (
            {"file": "other.json"},
            {"file": "../pyrightconfig.json"},
            {"temporary": str(self.root.parent / f".graphql-desk-{uuid4()}.tmp")},
            {"temporary": str(self.root / "unreserved.tmp")},
            {"text": "x" * (MAX_CONFIG_BYTES + 1)},
            {"expected_hash": "0" * 64},
        ):
            with self.subTest(change=tuple(change)):
                request = self.request(**change)
                self.assertFalse(self.worker.handle(request)["ok"])
                self.assertFalse(Path(cast(str, request["temporary"])).exists())
        self.assertEqual(self.target.read_bytes(), self.original)
        self.assertEqual(list(self.root.iterdir()), [self.target])

    def test_invalid_original_utf8_is_preserved_without_creating_a_stage(self) -> None:
        self.original = b'{"private": "PRIVATE_CONFIG\xff"}\n'
        self.target.write_bytes(self.original)
        request = self.request()
        reply = self.worker.handle(request)
        self.assertEqual(reply, {"ok": False, "error": "editor-request-failed"})
        self.assertEqual(self.target.read_bytes(), self.original)
        self.assertFalse(Path(cast(str, request["temporary"])).exists())

    def test_absent_cleanup_verification_rejects_a_namespace_change(self) -> None:
        reply, temporary = self.staged()
        temporary.unlink()
        absent = cast(Callable[[Path], bool], getattr(config_files, "_absent"))
        directory = cast(
            Callable[[Path], tuple[int, ...]], getattr(config_files, "_safe_directory")
        )
        changed = False

        def absent_then_changed(path: Path) -> bool:
            nonlocal changed
            result = absent(path)
            if path == temporary:
                changed = True
            return result

        def observed_directory(path: Path) -> tuple[int, ...]:
            identity = directory(path)
            if changed and path == self.root:
                return (identity[0], identity[1] + 1, *identity[2:])
            return identity

        with (
            patch("graphql_desk.config_files._absent", absent_then_changed),
            patch("graphql_desk.config_files._safe_directory", observed_directory),
        ):
            self.assertFalse(
                self.worker.handle(self.phase("verify_discard", reply["token"]))["ok"]
            )
        self.assertTrue(changed)
        self.assertEqual(self.target.read_bytes(), self.original)

    def test_capture_return_namespace_swap_does_not_create_an_orphan_stage(
        self,
    ) -> None:
        replacement_directory = tempfile.TemporaryDirectory(prefix="issue356-swap-")
        self.addCleanup(replacement_directory.cleanup)
        replacement = Path(replacement_directory.name).resolve()
        link_check = self.root / "directory-link-check"
        try:
            link_check.symlink_to(replacement, target_is_directory=True)
        except OSError:
            self.skipTest("Directory symlinks are unavailable")
        link_check.unlink()
        old_root = self.root.with_name(self.root.name + "-original")

        def restore_root() -> None:
            if self.root.is_symlink():
                self.root.unlink()
            if old_root.exists():
                old_root.rename(self.root)

        self.addCleanup(restore_root)
        capture = cast(
            Callable[..., ConfigSnapshot], getattr(config_files, "_capture_config")
        )
        swapped = False

        def capture_then_swap(path: Path, *, writable: bool = False) -> ConfigSnapshot:
            nonlocal swapped
            snapshot = capture(path, writable=writable)
            if writable:
                self.root.rename(old_root)
                self.root.symlink_to(replacement, target_is_directory=True)
                swapped = True
            return snapshot

        request = self.request()
        with patch("graphql_desk.config_files._capture_config", capture_then_swap):
            reply = self.worker.handle(request)
        self.assertTrue(swapped)
        self.assertFalse(reply["ok"])
        self.assertEqual((old_root / self.target.name).read_bytes(), self.original)
        self.assertEqual(list(replacement.iterdir()), [])

    def test_source_changes_fail_every_verify_but_owned_discard_is_independent(
        self,
    ) -> None:
        reply, temporary = self.staged()
        self.assertTrue(self.worker.handle(self.phase("verify", reply["token"]))["ok"])
        self.target.write_text("user edit", encoding="utf8")
        self.assertFalse(self.worker.handle(self.phase("verify", reply["token"]))["ok"])
        self.caller_discard(reply["token"], temporary)
        self.assertFalse(temporary.exists())
        self.assertEqual(self.target.read_text(), "user edit")

    def test_stage_mutation_or_same_bytes_replacement_is_preserved(self) -> None:
        for replace in (False, True):
            with self.subTest(replace=replace):
                self.worker = EditorWorker(config_workspace=self.root)
                reply, temporary = self.staged()
                if replace:
                    other = self.root / "replacement"
                    other.write_bytes(temporary.read_bytes())
                    os.replace(other, temporary)
                else:
                    temporary.write_text("foreign edit", encoding="utf8")
                self.assertFalse(
                    self.worker.handle(self.phase("verify_discard", reply["token"]))[
                        "ok"
                    ]
                )
                self.assertFalse(
                    self.worker.handle(self.phase("discard", reply["token"]))["ok"]
                )
                self.assertTrue(temporary.exists())
                self.assertFalse(self.worker.handle(self.request())["ok"])

    def test_security_change_is_detected_without_native_acl_mutation(self) -> None:
        reply, temporary = self.staged()
        current = temporary.stat()
        self.security[(current.st_dev, current.st_ino)] = b"OTHER_SECURITY"
        self.assertFalse(self.worker.handle(self.phase("verify", reply["token"]))["ok"])
        self.assertFalse(
            self.worker.handle(self.phase("verify_discard", reply["token"]))["ok"]
        )
        self.assertFalse(
            self.worker.handle(self.phase("discard", reply["token"]))["ok"]
        )
        self.assertTrue(temporary.exists())

    def test_authorization_is_preserved_before_content_is_written(self) -> None:
        observed: list[int] = []

        def preserve(descriptor: int, security: bytes | None) -> None:
            self.assertEqual(security, b"PRIVATE_SECURITY")
            observed.append(os.fstat(descriptor).st_size)

        with patch("graphql_desk.config_files.apply_config_dacl", preserve):
            reply, temporary = self.staged()
        self.assertEqual(observed, [0])
        self.caller_discard(reply["token"], temporary)

    def test_failed_security_copy_keeps_empty_slot_without_writing_secrets(
        self,
    ) -> None:
        request = self.request()
        with patch(
            "graphql_desk.config_files.apply_config_dacl",
            side_effect=OSError("PRIVATE_FAILURE"),
        ):
            reply = self.worker.handle(request)
        self.assertEqual(reply, {"ok": False, "error": "editor-request-failed"})
        temporary = Path(cast(str, request["temporary"]))
        self.assertEqual(temporary.read_bytes(), b"")
        next_request = self.request()
        self.assertFalse(self.worker.handle(next_request)["ok"])
        self.assertFalse(Path(cast(str, next_request["temporary"])).exists())
        self.assertEqual(self.target.read_bytes(), self.original)

    def test_changed_stage_is_preserved_before_writing_content(self) -> None:
        request = self.request()
        temporary = Path(cast(str, request["temporary"]))

        def replace_path(descriptor: int, security: bytes | None) -> None:
            temporary.write_text("foreign content", encoding="utf8")

        with patch("graphql_desk.config_files.apply_config_dacl", replace_path):
            reply = self.worker.handle(request)
        self.assertFalse(reply["ok"])
        self.assertEqual(temporary.read_text(), "foreign content")
        self.assertEqual(self.target.read_bytes(), self.original)

    def test_invalidation_retains_revoked_slot_until_verified_discard(self) -> None:
        reply, temporary = self.staged()
        self.worker.invalidate()
        self.assertTrue(temporary.exists())
        self.assertFalse(self.worker.handle(self.phase("verify", reply["token"]))["ok"])
        second = self.request()
        self.assertFalse(self.worker.handle(second)["ok"])
        self.assertFalse(Path(cast(str, second["temporary"])).exists())
        self.caller_discard(reply["token"], temporary)
        self.assertFalse(temporary.exists())
        self.staged()

    def test_restart_receipt_discards_only_the_exact_owned_stage(self) -> None:
        reply, temporary = self.staged()
        self.target.write_text("new user configuration", encoding="utf8")
        worker = EditorWorker(config_workspace=self.root)
        resumed = worker.handle(
            {
                "op": "config_file",
                "workspace": str(self.root),
                "action": "resume",
                "receipt": reply["receipt"],
            }
        )
        self.assertTrue(resumed["ok"], resumed)
        self.assertFalse(worker.handle(self.phase("verify", resumed["token"]))["ok"])
        self.caller_discard(resumed["token"], temporary, worker=worker)
        self.assertFalse(temporary.exists())
        self.assertEqual(self.target.read_text(), "new user configuration")

    def test_restart_receipt_cannot_delete_a_foreign_same_bytes_replacement(
        self,
    ) -> None:
        reply, temporary = self.staged()
        other = self.root / "foreign"
        other.write_bytes(temporary.read_bytes())
        os.replace(other, temporary)
        worker = EditorWorker(config_workspace=self.root)
        resumed = worker.handle(
            {
                "op": "config_file",
                "workspace": str(self.root),
                "action": "resume",
                "receipt": reply["receipt"],
            }
        )
        self.assertFalse(resumed["ok"])
        self.assertTrue(temporary.exists())

    def test_pyproject_target_is_staged_without_parsing_or_reformatting(self) -> None:
        self.target = self.root / "pyproject.toml"
        self.original = b'# keep\n[tool.pyright]\nstubPath="custom-stubs"\n'
        self.target.write_bytes(self.original)
        self.text = '# keep\n[tool.pyright]\nstubPath=".graphql-desk/live-typings"\n'
        reply, temporary = self.staged()
        self.assertEqual(self.target.read_bytes(), self.original)
        self.assertEqual(temporary.read_text(), self.text)
        self.caller_discard(reply["token"], temporary)

    def test_symlink_source_is_refused_without_touching_its_target(self) -> None:
        other = self.root / "other.json"
        other.write_bytes(self.original)
        self.target.unlink()
        try:
            self.target.symlink_to(other)
        except OSError:
            self.skipTest("File symlinks are unavailable")
        request = self.request()
        self.assertFalse(self.worker.handle(request)["ok"])
        self.assertEqual(other.read_bytes(), self.original)
        self.assertFalse(Path(cast(str, request["temporary"])).exists())

    def test_opened_file_identity_is_rechecked_after_lstat(self) -> None:
        identity = self.target.stat()
        snapshot = ConfigSnapshot(self.original, identity, b"PRIVATE_SECURITY")
        replacement = self.root / "replacement"
        replacement.write_bytes(self.original)
        real_open = Path.open

        def open_after_swap(path: Path, mode: str = "r", **kwargs: object) -> object:
            if path == self.target and replacement.exists():
                os.replace(replacement, path)
            return real_open(path, mode)

        with patch.object(Path, "open", open_after_swap):
            self.assertFalse(same_output(self.target, snapshot))
        self.assertEqual(self.target.read_bytes(), self.original)

    def test_restart_after_commit_verifies_published_receipt(self) -> None:
        reply, temporary = self.staged()
        os.replace(temporary, self.target)
        worker = EditorWorker(config_workspace=self.root)
        resumed = worker.handle(
            {
                "op": "config_file",
                "workspace": str(self.root),
                "action": "resume",
                "receipt": reply["receipt"],
            }
        )
        self.assertTrue(resumed["ok"], resumed)
        self.assertTrue(worker.handle(self.phase("finish", resumed["token"]))["ok"])
        self.assertEqual(self.target.read_text(), self.text)

    def test_receipt_contains_no_source_bytes_or_security_descriptor(self) -> None:
        reply, _ = self.staged()
        decoded = base64.b64decode(cast(str, reply["receipt"]))
        self.assertNotIn(b"PRIVATE_CONFIG", decoded)
        self.assertNotIn(b"PRIVATE_SECURITY", decoded)
        record = json.loads(decoded)
        for identity in (record["original"], record["staged"]):
            self.assertTrue(all(isinstance(item, str) for item in identity))
        record["staged"][1] = str(2**100)
        forged = base64.b64encode(json.dumps(record).encode()).decode()
        worker = EditorWorker(config_workspace=self.root)
        reply = worker.handle(
            {
                "op": "config_file",
                "workspace": str(self.root),
                "action": "resume",
                "receipt": forged,
            }
        )
        self.assertFalse(reply["ok"])

    def test_replaced_workspace_is_preserved(self) -> None:
        reply, temporary = self.staged()
        old_root = self.root.with_name(self.root.name + "-original")
        self.root.rename(old_root)
        self.addCleanup(lambda: old_root.rename(self.root))
        self.root.mkdir()
        self.addCleanup(self.root.rmdir)
        self.assertFalse(
            self.worker.handle(self.phase("discard", reply["token"]))["ok"]
        )
        self.assertTrue((old_root / temporary.name).exists())

    def test_readonly_parse_operation_does_not_touch_pending_stage(self) -> None:
        reply, temporary = self.staged()
        before = temporary.read_bytes()
        self.assertTrue(
            self.worker.handle(
                {
                    "op": "pyright_config",
                    "workspace": str(self.root),
                    "text": "[tool.pyright]\n",
                    "action": "inspect",
                    "managed_path": ".graphql-desk/live-typings",
                    "ownership": {
                        "top_level": False,
                        "execution_environment_roots": [],
                    },
                }
            )["ok"]
        )
        self.assertEqual(temporary.read_bytes(), before)
        self.caller_discard(reply["token"], temporary)

    def test_protocol_failures_redact_content_and_staging_is_explicit(self) -> None:
        request = self.request(text="PRIVATE_SECRET", expected_hash="invalid")
        writer = io.BytesIO()
        serve(
            io.BytesIO((json.dumps(request) + "\n").encode()),
            writer,
            config_workspace=self.root,
        )
        self.assertEqual(
            json.loads(writer.getvalue()),
            {"ok": False, "error": "editor-request-failed"},
        )
        self.assertNotIn(b"PRIVATE_SECRET", writer.getvalue())
        self.assertFalse(Path(cast(str, request["temporary"])).exists())

    def test_library_anchor_is_inert_and_stage_creation_does_not_replace_existing(
        self,
    ) -> None:
        request = self.request()
        temporary = Path(cast(str, request["temporary"]))
        temporary.write_text("foreign data", encoding="utf8")
        stage = ConfigFileStage.capture(
            capture_config_directories(self.root), self.target.name, temporary
        )
        with self.assertRaises(FileExistsError):
            stage.write(self.text, hashlib.sha256(self.original).hexdigest())
        with self.assertRaises(ValueError):
            stage.verify_discard()
        with self.assertRaises(ValueError):
            stage.discard()
        self.assertEqual(temporary.read_text(), "foreign data")


if __name__ == "__main__":
    unittest.main()
