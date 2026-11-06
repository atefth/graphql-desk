"""Pure Python equivalents of the advanced introspection formatting utilities."""

import json
import re
from collections.abc import Mapping
from pprint import pformat
from typing import TypedDict, cast

from typing_extensions import NotRequired

from .config import ScalarMapping
from .schema_loading import load_from_introspection
from .schema_types import render_schema_types

IntrospectionResult = TypedDict(
    "IntrospectionResult",
    {"__schema": dict[str, object], "name": NotRequired[str | None]},
)


class SchemaMap(TypedDict):
    name: str | None
    query: str
    mutation: str | None
    subscription: str | None
    types: dict[str, dict[str, object]]


_ANNOTATION = "# Generated schema introspection by GraphQL Desk.\n"


def _object(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError("Invalid introspection object")
    return cast(Mapping[str, object], value)


def _items(value: object) -> list[Mapping[str, object]]:
    if not isinstance(value, list):
        raise ValueError("Invalid introspection collection")
    return [_object(item) for item in cast(list[object], value)]


def _name(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[_A-Za-z][_0-9A-Za-z]*", value):
        raise ValueError("Invalid introspection name")
    return value


def _ref(value: object) -> dict[str, object]:
    item = _object(value)
    kind = item.get("kind")
    if kind in ("NON_NULL", "LIST"):
        return {"kind": kind, "ofType": _ref(item["ofType"])}
    result: dict[str, object] = {"name": _name(item["name"])}
    if kind is not None:
        result["kind"] = kind
    return result


def _input(item: Mapping[str, object]) -> dict[str, object]:
    result: dict[str, object] = {
        "name": _name(item["name"]),
        "type": _ref(item["type"]),
    }
    if item.get("defaultValue"):
        result["defaultValue"] = item["defaultValue"]
    return result


def _sorted(items: list[dict[str, object]]) -> list[dict[str, object]]:
    return sorted(items, key=lambda item: _name(item["name"]))


def _type(item: Mapping[str, object]) -> dict[str, object]:
    kind = item["kind"]
    result: dict[str, object] = {"kind": kind, "name": _name(item["name"])}
    if kind == "ENUM":
        result["enumValues"] = [
            {
                "name": _name(value["name"]),
                "isDeprecated": bool(value.get("isDeprecated")),
            }
            for value in _items(item["enumValues"])
        ]
    elif kind == "INPUT_OBJECT":
        result["inputFields"] = [_input(value) for value in _items(item["inputFields"])]
        result["isOneOf"] = bool(item.get("isOneOf"))
    elif kind in ("OBJECT", "INTERFACE"):
        result["fields"] = _sorted(
            [
                {
                    "name": _name(value["name"]),
                    "type": _ref(value["type"]),
                    "args": _sorted(
                        [_input(arg) for arg in _items(value.get("args") or [])]
                    ),
                    "isDeprecated": bool(value.get("isDeprecated")),
                }
                for value in _items(item.get("fields") or [])
            ]
        )
        result["interfaces"] = _sorted(
            [_ref(value) for value in _items(item.get("interfaces") or [])]
        )
    if kind in ("INTERFACE", "UNION"):
        result["possibleTypes"] = _sorted(
            [_ref(value) for value in _items(item.get("possibleTypes") or [])]
        )
    return result


def minify_introspection(source: Mapping[str, object]) -> IntrospectionResult:
    """Keep the pinned typing-relevant data without mutating the supplied query."""
    load_from_introspection(source)
    try:
        payload = _object(source.get("data", source))
        schema = _object(payload["__schema"])
        body: dict[str, object] = {
            "queryType": _ref(schema["queryType"]),
            "mutationType": _ref(schema["mutationType"])
            if schema.get("mutationType")
            else None,
            "subscriptionType": _ref(schema["subscriptionType"])
            if schema.get("subscriptionType")
            else None,
            "types": _sorted(
                [
                    _type(item)
                    for item in _items(schema["types"])
                    if not _name(item["name"]).startswith("__")
                    and item["kind"]
                    in (
                        "SCALAR",
                        "ENUM",
                        "INPUT_OBJECT",
                        "OBJECT",
                        "INTERFACE",
                        "UNION",
                    )
                ]
            ),
            "directives": [],
        }
        result: IntrospectionResult = {"__schema": body}
        if "name" in payload:
            name = payload["name"]
            result["name"] = _name(name) if name is not None else None
        return result
    except (KeyError, TypeError, ValueError, RecursionError):
        raise ValueError("Invalid introspection metadata") from None


def preprocess_introspection(source: Mapping[str, object]) -> SchemaMap:
    """Return a Python schema lookup record, rather than TypeScript source text."""
    compact = minify_introspection(source)
    schema = compact["__schema"]
    types: dict[str, dict[str, object]] = {}
    for item in _items(schema["types"]):
        entry = dict(item)
        if item["kind"] == "UNION":
            entry["fields"] = {}
        if "fields" in item:
            entry["fields"] = {
                _name(value["name"]): {"name": value["name"], "type": value["type"]}
                for value in _items(item["fields"])
            }
        if "possibleTypes" in item:
            entry["possibleTypes"] = tuple(
                _name(value["name"]) for value in _items(item["possibleTypes"])
            )
        if "enumValues" in item:
            entry["enumValues"] = tuple(
                _name(value["name"]) for value in _items(item["enumValues"])
            )
        types[_name(item["name"])] = entry
    return {
        "name": compact.get("name"),
        "query": _name(_object(schema["queryType"])["name"]),
        "mutation": _name(_object(schema["mutationType"])["name"])
        if schema["mutationType"]
        else None,
        "subscription": _name(_object(schema["subscriptionType"])["name"])
        if schema["subscriptionType"]
        else None,
        "types": types,
    }


def extract_introspection_header(contents: str) -> str | None:
    """Extract leading Python comments; a generated annotation is not custom."""
    lines: list[str] = []
    for line in contents.splitlines():
        if not line.lstrip().startswith("#"):
            break
        lines.append(line)
    result = "\n".join(lines) + "\n"
    return result if lines and _ANNOTATION.strip() not in result else None


def output_introspection_file(
    source: Mapping[str, object],
    *,
    file_type: str = ".pyi",
    should_preprocess: bool = True,
    preamble: str | None = None,
    scalars: tuple[ScalarMapping, ...] = (),
    enums: tuple[tuple[str, str], ...] = (),
) -> str:
    """Format matching Python runtime/stub exports; publication remains caller-owned."""
    if file_type not in (".py", ".pyi"):
        raise ValueError("Introspection output format must be .py or .pyi")
    if preamble is not None and (
        "\0" in preamble
        or any(
            line.strip() and not line.lstrip().startswith("#")
            for line in preamble.splitlines()
        )
        or re.search(r"coding\s*[:=]", preamble)
    ):
        raise ValueError(
            "Custom introspection header must contain only Python comments"
        )
    schema = load_from_introspection(source)
    declarations = render_schema_types(schema, scalars=scalars, enums=enums)
    header = preamble or _ANNOTATION
    if not header.endswith("\n"):
        header += "\n"
    imports = "from graphql_desk.internal import IntrospectionResult, SchemaMap\n"
    if file_type == ".pyi":
        values = "introspection: IntrospectionResult\n"
        if should_preprocess:
            values += "schema_map: SchemaMap\n"
    else:
        try:
            # JSON validation prevents arbitrary repr methods becoming generated code.
            payload = json.loads(
                json.dumps(source.get("data", source), allow_nan=False)
            )
        except (ValueError, TypeError, RecursionError):
            raise ValueError(
                "Introspection output requires JSON-compatible data"
            ) from None
        values = (
            "introspection: IntrospectionResult = "
            + pformat(payload, sort_dicts=True)
            + "\n"
        )
        if should_preprocess:
            values += (
                "schema_map: SchemaMap = "
                + pformat(preprocess_introspection(source), sort_dicts=True)
                + "\n"
            )
    return header + imports + declarations + "\n" + values
