"""Read-only acquisition of a finite, caller-authorized editor snapshot."""

import hashlib
import json
import os
import re
import sys
from collections.abc import Iterable, Mapping
from dataclasses import astuple, dataclass, field, replace
from pathlib import Path
from typing import cast

from .config import ProjectConfig, SchemaOrigin
from .external_type_sources import (
    capture_external_type_sources,
    external_class_base_symbols,
    interpreter_type_roots,
)
from .project import (
    ProjectConfigDiagnosticError,
    effective_project,
    project_bindings,
    validate_project_ownership,
)
from .source_graph import ModuleSnapshot
from .source_selection import SourceFile, select_project_sources


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
class LocalSchemaPath:
    project: str
    schema_name: str
    path: Path
    canonical_path: Path


@dataclass(frozen=True)
class RemoteSchemaOrigin:
    project: str
    schema_name: str
    origin: SchemaOrigin = field(repr=False)
    config_token: str = field(repr=False)
    refresh_interval: float = 0


@dataclass(frozen=True)
class RemoteSchemaSnapshot:
    """Explicitly fetched facts; stale last-good text never enters analysis."""

    project: str
    schema_name: str
    config_token: str = field(repr=False)
    credential_token: str = field(repr=False)
    version: int
    token: str
    text: str = field(repr=False)
    stale: bool = False


@dataclass(frozen=True)
class ConfigurationDiagnostic:
    uri: str
    version: int | None
    message: str
    start: tuple[int, int]
    end: tuple[int, int]

    def as_json(self) -> dict[str, object]:
        return {
            "uri": self.uri,
            "version": self.version,
            "code": "graphql-desk.configuration",
            "message": self.message,
            "range": {
                "start": {"line": self.start[0], "character": self.start[1]},
                "end": {"line": self.end[0], "character": self.end[1]},
            },
        }


@dataclass(frozen=True)
class EditorPreparation:
    """Schema targets and redacted private origins; no source/schema contents."""

    local_schemas: tuple[LocalSchemaPath, ...]
    workspace: Path = field(repr=False)
    _candidates: tuple[Path, ...] = field(repr=False)
    _observations: tuple[_Observation, ...] = field(repr=False)
    _configs: tuple[tuple[str, str, str], ...] = field(repr=False)
    configuration_diagnostics: tuple[ConfigurationDiagnostic, ...] = ()
    remote_origins: tuple[RemoteSchemaOrigin, ...] = field(default=(), repr=False)
    project_preferences: tuple[tuple[str, tuple[str, ...]], ...] = ()


@dataclass
class _CapturedProjects:
    workspace: Path
    paths: tuple[Path, ...]
    buffers: dict[Path, OpenBuffer]
    observations: dict[tuple[Path, str], _Observation]
    projects: tuple[tuple[ProjectConfig, tuple[SourceFile, ...]], ...]
    config_facts: tuple[tuple[str, str, str], ...]
    configuration_diagnostics: tuple[ConfigurationDiagnostic, ...]


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
    _disk_modules: tuple[ModuleSnapshot, ...] = field(repr=False)
    _saved_modules: tuple[ModuleSnapshot, ...] = field(repr=False)
    _remote_schemas: tuple[RemoteSchemaSnapshot, ...] = field(default=(), repr=False)
    project_configs: tuple[ProjectConfig, ...] = field(default=(), repr=False)
    external_mapping_modules: tuple[ModuleSnapshot, ...] = field(default=(), repr=False)

    @property
    def request(self) -> dict[str, object]:
        # A frozen dataclass alone would not freeze a nested caller-mutable dict.
        return cast(dict[str, object], json.loads(self._request_json))

    @property
    def disk_modules(self) -> tuple[ModuleSnapshot, ...]:
        """Disk-origin modules used to reuse unchanged analysis versions."""
        return self._disk_modules

    @property
    def saved_modules(self) -> tuple[ModuleSnapshot, ...]:
        """Backing-file source snapshots, independent of open-buffer overlays."""
        return self._saved_modules

    def reuse_disk_sources(self, previous: "EditorSnapshot") -> "EditorSnapshot":
        """Retain only exactly matching captured disk module versions."""
        _validate_previous(previous, self.workspace, self.generation)
        old_modules = {
            (item.project, item.module, item.uri): item
            for item in previous._disk_modules
        }
        old_observations = set(previous._observations)
        observations = {
            item.path.as_uri(): item
            for item in self._observations
            if item.origin == "disk"
        }
        retained: list[ModuleSnapshot] = []
        for module in self._disk_modules:
            old = old_modules.get((module.project, module.module, module.uri))
            if (
                old is not None
                and replace(old, version=module.version) == module
                and observations.get(module.uri) in old_observations
            ):
                module = old
            retained.append(module)
        versions = {
            (item.project, item.module, item.uri): item.version for item in retained
        }
        request = self.request
        for item in cast(list[dict[str, object]], request["modules"]):
            key = (
                cast(str, item["project"]),
                cast(str, item["module"]),
                cast(str, item["uri"]),
            )
            if key in versions:
                item["version"] = versions[key]
        return replace(
            self,
            _request_json=json.dumps(request, ensure_ascii=True),
            _disk_modules=tuple(retained),
        )


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


