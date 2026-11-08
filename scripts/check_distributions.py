"""Build checked source archives and install both wheels without network access."""

import base64
import configparser
import csv
import hashlib
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from collections.abc import Generator
from contextlib import contextmanager
from email.parser import Parser
from pathlib import Path, PurePosixPath
from typing import Any, cast

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

SOURCE_ROOTS = (
    "src/graphql_desk",
    "docs/release",
    "pyproject.toml",
    "README.md",
    "LICENSE",
)
SDIST_CONTROLS = (".gitignore", ".hgignore")


def check_repository_path(root: Path, path: Path) -> None:
    if not path.is_absolute() or not path.is_relative_to(root) or ".." in path.parts:
        raise ValueError("Distribution path must stay inside the repository")
    for parent in (path, *path.parents):
        if parent == root.parent:
            break
        try:
            value = parent.lstat()
        except FileNotFoundError:
            continue
        if (
            stat.S_ISLNK(value.st_mode)
            or getattr(value, "st_file_attributes", 0) & 0x400
        ):
            raise ValueError("Distribution path contains a link or reparse point")


def _member(name: str) -> None:
    if (
        not name
        or name.startswith("/")
        or "\\" in name
        or ":" in name
        or "\0" in name
        or any(part in {"", ".", ".."} for part in name.split("/"))
    ):
        raise ValueError("Unsafe distribution archive member")


def _project(root: Path) -> dict[str, Any]:
    data = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    includes = data["tool"]["hatch"]["build"]["targets"]["sdist"]["only-include"]
    if includes != list(SOURCE_ROOTS):
        raise ValueError("Source archive inclusion policy needs review")
    if data["project"].get("license") != "MIT" or "LICENSE" not in data["project"].get(
        "license-files", []
    ):
        raise ValueError("Project MIT license selection needs review")
    return cast(dict[str, Any], data["project"])


def source_inputs(root: Path) -> dict[str, bytes]:
    result = subprocess.run(
        ["git", "ls-files", "--stage", "-z", "--", *SOURCE_ROOTS],
        cwd=root,
        check=True,
        stdout=subprocess.PIPE,
    )
    files: dict[str, bytes] = {}
    for entry in result.stdout.decode("utf-8").split("\0"):
        if not entry:
            continue
        metadata, name = entry.split("\t", 1)
        mode, _digest, stage = metadata.split()
        _member(name)
        if not any(
            name == base or name.startswith(base + "/") for base in SOURCE_ROOTS
        ):
            raise ValueError("Source archive input is outside the maintained roots")
        if mode not in {"100644", "100755"} or stage != "0":
            raise ValueError("Source archive requires regular, resolved tracked files")
        file = root / name
        check_repository_path(root, file)
        if not file.is_file():
            raise ValueError("Source archive input is missing or not a file")
        if any(
            part.startswith((".venv", ".tmp", ".cache", ".graphql-desk", ".agent-"))
            or part
            in {"__pycache__", "node_modules", "site-packages", "host-temp", "tests"}
            for part in PurePosixPath(name).parts
        ):
            raise ValueError("Private or generated source archive input")
        files[name] = file.read_bytes()
    for required in (
        "pyproject.toml",
        "README.md",
        "LICENSE",
        "src/graphql_desk/py.typed",
    ):
        if required not in files:
            raise ValueError(f"Missing maintained source archive input: {required}")
    return files


def _license_files(root: Path, project: dict[str, Any]) -> list[str]:
    files: list[str] = []
    for pattern in project["license-files"]:
        selected = [file for file in root.glob(pattern) if file.is_file()]
        if not selected:
            raise ValueError("Third-party notice selection is missing")
        for file in selected:
            check_repository_path(root, file)
            files.append(file.relative_to(root).as_posix())
    if not files or len(files) != len(set(files)):
        raise ValueError("Third-party notice selection is missing or duplicated")
    return sorted(files)


