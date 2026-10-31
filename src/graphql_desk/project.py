"""Nearest project and schema selection without source discovery or execution."""

import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal

from .config import (
    ProjectConfig,
    ProjectConfigParseError,
    SchemaConfig,
    generated_output_paths,
    load_project_config,
    parse_project_config,
    parse_project_settings,
)
from .source_graph import FactoryBinding, ProjectBindings, ResolvedCall


class ProjectConfigDiagnosticError(ValueError):
    """Configuration parse error anchored to its selected pyproject.toml."""

    def __init__(self, path: Path, error: ProjectConfigParseError) -> None:
        super().__init__(str(error))
        self.path = path
        self.message = error.message
        self.start = error.start
        self.end = error.end


def _canonical(path: Path) -> Path:
    try:
        return Path(os.path.normcase(str(path.resolve())))
    except (OSError, ValueError, RuntimeError):
        raise ValueError("Cannot resolve a project path") from None


def normalize_config_texts(
    texts: Mapping[Path, str | None] | None,
) -> dict[Path, str | None] | None:
    if texts is None:
        return None
    captured: dict[Path, str | None] = {}
    for path, text in texts.items():
        identity = _canonical(path)
        if identity in captured and captured[identity] != text:
            raise ValueError("Conflicting captured configuration aliases")
        captured[identity] = text
    return captured


def _load_config(
    path: Path,
    scope: Literal["project", "explicit_cli"],
    texts: Mapping[Path, str | None] | None,
) -> ProjectConfig | None:
    try:
        if texts is None:
            return load_project_config(path, source_scope=scope)
        identity = _canonical(path)
        if identity not in texts:
            raise ValueError("Configuration path was not captured")
        text = texts[identity]
        return (
            None
            if text is None
            else parse_project_config(text, config_path=identity, source_scope=scope)
        )
    except ProjectConfigParseError as error:
        raise ProjectConfigDiagnosticError(path, error) from None


def find_project(
    directory: Path,
    *,
    workspace: Path | None = None,
    selected_config: ProjectConfig | None = None,
    config_texts: Mapping[Path, str | None] | None = None,
) -> ProjectConfig | None:
    """Find the nearest configured ancestor, including the workspace boundary.

    Inactive pyprojects do not hide configured ancestors. No source enumeration,
    glob matching or document binding analysis is performed here. Supplying no
    workspace enables the CLI's upward search to the filesystem root.
    config_texts, when supplied, is a closed captured map: None means absence,
    and a missing key rejects instead of falling back to a later disk read.
    Captured values are authoritative input facts, not current file-type claims.
    """
    texts = normalize_config_texts(config_texts)
    current = _canonical(directory)
    boundary = _canonical(workspace) if workspace is not None else None
    if current.is_file():
        raise ValueError("Project search requires a directory")
    if boundary is not None and not current.is_relative_to(boundary):
        raise ValueError("Project search escapes its workspace")
    while True:
        candidate = current / "pyproject.toml"
        resolved = _canonical(candidate)
        if boundary is not None and not resolved.is_relative_to(boundary):
            raise ValueError("Project configuration escapes its workspace")
        if texts is not None or resolved.is_file():
            scope = (
                selected_config.source_scope
                if selected_config is not None
                and resolved == _canonical(selected_config.path)
                else "project"
            )
            config = _load_config(resolved, scope, texts)
            if config is not None:
                return config
        elif candidate.exists() or candidate.is_symlink():
            raise ValueError("Project configuration must be a file")
        if current == boundary or current == current.parent:
            return None
        current = current.parent


@dataclass(frozen=True)
class EffectiveProject:
    config: ProjectConfig
    origin: Literal["explicit", "nearest", "editor_fallback"]


def effective_project(
    directory: Path,
    *,
    explicit_config: Path | None = None,
    workspace: Path | None = None,
    editor_fallback: Mapping[str, object] | None = None,
    config_texts: Mapping[Path, str | None] | None = None,
) -> EffectiveProject | None:
    """Choose one entire project config before considering editor fallback.

    An explicit config is a user-selected CLI path and can lie outside the
    nearest-search workspace. Relative explicit paths use directory as CLI cwd.
    Fallback paths use the workspace root; its config.path is virtual context.
    Command-specific option overrides belong to the future CLI dispatcher.
    """
    texts = normalize_config_texts(config_texts)
    if explicit_config is not None:
        selected = _load_config(
            _canonical(directory / explicit_config), "explicit_cli", texts
        )
        if selected is None:
            raise ValueError("Explicit configuration has no GraphQL Desk project")
        return EffectiveProject(selected, "explicit")
    selected = find_project(directory, workspace=workspace, config_texts=texts)
    if selected is not None:
        return EffectiveProject(selected, "nearest")
    if editor_fallback is None:
        return None
    if workspace is None:
        raise ValueError("Editor fallback requires a workspace root")
    context = _canonical(workspace) / "pyproject.toml"
    return EffectiveProject(
        parse_project_settings(editor_fallback, config_path=context), "editor_fallback"
    )


