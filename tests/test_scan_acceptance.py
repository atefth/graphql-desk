"""Contract acceptance across the experimental scan's full report pipeline."""

import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from graphql_desk.cli import main
from graphql_desk.scan import scan_project
from graphql_desk.scan_reporting import json_report, terminal_report

FIXTURE = Path(__file__).parent / "fixtures" / "scan_acceptance"


class ScanAcceptanceTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        shutil.copytree(FIXTURE, self.root, dirs_exist_ok=True)
        for source in self.root.rglob("*.py.txt"):
            source.rename(source.with_suffix(""))

    def test_all_rules_and_report_formats_match_contract_snapshots(self) -> None:
        with patch.object(Path, "cwd", return_value=self.root):
            report = scan_project()
            repeated = scan_project()

        expected_rules = {
            "field-usage",
            "input-usage",
            "deprecated-usage",
            "orphan-fragments",
            "cross-feature-fragments",
            "operation-complexity",
            "fetch-depth",
            "directive-usage",
        }
        self.assertEqual(set(report.rules), expected_rules)
        self.assertEqual(report, repeated)

        expected_json = (FIXTURE / "expected-report.json").read_text(encoding="utf-8")
        self.assertEqual(json_report(report), expected_json)
        expected_graph = (FIXTURE / "expected-graph.json").read_text(encoding="utf-8")
        self.assertEqual(json_report(report, graph=True), expected_graph)
        self.assertEqual(
            terminal_report(report),
            (FIXTURE / "expected-terminal.txt").read_text(encoding="utf-8"),
        )

        payload = json.loads(expected_json)
        self.assertEqual(
            payload["overview"]["coverageBySchema"]["admin"]["percent"], 67
        )
        users_coordinates = {
            item["ref"]["schemaName"]
            for item in payload["rules"]["field-usage"]
            if item["ref"]["typeName"] == "Query"
            and item["ref"]["fieldName"] == "users"
        }
        self.assertEqual(users_coordinates, {"admin", "default"})
        simplicity = next(
            item["data"]
            for item in payload["rules"]["operation-complexity"]
            if item["ref"]["operationName"] == "Simple"
        )
        self.assertEqual(
            (
                simplicity["depth"],
                simplicity["fieldCount"],
                simplicity["listFields"],
                simplicity["score"],
            ),
            (2, 2, 1, 9),
        )
        name_usage = next(
            item["data"]
            for item in payload["rules"]["field-usage"]
            if item["ref"]["schemaName"] == "default"
            and item["ref"]["typeName"] == "User"
            and item["ref"]["fieldName"] == "name"
        )
        self.assertEqual(name_usage["count"], 4)
        direct_definition_ids = [item["defId"] for item in name_usage["directUsages"]]
        self.assertEqual(len(direct_definition_ids), 4)
        self.assertEqual(
            len(direct_definition_ids), len(set(direct_definition_ids))
        )  # repeated alias in Users counts once for that definition
        self.assertEqual(name_usage["count"], len(set(direct_definition_ids)))
        identity_usage = next(
            item["data"]
            for item in payload["rules"]["field-usage"]
            if item["ref"]["schemaName"] == "default"
            and item["ref"]["typeName"] == "User"
            and item["ref"]["fieldName"] == "id"
        )
        self.assertEqual(identity_usage["count"], 2)
        self.assertEqual(identity_usage["operations"], ["project:default:query:Users"])
        self.assertEqual(
            next(
                item["data"]["depth"]
                for item in payload["rules"]["fetch-depth"]
                if item["ref"]["operationName"] == "Simple"
            ),
            2,
        )
        self.assertEqual(
            {item["ref"]["name"] for item in payload["rules"]["orphan-fragments"]},
            {"OrphanParent"},
        )
        self.assertIn(
            "OrphanChild",
            {
                item["name"]
                for item in payload["fragments"]
                if item["name"]
                not in {
                    orphan["ref"]["name"]
                    for orphan in payload["rules"]["orphan-fragments"]
                }
            },
        )
        self.assertTrue(
            any(
                item["data"]["count"] == 0
                and item["ref"]["fieldName"] == "deprecatedOnly"
                for item in payload["rules"]["deprecated-usage"]
            )
        )
        self.assertEqual(
            {
                item["ref"]["directiveName"]
                for item in payload["rules"]["directive-usage"]
            },
            {"audit", "include", "skip"},
        )

    def test_pinned_upstream_examples_and_cli_warning_policy(self) -> None:
        with (
            patch.object(Path, "cwd", return_value=self.root),
            patch.dict(os.environ, {"GITHUB_ACTIONS": "false"}),
        ):
            stdout = io.StringIO()
            stderr = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = main(["scan", "--format", "json"])
            self.assertEqual(code, 0)
            self.assertIn("analysis warning", stderr.getvalue())

            expected_json = (FIXTURE / "expected-report.json").read_text(
                encoding="utf-8"
            )
            self.assertEqual(stdout.getvalue(), expected_json)

            ci_stdout = io.StringIO()
            ci_stderr = io.StringIO()
            with (
                patch.dict(os.environ, {"GITHUB_ACTIONS": "true"}),
                redirect_stdout(ci_stdout),
                redirect_stderr(ci_stderr),
            ):
                ci_code = main(["scan", "--format", "json"])
            self.assertEqual(ci_code, 0)
            self.assertEqual(ci_stdout.getvalue(), expected_json)
            self.assertEqual(ci_stderr.getvalue(), "")

            graph_stdout = io.StringIO()
            graph_stderr = io.StringIO()
            with redirect_stdout(graph_stdout), redirect_stderr(graph_stderr):
                graph_code = main(["scan", "--graph"])
            self.assertEqual(graph_code, 0)
            self.assertIn("analysis warning", graph_stderr.getvalue())
            self.assertEqual(
                graph_stdout.getvalue(),
                (FIXTURE / "expected-graph.json").read_text(encoding="utf-8"),
            )

            warning_stdout = io.StringIO()
            warning_stderr = io.StringIO()
            with redirect_stdout(warning_stdout), redirect_stderr(warning_stderr):
                warning_code = main(["scan", "--format", "json", "--fail-on-warn"])
            self.assertEqual(warning_code, 1)
            warning_payload = json.loads(warning_stdout.getvalue())
            self.assertEqual(warning_payload["warnings"][0]["file"], "broken.py")
            self.assertTrue(warning_payload["warnings"])

            missing_schema_stdout = io.StringIO()
            missing_schema_stderr = io.StringIO()
            with (
                redirect_stdout(missing_schema_stdout),
                redirect_stderr(missing_schema_stderr),
            ):
                missing_schema_code = main(
                    ["scan", "--schema", "missing", "--format", "json"]
                )
            self.assertEqual(missing_schema_code, 1)
            self.assertEqual(missing_schema_stdout.getvalue(), "")
            self.assertIn("command failed", missing_schema_stderr.getvalue())

        # These facts mirror the pinned gql.tada scan tests. The complete
        # snapshots above are Python contract output, not upstream goldens.
        report = json.loads(expected_json)
        rules = report["rules"]
        name_usage = next(
            item["data"]
            for item in rules["field-usage"]
            if item["ref"]["schemaName"] == "default"
            and item["ref"]["typeName"] == "User"
            and item["ref"]["fieldName"] == "name"
        )
        self.assertEqual(name_usage["count"], 4)
        self.assertIn("project:default:query:Users", name_usage["operations"])
        self.assertIn(
            "Filter.sort",
            {
                f"{item['ref']['typeName']}.{item['ref']['fieldName']}"
                for item in rules["input-usage"]
            },
        )
        self.assertIn(
            "Sort.NEWEST",
            {
                f"{item['ref']['typeName']}.{item['ref']['fieldName']}"
                for item in rules["input-usage"]
            },
        )
        self.assertEqual(
            {item["ref"]["fieldName"] for item in rules["deprecated-usage"]},
            {"legacy", "deprecatedOnly"},
        )
        user_fields = next(
            item["data"]
            for item in rules["cross-feature-fragments"]
            if item["ref"]["name"] == "UserFields"
        )
        self.assertEqual(user_fields["definingArea"], "features/shared")
        self.assertEqual(
            set(user_fields["consumerAreas"]), {"features/profile", "features/users"}
        )
        self.assertEqual(user_fields["spreadCount"], 2)
        user_fields_point = next(
            item
            for item in rules["cross-feature-fragments"]
            if item["ref"]["name"] == "UserFields"
        )
        self.assertEqual(user_fields_point["weight"], 2)
        graph = json.loads(
            (FIXTURE / "expected-graph.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            {edge["kind"] for edge in graph["edges"]},
            {"imports", "defines", "spreads", "selects", "onType"},
        )


if __name__ == "__main__":
    unittest.main()
