"""Explicit, uncached HTTP introspection; callers own trust and cancellation."""

import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from http.client import HTTPException
from typing import cast
from urllib.error import HTTPError
from urllib.parse import urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from graphql import (
    DocumentNode,
    FieldNode,
    FragmentDefinitionNode,
    GraphQLSchema,
    Visitor,
    get_introspection_query,
    parse,
    print_ast,
    visit,
)

from .config import SchemaOrigin, resolve_schema_headers
from .schema_loading import load_from_introspection

_SUPPORT_QUERY = """query IntrospectSupportQuery {
  directive: __type(name: "__Directive") { fields { name args { name } } }
  field: __type(name: "__Field") { fields { name args { name } } }
  type: __type(name: "__Type") { fields { name } }
  inputValue: __type(name: "__InputValue") { fields { name } }
}"""
# Host and transfer framing remain owned by the selected URL and transport.
_FORBIDDEN_HEADERS = {
    "host",
    "content-length",
    "transfer-encoding",
    "connection",
    "trailer",
    "upgrade",
    "proxy-authorization",
    "proxy-connection",
    "accept-encoding",
    "expect",
    "te",
}


@dataclass(frozen=True)
class _Features:
    repeatable: bool = False
    specified_by: bool = False
    one_of: bool = False
    input_deprecation: bool = False
    field_arguments: bool = False
    directive_arguments: bool = False


def _object(value: object) -> Mapping[str, object]:
    return cast(Mapping[str, object], value) if isinstance(value, dict) else {}


def _features(data: Mapping[str, object]) -> _Features:
    def fields(key: str) -> dict[str, Mapping[str, object]]:
        values = _object(data.get(key)).get("fields")
        if not isinstance(values, list):
            return {}
        return {
            str(_object(item).get("name")): _object(item)
            for item in cast(list[object], values)
        }

    def arguments(key: str) -> bool:
        values = fields(key).get("args", {}).get("args")
        return isinstance(values, list) and any(
            _object(item).get("name") == "includeDeprecated"
            for item in cast(list[object], values)
        )

    return _Features(
        "isRepeatable" in fields("directive"),
        "specifiedByURL" in fields("type"),
        "isOneOf" in fields("type"),
        "isDeprecated" in fields("inputValue"),
        arguments("field"),
        arguments("directive"),
    )


def _query(features: _Features) -> str:
    document = parse(
        get_introspection_query(
            specified_by_url=features.specified_by,
            directive_is_repeatable=features.repeatable,
            input_value_deprecation=True,
            one_of=features.one_of,
        )
    )

    class Prune(Visitor):
        def enter_field(
            self,
            node: FieldNode,
            _key: object,
            _parent: object,
            _path: object,
            ancestors: list[object],
        ) -> object:
            if node.name.value in {"isDeprecated", "deprecationReason"}:
                if not features.input_deprecation and any(
                    isinstance(item, FragmentDefinitionNode)
                    and item.name.value == "InputValue"
                    for item in ancestors
                ):
                    return self.REMOVE
            enabled = True
            if node.name.value == "inputFields":
                enabled = features.input_deprecation
            elif node.name.value == "args":
                enabled = (
                    features.directive_arguments
                    if any(
                        isinstance(item, FieldNode) and item.name.value == "directives"
                        for item in ancestors
                    )
                    else features.field_arguments
                )
            if not enabled and node.arguments:
                return FieldNode(
                    alias=node.alias,
                    name=node.name,
                    arguments=(),
                    directives=node.directives,
                    selection_set=node.selection_set,
                )
            return None

    return print_ast(cast(DocumentNode, visit(document, Prune())))


def _url(value: str) -> tuple[str, str, int]:
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
            or "#" in value
            or "\\" in value
            or any(ord(char) <= 32 or ord(char) == 127 for char in value)
            or parsed.port == 0
        ):
            raise ValueError
        return (
            parsed.scheme,
            parsed.hostname.lower(),
            parsed.port or (443 if parsed.scheme == "https" else 80),
        )
    except (ValueError, UnicodeError):
        raise ValueError("Invalid schema URL.") from None


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(
        self,
        req: Request,
        fp: object,
        code: int,
        msg: str,
        headers: object,
        newurl: str,
    ) -> None:
        return None


