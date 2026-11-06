"""Project diagnostics and explicit generation; importing performs no work."""

import argparse
import ast
import json
import os
import shutil
import stat
import sys
import tempfile
from collections.abc import Mapping, MutableMapping, Sequence
from contextlib import redirect_stderr
from dataclasses import dataclass, replace
from importlib import metadata
from io import StringIO
from pathlib import Path
from typing import Literal, NoReturn, cast

import tomlkit
from graphql import GraphQLError, print_schema

from . import config_files
from .config import (
    ProjectConfig,
    SchemaConfig,
    SchemaOrigin,
    generated_output_paths,
    parse_project_config,
    parse_project_settings,
)
from .document_validation import validate_indexed_document
from .external_type_sources import (
    capture_external_type_sources,
    external_class_base_symbols,
    interpreter_type_roots,
)
from .factory_generation import FactoryArtifacts, generate_project_factory_artifacts
from .field_usage import FieldUsageOptions, field_usage_diagnostics
from .fragments import (
    SchemaContext,
    index_fragments,
    unused_colocated_fragment_diagnostics,
)
from .output_generation import OutputGenerationConflict, publish_output_package
from .persisted_bindings import generated_header, render_persisted_bindings
from .persisted_manifest import collect_persisted_manifests
from .project import (
    effective_project,
    find_project,
    project_bindings,
    select_schema,
    validate_project_ownership,
)
from .release_artifacts import (
    check_wheel,
    publish_omitted_tree,
    render_omitted_files,
    validate_authoring_separation,
    validate_omitted_tree,
)
from .scan import scan_project
from .scan_reporting import json_report, terminal_report
from .schema_loading import load_local_schema
from .schema_types import render_schema_types
from .schema_url import load_from_url
from .source import Span
from .source_graph import (
    DocumentIdentity,
    GraphDiagnostic,
    ModuleDependency,
    ModuleSnapshot,
    TypeSymbolResolution,
    resolve_project,
    resolve_type_symbols,
)
from .source_selection import project_python_candidates, select_project_sources


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        # argparse otherwise repeats arbitrary argument values, including secrets.
        raise ValueError("Invalid arguments; use --help.")


class PersistedGenerationError(ValueError):
    """Sanitized persisted-generation failure with collected diagnostics."""

    def __init__(self, diagnostics: Sequence[str]) -> None:
        self.diagnostics = tuple(diagnostics)
        super().__init__("Persisted generation failed")


class OutputGenerationError(ValueError):
    """Sanitized schema-output generation failure with collected diagnostics."""

    def __init__(self, diagnostics: Sequence[str]) -> None:
        self.diagnostics = tuple(diagnostics)
        super().__init__("Schema output generation failed")


class SchemaGenerationError(ValueError):
    """Sanitized schema-download or conversion failure."""

    def __init__(self, diagnostics: Sequence[str]) -> None:
        self.diagnostics = tuple(diagnostics)
        super().__init__("Schema generation failed")


@dataclass(frozen=True)
class CheckDiagnostic:
    """One source or project diagnostic returned by :func:`check`."""

    severity: Literal["info", "warn", "error"]
    code: str
    message: str
    module: ModuleDependency | None = None
    range: Span | None = None
    schema: str | None = None
    line: int | None = None
    column: int | None = None
    file: str | None = None
    end_line: int | None = None
    end_column: int | None = None
    workflow_file: str | None = None


_WARNING_CODES = frozenset(
    {
        "invalid-python",
        "parse-fallback",
        "source-not-static",
        "source-too-complex",
        "persisted-not-static",
        "unused-colocated-fragment",
        "graphql-deprecation",
        "52005",
    }
)


def _check_diagnostic(
    diagnostic: GraphDiagnostic,
    source_texts: Mapping[tuple[str, str, str, int], str],
    source_files: Mapping[tuple[str, str, str, int], str],
    workflow_files: Mapping[tuple[str, str, str, int], str],
) -> CheckDiagnostic:
    identity = (
        diagnostic.module.project,
        diagnostic.module.module,
        diagnostic.module.uri,
        diagnostic.module.version,
    )
    source = source_texts.get(identity, "")
    before = source[: diagnostic.range.start]
    end_position = diagnostic.range.end - 1
    if (
        diagnostic.range.end > diagnostic.range.start
        and source[end_position : end_position + 1] == "\n"
    ):
        end_position -= 1
        if source[end_position : end_position + 1] == "\r":
            end_position -= 1
    end_line = end_column = None
    if end_position >= diagnostic.range.start:
        through_end = source[: end_position + 1]
        end_line = through_end.count("\n") + 1
        end_column = len(through_end.rsplit("\n", 1)[-1].removesuffix("\r"))
    return CheckDiagnostic(
        "warn" if diagnostic.code in _WARNING_CODES else "error",
        diagnostic.code,
        diagnostic.message,
        diagnostic.module,
        diagnostic.range,
        line=before.count("\n") + 1,
        column=len(before.rsplit("\n", 1)[-1].removesuffix("\r")) + 1,
        file=source_files.get(identity),
        end_line=end_line,
        end_column=end_column,
        workflow_file=workflow_files.get(identity),
    )


