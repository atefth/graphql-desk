"""Build, inspect and install the VSIX using isolated, provisioned tooling."""

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import zipfile
from contextlib import ExitStack
from pathlib import Path, PurePosixPath
from typing import Any
from xml.etree import ElementTree

from scripts.check_distributions import (
    check_repository_path,
    checked_distributions,
    task_directory,
)
from scripts.check_installed_editor import activation_environment, check_activation
from scripts.check_runtime_licenses import check_release_inventory

VSCODE_VERSIONS = ("1.141.0", "1.142.0")
REPOSITORY = "https://github.com/atefth/graphql-desk"
EDITOR_INPUTS = (
    "editor/package.json",
    "editor/package-lock.json",
    "editor/tsconfig.json",
)
PYTHON_REVIEW_INPUTS = ("pyproject.toml", "uv.lock")
NOTICE_ROOT = "docs/release/licenses/"
INVENTORY = "docs/release/runtime-dependencies.json"
NOTICES = "docs/release/third-party-notices.md"
VSIX_METADATA = {"[Content_Types].xml": 65536, "extension.vsixmanifest": 65536}
NATIVE_SUFFIXES = {
    ".dll",
    ".dylib",
    ".exe",
    ".node",
    ".py",
    ".pyd",
    ".pyi",
    ".so",
    ".vsix",
    ".wasm",
}
PACK = """
const [tool, cwd, packagePath] = process.argv.slice(1);
require(tool).createVSIX({cwd, packagePath, useYarn: false, rewriteRelativeLinks: false,
  followSymlinks: false}).catch(error => {
    console.error(error);
    process.exitCode = 1;
});
"""


def _path_parts(name: str) -> tuple[str, ...]:
    parts = tuple(name.split("/"))
    if any(part in {"", ".", ".."} for part in parts) or any(
        character in name for character in ("\\", ":", "\0")
    ):
        raise ValueError(f"Unsafe VSIX path: {name!r}")
    return parts


def _write(root: Path, name: str, data: bytes) -> None:
    target = root.joinpath(*_path_parts(name))
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("xb") as stream:
        stream.write(data)


def _locked_packages(lock: Path) -> dict[str, Any]:
    packages: dict[str, Any] = json.loads(lock.read_bytes())["packages"]
    if any(
        not package.get("resolved", "").startswith("https://registry.npmjs.org/")
        or not package.get("integrity", "").startswith("sha512-")
        for name, package in packages.items()
        if name
    ):
        raise ValueError("VSIX requires integrity-locked public npm packages")
    return packages


