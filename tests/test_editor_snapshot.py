"""Acquire actual configured snapshots without launching or writing project code."""

import json
import tempfile
import unittest
from collections.abc import Iterable, Mapping
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from typing import cast
from unittest.mock import patch

from graphql import build_schema, introspection_from_schema

import graphql_desk.editor_snapshot as editor_snapshot
import graphql_desk.source_graph as source_graph
from graphql_desk.editor_analysis import analyze_editor_snapshot
from graphql_desk.editor_snapshot import (
    EditorSnapshot,
    OpenBuffer,
    RemoteSchemaSnapshot,
    acquire_editor_snapshot,
    is_editor_snapshot_current,
    prepare_editor_snapshot,
)
from graphql_desk.project import EffectiveProject, effective_project
from graphql_desk.source_graph import (
    FactoryBinding,
    IncrementalResolver,
    ModuleSnapshot,
    ProjectBindings,
)

_SDL = "type User { id: ID! name: String } type Query { user: User hello: String }"
_BASE = """[tool.graphql-desk]
schema="schema.graphql"
schema_output="graphql_desk_generated/default.pyi"
"""
_QUERY = 'from graphql_desk import graphql\nq=graphql("{ user { id } }")\n'


def graph_inputs(
    snapshot: EditorSnapshot,
) -> tuple[tuple[ModuleSnapshot, ...], tuple[ProjectBindings, ...]]:
    request = snapshot.request
    modules = tuple(
        ModuleSnapshot(
            cast(str, item["project"]),
            cast(str, item["module"]),
            cast(str, item["uri"]),
            cast(int, item["version"]),
            cast(str, item["source"]),
            cast(bool, item["is_package"]),
        )
        for item in cast(list[dict[str, object]], request["modules"])
    )
    bindings = tuple(
        ProjectBindings(
            cast(str, item["project"]),
            tuple(cast(list[str], item["schemas"])),
            cast(str | None, item["default_schema"]),
            tuple(
                FactoryBinding(
                    cast(str, factory["module"]),
                    cast(str, factory["export"]),
                    cast(str | None, factory["schema_name"]),
                )
                for factory in cast(list[dict[str, object]], item["factories"])
            ),
        )
        for item in cast(list[dict[str, object]], request["bindings"])
    )
    return modules, bindings


