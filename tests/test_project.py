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
    project_bindings,
    schema_for_call,
    select_schema,
    validate_project_ownership,
)
from graphql_desk.source_graph import ModuleSnapshot, resolve_project

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

    def test_configured_factory_facts_and_real_call_schema_routing(self) -> None:
        config = self.config(
            self.root,
            """[tool.graphql-desk]
generated_package="generated.types"
schemas=[{name="shop",schema="shop.graphql",schema_output="generated/types/shop.pyi"},
{name="users",schema="users.graphql",schema_output="generated/types/users.pyi"}]
""",
        )
        facts = project_bindings(config, project_id="workspace-project")
        self.assertEqual(facts.schemas, ("shop", "users"))
        self.assertIsNone(facts.default_schema)
        self.assertEqual(
            tuple(
                (item.module, item.export, item.schema_name) for item in facts.factories
            ),
            (
                ("generated.types.shop", "graphql", "shop"),
                ("generated.types.users", "graphql", "users"),
            ),
        )
        snapshot = ModuleSnapshot(
            "workspace-project",
            "queries",
            "file:///queries.py",
            7,
            'from generated.types.shop import graphql\nusers=graphql("{ id }")',
        )
        resolved = resolve_project((snapshot,), bindings=(facts,))
        self.assertEqual(resolved.diagnostics, ())
        call = resolved.calls[0]
        self.assertEqual(call.binding, "users")
        self.assertEqual(call.schema_name, "shop")
        before = (call, call.dependencies, call.contributions)
        self.assertIs(
            schema_for_call(config, call, project_id="workspace-project"),
            config.schemas[0],
        )
        self.assertEqual(before, (call, call.dependencies, call.contributions))
        self.assertEqual(call.module.version, 7)
        self.assertEqual(call.module.uri, "file:///queries.py")
        with self.assertRaises(ValueError):
            schema_for_call(config, call, project_id="other-project")
        with self.assertRaises(ValueError):
            schema_for_call(
                config,
                replace(call, schema_name="unknown"),
                project_id="workspace-project",
            )
        with self.assertRaises(ValueError):
            schema_for_call(
                config, replace(call, schema_name=None), project_id="workspace-project"
            )

    def test_default_template_facts_do_not_leak_credentials_or_invent_gql_export(
        self,
    ) -> None:
        config = self.config(
            self.root,
            _BASE.replace(
                '"schema.graphql"',
                '{url="https://example.test?secret=SECRET",headers={Authorization="SECRET"}}',
            )
            + 'template="custom.query"',
        )
        facts = project_bindings(config, project_id="project")
        self.assertEqual(facts.default_schema, "default")
        self.assertNotIn("SECRET", repr(facts))
        self.assertFalse(any(item.export == "gql" for item in facts.factories))
        for source in (
            'from custom import query\nresult=query("{ id }")',
            'from graphql_desk import gql\nresult=gql("{ id }")',
            "from graphql_desk_generated.default import graphql\n"
            'result=graphql("{ id }")',
        ):
            result = resolve_project(
                (ModuleSnapshot("project", "queries", "file:///q.py", 1, source),),
                bindings=(facts,),
            )
            self.assertEqual(result.diagnostics, ())
            self.assertEqual(len(result.calls), 1)
            self.assertIs(
                schema_for_call(config, result.calls[0], project_id="project"),
                config.schemas[0],
            )

    def test_template_identity_dedup_reserved_initializer_and_project_separation(
        self,
    ) -> None:
        config = self.config(
            self.root, _BASE + 'template="graphql_desk_generated.default.graphql"'
        )
        facts = project_bindings(config, project_id="one")
        self.assertEqual(len(facts.factories), 1)
        self.assertEqual(facts.factories[0].schema_name, "default")
        with self.assertRaises(ValueError):
            project_bindings(
                replace(config, template="graphql_desk.init_graphql"), project_id="one"
            )
        with self.assertRaises(ValueError):
            project_bindings(config, project_id="")
        second = project_bindings(config, project_id="two")
        results = resolve_project(
            (
                ModuleSnapshot(
                    "one",
                    "q",
                    "file:///q.py",
                    1,
                    'from graphql_desk import graphql\nq=graphql("{ first }")',
                ),
                ModuleSnapshot(
                    "two",
                    "q",
                    "file:///q.py",
                    2,
                    'from graphql_desk import graphql\nq=graphql("{ second }")',
                ),
            ),
            bindings=(facts, second),
        )
        self.assertEqual(len(results.calls), 2)
        self.assertEqual(results.diagnostics, ())
        with self.assertRaises(ValueError):
            schema_for_call(config, results.calls[1], project_id="one")

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
        self.assertEqual(
            selected.config, replace(explicit, source_scope="explicit_cli")
        )
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

    def test_explicit_source_scope_survives_only_its_selected_config_reload(
        self,
    ) -> None:
        project = self.root / "project"
        project.mkdir()
        path = project / "pyproject.toml"
        path.write_text(_BASE + 'source_roots=["../external"]', encoding="utf-8")
        with self.assertRaises(ValueError):
            find_project(project)
        selected = effective_project(project, explicit_config=path)
        assert selected is not None
        self.assertEqual(selected.config.source_scope, "explicit_cli")
        reloaded = find_project(project, selected_config=selected.config)
        self.assertEqual(reloaded, selected.config)
        foreign = project / "nested"
        foreign.mkdir()
        (foreign / "pyproject.toml").write_text(
            _BASE + 'source_roots=["../../external"]', encoding="utf-8"
        )
        with self.assertRaises(ValueError):
            find_project(foreign, selected_config=selected.config)
        fallback = {
            "schema": "schema.graphql",
            "schema_output": "graphql_desk_generated/default.pyi",
            "source_roots": ["../external"],
        }
        workspace = self.root / "workspace"
        workspace.mkdir()
        with self.assertRaises(ValueError):
            effective_project(workspace, workspace=workspace, editor_fallback=fallback)

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

    def test_closed_config_capture_overrides_disk_and_requires_absence_records(
        self,
    ) -> None:
        parent = self.write_config(self.root)
        child = self.root / "child"
        child.mkdir()
        child_path = child / "pyproject.toml"
        captured = {parent.path: _BASE, child_path: None}
        self.assertEqual(
            find_project(child, workspace=self.root, config_texts=captured), parent
        )
        captured[child_path] = _BASE
        nested = find_project(child, workspace=self.root, config_texts=captured)
        assert nested is not None
        self.assertEqual(nested.root, child)
        self.assertFalse(child_path.exists())
        with self.assertRaisesRegex(ValueError, "not captured"):
            find_project(child, workspace=self.root, config_texts={parent.path: _BASE})
        captured[child_path] = "[tool.graphql-desk]\nunknown=true"
        with self.assertRaises(ValueError):
            find_project(child, workspace=self.root, config_texts=captured)
        # Captured inactive/absent facts never reread the invalid saved table.
        parent.path.write_text("[tool.graphql-desk]\nunknown=true", encoding="utf-8")
        self.assertIsNone(
            find_project(
                self.root, workspace=self.root, config_texts={parent.path: None}
            )
        )

    def test_capture_alias_conflict_and_effective_whole_object_precedence(self) -> None:
        path = self.root / "pyproject.toml"
        alias = self.root / "child" / ".." / "pyproject.toml"
        with self.assertRaisesRegex(ValueError, "aliases"):
            find_project(
                self.root, workspace=self.root, config_texts={path: _BASE, alias: None}
            )
        selected = effective_project(
            self.root,
            workspace=self.root,
            config_texts={path: _BASE},
            editor_fallback={"unknown": True},
        )
        assert selected is not None
        self.assertEqual(selected.origin, "nearest")
        fallback = effective_project(
            self.root,
            workspace=self.root,
            config_texts={path: "[project]\nname='inactive'"},
            editor_fallback={
                "schema": "fallback.graphql",
                "schema_output": "graphql_desk_generated/default.pyi",
            },
        )
        assert fallback is not None
        self.assertEqual(fallback.origin, "editor_fallback")
        self.assertEqual(
            fallback.config.schemas[0].origin.local_path, self.root / "fallback.graphql"
        )

    def test_explicit_and_selected_scope_is_preserved_with_captured_config(
        self,
    ) -> None:
        path = self.root / "pyproject.toml"
        text = _BASE + 'source_roots=["../shared"]\n'
        capture = {path: text}
        with self.assertRaises(ValueError):
            find_project(self.root, workspace=self.root, config_texts=capture)
        selected = effective_project(
            self.root, explicit_config=path, workspace=self.root, config_texts=capture
        )
        assert selected is not None
        self.assertEqual(selected.config.source_scope, "explicit_cli")
        self.assertEqual(
            find_project(
                self.root,
                workspace=self.root,
                selected_config=selected.config,
                config_texts=capture,
            ),
            selected.config,
        )

    def test_captured_absence_is_authoritative_even_over_a_disk_directory(self) -> None:
        path = self.root / "pyproject.toml"
        path.mkdir()
        with self.assertRaisesRegex(ValueError, "must be a file"):
            find_project(self.root, workspace=self.root)
        self.assertIsNone(
            find_project(self.root, workspace=self.root, config_texts={path: None})
        )


if __name__ == "__main__":
    unittest.main()