def _project_diagnostic(
    code: str, message: str, *, schema: str | None = None
) -> CheckDiagnostic:
    return CheckDiagnostic("error", code, message, schema=schema)


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
    initialize = commands.add_parser("init", help="Set up a project (work in progress)")
    initialize.add_argument(
        "directory", nargs="?", default=".", help="Project directory"
    )
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
    persisted.add_argument(
        "--omit-runtime-source",
        action="store_true",
        help="Stage a source-free package tree for persisted-only releases",
    )
    output = commands.add_parser(
        "generate-output", help="Generate schema and operation type packages"
    )
    output.add_argument(
        "-c", "--config", default=argparse.SUPPRESS, help="Select pyproject.toml"
    )
    output.add_argument(
        "--schema", default=argparse.SUPPRESS, help="Select one configured schema"
    )
    output.add_argument("-o", "--output", help="Select the configured schema stub path")
    output.add_argument(
        "--disable-preprocessing",
        action="store_true",
        default=None,
        help="Disable schema-output preprocessing",
    )
    schema = commands.add_parser(
        "generate-schema", help="Download or convert a schema to SDL"
    )
    schema.add_argument("input", help="Schema SDL/introspection JSON file or URL")
    schema.add_argument(
        "-c", "--config", default=argparse.SUPPRESS, help="Select pyproject.toml"
    )
    schema.add_argument(
        "--schema",
        default=argparse.SUPPRESS,
        help="Select configured destination and headers",
    )
    schema.add_argument("-o", "--output", help="Write SDL to this file")
    schema.add_argument(
        "--header",
        action="append",
        default=[],
        help="HTTP header 'Name: value' (repeatable)",
    )
    scan_parser = commands.add_parser(
        "scan", help="Report experimental schema-aware project insights"
    )
    scan_parser.add_argument(
        "-c", "--config", default=argparse.SUPPRESS, help="Select pyproject.toml"
    )
    scan_parser.add_argument(
        "--schema", default=argparse.SUPPRESS, help="Select one configured schema"
    )
    scan_parser.add_argument(
        "-f",
        "--format",
        choices=("text", "json"),
        default=None,
        help="Output format",
    )
    scan_parser.add_argument(
        "--graph", action="store_true", help="Emit the pure graph JSON instead"
    )
    scan_parser.add_argument("-o", "--output", help="Write report to this file")
    scan_parser.add_argument(
        "-w",
        "--fail-on-warn",
        action="store_true",
        help="Return failure when source discovery or parsing warnings occur",
    )
    check_parser = commands.add_parser(
        "check", help="Validate project GraphQL documents or a wheel artifact"
    )
    check_parser.add_argument(
        "-c", "--config", default=argparse.SUPPRESS, help="Select pyproject.toml"
    )
    check_parser.add_argument(
        "--schema", default=argparse.SUPPRESS, help="Select one configured schema"
    )
    check_parser.add_argument(
        "--level",
        "-l",
        choices=("info", "warn", "error"),
        default="info",
        help="Minimum diagnostic level to display",
    )
    check_parser.add_argument(
        "--fail-on-warn",
        "-w",
        action="store_true",
        help="Return failure when warnings occur",
    )
    check_parser.add_argument(
        "--format", choices=("text", "json"), default="text", help="Output format"
    )
    check_parser.add_argument(
        "--annotations-output",
        metavar="PATH",
        help="Write GitHub Actions annotations to a separate file",
    )
    check_parser.add_argument("--artifact", help="Verify a GraphQL Desk wheel")
    return parser


def check(
    *,
    config: Path | str | None = None,
    schema: str | None = None,
    environment: Mapping[str, str] | None = None,
) -> tuple[CheckDiagnostic, ...]:
    """Check statically discoverable documents without importing project code."""
    try:
        selected = effective_project(
            Path.cwd(),
            explicit_config=Path(config) if config is not None else None,
        )
    except (OSError, RuntimeError, UnicodeError, ValueError):
        return (
            _project_diagnostic(
                "project-unavailable",
                "Project configuration could not be checked.",
            ),
        )
    if selected is None:
        return (
            _project_diagnostic(
                "project-unavailable",
                "No GraphQL Desk project configuration was found.",
            ),
        )
    project_config = selected.config
    try:
        validate_project_ownership((project_config,))
        selected_schemas = (
            (select_schema(project_config, binding=schema),)
            if schema is not None
            else project_config.schemas
        )
    except (OSError, RuntimeError, UnicodeError, ValueError):
        return (
            _project_diagnostic(
                "schema-selection-failed"
                if schema is not None
                else "project-unavailable",
                "The selected schema or project configuration is unavailable.",
            ),
        )
    try:
        files = select_project_sources(
            project_config,
            project_python_candidates(project_config),
            validate_entrypoints=True,
        )
        project_id = project_config.path.as_uri()
        modules = tuple(
            ModuleSnapshot(
                project_id,
                source.module,
                source.path.as_uri(),
                1,
                source.path.read_text(encoding="utf-8-sig"),
                source.is_package,
            )
            for source in files
        )
        sources = resolve_project(
            modules,
            bindings=(project_bindings(project_config, project_id=project_id),),
            collect_document_imports=project_config.check_colocated_fragments,
        )
    except (OSError, RuntimeError, UnicodeError, ValueError):
        return (
            _project_diagnostic(
                "project-unavailable",
                "Project configuration or Python sources could not be checked.",
            ),
        )

    source_texts = {
        (item.project, item.module, item.uri, item.version): item.source
        for item in modules
    }
    project_root = project_config.root.resolve()
    workspace_root = Path(os.environ.get("GITHUB_WORKSPACE", str(Path.cwd()))).resolve()
    source_files: dict[tuple[str, str, str, int], str] = {}
    workflow_files: dict[tuple[str, str, str, int], str] = {}
    for source in files:
        identity = (project_id, source.module, source.path.as_uri(), 1)
        try:
            relative = source.path.resolve().relative_to(project_root).as_posix()
        except ValueError:
            pass
        else:
            source_files[identity] = relative
        try:
            workflow_relative = (
                source.path.resolve().relative_to(workspace_root).as_posix()
            )
        except ValueError:
            continue
        workflow_files[identity] = workflow_relative

    def check_diagnostic(item: GraphDiagnostic) -> CheckDiagnostic:
        return _check_diagnostic(item, source_texts, source_files, workflow_files)

    diagnostics = [check_diagnostic(item) for item in sources.diagnostics]
    schema_contexts: dict[tuple[str, str | None], SchemaContext] = {}
    for schema_config in selected_schemas:
        try:
            loaded = (
                load_local_schema(schema_config.origin.local_path)
                if schema_config.origin.local_path is not None
                else load_from_url(
                    schema_config.origin,
                    environment=os.environ if environment is None else environment,
                )
            )
        except (OSError, RuntimeError, ValueError):
            diagnostics.append(
                _project_diagnostic(
                    "schema-unavailable",
                    "Schema contents or access could not be checked.",
                    schema=schema_config.name,
                )
            )
            continue
        token = f"check:{project_id}:{schema_config.name}"
        schema_context = SchemaContext(schema_config.name, loaded, token)
        schema_contexts[(project_id, schema_config.name)] = schema_context
        if schema_config.name == "default":
            schema_contexts[(project_id, None)] = schema_context

    calls = tuple(
        call
        for call in sources.calls
        if (
            schema is None
            or call.schema_name == schema
            or (schema == "default" and call.schema_name is None)
        )
        and (project_id, call.schema_name) in schema_contexts
    )
    persisted = tuple(
        item
        for item in sources.persisted
        if schema is None
        or item.schema_name == schema
        or (schema == "default" and item.schema_name is None)
    )
    selected_sources = replace(
        sources, calls=calls, diagnostics=(), persisted=persisted
    )
    try:
        index = index_fragments(selected_sources, schemas=schema_contexts)
        diagnostics.extend(check_diagnostic(item) for item in index.diagnostics)
        diagnostics.extend(
            check_diagnostic(item)
            for item in collect_persisted_manifests(selected_sources, index).diagnostics
        )
        diagnostics.extend(
            check_diagnostic(item)
            for item in field_usage_diagnostics(
                modules,
                index,
                schema_contexts,
                {
                    project_id: FieldUsageOptions(
                        enabled=project_config.track_field_usage,
                        reserved_keys=project_config.reserved_keys,
                        unmasked_schemas=tuple(
                            item.name
                            for item in selected_schemas
                            if item.disable_masking
                        ),
                        client_directives=project_config.client_directives,
                    )
                },
            )
        )
        if project_config.check_colocated_fragments:
            diagnostics.extend(
                check_diagnostic(item)
                for item in unused_colocated_fragment_diagnostics(sources, index)
            )
        contexts_by_name = {item.name: item for item in selected_schemas}
        for document in index.documents:
            schema_name = document.schema_identity[1]
            matched_schema_config = contexts_by_name.get(schema_name)
            matched_context = (
                schema_contexts.get((project_id, matched_schema_config.name))
                if matched_schema_config
                else None
            )
            if matched_context is None:
                continue
            diagnostics.extend(
                check_diagnostic(item)
                for item in validate_indexed_document(
                    matched_context,
                    document,
                    fragment_index=index,
                    modules=modules,
                    client_directives=project_config.client_directives,
                    include_deprecations=True,
                )
            )
    except (GraphQLError, OSError, RuntimeError, TypeError, ValueError):
        diagnostics.append(
            _project_diagnostic(
                "analysis-unavailable",
                "GraphQL source analysis could not be completed.",
            )
        )

    unique: dict[tuple[object, ...], CheckDiagnostic] = {}
    for item in diagnostics:
        key = (
            item.severity,
            item.code,
            item.message,
            item.module,
            item.range,
            item.schema,
        )
        unique[key] = item
    return tuple(
        sorted(
            unique.values(),
            key=lambda item: (
                item.module.module if item.module else "",
                item.range.start if item.range else -1,
                item.schema or "",
                item.code,
                item.message,
            ),
        )
    )


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


