"""Run the installed extension with one isolated editor profile and checked wheel."""

import json
import os
import re
import signal
import subprocess
import sys
from pathlib import Path

from scripts.check_distributions import check_repository_path

SOURCE = (
    "from graphql_desk import gql\n"
    'bad = gql("query PackageDiagnostic { desk { noSuchDeskField } }")\n'
    'partial = gql("query PackageCompletion { desk { deskLa } }")\n'
)
SCHEMA = "type Desk { deskLabel: String! }\ntype Query { desk: Desk! }\n"
CONFIG = (
    '[tool.graphql-desk]\nschema="schema.graphql"\n'
    'schema_output="graphql_desk_generated/default.pyi"\n'
)


def _check_ipc_paths(profile: Path, temporary: Path) -> None:
    if sys.platform not in ("darwin", "linux"):
        return
    # Pinned VS Code 1.141/1.142 shorten only the random part of their socket.
    limit = 103 if sys.platform == "darwin" else 107
    static = [profile / "1.14-xxxxxx.sock"]
    if sys.platform == "linux":
        static.append(temporary / "vscode-00000000-1.14-xxxxxx.sock")
    random = temporary / "vscode-ipc-.sock"
    base_units = len(str(random).encode("utf-16-le")) // 2
    suffix_length = min(36, limit - 1 - base_units)
    if (
        suffix_length < 8
        or len(os.fsencode(random)) + suffix_length >= limit
        or any(len(os.fsencode(path)) >= limit for path in static)
    ):
        raise ValueError("VS Code IPC path is too long; use a shorter checkout path")


def activation_environment(task: Path, env: dict[str, str]) -> dict[str, str]:
    temporary = task / "tmp"
    _check_ipc_paths(task / "profile", temporary)
    temporary.mkdir(parents=True, mode=0o700)
    for name in ("home", "appdata", "localappdata", "cache", "config", "data"):
        (task / name).mkdir()
    result = {
        **{
            key: value
            for key, value in env.items()
            if not key.upper().startswith(("VSCODE_", "ELECTRON_"))
        },
        "HOME": str(task / "home"),
        "USERPROFILE": str(task / "home"),
        "APPDATA": str(task / "appdata"),
        "LOCALAPPDATA": str(task / "localappdata"),
        "XDG_CACHE_HOME": str(task / "cache"),
        "XDG_CONFIG_HOME": str(task / "config"),
        "XDG_DATA_HOME": str(task / "data"),
        "TEMP": str(temporary),
        "TMP": str(temporary),
        "TMPDIR": str(temporary),
    }
    if sys.platform == "linux":
        result["XDG_RUNTIME_DIR"] = str(temporary)
    if sys.platform == "win32":
        result["HOMEDRIVE"] = task.drive
        result["HOMEPATH"] = str(task / "home")[len(task.drive) :]
    return result


def _workspace_contents(root: Path) -> dict[Path, bytes | None]:
    entries: dict[Path, bytes | None] = {}
    for directory, folders, files in os.walk(root, followlinks=False):
        for name in folders + files:
            path = Path(directory) / name
            check_repository_path(root, path)
            entries[path.relative_to(root)] = (
                None if name in folders else path.read_bytes()
            )
    return entries


def _launch(
    desktop: Path, flags: list[str], task: Path, env: dict[str, str]
) -> tuple[int, str]:
    creation_flags = 0
    if sys.platform == "win32":
        creation_flags = subprocess.CREATE_NO_WINDOW
    with (task / "host.log").open("xb") as log:
        process = subprocess.Popen(
            (str(desktop), *flags),
            cwd=task,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=sys.platform != "win32",
            creationflags=creation_flags,
        )
        try:
            status = process.wait(timeout=90)
        except BaseException:
            if process.poll() is None:
                if sys.platform == "win32":
                    taskkill = Path(os.environ["SystemRoot"]) / "System32/taskkill.exe"
                    subprocess.run(
                        (str(taskkill), "/T", "/F", "/PID", str(process.pid)),
                        check=False,
                        timeout=10,
                        creationflags=creation_flags,
                    )
                else:
                    os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=15)
            raise
    return status, (task / "host.log").read_text(encoding="utf-8", errors="replace")


