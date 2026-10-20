"""Render annotation-only refinements of literal executable GraphQL ASTs."""

import keyword
import unicodedata
from dataclasses import fields
from typing import cast

from graphql import (
    DocumentNode,
    FragmentDefinitionNode,
    Node,
    OperationDefinitionNode,
    OperationType,
    VariableDefinitionNode,
    parse,
)
from graphql.language.ast import Location


def render_literal_ast_types(
    source: str, *, name: str, no_location: bool = False
) -> str:
    """Return .pyi declarations exposing ``<name>Document``.

    These are checker-only refinements, not runtime classes or parse overloads.
    Locations retain ordinary mutable metadata; their presence alone is refined.
    Parsing does not imply schema validity. Type-system definitions are unsupported.
    """
    try:
        document = parse(source, no_location=no_location)
    except RecursionError:
        raise ValueError("Literal AST exceeds declaration nesting limits") from None
    return render_document_ast_types(document, name=name)


def render_document_ast_types(document: DocumentNode, *, name: str) -> str:
    """Refine an existing executable AST without reparsing or normalizing it.

    These declarations describe the supplied nodes and location presence exactly;
    they do not construct runtime subclasses or change mutable location metadata.
    """
    if (
        not name.isidentifier()
        or keyword.iskeyword(name)
        or name.startswith("__")
        or unicodedata.normalize("NFKC", name) != name
    ):
        raise ValueError(f"Unsafe Python declaration name: {name!r}")
    if any(
        not isinstance(definition, (OperationDefinitionNode, FragmentDefinitionNode))
        for definition in document.definitions
    ):
        raise ValueError("Only executable GraphQL definitions are supported")
    declarations: list[str] = []
    names: dict[int, str] = {}

    def annotation(value: object, *, constant: bool = False) -> str:
        if isinstance(value, Node):
            return declaration(value, constant=constant)
        if isinstance(value, Location):
            return "_ast.Location"
        if isinstance(value, OperationType):
            return f"_Literal[_ast.OperationType.{value.name}]"
        if value is None:
            return "None"
        if isinstance(value, (str, bool, int)):
            return f"_Literal[{value!r}]"
        if isinstance(value, tuple):
            items = cast(tuple[object, ...], value)
            return (
                "tuple["
                + ", ".join(annotation(item, constant=constant) for item in items)
                + "]"
                if items
                else "tuple[()]"
            )
        raise ValueError(f"Unsupported AST attribute type: {type(value).__name__}")

    def declaration(node: Node, *, constant: bool = False) -> str:
        identity = id(node)
        if identity in names:
            return names[identity]
        class_name = (
            f"{name}Document"
            if isinstance(node, DocumentNode)
            else f"_{name}Node{len(names)}"
        )
        names[identity] = class_name
        attributes = [f"    kind: _ClassVar[_Literal[{node.kind!r}]]"]
        for attribute in fields(node):
            value: object = getattr(node, attribute.name)
            const_attribute = constant or (
                isinstance(node, VariableDefinitionNode)
                and attribute.name in ("default_value", "directives")
            )
            attributes.append(
                f"    {attribute.name}: {annotation(value, constant=const_attribute)}"
            )
        base = type(node).__name__
        # The parser uses ordinary node classes even in const-annotated positions.
        if constant and base in {
            "ListValueNode",
            "ObjectValueNode",
            "ObjectFieldNode",
            "DirectiveNode",
            "ArgumentNode",
        }:
            base = f"Const{base}"
        declarations.append(
            f"class {class_name}(_ast.{base}):\n" + "\n".join(attributes)
        )
        return class_name

    try:
        declaration(document)
    except RecursionError:
        raise ValueError("Literal AST exceeds declaration nesting limits") from None
    return (
        '"""Checker-only literal AST declarations; no runtime class identity."""\n\n'
        "from typing import ClassVar as _ClassVar, Literal as _Literal\n"
        "from graphql.language import ast as _ast\n\n"
        + "\n\n".join(declarations)
        + "\n"
    )
