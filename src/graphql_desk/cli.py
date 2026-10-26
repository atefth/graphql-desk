"""Explicit read-only diagnostics; importing this module performs no work."""

import argparse
import ast
import json
import os
import sys
import tempfile
from collections.abc import Mapping, Sequence
from contextlib import redirect_stderr
from dataclasses import replace
from importlib import metadata
from io import StringIO
from pathlib import Path
from typing import NoReturn

from .config import ProjectConfig, SchemaConfig
from .document_validation import validate_indexed_document
from .external_type_sources import (
    capture_external_type_sources,
    external_class_base_symbols,
    interpreter_type_roots,
)
from .fragments import SchemaContext, index_fragments
from .persisted_bindings import generated_header, render_persisted_bindings
from .persisted_manifest import collect_persisted_manifests
from .project import (
    effective_project,
    project_bindings,
    select_schema,
    validate_project_ownership,
)
from .schema_loading import load_local_schema
from .schema_types import render_schema_types
from .schema_url import load_from_url
from .source_graph import (
    DocumentIdentity,
    GraphDiagnostic,
    ModuleSnapshot,
    TypeSymbolResolution,
    resolve_project,
    resolve_type_symbols,
)
from .source_selection import select_project_sources


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        # argparse otherwise repeats arbitrary argument values, including secrets.
        raise ValueError("Invalid arguments; use --help.")


class PersistedGenerationError(ValueError):
    """Sanitized persisted-generation failure with collected diagnostics."""

    def __init__(self, diagnostics: Sequence[str]) -> None:
        self.diagnostics = tuple(diagnostics)
        super().__init__("Persisted generation failed")


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
    persisted = commands.add_parser(
        "generate-persisted",
        help="Generate persisted operation manifests and typed bindings",
    )
    persisted.add_argument(
        "-c", "--config", default=argparse.SUPPRESS, help="Select pyproject.toml"
    )
    persisted.add_argument(
        "--schema", default=argparse.SUPPRESS, help="Select one configured schema"
    )
    persisted.add_argument("-o", "--output", help="Write one manifest to this path")
    persisted.add_argument(
        "--disable-normalization", action="store_true", help="Keep authored text"
    )
    persisted.add_argument(
        "--fail-on-warn", action="store_true", help="Do not write when warnings occur"
    )
    return parser


def _python_candidates(config: ProjectConfig) -> tuple[Path, ...]:
    ignored = {
        ".git",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        ".venv",
        "venv",
        "__pycache__",
        "node_modules",
        "site-packages",
        "build",
        "dist",
    }
    candidates: list[Path] = []
    for root in config.source_roots:
        if not root.is_dir():
            continue
        for directory, names, files in os.walk(root, followlinks=False):
            names[:] = sorted(
                name
                for name in names
                if name not in ignored and not (Path(directory) / name).is_symlink()
            )
            candidates.extend(
                Path(directory) / name for name in files if name.endswith(".py")
            )
    return tuple(sorted(candidates, key=lambda item: item.as_posix()))


def _persisted_outputs(
    config: ProjectConfig,
    schema_name: str | None,
    explicit_output: str | None,
    *,
    stdout_is_terminal: bool,
) -> tuple[tuple[SchemaConfig, Path | None], ...]:
    schemas = (
        (select_schema(config, binding=schema_name),)
        if schema_name is not None
        else config.schemas
    )
    if explicit_output is not None:
        if len(schemas) != 1:
            raise ValueError("Select one schema when using --output")
        path = Path(explicit_output)
        destination = path if path.is_absolute() else Path.cwd() / path
        return ((schemas[0], destination.resolve()),)
    destinations = tuple((item, item.persisted_output) for item in schemas)
    if all(path is not None for _, path in destinations):
        return tuple((item, path) for item, path in destinations)
    if len(schemas) != 1:
        raise ValueError("Configure persisted_output for each schema or select one")
    if not stdout_is_terminal:
        return ((schemas[0], None),)
    raise ValueError("Select --output, configure persisted_output, or redirect stdout")


