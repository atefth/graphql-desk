"""Finite fragment composition facts from resolved Python source snapshots."""

from collections import defaultdict, deque
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Literal

from graphql import (
    DocumentNode,
    FragmentDefinitionNode,
    GraphQLError,
    GraphQLSchema,
    OperationDefinitionNode,
    parse,
    print_ast,
    validate,
)
from graphql.validation.rules.fragments_on_composite_types import (
    FragmentsOnCompositeTypesRule,
)
from graphql.validation.rules.known_fragment_names import KnownFragmentNamesRule
from graphql.validation.rules.known_type_names import KnownTypeNamesRule
from graphql.validation.rules.no_fragment_cycles import NoFragmentCyclesRule
from graphql.validation.rules.possible_fragment_spreads import (
    PossibleFragmentSpreadsRule,
)

from .source import Span
from .source_graph import (
    DocumentIdentity,
    GraphDiagnostic,
    ModuleDependency,
    ProjectSources,
    RejectedCall,
    ResolvedCall,
)

DefinitionKind = Literal["query", "mutation", "subscription", "fragment"]
_Executable = OperationDefinitionNode | FragmentDefinitionNode
_RULES = (
    KnownTypeNamesRule,
    FragmentsOnCompositeTypesRule,
    KnownFragmentNamesRule,
    PossibleFragmentSpreadsRule,
    NoFragmentCyclesRule,
)


@dataclass(frozen=True)
class SchemaContext:
    """Caller-owned generation input; token is not a hash computed by this index."""

    schema_name: str
    schema: GraphQLSchema
    token: str


@dataclass(frozen=True)
class DefinitionFact:
    document: DocumentIdentity
    kind: DefinitionKind
    name: str | None
    type_condition: str | None
    printed: str


@dataclass(frozen=True)
class IndexedDocument:
    call: ResolvedCall
    schema_identity: tuple[str, str]
    context_token: str
    selected_operation: DefinitionFact | None
    primary_fragment: DefinitionFact | None
    definitions: tuple[DefinitionFact, ...]
    dependencies: tuple[ModuleDependency, ...]


@dataclass(frozen=True)
class RejectedDocument:
    """Failed indexed owner and its local/dependency causes, not display ownership."""

    call: ResolvedCall
    diagnostics: tuple[GraphDiagnostic, ...]


@dataclass(frozen=True)
class FragmentIndex:
    documents: tuple[IndexedDocument, ...]
    diagnostics: tuple[GraphDiagnostic, ...]
    unavailable: tuple[RejectedDocument, ...] = ()
    rejected_calls: tuple[RejectedCall, ...] = ()


@dataclass(frozen=True)
class _Parsed:
    call: ResolvedCall
    context: SchemaContext
    nodes: tuple[_Executable, ...]
    facts: tuple[DefinitionFact, ...]
    selected: DefinitionFact | None
    primary: DefinitionFact | None


class _Invalid(Exception):
    def __init__(self, diagnostic: GraphDiagnostic) -> None:
        super().__init__(diagnostic.message)
        self.diagnostic = diagnostic


class _ReuseMismatch(Exception):
    pass


def _identity(call: ResolvedCall) -> DocumentIdentity:
    return DocumentIdentity(call.module, call.range)


def _error(
    call: ResolvedCall, code: str, message: str, *, range_: Span | None = None
) -> _Invalid:
    return _Invalid(GraphDiagnostic(call.module, code, message, range_ or call.range))


