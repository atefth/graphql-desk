"""Read-only setup reports and captured facts, without editor authority claims."""

import importlib
import json
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from graphql_desk import doctor
from graphql_desk.editor_analysis import analyze_editor_snapshot_details
from graphql_desk.editor_snapshot import (
    ConfigurationDiagnostic,
    OpenBuffer,
    acquire_editor_snapshot,
)


class DoctorTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="issue65-unit-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.config = self.root / "pyproject.toml"
        self.schema = self.root / "schema.graphql"
        self.schema.write_text("type Query { title: String }", encoding="utf-8")
        self.config.write_text(
            '[tool.graphql-desk]\nschema="schema.graphql"\n'
            'schema_output="graphql_desk_generated/default.pyi"\n',
            encoding="utf-8",
        )

    def saved(self) -> doctor.DoctorReport:
        return doctor.collect_project_health(self.root, environment={})

    def test_import_is_inert_and_saved_healthy_setup_still_has_no_live_authority(
        self,
    ) -> None:
        with patch("importlib.metadata.version", side_effect=AssertionError("no I/O")):
            importlib.reload(doctor)
        before = {path.name: path.read_bytes() for path in self.root.iterdir()}
        report = self.saved()
        self.assertEqual(report["exit_code"], 1)
        self.assertEqual(report["runtime"]["executable"], sys.executable)
        self.assertEqual(
            report["runtime"]["python_version"],
            ".".join(map(str, sys.version_info[:3])),
        )
        for name in ("configuration", "project-ownership", "schema.default"):
            self.assertEqual(
                next(
                    item["status"] for item in report["checks"] if item["name"] == name
                ),
                "ready",
            )
        live = {"generated-ownership", "active-editor", "active-checker", "live-bridge"}
        self.assertTrue(
            all(
                item["status"] == "unavailable"
                for item in report["checks"]
                if item["name"] in live
            )
        )
        self.assertEqual(
            before, {path.name: path.read_bytes() for path in self.root.iterdir()}
        )

    def test_forged_status_and_generation_claims_do_not_change_saved_health(
        self,
    ) -> None:
        generated = self.root / ".graphql-desk"
        generated.mkdir()
        for name in ("health.json", "complete.json", "project-config-owner.json"):
            (generated / name).write_text(
                '{"current":true,"version":"PRIVATE_CLAIM","owned":true}',
                encoding="utf-8",
            )
        report = self.saved()
        self.assertEqual(report["exit_code"], 1)
        self.assertNotIn("PRIVATE_CLAIM", json.dumps(report))
        self.assertTrue(
            all(
                item["status"] == "unavailable"
                for item in report["checks"]
                if item["name"] in {"generated-ownership", "live-bridge"}
            )
        )

    def test_missing_malformed_and_selected_schema_errors_are_redacted(self) -> None:
        for text in ("[tool.graphql-desk]\nschema=PRIVATE_CONFIG", "[tool.other]\nx=1"):
            self.config.write_text(text, encoding="utf-8")
            report = self.saved()
            self.assertEqual(report["checks"][0]["status"], "unavailable")
            self.assertNotIn("PRIVATE_CONFIG", json.dumps(report))
        self.config.write_text(
            '[tool.graphql-desk]\nschema="schema.graphql"\n'
            'schema_output="graphql_desk_generated/default.pyi"\n',
            encoding="utf-8",
        )
        report = doctor.collect_project_health(
            self.root, schema_name="PRIVATE_SCHEMA", environment={}
        )
        self.assertEqual(report["exit_code"], 1)
        self.assertNotIn("PRIVATE_SCHEMA", json.dumps(report))

    def test_complete_child_cannot_hide_parent_output_collision(self) -> None:
        self.config.write_text(
            '[tool.graphql-desk]\nschema="schema.graphql"\n'
            'generated_package="child"\nschema_output="child/default.pyi"\n',
            encoding="utf-8",
        )
        child = self.root / "child"
        child.mkdir()
        config = child / "pyproject.toml"
        config.write_text(
            '[tool.graphql-desk]\nschema="schema.graphql"\n'
            'schema_output="graphql_desk_generated/default.pyi"\n',
            encoding="utf-8",
        )
        (child / "schema.graphql").write_bytes(self.schema.read_bytes())
        report = doctor.collect_project_health(child, environment={})
        self.assertEqual(report["checks"][0]["status"], "unavailable")

    def test_local_stub_symbols_are_inspected_without_importing_application(
        self,
    ) -> None:
        self.schema.write_text(
            "scalar DateTime type Query { now: DateTime }", encoding="utf-8"
        )
        self.config.write_text(
            self.config.read_text()
            + '[tool.graphql-desk.scalars]\nDateTime="app_types.Clock"\n',
            encoding="utf-8",
        )
        (self.root / "app_types.py").write_text(
            "raise RuntimeError('PROJECT_MUST_NOT_EXECUTE')", encoding="utf-8"
        )
        (self.root / "app_types.pyi").write_text("class Clock: ...\n", encoding="utf-8")
        report = self.saved()
        mapping = next(
            item
            for item in report["checks"]
            if item["name"] == "schema.default.symbols"
        )
        self.assertEqual(mapping["status"], "ready")
        self.assertIn("resolved statically: 1", mapping["message"])
        self.assertNotIn("app_types", sys.modules)

    def test_explicit_any_is_named_without_coercion_or_import(self) -> None:
        self.schema.write_text(
            "scalar DateTime enum State { READY } "
            "type Query { now: DateTime state: State }",
            encoding="utf-8",
        )
        self.config.write_text(
            self.config.read_text()
            + '[tool.graphql-desk.scalars]\nDateTime="typing.Any"\n'
            + '[tool.graphql-desk.enums]\nState="typing.Any"\n',
            encoding="utf-8",
        )
        messages = [
            item["message"]
            for item in self.saved()["checks"]
            if item["name"] == "schema.default.explicit-any"
        ]
        self.assertEqual(len(messages), 3)
        self.assertTrue(any("input" in item for item in messages))
        self.assertTrue(any("output" in item for item in messages))
        self.assertTrue(any("enum" in item for item in messages))

    def test_named_schema_selection_and_missing_symbol_are_not_silent_success(
        self,
    ) -> None:
        self.config.write_text(
            '[[tool.graphql-desk.schemas]]\nname="first"\n'
            'schema="schema.graphql"\nschema_output="graphql_desk_generated/first.pyi"\n'
            '[[tool.graphql-desk.schemas]]\nname="second"\n'
            'schema="other.graphql"\nschema_output="graphql_desk_generated/second.pyi"\n',
            encoding="utf-8",
        )
        (self.root / "other.graphql").write_text(
            "PRIVATE_INVALID_SCHEMA", encoding="utf-8"
        )
        report = doctor.collect_project_health(
            self.root, schema_name="first", environment={}
        )
        self.assertTrue(
            any(item["name"] == "schema.first" for item in report["checks"])
        )
        self.assertFalse(
            any(item["name"].startswith("schema.second") for item in report["checks"])
        )
        report = self.saved()
        self.assertEqual(
            next(
                item["status"]
                for item in report["checks"]
                if item["name"] == "schema.second"
            ),
            "unavailable",
        )
        self.assertNotIn("PRIVATE_INVALID_SCHEMA", json.dumps(report))
        self.config.write_text(
            '[tool.graphql-desk]\nschema="schema.graphql"\n'
            'schema_output="graphql_desk_generated/default.pyi"\n'
            '[tool.graphql-desk.scalars]\nDateTime="missing_types.Clock"\n',
            encoding="utf-8",
        )
        self.schema.write_text(
            "scalar DateTime type Query { now: DateTime }", encoding="utf-8"
        )
        report = self.saved()
        symbols = next(
            item
            for item in report["checks"]
            if item["name"] == "schema.default.symbols"
        )
        self.assertNotEqual(symbols["status"], "ready")

    def test_unavailable_mapping_and_schema_are_actionable_without_private_text(
        self,
    ) -> None:
        self.schema.write_text(
            "scalar DateTime type Query { now: DateTime }", encoding="utf-8"
        )
        report = self.saved()
        self.assertEqual(
            next(
                item
                for item in report["checks"]
                if item["name"].endswith(".declarations")
            )["status"],
            "unavailable",
        )
        self.schema.write_text("PRIVATE_SCHEMA_CONTENT", encoding="utf-8")
        report = self.saved()
        self.assertEqual(
            next(item for item in report["checks"] if item["name"] == "schema.default")[
                "status"
            ],
            "unavailable",
        )
        self.assertNotIn("PRIVATE_SCHEMA_CONTENT", json.dumps(report))

    def test_remote_access_is_explicit_and_errors_do_not_echo_headers_or_url(
        self,
    ) -> None:
        self.config.write_text(
            '[tool.graphql-desk]\nschema="https://example.invalid/PRIVATE_URL"\n'
            'schema_output="graphql_desk_generated/default.pyi"\n',
            encoding="utf-8",
        )
        with patch(
            "graphql_desk.doctor.load_from_url",
            side_effect=ValueError("PRIVATE_URL PRIVATE_HEADER"),
        ) as loader:
            report = doctor.collect_project_health(
                self.root, environment={"AUTHORIZATION": "PRIVATE_HEADER"}
            )
        loader.assert_called_once()
        self.assertEqual(report["exit_code"], 1)
        self.assertNotIn("PRIVATE_", json.dumps(report))

    def test_captured_editor_setup_does_not_reload_disk_or_fetch(self) -> None:
        source = self.root / "q.py"
        source.write_text(
            'from graphql_desk import graphql\nq=graphql("{ title }")\n',
            encoding="utf-8",
        )
        schema_text = "type Query { title: Int }"
        snapshot = acquire_editor_snapshot(
            workspace=self.root,
            candidates=(source,),
            buffers=(OpenBuffer(self.schema, 4, schema_text),),
            generation=7,
        )
        analysis = analyze_editor_snapshot_details(
            snapshot.request,
            external_mapping_modules=snapshot.external_mapping_modules,
        )
        self.schema.write_text("PRIVATE_LATER_BROKEN_SCHEMA", encoding="utf-8")
        with (
            patch(
                "graphql_desk.doctor.load_local_schema",
                side_effect=AssertionError("no disk reload"),
            ),
            patch(
                "graphql_desk.doctor.load_from_url",
                side_effect=AssertionError("no fetch"),
            ),
            patch(
                "graphql_desk.doctor._resolve_local_mapping_symbols",
                side_effect=AssertionError("use capture"),
            ),
        ):
            report = doctor.collect_snapshot_health(
                snapshot, analysis, configuration_diagnostics=()
            )
        self.assertEqual(
            next(
                item["status"]
                for item in report["checks"]
                if item["name"] == "schema.default"
            ),
            "ready",
        )
        self.assertEqual(
            report["exit_code"], 1, "Captured facts are not a live publish permit"
        )
        self.assertNotIn("PRIVATE_LATER", json.dumps(report))
        other_request = snapshot.request
        other_request["request_id"] = "8"
        mismatched = replace(
            snapshot, generation=8, _request_json=json.dumps(other_request)
        )
        invalid = doctor.collect_snapshot_health(
            mismatched, analysis, configuration_diagnostics=()
        )
        self.assertEqual(invalid["checks"][0]["status"], "unavailable")

    def test_captured_external_stubs_and_configuration_failures_remain_visible(
        self,
    ) -> None:
        project = self.root / "project"
        vendor = self.root / "vendor"
        project.mkdir()
        vendor.mkdir()
        (vendor / "external_clock.pyi").write_text(
            "class Clock: ...\n", encoding="utf-8"
        )
        (project / "pyproject.toml").write_text(
            self.config.read_text()
            + '[tool.graphql-desk.scalars]\nDateTime="external_clock.Clock"\n',
            encoding="utf-8",
        )
        (project / "schema.graphql").write_text(
            "scalar DateTime type Query { now: DateTime }", encoding="utf-8"
        )
        source = project / "q.py"
        source.write_text(
            'from graphql_desk import graphql\nq=graphql("{ now }")\n', encoding="utf-8"
        )
        with patch.object(sys, "path", [str(vendor), *sys.path]):
            snapshot = acquire_editor_snapshot(
                workspace=project, candidates=(source,), generation=3
            )
        self.assertTrue(snapshot.external_mapping_modules)
        analysis = analyze_editor_snapshot_details(
            snapshot.request, external_mapping_modules=snapshot.external_mapping_modules
        )
        report = doctor.collect_snapshot_health(
            snapshot, analysis, configuration_diagnostics=()
        )
        self.assertEqual(
            next(
                item["status"]
                for item in report["checks"]
                if item["name"] == "schema.default.symbols"
            ),
            "ready",
        )
        diagnostic = ConfigurationDiagnostic(
            (project / "pyproject.toml").as_uri(),
            None,
            "PRIVATE_CONFIG_ERROR",
            (0, 0),
            (0, 0),
        )
        report = doctor.collect_snapshot_health(
            snapshot, analysis, configuration_diagnostics=(diagnostic,)
        )
        self.assertTrue(
            any(
                item["name"] == "configuration" and item["status"] == "unavailable"
                for item in report["checks"]
            )
        )
        self.assertNotIn("PRIVATE_CONFIG_ERROR", json.dumps(report))
        unavailable = replace(
            snapshot,
            unavailable_schemas=(
                (snapshot.project_configs[0].path.as_uri(), "default"),
            ),
        )
        report = doctor.collect_snapshot_health(
            unavailable, analysis, configuration_diagnostics=()
        )
        self.assertEqual(
            next(
                item["status"]
                for item in report["checks"]
                if item["name"] == "schema.default"
            ),
            "unavailable",
        )
        self.assertNotIn("external_clock", sys.modules)

    def test_empty_editor_configuration_stays_unavailable(self) -> None:
        source = self.root / "q.py"
        source.write_text("x = 1\n", encoding="utf-8")
        snapshot = acquire_editor_snapshot(
            workspace=self.root, candidates=(source,), generation=2
        )
        analysis = analyze_editor_snapshot_details(snapshot.request)
        report = doctor.collect_snapshot_health(
            replace(snapshot, project_configs=()),
            analysis,
            configuration_diagnostics=(),
        )
        self.assertEqual(report["checks"][0]["status"], "unavailable")


if __name__ == "__main__":
    unittest.main()
