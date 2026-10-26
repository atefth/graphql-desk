"""Source-free release projection ownership and wheel checks."""

import json
import tempfile
import unittest
import zipfile
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from graphql import build_schema

from graphql_desk.cli import main
from graphql_desk.config import parse_project_config
from graphql_desk.release_artifacts import (
    check_wheel,
    declared_import_roots,
    publish_omitted_tree,
    render_omitted_files,
    validate_authoring_separation,
)
from graphql_desk.source_selection import SourceFile


class ReleaseArtifactTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        # Match ProjectConfig.root, which is canonicalized before output paths are built.
        self.root = Path(temporary.name).resolve()
        config = parse_project_config(
            '[tool.graphql-desk]\nschema="schema.graphql"\n'
            'schema_output="graphql_desk_generated/default.pyi"\n',
            config_path=self.root / "pyproject.toml",
        )
        assert config is not None
        self.config = config

    @staticmethod
    def metadata(extra: str = "") -> str:
        return (
            "Metadata-Version: 2.1\nName: app\nVersion: 1.0\n"
            "Requires-Dist: graphql-desk\nRequires-Dist: graphql-core\n"
            f"{extra}\n"
        )

    def files(self) -> dict[str, str]:
        return render_omitted_files(
            self.config,
            {"default": ("saved = persisted_id('id')\n", "saved: object\n")},
            schemas={"default": build_schema("type Query { title: String }")},
        )

    def test_projection_has_generic_parser_and_source_free_persisted_package(
        self,
    ) -> None:
        files = self.files()

        self.assertIn(
            "def parse(source: str | _Source", files["graphql_desk-stubs/__init__.pyi"]
        )
        self.assertNotIn("Literal[", files["graphql_desk-stubs/__init__.pyi"])
        self.assertIn(
            "read_fragment_passthrough", files["graphql_desk-stubs/__init__.pyi"]
        )
        self.assertIn("unsafe_read_result", files["graphql_desk-stubs/__init__.pyi"])
        self.assertNotIn("query Q", "\n".join(files.values()))
        self.assertIn(
            "def __call__(self, source: str",
            files["graphql_desk_generated/default.pyi"],
        )
        self.assertIn(
            "Literal as __graphql_desk_Literal",
            files["graphql_desk_generated/default.pyi"],
        )
        self.assertIn(
            "overload as __graphql_desk_overload",
            files["graphql_desk_generated/default.pyi"],
        )
        self.assertIn(
            "saved = persisted_id('id')",
            files["graphql_desk_generated/persisted/default.py"],
        )

    def test_wheel_must_match_projection_and_contain_no_authoring_source(self) -> None:
        destination = self.root / "build" / "graphql-desk" / "omitted"
        files = self.files()
        publish_omitted_tree(destination, files, ("queries",), "graphql_desk_generated")
        wheel = self.root / "app.whl"

        with zipfile.ZipFile(wheel, "w") as archive:
            for relative, content in files.items():
                archive.writestr(relative, content)
            archive.writestr(
                "app.py", "from graphql_desk_generated.persisted.default import saved\n"
            )
            archive.writestr("app-1.0.dist-info/METADATA", self.metadata())
        check_wheel(wheel, destination / "manifest.json")

        with zipfile.ZipFile(wheel, "a") as archive:
            archive.writestr("graphql_desk_generated/queries.py", "query Q { title }\n")
        with self.assertRaisesRegex(ValueError, "differs"):
            check_wheel(wheel, destination / "manifest.json")

        anonymous_wheel = self.root / "anonymous.whl"
        with zipfile.ZipFile(anonymous_wheel, "w") as archive:
            for relative, content in files.items():
                archive.writestr(relative, content)
            archive.writestr(
                "app.py", "DOCUMENT = '''# query comment\nquery { title }'''\n"
            )
            archive.writestr("app-1.0.dist-info/METADATA", self.metadata())
        with self.assertRaisesRegex(ValueError, "document source"):
            check_wheel(anonymous_wheel, destination / "manifest.json")

        dynamic_wheel = self.root / "dynamic.whl"
        with zipfile.ZipFile(dynamic_wheel, "w") as archive:
            for relative, content in files.items():
                archive.writestr(relative, content)
            archive.writestr(
                "app.py",
                "def load_query():\n"
                "    from builtins import __import__ as load\n"
                "    return load('queries.ops')\n",
            )
            archive.writestr("app-1.0.dist-info/METADATA", self.metadata())
        with self.assertRaisesRegex(ValueError, "dynamic import"):
            check_wheel(dynamic_wheel, destination / "manifest.json")

        unresolved_wheel = self.root / "unresolved.whl"
        unresolved_files = {
            **files,
            "graphql_desk_generated/default.pyi": (
                "import not_a_dependency\n"
                + files["graphql_desk_generated/default.pyi"]
            ),
        }
        unresolved_destination = self.root / "unresolved-projection"
        publish_omitted_tree(
            unresolved_destination,
            unresolved_files,
            ("queries",),
            "graphql_desk_generated",
        )
        with zipfile.ZipFile(unresolved_wheel, "w") as archive:
            for relative, content in unresolved_files.items():
                archive.writestr(relative, content)
            archive.writestr("app-1.0.dist-info/METADATA", self.metadata())
        with self.assertRaisesRegex(ValueError, "unresolved import"):
            check_wheel(unresolved_wheel, unresolved_destination / "manifest.json")

        conditional_wheel = self.root / "conditional.whl"
        with zipfile.ZipFile(conditional_wheel, "w") as archive:
            for relative, content in files.items():
                archive.writestr(relative, content)
            archive.writestr(
                "app-1.0.dist-info/METADATA",
                self.metadata().replace(
                    "Requires-Dist: graphql-desk\n",
                    "Requires-Dist: graphql-desk; extra == 'typing'\n",
                ),
            )
        with self.assertRaisesRegex(ValueError, "unconditionally require GraphQL Desk"):
            check_wheel(conditional_wheel, destination / "manifest.json")

    def test_declared_distribution_import_aliases_are_recognized(self) -> None:
        roots = declared_import_roots(
            ["PyYAML >= 6", "python-dateutil >= 2.9"],
            {"yaml": ["PyYAML"], "dateutil": ["python-dateutil"]},
        )
        self.assertIn("yaml", roots)
        self.assertIn("dateutil", roots)

    def test_modified_owned_output_and_unowned_tree_are_preserved(self) -> None:
        destination = self.root / "build" / "graphql-desk" / "omitted"
        files = self.files()
        publish_omitted_tree(destination, files, ("queries",), "graphql_desk_generated")
        generated = destination / "graphql_desk_generated" / "__init__.py"
        generated.write_text("user edit\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "changed; preserved"):
            publish_omitted_tree(
                destination, files, ("queries",), "graphql_desk_generated"
            )
        self.assertEqual(generated.read_text(encoding="utf-8"), "user edit\n")

        unowned = self.root / "unowned"
        unowned.mkdir()
        (unowned / "keep.txt").write_text("keep\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "unowned"):
            publish_omitted_tree(unowned, files, ("queries",), "graphql_desk_generated")
        self.assertTrue((unowned / "keep.txt").exists())
        empty_directory = self.root / "empty"
        (empty_directory / "keep").mkdir(parents=True)
        with self.assertRaisesRegex(ValueError, "unowned"):
            publish_omitted_tree(
                empty_directory, files, ("queries",), "graphql_desk_generated"
            )
        self.assertTrue((empty_directory / "keep").is_dir())

    def test_authoring_module_must_be_separate_and_unimported(self) -> None:
        queries = self.root / "queries"
        queries.mkdir()
        app = self.root / "app.py"
        app.write_text("from queries.operations import query\n", encoding="utf-8")
        operations = queries / "operations.py"
        operations.write_text(
            "query = graphql('query Q { title }')\n", encoding="utf-8"
        )
        selected = (
            SourceFile(app, self.root, "app", False),
            SourceFile(operations, queries, "queries.operations", False),
        )

        with self.assertRaisesRegex(ValueError, "import"):
            validate_authoring_separation(selected, (queries,))

        app.write_text(
            "from graphql_desk_generated.persisted.default import query\n",
            encoding="utf-8",
        )
        self.assertEqual(
            validate_authoring_separation(selected, (queries,)), ("queries.operations",)
        )

    def test_cli_generates_and_checks_omitted_projection(self) -> None:
        queries = self.root / "queries"
        queries.mkdir()
        (queries / "operations.py").write_text(
            "from graphql_desk import graphql\n"
            'query = graphql("query Q { title }")\n'
            'saved = graphql.persisted("id", query)\n',
            encoding="utf-8",
        )
        (self.root / "app.py").write_text(
            "from graphql_desk_generated.persisted.default import saved\n",
            encoding="utf-8",
        )
        (self.root / "schema.graphql").write_text(
            "type Query { title: String }", encoding="utf-8"
        )
        (self.root / "pyproject.toml").write_text(
            '[tool.graphql-desk]\nschema="schema.graphql"\n'
            'schema_output="graphql_desk_generated/default.pyi"\n'
            'persisted_output="manifest.json"\n'
            'authoring_roots=["queries"]\n',
            encoding="utf-8",
        )

        output, errors = StringIO(), StringIO()
        with patch.object(Path, "cwd", return_value=self.root):
            with redirect_stdout(output), redirect_stderr(errors):
                code = main(["generate-persisted", "--omit-runtime-source"])
        self.assertEqual((code, output.getvalue(), errors.getvalue()), (0, "", ""))
        destination = self.root / "build" / "graphql-desk" / "omitted"
        manifest = json.loads(
            (destination / "manifest.json").read_text(encoding="utf-8")
        )
        files = {
            record["path"]: (destination / record["path"]).read_text(encoding="utf-8")
            for record in manifest["files"]
        }
        wheel = self.root / "app.whl"
        with zipfile.ZipFile(wheel, "w") as archive:
            for name, content in files.items():
                archive.writestr(name, content)
            archive.writestr("app-1.0.dist-info/METADATA", self.metadata())
        with patch.object(Path, "cwd", return_value=self.root):
            with redirect_stdout(output), redirect_stderr(errors):
                code = main(["check", "--artifact", str(wheel)])
        self.assertEqual(code, 0)
        self.assertIn("matches the source-free", output.getvalue())
        self.assertEqual(errors.getvalue(), "")

        original_manifest = (self.root / "manifest.json").read_text(encoding="utf-8")
        binding = self.root / "graphql_desk_generated" / "persisted" / "default.py"
        original_binding = binding.read_text(encoding="utf-8")
        (queries / "operations.py").write_text(
            "from graphql_desk import graphql\n"
            'query = graphql("query Q { title }")\n'
            'saved = graphql.persisted("changed-id", query)\n',
            encoding="utf-8",
        )
        (destination / "unowned.txt").write_text("preserve\n", encoding="utf-8")
        output, errors = StringIO(), StringIO()
        with patch.object(Path, "cwd", return_value=self.root):
            with redirect_stdout(output), redirect_stderr(errors):
                code = main(["generate-persisted", "--omit-runtime-source"])
        self.assertEqual(code, 1)
        self.assertEqual(
            (self.root / "manifest.json").read_text(encoding="utf-8"), original_manifest
        )
        self.assertEqual(binding.read_text(encoding="utf-8"), original_binding)
        self.assertTrue((destination / "unowned.txt").exists())


if __name__ == "__main__":
    unittest.main()
