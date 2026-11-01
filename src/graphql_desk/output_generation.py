"""Owned publication for generated schema factory packages."""

import hashlib
import json
import re
import shutil
import tempfile
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import cast

_OWNER = "graphql-desk-generated-output"
_FORMAT = 1
_MANIFEST = ".graphql-desk-manifest.json"


class OutputGenerationConflict(ValueError):
    """A concurrent destination was retained for manual recovery."""


def publish_output_package(
    root: Path,
    files: Mapping[str, str],
    *,
    generated_package: str,
    disable_preprocessing: bool,
) -> None:
    """Atomically replace only a verified GraphQL Desk-owned package tree."""
    paths = _validated_files(files)
    if _MANIFEST in paths:
        raise ValueError("Generated output cannot replace its ownership manifest")
    for parent in (root.absolute(), *root.absolute().parents):
        if parent.is_symlink():
            raise ValueError("Generated package path contains a symlink")

    manifest = {
        "owner": _OWNER,
        "format": _FORMAT,
        "mode": "retained",
        "generated_package": generated_package,
        "disable_preprocessing": disable_preprocessing,
        "files": [
            {
                "path": path,
                "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            }
            for path, text in sorted(files.items())
        ],
    }
    contents = {
        **files,
        _MANIFEST: json.dumps(manifest, sort_keys=True, indent=2) + "\n",
    }
    _validate_existing(root, generated_package)
    root = root.absolute()
    root.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=".graphql-desk-output-", dir=root.parent
    ) as temporary:
        staged = Path(temporary) / "package"
        staged.mkdir()
        for name, text in contents.items():
            target = staged.joinpath(*PurePosixPath(name).parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8", newline="\n")
        backup_directory: Path | None = None
        backup: Path | None = None
        try:
            if root.exists():
                # Recheck immediately before the directory swap. A user may
                # have changed generated files while the replacement was staged.
                _validate_existing(root, generated_package)
                backup_directory = Path(
                    tempfile.mkdtemp(prefix=".graphql-desk-previous-", dir=root.parent)
                )
                backup = backup_directory / "package"
                root.replace(backup)
            staged.replace(root)
        except BaseException as error:
            if backup is not None and backup.exists():
                if root.exists():
                    try:
                        recovery_directory = Path(
                            tempfile.mkdtemp(
                                prefix=".graphql-desk-conflict-", dir=root.parent
                            )
                        )
                    except OSError:
                        raise OutputGenerationConflict(
                            "A concurrent destination remains in place; the previous "
                            f"package is preserved at {backup}."
                        ) from error
                    recovery = recovery_directory / "package"
                    try:
                        root.replace(recovery)
                    except OSError:
                        raise OutputGenerationConflict(
                            "A concurrent destination remains in place; the previous "
                            f"package is preserved at {backup}."
                        ) from error
                    try:
                        backup.replace(root)
                    except OSError:
                        raise OutputGenerationConflict(
                            "The concurrent package was preserved at "
                            f"{recovery}; the previous package is preserved at "
                            f"{backup}."
                        ) from error
                    if backup_directory is not None:
                        shutil.rmtree(backup_directory)
                    raise OutputGenerationConflict(
                        "A concurrent package was preserved at "
                        f"{recovery}; the previous package was restored. "
                        "Resolve the concurrent package and retry generation."
                    ) from error
                try:
                    backup.replace(root)
                except OSError:
                    raise OutputGenerationConflict(
                        f"The previous package is preserved at {backup}."
                    ) from error
                if backup_directory is not None:
                    shutil.rmtree(backup_directory)
            elif backup_directory is not None:
                shutil.rmtree(backup_directory)
            raise
        if backup_directory is not None:
            shutil.rmtree(backup_directory)


def _validated_files(files: Mapping[str, str]) -> set[str]:
    paths: set[str] = set()
    folded: set[str] = set()
    for name in files:
        relative = PurePosixPath(name)
        if (
            not name
            or relative.is_absolute()
            or ".." in relative.parts
            or "\\" in name
            or relative.as_posix() != name
        ):
            raise ValueError("Generated output contains an unsafe path")
        if name in paths or name.casefold() in folded:
            raise ValueError("Generated output paths collide")
        paths.add(name)
        folded.add(name.casefold())
    if not paths:
        raise ValueError("Generated output is empty")
    return paths


def _validate_existing(root: Path, generated_package: str) -> None:
    root = root.absolute()
    if not root.exists():
        return
    if not root.is_dir() or root.is_symlink():
        raise ValueError("Generated package is not a safe directory")
    manifest_path = root / _MANIFEST
    if not manifest_path.exists():
        if any(root.iterdir()):
            raise ValueError("Generated package is not GraphQL Desk-owned")
        return
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise ValueError("Generated package manifest is not a regular file")
    try:
        value = cast(object, json.loads(manifest_path.read_text(encoding="utf-8")))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ValueError("Generated package manifest is invalid") from None
    if not isinstance(value, dict):
        raise ValueError("Generated package manifest is invalid")
    record = cast(dict[str, object], value)
    if (
        record.get("owner") != _OWNER
        or record.get("format") != _FORMAT
        or record.get("mode") != "retained"
        or record.get("generated_package") != generated_package
    ):
        raise ValueError("Generated package is not GraphQL Desk-owned")
    entries = record.get("files")
    if not isinstance(entries, list):
        raise ValueError("Generated package manifest is invalid")
    previous: dict[str, str] = {}
    for entry in cast(list[object], entries):
        if not isinstance(entry, dict):
            raise ValueError("Generated package manifest is invalid")
        item = cast(dict[str, object], entry)
        name, digest = item.get("path"), item.get("sha256")
        if (
            not isinstance(name, str)
            or not isinstance(digest, str)
            or name in previous
            or not re.fullmatch(r"[0-9a-f]{64}", digest)
        ):
            raise ValueError("Generated package manifest is invalid")
        _validated_files({name: ""})
        previous[name] = digest
    if _MANIFEST in previous:
        raise ValueError("Generated package manifest is invalid")
    descendants = tuple(root.rglob("*"))
    if any(path.is_symlink() for path in descendants):
        raise ValueError("Generated package contains a symlink")
    expected = set(previous) | {_MANIFEST}
    actual = {
        path.relative_to(root).as_posix() for path in descendants if path.is_file()
    }
    if actual != expected:
        raise ValueError("Generated package contains unowned files")
    directories = {
        path.relative_to(root).as_posix() for path in descendants if path.is_dir()
    }
    allowed_directories = {"."}
    for name in previous:
        parent = PurePosixPath(name).parent
        while parent != PurePosixPath("."):
            allowed_directories.add(parent.as_posix())
            parent = parent.parent
    if directories - allowed_directories:
        raise ValueError("Generated package contains unowned directories")
    for name, digest in previous.items():
        target = root.joinpath(*PurePosixPath(name).parts)
        if (
            not target.is_file()
            or hashlib.sha256(target.read_bytes()).hexdigest() != digest
        ):
            raise ValueError("Generated package changed; preserved")