def _stage_outputs(outputs: Mapping[Path, str]) -> None:
    staged: list[tuple[Path, Path]] = []
    backups: dict[Path, Path | None] = {}
    committed: list[Path] = []
    recovery_paths: list[Path] = []
    rollback_failed = False
    try:
        for target, text in outputs.items():
            target.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                newline="\n",
                dir=target.parent,
                prefix=f".{target.name}.",
                suffix=".tmp",
                delete=False,
            ) as stream:
                stream.write(text)
                stream.flush()
                os.fsync(stream.fileno())
                staged.append((Path(stream.name), target))
        for _, target in staged:
            if target.exists():
                with tempfile.NamedTemporaryFile(
                    mode="wb",
                    dir=target.parent,
                    prefix=f".{target.name}.",
                    suffix=".bak",
                    delete=False,
                ) as stream:
                    backup_path = Path(stream.name)
                backup_path.unlink()
                os.link(target, backup_path)
                backups[target] = backup_path
            else:
                backups[target] = None
        for temporary, target in staged:
            os.replace(temporary, target)
            committed.append(target)
    except BaseException:
        for target in reversed(committed):
            previous_backup = backups[target]
            try:
                if previous_backup is None:
                    target.unlink(missing_ok=True)
                else:
                    os.replace(previous_backup, target)
                    backups[target] = None
            except BaseException:
                rollback_failed = True
                recovery_paths.append(target)
                if previous_backup is not None:
                    recovery_paths.append(previous_backup)
        raise
    finally:
        for temporary, _ in staged:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
        if not rollback_failed:
            for stale_backup in backups.values():
                if stale_backup is not None:
                    try:
                        stale_backup.unlink(missing_ok=True)
                    except OSError:
                        pass
        else:
            retained = sorted(
                {
                    str(path)
                    for path in (*recovery_paths, *backups.values())
                    if path is not None and path.exists()
                }
            )
            if retained:
                print(
                    "Persisted output recovery copies were retained at: "
                    + ", ".join(retained),
                    file=sys.stderr,
                )


def _report_diagnostics(
    diagnostics: Sequence[GraphDiagnostic], modules: tuple[ModuleSnapshot, ...]
) -> None:
    sources = {
        (item.project, item.module, item.uri, item.version): item.source
        for item in modules
    }
    for diagnostic in diagnostics:
        source = sources.get(
            (
                diagnostic.module.project,
                diagnostic.module.module,
                diagnostic.module.uri,
                diagnostic.module.version,
            ),
            "",
        )
        before = source[: diagnostic.range.start]
        line = before.count("\n") + 1
        column = len(before.rsplit("\n", 1)[-1].removesuffix("\r")) + 1
        print(
            f"{diagnostic.module.module}:{line}:{column}: {diagnostic.message}",
            file=sys.stderr,
        )