def _metadata(data: bytes, root: Path, project: dict[str, Any]) -> None:
    message = Parser().parsestr(data.decode("utf-8"))
    allowed = {
        "metadata-version",
        "name",
        "version",
        "summary",
        "requires-python",
        "requires-dist",
        "license-file",
        "license-expression",
        "description-content-type",
    }
    if any(field.lower() not in allowed for field in message.keys()):
        raise ValueError("Unexpected distribution metadata field")
    for field, expected in (
        ("Metadata-Version", "2.5"),
        ("Name", project["name"]),
        ("Version", project["version"]),
        ("Summary", project["description"]),
        ("Requires-Python", project["requires-python"]),
        ("License-Expression", "MIT"),
    ):
        if message.get_all(field) != [expected]:
            raise ValueError(f"Distribution metadata differs: {field}")
    requirements = [
        Requirement(value) for value in message.get_all("Requires-Dist", [])
    ]
    expected_requirements = [Requirement(value) for value in project["dependencies"]]
    if len(requirements) != len(expected_requirements) or set(requirements) != set(
        expected_requirements
    ):
        raise ValueError("Distribution runtime dependencies differ")
    if sorted(message.get_all("License-File", [])) != _license_files(root, project):
        raise ValueError("Distribution third-party notice metadata differs")
    if (
        project["readme"] != "README.md"
        or message.get_all("Description-Content-Type") != ["text/markdown"]
        or message.get_payload() != (root / "README.md").read_text(encoding="utf-8")
    ):
        raise ValueError("Distribution description differs")


def _metadata_limit(
    sources: dict[str, bytes], project: dict[str, Any], notices: list[str]
) -> int:
    # Known description/config text plus per-header and fixed-format overhead.
    return (
        len(sources["README.md"])
        + len(sources["pyproject.toml"])
        + sum(len(name.encode("utf-8")) + 32 for name in notices)
        + 32 * len(project["dependencies"])
        + 4096
    )


def _zip_contents(
    path: Path, expected: dict[str, bytes], metadata_limits: dict[str, int]
) -> dict[str, bytes]:
    with zipfile.ZipFile(path) as archive:
        files: dict[str, bytes] = {}
        seen: set[str] = set()
        for item in archive.infolist():
            name = item.filename
            _member(name.rstrip("/"))
            if name.casefold() in seen or stat.S_IFMT(item.external_attr >> 16) not in {
                0,
                stat.S_IFREG,
            }:
                raise ValueError("Duplicate or linked distribution member")
            seen.add(name.casefold())
            if item.is_dir():
                raise ValueError("Unexpected wheel directory entry")
            if name not in expected and name not in metadata_limits:
                raise ValueError("Wheel contains unexpected files")
            if (
                name in expected
                and item.file_size != len(expected[name])
                or name in metadata_limits
                and item.file_size > metadata_limits[name]
            ):
                raise ValueError("Wheel member size differs or exceeds its bound")
            with archive.open(item) as stream:
                data = stream.read(item.file_size + 1)
            if len(data) != item.file_size:
                raise ValueError("Wheel member declared size differs")
            files[name] = data
        return files


def check_core_wheel(path: Path, repository: Path) -> None:
    """Inspect actual bytes and RECORD without extracting/importing the wheel."""
    project = _project(repository)
    name = canonicalize_name(project["name"]).replace("-", "_")
    if path.name != f"{name}-{project['version']}-py3-none-any.whl":
        raise ValueError("Unexpected wheel filename")
    prefix = f"{name}-{project['version']}.dist-info/"
    sources = source_inputs(repository)
    expected = {
        name.removeprefix("src/"): data
        for name, data in sources.items()
        if name.startswith("src/graphql_desk/")
    }
    notices = _license_files(repository, project)
    for name in notices:
        if name not in sources:
            raise ValueError("Untracked third-party notice cannot be distributed")
        expected[f"{prefix}licenses/{name}"] = sources[name]
    metadata = {
        f"{prefix}METADATA": _metadata_limit(sources, project, notices),
        f"{prefix}WHEEL": 4096,
        f"{prefix}entry_points.txt": len(sources["pyproject.toml"]) + 1024,
        f"{prefix}RECORD": 0,
    }
    # CSV quoting can double path bytes; hash/size/punctuation need <128 per row.
    metadata[f"{prefix}RECORD"] = sum(
        2 * len(name.encode("utf-8")) + 128 for name in (*expected, *metadata)
    )
    files = _zip_contents(path, expected, metadata)
    if set(files) != set(expected) | set(metadata):
        raise ValueError("Wheel contains unexpected or missing files")
    if any(files[name] != data for name, data in expected.items()):
        raise ValueError("Wheel source/notice bytes differ")
    _metadata(files[f"{prefix}METADATA"], repository, project)
    wheel = Parser().parsestr(files[f"{prefix}WHEEL"].decode("utf-8"))
    if any(
        wheel.get_all(key) != [value]
        for key, value in (
            ("Wheel-Version", "1.0"),
            ("Root-Is-Purelib", "true"),
            ("Tag", "py3-none-any"),
        )
    ):
        raise ValueError("Unexpected wheel format or platform tag")
    entrypoints = configparser.ConfigParser(interpolation=None)
    entrypoints.read_string(files[f"{prefix}entry_points.txt"].decode("utf-8"))
    if (
        entrypoints.sections() != ["console_scripts"]
        or dict(entrypoints["console_scripts"]) != project["scripts"]
    ):
        raise ValueError("Wheel entry points differ")
    rows = list(csv.reader(io.StringIO(files[f"{prefix}RECORD"].decode("utf-8"))))
    if any(len(row) != 3 for row in rows) or len(rows) != len(files):
        raise ValueError("Invalid wheel RECORD")
    if {row[0] for row in rows} != set(files):
        raise ValueError("Wheel RECORD membership differs")
    for filename, digest, size in rows:
        data = files[filename]
        expected_hash = (
            base64.urlsafe_b64encode(hashlib.sha256(data).digest())
            .rstrip(b"=")
            .decode()
        )
        if filename == f"{prefix}RECORD":
            if digest or size:
                raise ValueError("Wheel RECORD must not hash itself")
        elif digest != f"sha256={expected_hash}" or size != str(len(data)):
            raise ValueError("Wheel RECORD integrity differs")