def _parse(call: ResolvedCall, context: SchemaContext) -> _Parsed:
    identity = _identity(call)
    nodes: list[_Executable] = []
    facts: list[DefinitionFact] = []
    operations: list[DefinitionFact] = []
    names: set[tuple[str, str]] = set()
    # The index keeps Python call spans and never returns the GraphQL AST.
    for node in parse(call.source, no_location=True).definitions:
        if not isinstance(node, (OperationDefinitionNode, FragmentDefinitionNode)):
            raise _error(
                call,
                "unsupported-definition",
                "Only executable definitions are supported",
            )
        name = node.name.value if node.name else None
        kind: DefinitionKind = (
            node.operation.value
            if isinstance(node, OperationDefinitionNode)
            else "fragment"
        )
        if name is not None:
            key = ("fragment" if kind == "fragment" else "operation", name)
            if key in names:
                raise _error(
                    call, "duplicate-definition", "Duplicate local definition name"
                )
            names.add(key)
        fact = DefinitionFact(
            identity,
            kind,
            name,
            node.type_condition.name.value
            if isinstance(node, FragmentDefinitionNode)
            else None,
            print_ast(node),
        )
        nodes.append(node)
        facts.append(fact)
        if kind != "fragment":
            operations.append(fact)
    selected: DefinitionFact | None = None
    if operations:
        if len(operations) > 1 and any(item.name is None for item in operations):
            raise _error(
                call,
                "invalid-operation-selection",
                "An anonymous operation must be the only operation",
            )
        if call.operation is None:
            if len(operations) != 1:
                raise _error(
                    call,
                    "invalid-operation-selection",
                    "Multiple operations require an explicit selector",
                )
            selected = operations[0]
        else:
            selected = next(
                (item for item in operations if item.name == call.operation), None
            )
            if selected is None:
                raise _error(
                    call,
                    "invalid-operation-selection",
                    "Unknown selected operation",
                    range_=call.operation_range,
                )
    elif call.operation is not None:
        raise _error(
            call,
            "invalid-operation-selection",
            "Fragment documents cannot select an operation",
        )
    return _Parsed(
        call,
        context,
        tuple(nodes),
        tuple(facts),
        selected,
        facts[0] if not operations else None,
    )


def index_fragments(
    sources: ProjectSources,
    *,
    schemas: Mapping[tuple[str, str | None], SchemaContext],
    previous_documents: tuple[IndexedDocument, ...] = (),
    unchanged_schemas: frozenset[tuple[str, str]] = frozenset(),
) -> FragmentIndex:
    """Index one finite snapshot, preserving runtime composition order.

    Every schema, including an unbound None identity, requires explicit context.
    Only fragment-related GraphQL rules run here; renderers must still perform
    full operation validation. No AST escapes, source is rewritten, or hash/byte
    normalization policy is selected. Context schemas must remain stable while
    this synchronous function runs; their caller-supplied tokens are copied.

    Reuse requires accepted documents from the previous capture and schema
    scopes whose exact input bytes were verified unchanged by the caller.
    """
    try:
        return _index_fragments(sources, schemas, previous_documents, unchanged_schemas)
    except _ReuseMismatch:
        return _index_fragments(sources, schemas, (), frozenset())


def _reusable_documents(
    previous: tuple[IndexedDocument, ...],
    calls: Mapping[DocumentIdentity, ResolvedCall],
    schemas: Mapping[tuple[str, str | None], SchemaContext],
    unchanged_schemas: frozenset[tuple[str, str]],
    failed: set[DocumentIdentity],
    mixed: set[tuple[str, str]],
) -> dict[DocumentIdentity, IndexedDocument]:
    reusable: dict[DocumentIdentity, IndexedDocument] = {}
    for document in previous:
        identity = _identity(document.call)
        call = calls.get(identity)
        if call is None or call != document.call or identity in failed:
            continue
        context = schemas.get((call.module.project, call.schema_name))
        if (
            document.schema_identity not in unchanged_schemas
            or context is None
            or document.schema_identity != (call.module.project, context.schema_name)
            or document.context_token != context.token
            or any(
                (dependency.project, dependency.module) in mixed
                for dependency in (call.module, *call.dependencies)
            )
        ):
            continue
        reusable[identity] = replace(document, call=call)

    # A hit must keep every explicit edge inside the previous successful graph.
    dependents: dict[DocumentIdentity, list[DocumentIdentity]] = defaultdict(list)
    invalid = deque[DocumentIdentity]()
    for identity, document in reusable.items():
        for reference in document.call.fragments:
            if reference.target in reusable:
                dependents[reference.target].append(identity)
            else:
                invalid.append(identity)
    while invalid:
        identity = invalid.popleft()
        if reusable.pop(identity, None) is not None:
            invalid.extend(dependents[identity])
    return reusable