def _utf16_position(text: str, offset: int) -> tuple[int, int]:
    offset = min(max(offset, 0), len(text))
    line = 0
    start = 0
    for match in re.finditer(r"\r\n|\r|\n", text):
        if match.start() >= offset:
            break
        line += 1
        start = match.end()
    return line, len(text[start:offset].encode("utf-16-le")) // 2


def _check_version(value: object) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("Snapshot versions must be nonnegative integers")


def _validate_previous(
    previous: EditorSnapshot, workspace: Path, generation: int
) -> None:
    _check_version(previous.generation)
    if (
        _identity(previous.workspace) != _identity(workspace)
        or previous.generation > generation
    ):
        raise ValueError(
            "Previous snapshot requires the same workspace and no later generation"
        )


def _capture_projects(
    workspace: Path,
    candidates: tuple[Path, ...],
    buffers: tuple[OpenBuffer, ...],
    editor_fallback: Mapping[str, object] | None,
) -> _CapturedProjects:
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
    config_errors: dict[Path, ProjectConfigDiagnosticError] = {}

    config_texts: dict[Path, str | None] = {}

    def capture_config(path: Path) -> None:
        canonical = _identity(path)
        if not canonical.is_relative_to(_identity(base)):
            raise ValueError("Project configuration escapes its workspace")
        if canonical in config_texts:
            return
        if not path.is_file() and (path.exists() or path.is_symlink()):
            raise ValueError("Project configuration must be a file")
        buffer = open_files.get(canonical)
        if buffer is not None:
            text: str | None = buffer.text
            digest = _text_digest(buffer.text)
            origin = "config-buffer"
        elif path.is_file():
            text, digest = _read(path)
            origin = "config-disk"
        else:
            text = digest = None
            origin = "config-disk"
        config_texts[canonical] = text
        observations[(path, "config")] = _Observation(
            path, canonical, origin, buffer.version if buffer else None, digest
        )

    # Complete the closed capture before any discovery or ownership helper runs.
    for path in paths:
        current = _path(path).parent
        while True:
            capture_config(current / "pyproject.toml")
            if current == base:
                break
            current = current.parent
    for path in paths:
        try:
            selected = effective_project(
                _path(path).parent,
                workspace=base,
                editor_fallback=editor_fallback,
                config_texts=config_texts,
            )
        except ProjectConfigDiagnosticError as caught_error:
            config_errors.setdefault(_identity(caught_error.path), caught_error)
            continue
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
    projects = tuple(
        (
            config,
            select_project_sources(
                config, paths, workspace=base, config_texts=config_texts
            ),
        )
        for config in sorted(configs.values(), key=lambda item: str(item.path))
    )
    config_diagnostics: list[ConfigurationDiagnostic] = []
    for path, diagnostic_error in sorted(
        config_errors.items(), key=lambda item: str(item[0])
    ):
        text = config_texts.get(path)
        observation = next(
            (
                item
                for item in observations.values()
                if item.canonical == path and item.origin.startswith("config-")
            ),
            None,
        )
        if text is None or observation is None:
            continue
        start = min(max(diagnostic_error.start, 0), len(text))
        end = min(max(diagnostic_error.end, start), len(text))
        config_diagnostics.append(
            ConfigurationDiagnostic(
                observation.path.as_uri(),
                observation.version,
                diagnostic_error.message,
                _utf16_position(text, start),
                _utf16_position(text, end),
            )
        )
    return _CapturedProjects(
        base,
        paths,
        open_files,
        observations,
        projects,
        tuple(sorted(config_facts.values())),
        tuple(config_diagnostics),
    )