def _generate_persisted(
    config_path: Path | None,
    schema_name: str | None,
    explicit_output: str | None,
    *,
    disable_normalization: bool,
    fail_on_warn: bool,
    environment: Mapping[str, str],
    stdout_is_terminal: bool,
) -> int:
    selected = effective_project(Path.cwd(), explicit_config=config_path)
    if selected is None:
        raise ValueError("No GraphQL Desk project configuration was found")
    config = selected.config
    validate_project_ownership((config,))
    destinations = _persisted_outputs(
        config, schema_name, explicit_output, stdout_is_terminal=stdout_is_terminal
    )
    project_id = config.path.as_uri()
    candidate_files = _python_candidates(config)
    selected_files = select_project_sources(
        config,
        candidate_files,
        include_stubs=False,
        validate_entrypoints=False,
    )
    modules = tuple(
        ModuleSnapshot(
            project_id,
            source.module,
            source.path.as_uri(),
            1,
            source.path.read_text(encoding="utf-8"),
            source.is_package,
        )
        for source in selected_files
    )
    protected = {
        config.path.resolve(),
        *(
            schema.origin.local_path.resolve()
            for schema in config.schemas
            if schema.origin.local_path is not None
        ),
        *(path.resolve() for path in candidate_files),
    }
    if any(
        destination is not None and destination.resolve() in protected
        for _, destination in destinations
    ):
        raise ValueError("Persisted output cannot overwrite project inputs")
    selected_schemas = tuple(schema for schema, _ in destinations)
    selected_config = replace(config, schemas=selected_schemas)
    bindings = project_bindings(selected_config, project_id=project_id)
    sources = resolve_project(modules, bindings=(bindings,))
    schema_contexts: dict[tuple[str, str | None], SchemaContext] = {}
    for schema_config in selected_schemas:
        loaded_schema = (
            load_local_schema(schema_config.origin.local_path)
            if schema_config.origin.local_path is not None
            else load_from_url(schema_config.origin, environment=environment)
        )
        token = f"persisted-cli:{project_id}:{schema_config.name}"
        context = SchemaContext(schema_config.name, loaded_schema, token)
        schema_contexts[(project_id, schema_config.name)] = context
        if bindings.default_schema == schema_config.name:
            schema_contexts[(project_id, None)] = context
    index = index_fragments(sources, schemas=schema_contexts)
    indexed = {
        DocumentIdentity(document.call.module, document.call.range): document
        for document in index.documents
    }
    validation_errors: list[GraphDiagnostic] = []
    for declaration in sources.persisted:
        document = indexed.get(declaration.document)
        validation_context = schema_contexts.get((project_id, declaration.schema_name))
        if document is None or validation_context is None:
            continue
        validation_errors.extend(
            validate_indexed_document(
                validation_context,
                document,
                fragment_index=index,
                modules=modules,
                client_directives=config.client_directives,
            )
        )
    if validation_errors:
        _report_diagnostics(tuple(validation_errors), modules)
        return 1
    collection = collect_persisted_manifests(
        sources, index, normalize=not disable_normalization
    )
    if collection.diagnostics:
        _report_diagnostics(collection.diagnostics, modules)
        return 1
    incomplete_analysis = tuple(
        item
        for item in sources.diagnostics
        if item.code
        in {
            "persisted-not-static",
            "invalid-python",
            "parse-fallback",
            "source-too-complex",
        }
    )
    if incomplete_analysis:
        _report_diagnostics(incomplete_analysis, modules)
        print("Incomplete source analysis prevents persisted output.", file=sys.stderr)
        return 1
    if sources.diagnostics:
        _report_diagnostics(sources.diagnostics, modules)
    if fail_on_warn and sources.diagnostics:
        print("Source warnings prevent persisted output.", file=sys.stderr)
        return 1
    if config.client_directives:
        print(
            "Persisted generation cannot account for configured client transforms.",
            file=sys.stderr,
        )
        return 1
    manifests = collection.manifests
    output_text: dict[Path, str] = {}
    stdout_text: str | None = None
    package = config.root.joinpath(*config.generated_package.split("."))
    persisted_package = package / "persisted"
    for schema, destination in destinations:
        entries = manifests.get((project_id, schema.name), ())
        text = (
            json.dumps(
                {entry.id: entry.document for entry in entries},
                ensure_ascii=True,
                indent=2,
            )
            + "\n"
        )
        if destination is None:
            if stdout_text is not None or len(destinations) != 1:
                raise ValueError("Select one schema for machine stdout")
            stdout_text = text
        else:
            output_text[destination] = text
        runtime, stub = render_persisted_bindings(
            schema,
            schema_contexts[(project_id, schema.name)],
            sources.persisted,
            index,
            client_directives=config.client_directives,
        )
        for path, generated in (
            (persisted_package / f"{schema.name}.py", runtime),
            (persisted_package / f"{schema.name}.pyi", stub),
        ):
            resolved_parent = path.parent.resolve()
            if not resolved_parent.is_relative_to(package.resolve()):
                raise ValueError("Generated persisted binding escapes its package")
            if path.is_symlink():
                raise ValueError("Generated persisted binding cannot replace a symlink")
            if path.exists():
                if not path.is_file() or not path.read_text(
                    encoding="utf-8"
                ).startswith(generated_header()):
                    raise ValueError(
                        "Generated persisted binding is not GraphQL Desk-owned"
                    )
            if path.resolve() in output_text:
                raise ValueError("Persisted manifest and binding outputs collide")
            output_text[path.resolve()] = generated
    if output_text:
        _stage_outputs(output_text)
    if stdout_text is not None:
        print(stdout_text, end="")
    return 0