def check_core_sdist(path: Path, repository: Path) -> None:
    """Allow only maintained tracked build inputs plus official PKG-INFO."""
    project = _project(repository)
    name = canonicalize_name(project["name"]).replace("-", "_")
    if path.name != f"{name}-{project['version']}.tar.gz":
        raise ValueError("Unexpected source archive filename")
    prefix = f"{name}-{project['version']}/"
    expected = {
        **source_inputs(repository),
        **dict.fromkeys(SDIST_CONTROLS, b""),
    }
    metadata_limit = _metadata_limit(
        expected, project, _license_files(repository, project)
    )
    files: dict[str, bytes] = {}
    seen: set[str] = set()
    with tarfile.open(path, "r:gz") as archive:
        for item in archive:
            _member(item.name)
            if not item.isfile() or not item.name.startswith(prefix):
                raise ValueError("Linked or unexpected source archive member")
            relative = item.name.removeprefix(prefix)
            if relative not in expected and relative != "PKG-INFO":
                raise ValueError(
                    "Source archive contains unexpected file: " + repr(relative)[:256]
                )
            if relative.casefold() in seen:
                raise ValueError("Duplicate source archive member")
            seen.add(relative.casefold())
            if (
                relative in expected
                and item.size != len(expected[relative])
                or relative == "PKG-INFO"
                and not 0 <= item.size <= metadata_limit
            ):
                raise ValueError(
                    "Source archive member size differs or exceeds its bound"
                )
            stream = archive.extractfile(item)
            if stream is None:
                raise ValueError("Missing source archive bytes")
            with stream:
                data = stream.read(item.size + 1)
            if len(data) != item.size:
                raise ValueError("Source archive member declared size differs")
            files[relative] = data
    if set(files) != set(expected) | {"PKG-INFO"}:
        raise ValueError("Source archive contains unexpected or missing files")
    if any(files[name] != data for name, data in expected.items()):
        raise ValueError("Source archive bytes differ")
    _metadata(files["PKG-INFO"], repository, project)


@contextmanager
def task_directory(
    root: Path, *, prefix: str = "distribution-check-"
) -> Generator[Path]:
    parent = root / ".tmp"
    check_repository_path(root, parent)
    parent.mkdir(exist_ok=True)
    path = Path(tempfile.mkdtemp(prefix=prefix, dir=parent))
    original = path.stat()
    try:
        yield path
    finally:
        check_repository_path(root, path)
        current = path.stat()
        if (current.st_dev, current.st_ino) != (original.st_dev, original.st_ino):
            raise ValueError(f"Distribution task changed; preserved: {path}")
        # stdlib rmtree removes symlinks/junctions themselves, not their targets.
        shutil.rmtree(path)


def run(*arguments: str, cwd: Path, environment: dict[str, str]) -> None:
    subprocess.run(arguments, cwd=cwd, env=environment, check=True)


