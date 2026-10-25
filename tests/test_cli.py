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

    def test_invalid_inactive_missing_config_and_static_mapping_symbols(self) -> None:
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
        self.assertIn("captured local mapping symbols unavailable", err)
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

    def test_mapping_declarations_use_schema_policy_without_imports_or_writes(
        self,
    ) -> None:
        self.schema.write_text(
            "scalar Date\nenum Color { RED BLUE }\n"
            "input Filter { date: Date color: Color }\n"
            "type Query { date(filter: Filter): Date }",
            encoding="utf8",
        )
        for mappings, ready in (
            ("", False),
            (
                'scalars={Date="external_types.Date"}\nenums={Color="external_types.Color"}',
                True,
            ),
            (
                'scalars={Date="external_types.Date", Missing="PRIVATE_MARKER.Type"}',
                False,
            ),
            (
                'scalars={Date="external_types.Date", Color="external_types.Color"}',
                False,
            ),
            (
                'scalars={Date="external_types.Date"}\nenums={Date="external_types.Date"}',
                False,
            ),
        ):
            with self.subTest(mappings=mappings):
                self.config.write_text(
                    '[tool.graphql-desk]\nschema="schema.graphql"\n'
                    'schema_output="graphql_desk_generated/default.pyi"\n'
                    + mappings
                    + "\n",
                    encoding="utf8",
                )
                before = {
                    p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()
                }
                with patch(
                    "importlib.import_module", side_effect=AssertionError("No imports")
                ):
                    code, out, err = self.invoke(["doctor"])
                self.assertEqual((code, out), (1, ""))
                self.assertIn("schema default: ready", err)
                self.assertIn(
                    "mapping declarations ready"
                    if ready
                    else "mapping declarations unavailable",
                    err,
                )
                self.assertNotIn("PRIVATE_MARKER", err)
                if ready:
                    self.assertIn(
                        "external mapping symbols unverified: 2",
                        err,
                    )
                self.assertEqual(
                    before,
                    {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()},
                )

    def test_doctor_resolves_local_mapping_names_without_importing_project_code(
        self,
    ) -> None:
        self.schema.write_text(
            "scalar Date\nenum Color { RED BLUE }\n"
            "type Query { date: Date color: Color }",
            encoding="utf8",
        )
        local_types = self.root / "local_types.py"
        local_types.write_text(
            "class DateInput: ...\nclass DateOutput: ...\nclass Color: ...\n",
            encoding="utf8",
        )
        self.config.write_text(
            '[tool.graphql-desk]\nschema="schema.graphql"\n'
            'schema_output="graphql_desk_generated/default.pyi"\n'
            'scalars={Date={input="local_types.DateInput",output="local_types.DateOutput"}}\n'
            'enums={Color="local_types.Color"}\n',
            encoding="utf8",
        )
        with patch("importlib.import_module", side_effect=AssertionError("No imports")):
            _code, _out, err = self.invoke(["doctor"])
        self.assertIn(
            "captured local mapping symbols resolved (static names only): 3",
            err,
        )
        self.assertNotIn("external mapping symbols unverified", err)
        self.assertNotIn("local_types", sys.modules)

        self.config.write_text(
            self.config.read_text(encoding="utf8").replace(
                "local_types.DateOutput", "local_types.MissingOutput"
            ),
            encoding="utf8",
        )
        with patch("importlib.import_module", side_effect=AssertionError("No imports")):
            _code, _out, err = self.invoke(["doctor"])
        self.assertIn(
            "captured local mapping symbols unavailable: missing=1",
            err,
        )

    def test_doctor_resolves_interpreter_stub_types_without_importing_them(
        self,
    ) -> None:
        self.schema.write_text(
            "scalar Date\ntype Query { date: Date }", encoding="utf8"
        )
        self.config.write_text(
            '[tool.graphql-desk]\nschema="schema.graphql"\n'
            'schema_output="graphql_desk_generated/default.pyi"\n'
            'scalars={Date="vendor_types.Date"}\n',
            encoding="utf8",
        )
        with tempfile.TemporaryDirectory() as directory:
            package = Path(directory) / "vendor_types"
            package.mkdir()
            (package / "__init__.pyi").write_text(
                "from .types import Date\n", encoding="utf8"
            )
            (package / "types.pyi").write_text(
                "raise RuntimeError('must not execute')\nclass Date: ...\n",
                encoding="utf8",
            )
            with patch("graphql_desk.cli.sys.path", [directory, *sys.path]):
                code, out, err = self.invoke(["doctor"])

        self.assertEqual((code, out), (1, ""))
        self.assertIn(
            "interpreter mapping symbols resolved (static names only): 1", err
        )
        self.assertNotIn("external mapping symbols unverified", err)

    def test_doctor_reports_external_non_type_without_claiming_verification(
        self,
    ) -> None:
        self.schema.write_text(
            "scalar Date\ntype Query { date: Date }", encoding="utf8"
        )
        self.config.write_text(
            '[tool.graphql-desk]\nschema="schema.graphql"\n'
            'schema_output="graphql_desk_generated/default.pyi"\n'
            'scalars={Date="vendor_types.Date"}\n',
            encoding="utf8",
        )
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "vendor_types.py").write_text(
                "raise RuntimeError('must not execute')\nDate = 1\n", encoding="utf8"
            )
            with patch("graphql_desk.cli.sys.path", [directory, *sys.path]):
                code, out, err = self.invoke(["doctor"])

        self.assertEqual((code, out), (1, ""))
        self.assertIn("interpreter mapping symbols unavailable: not-type=1", err)
        self.assertNotIn("interpreter mapping symbols resolved", err)

    def test_doctor_resolves_stub_only_mapping_symbols_and_package_reexports(
        self,
    ) -> None:
        self.schema.write_text(
            "scalar Date\nenum Color { RED BLUE }\n"
            "type Query { date: Date color: Color }",
            encoding="utf8",
        )
        (self.root / "models.pyi").write_text(
            "class DateInput: ...\nclass DateOutput: ...\n", encoding="utf8"
        )
        package = self.root / "colors"
        package.mkdir()
        (package / "__init__.pyi").write_text(
            "from .types import Color as Color\n", encoding="utf8"
        )
        (package / "types.pyi").write_text("class Color: ...\n", encoding="utf8")
        self.config.write_text(
            '[tool.graphql-desk]\nschema="schema.graphql"\n'
            'schema_output="graphql_desk_generated/default.pyi"\n'
            'scalars={Date={input="models.DateInput",output="models.DateOutput"}}\n'
            'enums={Color="colors.Color"}\n',
            encoding="utf8",
        )
        with patch("importlib.import_module", side_effect=AssertionError("No imports")):
            _code, _out, err = self.invoke(["doctor"])
        self.assertIn(
            "captured local mapping symbols resolved (static names only): 3",
            err,
        )
        self.assertNotIn("external mapping symbols unverified", err)
        self.assertNotIn("colors", sys.modules)
        self.assertNotIn("models", sys.modules)

    def test_doctor_prefers_stub_and_reports_missing_or_invalid_stub_exports(
        self,
    ) -> None:
        self.schema.write_text(
            "scalar Date\ntype Query { date: Date }", encoding="utf8"
        )
        implementation = self.root / "models.py"
        implementation.write_text("class Date: ...\n", encoding="utf8")
        stub = self.root / "models.pyi"
        stub.write_text("class Other: ...\n", encoding="utf8")
        self.config.write_text(
            '[tool.graphql-desk]\nschema="schema.graphql"\n'
            'schema_output="graphql_desk_generated/default.pyi"\n'
            'scalars={Date="models.Date"}\n',
            encoding="utf8",
        )
        with patch("importlib.import_module", side_effect=AssertionError("No imports")):
            _code, _out, err = self.invoke(["doctor"])
        self.assertIn(
            "captured local mapping symbols unavailable: missing=1",
            err,
        )

        stub.write_text("class Date(: ...\n", encoding="utf8")
        with patch("importlib.import_module", side_effect=AssertionError("No imports")):
            _code, _out, err = self.invoke(["doctor"])
        self.assertIn(
            "captured local mapping symbols unavailable: unresolved=1",
            err,
        )

    def test_doctor_follows_submodule_import_aliases_in_stub_packages(self) -> None:
        cases = (
            (
                "relative",
                "relative/__init__.pyi",
                "from . import types as _types\nDate = _types.Date\n",
                "relative/types.pyi",
            ),
            (
                "absolute",
                "absolute/__init__.pyi",
                "from absolute import types as _types\nDate = _types.Date\n",
                "absolute/types.pyi",
            ),
            (
                "nested",
                "nested/child/__init__.pyi",
                "from .. import types as _types\nDate = _types.Date\n",
                "nested/types.pyi",
            ),
        )
        for package, init_path, init_text, types_path in cases:
            with self.subTest(package=package):
                for relative, content in (
                    (init_path, init_text),
                    (types_path, "class Date: ...\n"),
                ):
                    path = self.root / relative
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(content, encoding="utf8")
                self.schema.write_text(
                    "scalar Date\ntype Query { date: Date }", encoding="utf8"
                )
                symbol_module = f"{package}.child" if package == "nested" else package
                self.config.write_text(
                    '[tool.graphql-desk]\nschema="schema.graphql"\n'
                    'schema_output="graphql_desk_generated/default.pyi"\n'
                    f'scalars={{Date="{symbol_module}.Date"}}\n',
                    encoding="utf8",
                )
                with patch(
                    "importlib.import_module",
                    side_effect=AssertionError("No imports"),
                ):
                    _code, _out, err = self.invoke(["doctor"])
                self.assertIn(
                    "captured local mapping symbols resolved (static names only): 1",
                    err,
                )
                self.assertNotIn("external mapping symbols unverified", err)
                self.assertNotIn(package, sys.modules)

    def test_explicit_any_report_preserves_mapping_directions_and_schema_selection(
        self,
    ) -> None:
        self.schema.write_text(
            "scalar Date\nenum Color { RED }\ntype Query { date: Date color: Color }",
            encoding="utf8",
        )
        self.config.write_text(
            '[tool.graphql-desk]\n[[tool.graphql-desk.schemas]]\nname="first"\n'
            'schema="schema.graphql"\nschema_output="graphql_desk_generated/first.pyi"\n'
            'scalars={Date={input="typing.Any",output="local_types.Date"}}\n'
            'enums={Color="typing.Any"}\n'
            '[[tool.graphql-desk.schemas]]\nname="second"\nschema="schema.graphql"\n'
            'schema_output="graphql_desk_generated/second.pyi"\n'
            'scalars={Date={input="local_types.Date",output="typing.Any"}}\n',
            encoding="utf8",
        )
        _code, _out, err = self.invoke(["doctor", "--schema", "first"])
        self.assertIn("schema first: explicit Any scalar input mapping Date", err)
        self.assertIn("schema first: explicit Any enum mapping Color", err)
        self.assertNotIn("scalar output mapping", err)
        self.assertNotIn("schema second:", err)
        _code, _out, err = self.invoke(["doctor", "--schema", "second"])
        self.assertIn("schema second: explicit Any scalar output mapping Date", err)
        self.assertNotIn("scalar input mapping", err)
        self.assertNotIn("schema first:", err)

    def test_actual_http_access_has_one_pass_headers_and_redacted_failures(
        self,
    ) -> None:
        with endpoint() as (url, server):
            self.config.write_text(
                '[tool.graphql-desk]\nschema={url="' + url + '/PRIVATE_MARKER",'
                'headers={"X-Key"="${env:TOKEN}"}}\n'
                'schema_output="graphql_desk_generated/default.pyi"\n'
                'scalars={URL="typing.Any"}\n',
                encoding="utf8",
            )
            with patch.dict(
                os.environ,
                {"TOKEN": "${env:OTHER}", "OTHER": "PRIVATE_MARKER"},
            ):
                for _ in range(2):
                    code, out, err = self.invoke(["doctor"])
                    self.assertEqual((code, out), (1, ""))
                    self.assertIn("schema default: ready", err)
                    self.assertIn("mapping declarations ready", err)
                    self.assertIn("explicit Any scalar input mapping URL", err)
                    self.assertNotIn("PRIVATE_MARKER", err)
            self.assertEqual(len(server.records), 4)
            self.assertEqual(
                [record[1]["X-Key"] for record in server.records], ["${env:OTHER}"] * 4
            )
            without_token = dict(os.environ)
            without_token.pop("TOKEN", None)
            with patch.dict(os.environ, without_token, clear=True):
                _code, _out, err = self.invoke(["doctor"])
                self.assertIn("schema default: unavailable", err)
                self.assertNotIn("mapping declarations", err)
                self.assertNotIn("PRIVATE_MARKER", err)
            self.assertEqual(len(server.records), 4)
            server.mode = "auth"
            with patch.dict(os.environ, {"TOKEN": "PRIVATE_MARKER"}):
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
