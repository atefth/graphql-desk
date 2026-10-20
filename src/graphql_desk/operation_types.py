"""Render the supported operation subset as static dictionary declarations."""

import keyword
import unicodedata
from collections.abc import Iterable

from graphql import (
    FieldNode,
    FragmentDefinitionNode,
    FragmentSpreadNode,
    GraphQLCompositeType,
    GraphQLEnumType,
    GraphQLInterfaceType,
    GraphQLNonNull,
    GraphQLObjectType,
    GraphQLScalarType,
    GraphQLSchema,
    GraphQLUnionType,
    InlineFragmentNode,
    ListTypeNode,
    NamedTypeNode,
    NameNode,
    Node,
    NonNullTypeNode,
    OperationDefinitionNode,
    SelectionNode,
    TypeNode,
    Visitor,
    parse,
    parse_type,
    print_ast,
    visit,
)

from .config import ScalarMapping
from .fragment_refs import fragment_identity_key
from .fragments import FragmentIndex, IndexedDocument, SchemaContext
from .schema_types import mapped_type_declarations, render_schema_types
from .source_graph import DocumentIdentity
from .validation import validate_document

_CONDITIONAL = frozenset({"include", "skip", "defer"})
# True marks a conditional occurrence chosen present in a correlated alternative.
_Field = tuple[GraphQLCompositeType, FieldNode, bool]


def _directives(
    node: FieldNode | InlineFragmentNode | FragmentSpreadNode | FragmentDefinitionNode,
) -> set[str]:
    return {directive.name.value for directive in node.directives or ()}


def _null_override(node: FieldNode) -> bool | None:
    names = _directives(node)
    if names & {"required", "_required"}:
        return False
    if names & {"optional", "_optional"}:
        return True
    return None


def _outer_nullable(scope: GraphQLCompositeType, node: FieldNode) -> bool:
    if node.name.value == "__typename":
        return False
    if node.name.value.startswith("__"):
        raise ValueError("Introspection object selections are not supported yet")
    override = _null_override(node)
    if override is not None:
        return override
    assert not isinstance(scope, GraphQLUnionType)
    return not isinstance(scope.fields[node.name.value].type, GraphQLNonNull)


class _SupportedSyntax(Visitor):
    def __init__(self, *, disable_masking: bool) -> None:
        super().__init__()
        self.disable_masking = disable_masking

    def enter(self, node: Node, *_args: object) -> None:
        if isinstance(node, FragmentSpreadNode) and not self.disable_masking:
            raise ValueError(
                "Masked fragment spreads are not supported yet; "
                "disable masking explicitly"
            )


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


