"""Trusted-caller, read-only editor worker; UTF-8 NDJSON on standard streams."""

import json
import math
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO, cast
from uuid import uuid4

from .editor_analysis import analyze_editor_snapshot
from .editor_snapshot import (
    EditorPreparation,
    EditorSnapshot,
    OpenBuffer,
    acquire_editor_snapshot,
    is_editor_snapshot_current,
    prepare_editor_snapshot,
)
from .source_graph import IncrementalResolver

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
    """One workspace and active job; callers own trust, cancellation and freshness."""

    def __init__(self) -> None:
        self._workspace: Path | None = None
        self._generation = 0
        self._identity: str | None = None
        self._capture: _Capture | None = None
        self._preparation: EditorPreparation | None = None
        self._snapshot: EditorSnapshot | None = None
        self._previous: EditorSnapshot | None = None
        self._schema_buffers: tuple[OpenBuffer, ...] = ()
        self._resolver = IncrementalResolver()

    def invalidate(self) -> None:
        self._identity = None
        self._capture = None
        self._preparation = None
        self._snapshot = None
        self._previous = None
        self._schema_buffers = ()
        self._resolver.clear()

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
        }:
            raise ValueError("Invalid operation")
        keys = {"op"} | _CAPTURE_KEYS
        if operation != "prepare":
            keys |= {"id", "schema_buffers"}
        item = _object(raw, keys)
        capture = _capture(item)
        workspace = _canonical(capture.workspace)
        if self._workspace is not None and workspace != self._workspace:
            raise ValueError("Worker workspace changed")
        self._workspace = workspace
        if operation == "prepare":
            self._identity = None
            self._snapshot = None
            self._schema_buffers = ()
            preparation = prepare_editor_snapshot(
                workspace=capture.workspace,
                candidates=capture.candidates,
                buffers=capture.buffers,
                editor_fallback=capture.fallback,
            )
            self._capture = capture
            self._preparation = preparation
            self._identity = uuid4().hex
            required = {path.canonical_path for path in preparation.local_schemas}
            return {
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
                "required_buffers": [
                    doc.identity
                    for doc in capture.documents
                    if doc.canonical in required
                ],
            }
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
        if operation == "current":
            current = (
                self._snapshot is not None
                and schema_buffers == self._schema_buffers
                and is_editor_snapshot_current(
                    self._snapshot,
                    candidates=capture.candidates,
                    buffers=buffers,
                    editor_fallback=capture.fallback,
                )
            )
            if not current:
                self.invalidate()
            return {"ok": True, "current": current}
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
        )
        request = snapshot.request
        response = analyze_editor_snapshot(request, resolver=self._resolver)
        self._snapshot = self._previous = snapshot
        self._schema_buffers = schema_buffers
        return {
            "ok": True,
            "id": self._identity,
            "request": request,
            "response": response,
            "unavailable_schemas": snapshot.unavailable_schemas,
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
