import json
import tempfile
import traceback
import unittest
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from unittest.mock import patch

from graphql import introspection_from_schema, print_schema

from graphql_desk.schema_loading import (
    load_from_introspection,
    load_from_sdl,
    load_local_schema,
)

_SDL = "type Query { greeting: String! }"


class SchemaLoadingTests(unittest.TestCase):
    def test_sdl_and_introspection_round_trip(self) -> None:
        schema = load_from_sdl(_SDL)
        data = introspection_from_schema(schema)
        payloads: tuple[Mapping[str, object], ...] = (
            data,
            {"data": data},
            {"data": data, "errors": []},
        )
        for payload in payloads:
            self.assertEqual(
                print_schema(load_from_introspection(MappingProxyType(payload))),
                print_schema(schema),
            )

    def test_local_extensions_bom_and_crlf(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            for suffix in (".graphql", ".gql", ".sdl", ".GRAPHQL"):
                file = Path(directory) / f"schema{suffix}"
                file.write_bytes(b"\xef\xbb\xbf" + (_SDL + "\r\n").encode())
                self.assertEqual(
                    print_schema(load_local_schema(file)),
                    print_schema(load_from_sdl(_SDL)),
                )
            file = Path(directory) / "schema.json"
            data = introspection_from_schema(load_from_sdl(_SDL))
            file.write_text(json.dumps({"data": data}), encoding="utf-8-sig")
            self.assertEqual(
                print_schema(load_local_schema(str(file))),
                print_schema(load_from_sdl(_SDL)),
            )

    def test_repeated_load_reads_changed_saved_schema(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            file = Path(directory) / "schema.gql"
            file.write_text(_SDL)
            first = load_local_schema(file)
            file.write_text("type Query { changed: Int }")
            second = load_local_schema(file)
            self.assertNotEqual(print_schema(first), print_schema(second))

    def test_invalid_sdl_and_invalid_schema_fail(self) -> None:
        for source in (
            "type Query {",
            "type Other { id: ID }",
            "type Query { x: Missing }",
        ):
            with self.subTest(source=source), self.assertRaises(ValueError):
                load_from_sdl(source)

    def test_partial_and_error_introspection_responses_fail(self) -> None:
        valid = introspection_from_schema(load_from_sdl(_SDL))
        invalid: tuple[Mapping[str, object], ...] = (
            {},
            {"__schema": None},
            {"__schema": {}},
            {"data": None},
            {"data": valid, "errors": [{"message": "server failed"}]},
            {"data": valid, "errors": None},
            {"data": valid, "__schema": {}},
            {"__schema": {"types": "invalid"}},
        )
        for data in invalid:
            with self.subTest(data=data), self.assertRaises(ValueError):
                load_from_introspection(data)

    def test_invalid_local_content_and_missing_file_fail(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            file = Path(directory) / "schema.json"
            for content in (b"{", b"[]", b"null", b"\xff"):
                file.write_bytes(content)
                with self.subTest(content=content), self.assertRaises(ValueError):
                    load_local_schema(file)
            with self.assertRaises(ValueError):
                load_local_schema(file.with_name("missing.gql"))

    def test_introspection_error_traceback_does_not_echo_response(self) -> None:
        malformed: Mapping[str, object] = {
            "__schema": {
                "types": [
                    {"name": "X", "kind": "BROKEN", "description": "PRIVATE_MARKER"}
                ]
            }
        }
        try:
            load_from_introspection(malformed)
        except ValueError:
            rendered = traceback.format_exc()
        else:
            self.fail("Malformed introspection must fail")
        self.assertNotIn("PRIVATE_MARKER", rendered)

    def test_urls_and_unknown_formats_do_not_read_files(self) -> None:
        with patch.object(
            Path, "read_text", side_effect=AssertionError("must not read")
        ):
            for path in (
                "https://user:secret@example.test/schema.json",
                "https:schema.json",
                "file:///schema.graphql",
                "schema.py",
                "schema",
            ):
                with self.subTest(path=path), self.assertRaises(ValueError) as error:
                    load_local_schema(path)
                self.assertNotIn("secret", str(error.exception))


if __name__ == "__main__":
    unittest.main()
