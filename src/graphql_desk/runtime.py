"""Untyped runtime construction; generated factories supply precise inference."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from graphql import (
    DocumentNode,
    FragmentDefinitionNode,
    FragmentSpreadNode,
    Node,
    OperationDefinitionNode,
    Visitor,
    parse,
    print_ast,
    visit,
)

if TYPE_CHECKING:
    from . import TypedDocument


class _Spreads(Visitor):
    def __init__(self) -> None:
        super().__init__()
        self.names: set[str] = set()

    def enter_fragment_spread(self, node: FragmentSpreadNode, *_args: object) -> None:
        self.names.add(node.name.value)


def _spreads(node: Node) -> set[str]:
    visitor = _Spreads()
    visit(node, visitor)
    return visitor.names


def _construct(
    source: str,
    fragments: Iterable[TypedDocument[Any, Any]],
    *,
    operation: str | None,
    schema_name: str | None,
) -> TypedDocument[Any, Any]:
    from . import TypedDocument

    parsed = parse(source)
    definitions = list(parsed.definitions)
    operations: list[OperationDefinitionNode] = []
    by_name: dict[str, FragmentDefinitionNode] = {}
    operation_names: set[str] = set()
    for definition in definitions:
        if isinstance(definition, OperationDefinitionNode):
            if definition.name is not None:
                name = definition.name.value
                if name in operation_names:
                    raise ValueError(f"Duplicate operation name: {name!r}")
                operation_names.add(name)
            operations.append(definition)
        elif isinstance(definition, FragmentDefinitionNode):
            name = definition.name.value
            if name in by_name:
                raise ValueError(f"Duplicate fragment name: {name!r}")
            by_name[name] = definition
        else:
            raise ValueError("Only operation and fragment definitions are supported")
    known_schemas: set[str] = {schema_name} if schema_name is not None else set()
    for fragment in fragments:
        if fragment.schema_name is not None:
            known_schemas.add(fragment.schema_name)
        if len(known_schemas) > 1:
            raise ValueError("Fragments belong to different schema bindings")
        for definition in fragment.ast.definitions:
            if not isinstance(definition, FragmentDefinitionNode):
                raise ValueError(
                    "Supplied fragment documents cannot contain operations"
                )
            name = definition.name.value
            previous = by_name.get(name)
            if previous is not None:
                if print_ast(previous) != print_ast(definition):
                    raise ValueError(f"Conflicting fragment definitions: {name!r}")
            else:
                by_name[name] = definition
                definitions.append(definition)
    ast = DocumentNode(definitions=tuple(definitions), loc=parsed.loc)
    unknown = _spreads(ast) - by_name.keys()
    if unknown:
        raise ValueError(f"Unknown fragment spreads: {', '.join(sorted(unknown))}")
    edges = {name: _spreads(fragment) for name, fragment in by_name.items()}
    visited: set[str] = set()
    active: set[str] = set()

    def check_cycles(name: str) -> None:
        if name in active:
            raise ValueError(f"Fragment cycle includes {name!r}")
        if name in visited:
            return
        active.add(name)
        for dependency in sorted(edges[name]):
            check_cycles(dependency)
        active.remove(name)
        visited.add(name)

    for name in by_name:
        check_cycles(name)
    if operations:
        if len(operations) > 1 and any(item.name is None for item in operations):
            raise ValueError("An anonymous operation must be the only operation")
        if operation is None:
            if len(operations) != 1:
                raise ValueError("Multiple operations require an explicit selection")
            selected = operations[0]
        else:
            matches = [
                item
                for item in operations
                if item.name and item.name.value == operation
            ]
            if not matches:
                raise ValueError(f"Unknown selected operation: {operation!r}")
            selected = matches[0]
        operation_name = selected.name.value if selected.name else None
    else:
        if operation is not None:
            raise ValueError("Fragment-only documents cannot select an operation")
        if not isinstance(parsed.definitions[0], FragmentDefinitionNode):
            raise ValueError("Fragment-only documents require a primary fragment")
        operation_name = None
    return TypedDocument(
        ast, operation_name, schema_name=next(iter(known_schemas), None)
    )


def graphql(
    source: str,
    fragments: Iterable[TypedDocument[Any, Any]] = (),
    *,
    operation: str | None = None,
) -> TypedDocument[Any, Any]:
    """Build an untyped document; no schema validation or result coercion occurs."""
    return _construct(source, fragments, operation=operation, schema_name=None)


gql = graphql


@dataclass(frozen=True)
class GraphQLFactory:
    """A schema-name binding; it does not resolve configuration or load a schema."""

    schema_name: str

    def __post_init__(self) -> None:
        if re.fullmatch(r"[_A-Za-z][_0-9A-Za-z]*", self.schema_name) is None:
            raise ValueError(f"Invalid schema binding name: {self.schema_name!r}")

    def __call__(
        self,
        source: str,
        fragments: Iterable[TypedDocument[Any, Any]] = (),
        *,
        operation: str | None = None,
    ) -> TypedDocument[Any, Any]:
        return _construct(
            source, fragments, operation=operation, schema_name=self.schema_name
        )


def init_graphql(schema: str) -> GraphQLFactory:
    """Bind a syntactically valid schema name, without reading project config."""
    return GraphQLFactory(schema)
