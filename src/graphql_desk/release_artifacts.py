"""Build and inspect source-free GraphQL Desk release artifacts."""

import ast
import hashlib
import json
import re
import shutil
import sys
import tempfile
import zipfile
from collections.abc import Mapping, Sequence
from email.parser import Parser
from importlib.metadata import (
    PackageNotFoundError,
    distribution,
    packages_distributions,
)
from pathlib import Path, PurePosixPath
from typing import cast

from graphql import GraphQLSchema

from .config import ProjectConfig
from .parse_generation import render_captured_parse_overlay
from .scalar_types import render_scalar_method
from .schema_types import render_schema_types
from .source_selection import SourceFile

_OWNER = "graphql-desk-omitted-release"
_MANIFEST = "manifest.json"
_IMPORTS_BY_DISTRIBUTION = {"graphql-core": "graphql"}
_DOCUMENT = re.compile(
    r"(?i)\b(?:(?:query|mutation|subscription)(?:\s+[_A-Za-z][_0-9A-Za-z]*)?"
    r"|fragment\s+[_A-Za-z][_0-9A-Za-z]*)\b[\s\S]*?\{"
)


def validate_authoring_separation(
    selected_files: tuple[SourceFile, ...], authoring_roots: tuple[Path, ...]
) -> tuple[str, ...]:
    """Require dedicated static authoring modules and no opaque/importing app path."""
    authoring = {
        item.module
        for item in selected_files
        if any(item.path.is_relative_to(root) for root in authoring_roots)
    }
    if not authoring:
        raise ValueError("authoring_roots must contain selected Python modules")
    for item in selected_files:
        try:
            tree = ast.parse(
                item.path.read_text(encoding="utf-8"), filename=item.module
            )
        except (OSError, SyntaxError, UnicodeError, ValueError):
            raise ValueError(
                "Cannot prove authoring and application module separation"
            ) from None
        if item.module in authoring:
            continue
        package = (
            item.module.split(".") if item.is_package else item.module.split(".")[:-1]
        )
        dynamic_names = _dynamic_import_names(tree)
        for node in ast.walk(tree):
            imported: list[str] = []
            if isinstance(node, ast.Import):
                imported.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                parts = (
                    package[: max(0, len(package) - node.level + 1)]
                    if node.level
                    else []
                )
                if node.module:
                    parts.extend(node.module.split("."))
                base = ".".join(parts)
                if base:
                    imported.append(base)
                    imported.extend(
                        f"{base}.{alias.name}"
                        for alias in node.names
                        if alias.name != "*"
                    )
            if isinstance(node, ast.Call) and (
                isinstance(node.func, ast.Name)
                and node.func.id in dynamic_names
                or isinstance(node.func, ast.Attribute)
                and node.func.attr in {"import_module", "__import__", "eval", "exec"}
            ):
                raise ValueError("Opaque dynamic imports prevent source omission")
            if any(
                imported_name == module
                or imported_name.startswith(module + ".")
                or module.startswith(imported_name + ".")
                for imported_name in imported
                for module in authoring
            ):
                raise ValueError(
                    "Application modules import or may load authoring modules"
                )
    return tuple(sorted(authoring))


