"""Read-only project scanning with the full experimental rule set."""

from __future__ import annotations

import os
import tokenize
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path

from graphql import GraphQLSchema

from .config import ProjectConfig
from .fragments import SchemaContext, index_fragments
from .project import (
    effective_project,
    project_bindings,
    select_schema,
    validate_project_ownership,
)
from .scan_complexity_rules import operation_complexity_rule
from .scan_deprecation_rules import deprecated_usage_rule
from .scan_depth_rules import fetch_depth_rule
from .scan_directive_rules import directive_usage_rule
from .scan_fragment_rules import cross_feature_fragment_rule, orphan_fragment_rule
from .scan_graph import build_scan_graph
from .scan_input_rules import input_usage_rule
from .scan_reporting import ScanReport, build_scan_report
from .scan_rules import field_usage_rule
from .schema_loading import load_local_schema
from .schema_url import load_from_url
from .source_graph import (
    GraphDiagnostic,
    ModuleSnapshot,
    ProjectSources,
    resolve_project,
)
from .source_selection import (
    SourceFile,
    project_python_candidates,
    select_project_sources,
)


def _warning(
    diagnostic: GraphDiagnostic,
    sources: Mapping[tuple[str, str, str, int], ModuleSnapshot],
    paths: Mapping[tuple[str, str], Path],
) -> dict[str, object]:
    identity = (
        diagnostic.module.project,
        diagnostic.module.module,
        diagnostic.module.uri,
        diagnostic.module.version,
    )
    source = sources.get(identity)
    before = source.source[: diagnostic.range.start] if source is not None else ""
    relative_path = paths.get(
        (diagnostic.module.project, diagnostic.module.module), Path()
    ).as_posix()
    line = before.count("\n") + 1
    column = len(before.rsplit("\n", 1)[-1].removesuffix("\r")) + 1
    return {
        "code": diagnostic.code,
        "message": diagnostic.message,
        "file": relative_path,
        "line": line,
        "column": column,
    }


def _source_path(
    source: SourceFile, config: ProjectConfig, roots: tuple[Path, ...]
) -> Path:
    path = source.path.resolve()
    root = config.root.resolve()
    try:
        return path.relative_to(root)
    except ValueError:
        source_root = source.source_root.resolve()
        index = roots.index(source_root) + 1
        return Path(f"external-source-root-{index}") / path.relative_to(source_root)


def _read_source(source: SourceFile) -> str:
    try:
        with tokenize.open(source.path) as stream:
            return stream.read()
    except (SyntaxError, UnicodeError):
        raise ValueError(
            "Python source files must use a supported text encoding"
        ) from None


def scan_project(
    *,
    config: Path | str | None = None,
    schema: str | None = None,
    environment: Mapping[str, str] | None = None,
) -> ScanReport:
    """Analyze one configured project without importing its Python modules."""
    selected = effective_project(
        Path.cwd(), explicit_config=Path(config) if config is not None else None
    )
    if selected is None:
        raise ValueError("No GraphQL Desk project configuration was found")
    project: ProjectConfig = selected.config
    validate_project_ownership((project,))
    project_id = "project"
    selected_schemas = (
        (select_schema(project, binding=schema),)
        if schema is not None
        else project.schemas
    )
    roots = tuple(path.resolve() for path in project.source_roots)
    files = select_project_sources(
        project,
        project_python_candidates(project),
        validate_entrypoints=True,
    )
    modules = tuple(
        ModuleSnapshot(
            project_id,
            source.module,
            source.path.as_uri(),
            1,
            _read_source(source),
            source.is_package,
        )
        for source in files
    )
    sources: ProjectSources = resolve_project(
        modules, bindings=(project_bindings(project, project_id=project_id),)
    )
    contexts: dict[tuple[str, str | None], SchemaContext] = {}
    by_name: dict[str, SchemaContext] = {}
    schema_environment = os.environ if environment is None else environment
    for schema_config in selected_schemas:
        loaded: GraphQLSchema = (
            load_local_schema(schema_config.origin.local_path)
            if schema_config.origin.local_path is not None
            else load_from_url(schema_config.origin, environment=schema_environment)
        )
        context = SchemaContext(
            schema_config.name, loaded, f"scan:{project_id}:{schema_config.name}"
        )
        by_name[schema_config.name] = context
        contexts[(project_id, schema_config.name)] = context
        if schema_config.name == "default":
            contexts[(project_id, None)] = context
    calls = tuple(
        call
        for call in sources.calls
        if (call.schema_name is None and (project_id, None) in contexts)
        or call.schema_name in by_name
    )
    selected_sources = replace(
        sources,
        calls=calls,
        persisted=tuple(
            item
            for item in sources.persisted
            if (item.schema_name is None and (project_id, None) in contexts)
            or item.schema_name in by_name
        ),
    )
    index = index_fragments(selected_sources, schemas=contexts)
    path_map = {
        (project_id, source.module): _source_path(source, project, roots)
        for source in files
    }
    graph = build_scan_graph(
        selected_sources,
        index,
        modules,
        paths=path_map,
        roots={project_id: project.root},
        source_roots={project_id: roots},
        entrypoints={project_id: project.entrypoints},
    )
    warnings_by_key = {
        (
            item.module.project,
            item.module.module,
            item.module.uri,
            item.module.version,
            item.code,
            item.range.start,
            item.range.end,
        ): item
        for item in graph.diagnostics
    }
    diagnostics = tuple(warnings_by_key[key] for key in sorted(warnings_by_key))
    source_by_identity = {
        (item.project, item.module, item.uri, item.version): item for item in modules
    }
    warnings = tuple(
        _warning(item, source_by_identity, path_map) for item in diagnostics
    )
    rules: dict[str, tuple[object, ...]] = {
        "field-usage": field_usage_rule(graph, index, contexts),
        "input-usage": input_usage_rule(graph, index, contexts),
        "deprecated-usage": deprecated_usage_rule(graph, index, contexts),
        "orphan-fragments": orphan_fragment_rule(graph),
        "cross-feature-fragments": cross_feature_fragment_rule(graph),
        "operation-complexity": operation_complexity_rule(graph, index, contexts),
        "fetch-depth": fetch_depth_rule(graph),
        "directive-usage": directive_usage_rule(graph, index),
    }
    return build_scan_report(graph, by_name, rules, warnings)
