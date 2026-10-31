"""Project checking reuses static source and schema analysis without imports."""

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from graphql_desk.cli import CheckDiagnostic, check, main


class CheckCLITests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.config = self.root / "pyproject.toml"
        self.schema = self.root / "schema.graphql"
        self.schema.write_text("type Query { title: String }", encoding="utf-8")
        self.config.write_text(
            '[tool.graphql-desk]\nschema="schema.graphql"\n'
            'schema_output="graphql_desk_generated/default.pyi"\n',
            encoding="utf-8",
        )

    def invoke(
        self, argv: list[str], *, github_actions: bool = False
    ) -> tuple[int, str, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, {"GITHUB_WORKSPACE": str(self.root)}):
            if github_actions:
                os.environ["GITHUB_ACTIONS"] = "true"
            else:
                os.environ.pop("GITHUB_ACTIONS", None)
            with patch.object(Path, "cwd", return_value=self.root):
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    code = main(argv)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_valid_document_and_import_inert_project(self) -> None:
        (self.root / "operation.py").write_text(
            "raise RuntimeError('PROJECT_MUST_NOT_EXECUTE')\n"
            "from typing import TypeVar\n"
            "from graphql_desk import TypedDocument, graphql\n"
            "R = TypeVar('R')\nV = TypeVar('V')\n"
            "def execute(document: TypedDocument[R, V], variables: V) -> R: ...\n"
            'query = graphql("query { title }")\n'
            "result = execute(query, {})\nprint(result.title)\n",
            encoding="utf-8",
        )

        result = check(config=self.config)
        self.assertEqual(result, ())
        code, stdout, stderr = self.invoke(["check"])
        self.assertEqual((code, stdout, stderr), (0, "", ""))

    def test_invalid_graphql_is_error_on_stderr(self) -> None:
        (self.root / "operation.py").write_text(
            'from graphql_desk import graphql\nquery = graphql("query { absent }")\n',
            encoding="utf-8",
        )

        result = check(config=self.config)
        self.assertEqual([item.code for item in result], ["graphql-validation"])
        self.assertEqual([item.severity for item in result], ["error"])
        code, stdout, stderr = self.invoke(["check"])
        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("error [graphql-validation]", stderr)
        self.assertIn("operation:2:", stderr)

    def test_utf8_bom_does_not_hide_invalid_graphql(self) -> None:
        source = (
            'from graphql_desk import graphql\nquery = graphql("query { absent }")\n'
        )
        (self.root / "operation.py").write_bytes(
            b"\xef\xbb\xbf" + source.encode("utf-8")
        )

        diagnostics = check(config=self.config)
        self.assertEqual([item.code for item in diagnostics], ["graphql-validation"])
        self.assertEqual([item.severity for item in diagnostics], ["error"])
        code, stdout, stderr = self.invoke(["check"])
        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("error [graphql-validation]", stderr)

    def test_deprecated_fields_are_warnings(self) -> None:
        self.schema.write_text(
            'type Query { old: String @deprecated(reason: "Use current") '
            "current: String }",
            encoding="utf-8",
        )
        (self.root / "operation.py").write_text(
            'from graphql_desk import graphql\nquery = graphql("query { old }")\n',
            encoding="utf-8",
        )

        diagnostics = check(config=self.config)
        self.assertEqual([item.code for item in diagnostics], ["graphql-deprecation"])
        self.assertEqual([item.severity for item in diagnostics], ["warn"])
        self.assertIn("Use current", diagnostics[0].message)

    def test_warning_visibility_does_not_change_failure_policy(self) -> None:
        (self.root / "operation.py").write_text(
            "from graphql_desk import graphql\nquery = graphql(build_query())\n",
            encoding="utf-8",
        )

        result = check(config=self.config)
        self.assertTrue(result)
        self.assertEqual({item.severity for item in result}, {"warn"})
        code, stdout, stderr = self.invoke(["check", "--level", "error"])
        self.assertEqual((code, stdout, stderr), (0, "", ""))
        code, stdout, stderr = self.invoke(
            ["check", "--level", "error", "--fail-on-warn"]
        )
        self.assertEqual((code, stdout, stderr), (1, "", ""))
        code, stdout, stderr = self.invoke(["check", "-l", "warn"])
        self.assertEqual(code, 0)
        self.assertEqual(stdout, "")
        self.assertIn("warn [source-not-static]", stderr)

    def test_field_usage_warning_honors_project_configuration(self) -> None:
        self.schema.write_text(
            "type User { id: ID! name: String email: String } "
            "type Query { user: User }",
            encoding="utf-8",
        )
        (self.root / "operation.py").write_text(
            "from typing import TypeVar\n"
            "from graphql_desk import TypedDocument, gql\n"
            "R = TypeVar('R')\nV = TypeVar('V')\n"
            "def execute(document: TypedDocument[R, V], variables: V) -> R: ...\n"
            'query = gql("query { user { id name email } }")\n'
            "result = execute(query, {})\nprint(result.user.name)\n",
            encoding="utf-8",
        )

        diagnostics = check(config=self.config)
        self.assertIn("52005", [item.code for item in diagnostics])
        self.assertEqual({item.severity for item in diagnostics}, {"warn"})
        code, stdout, stderr = self.invoke(["check"])
        self.assertEqual(code, 0)
        self.assertEqual(stdout, "")
        self.assertIn("warn [52005]", stderr)

        self.config.write_text(
            '[tool.graphql-desk]\nschema="schema.graphql"\n'
            'schema_output="graphql_desk_generated/default.pyi"\n'
            "track_field_usage=false\n",
            encoding="utf-8",
        )
        self.assertEqual(check(config=self.config), ())

    def test_named_schema_selection_and_unknown_schema(self) -> None:
        (self.root / "second.graphql").write_text(
            "type Query { name: String }", encoding="utf-8"
        )
        self.config.write_text(
            '[tool.graphql-desk]\ngenerated_package="graphql_desk_generated"\n'
            "track_field_usage=false\n"
            '[[tool.graphql-desk.schemas]]\nname="first"\nschema="schema.graphql"\n'
            'schema_output="graphql_desk_generated/first.pyi"\n'
            '[[tool.graphql-desk.schemas]]\nname="second"\nschema="second.graphql"\n'
            'schema_output="graphql_desk_generated/second.pyi"\n',
            encoding="utf-8",
        )
        (self.root / "operation.py").write_text(
            "from graphql_desk_generated.first import graphql as first\n"
            "from graphql_desk_generated.second import graphql as second\n"
            'bad = first("query { absent }")\n'
            'good = second("query { name }")\n',
            encoding="utf-8",
        )

        result = check(config=self.config, schema="second")
        self.assertEqual(result, ())
        result = check(config=self.config, schema="missing")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].severity, "error")
        self.assertEqual(result[0].code, "schema-selection-failed")
        code, stdout, stderr = self.invoke(["check", "--schema", "second"])
        self.assertEqual((code, stdout, stderr), (0, "", ""))
        code, stdout, stderr = self.invoke(
            ["check", "--schema", "PRIVATE_SCHEMA_MARKER"]
        )
        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertNotIn("PRIVATE_SCHEMA_MARKER", stderr)

    def test_schema_load_failure_is_sanitized(self) -> None:
        self.schema.write_text("PRIVATE_SCHEMA_MARKER {", encoding="utf-8")

        diagnostics = check(config=self.config)
        self.assertEqual([item.code for item in diagnostics], ["schema-unavailable"])
        self.assertEqual([item.severity for item in diagnostics], ["error"])
        with patch.dict(os.environ, {"GITHUB_ACTIONS": "true"}):
            code, stdout, stderr = self.invoke(["check"], github_actions=True)
        self.assertEqual(code, 1)
        self.assertEqual(
            stdout, "::error::Schema contents or access could not be checked.\n"
        )
        self.assertNotIn("PRIVATE_SCHEMA_MARKER", stderr + stdout)
        self.assertIn("schema default: error [schema-unavailable]", stderr)

    def test_github_annotation_escaping_and_severity(self) -> None:
        diagnostics = (
            CheckDiagnostic(
                "warn",
                "52005",
                "100% done\r\nnext: value,other",
                file="nested/a:b,c%.py",
                line=2,
                column=3,
                end_line=4,
                end_column=5,
            ),
            CheckDiagnostic("info", "notice", "ready"),
        )
        with patch("graphql_desk.cli.check", return_value=diagnostics):
            with patch.dict(os.environ, {"GITHUB_ACTIONS": "true"}):
                code, stdout, _ = self.invoke(
                    ["check", "--level", "info"], github_actions=True
                )
        self.assertEqual(code, 0)
        self.assertEqual(
            stdout,
            "::warning file=nested/a%3Ab%2Cc%25.py,line=2,col=3,endLine=4,"
            "endColumn=5::100%25 done%0D%0Anext: value,other\n"
            "::notice::ready\n",
        )

    def test_human_github_check_emits_annotations_on_stdout(self) -> None:
        (self.root / "operation.py").write_text(
            'from graphql_desk import graphql\nquery = graphql("query { absent }")\n',
            encoding="utf-8",
        )
        with patch.dict(os.environ, {"GITHUB_ACTIONS": "true"}):
            code, stdout, stderr = self.invoke(["check"], github_actions=True)
        self.assertEqual(code, 1)
        self.assertIn(
            "::error file=operation.py,line=2,col=26,endLine=2,endColumn=31::",
            stdout,
        )
        self.assertIn("error [graphql-validation]", stderr)
        self.assertNotIn(str(self.root), stdout)

    def test_external_source_root_diagnostic_is_not_misannotated(self) -> None:
        external = tempfile.TemporaryDirectory(dir=self.root.parent)
        self.addCleanup(external.cleanup)
        source_root = Path(external.name).resolve()
        (source_root / "operation.py").write_text(
            'from graphql_desk import graphql\nquery = graphql("query { absent }")\n',
            encoding="utf-8",
        )
        source_root_setting = os.path.relpath(source_root, self.root).replace("\\", "/")
        self.config.write_text(
            '[tool.graphql-desk]\nschema="schema.graphql"\n'
            'schema_output="graphql_desk_generated/default.pyi"\n'
            f'source_roots=["{source_root_setting}"]\n',
            encoding="utf-8",
        )

        with patch.dict(os.environ, {"GITHUB_ACTIONS": "true"}):
            code, stdout, stderr = self.invoke(
                ["check", "--config", str(self.config)], github_actions=True
            )

        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("error [graphql-validation]", stderr)
        self.assertNotIn(str(source_root), stdout + stderr)

    def test_nested_config_annotations_use_workspace_relative_paths(self) -> None:
        project = self.root / "packages" / "api"
        project.mkdir(parents=True)
        (project / "schema.graphql").write_text(
            "type Query { title: String }", encoding="utf-8"
        )
        config = project / "pyproject.toml"
        config.write_text(
            '[tool.graphql-desk]\nschema="schema.graphql"\n'
            'schema_output="graphql_desk_generated/default.pyi"\n',
            encoding="utf-8",
        )
        (project / "operation.py").write_text(
            'from graphql_desk import graphql\nquery = graphql("query { absent }")\n',
            encoding="utf-8",
        )

        with patch.dict(
            os.environ,
            {"GITHUB_ACTIONS": "true", "GITHUB_WORKSPACE": str(self.root)},
        ):
            code, stdout, stderr = self.invoke(
                ["check", "--config", str(config)], github_actions=True
            )

        self.assertEqual(code, 1)
        self.assertIn("::error file=packages/api/operation.py,line=2,col=26,", stdout)
        self.assertIn("error [graphql-validation]", stderr)
        self.assertNotIn(str(self.root), stdout)

    def test_json_mode_keeps_annotations_separate_and_callable_is_silent(self) -> None:
        (self.root / "operation.py").write_text(
            'from graphql_desk import graphql\nquery = graphql("query { absent }")\n',
            encoding="utf-8",
        )
        with patch.dict(os.environ, {"GITHUB_ACTIONS": "true"}):
            with patch("builtins.print") as output:
                diagnostics = check(config=self.config)
            output.assert_not_called()
            code, stdout, stderr = self.invoke(
                [
                    "check",
                    "--format",
                    "json",
                    "--annotations-output",
                    "annotations.txt",
                ],
                github_actions=True,
            )
        self.assertEqual(code, 1)
        self.assertEqual(stderr, "")
        self.assertNotIn("::error", stdout)
        record = json.loads(stdout)[0]
        self.assertEqual(record["file"], "operation.py")
        self.assertEqual(record["line"], 2)
        self.assertEqual(record["column"], 26)
        self.assertEqual(record["end_line"], record["line"])
        self.assertEqual(record["end_column"], 31)
        annotations = (self.root / "annotations.txt").read_text(encoding="utf-8")
        self.assertIn("::error file=operation.py,", annotations)
        self.assertEqual(len(diagnostics), 1)
        self.assertNotIn(str(self.root), stdout + annotations)

    def test_github_annotations_follow_display_level_but_not_failure_policy(
        self,
    ) -> None:
        diagnostics = (
            CheckDiagnostic("warn", "warning", "hidden warning"),
            CheckDiagnostic("error", "error", "visible error"),
        )
        with patch("graphql_desk.cli.check", return_value=diagnostics):
            with patch.dict(os.environ, {"GITHUB_ACTIONS": "true"}):
                code, stdout, stderr = self.invoke(
                    ["check", "--level", "error", "--fail-on-warn"],
                    github_actions=True,
                )
        self.assertEqual(code, 1)
        self.assertEqual(stdout, "::error::visible error\n")
        self.assertNotIn("hidden warning", stderr + stdout)
        self.assertIn("visible error", stderr)


if __name__ == "__main__":
    unittest.main()
