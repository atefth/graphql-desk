"""Render a schema-bound scalar method without importing configured symbols."""

from graphql import (
    GraphQLEnumType,
    GraphQLInputObjectType,
    GraphQLScalarType,
    GraphQLSchema,
    specified_scalar_types,
)

from .config import ScalarMapping
from .schema_types import mapped_type_declarations, render_schema_types


def render_scalar_method(
    schema: GraphQLSchema,
    *,
    scalars: tuple[ScalarMapping, ...] = (),
    enums: tuple[tuple[str, str], ...] = (),
) -> str:
    """Return indented overloads for a generated factory Protocol's scalar method.

    The enclosing module supplies render_schema_types aliases once, plus typing
    Literal/overload imported as __graphql_desk_Literal/__graphql_desk_overload.
    Known names are specified scalars and this schema's enum/input/custom scalars.
    None is preserved for each known name; there is no unknown-name/value fallback.
    """
    # Reuse the shared renderer's mapping, input and declaration safety policy.
    render_schema_types(schema, scalars=scalars, enums=enums)
    inputs, _, _ = mapped_type_declarations(schema, scalars, enums)
    methods: list[str] = []
    for name, type_ in sorted({**specified_scalar_types, **schema.type_map}.items()):
        if name.startswith("__") or not isinstance(
            type_, (GraphQLScalarType, GraphQLEnumType, GraphQLInputObjectType)
        ):
            continue
        value = inputs.get(name, name)
        for annotation in ("None", value):
            methods.append(
                "    @__graphql_desk_overload\n"
                "    def scalar(self, "
                f"name: __graphql_desk_Literal[{name!r}], value: {annotation}"
                f") -> {annotation}: ...\n"
            )
    return "\n".join(methods)
