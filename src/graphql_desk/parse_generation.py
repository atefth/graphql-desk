"""Pure parser companions; callers own any publication."""

from dataclasses import dataclass

from graphql import DocumentNode, FragmentDefinitionNode, OperationDefinitionNode, parse

from .literal_ast import render_document_ast_types


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
    documents: list[tuple[str, DocumentNode]] = []
    for source in sorted(set(sources)):
        document = parse(source)
        if all(
            isinstance(node, (OperationDefinitionNode, FragmentDefinitionNode))
            for node in document.definitions
        ):
            documents.append((source, document))
    fallback = (
        "def parse(source: str | _Source, no_location: bool = False, "
        "max_tokens: int | None = None, "
        "experimental_fragment_arguments: bool = False) -> _DocumentNode: ...\n"
    )
    stub = "from graphql import DocumentNode as _DocumentNode, Source as _Source\n"
    if documents:
        stub += "from typing import Literal as _ParseLiteral, overload as _overload\n"
        for index, (_, document) in enumerate(documents):
            stub += render_document_ast_types(document, name=f"Parse{index}") + "\n"
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
