"""Render the supported operation subset as static dictionary declarations."""

import keyword
import unicodedata

from graphql import (
    FieldNode,
    GraphQLEnumType,
    GraphQLObjectType,
    GraphQLScalarType,
    GraphQLSchema,
    ListTypeNode,
    NamedTypeNode,
    Node,
    NonNullTypeNode,
    OperationDefinitionNode,
    TypeNode,
    Visitor,
    parse,
    parse_type,
    validate,
    visit,
)

from .schema_types import render_schema_types


class _SupportedSyntax(Visitor):
    def enter(self, node: Node, *_args: object) -> None:
        if node.kind in {
            "directive",
            "fragment_definition",
            "fragment_spread",
            "inline_fragment",
        }:
            raise ValueError(f"Unsupported operation syntax: {node.kind}")


def _output_type(type_: TypeNode, leaf: str, *, nullable: bool = True) -> str:
    if isinstance(type_, NonNullTypeNode):
        return _output_type(type_.type, leaf, nullable=False)
    if isinstance(type_, ListTypeNode):
        result = f"list[{_output_type(type_.type, leaf)}]"
    elif isinstance(type_, NamedTypeNode):
        result = leaf
    else:
        raise TypeError(f"Unsupported output type node: {type(type_).__name__}")
    return f"{result} | None" if nullable else result


def _named_type(type_: TypeNode) -> str:
    while isinstance(type_, (ListTypeNode, NonNullTypeNode)):
        type_ = type_.type
    assert isinstance(type_, NamedTypeNode)
    return type_.name.value


def render_operation_types(
    schema: GraphQLSchema, source: str, *, name: str, operation: str | None = None
) -> str:
    """Render validated operation declarations, rejecting unsupported semantics.

    No fragments, directives, abstract outputs or custom scalar mappings are
    supported yet. This renders declarations only, not execution or inference.
    """
    if (
        not name.isidentifier()
        or keyword.iskeyword(name)
        or name.startswith("__")
        or unicodedata.normalize("NFKC", name) != name
    ):
        raise ValueError(f"Unsafe Python operation name: {name!r}")
    output = render_schema_types(schema)
    document = parse(source)
    visit(document, _SupportedSyntax())
    errors = validate(schema, document)
    if errors:
        raise ValueError("; ".join(error.message for error in errors))
    operations = [
        definition
        for definition in document.definitions
        if isinstance(definition, OperationDefinitionNode)
    ]
    if operation is None:
        if len(operations) != 1:
            raise ValueError("Select an operation explicitly for multiple operations")
        selected = operations[0]
    else:
        matches = [
            item for item in operations if item.name and item.name.value == operation
        ]
        if len(matches) != 1:
            raise ValueError(f"Unknown selected operation: {operation!r}")
        selected = matches[0]
    reserved = set(schema.type_map)
    declarations: list[str] = []

    def declare(class_name: str, fields: list[tuple[str, str]]) -> None:
        if class_name in reserved:
            raise ValueError(f"Colliding generated declaration name: {class_name!r}")
        reserved.add(class_name)
        body = "\n".join(f"    {key!r}: {annotation}," for key, annotation in fields)
        declarations.append(f"{class_name} = TypedDict({class_name!r}, {{\n{body}\n}})")

    variables: list[tuple[str, str]] = []
    for variable in sorted(
        selected.variable_definitions or (), key=lambda item: item.variable.name.value
    ):
        annotation = _output_type(variable.type, _named_type(variable.type))
        if variable.default_value is not None or not isinstance(
            variable.type, NonNullTypeNode
        ):
            rendered = f"NotRequired[{annotation!r}]"
        else:
            rendered = repr(annotation)
        variables.append((variable.variable.name.value, rendered))
    declare(f"{name}Variables", variables)

    def selection(
        parent: GraphQLObjectType, nodes: list[FieldNode], class_name: str
    ) -> None:
        groups: dict[str, list[FieldNode]] = {}
        for node in nodes:
            key = node.alias.value if node.alias else node.name.value
            groups.setdefault(key, []).append(node)
        fields: list[tuple[str, str]] = []
        for key, group in sorted(groups.items()):
            node = group[0]
            if node.name.value == "__typename":
                fields.append((key, repr(f"Literal[{parent.name!r}]")))
                continue
            if node.name.value.startswith("__"):
                raise ValueError(
                    "Introspection object selections are not supported yet"
                )
            field = parent.fields[node.name.value]
            field_type = parse_type(str(field.type))
            named = schema.get_type(_named_type(field_type))
            assert named is not None
            if isinstance(named, GraphQLObjectType):
                child_name = f"{class_name}_{key}"
                children: list[FieldNode] = []
                for item in group:
                    assert item.selection_set is not None
                    for child in item.selection_set.selections:
                        assert isinstance(child, FieldNode)
                        children.append(child)
                selection(named, children, child_name)
                leaf = child_name
            elif isinstance(named, (GraphQLScalarType, GraphQLEnumType)):
                leaf = named.name
            else:
                raise ValueError(f"Unsupported output type: {named.name}")
            annotation = _output_type(field_type, leaf)
            fields.append((key, repr(annotation)))
        declare(class_name, fields)

    root = schema.get_root_type(selected.operation)
    assert root is not None
    nodes: list[FieldNode] = []
    for node in selected.selection_set.selections:
        assert isinstance(node, FieldNode)
        nodes.append(node)
    selection(root, nodes, f"{name}Result")
    return output + "\n" + "\n\n".join(declarations) + "\n"
