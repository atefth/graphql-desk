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
from tests import test_factory_generation as factory_tests
from tests.test_schema_url import endpoint


class EditorWorkerTests(unittest.TestCase):
    def test_analysis_fingerprint_tracks_source_config_and_schema_content(self) -> None:
        def fingerprint() -> str:
            capture = self.capture()
            identity = self.prepare(capture)
            self.assertTrue(self.analyze(capture, identity)["ok"])
            result = self.worker.handle(
                {"op": "artifacts", **capture, "id": identity, "schema_buffers": []}
            )
            self.assertTrue(result["ok"], result)
            value = cast(str, result["input_fingerprint"])
            self.assertRegex(value, r"^[a-f0-9]{64}$")
            return value

        self.source.write_text(
            'from graphql_desk import gql\nq=gql("{ disk }")\n', encoding="utf8"
        )
        baseline = fingerprint()
        self.assertEqual(fingerprint(), baseline)

        self.source.write_text(
            self.source.read_text() + "# source change\n", encoding="utf8"
        )
        source_change = fingerprint()
        self.assertNotEqual(source_change, baseline)

        self.config.write_text(
            self.config.read_text() + 'client_directives=["local"]\n', encoding="utf8"
        )
        config_change = fingerprint()
        self.assertNotEqual(config_change, source_change)

        self.schema.write_text("type Query { disk: Int }", encoding="utf8")
        self.assertNotEqual(fingerprint(), config_change)

    def test_analysis_fingerprint_does_not_expose_remote_credentials(self) -> None:
        self.config.write_text(
            '[tool.graphql-desk]\nschema={url="https://example.test/graphql?token=PRIVATE",'
            'headers={Authorization="PRIVATE"}}\n'
            'schema_output="graphql_desk_generated/default.pyi"\n',
            encoding="utf8",
        )
        capture = self.capture()
        identity = self.prepare(capture)
        self.assertTrue(self.analyze(capture, identity)["ok"])
        result = self.worker.handle(
            {"op": "artifacts", **capture, "id": identity, "schema_buffers": []}
        )
        self.assertTrue(result["ok"], result)
        self.assertRegex(cast(str, result["input_fingerprint"]), r"^[a-f0-9]{64}$")
        self.assertNotIn("PRIVATE", json.dumps(result))

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

    def test_prepare_retains_remote_only_with_matching_config_and_revision(
        self,
    ) -> None:
        with endpoint() as (url, server):
            capture, identity, project = self.remote_capture(url)
            loaded = self.refresh(capture, identity, project)
            revisions = [
                {
                    "project": project,
                    "schema_name": "default",
                    "revision": "credentials-1",
                }
            ]
            self.source.write_text(
                'from graphql_desk import gql\nq=gql("{title}") # edit\n',
                encoding="utf8",
            )
            prepared = self.worker.handle(
                {"op": "prepare", **capture, "remote_revisions": revisions}
            )
            self.assertTrue(prepared["ok"], prepared)
            self.assertEqual(prepared["remote_revisions"], revisions)
            self.assertEqual(
                prepared["remote_schemas"],
                [
                    {
                        "project": project,
                        "schema_name": "default",
                        "environment_names": ["TOKEN"],
                        "refresh_interval": 0,
                    }
                ],
            )
            self.assertEqual(
                prepared["project_preferences"],
                [{"project": project, "client_directives": []}],
            )
            analyzed = self.worker.handle(
                {
                    "op": "analyze",
                    **capture,
                    "id": prepared["id"],
                    "schema_buffers": [],
                    "remote_revisions": revisions,
                }
            )
            self.assertTrue(analyzed["ok"], analyzed)
            self.assertEqual(
                len(cast(dict[str, list[object]], analyzed["request"])["schemas"]), 1
            )
            self.assertEqual(len(server.records), 2)
            nested = self.root / "nested"
            nested.mkdir()
            dependency = nested / "extra.py"
            dependency.write_text("value=1\n", encoding="utf8")
            expanded = {**capture, "candidates": [str(self.source), str(dependency)]}
            prepared = self.worker.handle(
                {"op": "prepare", **expanded, "remote_revisions": revisions}
            )
            self.assertEqual(prepared["remote_revisions"], revisions)
            self.assertEqual(len(server.records), 2)
            nested_config = nested / "pyproject.toml"
            nested_config.write_text(self.config.read_text(), encoding="utf8")
            prepared = self.worker.handle(
                {"op": "prepare", **expanded, "remote_revisions": revisions}
            )
            self.assertEqual(prepared["remote_revisions"], revisions)
            expanded_result = self.worker.handle(
                {
                    "op": "analyze",
                    **expanded,
                    "id": prepared["id"],
                    "schema_buffers": [],
                    "remote_revisions": revisions,
                }
            )
            self.assertEqual(
                len(
                    cast(dict[str, list[object]], expanded_result["request"])["schemas"]
                ),
                1,
            )
            self.assertIn(
                (nested_config.as_uri(), "default"),
                cast(
                    tuple[tuple[str, str], ...], expanded_result["unavailable_schemas"]
                ),
            )
            self.assertEqual(len(server.records), 2)
            changed = [{**revisions[0], "revision": "credentials-2"}]
            prepared = self.worker.handle(
                {"op": "prepare", **capture, "remote_revisions": changed}
            )
            self.assertEqual(prepared["remote_revisions"], [])
            analyzed = self.worker.handle(
                {
                    "op": "analyze",
                    **capture,
                    "id": prepared["id"],
                    "schema_buffers": [],
                    "remote_revisions": [],
                }
            )
            self.assertEqual(
                cast(dict[str, list[object]], analyzed["request"])["schemas"], []
            )
            self.assertEqual(len(server.records), 2)
            # A fresh explicit refresh, then a real configuration change, also drops it.
            loaded = self.refresh(capture, cast(str, prepared["id"]), project)
            self.assertTrue(loaded["ok"], loaded)
            self.config.write_text(
                self.config.read_text().replace(
                    "schema_output=",
                    'client_directives=["local","local"]\nschema_output=',
                ),
                encoding="utf8",
            )
            prepared = self.worker.handle(
                {"op": "prepare", **capture, "remote_revisions": revisions}
            )
            self.assertEqual(prepared["remote_revisions"], [])
            self.assertEqual(
                prepared["project_preferences"],
                [{"project": project, "client_directives": ["local"]}],
            )
            self.assertNotIn("PRIVATE_MARKER", json.dumps(prepared))

    def test_prepare_exposes_validated_polling_interval_without_network_or_secrets(
        self,
    ) -> None:
        with endpoint() as (url, server):
            capture, _, project = self.remote_capture(url)
            original = self.config.read_text(encoding="utf8")
            for interval in (0.125, 1e308, 0):
                self.config.write_text(
                    original + f"\nschema_refresh_interval={interval}\n",
                    encoding="utf8",
                )
                prepared = self.worker.handle(
                    {"op": "prepare", **capture, "remote_revisions": []}
                )
                self.assertTrue(prepared["ok"], prepared)
                self.assertEqual(
                    prepared["remote_schemas"],
                    [
                        {
                            "project": project,
                            "schema_name": "default",
                            "environment_names": ["TOKEN"],
                            "refresh_interval": interval,
                        }
                    ],
                )
                self.assertNotIn("PRIVATE_MARKER", json.dumps(prepared))
            self.assertEqual(server.records, [])

    def test_missed_disk_source_change_rebinds_remote_cache_without_http(self) -> None:
        with endpoint() as (url, server):
            capture, identity, project = self.remote_capture(url)
            loaded = self.refresh(capture, identity, project)
            revisions = [
                {
                    "project": project,
                    "schema_name": "default",
                    "revision": "credentials-1",
                }
            ]
            request = {
                **capture,
                "id": loaded["id"],
                "schema_buffers": [],
                "remote_revisions": revisions,
            }
            self.assertTrue(self.worker.handle({"op": "analyze", **request})["ok"])
            self.source.write_text(
                self.source.read_text() + "# missed watcher edit\n", encoding="utf8"
            )
            self.assertEqual(
                self.worker.handle({"op": "current", **request}),
                {"ok": True, "current": False},
            )
            prepared = self.worker.handle(
                {"op": "prepare", **capture, "remote_revisions": revisions}
            )
            self.assertNotEqual(prepared["id"], loaded["id"])
            self.assertEqual(prepared["remote_revisions"], revisions)
            rebound = self.worker.handle(
                {"op": "analyze", **request, "id": prepared["id"]}
            )
            self.assertEqual(
                len(cast(dict[str, list[object]], rebound["request"])["schemas"]), 1
            )
            self.assertEqual(len(server.records), 2)
            # An old-ID protocol violation still invokes full invalidation.
            self.assertFalse(self.worker.handle({"op": "current", **request})["ok"])
            prepared = self.worker.handle(
                {"op": "prepare", **capture, "remote_revisions": revisions}
            )
            self.assertEqual(prepared["remote_revisions"], [])

    def test_prepare_retained_stale_remote_never_enters_analysis(self) -> None:
        with endpoint() as (url, server):
            capture, identity, project = self.remote_capture(url)
            loaded = self.refresh(capture, identity, project)
            server.mode = "auth"
            stale = self.refresh(capture, cast(str, loaded["id"]), project)
            self.assertEqual(stale["status"], "stale")
            revisions = [
                {
                    "project": project,
                    "schema_name": "default",
                    "revision": "credentials-1",
                }
            ]
            prepared = self.worker.handle(
                {"op": "prepare", **capture, "remote_revisions": revisions}
            )
            self.assertEqual(prepared["remote_revisions"], revisions)
            analyzed = self.worker.handle(
                {
                    "op": "analyze",
                    **capture,
                    "id": prepared["id"],
                    "schema_buffers": [],
                    "remote_revisions": revisions,
                }
            )
            self.assertEqual(
                cast(dict[str, list[object]], analyzed["request"])["schemas"], []
            )
            self.assertEqual(
                cast(list[dict[str, object]], analyzed["remote_schemas"])[0]["status"],
                "stale",
            )

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

    def test_captured_factory_artifacts_reuse_analysis_and_type_original_source(
        self,
    ) -> None:
        config = (
            self.config.read_text(encoding="utf8")
            + 'scalars={Token={input="builtins.str",output="builtins.int"}}\n'
        )
        source = """from typing import TYPE_CHECKING,TypeVar
from typing_extensions import assert_type
from graphql_desk import TypedDocument
from graphql_desk_generated.default import graphql
doc=graphql("query Q { selected token }")
if TYPE_CHECKING:
 R=TypeVar("R")
 V=TypeVar("V")
 def execute(document:TypedDocument[R,V])->R: raise RuntimeError
 assert_type(execute(doc)["selected"],str|None)
 assert_type(execute(doc)["token"],int)
"""
        capture = self.capture()
        capture["documents"] = [
            {**self.document(self.source), "id": "source"},
            {**self.document(self.config), "id": "config"},
            self.document(self.schema),
        ]
        capture["buffers"] = [
            self.buffer(self.source, source),
            self.buffer(self.config, config),
        ]
        buffers = [
            self.buffer(
                self.schema, "scalar Token type Query {selected:String token:Token!}"
            )
        ]
        identity = self.prepare(capture)
        self.assertTrue(self.analyze(capture, identity, buffers)["ok"])
        original = {
            path: path.read_bytes() for path in (self.source, self.schema, self.config)
        }
        with (
            patch(
                "graphql_desk.editor_analysis.resolve_project",
                side_effect=AssertionError("duplicate resolver"),
            ),
            patch(
                "graphql_desk.editor_analysis.index_fragments",
                side_effect=AssertionError("duplicate index"),
            ),
        ):
            result = self.worker.handle(
                {
                    "op": "artifacts",
                    **capture,
                    "id": identity,
                    "schema_buffers": buffers,
                }
            )
        self.assertTrue(result["ok"], result)
        projects = cast(list[dict[str, object]], result["projects"])
        self.assertEqual(len(projects), 1)
        self.assertEqual(projects[0]["project"], self.config.as_uri())
        self.assertEqual(projects[0]["diagnostics"], [])
        files = cast(list[dict[str, str]], projects[0]["files"])
        self.assertIn(
            "graphql_desk_generated/default.pyi",
            {file["relative_path"] for file in files},
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for file in files:
                path = root / file["relative_path"]
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(file["text"], encoding="utf8")
            (root / "main.py").write_text(source, encoding="utf8")
            factory_tests.FactoryGenerationTests().check(root)
            runtime = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "import main; assert main.doc.schema_name=='default'",
                ],
                cwd=root,
                capture_output=True,
                text=True,
            )
            self.assertEqual(runtime.returncode, 0, runtime.stderr)
        for path, content in original.items():
            self.assertEqual(path.read_bytes(), content)
        self.assertFalse((self.root / "graphql_desk_generated").exists())

    def test_factory_artifacts_invalid_and_repaired_calls_preserve_diagnostics(
        self,
    ) -> None:
        self.schema.write_text("type Query {value:String!}", encoding="utf8")
        for selected in ("value", "missing", "value"):
            source = (
                'from graphql_desk_generated.default import graphql\nq=graphql("{ '
                + selected
                + ' }")\n'
            )
            capture = self.capture()
            capture["documents"] = [self.document(self.source)]
            capture["buffers"] = [self.buffer(self.source, source)]
            identity = self.prepare(capture)
            analyzed = self.analyze(capture, identity)
            self.assertTrue(analyzed["ok"], analyzed)
            result = self.worker.handle(
                {"op": "artifacts", **capture, "id": identity, "schema_buffers": []}
            )
            self.assertTrue(result["ok"], result)
            project = cast(list[dict[str, object]], result["projects"])[0]
            files = cast(list[dict[str, str]], project["files"])
            factory = next(
                file["text"]
                for file in files
                if file["relative_path"].endswith("/default.pyi")
            )
            self.assertEqual("GraphqlDeskUnavailable" in factory, selected == "missing")
            codes = [
                item["code"]
                for item in cast(list[dict[str, object]], project["diagnostics"])
            ]
            self.assertEqual(
                codes,
                ["graphql-validation", "factory-unavailable"]
                if selected == "missing"
                else [],
            )

    def test_syntax_error_keeps_sibling_types_and_repair_restores_them(self) -> None:
        self.schema.write_text("type Query {value:String!}", encoding="utf8")
        source = """from typing import TYPE_CHECKING, TypeVar
from typing_extensions import assert_type
from graphql_desk import TypedDocument
from graphql_desk_generated.default import graphql
from graphql_desk_generated.sources.main import GoodResult, GoodVariables
broken=graphql("query Broken { value")
good=graphql("query Good { value }")
if TYPE_CHECKING:
 R=TypeVar("R")
 V=TypeVar("V")
 def execute(document:TypedDocument[R,V],variables:V)->R: raise RuntimeError
 assert_type(execute(good,GoodVariables()),GoodResult)
 execute(broken,GoodVariables()) # error
"""

        def typecheck(text: str, *, expect_broken_error: bool) -> None:
            version = 2 if expect_broken_error else 3
            self.source.write_text(text, encoding="utf8")
            capture = self.capture()
            capture["documents"] = [self.document(self.source, version)]
            capture["buffers"] = [self.buffer(self.source, text, version)]
            identity = self.prepare(capture)
            analyzed = self.analyze(capture, identity)
            self.assertTrue(analyzed["ok"], analyzed)
            result = self.worker.handle(
                {"op": "artifacts", **capture, "id": identity, "schema_buffers": []}
            )
            self.assertTrue(result["ok"], result)
            project = cast(list[dict[str, object]], result["projects"])[0]
            diagnostics = cast(list[dict[str, object]], project["diagnostics"])
            self.assertEqual(
                [item["code"] for item in diagnostics],
                ["invalid-fragments"] if expect_broken_error else [],
            )
            files = cast(list[dict[str, str]], project["files"])
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                for file in files:
                    path = root / file["relative_path"]
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(file["text"], encoding="utf8")
                (root / "main.py").write_text(text, encoding="utf8")
                errors = (
                    {
                        "main.py": {
                            next(
                                number
                                for number, line in enumerate(text.splitlines(), 1)
                                if "execute(broken" in line
                            )
                        }
                    }
                    if expect_broken_error
                    else None
                )
                factory_tests.FactoryGenerationTests().check(root, errors)

        typecheck(source, expect_broken_error=True)
        repaired = (
            source.replace(
                'broken=graphql("query Broken { value")',
                'broken=graphql("query Broken { value }")',
            )
            .replace(
                "from graphql_desk_generated.sources.main import "
                "GoodResult, GoodVariables\n",
                "from graphql_desk_generated.sources.main import "
                "GoodResult, GoodVariables, BrokenResult\n",
            )
            .replace(
                " execute(broken,GoodVariables()) # error",
                " assert_type(execute(broken,GoodVariables()),BrokenResult)",
            )
        )
        typecheck(repaired, expect_broken_error=False)

    def test_factory_artifacts_require_exact_current_analyzed_capture(self) -> None:
        capture = self.capture()
        identity = self.prepare(capture)
        request: dict[str, object] = {
            "op": "artifacts",
            **capture,
            "id": identity,
            "schema_buffers": [],
        }
        self.assertFalse(self.worker.handle(request)["ok"])
        for mutation in ("source", "config", "snapshot", "generation"):
            capture = self.capture()
            identity = self.prepare(capture)
            self.assertTrue(self.analyze(capture, identity)["ok"])
            request = {
                "op": "artifacts",
                **capture,
                "id": identity,
                "schema_buffers": [],
            }
            if mutation == "source":
                self.source.write_text(
                    self.source.read_text(encoding="utf8") + "# changed\n",
                    encoding="utf8",
                )
            elif mutation == "config":
                self.config.write_text(
                    self.config.read_text(encoding="utf8") + "disable_masking=true\n",
                    encoding="utf8",
                )
            elif mutation == "snapshot":
                request["schema_buffers"] = [
                    self.buffer(self.schema, "type Query {changed:Int}")
                ]
            else:
                self.prepare(capture)
            result = self.worker.handle(request)
            if mutation in {"source", "config"}:
                self.assertEqual(
                    result,
                    {"ok": True, "id": identity, "current": False},
                    mutation,
                )
            else:
                self.assertFalse(result["ok"], mutation)

    def test_factory_artifacts_preserve_healthy_project_beside_unfetched_remote(
        self,
    ) -> None:
        self.source.write_text(
            'from graphql_desk_generated.default import graphql\nq=graphql("{disk}")\n',
            encoding="utf8",
        )
        nested = self.root / "nested"
        nested.mkdir()
        nested_config = nested / "pyproject.toml"
        nested_config.write_text(
            '[tool.graphql-desk]\nschema={url="http://127.0.0.1:1/graphql"}\nschema_output="graphql_desk_generated/default.pyi"\n',
            encoding="utf8",
        )
        nested_source = nested / "main.py"
        nested_source.write_text(
            "from graphql_desk_generated.default import graphql\n"
            'q=graphql("{missing}")\n',
            encoding="utf8",
        )
        capture = self.capture()
        capture["candidates"] = [str(self.source), str(nested_source)]
        with patch("graphql_desk.editor_worker.load_from_url") as loader:
            identity = self.prepare(capture)
            self.assertTrue(self.analyze(capture, identity)["ok"])
            result = self.worker.handle(
                {"op": "artifacts", **capture, "id": identity, "schema_buffers": []}
            )
            loader.assert_not_called()
        self.assertTrue(result["ok"], result)
        self.assertEqual(
            result["unavailable_schemas"], ((nested_config.as_uri(), "default"),)
        )
        projects = {
            project["project"]: project
            for project in cast(list[dict[str, object]], result["projects"])
        }
        self.assertTrue(projects[self.config.as_uri()]["files"])
        self.assertEqual(projects[self.config.as_uri()]["diagnostics"], [])
        self.assertEqual(projects[nested_config.as_uri()]["files"], [])
        self.assertEqual(
            [
                diagnostic["code"]
                for diagnostic in cast(
                    list[dict[str, object]],
                    projects[nested_config.as_uri()]["diagnostics"],
                )
            ],
            ["missing-schema-context"],
        )

    def test_factory_artifacts_generate_all_named_schemas_for_one_project(self) -> None:
        (self.base / "alpha.graphql").write_text(
            "type Query { alpha: String! }", encoding="utf8"
        )
        (self.base / "beta.graphql").write_text(
            "type Query { beta: Int! }", encoding="utf8"
        )
        self.config.write_text(
            "[tool.graphql-desk]\n"
            '[[tool.graphql-desk.schemas]]\nname="alpha"\n'
            'schema="../alpha.graphql"\n'
            'schema_output="graphql_desk_generated/alpha.pyi"\n'
            '[[tool.graphql-desk.schemas]]\nname="beta"\n'
            'schema="../beta.graphql"\n'
            'schema_output="graphql_desk_generated/beta.pyi"\n',
            encoding="utf8",
        )
        self.source.write_text(
            "from graphql_desk_generated.alpha import graphql as alpha\n"
            "from graphql_desk_generated.beta import graphql as beta\n"
            'a=alpha("{ alpha }")\nb=beta("{ beta }")\n',
            encoding="utf8",
        )
        capture = self.capture()
        identity = self.prepare(capture)
        analyzed = self.analyze(capture, identity)
        self.assertTrue(analyzed["ok"], analyzed)
        result = self.worker.handle(
            {"op": "artifacts", **capture, "id": identity, "schema_buffers": []}
        )
        self.assertTrue(result["ok"], result)
        projects = cast(list[dict[str, object]], result["projects"])
        self.assertEqual(len(projects), 1)
        files = cast(list[dict[str, object]], projects[0]["files"])
        paths = {cast(str, file["relative_path"]) for file in files}
        self.assertIn("graphql_desk_generated/alpha.pyi", paths)
        self.assertIn("graphql_desk_generated/beta.pyi", paths)
        self.assertEqual(result["unavailable_schemas"], ())

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
