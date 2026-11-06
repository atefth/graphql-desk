"""Actual archive allowlists and the offline distribution orchestration."""

import base64
import csv
import hashlib
import io
import os
import stat
import subprocess
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from hatchling.utils.fs import locate_file

from scripts import check_distributions as checker


class DistributionTests(unittest.TestCase):
    def setUp(self) -> None:
        repository = Path(__file__).resolve().parents[1]
        parent = repository / ".tmp"
        parent.mkdir(exist_ok=True)
        checker.check_repository_path(repository, parent)
        temporary = tempfile.TemporaryDirectory(prefix="distribution-unit-", dir=parent)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.contents = {
            "pyproject.toml": b"""[project]
name="graphql-desk"
version="0.1.0"
description="Typed GraphQL documents for Python"
readme="README.md"
requires-python=">=3.10"
license="MIT"
dependencies=["graphql-core==3.3.1"]
license-files=["LICENSE",
"docs/release/third-party-notices.md", "docs/release/licenses/*"]
[project.scripts]
graphql-desk="graphql_desk.cli:main"
[tool.hatch.build.targets.sdist]
only-include=["src/graphql_desk", "docs/release",
"pyproject.toml", "README.md", "LICENSE"]
""",
            "README.md": b"Example package\n",
            "LICENSE": (repository / "LICENSE").read_bytes(),
            "src/graphql_desk/__init__.py": (
                b"raise AssertionError('never imported by archive inspection')\n"
            ),
            "src/graphql_desk/py.typed": b"",
            "docs/release/third-party-notices.md": (
                b"See [license](licenses/dependency.txt).\n"
            ),
            "docs/release/licenses/dependency.txt": (
                b"Copyright required\r\nPermission required\r\n"
            ),
        }
        for name, data in self.contents.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        tracked = b"".join(
            f"100644 {'a' * 40} 0\t{name}\0".encode() for name in self.contents
        )
        result = subprocess.CompletedProcess(["git"], 0, stdout=tracked)
        self.git = patch(
            "scripts.check_distributions.subprocess.run", return_value=result
        )
        self.git.start()
        self.addCleanup(self.git.stop)
        self.info = "graphql_desk-0.1.0.dist-info/"
        self.metadata = (
            b"Metadata-Version: 2.5\nName: graphql-desk\nVersion: 0.1.0\n"
            b"Summary: Typed GraphQL documents for Python\n"
            b"Requires-Python: >=3.10\nRequires-Dist: graphql-core==3.3.1\n"
            b"License-Expression: MIT\nLicense-File: LICENSE\n"
            b"License-File: docs/release/third-party-notices.md\n"
            b"License-File: docs/release/licenses/dependency.txt\n"
            b"Description-Content-Type: text/markdown\n\nExample package\n"
        )

    def wheel_files(self) -> dict[str, bytes]:
        files = {
            name.removeprefix("src/"): data
            for name, data in self.contents.items()
            if name.startswith("src/")
        }
        files.update(
            {
                f"{self.info}licenses/{name}": data
                for name, data in self.contents.items()
                if name == "LICENSE" or name.startswith("docs/release/")
            }
        )
        files.update(
            {
                f"{self.info}METADATA": self.metadata,
                f"{self.info}WHEEL": (
                    b"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
                ),
                f"{self.info}entry_points.txt": (
                    b"[console_scripts]\ngraphql-desk = graphql_desk.cli:main\n"
                ),
            }
        )
        return files

    def wheel(
        self, files: dict[str, bytes] | None = None, target: Path | None = None
    ) -> Path:
        files = dict(self.wheel_files() if files is None else files)
        rows = [
            [
                name,
                "sha256="
                + base64.urlsafe_b64encode(hashlib.sha256(data).digest())
                .rstrip(b"=")
                .decode(),
                str(len(data)),
            ]
            for name, data in files.items()
        ]
        record = f"{self.info}RECORD"
        rows.append([record, "", ""])
        stream = io.StringIO()
        csv.writer(stream).writerows(rows)
        files[record] = stream.getvalue().encode()
        target = target or self.root / "graphql_desk-0.1.0-py3-none-any.whl"
        with zipfile.ZipFile(target, "w") as archive:
            for name, data in files.items():
                archive.writestr(name, data)
        return target

    def sdist(
        self,
        target: Path | None = None,
        extra: str | None = None,
        controls: dict[str, bytes] | None = None,
    ) -> Path:
        target = target or self.root / "graphql_desk-0.1.0.tar.gz"
        files = {
            **self.contents,
            "PKG-INFO": self.metadata,
            **({".gitignore": b"", ".hgignore": b""} if controls is None else controls),
        }
        if extra:
            files[extra] = b"must not ship"
        with tarfile.open(target, "w:gz") as archive:
            for name, data in files.items():
                info = tarfile.TarInfo(f"graphql_desk-0.1.0/{name}")
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
        return target

    def test_exact_archives_preserve_notice_bytes_without_importing_code(self) -> None:
        checker.check_core_wheel(self.wheel(), self.root)
        checker.check_core_sdist(self.sdist(), self.root)

    def test_private_or_test_files_never_belong_to_the_wheel(self) -> None:
        for name in (
            "tests/secret.py",
            ".venv-alt/private.txt",
            "graphql_desk/__pycache__/x.pyc",
            "Pylance/extension.js",
        ):
            with self.subTest(name=name):
                files = self.wheel_files()
                files[name] = b"must not ship"
                with self.assertRaisesRegex(ValueError, "unexpected"):
                    checker.check_core_wheel(self.wheel(files), self.root)

    def test_wheel_rejects_missing_or_changed_bytes_metadata_and_entrypoint(
        self,
    ) -> None:
        for name, replacement in (
            ("graphql_desk/py.typed", None),
            (f"{self.info}licenses/LICENSE", None),
            (f"{self.info}licenses/LICENSE", b"license changed"),
            ("graphql_desk/__init__.py", b"foreign"),
            (
                f"{self.info}licenses/docs/release/licenses/dependency.txt",
                b"copyright removed",
            ),
            (f"{self.info}METADATA", self.metadata.replace(b"3.3.1", b"3.3.0")),
            (
                f"{self.info}METADATA",
                self.metadata.replace(b"Example package", b"Stale package"),
            ),
            (
                f"{self.info}METADATA",
                self.metadata.replace(b"Version: 0.1.0", b"Version: 0.0.1"),
            ),
            (f"{self.info}METADATA", b"Project-URL: PRIVATE\n" + self.metadata),
            (
                f"{self.info}METADATA",
                self.metadata.replace(b"License-Expression: MIT\n", b""),
            ),
            (
                f"{self.info}METADATA",
                self.metadata.replace(
                    b"License-Expression: MIT", b"License-Expression: ISC"
                ),
            ),
            (
                f"{self.info}entry_points.txt",
                b"[console_scripts]\ngraphql-desk = foreign:main\n",
            ),
        ):
            with self.subTest(name=name):
                files = self.wheel_files()
                if replacement is None:
                    del files[name]
                else:
                    files[name] = replacement
                with self.assertRaises(ValueError):
                    checker.check_core_wheel(self.wheel(files), self.root)

    def test_artifact_filename_must_match_the_project(self) -> None:
        with self.assertRaisesRegex(ValueError, "filename"):
            checker.check_core_wheel(
                self.wheel(target=self.root / "foreign.whl"), self.root
            )
        with self.assertRaisesRegex(ValueError, "filename"):
            checker.check_core_sdist(
                self.sdist(target=self.root / "foreign.tar.gz"), self.root
            )

    def test_wheel_record_integrity_is_checked_separately(self) -> None:
        path = self.wheel()
        with zipfile.ZipFile(path) as archive:
            files = {name: archive.read(name) for name in archive.namelist()}
        files[f"{self.info}RECORD"] = files[f"{self.info}RECORD"].replace(
            b"sha256=", b"sha512=", 1
        )
        with zipfile.ZipFile(path, "w") as archive:
            for name, data in files.items():
                archive.writestr(name, data)
        with self.assertRaisesRegex(ValueError, "RECORD integrity"):
            checker.check_core_wheel(path, self.root)

    def test_oversized_allowed_wheel_member_is_rejected_before_payload_read(
        self,
    ) -> None:
        for name in (
            "graphql_desk/__init__.py",
            f"{self.info}METADATA",
            f"{self.info}RECORD",
        ):
            with self.subTest(name=name):
                path = self.root / "graphql_desk-0.1.0-py3-none-any.whl"
                with zipfile.ZipFile(
                    path, "w", compression=zipfile.ZIP_DEFLATED
                ) as archive:
                    archive.writestr(name, b"x" * (128 * 1024))
                with (
                    patch.object(
                        zipfile.ZipFile,
                        "open",
                        side_effect=AssertionError("Payload was read"),
                    ) as read,
                    self.assertRaisesRegex(ValueError, "size"),
                ):
                    checker.check_core_wheel(path, self.root)
                read.assert_not_called()

    def test_oversized_allowed_sdist_member_is_rejected_before_payload_read(
        self,
    ) -> None:
        for name in ("pyproject.toml", "PKG-INFO"):
            with self.subTest(name=name):
                path = self.root / "graphql_desk-0.1.0.tar.gz"
                with tarfile.open(path, "w:gz") as archive:
                    info = tarfile.TarInfo(f"graphql_desk-0.1.0/{name}")
                    info.size = 128 * 1024
                    archive.addfile(info, io.BytesIO(b"x" * info.size))
                with (
                    patch.object(
                        tarfile.TarFile,
                        "extractfile",
                        side_effect=AssertionError("Payload was read"),
                    ) as read,
                    self.assertRaisesRegex(ValueError, "size"),
                ):
                    checker.check_core_sdist(path, self.root)
                read.assert_not_called()

    def test_duplicate_traversal_and_link_archive_entries_are_rejected(self) -> None:
        for kind in ("duplicate", "traversal", "link"):
            with self.subTest(kind=kind):
                path = self.wheel()
                with zipfile.ZipFile(path, "a") as archive:
                    if kind == "link":
                        entry = zipfile.ZipInfo("linked.py")
                        entry.external_attr = (stat.S_IFLNK | 0o777) << 16
                        archive.writestr(entry, b"outside")
                    else:
                        archive.writestr(
                            "GRAPHQL_DESK/__INIT__.PY"
                            if kind == "duplicate"
                            else "../escape",
                            b"outside",
                        )
                with self.assertRaisesRegex(ValueError, "Duplicate|linked|Unsafe"):
                    checker.check_core_wheel(path, self.root)
        for kind in ("duplicate", "traversal", "link"):
            with self.subTest(tar=kind):
                path = self.root / "graphql_desk-0.1.0.tar.gz"
                with tarfile.open(path, "w:gz") as archive:
                    name = (
                        "../escape"
                        if kind == "traversal"
                        else "graphql_desk-0.1.0/README.md"
                    )
                    info = tarfile.TarInfo(name)
                    if kind == "link":
                        info.type = tarfile.SYMTYPE
                        info.linkname = "/outside"
                        archive.addfile(info)
                    else:
                        data = self.contents["README.md"]
                        info.size = len(data)
                        archive.addfile(info, io.BytesIO(data))
                        if kind == "duplicate":
                            archive.addfile(info, io.BytesIO(data))
                with self.assertRaisesRegex(ValueError, "Duplicate|Linked|Unsafe"):
                    checker.check_core_sdist(path, self.root)

    def test_sdist_rejects_venv_leak_and_source_changes(self) -> None:
        with self.assertRaisesRegex(ValueError, "unexpected"):
            checker.check_core_sdist(
                self.sdist(extra=".venv-alt/private.txt"), self.root
            )
        path = self.sdist()
        (self.root / "README.md").write_bytes(b"Changed package\n")
        with self.assertRaisesRegex(ValueError, "bytes differ"):
            checker.check_core_sdist(path, self.root)

    def test_unexpected_sdist_member_has_a_bounded_name_diagnostic(self) -> None:
        for name in (".parent-ignore", "docs/release/" + "x" * 1024):
            with self.subTest(name=name), self.assertRaises(ValueError) as failure:
                checker.check_core_sdist(self.sdist(extra=name), self.root)
            self.assertEqual(
                str(failure.exception),
                "Source archive contains unexpected file: " + repr(name)[:256],
            )

    def test_sdist_requires_both_exact_empty_build_controls(self) -> None:
        for name in (".gitignore", ".hgignore"):
            for value in (None, b"PRIVATE_ANCESTOR_CONTENT"):
                controls = {".gitignore": b"", ".hgignore": b""}
                if value is None:
                    del controls[name]
                else:
                    controls[name] = value
                with (
                    self.subTest(name=name, value=value),
                    self.assertRaises(ValueError),
                ):
                    checker.check_core_sdist(self.sdist(controls=controls), self.root)

    def test_untracked_private_files_are_not_build_inputs(self) -> None:
        path = self.root / "src/graphql_desk/private_user.py"
        path.write_bytes(b"PRIVATE_USER_CONTENT")
        self.assertNotIn(
            "src/graphql_desk/private_user.py", checker.source_inputs(self.root)
        )

    def test_missing_explicit_notice_selection_is_not_silently_ignored(self) -> None:
        path = self.root / "pyproject.toml"
        data = path.read_bytes().replace(
            b'"docs/release/third-party-notices.md"', b'"docs/release/absent.md"'
        )
        path.write_bytes(data)
        with self.assertRaisesRegex(ValueError, "selection is missing"):
            checker.check_core_wheel(self.wheel(), self.root)

    def test_sdist_requires_the_root_grant_and_matching_mit_metadata(self) -> None:
        original = self.contents["LICENSE"]
        for replacement in (None, b"license changed"):
            if replacement is None:
                del self.contents["LICENSE"]
            else:
                self.contents["LICENSE"] = replacement
            path = self.sdist()
            self.contents["LICENSE"] = original
            with self.subTest(replacement=replacement), self.assertRaises(ValueError):
                checker.check_core_sdist(path, self.root)
        for metadata in (
            self.metadata.replace(b"License-Expression: MIT\n", b""),
            self.metadata.replace(
                b"License-Expression: MIT", b"License-Expression: ISC"
            ),
        ):
            with patch.object(self, "metadata", metadata):
                path = self.sdist()
            with self.assertRaisesRegex(ValueError, "License-Expression"):
                checker.check_core_sdist(path, self.root)

    def test_project_license_policy_requires_mit_and_the_root_license_file(
        self,
    ) -> None:
        path = self.root / "pyproject.toml"
        original = path.read_bytes()
        for changed in (
            original.replace(b'license="MIT"', b'license="ISC"'),
            original.replace(b'license-files=["LICENSE",', b"license-files=["),
        ):
            path.write_bytes(changed)
            with self.assertRaisesRegex(ValueError, "MIT license selection"):
                checker.check_core_wheel(self.wheel(), self.root)

    def test_task_directory_cleans_success_and_failure_inside_repository(self) -> None:
        for failure in (False, True):
            work = None
            try:
                with checker.task_directory(self.root) as work:
                    self.assertTrue(work.is_relative_to(self.root))
                    (work / "owned.txt").write_bytes(b"owned")
                    if failure:
                        raise RuntimeError("fixture failure")
            except RuntimeError:
                self.assertTrue(failure)
            assert work is not None
            self.assertFalse(work.exists())

    def test_changed_task_identity_is_preserved_without_cleanup(self) -> None:
        with patch("scripts.check_distributions.shutil.rmtree") as remove:
            changed = None
            try:
                with self.assertRaisesRegex(ValueError, "changed; preserved"):
                    with checker.task_directory(self.root) as work:
                        value = work.stat()
                        changed = patch.object(
                            Path,
                            "stat",
                            return_value=SimpleNamespace(
                                st_dev=value.st_dev,
                                st_ino=value.st_ino + 1,
                                st_mode=stat.S_IFDIR,
                                st_file_attributes=0,
                            ),
                        )
                        changed.start()
            finally:
                if changed is not None:
                    changed.stop()
            remove.assert_not_called()

    def test_paths_refuse_escape_and_reparse_points_before_tool_calls(self) -> None:
        for path in (
            Path("relative"),
            self.root.parent / "outside",
            self.root / ".." / "outside",
        ):
            with self.subTest(path=path), self.assertRaisesRegex(ValueError, "inside"):
                checker.check_repository_path(self.root, path)
        with patch.object(
            Path,
            "lstat",
            return_value=SimpleNamespace(
                st_mode=stat.S_IFDIR,
                st_file_attributes=0x400,
            ),
        ):
            with self.assertRaisesRegex(ValueError, "reparse"):
                checker.check_repository_path(self.root, self.root / "cache")

    def test_pipeline_uses_offline_commands_existing_cache_and_clean_sources(
        self,
    ) -> None:
        cache = self.root / "cache"
        cache.mkdir()
        stale = self.root / "runtime-requirements.txt"
        stale.write_text("UNREVIEWED_DEV_EXPORT\n")
        (self.root / "src/graphql_desk/private_user.py").write_text("PRIVATE")
        calls: list[tuple[tuple[str, ...], dict[str, str]]] = []
        exported: list[Path] = []

        def official(*arguments: str, cwd: Path, environment: dict[str, str]) -> None:
            calls.append((arguments, environment))
            self.assertEqual(environment["UV_CACHE_DIR"], str(cache))
            self.assertNotIn("PYTHONPATH", environment)
            self.assertNotIn("PYTHONHOME", environment)
            self.assertTrue(
                Path(environment["TEMP"]).is_relative_to(self.root / ".tmp")
            )
            if arguments[0] == "uv":
                self.assertIn("--offline", arguments)
            if arguments[:2] == ("uv", "export"):
                self.assertEqual(cwd, self.root)
                for flag in ("--locked", "--no-dev", "--no-emit-project"):
                    self.assertIn(flag, arguments)
                requirements = Path(arguments[arguments.index("--output-file") + 1])
                self.assertTrue(requirements.is_relative_to(Path(environment["TEMP"])))
                self.assertFalse(requirements.exists())
                requirements.write_text("LOCKED_RUNTIME_HASHES\n")
                exported.append(requirements)
            if "-r" in arguments:
                self.assertEqual(len(exported), 1)
                self.assertIn("--no-deps", arguments)
                self.assertIn("--require-hashes", arguments)
                self.assertEqual(arguments[arguments.index("-r") + 1], str(exported[0]))
                self.assertEqual(exported[0].read_text(), "LOCKED_RUNTIME_HASHES\n")
            if arguments[:2] == ("uv", "build"):
                self.assertEqual(
                    sum(flag in arguments for flag in ("--sdist", "--wheel")),
                    1,
                    "Direct source and wheel builds must be explicit and independent",
                )
                output = Path(arguments[arguments.index("--out-dir") + 1])
                output.mkdir(exist_ok=True)
                if "--sdist" in arguments:
                    source = Path(arguments[arguments.index("--out-dir") - 1])
                    self.assertFalse(
                        (source / "src/graphql_desk/private_user.py").exists()
                    )
                    for control in (".gitignore", ".hgignore"):
                        self.assertEqual((source / control).read_bytes(), b"")
                        self.assertTrue((source / control).is_file())
                        self.assertEqual(
                            locate_file(str(source), control), str(source / control)
                        )
                    self.sdist(output / "graphql_desk-0.1.0.tar.gz")
                else:
                    self.wheel(target=output / "graphql_desk-0.1.0-py3-none-any.whl")
            self.assertTrue(cwd.is_relative_to(self.root))

        with (
            patch(
                "scripts.check_distributions.__file__",
                str(self.root / "scripts/check_distributions.py"),
            ),
            patch.dict(
                os.environ,
                {
                    "UV_CACHE_DIR": str(cache),
                    "PYTHONPATH": "PRIVATE",
                    "PYTHONHOME": "PRIVATE",
                },
            ),
            patch("scripts.check_distributions.run", side_effect=official),
        ):
            checker.main()
        self.assertEqual(
            sum(args[:3] == ("uv", "pip", "install") for args, _ in calls), 4
        )
        self.assertEqual(
            sum(args[:3] == ("uv", "pip", "check") for args, _ in calls), 2
        )
        self.assertEqual(
            sum(
                Path(args[0]).name in {"graphql-desk.exe", "graphql-desk"}
                for args, _ in calls
            ),
            2,
        )
        self.assertEqual(list((self.root / ".tmp").iterdir()), [])
        self.assertTrue((self.root / "src/graphql_desk/private_user.py").exists())
        self.assertEqual(stale.read_text(), "UNREVIEWED_DEV_EXPORT\n")
        self.assertEqual(len(exported), 1)
        self.assertFalse(
            exported[0].exists(), "Private export is cleaned with its task"
        )
        builds = [args for args, _ in calls if args[:2] == ("uv", "build")]
        self.assertEqual(len(builds), 3)
        inputs = [Path(args[args.index("--out-dir") - 1]) for args in builds]
        self.assertIn("--sdist", builds[0])
        self.assertIn("--wheel", builds[1])
        self.assertIn("--wheel", builds[2])
        self.assertEqual(inputs[0], inputs[1], "The first wheel uses the staged source")
        self.assertEqual(inputs[2].name, "graphql_desk-0.1.0.tar.gz")

    def test_missing_or_external_cache_refuses_before_build_or_install(self) -> None:
        for cache in ("", str(self.root.parent / "outside"), str(self.root / "absent")):
            with (
                self.subTest(cache=cache),
                patch(
                    "scripts.check_distributions.__file__",
                    str(self.root / "scripts/check_distributions.py"),
                ),
                patch.dict(os.environ, {"UV_CACHE_DIR": cache}),
                patch("scripts.check_distributions.run") as run,
                self.assertRaises(ValueError),
            ):
                checker.main()
            run.assert_not_called()
        self.assertFalse((self.root / ".tmp").exists())


if __name__ == "__main__":
    unittest.main()
