"""Real snapshot resolution and exact maps cross the JSON boundary unchanged."""

import copy
import json
import unittest
from pathlib import Path
from typing import cast
from unittest.mock import patch

from graphql import build_schema, introspection_from_schema

from graphql_desk import source_graph
from graphql_desk.editor_analysis import analyze_editor_json, analyze_editor_snapshot
from graphql_desk.persisted import editor_sha256_id
from graphql_desk.source_graph import IncrementalResolver, ModuleSnapshot


def module(
    name: str, source: str, *, project: str = "p", version: int = 1
) -> dict[str, object]:
    return {
        "project": project,
        "module": name,
        "uri": f"file:///{project}/{name}.py",
        "version": version,
        "source": source,
        "is_package": False,
    }


def request(
    source: str = "from graphql_desk import gql\nq=gql('{ user { id } }')",
) -> dict[str, object]:
    return {
        "request_id": "test-1",
        "modules": [module("main", source)],
        "bindings": [
            {
                "project": "p",
                "schemas": ["default"],
                "default_schema": "default",
                "factories": [],
                "type_mappings": [],
            }
        ],
        "schemas": [
            {
                "project": "p",
                "schema_name": "default",
                "uri": "file:///p/schema.graphql",
                "version": 2,
                "token": "schema-2",
                "format": "sdl",
                "text": "type User { id: ID! name: String } type Query { user: User }",
            }
        ],
    }


def object_(value: object) -> dict[str, object]:
    assert isinstance(value, dict)
    return cast(dict[str, object], value)


def array(value: object) -> list[object]:
    assert isinstance(value, list)
    return cast(list[object], value)


