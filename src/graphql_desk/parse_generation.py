"""Pure parser companions; callers own any publication."""

from dataclasses import dataclass

from graphql import (
    DocumentNode,
    FragmentDefinitionNode,
    GraphQLError,
    OperationDefinitionNode,
    parse,
)

from .literal_ast import render_document_ast_types
from .source import Span
from .source_graph import (
    GraphDiagnostic,
    ModuleDependency,
    ModuleSnapshot,
    ParseSources,
    resolve_parse_sources,
)
from .source_maps import HostSpan, build_source_map


@dataclass(frozen=True)
class ParseCompanion:
    runtime: str
    stub: str


def render_parse_companion(sources: tuple[str, ...]) -> ParseCompanion:
    """Describe supplied executable literals; other inputs retain ordinary ASTs.

    Only the default parser grammar/options select exact views. SDL remains an
    ordinary DocumentNode. Invalid supplied syntax raises the parser's error.
    This does not validate against a schema, acquire source or write files.
    """
    return _render_parse_companion(
        tuple((source, parse(source)) for source in sorted(set(sources)))
    )


def render_captured_parse_overlay(
    modules: tuple[ModuleSnapshot, ...],
) -> ParseCompanion:
    """Render workspace overloads for normal ``graphql_desk.parse`` imports."""
    facts = resolve_parse_sources(modules)
    sources = sorted({call.source for call in facts.calls})
    parsed: list[tuple[str, DocumentNode]] = []
    for source in sources:
        try:
            document = parse(source)
        except (GraphQLError, RecursionError, ValueError):
            continue
        if _executable(document):
            parsed.append((source, document))
    companion = _render_parse_companion(tuple(parsed))
    graphql_import = (
        "from graphql import DocumentNode as _DocumentNode, Source as _Source\n"
    )
    if not companion.stub.startswith(graphql_import):
        raise ValueError("Parser overlay is missing its GraphQL imports")
    return ParseCompanion(
        companion.runtime,
        "\n".join(
            (
                "from .typed_document import TypedDocument",
                "from graphql import DocumentNode as _DocumentNode, Source as _Source",
                "",
                companion.stub[len(graphql_import) :].rstrip(),
                "",
                "from .fragment_helpers import read_fragment, "
                "read_fragment_passthrough",
                "from .runtime import gql, graphql, init_graphql",
                "from .testing import unsafe_read_result",
                "",
                "__all__ = [",
                "    'TypedDocument', 'gql', 'graphql', 'init_graphql', 'parse',",
                "    'read_fragment', 'read_fragment_passthrough',",
                "    'unsafe_read_result',",
                "]",
            )
        )
        + "\n",
    )


@dataclass(frozen=True)
class CapturedParseCompanion:
    module: ModuleDependency
    companion: ParseCompanion
    diagnostics: tuple[GraphDiagnostic, ...]


def render_captured_parse_companion(
    modules: tuple[ModuleSnapshot, ...],
    *,
    project_id: str,
    module: str,
    generated_package: str | None = None,
) -> CapturedParseCompanion:
    """Select one captured owner's declarations and diagnostics; never publish.

    Invalid parser strings are omitted while independent valid strings survive.
    Syntax diagnostics name actual contributing snapshots, which may differ from
    the selected owner. Unavailable provenance falls back to the whole host call;
    decoded parser locations remain in messages. Imports are not rewritten.
    """
    facts = resolve_parse_sources(modules, generated_package=generated_package)
    return _render_captured_parse_companion(modules, facts, project_id, module)


def render_captured_parse_companions(
    modules: tuple[ModuleSnapshot, ...],
    *,
    project_id: str,
    generated_package: str | None = None,
) -> tuple[CapturedParseCompanion, ...]:
    """Render recognized parser owners after resolving the project graph once."""
    facts = resolve_parse_sources(modules, generated_package=generated_package)
    calls = {item.module for item in facts.calls}
    diagnostics = {
        item.module
        for item in facts.diagnostics
        if item.code == "parse-fallback" and item.module.project == project_id
    }
    return tuple(
        _render_captured_parse_companion(modules, facts, project_id, item.module)
        for item in modules
        if item.project == project_id
        and ModuleDependency(item.project, item.module, item.uri, item.version)
        in calls | diagnostics
    )