def _check_observations(observations: Iterable[_Observation]) -> None:
    for observation in observations:
        if _identity(observation.path) != observation.canonical:
            raise ValueError("Inputs changed during snapshot acquisition")
        if observation.origin == "config-disk":
            digest = _read(observation.path)[1] if observation.path.is_file() else None
            if digest != observation.digest:
                raise ValueError("Inputs changed during snapshot acquisition")
        elif observation.origin == "saved-disk":
            if not observation.path.is_file():
                raise ValueError("Inputs changed during snapshot acquisition")
            if _read(observation.path)[1] != observation.digest:
                raise ValueError("Inputs changed during snapshot acquisition")
        elif observation.origin == "saved-missing":
            if observation.path.exists() or observation.path.is_symlink():
                raise ValueError("Inputs changed during snapshot acquisition")
        elif (
            observation.origin == "disk"
            and _read(observation.path)[1] != observation.digest
        ):
            raise ValueError("Inputs changed during snapshot acquisition")


def _preparation(captured: _CapturedProjects) -> EditorPreparation:
    observations = tuple(
        sorted(
            captured.observations.values(),
            key=lambda item: (str(item.path), item.origin),
        )
    )
    return EditorPreparation(
        tuple(
            LocalSchemaPath(
                config.path.as_uri(),
                schema.name,
                schema.origin.local_path,
                _identity(schema.origin.local_path),
            )
            for config, _ in captured.projects
            for schema in config.schemas
            if schema.origin.local_path is not None
        ),
        captured.workspace,
        tuple(sorted(set(captured.paths), key=str)),
        observations,
        captured.config_facts,
        captured.configuration_diagnostics,
        tuple(
            RemoteSchemaOrigin(
                config.path.as_uri(),
                schema.name,
                schema.origin,
                _text_digest(
                    repr(
                        (
                            astuple(config),
                            tuple(
                                item
                                for item in observations
                                if item.canonical == _identity(config.path)
                            ),
                        )
                    )
                ),
                config.schema_refresh_interval,
            )
            for config, _ in captured.projects
            for schema in config.schemas
            if schema.origin.url is not None
        ),
        tuple(
            (config.path.as_uri(), tuple(dict.fromkeys(config.client_directives)))
            for config, _ in captured.projects
        ),
    )


def prepare_editor_snapshot(
    *,
    workspace: Path,
    candidates: tuple[Path, ...],
    buffers: tuple[OpenBuffer, ...] = (),
    editor_fallback: Mapping[str, object] | None = None,
) -> EditorPreparation:
    """List configured local schema paths before reading source/schema contents.

    The trusted caller supplies finite source candidates and captured config
    buffers. Paths are the shared parser's resolved paths, not original TOML
    spellings. Missing files and unsupported suffixes remain in the manifest so
    the caller can capture open buffers before full acquisition. This does not
    prove complete enumeration, trust, freshness or schema validity.
    """
    captured = _capture_projects(workspace, candidates, buffers, editor_fallback)
    _check_observations(captured.observations.values())
    return _preparation(captured)


