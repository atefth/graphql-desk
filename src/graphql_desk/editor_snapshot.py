"""Read-only acquisition of a finite, caller-authorized editor snapshot."""

import hashlib
import json
import os
from collections.abc import Mapping
from dataclasses import astuple, dataclass, field
from pathlib import Path
from typing import cast

from .config import ProjectConfig
from .project import effective_project, project_bindings, validate_project_ownership
from .source_selection import select_project_sources


@dataclass(frozen=True)
class OpenBuffer:
    path: Path
    version: int
    text: str = field(repr=False)
    is_dirty: bool = False


@dataclass(frozen=True)
class _Observation:
    path: Path
    canonical: Path
    origin: str
    version: int | None
    digest: str | None


@dataclass(frozen=True)
class EditorSnapshot:
    """A request copy and private immutable freshness facts, not a publish permit."""

    unavailable_schemas: tuple[tuple[str, str], ...]
    workspace: Path = field(repr=False)
    generation: int = field(repr=False)
    _candidates: tuple[Path, ...] = field(repr=False)
    _request_json: str = field(repr=False)
    _observations: tuple[_Observation, ...] = field(repr=False)
    _configs: tuple[tuple[str, str, str], ...] = field(repr=False)

    @property
    def request(self) -> dict[str, object]:
        # A frozen dataclass alone would not freeze a nested caller-mutable dict.
        return cast(dict[str, object], json.loads(self._request_json))


def _path(path: Path) -> Path:
    try:
        return path.resolve()
    except (OSError, ValueError, RuntimeError):
        raise ValueError("Cannot resolve a snapshot path") from None


def _identity(path: Path) -> Path:
    return Path(os.path.normcase(str(_path(path))))


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read(path: Path) -> tuple[str, str]:
    try:
        data = path.read_bytes()
        return data.decode("utf-8-sig"), _digest(data)
    except (OSError, UnicodeError):
        raise ValueError("Cannot read a selected UTF-8 local file") from None


def _text_digest(text: str) -> str:
    return _digest(text.encode("utf-8", "surrogatepass"))


def _check_version(value: object) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("Snapshot versions must be nonnegative integers")


