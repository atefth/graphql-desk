"""Nearest project and schema selection without source discovery or execution."""

import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal

from .config import (
    ProjectConfig,
    SchemaConfig,
    generated_output_paths,
    load_project_config,
    parse_project_settings,
)


def _canonical(path: Path) -> Path:
    try:
        return Path(os.path.normcase(str(path.resolve())))
    except (OSError, ValueError, RuntimeError):
        raise ValueError("Cannot resolve a project path") from None


def find_project(
    directory: Path, *, workspace: Path | None = None
) -> ProjectConfig | None:
    """Find the nearest configured ancestor, including the workspace boundary.

    Inactive pyprojects do not hide configured ancestors. No source enumeration,
    glob matching or document binding analysis is performed here. Supplying no
    workspace enables the CLI's upward search to the filesystem root.
    """
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
        if resolved.is_file():
            config = load_project_config(resolved)
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
) -> EffectiveProject | None:
    """Choose one entire project config before considering editor fallback.

    An explicit config is a user-selected CLI path and can lie outside the
    nearest-search workspace. Relative explicit paths use directory as CLI cwd.
    Fallback paths use the workspace root; its config.path is virtual context.
    Command-specific option overrides belong to the future CLI dispatcher.
    """
    if explicit_config is not None:
        selected = load_project_config(_canonical(directory / explicit_config))
        if selected is None:
            raise ValueError("Explicit configuration has no GraphQL Desk project")
        return EffectiveProject(selected, "explicit")
    selected = find_project(directory, workspace=workspace)
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
