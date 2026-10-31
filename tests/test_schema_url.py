"""Real HTTP requests and GraphQL execution for the explicit URL loader."""

import json
import re
import ssl
import threading
import time
import traceback
import unittest
from collections.abc import Generator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar, cast
from unittest.mock import patch

from graphql import (
    GraphQLInputObjectType,
    GraphQLScalarType,
    build_schema,
    graphql_sync,  # pyright: ignore[reportUnknownVariableType]
)

from graphql_desk.config import SchemaOrigin
from graphql_desk.schema_url import get_url_config, load_from_url

TLS_FIXTURES = Path(__file__).parent / "fixtures" / "tls"


class Endpoint(BaseHTTPRequestHandler):
    records: ClassVar[list[tuple[str, dict[str, str], dict[str, object]]]] = []
    mode: ClassVar[str] = "modern"
    target: ClassVar[str] = ""

    def log_message(self, format: str, *args: object) -> None:
        pass

    def do_GET(self) -> None:
        self.do_POST()

    def do_POST(self) -> None:
        raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        body = cast(dict[str, object], json.loads(raw)) if raw else {}
        self.records.append((self.command, dict(self.headers), body))
        query = str(body.get("query", ""))
        if self.path == "/redirect":
            self.send_response(307)
            self.send_header("Location", self.target)
            self.end_headers()
            return
        if self.path == "/get":
            self.send_response(303)
            self.send_header("Location", "/schema")
            self.end_headers()
            return
        if self.mode in {"auth", "forbidden", "http-error"}:
            self.send_response(
                {"auth": 401, "forbidden": 403, "http-error": 500}[self.mode],
                "secret response",
            )
            self.end_headers()
            return
        schema = build_schema("""
            scalar URL @specifiedBy(url: "https://example.com/spec")
            directive @tag(old: String @deprecated) repeatable on FIELD
            input Choice @oneOf { name: String count: Int }
            type Query { title(old: String @deprecated): String }
        """)
        if self.mode in {"support-error", "support-400"} and (
            "IntrospectSupportQuery" in query
        ):
            data: object = {"errors": [{"message": "secret"}]}
        elif self.mode in {"graphql-auth", "graphql-forbidden"}:
            data = {
                "errors": [
                    {
                        "message": "secret response",
                        "extensions": {
                            "code": (
                                "UNAUTHENTICATED"
                                if self.mode == "graphql-auth"
                                else "FORBIDDEN"
                            )
                        },
                    }
                ]
            }
        elif self.mode == "missing-data":
            data = {}
        elif self.mode == "invalid-errors":
            data = {"errors": "secret"}
        elif self.mode == "query-error" and "IntrospectionQuery" in query:
            data = {"errors": [{"message": "secret"}]}
        else:
            data = graphql_sync(schema, query or "{ __typename }").formatted
        if "IntrospectionQuery" in query:
            if self.mode == "partial":
                cast(dict[str, object], data)["errors"] = [
                    {"message": "secret response"}
                ]
            elif self.mode == "incomplete":
                data = {
                    "data": {"__schema": {"queryType": {"name": "secret response"}}}
                }
        if self.mode in {"legacy", "mixed", "mixed-field"}:
            # Emulate older servers without mutating core's global introspection types.
            payload = cast(dict[str, object], data)
            result_data = cast(dict[str, object], payload.get("data", {}))
            objects = dict(result_data)
            if "__schema" in result_data:
                objects = {
                    str(item["name"]): item
                    for item in cast(
                        list[dict[str, object]],
                        cast(dict[str, object], result_data["__schema"])["types"],
                    )
                }
            aliases = {
                "type": "__Type",
                "inputValue": "__InputValue",
                "directive": "__Directive",
                "field": "__Field",
            }
            for key, item in objects.items():
                if not isinstance(item, dict):
                    continue
                obj = cast(dict[str, object], item)
                if not obj.get("fields"):
                    continue
                obj = cast(dict[str, object], item)
                type_name = aliases.get(key, key)
                omitted: set[str] = (
                    {"specifiedByURL", "isOneOf"}
                    if (type_name == "__Type" and self.mode != "mixed-field")
                    else set()
                )
                if self.mode == "legacy":
                    omitted |= {"isRepeatable", "isDeprecated", "deprecationReason"}
                fields = cast(list[dict[str, object]], obj["fields"])
                obj["fields"] = [
                    field for field in fields if field["name"] not in omitted
                ]
                for field in fields:
                    if (
                        (
                            type_name == "__Directive"
                            and field["name"] == "args"
                            and self.mode != "mixed-field"
                        )
                        or (
                            self.mode == "mixed-field"
                            and type_name == "__Field"
                            and field["name"] == "args"
                        )
                        or (
                            self.mode == "legacy"
                            and field["name"] in {"args", "inputFields"}
                        )
                    ):
                        field["args"] = [
                            arg
                            for arg in cast(
                                list[dict[str, object]],
                                field.get("args", []),
                            )
                            if arg["name"] != "includeDeprecated"
                        ]
            if (
                "IntrospectionQuery" in query
                and self.mode == "legacy"
                and any(
                    value in query
                    for value in ("specifiedByURL", "isOneOf", "isRepeatable")
                )
            ):
                data = {"errors": [{"message": "Unsupported feature"}]}
            if "IntrospectionQuery" in query:
                directive_query = query.split("fragment FullType")[0]
                field_query = query.split("fragment FullType")[-1]
                unsupported = (
                    "args(includeDeprecated: true)" in directive_query
                    if self.mode != "mixed-field"
                    else "args(includeDeprecated: true)" in field_query
                )
                if self.mode == "legacy":
                    unsupported |= bool(
                        re.search(
                            r"(?:args|inputFields)\(includeDeprecated: true\)",
                            field_query,
                        )
                    )
                    unsupported |= (
                        "isDeprecated"
                        in query.split("fragment InputValue")[-1].split(
                            "fragment TypeRef"
                        )[0]
                    )
                if unsupported:
                    data = {"errors": [{"message": "Unsupported argument"}]}
        encoded = json.dumps(data).encode()
        if self.mode in {"utf16", "utf32"}:
            encoded = json.dumps(data).encode(
                "utf-16" if self.mode == "utf16" else "utf-32"
            )
        if self.mode == "malformed":
            encoded = b"secret not JSON"
        if self.mode == "slow":
            time.sleep(0.15)
        self.send_response(
            400
            if self.mode == "support-400" and ("IntrospectSupportQuery" in query)
            else 200
        )
        if self.mode in {"chunked", "conflict"}:
            self.send_header("Transfer-Encoding", "chunked")
        if self.mode != "chunked":
            self.send_header("Content-Length", str(len(encoded)))
        if self.mode == "duplicate-length":
            self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        try:
            self.wfile.write(
                f"{len(encoded):x}\r\n".encode() + encoded + b"\r\n0\r\n\r\n"
                if self.mode == "chunked"
                else encoded
            )
        except OSError:
            pass


