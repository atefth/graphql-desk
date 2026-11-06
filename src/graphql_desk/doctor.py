"""Read-only setup checks; live editor authority belongs to the editor host."""

import ast
import sys
from collections.abc import Mapping
from importlib import metadata
from pathlib import Path
from typing import Literal, TypedDict

from graphql import GraphQLSchema

from .config import ProjectConfig, SchemaConfig
from .editor_analysis import EditorAnalysis
from .editor_snapshot import ConfigurationDiagnostic, EditorSnapshot
from .external_type_sources import (
    capture_external_type_sources,
    external_class_base_symbols,
    interpreter_type_roots,
)
from .project import (
    effective_project,
    find_project,
    select_schema,
    validate_project_ownership,
)
from .schema_loading import load_local_schema
from .schema_types import render_schema_types
from .schema_url import load_from_url
from .source_graph import ModuleSnapshot, TypeSymbolResolution, resolve_type_symbols
from .source_selection import select_project_sources


class DoctorCheck(TypedDict):
    name: str
    status: Literal["ready", "degraded", "unavailable"]
    message: str
    project: str | None


class DoctorRuntime(TypedDict):
    executable: str
    python_version: str
    graphql_desk_version: str | None
    mypy_version: str | None
    pyright_version: str | None


class DoctorReport(TypedDict):
    checks: list[DoctorCheck]
    runtime: DoctorRuntime
    exit_code: int


def _check(
    name: str,
    status: Literal["ready", "degraded", "unavailable"],
    message: str,
    project: str | None = None,
) -> DoctorCheck:
    return {"name": name, "status": status, "message": message, "project": project}


def _version(distribution: str) -> str | None:
    try:
        return metadata.version(distribution)
    except (metadata.PackageNotFoundError, OSError, ValueError):
        return None


def _report(checks: list[DoctorCheck]) -> DoctorReport:
    # Installed distributions are not evidence that an editor uses these checkers.
    return {
        "checks": checks,
        "runtime": {
            "executable": sys.executable,
            "python_version": ".".join(map(str, sys.version_info[:3])),
            "graphql_desk_version": _version("graphql-desk"),
            "mypy_version": _version("mypy"),
            "pyright_version": _version("pyright"),
        },
        "exit_code": int(any(item["status"] != "ready" for item in checks)),
    }


def _live_unavailable() -> list[DoctorCheck]:
    return [
        _check(
            "generated-ownership",
            "unavailable",
            "Run GraphQL Desk: Doctor in the editor to verify generated ownership.",
        ),
        _check(
            "active-editor",
            "unavailable",
            "No authoritative live editor session is attached to this report.",
        ),
        _check(
            "active-checker",
            "unavailable",
            "Installed checker distributions do not establish an active checker.",
        ),
        _check(
            "live-bridge",
            "unavailable",
            "Open the project in the trusted editor and run GraphQL Desk: Doctor.",
        ),
    ]


def _mapping_symbols(schema: SchemaConfig) -> tuple[str, ...]:
    return tuple(
        sorted(
            {
                symbol
                for mapping in schema.scalars
                for symbol in (mapping.input, mapping.output)
                if symbol != "typing.Any"
            }
            | {symbol for _, symbol in schema.enums if symbol != "typing.Any"}
        )
    )


def _resolve_local_mapping_symbols(
    config: ProjectConfig, symbols: tuple[str, ...]
) -> tuple[tuple[TypeSymbolResolution, ...], tuple[ModuleSnapshot, ...]]:
    if not symbols:
        return (), ()
    candidates: set[Path] = set()

    def add_module(module: str) -> None:
        parts = module.split(".")
        for root in config.source_roots:
            for suffix in (".pyi", ".py"):
                candidates.add(root.joinpath(*parts).with_suffix(suffix))
                candidates.add(root.joinpath(*parts, f"__init__{suffix}"))
                candidates.update(
                    root.joinpath(*parts[:length], f"__init__{suffix}")
                    for length in range(1, len(parts))
                )

    for symbol in symbols:
        add_module(symbol.rpartition(".")[0])

    sources_by_path: dict[Path, ModuleSnapshot] = {}
    supplied_modules: set[str] = set()
    while True:
        sources = select_project_sources(
            config,
            tuple(path for path in candidates if path.is_file()),
            workspace=config.root if config.source_scope != "explicit_cli" else None,
            include_stubs=True,
            validate_entrypoints=False,
        )
        added = False
        for source in sources:
            if source.path in sources_by_path:
                continue
            source_text = source.path.read_text(encoding="utf-8")
            sources_by_path[source.path] = ModuleSnapshot(
                config.path.as_uri(),
                source.module,
                source.path.as_uri(),
                1,
                source_text,
                source.is_package,
            )
            if source.module in supplied_modules:
                continue
            supplied_modules.add(source.module)
            try:
                tree = ast.parse(source_text)
            except (SyntaxError, ValueError, UnicodeError, RecursionError):
                continue
            for statement in tree.body:
                if isinstance(statement, ast.Import):
                    imported_modules = tuple(alias.name for alias in statement.names)
                elif isinstance(statement, ast.ImportFrom):
                    package = source.module.split(".")
                    if not source.is_package:
                        package.pop()
                    if statement.level:
                        if statement.level > len(package):
                            continue
                        base = package[: len(package) - statement.level + 1]
                        imported_name = ".".join(
                            [*base, *([statement.module] if statement.module else [])]
                        )
                    else:
                        imported_name = statement.module or ""
                    imported_modules = (
                        (imported_name,) if imported_name else ()
                    ) + tuple(
                        f"{imported_name}.{alias.name}"
                        for alias in statement.names
                        if imported_name and alias.name != "*"
                    )
                else:
                    continue
                for module in imported_modules:
                    add_module(module)
                    added = True
        if not added:
            break
    modules = tuple(sources_by_path.values())
    return resolve_type_symbols(
        symbols, modules=modules, project_id=config.path.as_uri()
    ), modules


