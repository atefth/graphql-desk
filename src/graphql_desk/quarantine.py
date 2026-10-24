"""Fail-closed typing quarantine for generated document declarations."""

import ast
from collections.abc import Mapping
from typing import cast

from .factory_generation import GeneratedFile
from .source_graph import DocumentIdentity


def _identity_key(value: object) -> tuple[str, str, str, int, int, int]:
    if isinstance(value, DocumentIdentity):
        module = value.module
        span = value.call_range
        return (
            module.project,
            module.module,
            module.uri,
            module.version,
            span.start,
            span.end,
        )
    if not isinstance(value, Mapping):
        raise ValueError("Invalid operation owner identity")
    identity = cast(Mapping[str, object], value)
    module_data = identity.get("module")
    span_data = identity.get("codepoint", identity.get("call_range"))
    if not isinstance(module_data, Mapping) or not isinstance(span_data, Mapping):
        raise ValueError("Invalid operation owner identity")
    module_mapping = cast(Mapping[str, object], module_data)
    location_mapping = cast(Mapping[str, object], span_data)
    project, name, uri, version = (
        module_mapping.get("project"),
        module_mapping.get("module"),
        module_mapping.get("uri"),
        module_mapping.get("version"),
    )
    start, end = location_mapping.get("start"), location_mapping.get("end")
    if (
        not all(isinstance(item, str) and item for item in (project, name, uri))
        or type(version) is not int
        or type(start) is not int
        or type(end) is not int
        or start < 0
        or end < start
    ):
        raise ValueError("Invalid operation owner identity")
    return cast(str, project), cast(str, name), cast(str, uri), version, start, end


def _offset(source: str, node: ast.AST, *, end: bool = False) -> int:
    line_number = getattr(node, "end_lineno" if end else "lineno", None)
    column = getattr(node, "end_col_offset" if end else "col_offset", None)
    if not isinstance(line_number, int) or not isinstance(column, int):
        raise ValueError("Generated declaration has no source range")
    lines = source.splitlines(keepends=True)
    if not 1 <= line_number <= len(lines):
        raise ValueError("Generated declaration range is invalid")
    prefix = sum(len(line) for line in lines[: line_number - 1])
    try:
        prefix += len(lines[line_number - 1].encode("utf-8")[:column].decode("utf-8"))
    except UnicodeDecodeError:
        raise ValueError("Generated declaration range is invalid") from None
    return prefix


def _base_name(node: ast.expr) -> str | None:
    value = node.value if isinstance(node, ast.Subscript) else node
    return value.id if isinstance(value, ast.Name) else None


def _generic_arguments(node: ast.expr, expected: int) -> list[ast.expr]:
    if not isinstance(node, ast.Subscript):
        raise ValueError("Unsupported generated document base")
    arguments = (
        list(node.slice.elts) if isinstance(node.slice, ast.Tuple) else [node.slice]
    )
    if len(arguments) != expected:
        raise ValueError("Unsupported generated document type parameters")
    return arguments


def _quarantine_class(source: str, tree: ast.Module, symbol: str) -> str:
    matches = [
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == symbol
    ]
    if len(matches) != 1:
        raise ValueError("Generated document declaration is missing or ambiguous")
    declaration = matches[0]
    fixture = [
        base
        for base in declaration.bases
        if _base_name(base) == "__graphql_desk_FixtureDocument"
    ]
    fragment = [
        base
        for base in declaration.bases
        if _base_name(base) == "__graphql_desk_FragmentDocument"
    ]
    if (
        len(fixture) != 1
        or len(fragment) > 1
        or len(declaration.bases) != 1 + len(fragment)
    ):
        raise ValueError("Unsupported generated document declaration bases")

    fixture_args = _generic_arguments(fixture[0], 3)
    fragment_args = _generic_arguments(fragment[0], 4) if fragment else []
    replacements: list[tuple[int, int, str]] = []
    for argument in fixture_args:
        if not isinstance(argument, ast.Name) or argument.id != "object":
            replacements.append(
                (
                    _offset(source, argument),
                    _offset(source, argument, end=True),
                    "object",
                )
            )
    for argument in fragment_args[1:]:
        if not isinstance(argument, ast.Name) or argument.id != "object":
            replacements.append(
                (
                    _offset(source, argument),
                    _offset(source, argument, end=True),
                    "object",
                )
            )
    if not replacements:
        return source
    for start, end, replacement in sorted(replacements, reverse=True):
        source = source[:start] + replacement + source[end:]
    return source


def quarantine_generated_stubs(
    files: tuple[GeneratedFile, ...],
    operations: tuple[dict[str, object], ...],
    invalidated_owners: tuple[object, ...],
) -> tuple[GeneratedFile, ...]:
    """Replace selected generated document type arguments with ``object``.

    Coalesced constructor declarations are quarantined together through their
    canonical owner. Runtime companions and document identity arguments are
    unchanged; an unsupported or unlocatable declaration raises ``ValueError``.
    """
    if not invalidated_owners:
        return files
    invalidated = {_identity_key(owner) for owner in invalidated_owners}
    selected = [
        operation
        for operation in operations
        if _identity_key(operation.get("owner")) in invalidated
    ]
    if not selected:
        raise ValueError("No operation provenance matches invalidated owners")
    canonical = {
        _identity_key(operation.get("canonical_owner") or operation.get("owner"))
        for operation in selected
    }
    declarations: set[tuple[str, str]] = set()
    for operation in operations:
        if (
            _identity_key(operation.get("canonical_owner") or operation.get("owner"))
            not in canonical
            or operation.get("status") != "precise"
        ):
            continue
        path = operation.get("declaration_path")
        symbol = operation.get("declaration_symbol")
        if not isinstance(path, str) or not isinstance(symbol, str):
            raise ValueError("Precise operation has no generated declaration owner")
        declarations.add((path, symbol))
    if not declarations:
        return files

    by_path: dict[str, set[str]] = {}
    for path, symbol in declarations:
        by_path.setdefault(path, set()).add(symbol)
    found: set[str] = set()
    result: list[GeneratedFile] = []
    for file in files:
        symbols = by_path.get(file.relative_path)
        if symbols is None:
            result.append(file)
            continue
        if not file.relative_path.endswith(".pyi"):
            raise ValueError("Quarantine target is not a typing stub")
        text = file.text
        tree = ast.parse(text)
        for symbol in sorted(symbols):
            text = _quarantine_class(text, tree, symbol)
            tree = ast.parse(text)
            found.add(file.relative_path + "\0" + symbol)
        result.append(GeneratedFile(file.relative_path, text))
    missing = {path + "\0" + symbol for path, symbol in declarations} - found
    if missing:
        raise ValueError("Generated document declaration file is missing")
    return tuple(result)
