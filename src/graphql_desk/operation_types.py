"""Render the supported operation subset as static dictionary declarations."""

import keyword
import re
import unicodedata

from graphql import (
    DirectiveNode,
    FieldNode,
    GraphQLCompositeType,
    GraphQLEnumType,
    GraphQLInterfaceType,
    GraphQLObjectType,
    GraphQLScalarType,
    GraphQLSchema,
    GraphQLUnionType,
    InlineFragmentNode,
    KnownDirectivesRule,
    ListTypeNode,
    NamedTypeNode,
    Node,
    NonNullTypeNode,
    OperationDefinitionNode,
    SelectionNode,
    TypeNode,
    Visitor,
    parse,
    parse_type,
    specified_rules,
    validate,
    visit,
)

from .config import ScalarMapping
from .schema_types import mapped_type_declarations, render_schema_types

_CONDITIONAL = frozenset({"include", "skip", "defer"})
# GraphQLSP's pinned built-in unknown-directive suppression list.
_CLIENT_DIRECTIVES = frozenset(
    {
        "populate",
        "client",
        "unmask",
        "_unmask",
        "_optional",
        "_relayPagination",
        "_simplePagination",
        "_required",
        "optional",
        "required",
        "arguments",
        "argumentDefinitions",
        "connection",
        "refetchable",
        "relay",
        "inline",
    }
)


def _directives(node: FieldNode | InlineFragmentNode) -> set[str]:
    return {directive.name.value for directive in node.directives or ()}


def _null_override(node: FieldNode) -> bool | None:
    names = _directives(node)
    if names & {"required", "_required"}:
        return False
    if names & {"optional", "_optional"}:
        return True
    return None


