"""Trusted-caller, read-only editor worker; UTF-8 NDJSON on standard streams."""

import hashlib
import json
import math
import os
import re
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import BinaryIO, cast
from uuid import uuid4

from graphql import introspection_from_schema

from .editor_analysis import (
    EditorAnalysis,
    analyze_editor_snapshot_details,
    editor_diagnostic,
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
from .factory_generation import FactoryArtifacts, generate_project_factory_artifacts
from .fragments import FragmentIndex
from .schema_url import load_from_url
from .source_graph import IncrementalResolver, ModuleDependency

MAX_LINE_BYTES = 8 * 1024 * 1024
_CAPTURE_KEYS = {"workspace", "candidates", "buffers", "documents", "editor_fallback"}


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


def _path(value: object) -> Path:
    path = Path(_string(value))
    if not path.is_absolute():
        raise ValueError("Absolute local path required")
    return path


def _canonical(path: Path) -> Path:
    return Path(os.path.normcase(str(path.resolve())))


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
    allowed = {_canonical(path) for path in candidates}
    base = _canonical(workspace)
    for path in candidates:
        parent = _canonical(path).parent
        if not parent.is_relative_to(base):
            raise ValueError("Candidate escapes workspace")
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
    return _Capture(
        workspace,
        tuple(sorted(candidates, key=str)),
        tuple(sorted(buffers, key=lambda buffer: str(buffer.path))),
        tuple(sorted(documents, key=lambda doc: doc.identity)),
        json.dumps(fallback, sort_keys=True, allow_nan=False),
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


class EditorWorker:
    """One workspace/job; callers own trust, cancellation and credential revisions.

    Only explicit refresh fetches a prepared target. Analyze/current must repeat
    every active remote credential revision; changes require refresh/invalidation.
    Failed refresh retains matching last-good only as visible, unavailable state.
    """

    def __init__(self) -> None:
        self._workspace: Path | None = None
        self._generation = 0
        self._identity: str | None = None
        self._capture: _Capture | None = None
        self._preparation: EditorPreparation | None = None
        self._snapshot: EditorSnapshot | None = None
        self._analysis: EditorAnalysis | None = None
        self._previous: EditorSnapshot | None = None
        self._schema_buffers: tuple[OpenBuffer, ...] = ()
        self._resolver = IncrementalResolver()
        self._remote: dict[tuple[str, str], RemoteSchemaSnapshot] = {}
        self._last_good: dict[tuple[str, str], RemoteSchemaSnapshot] = {}
        self._remote_revisions: dict[tuple[str, str], str] = {}

    def _clear_job(self) -> None:
        self._identity = None
        self._capture = None
        self._preparation = None
        self._snapshot = None
        self._analysis = None
        self._previous = None
        self._schema_buffers = ()
        self._resolver.clear()

    def invalidate(self) -> None:
        self._clear_job()
        self._remote.clear()
        self._last_good.clear()
        self._remote_revisions.clear()

    def handle(self, value: object) -> dict[str, object]:
        """Malformed/failed messages invalidate all previously issued job IDs."""
        try:
            return self._handle(value)
        except Exception:
            self.invalidate()
            return {"ok": False, "error": "editor-request-failed"}

    def _handle(self, value: object) -> dict[str, object]:
        if not isinstance(value, dict):
            raise ValueError("Invalid request")
        raw = cast(dict[str, object], value)
        operation = raw.get("op")
        if not isinstance(operation, str) or operation not in {
            "prepare",
            "analyze",
            "current",
            "refresh",
            "artifacts",
        }:
            raise ValueError("Invalid operation")
        keys = {"op"} | _CAPTURE_KEYS
        if operation != "prepare":
            keys |= {"id", "schema_buffers"}
        if operation == "refresh":
            keys |= {"project", "schema_name", "credential_revision", "environment"}
        elif (
            operation in {"prepare", "analyze", "current", "artifacts"}
            and "remote_revisions" in raw
        ):
            keys.add("remote_revisions")
        item = _object(raw, keys)
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
            or capture != self._capture
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
        if operation in {"current", "artifacts"}:
            current = (
                self._snapshot is not None
                and schema_buffers == self._schema_buffers
                and is_editor_snapshot_current(
                    self._snapshot,
                    candidates=capture.candidates,
                    buffers=buffers,
                    editor_fallback=capture.fallback,
                    remote_schemas=remote_schemas,
                )
            )
            if not current:
                self._clear_job()
            if operation == "current":
                return {"ok": True, "current": current}
            if not current or self._snapshot is None or self._analysis is None:
                raise ValueError("Artifacts require a current analyzed job")
            projects: list[dict[str, object]] = []
            facts = self._analysis
            snapshots = {
                ModuleDependency(
                    module.project, module.module, module.uri, module.version
                ): module.source
                for module in facts.modules
            }
            for config in self._snapshot.project_configs:
                project = config.path.as_uri()
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
                        modules=tuple(
                            module
                            for module in facts.modules
                            if module.project == project
                        ),
                        project_id=project,
                    )
                )
                projects.append(
                    {
                        "project": project,
                        "generated_package": config.generated_package,
                        "files": [
                            {"relative_path": file.relative_path, "text": file.text}
                            for file in artifacts.files
                        ],
                        "diagnostics": [
                            editor_diagnostic(diagnostic, snapshots)
                            for diagnostic in artifacts.diagnostics
                        ],
                    }
                )
            return {
                "ok": True,
                "id": self._identity,
                "projects": projects,
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
        )
        request = snapshot.request
        analysis = analyze_editor_snapshot_details(request, resolver=self._resolver)
        self._analysis = analysis
        self._snapshot = self._previous = snapshot
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
                )
                != self._preparation
            ):
                raise ValueError("Configuration changed during schema refresh")

        recheck()
        origin = origins[key]
        self._snapshot = None
        self._previous = None
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


def serve(reader: BinaryIO, writer: BinaryIO) -> None:
    """Read bounded UTF-8 lines; synchronous cancellation requires caller shutdown."""
    worker = EditorWorker()
    while line := reader.readline(MAX_LINE_BYTES + 1):
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
            worker.invalidate()
            result = {"ok": False, "error": "editor-request-failed"}
        encoded = (json.dumps(result, ensure_ascii=True) + "\n").encode("utf-8")
        if len(encoded) > MAX_LINE_BYTES:
            worker.invalidate()
            encoded = b'{"ok": false, "error": "editor-request-failed"}\n'
        writer.write(encoded)
        writer.flush()


if __name__ == "__main__":
    serve(sys.stdin.buffer, sys.stdout.buffer)
