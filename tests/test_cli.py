"""Real CLI diagnostics without project imports, writes or false health claims."""

import io
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from importlib import metadata
from pathlib import Path
from unittest.mock import patch

from graphql_desk.cli import main
from tests.test_schema_url import endpoint


class CLITests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.config = self.root / "pyproject.toml"
        self.schema = self.root / "schema.graphql"
        self.schema.write_text("type Query { title: String }", encoding="utf8")
        self.config.write_text(
            '[tool.graphql-desk]\nschema="schema.graphql"\n'
            'schema_output="graphql_desk_generated/default.pyi"\n',
            encoding="utf8",
        )
        (self.root / "local_types.py").write_text(
            "raise RuntimeError('PROJECT_MUST_NOT_EXECUTE')", encoding="utf8"
        )

    def invoke(self, argv: list[str], cwd: Path | None = None) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with patch.object(Path, "cwd", return_value=cwd or self.root):
            with redirect_stdout(out), redirect_stderr(err):
                code = main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_help_version_and_invalid_arguments_do_not_load_projects(self) -> None:
        with patch(
            "graphql_desk.cli.effective_project",
            side_effect=AssertionError("No discovery"),
        ):
            for argv in (["--help"], ["doctor", "--help"], ["--version"]):
                code, out, err = self.invoke(argv)
                self.assertEqual(code, 0)
                self.assertTrue(out)
                self.assertEqual(err, "")
            for argv in (
                [],
                ["PRIVATE_MARKER"],
                ["doctor", "--unknown", "Authorization: PRIVATE_MARKER"],
                ["doctor", "--config"],
            ):
                code, out, err = self.invoke(argv)
                self.assertEqual(code, 2)
                self.assertEqual(out, "")
                self.assertEqual(err, "Invalid arguments; use --help.\n")

    def test_nearest_explicit_and_degraded_reports_without_writes(self) -> None:
        nested = self.root / "nested"
        nested.mkdir()
        before = {
            path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()
        }
        for argv in (
            ["doctor"],
            ["--config", "../pyproject.toml", "doctor"],
            ["doctor", "-c", "../pyproject.toml"],
        ):
            code, out, err = self.invoke(argv, nested)
            self.assertEqual(code, 1)
            self.assertEqual(out, "")
            self.assertIn("configuration: ready", err)
            self.assertIn("schema default: ready", err)
            self.assertIn("interpreter: Python", err)
            self.assertIn("installed mypy distribution:", err)
            self.assertIn("active editor: unavailable", err)
            self.assertIn("live bridge: unavailable", err)
            self.assertIn("generated ownership: unavailable", err)
            self.assertIn("status: degraded", err)
        after = {
            path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()
        }
        self.assertEqual(before, after)

    def test_named_schema_selection_and_fresh_local_schema_reads(self) -> None:
        self.config.write_text(
            '[tool.graphql-desk]\n[[tool.graphql-desk.schemas]]\nname="first"\n'
            'schema="schema.graphql"\nschema_output="graphql_desk_generated/first.pyi"\n'
            '[[tool.graphql-desk.schemas]]\nname="second"\nschema="missing.graphql"\n'
            'schema_output="graphql_desk_generated/second.pyi"\n',
            encoding="utf8",
        )
        for argv in (["doctor", "--schema", "first"], ["--schema", "first", "doctor"]):
            code, out, err = self.invoke(argv)
            self.assertEqual((code, out), (1, ""))
            self.assertIn("schema first: ready", err)
            self.assertNotIn("schema second:", err)
        _code, _out, err = self.invoke(["doctor"])
        self.assertIn("schema second: unavailable", err)
        self.schema.write_text("PRIVATE_MARKER {", encoding="utf8")
        code, out, err = self.invoke(["doctor", "--schema", "first"])
        self.assertEqual((code, out), (1, ""))
        self.assertIn("schema first: unavailable", err)
        self.assertNotIn("PRIVATE_MARKER", err)
        code, out, err = self.invoke(["doctor", "--schema", "PRIVATE_MARKER"])
        self.assertEqual((code, out), (1, ""))
        self.assertIn("configuration: unavailable", err)
        self.assertNotIn("PRIVATE_MARKER", err)

    def test_invalid_inactive_missing_config_and_unverified_symbols(self) -> None:
        for text in ("[INVALID_PRIVATE_MARKER", "[project]\nname='inactive'\n"):
            self.config.write_text(text, encoding="utf8")
            code, out, err = self.invoke(["doctor", "--config", str(self.config)])
            self.assertEqual((code, out), (1, ""))
            self.assertIn("configuration: unavailable", err)
            self.assertNotIn("PRIVATE_MARKER", err)
        code, out, err = self.invoke(["doctor", "--config", "missing.toml"])
        self.assertEqual((code, out), (1, ""))
        self.assertIn("configuration: unavailable", err)
        self.config.write_text(
            '[tool.graphql-desk]\nschema="schema.graphql"\n'
            'schema_output="graphql_desk_generated/default.pyi"\n'
            'scalars={String="local_types.Text"}\n',
            encoding="utf8",
        )
        with patch(
            "importlib.import_module", side_effect=AssertionError("No project import")
        ):
            _code, _out, err = self.invoke(["doctor"])
        self.assertIn("mapping symbols: unverified", err)
        self.assertNotIn("local_types", sys.modules)

    def test_explicit_config_preserves_external_root_scope(self) -> None:
        project = self.root / "project"
        external = self.root / "external"
        project.mkdir()
        external.mkdir()
        (project / "schema.graphql").write_text(
            "type Query { x: String }", encoding="utf8"
        )
        (project / "pyproject.toml").write_text(
            '[tool.graphql-desk]\nschema="schema.graphql"\n'
            'source_roots=["../external"]\n'
            'schema_output="graphql_desk_generated/default.pyi"\n',
            encoding="utf8",
        )
        code, out, err = self.invoke(["doctor"], project)
        self.assertEqual((code, out), (1, ""))
        self.assertIn("configuration: unavailable", err)
        code, out, err = self.invoke(["doctor", "--config", "pyproject.toml"], project)
        self.assertEqual((code, out), (1, ""))
        self.assertIn("configuration: ready (explicit)", err)
        self.assertIn("schema default: ready", err)

    def test_actual_http_access_has_one_pass_headers_and_redacted_failures(
        self,
    ) -> None:
        with endpoint() as (url, server):
            self.config.write_text(
                '[tool.graphql-desk]\nschema={url="' + url + '/PRIVATE_MARKER",'
                'headers={"X-Key"="${env:TOKEN}"}}\n'
                'schema_output="graphql_desk_generated/default.pyi"\n',
                encoding="utf8",
            )
            with patch.dict(
                os.environ,
                {"TOKEN": "${env:OTHER}", "OTHER": "PRIVATE_MARKER"},
                clear=True,
            ):
                for _ in range(2):
                    code, out, err = self.invoke(["doctor"])
                    self.assertEqual((code, out), (1, ""))
                    self.assertIn("schema default: ready", err)
                    self.assertNotIn("PRIVATE_MARKER", err)
            self.assertEqual(len(server.records), 4)
            self.assertEqual(
                [record[1]["X-Key"] for record in server.records], ["${env:OTHER}"] * 4
            )
            with patch.dict(os.environ, {}, clear=True):
                _code, _out, err = self.invoke(["doctor"])
                self.assertIn("schema default: unavailable", err)
                self.assertNotIn("PRIVATE_MARKER", err)
            self.assertEqual(len(server.records), 4)
            server.mode = "auth"
            with patch.dict(os.environ, {"TOKEN": "PRIVATE_MARKER"}, clear=True):
                _code, _out, err = self.invoke(["doctor"])
                self.assertIn("schema default: unavailable", err)
                self.assertNotIn("PRIVATE_MARKER", err)
            self.assertEqual(len(server.records), 5)

    def test_missing_distribution_and_module_entrypoint(self) -> None:
        with patch(
            "graphql_desk.cli.metadata.version",
            side_effect=metadata.PackageNotFoundError("PRIVATE_MARKER"),
        ):
            _code, _out, err = self.invoke(["doctor"])
            self.assertIn("installed mypy distribution: unavailable", err)
            self.assertNotIn("PRIVATE_MARKER", err)
        for command, expected in (
            (["--help"], 0),
            (["doctor"], 1),
            (["PRIVATE_MARKER"], 2),
        ):
            result = subprocess.run(
                [sys.executable, "-I", "-m", "graphql_desk", *command],
                cwd=self.root,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
            self.assertNotIn("PRIVATE_MARKER", result.stdout + result.stderr)
        result = subprocess.run(
            [
                sys.executable,
                "-I",
                "-c",
                "import graphql_desk.cli, graphql_desk.__main__; print('inert')",
            ],
            cwd=self.root,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "inert\n")
        self.assertEqual(result.stderr, "")

    def test_keyboard_interrupt_is_a_sanitized_exit_code(self) -> None:
        with patch("graphql_desk.cli.load_local_schema", side_effect=KeyboardInterrupt):
            self.assertEqual(
                self.invoke(["doctor"]),
                (
                    130,
                    "",
                    "GraphQL Desk doctor\nconfiguration: ready (nearest)\n"
                    "Doctor cancelled.\n",
                ),
            )