class EditorAnalysisTests(unittest.TestCase):
    def test_colocated_fragment_warning_defaults_on_and_can_be_disabled(self) -> None:
        payload = request(
            "from fragments import card as ui_card\n"
            "from graphql_desk import gql\nquery=gql('{ user { id } }')"
        )
        array(payload["modules"]).append(
            module(
                "fragments",
                "from graphql_desk import gql\n"
                "card=gql('fragment Card on User { id }')",
            )
        )
        diagnostics = array(analyze_editor_snapshot(payload)["diagnostics"])
        self.assertEqual(
            [object_(item)["code"] for item in diagnostics],
            ["unused-colocated-fragment"],
        )
        disabled = analyze_editor_snapshot(
            payload, colocated_fragment_checks={"p": False}
        )
        self.assertEqual(disabled["diagnostics"], [])

    def test_missing_persisted_id_insertion_requires_a_resolved_document(self) -> None:
        source = (
            "from graphql_desk import gql\n"
            "from fragments import card\n"
            "query = gql('query Q { user { ...Card } }', [card])\n"
            "saved = gql.persisted(document=query)\n"
        )
        payload = request(source)
        array(payload["modules"]).append(
            module(
                "fragments",
                "from graphql_desk import gql\n"
                "card = gql('fragment Card on User { name }')\n",
            )
        )
        fragment_module = array(payload["modules"])[-1]
        response = analyze_editor_snapshot(payload)
        insertion = object_(array(response["persisted_id_insertions"])[0])
        expected = editor_sha256_id(
            "query Q { user { ...Card } }", ("fragment Card on User {\n  name\n}",)
        )
        self.assertEqual(insertion["expected_id"], expected)
        insertion_range = object_(insertion["insertion"])
        insertion_codepoint = object_(insertion_range["codepoint"])
        self.assertEqual(insertion_codepoint["start"], source.index("document"))
        diagnostic = next(
            object_(item)
            for item in array(response["diagnostics"])
            if object_(item)["code"] == "persisted-id-missing"
        )
        call_start = source.index("gql.persisted")
        diagnostic_range = object_(diagnostic["range"])
        diagnostic_codepoint = object_(diagnostic_range["codepoint"])
        self.assertEqual(diagnostic_codepoint["start"], call_start)
        self.assertEqual(diagnostic_codepoint["end"], source.index("\n", call_start))

        ambiguous = request(
            source.replace(
                "gql.persisted(document=query)",
                "gql.persisted(make_document())",
            )
        )
        ambiguous_response = analyze_editor_snapshot(ambiguous)
        self.assertEqual(ambiguous_response["persisted_id_insertions"], [])

        positional_source = source.replace(
            "gql.persisted(document=query)", "gql.persisted(query)"
        )
        positional = request(positional_source)
        array(positional["modules"]).append(fragment_module)
        positional_response = analyze_editor_snapshot(positional)
        positional_insertion = object_(
            array(positional_response["persisted_id_insertions"])[0]
        )
        self.assertTrue(positional_insertion["positional"])
        positional_range = object_(positional_insertion["insertion"])
        self.assertEqual(
            object_(positional_range["codepoint"])["start"],
            positional_source.index("gql.persisted(query)") + len("gql.persisted("),
        )

        grouped_source = positional_source.replace(
            "gql.persisted(query)", "gql.persisted((query))"
        )
        grouped = request(grouped_source)
        array(grouped["modules"]).append(fragment_module)
        grouped_response = analyze_editor_snapshot(grouped)
        grouped_insertion = object_(
            array(grouped_response["persisted_id_insertions"])[0]
        )
        grouped_range = object_(grouped_insertion["insertion"])
        self.assertEqual(
            object_(grouped_range["codepoint"])["start"],
            grouped_source.index("gql.persisted((") + len("gql.persisted("),
        )

    def test_persisted_editor_snapshot_reports_stale_hash_and_exact_literal_action(
        self,
    ) -> None:
        source = (
            "from graphql_desk import gql\n"
            "from fragments import card\n"
            "query = gql('query Q { user { ...Card } }', [card])\n"
            "registered = gql.persisted('sha256:" + "0" * 64 + "', query)\n"
        )
        payload = request(source)
        array(payload["modules"]).append(
            module(
                "fragments",
                "from graphql_desk import gql\n"
                "card = gql('fragment Card on User { name }')\n",
            )
        )

        response = analyze_editor_snapshot(payload)
        persisted = object_(array(response["persisted"])[0])
        main = object_(array(payload["modules"])[0])
        id_text = "'sha256:" + "0" * 64 + "'"
        start = cast(str, main["source"]).index(id_text)
        expected = editor_sha256_id(
            "query Q { user { ...Card } }", ("fragment Card on User {\n  name\n}",)
        )
        self.assertEqual(persisted["expected_id"], expected)
        self.assertTrue(persisted["id_is_literal"])
        id_range = object_(persisted["id_range"])
        self.assertEqual(
            object_(id_range["codepoint"]),
            {"start": start, "end": start + len(id_text)},
        )
        diagnostics = [object_(item) for item in array(response["diagnostics"])]
        stale = next(
            item for item in diagnostics if item["code"] == "persisted-hash-mismatch"
        )
        self.assertEqual(stale["range"], id_range)

        alias_source = source.replace(
            "registered = gql.persisted('sha256:" + "0" * 64 + "', query)",
            "id = 'sha256:" + "0" * 64 + "'\nregistered = gql.persisted(id, query)",
        )
        object_(array(payload["modules"])[0])["source"] = alias_source
        alias = object_(array(analyze_editor_snapshot(payload)["persisted"])[0])
        self.assertFalse(alias["id_is_literal"])

    def test_malformed_persisted_calls_report_exact_diagnostics_without_fixes(
        self,
    ) -> None:
        prefix = (
            "from graphql_desk import gql, init_graphql\n"
            "query = gql('{ user { id } }')\n"
        )
        malformed = (
            (
                "starred positional",
                "gql.persisted(*args)",
                "gql.persisted(*args)",
                "persisted-not-static",
                "Persisted arguments must be supplied explicitly",
            ),
            (
                "expanded keywords",
                "gql.persisted(**kwargs)",
                "gql.persisted(**kwargs)",
                "persisted-not-static",
                "Persisted arguments must be supplied explicitly",
            ),
            (
                "duplicate keyword",
                "gql.persisted(id='a', id='b', document=query)",
                None,
                "invalid-python",
                "duplicate keyword: id",
            ),
            (
                "unknown keyword",
                "gql.persisted(query=query)",
                "gql.persisted(query=query)",
                "persisted-not-static",
                "Use id and document arguments only",
            ),
            (
                "positional ID plus keyword ID",
                "gql.persisted('a', id='b', document=query)",
                "gql.persisted('a', id='b', document=query)",
                "persisted-not-static",
                "Supply id and document once each",
            ),
            (
                "two positionals plus document",
                "gql.persisted('a', query, document=query)",
                "gql.persisted('a', query, document=query)",
                "persisted-not-static",
                "Supply id and document once each",
            ),
            (
                "excess positional arguments",
                "gql.persisted('a', query, query)",
                "gql.persisted('a', query, query)",
                "persisted-not-static",
                "Supply id and document once each",
            ),
            (
                "missing both arguments",
                "gql.persisted()",
                "gql.persisted()",
                "persisted-not-static",
                "Persisted declarations require a static ID and document",
            ),
            (
                "missing document",
                "gql.persisted(id='a')",
                "gql.persisted(id='a')",
                "persisted-not-static",
                "Persisted declarations require a static ID and document",
            ),
            (
                "non-string ID literal",
                "gql.persisted(123, query)",
                "123",
                "persisted-not-static",
                "Persisted ID must resolve to a static string",
            ),
            (
                "unresolved ID",
                "gql.persisted(make_id(), query)",
                "make_id()",
                "persisted-not-static",
                "Persisted ID must resolve to a static string",
            ),
            (
                "unresolved document",
                "gql.persisted('a', make_document())",
                "make_document()",
                "persisted-not-static",
                "Persisted document must resolve to a GraphQL Desk document",
            ),
        )
        for label, expression, diagnostic_span, code, message in malformed:
            source = prefix + f"saved = {expression}\n"
            with self.subTest(shape=label):
                response = analyze_editor_snapshot(request(source))
                self.assertEqual(response["persisted"], [])
                self.assertEqual(response["persisted_id_insertions"], [])
                diagnostics = [
                    object_(item)
                    for item in array(response["diagnostics"])
                    if object_(item)["code"] == code
                ]
                self.assertEqual(len(diagnostics), 1)
                diagnostic = diagnostics[0]
                diagnostic_message = cast(str, diagnostic["message"])
                if label == "duplicate keyword":
                    self.assertIn("keyword argument repeated: id", diagnostic_message)
                    self.assertIn("line 3", diagnostic_message)
                else:
                    self.assertEqual(diagnostic_message, message)
                codepoint = object_(object_(diagnostic["range"])["codepoint"])
                if diagnostic_span is None:
                    expected = {"start": 0, "end": len(source)}
                else:
                    start = source.index(diagnostic_span, source.index("saved = "))
                    expected = {"start": start, "end": start + len(diagnostic_span)}
                self.assertEqual(
                    codepoint,
                    expected,
                )

        wrong_schema_source = (
            "from graphql_desk import init_graphql\n"
            "main = init_graphql('main')\n"
            "other = init_graphql('other')\n"
            "query = main('query Q { user { id } }')\n"
            "other_query = other('query Other { user { id } }')\n"
            "saved = main.persisted('a', other_query)\n"
        )
        wrong_schema = request(wrong_schema_source)
        object_(array(wrong_schema["bindings"])[0])["schemas"] = ["main", "other"]
        object_(array(wrong_schema["bindings"])[0])["default_schema"] = "main"
        wrong_schema["schemas"] = [
            {
                "project": "p",
                "schema_name": name,
                "uri": f"file:///p/{name}.graphql",
                "version": version,
                "token": f"{name}-schema",
                "format": "sdl",
                "text": "type User { id: ID! } type Query { user: User }",
            }
            for name, version in (("main", 2), ("other", 3))
        ]
        response = analyze_editor_snapshot(wrong_schema)
        self.assertEqual(response["persisted"], [])
        self.assertEqual(response["persisted_id_insertions"], [])
        diagnostic = next(
            object_(item)
            for item in array(response["diagnostics"])
            if object_(item)["code"] == "persisted-not-static"
        )
        self.assertEqual(
            diagnostic["message"],
            "Persisted declaration and document use different schemas",
        )
        codepoint = object_(object_(diagnostic["range"])["codepoint"])
        start = wrong_schema_source.index(
            "other_query", wrong_schema_source.index("saved = ")
        )
        self.assertEqual(codepoint, {"start": start, "end": start + len("other_query")})

    def test_valid_opaque_persisted_ids_are_not_misdiagnosed(self) -> None:
        source = (
            "from graphql_desk import gql\n"
            "query = gql('{ user { id } }')\n"
            "saved = gql.persisted('opaque:λ', query)\n"
        )
        response = analyze_editor_snapshot(request(source))
        self.assertEqual(response["diagnostics"], [])
        self.assertEqual(response["persisted_id_insertions"], [])
        self.assertEqual(object_(array(response["persisted"])[0])["id"], "opaque:λ")

    def test_type_mapping_status_uses_captured_symbols_only(self) -> None:
        payload = request("from graphql_desk import gql\nq=gql('{ date }')")
        schema = object_(array(payload["schemas"])[0])
        schema["text"] = "scalar Date\ntype Query { date: Date }"
        binding = object_(array(payload["bindings"])[0])
        binding["type_mappings"] = [
            {
                "schema_name": "default",
                "scalars": [
                    {
                        "name": "Date",
                        "input": "local_types.DateValue",
                        "output": "vendor_dates.DateValue",
                    }
                ],
                "enums": [],
            }
        ]
        array(payload["modules"]).append(module("local_types", "class DateValue: ..."))
        response = analyze_editor_snapshot(payload)
        diagnostics = [object_(item) for item in array(response["diagnostics"])]
        self.assertEqual([item["code"] for item in diagnostics], ["mapping-unverified"])
        self.assertIn(
            "outside the captured project snapshot",
            cast(str, diagnostics[0]["message"]),
        )

        mappings = cast(list[dict[str, object]], binding["type_mappings"])
        scalar_mappings = cast(list[dict[str, object]], mappings[0]["scalars"])
        scalar_mappings[0]["output"] = "local_types.Missing"
        response = analyze_editor_snapshot(payload)
        diagnostics = [object_(item) for item in array(response["diagnostics"])]
        self.assertEqual([item["code"] for item in diagnostics], ["mapping-unresolved"])
        self.assertIn("Missing", cast(str, diagnostics[0]["message"]))

    def test_interpreter_mapping_sources_verify_only_the_matching_project_schema(
        self,
    ) -> None:
        payload = request("from graphql_desk import gql\nq=gql('{ date }')")
        binding = object_(array(payload["bindings"])[0])
        binding["schemas"] = ["events", "admin"]
        binding["default_schema"] = "events"
        binding["type_mappings"] = [
            {
                "schema_name": "events",
                "scalars": [
                    {
                        "name": "Date",
                        "input": "vendor_dates.DateInput",
                        "output": "vendor_dates.DateOutput",
                    }
                ],
                "enums": [],
            }
        ]
        schema = object_(array(payload["schemas"])[0])
        schema["schema_name"] = "events"
        schema["text"] = "scalar Date\ntype Query { date: Date }"
        cast(list[dict[str, object]], payload["schemas"]).append(
            {
                **schema,
                "schema_name": "admin",
                "token": "admin-schema",
                "text": "type Query { status: String }",
            }
        )
        external = ModuleSnapshot(
            "p#interpreter",
            "vendor_dates",
            "file:///external/vendor_dates.pyi",
            1,
            "class DateInput: ...\nclass DateOutput: ...\n",
        )

        response = analyze_editor_snapshot(
            payload, external_mapping_modules=(external,)
        )
        self.assertEqual(response["diagnostics"], [])

        missing = ModuleSnapshot(
            external.project,
            external.module,
            external.uri,
            2,
            "class DateInput: ...\nclass Different: ...\n",
        )
        response = analyze_editor_snapshot(payload, external_mapping_modules=(missing,))
        diagnostics = [object_(item) for item in array(response["diagnostics"])]
        self.assertEqual([item["code"] for item in diagnostics], ["mapping-unresolved"])

        isolated = ModuleSnapshot(
            "other#interpreter",
            external.module,
            external.uri,
            1,
            external.source,
        )
        response = analyze_editor_snapshot(
            payload, external_mapping_modules=(isolated,)
        )
        diagnostics = [object_(item) for item in array(response["diagnostics"])]
        self.assertEqual(
            [item["code"] for item in diagnostics],
            ["mapping-unverified", "mapping-unverified"],
        )

    def test_explicit_resolver_reuses_only_source_facts(self) -> None:
        payload = request()
        resolver = IncrementalResolver()
        cold = analyze_editor_snapshot(payload)
        self.assertEqual(analyze_editor_snapshot(payload, resolver=resolver), cold)
        with patch.object(
            source_graph, "_parse", wraps=getattr(source_graph, "_parse")
        ) as parse:
            self.assertEqual(analyze_editor_snapshot(payload, resolver=resolver), cold)
            changed = copy.deepcopy(payload)
            changed["request_id"] = "new-request"
            schema = object_(array(changed["schemas"])[0])
            schema["token"] = "new-schema"
            schema["text"] = "type User { name: String } type Query { user: User }"
            response = analyze_editor_snapshot(changed, resolver=resolver)
            self.assertEqual(response, analyze_editor_snapshot(changed))
            self.assertEqual(response["request_id"], "new-request")
            self.assertEqual(
                object_(array(response["schemas"])[0])["token"], "new-schema"
            )
            schema["text"] = "not valid SDL"
            with self.assertRaisesRegex(ValueError, "Invalid supplied local schema"):
                analyze_editor_snapshot(changed, resolver=resolver)
            # The cold comparison parsed once; both warm requests reused source facts.
            self.assertEqual(parse.call_count, 1)
            object_(array(payload["modules"])[0])["source"] = (
                "from graphql_desk import gql\nq=gql('{ user { name } }')"
            )
            self.assertFalse(
                array(
                    analyze_editor_snapshot(payload, resolver=resolver)["diagnostics"]
                )
            )
            self.assertEqual(parse.call_count, 2)

    def test_json_real_resolver_index_and_no_source_io(self) -> None:
        payload = request()
        array(payload["modules"]).append(
            module("unused", "raise RuntimeError('never execute')")
        )
        with patch.object(
            Path, "read_text", side_effect=AssertionError("No file reads")
        ):
            response = object_(json.loads(analyze_editor_json(json.dumps(payload))))
        self.assertEqual(response["request_id"], "test-1")
        self.assertEqual(response["diagnostics"], [])
        self.assertEqual(len(array(response["modules"])), 2)
        call = object_(array(response["calls"])[0])
        entry = object_(call["index"])
        self.assertEqual(entry["context_token"], "schema-2")
        self.assertEqual(object_(entry["selected_operation"])["kind"], "query")
        self.assertEqual(object_(array(response["schemas"])[0])["version"], 2)

    def test_invalid_graphql_keeps_maps_and_incomplete_python_does_not(self) -> None:
        response = analyze_editor_snapshot(
            request("from graphql_desk import gql\nq=gql('{ user { na')")
        )
        call = object_(array(response["calls"])[0])
        self.assertEqual(call["source"], "{ user { na")
        self.assertIsNone(call["index"])
        self.assertTrue(array(object_(call["map"])["utf16"]))
        self.assertTrue(array(response["diagnostics"]))
        invalid = analyze_editor_snapshot(
            request("from graphql_desk import gql\nq=gql('")
        )
        self.assertEqual(invalid["calls"], [])
        self.assertTrue(array(invalid["diagnostics"]))
        repaired = analyze_editor_snapshot(request())
        self.assertFalse(array(repaired["diagnostics"]))

    def test_real_codepoint_utf16_escapes_crlf_and_repeated_origins(self) -> None:
        source = (
            "# 😀\r\nfrom graphql_desk import gql\r\nTEXT='😀\\n'\r\nq=gql(TEXT+TEXT)"
        )
        response = analyze_editor_snapshot(request(source))
        call = object_(array(response["calls"])[0])
        mapping = object_(call["map"])
        cp = [object_(item) for item in array(mapping["codepoint"])]
        utf16 = [object_(item) for item in array(mapping["utf16"])]
        self.assertEqual(len(cp), 4)
        self.assertEqual(object_(cp[0]["decoded"]), {"start": 0, "end": 1})
        self.assertEqual(object_(utf16[0]["decoded"]), {"start": 0, "end": 2})
        cp_host = object_(object_(cp[0]["host"])["range"])
        utf16_host = object_(object_(utf16[0]["host"])["range"])
        self.assertEqual(utf16_host["start"], cast(int, cp_host["start"]) + 1)
        self.assertEqual(utf16_host["end"], cast(int, cp_host["end"]) + 2)
        self.assertEqual(cp[1]["linear"], False)
        self.assertEqual(cp[0]["host"], cp[2]["host"])
        self.assertEqual(object_(utf16[2]["decoded"]), {"start": 3, "end": 5})

    def test_imported_fragment_closure_has_real_document_provenance(self) -> None:
        payload = request(
            "from graphql_desk import gql\nfrom fragments import frag\n"
            "q=gql('{ user { ...Card } }',[frag])"
        )
        array(payload["modules"]).append(
            module(
                "fragments",
                "from graphql_desk import gql\n"
                "frag=gql('fragment Card on User { id }')",
                version=7,
            )
        )
        response = analyze_editor_snapshot(payload)
        self.assertEqual(response["diagnostics"], [])
        call = object_(array(response["calls"])[0])
        reference = object_(array(call["fragments"])[0])
        target = object_(reference["target"])
        self.assertEqual(object_(target["module"])["version"], 7)
        entry = object_(call["index"])
        definitions = [object_(item) for item in array(entry["definitions"])]
        self.assertEqual([item["name"] for item in definitions], [None, "Card"])
        self.assertEqual(definitions[1]["document"], target)
        self.assertEqual(call["source"], "{ user { ...Card } }")
        self.assertEqual(len(array(entry["dependencies"])), 2)

    def test_explicit_fact_routing_and_introspection(self) -> None:
        payload = request(
            "from graphql_desk import init_graphql\ngql=init_graphql('shop')\n"
            "q=gql('{ user { id } }')"
        )
        binding = object_(array(payload["bindings"])[0])
        binding["schemas"] = ["shop"]
        binding["default_schema"] = None
        schema = object_(array(payload["schemas"])[0])
        schema["schema_name"] = "shop"
        schema["format"] = "introspection"
        schema["uri"] = "file:///p/schema.json"
        schema["text"] = json.dumps(
            introspection_from_schema(build_schema(cast(str, schema["text"])))
        )
        response = analyze_editor_snapshot(payload)
        call = object_(array(response["calls"])[0])
        self.assertEqual(call["schema_name"], "shop")
        self.assertEqual(object_(call["index"])["schema_name"], "shop")
        object_(array(payload["modules"])[0])["source"] = (
            "from graphql_desk import gql\nq=gql('{ user { id } }')"
        )
        self.assertEqual(analyze_editor_snapshot(payload)["calls"], [])

    def test_configured_factory_identity_and_dynamic_source_do_not_guess(self) -> None:
        payload = request(
            "from generated.default import graphql as document\n"
            "q=document('{ user { id } }')"
        )
        object_(array(payload["bindings"])[0])["factories"] = [
            {
                "module": "generated.default",
                "export": "graphql",
                "schema_name": "default",
            }
        ]
        response = analyze_editor_snapshot(payload)
        self.assertEqual(len(array(response["calls"])), 1)
        self.assertEqual(object_(array(response["calls"])[0])["schema_name"], "default")
        for source in (
            "from unrelated import graphql\nq=graphql('{ user { id } }')",
            "from graphql_desk import gql\nq=gql(dynamic)",
            "from graphql_desk import gql\ngql=lambda source:source\n"
            "q=gql('{ user { id } }')",
        ):
            with self.subTest(source=source):
                object_(array(payload["modules"])[0])["source"] = source
                self.assertEqual(analyze_editor_snapshot(payload)["calls"], [])

    def test_invalid_primary_keeps_valid_explicit_fragment_target(self) -> None:
        payload = request(
            "from graphql_desk import gql\nfrom fragments import frag\n"
            "q=gql('{ user { ...',[frag])"
        )
        array(payload["modules"]).append(
            module(
                "fragments",
                "from graphql_desk import gql\n"
                "frag=gql('fragment Card on User { id }')",
            )
        )
        response = analyze_editor_snapshot(payload)
        calls = [object_(item) for item in array(response["calls"])]
        self.assertIsNone(calls[0]["index"])
        self.assertTrue(array(calls[0]["fragments"]))
        self.assertEqual(
            object_(object_(calls[1]["index"])["primary_fragment"])["name"], "Card"
        )

    def test_empty_and_continuation_atoms_survive_both_encodings(self) -> None:
        for literal in ('""', '"a\\\r\nb"'):
            with self.subTest(literal=literal):
                response = analyze_editor_snapshot(
                    request("from graphql_desk import gql\nq=gql(" + literal + ")")
                )
                call = object_(array(response["calls"])[0])
                mapping = object_(call["map"])
                self.assertEqual(mapping["codepoint"], mapping["utf16"])
                atoms = [object_(item) for item in array(mapping["codepoint"])]
                self.assertTrue(
                    any(
                        object_(atom["decoded"])["start"]
                        == object_(atom["decoded"])["end"]
                        for atom in atoms
                    )
                )

    def test_missing_schema_context_is_explicit_without_losing_source(self) -> None:
        payload = request()
        payload["schemas"] = []
        response = analyze_editor_snapshot(payload)
        call = object_(array(response["calls"])[0])
        self.assertIsNone(call["index"])
        self.assertEqual(
            object_(array(response["diagnostics"])[0])["code"], "missing-schema-context"
        )

    def test_strict_json_and_snapshot_shapes(self) -> None:
        for text in ('{"request_id":"a","request_id":"b"}', "NaN", "Infinity", "{"):
            with self.subTest(text=text), self.assertRaisesRegex(ValueError, "JSON"):
                analyze_editor_json(text)
        changes: list[tuple[str, str, object]] = [
            ("modules", "version", True),
            ("modules", "version", -1),
            ("modules", "version", 1.0),
            ("modules", "is_package", 0),
            ("modules", "uri", "https://example.test/main.py"),
            ("modules", "project", "unknown"),
            ("modules", "extra", "no"),
            ("schemas", "uri", "https://example.test/schema"),
            ("schemas", "version", False),
            ("schemas", "schema_name", "unknown"),
            ("schemas", "format", "remote"),
            ("schemas", "token", ""),
            ("bindings", "default_schema", "unknown"),
        ]
        for group, key, value in changes:
            with self.subTest(group=group, key=key, value=value):
                payload = request()
                object_(array(payload[group])[0])[key] = value
                with self.assertRaises(ValueError):
                    analyze_editor_snapshot(payload)
        for group in ("modules", "schemas", "bindings"):
            payload = request()
            array(payload[group]).append(copy.deepcopy(array(payload[group])[0]))
            with self.subTest(group=group), self.assertRaises(ValueError):
                analyze_editor_snapshot(payload)
        payload = request()
        payload["extra"] = "no"
        with self.assertRaises(ValueError):
            analyze_editor_snapshot(payload)

    def test_invalid_schema_does_not_echo_payload_in_exception(self) -> None:
        for format_, text in (
            ("sdl", "PRIVATE_MARKER"),
            ("introspection", '{"data":"PRIVATE_MARKER"}'),
        ):
            payload = request()
            schema = object_(array(payload["schemas"])[0])
            schema["format"], schema["text"] = format_, text
            with (
                self.subTest(format=format_),
                self.assertRaisesRegex(ValueError, "supplied local schema") as caught,
            ):
                analyze_editor_snapshot(payload)
            self.assertNotIn("PRIVATE_MARKER", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
