"""Trusted-caller editor worker; only explicit config staging writes files."""

import base64
import gzip
import hashlib
import json
import math
import os
import re
import sys
import traceback
from collections.abc import MutableMapping, MutableSequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import BinaryIO, cast
from uuid import uuid4

import tomlkit
from graphql import introspection_from_schema

from .config_files import (
    ConfigFileStage,
    capture_config_directories,
)
from .doctor import collect_snapshot_health
from .editor_analysis import (
    EditorAnalysis,
    FragmentReuse,
    analyze_editor_snapshot_details,
    editor_diagnostic,
    utf16_points,
)
from .editor_snapshot import (
    EditorPreparation,
    EditorSnapshot,
    OpenBuffer,
    RemoteSchemaSnapshot,
    acquire_editor_snapshot,
    is_editor_snapshot_current,
    prepare_editor_snapshot,
)
from .factory_generation import (
    FactoryArtifacts,
    GeneratedFile,
    generate_project_factory_artifacts,
)
from .field_usage import FieldUsageOptions
from .fragments import FragmentIndex
from .parse_generation import (
    render_captured_parse_companions,
    render_captured_parse_overlay,
)
from .quarantine import quarantine_generated_stubs
from .schema_url import load_from_url
from .source_graph import (
    DocumentIdentity,
    IncrementalResolver,
    ModuleDependency,
    ModuleSnapshot,
)
from .testing_helpers import render_testing_helper_artifacts

MAX_LINE_BYTES = 8 * 1024 * 1024
ANALYSIS_PAGE_BYTES = 4 * 1024 * 1024
MAX_ANALYSIS_PAGES = 16
MAX_PYRIGHT_CONFIG_BYTES = 1024 * 1024
LIVE_STUB_ROOT = ".graphql-desk/live-typings"
LIVE_GENERATION_STUB_PATH = re.compile(
    r"\.graphql-desk/live-typings/generations/g-\d+-[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\Z"
)
_CAPTURE_KEYS = {"workspace", "candidates", "buffers", "documents", "editor_fallback"}
_MANAGED_PYRIGHT_STUB_PATH = re.compile(
    r"\.graphql-desk/live-typings(?:/generations/g-\d+-[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})?\Z"
)


def _object(value: object, keys: set[str]) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError("Invalid object")
    item = cast(dict[str, object], value)
    if set(item) != keys:
        raise ValueError("Invalid object")
    return item


def _array(value: object) -> list[object]:
    if not isinstance(value, list):
        raise ValueError("Invalid array")
    return cast(list[object], value)


def _string(value: object) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError("Invalid string")
    return value


