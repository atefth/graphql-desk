"""Fail when the installed runtime dependency closure needs a license review."""

import json
import sys
from hashlib import sha256
from importlib.metadata import distribution
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

from packaging.markers import Marker
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name


def runtime_inventory(root: str) -> list[dict[str, str]]:
    pending = [root]
    seen: set[str] = set()
    inventory: list[dict[str, str]] = []
    approved = {
        "graphql-core": "MIT",
        "tomli": "MIT",
        "tomlkit": "MIT",
        "typing-extensions": "PSF-2.0",
    }
    while pending:
        name = canonicalize_name(pending.pop())
        if name in seen:
            continue
        seen.add(name)
        package = distribution(name)
        for raw in package.requires or []:
            requirement = Requirement(raw)
            if requirement.marker is None or requirement.marker.evaluate({"extra": ""}):
                pending.append(requirement.name)
        if name == canonicalize_name(root):
            continue
        metadata = package.metadata
        license_name = (
            metadata["License-Expression"]
            if "License-Expression" in metadata
            else metadata["License"]
            if "License" in metadata
            else ""
        )
        if approved.get(name) != license_name:
            raise ValueError(
                f"License review required: {name} {package.version}: {license_name}"
            )
        inventory.append(
            {"name": name, "version": package.version, "license": license_name}
        )
    return sorted(inventory, key=lambda item: item["name"])


def _installed_notice_hashes(root: Path, notice: dict[str, Any]) -> set[str]:
    hashes = {str(notice["sha256"])}
    if "lf_sha256" in notice:
        preserved = (root / notice["notice_path"]).read_bytes()
        alternate = sha256(preserved.replace(b"\r\n", b"\n")).hexdigest()
        if alternate != notice["lf_sha256"]:
            raise ValueError("Reviewed newline-only notice variant changed")
        hashes.add(alternate)
    return hashes


def check_release_inventory(root: Path, *, installed_node: bool = False) -> None:
    """Check the reviewed lock projection and notices; never fetch or write files."""
    manifest: dict[str, Any] = json.loads(
        (root / "docs/release/runtime-dependencies.json").read_text(encoding="utf-8")
    )
    if manifest["format"] != 1 or manifest["scope"] != "locked-runtime-candidates":
        raise ValueError("Unknown release inventory format")
    python_lock = tomllib.loads((root / "uv.lock").read_text(encoding="utf-8"))
    packages = {item["name"]: item for item in python_lock["package"]}
    requirements = [
        Requirement(value)
        for value in tomllib.loads(
            (root / "pyproject.toml").read_text(encoding="utf-8")
        )["project"]["dependencies"]
    ]
    direct = {canonicalize_name(value.name): value for value in requirements}
    locked = {
        item["name"]: item.get("marker")
        for item in packages["graphql-desk"]["dependencies"]
    }
    if set(direct) != set(locked):
        raise ValueError("Python runtime requirement/lock drift")
    records = {item["name"]: item for item in manifest["python"]}
    if len(records) != len(manifest["python"]) or set(records) != set(locked):
        raise ValueError("Python runtime inventory drift")
    for name, marker in locked.items():
        item = records[name]
        package = packages[name]
        requirement = direct[name]
        if (
            package.get("dependencies")
            or str(requirement.specifier) != f"=={item['version']}"
            or requirement.marker != (Marker(marker) if marker else None)
            or item["marker"] != marker
            or item["version"] != package["version"]
            or item["source"] != package["source"]
            or item["artifacts"]
            != [
                {"url": artifact["url"], "hash": artifact["hash"]}
                for artifact in [package["sdist"], *package["wheels"]]
            ]
        ):
            raise ValueError(f"Python runtime lock review required: {name}")
        if requirement.marker is None or requirement.marker.evaluate({"extra": ""}):
            installed = distribution(name)
            license_name = (
                installed.metadata["License-Expression"]
                if "License-Expression" in installed.metadata
                else installed.metadata["License"]
                if "License" in installed.metadata
                else ""
            )
            if installed.version != item["version"] or license_name != item["spdx"]:
                raise ValueError(f"Installed Python metadata drift: {name}")
            for notice in item["license_files"]:
                data = Path(
                    str(installed.locate_file(notice["source_path"]))
                ).read_bytes()
                if sha256(data).hexdigest() not in _installed_notice_hashes(
                    root, notice
                ):
                    raise ValueError(f"Installed Python notice drift: {name}")

    node_lock: dict[str, Any] = json.loads(
        (root / "editor/package-lock.json").read_text(encoding="utf-8")
    )
    node_manifest: dict[str, Any] = json.loads(
        (root / "editor/package.json").read_text(encoding="utf-8")
    )
    if node_lock["packages"][""]["dependencies"] != node_manifest["dependencies"]:
        raise ValueError("Node runtime requirement/lock drift")
    if manifest["node_overrides"] != node_manifest.get("overrides", {}):
        raise ValueError("Node override review required")
    runtime = {
        path: item
        for path, item in node_lock["packages"].items()
        if path and not item.get("dev", False)
    }
    node_records = {item["path"]: item for item in manifest["node"]}
    if len(node_records) != len(manifest["node"]) or set(runtime) != set(node_records):
        raise ValueError("Node runtime inventory drift")
    for path, package in runtime.items():
        item = node_records[path]
        if (
            item["version"] != package["version"]
            or item["spdx"] != package["license"]
            or item["source"]
            != {"url": package["resolved"], "integrity": package["integrity"]}
        ):
            raise ValueError(f"Node runtime lock review required: {path}")
        if installed_node:
            folder = root / "editor" / path
            metadata = json.loads((folder / "package.json").read_text(encoding="utf-8"))
            if any(metadata[key] != item[key] for key in ("name", "version")) or (
                metadata["license"] != item["spdx"]
            ):
                raise ValueError(f"Installed Node metadata drift: {path}")
            for notice in item["license_files"]:
                if (
                    sha256((folder / notice["source_path"]).read_bytes()).hexdigest()
                    != (notice["sha256"])
                ):
                    raise ValueError(f"Installed Node notice drift: {path}")

    for item in [*manifest["python"], *manifest["node"]]:
        if not item["license_files"]:
            raise ValueError(f"Missing reviewed notices: {item['name']}")
        for notice in item["license_files"]:
            path = root / notice["notice_path"]
            if not path.resolve().is_relative_to(root / "docs/release/licenses"):
                raise ValueError("Notice path escapes release licenses")
            if sha256(path.read_bytes()).hexdigest() != notice["sha256"]:
                raise ValueError(f"Preserved notice drift: {item['name']}")
            _installed_notice_hashes(root, notice)


if __name__ == "__main__":
    if sys.argv[1:] not in ([], ["--installed-node"]):
        raise SystemExit("Usage: check_runtime_licenses.py [--installed-node]")
    result = runtime_inventory("graphql-desk")
    expected = [
        {"name": "graphql-core", "version": "3.3.1", "license": "MIT"},
        {"name": "tomlkit", "version": "0.15.2", "license": "MIT"},
        {"name": "typing-extensions", "version": "4.16.0", "license": "PSF-2.0"},
    ]
    if sys.version_info < (3, 11):
        expected.insert(1, {"name": "tomli", "version": "2.5.0", "license": "MIT"})
    assert result == expected
    check_release_inventory(
        Path(__file__).resolve().parents[1],
        installed_node="--installed-node" in sys.argv,
    )
    print(json.dumps(result, indent=2))
