"""Byte-compatible persisted-document normalization and editor IDs."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from dataclasses import replace

from graphql import (
    FragmentDefinitionNode,
    Visitor,
    parse,
    print_ast,
    visit,
)


class _StripUnmask(Visitor):
    def enter_fragment_definition(
        self, node: FragmentDefinitionNode, *_args: object
    ) -> FragmentDefinitionNode:
        directives = tuple(
            directive
            for directive in (node.directives or ())
            if directive.name.value != "_unmask"
        )
        return replace(node, directives=directives)


def _printed_fragments(sources: Iterable[str]) -> tuple[str, ...]:
    printed: list[str] = []
    seen: set[str] = set()
    names: dict[str, str] = {}
    for source in sources:
        document = visit(parse(source), _StripUnmask())
        for definition in document.definitions:
            if not isinstance(definition, FragmentDefinitionNode):
                raise ValueError("Composed persisted dependencies must be fragments")
            text = print_ast(definition)
            previous = names.get(definition.name.value)
            if previous is not None and previous != text:
                raise ValueError(
                    f"Conflicting fragment definitions: {definition.name.value!r}"
                )
            names[definition.name.value] = text
            if text not in seen:
                seen.add(text)
                printed.append(text)
    return tuple(printed)


def editor_hash_input(source: str, fragments: Iterable[str] = ()) -> str:
    """Print source plus already-resolved transitive fragments in caller order."""
    combined = source + "".join(f"\n\n{item}" for item in _printed_fragments(fragments))
    return print_ast(parse(combined))


def editor_sha256_id(source: str, fragments: Iterable[str] = ()) -> str:
    text = editor_hash_input(source, fragments)
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def manifest_document_bytes(
    source: str,
    fragments: Iterable[str] = (),
    *,
    normalize: bool = True,
) -> str:
    """Return gql.tada's manifest bytes from ordered, resolved fragments."""
    document = parse(source)
    base = print_ast(visit(document, _StripUnmask())) if normalize else source
    return base + "".join(f"\n\n{item}" for item in _printed_fragments(fragments))