def _check_init_directory(directory: Path, expected: os.stat_result) -> None:
    current = directory.lstat()
    if (
        not stat.S_ISDIR(current.st_mode)
        or (current.st_dev, current.st_ino) != (expected.st_dev, expected.st_ino)
        or any(parent.is_symlink() for parent in directory.parents)
    ):
        raise OutputGenerationConflict("Project directory changed; rerun init.")


def _commit_init_output(
    temporary: Path,
    target: Path,
    expected: config_files.ConfigSnapshot | None,
    staged: config_files.ConfigSnapshot,
    directory_identity: os.stat_result,
) -> None:
    recovery: Path | None = None
    moved = committed = False
    try:
        _check_init_directory(target.parent, directory_identity)
        if not config_files.same_output(temporary, staged):
            raise OutputGenerationConflict("Staged configuration changed; rerun init.")
        if expected is not None:
            if not config_files.same_output(target, expected):
                raise OutputGenerationConflict(
                    "Project configuration changed; rerun init."
                )
            with tempfile.NamedTemporaryFile(
                dir=target.parent,
                prefix=".pyproject.",
                suffix=".recovery",
                delete=False,
            ) as stream:
                recovery = Path(stream.name)
            _check_init_directory(target.parent, directory_identity)
            os.replace(target, recovery)
            moved = True
            if not config_files.same_output(recovery, expected):
                raise OutputGenerationConflict(
                    "Project configuration changed; rerun init."
                )
        _check_init_directory(target.parent, directory_identity)
        if not config_files.same_output(temporary, staged):
            raise OutputGenerationConflict("Staged configuration changed; rerun init.")
        os.link(temporary, target)
        committed = True
        _check_init_directory(target.parent, directory_identity)
        if not config_files.same_output(target, staged):
            raise OutputGenerationConflict(
                "Published configuration changed; review retained files."
            )
        if recovery is not None:
            assert expected is not None
            if not config_files.same_output(recovery, expected):
                raise OutputGenerationConflict(
                    "Configuration changed during init; review the recovery copy."
                )
            recovery.unlink()
            recovery = None
    finally:
        if recovery is not None:
            if not moved:
                recovery.unlink(missing_ok=True)
            elif not committed:
                try:
                    _check_init_directory(target.parent, directory_identity)
                    os.link(recovery, target)
                    recovery.unlink()
                except (OSError, OutputGenerationConflict):
                    print(
                        f"Original configuration retained at: {recovery}",
                        file=sys.stderr,
                    )
            else:
                print(
                    f"Original configuration retained at: {recovery}", file=sys.stderr
                )