def _render_captured_parse_companion(
    modules: tuple[ModuleSnapshot, ...],
    facts: ParseSources,
    project_id: str,
    module: str,
) -> CapturedParseCompanion:
    owners = [
        item for item in modules if (item.project, item.module) == (project_id, module)
    ]
    if len(owners) != 1:
        raise ValueError("A unique captured parser module is required")
    owner = owners[0]
    identity = ModuleDependency(owner.project, owner.module, owner.uri, owner.version)
    diagnostics = [item for item in facts.diagnostics if item.module == identity]
    documents: dict[str, tuple[str, str]] = {}
    seen: set[str] = set()
    failures: dict[str, tuple[str, str, tuple[int, ...]]] = {}
    for call in facts.calls:
        if call.module != identity:
            continue
        if call.source not in seen:
            seen.add(call.source)
            try:
                document = parse(call.source)
                if _executable(document):
                    index = len(documents)
                    documents[call.source] = (
                        render_document_ast_types(document, name=f"Parse{index}"),
                        render_document_ast_types(
                            parse(call.source, no_location=True),
                            name=f"Parse{index}NoLocation",
                        ),
                    )
            except GraphQLError as error:
                failures[call.source] = (
                    "parse-syntax",
                    str(error),
                    tuple(error.positions or ()),
                )
            except (RecursionError, ValueError) as error:
                failures[call.source] = (
                    "parse-fallback",
                    str(error) or "Literal AST exceeds declaration nesting limits",
                    (),
                )
        if call.source not in failures:
            continue
        code, message, positions = failures[call.source]
        origins: tuple[HostSpan, ...] = ()
        if positions:
            try:
                mapping = build_source_map(call, modules)
                mapped = [
                    mapping.to_host(Span(position, min(position + 1, len(call.source))))
                    for position in positions
                ]
                if all(mapped):
                    origins = tuple(
                        dict.fromkeys(origin for group in mapped for origin in group)
                    )
            except (ValueError, RecursionError):
                pass
        if not origins:
            origins = (HostSpan(call.module, call.range),)
        diagnostics.extend(
            GraphDiagnostic(origin.module, code, message, origin.range)
            for origin in origins
        )
    return CapturedParseCompanion(
        identity,
        _assemble_parse_companion(tuple(documents.items())),
        tuple(diagnostics),
    )


def _render_parse_companion(
    parsed: tuple[tuple[str, DocumentNode], ...],
) -> ParseCompanion:
    documents: list[tuple[str, tuple[str, str]]] = []
    for source, document in parsed:
        if _executable(document):
            index = len(documents)
            try:
                declarations = (
                    render_document_ast_types(document, name=f"Parse{index}"),
                    render_document_ast_types(
                        parse(source, no_location=True),
                        name=f"Parse{index}NoLocation",
                    ),
                )
            except (RecursionError, ValueError):
                continue
            documents.append((source, declarations))
    return _assemble_parse_companion(tuple(documents))


def _executable(document: DocumentNode) -> bool:
    return all(
        isinstance(node, (OperationDefinitionNode, FragmentDefinitionNode))
        for node in document.definitions
    )


def _assemble_parse_companion(
    documents: tuple[tuple[str, tuple[str, str]], ...],
) -> ParseCompanion:
    fallback = (
        "def parse(source: str | _Source, no_location: bool = False, "
        "max_tokens: int | None = None, "
        "experimental_fragment_arguments: bool = False) -> _DocumentNode: ...\n"
    )
    stub = "from graphql import DocumentNode as _DocumentNode, Source as _Source\n"
    if documents:
        stub += "from typing import Literal as _ParseLiteral, overload as _overload\n"
        for _, declarations in documents:
            stub += "\n".join(declarations) + "\n"
        for index, (source, _) in enumerate(documents):
            stub += (
                "@_overload\n"
                f"def parse(source: _ParseLiteral[{source!r}], "
                "no_location: _ParseLiteral[False] = False, "
                "max_tokens: None = None, "
                "experimental_fragment_arguments: _ParseLiteral[False] = False) "
                f"-> Parse{index}Document: ...\n"
                "@_overload\n"
                f"def parse(source: _ParseLiteral[{source!r}], "
                "no_location: _ParseLiteral[True], "
                "max_tokens: None = None, "
                "experimental_fragment_arguments: _ParseLiteral[False] = False) "
                f"-> Parse{index}NoLocationDocument: ...\n"
            )
        stub += "@_overload\n"
    stub += fallback
    return ParseCompanion("from graphql_desk import parse as parse\n", stub)
