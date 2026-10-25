"""Explicit read-only diagnostics; importing this module performs no work."""

import argparse
import ast
import os
import sys
from collections.abc import Mapping, Sequence
from importlib import metadata
from pathlib import Path
from typing import NoReturn

from .config import ProjectConfig, SchemaConfig
from .external_type_sources import capture_external_type_sources
from .project import effective_project, select_schema, validate_project_ownership
from .schema_loading import load_local_schema
from .schema_types import render_schema_types
from .schema_url import load_from_url
from .source_graph import ModuleSnapshot, TypeSymbolResolution, resolve_type_symbols
from .source_selection import select_project_sources


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        # argparse otherwise repeats arbitrary argument values, including secrets.
        raise ValueError("Invalid arguments; use --help.")


def _version(distribution: str) -> str:
    try:
        return metadata.version(distribution)
    except (metadata.PackageNotFoundError, OSError, ValueError):
        return "unavailable (distribution metadata not found)"


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
) -> tuple[TypeSymbolResolution, ...]:
    if not symbols:
        return ()
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
    )


def _resolve_interpreter_mapping_symbols(
    config: ProjectConfig, symbols: tuple[str, ...]
) -> tuple[TypeSymbolResolution, ...]:
    roots: list[Path] = []
    local_roots = (config.root, *config.source_roots)
    for value in sys.path:
        if not value:
            continue
        try:
            root = Path(value).resolve(strict=True)
        except (OSError, RuntimeError):
            continue
        if not root.is_dir() or any(
            root == local or root.is_relative_to(local) for local in local_roots
        ):
            continue
        roots.append(root)
    project_id = config.path.as_uri() + "#interpreter"
    modules = capture_external_type_sources(
        symbols, search_roots=tuple(roots), project_id=project_id
    )
    return resolve_type_symbols(symbols, modules=modules, project_id=project_id)


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="graphql-desk", description="GraphQL Desk diagnostics")
    parser.add_argument(
        "--version", action="version", version="%(prog)s " + _version("graphql-desk")
    )
    parser.add_argument(
        "-c",
        "--config",
        default=argparse.SUPPRESS,
        help="Select a pyproject.toml explicitly",
    )
    parser.add_argument(
        "--schema", default=argparse.SUPPRESS, help="Select one configured schema"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    doctor = commands.add_parser(
        "doctor", help="Report project readiness without writes"
    )
    doctor.add_argument(
        "-c",
        "--config",
        default=argparse.SUPPRESS,
        help="Select a pyproject.toml explicitly",
    )
    doctor.add_argument(
        "--schema", default=argparse.SUPPRESS, help="Select one configured schema"
    )
    return parser


def _doctor(
    config_path: Path | None,
    schema_name: str | None,
    *,
    environment: Mapping[str, str],
) -> int:
    print("GraphQL Desk doctor", file=sys.stderr)
    try:
        selected = effective_project(Path.cwd(), explicit_config=config_path)
        if selected is None:
            print(
                "configuration: unavailable; configure [tool.graphql-desk] "
                "or select --config PATH",
                file=sys.stderr,
            )
            return 1
        config = selected.config
        validate_project_ownership((config,))
        schemas = (
            (select_schema(config, binding=schema_name),)
            if schema_name is not None
            else config.schemas
        )
    except (OSError, ValueError, RuntimeError):
        print(
            "configuration: unavailable; check the selected project configuration "
            "and schema name",
            file=sys.stderr,
        )
        return 1
    print(f"configuration: ready ({selected.origin})", file=sys.stderr)
    for schema in schemas:
        try:
            if schema.origin.local_path is not None:
                loaded = load_local_schema(schema.origin.local_path)
            else:
                loaded = load_from_url(schema.origin, environment=environment)
        except (OSError, ValueError, RuntimeError):
            print(
                f"schema {schema.name}: unavailable; check schema contents, "
                "access and configured header environment",
                file=sys.stderr,
            )
        else:
            print(f"schema {schema.name}: ready", file=sys.stderr)
            for name, direction, symbol in (
                *((item.name, "scalar input", item.input) for item in schema.scalars),
                *((item.name, "scalar output", item.output) for item in schema.scalars),
                *((name, "enum", symbol) for name, symbol in schema.enums),
            ):
                if symbol == "typing.Any":
                    # Configuration has already restricted names to GraphQL identifiers.
                    print(
                        f"schema {schema.name}: explicit Any {direction} "
                        f"mapping {name}",
                        file=sys.stderr,
                    )
            try:
                render_schema_types(loaded, scalars=schema.scalars, enums=schema.enums)
            except (ValueError, TypeError, RecursionError):
                print(
                    f"schema {schema.name}: mapping declarations unavailable; "
                    "check scalar and enum mappings and schema declarations",
                    file=sys.stderr,
                )
            else:
                print(
                    f"schema {schema.name}: mapping declarations ready", file=sys.stderr
                )
            symbols = _mapping_symbols(schema)
            if not symbols:
                print(
                    f"schema {schema.name}: mapping symbols not configured",
                    file=sys.stderr,
                )
                continue
            try:
                resolutions = _resolve_local_mapping_symbols(config, symbols)
            except (OSError, ValueError, RuntimeError, UnicodeError):
                print(
                    f"schema {schema.name}: local mapping symbols unavailable; "
                    "source capture failed",
                    file=sys.stderr,
                )
                continue
            resolved = sum(item.status == "resolved" for item in resolutions)
            external = sum(item.status == "external" for item in resolutions)
            unavailable = [
                item
                for item in resolutions
                if item.status not in {"resolved", "external"}
            ]
            if resolved:
                print(
                    f"schema {schema.name}: captured local mapping symbols resolved "
                    f"(static names only): {resolved}",
                    file=sys.stderr,
                )
            if external:
                external_symbols = tuple(
                    item.symbol for item in resolutions if item.status == "external"
                )
                try:
                    environment_resolutions = _resolve_interpreter_mapping_symbols(
                        config, external_symbols
                    )
                except (OSError, ValueError, RuntimeError, UnicodeError):
                    print(
                        f"schema {schema.name}: interpreter mapping symbols "
                        f"unverified: {external}",
                        file=sys.stderr,
                    )
                else:
                    interpreter_resolved = sum(
                        item.status == "resolved" for item in environment_resolutions
                    )
                    interpreter_unverified = sum(
                        item.status == "external" for item in environment_resolutions
                    )
                    interpreter_unavailable = [
                        item
                        for item in environment_resolutions
                        if item.status not in {"resolved", "external"}
                    ]
                    if interpreter_resolved:
                        print(
                            f"schema {schema.name}: interpreter mapping symbols "
                            f"resolved (static names only): {interpreter_resolved}",
                            file=sys.stderr,
                        )
                    if interpreter_unverified:
                        print(
                            f"schema {schema.name}: external mapping symbols "
                            f"unverified: {interpreter_unverified}",
                            file=sys.stderr,
                        )
                    if interpreter_unavailable:
                        environment_statuses: dict[str, int] = {}
                        for item in interpreter_unavailable:
                            environment_statuses[item.status] = (
                                environment_statuses.get(item.status, 0) + 1
                            )
                        summary = ", ".join(
                            f"{status}={count}"
                            for status, count in sorted(environment_statuses.items())
                        )
                        print(
                            f"schema {schema.name}: interpreter mapping symbols "
                            f"unavailable: {summary}",
                            file=sys.stderr,
                        )
            if unavailable:
                statuses: dict[str, int] = {}
                for item in unavailable:
                    statuses[item.status] = statuses.get(item.status, 0) + 1
                summary = ", ".join(
                    f"{status}={count}" for status, count in sorted(statuses.items())
                )
                print(
                    f"schema {schema.name}: captured local mapping symbols "
                    f"unavailable: {summary}",
                    file=sys.stderr,
                )
    print(
        "interpreter: Python " + ".".join(map(str, sys.version_info[:3])),
        file=sys.stderr,
    )
    for checker in ("mypy", "pyright"):
        print(f"installed {checker} distribution: {_version(checker)}", file=sys.stderr)
    print(
        "generated ownership: unavailable; no verified ownership manifest",
        file=sys.stderr,
    )
    print(
        "active editor: unavailable; no authoritative editor session", file=sys.stderr
    )
    print("live bridge: unavailable; no verified bridge health", file=sys.stderr)
    print(
        "status: degraded; editor/bridge and generated ownership remain unverified",
        file=sys.stderr,
    )
    return 1


def main(argv: Sequence[str] | None = None) -> int:
    """Return an exit code; only the module/console entrypoint terminates a process.

    Doctor is deliberately degraded until editor, bridge and owned-generation
    health have authoritative inputs. Schema loading is explicit, never discovery.
    """
    try:
        arguments = _parser().parse_args(argv)
    except ValueError:
        print("Invalid arguments; use --help.", file=sys.stderr)
        return 2
    except SystemExit as error:
        return error.code if isinstance(error.code, int) else 2
    try:
        return _doctor(
            Path(arguments.config) if hasattr(arguments, "config") else None,
            arguments.schema if hasattr(arguments, "schema") else None,
            environment=dict(os.environ),
        )
    except KeyboardInterrupt:
        print("Doctor cancelled.", file=sys.stderr)
        return 130
