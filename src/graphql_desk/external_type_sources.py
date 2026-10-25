"""Capture bounded Python source for external type-symbol checks."""

import ast
import keyword
import sys
import unicodedata
from collections.abc import Iterable
from pathlib import Path

from .source_graph import ModuleSnapshot

_MAX_MODULES = 256
_MAX_BYTES = 2_000_000


def interpreter_type_roots(
    search_path: Iterable[str], *, excluded_roots: tuple[Path, ...]
) -> tuple[Path, ...]:
    local_roots = tuple(root.resolve() for root in excluded_roots)
    roots: list[Path] = []
    for value in search_path:
        if not value:
            continue
        try:
            root = Path(value).resolve(strict=True)
        except (OSError, RuntimeError):
            continue
        if root.is_dir() and not any(
            root == local or root.is_relative_to(local) for local in local_roots
        ):
            roots.append(root)
    return tuple(dict.fromkeys(roots))


def _has_local_import_candidate(symbol: str, roots: tuple[Path, ...]) -> bool:
    parts = symbol.split(".")
    for root in roots:
        package = root / parts[0]
        if any(
            path.exists() or path.is_symlink()
            for path in (
                root / f"{parts[0]}.py",
                root / f"{parts[0]}.pyi",
                root / f"{parts[0]}-stubs",
                package,
            )
        ):
            return True
    return False


def capture_external_type_sources(
    symbols: tuple[str, ...],
    *,
    search_roots: tuple[Path, ...],
    project_id: str,
    shadow_roots: tuple[Path, ...] = (),
) -> tuple[ModuleSnapshot, ...]:
    """Read only matching `.pyi`/`.py` sources; never import configured modules."""
    if not project_id or any(
        len(parts := symbol.split(".")) < 2
        or any(
            not part.isidentifier()
            or keyword.iskeyword(part)
            or unicodedata.normalize("NFKC", part) != part
            for part in parts
        )
        for symbol in symbols
    ):
        raise ValueError("External type symbols require qualified canonical names")

    roots = tuple(dict.fromkeys(root.resolve() for root in search_roots))
    selected: dict[str, tuple[Path, bool]] = {}

    def add_module(module: str) -> None:
        if not module or module in selected:
            return
        parts = module.split(".")
        stub_roots: list[tuple[Path, bool]] = []
        namespace_stub_root = False
        for root in roots:
            try:
                stub_root = (root / f"{parts[0]}-stubs").resolve(strict=True)
                stub_root.relative_to(root)
            except (OSError, ValueError, RuntimeError):
                continue
            if not stub_root.is_dir():
                continue
            marker = stub_root / "__init__.pyi"
            if not marker.is_file():
                namespace_stub_root = True
                continue
            partial = False
            partial_path = stub_root / "py.typed"
            try:
                partial_marker = partial_path.resolve(strict=True)
                partial_marker.relative_to(root)
                if partial_marker.stat().st_size > 1024:
                    raise ValueError("External stub package marker is too large")
                partial_text = partial_path.read_text(encoding="utf-8")
            except FileNotFoundError:
                pass
            except (OSError, UnicodeError, ValueError, RuntimeError):
                raise ValueError("Cannot read external stub package marker") from None
            else:
                partial = "partial" in partial_text.splitlines()
            stub_roots.append((stub_root, partial))

        if namespace_stub_root and stub_roots:
            raise ValueError("Ambiguous external stub package")
        if len(stub_roots) > 1:
            raise ValueError("Ambiguous external stub package")
        if namespace_stub_root:
            return
        if stub_roots:
            stub_root, partial = stub_roots[0]
            base = stub_root.joinpath(*parts[1:])
            stub_candidates = (
                (((base / "__init__.pyi",), True),)
                if len(parts) == 1
                else (
                    ((base.with_suffix(".pyi"),), False),
                    ((base / "__init__.pyi",), True),
                )
            )
            choices: list[tuple[Path, bool]] = []
            for stub_paths, is_package in stub_candidates:
                for path in stub_paths:
                    try:
                        resolved = path.resolve(strict=True)
                        resolved.relative_to(stub_root)
                    except (OSError, ValueError, RuntimeError):
                        continue
                    if resolved.is_file():
                        choices.append((resolved, is_package))
                        break
            if len(choices) > 1:
                raise ValueError("Ambiguous external stub module")
            if choices:
                selected[module] = choices[0]
                return
            if not partial:
                return

        runtime_matches: list[tuple[Path, bool]] = []
        for root in roots:
            base = root.joinpath(*parts)
            runtime_choices: list[tuple[Path, bool]] = []
            for paths, is_package in (
                ((base.with_suffix(".pyi"), base.with_suffix(".py")), False),
                ((base / "__init__.pyi", base / "__init__.py"), True),
            ):
                for path in paths:
                    try:
                        resolved = path.resolve(strict=True)
                        resolved.relative_to(root)
                    except (OSError, ValueError, RuntimeError):
                        continue
                    if resolved.is_file():
                        runtime_choices.append((resolved, is_package))
                        break
            if len(runtime_choices) > 1:
                raise ValueError("Ambiguous external type module")
            runtime_matches.extend(runtime_choices)
        if len(runtime_matches) > 1:
            raise ValueError("Ambiguous external type module")
        if runtime_matches:
            selected[module] = runtime_matches[0]

    for symbol in symbols:
        if _has_local_import_candidate(symbol, shadow_roots):
            continue
        imported, _, _ = symbol.rpartition(".")
        parts = imported.split(".")
        for end in range(1, len(parts) + 1):
            add_module(".".join(parts[:end]))

    sources: dict[str, ModuleSnapshot] = {}
    queued = list(selected)
    consumed = 0
    while queued:
        module = queued.pop(0)
        if module in sources:
            continue
        if len(sources) >= _MAX_MODULES:
            raise ValueError("External type source limit exceeded")
        path, is_package = selected[module]
        size = path.stat().st_size
        if consumed + size > _MAX_BYTES:
            raise ValueError("External type source size limit exceeded")
        source = path.read_text(encoding="utf-8")
        consumed += len(source.encode("utf-8"))
        if consumed > _MAX_BYTES:
            raise ValueError("External type source size limit exceeded")
        sources[module] = ModuleSnapshot(
            project_id, module, path.as_uri(), 1, source, is_package
        )
        try:
            tree = ast.parse(source)
        except (SyntaxError, ValueError, UnicodeError, RecursionError):
            continue
        for statement in tree.body:
            if isinstance(statement, ast.Import):
                imported_modules = tuple(alias.name for alias in statement.names)
            elif isinstance(statement, ast.ImportFrom):
                package = module.split(".")
                if not is_package:
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
                imported_modules = ((imported_name,) if imported_name else ()) + tuple(
                    f"{imported_name}.{alias.name}"
                    for alias in statement.names
                    if imported_name and alias.name != "*"
                )
            else:
                continue
            for imported_module in imported_modules:
                if imported_module.partition(".")[0] in sys.stdlib_module_names:
                    continue
                before = len(selected)
                parts = imported_module.split(".")
                for end in range(1, len(parts) + 1):
                    add_module(".".join(parts[:end]))
                if len(selected) != before:
                    queued.extend(
                        name
                        for name in selected
                        if name not in sources and name not in queued
                    )

    return tuple(sources[name] for name in sorted(sources))