@contextmanager
def endpoint(
    mode: str = "modern", *, certificate: str | None = None
) -> Generator[tuple[str, type[Endpoint]], None, None]:
    handler = type("Handler", (Endpoint,), {"records": [], "mode": mode})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    if certificate is not None:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(
            TLS_FIXTURES / f"{certificate}.pem", TLS_FIXTURES / "server-key.pem"
        )
        server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        scheme = "https" if certificate is not None else "http"
        yield f"{scheme}://127.0.0.1:{server.server_port}", handler
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


class SchemaURLTests(unittest.TestCase):
    def test_url_config_is_pure_and_preserves_unresolved_headers(self) -> None:
        origin = SchemaOrigin(
            url="https://example.com/graphql?token=private",
            headers=(("Authorization", "Bearer ${env:TOKEN}"),),
        )
        with patch("graphql_desk.schema_url.build_opener") as opener:
            self.assertIs(get_url_config(origin), origin)
            self.assertEqual(origin.headers[0][1], "Bearer ${env:TOKEN}")
            self.assertNotIn("private", repr(origin))
            self.assertNotIn("TOKEN", repr(origin))
            for invalid in (
                None,
                SchemaOrigin(local_path=Path("schema.graphql")),
                SchemaOrigin(url="https://example.com", local_path=Path("schema.gql")),
                SchemaOrigin(url="ftp://example.com"),
                SchemaOrigin(url="http://user:private@example.com"),
                SchemaOrigin(url="https://example.com:0"),
                SchemaOrigin(url="https://example.com/#private"),
            ):
                self.assertIsNone(get_url_config(invalid))
            opener.assert_not_called()

    def test_verified_tls_authentication_and_certificate_failures(self) -> None:
        trusted = ssl.create_default_context(cafile=str(TLS_FIXTURES / "ca.pem"))
        with (
            endpoint(certificate="localhost") as (url, handler),
            patch("ssl._create_default_https_context", return_value=trusted),
        ):
            schema = load_from_url(
                SchemaOrigin(url=url, headers=(("Authorization", "Bearer private"),)),
                environment={},
            )
            self.assertIn("Query", schema.type_map)
            self.assertEqual(len(handler.records), 2)
            self.assertTrue(
                all(
                    item[1]["Authorization"] == "Bearer private"
                    for item in handler.records
                )
            )
        for certificate, context in (
            ("localhost", ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)),
            ("expired", trusted),
            ("wrong-host", trusted),
        ):
            with (
                self.subTest(certificate=certificate),
                endpoint(certificate=certificate) as (url, handler),
                patch("ssl._create_default_https_context", return_value=context),
            ):
                try:
                    load_from_url(
                        SchemaOrigin(
                            url=url + "/private-url-token",
                            headers=(("Authorization", "Bearer private-header-token"),),
                        ),
                        environment={},
                    )
                except ValueError as error:
                    self.assertEqual(str(error), "Schema transport failed.")
                    self.assertNotIn("private-url-token", traceback.format_exc())
                    self.assertNotIn("private-header-token", traceback.format_exc())
                else:
                    self.fail("Certificate verification must reject the connection")
                self.assertEqual(handler.records, [])

    def test_https_redirect_cannot_downgrade_or_send_credentials(self) -> None:
        trusted = ssl.create_default_context(cafile=str(TLS_FIXTURES / "ca.pem"))
        with (
            endpoint() as (http_url, insecure),
            endpoint(certificate="localhost") as (url, secure),
            patch("ssl._create_default_https_context", return_value=trusted),
        ):
            secure.target = http_url
            with self.assertRaisesRegex(ValueError, "Schema redirect failed"):
                load_from_url(
                    SchemaOrigin(
                        url=url + "/redirect", headers=(("X-Key", "private"),)
                    ),
                    environment={},
                )
            self.assertEqual(len(secure.records), 1)
            self.assertEqual(insecure.records, [])

    def test_current_features_headers_and_no_cache(self) -> None:
        with endpoint() as (url, handler):
            origin = SchemaOrigin(url=url, headers=(("X-Key", "${env:TOKEN}"),))
            for token in ("first", "second"):
                schema = load_from_url(origin, environment={"TOKEN": token})
                self.assertTrue(
                    cast(GraphQLInputObjectType, schema.get_type("Choice")).is_one_of
                )
                directive = schema.get_directive("tag")
                assert directive is not None
                self.assertTrue(directive.is_repeatable)
                self.assertEqual(
                    cast(GraphQLScalarType, schema.get_type("URL")).specified_by_url,
                    "https://example.com/spec",
                )
                query = schema.query_type
                assert query is not None
                self.assertIsNotNone(
                    query.fields["title"].args["old"].deprecation_reason
                )
                self.assertIsNotNone(directive.args["old"].deprecation_reason)
            self.assertEqual(len(handler.records), 4)
            self.assertEqual(
                [item[1]["X-Key"] for item in handler.records],
                ["first", "first", "second", "second"],
            )
            self.assertTrue(all(item[0] == "POST" for item in handler.records))
            self.assertTrue(all(item[2]["variables"] == {} for item in handler.records))

    def test_application_headers_override_defaults(self) -> None:
        with endpoint() as (url, handler):
            load_from_url(
                SchemaOrigin(
                    url=url,
                    headers=(
                        ("aCcEpT", "application/json; custom=1"),
                        ("Content-Type", "application/json; charset=utf-8"),
                    ),
                ),
                environment={},
            )
            self.assertEqual(len(handler.records), 2)
            for _method, headers, body in handler.records:
                normalized = {name.lower(): value for name, value in headers.items()}
                self.assertEqual(normalized["accept"], "application/json; custom=1")
                self.assertEqual(
                    normalized["content-type"], "application/json; charset=utf-8"
                )
                self.assertIn("query", body)

    def test_legacy_and_independent_argument_support(self) -> None:
        for mode in ("legacy", "mixed", "mixed-field", "support-error", "support-400"):
            with self.subTest(mode=mode), endpoint(mode) as (url, handler):
                schema = load_from_url(SchemaOrigin(url=url), environment={})
                self.assertIn("Query", schema.type_map)
                self.assertEqual(
                    len(handler.records), 3 if mode.startswith("support-") else 2
                )
                if mode in {"mixed", "mixed-field"}:
                    query = str(handler.records[-1][2]["query"])
                    self.assertIn("args(includeDeprecated: true)", query)
                    self.assertIn("args {", query)

    def test_redirects_strip_all_foreign_headers_and_preserve_post(self) -> None:
        with endpoint() as (foreign, target), endpoint() as (url, source):
            source.target = foreign
            load_from_url(
                SchemaOrigin(
                    url=url + "/redirect",
                    headers=(
                        ("Authorization", "secret"),
                        ("X-Api-Key", "secret"),
                        ("Cookie", "secret"),
                    ),
                ),
                environment={},
            )
            self.assertEqual(len(target.records), 2)
            for method, headers, _body in target.records:
                self.assertEqual(method, "POST")
                self.assertNotIn("Authorization", headers)
                self.assertNotIn("X-Api-Key", headers)
                self.assertNotIn("Cookie", headers)
            source.target = url + "/schema"
            load_from_url(
                SchemaOrigin(url=url + "/redirect", headers=(("X-Key", "secret"),)),
                environment={},
            )
            self.assertEqual(source.records[-1][1]["X-Key"], "secret")

    def test_get_redirect_semantics(self) -> None:
        with endpoint() as (url, handler):
            with self.assertRaises(ValueError):
                load_from_url(SchemaOrigin(url=url + "/get"), environment={})
            self.assertEqual(handler.records[1][0], "GET")
            self.assertEqual(handler.records[1][2], {})

    def test_failures_are_bounded_and_redacted(self) -> None:
        for mode in (
            "auth",
            "forbidden",
            "http-error",
            "graphql-auth",
            "graphql-forbidden",
            "malformed",
            "utf16",
            "utf32",
            "modern",
            "slow",
            "conflict",
            "duplicate-length",
            "missing-data",
            "invalid-errors",
            "query-error",
            "partial",
            "incomplete",
        ):
            with self.subTest(mode=mode), endpoint(mode) as (url, handler):
                token = "private" + "-credential"
                try:
                    origin = SchemaOrigin(
                        url=url + "/" + token, headers=(("X-Key", token),)
                    )
                    load_from_url(
                        origin,
                        environment={},
                        timeout=0.02 if mode == "slow" else 10.0,
                        max_response_bytes=20 if mode == "modern" else 1000000,
                    )
                except ValueError:
                    self.assertNotIn(token, traceback.format_exc())
                    self.assertNotIn("secret response", traceback.format_exc())
                else:
                    self.fail("Expected a bounded failure")
                self.assertEqual(
                    len(handler.records),
                    2 if mode in {"query-error", "partial", "incomplete"} else 1,
                )

    def test_chunked_response_and_redirect_failures(self) -> None:
        with endpoint("chunked") as (url, _handler):
            load_from_url(SchemaOrigin(url=url), environment={})
        with endpoint() as (url, handler):
            for target, count in (
                (url + "/redirect", 6),
                ("http://user:secret@localhost", 1),
            ):
                handler.target = target
                handler.records.clear()
                with self.assertRaises(ValueError):
                    load_from_url(SchemaOrigin(url=url + "/redirect"), environment={})
                self.assertEqual(len(handler.records), count)

    def test_rejects_invalid_inputs_without_network(self) -> None:
        for url in (
            "http://user:secret@localhost",
            "ftp://localhost",
            "http://localhost/#secret",
            "http://localhost/\nsecret",
        ):
            with (
                self.subTest(url=url),
                patch("graphql_desk.schema_url.build_opener") as opener,
            ):
                with self.assertRaises(ValueError):
                    load_from_url(SchemaOrigin(url=url), environment={})
                opener.assert_not_called()
        for name in ("Content-Length", "Host", "Transfer-Encoding", "Connection"):
            with self.subTest(header=name), self.assertRaises(ValueError):
                load_from_url(
                    SchemaOrigin(url="http://localhost", headers=((name, "secret"),)),
                    environment={},
                )
        with patch.dict("os.environ", {"HTTP_PROXY": "http://invalid:1"}):
            with endpoint() as (url, _handler):
                load_from_url(SchemaOrigin(url=url), environment={})

    def test_header_resolution_and_limits_fail_before_network(self) -> None:
        with patch("graphql_desk.schema_url.build_opener") as opener:
            for headers in (
                (("X-Key", "${env:MISSING}"),),
                (("X-Key", "${env:TOKEN}"),),
                (("X-Key", "one"), ("x-key", "two")),
            ):
                with self.assertRaises(ValueError):
                    load_from_url(
                        SchemaOrigin(url="http://localhost", headers=headers),
                        environment={"TOKEN": "invalid\r\nheader"},
                    )
            for timeout in (0.0, -1.0, float("inf"), float("nan"), True):
                with self.assertRaises(ValueError):
                    load_from_url(
                        SchemaOrigin(url="http://localhost"),
                        environment={},
                        timeout=timeout,
                    )
            for limit in (0, -1, True):
                with self.assertRaises(ValueError):
                    load_from_url(
                        SchemaOrigin(url="http://localhost"),
                        environment={},
                        max_response_bytes=limit,
                    )
            opener.assert_not_called()
