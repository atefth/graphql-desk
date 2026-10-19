"""Configuration facts remain deterministic, immutable and free of execution."""

import tempfile
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest.mock import patch

from graphql_desk.config import load_project_config, parse_project_config

_BASE = """[tool.graphql-desk]
schema = "schema.graphql"
schema_output = "graphql_desk_generated/default.pyi"
"""


class ConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.path = self.root / "pyproject.toml"

    def test_inactive_project_and_default_facts(self) -> None:
        self.assertIsNone(
            parse_project_config('[project]\nname="other"', config_path=self.path)
        )
        config = parse_project_config(_BASE, config_path=self.path)
        assert config is not None
        self.assertEqual(config.root, self.root)
        self.assertEqual(config.generated_package, "graphql_desk_generated")
        self.assertEqual(len(config.schemas), 1)
        schema = config.schemas[0]
        self.assertEqual(schema.name, "default")
        self.assertEqual(schema.origin.local_path, self.root / "schema.graphql")
        self.assertIsNone(schema.origin.url)
        self.assertIsNone(schema.persisted_output)
        self.assertIsNone(schema.cache_output)
        self.assertEqual(schema.scalars, ())
        self.assertEqual(schema.enums, ())
        self.assertFalse(schema.disable_masking)
        self.assertFalse(config.cache_enabled)
        self.assertFalse(config.disable_preprocessing)
        self.assertTrue(config.track_field_usage)
        self.assertTrue(config.check_colocated_fragments)
        self.assertEqual(config.client_directives, ())
        self.assertEqual(config.schema_refresh_interval, 0)
        self.assertIsNone(config.template)
        self.assertEqual(config.source_roots, (self.root,))
        self.assertEqual(config.include, ("**/*.py",))
        self.assertEqual(
            config.exclude,
            (
                "**/.git/**",
                "**/.venv/**",
                "**/venv/**",
                "**/__pycache__/**",
                "**/site-packages/**",
                "**/build/**",
                "**/dist/**",
            ),
        )
        self.assertIsNone(config.entrypoints)
        self.assertEqual(config.authoring_roots, ())
        with self.assertRaises(FrozenInstanceError):
            setattr(config, "generated_package", "other")

    def test_read_explicit_config_and_resolve_config_dir(self) -> None:
        self.path.write_text(
            _BASE.replace('"schema.graphql"', '"${configDir}/schema.graphql"')
        )
        config = load_project_config(self.path)
        assert config is not None
        self.assertEqual(
            config.schemas[0].origin.local_path, self.root / "schema.graphql"
        )
        self.assertFalse((self.root / "graphql_desk_generated").exists())

    def test_remote_headers_are_unresolved_and_redacted(self) -> None:
        text = _BASE.replace(
            '"schema.graphql"',
            '{url="https://example.test/graphql?token=SECRET", '
            'headers={Authorization="Bearer SECRET ${env:TOKEN}", Empty=""}}',
        )
        with patch.dict("os.environ", {}, clear=True):
            config = parse_project_config(text, config_path=self.path)
        assert config is not None
        origin = config.schemas[0].origin
        self.assertIsNone(origin.local_path)
        self.assertEqual(
            origin.headers,
            (("Authorization", "Bearer SECRET ${env:TOKEN}"), ("Empty", "")),
        )
        self.assertNotIn("SECRET", repr(config))
        self.assertNotIn("TOKEN", repr(config))

    def test_named_schema_records_and_all_project_controls(self) -> None:
        text = """[tool.graphql-desk]
generated_package = "generated.types"
template = "queries.custom"
track_field_usage = false
check_colocated_fragments = false
client_directives = ["client", "defer"]
schema_refresh_interval = 1.5
disable_preprocessing = true
source_roots = ["src", "src/."]
include = ["**/*.py", "one?.py"]
exclude = []
entrypoints = ["queries.py"]
authoring_roots = ["src/queries"]
templateIsCallExpression = true
[[tool.graphql-desk.schemas]]
name = "first"
schema = "schema.graphql"
schema_output = "generated/types/first.pyi"
persisted_output = "persisted/first.json"
cache_output = "cache/first.json"
disable_masking = true
scalars = {Date = {input="builtins.str", output="datetime.datetime"}}
enums = {Color = "domain.Color"}
[[tool.graphql-desk.schemas]]
name = "second"
schema = "https://example.test/graphql"
schema_output = "generated/types/second.pyi"
scalars = {ID = "builtins.str"}
"""
        config = parse_project_config(text, config_path=self.path)
        assert config is not None
        self.assertEqual(tuple(s.name for s in config.schemas), ("first", "second"))
        self.assertEqual(config.schemas[0].scalars[0].output, "datetime.datetime")
        self.assertEqual(config.schemas[1].scalars[0].input, "builtins.str")
        self.assertEqual(config.schemas[0].enums, (("Color", "domain.Color"),))
        self.assertTrue(config.schemas[0].disable_masking)
        self.assertEqual(config.source_roots, (self.root / "src",))
        self.assertEqual(config.authoring_roots, (self.root / "src/queries",))
        self.assertEqual(config.entrypoints, ("queries.py",))
        self.assertEqual(config.schema_refresh_interval, 1.5)
        self.assertTrue(config.disable_preprocessing)
        self.assertFalse(config.track_field_usage)
        self.assertFalse(config.check_colocated_fragments)
        self.assertEqual(config.template, "queries.custom")

    def test_invalid_project_controls(self) -> None:
        additions = (
            "unknown = true",
            "global = true",
            "editorContributed = true",
            "cache_enabled = true",
            'cache_enabled = "false"',
            "templateIsCallExpression = false",
            'template = "unqualified"',
            'generated_package = "../outside"',
            'generated_package = "class"',
            'generated_package = "𝑩ook"',
            "track_field_usage = 1",
            'check_colocated_fragments = "true"',
            "disable_preprocessing = 0",
            "disable_masking = 1",
            "schema_refresh_interval = -1",
            "schema_refresh_interval = nan",
            "schema_refresh_interval = inf",
            "schema_refresh_interval = true",
            'client_directives = ["@client"]',
            'client_directives = "client"',
            'include = ["!file.py"]',
            'exclude = ["[a-z].py"]',
            'include = ["../*.py"]',
            'source_roots = ["../outside"]',
            'authoring_roots = ["../outside"]',
            'entrypoints = ["*.py"]',
            'entrypoints = ["../outside.py"]',
            'scalars = {Date = {input="builtins.str"}}',
            'scalars = {Date = {input="builtins.str", '
            'output="datetime.datetime", extra=1}}',
            'scalars = {Date = "str"}',
            'enums = {Color = {input="domain.Color"}}',
            'scalars = {Date = "danger.run()"}',
        )
        for addition in additions:
            with self.subTest(addition=addition), self.assertRaises(ValueError):
                parse_project_config(_BASE + addition, config_path=self.path)

    def test_invalid_schema_origins_never_echo_credentials(self) -> None:
        for origin in (
            '{url="schema.graphql"}',
            '{url="ftp://host/SECRET"}',
            '{url="https://host/SECRET", headers={Authorization=12}}',
            '{url="https://host/SECRET", headers={Authorization="${other:SECRET}"}}',
            '{url="https://host/SECRET", headers={"bad header"="SECRET"}}',
            '{url="https://host/SECRET", unknown="SECRET"}',
            '"http://"',
            '"https://host:bad/SECRET"',
            '"${env:SECRET}/schema.graphql"',
            '"ftp://host/SECRET"',
        ):
            with self.subTest(origin=origin), self.assertRaises(ValueError) as caught:
                parse_project_config(
                    _BASE.replace('"schema.graphql"', origin), config_path=self.path
                )
            self.assertNotIn("SECRET", str(caught.exception))

    def test_named_schema_and_output_collisions(self) -> None:
        for text in (
            _BASE + "schemas = []",
            "[tool.graphql-desk]\nschemas=[]",
            _BASE.replace("default.pyi", "other.pyi"),
            _BASE + 'persisted_output="graphql_desk_generated/default.py"',
            _BASE + 'persisted_output="pyproject.toml"',
            _BASE + 'persisted_output="schema.graphql"',
            _BASE + 'persisted_output="graphql_desk_generated/__init__.py"',
            _BASE + 'persisted_output="graphql_desk_generated"',
            _BASE + 'persisted_output="same.json"\ncache_output="same.json"',
            """[tool.graphql-desk]
schemas = [
 {name="x",schema="x.graphql",schema_output="graphql_desk_generated/x.pyi"},
 {name="x",schema="x.graphql",schema_output="graphql_desk_generated/x.pyi"}]
""",
            '[tool.graphql-desk]\nschemas = [{name="sources", '
            'schema="schema.graphql", '
            'schema_output="graphql_desk_generated/sources.pyi"}]',
            '[tool.graphql-desk]\nschemas = [{name="café", '
            'schema="schema.graphql", '
            'schema_output="graphql_desk_generated/café.pyi"}]',
        ):
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse_project_config(text, config_path=self.path)

    def test_symlink_package_cannot_escape_project(self) -> None:
        with tempfile.TemporaryDirectory() as external:
            link = self.root / "graphql_desk_generated"
            try:
                link.symlink_to(external, target_is_directory=True)
            except OSError:
                self.skipTest("Creating symlinks is not permitted on this host")
            with self.assertRaises(ValueError):
                parse_project_config(_BASE, config_path=self.path)

    def test_package_symlink_cannot_claim_the_project_root(self) -> None:
        link = self.root / "alias"
        try:
            link.symlink_to(self.root, target_is_directory=True)
        except OSError:
            self.skipTest("Creating symlinks is not permitted on this host")
        text = _BASE.replace(
            "graphql_desk_generated/default.pyi", "alias/child/default.pyi"
        )
        text += 'generated_package="alias.child"'
        with self.assertRaises(ValueError):
            parse_project_config(text, config_path=self.path)

    def test_companion_and_initializer_symlinks_cannot_escape_or_collide(self) -> None:
        package = self.root / "graphql_desk_generated"
        package.mkdir()
        destination = self.root / "out.json"
        destination.write_text("untouched")
        for filename in ("default.py", "__init__.py", "__init__.pyi"):
            link = package / filename
            try:
                link.symlink_to(destination)
            except OSError:
                self.skipTest("Creating symlinks is not permitted on this host")
            try:
                with self.assertRaises(ValueError):
                    parse_project_config(
                        _BASE + 'persisted_output="out.json"', config_path=self.path
                    )
                self.assertEqual(destination.read_text(), "untouched")
            finally:
                link.unlink()

    def test_invalid_toml_is_redacted_and_no_user_symbol_is_imported(self) -> None:
        with self.assertRaisesRegex(ValueError, "Invalid pyproject TOML") as caught:
            parse_project_config('SECRET = "', config_path=self.path)
        self.assertNotIn("SECRET", str(caught.exception))
        config = parse_project_config(
            _BASE + 'scalars={Date="nonexistent.module.Type"}', config_path=self.path
        )
        assert config is not None
        self.assertEqual(config.schemas[0].scalars[0].input, "nonexistent.module.Type")


if __name__ == "__main__":
    unittest.main()
