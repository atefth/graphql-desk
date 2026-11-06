"""The runtime license allowlist must fail closed."""

import json
import unittest
from importlib.metadata import Distribution, distribution
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from scripts.check_runtime_licenses import check_release_inventory, runtime_inventory


class RuntimeLicenseTests(unittest.TestCase):
    def inventory(self, dependency: SimpleNamespace) -> list[dict[str, str]]:
        packages = {
            "graphql-desk": SimpleNamespace(
                requires=["graphql-core==3.3.0", 'ignored; extra == "optional"']
            ),
            "graphql-core": dependency,
        }
        with patch("scripts.check_runtime_licenses.distribution", packages.__getitem__):
            return runtime_inventory("graphql-desk")

    def test_approved_license_and_optional_dependency(self) -> None:
        self.assertEqual(
            self.inventory(
                SimpleNamespace(
                    requires=[], version="3.3.0", metadata={"License-Expression": "MIT"}
                )
            ),
            [{"name": "graphql-core", "version": "3.3.0", "license": "MIT"}],
        )

    def test_missing_or_changed_license_requires_review(self) -> None:
        for metadata in ({}, {"License": "GPL-3.0-only"}):
            with self.subTest(metadata=metadata), self.assertRaises(ValueError):
                self.inventory(
                    SimpleNamespace(requires=[], version="3.3.0", metadata=metadata)
                )

    def test_tomlkit_mit_license_is_approved(self) -> None:
        packages = {
            "graphql-desk": SimpleNamespace(requires=["tomlkit==0.15.2"]),
            "tomlkit": SimpleNamespace(
                requires=[], version="0.15.2", metadata={"License-Expression": "MIT"}
            ),
        }
        with patch("scripts.check_runtime_licenses.distribution", packages.__getitem__):
            self.assertEqual(
                runtime_inventory("graphql-desk"),
                [{"name": "tomlkit", "version": "0.15.2", "license": "MIT"}],
            )

    def test_tomli_is_a_runtime_dependency_only_before_python_311(self) -> None:
        packages = {
            "graphql-desk": SimpleNamespace(
                requires=["tomli==2.5.0; python_full_version < '3.11'"]
            ),
            "tomli": SimpleNamespace(
                requires=[], version="2.5.0", metadata={"License-Expression": "MIT"}
            ),
        }
        for version, required in (("3.10.99", True), ("3.13.7", False)):
            with (
                self.subTest(version=version),
                patch(
                    "scripts.check_runtime_licenses.distribution", packages.__getitem__
                ),
                patch(
                    "packaging.markers.default_environment",
                    return_value={"python_full_version": version},
                ),
            ):
                self.assertEqual(
                    runtime_inventory("graphql-desk"),
                    [{"name": "tomli", "version": "2.5.0", "license": "MIT"}]
                    if required
                    else [],
                )


class ReleaseInventoryTests(unittest.TestCase):
    root = Path(__file__).resolve().parents[1]

    def test_reviewed_runtime_locks_and_preserved_notices_match(self) -> None:
        check_release_inventory(self.root)

    def test_runtime_lock_drift_fails_but_development_drift_is_outside_scope(
        self,
    ) -> None:
        read = Path.read_text
        target = self.root / "editor/package-lock.json"
        for development in (False, True):
            with self.subTest(development=development):

                def changed(path: Path, *args: Any, **kwargs: Any) -> str:
                    text = read(path, *args, **kwargs)
                    if path == target:
                        data = json.loads(text)
                        name = (
                            "node_modules/@vscode/test-electron"
                            if development
                            else "node_modules/graphql"
                        )
                        data["packages"][name]["version"] = "999.0.0"
                        return json.dumps(data)
                    return text

                with patch.object(Path, "read_text", changed):
                    if development:
                        check_release_inventory(self.root)
                    else:
                        with self.assertRaisesRegex(ValueError, "Node runtime lock"):
                            check_release_inventory(self.root)

    def test_notice_tampering_fails_before_distribution(self) -> None:
        manifest = json.loads(
            (self.root / "docs/release/runtime-dependencies.json").read_text()
        )
        target = self.root / manifest["python"][0]["license_files"][0]["notice_path"]
        read = Path.read_bytes

        def changed(path: Path) -> bytes:
            return b"missing required copyright" if path == target else read(path)

        with patch.object(Path, "read_bytes", changed):
            with self.assertRaisesRegex(ValueError, "Preserved notice drift"):
                check_release_inventory(self.root)

    def test_reviewed_tomli_newlines_pass_but_changed_copyright_fails(self) -> None:
        manifest = json.loads(
            (self.root / "docs/release/runtime-dependencies.json").read_text()
        )
        tomli = next(item for item in manifest["python"] if item["name"] == "tomli")
        raw = (self.root / tomli["license_files"][0]["notice_path"]).read_bytes()
        lf = raw.replace(b"\r\n", b"\n")
        fake = self.root / "unused-installed-tomli-LICENSE"

        def locate(_path: str) -> Path:
            return fake

        installed = SimpleNamespace(
            version="2.5.0",
            metadata={"License-Expression": "MIT"},
            locate_file=locate,
        )

        def package(name: str) -> Distribution | SimpleNamespace:
            return installed if name == "tomli" else distribution(name)

        read = Path.read_bytes

        for data, accepted in (
            (raw, True),
            (lf, True),
            (lf.replace(b"2021 Taneli Hukkinen", b"2021 Someone Else"), False),
        ):
            with self.subTest(accepted=accepted, data=data[:30]):

                def read_notice(path: Path) -> bytes:
                    return data if path == fake else read(path)

                with (
                    patch.object(Path, "read_bytes", read_notice),
                    patch(
                        "scripts.check_runtime_licenses.distribution",
                        side_effect=package,
                    ),
                    patch(
                        "packaging.markers.default_environment",
                        return_value={"python_full_version": "3.10.18"},
                    ),
                ):
                    if accepted:
                        check_release_inventory(self.root)
                    else:
                        with self.assertRaisesRegex(
                            ValueError, "Installed Python notice drift: tomli"
                        ):
                            check_release_inventory(self.root)

    def test_new_runtime_package_needs_review(self) -> None:
        read = Path.read_text
        target = self.root / "editor/package-lock.json"

        def changed(path: Path, *args: Any, **kwargs: Any) -> str:
            text = read(path, *args, **kwargs)
            if path == target:
                data = json.loads(text)
                data["packages"]["node_modules/new-runtime"] = {"version": "1.0.0"}
                return json.dumps(data)
            return text

        with patch.object(Path, "read_text", changed):
            with self.assertRaisesRegex(ValueError, "Node runtime inventory drift"):
                check_release_inventory(self.root)


if __name__ == "__main__":
    unittest.main()