def _resolve_interpreter_mapping_symbols(
    config: ProjectConfig,
    symbols: tuple[str, ...],
    local_modules: tuple[ModuleSnapshot, ...],
) -> tuple[TypeSymbolResolution, ...]:
    roots = interpreter_type_roots(
        sys.path, excluded_roots=(config.root, *config.source_roots)
    )
    project_id = config.path.as_uri() + "#interpreter"
    modules = capture_external_type_sources(
        symbols,
        search_roots=roots,
        project_id=project_id,
        shadow_roots=(config.root, *config.source_roots),
    )
    base_symbols = external_class_base_symbols(local_modules)
    if base_symbols:
        try:
            external_bases = capture_external_type_sources(
                base_symbols,
                search_roots=roots,
                project_id=project_id,
                shadow_roots=(config.root, *config.source_roots),
            )
        except (OSError, RuntimeError, ValueError):
            external_bases = ()
        captured = {module.module for module in modules}
        modules = (
            *modules,
            *(item for item in external_bases if item.module not in captured),
        )
    local_project = config.path.as_uri()
    local_module_names = {module.module for module in local_modules}
    type_modules = (
        *local_modules,
        *(
            ModuleSnapshot(
                local_project,
                module.module,
                module.uri,
                module.version,
                module.source,
                module.is_package,
            )
            for module in modules
            if module.module not in local_module_names
        ),
    )
    return resolve_type_symbols(symbols, modules=type_modules, project_id=local_project)


def _schema_checks(
    config: ProjectConfig,
    schema: SchemaConfig,
    loaded: GraphQLSchema | None,
    resolutions: tuple[TypeSymbolResolution, ...] | None,
) -> list[DoctorCheck]:
    project = config.path.as_uri()
    name = f"schema.{schema.name}"
    if loaded is None:
        return [
            _check(
                name,
                "unavailable",
                "Check schema contents, access and configured header environment.",
                project,
            )
        ]
    checks = [_check(name, "ready", "Schema is available.", project)]
    try:
        render_schema_types(loaded, scalars=schema.scalars, enums=schema.enums)
    except (ValueError, TypeError, RecursionError):
        checks.append(
            _check(
                name + ".declarations",
                "unavailable",
                "Check scalar and enum mappings and schema declarations.",
                project,
            )
        )
    else:
        checks.append(
            _check(
                name + ".declarations",
                "ready",
                "Mapping declarations are valid.",
                project,
            )
        )
    for scalar in schema.scalars:
        for direction, symbol in (("input", scalar.input), ("output", scalar.output)):
            if symbol == "typing.Any":
                checks.append(
                    _check(
                        name + ".explicit-any",
                        "ready",
                        f"explicit Any scalar {direction} mapping {scalar.name}",
                        project,
                    )
                )
    for enum, symbol in schema.enums:
        if symbol == "typing.Any":
            checks.append(
                _check(
                    name + ".explicit-any",
                    "ready",
                    f"explicit Any enum mapping {enum}",
                    project,
                )
            )
    if not _mapping_symbols(schema):
        checks.append(
            _check(
                name + ".symbols",
                "ready",
                "No source-visible mapping symbols are configured.",
                project,
            )
        )
    elif resolutions is None:
        checks.append(
            _check(
                name + ".symbols",
                "unavailable",
                "Mapping source capture failed; check readable type declarations.",
                project,
            )
        )
    else:
        unavailable = sum(item.status != "resolved" for item in resolutions)
        checks.append(
            _check(
                name + ".symbols",
                "degraded" if unavailable else "ready",
                "Mapping symbols resolved statically: "
                f"{len(resolutions) - unavailable}; unverified: {unavailable}. "
                "Static names do not prove assignability or runtime identity.",
                project,
            )
        )
    return checks


