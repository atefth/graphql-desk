"""Select a finite set of supplied source paths without reading Python text."""

import keyword
import os
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from .config import ProjectConfig, generated_output_paths
from .project import (
    find_project,
    normalize_config_texts,
    validate_project_ownership,
)


@dataclass(frozen=True)
class SourceFile:
    path: Path
    source_root: Path
    module: str
    is_package: bool


def _matches(path: str, pattern: str) -> bool:
    """Full, case-sensitive component glob; ** consumes zero or more components."""
    parts = path.split("/")
    patterns = pattern.split("/")
    positions = {0}
    for component in patterns:
        if component == "**":
            positions = {
                end for start in positions for end in range(start, len(parts) + 1)
            }
        else:
            expression = "".join(
                "[^/]*" if char == "*" else "[^/]" if char == "?" else re.escape(char)
                for char in component
            )
            positions = {
                start + 1
                for start in positions
                if start < len(parts) and re.fullmatch(expression, parts[start])
            }
    return len(parts) in positions


def _resolved(path: Path) -> Path:
    try:
        return path.resolve()
    except (OSError, ValueError, RuntimeError):
        raise ValueError("Cannot resolve a source path") from None


def _identity(path: Path) -> Path:
    # Case normalization is only for identity; glob paths retain their spelling.
    return Path(os.path.normcase(str(_resolved(path))))


def _module(relative: Path) -> tuple[str, bool]:
    parts = list(relative.with_suffix("").parts)
    if not parts:
        raise ValueError("Source path has no module identity")
    package = parts[-1] == "__init__"
    if package:
        parts.pop()
    if not parts:
        raise ValueError("Source-root __init__.py has no module identity")
    if any(
        not part.isidentifier()
        or keyword.iskeyword(part)
        or unicodedata.normalize("NFKC", part) != part
        for part in parts
    ):
        raise ValueError("Source requires canonical Python module components")
    return ".".join(parts), package


def select_project_sources(
    config: ProjectConfig,
    candidates: tuple[Path, ...],
    *,
    workspace: Path | None = None,
    config_texts: Mapping[Path, str | None] | None = None,
    include_stubs: bool = False,
    validate_entrypoints: bool = True,
) -> tuple[SourceFile, ...]:
    """Select supplied paths; never enumerate directories or read Python text.

    Relative candidates use config.root. Missing files may describe unsaved new
    sources. Existing directories and broken symlinks are rejected. The first
    matching configured root owns an overlapping candidate. Root __init__.py
    cannot name a package; nested __init__.py names its containing package.
    External roots and workspace escape require explicit_cli config context.
    Foreign configured projects retain nearest ownership. Caller-owned source/
    config/schema versions still require lifecycle checks. A supplied closed
    config_texts map also governs every nearest-owner lookup.
    """
    texts = normalize_config_texts(config_texts)
    validate_project_ownership((config,))
    root = _resolved(config.root)
    boundary = _resolved(workspace) if workspace is not None else None
    if (
        boundary is not None
        and config.source_scope != "explicit_cli"
        and not root.is_relative_to(boundary)
    ):
        raise ValueError("Source project escapes its workspace")
    roots = tuple(_resolved(path) for path in config.source_roots)
    if any(path.exists() and not path.is_dir() for path in roots):
        raise ValueError("Source roots must be directories")
    if config.source_scope != "explicit_cli" and any(
        not path.is_relative_to(root) for path in roots
    ):
        raise ValueError("Source roots must stay inside their project")
    package = _resolved(root.joinpath(*config.generated_package.split(".")))
    outputs = {
        _identity(path)
        for path in generated_output_paths(root, package, config.schemas)
    }
    selected: dict[Path, SourceFile] = {}
    names: dict[str, Path] = {}
    entry_matches: dict[str, set[Path]] = {
        entry: set() for entry in config.entrypoints or ()
    }
    ordered_candidates = sorted(
        candidates,
        key=lambda item: (item.suffix != ".pyi", item.as_posix()),
    )
    for candidate in ordered_candidates:
        supplied = root / candidate
        path = _resolved(supplied)
        lexical_roots = [
            base for base in roots if supplied.absolute().is_relative_to(base)
        ]
        matches = [base for base in roots if path.is_relative_to(base)]
        if lexical_roots and not any(
            path.is_relative_to(base) for base in lexical_roots
        ):
            raise ValueError("Source symlink escapes its configured root")
        if not matches:
            continue
        if supplied.is_symlink() and not supplied.exists():
            raise ValueError("Source symlink is broken")
        if path.exists() and not path.is_file():
            raise ValueError("Source candidate must be a file")
        if path.suffix != ".py" and not (include_stubs and path.suffix == ".pyi"):
            continue
        identity = _identity(path)
        if (
            path.is_relative_to(package)
            or path.is_relative_to(root / "build" / "graphql-desk")
            or identity in outputs
        ):
            continue
        eligible: list[Path] = []
        for base in matches:
            relative = path.relative_to(base)
            pattern_paths: tuple[str, ...] = (relative.as_posix(),)
            if include_stubs and path.suffix == ".pyi":
                # Default `.py` include/exclude rules also apply to the matching stub.
                pattern_paths = (relative.with_suffix(".py").as_posix(), *pattern_paths)
            if any(
                _matches(candidate_path, pattern)
                for candidate_path in pattern_paths
                for pattern in config.include
            ) and not any(
                _matches(candidate_path, pattern)
                for candidate_path in pattern_paths
                for pattern in config.exclude
            ):
                eligible.append(base)
        if not eligible:
            continue
        owner = find_project(
            path.parent,
            workspace=root if path.is_relative_to(root) else None,
            selected_config=config,
            config_texts=texts,
        )
        if owner is not None and _identity(owner.path) != _identity(config.path):
            continue
        base = eligible[0]
        name, is_package = _module(path.relative_to(base))
        if name in names and names[name] != identity:
            previous = selected[names[name]]
            if (
                include_stubs
                and previous.source_root == base
                and previous.path.suffix == ".pyi"
                and path.suffix == ".py"
            ):
                continue
            raise ValueError("Distinct sources have the same Python module identity")
        names[name] = identity
        selected.setdefault(identity, SourceFile(path, base, name, is_package))
        for entry in entry_matches:
            if any(path.relative_to(b).as_posix() == entry for b in eligible):
                entry_matches[entry].add(identity)
    if validate_entrypoints and any(
        len(paths) != 1 for paths in entry_matches.values()
    ):
        raise ValueError("Entrypoint is unknown, excluded or ambiguous")
    return tuple(sorted(selected.values(), key=lambda source: source.path.as_posix()))