def stage_inputs(root: Path, task: Path) -> tuple[set[str], dict[str, Any]]:
    """Stage maintained build inputs and check the exact inventory to be shipped."""
    editor = task / "editor"
    roots = (
        *EDITOR_INPUTS,
        *PYTHON_REVIEW_INPUTS,
        "editor/src",
        "LICENSE",
        "README.md",
        INVENTORY,
        NOTICES,
        NOTICE_ROOT,
    )
    entries = subprocess.check_output(
        ["git", "ls-files", "--stage", "-z", "--", *roots], cwd=root
    )
    files: dict[str, bytes] = {}
    for entry in entries.decode("utf-8").split("\0"):
        if not entry:
            continue
        metadata, name = entry.split("\t", 1)
        mode, blob, stage = metadata.split()
        if mode not in {"100644", "100755"} or stage != "0" or name in files:
            raise ValueError("VSIX requires unique, regular tracked inputs")
        _path_parts(name)
        path = root / name
        check_repository_path(root, path)
        if not path.is_file():
            raise ValueError(f"Missing VSIX input: {name}")
        if name in PYTHON_REVIEW_INPUTS:
            files[name] = path.read_bytes()
        elif name.startswith("editor/"):
            if name not in EDITOR_INPUTS and (
                not name.startswith("editor/src/")
                or path.suffix not in {".js", ".ts"}
                or name.endswith(".d.ts")
            ):
                raise ValueError(f"Unexpected VSIX build input: {name}")
            files[name] = path.read_bytes()
        else:
            # Git archive can apply the checkout's CRLF conversion to the MIT grant.
            files[name] = subprocess.check_output(
                ["git", "cat-file", "blob", blob], cwd=root
            )
    inventory: dict[str, Any] = json.loads(files[INVENTORY])
    notice_paths = {INVENTORY, NOTICES}
    for dependency in (*inventory["node"], *inventory["python"]):
        for notice in dependency["license_files"]:
            name = notice["notice_path"]
            if (
                not name.startswith(NOTICE_ROOT)
                or hashlib.sha256(files[name]).hexdigest() != notice["sha256"]
            ):
                raise ValueError("VSIX notice differs from its reviewed inventory")
            notice_paths.add(name)
    for name in notice_paths:
        _write(editor, name, files[name])
        _write(task, name, files[name])
    for name in PYTHON_REVIEW_INPUTS:
        _write(task, name, files[name])
    _write(editor, "LICENSE.txt", files["LICENSE"])
    _write(
        editor,
        "README.md",
        files["README.md"].replace(
            b"[MIT](LICENSE)", f"[MIT]({REPOSITORY}/blob/main/LICENSE)".encode()
        ),
    )

    manifest: dict[str, Any] = json.loads(files["editor/package.json"])
    if (
        manifest.get("engines") != {"vscode": "^1.141.0"}
        or manifest.get("main") != "./src/extension.js"
        or manifest.get("license") != "MIT"
        or manifest.get("private") is not True
        or manifest.get("browser")
        or manifest.get("extensionDependencies")
        or manifest.get("extensionPack")
        or "vscode:prepublish" in manifest.get("scripts", {})
    ):
        raise ValueError("VSIX engine, entry point or distribution policy changed")
    manifest["repository"] = {
        "type": "git",
        "url": f"{REPOSITORY}.git",
    }
    manifest["files"] = [
        "src/**/*.js",
        "LICENSE.txt",
        "docs/release/**",
        "node_modules/**",
    ]
    files["editor/package.json"] = (json.dumps(manifest, indent=2) + "\n").encode()
    compiled: set[str] = set()
    for name, data in files.items():
        if not name.startswith("editor/"):
            continue
        relative = name.removeprefix("editor/")
        _write(editor, relative, data)
        if relative.startswith("src/"):
            output = PurePosixPath(relative).with_suffix(".js").as_posix()
            if output in compiled:
                raise ValueError(f"Multiple inputs would produce {output}")
            compiled.add(output)
    if "src/extension.js" not in compiled:
        raise ValueError("Missing VSIX entry point")
    check_release_inventory(task)
    return compiled, inventory


def _payload(
    editor: Path, compiled: set[str], inventory: dict[str, Any]
) -> dict[str, bytes]:
    files = {
        name: (editor / name).read_bytes()
        for name in compiled | {"LICENSE.txt", "package.json", INVENTORY, NOTICES}
    }
    runtime = {
        name: package
        for name, package in _locked_packages(editor / "package-lock.json").items()
        if name and not package.get("dev", False)
    }
    if set(runtime) != {item["path"] for item in inventory["node"]}:
        raise ValueError("VSIX runtime closure differs from the reviewed inventory")
    for dependency in (*inventory["node"], *inventory["python"]):
        for notice in dependency["license_files"]:
            name = notice["notice_path"]
            files[name] = (editor / name).read_bytes()
    for record in inventory["node"]:
        path = record["path"]
        directory = editor.joinpath(*_path_parts(path))
        check_repository_path(editor, directory)
        for child in directory.rglob("*"):
            check_repository_path(editor, child)
            if child.is_dir():
                continue
            if not child.is_file() or child.suffix.lower() in NATIVE_SUFFIXES:
                raise ValueError(f"Unexpected npm runtime payload: {child.name}")
            name = child.relative_to(editor).as_posix()
            _path_parts(name)
            files[name] = child.read_bytes()
        package = json.loads(files[f"{path}/package.json"])
        if (
            package["name"] != record["name"]
            or package["version"] != runtime[path]["version"]
            or package["license"] != record["spdx"]
        ):
            raise ValueError(f"Unreviewed npm runtime package: {path}")
        for notice in record["license_files"]:
            data = files[f"{path}/{notice['source_path']}"]
            if hashlib.sha256(data).hexdigest() != notice["sha256"]:
                raise ValueError(f"Unreviewed npm runtime notice: {path}")
    return files