def _stage_outputs(
    outputs: Mapping[Path, str],
    *,
    expected: Mapping[Path, config_files.ConfigSnapshot | None] | None = None,
    directory_identity: os.stat_result | None = None,
) -> None:
    if expected is not None and (
        len(outputs) != 1
        or outputs.keys() != expected.keys()
        or directory_identity is None
    ):
        raise ValueError("Configuration staging requires one matching snapshot")
    staged: list[tuple[Path, Path]] = []
    stage_snapshots: dict[Path, config_files.ConfigSnapshot] = {}
    backups: dict[Path, Path | None] = {}
    committed: list[Path] = []
    recovery_paths: list[Path] = []
    rollback_failed = False
    try:
        for target, text in outputs.items():
            if expected is not None:
                assert directory_identity is not None
                _check_init_directory(target.parent, directory_identity)
            else:
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
                staged.append((Path(stream.name), target))
                if expected is not None:
                    stage_snapshots[Path(stream.name)] = config_files.ConfigSnapshot(
                        b"",
                        os.fstat(stream.fileno()),
                        config_files.config_dacl(stream.fileno()),
                    )
                    config_files.check_config_acl(stream.fileno())
                snapshot = expected.get(target) if expected is not None else None
                if snapshot is not None:
                    config_files.preserve_config_owner(
                        stream.fileno(),
                        snapshot,
                        stage_snapshots[Path(stream.name)].dacl,
                    )
                    stage_snapshots[Path(stream.name)] = config_files.ConfigSnapshot(
                        b"",
                        os.fstat(stream.fileno()),
                        config_files.config_dacl(stream.fileno()),
                    )
                    config_files.apply_config_dacl(stream.fileno(), snapshot.dacl)
                if expected is not None:
                    assert directory_identity is not None
                    _check_init_directory(target.parent, directory_identity)
                    config_files.check_config_acl(stream.fileno())
                stream.write(text)
                stream.flush()
                if expected is not None:
                    stage_snapshots[Path(stream.name)] = config_files.ConfigSnapshot(
                        text.encode("utf-8"),
                        os.fstat(stream.fileno()),
                        config_files.config_dacl(stream.fileno()),
                    )
                os.fsync(stream.fileno())
        if expected is not None:
            temporary, target = staged[0]
            assert directory_identity is not None
            _commit_init_output(
                temporary,
                target,
                expected[target],
                stage_snapshots[temporary],
                directory_identity,
            )
            return
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
                staged_snapshot = stage_snapshots.get(temporary)
                if expected is not None and (
                    staged_snapshot is None
                    or not config_files.same_output(temporary, staged_snapshot)
                ):
                    print(
                        f"Changed temporary configuration retained at: {temporary}",
                        file=sys.stderr,
                    )
                    continue
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


def _report_check_diagnostics(
    diagnostics: Sequence[CheckDiagnostic],
    *,
    level: str,
) -> None:
    for diagnostic in _visible_check_diagnostics(diagnostics, level=level):
        location = ""
        if diagnostic.module is not None and diagnostic.range is not None:
            location = (
                f"{diagnostic.module.module}:{diagnostic.line}:{diagnostic.column}: "
            )
        elif diagnostic.schema is not None:
            location = f"schema {diagnostic.schema}: "
        print(
            f"{location}{diagnostic.severity} [{diagnostic.code}]: "
            f"{diagnostic.message}",
            file=sys.stderr,
        )


def _visible_check_diagnostics(
    diagnostics: Sequence[CheckDiagnostic], *, level: str
) -> tuple[CheckDiagnostic, ...]:
    minimum = {"info": 0, "warn": 1, "error": 2}[level]
    rank = {"info": 0, "warn": 1, "error": 2}
    return tuple(item for item in diagnostics if rank[item.severity] >= minimum)


def _escape_workflow_value(value: str, *, property_value: bool = False) -> str:
    escaped = value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    if property_value:
        escaped = escaped.replace(":", "%3A").replace(",", "%2C")
    return escaped


def _workflow_annotation(diagnostic: CheckDiagnostic) -> str | None:
    annotation_file = diagnostic.workflow_file
    if diagnostic.module is not None and annotation_file is None:
        return None
    if diagnostic.module is None:
        annotation_file = diagnostic.file
    command = {"error": "error", "warn": "warning", "info": "notice"}[
        diagnostic.severity
    ]
    properties: list[tuple[str, str]] = []
    if annotation_file is not None:
        for key, value in (
            ("file", annotation_file),
            ("line", str(diagnostic.line) if diagnostic.line is not None else None),
            ("col", str(diagnostic.column) if diagnostic.column is not None else None),
            (
                "endLine",
                str(diagnostic.end_line) if diagnostic.end_line is not None else None,
            ),
            (
                "endColumn",
                str(diagnostic.end_column)
                if diagnostic.end_column is not None
                else None,
            ),
        ):
            if value is not None:
                properties.append(
                    (key, _escape_workflow_value(value, property_value=True))
                )
    suffix = " " + ",".join(f"{key}={value}" for key, value in properties)
    if not properties:
        suffix = ""
    message = _escape_workflow_value(diagnostic.message)
    return f"::{command}{suffix}::{message}"


def _json_check_diagnostic(diagnostic: CheckDiagnostic) -> dict[str, str | int]:
    fields: dict[str, str | int | None] = {
        "severity": diagnostic.severity,
        "code": diagnostic.code,
        "message": diagnostic.message,
        "file": diagnostic.file,
        "line": diagnostic.line,
        "column": diagnostic.column,
        "end_line": diagnostic.end_line,
        "end_column": diagnostic.end_column,
        "schema": diagnostic.schema,
    }
    return {key: value for key, value in fields.items() if value is not None}