@contextmanager
def checked_distributions(root: Path) -> Generator[Path]:
    cache = os.environ.get("UV_CACHE_DIR")
    if not cache:
        raise ValueError("Set a repository-owned UV_CACHE_DIR before uv sync")
    check_repository_path(root, Path(cache))
    if not Path(cache).is_dir():
        raise ValueError("The existing provisioned uv cache is required")
    sources = source_inputs(root)
    project = _project(root)
    with task_directory(root) as work:
        source = work / "source"
        for name, data in sources.items():
            file = source / name
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_bytes(data)
        # Hatch searches parents for these files and force-includes what it finds.
        for name in SDIST_CONTROLS:
            with (source / name).open("xb"):
                pass
        environment = dict(os.environ)
        for name in ("PYTHONPATH", "PYTHONHOME"):
            environment.pop(name, None)
        environment.update(
            TEMP=str(work),
            TMP=str(work),
            TMPDIR=str(work),
            PYTHONDONTWRITEBYTECODE="1",
            UV_PYTHON_DOWNLOADS="never",
        )
        requirements = work / "runtime-requirements.txt"
        run(
            "uv",
            "export",
            "--python",
            sys.executable,
            "--offline",
            "--locked",
            "--no-dev",
            "--no-emit-project",
            "--output-file",
            str(requirements),
            cwd=root,
            environment=environment,
        )
        run(
            sys.executable,
            "-B",
            str(root / "scripts/check_runtime_licenses.py"),
            cwd=root,
            environment=environment,
        )
        direct = work / "direct"
        rebuilt = work / "rebuilt"
        for artifact in ("--sdist", "--wheel"):
            run(
                "uv",
                "build",
                artifact,
                "--offline",
                "--no-build-isolation",
                "--python",
                sys.executable,
                str(source),
                "--out-dir",
                str(direct),
                cwd=work,
                environment=environment,
            )
        wheels = list(direct.glob("*.whl"))
        sdists = list(direct.glob("*.tar.gz"))
        if len(wheels) != 1 or len(sdists) != 1:
            raise ValueError("Expected one official wheel and source archive")
        check_core_wheel(wheels[0], root)
        check_core_sdist(sdists[0], root)
        run(
            "uv",
            "build",
            "--offline",
            "--wheel",
            "--no-build-isolation",
            "--python",
            sys.executable,
            str(sdists[0]),
            "--out-dir",
            str(rebuilt),
            cwd=work,
            environment=environment,
        )
        rebuilt_wheels = list(rebuilt.glob("*.whl"))
        if len(rebuilt_wheels) != 1:
            raise ValueError("Expected one source-rebuilt wheel")
        wheels += rebuilt_wheels
        interpreter = "Scripts/python.exe" if os.name == "nt" else "bin/python"
        for index, wheel in enumerate(wheels):
            check_core_wheel(wheel, root)
            target = work / f"install-{index}"
            run(
                "uv",
                "venv",
                "--offline",
                "--python",
                sys.executable,
                str(target),
                cwd=work,
                environment=environment,
            )
            python = target / interpreter
            run(
                "uv",
                "pip",
                "install",
                "--offline",
                "--python",
                str(python),
                "--require-hashes",
                "--no-deps",
                "-r",
                str(requirements),
                cwd=work,
                environment=environment,
            )
            run(
                "uv",
                "pip",
                "install",
                "--offline",
                "--python",
                str(python),
                "--no-deps",
                str(wheel),
                cwd=work,
                environment=environment,
            )
            run(
                "uv",
                "pip",
                "check",
                "--offline",
                "--python",
                str(python),
                cwd=work,
                environment=environment,
            )
            run(
                str(python),
                "-I",
                "-B",
                "-c",
                "import graphql_desk, importlib.metadata as m, pathlib, sys; "
                f"assert m.version('graphql-desk') == {project['version']!r}; "
                "p = pathlib.Path(graphql_desk.__file__).resolve(); "
                "assert p.is_relative_to(pathlib.Path(sys.prefix).resolve()); "
                "assert any(str(f).endswith('/py.typed') "
                "for f in m.files('graphql-desk') or []); "
                "print(graphql_desk.__file__)",
                cwd=work,
                environment=environment,
            )
            command = target / (
                "Scripts/graphql-desk.exe" if os.name == "nt" else "bin/graphql-desk"
            )
            run(str(command), "--help", cwd=work, environment=environment)
            run(
                str(python),
                "-I",
                "-B",
                "-m",
                "graphql_desk",
                "--help",
                cwd=work,
                environment=environment,
            )
        print(
            json.dumps(
                {
                    "checked_wheels": [
                        hashlib.sha256(p.read_bytes()).hexdigest() for p in wheels
                    ],
                    "scope": "internal wheel/sdist offline install; "
                    "release approval remains open",
                }
            )
        )

        yield work / "install-0" / interpreter


def main() -> None:
    with checked_distributions(Path(__file__).resolve().parents[1]):
        pass


if __name__ == "__main__":
    main()
