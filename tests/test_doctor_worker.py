"""Doctor reports use the same current selected-worker capture, never a health file."""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import cast
from unittest.mock import patch

from graphql import build_schema

from graphql_desk.doctor import DoctorReport, collect_snapshot_health
from graphql_desk.editor_analysis import EditorAnalysis
from graphql_desk.editor_snapshot import ConfigurationDiagnostic, EditorSnapshot
from graphql_desk.editor_worker import EditorWorker


class DoctorWorkerTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="issue65-worker-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.source = self.root / "main.py"
        self.source.write_text(
            'from graphql_desk import gql\nq=gql("{title}")\n',
            encoding="utf8",
        )
        self.schema = self.root / "schema.graphql"
        self.schema.write_text("type Query { title: String }", encoding="utf8")
        self.config = self.root / "pyproject.toml"
        self.config.write_text(
            '[tool.graphql-desk]\nschema="schema.graphql"\n'
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

    def job(self, capture: dict[str, object]) -> dict[str, object]:
        prepared = self.worker.handle({"op": "prepare", **capture})
        self.assertTrue(prepared["ok"], prepared)
        request: dict[str, object] = {
            **capture,
            "id": prepared["id"],
            "schema_buffers": [],
        }
        analyzed = self.worker.handle({"op": "analyze", **request})
        self.assertTrue(analyzed["ok"], analyzed)
        return request

    def test_current_report_uses_selected_interpreter_and_preserves_no_live_authority(
        self,
    ) -> None:
        request = self.job(self.capture())
        before = {
            path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()
        }
        with (
            patch(
                "graphql_desk.doctor.load_local_schema",
                side_effect=AssertionError("No reload"),
            ),
            patch(
                "graphql_desk.doctor.load_from_url",
                side_effect=AssertionError("No fetch"),
            ),
            patch(
                "graphql_desk.doctor._resolve_local_mapping_symbols",
                side_effect=AssertionError("Use captured modules"),
            ),
        ):
            reply = self.worker.handle({"op": "doctor", **request})
        self.assertTrue(reply["ok"], reply)
        self.assertEqual(set(reply), {"ok", "id", "current", "report"})
        self.assertTrue(reply["current"])
        report = cast(dict[str, object], reply["report"])
        runtime = cast(dict[str, object], report["runtime"])
        self.assertEqual(runtime["executable"], sys.executable)
        self.assertEqual(
            runtime["python_version"], ".".join(map(str, sys.version_info[:3]))
        )
        self.assertEqual(report["exit_code"], 1)
        checks = cast(list[dict[str, object]], report["checks"])
        self.assertTrue(
            any(
                item["name"] == "schema.default" and item["status"] == "ready"
                for item in checks
            )
        )
        self.assertTrue(
            all(
                item["status"] == "unavailable"
                for item in checks
                if item["name"]
                in {
                    "generated-ownership",
                    "active-editor",
                    "active-checker",
                    "live-bridge",
                }
            )
        )
        self.assertEqual(
            before,
            {
                path: path.read_bytes()
                for path in self.root.rglob("*")
                if path.is_file()
            },
        )

    def test_changed_source_schema_and_config_return_no_report_and_invalidate(
        self,
    ) -> None:
        for path in (self.source, self.schema, self.config):
            with self.subTest(path=path.name):
                request = self.job(self.capture())
                path.write_text(
                    path.read_text(encoding="utf8") + "\n# PRIVATE_CHANGE\n",
                    encoding="utf8",
                )
                reply = self.worker.handle({"op": "doctor", **request})
                self.assertEqual(
                    reply, {"ok": True, "id": request["id"], "current": False}
                )
                self.assertFalse(self.worker.handle({"op": "current", **request})["ok"])

    def test_edit_during_collection_is_rechecked_before_returning_report(self) -> None:
        request = self.job(self.capture())

        def collect(
            snapshot: EditorSnapshot,
            analysis: EditorAnalysis,
            *,
            configuration_diagnostics: tuple[ConfigurationDiagnostic, ...],
        ) -> DoctorReport:
            report = collect_snapshot_health(
                snapshot,
                analysis,
                configuration_diagnostics=configuration_diagnostics,
            )
            self.source.write_text("# changed while collecting\n", encoding="utf8")
            return report

        with patch(
            "graphql_desk.editor_worker.collect_snapshot_health", side_effect=collect
        ):
            reply = self.worker.handle({"op": "doctor", **request})
        self.assertEqual(reply, {"ok": True, "id": request["id"], "current": False})

    def test_malformed_peer_configuration_cannot_disappear_behind_valid_project(
        self,
    ) -> None:
        healthy = self.root / "healthy"
        healthy.mkdir()
        for path in (self.source, self.schema, self.config):
            (healthy / path.name).write_bytes(path.read_bytes())
        self.config.unlink()
        peer = self.root / "peer"
        peer.mkdir()
        (peer / "pyproject.toml").write_text(
            "[tool.graphql-desk]\nschema=PRIVATE_PEER\n", encoding="utf8"
        )
        source = peer / "main.py"
        source.write_text("pass\n", encoding="utf8")
        capture = self.capture()
        capture["candidates"] = [str(healthy / "main.py"), str(source)]
        request = self.job(capture)
        reply = self.worker.handle({"op": "doctor", **request})
        self.assertTrue(reply["ok"], reply)
        report = cast(dict[str, object], reply["report"])
        checks = cast(list[dict[str, object]], report["checks"])
        self.assertTrue(
            any(
                item["name"] == "configuration" and item["status"] == "unavailable"
                for item in checks
            )
        )
        self.assertNotIn("PRIVATE_PEER", json.dumps(reply))

    def test_remote_schema_is_never_fetched_and_private_inputs_are_not_echoed(
        self,
    ) -> None:
        self.config.write_text(
            '[tool.graphql-desk]\nschema={url="https://example.invalid/PRIVATE_URL",'
            'headers={"Authorization"="PRIVATE_HEADER"}}\n'
            'schema_output="graphql_desk_generated/default.pyi"\n',
            encoding="utf8",
        )
        with (
            patch(
                "graphql_desk.editor_worker.load_from_url",
                side_effect=AssertionError("No fetch"),
            ),
            patch(
                "graphql_desk.doctor.load_from_url",
                side_effect=AssertionError("No fetch"),
            ),
        ):
            request = self.job(self.capture())
            reply = self.worker.handle({"op": "doctor", **request})
        self.assertTrue(reply["ok"], reply)
        report = cast(dict[str, object], reply["report"])
        self.assertTrue(
            any(
                item["name"] == "schema.default" and item["status"] == "unavailable"
                for item in cast(list[dict[str, object]], report["checks"])
            )
        )
        self.assertNotIn("PRIVATE_", json.dumps(reply))
        invalid = self.worker.handle(
            {"op": "doctor", **request, "environment": {"TOKEN": "PRIVATE_TOKEN"}}
        )
        self.assertEqual(invalid, {"ok": False, "error": "editor-request-failed"})

    def test_report_requires_analysis_and_same_credential_revision(self) -> None:
        capture = self.capture()
        prepared = self.worker.handle({"op": "prepare", **capture})
        self.assertFalse(
            self.worker.handle(
                {"op": "doctor", **capture, "id": prepared["id"], "schema_buffers": []}
            )["ok"]
        )
        self.config.write_text(
            '[tool.graphql-desk]\nschema="https://example.invalid/graphql"\n'
            'schema_output="graphql_desk_generated/default.pyi"\n',
            encoding="utf8",
        )
        prepared = self.worker.handle({"op": "prepare", **capture})
        with patch(
            "graphql_desk.editor_worker.load_from_url",
            return_value=build_schema("type Query { title: String }"),
        ) as fetch:
            loaded = self.worker.handle(
                {
                    "op": "refresh",
                    **capture,
                    "id": prepared["id"],
                    "schema_buffers": [],
                    "project": self.config.as_uri(),
                    "schema_name": "default",
                    "credential_revision": "one",
                    "environment": {},
                }
            )
            self.assertTrue(loaded["ok"], loaded)
            request = {
                **capture,
                "id": loaded["id"],
                "schema_buffers": [],
                "remote_revisions": [
                    {
                        "project": self.config.as_uri(),
                        "schema_name": "default",
                        "revision": "one",
                    }
                ],
            }
            self.assertTrue(self.worker.handle({"op": "analyze", **request})["ok"])
            self.assertTrue(self.worker.handle({"op": "doctor", **request})["ok"])
            changed = {
                **request,
                "remote_revisions": [
                    {
                        "project": self.config.as_uri(),
                        "schema_name": "default",
                        "revision": "two",
                    }
                ],
            }
            self.assertFalse(self.worker.handle({"op": "doctor", **changed})["ok"])
            self.assertEqual(fetch.call_count, 1)
