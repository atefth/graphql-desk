"""Render schema declarations without executing project code."""

import keyword

from graphql import (
    GraphQLEnumType,
    GraphQLInputObjectType,
    GraphQLScalarType,
    GraphQLSchema,
    ListTypeNode,
    NamedTypeNode,
    NonNullTypeNode,
    TypeNode,
    Undefined,
    assert_valid_schema,
    parse_type,
    specified_scalar_types,
)

_BUILTINS = {
    "Boolean": "bool",
    "Float": "float",
    "ID": "str",
    "Int": "int",
    "String": "str",
}
_RESERVED = {
    "Literal",
    "TypedDict",
    "NotRequired",
    "bool",
    "float",
    "int",
    "str",
    "list",
}


def _input_type(type_: TypeNode, *, nullable: bool = True) -> str:
    if isinstance(type_, NonNullTypeNode):
        return _input_type(type_.type, nullable=False)
    if isinstance(type_, ListTypeNode):
        result = f"list[{_input_type(type_.type)}]"
    elif isinstance(type_, NamedTypeNode):
        result = type_.name.value
    else:
        raise TypeError(f"Unsupported input type node: {type(type_).__name__}")
    return f"{result} | None" if nullable else result


def render_schema_types(schema: GraphQLSchema) -> str:
    """Return importable Python 3.10 declarations for built-in schema inputs.

    Custom scalars and oneOf inputs require dedicated mappings/validation and are
    rejected. Non-null input fields with defaults retain upstream's nullable
    static compatibility behavior; GraphQL execution may still reject null.
    """
    assert_valid_schema(schema)
    declarations: list[str] = []
    for name, type_ in sorted(schema.type_map.items()):
        if name.startswith("__"):
            continue
        if name in _BUILTINS:
            if type_ is not specified_scalar_types[name]:
                raise ValueError(f"Colliding built-in scalar name: {name!r}")
            continue
        if isinstance(type_, GraphQLScalarType):
            raise ValueError(f"Custom scalar {name!r} requires an explicit mapping")
        if not isinstance(type_, (GraphQLEnumType, GraphQLInputObjectType)):
            continue
        if not name.isidentifier() or keyword.iskeyword(name) or name in _RESERVED:
            raise ValueError(f"Unsafe or colliding Python declaration name: {name!r}")
        if isinstance(type_, GraphQLEnumType):
            values = ", ".join(repr(value) for value in sorted(type_.values))
            declarations.append(f"{name} = Literal[{values}]")
            continue
        if type_.is_one_of:
            raise ValueError(f"oneOf input {name!r} is not supported yet")
        fields: list[str] = []
        for field_name, field in sorted(type_.fields.items()):
            has_default = (
                field.default is not None or field.default_value is not Undefined
            )
            field_type = parse_type(str(field.type))
            annotation = _input_type(field_type)
            if has_default and isinstance(field_type, NonNullTypeNode):
                annotation = _input_type(field_type.type)
            if has_default or not isinstance(field_type, NonNullTypeNode):
                rendered = f"NotRequired[{annotation!r}]"
            else:
                rendered = repr(annotation)
            fields.append(f"    {field_name!r}: {rendered},")
        declarations.append(
            f"{name} = TypedDict({name!r}, {{\n" + "\n".join(fields) + "\n})"
        )
    header = [
        '"""Generated schema types; do not edit."""',
        "from typing import Literal",
        "from typing_extensions import NotRequired, TypedDict",
        "",
        *[f"{name} = {value}" for name, value in sorted(_BUILTINS.items())],
    ]
    return "\n".join(header) + "\n\n" + "\n\n".join(declarations) + "\n"