def select_schema(
    project: ProjectConfig, *, binding: str | None = None
) -> SchemaConfig:
    """Resolve a static binding; only a single default permits an unbound call."""
    if binding is None:
        if len(project.schemas) == 1 and project.schemas[0].name == "default":
            return project.schemas[0]
        raise ValueError("Named schemas require an explicit resolved schema binding")
    for schema in project.schemas:
        if schema.name == binding:
            return schema
    raise ValueError("Unknown schema binding")


def project_bindings(config: ProjectConfig, *, project_id: str) -> ProjectBindings:
    """Describe configured constructors without importing generated modules.

    The caller supplies the same opaque identity used in ModuleSnapshot.project;
    no identity is guessed from paths. This does not create generated modules or
    establish source eligibility, credentials or static symbol availability.
    """
    if not project_id:
        raise ValueError("Project identity must be nonempty")
    schemas = tuple(schema.name for schema in config.schemas)
    default = "default" if schemas == ("default",) else None
    factories = [
        FactoryBinding(f"{config.generated_package}.{schema}", "graphql", schema, True)
        for schema in schemas
    ]
    if config.template is not None:
        module, _, export = config.template.rpartition(".")
        if (module, export) == ("graphql_desk", "init_graphql"):
            raise ValueError("The schema initializer cannot be a document template")
        if not any(
            (item.module, item.export) == (module, export) for item in factories
        ):
            factories.append(
                FactoryBinding(
                    module,
                    export,
                    fragment_composition=config.template_fragment_composition,
                )
            )
    return ProjectBindings(project_id, schemas, default, tuple(factories))


def schema_for_call(
    config: ProjectConfig, call: ResolvedCall, *, project_id: str
) -> SchemaConfig:
    """Route actual schema metadata, leaving call provenance/version facts intact.

    call.binding is a Python assignment name and never selects a schema. This
    checks project/schema identity only, not source matching or snapshot freshness.
    """
    if not project_id or call.module.project != project_id:
        raise ValueError("Resolved call belongs to a different project")
    return select_schema(config, binding=call.schema_name)


def _overlap(first: Path, second: Path) -> bool:
    return first.is_relative_to(second) or second.is_relative_to(first)


def validate_project_ownership(projects: Iterable[ProjectConfig]) -> None:
    """Reject shared generated ownership for a supplied project snapshot.

    Nested source roots are allowed: nearest-project selection takes precedence.
    This does not discover every project, assign source files or authorize writes;
    callers must supply all relevant projects and recheck before any mutation.
    """
    seen: dict[Path, ProjectConfig] = {}
    # Only this finite input snapshot is retained; there is no project registry.
    checked: list[tuple[Path, tuple[Path, ...], tuple[Path, ...]]] = []
    for project in projects:
        identity = _canonical(project.path)
        normalized = replace(project, path=identity, root=_canonical(project.root))
        if identity in seen:
            if seen[identity] != normalized:
                raise ValueError("Conflicting snapshots for one project")
            continue
        seen[identity] = normalized
        root = _canonical(project.root)
        package = _canonical(root.joinpath(*project.generated_package.split(".")))
        outputs = tuple(
            _canonical(path)
            for path in generated_output_paths(root, package, project.schemas)
        )
        inputs = (
            identity,
            *(
                _canonical(schema.origin.local_path)
                for schema in project.schemas
                if schema.origin.local_path is not None
            ),
        )
        for index, output in enumerate(outputs):
            if any(_overlap(output, other) for other in (*outputs[:index], *inputs)):
                raise ValueError("Project generated paths collide")
        for other_package, other_outputs, other_inputs in checked:
            if (
                _overlap(package, other_package)
                or any(
                    _overlap(output, other_package)
                    or any(
                        _overlap(output, other)
                        for other in (*other_outputs, *other_inputs)
                    )
                    for output in outputs
                )
                or any(
                    _overlap(other, package)
                    or any(_overlap(other, path) for path in inputs)
                    for other in other_outputs
                )
            ):
                raise ValueError("Projects share generated output ownership")
        checked.append((package, outputs, inputs))