class EditorSnapshotTests(unittest.TestCase):
    def test_supplied_remote_facts_are_explicit_current_and_stale_safe(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "main.py"
            source.write_text(_QUERY, encoding="utf8")
            config = root / "pyproject.toml"
            config.write_text(
                '[tool.graphql-desk]\nschema={url="https://example.test/PRIVATE",'
                'headers={Authorization="PRIVATE"}}\n'
                'schema_output="graphql_desk_generated/default.pyi"\n',
                encoding="utf8",
            )
            prepared = prepare_editor_snapshot(workspace=root, candidates=(source,))
            origin = prepared.remote_origins[0]
            self.assertNotIn("PRIVATE", repr(prepared))
            text = json.dumps(introspection_from_schema(build_schema(_SDL)))
            remote = RemoteSchemaSnapshot(
                origin.project,
                origin.schema_name,
                origin.config_token,
                "credential-token",
                1,
                "opaque-token",
                text,
            )
            plain = acquire_editor_snapshot(
                workspace=root, candidates=(source,), generation=1
            )
            self.assertTrue(plain.unavailable_schemas)
            captured = acquire_editor_snapshot(
                workspace=root,
                candidates=(source,),
                generation=2,
                preparation=prepared,
                remote_schemas=(remote,),
            )
            self.assertFalse(captured.unavailable_schemas)
            self.assertNotIn("PRIVATE", json.dumps(captured.request))
            self.assertTrue(
                is_editor_snapshot_current(
                    captured, candidates=(source,), remote_schemas=(remote,)
                )
            )
            self.assertFalse(is_editor_snapshot_current(captured, candidates=(source,)))
            self.assertFalse(
                is_editor_snapshot_current(
                    captured,
                    candidates=(source,),
                    remote_schemas=(replace(remote, credential_token="changed"),),
                )
            )
            stale = acquire_editor_snapshot(
                workspace=root,
                candidates=(source,),
                generation=3,
                remote_schemas=(replace(remote, stale=True),),
            )
            self.assertTrue(stale.unavailable_schemas)
            self.assertEqual(stale.request["schemas"], [])
            for records in (
                (remote, remote),
                (replace(remote, config_token="foreign"),),
                (replace(remote, project="foreign"),),
            ):
                with self.assertRaises(ValueError):
                    acquire_editor_snapshot(
                        workspace=root,
                        candidates=(source,),
                        generation=4,
                        remote_schemas=records,
                    )

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()

    def write(self, name: str, text: str) -> Path:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8"))
        return path

    def project(self, prefix: str = "", text: str = _BASE) -> Path:
        self.write(prefix + "pyproject.toml", text)
        self.write(prefix + "schema.graphql", _SDL)
        return self.root / prefix

    def test_actual_project_to_analyzer_and_request_copy_isolation(self) -> None:
        self.project()
        source = self.write("q.py", _QUERY)
        snapshot = acquire_editor_snapshot(
            workspace=self.root, candidates=(source,), generation=7
        )
        request = snapshot.request
        result = analyze_editor_snapshot(request)
        self.assertEqual(result["request_id"], "7")
        self.assertEqual(result["diagnostics"], [])
        modules = cast(list[dict[str, object]], request["modules"])
        self.assertEqual(modules[0]["version"], 7)
        self.assertEqual(modules[0]["module"], "q")
        self.assertEqual(modules[0]["uri"], source.as_uri())
        self.assertEqual(set(request), {"request_id", "modules", "schemas", "bindings"})
        modules.clear()
        self.assertTrue(snapshot.request["modules"])
        with self.assertRaises(FrozenInstanceError):
            setattr(snapshot, "generation", 8)
        self.assertTrue(is_editor_snapshot_current(snapshot, candidates=(source,)))

    def test_open_imported_fragment_and_new_buffer_override_saved_sources(self) -> None:
        self.project()
        leaf = self.write(
            "leaf.py",
            'from graphql_desk import graphql\nf=graphql("fragment F on User { id }")',
        )
        query = self.write(
            "q.py",
            "from graphql_desk import graphql\nfrom leaf import f\n"
            'q=graphql("{ user { ...F } }",[f])',
        )
        new = self.root / "new.py"
        buffers = (
            OpenBuffer(
                leaf,
                4,
                "from graphql_desk import graphql\n"
                'f=graphql("fragment F on User { name }")',
                True,
            ),
            OpenBuffer(new, 2, _QUERY, True),
        )
        snapshot = acquire_editor_snapshot(
            workspace=self.root,
            candidates=(leaf, query, new),
            buffers=buffers,
            generation=9,
        )
        result = analyze_editor_snapshot(snapshot.request)
        self.assertEqual(result["diagnostics"], [])
        self.assertIn("name", json.dumps(result))
        self.assertFalse(new.exists())
        self.assertIn("{ id }", leaf.read_text(encoding="utf-8"))
        self.assertTrue(
            is_editor_snapshot_current(
                snapshot, candidates=(leaf, query, new), buffers=buffers
            )
        )
        self.assertFalse(
            is_editor_snapshot_current(
                snapshot,
                candidates=(leaf, query, new),
                buffers=(replace(buffers[0], version=5), buffers[1]),
            )
        )
        self.assertFalse(
            is_editor_snapshot_current(
                snapshot, candidates=(leaf, query, new), buffers=()
            )
        )

    def test_nearest_nested_projects_and_whole_object_fallback(self) -> None:
        self.project()
        self.project("child/")
        one = self.write("one.py", _QUERY)
        two = self.write("child/two.py", _QUERY)
        snapshot = acquire_editor_snapshot(
            workspace=self.root,
            candidates=(one, two),
            generation=1,
            editor_fallback={"unknown": True},
        )
        result = analyze_editor_snapshot(snapshot.request)
        self.assertEqual(result["diagnostics"], [])
        bindings = cast(list[dict[str, object]], snapshot.request["bindings"])
        self.assertEqual(len(bindings), 2)
        self.assertNotEqual(bindings[0]["project"], bindings[1]["project"])
        self.assertTrue(
            is_editor_snapshot_current(
                snapshot,
                candidates=(one, two),
                editor_fallback={"another invalid unused key": False},
            )
        )

    def test_dirty_root_and_new_nested_config_buffers_are_authoritative(
        self,
    ) -> None:
        self.project()
        source = self.write("child/q.py", _QUERY)
        config = self.root / "pyproject.toml"
        self.write("child/schema.graphql", _SDL)
        for buffer in (
            OpenBuffer(config, 2, _BASE, True),
            OpenBuffer(config, 2, _BASE + "\n", False),
            OpenBuffer(source.parent / "pyproject.toml", 1, _BASE, True),
            OpenBuffer(source.parent / "pyproject.toml", 1, _BASE, False),
        ):
            with self.subTest(buffer=buffer.path):
                captured = acquire_editor_snapshot(
                    workspace=self.root,
                    candidates=(source,),
                    buffers=(buffer,),
                    generation=1,
                )
                self.assertEqual(
                    analyze_editor_snapshot(captured.request)["diagnostics"], []
                )
        matching = OpenBuffer(config, 3, _BASE)
        snapshot = acquire_editor_snapshot(
            workspace=self.root, candidates=(source,), buffers=(matching,), generation=1
        )
        self.assertTrue(
            is_editor_snapshot_current(
                snapshot, candidates=(source,), buffers=(matching,)
            )
        )
        self.assertFalse(
            is_editor_snapshot_current(
                snapshot,
                candidates=(source,),
                buffers=(replace(matching, version=4),),
            )
        )

    def test_invalid_nearer_config_and_new_disk_config_invalidate(self) -> None:
        self.project()
        source = self.write("child/q.py", _QUERY)
        snapshot = acquire_editor_snapshot(
            workspace=self.root, candidates=(source,), generation=1
        )
        self.write("child/pyproject.toml", "[tool.graphql-desk]\nunknown=true")
        self.assertFalse(is_editor_snapshot_current(snapshot, candidates=(source,)))
        with self.assertRaises(ValueError):
            acquire_editor_snapshot(
                workspace=self.root, candidates=(source,), generation=2
            )
        self.project("child/")
        self.assertFalse(is_editor_snapshot_current(snapshot, candidates=(source,)))

    def test_unsaved_local_schema_and_json_snapshot_use_existing_loader(self) -> None:
        self.project()
        source = self.write("q.py", _QUERY.replace("{ id }", "{ added }"))
        schema = self.root / "schema.graphql"
        buffer = OpenBuffer(
            schema, 12, _SDL.replace("id: ID!", "id: ID! added: Int"), True
        )
        snapshot = acquire_editor_snapshot(
            workspace=self.root, candidates=(source,), buffers=(buffer,), generation=1
        )
        self.assertEqual(analyze_editor_snapshot(snapshot.request)["diagnostics"], [])
        self.assertFalse(is_editor_snapshot_current(snapshot, candidates=(source,)))
        self.write("pyproject.toml", _BASE.replace("schema.graphql", "schema.json"))
        data = introspection_from_schema(build_schema(buffer.text))
        self.write("schema.json", json.dumps(data))
        snapshot = acquire_editor_snapshot(
            workspace=self.root, candidates=(source,), generation=2
        )
        self.assertEqual(analyze_editor_snapshot(snapshot.request)["diagnostics"], [])
        self.assertEqual(
            cast(list[dict[str, object]], snapshot.request["schemas"])[0]["format"],
            "introspection",
        )

    def test_explicit_external_schema_allowed_but_external_source_rejected(
        self,
    ) -> None:
        project = self.project(
            "project/", _BASE.replace("schema.graphql", "../shared.graphql")
        )
        schema = self.write("shared.graphql", _SDL)
        source = self.write("project/q.py", _QUERY)
        snapshot = acquire_editor_snapshot(
            workspace=project,
            candidates=(source,),
            buffers=(OpenBuffer(schema, 3, _SDL, True),),
            generation=1,
        )
        self.assertEqual(analyze_editor_snapshot(snapshot.request)["diagnostics"], [])
        with self.assertRaisesRegex(ValueError, "escapes"):
            acquire_editor_snapshot(
                workspace=project,
                candidates=(self.write("outside.py", _QUERY),),
                generation=1,
            )

    def test_remote_schema_and_fallback_credentials_never_serialize(self) -> None:
        source = self.write("q.py", _QUERY)
        fallback: dict[str, object] = {
            "schema": {
                "url": "https://private.example.invalid/graphql",
                "headers": {"Authorization": "Bearer TOP_SECRET"},
            },
            "schema_output": "graphql_desk_generated/default.pyi",
        }
        snapshot = acquire_editor_snapshot(
            workspace=self.root,
            candidates=(source,),
            editor_fallback=fallback,
            generation=1,
        )
        self.assertEqual(len(snapshot.unavailable_schemas), 1)
        self.assertEqual(snapshot.request["schemas"], [])
        serialized = json.dumps(snapshot.request) + repr(snapshot)
        for secret in ("TOP_SECRET", "private.example.invalid", "Authorization"):
            self.assertNotIn(secret, serialized)
        result = analyze_editor_snapshot(snapshot.request)
        self.assertIn("missing-schema-context", json.dumps(result))
        changed = dict(fallback)
        changed["schema"] = {
            "url": "https://private.example.invalid/graphql",
            "headers": {"Authorization": "Bearer CHANGED"},
        }
        self.assertFalse(
            is_editor_snapshot_current(
                snapshot, candidates=(source,), editor_fallback=changed
            )
        )

    def test_excluded_generated_sources_not_read_and_entrypoints_need_full_input(
        self,
    ) -> None:
        self.project()
        source = self.write("q.py", _QUERY)
        excluded = self.root / "graphql_desk_generated" / "default.py"
        original_read = Path.read_bytes

        def checked(path: Path) -> bytes:
            if path == excluded:
                raise AssertionError("Excluded source was read")
            return original_read(path)

        with patch.object(Path, "read_bytes", checked):
            snapshot = acquire_editor_snapshot(
                workspace=self.root, candidates=(source, excluded), generation=1
            )
        self.assertEqual(
            len(cast(list[dict[str, object]], snapshot.request["modules"])), 1
        )
        self.write("pyproject.toml", _BASE + 'entrypoints=["q.py"]\n')
        other = self.write("other.py", _QUERY)
        with self.assertRaisesRegex(ValueError, "Entrypoint"):
            acquire_editor_snapshot(
                workspace=self.root, candidates=(other,), generation=2
            )

    def test_content_close_delete_candidates_and_schema_changes_invalidate(
        self,
    ) -> None:
        self.project()
        source = self.write("q.py", _QUERY)
        snapshot = acquire_editor_snapshot(
            workspace=self.root, candidates=(source,), generation=1
        )
        source.write_bytes(_QUERY.replace("{ id }", "{ name }").encode())
        self.assertFalse(is_editor_snapshot_current(snapshot, candidates=(source,)))
        source.write_bytes(_QUERY.encode())
        self.assertTrue(is_editor_snapshot_current(snapshot, candidates=(source,)))
        self.assertFalse(
            is_editor_snapshot_current(
                snapshot, candidates=(source, self.root / "extra.py")
            )
        )
        self.write("schema.graphql", _SDL + "\n")
        self.assertFalse(is_editor_snapshot_current(snapshot, candidates=(source,)))
        self.write("schema.graphql", _SDL)
        source.unlink()
        self.assertFalse(is_editor_snapshot_current(snapshot, candidates=(source,)))

    def test_crlf_unicode_bom_and_duplicate_open_identity(self) -> None:
        self.project()
        text = "# café \U0001f600\r\n" + _QUERY.replace("\n", "\r\n")
        source = self.write("q.py", text)
        source.write_bytes(b"\xef\xbb\xbf" + source.read_bytes())
        snapshot = acquire_editor_snapshot(
            workspace=self.root, candidates=(source,), generation=1
        )
        self.assertEqual(
            cast(list[dict[str, object]], snapshot.request["modules"])[0]["source"],
            text,
        )
        self.assertEqual(analyze_editor_snapshot(snapshot.request)["diagnostics"], [])
        with self.assertRaisesRegex(ValueError, "Conflicting"):
            acquire_editor_snapshot(
                workspace=self.root,
                candidates=(source,),
                generation=1,
                buffers=(OpenBuffer(source, 1, text), OpenBuffer(source, 2, text)),
            )

    def test_source_symlink_escape_and_retarget_invalidate(self) -> None:
        self.project(text=_BASE + 'source_roots=["src"]\n')
        source = self.write("src/q.py", _QUERY)
        outside = self.write("outside.py", _QUERY)
        alias = source.parent / "alias.py"
        try:
            alias.symlink_to(source)
        except OSError as error:
            self.skipTest(f"Host does not permit symlink fixtures: {error}")
        snapshot = acquire_editor_snapshot(
            workspace=self.root, candidates=(alias,), generation=1
        )
        self.assertTrue(is_editor_snapshot_current(snapshot, candidates=(alias,)))
        alias.unlink()
        alias.symlink_to(outside)
        self.assertFalse(is_editor_snapshot_current(snapshot, candidates=(alias,)))
        with self.assertRaisesRegex(ValueError, "symlink"):
            acquire_editor_snapshot(
                workspace=self.root, candidates=(alias,), generation=2
            )

    def test_generation_and_buffer_versions_require_actual_integers(self) -> None:
        self.project()
        source = self.write("q.py", _QUERY)
        for invalid in (1.5, True, -1):
            with self.subTest(invalid=invalid):
                with self.assertRaisesRegex(ValueError, "integers"):
                    acquire_editor_snapshot(
                        workspace=self.root,
                        candidates=(source,),
                        generation=cast(int, invalid),
                    )
                with self.assertRaisesRegex(ValueError, "integers"):
                    acquire_editor_snapshot(
                        workspace=self.root,
                        candidates=(source,),
                        generation=1,
                        buffers=(OpenBuffer(source, cast(int, invalid), _QUERY),),
                    )

    def test_config_aba_helpers_use_the_same_captured_text(self) -> None:
        self.project()
        source = self.write("q.py", _QUERY)
        config = self.root / "pyproject.toml"
        self.write("other.graphql", _SDL)

        def swap(
            directory: Path,
            *,
            workspace: Path,
            editor_fallback: object,
            config_texts: Mapping[Path, str | None],
        ) -> EffectiveProject | None:
            self.assertIsNone(editor_fallback)
            config.write_bytes(
                _BASE.replace("schema.graphql", "other.graphql").encode()
            )
            try:
                return effective_project(
                    directory, workspace=workspace, config_texts=config_texts
                )
            finally:
                config.write_bytes(_BASE.encode())

        with patch("graphql_desk.editor_snapshot.effective_project", side_effect=swap):
            snapshot = acquire_editor_snapshot(
                workspace=self.root, candidates=(source,), generation=1
            )
        # Helpers see captured A even while the disk temporarily contains B.
        schemas = cast(list[dict[str, object]], snapshot.request["schemas"])
        self.assertEqual(schemas[0]["uri"], (self.root / "schema.graphql").as_uri())
        self.assertTrue(is_editor_snapshot_current(snapshot, candidates=(source,)))
        config.write_bytes(_BASE.replace("schema.graphql", "other.graphql").encode())
        self.assertFalse(is_editor_snapshot_current(snapshot, candidates=(source,)))

    def test_change_during_acquisition_is_rejected(self) -> None:
        self.project()
        source = self.write("q.py", _QUERY)
        original_read = Path.read_bytes
        reads = 0

        def changing(path: Path) -> bytes:
            nonlocal reads
            data = original_read(path)
            if path == source:
                reads += 1
                if reads == 1:
                    source.write_bytes(_QUERY.replace("{ id }", "{ name }").encode())
            return data

        with (
            patch.object(Path, "read_bytes", changing),
            self.assertRaisesRegex(ValueError, "changed"),
        ):
            acquire_editor_snapshot(
                workspace=self.root, candidates=(source,), generation=1
            )

    def test_saved_source_change_during_dirty_overlay_acquisition_is_rejected(
        self,
    ) -> None:
        self.project()
        source = self.write("q.py", _QUERY)
        buffer = OpenBuffer(source, 3, _QUERY.replace("{ id }", "{ name }"), True)
        original_read = Path.read_bytes
        reads = 0

        def changing(path: Path) -> bytes:
            nonlocal reads
            data = original_read(path)
            if path == source:
                reads += 1
                if reads == 1:
                    source.write_bytes(_QUERY.replace("{ id }", "{ name }").encode())
            return data

        with (
            patch.object(Path, "read_bytes", changing),
            self.assertRaisesRegex(ValueError, "changed"),
        ):
            acquire_editor_snapshot(
                workspace=self.root,
                candidates=(source,),
                buffers=(buffer,),
                generation=1,
            )

    def test_saved_missing_source_appearing_during_acquisition_is_rejected(
        self,
    ) -> None:
        self.project()
        source = self.root / "q.py"
        buffer = OpenBuffer(source, 3, _QUERY, True)
        original_check = editor_snapshot._check_observations

        def appearing(
            observations: Iterable[editor_snapshot._Observation],
        ) -> None:
            captured = tuple(observations)
            if any(
                observation.path == source and observation.origin == "saved-missing"
                for observation in captured
            ):
                source.write_bytes(_QUERY.encode())
            original_check(captured)

        with (
            patch.object(editor_snapshot, "_check_observations", appearing),
            self.assertRaisesRegex(ValueError, "changed"),
        ):
            acquire_editor_snapshot(
                workspace=self.root,
                candidates=(source,),
                buffers=(buffer,),
                generation=1,
            )

    def test_dirty_root_config_changes_module_and_schema_routing(self) -> None:
        self.project()
        source = self.write("src/q.py", _QUERY.replace("{ id }", "{ added }"))
        other = self.write(
            "other.graphql", _SDL.replace("id: ID!", "id: ID! added: Int")
        )
        text = (
            _BASE.replace("schema.graphql", "other.graphql") + 'source_roots=["src"]\n'
        )
        buffer = OpenBuffer(self.root / "pyproject.toml", 9, text, True)
        snapshot = acquire_editor_snapshot(
            workspace=self.root, candidates=(source,), buffers=(buffer,), generation=1
        )
        self.assertEqual(analyze_editor_snapshot(snapshot.request)["diagnostics"], [])
        self.assertEqual(
            cast(list[dict[str, object]], snapshot.request["modules"])[0]["module"], "q"
        )
        self.assertEqual(
            cast(list[dict[str, object]], snapshot.request["schemas"])[0]["uri"],
            other.as_uri(),
        )
        self.assertFalse(is_editor_snapshot_current(snapshot, candidates=(source,)))
        self.assertTrue(
            is_editor_snapshot_current(
                snapshot, candidates=(source,), buffers=(buffer,)
            )
        )

    def test_new_nested_config_and_unsaved_inactive_table_select_consistently(
        self,
    ) -> None:
        self.project()
        source = self.write("child/q.py", _QUERY)
        self.write("child/schema.graphql", _SDL)
        path = source.parent / "pyproject.toml"
        buffer = OpenBuffer(path, 1, _BASE, True)
        snapshot = acquire_editor_snapshot(
            workspace=self.root, candidates=(source,), buffers=(buffer,), generation=1
        )
        modules = cast(list[dict[str, object]], snapshot.request["modules"])
        self.assertEqual(modules[0]["module"], "q")
        self.assertEqual(modules[0]["project"], path.as_uri())
        self.assertEqual(analyze_editor_snapshot(snapshot.request)["diagnostics"], [])
        self.assertFalse(path.exists())
        inactive = replace(buffer, version=2, text="[project]\nname='inactive'")
        next_snapshot = acquire_editor_snapshot(
            workspace=self.root, candidates=(source,), buffers=(inactive,), generation=2
        )
        modules = cast(list[dict[str, object]], next_snapshot.request["modules"])
        self.assertEqual(modules[0]["module"], "child.q")
        self.assertEqual(modules[0]["project"], (self.root / "pyproject.toml").as_uri())
        invalid = replace(buffer, text="[tool.graphql-desk]\nunknown=true")
        with self.assertRaises(ValueError):
            acquire_editor_snapshot(
                workspace=self.root,
                candidates=(source,),
                buffers=(invalid,),
                generation=3,
            )

    def test_buffered_config_cannot_adopt_a_directory_or_broken_symlink(self) -> None:
        source = self.write("q.py", _QUERY)
        config = self.root / "pyproject.toml"
        config.mkdir()
        buffer = OpenBuffer(config, 1, _BASE, True)
        with self.assertRaisesRegex(ValueError, "must be a file"):
            acquire_editor_snapshot(
                workspace=self.root,
                candidates=(source,),
                buffers=(buffer,),
                generation=1,
            )
        config.rmdir()
        try:
            config.symlink_to(self.root / "missing.toml")
        except OSError as error:
            self.skipTest(f"Host does not permit symlink fixtures: {error}")
        with self.assertRaisesRegex(ValueError, "must be a file"):
            acquire_editor_snapshot(
                workspace=self.root,
                candidates=(source,),
                buffers=(buffer,),
                generation=2,
            )

    def test_previous_snapshot_reuses_actual_incremental_project_across_generations(
        self,
    ) -> None:
        self.project()
        source = self.write("q.py", _QUERY)
        first = acquire_editor_snapshot(
            workspace=self.root, candidates=(source,), generation=1
        )
        resolver = IncrementalResolver()
        modules, bindings = graph_inputs(first)
        expected = resolver.resolve(modules, bindings=bindings)
        with (
            patch.object(
                source_graph, "_parse", wraps=getattr(source_graph, "_parse")
            ) as parse,
            patch.object(
                source_graph, "_resolve", wraps=getattr(source_graph, "_resolve")
            ) as resolve,
        ):
            previous = first
            for generation in (2, 3, 4):
                current = acquire_editor_snapshot(
                    workspace=self.root,
                    candidates=(source,),
                    generation=generation,
                    previous=previous,
                )
                modules, bindings = graph_inputs(current)
                self.assertEqual(modules[0].version, 1)
                self.assertEqual(resolver.resolve(modules, bindings=bindings), expected)
                self.assertTrue(
                    is_editor_snapshot_current(current, candidates=(source,))
                )
                self.assertNotEqual(
                    current.request["request_id"], previous.request["request_id"]
                )
                self.assertNotEqual(
                    current.request["schemas"], previous.request["schemas"]
                )
                previous = current
            self.assertEqual((parse.call_count, resolve.call_count), (0, 0))

    def test_previous_snapshot_changed_project_leaves_other_project_reusable(
        self,
    ) -> None:
        self.project("one/")
        self.project("two/")
        one = self.write("one/q.py", _QUERY)
        two = self.write("two/q.py", _QUERY)
        candidates = (one, two)
        first = acquire_editor_snapshot(
            workspace=self.root, candidates=candidates, generation=1
        )
        resolver = IncrementalResolver()
        modules, bindings = graph_inputs(first)
        resolver.resolve(modules, bindings=bindings)
        self.write("one/q.py", _QUERY.replace("id", "name"))
        second = acquire_editor_snapshot(
            workspace=self.root, candidates=candidates, generation=2, previous=first
        )
        modules, bindings = graph_inputs(second)
        self.assertEqual([item.version for item in modules], [2, 1])
        with (
            patch.object(
                source_graph, "_parse", wraps=getattr(source_graph, "_parse")
            ) as parse,
            patch.object(
                source_graph, "_resolve", wraps=getattr(source_graph, "_resolve")
            ) as resolve,
        ):
            resolver.resolve(modules, bindings=bindings)
            self.assertEqual((parse.call_count, resolve.call_count), (1, 1))
        self.assertFalse(is_editor_snapshot_current(first, candidates=candidates))
        self.assertTrue(is_editor_snapshot_current(second, candidates=candidates))

    def test_previous_snapshot_preserves_origin_and_exact_disk_byte_guards(
        self,
    ) -> None:
        self.project()
        source = self.write("q.py", _QUERY)
        first = acquire_editor_snapshot(
            workspace=self.root, candidates=(source,), generation=1
        )
        # BOM-only change decodes to identical source but is a new disk observation.
        source.write_bytes(b"\xef\xbb\xbf" + _QUERY.encode())
        second = acquire_editor_snapshot(
            workspace=self.root, candidates=(source,), generation=2, previous=first
        )
        self.assertEqual(graph_inputs(second)[0][0].version, 2)
        self.assertFalse(is_editor_snapshot_current(first, candidates=(source,)))
        buffer = OpenBuffer(source, 17, _QUERY)
        opened = acquire_editor_snapshot(
            workspace=self.root,
            candidates=(source,),
            buffers=(buffer,),
            generation=3,
            previous=second,
        )
        self.assertEqual(graph_inputs(opened)[0][0].version, 17)
        changed = replace(buffer, text=_QUERY.replace("id", "name"))
        self.assertFalse(
            is_editor_snapshot_current(opened, candidates=(source,), buffers=(changed,))
        )
        closed = acquire_editor_snapshot(
            workspace=self.root, candidates=(source,), generation=4, previous=opened
        )
        self.assertEqual(graph_inputs(closed)[0][0].version, 4)
        self.assertFalse(is_editor_snapshot_current(opened, candidates=(source,)))
        self.assertFalse(
            is_editor_snapshot_current(closed, candidates=(source,), buffers=(buffer,))
        )

    def test_previous_snapshot_does_not_hide_schema_config_or_ownership_changes(
        self,
    ) -> None:
        self.project()
        source = self.write("nested/q.py", _QUERY)
        first = acquire_editor_snapshot(
            workspace=self.root, candidates=(source,), generation=1
        )
        self.write("schema.graphql", _SDL + "\nscalar Extra")
        second = acquire_editor_snapshot(
            workspace=self.root, candidates=(source,), generation=2, previous=first
        )
        self.assertEqual(graph_inputs(second)[0], graph_inputs(first)[0])
        self.assertFalse(is_editor_snapshot_current(first, candidates=(source,)))
        self.write("pyproject.toml", _BASE + "# changed config capture\n")
        third = acquire_editor_snapshot(
            workspace=self.root, candidates=(source,), generation=3, previous=second
        )
        self.assertEqual(graph_inputs(third)[0], graph_inputs(second)[0])
        self.assertFalse(is_editor_snapshot_current(second, candidates=(source,)))
        self.project("nested/")
        fourth = acquire_editor_snapshot(
            workspace=self.root, candidates=(source,), generation=4, previous=third
        )
        self.assertNotEqual(
            graph_inputs(fourth)[0][0].project, graph_inputs(third)[0][0].project
        )
        self.assertEqual(graph_inputs(fourth)[0][0].version, 4)
        self.assertFalse(is_editor_snapshot_current(third, candidates=(source,)))

    def test_previous_snapshot_baseline_validation_and_no_baseline_behavior(
        self,
    ) -> None:
        self.project()
        source = self.write("q.py", _QUERY)
        first = acquire_editor_snapshot(
            workspace=self.root, candidates=(source,), generation=5
        )
        for previous in (
            replace(first, workspace=self.root / "other"),
            replace(first, generation=6),
        ):
            with self.subTest(previous=previous):
                with self.assertRaisesRegex(ValueError, "Previous snapshot"):
                    acquire_editor_snapshot(
                        workspace=self.root,
                        candidates=(source,),
                        generation=5,
                        previous=previous,
                    )
        no_baseline = acquire_editor_snapshot(
            workspace=self.root, candidates=(source,), generation=6
        )
        self.assertEqual(graph_inputs(no_baseline)[0][0].version, 6)
        empty = acquire_editor_snapshot(
            workspace=self.root, candidates=(), generation=7, previous=first
        )
        self.assertEqual(graph_inputs(empty), ((), ()))
        self.assertTrue(is_editor_snapshot_current(empty, candidates=()))

    def test_preparation_lists_external_missing_schema_without_reading_contents(
        self,
    ) -> None:
        for suffix, schema_text in (
            (".graphql", _SDL),
            (".json", json.dumps(introspection_from_schema(build_schema(_SDL)))),
        ):
            with self.subTest(suffix=suffix):
                external = self.root.parent / (self.root.name + "-missing" + suffix)
                self.project(text=_BASE.replace("schema.graphql", external.as_posix()))
                source = self.root / "new.py"
                with patch.object(
                    editor_snapshot, "_read", wraps=getattr(editor_snapshot, "_read")
                ) as read:
                    prepared = prepare_editor_snapshot(
                        workspace=self.root, candidates=(source,)
                    )
                    self.assertTrue(
                        all(
                            call.args[0].name == "pyproject.toml"
                            for call in read.call_args_list
                        )
                    )
                self.assertEqual(len(prepared.local_schemas), 1)
                schema = prepared.local_schemas[0]
                self.assertEqual(
                    (schema.path, schema.canonical_path),
                    (external.resolve(), external.resolve()),
                )
                self.assertEqual(schema.schema_name, "default")
                self.assertEqual(
                    schema.project, (self.root / "pyproject.toml").as_uri()
                )
                with self.assertRaises(FrozenInstanceError):
                    setattr(prepared, "local_schemas", ())
                buffers = (
                    OpenBuffer(source, 1, _QUERY, True),
                    OpenBuffer(external, 9, schema_text, True),
                )
                snapshot = acquire_editor_snapshot(
                    workspace=self.root,
                    candidates=(source,),
                    buffers=buffers,
                    generation=1,
                    preparation=prepared,
                )
                self.assertEqual(
                    analyze_editor_snapshot(snapshot.request)["diagnostics"], []
                )
                self.assertFalse(source.exists())
                self.assertFalse(external.exists())
                with self.assertRaisesRegex(ValueError, "Cannot read"):
                    acquire_editor_snapshot(
                        workspace=self.root,
                        candidates=(source,),
                        buffers=(buffers[0],),
                        generation=2,
                        preparation=prepared,
                    )

    def test_preparation_named_dirty_config_and_unsupported_schema_manifest(
        self,
    ) -> None:
        self.project()
        source = self.write("q.py", _QUERY)
        config = """[tool.graphql-desk]
[[tool.graphql-desk.schemas]]
name="local"
schema="missing.unsupported"
schema_output="graphql_desk_generated/local.pyi"
[[tool.graphql-desk.schemas]]
name="second"
schema="second.json"
schema_output="graphql_desk_generated/second.pyi"
[[tool.graphql-desk.schemas]]
name="remote"
schema={url="https://example.test/SECRET", headers={Authorization="TOP_SECRET"}}
schema_output="graphql_desk_generated/remote.pyi"
"""
        buffer = OpenBuffer(self.root / "pyproject.toml", 2, config, True)
        prepared = prepare_editor_snapshot(
            workspace=self.root, candidates=(source,), buffers=(buffer,)
        )
        self.assertEqual(
            [item.schema_name for item in prepared.local_schemas], ["local", "second"]
        )
        self.assertEqual(
            [item.path.name for item in prepared.local_schemas],
            ["missing.unsupported", "second.json"],
        )
        self.assertNotIn("SECRET", repr(prepared))
        with self.assertRaisesRegex(ValueError, "Unsupported local schema format"):
            acquire_editor_snapshot(
                workspace=self.root,
                candidates=(source,),
                buffers=(buffer,),
                generation=1,
                preparation=prepared,
            )

    def test_preparation_rejects_config_phase_changes_before_source_reads(self) -> None:
        self.project()
        source = self.write("q.py", _QUERY)
        prepared = prepare_editor_snapshot(workspace=self.root, candidates=(source,))
        self.write("pyproject.toml", _BASE + "# new capture\n")
        with patch.object(
            editor_snapshot, "_read", wraps=getattr(editor_snapshot, "_read")
        ) as read:
            with self.assertRaisesRegex(
                ValueError, "Prepared editor configuration changed"
            ):
                acquire_editor_snapshot(
                    workspace=self.root,
                    candidates=(source,),
                    generation=1,
                    preparation=prepared,
                )
            self.assertTrue(
                all(
                    call.args[0].name == "pyproject.toml"
                    for call in read.call_args_list
                )
            )
        # Same text from a newly opened config buffer is still a changed origin.
        self.write("pyproject.toml", _BASE)
        with self.assertRaisesRegex(
            ValueError, "Prepared editor configuration changed"
        ):
            acquire_editor_snapshot(
                workspace=self.root,
                candidates=(source,),
                buffers=(OpenBuffer(self.root / "pyproject.toml", 1, _BASE),),
                generation=1,
                preparation=prepared,
            )

    def test_preparation_schema_syntax_is_analyzer_work_and_new_nested_config_rejects(
        self,
    ) -> None:
        self.project()
        source = self.write("nested/q.py", _QUERY)
        self.write("schema.graphql", "not valid SDL")
        prepared = prepare_editor_snapshot(workspace=self.root, candidates=(source,))
        snapshot = acquire_editor_snapshot(
            workspace=self.root,
            candidates=(source,),
            generation=1,
            preparation=prepared,
        )
        with self.assertRaisesRegex(ValueError, "Invalid supplied local schema"):
            analyze_editor_snapshot(snapshot.request)
        nested = OpenBuffer(self.root / "nested/pyproject.toml", 1, _BASE, True)
        with self.assertRaisesRegex(
            ValueError, "Prepared editor configuration changed"
        ):
            acquire_editor_snapshot(
                workspace=self.root,
                candidates=(source,),
                buffers=(nested,),
                generation=2,
                preparation=prepared,
            )

    def test_preparation_preserves_fallback_validation_and_credential_changes(
        self,
    ) -> None:
        source = self.write("q.py", _QUERY)
        fallback: dict[str, object] = {
            "schema": {
                "url": "https://example.test/SECRET",
                "headers": {"Authorization": "SECRET"},
            },
            "schema_output": "graphql_desk_generated/default.pyi",
        }
        prepared = prepare_editor_snapshot(
            workspace=self.root, candidates=(source,), editor_fallback=fallback
        )
        self.assertEqual(prepared.local_schemas, ())
        self.assertNotIn("SECRET", repr(prepared))
        snapshot = acquire_editor_snapshot(
            workspace=self.root,
            candidates=(source,),
            editor_fallback=fallback,
            preparation=prepared,
            generation=1,
        )
        self.assertEqual(snapshot.unavailable_schemas[0][1], "default")
        changed = {
            **fallback,
            "schema": {
                "url": "https://example.test/SECRET",
                "headers": {"Authorization": "CHANGED"},
            },
        }
        with self.assertRaisesRegex(
            ValueError, "Prepared editor configuration changed"
        ):
            acquire_editor_snapshot(
                workspace=self.root,
                candidates=(source,),
                editor_fallback=changed,
                preparation=prepared,
                generation=2,
            )
        self.write("pyproject.toml", "[tool.graphql-desk]\nunknown=true")
        with self.assertRaises(ValueError):
            prepare_editor_snapshot(workspace=self.root, candidates=(source,))


if __name__ == "__main__":
    unittest.main()
