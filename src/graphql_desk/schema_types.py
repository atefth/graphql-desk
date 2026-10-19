"""Render schema declarations without executing project code."""

import keyword
import unicodedata

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

from .config import ScalarMapping

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


def _symbol(value: str, label: str) -> str:
    parts = value.split(".")
    if len(parts) < 2 or any(
        not part.isidentifier()
        or keyword.iskeyword(part)
        or unicodedata.normalize("NFKC", part) != part
        for part in parts
    ):
        raise ValueError(f"{label} must be an import-qualified type")
    return value


def mapped_type_declarations(
    schema: GraphQLSchema,
    scalars: tuple[ScalarMapping, ...],
    enums: tuple[tuple[str, str], ...],
) -> tuple[dict[str, str], dict[str, str], list[str]]:
    inputs: dict[str, str] = {}
    outputs: dict[str, str] = {}
    aliases: dict[str, str] = {}
    seen: set[str] = set()
    for mapping in scalars:
        if mapping.name in seen:
            raise ValueError(f"Duplicate scalar mapping: {mapping.name!r}")
        seen.add(mapping.name)
        if not isinstance(schema.get_type(mapping.name), GraphQLScalarType):
            raise ValueError(f"Unknown or non-scalar mapping: {mapping.name!r}")
        if mapping.name in _BUILTINS:
            expected = f"builtins.{_BUILTINS[mapping.name]}"
            if mapping.input != expected or mapping.output != expected:
                raise ValueError(
                    "Built-in scalar mappings must retain their fixed types"
                )
            continue
        for suffix, symbol, names in (
            ("Input", mapping.input, inputs),
            ("Output", mapping.output, outputs),
        ):
            alias = mapping.name + suffix
            if alias in schema.type_map or alias in aliases:
                raise ValueError(f"Colliding mapped declaration name: {alias!r}")
            aliases[alias] = _symbol(symbol, "scalar mapping")
            names[mapping.name] = alias
    seen.clear()
    for name, symbol in enums:
        if name in seen:
            raise ValueError(f"Duplicate enum mapping: {name!r}")
        seen.add(name)
        if not isinstance(schema.get_type(name), GraphQLEnumType):
            raise ValueError(f"Unknown or non-enum mapping: {name!r}")
        aliases[name] = _symbol(symbol, "enum mapping")
        inputs[name] = outputs[name] = name
    imports = {
        symbol: f"__graphql_desk_type_{index}"
        for index, symbol in enumerate(sorted(set(aliases.values())))
    }
    lines: list[str] = []
    for symbol, imported in imports.items():
        module, _, name = symbol.rpartition(".")
        lines.append(f"from {module} import {name} as {imported}")
    for alias, symbol in sorted(aliases.items()):
        if symbol == "typing.Any":
            lines.append(f"# Explicit Any mapping: {alias}")
        lines.append(f"{alias} = {imports[symbol]}")
    return inputs, outputs, lines


def _input_type(
    type_: TypeNode, names: dict[str, str], *, nullable: bool = True
) -> str:
    if isinstance(type_, NonNullTypeNode):
        return _input_type(type_.type, names, nullable=False)
    if isinstance(type_, ListTypeNode):
        result = f"list[{_input_type(type_.type, names)}]"
    elif isinstance(type_, NamedTypeNode):
        result = names.get(type_.name.value, type_.name.value)
    else:
        raise TypeError(f"Unsupported input type node: {type(type_).__name__}")
    return f"{result} | None" if nullable else result


def render_schema_types(
    schema: GraphQLSchema,
    *,
    scalars: tuple[ScalarMapping, ...] = (),
    enums: tuple[tuple[str, str], ...] = (),
) -> str:
    """Return importable Python 3.10 declarations for built-in schema inputs.

    Unmapped custom scalars are rejected. Rendering never imports
    configured symbols; importing the generated module does. Non-null input fields
    with defaults retain upstream's nullable
    static compatibility behavior; GraphQL execution may still reject null.
    """
    assert_valid_schema(schema)
    inputs, _, mappings = mapped_type_declarations(schema, scalars, enums)
    declarations: list[str] = []
    for name, type_ in sorted(schema.type_map.items()):
        if name.startswith("__"):
            continue
        if name in _BUILTINS:
            if type_ is not specified_scalar_types[name]:
                raise ValueError(f"Colliding built-in scalar name: {name!r}")
            continue
        if isinstance(type_, GraphQLScalarType):
            if name not in inputs:
                raise ValueError(f"Custom scalar {name!r} requires an explicit mapping")
            continue
        if not isinstance(type_, (GraphQLEnumType, GraphQLInputObjectType)):
            continue
        if not name.isidentifier() or keyword.iskeyword(name) or name in _RESERVED:
            raise ValueError(f"Unsafe or colliding Python declaration name: {name!r}")
        if isinstance(type_, GraphQLEnumType):
            if name in inputs:
                continue
            values = ", ".join(repr(value) for value in sorted(type_.values))
            declarations.append(f"{name} = Literal[{values}]")
            continue
        if type_.is_one_of:
            variants: list[str] = []
            for index, chosen in enumerate(sorted(type_.fields)):
                variant = f"__graphql_desk_oneof_{name}_{index}"
                variants.append(variant)
                annotation = _input_type(
                    parse_type(str(type_.fields[chosen].type)), inputs, nullable=False
                )
                declarations.append(
                    f"{variant} = TypedDict({variant!r}, {{\n"
                    f"    {chosen!r}: {annotation!r},\n}}, closed=True)"
                )
            declarations.append(f"{name} = " + " | ".join(variants))
            continue
        fields: list[str] = []
        for field_name, field in sorted(type_.fields.items()):
            has_default = (
                field.default is not None or field.default_value is not Undefined
            )
            field_type = parse_type(str(field.type))
            annotation = _input_type(field_type, inputs)
            if has_default and isinstance(field_type, NonNullTypeNode):
                annotation = _input_type(field_type.type, inputs)
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
        "from typing import Literal as Literal",
        "from typing_extensions import NotRequired as NotRequired, "
        "TypedDict as TypedDict",
        "",
        *[f"{name} = {value}" for name, value in sorted(_BUILTINS.items())],
        *mappings,
    ]
    return "\n".join(header) + "\n\n" + "\n\n".join(declarations) + "\n"