def _generate_persisted(
    config_path: Path | None,
    schema_name: str | None,
    explicit_output: str | None,
    *,
    disable_normalization: bool,
    fail_on_warn: bool,
    omit_runtime_source: bool,
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
    candidate_files = project_python_candidates(config)
    selected_files = select_project_sources(
        config,
        candidate_files,
        include_stubs=False,
        validate_entrypoints=False,
    )
    authoring_modules: tuple[str, ...] = ()
    if omit_runtime_source:
        if not config.authoring_roots:
            raise ValueError("authoring_roots are required for source omission")
        authoring_modules = validate_authoring_separation(
            selected_files, config.authoring_roots
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
    omitted_bindings: dict[str, tuple[str, str]] = {}
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
        omitted_bindings[schema.name] = (runtime, stub)
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
    omitted_root: Path | None = None
    omitted_files: dict[str, str] | None = None
    if omit_runtime_source:
        if any(
            item.module.module not in authoring_modules for item in sources.persisted
        ):
            raise ValueError("Persisted declarations must live in authoring_roots")
        release_config = replace(config, schemas=selected_schemas)
        omitted_files = render_omitted_files(
            release_config,
            omitted_bindings,
            schemas={
                name: schema_contexts[(project_id, name)].schema
                for name in omitted_bindings
            },
        )
        omitted_root = config.root / "build" / "graphql-desk" / "omitted"
        validate_omitted_tree(omitted_root)
    if output_text:
        _stage_outputs(output_text)
    if omitted_root is not None and omitted_files is not None:
        publish_omitted_tree(
            omitted_root,
            omitted_files,
            authoring_modules,
            config.generated_package,
        )
    if stdout_text is not None:
        print(stdout_text, end="")
    return 0


def _scan(
    config_path: Path | None,
    schema_name: str | None,
    output: str | None,
    *,
    output_format: str | None,
    graph: bool,
    fail_on_warn: bool,
    stdout_is_terminal: bool,
) -> int:
    selected = effective_project(Path.cwd(), explicit_config=config_path)
    if selected is None:
        raise ValueError("No GraphQL Desk project configuration was found")
    project = selected.config
    report = scan_project(
        config=config_path,
        schema=schema_name,
        environment=dict(os.environ),
    )
    machine = (
        graph
        or output_format == "json"
        or (output_format is None and (output is not None or not stdout_is_terminal))
    )
    if machine:
        text = json_report(report, graph=graph)
    else:
        width = min(shutil.get_terminal_size(fallback=(80, 24)).columns, 120)
        text = terminal_report(report, width=width if stdout_is_terminal else None)
    if output is None:
        print(text, end="")
        if machine and report.warnings and os.environ.get("GITHUB_ACTIONS") != "true":
            print(
                f"Scan completed with {len(report.warnings)} analysis warning(s).",
                file=sys.stderr,
            )
    else:
        destination = Path(output)
        if not destination.is_absolute():
            destination = Path.cwd() / destination
        if destination.is_symlink():
            raise ValueError("Scan output cannot replace a symlink")
        if any(parent.is_symlink() for parent in destination.parents):
            raise ValueError("Scan output cannot pass through a symlink")
        resolved_destination = destination.resolve()
        protected = {
            project.path.resolve(),
            *(
                item.origin.local_path.resolve()
                for item in project.schemas
                if item.origin.local_path is not None
            ),
            *(item.resolve() for item in project_python_candidates(project)),
            *(
                (project.root / item.schema_output).resolve()
                for item in project.schemas
            ),
            *(
                (project.root / path).resolve()
                for item in project.schemas
                for path in (item.persisted_output, item.cache_output)
                if path is not None
            ),
        }
        if resolved_destination in protected:
            raise ValueError("Scan output cannot overwrite a project input or artifact")
        _stage_outputs({resolved_destination: text})
        if report.warnings:
            print(
                f"Scan report written with {len(report.warnings)} analysis warning(s).",
                file=sys.stderr,
            )
    return 1 if fail_on_warn and report.warnings else 0


def _schema_header_arguments(entries: Sequence[str]) -> dict[str, str]:
    headers: dict[str, str] = {}
    for entry in entries:
        name, separator, value = entry.partition(":")
        if not separator or not name.strip(" "):
            raise ValueError("Invalid schema header")
        headers[name.strip(" ")] = value.lstrip(" ")
    return headers


def _generate_schema(
    input: str,
    config_path: Path | None,
    schema_name: str | None,
    explicit_output: str | None,
    *,
    headers: object,
    environment: Mapping[str, str],
    stdout_is_terminal: bool,
) -> int:
    selected = effective_project(Path.cwd(), explicit_config=config_path)
    project = selected.config if selected is not None else None
    configured = None
    if schema_name is not None:
        if project is None:
            raise ValueError("Schema selection requires a project configuration")
        configured = select_schema(project, binding=schema_name)
    elif project is not None and len(project.schemas) == 1:
        if project.schemas[0].name == "default":
            configured = select_schema(project)

    if input.lower().startswith(("http:", "https:")):
        origin = SchemaOrigin(url=input)
        if configured is not None and configured.origin.url == input:
            origin = configured.origin
    else:
        source = Path(input)
        origin = SchemaOrigin(
            local_path=source if source.is_absolute() else Path.cwd() / source
        )
    if headers is not None:
        if not isinstance(headers, Mapping):
            raise ValueError("Invalid schema headers")
        if headers and origin.url is None:
            raise ValueError("Schema headers require a URL input")
        overrides: dict[str, tuple[str, str]] = {}
        for name, value in cast(Mapping[object, object], headers).items():
            if not isinstance(name, str) or not isinstance(value, str):
                raise ValueError("Invalid schema headers")
            overrides[name.lower()] = (name, value)
        origin = replace(
            origin,
            headers=tuple(
                pair for pair in origin.headers if pair[0].lower() not in overrides
            )
            + tuple(overrides.values()),
        )

    destination = None
    if explicit_output is not None:
        if "://" in explicit_output:
            raise ValueError("Schema output must be a local path")
        destination = Path(explicit_output)
        if not destination.is_absolute():
            destination = Path.cwd() / destination
    elif not stdout_is_terminal:
        destination = None
    elif (
        configured is not None
        and configured.origin.local_path is not None
        and configured.origin.local_path.suffix.lower() in {".graphql", ".gql", ".sdl"}
    ):
        destination = (
            configured.origin.configured_local_path or configured.origin.local_path
        )
    else:
        raise ValueError("Select --output, a configured SDL path, or redirect stdout")

    if destination is not None:
        if destination.is_symlink() or any(
            parent.is_symlink() for parent in destination.parents
        ):
            raise ValueError("Schema output cannot pass through a symlink")
        if destination.suffix.lower() in {".py", ".pyi"}:
            raise ValueError("Schema output cannot replace Python sources or stubs")
        protected: set[Path] = set()
        if (
            origin.local_path is not None
            and origin.local_path.suffix.lower() == ".json"
        ):
            protected.add(origin.local_path.resolve())
        if project is not None:
            protected.add(project.path.resolve())
            package = project.root.joinpath(*project.generated_package.split("."))
            if destination.resolve().is_relative_to(package.resolve()):
                raise ValueError("Schema output cannot modify the generated package")
            protected.update(
                path.resolve()
                for path in generated_output_paths(
                    project.root, package, project.schemas
                )
            )
            for item in project.schemas:
                if (
                    item.origin.local_path is not None
                    and item.origin.local_path.suffix.lower() == ".json"
                ):
                    protected.add(item.origin.local_path.resolve())
        if destination.resolve() in protected:
            raise ValueError(
                "Schema output cannot replace project configuration or artifacts"
            )

    loaded = (
        load_local_schema(origin.local_path)
        if origin.local_path is not None
        else load_from_url(origin, environment=environment)
    )
    text = print_schema(loaded)
    if destination is None:
        print(text, end="")
    else:
        _stage_outputs({destination: text})
    print("Schema generated.", file=sys.stderr)
    return 0


def generate_schema(
    *,
    input: Path | str,
    config: Path | str | None = None,
    schema: str | None = None,
    output: Path | str | None = None,
    headers: Mapping[str, str] | None = None,
    silent: bool = False,
    environment: Mapping[str, str] | None = None,
) -> None:
    """Download or convert a schema to SDL without exiting the caller."""
    reporting = StringIO()
    try:
        with redirect_stderr(reporting):
            _generate_schema(
                str(input),
                Path(config) if config is not None else None,
                schema,
                str(output) if output is not None else None,
                headers=headers,
                environment=os.environ if environment is None else environment,
                stdout_is_terminal=True,
            )
    except (OSError, RuntimeError, UnicodeError, ValueError):
        raise SchemaGenerationError(
            ("Check schema input, access, configuration and destination.",)
        ) from None
    if not silent:
        print(reporting.getvalue(), end="", file=sys.stderr)


def _generate_output(
    config_path: Path | None,
    schema_name: str | None,
    explicit_output: str | None,
    *,
    disable_preprocessing: bool | None,
    environment: Mapping[str, str],
    stdout_is_terminal: bool,
) -> int:
    selected = effective_project(Path.cwd(), explicit_config=config_path)
    if selected is None:
        raise ValueError("No GraphQL Desk project configuration was found")
    original = selected.config
    validate_project_ownership((original,))
    schemas = (
        (select_schema(original, binding=schema_name),)
        if schema_name is not None
        else original.schemas
    )
    if explicit_output is not None and len(schemas) != 1:
        raise ValueError("Select one schema when using --output")
    to_stdout = explicit_output is None and not stdout_is_terminal
    if to_stdout and len(schemas) != 1:
        raise ValueError("Select one schema when writing generated types to stdout")
    config = replace(
        original,
        schemas=schemas,
        disable_preprocessing=(
            original.disable_preprocessing
            if disable_preprocessing is None
            else original.disable_preprocessing or disable_preprocessing
        ),
    )
    if explicit_output is not None:
        destination = Path(explicit_output)
        if not destination.is_absolute():
            destination = Path.cwd() / destination
        if destination.resolve() != schemas[0].schema_output.resolve():
            raise ValueError(
                "--output must be the selected schema_output inside generated_package"
            )

    project_id = config.path.as_uri()
    source_files = select_project_sources(
        config,
        project_python_candidates(config),
        include_stubs=False,
        validate_entrypoints=True,
    )
    modules = tuple(
        ModuleSnapshot(
            project_id,
            source.module,
            source.path.as_uri(),
            1,
            source.path.read_text(encoding="utf-8-sig"),
            source.is_package,
        )
        for source in source_files
    )
    bindings = project_bindings(config, project_id=project_id)
    sources = resolve_project(
        modules,
        bindings=(bindings,),
        collect_document_imports=config.check_colocated_fragments,
    )
    contexts: dict[tuple[str, str | None], SchemaContext] = {}
    schema_contexts: list[SchemaContext] = []
    for schema in schemas:
        loaded = (
            load_local_schema(schema.origin.local_path)
            if schema.origin.local_path is not None
            else load_from_url(schema.origin, environment=environment)
        )
        context = SchemaContext(
            schema.name,
            loaded,
            f"factory-output:{project_id}:{schema.name}",
        )
        contexts[(project_id, schema.name)] = context
        if bindings.default_schema == schema.name:
            contexts[(project_id, None)] = context
        schema_contexts.append(context)
    index = index_fragments(sources, schemas=contexts)
    artifacts: FactoryArtifacts = generate_project_factory_artifacts(
        config,
        tuple(schema_contexts),
        index,
        modules=modules,
        project_id=project_id,
    )
    diagnostics = artifacts.diagnostics
    if diagnostics:
        _report_diagnostics(diagnostics, modules)
    if not artifacts.files:
        if not diagnostics:
            print("Schema output could not be generated.", file=sys.stderr)
        return 1

    package_prefix = config.generated_package.replace(".", "/") + "/"
    if to_stdout:
        stub_path = package_prefix + schemas[0].name + ".pyi"
        stub = next(
            (item.text for item in artifacts.files if item.relative_path == stub_path),
            None,
        )
        if stub is None:
            raise ValueError("Generated schema stub is unavailable")
        print(stub, end="")
        return 0

    package_files: dict[str, str] = {}
    for item in artifacts.files:
        if not item.relative_path.startswith(package_prefix):
            raise ValueError("Generated output escapes generated_package")
        package_files[item.relative_path.removeprefix(package_prefix)] = item.text
    publish_output_package(
        config.root.joinpath(*config.generated_package.split(".")),
        package_files,
        generated_package=config.generated_package,
        disable_preprocessing=config.disable_preprocessing,
    )
    return 0


def generate_output(
    *,
    config: Path | str | None = None,
    schema: str | None = None,
    output: Path | str | None = None,
    disable_preprocessing: bool | None = None,
    silent: bool = False,
    environment: Mapping[str, str] | None = None,
) -> None:
    """Generate configured schema and operation typing files."""
    diagnostics = StringIO()
    try:
        with redirect_stderr(diagnostics):
            result = _generate_output(
                Path(config) if config is not None else None,
                schema,
                str(output) if output is not None else None,
                disable_preprocessing=disable_preprocessing,
                environment=os.environ if environment is None else environment,
                stdout_is_terminal=True,
            )
    except OutputGenerationConflict as error:
        raise OutputGenerationError((str(error),)) from None
    except (OSError, RuntimeError, UnicodeError, ValueError):
        messages = tuple(diagnostics.getvalue().splitlines())
        raise OutputGenerationError(
            messages or ("Check configuration, sources, schema access and outputs.",)
        ) from None
    messages = tuple(diagnostics.getvalue().splitlines())
    if result:
        raise OutputGenerationError(
            messages or ("Check configuration, sources, schema access and outputs.",)
        )
    if messages and not silent:
        print("\n".join(messages), file=sys.stderr)


def generate_persisted(
    *,
    config: Path | str | None = None,
    schema: str | None = None,
    output: Path | str | None = None,
    disable_normalization: bool = False,
    fail_on_warn: bool = False,
    omit_runtime_source: bool = False,
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
                omit_runtime_source=omit_runtime_source,
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


def _init(directory: Path) -> int:
    """Set up one project without executing it or installing dependencies."""
    print("GraphQL Desk init (work in progress)", file=sys.stderr)
    proposal = {
        "schema": "<schema path or HTTP(S) URL>",
        "schema_output": "graphql_desk_generated/default.pyi",
    }
    try:
        target = directory if directory.is_absolute() else Path.cwd() / directory
        if target.is_symlink() or any(parent.is_symlink() for parent in target.parents):
            raise ValueError("Init directory cannot pass through a symlink")
        target = target.resolve()
        directory_identity = target.lstat()
        if not stat.S_ISDIR(directory_identity.st_mode):
            raise ValueError("Init requires an existing project directory")
        config_path = target / "pyproject.toml"
        expected = None
        if config_path.exists() or config_path.is_symlink():
            identity = config_path.lstat()
            if not stat.S_ISREG(identity.st_mode):
                raise ValueError("Project configuration must be a regular file")
            if not identity.st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH):
                raise ValueError("Project configuration is read-only")
            with config_path.open("r+b") as stream:
                identity = os.fstat(stream.fileno())
                config_files.check_config_acl(stream.fileno())
                original = stream.read()
                expected = config_files.ConfigSnapshot(
                    original, identity, config_files.config_dacl(stream.fileno())
                )
            if not config_files.same_output(config_path, expected):
                raise ValueError("Project configuration changed")
            document = tomlkit.parse(original.decode("utf-8"))
        else:
            document = tomlkit.document()
        values = cast(dict[str, object], document.unwrap())
        tool = values.get("tool", {})
        if not isinstance(tool, dict):
            raise ValueError("Project tools must be a table")
        settings_value = cast(dict[str, object], tool).get("graphql-desk", {})
        if not isinstance(settings_value, dict):
            raise ValueError("GraphQL Desk settings must be a table")
        settings = cast(dict[str, object], settings_value)
        ancestor = find_project(target.parent) if target.parent != target else None
        try:
            configured = parse_project_config(
                tomlkit.dumps(document), config_path=config_path
            )
        except ValueError:
            configured = None
        if configured is not None:
            validate_project_ownership(
                (configured,) if ancestor is None else (ancestor, configured)
            )
            print("Project is already configured; no files changed.", file=sys.stderr)
            print("Next: graphql-desk generate-output", file=sys.stderr)
            return 0
        if "schemas" in settings:
            raise ValueError("Incomplete named schemas require an explicit edit")
        validation = dict(settings)
        validation.setdefault("schema", "https://example.invalid/graphql")
        existing_package = settings.get("generated_package", "graphql_desk_generated")
        if not isinstance(existing_package, str):
            raise ValueError("Generated package must be a name")
        validation.setdefault(
            "schema_output", existing_package.replace(".", "/") + "/default.pyi"
        )
        parse_project_settings(validation, config_path=config_path)
        additions: dict[str, object] = {}
        if "schema" not in settings:
            print(
                "Schema SDL/JSON path or HTTP(S) URL: ",
                end="",
                file=sys.stderr,
                flush=True,
            )
            source = input().strip()
            checked = parse_project_settings(
                {
                    "schema": source,
                    "schema_output": "graphql_desk_generated/default.pyi",
                },
                config_path=config_path,
            )
            if checked.schemas[0].origin.local_path is not None:
                load_local_schema(checked.schemas[0].origin.local_path)
            additions["schema"] = source
        if "generated_package" not in settings:
            print(
                "Generated package directory [graphql_desk_generated]: ",
                end="",
                file=sys.stderr,
                flush=True,
            )
            selected = input().strip() or "graphql_desk_generated"
            output = target / selected.replace("\\", "/")
            if output.is_symlink() or any(
                parent.is_symlink() for parent in output.parents
            ):
                raise ValueError("Generated package cannot pass through a symlink")
            parts = output.resolve().relative_to(target).parts
            if not parts:
                raise ValueError("Generated package must be below the project root")
            package = ".".join(parts)
            if target.joinpath(*package.split(".")) != output.resolve():
                raise ValueError("Generated directories must be Python package names")
            if package != "graphql_desk_generated":
                additions["generated_package"] = package
        else:
            existing_package = settings["generated_package"]
            if not isinstance(existing_package, str):
                raise ValueError("Generated package must be a name")
            package = existing_package
        if "schema_output" not in settings:
            additions["schema_output"] = package.replace(".", "/") + "/default.pyi"
        merged = {**settings, **additions}
        configured = parse_project_settings(merged, config_path=config_path)
        validate_project_ownership(
            (configured,) if ancestor is None else (ancestor, configured)
        )
        if "tool" not in document:
            document["tool"] = tomlkit.table()
        tool_document = cast(MutableMapping[str, object], document["tool"])
        if "graphql-desk" not in tool_document:
            tool_document["graphql-desk"] = tomlkit.table()
        settings_document = cast(
            MutableMapping[str, object], tool_document["graphql-desk"]
        )
        for key, value in additions.items():
            settings_document[key] = value
        text = tomlkit.dumps(document)
        parse_project_config(text, config_path=config_path)
        preview = dict(additions)
        origin = configured.schemas[0].origin
        if "schema" in preview and origin.url is not None:
            preview["schema"] = "<selected HTTP(S) URL; redacted>"
        print("Proposed keys for [tool.graphql-desk]:", file=sys.stderr)
        print(tomlkit.dumps(preview), end="", file=sys.stderr)
        print(
            "Apply this edit to pyproject.toml? [y/N]: ",
            end="",
            file=sys.stderr,
            flush=True,
        )
        if input().strip().lower() not in {"y", "yes"}:
            print("Init cancelled; no files changed.", file=sys.stderr)
            return 130
        _stage_outputs(
            {config_path: text},
            expected={config_path: expected},
            directory_identity=directory_identity,
        )
        print(
            "Configured pyproject.toml. No dependencies were installed.",
            file=sys.stderr,
        )
        print(
            "Next: graphql-desk generate-output; "
            "then enable the GraphQL Desk VS Code extension. "
            "For URL schemas, generation fetches the schema using configured headers.",
            file=sys.stderr,
        )
        return 0
    except EOFError:
        print("Init cancelled; no files changed.", file=sys.stderr)
        return 130
    except OutputGenerationConflict:
        raise
    except (ValueError, UnicodeError):
        print(
            "Init could not safely merge configuration; no files changed.",
            file=sys.stderr,
        )
        print(
            "Review this minimal edit against the existing configuration:",
            file=sys.stderr,
        )
        print(
            tomlkit.dumps({"tool": {"graphql-desk": proposal}}), end="", file=sys.stderr
        )
        return 1


def main(argv: Sequence[str] | None = None) -> int:
    """Return an exit code; only the module/console entrypoint exits the process."""
    try:
        command_line = list(sys.argv[1:] if argv is None else argv)
        for position in range(len(command_line) - 1):
            alias = command_line[position : position + 2]
            if alias == ["generate", "persisted"]:
                command_line[position : position + 2] = ["generate-persisted"]
                break
            if alias == ["generate", "output"]:
                command_line[position : position + 2] = ["generate-output"]
                break
            if alias == ["generate", "schema"]:
                command_line[position : position + 2] = ["generate-schema"]
                break
        if "--force-ts-format" in command_line:
            print(
                "TypeScript output formats are unsupported; "
                "GraphQL Desk generates Python stubs. "
                "Remove --force-ts-format.",
                file=sys.stderr,
            )
            return 2
        arguments = _parser().parse_args(command_line)
    except ValueError:
        print("Invalid arguments; use --help.", file=sys.stderr)
        return 2
    except SystemExit as error:
        return error.code if isinstance(error.code, int) else 2
    try:
        config_path = Path(arguments.config) if hasattr(arguments, "config") else None
        schema_name = arguments.schema if hasattr(arguments, "schema") else None
        if arguments.command == "init":
            if config_path is not None or schema_name is not None:
                raise ValueError("Init selects its project by directory")
            return _init(Path(arguments.directory))
        if arguments.command == "generate-schema":
            return _generate_schema(
                arguments.input,
                config_path,
                schema_name,
                arguments.output,
                headers=_schema_header_arguments(arguments.header),
                environment=dict(os.environ),
                stdout_is_terminal=sys.stdout.isatty(),
            )
        if arguments.command == "generate-output":
            return _generate_output(
                config_path,
                schema_name,
                arguments.output,
                disable_preprocessing=arguments.disable_preprocessing,
                environment=dict(os.environ),
                stdout_is_terminal=sys.stdout.isatty(),
            )
        if arguments.command == "generate-persisted":
            return _generate_persisted(
                config_path,
                schema_name,
                arguments.output,
                disable_normalization=arguments.disable_normalization,
                fail_on_warn=arguments.fail_on_warn,
                omit_runtime_source=arguments.omit_runtime_source,
                environment=dict(os.environ),
                stdout_is_terminal=sys.stdout.isatty(),
            )
        if arguments.command == "scan":
            return _scan(
                config_path,
                schema_name,
                arguments.output,
                output_format=arguments.format,
                graph=arguments.graph,
                fail_on_warn=arguments.fail_on_warn,
                stdout_is_terminal=sys.stdout.isatty(),
            )
        if arguments.command == "check":
            if arguments.artifact is not None:
                if (
                    arguments.format != "text"
                    or arguments.annotations_output is not None
                ):
                    raise ValueError(
                        "Artifact checks do not support project output options."
                    )
                selected = effective_project(Path.cwd(), explicit_config=config_path)
                if selected is None:
                    raise ValueError("No GraphQL Desk project configuration was found")
                check_wheel(
                    Path(arguments.artifact),
                    selected.config.root
                    / "build"
                    / "graphql-desk"
                    / "omitted"
                    / "manifest.json",
                )
                print("Wheel matches the source-free GraphQL Desk release manifest.")
                return 0
            diagnostics = check(
                config=config_path,
                schema=schema_name,
                environment=dict(os.environ),
            )
            visible = _visible_check_diagnostics(diagnostics, level=arguments.level)
            if arguments.format == "json":
                print(
                    json.dumps(
                        [_json_check_diagnostic(item) for item in visible],
                        ensure_ascii=False,
                    )
                )
            else:
                _report_check_diagnostics(diagnostics, level=arguments.level)
            annotations = tuple(
                annotation
                for item in visible
                if (annotation := _workflow_annotation(item)) is not None
            )
            if arguments.annotations_output is not None:
                output_path = Path(arguments.annotations_output)
                if not output_path.is_absolute():
                    output_path = Path.cwd() / output_path
                output_path.write_text(
                    "\n".join(annotations) + ("\n" if annotations else ""),
                    encoding="utf-8",
                    newline="\n",
                )
            elif (
                arguments.format == "text"
                and os.environ.get("GITHUB_ACTIONS") == "true"
            ):
                for annotation in annotations:
                    print(annotation)
            if any(item.severity == "error" for item in diagnostics):
                return 1
            if arguments.fail_on_warn and any(
                item.severity == "warn" for item in diagnostics
            ):
                return 1
            return 0
        return _doctor(config_path, schema_name, environment=dict(os.environ))
    except KeyboardInterrupt:
        message = (
            "Doctor cancelled."
            if arguments.command == "doctor"
            else "GraphQL Desk command cancelled."
        )
        print(message, file=sys.stderr)
        return 130
    except OutputGenerationConflict as error:
        print(str(error), file=sys.stderr)
        return 1
    except (OSError, RuntimeError, ValueError):
        print(
            "GraphQL Desk command failed; check configuration, sources and outputs.",
            file=sys.stderr,
        )
        return 1