def _symbol_checks(
    config: ProjectConfig, schema: SchemaConfig
) -> tuple[tuple[TypeSymbolResolution, ...] | None, list[DoctorCheck]]:
    symbols = _mapping_symbols(schema)
    if not symbols:
        return (), []
    project = config.path.as_uri()
    name = f"schema.{schema.name}.symbols"
    checks: list[DoctorCheck] = []
    try:
        local, modules = _resolve_local_mapping_symbols(config, symbols)
    except (OSError, ValueError, RuntimeError, UnicodeError):
        return None, [
            _check(
                name + ".local",
                "unavailable",
                "local mapping symbols unavailable; source capture failed",
                project,
            )
        ]
    local_by_symbol = {item.symbol: item for item in local}
    by_symbol = dict(local_by_symbol)
    resolved = sum(item.status == "resolved" for item in local)
    if resolved:
        checks.append(
            _check(
                name + ".local",
                "ready",
                "captured local mapping symbols resolved (static names only): "
                + str(resolved),
                project,
            )
        )
    if resolved != len(symbols):
        try:
            external = _resolve_interpreter_mapping_symbols(config, symbols, modules)
        except (OSError, ValueError, RuntimeError, UnicodeError):
            checks.append(
                _check(
                    name + ".interpreter",
                    "unavailable",
                    "interpreter mapping symbols unavailable; source capture failed",
                    project,
                )
            )
        else:
            for item in external:
                if local_by_symbol[item.symbol].status != "resolved":
                    by_symbol[item.symbol] = item
            count = sum(
                by_symbol[symbol].status == "resolved"
                and local_by_symbol[symbol].status != "resolved"
                for symbol in symbols
            )
            if count:
                checks.append(
                    _check(
                        name + ".interpreter",
                        "ready",
                        "interpreter mapping symbols resolved (static names only): "
                        + str(count),
                        project,
                    )
                )
            count = sum(by_symbol[symbol].status == "external" for symbol in symbols)
            if count:
                checks.append(
                    _check(
                        name + ".external",
                        "degraded",
                        f"external mapping symbols unverified: {count}",
                        project,
                    )
                )
            unavailable = [
                by_symbol[symbol]
                for symbol in symbols
                if local_by_symbol[symbol].status == "external"
                and by_symbol[symbol].status not in {"resolved", "external"}
            ]
            if unavailable:
                checks.append(
                    _check(
                        name + ".interpreter",
                        "unavailable",
                        "interpreter mapping symbols unavailable: "
                        + _symbol_statuses(unavailable),
                        project,
                    )
                )
    unavailable = [
        by_symbol[symbol]
        for symbol in symbols
        if local_by_symbol[symbol].status not in {"resolved", "external"}
        and by_symbol[symbol].status not in {"resolved", "external"}
    ]
    if unavailable:
        checks.append(
            _check(
                name + ".local",
                "unavailable",
                "captured local mapping symbols unavailable: "
                + _symbol_statuses(unavailable),
                project,
            )
        )
    return tuple(by_symbol[symbol] for symbol in symbols), checks


def _symbol_statuses(resolutions: list[TypeSymbolResolution]) -> str:
    statuses: dict[str, int] = {}
    for item in resolutions:
        statuses[item.status] = statuses.get(item.status, 0) + 1
    return ", ".join(f"{status}={count}" for status, count in sorted(statuses.items()))


def collect_project_health(
    directory: Path,
    *,
    config_path: Path | None = None,
    schema_name: str | None = None,
    environment: Mapping[str, str],
) -> DoctorReport:
    """Explicitly inspect saved setup, without executing project Python or writing."""
    checks: list[DoctorCheck] = []
    try:
        selected = effective_project(directory, explicit_config=config_path)
        if selected is None:
            raise ValueError("Missing configuration")
        config = selected.config
        ancestor = (
            find_project(config.root.parent)
            if config.root.parent != config.root
            else None
        )
        validate_project_ownership(
            (config,) if ancestor is None else (ancestor, config)
        )
        schemas = (
            (select_schema(config, binding=schema_name),)
            if schema_name is not None
            else config.schemas
        )
    except (OSError, ValueError, RuntimeError):
        checks.append(
            _check(
                "configuration",
                "unavailable",
                "Configure [tool.graphql-desk] and check the selected "
                "configuration and schema name.",
            )
        )
        return _report([*checks, *_live_unavailable()])
    checks.append(
        _check(
            "configuration",
            "ready",
            f"ready ({selected.origin})",
            config.path.as_uri(),
        )
    )
    checks.append(
        _check(
            "project-ownership",
            "ready",
            "Selected and nearest ancestor generated paths do not conflict.",
            config.path.as_uri(),
        )
    )
    for schema in schemas:
        try:
            loaded = (
                load_local_schema(schema.origin.local_path)
                if schema.origin.local_path is not None
                else load_from_url(schema.origin, environment=environment)
            )
        except (OSError, ValueError, RuntimeError):
            loaded = None
        resolutions = None
        symbol_checks: list[DoctorCheck] = []
        if loaded is not None:
            resolutions, symbol_checks = _symbol_checks(config, schema)
        checks.extend(_schema_checks(config, schema, loaded, resolutions))
        checks.extend(symbol_checks)
    return _report([*checks, *_live_unavailable()])