def acquire_editor_snapshot(
    *,
    workspace: Path,
    candidates: tuple[Path, ...],
    buffers: tuple[OpenBuffer, ...] = (),
    generation: int,
    editor_fallback: Mapping[str, object] | None = None,
) -> EditorSnapshot:
    """Acquire exact analyzer facts without processes, imports, writes or fetching.

    The trusted caller must authorize these finite local candidates and supply a
    new monotonic generation for every acquisition. No directory enumeration or
    complete-project discovery is implied. Local schema paths explicitly selected
    by config may be outside the workspace; Python source candidates may not.
    Open buffers override disk. Dirty/new config buffers fail closed until shared
    overlay-aware configuration discovery exists. Before publishing, recheck with
    newly captured buffers/candidates/fallback and the caller's latest-job guard.
    Reads are best effort, not atomic: config can change A-to-B-to-A across the
    existing helpers' separate reads without the final byte check noticing.
    """
    _check_version(generation)
    base = _path(workspace)
    if not base.is_dir():
        raise ValueError("Editor workspace must be a directory")
    open_files: dict[Path, OpenBuffer] = {}
    for buffer in buffers:
        _check_version(buffer.version)
        key = _identity(base / buffer.path)
        existing = open_files.get(key)
        normalized = OpenBuffer(key, buffer.version, buffer.text, buffer.is_dirty)
        if existing is not None and existing != normalized:
            raise ValueError("Conflicting open buffers for one canonical file")
        open_files[key] = normalized
    paths = tuple(dict.fromkeys(base / candidate for candidate in candidates))
    if any(not _path(path).is_relative_to(base) for path in paths):
        raise ValueError("Editor source candidate escapes its workspace")
    observations: dict[tuple[Path, str], _Observation] = {}
    configs: dict[Path, ProjectConfig] = {}
    config_facts: dict[str, tuple[str, str, str]] = {}

    def config_guard(path: Path) -> None:
        canonical = _identity(path)
        if not canonical.is_relative_to(_identity(base)):
            raise ValueError("Project configuration escapes its workspace")
        buffer = open_files.get(canonical)
        if buffer is not None and buffer.is_dirty:
            raise ValueError("Unsaved project configuration is not supported yet")
        if path.is_file():
            text, digest = _read(path)
            if buffer is not None and buffer.text != text:
                raise ValueError("Unsaved project configuration is not supported yet")
        else:
            if path.exists() or path.is_symlink():
                raise ValueError("Project configuration must be a file")
            if buffer is not None:
                raise ValueError("Unsaved project configuration is not supported yet")
            digest = None
        observations[(path, "config")] = _Observation(
            path, canonical, "config", buffer.version if buffer else None, digest
        )

    for path in paths:
        current = _path(path).parent
        while True:
            config_guard(current / "pyproject.toml")
            if current == base:
                break
            current = current.parent
        selected = effective_project(
            _path(path).parent, workspace=base, editor_fallback=editor_fallback
        )
        if selected is None:
            continue
        config = selected.config
        if config.source_scope != "project":
            raise ValueError("Editor acquisition cannot authorize explicit CLI scope")
        identity = _identity(config.path)
        if identity in configs and configs[identity] != config:
            raise ValueError("Conflicting project facts during acquisition")
        configs[identity] = config
        project_id = config.path.as_uri()
        # Full validated facts are hashed privately, including redacted repr fields.
        config_facts[project_id] = (
            project_id,
            selected.origin,
            _text_digest(repr(astuple(config))),
        )
    validate_project_ownership(configs.values())
    modules: list[dict[str, object]] = []
    bindings: list[dict[str, object]] = []
    schemas: list[dict[str, object]] = []
    unavailable: list[tuple[str, str]] = []

    def contents(path: Path) -> tuple[str, int]:
        key = _identity(path)
        buffer = open_files.get(key)
        if buffer is not None:
            observations[(path, "text")] = _Observation(
                path, key, "buffer", buffer.version, _text_digest(buffer.text)
            )
            return buffer.text, buffer.version
        text, digest = _read(path)
        observations[(path, "text")] = _Observation(path, key, "disk", None, digest)
        return text, generation

    for config in sorted(configs.values(), key=lambda item: str(item.path)):
        project_id = config.path.as_uri()
        selected_files = select_project_sources(config, paths, workspace=base)
        facts = project_bindings(config, project_id=project_id)
        bindings.append(
            {
                "project": facts.project,
                "schemas": list(facts.schemas),
                "default_schema": facts.default_schema,
                "factories": [
                    {
                        "module": f.module,
                        "export": f.export,
                        "schema_name": f.schema_name,
                    }
                    for f in facts.factories
                ],
            }
        )
        for source in selected_files:
            text, version = contents(source.path)
            modules.append(
                {
                    "project": project_id,
                    "module": source.module,
                    "uri": source.path.as_uri(),
                    "version": version,
                    "source": text,
                    "is_package": source.is_package,
                }
            )
        for schema in config.schemas:
            if schema.origin.local_path is None:
                unavailable.append((project_id, schema.name))
                continue
            schema_path = _path(schema.origin.local_path)
            suffix = schema_path.suffix.lower()
            if suffix not in {".graphql", ".gql", ".sdl", ".json"}:
                raise ValueError("Unsupported local schema format")
            text, version = contents(schema_path)
            # shortcut: Generation conservatively invalidates all schema contexts.
            # Refine tokens only with future incremental-cache/performance work.
            schemas.append(
                {
                    "project": project_id,
                    "schema_name": schema.name,
                    "uri": schema_path.as_uri(),
                    "version": version,
                    "token": f"{generation}:{project_id}:{schema.name}",
                    "format": "introspection" if suffix == ".json" else "sdl",
                    "text": text,
                }
            )
    # Best-effort change detection; mandatory reacquisition still guards publish.
    # In particular, ABA across separate config-helper reads can escape this check.
    for observation in observations.values():
        if _identity(observation.path) != observation.canonical:
            raise ValueError("Inputs changed during snapshot acquisition")
        if observation.origin == "config":
            digest = _read(observation.path)[1] if observation.path.is_file() else None
            if digest != observation.digest:
                raise ValueError("Inputs changed during snapshot acquisition")
        elif (
            observation.origin == "disk"
            and _read(observation.path)[1] != observation.digest
        ):
            raise ValueError("Inputs changed during snapshot acquisition")
    request = {
        "request_id": str(generation),
        "modules": modules,
        "bindings": bindings,
        "schemas": schemas,
    }
    return EditorSnapshot(
        tuple(unavailable),
        base,
        generation,
        tuple(sorted({_path(path) for path in paths}, key=str)),
        json.dumps(request, ensure_ascii=True),
        tuple(
            sorted(
                observations.values(), key=lambda item: (str(item.path), item.origin)
            )
        ),
        tuple(sorted(config_facts.values())),
    )


def is_editor_snapshot_current(
    snapshot: EditorSnapshot,
    *,
    candidates: tuple[Path, ...],
    buffers: tuple[OpenBuffer, ...] = (),
    editor_fallback: Mapping[str, object] | None = None,
) -> bool:
    """Reobserve finite inputs; a true result still needs the latest-job guard."""
    try:
        observed = acquire_editor_snapshot(
            workspace=snapshot.workspace,
            candidates=candidates,
            buffers=buffers,
            generation=snapshot.generation,
            editor_fallback=editor_fallback,
        )
    except (OSError, ValueError, RuntimeError):
        return False
    return observed == snapshot
