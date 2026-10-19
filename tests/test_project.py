"""Project search never crosses its workspace or guesses a schema binding."""

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from graphql_desk.config import ProjectConfig, load_project_config, parse_project_config
from graphql_desk.project import (
    effective_project,
    find_project,
    select_schema,
    validate_project_ownership,
)

_BASE = """[tool.graphql-desk]
schema="schema.graphql"
schema_output="graphql_desk_generated/default.pyi"
"""


class ProjectTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()

    def config(self, root: Path, text: str = _BASE) -> ProjectConfig:
        root.mkdir(parents=True, exist_ok=True)
        config = parse_project_config(text, config_path=root / "pyproject.toml")
        assert config is not None
        return config

    def write_config(self, root: Path, text: str = _BASE) -> ProjectConfig:
        config = self.config(root, text)
        config.path.write_text(text, encoding="utf-8")
        return config

    def test_effective_project_selects_whole_configs_before_fallback_validation(
        self,
    ) -> None:
        project = self.write_config(
            self.root,
            _BASE.replace(
                '"schema.graphql"',
                '{url="https://project.test",headers={Authorization="project"}}',
            ),
        )
        fallback: dict[str, object] = {
            "schema": {
                "url": "https://fallback.test",
                "headers": {"Other": "fallback"},
            },
            "schema_output": "graphql_desk_generated/default.pyi",
        }
        selected = effective_project(
            self.root, workspace=self.root, editor_fallback=fallback
        )
        assert selected is not None
        self.assertEqual(selected.origin, "nearest")
        self.assertEqual(selected.config, project)
        self.assertEqual(
            selected.config.schemas[0].origin.headers, (("Authorization", "project"),)
        )
        self.assertEqual(
            effective_project(
                self.root, workspace=self.root, editor_fallback={"invalid": "SECRET"}
            ),
            selected,
        )

    def test_editor_fallback_paths_use_workspace_and_no_file_is_created(self) -> None:
        directory = self.root / "src"
        directory.mkdir()
        selected = effective_project(
            directory,
            workspace=self.root,
            editor_fallback={
                "schema": "schema.graphql",
                "schema_output": "graphql_desk_generated/default.pyi",
            },
        )
        assert selected is not None
        self.assertEqual(selected.origin, "editor_fallback")
        self.assertEqual(selected.config.root, self.root)
        self.assertEqual(
            selected.config.schemas[0].origin.local_path, self.root / "schema.graphql"
        )
        self.assertFalse(selected.config.path.exists())
        self.assertIsNone(effective_project(directory, workspace=self.root))
        with self.assertRaises(ValueError):
            effective_project(self.root, editor_fallback={})

    def test_explicit_configuration_wins_even_outside_nearest_workspace(self) -> None:
        explicit = self.write_config(self.root / "explicit")
        workspace = self.root / "workspace"
        self.write_config(workspace)
        selected = effective_project(
            workspace,
            explicit_config=Path("../explicit/pyproject.toml"),
            workspace=workspace,
            editor_fallback={"bad": True},
        )
        assert selected is not None
        self.assertEqual(selected.origin, "explicit")
        self.assertEqual(selected.config, explicit)
        explicit.path.write_text('[project]\nname="inactive"')
        with self.assertRaises(ValueError):
            effective_project(
                workspace, explicit_config=explicit.path, workspace=workspace
            )
        explicit.path.write_text('SECRET="')
        with self.assertRaises(ValueError) as caught:
            effective_project(
                workspace, explicit_config=explicit.path, workspace=workspace
            )
        self.assertNotIn("SECRET", str(caught.exception))
        explicit.path.unlink()
        with self.assertRaises(OSError):
            effective_project(
                workspace, explicit_config=explicit.path, workspace=workspace
            )

    def test_invalid_nearest_never_activates_editor_fallback(self) -> None:
        self.root.joinpath("pyproject.toml").write_text(
            "[tool.graphql-desk]\nunknown=true"
        )
        with self.assertRaises(ValueError):
            effective_project(
                self.root,
                workspace=self.root,
                editor_fallback={
                    "schema": "schema.graphql",
                    "schema_output": "graphql_desk_generated/default.pyi",
                },
            )

    def test_virtual_fallback_context_does_not_follow_inactive_config_symlink(
        self,
    ) -> None:
        inactive = self.root / "inactive"
        inactive.mkdir()
        target = inactive / "pyproject.toml"
        target.write_text('[project]\nname="inactive"')
        try:
            (self.root / "pyproject.toml").symlink_to(target)
        except OSError:
            self.skipTest("Creating symlinks is not permitted on this host")
        selected = effective_project(
            self.root,
            workspace=self.root,
            editor_fallback={
                "schema": "schema.graphql",
                "schema_output": "graphql_desk_generated/default.pyi",
            },
        )
        assert selected is not None
        self.assertEqual(selected.config.root, self.root)
        self.assertEqual(
            selected.config.schemas[0].origin.local_path, self.root / "schema.graphql"
        )
        self.assertEqual(target.read_text(), '[project]\nname="inactive"')
        with self.assertRaises(ValueError):
            effective_project(
                self.root,
                workspace=self.root,
                editor_fallback={
                    "schema": "schema.graphql",
                    "schema_output": "graphql_desk_generated/default.pyi",
                    "persisted_output": "pyproject.toml",
                },
            )

    def test_nearest_configured_project_and_inactive_nested_pyproject(self) -> None:
        parent = self.write_config(self.root)
        child = self.root / "child"
        leaf = child / "src"
        leaf.mkdir(parents=True)
        (child / "pyproject.toml").write_text('[project]\nname="inactive"')
        self.assertEqual(find_project(leaf, workspace=self.root), parent)
        nested = self.write_config(child)
        self.assertEqual(find_project(leaf, workspace=self.root), nested)
        self.assertEqual(find_project(leaf), nested)
        # Overlapping source roots are legal: nearest-project selection decides.
        validate_project_ownership((parent, nested))

    def test_workspace_boundary_is_inclusive_and_does_not_read_ancestors(self) -> None:
        self.write_config(self.root)
        workspace = self.root / "workspace"
        workspace.mkdir()
        with patch(
            "graphql_desk.project.load_project_config", wraps=load_project_config
        ) as loader:
            self.assertIsNone(find_project(workspace, workspace=workspace))
            loader.assert_not_called()
        configured = self.write_config(workspace)
        self.assertEqual(find_project(workspace, workspace=workspace), configured)
        with self.assertRaises(ValueError):
            find_project(self.root, workspace=workspace)

    def test_invalid_nearer_config_never_falls_back(self) -> None:
        self.write_config(self.root)
        nested = self.root / "nested"
        nested.mkdir()
        for text in ("[tool.graphql-desk]\nunknown=true", 'SECRET="'):
            (nested / "pyproject.toml").write_text(text)
            with self.assertRaises(ValueError) as caught:
                find_project(nested, workspace=self.root)
            self.assertNotIn("SECRET", str(caught.exception))

    def test_file_input_is_not_guessed_to_be_a_directory(self) -> None:
        source = self.root / "query.py"
        source.write_text("# source")
        with self.assertRaises(ValueError):
            find_project(source, workspace=self.root)

    def test_schema_selection_requires_named_bindings(self) -> None:
        default = self.config(self.root)
        self.assertEqual(select_schema(default), default.schemas[0])
        self.assertEqual(select_schema(default, binding="default"), default.schemas[0])
        named_text = (
            '[tool.graphql-desk]\nschemas=[{name="shop",'
            'schema="shop.graphql",'
            'schema_output="graphql_desk_generated/shop.pyi"}]'
        )
        named = self.config(self.root, named_text)
        self.assertEqual(select_schema(named, binding="shop"), named.schemas[0])
        for binding in (None, "unknown", "query.py", "SECRET"):
            with self.subTest(binding=binding), self.assertRaises(ValueError) as caught:
                select_schema(named, binding=binding)
            self.assertNotIn("SECRET", str(caught.exception))
        multiple = replace(named, schemas=(default.schemas[0], *named.schemas))
        with self.assertRaises(ValueError):
            select_schema(multiple)

    def test_identical_alias_facts_dedup_but_conflicting_snapshots_fail(self) -> None:
        config = self.config(self.root)
        alias = replace(config, path=self.root / "child" / ".." / "pyproject.toml")
        # Canonical aliases still describe the same config facts after normalization.
        validate_project_ownership((config, config))
        with self.assertRaises(ValueError):
            validate_project_ownership(
                (config, replace(config, track_field_usage=False))
            )
        validate_project_ownership((config, alias))

    def test_shared_persisted_outputs_and_cross_project_inputs_are_rejected(
        self,
    ) -> None:
        first = self.config(
            self.root / "first", _BASE + 'persisted_output="../same.json"'
        )
        second = self.config(
            self.root / "second", _BASE + 'persisted_output="../same.json"'
        )
        for projects in ((first, second), (second, first)):
            with self.assertRaises(ValueError):
                validate_project_ownership(projects)
        second = self.config(
            self.root / "second", _BASE.replace('"schema.graphql"', '"../same.json"')
        )
        with self.assertRaises(ValueError):
            validate_project_ownership((first, second))

    def test_generated_trees_and_implicit_initializers_are_reserved(self) -> None:
        first = self.config(
            self.root,
            _BASE.replace("graphql_desk_generated", "child")
            + 'generated_package="child"',
        )
        second = self.config(self.root / "child")
        with self.assertRaises(ValueError):
            validate_project_ownership((first, second))
        # Sibling generated packages still share the parent's initializer.
        text = _BASE.replace("graphql_desk_generated", "child/one")
        first = self.config(self.root, text + 'generated_package="child.one"')
        second = self.config(
            self.root / "child", _BASE + 'persisted_output="__init__.py"'
        )
        with self.assertRaises(ValueError):
            validate_project_ownership((first, second))

    def test_output_directory_ancestor_collision_is_rejected(self) -> None:
        first = self.config(self.root / "first", _BASE + 'persisted_output="../out"')
        second = self.config(
            self.root / "second", _BASE + 'persisted_output="../out/data.json"'
        )
        with self.assertRaises(ValueError):
            validate_project_ownership((first, second))

    def test_workspace_rejects_config_and_directory_symlink_escapes(self) -> None:
        workspace = self.root / "workspace"
        external = self.root / "external"
        workspace.mkdir()
        config = self.write_config(external)
        link = workspace / "pyproject.toml"
        try:
            link.symlink_to(config.path)
        except OSError:
            self.skipTest("Creating symlinks is not permitted on this host")
        with patch("graphql_desk.project.load_project_config") as loader:
            with self.assertRaises(ValueError):
                find_project(workspace, workspace=workspace)
            loader.assert_not_called()
        link.unlink()
        link = workspace / "linked"
        link.symlink_to(external, target_is_directory=True)
        with self.assertRaises(ValueError):
            find_project(link, workspace=workspace)

    def test_companion_retarget_after_parse_is_rechecked(self) -> None:
        config = self.config(self.root)
        package = config.schemas[0].schema_output.parent
        package.mkdir()
        external = self.root / "external.py"
        external.write_text("# untouched")
        companion = package / "default.py"
        try:
            companion.symlink_to(external)
        except OSError:
            self.skipTest("Creating symlinks is not permitted on this host")
        with self.assertRaises(ValueError):
            validate_project_ownership((config,))
        self.assertEqual(external.read_text(), "# untouched")


if __name__ == "__main__":
    unittest.main()