def load_from_url(
    origin: SchemaOrigin,
    *,
    environment: Mapping[str, str],
    timeout: float = 10.0,
    max_response_bytes: int = 16 * 1024 * 1024,
) -> GraphQLSchema:
    """Fetch one schema, resolving only explicitly supplied header variables.

    The timeout bounds socket operations, not total elapsed time or DNS lookup.
    No process proxies, credentials, schema cache or project code are consulted.
    """
    if (
        isinstance(timeout, bool)
        or not math.isfinite(timeout)
        or timeout <= 0
        or type(max_response_bytes) is not int
        or max_response_bytes <= 0
    ):
        raise ValueError("Invalid schema transport limits.")
    if origin.url is None or origin.local_path is not None:
        raise ValueError("Schema URL origin is required.")
    initial = _url(origin.url)
    try:
        resolved = resolve_schema_headers(origin, environment=environment)
        headers: dict[str, str] = {}
        for name, value in resolved.headers:
            lower = name.lower()
            if lower in _FORBIDDEN_HEADERS or lower in headers:
                raise ValueError
            if any(ord(char) < 32 or ord(char) == 127 for char in value):
                raise ValueError
            value.encode("latin-1")
            headers[lower] = value
    except (ValueError, UnicodeError):
        raise ValueError("Invalid schema request headers.") from None
    opener = build_opener(ProxyHandler({}), _NoRedirect())
    opener.addheaders = []

    def request(query: str, operation: str) -> Mapping[str, object]:
        url = origin.url
        assert url is not None
        configured = dict(headers)
        body: bytes | None = json.dumps(
            {
                "query": query,
                "operationName": operation,
                "variables": {},
            }
        ).encode("utf-8")
        for redirects in range(6):
            fixed = {
                "accept": "application/graphql-response+json, application/json",
                "accept-encoding": "identity",
            }
            if body is not None:
                fixed["content-type"] = "application/json"
            req = Request(url, data=body, headers={**fixed, **configured})
            try:
                response = opener.open(req, timeout=timeout)
            except HTTPError as error:
                response = error
            except (OSError, ValueError, HTTPException):
                raise ValueError("Schema transport failed.") from None
            try:
                with response:
                    code = response.status
                    if code in {301, 302, 303, 307, 308}:
                        location = response.headers.get("Location")
                        if not location or redirects == 5:
                            raise ValueError("Schema redirect failed.")
                        target = urljoin(url, location)
                        current_origin, next_origin = _url(url), _url(target)
                        if current_origin[0] == "https" and next_origin[0] != "https":
                            raise ValueError("Schema redirect failed.")
                        if next_origin != initial:
                            configured = {}
                        if code in {301, 302, 303}:
                            body = None
                        url = target
                        continue
                    if code is None or (
                        not 200 <= code < 300 and code not in {400, 422}
                    ):
                        raise ValueError("Schema HTTP request failed.")
                    lengths = response.headers.get_all("Content-Length", [])
                    encoding = response.headers.get("Content-Encoding", "identity")
                    if (
                        len(lengths) > 1
                        or len(response.headers.get_all("Transfer-Encoding", [])) > 1
                        or len(response.headers.get_all("Content-Encoding", [])) > 1
                        or (
                            response.headers.get("Transfer-Encoding") is not None
                            and (
                                bool(lengths)
                                or response.headers.get("Transfer-Encoding", "").lower()
                                != "chunked"
                            )
                        )
                        or encoding.lower() != "identity"
                    ):
                        raise ValueError("Invalid schema response framing.")
                    if lengths and (
                        len(lengths[0]) > 20
                        or not re.fullmatch(r"[0-9]+", lengths[0])
                        or int(lengths[0]) > max_response_bytes
                    ):
                        raise ValueError("Schema response exceeds limits.")
                    raw = response.read(max_response_bytes + 1)
                    if len(raw) > max_response_bytes:
                        raise ValueError("Schema response exceeds limits.")
                    if lengths and len(raw) != int(lengths[0]):
                        raise ValueError("Invalid schema response framing.")
            except (OSError, HTTPException):
                raise ValueError("Schema response read failed.") from None
            try:
                result: object = json.loads(raw.decode("utf-8-sig"))
                if not isinstance(result, dict):
                    raise ValueError
                payload = cast(Mapping[str, object], result)
                errors = payload.get("errors", [])
                if not isinstance(errors, list) or any(
                    not isinstance(_object(item).get("message"), str)
                    for item in cast(list[object], errors)
                ):
                    raise ValueError
                if not errors and (
                    code in {400, 422} or not isinstance(payload.get("data"), dict)
                ):
                    raise ValueError
            except (ValueError, UnicodeError, RecursionError):
                raise ValueError("Invalid schema response JSON.") from None
            if any(
                _object(_object(item).get("extensions")).get("code")
                in ("UNAUTHENTICATED", "FORBIDDEN")
                for item in cast(list[object], errors)
            ):
                raise ValueError("Schema authentication failed.") from None
            return payload
        raise ValueError("Schema redirect failed.")

    support = request(_SUPPORT_QUERY, "IntrospectSupportQuery")
    if support.get("errors"):
        baseline = request(_query(_Features()), "IntrospectionQuery")
        load_from_introspection(baseline)
        schema_data = _object(_object(baseline.get("data")).get("__schema"))
        types = schema_data.get("types")
        names = {
            "__Directive": "directive",
            "__Field": "field",
            "__Type": "type",
            "__InputValue": "inputValue",
        }
        inferred = {
            names[str(_object(item).get("name"))]: item
            for item in cast(list[object], types or [])
            if str(_object(item).get("name")) in names
        }
        features = _features(inferred)
        if features == _Features():
            return load_from_introspection(baseline)
    else:
        features = _features(_object(support.get("data")))
    return load_from_introspection(request(_query(features), "IntrospectionQuery"))
