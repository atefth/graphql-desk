"""Finite fragment composition facts from resolved Python source snapshots."""

from collections.abc import Mapping
from dataclasses import dataclass
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

from .source_graph import (
    DocumentIdentity,
    GraphDiagnostic,
    ModuleDependency,
    ProjectSources,
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
class FragmentIndex:
    documents: tuple[IndexedDocument, ...]
    diagnostics: tuple[GraphDiagnostic, ...]


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


def _identity(call: ResolvedCall) -> DocumentIdentity:
    return DocumentIdentity(call.module, call.range)


def _error(call: ResolvedCall, code: str, message: str) -> _Invalid:
    return _Invalid(GraphDiagnostic(call.module, code, message, call.range))


def _parse(call: ResolvedCall, context: SchemaContext) -> _Parsed:
    identity = _identity(call)
    nodes: list[_Executable] = []
    facts: list[DefinitionFact] = []
    operations: list[DefinitionFact] = []
    names: set[tuple[str, str]] = set()
    for node in parse(call.source).definitions:
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
                    call, "invalid-operation-selection", "Unknown selected operation"
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
) -> FragmentIndex:
    """Index one finite snapshot, preserving runtime composition order.

    Every schema, including an unbound None identity, requires explicit context.
    Only fragment-related GraphQL rules run here; renderers must still perform
    full operation validation. No AST escapes, source is rewritten, or hash/byte
    normalization policy is selected. Context schemas must remain stable while
    this synchronous function runs; their caller-supplied tokens are copied.
    """
    calls: dict[DocumentIdentity, ResolvedCall] = {}
    failed: set[DocumentIdentity] = set()
    diagnostics = list(sources.diagnostics)
    versions: dict[tuple[str, str], set[ModuleDependency]] = {}
    for call in sources.calls:
        identity = _identity(call)
        if identity in calls and calls[identity] != call:
            failed.add(identity)
            diagnostics.append(
                _error(
                    call, "conflicting-document", "Conflicting document snapshots"
                ).diagnostic
            )
        calls[identity] = call
        for dependency in (call.module, *call.dependencies):
            versions.setdefault((dependency.project, dependency.module), set()).add(
                dependency
            )
    mixed = {key for key, values in versions.items() if len(values) > 1}
    parsed: dict[DocumentIdentity, _Parsed] = {}
    completed: dict[
        DocumentIdentity, tuple[IndexedDocument, tuple[_Executable, ...]]
    ] = {}
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
            except RecursionError:
                failed.add(identity)
                diagnostics.append(
                    _error(
                        call,
                        "source-too-complex",
                        "Fragment syntax exceeds the analysis recursion limit",
                    ).diagnostic
                )
            except (GraphQLError, TypeError, ValueError) as error:
                failed.add(identity)
                diagnostics.append(
                    _error(call, "invalid-fragments", str(error)).diagnostic
                )
    return FragmentIndex(
        tuple(completed[identity][0] for identity in calls if identity in completed),
        tuple(diagnostics),
    )
