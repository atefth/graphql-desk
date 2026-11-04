"""CLI and callable schema download/conversion acceptance."""

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from graphql import build_schema, introspection_from_schema, print_schema

from graphql_desk.cli import SchemaGenerationError, generate_schema, main
from graphql_desk.schema_loading import load_from_sdl, load_local_schema
from tests.test_schema_url import endpoint


class TerminalBuffer(io.StringIO):
    def isatty(self) -> bool:
        return True


class GenerateSchemaTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.input = self.root / "schema.graphql"
        self.input_text = '"""Sample schema."""\ntype Query { title: String! }\n'
        self.input.write_text(self.input_text, encoding="utf8")
        self.config = self.root / "pyproject.toml"

    def write_config(self, schema: str) -> None:
        self.config.write_text(
            "[tool.graphql-desk]\n"
            f"schema = {json.dumps(schema)}\n"
            'schema_output = "graphql_desk_generated/default.pyi"\n',
            encoding="utf8",
        )

    def cli(
        self, arguments: list[str], *, cwd: Path | None = None, terminal: bool = False
    ) -> tuple[int, str, str]:
        stdout = TerminalBuffer() if terminal else io.StringIO()
        stderr = io.StringIO()
        with (
            patch("pathlib.Path.cwd", return_value=cwd or self.root),
            patch("sys.stdout", stdout),
            redirect_stderr(stderr),
        ):
            code = main(arguments)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_configured_sdl_is_replaced_with_stable_canonical_sdl(self) -> None:
        self.write_config("schema.graphql")
        expected = print_schema(load_from_sdl(self.input_text))

        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            generate_schema(input=self.input, config=self.config, silent=True)
        first = self.input.read_text(encoding="utf8")
        self.input.write_text(self.input_text, encoding="utf8")
        code, stdout, stderr = self.cli(
            ["generate-schema", "schema.graphql", "--config", str(self.config)],
            terminal=True,
        )
        self.assertEqual((code, stdout, stderr), (0, "", "Schema generated.\n"))
        self.assertEqual(self.input.read_text(encoding="utf8"), expected)
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            generate_schema(input=self.input, config=self.config, silent=True)

        self.assertEqual(first, expected)
        self.assertEqual(self.input.read_text(encoding="utf8"), expected)

    def test_redirected_stdout_precedes_configured_sdl_destination(self) -> None:
        self.write_config("schema.graphql")
        before = self.input_text
        expected = print_schema(load_from_sdl(before))

        code, stdout, stderr = self.cli(
            ["generate-schema", "schema.graphql", "--config", str(self.config)]
        )

        self.assertEqual((code, stdout, stderr), (0, expected, "Schema generated.\n"))
        self.assertEqual(self.input.read_text(encoding="utf8"), before)

    def test_introspection_json_is_not_overwritten_and_redirected_stdout_is_sdl_only(
        self,
    ) -> None:
        json_path = self.root / "schema.json"
        schema = build_schema('"""Sample schema.""" type Query { title: String! }')
        json_text = json.dumps({"data": introspection_from_schema(schema)})
        json_path.write_text(json_text, encoding="utf8")
        self.write_config("schema.json")
        expected = print_schema(load_local_schema(json_path))

        code, stdout, stderr = self.cli(
            ["generate-schema", "schema.json", "--config", str(self.config)]
        )

        self.assertEqual((code, stdout, stderr), (0, expected, "Schema generated.\n"))
        self.assertEqual(json_path.read_text(encoding="utf8"), json_text)
        destination = self.root / "converted.graphql"
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            generate_schema(
                input=json_path,
                config=self.config,
                output=destination,
                silent=True,
            )
        self.assertEqual(destination.read_text(encoding="utf8"), expected)
        self.assertEqual(json_path.read_text(encoding="utf8"), json_text)

    def test_raw_sdl_input_and_explicit_output_work_without_project_config(
        self,
    ) -> None:
        bare = self.root / "bare"
        bare.mkdir()
        destination = self.root / "converted.graphql"
        expected = print_schema(load_from_sdl(self.input_text))

        code, stdout, stderr = self.cli(
            [
                "generate",
                "schema",
                str(self.input),
                "--output",
                str(destination),
            ],
            cwd=bare,
        )

        self.assertEqual((code, stdout, stderr), (0, "", "Schema generated.\n"))
        self.assertEqual(destination.read_text(encoding="utf8"), expected)

    def test_explicit_config_and_schema_selector_are_validated(self) -> None:
        self.write_config("schema.graphql")
        destination = self.root / "selected.graphql"
        for arguments in (
            [
                "generate-schema",
                str(self.input),
                "--config",
                str(self.config),
                "--schema",
                "missing",
                "--output",
                str(destination),
            ],
            [
                "generate-schema",
                str(self.input),
                "--config",
                str(self.root / "missing-pyproject.toml"),
                "--output",
                str(destination),
            ],
        ):
            code, stdout, stderr = self.cli(arguments)
            self.assertEqual(code, 1)
            self.assertEqual(stdout, "")
            self.assertTrue(stderr)
            self.assertFalse(destination.exists())

    def test_named_config_supplies_headers_only_for_exact_input_url_and_cli_last_wins(
        self,
    ) -> None:
        with endpoint() as (url, handler):
            selected_url = f"{url}/graphql?source=selected"
            other_url = f"{url}/graphql?source=other"
            configured_header = json.dumps("Bearer ${env:BASE_TOKEN}")
            self.config.write_text(
                "[tool.graphql-desk]\n"
                'generated_package = "graphql_desk_generated"\n'
                "[[tool.graphql-desk.schemas]]\n"
                'name = "local"\n'
                'schema = "schema.graphql"\n'
                'schema_output = "graphql_desk_generated/local.pyi"\n'
                "[[tool.graphql-desk.schemas]]\n"
                'name = "remote"\n'
                f"schema = {{ url = {json.dumps(selected_url)}, headers = "
                f"{{ Authorization = {configured_header} }} }}\n"
                'schema_output = "graphql_desk_generated/remote.pyi"\n',
                encoding="utf8",
            )
            output = self.root / "downloaded.graphql"
            with patch.dict("os.environ", {"BASE_TOKEN": "CONFIG_TOKEN"}):
                code, stdout, stderr = self.cli(
                    [
                        "generate-schema",
                        selected_url,
                        "--config",
                        str(self.config),
                        "--schema",
                        "remote",
                        "--output",
                        str(output),
                        "--header",
                        "authorization: Bearer FIRST_TOKEN",
                        "--header",
                        "AUTHORIZATION: Bearer LAST_TOKEN",
                    ]
                )
            self.assertEqual((code, stdout, stderr), (0, "", "Schema generated.\n"))
            self.assertEqual(
                [item[1].get("Authorization") for item in handler.records],
                ["Bearer LAST_TOKEN"] * len(handler.records),
            )
            self.assertEqual(
                output.read_text(encoding="utf8"),
                print_schema(
                    build_schema(
                        'scalar URL @specifiedBy(url: "https://example.com/spec") '
                        "directive @tag(old: String @deprecated) repeatable on FIELD "
                        "input Choice @oneOf { name: String count: Int } "
                        "type Query { title(old: String @deprecated): String }"
                    )
                ),
            )

            handler.records.clear()
            inherited = self.root / "inherited.graphql"
            generate_schema(
                input=selected_url,
                config=self.config,
                schema="remote",
                output=inherited,
                environment={"BASE_TOKEN": "CONFIG_TOKEN"},
                silent=True,
            )
            self.assertTrue(handler.records)
            self.assertTrue(
                all(
                    item[1].get("Authorization") == "Bearer CONFIG_TOKEN"
                    for item in handler.records
                )
            )

            handler.records.clear()
            generate_schema(
                input=other_url,
                config=self.config,
                schema="remote",
                output=self.root / "unconfigured-url.graphql",
                environment={"BASE_TOKEN": "CONFIG_TOKEN"},
                silent=True,
            )
            self.assertTrue(handler.records)
            self.assertTrue(
                all("Authorization" not in item[1] for item in handler.records)
            )

            with self.assertRaises(SchemaGenerationError) as caught:
                generate_schema(
                    input=selected_url,
                    config=self.config,
                    schema="remote",
                    output=self.root / "invalid-header.graphql",
                    headers={"Bad Header": "PRIVATE_MALFORMED_HEADER"},
                    environment={"BASE_TOKEN": "CONFIG_TOKEN"},
                    silent=True,
                )
            self.assertNotIn(
                "PRIVATE_MALFORMED_HEADER",
                "\n".join(caught.exception.diagnostics),
            )

    def test_remote_errors_are_sanitized_and_existing_output_is_preserved(self) -> None:
        with endpoint("auth") as (url, _handler):
            private_url = f"{url}/graphql?token=URL_SECRET"
            auth_header = json.dumps("Bearer ${env:HEADER_SECRET}")
            self.write_config(
                f"{{ url = {json.dumps(private_url)}, headers = "
                f"{{ Authorization = {auth_header} }} }}"
            )
            destination = self.root / "existing.graphql"
            destination.write_text("user-owned output\n", encoding="utf8")

            with self.assertRaises(SchemaGenerationError) as caught:
                generate_schema(
                    input=private_url,
                    config=self.config,
                    output=destination,
                    environment={"HEADER_SECRET": "PRIVATE_HEADER"},
                )

        self.assertEqual(destination.read_text(encoding="utf8"), "user-owned output\n")
        diagnostics = "\n".join(caught.exception.diagnostics)
        self.assertNotIn("URL_SECRET", diagnostics)
        self.assertNotIn("PRIVATE_HEADER", diagnostics)
        self.assertNotIn("secret response", diagnostics)

    def test_invalid_sdl_leaves_explicit_destination_unchanged(self) -> None:
        invalid = self.root / "invalid.graphql"
        invalid.write_text("type Query { PRIVATE_SOURCE_MARKER", encoding="utf8")
        destination = self.root / "existing.graphql"
        destination.write_text("preserve me\n", encoding="utf8")

        with self.assertRaises(SchemaGenerationError) as caught:
            generate_schema(input=invalid, output=destination)

        self.assertEqual(destination.read_text(encoding="utf8"), "preserve me\n")
        self.assertTrue(caught.exception.diagnostics)

    def test_explicit_output_cannot_replace_json_config_source_or_artifact(
        self,
    ) -> None:
        json_path = self.root / "schema.json"
        schema = build_schema("type Query { title: String! }")
        json_text = json.dumps({"data": introspection_from_schema(schema)})
        json_path.write_text(json_text, encoding="utf8")
        self.write_config("schema.json")
        with self.assertRaises(SchemaGenerationError):
            generate_schema(
                input=json_path,
                config=self.config,
                output=json_path,
                silent=True,
            )
        self.assertEqual(json_path.read_text(encoding="utf8"), json_text)

        source = self.root / "main.py"
        source.write_text("from graphql_desk import gql\n", encoding="utf8")
        generated = self.root / "graphql_desk_generated"
        manifest = generated / ".graphql-desk-manifest.json"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text("preserve generated manifest\n", encoding="utf8")
        persisted = generated / "persisted" / "default.pyi"
        persisted.parent.mkdir(parents=True, exist_ok=True)
        persisted.write_text("preserve persisted binding\n", encoding="utf8")
        user_stub = self.root / "user.pyi"
        user_stub.write_text("preserve user stub\n", encoding="utf8")
        for destination in (
            self.config,
            source,
            generated / "default.pyi",
            manifest,
            persisted,
            user_stub,
            generated / "notes.graphql",
        ):
            original = destination.read_bytes() if destination.exists() else None
            with self.subTest(destination=destination):
                with self.assertRaises(SchemaGenerationError):
                    generate_schema(
                        input=self.input,
                        config=self.config,
                        output=destination,
                        silent=True,
                    )
                self.assertEqual(destination.exists(), original is not None)
                if original is not None:
                    self.assertEqual(destination.read_bytes(), original)

    def test_configured_sdl_symlink_cannot_become_implicit_output(self) -> None:
        target = self.root / "schema-target.graphql"
        target.write_text(self.input_text, encoding="utf8")
        link = self.root / "schema-link.graphql"
        try:
            link.symlink_to(target)
        except (NotImplementedError, OSError) as error:
            self.skipTest(f"file symlinks are unavailable: {error}")
        self.write_config("schema-link.graphql")
        original_target = target.read_bytes()

        code, stdout, stderr = self.cli(
            ["generate-schema", "schema-link.graphql", "--config", str(self.config)],
            terminal=True,
        )
        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertTrue(stderr)
        self.assertTrue(link.is_symlink())
        self.assertEqual(target.read_bytes(), original_target)

        with self.assertRaises(SchemaGenerationError):
            generate_schema(input=link, config=self.config, silent=True)
        self.assertTrue(link.is_symlink())
        self.assertEqual(target.read_bytes(), original_target)

    def test_explicit_symlink_output_preserves_link_and_target(self) -> None:
        target = self.root / "output-target.graphql"
        target.write_text("user-owned output\n", encoding="utf8")
        link = self.root / "output-link.graphql"
        try:
            link.symlink_to(target)
        except (NotImplementedError, OSError) as error:
            self.skipTest(f"file symlinks are unavailable: {error}")
        original = target.read_bytes()

        with self.assertRaises(SchemaGenerationError):
            generate_schema(input=self.input, output=link, silent=True)

        self.assertTrue(link.is_symlink())
        self.assertEqual(target.read_bytes(), original)

    def test_json_source_and_remote_origin_are_not_overwritten_by_default(self) -> None:
        json_path = self.root / "schema.json"
        schema = build_schema("type Query { title: String! }")
        json_text = json.dumps({"data": introspection_from_schema(schema)})
        json_path.write_text(json_text, encoding="utf8")
        self.write_config("schema.json")
        code, stdout, stderr = self.cli(
            ["generate-schema", str(json_path), "--config", str(self.config)],
            terminal=True,
        )
        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertTrue(stderr)
        self.assertEqual(json_path.read_text(encoding="utf8"), json_text)

        with endpoint() as (url, handler):
            self.write_config(url)
            with self.assertRaises(SchemaGenerationError):
                generate_schema(input=url, config=self.config, silent=True)
            self.assertEqual(
                handler.records, [], "Missing destination fails before network access"
            )
            code, stdout, stderr = self.cli(
                ["generate-schema", url, "--config", str(self.config)]
            )
            self.assertEqual(code, 0, stderr)
            self.assertTrue(stdout)
            self.assertFalse(
                (self.root / "graphql_desk_generated").exists(),
                "A remote source has no implicit local file destination",
            )
            self.assertTrue(handler.records)

    def test_silent_callable_suppresses_success_reporting(self) -> None:
        destination = self.root / "silent.graphql"
        output = io.StringIO()
        errors = io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            generate_schema(input=self.input, output=destination, silent=True)

        self.assertEqual(output.getvalue(), "")
        self.assertEqual(errors.getvalue(), "")
        self.assertTrue(destination.is_file())

    def test_command_help_does_not_discover_or_load_a_project(self) -> None:
        with patch(
            "graphql_desk.cli.effective_project",
            side_effect=AssertionError("Help must not discover a project"),
        ):
            code, stdout, stderr = self.cli(["generate-schema", "--help"])

        self.assertEqual(code, 0)
        self.assertIn("input", stdout)
        self.assertEqual(stderr, "")


if __name__ == "__main__":
    unittest.main()
