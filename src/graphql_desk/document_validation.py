"""Validate indexed original ASTs and map errors to supplied Python snapshots."""

from graphql import (
    DocumentNode,
    FragmentDefinitionNode,
    OperationDefinitionNode,
    Source,
    parse,
    print_ast,
)

from .fragments import DefinitionFact, FragmentIndex, IndexedDocument, SchemaContext
from .source import Span
from .source_graph import DocumentIdentity, GraphDiagnostic, ModuleSnapshot
from .source_maps import SourceMap, build_source_map
from .validation import validate_deprecations, validate_document


def validate_indexed_document(
    context: SchemaContext,
    document: IndexedDocument,
    *,
    fragment_index: FragmentIndex,
    modules: tuple[ModuleSnapshot, ...],
    client_directives: tuple[str, ...] = (),
) -> tuple[GraphDiagnostic, ...]:
    """Use original locations, including imported and split literal origins.

    The caller owns config/schema freshness and must supply a trusted index.
    Supplied source bytes, owner identities and context tokens are checked here.
    """
    if document not in fragment_index.documents:
        raise ValueError("Indexed document is absent from its owner registry")
    if (
        not document.definitions
        or document.schema_identity
        != (document.call.module.project, context.schema_name)
        or document.context_token != context.token
    ):
        raise ValueError("Indexed document schema context differs")
    owners: dict[DocumentIdentity, IndexedDocument] = {}
    for registered in fragment_index.documents:
        identity = DocumentIdentity(registered.call.module, registered.call.range)
        if identity in owners:
            raise ValueError("Duplicate indexed owner identity")
        owners[identity] = registered
    parsed: dict[
        DocumentIdentity,
        dict[DefinitionFact, OperationDefinitionNode | FragmentDefinitionNode],
    ] = {}
    origins: dict[int, SourceMap] = {}
    nodes: list[OperationDefinitionNode | FragmentDefinitionNode] = []
    for fact in document.definitions:
        owner = owners.get(fact.document)
        if (
            owner is None
            or owner.schema_identity != document.schema_identity
            or owner.schema_identity
            != (document.call.module.project, context.schema_name)
            or owner.context_token != context.token
            or owner.call.schema_name not in (None, context.schema_name)
            or fact not in owner.definitions
        ):
            raise ValueError("Indexed definition owner or schema context differs")
        if fact.document not in parsed:
            mapping = build_source_map(owner.call, modules)
            source = Source(owner.call.source, name=owner.call.module.uri)
            originals = parse(source)
            local: dict[
                DefinitionFact, OperationDefinitionNode | FragmentDefinitionNode
            ] = {}
            for node in originals.definitions:
                if not isinstance(
                    node, (OperationDefinitionNode, FragmentDefinitionNode)
                ):
                    raise ValueError(
                        "Indexed source contains a non-executable definition"
                    )
                original = DefinitionFact(
                    fact.document,
                    "fragment"
                    if isinstance(node, FragmentDefinitionNode)
                    else node.operation.value,
                    node.name.value if node.name else None,
                    node.type_condition.name.value
                    if isinstance(node, FragmentDefinitionNode)
                    else None,
                    print_ast(node),
                )
                if original in local:
                    raise ValueError("Duplicate original definition")
                local[original] = node
            parsed[fact.document] = local
            origins[id(source)] = mapping
        original_node = parsed[fact.document].get(fact)
        if original_node is None:
            raise ValueError("Indexed definition differs from original source")
        nodes.append(original_node)
    diagnostics: list[GraphDiagnostic] = []
    validated = DocumentNode(definitions=tuple(nodes))
    errors = (
        (error, "graphql-validation")
        for error in validate_document(
            context.schema, validated, client_directives=client_directives
        )
    )
    warnings = (
        (error, "graphql-deprecation")
        for error in validate_deprecations(context.schema, validated)
    )
    for error, code in (*errors, *warnings):
        located = False
        for error_node in error.nodes or ():
            if error_node.loc is None:
                continue
            error_mapping = origins.get(id(error_node.loc.source))
            if error_mapping is None:
                raise ValueError("Validation error has an unknown source origin")
            for host in error_mapping.to_host(
                Span(error_node.loc.start, error_node.loc.end)
            ):
                diagnostics.append(
                    GraphDiagnostic(
                        host.module, code, error.message, host.range
                    )
                )
                located = True
        if not located:
            diagnostics.append(
                GraphDiagnostic(
                    document.call.module,
                    code,
                    error.message,
                    document.call.range,
                )
            )
    return tuple(dict.fromkeys(diagnostics))