def _index_fragments(
    sources: ProjectSources,
    schemas: Mapping[tuple[str, str | None], SchemaContext],
    previous_documents: tuple[IndexedDocument, ...],
    unchanged_schemas: frozenset[tuple[str, str]],
) -> FragmentIndex:
    calls: dict[DocumentIdentity, ResolvedCall] = {}
    failed: set[DocumentIdentity] = set()
    causes: dict[DocumentIdentity, tuple[GraphDiagnostic, ...]] = {}
    diagnostics = list(sources.diagnostics)
    versions: dict[tuple[str, str], set[ModuleDependency]] = {}
    for call in sources.calls:
        identity = _identity(call)
        if identity in calls and calls[identity] != call:
            failed.add(identity)
            diagnostic = _error(
                call, "conflicting-document", "Conflicting document snapshots"
            ).diagnostic
            diagnostics.append(diagnostic)
            causes[identity] = (*causes.get(identity, ()), diagnostic)
        calls[identity] = call
        for dependency in (call.module, *call.dependencies):
            versions.setdefault((dependency.project, dependency.module), set()).add(
                dependency
            )
    mixed = {key for key, values in versions.items() if len(values) > 1}
    parsed: dict[DocumentIdentity, _Parsed] = {}
    completed: dict[
        DocumentIdentity, tuple[IndexedDocument, tuple[_Executable, ...] | None]
    ] = {
        identity: (document, None)
        for identity, document in _reusable_documents(
            previous_documents, calls, schemas, unchanged_schemas, failed, mixed
        ).items()
    }
    materialized: dict[DocumentIdentity, dict[DefinitionFact, _Executable]] = {}

    def cached_nodes(document: IndexedDocument) -> tuple[_Executable, ...]:
        nodes: list[_Executable] = []
        try:
            for fact in document.definitions:
                if fact.document not in materialized:
                    owner = calls[fact.document]
                    current = _parse(
                        owner, schemas[(owner.module.project, owner.schema_name)]
                    )
                    materialized[fact.document] = dict(
                        zip(current.facts, current.nodes, strict=True)
                    )
                nodes.append(materialized[fact.document][fact])
        except (
            _Invalid,
            GraphQLError,
            KeyError,
            RecursionError,
            TypeError,
            ValueError,
        ):
            raise _ReuseMismatch from None
        return tuple(nodes)

    for root in calls:
        active: set[DocumentIdentity] = set()
        stack = [(root, False)]
        while stack:
            identity, finishing = stack.pop()
            if finishing:
                active.discard(identity)
            if identity in completed or identity in failed:
                continue
            call = calls[identity]
            try:
                if not finishing:
                    context = schemas.get((call.module.project, call.schema_name))
                    if context is None:
                        raise _error(
                            call,
                            "missing-schema-context",
                            "An explicit schema context is required",
                        )
                    if not context.schema_name or (
                        call.schema_name is not None
                        and call.schema_name != context.schema_name
                    ):
                        raise _error(
                            call,
                            "schema-context-mismatch",
                            "Schema context identity does not match the call",
                        )
                    if any(
                        (item.project, item.module) in mixed
                        for item in (call.module, *call.dependencies)
                    ):
                        raise _error(
                            call,
                            "mixed-source-versions",
                            "Source snapshot versions conflict",
                        )
                    if identity not in parsed:
                        parsed[identity] = _parse(call, context)
                    for reference in call.fragments:
                        if reference.target.module.project != call.module.project:
                            raise _Invalid(
                                GraphDiagnostic(
                                    reference.reference_module,
                                    "cross-project-fragment",
                                    "Fragment belongs to another project",
                                    reference.range,
                                )
                            )
                        if reference.target not in calls:
                            raise _Invalid(
                                GraphDiagnostic(
                                    reference.reference_module,
                                    "missing-fragment-target",
                                    "Fragment target is missing or stale",
                                    reference.range,
                                )
                            )
                        if reference.target in active:
                            raise _Invalid(
                                GraphDiagnostic(
                                    reference.reference_module,
                                    "fragment-reference-cycle",
                                    "Explicit fragment references form a cycle",
                                    reference.range,
                                )
                            )
                    active.add(identity)
                    stack.append((identity, True))
                    stack.extend(
                        (item.target, False) for item in reversed(call.fragments)
                    )
                    continue
                local = parsed[identity]
                nodes = list(local.nodes)
                facts = list(local.facts)
                fragments = {
                    fact.name: fact for fact in facts if fact.kind == "fragment"
                }
                dependencies = {
                    (item.project, item.module): item
                    for item in (call.module, *call.dependencies)
                }
                for reference in call.fragments:
                    target = completed.get(reference.target)
                    if target is None:
                        causes[identity] = tuple(
                            dict.fromkeys(
                                diagnostic
                                for dependency in call.fragments
                                for diagnostic in causes.get(dependency.target, ())
                            )
                        )
                        raise _Invalid(
                            GraphDiagnostic(
                                reference.reference_module,
                                "unavailable-fragment",
                                "Fragment target is unavailable",
                                reference.range,
                            )
                        )
                    document, target_nodes = target
                    if document.selected_operation is not None:
                        raise _Invalid(
                            GraphDiagnostic(
                                reference.reference_module,
                                "operation-as-fragment",
                                "An operation cannot be supplied as a fragment",
                                reference.range,
                            )
                        )
                    if document.schema_identity != (
                        call.module.project,
                        local.context.schema_name,
                    ):
                        raise _Invalid(
                            GraphDiagnostic(
                                reference.reference_module,
                                "cross-schema-fragment",
                                "Fragment belongs to another schema",
                                reference.range,
                            )
                        )
                    if document.context_token != local.context.token:
                        raise _Invalid(
                            GraphDiagnostic(
                                reference.reference_module,
                                "schema-context-mismatch",
                                "Fragment schema context tokens differ",
                                reference.range,
                            )
                        )
                    if target_nodes is None:
                        target_nodes = cached_nodes(document)
                        completed[reference.target] = document, target_nodes
                    for node, fact in zip(target_nodes, document.definitions):
                        previous = fragments.get(fact.name)
                        if previous is not None:
                            if previous.printed != fact.printed:
                                raise _error(
                                    call,
                                    "conflicting-fragment",
                                    "Conflicting imported fragment definitions",
                                )
                            continue
                        fragments[fact.name] = fact
                        nodes.append(node)
                        facts.append(fact)
                    for dependency in document.dependencies:
                        key = (dependency.project, dependency.module)
                        if key in dependencies and dependencies[key] != dependency:
                            raise _error(
                                call,
                                "mixed-source-versions",
                                "Fragment dependency versions conflict",
                            )
                        dependencies[key] = dependency
                errors = validate(
                    local.context.schema,
                    DocumentNode(definitions=tuple(nodes)),
                    rules=_RULES,
                )
                if errors:
                    raise _error(
                        call,
                        "invalid-fragments",
                        "; ".join(error.message for error in errors),
                    )
                document = IndexedDocument(
                    call,
                    (call.module.project, local.context.schema_name),
                    local.context.token,
                    local.selected,
                    local.primary,
                    tuple(facts),
                    tuple(
                        sorted(
                            dependencies.values(),
                            key=lambda item: (item.project, item.module),
                        )
                    ),
                )
                completed[identity] = document, tuple(nodes)
            except _Invalid as error:
                failed.add(identity)
                diagnostics.append(error.diagnostic)
                causes[identity] = (error.diagnostic, *causes.get(identity, ()))
            except RecursionError:
                failed.add(identity)
                diagnostic = _error(
                    call,
                    "source-too-complex",
                    "Fragment syntax exceeds the analysis recursion limit",
                ).diagnostic
                diagnostics.append(diagnostic)
                causes[identity] = (diagnostic,)
            except (GraphQLError, TypeError, ValueError) as error:
                failed.add(identity)
                diagnostic = _error(call, "invalid-fragments", str(error)).diagnostic
                diagnostics.append(diagnostic)
                causes[identity] = (diagnostic,)
    return FragmentIndex(
        tuple(completed[identity][0] for identity in calls if identity in completed),
        tuple(diagnostics),
        tuple(
            RejectedDocument(call, causes[identity])
            for identity, call in calls.items()
            if identity in failed
        ),
        sources.unavailable,
    )


def unused_colocated_fragment_diagnostics(
    sources: ProjectSources, index: FragmentIndex
) -> tuple[GraphDiagnostic, ...]:
    """Warn when an imported fragment is not composed by a document in its module."""
    indexed = {
        DocumentIdentity(item.call.module, item.call.range): item
        for item in index.documents
    }
    used = {
        (item.call.module, item.schema_identity, fact.document)
        for item in index.documents
        for fact in item.definitions
        if fact.kind == "fragment"
    }
    modules_with_documents = {item.call.module for item in index.documents}
    diagnostics: list[GraphDiagnostic] = []
    for imported in sources.document_imports:
        fragment = indexed.get(imported.document)
        if fragment is None or fragment.primary_fragment is None:
            continue
        if imported.module not in modules_with_documents:
            continue
        if (imported.module, fragment.schema_identity, imported.document) in used:
            continue
        name = fragment.primary_fragment.name or "<anonymous>"
        diagnostics.append(
            GraphDiagnostic(
                imported.module,
                "unused-colocated-fragment",
                f"Imported fragment {name!r} is not composed by a GraphQL document "
                "in this module.",
                imported.range,
            )
        )
    return tuple(diagnostics)
