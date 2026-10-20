"""Acquire actual configured snapshots without launching or writing project code."""

import json
import tempfile
import unittest
from collections.abc import Mapping
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from typing import cast
from unittest.mock import patch

from graphql import build_schema, introspection_from_schema

from graphql_desk.editor_analysis import analyze_editor_snapshot
from graphql_desk.editor_snapshot import (
    OpenBuffer,
    acquire_editor_snapshot,
    is_editor_snapshot_current,
)
from graphql_desk.project import EffectiveProject, effective_project

_SDL = "type User { id: ID! name: String } type Query { user: User hello: String }"
_BASE = """[tool.graphql-desk]
schema="schema.graphql"
schema_output="graphql_desk_generated/default.pyi"
"""
_QUERY = 'from graphql_desk import graphql\nq=graphql("{ user { id } }")\n'


class EditorSnapshotTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