def check_activation(
    repository: Path,
    desktop: Path,
    python: Path,
    task: Path,
    env: dict[str, str],
    version: str,
    *,
    trusted: bool,
) -> None:
    workspace = task / "workspace"
    (workspace / ".vscode").mkdir(parents=True)
    (workspace / "main.py").write_bytes(SOURCE.encode())
    (workspace / "schema.graphql").write_bytes(SCHEMA.encode())
    (workspace / "pyproject.toml").write_bytes(CONFIG.encode())
    (workspace / ".vscode/settings.json").write_bytes(
        json.dumps(
            {"graphqlDesk.pythonPath": str(python), "files.autoSave": "off"}
        ).encode()
    )
    before = _workspace_contents(workspace)
    profile = task / "profile"
    settings_path = profile / "User/settings.json"
    settings = json.loads(settings_path.read_bytes())
    settings.update(
        {
            "git.enabled": False,
            "security.workspace.trust.enabled": True,
            "security.workspace.trust.startupPrompt": "never",
            "security.workspace.trust.banner": "never",
            "window.restoreWindows": "none",
            "workbench.startupEditor": "none",
        }
    )
    settings_path.write_bytes(json.dumps(settings).encode())

    extensions = task / "extensions"
    (product_manifest,) = extensions.glob("*/package.json")
    check_repository_path(task, product_manifest)
    product = json.loads(product_manifest.read_bytes())
    probe = task / "probe"
    probe.mkdir()
    (probe / "package.json").write_bytes(
        json.dumps(
            {
                "name": "installed-artifacts-observer",
                "publisher": "graphql-desk-tests",
                "version": "0.0.0",
                "engines": {"vscode": "^1.141.0"},
            }
        ).encode()
    )
    check = probe / "installedArtifacts.host.cjs"
    source_path = repository / "editor/test" / check.name
    check_repository_path(repository, source_path)
    check.write_bytes(source_path.read_bytes())
    (probe / "expectations.json").write_bytes(
        json.dumps(
            {
                "task": str(task),
                "workspace": str(workspace),
                "python": str(python),
                "productId": f"{product['publisher']}.{product['name']}",
                "productVersion": product["version"],
                "product": str(product_manifest.parent),
                "tmp": env["TEMP"],
                "trusted": trusted,
                "vscode": version,
            }
        ).encode()
    )
    # test-electron disables workspace trust. Preserve it in the restricted case.
    flags = [
        str(workspace),
        f"--user-data-dir={profile}",
        f"--extensions-dir={extensions}",
        f"--shared-data-dir={task / 'shared'}",
        f"--extensions-download-dir={task / 'extension-downloads'}",
        f"--agent-plugins-dir={task / 'agent-plugins'}",
        f"--agents-user-data-dir={task / 'agent-data'}",
        f"--agents-extensions-dir={task / 'agent-extensions'}",
        f"--crash-reporter-directory={task / 'crash'}",
        f"--extensionDevelopmentPath={probe}",
        f"--extensionTestsPath={check}",
        "--disable-updates",
        "--disable-telemetry",
        "--disable-gpu",
        "--no-sandbox",
        "--disable-gpu-sandbox",
        "--no-cached-data",
        "--skip-welcome",
        "--skip-release-notes",
        "--disable-extension=github.copilot-chat",
        "--disable-extension=github.copilot",
        "--disable-extension=vscode.github-authentication",
    ]
    if trusted:
        flags.append("--disable-workspace-trust")
    status, log = _launch(desktop, flags, task, env)
    evidence_file = task / "host-evidence.json"
    evidence: dict[str, object] = (
        json.loads(evidence_file.read_bytes()) if evidence_file.is_file() else {}
    )
    if status or evidence.get("passed") is not True:
        raise ValueError(
            f"Installed VS Code {version} activation failed:\n{log[-4096:]}"
        )
    if not re.search(
        r"Creating shared storage database at ':memory:'", log
    ) or re.search(r"Creating shared storage database at (?!':memory:')", log):
        raise ValueError("Editor host opened persistent shared storage")
    after = _workspace_contents(workspace)
    if any(after.get(path) != value for path, value in before.items()):
        raise ValueError("Editor host changed a fixture source file or setting")
    if not trusted and after != before:
        raise ValueError("Restricted editor host created workspace files")
    print(json.dumps(evidence), flush=True)
