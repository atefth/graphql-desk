"""Real read-only worker protocol, schema capture and stale-job boundaries."""

import io
import json
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Mapping
from copy import deepcopy
from pathlib import Path
from typing import cast
from unittest.mock import patch
from uuid import UUID

from graphql import GraphQLSchema, build_schema, introspection_from_schema

from graphql_desk.config import SchemaOrigin
from graphql_desk.editor_worker import MAX_LINE_BYTES, EditorWorker, serve
from tests.test_schema_url import endpoint


class EditorWorkerTests(unittest.TestCase):
    def remote_capture(self, url: str) -> tuple[dict[str, object], str, str]:
        self.config.write_text(
            '[tool.graphql-desk]\nschema={url="' + url + '/PRIVATE_MARKER",'
            'headers={"X-Key"="${env:TOKEN}"}}\n'
            'schema_output="graphql_desk_generated/default.pyi"\n',
            encoding="utf8",
        )
        self.source.write_text(
            'from graphql_desk import gql\nq=gql("{title}")\n', encoding="utf8"
        )
        capture = self.capture()
        return capture, self.prepare(capture), self.config.as_uri()

    def refresh(
        self,
        capture: dict[str, object],
        identity: str,
        project: str,
        revision: str = "credentials-1",
        token: str = "PRIVATE_MARKER",
        schema_name: str = "default",
    ) -> dict[str, object]:
        result = self.worker.handle(
            {
                "op": "refresh",
                **capture,
                "id": identity,
                "schema_buffers": [],
                "project": project,
                "schema_name": schema_name,
                "credential_revision": revision,
                "environment": {"TOKEN": token},
            }
        )
        self.assertNotIn("PRIVATE_MARKER", json.dumps(result))
        return result

    def test_explicit_http_refresh_analysis_and_visible_stale_last_good(self) -> None:
        with endpoint() as (url, server):
            capture, identity, project = self.remote_capture(url)
            self.assertEqual(server.records, [])
            initial = self.analyze(capture, identity)
            self.assertEqual(cast(dict[str, object], initial["request"])["schemas"], [])
            loaded = self.refresh(capture, identity, project)
            self.assertEqual(loaded["status"], "ready")
            self.assertNotEqual(loaded["id"], identity)
            self.assertEqual(len(server.records), 2)
            revisions = [
                {
                    "project": project,
                    "schema_name": "default",
                    "revision": "credentials-1",
                }
            ]
            request = {
                "op": "analyze",
                **capture,
                "id": loaded["id"],
                "schema_buffers": [],
                "remote_revisions": revisions,
            }
            analyzed = self.worker.handle(request)
            self.assertTrue(analyzed["ok"], analyzed)
            facts = cast(dict[str, object], analyzed["request"])
            schemas = cast(list[dict[str, object]], facts["schemas"])
            self.assertEqual(len(schemas), 1)
            self.assertTrue(str(schemas[0]["uri"]).startswith("untitled:"))
            response = cast(dict[str, object], analyzed["response"])
            self.assertEqual(len(cast(list[object], response["schemas"])), 1)
            self.assertNotIn("PRIVATE_MARKER", json.dumps(analyzed))
            self.assertEqual(
                self.worker.handle({**request, "op": "current"}),
                {"ok": True, "current": True},
            )
            self.assertEqual(len(server.records), 2)
            server.mode = "auth"
            stale = self.refresh(capture, cast(str, loaded["id"]), project)
            self.assertEqual(stale["status"], "stale")
            self.assertTrue(stale["last_good"])
            self.assertEqual(stale["error"], "schema-refresh-failed")
            analyzed = self.worker.handle({**request, "id": stale["id"]})
            self.assertTrue(analyzed["ok"], analyzed)
            self.assertEqual(
                cast(dict[str, object], analyzed["request"])["schemas"], []
            )
            self.assertEqual(
                analyzed["remote_schemas"],
                [{"project": project, "schema_name": "default", "status": "stale"}],
            )
            # Different explicit credentials never inherit the previous last-good.
            failed = self.refresh(
                capture,
                cast(str, stale["id"]),
                project,
                revision="credentials-2",
                token="changed",
            )
            self.assertEqual(failed["status"], "unavailable")
            self.assertFalse(failed["last_good"])
            self.assertFalse(self.worker.handle({**request, "id": failed["id"]})["ok"])

    def test_refresh_rejects_changed_config_and_old_jobs(self) -> None:
        from graphql_desk.schema_url import load_from_url

        with endpoint() as (url, _server):
            capture, identity, project = self.remote_capture(url)
            loaded = self.refresh(capture, identity, project)
            self.assertFalse(self.analyze(capture, identity)["ok"])
            capture, identity, project = self.remote_capture(url)

            def change_config(
                origin: SchemaOrigin, *, environment: Mapping[str, str]
            ) -> GraphQLSchema:
                result = load_from_url(origin, environment=environment)
                self.config.write_text(
                    self.config.read_text(encoding="utf8") + "# changed during fetch\n",
                    encoding="utf8",
                )
                return result

            with patch(
                "graphql_desk.editor_worker.load_from_url", side_effect=change_config
            ):
                result = self.refresh(capture, identity, project)
            self.assertEqual(result, {"ok": False, "error": "editor-request-failed"})
            self.assertNotEqual(loaded["id"], identity)

    def test_last_good_never_crosses_environment_or_config_capture(self) -> None:
        with endpoint() as (url, server):
            capture, identity, project = self.remote_capture(url)
            loaded = self.refresh(capture, identity, project)
            server.mode = "auth"
            changed = self.refresh(
                capture,
                cast(str, loaded["id"]),
                project,
                token="changed-with-same-revision",
            )
            self.assertEqual(changed["status"], "unavailable")
            self.assertFalse(changed["last_good"])
            self.config.write_text(
                self.config.read_text(encoding="utf8") + "# new closed capture\n",
                encoding="utf8",
            )
            identity = self.prepare(capture)
            failed = self.refresh(capture, identity, project)
            self.assertEqual(failed["status"], "unavailable")
            self.assertFalse(failed["last_good"])

    def test_remote_target_must_be_prepared_before_network(self) -> None:
        capture = self.capture()
        identity = self.prepare(capture)
        with patch("graphql_desk.editor_worker.load_from_url") as loader:
            result = self.refresh(capture, identity, self.config.as_uri())
            self.assertFalse(result["ok"])
            loader.assert_not_called()

    def test_failed_named_schema_never_substitutes_another_ready_schema(self) -> None:
        with endpoint() as (url, server):
            self.config.write_text(
                "[tool.graphql-desk]\n"
                + "".join(
                    '[[tool.graphql-desk.schemas]]\nname="' + name + '"\n'
                    'schema="'
                    + url
                    + '"\nschema_output="graphql_desk_generated/'
                    + name
                    + '.pyi"\n'
                    for name in ("alpha", "beta")
                ),
                encoding="utf8",
            )
            self.source.write_text(
                "from graphql_desk_generated.alpha import graphql as a\n"
                "from graphql_desk_generated.beta import graphql as b\n"
                'first=a("{title}")\nsecond=b("{title}")\n',
                encoding="utf8",
            )
            capture, project = self.capture(), self.config.as_uri()
            loaded = self.refresh(
                capture, self.prepare(capture), project, schema_name="alpha"
            )
            server.mode = "auth"
            failed = self.refresh(
                capture, cast(str, loaded["id"]), project, schema_name="beta"
            )
            self.assertEqual(failed["status"], "unavailable")
            result = self.worker.handle(
                {
                    "op": "analyze",
                    **capture,
                    "id": failed["id"],
                    "schema_buffers": [],
                    "remote_revisions": [
                        {
                            "project": project,
                            "schema_name": name,
                            "revision": "credentials-1",
                        }
                        for name in ("alpha", "beta")
                    ],
                }
            )
            self.assertTrue(result["ok"], result)
            schemas = cast(
                list[dict[str, object]],
                cast(dict[str, object], result["request"])["schemas"],
            )
            self.assertEqual([schema["schema_name"] for schema in schemas], ["alpha"])
            self.assertEqual(result["unavailable_schemas"], ((project, "beta"),))

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.root = self.base / "workspace"
        self.root.mkdir()
        self.schema = self.base / "outside.graphql"
        self.schema.write_text("type Query { disk: String }", encoding="utf8")
        self.source = self.root / "main.py"
        self.source.write_text(
            'raise RuntimeError("DO_NOT_EXECUTE")\n'
            'from graphql_desk import gql\nq=gql("{ value }")\n',
            encoding="utf8",
        )
        self.config = self.root / "pyproject.toml"
        self.config.write_text(
            '[tool.graphql-desk]\nschema="../outside.graphql"\n'
            'schema_output="graphql_desk_generated/default.pyi"\n',
            encoding="utf8",
        )
        self.worker = EditorWorker()

    def capture(self) -> dict[str, object]:
        return {
            "workspace": str(self.root),
            "candidates": [str(self.source)],
            "buffers": [],
            "documents": [],
            "editor_fallback": None,
        }

    def document(self, path: Path, version: int = 2) -> dict[str, object]:
        return {
            "id": "schema",
            "path": str(path),
            "version": version,
            "is_dirty": True,
        }

    def buffer(self, path: Path, text: str, version: int = 2) -> dict[str, object]:
        return {
            "path": str(path),
            "version": version,
            "text": text,
            "is_dirty": True,
        }

    def prepare(self, capture: dict[str, object]) -> str:
        result = self.worker.handle({"op": "prepare", **capture})
        self.assertTrue(result["ok"], result)
        return cast(str, result["id"])

    def analyze(
        self,
        capture: dict[str, object],
        identity: str,
        buffers: list[dict[str, object]] | None = None,
    ) -> dict[str, object]:
        return self.worker.handle(
            {
                "op": "analyze",
                **capture,
                "id": identity,
                "schema_buffers": buffers or [],
            }
        )

    def test_dirty_external_sdl_and_final_recheck(self) -> None:
        capture = self.capture()
        capture["documents"] = [self.document(self.schema)]
        result = self.worker.handle({"op": "prepare", **capture})
        self.assertEqual(result["required_buffers"], ["schema"])
        buffers = [self.buffer(self.schema, "# 😀\ntype Query { value: String }")]
        identity = cast(str, result["id"])
        analyzed = self.analyze(capture, identity, buffers)
        self.assertTrue(analyzed["ok"], analyzed)
        request = cast(dict[str, object], analyzed["request"])
        schema = cast(list[dict[str, object]], request["schemas"])[0]
        self.assertEqual(schema["text"], buffers[0]["text"])
        self.assertEqual(schema["version"], 2)
        current = {
            "op": "current",
            **capture,
            "id": identity,
            "schema_buffers": buffers,
        }
        self.assertEqual(self.worker.handle(current), {"ok": True, "current": True})
        self.source.write_text("# changed", encoding="utf8")
        self.assertEqual(self.worker.handle(current), {"ok": True, "current": False})
        self.assertFalse(self.worker.handle(current)["ok"])
        self.assertEqual(
            self.schema.read_text(encoding="utf8"), "type Query { disk: String }"
        )

    def test_missing_external_introspection_from_buffer(self) -> None:
        schema = self.base / "missing.json"
        self.config.write_text(
            self.config.read_text(encoding="utf8").replace(
                "outside.graphql", "missing.json"
            ),
            encoding="utf8",
        )
        capture = self.capture()
        capture["documents"] = [self.document(schema)]
        identity = self.prepare(capture)
        text = json.dumps(
            introspection_from_schema(build_schema("type Query { value: String }"))
        )
        result = self.analyze(capture, identity, [self.buffer(schema, text)])
        self.assertTrue(result["ok"], result)
        self.assertFalse(schema.exists())

    def test_missing_invalid_and_named_schema_paths_in_preparation(self) -> None:
        self.config.write_text(
            "[tool.graphql-desk]\n[[tool.graphql-desk.schemas]]\n"
            'name="first"\nschema="../missing.graphql"\n'
            'schema_output="graphql_desk_generated/first.pyi"\n'
            "[[tool.graphql-desk.schemas]]\n"
            'name="second"\nschema="../invalid.xyz"\n'
            'schema_output="graphql_desk_generated/second.pyi"\n',
            encoding="utf8",
        )
        result = self.worker.handle({"op": "prepare", **self.capture()})
        self.assertTrue(result["ok"], result)
        paths = cast(list[dict[str, object]], result["local_schemas"])
        self.assertEqual({entry["schema_name"] for entry in paths}, {"first", "second"})
        failed = self.analyze(self.capture(), cast(str, result["id"]))
        self.assertEqual(failed, {"ok": False, "error": "editor-request-failed"})

    def test_schema_text_metadata_and_complete_open_set_required(self) -> None:
        for mutation in ("missing", "version", "extra", "new-open", "dirty"):
            with self.subTest(mutation=mutation):
                capture = self.capture()
                capture["documents"] = [self.document(self.schema)]
                identity = self.prepare(capture)
                buffers = [self.buffer(self.schema, "type Query { value: String }")]
                if mutation == "missing":
                    buffers = []
                elif mutation == "version":
                    buffers[0]["version"] = 3
                elif mutation == "extra":
                    buffers.append(
                        self.buffer(self.base / "unrelated.graphql", "PRIVATE")
                    )
                elif mutation == "new-open":
                    capture["documents"] = []
                else:
                    buffers[0]["is_dirty"] = False
                self.assertFalse(self.analyze(capture, identity, buffers)["ok"])

    def test_changed_config_source_buffer_and_workspace(self) -> None:
        capture = self.capture()
        identity = self.prepare(capture)
        self.config.write_text(
            self.config.read_text(encoding="utf8") + "disable_masking=true\n",
            encoding="utf8",
        )
        self.assertFalse(self.analyze(capture, identity)["ok"])
        capture["documents"] = [self.document(self.source)]
        capture["buffers"] = [self.buffer(self.source, "# unsaved")]
        identity = self.prepare(capture)
        changed = deepcopy(capture)
        cast(list[dict[str, object]], changed["buffers"])[0]["text"] = "# changed"
        self.assertFalse(self.analyze(changed, identity)["ok"])
        changed["workspace"] = str(self.base)
        self.assertFalse(self.worker.handle({"op": "prepare", **changed})["ok"])

    def test_ids_supersession_failure_restart_and_previous_versions(self) -> None:
        capture = self.capture()
        first = self.prepare(capture)
        initial = self.analyze(capture, first)
        self.assertTrue(initial["ok"])
        second = self.prepare(capture)
        result = self.analyze(capture, second)
        self.assertTrue(result["ok"])
        before = cast(dict[str, object], initial["request"])
        after = cast(dict[str, object], result["request"])
        self.assertEqual(before["modules"], after["modules"])
        self.assertNotEqual(before["request_id"], after["request_id"])
        self.assertFalse(self.analyze(capture, first)["ok"])
        self.assertFalse(self.analyze(capture, second)["ok"])
        self.worker = EditorWorker()
        self.assertFalse(self.analyze(capture, second)["ok"])
        self.assertNotEqual(self.prepare(capture), second)

    def test_strict_capture_and_unrelated_buffer_rejection(self) -> None:
        for field, value in (
            ("workspace", "."),
            ("candidates", ["relative.py"]),
            ("documents", [{**self.document(self.schema), "version": True}]),
            ("buffers", [self.buffer(self.schema, "SECRET")]),
            ("editor_fallback", []),
        ):
            with self.subTest(field=field):
                self.assertFalse(
                    self.worker.handle(
                        {"op": "prepare", **self.capture(), field: value}
                    )["ok"]
                )
        self.assertFalse(self.worker.handle({"op": []})["ok"])

    def test_canonical_schema_aliases_and_conflicting_metadata(self) -> None:
        alias = self.base / "alias.graphql"
        try:
            alias.symlink_to(self.schema)
        except OSError:
            self.skipTest("File symlink creation unavailable")
        capture = self.capture()
        capture["documents"] = [self.document(alias)]
        identity = self.prepare(capture)
        self.assertTrue(
            self.analyze(
                capture, identity, [self.buffer(alias, "type Query { value: String }")]
            )["ok"]
        )
        capture["documents"] = [
            self.document(alias),
            {**self.document(self.schema), "id": "other"},
        ]
        self.assertFalse(self.worker.handle({"op": "prepare", **capture})["ok"])

    def test_final_open_set_and_schema_origin_changes_invalidate(self) -> None:
        capture = self.capture()
        identity = self.prepare(capture)
        self.assertTrue(self.analyze(capture, identity)["ok"])
        capture["documents"] = [self.document(self.schema)]
        result = self.worker.handle(
            {"op": "current", **capture, "id": identity, "schema_buffers": []}
        )
        self.assertFalse(result["ok"])
        capture = self.capture()
        capture["documents"] = [self.document(self.schema)]
        identity = self.prepare(capture)
        buffers = [self.buffer(self.schema, "type Query { value: String }")]
        self.assertTrue(self.analyze(capture, identity, buffers)["ok"])
        buffers[0]["text"] = "type Query { replacement: String }"
        self.assertEqual(
            self.worker.handle(
                {"op": "current", **capture, "id": identity, "schema_buffers": buffers}
            ),
            {"ok": True, "current": False},
        )

    def test_remote_unavailable_and_invalid_schema_error_redaction(self) -> None:
        self.config.write_text(
            "[tool.graphql-desk]\n"
            'schema={url="https://example.test/PRIVATE_MARKER",'
            'headers={Authorization="PRIVATE_MARKER"}}\n'
            'schema_output="graphql_desk_generated/default.pyi"\n',
            encoding="utf8",
        )
        capture = self.capture()
        prepared = self.worker.handle({"op": "prepare", **capture})
        self.assertTrue(prepared["ok"])
        self.assertEqual(prepared["local_schemas"], [])
        result = self.analyze(capture, cast(str, prepared["id"]))
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["unavailable_schemas"])
        self.assertNotIn("PRIVATE_MARKER", json.dumps(result))
        self.config.write_text(
            '[tool.graphql-desk]\nschema="../outside.graphql"\n'
            'schema_output="graphql_desk_generated/default.pyi"\n',
            encoding="utf8",
        )
        self.schema.write_text("PRIVATE_MARKER {", encoding="utf8")
        result = self.analyze(capture, self.prepare(capture))
        self.assertEqual(result, {"ok": False, "error": "editor-request-failed"})

    def test_bounded_json_protocol_redacts_and_recovers(self) -> None:
        prepare = json.dumps({"op": "prepare", **self.capture()}).encode()
        inputs = [
            b'{"op":"PRIVATE_MARKER","op":"prepare"}',
            b'{"PRIVATE_MARKER":NaN}',
            b'{"PRIVATE_MARKER":1e999}',
            b"\xffPRIVATE_MARKER",
            b" " * MAX_LINE_BYTES + b"PRIVATE_MARKER",
            prepare,
        ]
        writer = io.BytesIO()
        serve(io.BytesIO(b"\n".join(inputs) + b"\n"), writer)
        lines = writer.getvalue().splitlines()
        self.assertEqual(len(lines), 6)
        for line in lines[:-1]:
            self.assertEqual(
                json.loads(line), {"ok": False, "error": "editor-request-failed"}
            )
        self.assertTrue(json.loads(lines[-1])["ok"])
        self.assertNotIn(b"PRIVATE_MARKER", writer.getvalue())

    def test_disk_analysis_response_bound_invalidates_job(self) -> None:
        self.source.write_text("# " + "x" * 4096, encoding="utf8")
        capture = self.capture()
        identity = UUID(int=1)
        messages: list[dict[str, object]] = [
            {"op": "prepare", **capture},
            {"op": "analyze", **capture, "id": identity.hex, "schema_buffers": []},
            {"op": "current", **capture, "id": identity.hex, "schema_buffers": []},
        ]
        data = b"".join((json.dumps(item) + "\n").encode() for item in messages)
        writer = io.BytesIO()
        with (
            patch("graphql_desk.editor_worker.MAX_LINE_BYTES", 2048),
            patch("graphql_desk.editor_worker.uuid4", return_value=identity),
        ):
            serve(io.BytesIO(data), writer)
        lines = writer.getvalue().splitlines()
        self.assertEqual(len(lines), 3)
        self.assertTrue(json.loads(lines[0])["ok"])
        for line in lines[1:]:
            self.assertEqual(
                json.loads(line), {"ok": False, "error": "editor-request-failed"}
            )
        self.assertTrue(all(len(line) + 1 <= 2048 for line in lines))
        self.assertNotIn(b"x" * 100, writer.getvalue())

    def test_private_capture_repr_hides_fallback_credentials(self) -> None:
        capture = self.capture()
        capture["editor_fallback"] = {
            "schema": {
                "url": "https://example.test/graphql",
                "headers": {"Authorization": "PRIVATE_MARKER"},
            },
            "schema_output": "graphql_desk_generated/default.pyi",
        }
        self.prepare(capture)
        self.assertNotIn("PRIVATE_MARKER", repr(vars(self.worker)["_capture"]))

    def test_real_isolated_utf8_persistent_subprocess(self) -> None:
        process = subprocess.Popen(
            [sys.executable, "-I", "-X", "utf8", "-m", "graphql_desk.editor_worker"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=self.base,
        )

        def cleanup() -> None:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=10)
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None:
                    stream.close()

        self.addCleanup(cleanup)
        assert process.stdin is not None and process.stdout is not None
        capture = self.capture()
        capture["documents"] = [self.document(self.schema)]

        def exchange(message: dict[str, object]) -> dict[str, object]:
            assert process.stdin is not None and process.stdout is not None
            process.stdin.write(
                (json.dumps(message, ensure_ascii=False) + "\n").encode()
            )
            process.stdin.flush()
            return cast(dict[str, object], json.loads(process.stdout.readline()))

        prepared = exchange({"op": "prepare", **capture})
        buffers = [self.buffer(self.schema, "# 😀\ntype Query { value: String }")]
        request = {**capture, "id": prepared["id"], "schema_buffers": buffers}
        analyzed = exchange({"op": "analyze", **request})
        self.assertTrue(analyzed["ok"], analyzed)
        schema = cast(
            list[dict[str, object]],
            cast(dict[str, object], analyzed["request"])["schemas"],
        )[0]
        self.assertEqual(schema["text"], buffers[0]["text"])
        self.assertEqual(
            exchange({"op": "current", **request}), {"ok": True, "current": True}
        )
        for malformed in (
            b'{"op":"PRIVATE_MARKER","op":"prepare"}\n',
            b" " * MAX_LINE_BYTES + b"PRIVATE_MARKER\n",
        ):
            process.stdin.write(malformed)
            process.stdin.flush()
            self.assertEqual(
                json.loads(process.stdout.readline()),
                {"ok": False, "error": "editor-request-failed"},
            )
        self.assertFalse(exchange({"op": "current", **request})["ok"])
        process.stdin.close()
        self.assertEqual(process.wait(timeout=10), 0)
        assert process.stderr is not None
        self.assertEqual(process.stderr.read(), b"")
        process.stdout.close()
        process.stderr.close()


if __name__ == "__main__":
    unittest.main()