def _version(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("Invalid version")
    return value


def _boolean(value: object) -> bool:
    if not isinstance(value, bool):
        raise ValueError("Invalid boolean")
    return value


def _pyright_config_request(value: dict[str, object]) -> dict[str, object]:
    if set(value) != {"op", "workspace", "text", "action", "managed_path", "ownership"}:
        raise ValueError("Invalid Pyright config request")
    workspace = _path(value["workspace"])
    text = value["text"]
    action = value["action"]
    managed_path = value["managed_path"]
    ownership_value = value["ownership"]
    if not isinstance(ownership_value, dict):
        raise ValueError("Invalid Pyright config request")
    ownership_record = cast(dict[str, object], ownership_value)
    if (
        not workspace.is_dir()
        or not isinstance(text, str)
        or len(text.encode("utf-8")) > MAX_PYRIGHT_CONFIG_BYTES
        or action
        not in {
            "inspect",
            "add",
            "remove",
            "set_stub_path",
            "restore_stub_path",
            "prepare_stub_path",
            "release_stub_path",
        }
        or not isinstance(managed_path, str)
        or not managed_path
        or "\x00" in managed_path
        or action != "inspect"
        and Path(managed_path).is_absolute()
        or action == "set_stub_path"
        and LIVE_GENERATION_STUB_PATH.fullmatch(managed_path) is None
        or action != "set_stub_path"
        and managed_path != LIVE_STUB_ROOT
        or not {"top_level", "execution_environment_roots"}.issubset(ownership_record)
        or set(ownership_record)
        - {
            "top_level",
            "execution_environment_roots",
            "expected_stub_path_present",
            "expected_stub_path",
            "restore_stub_path_present",
            "restore_stub_path",
            "custom_stub_path",
            "custom_stub_path_added",
            "remove_extra_paths_key",
            "original_stub_path_present",
            "original_stub_path",
            "remove_managed_paths",
            "legacy_top_level",
            "legacy_execution_environment_roots",
        }
        or action in {"inspect", "add", "remove"}
        and set(ownership_record) != {"top_level", "execution_environment_roots"}
        or action == "set_stub_path"
        and set(ownership_record)
        != {
            "top_level",
            "execution_environment_roots",
            "expected_stub_path_present",
            "expected_stub_path",
        }
        or action == "restore_stub_path"
        and set(ownership_record)
        != {
            "top_level",
            "execution_environment_roots",
            "expected_stub_path_present",
            "expected_stub_path",
            "restore_stub_path_present",
            "restore_stub_path",
        }
        or action == "prepare_stub_path"
        and set(ownership_record)
        != {
            "top_level",
            "execution_environment_roots",
            "expected_stub_path_present",
            "expected_stub_path",
            "custom_stub_path",
            "custom_stub_path_added",
            "remove_extra_paths_key",
            "remove_managed_paths",
            "legacy_top_level",
            "legacy_execution_environment_roots",
        }
        or action == "release_stub_path"
        and set(ownership_record)
        != {
            "top_level",
            "execution_environment_roots",
            "expected_stub_path_present",
            "expected_stub_path",
            "custom_stub_path",
            "custom_stub_path_added",
            "remove_extra_paths_key",
            "original_stub_path_present",
            "original_stub_path",
        }
        or not isinstance(ownership_record.get("top_level"), bool)
        or not isinstance(ownership_record.get("execution_environment_roots"), list)
        or any(
            not isinstance(item, str) or not item
            for item in cast(
                list[object], ownership_record.get("execution_environment_roots")
            )
        )
    ):
        raise ValueError("Invalid Pyright config request")
    ownership = ownership_record
    owned_environment_roots = cast(list[str], ownership["execution_environment_roots"])
    if action in {"prepare_stub_path", "release_stub_path"} and len(
        set(owned_environment_roots)
    ) != len(owned_environment_roots):
        raise ValueError("Duplicate custom stub execution environment ownership")
    document = cast(MutableMapping[str, object], tomlkit.parse(text))
    tool_value = document.get("tool")
    if tool_value is None:
        return {
            "ok": True,
            "found": False,
            "text": text,
            "stub_path": None,
            "has_stub_path": False,
            "extra_paths": [],
            "has_execution_environments": False,
            "execution_environments": [],
            "ownership": {"top_level": True, "execution_environment_roots": []},
        }
    if not isinstance(tool_value, MutableMapping):
        raise ValueError("Invalid TOML tool table")
    tool = cast(MutableMapping[str, object], tool_value)
    if "pyright" not in tool:
        return {
            "ok": True,
            "found": False,
            "text": text,
            "stub_path": None,
            "has_stub_path": False,
            "extra_paths": [],
            "has_execution_environments": False,
            "execution_environments": [],
            "ownership": {"top_level": True, "execution_environment_roots": []},
        }
    pyright_value = tool["pyright"]
    if not isinstance(pyright_value, MutableMapping):
        raise ValueError("Invalid Pyright table")
    pyright = cast(MutableMapping[str, object], pyright_value)
    extra_paths_value = pyright.get("extraPaths", [])
    if not isinstance(extra_paths_value, MutableSequence):
        raise ValueError("Invalid Pyright extraPaths")
    extra_paths = cast(MutableSequence[object], extra_paths_value)
    if any(not isinstance(item, str) for item in extra_paths):
        raise ValueError("Invalid Pyright extraPaths")
    stub_path = pyright.get("stubPath")
    has_stub_path = "stubPath" in pyright
    if stub_path is not None and not isinstance(stub_path, str):
        raise ValueError("Invalid Pyright stubPath")
    if action in {
        "set_stub_path",
        "restore_stub_path",
        "prepare_stub_path",
        "release_stub_path",
    }:
        expected_present = ownership_record.get("expected_stub_path_present")
        expected_path = ownership_record.get("expected_stub_path")
        if (
            not isinstance(expected_present, bool)
            or expected_present
            and not isinstance(expected_path, str)
            or has_stub_path != expected_present
            or expected_present
            and stub_path != expected_path
        ):
            raise ValueError(
                "Pyright stubPath changed before GraphQL Desk could update it; "
                "preserved"
            )
        if action == "set_stub_path":
            stub_path = managed_path
            pyright["stubPath"] = managed_path
            has_stub_path = True
        elif action == "restore_stub_path":
            restore_present = ownership_record.get("restore_stub_path_present")
            restore_path = ownership_record.get("restore_stub_path")
            if (
                not isinstance(restore_present, bool)
                or restore_present
                and not isinstance(restore_path, str)
            ):
                raise ValueError("Invalid original Pyright stubPath; preserved")
            if restore_present:
                stub_path = restore_path
                pyright["stubPath"] = restore_path
            else:
                stub_path = None
                pyright.pop("stubPath", None)
            has_stub_path = restore_present
    execution_environments_value = pyright.get("executionEnvironments", [])
    if not isinstance(execution_environments_value, MutableSequence):
        raise ValueError("Invalid Pyright executionEnvironments")
    execution_environments = cast(MutableSequence[object], execution_environments_value)
    environment_roots: set[str] = set()
    environments: list[MutableMapping[str, object]] = []
    for environment_value in execution_environments:
        if not isinstance(environment_value, MutableMapping):
            raise ValueError("Invalid Pyright execution environment root")
        environment = cast(MutableMapping[str, object], environment_value)
        if not isinstance(environment.get("root"), str) or not environment["root"]:
            raise ValueError("Invalid Pyright execution environment root")
        root = cast(str, environment["root"])
        if root in environment_roots:
            raise ValueError("Duplicate Pyright execution environment root")
        environment_roots.add(root)
        paths_value = environment.get("extraPaths", [])
        if not isinstance(paths_value, MutableSequence):
            raise ValueError("Invalid execution environment extraPaths")
        paths = cast(MutableSequence[object], paths_value)
        if any(not isinstance(item, str) for item in paths):
            raise ValueError("Invalid execution environment extraPaths")
        environments.append(environment)
    if action == "prepare_stub_path":
        custom_path = ownership_record.get("custom_stub_path")
        custom_added = ownership_record.get("custom_stub_path_added")
        remove_managed = ownership_record.get("remove_managed_paths")
        legacy_top_level = ownership_record.get("legacy_top_level")
        legacy_roots = ownership_record.get("legacy_execution_environment_roots")
        if (
            not isinstance(custom_path, str)
            or not isinstance(custom_added, bool)
            or not isinstance(remove_managed, bool)
            or not isinstance(legacy_top_level, bool)
            or not isinstance(legacy_roots, list)
            or any(
                not isinstance(item, str) or not item
                for item in cast(list[object], legacy_roots)
            )
        ):
            raise ValueError("Invalid managed Pyright stubPath ownership; preserved")
        if remove_managed:
            matches = [
                index for index, item in enumerate(extra_paths) if item == managed_path
            ]
            if len(matches) > 1:
                raise ValueError("Duplicate GraphQL Desk extraPath; preserved")
            if matches:
                del extra_paths[matches[0]]
                if extra_paths or not legacy_top_level:
                    pyright["extraPaths"] = extra_paths
                else:
                    pyright.pop("extraPaths", None)
            for environment in environments:
                paths = cast(MutableSequence[object], environment.get("extraPaths", []))
                env_matches = [
                    index for index, item in enumerate(paths) if item == managed_path
                ]
                if len(env_matches) > 1:
                    raise ValueError(
                        "Duplicate GraphQL Desk execution environment path; preserved"
                    )
                if env_matches:
                    del paths[env_matches[0]]
                    if paths or cast(str, environment["root"]) not in cast(
                        list[str], legacy_roots
                    ):
                        environment["extraPaths"] = paths
                    else:
                        environment.pop("extraPaths", None)
        if custom_added:
            if custom_path in extra_paths:
                raise ValueError(
                    "GraphQL Desk custom stub root appeared during setup; preserved"
                )
            extra_paths.append(custom_path)
            pyright["extraPaths"] = extra_paths
        for root in owned_environment_roots:
            custom_environment: MutableMapping[str, object] | None = next(
                (item for item in environments if item["root"] == root), None
            )
            if custom_environment is None or "extraPaths" not in custom_environment:
                raise ValueError("Custom stub execution environment ownership changed")
            paths = cast(MutableSequence[object], custom_environment["extraPaths"])
            if custom_path in paths:
                raise ValueError("Custom stub execution environment ownership changed")
            paths.append(custom_path)
        stub_path = managed_path
        has_stub_path = True
        pyright["stubPath"] = managed_path
    elif action == "release_stub_path":
        custom_path = ownership_record.get("custom_stub_path")
        custom_added = ownership_record.get("custom_stub_path_added")
        remove_extra_key = ownership_record.get("remove_extra_paths_key")
        original_present = ownership_record.get("original_stub_path_present")
        original_path = ownership_record.get("original_stub_path")
        if (
            not isinstance(custom_path, str)
            or not isinstance(custom_added, bool)
            or not isinstance(remove_extra_key, bool)
            or not isinstance(original_present, bool)
            or original_present
            and not isinstance(original_path, str)
        ):
            raise ValueError("Invalid managed Pyright stubPath release; preserved")
        if custom_added:
            matches = [
                index for index, item in enumerate(extra_paths) if item == custom_path
            ]
            if len(matches) != 1:
                raise ValueError(
                    "GraphQL Desk custom stub root ownership changed; preserved"
                )
            del extra_paths[matches[0]]
            if remove_extra_key and not extra_paths:
                pyright.pop("extraPaths", None)
            else:
                pyright["extraPaths"] = extra_paths
        for root in owned_environment_roots:
            custom_environment = next(
                (item for item in environments if item["root"] == root), None
            )
            if custom_environment is None or "extraPaths" not in custom_environment:
                raise ValueError("Custom stub execution environment ownership changed")
            paths = cast(MutableSequence[object], custom_environment["extraPaths"])
            matches = [index for index, item in enumerate(paths) if item == custom_path]
            if len(matches) != 1:
                raise ValueError("Custom stub execution environment ownership changed")
            del paths[matches[0]]
        if original_present:
            pyright["stubPath"] = original_path
            stub_path = original_path
        else:
            pyright.pop("stubPath", None)
            stub_path = None
        has_stub_path = original_present
    matches = [index for index, item in enumerate(extra_paths) if item == managed_path]
    had_top_level_extra_paths = "extraPaths" in pyright
    if len(matches) > 1:
        raise ValueError("Duplicate GraphQL Desk extraPath")
    if action == "add" and not matches:
        extra_paths.append(managed_path)
        pyright["extraPaths"] = extra_paths
    elif action == "remove" and matches:
        del extra_paths[matches[0]]
        if extra_paths or not ownership["top_level"]:
            pyright["extraPaths"] = extra_paths
        else:
            pyright.pop("extraPaths", None)
    created_environment_roots: list[str] = []
    for environment in environments:
        paths = cast(MutableSequence[object], environment.get("extraPaths", []))
        environment_matches = [
            index for index, item in enumerate(paths) if item == managed_path
        ]
        if len(environment_matches) > 1:
            raise ValueError("Duplicate GraphQL Desk execution environment path")
        if action == "add" and not environment_matches:
            if "extraPaths" not in environment:
                created_environment_roots.append(cast(str, environment["root"]))
            paths.append(managed_path)
            environment["extraPaths"] = paths
        elif action == "remove" and environment_matches:
            del paths[environment_matches[0]]
            if paths:
                environment["extraPaths"] = paths
            elif cast(str, environment["root"]) in owned_environment_roots:
                environment.pop("extraPaths", None)
            else:
                environment["extraPaths"] = paths
    return {
        "ok": True,
        "found": True,
        "text": text if action == "inspect" else tomlkit.dumps(document),
        "stub_path": stub_path,
        "has_stub_path": has_stub_path,
        "extra_paths": list(
            cast(MutableSequence[object], pyright.get("extraPaths", []))
        ),
        "has_execution_environments": bool(execution_environments),
        "execution_environments": [
            {
                "root": cast(str, item["root"]),
                "extra_paths": list(
                    cast(MutableSequence[object], item.get("extraPaths", []))
                ),
                "has_extra_paths": "extraPaths" in item,
            }
            for item in environments
        ],
        "ownership": {
            "top_level": not had_top_level_extra_paths,
            "execution_environment_roots": created_environment_roots,
        },
    }


def _path(value: object) -> Path:
    path = Path(_string(value))
    if not path.is_absolute():
        raise ValueError("Absolute local path required")
    return path


def _canonical(path: Path) -> Path:
    return Path(os.path.normcase(str(path.resolve())))


def _analysis_input_fingerprint(snapshot: EditorSnapshot) -> str:
    """Hash the exact semantic inputs without serializing schema credentials."""
    request = snapshot.request
    modules = cast(list[dict[str, object]], request["modules"])
    bindings = cast(list[dict[str, object]], request["bindings"])
    schemas = cast(list[dict[str, object]], request["schemas"])
    projects: list[dict[str, object]] = []
    for config in snapshot.project_configs:
        project = config.path.as_uri()
        projects.append(
            {
                "project": project,
                "config": {
                    "root": str(config.root),
                    "generated_package": config.generated_package,
                    "schemas": sorted(
                        [
                            {
                                "name": item.name,
                                "origin": (
                                    "local"
                                    if item.origin.local_path is not None
                                    else "remote"
                                ),
                                "local_path": str(item.origin.local_path)
                                if item.origin.local_path is not None
                                else None,
                                "schema_output": str(item.schema_output),
                                "persisted_output": str(item.persisted_output)
                                if item.persisted_output is not None
                                else None,
                                "cache_output": str(item.cache_output)
                                if item.cache_output is not None
                                else None,
                                "scalars": sorted(
                                    (scalar.name, scalar.input, scalar.output)
                                    for scalar in item.scalars
                                ),
                                "enums": sorted(item.enums),
                                "disable_masking": item.disable_masking,
                            }
                            for item in config.schemas
                        ],
                        key=lambda item: str(item["name"]),
                    ),
                    "template": config.template,
                    "track_field_usage": config.track_field_usage,
                    "reserved_keys": config.reserved_keys,
                    "check_colocated_fragments": config.check_colocated_fragments,
                    "client_directives": config.client_directives,
                    "schema_refresh_interval": config.schema_refresh_interval,
                    "disable_preprocessing": config.disable_preprocessing,
                    "cache_enabled": config.cache_enabled,
                    "source_roots": [str(item) for item in config.source_roots],
                    "include": config.include,
                    "exclude": config.exclude,
                    "entrypoints": config.entrypoints,
                    "authoring_roots": [str(item) for item in config.authoring_roots],
                },
                "modules": sorted(
                    (item["module"], item["uri"], item["is_package"], item["source"])
                    for item in modules
                    if item["project"] == project
                ),
                "bindings": sorted(
                    json.dumps(
                        item, sort_keys=True, ensure_ascii=True, separators=(",", ":")
                    )
                    for item in bindings
                    if item["project"] == project
                ),
                "schemas": sorted(
                    (item["schema_name"], item["format"], item["text"])
                    for item in schemas
                    if item["project"] == project
                ),
                "external_mapping_modules": sorted(
                    (item.module, item.uri, item.source)
                    for item in snapshot.external_mapping_modules
                    if item.project == project + "#interpreter"
                ),
            }
        )
    payload = json.dumps(
        {"format": 1, "projects": projects},
        sort_keys=True,
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf8")).hexdigest()


def _saved_source_fingerprint(snapshot: EditorSnapshot) -> str:
    """Identify captured disk sources separately from live editor overlays."""
    sources = sorted(
        (item.project, item.module, item.uri, item.is_package, item.source)
        for item in snapshot.saved_modules
    )
    payload = json.dumps(
        {"format": 1, "sources": sources},
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf8")).hexdigest()


def _validated_provenance_identity(value: object) -> DocumentIdentity:
    if not isinstance(value, DocumentIdentity):
        raise ValueError("Invalid operation provenance identity")
    return value


def _provenance_identity(
    value: object, utf16_positions: dict[ModuleDependency, dict[int, int]]
) -> dict[str, object]:
    value = _validated_provenance_identity(value)
    positions = utf16_positions[value.module]
    return {
        "module": {
            "project": value.module.project,
            "module": value.module.module,
            "uri": value.module.uri,
            "version": value.module.version,
        },
        "codepoint": {"start": value.call_range.start, "end": value.call_range.end},
        "utf16": {
            "start": positions[value.call_range.start],
            "end": positions[value.call_range.end],
        },
    }


def _operation_provenance(
    records: tuple[object, ...], snapshots: dict[ModuleDependency, str]
) -> list[dict[str, object]]:
    from dataclasses import asdict

    from .factory_generation import OperationProvenance

    points: dict[ModuleDependency, set[int]] = {}
    for record in records:
        if not isinstance(record, OperationProvenance):
            raise ValueError("Invalid operation provenance")
        identities = (
            record.owner,
            *(record.direct_fragments or ()),
            *(record.definition_owners or ()),
            record.canonical_owner,
        )
        for identity in identities:
            if identity is not None:
                identity = _validated_provenance_identity(identity)
                points.setdefault(identity.module, set()).update(
                    (identity.call_range.start, identity.call_range.end)
                )
    utf16_positions = {
        module: utf16_points(snapshots[module], offsets)
        for module, offsets in points.items()
    }
    result: list[dict[str, object]] = []
    for record in records:
        if not isinstance(record, OperationProvenance):
            raise ValueError("Invalid operation provenance")
        serialized = asdict(record)
        serialized["owner"] = _provenance_identity(record.owner, utf16_positions)
        for field_name in ("direct_fragments", "definition_owners"):
            values = getattr(record, field_name)
            serialized[field_name] = (
                None
                if values is None
                else [_provenance_identity(value, utf16_positions) for value in values]
            )
        serialized["canonical_owner"] = (
            None
            if record.canonical_owner is None
            else _provenance_identity(record.canonical_owner, utf16_positions)
        )
        serialized["dependencies"] = [
            {
                "project": item.project,
                "module": item.module,
                "uri": item.uri,
                "version": item.version,
            }
            for item in record.dependencies
        ]
        serialized["source_exports"] = [asdict(item) for item in record.source_exports]
        result.append(serialized)
    return result


@dataclass(frozen=True)
class _Document:
    identity: str
    path: Path
    canonical: Path
    version: int
    is_dirty: bool


@dataclass(frozen=True)
class _Capture:
    workspace: Path
    candidates: tuple[Path, ...]
    buffers: tuple[OpenBuffer, ...]
    documents: tuple[_Document, ...]
    fallback_json: str = field(repr=False)
    managed_pyright_config_path: str | None = None
    managed_pyright_stub_path: str | None = None
    managed_pyright_previous_stub_path: str | None = None
    managed_pyright_previous_stub_path_present: bool = False
    managed_pyright_previous_stub_path_known: bool = False

    @property
    def fallback(self) -> dict[str, object] | None:
        return cast(dict[str, object] | None, json.loads(self.fallback_json))


def _buffer(value: object) -> OpenBuffer:
    item = _object(value, {"path", "version", "text", "is_dirty"})
    text = item["text"]
    if not isinstance(text, str):
        raise ValueError("Invalid buffer text")
    return OpenBuffer(
        _path(item["path"]),
        _version(item["version"]),
        text,
        _boolean(item["is_dirty"]),
    )


def _capture(item: dict[str, object]) -> _Capture:
    workspace = _path(item["workspace"])
    candidates = tuple(_path(value) for value in _array(item["candidates"]))
    if any(path.suffix != ".py" for path in candidates):
        raise ValueError("Python source candidates required")
    documents: list[_Document] = []
    for value in _array(item["documents"]):
        doc = _object(value, {"id", "path", "version", "is_dirty"})
        path = _path(doc["path"])
        documents.append(
            _Document(
                _string(doc["id"]),
                path,
                _canonical(path),
                _version(doc["version"]),
                _boolean(doc["is_dirty"]),
            )
        )
    if len({doc.identity for doc in documents}) != len(documents) or len(
        {doc.canonical for doc in documents}
    ) != len(documents):
        raise ValueError("Conflicting document identities")
    buffers = tuple(_buffer(value) for value in _array(item["buffers"]))
    canonical_candidates = tuple(_canonical(path) for path in candidates)
    allowed = set(canonical_candidates)
    base = _canonical(workspace)
    if any(not path.parent.is_relative_to(base) for path in canonical_candidates):
        raise ValueError("Candidate escapes workspace")
    # Config ancestors can only authorize text for a non-source open document.
    if any(doc.canonical not in allowed for doc in documents):
        for path, canonical in zip(candidates, canonical_candidates, strict=True):
            if _canonical(path) != canonical:
                raise ValueError("Candidate changed while checking open buffers")
            parent = canonical.parent
            while True:
                allowed.add(_canonical(parent / "pyproject.toml"))
                if parent == base:
                    break
                parent = parent.parent
    _check_buffers(buffers, documents)
    if any(_canonical(buffer.path) not in allowed for buffer in buffers):
        raise ValueError("Unrequested buffer text")
    # Every open source/config capability must have its current text captured.
    if {doc.canonical for doc in documents if doc.canonical in allowed} != {
        _canonical(buffer.path) for buffer in buffers
    }:
        raise ValueError("Missing source/config buffer")
    fallback = item["editor_fallback"]
    if fallback is not None and not isinstance(fallback, dict):
        raise ValueError("Invalid fallback")
    managed_stub_path = item.get("managed_pyright_stub_path")
    managed_config_path = item.get("managed_pyright_config_path")
    previous_stub_path = item.get("managed_pyright_previous_stub_path")
    previous_stub_path_known = "managed_pyright_previous_stub_path_present" in item
    previous_stub_path_present = item.get(
        "managed_pyright_previous_stub_path_present", False
    )
    if managed_config_path is not None and (
        not isinstance(managed_config_path, str)
        or managed_config_path != "pyproject.toml"
    ):
        raise ValueError("Invalid GraphQL Desk-owned Pyright config path")
    if (managed_config_path is None) != (managed_stub_path is None):
        raise ValueError("Incomplete GraphQL Desk-owned Pyright config identity")
    if managed_stub_path is not None and (
        not isinstance(managed_stub_path, str)
        or not _MANAGED_PYRIGHT_STUB_PATH.fullmatch(managed_stub_path)
    ):
        raise ValueError("Invalid GraphQL Desk-owned Pyright stubPath")
    if not isinstance(previous_stub_path_present, bool):
        raise ValueError("Invalid GraphQL Desk previous Pyright stubPath ownership")
    if previous_stub_path_present and (
        not isinstance(previous_stub_path, str)
        or not previous_stub_path
        or len(previous_stub_path) > 4096
        or "\0" in previous_stub_path
    ):
        raise ValueError("Invalid GraphQL Desk previous Pyright stubPath")
    if not previous_stub_path_present and previous_stub_path is not None:
        raise ValueError("Unexpected GraphQL Desk previous Pyright stubPath")
    return _Capture(
        workspace,
        tuple(sorted(candidates, key=str)),
        tuple(sorted(buffers, key=lambda buffer: str(buffer.path))),
        tuple(sorted(documents, key=lambda doc: doc.identity)),
        json.dumps(fallback, sort_keys=True, allow_nan=False),
        managed_config_path,
        managed_stub_path,
        cast(str | None, previous_stub_path),
        previous_stub_path_present,
        previous_stub_path_known,
    )


def _check_buffers(
    buffers: tuple[OpenBuffer, ...], documents: list[_Document] | tuple[_Document, ...]
) -> None:
    seen: set[Path] = set()
    for buffer in buffers:
        key = _canonical(buffer.path)
        matches = [doc for doc in documents if doc.canonical == key]
        if (
            key in seen
            or len(matches) != 1
            or matches[0].version != buffer.version
            or matches[0].is_dirty != buffer.is_dirty
        ):
            raise ValueError("Buffer metadata mismatch")
        seen.add(key)


def _quarantine_request(raw: dict[str, object]) -> dict[str, object]:
    item = _object(raw, {"op", "id", "workspace", "projects", "invalidated_owners"})
    identifier = _string(item["id"])
    _path(item["workspace"])
    groups = _array(item["projects"])
    invalidated = _array(item["invalidated_owners"])
    if len(groups) > 4096 or len(invalidated) > 8192:
        raise ValueError("Invalid quarantine request")
    owner_keys = {_json_identity(owner) for owner in invalidated}
    projects: list[dict[str, object]] = []
    seen_projects: set[str] = set()
    matched = False
    total = 0
    for raw_group in groups:
        group = _object(
            raw_group, {"project", "generated_package", "files", "provenance"}
        )
        project = _string(group["project"])
        package = _string(group["generated_package"])
        if project in seen_projects or not re.fullmatch(
            r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*", package
        ):
            raise ValueError("Invalid quarantine project")
        seen_projects.add(project)
        files = _array(group["files"])
        provenance = _array(group["provenance"])
        if len(files) > 4096 or len(provenance) > 8192:
            raise ValueError("Invalid quarantine project")
        for record in provenance:
            if not isinstance(record, dict):
                raise ValueError("Invalid quarantine provenance")
            record = cast(dict[str, object], record)
            if not isinstance(record.get("owner"), dict):
                raise ValueError("Invalid quarantine provenance")
        matched = matched or any(
            _json_identity(cast(dict[str, object], record)["owner"]) in owner_keys
            for record in provenance
        )
        parsed_files: list[GeneratedFile] = []
        prefix = package.replace(".", "/") + "/"
        for raw_file in files:
            file = _object(raw_file, {"relative_path", "text"})
            relative = _string(file["relative_path"])
            text = file["text"]
            if (
                not relative.startswith(prefix)
                or "\\" in relative
                or "\x00" in relative
                or relative.startswith("/")
                or any(part in {"", ".", ".."} for part in relative.split("/"))
                or not relative.endswith(".pyi")
                or not isinstance(text, str)
            ):
                raise ValueError("Invalid quarantine artifact")
            total += len(text.encode("utf-8"))
            if total > 4 * 1024 * 1024:
                raise ValueError("Quarantine artifacts exceed limit")
            parsed_files.append(GeneratedFile(relative, text))
        quarantined = (
            quarantine_generated_stubs(
                tuple(parsed_files),
                tuple(cast(dict[str, object], record) for record in provenance),
                tuple(invalidated),
            )
            if any(
                _json_identity(cast(dict[str, object], record)["owner"]) in owner_keys
                for record in provenance
            )
            else tuple(parsed_files)
        )
        projects.append(
            {
                "project": project,
                "generated_package": package,
                "files": [
                    {"relative_path": file.relative_path, "text": file.text}
                    for file in quarantined
                ],
            }
        )
    if invalidated and not matched:
        raise ValueError("No operation provenance matches invalidated owners")
    return {"ok": True, "id": identifier, "projects": projects}


def _json_identity(value: object) -> str:
    if not isinstance(value, dict):
        raise ValueError("Invalid operation identity")
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


class EditorWorker:
    """One workspace/job; callers own trust, cancellation and credential revisions.

    Only explicit refresh fetches a prepared target. Analyze/current must repeat
    every active remote credential revision; changes require refresh/invalidation.
    Failed refresh retains matching last-good only as visible, unavailable state.
    """

    def __init__(self, *, config_workspace: Path | None = None) -> None:
        self._workspace: Path | None = None
        self._generation = 0
        self._identity: str | None = None
        self._capture: _Capture | None = None
        self._preparation: EditorPreparation | None = None
        self._snapshot: EditorSnapshot | None = None
        self._analysis: EditorAnalysis | None = None
        self._previous: EditorSnapshot | None = None
        self._previous_fragments: FragmentReuse | None = None
        self._schema_buffers: tuple[OpenBuffer, ...] = ()
        self._resolver = IncrementalResolver()
        self._remote: dict[tuple[str, str], RemoteSchemaSnapshot] = {}
        self._last_good: dict[tuple[str, str], RemoteSchemaSnapshot] = {}
        self._remote_revisions: dict[tuple[str, str], str] = {}
        self._config_directories = (
            capture_config_directories(config_workspace)
            if config_workspace is not None
            else None
        )
        if config_workspace is not None:
            self._workspace = _canonical(config_workspace)
        self._config_stage: ConfigFileStage | None = None

    def _clear_job(self) -> None:
        self._identity = None
        self._capture = None
        self._preparation = None
        self._snapshot = None
        self._analysis = None
        self._previous = None
        self._previous_fragments = None
        self._schema_buffers = ()
        self._resolver.clear()

    def invalidate(self) -> None:
        if self._config_stage is not None:
            # Revocation retains the single slot until explicit verified recovery.
            self._config_stage.revoked = True
        self._clear_job()
        self._remote.clear()
        self._last_good.clear()
        self._remote_revisions.clear()

    def handle(self, value: object) -> dict[str, object]:
        """Malformed/failed messages invalidate all previously issued job IDs."""
        previous_stage = self._config_stage
        try:
            return self._handle(value)
        except Exception as error:
            stage = self._config_stage
            self.invalidate()
            if stage is not None and stage is not previous_stage and stage.attempted:
                native_code = getattr(error, "winerror", None) or getattr(
                    error, "errno", 0
                )
                if type(native_code) is not int or not 0 <= native_code <= 65535:
                    native_code = 0
                return {
                    "ok": False,
                    "error": "editor-request-failed",
                    "config_phase": stage.failure_phase,
                    "native_code": native_code,
                }
            return {"ok": False, "error": "editor-request-failed"}

    def _config_file(self, raw: dict[str, object]) -> dict[str, object]:
        directories = self._config_directories
        if directories is None:
            raise ValueError("Configuration staging is not enabled for this worker")
        workspace = _path(raw.get("workspace"))
        if workspace != directories[-1][0] or (
            self._workspace is not None and _canonical(workspace) != self._workspace
        ):
            raise ValueError("Configuration workspace changed")
        action = raw.get("action")
        keys = {"op", "workspace", "action"}
        if action == "stage":
            item = _object(raw, keys | {"file", "temporary", "expected_hash", "text"})
            if self._config_stage is not None:
                raise ValueError("A configuration stage already needs recovery")
            text = item["text"]
            if not isinstance(text, str):
                raise ValueError("Invalid configuration text")
            stage = ConfigFileStage.capture(
                directories, _string(item["file"]), _path(item["temporary"])
            )
            self._config_stage = stage
            try:
                stage.write(text, _string(item["expected_hash"]))
            except Exception:
                if not stage.attempted:
                    self._config_stage = None
                raise
            assert stage.original is not None and stage.staged is not None
            return {
                "ok": True,
                "token": stage.token,
                "receipt": stage.receipt(),
                "original_hash": stage.original[-2],
                "staged_hash": stage.staged[-2],
            }
        if action == "resume":
            item = _object(raw, keys | {"receipt"})
            if self._config_stage is not None:
                raise ValueError("A configuration stage already needs recovery")
            stage = ConfigFileStage.resume(directories, _string(item["receipt"]))
            self._config_stage = stage
            return {"ok": True, "token": stage.token}
        item = _object(raw, keys | {"token"})
        token = _string(item["token"])
        pending = self._config_stage
        if pending is None or token != pending.token:
            raise ValueError("Invalid configuration staging capability")
        if action == "verify":
            pending.verify()
        elif action == "verify_discard":
            pending.verify_discard()
        elif action == "discard":
            pending.discard()
            self._config_stage = None
        elif action == "finish":
            pending.finish()
            self._config_stage = None
        else:
            raise ValueError("Invalid configuration staging action")
        return {"ok": True, "token": token}

    def _handle(self, value: object) -> dict[str, object]:
        if not isinstance(value, dict):
            raise ValueError("Invalid request")
        raw = cast(dict[str, object], value)
        operation = raw.get("op")
        if operation == "config_file":
            return self._config_file(raw)
        if operation == "quarantine":
            return _quarantine_request(raw)
        if operation == "pyright_config":
            return _pyright_config_request(raw)
        if not isinstance(operation, str) or operation not in {
            "prepare",
            "analyze",
            "current",
            "refresh",
            "artifacts",
            "doctor",
        }:
            raise ValueError("Invalid operation")
        keys = {"op"} | _CAPTURE_KEYS
        if operation != "prepare":
            keys |= {"id", "schema_buffers"}
        if operation == "refresh":
            keys |= {"project", "schema_name", "credential_revision", "environment"}
        elif (
            operation in {"prepare", "analyze", "current", "artifacts", "doctor"}
            and "remote_revisions" in raw
        ):
            keys.add("remote_revisions")
        allowed_keys = keys | {
            "managed_pyright_config_path",
            "managed_pyright_stub_path",
            "managed_pyright_previous_stub_path",
            "managed_pyright_previous_stub_path_present",
        }
        if not keys <= set(raw) or not set(raw) <= allowed_keys:
            raise ValueError("Invalid object")
        item = raw
        capture = _capture(item)
        workspace = _canonical(capture.workspace)
        if self._workspace is not None and workspace != self._workspace:
            raise ValueError("Worker workspace changed")
        self._workspace = workspace
        if operation == "prepare":
            self._identity = None
            self._snapshot = None
            self._analysis = None
            self._schema_buffers = ()
            revisions = self._revisions(item)
            preparation = prepare_editor_snapshot(
                workspace=capture.workspace,
                candidates=capture.candidates,
                buffers=capture.buffers,
                editor_fallback=capture.fallback,
                managed_pyright_config_path=capture.managed_pyright_config_path,
                managed_pyright_stub_path=capture.managed_pyright_stub_path,
            )
            self._capture = capture
            self._preparation = preparation
            self._identity = uuid4().hex
            origins = {
                (entry.project, entry.schema_name): entry
                for entry in preparation.remote_origins
            }
            self._remote = {
                key: schema
                for key, schema in self._remote.items()
                if key in origins
                and schema.config_token == origins[key].config_token
                and revisions.get(key) == self._remote_revisions.get(key)
                and key in revisions
            }
            self._remote_revisions = {key: revisions[key] for key in self._remote}
            self._last_good = {
                key: schema
                for key, schema in self._last_good.items()
                if key in origins and schema.config_token == origins[key].config_token
            }
            required = {path.canonical_path for path in preparation.local_schemas}
            reply: dict[str, object] = {
                "ok": True,
                "id": self._identity,
                "configuration_diagnostics": [
                    diagnostic.as_json()
                    for diagnostic in preparation.configuration_diagnostics
                ],
                "local_schemas": [
                    {
                        "project": path.project,
                        "schema_name": path.schema_name,
                        "path": str(path.path),
                        "canonical_path": str(path.canonical_path),
                    }
                    for path in preparation.local_schemas
                ],
                "remote_schemas": [
                    {"project": entry.project, "schema_name": entry.schema_name}
                    for entry in preparation.remote_origins
                ],
                "required_buffers": [
                    doc.identity
                    for doc in capture.documents
                    if doc.canonical in required
                ],
            }
            if "remote_revisions" in item:
                reply["project_preferences"] = [
                    {"project": project, "client_directives": list(directives)}
                    for project, directives in preparation.project_preferences
                ]
                reply["remote_revisions"] = [
                    {"project": key[0], "schema_name": key[1], "revision": revision}
                    for key, revision in sorted(self._remote_revisions.items())
                ]
                reply["remote_schemas"] = [
                    {
                        "project": entry.project,
                        "schema_name": entry.schema_name,
                        "environment_names": sorted(
                            set(
                                name
                                for _, value in entry.origin.headers
                                for name in re.findall(
                                    r"\$\{env:([A-Za-z_][A-Za-z_0-9]*)\}", value
                                )
                            )
                        ),
                        "refresh_interval": entry.refresh_interval,
                    }
                    for entry in preparation.remote_origins
                ]
            return reply
        if (
            _string(item["id"]) != self._identity
            or self._preparation is None
            or self._capture is None
            or replace(
                capture,
                managed_pyright_config_path=self._capture.managed_pyright_config_path,
                managed_pyright_stub_path=self._capture.managed_pyright_stub_path,
                managed_pyright_previous_stub_path=self._capture.managed_pyright_previous_stub_path,
                managed_pyright_previous_stub_path_present=self._capture.managed_pyright_previous_stub_path_present,
                managed_pyright_previous_stub_path_known=self._capture.managed_pyright_previous_stub_path_known,
            )
            != self._capture
        ):
            raise ValueError("Stale job")
        schema_buffers = tuple(
            _buffer(buffer) for buffer in _array(item["schema_buffers"])
        )
        _check_buffers(schema_buffers, capture.documents)
        required = {path.canonical_path for path in self._preparation.local_schemas}
        expected = {
            doc.canonical for doc in capture.documents if doc.canonical in required
        }
        if {_canonical(buffer.path) for buffer in schema_buffers} != expected:
            raise ValueError("Schema buffer set mismatch")
        buffers = capture.buffers + schema_buffers
        if operation == "refresh":
            return self._refresh(item, capture, buffers)
        revisions = self._revisions(item)
        if revisions != self._remote_revisions:
            raise ValueError("Remote credential revision changed")
        remote_schemas = tuple(self._remote[key] for key in sorted(self._remote))
        if operation in {"current", "artifacts", "doctor"}:
            has_analyzed_job = self._snapshot is not None and self._analysis is not None

            def current_job() -> bool:
                return (
                    self._snapshot is not None
                    and schema_buffers == self._schema_buffers
                    and is_editor_snapshot_current(
                        self._snapshot,
                        candidates=capture.candidates,
                        buffers=buffers,
                        editor_fallback=capture.fallback,
                        remote_schemas=remote_schemas,
                        managed_pyright_config_path=capture.managed_pyright_config_path,
                        managed_pyright_stub_path=capture.managed_pyright_stub_path,
                        managed_pyright_previous_stub_path=capture.managed_pyright_previous_stub_path,
                        managed_pyright_previous_stub_path_present=capture.managed_pyright_previous_stub_path_present,
                        managed_pyright_previous_stub_path_known=capture.managed_pyright_previous_stub_path_known,
                    )
                )

            current = current_job()
            if not current:
                self._clear_job()
            if operation == "current":
                return {"ok": True, "current": current}
            if not current:
                if not has_analyzed_job:
                    raise ValueError("Artifacts require an analyzed job")
                return {"ok": True, "id": item["id"], "current": False}
            if self._snapshot is None or self._analysis is None:
                raise ValueError("Artifacts require a current analyzed job")
            if operation == "doctor":
                report = collect_snapshot_health(
                    self._snapshot,
                    self._analysis,
                    configuration_diagnostics=self._preparation.configuration_diagnostics,
                )
                if not current_job():
                    self._clear_job()
                    return {"ok": True, "id": item["id"], "current": False}
                return {
                    "ok": True,
                    "id": self._identity,
                    "current": True,
                    "report": report,
                }
            projects: list[dict[str, object]] = []
            testing_result_overloads: list[str] = []
            masking_overloads: list[str] = []
            facts = self._analysis
            snapshots = {
                ModuleDependency(
                    module.project, module.module, module.uri, module.version
                ): module.source
                for module in facts.modules
            }
            for config in self._snapshot.project_configs:
                project = config.path.as_uri()
                project_modules = tuple(
                    module for module in facts.modules if module.project == project
                )
                project_module_names = {module.module for module in project_modules}
                mapping_modules = tuple(
                    ModuleSnapshot(
                        project,
                        module.module,
                        module.uri,
                        module.version,
                        module.source,
                        module.is_package,
                    )
                    for module in self._snapshot.external_mapping_modules
                    if module.project == project + "#interpreter"
                    and module.module not in project_module_names
                )
                index = FragmentIndex(
                    tuple(
                        document
                        for document in facts.index.documents
                        if document.call.module.project == project
                    ),
                    tuple(
                        diagnostic
                        for diagnostic in facts.index.diagnostics
                        if diagnostic.module.project == project
                    ),
                    tuple(
                        document
                        for document in facts.index.unavailable
                        if document.call.module.project == project
                    ),
                    tuple(
                        call
                        for call in facts.index.rejected_calls
                        if call.module.project == project
                    ),
                )
                artifacts = (
                    FactoryArtifacts((), index.diagnostics)
                    if any(
                        unavailable_project == project
                        for unavailable_project, _ in self._snapshot.unavailable_schemas
                    )
                    else generate_project_factory_artifacts(
                        config,
                        tuple(
                            facts.schemas[(project, schema.name)]
                            for schema in config.schemas
                        ),
                        index,
                        modules=(*project_modules, *mapping_modules),
                        project_id=project,
                    )
                )
                generated_files = {
                    file.relative_path: file.text for file in artifacts.files
                }
                testing_artifacts = render_testing_helper_artifacts(
                    config,
                    tuple(
                        (schema, facts.schemas[(project, schema.name)])
                        for schema in config.schemas
                        if (project, schema.name) in facts.schemas
                    ),
                    index,
                    project_modules,
                    artifacts.provenance,
                )
                for filename, text in testing_artifacts.files:
                    generated_files[filename] = text
                    parts = filename.split("/")[:-1]
                    for length in range(1, len(parts) + 1):
                        package = "/".join(parts[:length])
                        generated_files.setdefault(
                            package + "/__init__.py",
                            '"""Generated GraphQL Desk package."""\n',
                        )
                        generated_files.setdefault(package + "/__init__.pyi", "")
                testing_result_overloads.append(testing_artifacts.read_result_stub)
                masking_overloads.append(testing_artifacts.mask_fragments_stub)
                parser_diagnostics: list[dict[str, object]] = []
                parser_diagnostics.extend(
                    editor_diagnostic(item, snapshots)
                    for item in testing_artifacts.diagnostics
                )
                for companion in render_captured_parse_companions(
                    project_modules,
                    project_id=project,
                    generated_package=config.generated_package,
                ):
                    module = next(
                        item
                        for item in project_modules
                        if item.module == companion.module.module
                        and item.uri == companion.module.uri
                    )
                    base = (
                        f"{config.generated_package.replace('.', '/')}/parsers/"
                        f"{module.module.replace('.', '/')}"
                    )
                    if module.is_package:
                        base += "/__init__"
                    generated_files[base + ".py"] = companion.companion.runtime
                    generated_files[base + ".pyi"] = companion.companion.stub
                    parts = (base + ".py").split("/")[:-1]
                    for length in range(1, len(parts) + 1):
                        package = "/".join(parts[:length])
                        generated_files.setdefault(
                            package + "/__init__.py",
                            '"""Generated GraphQL Desk package."""\n',
                        )
                        generated_files.setdefault(package + "/__init__.pyi", "")
                    parser_diagnostics.extend(
                        editor_diagnostic(item, snapshots)
                        for item in companion.diagnostics
                    )
                projects.append(
                    {
                        "project": project,
                        "generated_package": config.generated_package,
                        "files": [
                            {"relative_path": filename, "text": text}
                            for filename, text in sorted(generated_files.items())
                        ],
                        "diagnostics": [
                            editor_diagnostic(diagnostic, snapshots)
                            for diagnostic in artifacts.diagnostics
                        ]
                        + parser_diagnostics,
                        "provenance": _operation_provenance(
                            artifacts.provenance, snapshots
                        ),
                    }
                )
            overlay = render_captured_parse_overlay(facts.modules)
            overload_imports = (
                "from typing import overload as _graphql_desk_overload\n"
                "from collections.abc import Sequence as _graphql_desk_Sequence\n"
            )
            testing_stub = (
                overload_imports
                + "from typing import TypeVar as _graphql_desk_TypeVar\n"
                "from collections.abc import Mapping as _graphql_desk_Mapping\n"
                "from graphql_desk.fragment_refs import FixtureDocument as "
                "_GraphqlDeskFixtureDocument\n"
                "_GraphqlDeskResult = _graphql_desk_TypeVar('_GraphqlDeskResult')\n"
                "_GraphqlDeskVariables = _graphql_desk_TypeVar("
                "'_GraphqlDeskVariables')\n"
                "_GraphqlDeskInput = _graphql_desk_TypeVar('_GraphqlDeskInput', "
                "bound=_graphql_desk_Mapping[str, object])\n"
                "def unsafe_read_result(document: "
                "_GraphqlDeskFixtureDocument[_GraphqlDeskResult, "
                "_GraphqlDeskVariables, _GraphqlDeskInput], "
                "data: _GraphqlDeskInput) -> _GraphqlDeskResult: ...\n"
                + "".join(testing_result_overloads)
                + "@_graphql_desk_overload\n"
                + "def read_result(document: object, data: object, "
                "fragments: _graphql_desk_Sequence[object]) -> object: ...\n"
            )
            fragment_refs_stub = (
                "from collections.abc import Callable as _Callable, "
                "Mapping as _Mapping\n"
                "from typing import Generic as _Generic, TypeVar as _TypeVar\n"
                "from typing_extensions import ReadOnly as _ReadOnly, "
                "TypedDict as _TypedDict\n"
                "from .typed_document import TypedDocument\n"
                "from graphql_desk.fragments import DefinitionFact, IndexedDocument\n"
                "_Key = _TypeVar('_Key')\n"
                "_Identity = _TypeVar('_Identity')\n"
                "_Identity_contra = _TypeVar('_Identity_contra', "
                "contravariant=True)\n"
                "_Result = _TypeVar('_Result')\n"
                "_Variables = _TypeVar('_Variables')\n"
                "_ReadInput = _TypeVar('_ReadInput', "
                "bound=_Mapping[str, object])\n"
                "class FragmentIdentity(_Generic[_Key]): ...\n"
                "class FragmentRef(_TypedDict, _Generic[_Identity_contra]):\n"
                "    _graphql_desk_φ: _ReadOnly[_Callable[["
                "_Identity_contra], None]]\n"
                "class FragmentDocument(TypedDocument[_Result, _Variables], "
                "_Generic[_Identity, _Result, _Variables]): ...\n"
                "class ReadableFragmentDocument(FragmentDocument["
                "_Identity, _Result, _Variables], _Generic[_Identity, "
                "_Result, _Variables, _ReadInput]): ...\n"
                "class FixtureDocument(TypedDocument[_Result, _Variables], "
                "_Generic[_Result, _Variables, _ReadInput]): ...\n"
                "def fragment_identity_key(owner: IndexedDocument, "
                "definition: DefinitionFact) -> str: ...\n"
            )
            mask_stub = (
                overload_imports
                + "".join(masking_overloads)
                + "@_graphql_desk_overload\n"
                + "def mask_fragments(fragments: _graphql_desk_Sequence[object], "
                "data: object) -> object: ...\n"
            )
            return {
                "ok": True,
                "id": self._identity,
                "input_fingerprint": _analysis_input_fingerprint(self._snapshot),
                "saved_source_fingerprint": _saved_source_fingerprint(self._snapshot),
                "projects": projects,
                "parser_overlay": [
                    {
                        "relative_path": "graphql_desk-stubs/typed_document.pyi",
                        "text": (
                            "from dataclasses import dataclass as _dataclass\n"
                            "from typing import Generic as _Generic, "
                            "TypeVar as _TypeVar\n"
                            "from graphql import DocumentNode as _DocumentNode\n"
                            "_Result = _TypeVar('_Result')\n"
                            "_Variables = _TypeVar('_Variables')\n"
                            "@_dataclass(frozen=True)\n"
                            "class TypedDocument(_Generic[_Result, _Variables]):\n"
                            "    def __init__(self, ast: _DocumentNode, "
                            "operation_name: str | None = None, *, "
                            "schema_name: str | None = None, "
                            "persisted_id: str | None = None) -> None: ...\n"
                            "    ast: _DocumentNode\n"
                            "    operation_name: str | None\n"
                            "    schema_name: str | None\n"
                            "    persisted_id: str | None\n"
                        ),
                    },
                    {
                        "relative_path": "graphql_desk-stubs/fragment_refs.pyi",
                        "text": fragment_refs_stub,
                    },
                    {
                        "relative_path": "graphql_desk-stubs/__init__.pyi",
                        "text": overlay.stub + "\n" + mask_stub,
                    },
                    {
                        "relative_path": "graphql_desk-stubs/testing.pyi",
                        "text": testing_stub,
                    },
                    {
                        "relative_path": "graphql_desk-stubs/py.typed",
                        "text": "partial\n",
                    },
                ],
                "unavailable_schemas": self._snapshot.unavailable_schemas,
            }
        if self._snapshot is not None:
            raise ValueError("Job already analyzed")
        self._generation += 1
        snapshot = acquire_editor_snapshot(
            workspace=capture.workspace,
            candidates=capture.candidates,
            buffers=buffers,
            generation=self._generation,
            editor_fallback=capture.fallback,
            preparation=self._preparation,
            previous=self._previous,
            remote_schemas=remote_schemas,
            managed_pyright_config_path=capture.managed_pyright_config_path,
            managed_pyright_stub_path=capture.managed_pyright_stub_path,
        )
        request = snapshot.request
        analysis = analyze_editor_snapshot_details(
            request,
            resolver=self._resolver,
            previous_fragments=self._previous_fragments,
            external_mapping_modules=snapshot.external_mapping_modules,
            colocated_fragment_checks={
                config.path.as_uri(): config.check_colocated_fragments
                for config in snapshot.project_configs
            },
            field_usage_options={
                config.path.as_uri(): FieldUsageOptions(
                    enabled=config.track_field_usage,
                    reserved_keys=config.reserved_keys,
                    unmasked_schemas=tuple(
                        schema.name
                        for schema in config.schemas
                        if schema.disable_masking
                    ),
                    client_directives=config.client_directives,
                )
                for config in snapshot.project_configs
            },
        )
        self._analysis = analysis
        self._snapshot = self._previous = snapshot
        self._previous_fragments = analysis.fragment_reuse
        self._schema_buffers = schema_buffers
        return {
            "ok": True,
            "id": self._identity,
            "request": request,
            "response": analysis.response,
            "unavailable_schemas": snapshot.unavailable_schemas,
            "remote_schemas": [
                {
                    "project": key[0],
                    "schema_name": key[1],
                    "status": (
                        "unavailable"
                        if key not in self._remote
                        else "stale"
                        if self._remote[key].stale
                        else "ready"
                    ),
                }
                for key in sorted(self._remote_revisions)
            ],
        }

    @staticmethod
    def _revisions(item: dict[str, object]) -> dict[tuple[str, str], str]:
        revisions: dict[tuple[str, str], str] = {}
        for value in _array(item.get("remote_revisions", [])):
            revision = _object(value, {"project", "schema_name", "revision"})
            key = (_string(revision["project"]), _string(revision["schema_name"]))
            if key in revisions:
                raise ValueError("Duplicate credential revision")
            revisions[key] = _string(revision["revision"])
        return revisions

    def _refresh(
        self,
        item: dict[str, object],
        capture: _Capture,
        buffers: tuple[OpenBuffer, ...],
    ) -> dict[str, object]:
        assert self._preparation is not None
        key = (_string(item["project"]), _string(item["schema_name"]))
        origins = {
            (entry.project, entry.schema_name): entry
            for entry in self._preparation.remote_origins
        }
        if key not in origins:
            raise ValueError("Unprepared remote schema")
        revision = _string(item["credential_revision"])
        raw_environment = item["environment"]
        if not isinstance(raw_environment, dict):
            raise ValueError("Invalid explicit environment")
        environment: dict[str, str] = {}
        for name, value in cast(dict[object, object], raw_environment).items():
            if not isinstance(name, str) or not isinstance(value, str):
                raise ValueError("Invalid explicit environment")
            environment[name] = value
        credential_token = hashlib.sha256(
            json.dumps(
                [revision, environment], sort_keys=True, ensure_ascii=True
            ).encode()
        ).hexdigest()

        def recheck() -> None:
            if (
                prepare_editor_snapshot(
                    workspace=capture.workspace,
                    candidates=capture.candidates,
                    buffers=capture.buffers,
                    editor_fallback=capture.fallback,
                    managed_pyright_config_path=capture.managed_pyright_config_path,
                    managed_pyright_stub_path=capture.managed_pyright_stub_path,
                )
                != self._preparation
            ):
                raise ValueError("Configuration changed during schema refresh")

        recheck()
        origin = origins[key]
        self._snapshot = None
        self._previous = None
        self._previous_fragments = None
        self._resolver.clear()
        self._generation += 1
        try:
            schema = load_from_url(origin.origin, environment=environment)
            text = json.dumps(introspection_from_schema(schema))
        except ValueError:
            previous = self._last_good.get(key)
            if (
                previous is not None
                and previous.config_token == origin.config_token
                and previous.credential_token == credential_token
            ):
                self._remote[key] = replace(previous, stale=True)
            else:
                self._remote.pop(key, None)
            status = "stale" if key in self._remote else "unavailable"
        else:
            record = RemoteSchemaSnapshot(
                key[0],
                key[1],
                origin.config_token,
                credential_token,
                self._generation,
                uuid4().hex,
                text,
            )
            self._remote[key] = self._last_good[key] = record
            status = "ready"
        recheck()
        self._remote_revisions[key] = revision
        self._schema_buffers = buffers[len(capture.buffers) :]
        self._identity = uuid4().hex
        return {
            "ok": True,
            "id": self._identity,
            "project": key[0],
            "schema_name": key[1],
            "status": status,
            "last_good": status == "stale",
            "error": None if status == "ready" else "schema-refresh-failed",
        }


def _pairs(items: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in items:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def _constant(_value: str) -> object:
    raise ValueError("Nonfinite JSON")


def _float(value: str) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("Nonfinite JSON")
    return result


def _encode_analysis_pages(result: dict[str, object]) -> tuple[bytes, ...]:
    def identity(value: object) -> tuple[str, str, str, int]:
        module = _object(value, {"project", "module", "uri", "version"})
        return (
            _string(module["project"]),
            _string(module["module"]),
            _string(module["uri"]),
            _version(module["version"]),
        )

    response = result.get("response")
    if not isinstance(response, dict):
        return ()
    response = cast(dict[str, object], response)
    try:
        modules = _array(response.get("modules"))
        calls = _array(response.get("calls"))
        indexes = {identity(module): index for index, module in enumerate(modules)}
        if len(indexes) != len(modules):
            return ()

        def index_call(value: object) -> dict[str, object]:
            if not isinstance(value, dict):
                raise ValueError("Invalid analysis call")
            call = cast(dict[str, object], value)
            maps = _object(call.get("map"), {"codepoint", "utf16"})
            indexed: dict[str, list[dict[str, object]]] = {}
            for encoding, atoms in maps.items():
                indexed[encoding] = []
                for item in _array(atoms):
                    atom = _object(item, {"decoded", "host", "linear"})
                    host = _object(atom["host"], {"module", "range"})
                    owner = indexes.get(identity(host["module"]))
                    if owner is None:
                        raise ValueError("Unknown analysis map owner")
                    indexed[encoding].append(
                        {**atom, "host": {**host, "module": owner}}
                    )
            return {**call, "map": indexed}

        compact = bytearray()
        limit = ANALYSIS_PAGE_BYTES * MAX_ANALYSIS_PAGES
        encoder = json.JSONEncoder(ensure_ascii=True, separators=(",", ":"))

        def add(data: bytes) -> None:
            if len(compact) + len(data) >= limit:
                raise ValueError("Oversized analysis reply")
            compact.extend(data)

        def encode_fields(fields: dict[str, object], *, nested: bool = False) -> None:
            add(b"{")
            for index, (key, value) in enumerate(fields.items()):
                if index:
                    add(b",")
                add(encoder.encode(key).encode("ascii") + b":")
                if not nested and key == "response":
                    encode_fields(response, nested=True)
                elif nested and key == "calls":
                    add(b"[")
                    for call_index, call in enumerate(calls):
                        if call_index:
                            add(b",")
                        add(encoder.encode(index_call(call)).encode("ascii"))
                    add(b"]")
                else:
                    add(encoder.encode(value).encode("ascii"))
            add(b"}")

        encode_fields(result)
    except (ValueError, RecursionError):
        return ()

    compact.append(10)
    size = ANALYSIS_PAGE_BYTES
    count = (len(compact) + size - 1) // size
    content = memoryview(compact)
    frames: list[bytes] = []
    for index in range(count):
        payload = base64.b64encode(
            gzip.compress(
                content[index * size : (index + 1) * size], compresslevel=1, mtime=0
            )
        )
        frame = (
            f'["gzip-analysis-page",{index},{count},"'.encode("ascii")
            + payload
            + b'"]\n'
        )
        if len(frame) > MAX_LINE_BYTES:
            return ()
        frames.append(frame)
    return tuple(frames)


def serve(
    reader: BinaryIO, writer: BinaryIO, *, config_workspace: Path | None = None
) -> None:
    """Read bounded UTF-8 lines; synchronous cancellation requires caller shutdown."""
    worker = EditorWorker(config_workspace=config_workspace)
    while line := reader.readline(MAX_LINE_BYTES + 1):
        value: object = None
        result: dict[str, object]
        try:
            if len(line) > MAX_LINE_BYTES:
                while not line.endswith(b"\n"):
                    line = reader.readline(MAX_LINE_BYTES + 1)
                    if not line:
                        break
                raise ValueError("Oversized request")
            value = json.loads(
                line.decode("utf-8"),
                object_pairs_hook=_pairs,
                parse_constant=_constant,
                parse_float=_float,
            )
            result = worker.handle(value)
        except (ValueError, UnicodeError, RecursionError):
            if os.environ.get("GRAPHQL_DESK_CAPTURE_WORKER_STDERR") == "1":
                traceback.print_exc(file=sys.stderr)
            worker.invalidate()
            result = {"ok": False, "error": "editor-request-failed"}
        encoded = (json.dumps(result, ensure_ascii=True) + "\n").encode("utf-8")
        frames: tuple[bytes, ...]
        if len(encoded) <= MAX_LINE_BYTES:
            frames = (encoded,)
        else:
            del encoded
            frames = (
                _encode_analysis_pages(result)
                if isinstance(value, dict)
                and cast(dict[str, object], value).get("op") == "analyze"
                and result.get("ok") is True
                else ()
            )
        if not frames:
            worker.invalidate()
            frames = (b'{"ok": false, "error": "editor-request-failed"}\n',)
        for frame in frames:
            writer.write(frame)
        writer.flush()


if __name__ == "__main__":
    arguments = sys.argv[1:]
    config_workspace = None
    if arguments:
        if len(arguments) != 2 or arguments[0] != "--config-workspace":
            raise SystemExit("Invalid editor worker startup arguments")
        config_workspace = _path(arguments[1])
    serve(sys.stdin.buffer, sys.stdout.buffer, config_workspace=config_workspace)