def check_archive(archive_path: Path, payload: dict[str, bytes], readme: bytes) -> None:
    """Check the packer's actual bytes and exact paths before installing."""
    expected = {f"extension/{name}": data for name, data in payload.items()}
    expected["extension/readme.md"] = readme
    seen: set[str] = set()
    metadata: dict[str, bytes] = {}
    with zipfile.ZipFile(archive_path) as archive:
        for item in archive.infolist():
            name = item.filename
            _path_parts(name)
            if (
                name != item.orig_filename
                or name.casefold() in seen
                or stat.S_IFMT(item.external_attr >> 16) not in {0, stat.S_IFREG}
            ):
                raise ValueError(f"Duplicate, linked or invalid VSIX member: {name}")
            seen.add(name.casefold())
            if name in expected:
                if item.file_size != len(expected[name]):
                    raise ValueError(f"VSIX member size differs: {name}")
            elif (
                name not in VSIX_METADATA
                or not 0 < item.file_size <= VSIX_METADATA[name]
            ):
                raise ValueError(f"Unexpected or oversized VSIX member: {name}")
            with archive.open(item) as stream:
                data = stream.read(item.file_size + 1)
            if len(data) != item.file_size:
                raise ValueError(f"VSIX member declared size differs: {name}")
            if name in expected:
                if expected.pop(name) != data:
                    raise ValueError(f"VSIX member bytes differ: {name}")
            else:
                metadata[name] = data
    missing = expected.keys() | (VSIX_METADATA.keys() - metadata.keys())
    if missing:
        raise ValueError(f"VSIX members are missing: {sorted(missing)}")
    if any(b"<!DOCTYPE" in metadata[name] for name in VSIX_METADATA):
        raise ValueError("Unexpected VSIX metadata document type")
    manifest = json.loads(payload["package.json"])
    xml = ElementTree.fromstring(metadata["extension.vsixmanifest"])
    identity = xml.find(".//{*}Identity")
    engine = xml.find(".//{*}Property[@Id='Microsoft.VisualStudio.Code.Engine']")
    if (
        identity is None
        or identity.get("Id") != manifest["name"]
        or identity.get("Publisher") != manifest["publisher"]
        or identity.get("Version") != manifest["version"]
        or engine is None
        or engine.get("Value") != manifest["engines"]["vscode"]
        or ElementTree.fromstring(metadata["[Content_Types].xml"]).tag.rsplit("}", 1)[
            -1
        ]
        != "Types"
    ):
        raise ValueError("VSIX identity or content-type metadata differs")


def _run(*command: str, cwd: Path, env: dict[str, str]) -> str:
    result = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
        check=False,
    )
    if result.returncode:
        raise ValueError(
            f"{Path(command[0]).name} failed: {(result.stdout + result.stderr)[-4096:]}"
        )
    return result.stdout


