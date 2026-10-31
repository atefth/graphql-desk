"""Deterministic project graph facts for the experimental scan command."""

from __future__ import annotations

import hashlib
import os
from collections import defaultdict, deque
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from graphql import (
    FragmentDefinitionNode,
    GraphQLError,
    OperationDefinitionNode,
    parse,
    print_ast,
)
from graphql.language import FragmentSpreadNode, Node, Visitor, visit

from .fragments import FragmentIndex
from .source import Span
from .source_graph import (
    DocumentIdentity,
    GraphDiagnostic,
    ModuleDependency,
    ModuleSnapshot,
    ProjectSources,
    resolve_module_imports,
)
from .source_maps import SourceMap, build_source_map


@dataclass(frozen=True)
class ScanLocation:
    project: str
    module: str
    uri: str
    line: int
    column: int


@dataclass(frozen=True)
class ScanDefinition:
    id: str
    kind: str
    name: str | None
    project: str
    schema_name: str
    module: str
    locations: tuple[ScanLocation, ...]
    variables: tuple[tuple[str, str, str | None], ...]
    fragment_spreads: tuple[str, ...]
    hash: str
    type_condition: str | None = None


@dataclass(frozen=True)
class ScanModule:
    project: str
    name: str
    uri: str
    path: str
    area: str
    entrypoint: bool
    distance: int | None
    imports: tuple[str, ...]
    imported_by: tuple[str, ...]
    reverse_reachable: tuple[str, ...]


@dataclass(frozen=True)
class ScanGraph:
    definitions: tuple[ScanDefinition, ...]
    modules: tuple[ScanModule, ...]
    diagnostics: tuple[GraphDiagnostic, ...]


def _line_column(source: str, offset: int) -> tuple[int, int]:
    prefix = source[:offset]
    line = prefix.count("\n") + prefix.count("\r") - prefix.count("\r\n") + 1
    previous = max(prefix.rfind("\n"), prefix.rfind("\r"))
    return line, offset - previous


def _source_locations(
    source_map: SourceMap,
    span: Span,
    snapshots: Mapping[ModuleDependency, ModuleSnapshot],
) -> tuple[ScanLocation, ...]:
    by_module: dict[ModuleDependency, list[Span]] = defaultdict(list)
    module_order: list[ModuleDependency] = []
    for host in source_map.to_host(span):
        if host.module not in by_module:
            module_order.append(host.module)
        by_module[host.module].append(host.range)
    locations: list[ScanLocation] = []
    for module in module_order:
        ranges = by_module[module]
        snapshot = snapshots.get(module)
        if snapshot is None:
            continue
        merged: list[Span] = []
        for item in sorted(ranges, key=lambda value: (value.start, value.end)):
            if merged and item.start <= merged[-1].end:
                merged[-1] = Span(merged[-1].start, max(merged[-1].end, item.end))
            else:
                merged.append(item)
        for item in merged:
            line, column = _line_column(snapshot.source, item.start)
            locations.append(
                ScanLocation(module.project, module.module, module.uri, line, column)
            )
    return tuple(locations)


def _direct_spreads(node: Node) -> tuple[str, ...]:
    class Spreads(Visitor):
        def __init__(self) -> None:
            super().__init__()
            self.names: list[str] = []

        def enter_fragment_spread(
            self, spread: FragmentSpreadNode, *_args: object
        ) -> None:
            self.names.append(spread.name.value)

    visitor = Spreads()
    visit(node, visitor)
    return tuple(visitor.names)


def _variables(
    node: OperationDefinitionNode,
) -> tuple[tuple[str, str, str | None], ...]:
    values: list[tuple[str, str, str | None]] = []
    for definition in node.variable_definitions or ():
        default = (
            print_ast(definition.default_value)
            if definition.default_value is not None
            else None
        )
        values.append(
            (
                definition.variable.name.value,
                print_ast(definition.type),
                default,
            )
        )
    return tuple(values)


def _definition_base(item: ScanDefinition) -> str:
    name = item.name or "anonymous"
    return f"{item.project}:{item.schema_name}:{item.kind}:{name}"


def _assign_ids(items: list[ScanDefinition]) -> tuple[ScanDefinition, ...]:
    ordered = sorted(
        items,
        key=lambda item: (
            item.schema_name,
            item.kind,
            item.name or "",
            item.module,
            tuple((loc.uri, loc.line, loc.column) for loc in item.locations),
            item.hash,
        ),
    )
    counts: dict[str, int] = defaultdict(int)
    result: list[ScanDefinition] = []
    for item in ordered:
        base = _definition_base(item)
        counts[base] += 1
        suffix = counts[base]
        result.append(
            ScanDefinition(
                id=base if suffix == 1 else f"{base}~{suffix}",
                kind=item.kind,
                name=item.name,
                project=item.project,
                schema_name=item.schema_name,
                module=item.module,
                locations=item.locations,
                variables=item.variables,
                fragment_spreads=item.fragment_spreads,
                hash=item.hash,
                type_condition=item.type_condition,
            )
        )
    return tuple(result)


