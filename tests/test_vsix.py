"""The verified extension archive rejects any bytes outside its locked payload."""

import json
import stat
import subprocess
import unittest
import warnings
import zipfile
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from scripts.check_distributions import task_directory
from scripts.check_vsix import INVENTORY, check_archive, stage_inputs


class VsixTests(unittest.TestCase):
    def setUp(self) -> None:
        cleanup = ExitStack()
        self.addCleanup(cleanup.close)
        root = Path(__file__).resolve().parents[1]
        self.archive = cleanup.enter_context(task_directory(root)) / "test.vsix"
        self.payload = {
            "package.json": json.dumps(
                {
                    "publisher": "graphql-desk",
                    "name": "graphql-desk-editor",
                    "version": "0.1.0",
                    "engines": {"vscode": "^1.141.0"},
                }
            ).encode(),
            "LICENSE.txt": b"Exact project grant\n",
            "src/extension.js": b"exports.activate = () => {};\n",
            "node_modules/example/LICENSE": b"Exact runtime notice\n",
        }
        self.readme = b"# GraphQL Desk\n"
        self.metadata = {
            "extension/readme.md": self.readme,
            "[Content_Types].xml": b"<Types/>",
            "extension.vsixmanifest": (
                b'<PackageManifest><Metadata><Identity Publisher="graphql-desk" '
                b'Id="graphql-desk-editor" Version="0.1.0"/><Properties>'
                b'<Property Id="Microsoft.VisualStudio.Code.Engine" Value="^1.141.0"/>'
                b"</Properties></Metadata></PackageManifest>"
            ),
        }

    def write_archive(
        self,
        *,
        omit: str = "",
        extra: tuple[str | zipfile.ZipInfo, bytes] | None = None,
        replace: tuple[str, bytes] | None = None,
    ) -> None:
        members = {f"extension/{name}": data for name, data in self.payload.items()}
        members.update(self.metadata)
        members.pop(omit, None)
        if replace is not None:
            members[replace[0]] = replace[1]
        with zipfile.ZipFile(self.archive, "w") as archive:
            for name, data in members.items():
                archive.writestr(name, data)
            if extra is not None:
                with warnings.catch_warnings():
                    warnings.filterwarnings("ignore", message="Duplicate name:")
                    archive.writestr(*extra)

    def verify(self) -> None:
        check_archive(self.archive, self.payload, self.readme)

    def test_verified_archive_matches_locked_payload(self) -> None:
        self.write_archive()
        self.verify()

    def test_rejects_foreign_paths_and_case_collisions(self) -> None:
        for name in (
            "extension/../other",
            "extension/.vscode-test/profile",
            "extension/node_modules/other/package.json",
            "EXTENSION/LICENSE.txt",
            "extension/LICENSE.txt",
        ):
            with self.subTest(name=name):
                self.write_archive(extra=(name, b"unreviewed"))
                with self.assertRaises(ValueError):
                    self.verify()

    def test_rejects_linked_members_and_missing_runtime_files(self) -> None:
        linked = zipfile.ZipInfo("extension/src/extension.js")
        linked.create_system = 3
        linked.external_attr = (stat.S_IFLNK | 0o777) << 16
        self.write_archive(omit=linked.filename, extra=(linked, b"outside"))
        with self.assertRaisesRegex(ValueError, "linked"):
            self.verify()
        self.write_archive(omit="extension/node_modules/example/LICENSE")
        with self.assertRaisesRegex(ValueError, "missing"):
            self.verify()

    def test_rejects_changed_notices_and_wrong_metadata(self) -> None:
        self.write_archive(
            replace=("extension/LICENSE.txt", b"Other project grant\n"),
        )
        with self.assertRaisesRegex(ValueError, "bytes differ"):
            self.verify()
        name = "extension.vsixmanifest"
        wrong = self.metadata[name].replace(b"^1.141.0", b"^1.142.0")
        self.write_archive(replace=(name, wrong))
        with self.assertRaisesRegex(ValueError, "identity"):
            self.verify()
        self.write_archive(replace=(name, b"x" * 65537))
        with self.assertRaisesRegex(ValueError, "oversized"):
            self.verify()

    def test_rejects_same_size_readme_replacement(self) -> None:
        self.write_archive(replace=("extension/readme.md", b"# Other Window\n"))
        self.assertEqual(len(self.readme), len(b"# Other Window\n"))
        with self.assertRaisesRegex(ValueError, "bytes differ.*readme"):
            self.verify()

    def test_rejects_shipped_inventory_from_an_older_index(self) -> None:
        root = Path(__file__).resolve().parents[1]
        index_blob = subprocess.check_output(
            ["git", "rev-parse", f":{INVENTORY}"], cwd=root, text=True
        ).strip()
        execute = subprocess.check_output
        for runtime, label in (("node", "Node"), ("python", "Python")):
            with self.subTest(runtime=runtime), task_directory(root) as stage:
                stale = json.loads((root / INVENTORY).read_bytes())
                stale[runtime][0]["version"] = "0.0.0"

                def index_read(command: list[str], *, cwd: Path) -> bytes:
                    if command == ["git", "cat-file", "blob", index_blob]:
                        return json.dumps(stale).encode()
                    return execute(command, cwd=cwd)

                with (
                    patch("scripts.check_vsix.subprocess.check_output", index_read),
                    self.assertRaisesRegex(ValueError, f"{label} runtime lock review"),
                ):
                    stage_inputs(root, stage)