class _SupportedSyntax(Visitor):
    def enter(self, node: Node, *_args: object) -> None:
        if node.kind in {
            "fragment_definition",
            "fragment_spread",
        }:
            raise ValueError(f"Unsupported operation syntax: {node.kind}")
        if isinstance(node, InlineFragmentNode) and _directives(node) & _CONDITIONAL:
            raise ValueError("Conditional inline fragments are not supported yet")


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
    schema: GraphQLSchema,
    source: str,
    *,
    name: str,
    operation: str | None = None,
    scalars: tuple[ScalarMapping, ...] = (),
    enums: tuple[tuple[str, str], ...] = (),
    client_directives: tuple[str, ...] = (),
) -> str:
    """Render validated operation declarations, rejecting unsupported semantics.

    Named fragments, conditional inline fragments and conditionally repeated
    composite fields are not supported yet. No execution or inference occurs.
    """
    if (
        not name.isidentifier()
        or keyword.iskeyword(name)
        or name.startswith("__")
        or unicodedata.normalize("NFKC", name) != name
    ):
        raise ValueError(f"Unsafe Python operation name: {name!r}")
    output = render_schema_types(schema, scalars=scalars, enums=enums)
    inputs, outputs, _ = mapped_type_declarations(schema, scalars, enums)
    document = parse(source)
    visit(document, _SupportedSyntax())
    if any(
        re.fullmatch(r"[_A-Za-z][_0-9A-Za-z]*", item) is None
        for item in client_directives
    ):
        raise ValueError("Client directives must be GraphQL names without @")
    client_names = _CLIENT_DIRECTIVES | set(client_directives)

    class KnownClientDirectives(KnownDirectivesRule):
        def enter_directive(
            self,
            node: DirectiveNode,
            key: object,
            parent: object,
            path: object,
            ancestors: list[Node],
        ) -> None:
            if node.name.value in client_names and not schema.get_directive(
                node.name.value
            ):
                return
            super().enter_directive(node, key, parent, path, ancestors)

    errors = validate(
        schema,
        document,
        [
            KnownClientDirectives if rule is KnownDirectivesRule else rule
            for rule in specified_rules
        ],
    )
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
    reserved = set(schema.type_map) | set(inputs.values()) | set(outputs.values())
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
        leaf = _named_type(variable.type)
        annotation = _output_type(variable.type, inputs.get(leaf, leaf))
        if variable.default_value is not None or not isinstance(
            variable.type, NonNullTypeNode
        ):
            rendered = f"NotRequired[{annotation!r}]"
        else:
            rendered = repr(annotation)
        variables.append((variable.variable.name.value, rendered))
    declare(f"{name}Variables", variables)

    variant_index = 0
    needs_never = False

    def possible(parent: GraphQLCompositeType) -> tuple[GraphQLObjectType, ...]:
        if isinstance(parent, GraphQLObjectType):
            return (parent,)
        return tuple(schema.get_possible_types(parent))

    def collect(
        concrete: GraphQLObjectType,
        nodes: list[tuple[GraphQLCompositeType, SelectionNode]],
    ) -> list[tuple[GraphQLCompositeType, FieldNode]]:
        fields: list[tuple[GraphQLCompositeType, FieldNode]] = []
        for scope, node in nodes:
            if concrete not in possible(scope):
                continue
            if isinstance(node, FieldNode):
                fields.append((scope, node))
            else:
                assert isinstance(node, InlineFragmentNode)
                condition = (
                    schema.get_type(node.type_condition.name.value)
                    if node.type_condition
                    else scope
                )
                assert isinstance(
                    condition,
                    (GraphQLObjectType, GraphQLInterfaceType, GraphQLUnionType),
                )
                fields.extend(
                    collect(
                        concrete,
                        [(condition, child) for child in node.selection_set.selections],
                    )
                )
        return fields

    def selection(
        parents: list[GraphQLCompositeType],
        nodes: list[tuple[GraphQLCompositeType, SelectionNode]],
        class_name: str,
    ) -> None:
        nonlocal variant_index, needs_never
        abstract = any(not isinstance(parent, GraphQLObjectType) for parent in parents)
        candidates = set(possible(parents[0]))
        for parent in parents[1:]:
            candidates.intersection_update(possible(parent))
        branches: list[str] = []
        for concrete in sorted(candidates, key=lambda item: item.name):
            branch = class_name
            if abstract:
                branch = f"__graphql_desk_variant_{variant_index}"
                variant_index += 1
            branches.append(branch)
            groups: dict[str, list[tuple[GraphQLCompositeType, FieldNode]]] = {}
            for scope, node in collect(concrete, nodes):
                key = node.alias.value if node.alias else node.name.value
                groups.setdefault(key, []).append((scope, node))
            fields: list[tuple[str, str]] = []
            # An authored alias owns its response key, even when named __typename.
            if abstract and "__typename" not in groups:
                fields.append(
                    ("__typename", f"NotRequired[{f'Literal[{concrete.name!r}]'!r}]")
                )
            for key, group in sorted(groups.items()):
                scope, node = group[0]
                optional = all(_directives(item) & _CONDITIONAL for _, item in group)
                overrides = {_null_override(item) for _, item in group}
                if len(overrides) > 1:
                    raise ValueError(
                        "Repeated fields have different nullability directives"
                    )

                def annotation(value: str) -> str:
                    return f"NotRequired[{value!r}]" if optional else repr(value)

                if node.name.value == "__typename":
                    # Pinned inference keeps typename literal even with @optional.
                    fields.append((key, annotation(f"Literal[{concrete.name!r}]")))
                    continue
                if node.name.value.startswith("__"):
                    raise ValueError(
                        "Introspection object selections are not supported yet"
                    )
                assert not isinstance(scope, GraphQLUnionType)
                field_type = parse_type(str(scope.fields[node.name.value].type))
                named = schema.get_type(_named_type(field_type))
                assert named is not None
                if isinstance(
                    named, (GraphQLObjectType, GraphQLInterfaceType, GraphQLUnionType)
                ):
                    if len(group) > 1 and any(
                        _directives(item) & _CONDITIONAL for _, item in group
                    ):
                        raise ValueError(
                            "Conditionally repeated composite fields "
                            "are not supported yet"
                        )
                    child_name = f"{branch}_{key}"
                    children: list[tuple[GraphQLCompositeType, SelectionNode]] = []
                    child_parents: list[GraphQLCompositeType] = []
                    for item_scope, item in group:
                        assert not isinstance(item_scope, GraphQLUnionType)
                        child_type = schema.get_type(
                            _named_type(
                                parse_type(str(item_scope.fields[item.name.value].type))
                            )
                        )
                        assert isinstance(
                            child_type,
                            (GraphQLObjectType, GraphQLInterfaceType, GraphQLUnionType),
                        )
                        child_parents.append(child_type)
                        assert item.selection_set is not None
                        children.extend(
                            (child_type, child)
                            for child in item.selection_set.selections
                        )
                    selection(child_parents, children, child_name)
                    leaf = child_name
                elif isinstance(named, (GraphQLScalarType, GraphQLEnumType)):
                    leaf = outputs.get(named.name, named.name)
                else:
                    raise ValueError(f"Unsupported output type: {named.name}")
                override = _null_override(node)
                if override is None:
                    value = _output_type(field_type, leaf)
                else:
                    outer = (
                        field_type.type
                        if isinstance(field_type, NonNullTypeNode)
                        else field_type
                    )
                    value = _output_type(outer, leaf, nullable=override)
                fields.append((key, annotation(value)))
            declare(branch, fields)
        if abstract or not branches:
            if class_name in reserved:
                raise ValueError(
                    f"Colliding generated declaration name: {class_name!r}"
                )
            reserved.add(class_name)
            if not branches:
                needs_never = True
            declarations.append(
                f"{class_name} = " + (" | ".join(branches) or "__graphql_desk_Never")
            )

    root = schema.get_root_type(selected.operation)
    assert root is not None
    selection(
        [root],
        [(root, node) for node in selected.selection_set.selections],
        f"{name}Result",
    )
    if needs_never:
        output += "\nfrom typing_extensions import Never as __graphql_desk_Never\n"
    return output + "\n" + "\n\n".join(declarations) + "\n"