def _module_facts(
    modules: tuple[ModuleSnapshot, ...],
    paths: Mapping[tuple[str, str], Path],
    roots: Mapping[str, Path],
    source_roots: Mapping[str, tuple[Path, ...]],
    entrypoints: Mapping[str, tuple[str, ...] | None],
) -> tuple[ScanModule, ...]:
    identities = {(item.project, item.module): item for item in modules}
    relative_paths: dict[tuple[str, str], str] = {}
    areas: dict[tuple[str, str], str] = {}
    path_owners: dict[tuple[str, str], tuple[str, str]] = {}
    for key, snapshot in identities.items():
        root = roots.get(snapshot.project)
        source_path = paths.get(key)
        if root is None or source_path is None:
            raise ValueError("Every scan module needs a project root and selected path")
        resolved_root = root.resolve()
        resolved_path = (
            (resolved_root / source_path).resolve()
            if not source_path.is_absolute()
            else source_path.resolve()
        )
        if not resolved_path.is_relative_to(resolved_root):
            raise ValueError("Selected module path escapes its project root")
        relative_path = resolved_path.relative_to(resolved_root).as_posix()
        path_identity = (snapshot.project, os.path.normcase(str(resolved_path)))
        previous = path_owners.get(path_identity)
        if previous is not None and previous != key:
            raise ValueError("Distinct modules cannot share one selected path")
        path_owners[path_identity] = key
        relative_paths[key] = relative_path
        parent = Path(relative_path).parent.as_posix()
        areas[key] = "(root)" if parent == "." else parent

    edges = resolve_module_imports(modules)
    outgoing: dict[tuple[str, str], set[tuple[str, str]]] = defaultdict(set)
    incoming: dict[tuple[str, str], set[tuple[str, str]]] = defaultdict(set)
    for edge in edges:
        source = (edge.importer.project, edge.importer.module)
        target = (edge.imported.project, edge.imported.module)
        if source in identities and target in identities:
            outgoing[source].add(target)
            incoming[target].add(source)

    projects = {item.project for item in modules}
    if set(entrypoints or ()) - projects:
        raise ValueError("Configured entrypoint project is unknown")
    entries: set[tuple[str, str]] = set()
    for project in projects:
        configured = entrypoints.get(project)
        if configured is None:
            entries.update(
                key for key in identities if key[0] == project and not incoming[key]
            )
            continue
        project_root = roots[project].resolve()
        by_path: dict[str, set[tuple[str, str]]] = defaultdict(set)
        for key, relative_path in relative_paths.items():
            if key[0] != project:
                continue
            source_path = Path(relative_path)
            for configured_root in source_roots.get(project, (Path("."),)):
                source_root = (
                    configured_root.resolve().relative_to(project_root)
                    if configured_root.is_absolute()
                    else configured_root
                )
                if source_path.is_relative_to(source_root):
                    by_path[source_path.relative_to(source_root).as_posix()].add(key)
        for entrypoint in configured:
            normalized = Path(entrypoint).as_posix()
            matches = by_path.get(normalized, set())
            if len(matches) != 1:
                raise ValueError("Configured entrypoint is unknown or excluded")
            entry_key = next(iter(matches))
            if entry_key in entries:
                raise ValueError("Configured entrypoints must be unique")
            entries.add(entry_key)

    distances: dict[tuple[str, str], int] = {}
    for entry in sorted(entries):
        distances[entry] = 0
        queue = deque([entry])
        while queue:
            current = queue.popleft()
            for target in sorted(outgoing[current]):
                candidate = distances[current] + 1
                if target not in distances or candidate < distances[target]:
                    distances[target] = candidate
                    queue.append(target)

    records: list[ScanModule] = []
    for key, snapshot in sorted(identities.items()):
        reached: set[tuple[str, str]] = {key}
        queue = deque([key])
        while queue:
            current = queue.popleft()
            for importer in sorted(incoming[current]):
                if importer not in reached:
                    reached.add(importer)
                    queue.append(importer)
        records.append(
            ScanModule(
                project=snapshot.project,
                name=snapshot.module,
                uri=snapshot.uri,
                path=relative_paths[key],
                area=areas[key],
                entrypoint=key in entries,
                distance=distances.get(key),
                imports=tuple(
                    sorted(name for project, name in outgoing[key] if project == key[0])
                ),
                imported_by=tuple(
                    sorted(name for project, name in incoming[key] if project == key[0])
                ),
                reverse_reachable=tuple(
                    sorted(name for project, name in reached if project == key[0])
                ),
            )
        )
    return tuple(records)