def _install(code: Path, archive: Path, task: Path, env: dict[str, str]) -> str:
    executable = code.resolve(strict=True)
    if not executable.is_file():
        raise ValueError("Select a provisioned VS Code executable")
    command = [str(executable)]
    if os.name == "nt":
        app = executable.parent
        entries = [app / "resources/app/out/cli.js"]
        entries.extend(
            child / "resources/app/out/cli.js"
            for child in app.iterdir()
            if re.fullmatch(r"[0-9a-f]{10}", child.name)
        )
        entries = [entry for entry in entries if entry.is_file()]
        if executable.name.lower() != "code.exe" or len(entries) != 1:
            raise ValueError("Select the Code.exe in a provisioned VS Code archive")
        command.append(str(entries[0]))
        env = {**env, "ELECTRON_RUN_AS_NODE": "1"}
    profile = task / "profile"
    extensions = task / "extensions"
    extensions.mkdir(parents=True)
    _write(
        profile,
        "User/settings.json",
        json.dumps(
            {
                "extensions.autoUpdate": "off",
                "extensions.autoCheckUpdates": False,
                "telemetry.telemetryLevel": "off",
                "update.mode": "none",
            }
        ).encode(),
    )
    command.extend(
        (
            f"--user-data-dir={profile}",
            f"--extensions-dir={extensions}",
            f"--shared-data-dir={task / 'shared'}",
        )
    )
    versions = _run(*command, "--version", cwd=task, env=env).splitlines()
    if not versions or versions[0] not in VSCODE_VERSIONS:
        raise ValueError(f"Select a pinned VS Code version from {VSCODE_VERSIONS}")
    _run(*command, "--install-extension", str(archive), cwd=task, env=env)
    output = _run(*command, "--list-extensions", "--show-versions", cwd=task, env=env)
    with zipfile.ZipFile(archive) as package:
        manifest = json.loads(package.read("extension/package.json"))
        identifier = f"{manifest['publisher']}.{manifest['name']}"
        if output.split() != [f"{identifier}@{manifest['version']}"]:
            raise ValueError("Unexpected extensions in the isolated VS Code profile")
        installed = tuple(extensions.glob(f"{identifier}-*/package.json"))
        if len(installed) != 1:
            raise ValueError(
                "VSIX did not install into the isolated extensions directory"
            )
        check_repository_path(task, installed[0])
        installed_manifest = json.loads(installed[0].read_bytes())
        installed_manifest.pop("__metadata", None)
        if installed_manifest != manifest:
            raise ValueError("Installed VSIX manifest differs")
        for item in package.infolist():
            if (
                item.filename.startswith("extension/")
                and item.filename != "extension/package.json"
            ):
                target = installed[0].parent / item.filename.removeprefix("extension/")
                check_repository_path(task, target)
                if not target.is_file() or target.read_bytes() != package.read(item):
                    raise ValueError(f"Installed VSIX member differs: {item.filename}")
    if _run(*command, "--version", cwd=task, env=env).splitlines() != versions:
        raise ValueError("VS Code changed during install verification")
    return versions[0]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--code",
        type=Path,
        action="append",
        help="Provisioned VS Code CLI, or desktop executable with --activate",
    )
    provisioning = parser.add_mutually_exclusive_group()
    provisioning.add_argument(
        "--provision",
        action="store_true",
        help="Prime and audit the locked npm cache online before the offline proof",
    )
    provisioning.add_argument(
        "--provision-only",
        action="store_true",
        help="Prime and audit the locked npm cache without building packages",
    )
    parser.add_argument(
        "--activate",
        action="store_true",
        help="Run trusted and restricted editor hosts with the checked wheel",
    )
    args = parser.parse_args()
    if args.provision_only:
        if args.activate or args.code:
            parser.error("--provision-only does not take an editor or --activate")
    elif not args.code:
        parser.error("--code is required to verify a VSIX")
    root = Path(__file__).resolve().parents[1]
    cache = Path(os.environ.get("NPM_CONFIG_CACHE", ""))
    check_repository_path(root, cache)
    if not cache.is_dir():
        raise ValueError("Use an existing repository-owned NPM_CONFIG_CACHE")
    node = shutil.which("node")
    npm = shutil.which("npm.cmd" if os.name == "nt" else "npm")
    if node is None or npm is None:
        raise ValueError("Provision Node and npm before checking the VSIX")
    with ExitStack() as tasks:
        python = (
            tasks.enter_context(checked_distributions(root)) if args.activate else None
        )
        # Unix sockets inherit the activation profile's absolute path.
        task = tasks.enter_context(
            task_directory(root, prefix="a-") if args.activate else task_directory(root)
        )
        editor = task / "editor"
        tools = task / "packaging"
        for name in ("package.json", "package-lock.json"):
            path = root / "editor/packaging" / name
            check_repository_path(root, path)
            _write(tools, name, path.read_bytes())
        compiled, inventory = stage_inputs(root, task)
        for directory in (editor, tools):
            _locked_packages(directory / "package-lock.json")
        env = {
            name: value
            for name, value in os.environ.items()
            if not name.lower().startswith("npm_config_")
        }
        for name in (
            "NODE_OPTIONS",
            "NODE_PATH",
            "NODE_AUTH_TOKEN",
            "NPM_TOKEN",
            "VSCODE_PORTABLE",
            "VSCODE_DEV",
            "VSCODE_IPC_HOOK_CLI",
            "ELECTRON_RUN_AS_NODE",
        ):
            env.pop(name, None)
        for name in ("tmp", "appdata", "localappdata", "cache"):
            (task / name).mkdir()
        env.update(
            TEMP=str(task / "tmp"),
            TMP=str(task / "tmp"),
            TMPDIR=str(task / "tmp"),
            APPDATA=str(task / "appdata"),
            LOCALAPPDATA=str(task / "localappdata"),
            XDG_CACHE_HOME=str(task / "cache"),
            NODE_DISABLE_COMPILE_CACHE="1",
            NPM_CONFIG_CACHE=str(cache),
            NPM_CONFIG_USERCONFIG=str(task / "npmrc"),
            NPM_CONFIG_GLOBALCONFIG=str(task / "global-npmrc"),
            NPM_CONFIG_PREFIX=str(task / "npm-prefix"),
            NPM_CONFIG_UPDATE_NOTIFIER="false",
            NPM_CONFIG_REGISTRY="https://registry.npmjs.org/",
            SOURCE_DATE_EPOCH=subprocess.check_output(
                ["git", "log", "-1", "--format=%ct"], cwd=root, text=True
            ).strip(),
            TZ="UTC",
        )
        version = _run(node, "--version", cwd=task, env=env).strip()
        if not version.startswith("v22."):
            raise ValueError(f"Use the pinned Node 22 toolchain, got {version}")
        print(f"Build Node {version}", flush=True)
        install = (npm, "ci", "--ignore-scripts", "--no-fund", "--no-audit")
        if args.provision or args.provision_only:
            for directory in (editor, tools):
                _run(*install, cwd=directory, env=env)
                print(_run(npm, "audit", cwd=directory, env=env).strip(), flush=True)
        if args.provision_only:
            return
        # Dead proxies catch incidental requests. The Ubuntu CI step enforces isolation.
        proxy = "http://127.0.0.1:9"
        offline = {
            **env,
            "NPM_CONFIG_OFFLINE": "true",
            "HTTP_PROXY": proxy,
            "HTTPS_PROXY": proxy,
            "ALL_PROXY": proxy,
            "NO_PROXY": "",
        }
        for directory in (editor, tools):
            _run(*install, "--offline", cwd=directory, env=offline)
        _run(
            node,
            str(editor / "node_modules/typescript/bin/tsc"),
            "--project",
            "tsconfig.json",
            cwd=editor,
            env=offline,
        )
        payload = _payload(editor, compiled, inventory)
        archive = task / "graphql-desk.vsix"
        _run(
            node,
            "-e",
            PACK,
            str(tools / "node_modules/@vscode/vsce"),
            str(editor),
            str(archive),
            cwd=editor,
            env=offline,
        )
        check_archive(archive, payload, (editor / "README.md").read_bytes())
        print(
            f"Verified {len(payload)} exact files and "
            f"{len(inventory['node'])} runtime packages",
            flush=True,
        )
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        for index, code in enumerate(args.code):
            if python is None:
                version = _install(code, archive, task / f"install-{index}", offline)
                print(
                    f"VS Code {version} verified fresh local VSIX install", flush=True
                )
                continue
            desktop = code.resolve(strict=True)
            cli = desktop
            if os.name != "nt":
                electron = root / "editor/node_modules/@vscode/test-electron"
                check_repository_path(root, electron)
                cli = Path(
                    _run(
                        node,
                        "-e",
                        "const [tool, desktop] = process.argv.slice(1);"
                        "process.stdout.write(require(tool)"
                        ".resolveCliPathFromVSCodeExecutablePath(desktop));",
                        str(electron),
                        str(desktop),
                        cwd=task,
                        env=offline,
                    ).strip()
                )
            for trusted in (True, False):
                mode = "trusted" if trusted else "restricted"
                case = task / f"{index}{'t' if trusted else 'r'}"
                case_env = activation_environment(case, offline)
                version = _install(cli, archive, case, case_env)
                print(f"VS Code {version} checking {mode} activation", flush=True)
                check_activation(
                    root,
                    desktop,
                    python,
                    case,
                    case_env,
                    version,
                    trusted=trusted,
                )
        if hashlib.sha256(archive.read_bytes()).hexdigest() != digest:
            raise ValueError("VSIX changed during install verification")
        print(f"VSIX SHA-256 {digest}", flush=True)


if __name__ == "__main__":
    main()