def render_omitted_files(
    config: ProjectConfig,
    bindings: Mapping[str, tuple[str, str]],
    *,
    schemas: Mapping[str, GraphQLSchema],
) -> dict[str, str]:
    """Return a package tree with generic factories and typed ID constants only."""
    package = config.generated_package.replace(".", "/")
    files = {
        f"{package}/__init__.py": '"""Generated GraphQL Desk package."""\n',
        f"{package}/__init__.pyi": "",
    }
    parser = render_captured_parse_overlay(())
    files["graphql_desk-stubs/__init__.pyi"] = parser.stub
    files["graphql_desk-stubs/py.typed"] = "partial\n"
    for schema in config.schemas:
        loaded = schemas[schema.name]
        schema_text = render_schema_types(
            loaded, scalars=schema.scalars, enums=schema.enums
        )
        files[f"{package}/{schema.name}.py"] = (
            "from graphql_desk import init_graphql as _init_graphql\n"
            f"graphql = _init_graphql({schema.name!r})\n"
        )
        files[f"{package}/{schema.name}.pyi"] = (
            schema_text + "\nfrom collections.abc import Iterable as _Iterable\n"
            "from typing import Any as _Any, Protocol as _Protocol, "
            "TypeVar as _TypeVar, Literal as __graphql_desk_Literal, "
            "overload as __graphql_desk_overload\n"
            "from graphql import DocumentNode as _DocumentNode\n"
            "from graphql_desk import PersistedDocument as _PersistedDocument\n"
            "from graphql_desk import TypedDocument as _TypedDocument\n\n"
            "class _Document(_Protocol):\n"
            "    ast: _DocumentNode\n"
            "    operation_name: str | None\n"
            "    schema_name: str | None\n\n"
            "_Persisted = _TypeVar('_Persisted', bound=_PersistedDocument[Any, Any])\n"
            "class _Factory(_Protocol):\n"
            "    def __call__(self, source: str, "
            "fragments: _Iterable[_Document] = (), *, "
            "operation: str | None = None) -> "
            "_TypedDocument[Any, Any]: ...\n"
            "    def persisted(self, id: str, "
            "document: _Persisted) -> _Persisted: ...\n"
            + render_scalar_method(loaded, scalars=schema.scalars, enums=schema.enums)
            + "\ngraphql: _Factory\n"
        )
        runtime, stub = bindings[schema.name]
        files[f"{package}/persisted/__init__.py"] = '"""Persisted operations."""\n'
        files[f"{package}/persisted/__init__.pyi"] = ""
        files[f"{package}/persisted/{schema.name}.py"] = runtime
        files[f"{package}/persisted/{schema.name}.pyi"] = stub
    return files