def build_scan_graph(
    sources: ProjectSources,
    index: FragmentIndex,
    modules: tuple[ModuleSnapshot, ...],
    *,
    paths: Mapping[tuple[str, str], Path],
    roots: Mapping[str, Path],
    source_roots: Mapping[str, tuple[Path, ...]] | None = None,
    entrypoints: Mapping[str, tuple[str, ...] | None] | None = None,
) -> ScanGraph:
    """Build schema-qualified definition and Python import graph facts."""
    snapshots = {
        ModuleDependency(item.project, item.module, item.uri, item.version): item
        for item in modules
    }
    indexed = {
        DocumentIdentity(item.call.module, item.call.range): item
        for item in index.documents
    }
    definitions: list[ScanDefinition] = []
    origins: list[tuple[ScanDefinition, ModuleDependency, Span, str]] = []
    diagnostics = [*sources.diagnostics, *index.diagnostics]
    for call in sources.calls:
        indexed_document = indexed.get(DocumentIdentity(call.module, call.range))
        if indexed_document is None:
            continue
        if indexed_document.call != call:
            diagnostics.append(
                GraphDiagnostic(
                    call.module,
                    "stale-scan-index",
                    "Indexed source facts do not match the current call snapshot",
                    call.range,
                )
            )
            continue
        try:
            document = parse(call.source)
            source_map = build_source_map(call, modules)
        except (GraphQLError, RecursionError, TypeError, ValueError) as error:
            diagnostics.append(
                GraphDiagnostic(
                    call.module,
                    "scan-source-unavailable",
                    str(error),
                    call.range,
                )
            )
            continue
        for node in document.definitions:
            if not isinstance(node, (OperationDefinitionNode, FragmentDefinitionNode)):
                continue
            if node.loc is None:
                continue
            kind = (
                node.operation.value
                if isinstance(node, OperationDefinitionNode)
                else "fragment"
            )
            name = node.name.value if node.name is not None else None
            definition_span = Span(node.loc.start, node.loc.end)
            locations = _source_locations(source_map, definition_span, snapshots)
            host_ranges = source_map.to_host(definition_span)
            definitions.append(
                ScanDefinition(
                    id="",
                    kind=kind,
                    name=name,
                    project=call.module.project,
                    schema_name=indexed_document.schema_identity[1],
                    module=call.module.module,
                    locations=locations,
                    variables=_variables(node)
                    if isinstance(node, OperationDefinitionNode)
                    else (),
                    fragment_spreads=_direct_spreads(node),
                    hash=hashlib.sha256(print_ast(node).encode("utf-8")).hexdigest()[
                        :16
                    ],
                    type_condition=(
                        node.type_condition.name.value
                        if isinstance(node, FragmentDefinitionNode)
                        else None
                    ),
                )
            )
            if host_ranges:
                origins.append(
                    (
                        definitions[-1],
                        host_ranges[0].module,
                        host_ranges[0].range,
                        print_ast(node),
                    )
                )
    fragments: dict[tuple[str, str, str], list[tuple[ModuleDependency, Span, str]]] = (
        defaultdict(list)
    )
    for definition, module, span, printed in origins:
        if definition.kind == "fragment" and definition.name is not None:
            fragments[
                (definition.project, definition.schema_name, definition.name)
            ].append((module, span, printed))
    for (_, schema_name, fragment_name), origins_for_fragment in sorted(
        fragments.items()
    ):
        if len({printed for _, _, printed in origins_for_fragment}) > 1:
            for module, span, _ in origins_for_fragment:
                diagnostics.append(
                    GraphDiagnostic(
                        module,
                        "ambiguous-fragment-name",
                        "Conflicting fragment definitions named "
                        f"{fragment_name!r} in schema {schema_name!r}",
                        span,
                    )
                )
    ordered_diagnostics = tuple(
        sorted(
            dict.fromkeys(diagnostics),
            key=lambda item: (
                item.module.project,
                item.module.module,
                item.range.start,
                item.code,
                item.message,
            ),
        )
    )
    return ScanGraph(
        _assign_ids(definitions),
        _module_facts(
            modules,
            paths,
            roots,
            {} if source_roots is None else source_roots,
            {} if entrypoints is None else entrypoints,
        ),
        ordered_diagnostics,
    )
