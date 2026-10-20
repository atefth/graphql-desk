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
from .source_graph import (
    GraphDiagnostic,
    ModuleDependency,
    ModuleSnapshot,
    resolve_parse_sources,
)


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


@dataclass(frozen=True)
class CapturedParseCompanion:
    module: ModuleDependency
    companion: ParseCompanion
    diagnostics: tuple[GraphDiagnostic, ...]


def render_captured_parse_companion(
    modules: tuple[ModuleSnapshot, ...], *, project_id: str, module: str
) -> CapturedParseCompanion:
    """Select one captured owner's declarations and diagnostics; never publish.

    Invalid parser strings are omitted while independent valid strings survive.
    Syntax errors identify the host call and retain decoded parser locations in
    their message. Root imports are not rewritten or automatically made precise.
    """
    facts = resolve_parse_sources(modules)
    owners = [
        item for item in modules if (item.project, item.module) == (project_id, module)
    ]
    if len(owners) != 1:
        raise ValueError("A unique captured parser module is required")
    owner = owners[0]
    identity = ModuleDependency(owner.project, owner.module, owner.uri, owner.version)
    diagnostics = [item for item in facts.diagnostics if item.module == identity]
    documents: dict[str, str] = {}
    seen: set[str] = set()
    failures: dict[str, tuple[str, str]] = {}
    for call in facts.calls:
        if call.module != identity:
            continue
        if call.source in seen:
            if call.source in failures:
                code, message = failures[call.source]
                diagnostics.append(GraphDiagnostic(identity, code, message, call.range))
            continue
        seen.add(call.source)
        try:
            document = parse(call.source)
            if _executable(document):
                documents[call.source] = render_document_ast_types(
                    document, name=f"Parse{len(documents)}"
                )
        except GraphQLError as error:
            failures[call.source] = ("parse-syntax", str(error))
            diagnostics.append(
                GraphDiagnostic(identity, "parse-syntax", str(error), call.range)
            )
        except (RecursionError, ValueError) as error:
            failures[call.source] = (
                "parse-fallback",
                str(error) or "Literal AST exceeds declaration nesting limits",
            )
            diagnostics.append(
                GraphDiagnostic(
                    identity,
                    "parse-fallback",
                    str(error) or "Literal AST exceeds declaration nesting limits",
                    call.range,
                )
            )
    return CapturedParseCompanion(
        identity,
        _assemble_parse_companion(tuple(documents.items())),
        tuple(diagnostics),
    )


def _render_parse_companion(
    parsed: tuple[tuple[str, DocumentNode], ...],
) -> ParseCompanion:
    documents: list[tuple[str, str]] = []
    for source, document in parsed:
        if _executable(document):
            documents.append(
                (
                    source,
                    render_document_ast_types(document, name=f"Parse{len(documents)}"),
                )
            )
    return _assemble_parse_companion(tuple(documents))


def _executable(document: DocumentNode) -> bool:
    return all(
        isinstance(node, (OperationDefinitionNode, FragmentDefinitionNode))
        for node in document.definitions
    )


def _assemble_parse_companion(documents: tuple[tuple[str, str], ...]) -> ParseCompanion:
    fallback = (
        "def parse(source: str | _Source, no_location: bool = False, "
        "max_tokens: int | None = None, "
        "experimental_fragment_arguments: bool = False) -> _DocumentNode: ...\n"
    )
    stub = "from graphql import DocumentNode as _DocumentNode, Source as _Source\n"
    if documents:
        stub += "from typing import Literal as _ParseLiteral, overload as _overload\n"
        for _, declaration in documents:
            stub += declaration + "\n"
        for index, (source, _) in enumerate(documents):
            stub += (
                "@_overload\n"
                f"def parse(source: _ParseLiteral[{source!r}], "
                "no_location: _ParseLiteral[False] = False, "
                "max_tokens: None = None, "
                "experimental_fragment_arguments: _ParseLiteral[False] = False) "
                f"-> Parse{index}Document: ...\n"
            )
        stub += "@_overload\n"
    stub += fallback
    return ParseCompanion("from graphql_desk import parse as parse\n", stub)