def _render_operation_types(
    schema: GraphQLSchema,
    source: str,
    *,
    name: str,
    operation: str | None = None,
    scalars: tuple[ScalarMapping, ...] = (),
    enums: tuple[tuple[str, str], ...] = (),
    client_directives: tuple[str, ...] = (),
    disable_masking: bool = False,
    fragment_keys: dict[str, str] | None = None,
    selected_fragment: str | None = None,
) -> str:
    """Render validated operation declarations, rejecting unsupported semantics.

    Raw fragments require explicit unmasking; indexed callers supply owner keys.
    Conditional expansion is bounded; no execution or inference occurs.
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
    visit(
        document,
        _SupportedSyntax(disable_masking=disable_masking or fragment_keys is not None),
    )
    errors = validate_document(schema, document, client_directives=client_directives)
    if errors:
        raise ValueError("; ".join(error.message for error in errors))
    fragments = {
        definition.name.value: definition
        for definition in document.definitions
        if isinstance(definition, FragmentDefinitionNode)
    }
    identities: dict[str, str] = {}
    if fragment_keys:
        output += (
            "from graphql_desk.fragment_refs import "
            "FragmentIdentity as __graphql_desk_FragmentIdentity\n"
        )
        for index, (fragment_name, key) in enumerate(sorted(fragment_keys.items())):
            alias = f"__graphql_desk_fragment_identity_{index}"
            identities[fragment_name] = alias
            output += f"{alias} = __graphql_desk_FragmentIdentity[Literal[{key!r}]]\n"
    fragment_choices: dict[tuple[str, str], list[list[_Field]]] = {}
    operations = [
        definition
        for definition in document.definitions
        if isinstance(definition, OperationDefinitionNode)
    ]
    selected: OperationDefinitionNode | None = None
    if selected_fragment is not None:
        if operations or selected_fragment not in fragments:
            raise ValueError("Select a primary fragment from a fragment-only document")
    elif operation is None:
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

    def declare(
        class_name: str, fields: list[tuple[str, str]], *, closed: bool = False
    ) -> None:
        if class_name in reserved:
            raise ValueError(f"Colliding generated declaration name: {class_name!r}")
        reserved.add(class_name)
        body = "\n".join(f"    {key!r}: {annotation}," for key, annotation in fields)
        ending = ", closed=True" if closed else ""
        declarations.append(
            f"{class_name} = TypedDict({class_name!r}, {{\n{body}\n}}{ending})"
        )

    variables: list[tuple[str, str]] = []
    for variable in sorted(
        (selected.variable_definitions or ()) if selected is not None else (),
        key=lambda item: item.variable.name.value,
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
    needs_capabilities = False
    conditional_alternatives = 0

    def possible(parent: GraphQLCompositeType) -> tuple[GraphQLObjectType, ...]:
        if isinstance(parent, GraphQLObjectType):
            return (parent,)
        return tuple(schema.get_possible_types(parent))

    def bounded(alternatives: Iterable[list[_Field]]) -> list[list[_Field]]:
        unique: dict[tuple[tuple[str, ...], ...], list[_Field]] = {}
        for fields in alternatives:
            indexed: dict[tuple[str, ...], _Field] = {}
            for scope, node, forced in fields:
                key = (
                    node.alias.value if node.alias else node.name.value,
                    scope.name,
                    node.name.value,
                    str(_null_override(node)),
                    str(bool(_directives(node) & _CONDITIONAL) and not forced),
                    print_ast(node.selection_set) if node.selection_set else "",
                )
                indexed.setdefault(key, (scope, node, forced))
            identity = tuple(sorted(indexed))
            if identity not in unique:
                if len(unique) == 64:
                    raise ValueError(
                        "Conditional selection exceeds 64 presence alternatives"
                    )
                unique[identity] = [indexed[key] for key in identity]
        return [unique[key] for key in sorted(unique)]

    def collect(
        concrete: GraphQLObjectType,
        nodes: list[tuple[GraphQLCompositeType, SelectionNode]],
    ) -> list[list[_Field]]:
        alternatives: list[list[_Field]] = [[]]
        for scope, node in nodes:
            if concrete not in possible(scope):
                continue
            if isinstance(node, FieldNode):
                for fields in alternatives:
                    fields.append((scope, node, False))
                continue
            else:
                assert isinstance(node, (InlineFragmentNode, FragmentSpreadNode))
                definition = (
                    fragments[node.name.value]
                    if isinstance(node, FragmentSpreadNode)
                    else node
                )
                condition = (
                    schema.get_type(definition.type_condition.name.value)
                    if definition.type_condition
                    else scope
                )
                assert isinstance(
                    condition,
                    (GraphQLObjectType, GraphQLInterfaceType, GraphQLUnionType),
                )
                cache_key = (
                    (node.name.value, concrete.name)
                    if isinstance(node, FragmentSpreadNode)
                    else None
                )
                if concrete not in possible(condition):
                    additions: list[list[_Field]] = [[]]
                elif (
                    isinstance(node, FragmentSpreadNode)
                    and not disable_masking
                    and fragment_keys is not None
                    and "_unmask" not in _directives(definition)
                ):
                    marker = FieldNode(
                        name=NameNode(value="_graphql_desk_φ"),
                        alias=NameNode(value=identities[node.name.value]),
                    )
                    additions = [[(condition, marker, False)]]
                elif cache_key is not None and cache_key in fragment_choices:
                    additions = fragment_choices[cache_key]
                else:
                    additions = collect(
                        concrete,
                        [
                            (condition, child)
                            for child in definition.selection_set.selections
                        ],
                    )
                    if cache_key is not None:
                        fragment_choices[cache_key] = additions
                if (_directives(node) | _directives(definition)) & _CONDITIONAL:
                    additions = bounded([[], *additions])
            alternatives = bounded(
                [*previous, *addition]
                for previous in alternatives
                for addition in additions
            )
        return bounded(alternatives)

    def expand_repeated(fields: list[_Field]) -> list[list[_Field]]:
        groups: dict[str, list[_Field]] = {}
        for field in fields:
            _, node, _ = field
            key = node.alias.value if node.alias else node.name.value
            groups.setdefault(key, []).append(field)
        repeated = {
            key
            for key, group in groups.items()
            if len(group) > 1
            and (
                group[0][1].selection_set is not None
                or (
                    group[0][1].name.value != "__typename"
                    and len({_outer_nullable(scope, node) for scope, node, _ in group})
                    > 1
                    and not any(
                        not _outer_nullable(scope, node)
                        and (forced or not (_directives(node) & _CONDITIONAL))
                        for scope, node, forced in group
                    )
                )
            )
        }
        if not any(
            (node.alias.value if node.alias else node.name.value) in repeated
            and _directives(node) & _CONDITIONAL
            for _, node, _ in fields
        ):
            return [fields]
        alternatives: list[list[_Field]] = [[]]
        for scope, node, forced in fields:
            key = node.alias.value if node.alias else node.name.value
            if key in repeated and _directives(node) & _CONDITIONAL:
                alternatives = bounded(
                    option
                    for previous in alternatives
                    for option in (previous, [*previous, (scope, node, True)])
                )
            else:
                for previous in alternatives:
                    previous.append((scope, node, forced))
        return bounded(alternatives)

    def selection(
        parents: list[GraphQLCompositeType],
        nodes: list[tuple[GraphQLCompositeType, SelectionNode]],
        class_name: str,
        *,
        closed: bool = False,
    ) -> None:
        nonlocal variant_index, needs_never, conditional_alternatives
        nonlocal needs_capabilities
        abstract = any(not isinstance(parent, GraphQLObjectType) for parent in parents)
        candidates = set(possible(parents[0]))
        for parent in parents[1:]:
            candidates.intersection_update(possible(parent))
        branches: list[str] = []
        choices: list[tuple[GraphQLObjectType, list[list[_Field]]]] = []
        correlated = False
        count = 0
        for concrete in sorted(candidates, key=lambda item: item.name):
            alternatives = bounded(
                expanded
                for fields in collect(concrete, nodes)
                for expanded in expand_repeated(fields)
            )
            choices.append((concrete, alternatives))
            count += len(alternatives)
            correlated = correlated or len(alternatives) > 1
            if correlated and count > 64:
                raise ValueError(
                    "Conditional selection exceeds 64 presence alternatives"
                )
        if correlated:
            conditional_alternatives += count
            if conditional_alternatives > 256:
                raise ValueError(
                    "Operation exceeds 256 conditional presence alternatives"
                )
        owns_typename = any(
            (node.alias.value if node.alias else node.name.value) == "__typename"
            for _, alternatives in choices
            for fields in alternatives
            for _, node, _ in fields
        )
        for concrete, alternatives in choices:
            for variant_fields in alternatives:
                branch = class_name
                if abstract or correlated:
                    branch = f"__graphql_desk_variant_{variant_index}"
                    variant_index += 1
                branches.append(branch)
                groups: dict[str, list[_Field]] = {}
                capabilities: set[str] = set()
                for scope, node, forced in variant_fields:
                    if node.name.value == "_graphql_desk_φ":
                        assert node.alias is not None
                        capabilities.add(node.alias.value)
                        continue
                    key = node.alias.value if node.alias else node.name.value
                    groups.setdefault(key, []).append((scope, node, forced))
                fields: list[tuple[str, str]] = []
                if capabilities:
                    needs_capabilities = True
                    identity = " | ".join(sorted(capabilities))
                    fields.append(
                        (
                            "_graphql_desk_φ",
                            "__graphql_desk_ReadOnly["
                            f"__graphql_desk_Callable[[{identity}], None]]",
                        )
                    )
                # An authored alias owns its response key, even when named __typename.
                if abstract and not owns_typename:
                    fields.append(
                        (
                            "__typename",
                            f"NotRequired[{f'Literal[{concrete.name!r}]'!r}]",
                        )
                    )
                for key, group in sorted(groups.items()):
                    scope, node, _ = group[0]
                    optional = all(
                        _directives(item) & _CONDITIONAL and not forced
                        for _, item, forced in group
                    )
                    # Intersect active occurrences only; differing conditional
                    # wrappers were split by expand_repeated before this merge.
                    nullable = all(
                        _outer_nullable(scope, item) for scope, item, _ in group
                    )

                    def annotation(value: str) -> str:
                        return f"NotRequired[{value!r}]" if optional else repr(value)

                    if node.name.value == "__typename":
                        # Pinned inference keeps typename literal even with @optional.
                        fields.append((key, annotation(f"Literal[{concrete.name!r}]")))
                        continue
                    assert not isinstance(scope, GraphQLUnionType)
                    field_type = parse_type(str(scope.fields[node.name.value].type))
                    named = schema.get_type(_named_type(field_type))
                    assert named is not None
                    if isinstance(
                        named,
                        (GraphQLObjectType, GraphQLInterfaceType, GraphQLUnionType),
                    ):
                        child_name = f"{branch}_{key}"
                        children: list[tuple[GraphQLCompositeType, SelectionNode]] = []
                        child_parents: list[GraphQLCompositeType] = []
                        for item_scope, item, _ in group:
                            assert not isinstance(item_scope, GraphQLUnionType)
                            child_type = schema.get_type(
                                _named_type(
                                    parse_type(
                                        str(item_scope.fields[item.name.value].type)
                                    )
                                )
                            )
                            assert isinstance(
                                child_type,
                                (
                                    GraphQLObjectType,
                                    GraphQLInterfaceType,
                                    GraphQLUnionType,
                                ),
                            )
                            child_parents.append(child_type)
                            assert item.selection_set is not None
                            children.extend(
                                (child_type, child)
                                for child in item.selection_set.selections
                            )
                        selection(
                            child_parents,
                            children,
                            child_name,
                            closed=closed or correlated,
                        )
                        leaf = child_name
                    elif isinstance(named, (GraphQLScalarType, GraphQLEnumType)):
                        leaf = outputs.get(named.name, named.name)
                    else:
                        raise ValueError(f"Unsupported output type: {named.name}")
                    outer = (
                        field_type.type
                        if isinstance(field_type, NonNullTypeNode)
                        else field_type
                    )
                    value = _output_type(outer, leaf, nullable=nullable)
                    fields.append((key, annotation(value)))
                declare(branch, fields, closed=closed or correlated)
        if branches != [class_name]:
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

    root: GraphQLCompositeType
    if selected is not None:
        operation_root = schema.get_root_type(selected.operation)
        assert operation_root is not None
        root = operation_root
        root_nodes = selected.selection_set.selections
    else:
        assert selected_fragment is not None
        definition = fragments[selected_fragment]
        fragment_root = schema.get_type(definition.type_condition.name.value)
        assert isinstance(
            fragment_root, (GraphQLObjectType, GraphQLInterfaceType, GraphQLUnionType)
        )
        root = fragment_root
        root_nodes = definition.selection_set.selections
    selection(
        [root],
        [(root, node) for node in root_nodes],
        f"{name}Result",
    )
    if fragment_keys is not None:
        for fragment_name, definition in sorted(fragments.items()):
            if fragment_name == selected_fragment:
                continue
            parent = schema.get_type(definition.type_condition.name.value)
            assert isinstance(
                parent, (GraphQLObjectType, GraphQLInterfaceType, GraphQLUnionType)
            )
            selection(
                [parent],
                [(parent, node) for node in definition.selection_set.selections],
                f"{name}Fragment_{fragment_name}",
            )
    if needs_capabilities:
        output += (
            "\nfrom collections.abc import Callable as __graphql_desk_Callable\n"
            "from typing_extensions import ReadOnly as __graphql_desk_ReadOnly\n"
        )
    if needs_never:
        output += "\nfrom typing_extensions import Never as __graphql_desk_Never\n"
    return output + "\n" + "\n\n".join(declarations) + "\n"


def render_operation_types(
    schema: GraphQLSchema,
    source: str,
    *,
    name: str,
    operation: str | None = None,
    scalars: tuple[ScalarMapping, ...] = (),
    enums: tuple[tuple[str, str], ...] = (),
    client_directives: tuple[str, ...] = (),
    disable_masking: bool = False,
) -> str:
    """Render validated operations; masked spreads require indexed ownership."""
    return _render_operation_types(
        schema,
        source,
        name=name,
        operation=operation,
        scalars=scalars,
        enums=enums,
        client_directives=client_directives,
        disable_masking=disable_masking,
    )


def _indexed_fragment_keys(
    context: SchemaContext,
    document: IndexedDocument,
    fragment_index: FragmentIndex | None,
    disable_masking: bool,
) -> dict[str, str] | None:
    call = document.call
    if (
        document.schema_identity != (call.module.project, context.schema_name)
        or call.schema_name not in (None, context.schema_name)
        or document.context_token != context.token
    ):
        raise ValueError("Indexed document schema identity or context token differs")
    keys: dict[str, str] | None = None
    if not disable_masking and fragment_index is not None:
        if document not in fragment_index.documents:
            raise ValueError("Indexed document is absent from fragment owner registry")
        owners: dict[DocumentIdentity, IndexedDocument] = {}
        for owner in fragment_index.documents:
            identity = DocumentIdentity(owner.call.module, owner.call.range)
            if identity in owners:
                raise ValueError("Duplicate fragment owner identity")
            owners[identity] = owner
        keys = {}
        for fact in document.definitions:
            if fact.kind != "fragment":
                continue
            resolved_owner = owners.get(fact.document)
            if (
                resolved_owner is None
                or resolved_owner.schema_identity != document.schema_identity
                or resolved_owner.context_token != document.context_token
            ):
                raise ValueError("Fragment owner schema identity or context differs")
            assert fact.name is not None
            keys[fact.name] = fragment_identity_key(resolved_owner, fact)
    return keys


def render_indexed_operation_types(
    context: SchemaContext,
    document: IndexedDocument,
    *,
    name: str,
    scalars: tuple[ScalarMapping, ...] = (),
    enums: tuple[tuple[str, str], ...] = (),
    client_directives: tuple[str, ...] = (),
    disable_masking: bool = False,
    fragment_index: FragmentIndex | None = None,
) -> str:
    """Render trusted index facts with their selected operation and context.

    The caller owns snapshot/config/schema freshness. This checks supplied
    identities only; it neither reparses authored source nor loads project code.
    """
    keys = _indexed_fragment_keys(context, document, fragment_index, disable_masking)
    call = document.call
    selected = document.selected_operation
    if (
        selected is None
        or selected.kind == "fragment"
        or selected not in document.definitions
        or selected.document != DocumentIdentity(call.module, call.range)
        or (call.operation is not None and call.operation != selected.name)
    ):
        raise ValueError("Indexed document selected operation identity differs")
    return _render_operation_types(
        context.schema,
        "\n\n".join(fact.printed for fact in document.definitions),
        name=name,
        operation=selected.name,
        scalars=scalars,
        enums=enums,
        client_directives=client_directives,
        disable_masking=disable_masking,
        fragment_keys=keys,
    )


def render_indexed_fragment_types(
    context: SchemaContext,
    document: IndexedDocument,
    *,
    name: str,
    scalars: tuple[ScalarMapping, ...] = (),
    enums: tuple[tuple[str, str], ...] = (),
    client_directives: tuple[str, ...] = (),
    disable_masking: bool = False,
    fragment_index: FragmentIndex | None = None,
) -> str:
    """Render the primary fragment's payload without inventing an operation.

    Its own fields are visible; nested spreads follow the indexed masking policy.
    Variables are empty because fragment definitions declare no operation variables.
    The caller owns snapshot/config/schema freshness, as for indexed operations.
    """
    keys = _indexed_fragment_keys(context, document, fragment_index, disable_masking)
    primary = document.primary_fragment
    if (
        primary is None
        or primary.kind != "fragment"
        or primary.name is None
        or primary not in document.definitions
        or primary.document
        != DocumentIdentity(document.call.module, document.call.range)
        or document.selected_operation is not None
        or document.call.operation is not None
    ):
        raise ValueError("Indexed document primary fragment identity differs")
    return _render_operation_types(
        context.schema,
        "\n\n".join(fact.printed for fact in document.definitions),
        name=name,
        scalars=scalars,
        enums=enums,
        client_directives=client_directives,
        disable_masking=disable_masking,
        fragment_keys=keys,
        selected_fragment=primary.name,
    )
