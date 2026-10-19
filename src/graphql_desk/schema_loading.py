"""Load an explicitly supplied local schema without project imports or networking."""

import json
from collections.abc import Mapping
from pathlib import Path
from typing import TypeGuard, cast
from urllib.parse import urlsplit

from graphql import (
    GraphQLError,
    GraphQLSchema,
    IntrospectionQuery,
    assert_valid_schema,
    build_client_schema,
    build_schema,
)


def _is_object(value: object) -> TypeGuard[Mapping[str, object]]:
    return isinstance(value, Mapping)


def load_from_sdl(source: str) -> GraphQLSchema:
    """Build and validate SDL; parsing alone does not establish schema validity."""
    try:
        schema = build_schema(source)
        assert_valid_schema(schema)
    except (GraphQLError, TypeError, ValueError, RecursionError) as error:
        raise ValueError(f"Invalid SDL schema: {error}") from error
    return schema


def load_from_introspection(data: Mapping[str, object]) -> GraphQLSchema:
    """Accept an introspection object or an error-free GraphQL data envelope."""
    if not _is_object(data):
        raise ValueError("Introspection must be a JSON object.")
    if "errors" in data and data["errors"] != []:
        raise ValueError(
            "Introspection response contains errors or invalid error metadata."
        )
    if "data" in data and "__schema" in data:
        raise ValueError("Introspection contains ambiguous data and __schema roots.")
    payload = data.get("data", data)
    if not _is_object(payload) or not isinstance(payload.get("__schema"), dict):
        raise ValueError("Introspection requires a complete __schema object.")
    try:
        schema = build_client_schema(cast(IntrospectionQuery, dict(payload)))
        assert_valid_schema(schema)
    except (
        GraphQLError,
        TypeError,
        ValueError,
        KeyError,
        AttributeError,
        RecursionError,
    ):
        # Core errors can include a repr of the entire response; do not echo it.
        raise ValueError("Invalid or incomplete introspection schema.") from None
    return schema


def load_local_schema(path: str | Path) -> GraphQLSchema:
    """Read UTF-8 SDL/JSON from a caller-selected path, without caching or fetching."""
    text_path = str(path)
    scheme = urlsplit(text_path).scheme
    if "://" in text_path or (scheme and len(scheme) != 1):
        raise ValueError(
            "Remote schema URLs require the separate authenticated loader."
        )
    selected = Path(path)
    suffix = selected.suffix.lower()
    if suffix not in {".graphql", ".gql", ".sdl", ".json"}:
        raise ValueError("Local schema format must be .graphql, .gql, .sdl or .json.")
    try:
        source = selected.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError) as error:
        raise ValueError(f"Cannot read UTF-8 schema file {selected.name!r}.") from error
    if suffix != ".json":
        return load_from_sdl(source)
    try:
        data: object = json.loads(source)
    except (ValueError, RecursionError) as error:
        raise ValueError(f"Invalid introspection JSON in {selected.name!r}.") from error
    if not _is_object(data):
        raise ValueError("Introspection JSON must contain an object.")
    return load_from_introspection(data)