def generate_persisted(
    *,
    config: Path | str | None = None,
    schema: str | None = None,
    output: Path | str | None = None,
    disable_normalization: bool = False,
    fail_on_warn: bool = False,
    environment: Mapping[str, str] | None = None,
) -> None:
    """Generate manifests and ID-only typed bindings from shared project facts."""
    diagnostics = StringIO()
    try:
        with redirect_stderr(diagnostics):
            result = _generate_persisted(
                Path(config) if config is not None else None,
                schema,
                str(output) if output is not None else None,
                disable_normalization=disable_normalization,
                fail_on_warn=fail_on_warn,
                environment=os.environ if environment is None else environment,
                stdout_is_terminal=True,
            )
    except (OSError, RuntimeError, ValueError):
        messages = tuple(diagnostics.getvalue().splitlines())
        raise PersistedGenerationError(
            messages or ("Check configuration, sources, schema access and outputs.",)
        ) from None
    messages = tuple(diagnostics.getvalue().splitlines())
    if result:
        raise PersistedGenerationError(
            messages or ("Check configuration, sources, schema access and outputs.",)
        )
    if messages:
        print("\n".join(messages), file=sys.stderr)


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
                local_resolutions, local_modules = _resolve_local_mapping_symbols(
                    config, symbols
                )
            except (OSError, ValueError, RuntimeError, UnicodeError):
                print(
                    f"schema {schema.name}: local mapping symbols unavailable; "
                    "source capture failed",
                    file=sys.stderr,
                )
                continue
            local_by_symbol = {item.symbol: item for item in local_resolutions}
            resolutions = dict(local_by_symbol)
            resolved = sum(item.status == "resolved" for item in local_resolutions)
            if resolved:
                print(
                    f"schema {schema.name}: captured local mapping symbols resolved "
                    f"(static names only): {resolved}",
                    file=sys.stderr,
                )
            if any(item.status != "resolved" for item in local_resolutions):
                try:
                    environment_resolutions = _resolve_interpreter_mapping_symbols(
                        config, symbols, local_modules
                    )
                except (OSError, ValueError, RuntimeError, UnicodeError):
                    environment_resolutions = ()
                    print(
                        f"schema {schema.name}: interpreter mapping symbols "
                        "unavailable; source capture failed",
                        file=sys.stderr,
                    )
                else:
                    for item in environment_resolutions:
                        if local_by_symbol[item.symbol].status != "resolved":
                            resolutions[item.symbol] = item
                    interpreter_resolved = sum(
                        resolutions[symbol].status == "resolved"
                        and local_by_symbol[symbol].status != "resolved"
                        for symbol in symbols
                    )
                    interpreter_unverified = sum(
                        resolutions[symbol].status == "external" for symbol in symbols
                    )
                    interpreter_unavailable = [
                        resolutions[symbol]
                        for symbol in symbols
                        if local_by_symbol[symbol].status == "external"
                        and resolutions[symbol].status not in {"resolved", "external"}
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
            unavailable = [
                resolutions[symbol]
                for symbol in symbols
                if local_by_symbol[symbol].status not in {"resolved", "external"}
                and resolutions[symbol].status not in {"resolved", "external"}
            ]
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
    """Return an exit code; only the module/console entrypoint exits the process."""
    try:
        command_line = list(sys.argv[1:] if argv is None else argv)
        for position in range(len(command_line) - 1):
            if command_line[position : position + 2] == ["generate", "persisted"]:
                command_line[position : position + 2] = ["generate-persisted"]
                break
        arguments = _parser().parse_args(command_line)
    except ValueError:
        print("Invalid arguments; use --help.", file=sys.stderr)
        return 2
    except SystemExit as error:
        return error.code if isinstance(error.code, int) else 2
    try:
        config_path = Path(arguments.config) if hasattr(arguments, "config") else None
        schema_name = arguments.schema if hasattr(arguments, "schema") else None
        if arguments.command == "generate-persisted":
            return _generate_persisted(
                config_path,
                schema_name,
                arguments.output,
                disable_normalization=arguments.disable_normalization,
                fail_on_warn=arguments.fail_on_warn,
                environment=dict(os.environ),
                stdout_is_terminal=sys.stdout.isatty(),
            )
        return _doctor(config_path, schema_name, environment=dict(os.environ))
    except KeyboardInterrupt:
        message = (
            "Doctor cancelled."
            if arguments.command == "doctor"
            else "Persisted generation cancelled."
        )
        print(message, file=sys.stderr)
        return 130
    except (OSError, RuntimeError, ValueError):
        print(
            "GraphQL Desk command failed; check configuration, sources and outputs.",
            file=sys.stderr,
        )
        return 1