def acquire_editor_snapshot(
    *,
    workspace: Path,
    candidates: tuple[Path, ...],
    buffers: tuple[OpenBuffer, ...] = (),
    generation: int,
    editor_fallback: Mapping[str, object] | None = None,
    previous: EditorSnapshot | None = None,
    preparation: EditorPreparation | None = None,
    remote_schemas: tuple[RemoteSchemaSnapshot, ...] = (),
) -> EditorSnapshot:
    """Acquire exact analyzer facts without processes, imports, writes or fetching.

    The trusted caller must authorize these finite local candidates and supply a
    new monotonic generation for every acquisition. No directory enumeration or
    complete-project discovery is implied. An optional previous snapshot retains
    exact unchanged disk-source versions; it grants no publication permission.
    Local schema paths explicitly selected by config may be outside the workspace;
    Python source candidates may not.
    Open source/schema/config buffers override disk, including new configs. One
    closed config-text capture governs discovery and source ownership throughout.
    A supplied preparation must match that capture before source/schema reads.
    Before publishing, recheck newly captured buffers/candidates/fallback and the
    caller's latest-job guard. Source/schema reads remain best effort, not atomic.
    Remote schemas must be explicitly supplied from the same captured config;
    callers own credential revision and refresh/cancellation. Stale last-good
    records remain unavailable and never provide trusted analyzer contexts.
    """
    _check_version(generation)
    captured = _capture_projects(workspace, candidates, buffers, editor_fallback)
    base, paths = captured.workspace, captured.paths
    if previous is not None:
        _validate_previous(previous, base, generation)
    if preparation is not None and preparation != _preparation(captured):
        raise ValueError("Prepared editor configuration changed before acquisition")
    remote = {(item.project, item.schema_name): item for item in remote_schemas}
    origins = {
        (item.project, item.schema_name): item
        for item in _preparation(captured).remote_origins
    }
    if len(remote) != len(remote_schemas):
        raise ValueError("Duplicate remote schema snapshot")
    remote_schemas = tuple(remote[key] for key in sorted(remote))
    for key, item in remote.items():
        _check_version(item.version)
        if (
            key not in origins
            or item.config_token != origins[key].config_token
            or not item.credential_token
            or not re.fullmatch(r"[A-Za-z0-9_-]+", item.token)
            or not item.text
        ):
            raise ValueError("Remote schema capture differs from configuration")
    open_files = captured.buffers
    observations = captured.observations
    disk_modules: list[ModuleSnapshot] = []
    saved_modules: list[ModuleSnapshot] = []
    modules: list[dict[str, object]] = []
    bindings: list[dict[str, object]] = []
    schemas: list[dict[str, object]] = []
    unavailable: list[tuple[str, str]] = []
    external_mapping_modules: list[ModuleSnapshot] = []

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

    for config, selected_files in captured.projects:
        project_id = config.path.as_uri()
        project_type_sources: list[ModuleSnapshot] = []
        mapping_symbols = tuple(
            sorted(
                {
                    symbol
                    for schema in config.schemas
                    for mapping in schema.scalars
                    for symbol in (mapping.input, mapping.output)
                }
                | {symbol for schema in config.schemas for _, symbol in schema.enums}
            )
        )
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
                        "fragment_composition": f.fragment_composition,
                    }
                    for f in facts.factories
                ],
                "type_mappings": [
                    {
                        "schema_name": schema.name,
                        "scalars": [
                            {
                                "name": item.name,
                                "input": item.input,
                                "output": item.output,
                            }
                            for item in schema.scalars
                        ],
                        "enums": [
                            {"name": name, "type": symbol}
                            for name, symbol in schema.enums
                        ],
                    }
                    for schema in config.schemas
                ],
            }
        )
        for source in selected_files:
            text, version = contents(source.path)
            observation = observations[(source.path, "text")]
            saved_key = (source.path, "saved-source")
            saved_path = _identity(source.path)
            buffered = saved_path in open_files
            saved_text: str | None
            saved_digest: str | None
            if buffered and not saved_path.is_relative_to(_identity(base)):
                raise ValueError("Saved source escapes its workspace")
            if buffered and saved_path.is_file():
                saved_text, saved_digest = _read(saved_path)
            elif buffered:
                saved_text = None
                saved_digest = None
            else:
                saved_text, saved_digest = text, observation.digest
            observations[saved_key] = _Observation(
                source.path,
                _identity(source.path),
                "saved-disk" if saved_text is not None else "saved-missing",
                None,
                saved_digest,
            )
            if saved_text is not None:
                saved_modules.append(
                    ModuleSnapshot(
                        project_id,
                        source.module,
                        source.path.as_uri(),
                        generation,
                        saved_text,
                        source.is_package,
                    )
                )
            module_fact = ModuleSnapshot(
                project_id,
                source.module,
                source.path.as_uri(),
                version,
                text,
                source.is_package,
            )
            project_type_sources.append(module_fact)
            if observation.origin == "disk":
                disk_modules.append(module_fact)
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
        if mapping_symbols:
            shadow_roots = (base, config.root, *config.source_roots)
            try:
                roots = interpreter_type_roots(
                    sys.path,
                    excluded_roots=(base, config.root, *config.source_roots),
                )
                external_mapping_modules.extend(
                    capture_external_type_sources(
                        mapping_symbols,
                        search_roots=roots,
                        project_id=project_id + "#interpreter",
                        shadow_roots=shadow_roots,
                    )
                )
            except (OSError, RuntimeError, ValueError):
                # Missing, ambiguous, or oversized external packages stay unverified.
                roots = ()
            base_symbols = external_class_base_symbols(tuple(project_type_sources))
            if base_symbols and roots:
                try:
                    external_bases = capture_external_type_sources(
                        base_symbols,
                        search_roots=roots,
                        project_id=project_id + "#interpreter",
                        shadow_roots=shadow_roots,
                    )
                except (OSError, RuntimeError, ValueError):
                    # An oversized unrelated base graph must not erase mapping facts.
                    pass
                else:
                    captured_external_names = {
                        item.module
                        for item in external_mapping_modules
                        if item.project == project_id + "#interpreter"
                    }
                    external_mapping_modules.extend(
                        item
                        for item in external_bases
                        if item.module not in captured_external_names
                    )
        for schema in config.schemas:
            if schema.origin.local_path is None:
                supplied = remote.get((project_id, schema.name))
                if supplied is None or supplied.stale:
                    unavailable.append((project_id, schema.name))
                    continue
                schemas.append(
                    {
                        "project": project_id,
                        "schema_name": schema.name,
                        "uri": f"untitled:/graphql-desk/remote/{supplied.token}",
                        "version": supplied.version,
                        "token": supplied.token,
                        "format": "introspection",
                        "text": supplied.text,
                    }
                )
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
    # Helpers consumed one captured config map; remaining reads are best effort.
    # Mandatory full reacquisition and the latest-job guard still govern publish.
    _check_observations(observations.values())
    request = {
        "request_id": str(generation),
        "modules": modules,
        "bindings": bindings,
        "schemas": schemas,
    }
    snapshot = EditorSnapshot(
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
        captured.config_facts,
        tuple(disk_modules),
        tuple(saved_modules),
        remote_schemas,
        tuple(config for config, _ in captured.projects),
        tuple(external_mapping_modules),
    )
    return snapshot.reuse_disk_sources(previous) if previous is not None else snapshot


def is_editor_snapshot_current(
    snapshot: EditorSnapshot,
    *,
    candidates: tuple[Path, ...],
    buffers: tuple[OpenBuffer, ...] = (),
    editor_fallback: Mapping[str, object] | None = None,
    remote_schemas: tuple[RemoteSchemaSnapshot, ...] = (),
) -> bool:
    """Reobserve finite inputs; a true result still needs the latest-job guard."""
    try:
        observed = acquire_editor_snapshot(
            workspace=snapshot.workspace,
            candidates=candidates,
            buffers=buffers,
            generation=snapshot.generation,
            editor_fallback=editor_fallback,
            previous=snapshot,
            remote_schemas=remote_schemas,
        )
    except (OSError, ValueError, RuntimeError):
        return False
    return observed == snapshot
