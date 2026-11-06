"""Interactive project setup preserves user configuration and racing writers."""

import errno
import io
import os
import stat
import struct
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Callable, Sequence
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import cast
from unittest.mock import MagicMock, patch

from graphql_desk import config_files
from graphql_desk.cli import main
from graphql_desk.config import load_project_config


def windows_security(sddl: str) -> bytes:
    if sys.platform != "win32":
        raise AssertionError("Windows fixture requires Windows")
    import ctypes
    from ctypes import wintypes

    security = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    security.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.DWORD),
    ]
    security.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = (
        wintypes.BOOL
    )
    security.GetSecurityDescriptorLength.argtypes = [ctypes.c_void_p]
    security.GetSecurityDescriptorLength.restype = wintypes.DWORD
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    descriptor = ctypes.c_void_p()
    if not security.ConvertStringSecurityDescriptorToSecurityDescriptorW(
        sddl, 1, ctypes.byref(descriptor), None
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(
            descriptor, security.GetSecurityDescriptorLength(descriptor)
        )
    finally:
        kernel.LocalFree(descriptor)


class InitTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.config = self.root / "pyproject.toml"
        self.schema = self.root / "schema.graphql"
        self.schema.write_text("type Query { greeting: String! }", encoding="utf-8")
        (self.root / "application.py").write_text(
            "raise RuntimeError('PROJECT_MUST_NOT_EXECUTE')", encoding="utf-8"
        )

    def invoke(
        self, answers: Sequence[str | BaseException], args: list[str] | None = None
    ) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with (
            patch.object(Path, "cwd", return_value=self.root),
            patch("builtins.input", side_effect=answers),
            patch(
                "graphql_desk.cli.load_from_url",
                side_effect=AssertionError("No network"),
            ),
            patch("subprocess.run", side_effect=AssertionError("No installs")),
            patch("subprocess.Popen", side_effect=AssertionError("No installs")),
            redirect_stdout(out),
            redirect_stderr(err),
        ):
            code = main(args or ["init"])
        return code, out.getvalue(), err.getvalue()

    def recovery_files(self) -> list[Path]:
        return list(self.root.glob(".pyproject.*.recovery"))

    def test_fresh_project_uses_selected_schema_and_package_without_installs(
        self,
    ) -> None:
        code, out, err = self.invoke(["schema.graphql", "api/generated", "yes"])
        self.assertEqual((code, out), (0, ""))
        configured = load_project_config(self.config)
        assert configured is not None
        self.assertEqual(configured.schemas[0].origin.local_path, self.schema)
        self.assertEqual(configured.generated_package, "api.generated")
        self.assertEqual(
            configured.schemas[0].schema_output,
            self.root / "api" / "generated" / "default.pyi",
        )
        self.assertIn("work in progress", err)
        self.assertIn("No dependencies were installed", err)
        self.assertIn("graphql-desk generate-output", err)
        self.assertFalse((self.root / "api").exists())
        self.assertFalse(self.recovery_files())
        self.assertFalse(list(self.root.glob(".pyproject.toml.*.tmp")))

    def test_existing_unrelated_configuration_and_comments_are_preserved(self) -> None:
        original = (
            '# PRIVATE_KEEP\n[project]\nname = "application" # keep\n'
            "[tool.other]\nvalue = 42\n"
        )
        self.config.write_text(original, encoding="utf-8")
        code, out, err = self.invoke(
            [
                "https://user:PRIVATE_PASSWORD@example.test/graphql?token=PRIVATE_TOKEN",
                "",
                "y",
            ]
        )
        self.assertEqual((code, out), (0, ""))
        self.assertTrue(self.config.read_text(encoding="utf-8").startswith(original))
        self.assertIn("<selected HTTP(S) URL; redacted>", err)
        for secret in ("PRIVATE_KEEP", "PRIVATE_PASSWORD", "PRIVATE_TOKEN"):
            self.assertNotIn(secret, err)
        self.assertFalse(self.recovery_files())

    def test_partial_configuration_fills_only_missing_keys(self) -> None:
        original = (
            "# preserve\n[tool.graphql-desk]\n"
            'schema = {url="https://example.test/graphql",headers={Authorization="PRIVATE"}}\n'
            "track_field_usage = false # preserve flag\n"
        )
        self.config.write_text(original, encoding="utf-8")
        code, out, err = self.invoke(["custom_types", "yes"])
        self.assertEqual((code, out), (0, ""))
        self.assertTrue(self.config.read_text(encoding="utf-8").startswith(original))
        configured = load_project_config(self.config)
        assert configured is not None
        self.assertFalse(configured.track_field_usage)
        self.assertEqual(configured.generated_package, "custom_types")
        self.assertEqual(
            configured.schemas[0].origin.headers, (("Authorization", "PRIVATE"),)
        )
        self.assertNotIn("PRIVATE", err)

    def test_existing_package_choice_is_retained_and_complete_init_is_idempotent(
        self,
    ) -> None:
        self.config.write_text(
            '[tool.graphql-desk]\ngenerated_package="my.types"\n', encoding="utf-8"
        )
        code, _, _ = self.invoke(["schema.graphql", "yes"])
        self.assertEqual(code, 0)
        self.assertIn('schema_output = "my/types/default.pyi"', self.config.read_text())
        before = self.config.read_bytes()
        code, out, err = self.invoke([])
        self.assertEqual((code, out), (0, ""))
        self.assertIn("already configured", err)
        self.assertEqual(self.config.read_bytes(), before)

    def test_complete_child_rejects_parent_output_overlap_without_prompt_or_write(
        self,
    ) -> None:
        child = self.root / "child"
        child.mkdir()
        child_config = child / "pyproject.toml"
        original = (
            b"[tool.graphql-desk]\n"
            b'schema="https://example.test/graphql?token=PRIVATE_SECRET"\n'
            b'schema_output="graphql_desk_generated/default.pyi"\n'
        )
        child_config.write_bytes(original)
        parent = (
            '[tool.graphql-desk]\nschema="https://example.test/graphql"\n'
            'generated_package="child"\nschema_output="child/default.pyi"\n'
        )
        self.config.write_text(parent, encoding="utf-8")
        before = self.config.read_bytes()
        with patch("graphql_desk.config_files.config_dacl", return_value=None):
            code, out, err = self.invoke([], ["init", "child"])
        self.assertEqual((code, out), (1, ""))
        self.assertNotIn("PRIVATE_SECRET", err)
        self.assertEqual(self.config.read_bytes(), before)
        self.assertEqual(child_config.read_bytes(), original)
        self.assertFalse(list(self.root.rglob(".pyproject*")))
        self.config.write_text(
            parent.replace('"child"', '"parent_types"').replace(
                '"child/default.pyi"', '"parent_types/default.pyi"'
            ),
            encoding="utf-8",
        )
        with patch("graphql_desk.config_files.config_dacl", return_value=None):
            self.assertEqual(self.invoke([], ["init", "child"])[0], 0)
        self.assertEqual(child_config.read_bytes(), original)

    def test_directory_argument_selects_project_without_changing_cwd(self) -> None:
        child = self.root / "child"
        child.mkdir()
        (child / "schema.graphql").write_bytes(self.schema.read_bytes())
        code, _, _ = self.invoke(["schema.graphql", "", "yes"], ["init", "child"])
        self.assertEqual(code, 0)
        self.assertTrue((child / "pyproject.toml").is_file())
        self.assertFalse(self.config.exists())

    def test_cancel_eof_and_interrupt_preserve_all_files(self) -> None:
        for answers in (
            ["schema.graphql", "", "no"],
            [EOFError()],
            [KeyboardInterrupt()],
        ):
            with self.subTest(answers=answers):
                before = {
                    path.name: path.read_bytes()
                    for path in self.root.iterdir()
                    if path.is_file()
                }
                code, out, _ = self.invoke(answers)
                self.assertEqual((code, out), (130, ""))
                self.assertEqual(
                    {
                        path.name: path.read_bytes()
                        for path in self.root.iterdir()
                        if path.is_file()
                    },
                    before,
                )

    def test_invalid_or_ambiguous_config_returns_nonsecret_reviewable_proposal(
        self,
    ) -> None:
        cases: tuple[tuple[str, list[str]], ...] = (
            ("[project\nPRIVATE_SECRET", []),
            ('tool="PRIVATE_SECRET"\n', []),
            ('[tool]\ngraphql-desk="PRIVATE_SECRET"\n', []),
            ('[tool.graphql-desk]\nschemas=[{name="one"}]\n', []),
            ('[tool.graphql-desk]\nPRIVATE_SECRET="value"\n', []),
            (
                '[tool.graphql-desk]\nschema="schema.graphql"\nschema_output="PRIVATE_SECRET"\n',
                [],
            ),
        )
        for original, answers in cases:
            with self.subTest(original=original):
                self.config.write_text(original, encoding="utf-8")
                before = self.config.read_bytes()
                code, out, err = self.invoke(answers)
                self.assertEqual((code, out), (1, ""))
                self.assertEqual(self.config.read_bytes(), before)
                self.assertIn("Review this minimal edit", err)
                self.assertIn("[tool.graphql-desk]", err)
                self.assertNotIn("PRIVATE_SECRET", err)

    def test_invalid_package_and_missing_schema_do_not_write_configuration(
        self,
    ) -> None:
        for answers in (
            ["schema.graphql", "../escape"],
            ["schema.graphql", "not-a-package"],
            ["schema.graphql", "module.with.dots"],
            ["missing.graphql"],
        ):
            with self.subTest(answers=answers):
                self.assertEqual(self.invoke(answers)[0], 1)
                self.assertFalse(self.config.exists())

    def test_help_does_not_prompt_and_global_selectors_are_rejected(self) -> None:
        self.assertEqual(self.invoke([], ["init", "--help"])[0], 0)
        code, out, err = self.invoke([], ["--config", "PRIVATE", "init"])
        self.assertEqual((code, out), (1, ""))
        self.assertNotIn("PRIVATE", err)
        self.assertFalse(self.config.exists())

    def test_configuration_changed_before_move_is_preserved(self) -> None:
        original = b'[project]\nname="original"\n'
        competing = b'[project]\nname="concurrent"\n'
        self.config.write_bytes(original)
        real_replace = os.replace

        def replace(
            source: str | os.PathLike[str], destination: str | os.PathLike[str]
        ) -> None:
            if Path(source) == self.config:
                self.config.write_bytes(competing)
            real_replace(source, destination)

        with patch("graphql_desk.cli.os.replace", side_effect=replace):
            code, _, err = self.invoke(["schema.graphql", "", "yes"])
        self.assertEqual(code, 1)
        self.assertEqual(self.config.read_bytes(), competing)
        self.assertIn("changed", err)
        self.assertFalse(self.recovery_files())

    def test_change_during_staging_is_not_overwritten(self) -> None:
        self.config.write_text('[project]\nname="original"\n', encoding="utf-8")
        competing = b'[project]\nname="changed during staging"\n'
        real_sync = os.fsync

        def sync_file(descriptor: int) -> None:
            real_sync(descriptor)
            self.config.write_bytes(competing)

        with patch("graphql_desk.cli.os.fsync", side_effect=sync_file):
            code, _, _ = self.invoke(["schema.graphql", "", "yes"])
        self.assertEqual(code, 1)
        self.assertEqual(self.config.read_bytes(), competing)
        self.assertFalse(self.recovery_files())

    def test_foreign_staged_edit_is_retained_without_replacing_original(self) -> None:
        original = b'[project]\nname="original"\n'
        foreign = b'[project]\nname="foreign staged edit"\n'
        self.config.write_bytes(original)
        real_sync = os.fsync

        def sync_file(descriptor: int) -> None:
            real_sync(descriptor)
            next(self.root.glob(".pyproject.toml.*.tmp")).write_bytes(foreign)

        with patch("graphql_desk.cli.os.fsync", side_effect=sync_file):
            code, _, err = self.invoke(["schema.graphql", "", "yes"])
        self.assertEqual(code, 1)
        self.assertEqual(self.config.read_bytes(), original)
        stages = list(self.root.glob(".pyproject.toml.*.tmp"))
        self.assertEqual(len(stages), 1)
        self.assertEqual(stages[0].read_bytes(), foreign)
        self.assertIn(str(stages[0]), err)
        self.assertFalse(self.recovery_files())

    def test_published_stage_tamper_retains_original_recovery(self) -> None:
        original = b'[project]\nname="original"\n'
        foreign = b'[project]\nname="foreign published edit"\n'
        self.config.write_bytes(original)
        real_link = os.link

        def link(
            source: str | os.PathLike[str], destination: str | os.PathLike[str]
        ) -> None:
            real_link(source, destination)
            if Path(destination) == self.config:
                self.config.write_bytes(foreign)

        with patch("graphql_desk.cli.os.link", side_effect=link):
            code, _, err = self.invoke(["schema.graphql", "", "yes"])
        self.assertEqual(code, 1)
        self.assertEqual(self.config.read_bytes(), foreign)
        copies = self.recovery_files()
        self.assertEqual(len(copies), 1)
        self.assertEqual(copies[0].read_bytes(), original)
        self.assertIn(str(copies[0]), err)
        stages = list(self.root.glob(".pyproject.toml.*.tmp"))
        self.assertEqual(len(stages), 1)
        self.assertEqual(stages[0].read_bytes(), foreign)
        self.assertIn(str(stages[0]), err)

    def test_confirmation_directory_swap_cannot_redirect_staging(self) -> None:
        for existing in (False, True):
            with self.subTest(existing=existing):
                selected = self.root / f"selected_{existing}"
                selected.mkdir()
                original = b'[project]\nname="PRIVATE_ORIGINAL"\n'
                if existing:
                    (selected / "pyproject.toml").write_bytes(original)
                replacement = self.root / f"replacement_{existing}"
                replacement.mkdir()
                moved = self.root / f"moved_{existing}"
                answers = iter(["https://example.test/graphql", "", "yes"])

                def answer() -> str:
                    value = next(answers)
                    if value == "yes":
                        selected.rename(moved)
                        try:
                            selected.symlink_to(replacement, target_is_directory=True)
                        except OSError:
                            moved.rename(selected)
                            self.skipTest("Filesystem symlinks are unavailable")
                    return value

                with (
                    patch("builtins.input", side_effect=answer),
                    patch(
                        "graphql_desk.cli.tempfile.NamedTemporaryFile",
                        side_effect=AssertionError("No staging after directory swap"),
                    ),
                ):
                    out, err = io.StringIO(), io.StringIO()
                    with redirect_stdout(out), redirect_stderr(err):
                        code = main(["init", str(selected)])
                self.assertEqual(code, 1)
                self.assertEqual(list(replacement.iterdir()), [])
                self.assertFalse(list(moved.glob(".pyproject*")))
                if existing:
                    self.assertEqual((moved / "pyproject.toml").read_bytes(), original)

    def test_nonexistent_project_directory_is_not_created(self) -> None:
        missing = self.root / "missing"
        self.assertEqual(self.invoke([], ["init", str(missing)])[0], 1)
        self.assertFalse(missing.exists())

    def test_edit_through_original_after_publication_is_retained(self) -> None:
        self.config.write_text('[project]\nname="original"\n', encoding="utf-8")
        edited_original = b'[project]\nname="edited original"\n'
        real_link = os.link

        def link(
            source: str | os.PathLike[str], destination: str | os.PathLike[str]
        ) -> None:
            real_link(source, destination)
            if Path(destination) == self.config:
                self.recovery_files()[0].write_bytes(edited_original)

        with patch("graphql_desk.cli.os.link", side_effect=link):
            code, _, err = self.invoke(["schema.graphql", "", "yes"])
        self.assertEqual(code, 1)
        self.assertIsNotNone(load_project_config(self.config))
        copies = self.recovery_files()
        self.assertEqual(len(copies), 1)
        self.assertEqual(copies[0].read_bytes(), edited_original)
        self.assertIn(str(copies[0]), err)
        self.assertNotIn("no files changed", err.lower())

    def test_competing_file_after_move_and_restore_conflict_preserve_both(self) -> None:
        original = b'[project]\nname="original"\n'
        competing = b'[project]\nname="concurrent"\n'
        self.config.write_bytes(original)
        real_replace = os.replace

        def replace(
            source: str | os.PathLike[str], destination: str | os.PathLike[str]
        ) -> None:
            real_replace(source, destination)
            if Path(source) == self.config:
                self.config.write_bytes(competing)

        with patch("graphql_desk.cli.os.replace", side_effect=replace):
            code, _, err = self.invoke(["schema.graphql", "", "yes"])
        self.assertEqual(code, 1)
        self.assertEqual(self.config.read_bytes(), competing)
        copies = self.recovery_files()
        self.assertEqual(len(copies), 1)
        self.assertEqual(copies[0].read_bytes(), original)
        self.assertIn(str(copies[0]), err)
        self.assertFalse(list(self.root.glob(".pyproject.toml.*.tmp")))

    def test_new_configuration_appearing_at_commit_is_not_replaced(self) -> None:
        real_link = os.link
        competing = b'[project]\nname="concurrent"\n'

        def link(
            source: str | os.PathLike[str], destination: str | os.PathLike[str]
        ) -> None:
            if Path(destination) == self.config:
                self.config.write_bytes(competing)
            real_link(source, destination)

        with patch("graphql_desk.cli.os.link", side_effect=link):
            code, _, _ = self.invoke(["schema.graphql", "", "yes"])
        self.assertEqual(code, 1)
        self.assertEqual(self.config.read_bytes(), competing)
        self.assertFalse(self.recovery_files())

    def test_existing_mode_and_read_only_configuration_are_preserved(self) -> None:
        original = b'[project]\nname="keep"\n'
        self.config.write_bytes(original)
        self.config.chmod(0o640)
        mode = stat.S_IMODE(self.config.stat().st_mode)
        self.assertEqual(self.invoke(["schema.graphql", "", "yes"])[0], 0)
        self.assertEqual(stat.S_IMODE(self.config.stat().st_mode), mode)
        self.config.chmod(0o444)
        before = self.config.read_bytes()
        try:
            self.assertEqual(self.invoke([])[0], 1)
            self.assertEqual(self.config.read_bytes(), before)
        finally:
            self.config.chmod(mode)

    @unittest.skipIf(sys.platform == "win32", "POSIX ownership preservation")
    def test_posix_owner_is_preserved_before_content_and_after_publication(
        self,
    ) -> None:
        if sys.platform == "win32":
            raise AssertionError("POSIX fixture requires POSIX")
        self.config.write_text('[project]\nname="PRIVATE_CONFIG"\n', encoding="utf-8")
        directory_gid = self.root.stat().st_gid
        selected_gid = next(
            (group for group in os.getgroups() if group != directory_gid),
            os.getgid(),
        )
        if os.geteuid() == 0 and selected_gid == directory_gid:
            selected_gid += 1
        os.chown(self.config, -1, selected_gid)
        identity = self.config.stat()
        observed: list[tuple[int, int, int]] = []

        def observe(descriptor: int, dacl: bytes | None) -> None:
            staged = os.fstat(descriptor)
            observed.append((staged.st_uid, staged.st_gid, staged.st_size))

        with patch("graphql_desk.config_files.apply_config_dacl", side_effect=observe):
            code, _, _ = self.invoke(["schema.graphql", "", "yes"])
        self.assertEqual(code, 0)
        self.assertEqual(observed, [(identity.st_uid, identity.st_gid, 0)])
        published = self.config.stat()
        self.assertEqual(
            (published.st_uid, published.st_gid), (identity.st_uid, identity.st_gid)
        )

    @unittest.skipIf(sys.platform == "win32", "POSIX ownership preservation")
    def test_posix_ownership_failure_precedes_content_and_mutation(self) -> None:
        if sys.platform == "win32":
            raise AssertionError("POSIX fixture requires POSIX")
        original = b'[project]\nname="PRIVATE_CONFIG"\n'
        self.config.write_bytes(original)
        observed: list[int] = []

        def deny(descriptor: int, uid: int, gid: int) -> None:
            observed.append(os.fstat(descriptor).st_size)
            raise PermissionError("Cannot preserve original group")

        with patch("graphql_desk.config_files.os.fchown", side_effect=deny):
            code, _, err = self.invoke(["schema.graphql", "", "yes"])
        self.assertEqual(code, 1)
        self.assertEqual(observed, [0])
        self.assertEqual(self.config.read_bytes(), original)
        self.assertNotIn("PRIVATE_CONFIG", err)
        self.assertFalse(list(self.root.glob(".pyproject.toml.*.tmp")))
        self.assertFalse(self.recovery_files())

    def test_linux_acl_guard_accepts_only_proven_absence(self) -> None:
        check_acl = cast(
            Callable[[int], None], getattr(config_files, "check_config_acl")
        )
        with patch("graphql_desk.config_files.sys.platform", "linux"):
            with patch(
                "graphql_desk.config_files.os.getxattr",
                side_effect=OSError(errno.ENODATA, "No ACL"),
                create=True,
            ):
                check_acl(123)
            for value in (b"", b"ACL"):
                with (
                    self.subTest(value=value),
                    patch(
                        "graphql_desk.config_files.os.getxattr",
                        return_value=value,
                        create=True,
                    ),
                ):
                    with self.assertRaises(OSError):
                        check_acl(123)
            for code in (errno.EACCES, errno.EIO, errno.ENOTSUP):
                with (
                    self.subTest(code=code),
                    patch(
                        "graphql_desk.config_files.os.getxattr",
                        side_effect=OSError(code, "Unverified ACL"),
                        create=True,
                    ),
                ):
                    with self.assertRaises(OSError):
                        check_acl(123)

    @unittest.skipUnless(sys.platform == "linux", "Linux access ACL refusal")
    def test_linux_original_and_inherited_stage_acl_are_refused(self) -> None:
        if sys.platform != "linux":
            raise AssertionError("Linux fixture requires Linux")
        original = b'[project]\nname="PRIVATE_CONFIG"\n'
        self.config.write_bytes(original)
        entries = (
            (1, 6, 0xFFFFFFFF),
            (2, 0, os.getuid() + 1),
            (4, 4, 0xFFFFFFFF),
            (16, 4, 0xFFFFFFFF),
            (32, 0, 0xFFFFFFFF),
        )
        access = struct.pack("<I", 2) + b"".join(
            struct.pack("<HHI", *entry) for entry in entries
        )
        os.setxattr(self.config, "system.posix_acl_access", access)
        self.assertEqual(self.invoke([])[0], 1)
        self.assertEqual(self.config.read_bytes(), original)
        self.assertFalse(list(self.root.glob(".pyproject*")))
        os.removexattr(self.config, "system.posix_acl_access")
        os.setxattr(self.root, "system.posix_acl_default", access)
        self.addCleanup(os.removexattr, self.root, "system.posix_acl_default")
        self.assertEqual(self.invoke(["schema.graphql", "", "yes"])[0], 1)
        self.assertEqual(self.config.read_bytes(), original)
        retained = list(self.root.glob(".pyproject.toml.*.tmp"))
        self.assertEqual(len(retained), 1)
        self.assertEqual(retained[0].read_bytes(), b"")
        self.assertFalse(self.recovery_files())

    def test_darwin_acl_guard_rejects_opaque_errors_and_acl_entries(self) -> None:
        import ctypes

        check_acl = cast(
            Callable[[int], None], getattr(config_files, "check_config_acl")
        )
        cases = (
            (0, 0, None, 0, -1, errno.EINVAL, True),
            (0, 1, None, errno.ENOENT, -1, errno.EINVAL, True),
            (0, 1, 123, 0, -1, errno.EINVAL, True),
            (1, 1, None, errno.ENOENT, -1, errno.EINVAL, False),
            (-1, 1, None, errno.ENOENT, -1, errno.EINVAL, False),
            (0, -1, None, errno.ENOENT, -1, errno.EINVAL, False),
            (0, 2, None, errno.ENOENT, -1, errno.EINVAL, False),
            (0, 1, None, errno.EACCES, -1, errno.EINVAL, False),
            (0, 1, None, 0, -1, errno.EINVAL, False),
            (0, 1, 123, 0, 0, 0, False),
            (0, 1, 123, 0, -1, errno.EIO, False),
            (0, 1, 123, 0, 1, errno.EINVAL, False),
        )
        for opaque, capability, acl, read_error, result, entry_error, allowed in cases:
            with self.subTest(
                opaque=opaque, capability=capability, acl=acl, result=result
            ):
                native = MagicMock()
                native.fpathconf.side_effect = [opaque, capability]
                native.acl_free.return_value = 0

                def read(descriptor: int, kind: int) -> int | None:
                    self.assertEqual(ctypes.get_errno(), 0)
                    ctypes.set_errno(read_error)
                    return acl

                def first(handle: int, index: int, pointer: object) -> int:
                    self.assertEqual(ctypes.get_errno(), 0)
                    ctypes.set_errno(entry_error)
                    return result

                native.acl_get_fd_np.side_effect = read
                native.acl_get_entry.side_effect = first
                with (
                    patch("graphql_desk.config_files.sys.platform", "darwin"),
                    patch("ctypes.CDLL", return_value=native),
                ):
                    if allowed:
                        check_acl(123)
                    else:
                        with self.assertRaises(OSError):
                            check_acl(123)
                if opaque == 0 and capability == 1 and acl:
                    native.acl_free.assert_called_once_with(acl)
                else:
                    native.acl_free.assert_not_called()

    @unittest.skipUnless(sys.platform == "darwin", "Darwin access ACL refusal")
    def test_darwin_original_and_inherited_stage_acl_are_refused(self) -> None:
        original = b'[project]\nname="PRIVATE_CONFIG"\n'
        self.config.write_bytes(original)
        subprocess.run(
            ["/bin/chmod", "+a", "everyone allow read", str(self.config)],
            check=True,
            capture_output=True,
        )
        self.assertEqual(self.invoke([])[0], 1)
        self.assertEqual(self.config.read_bytes(), original)
        subprocess.run(
            ["/bin/chmod", "-N", str(self.config)], check=True, capture_output=True
        )
        subprocess.run(
            ["/bin/chmod", "+a", "everyone allow read,file_inherit", str(self.root)],
            check=True,
            capture_output=True,
        )
        self.addCleanup(
            subprocess.run,
            ["/bin/chmod", "-N", str(self.root)],
            check=True,
            capture_output=True,
        )
        self.assertEqual(self.invoke(["schema.graphql", "", "yes"])[0], 1)
        self.assertEqual(self.config.read_bytes(), original)
        retained = list(self.root.glob(".pyproject.toml.*.tmp"))
        self.assertEqual(len(retained), 1)
        self.assertEqual(retained[0].read_bytes(), b"")
        self.assertFalse(self.recovery_files())

    def test_security_copy_failure_precedes_secret_content_and_mutation(self) -> None:
        original = b'[project]\nname="PRIVATE_CONFIG"\n'
        self.config.write_bytes(original)
        observed: list[int] = []

        def fail(descriptor: int, dacl: bytes | None) -> None:
            observed.append(os.fstat(descriptor).st_size)
            raise OSError("Security could not be preserved")

        with patch("graphql_desk.config_files.apply_config_dacl", side_effect=fail):
            code, _, err = self.invoke(["schema.graphql", "", "yes"])
        self.assertEqual(code, 1)
        self.assertEqual(observed, [0])
        self.assertEqual(self.config.read_bytes(), original)
        self.assertNotIn("PRIVATE_CONFIG", err)
        self.assertFalse(list(self.root.glob(".pyproject.toml.*.tmp")))
        self.assertFalse(self.recovery_files())

    @unittest.skipUnless(sys.platform == "win32", "Windows DACL preservation")
    def test_restricted_windows_dacl_is_applied_before_write_and_retained(self) -> None:
        private_dacl = windows_security("D:P(A;;FA;;;OW)")
        self.config.write_text('[project]\nname="PRIVATE_CONFIG"\n', encoding="utf-8")
        real_apply = cast(
            Callable[[int, bytes | None], None],
            getattr(config_files, "apply_config_dacl"),
        )
        read_dacl = cast(
            Callable[[int], bytes | None], getattr(config_files, "config_dacl")
        )
        with self.config.open("r+b") as original:
            real_apply(original.fileno(), private_dacl)
            expected = read_dacl(original.fileno())
        same_owner = cast(
            Callable[[bytes | None, bytes | None], bool],
            getattr(config_files, "_same_config_owner"),
        )
        observed: list[bytes | None] = []

        def copy(descriptor: int, dacl: bytes | None) -> None:
            self.assertEqual(os.fstat(descriptor).st_size, 0)
            self.assertTrue(same_owner(expected, read_dacl(descriptor)))
            real_apply(descriptor, dacl)
            observed.append(read_dacl(descriptor))

        with patch("graphql_desk.config_files.apply_config_dacl", side_effect=copy):
            code, _, err = self.invoke(["schema.graphql", "", "yes"])
        self.assertEqual(code, 0)
        self.assertEqual(observed, [expected])
        with self.config.open("rb") as updated:
            self.assertEqual(read_dacl(updated.fileno()), expected)
            self.assertTrue(same_owner(expected, read_dacl(updated.fileno())))
        self.assertNotIn("PRIVATE_CONFIG", err)
        original_bytes = b'[project]\nname="PRIVATE_CONFIG"\n'
        self.config.write_bytes(original_bytes)

        def partially_fail(descriptor: int, dacl: bytes | None) -> None:
            real_apply(descriptor, dacl)
            raise OSError("Security operation failed after changing the DACL")

        with patch(
            "graphql_desk.config_files.apply_config_dacl", side_effect=partially_fail
        ):
            code, _, err = self.invoke(["schema.graphql", "", "yes"])
        self.assertEqual(code, 1)
        self.assertEqual(self.config.read_bytes(), original_bytes)
        retained = list(self.root.glob(".pyproject.toml.*.tmp"))
        self.assertEqual(len(retained), 1)
        self.assertEqual(retained[0].read_bytes(), b"")
        self.assertIn(str(retained[0]), err)

    @unittest.skipUnless(sys.platform == "win32", "Windows owner preservation")
    def test_windows_owner_mismatch_fails_before_dacl_copy_and_content(self) -> None:
        original = b'[project]\nname="PRIVATE_CONFIG"\n'
        self.config.write_bytes(original)
        identity = self.config.stat()
        other_owner = windows_security("O:SYD:P(A;;FA;;;OW)")
        read_security = cast(
            Callable[[int], bytes | None], getattr(config_files, "config_dacl")
        )
        same_owner = cast(
            Callable[[bytes | None, bytes | None], bool],
            getattr(config_files, "_same_config_owner"),
        )
        with self.config.open("rb") as original_file:
            self.assertFalse(
                same_owner(read_security(original_file.fileno()), other_owner)
            )
        real_apply = cast(
            Callable[[int, bytes | None], None],
            getattr(config_files, "apply_config_dacl"),
        )

        def different_owner(descriptor: int) -> bytes | None:
            current = os.fstat(descriptor)
            if (current.st_dev, current.st_ino) == (identity.st_dev, identity.st_ino):
                return other_owner
            return read_security(descriptor)

        with (
            patch("graphql_desk.config_files.config_dacl", side_effect=different_owner),
            patch(
                "graphql_desk.config_files.apply_config_dacl", wraps=real_apply
            ) as apply,
        ):
            code, _, err = self.invoke(["schema.graphql", "", "yes"])
        self.assertEqual(code, 1)
        apply.assert_not_called()
        self.assertEqual(self.config.read_bytes(), original)
        self.assertNotIn("PRIVATE_CONFIG", err)
        self.assertFalse(list(self.root.glob(".pyproject.toml.*.tmp")))
        self.assertFalse(self.recovery_files())

    def test_symlink_config_and_output_are_refused(self) -> None:
        other = self.root / "other.toml"
        other.write_text('[project]\nname="keep"\n', encoding="utf-8")
        try:
            self.config.symlink_to(other)
        except OSError:
            self.skipTest("Filesystem symlinks are unavailable")
        self.assertEqual(self.invoke([])[0], 1)
        self.assertEqual(other.read_text(), '[project]\nname="keep"\n')
        self.config.unlink()
        output = self.root / "linked"
        output.symlink_to(self.root / "other", target_is_directory=True)
        self.assertEqual(self.invoke(["schema.graphql", "linked"])[0], 1)
        self.assertFalse(self.config.exists())


if __name__ == "__main__":
    unittest.main()