def publish_omitted_tree(
    root: Path,
    files: Mapping[str, str],
    authoring_modules: tuple[str, ...],
    generated_package: str,
) -> None:
    """Replace only a fully verified GraphQL Desk-owned projection tree."""
    manifest = {
        "owner": _OWNER,
        "format": 1,
        "mode": "omitted",
        "generated_package": generated_package,
        "authoring_modules": list(authoring_modules),
        "files": [
            {"path": path, "sha256": hashlib.sha256(text.encode()).hexdigest()}
            for path, text in sorted(files.items())
        ],
    }
    contents = {
        **files,
        _MANIFEST: json.dumps(manifest, sort_keys=True, indent=2) + "\n",
    }
    validate_omitted_tree(root)
    root = root.absolute()
    root.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=".graphql-desk-omitted-", dir=root.parent
    ) as temporary:
        staged = Path(temporary) / "tree"
        staged.mkdir()
        for path, text in contents.items():
            relative = PurePosixPath(path)
            if (
                not path
                or relative.is_absolute()
                or ".." in relative.parts
                or "\\" in path
            ):
                raise ValueError("Invalid omitted release path")
            target = staged.joinpath(*relative.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8", newline="\n")
        backup = None
        try:
            if root.exists():
                backup = Path(temporary) / "previous"
                root.rename(backup)
            staged.rename(root)
        except BaseException:
            if backup is not None and backup.exists() and not root.exists():
                backup.rename(root)
            raise
        if backup is not None:
            shutil.rmtree(backup)


def validate_omitted_tree(root: Path) -> None:
    """Reject a staging path that contains unowned or changed files."""
    root = root.absolute()
    for parent in (root, *root.parents):
        if parent.exists() and parent.is_symlink():
            raise ValueError("Omitted release path contains a symlink")
    old_files: set[str] = set()
    manifest_path = root / _MANIFEST
    if root.exists():
        if not root.is_dir() or root.is_symlink():
            raise ValueError("Omitted release path is not a safe directory")
        if manifest_path.exists():
            if manifest_path.is_symlink() or not manifest_path.is_file():
                raise ValueError("Omitted release manifest is not a regular file")
            try:
                previous = cast(
                    object,
                    json.loads(manifest_path.read_text(encoding="utf-8")),
                )
            except (json.JSONDecodeError, UnicodeError):
                raise ValueError("Omitted release manifest is invalid") from None
            if not isinstance(previous, dict):
                raise ValueError("Omitted release manifest is invalid")
            previous = cast(dict[str, object], previous)
            if (
                previous.get("owner") != _OWNER
                or previous.get("format") != 1
                or previous.get("mode") != "omitted"
            ):
                raise ValueError("Omitted release tree is not GraphQL Desk-owned")
            previous_files = previous.get("files")
            if not isinstance(previous_files, list):
                raise ValueError("Omitted release manifest is invalid")
            for item in cast(list[object], previous_files):
                if not isinstance(item, dict):
                    raise ValueError("Omitted release manifest is invalid")
                item = cast(dict[str, object], item)
                path = item.get("path")
                digest = item.get("sha256")
                if not isinstance(path, str) or not isinstance(digest, str):
                    raise ValueError("Omitted release manifest is invalid")
                if not re.fullmatch(r"[0-9a-f]{64}", digest) or path in old_files:
                    raise ValueError("Omitted release manifest is invalid")
                relative = PurePosixPath(path)
                if (
                    not path
                    or relative.is_absolute()
                    or ".." in relative.parts
                    or "\\" in path
                ):
                    raise ValueError("Omitted release manifest has an invalid path")
                target = root.joinpath(*relative.parts)
                if target.is_symlink() or not target.is_file():
                    raise ValueError("Omitted release tree changed; preserved")
                if hashlib.sha256(target.read_bytes()).hexdigest() != digest:
                    raise ValueError("Omitted release tree changed; preserved")
                old_files.add(path)
            descendants = tuple(root.rglob("*"))
            if any(item.is_symlink() for item in descendants):
                raise ValueError("Omitted release tree contains a symlink; preserved")
            allowed_directories = {"."}
            for name in old_files | {_MANIFEST}:
                relative_parent = PurePosixPath(name).parent
                while relative_parent != PurePosixPath("."):
                    allowed_directories.add(relative_parent.as_posix())
                    relative_parent = relative_parent.parent
            directories = {
                item.relative_to(root).as_posix()
                for item in descendants
                if item.is_dir()
            }
            if directories - allowed_directories:
                raise ValueError(
                    "Omitted release tree contains unowned directories; preserved"
                )
            actual = {
                item.relative_to(root).as_posix()
                for item in descendants
                if item.is_file() and item != manifest_path
            }
            if actual != old_files:
                raise ValueError(
                    "Omitted release tree contains unowned files; preserved"
                )
        elif any(root.rglob("*")):
            raise ValueError("Omitted release tree contains unowned entries; preserved")


def check_wheel(path: Path, manifest_path: Path) -> None:
    """Verify an omitted wheel without importing or executing its contents."""
    if path.suffix != ".whl" or not path.is_file() or path.is_symlink():
        raise ValueError("Artifact must be a regular wheel file")
    try:
        manifest = cast(object, json.loads(manifest_path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError, UnicodeError):
        raise ValueError("Omitted release manifest is invalid") from None
    if not isinstance(manifest, dict):
        raise ValueError("Omitted release manifest is invalid")
    manifest = cast(dict[str, object], manifest)
    if (
        manifest.get("owner") != _OWNER
        or manifest.get("format") != 1
        or manifest.get("mode") != "omitted"
    ):
        raise ValueError("Omitted release manifest is invalid")
    records = manifest.get("files")
    package_name = manifest.get("generated_package")
    modules = manifest.get("authoring_modules")
    if (
        not isinstance(records, list)
        or not isinstance(package_name, str)
        or not isinstance(modules, list)
    ):
        raise ValueError("Omitted release manifest is invalid")
    records = cast(list[object], records)
    module_items = cast(list[object], modules)
    if not all(isinstance(module, str) for module in module_items):
        raise ValueError("Omitted release manifest is invalid")
    modules = cast(list[str], module_items)
    expected: dict[str, str] = {}
    for item in records:
        if not isinstance(item, dict):
            raise ValueError("Omitted release manifest is invalid")
        item = cast(dict[str, object], item)
        name, digest = item.get("path"), item.get("sha256")
        if not isinstance(name, str) or not isinstance(digest, str):
            raise ValueError("Omitted release manifest is invalid")
        relative = PurePosixPath(name)
        if (
            not name
            or relative.is_absolute()
            or ".." in relative.parts
            or "\\" in name
            or not re.fullmatch(r"[0-9a-f]{64}", digest)
        ):
            raise ValueError("Omitted release manifest is invalid")
        expected[name] = digest
    if len(expected) != len(records):
        raise ValueError("Omitted release manifest contains duplicate files")
    if not package_name or any(
        not part.isidentifier() for part in package_name.split(".")
    ):
        raise ValueError("Omitted release manifest is invalid")
    package = package_name.replace(".", "/") + "/"
    authoring = set(modules)
    try:
        archive_context = zipfile.ZipFile(path)
    except (OSError, zipfile.BadZipFile):
        raise ValueError("Artifact is not a valid wheel archive") from None
    with archive_context as archive:
        archive_names = archive.namelist()
        names = {name for name in archive_names if not name.endswith("/")}
        if len(names) != len(
            [name for name in archive_names if not name.endswith("/")]
        ):
            raise ValueError("Wheel contains duplicate files")
        if not set(expected).issubset(names):
            raise ValueError("Wheel is missing files from omitted release manifest")
        metadata_paths = [
            name for name in names if name.endswith(".dist-info/METADATA")
        ]
        if len(metadata_paths) != 1:
            raise ValueError("Wheel must contain one distribution metadata file")
        try:
            metadata = Parser().parsestr(
                _wheel_read(archive, metadata_paths[0]).decode("utf-8")
            )
        except (UnicodeError, ValueError):
            raise ValueError("Wheel distribution metadata is invalid") from None
        requirements = list(metadata.get_all("Requires-Dist", []))
        unconditional = [item for item in requirements if ";" not in item]
        installed_imports = packages_distributions()
        declared = declared_import_roots(unconditional, installed_imports)
        if not _import_resolves("graphql_desk", names, declared):
            raise ValueError(
                "Wheel must include or unconditionally require GraphQL Desk"
            )
        try:
            runtime_requirements = distribution("graphql-desk").requires or ()
        except PackageNotFoundError:
            runtime_requirements = ()
        declared.update(
            declared_import_roots(
                [item for item in runtime_requirements if ";" not in item],
                installed_imports,
            )
        )
        available = names
        generated = {name for name in names if name.startswith(package)}
        if generated != {name for name in expected if name.startswith(package)}:
            raise ValueError("Wheel generated package differs from omitted manifest")
        for name, digest in expected.items():
            data = _wheel_read(archive, name)
            if hashlib.sha256(data).hexdigest() != digest:
                raise ValueError(
                    "Wheel generated artifact differs from omitted manifest"
                )
            if name.endswith((".py", ".pyi")):
                _check_python_bytes(
                    name,
                    data,
                    authoring,
                    available,
                    declared,
                    check_imports=name in expected,
                )
        for module in authoring:
            stem = module.replace(".", "/")
            if any(
                name in names for name in (stem + ".py", stem + ".pyi", stem + ".pyc")
            ) or any(name.startswith(stem + "/") for name in names):
                raise ValueError("Wheel contains an excluded authoring module")
        for name in names:
            if name.endswith((".py", ".pyi")):
                _check_python_bytes(
                    name,
                    _wheel_read(archive, name),
                    authoring,
                    available,
                    declared,
                    check_imports=name in expected,
                )


def _wheel_read(archive: zipfile.ZipFile, name: str) -> bytes:
    try:
        return archive.read(name)
    except (KeyError, OSError, RuntimeError, zipfile.BadZipFile):
        raise ValueError("Wheel is incomplete or corrupt") from None


def _check_python_bytes(
    name: str,
    data: bytes,
    authoring: set[str],
    available: set[str],
    declared: set[str],
    *,
    check_imports: bool,
) -> None:
    try:
        tree = ast.parse(data.decode("utf-8"), filename=name)
    except (SyntaxError, UnicodeError, ValueError):
        raise ValueError(
            "Wheel contains invalid Python source or declarations"
        ) from None
    module_path = name.removesuffix(".pyi").removesuffix(".py")
    if module_path.startswith("graphql_desk-stubs/"):
        module_path = "graphql_desk/" + module_path.removeprefix("graphql_desk-stubs/")
    package_parts = (
        module_path.removesuffix("/__init__").split("/")
        if module_path.endswith("/__init__")
        else module_path.split("/")[:-1]
    )
    dynamic_names = _dynamic_import_names(tree)
    for node in ast.walk(tree):
        imported: list[str] = []
        resolvable: list[str] = []
        if isinstance(node, ast.Import):
            imported.extend(item.name for item in node.names)
            resolvable.extend(item.name for item in node.names)
        elif isinstance(node, ast.ImportFrom):
            parts = (
                package_parts[: max(0, len(package_parts) - node.level + 1)]
                if node.level
                else []
            )
            if node.module:
                parts.extend(node.module.split("."))
            base = ".".join(parts)
            if base:
                imported.append(base)
                resolvable.append(base)
                imported.extend(
                    f"{base}.{item.name}" for item in node.names if item.name != "*"
                )
        elif isinstance(node, ast.Call) and (
            isinstance(node.func, ast.Name)
            and node.func.id in dynamic_names
            or isinstance(node.func, ast.Attribute)
            and node.func.attr in {"import_module", "__import__", "eval", "exec"}
        ):
            raise ValueError("Wheel contains an opaque dynamic import")
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and _DOCUMENT.search(node.value)
        ):
            raise ValueError("Wheel contains retained GraphQL document source")
        if any(
            any(
                value == module or value.startswith(module + ".")
                for module in authoring
            )
            for value in imported
        ):
            raise ValueError("Wheel imports an excluded authoring module")
        if check_imports:
            for value in resolvable:
                if not _import_resolves(value, available, declared):
                    raise ValueError(f"Wheel contains an unresolved import: {value}")


def _dynamic_import_names(tree: ast.Module) -> set[str]:
    names = {"__import__", "eval", "exec"}
    for statement in ast.walk(tree):
        if not isinstance(statement, ast.ImportFrom) or statement.module not in {
            "importlib",
            "builtins",
        }:
            continue
        names.update(
            alias.asname or alias.name
            for alias in statement.names
            if (
                statement.module == "importlib"
                and alias.name == "import_module"
                or statement.module == "builtins"
                and alias.name in {"__import__", "eval", "exec"}
            )
        )
    return names


def declared_import_roots(
    requirements: list[str], installed_imports: Mapping[str, Sequence[str | None]]
) -> set[str]:
    roots: set[str] = set()
    distributions: set[str] = set()
    for requirement in requirements:
        match = re.match(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)", requirement)
        if match is None:
            raise ValueError("Wheel contains invalid dependency metadata")
        name = match.group(1).lower().replace("_", "-").replace(".", "-")
        distributions.add(name)
        roots.add(_IMPORTS_BY_DISTRIBUTION.get(name, name.replace("-", "_")))
    for module, packages in installed_imports.items():
        if any(
            package is not None
            and package.lower().replace("_", "-").replace(".", "-") in distributions
            for package in packages
        ):
            roots.add(module)
    return roots


def _import_resolves(
    module: str, wheel_files: set[str], dependencies: set[str]
) -> bool:
    root = module.split(".", 1)[0]
    if root in sys.stdlib_module_names or root in dependencies:
        return True
    stem = module.replace(".", "/")
    return any(
        path in wheel_files
        for path in (
            stem + ".py",
            stem + ".pyi",
            stem + ".pyc",
            stem + ".so",
            stem + ".pyd",
            stem + "/__init__.py",
            stem + "/__init__.pyi",
        )
    )