def terminal_health_report(report: DoctorReport) -> str:
    """Format shared saved checks without claiming terminal editor authority."""
    lines: list[str] = []
    for check in report["checks"]:
        name, status, message = check["name"], check["status"], check["message"]
        if name == "configuration" and status == "ready":
            lines.append(f"configuration: {message}")
        elif name.startswith("schema."):
            _, schema, *suffix = name.split(".")
            label = f"schema {schema}: "
            if not suffix:
                lines.append(
                    label + status + ("; " + message if status != "ready" else "")
                )
            elif suffix == ["declarations"]:
                lines.append(
                    label
                    + "mapping declarations "
                    + status
                    + ("; " + message if status != "ready" else "")
                )
            elif suffix == ["symbols"] and message.startswith("No source-visible"):
                lines.append(label + "mapping symbols not configured")
            else:
                lines.append(label + message)
        else:
            lines.append(name.replace("-", " ") + ": " + status + "; " + message)
    runtime = report["runtime"]
    lines.append(
        f"interpreter: Python {runtime['python_version']} ({runtime['executable']})"
    )
    for checker in ("mypy", "pyright"):
        version = (
            runtime["mypy_version"] if checker == "mypy" else runtime["pyright_version"]
        )
        lines.append(
            f"installed {checker} distribution: "
            + (version or "unavailable (distribution metadata not found)")
        )
    lines.append(
        "status: degraded; editor/bridge and generated ownership remain unverified"
    )
    return "\n".join(lines)


def collect_snapshot_health(
    snapshot: EditorSnapshot,
    analysis: EditorAnalysis,
    *,
    configuration_diagnostics: tuple[ConfigurationDiagnostic, ...],
) -> DoctorReport:
    """Inspect caller-bound captured facts; the caller must prove their freshness."""
    checks: list[DoctorCheck] = []
    if analysis.response.get("request_id") != snapshot.request.get("request_id"):
        checks.append(
            _check(
                "configuration",
                "unavailable",
                "Editor snapshot and analysis do not match; refresh the project.",
            )
        )
        return _report([*checks, *_live_unavailable()])
    try:
        if not snapshot.project_configs:
            raise ValueError("No configured project")
        validate_project_ownership(snapshot.project_configs)
    except (OSError, ValueError, RuntimeError):
        checks.append(
            _check(
                "configuration",
                "unavailable",
                "Check the captured project configuration and generated ownership.",
            )
        )
        return _report([*checks, *_live_unavailable()])
    if configuration_diagnostics:
        checks.append(
            _check(
                "configuration",
                "unavailable",
                "Repair the editor's GraphQL Desk configuration diagnostics.",
            )
        )
    for config in snapshot.project_configs:
        project = config.path.as_uri()
        checks.append(
            _check(
                "configuration",
                "ready",
                "Effective editor configuration is captured.",
                project,
            )
        )
        checks.append(
            _check(
                "project-ownership",
                "ready",
                "Captured projects have distinct generated ownership.",
                project,
            )
        )
        local = tuple(item for item in analysis.modules if item.project == project)
        local_names = {item.module for item in local}
        modules = (
            *local,
            *(
                ModuleSnapshot(
                    project,
                    item.module,
                    item.uri,
                    item.version,
                    item.source,
                    item.is_package,
                )
                for item in snapshot.external_mapping_modules
                if item.project == project + "#interpreter"
                and item.module not in local_names
            ),
        )
        for schema in config.schemas:
            context = analysis.schemas.get((project, schema.name))
            symbols = _mapping_symbols(schema)
            resolutions = resolve_type_symbols(
                symbols, modules=modules, project_id=project
            )
            checks.extend(
                _schema_checks(
                    config,
                    schema,
                    context.schema
                    if context
                    and (project, schema.name) not in snapshot.unavailable_schemas
                    else None,
                    resolutions,
                )
            )
    return _report([*checks, *_live_unavailable()])
