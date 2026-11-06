"""Run pinned Turms checks; provision the isolated, hashed environment explicitly."""

import argparse
import ast
import hashlib
import importlib.metadata as metadata
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def checked_task_root(repository: Path) -> Path:
    root = repository.resolve(strict=True)
    temporary = root / ".tmp"
    task = temporary / "issue85-eval"
    for directory in (temporary, task):
        if directory.is_symlink() or directory.is_junction():
            raise ValueError("Evaluation task directories must not be redirected")
    if not task.resolve().is_relative_to(root):
        raise ValueError("Evaluation task directory escapes the real repository")
    return task


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="results")
    arguments = parser.parse_args()
    assert sys.version_info[:2] == (3, 13), "Only Python 3.13 was evaluated"
    example = Path(__file__).resolve().parent
    task = checked_task_root(example.parents[1])
    assert Path(sys.prefix).resolve().is_relative_to(task.resolve()), (
        "Use the isolated task environment"
    )
    assert sys.prefix != sys.base_prefix, "A dedicated virtual environment is required"
    assert metadata.version("turms") == "2.3.0"
    assert metadata.version("graphql-core") == "3.3.1"
    try:
        metadata.distribution("graphql-desk")
    except metadata.PackageNotFoundError:
        pass
    else:
        raise AssertionError(
            "Core GraphQL Desk must not be installed in the evaluation environment"
        )
    output = task / arguments.output
    assert (
        output.resolve().is_relative_to(task.resolve())
        and output.resolve() != task.resolve()
    )
    output.mkdir(parents=True, exist_ok=False)
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith(("TURMS_", "PYTHON"))
    }
    environment.update(TEMP=str(task), TMP=str(task), NO_COLOR="1", TERM="dumb")
    sys.dont_write_bytecode = True

    def run(args: list[str], cwd: Path, log: str) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            [sys.executable, "-I", "-B", "-X", "utf8", *args],
            cwd=cwd,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=60,
        )
        (output / log).write_text(result.stdout + result.stderr, encoding="utf-8")
        return result

    lock = (example / "requirements.lock").read_text(encoding="utf-8")
    versions: dict[str, str] = {}
    licenses: dict[str, str | list[str]] = {}
    for name, version in re.findall(r"^([\w.-]+)==([^\s\\]+)", lock, re.MULTILINE):
        distribution = metadata.distribution(name)
        assert distribution.version == version, (name, distribution.version, version)
        versions[name] = version
        info = distribution.metadata
        licenses[name] = (
            info.get("License-Expression")
            or info.get("License")
            or [
                item
                for item in info.get_all("Classifier", [])
                if item.startswith("License ::")
            ]
        )
    with tempfile.TemporaryDirectory(prefix="fixture-", dir=task) as temporary:
        fixture = Path(temporary)
        for name in ("schema.graphql", "operations.graphql", "graphql.config.yaml"):
            shutil.copyfile(example / name, fixture / name)
        command = [
            "-m",
            "turms.cli.main",
            "gen",
            "--config",
            "graphql.config.yaml",
            "evaluation",
        ]
        first = run(command, fixture, "generate-first.log")
        assert first.returncode == 0, "First generation failed; see the log"
        generated = fixture / "generated" / "schema.py"
        first_bytes = generated.read_bytes()
        second = run(command, fixture, "generate-second.log")
        assert second.returncode == 0 and generated.read_bytes() == first_bytes
        text = generated.read_text(encoding="utf-8")
        assert text == (example / "expected_schema.txt").read_text(encoding="utf-8")
        imports = sorted(
            {
                node.module
                for node in ast.walk(ast.parse(text))
                if isinstance(node, ast.ImportFrom) and node.module
            }
        )
        assert imports == ["datetime", "enum", "pydantic", "typing"], imports
        assert not any(
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            for node in ast.walk(ast.parse(text))
        )
        (output / "generated_schema.py").write_text(
            text, encoding="utf-8", newline="\n"
        )
        checked = run(
            [str(example / "check_models.py"), str(generated)],
            fixture,
            "model-checks.log",
        )
        assert checked.returncode == 0, "Model checks failed; see the log"
        checks = json.loads(checked.stdout)
        invalid = fixture / "invalid"
        invalid.mkdir()
        for name in ("schema.graphql", "graphql.config.yaml"):
            shutil.copyfile(example / name, invalid / name)
        (invalid / "operations.graphql").write_text(
            'query Invalid { user(id: "u1") { missingField } }', encoding="utf-8"
        )
        bad = run(command, invalid, "invalid-graphql.log")
        assert (
            bad.returncode != 0 and not (invalid / "generated" / "schema.py").exists()
        )
    report = {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "backend": "turms",
        "locked_backend_wheel_sha256": (
            "d2269c099522e12612bf825c8dfc63bce603349874bed3506a630a0fcd513bd9"
        ),
        "environment_check": (
            "installed versions match the lock; "
            "provision separately with --require-hashes"
        ),
        "versions": versions,
        "licenses": licenses,
        "lock_sha256": hashlib.sha256(
            (example / "requirements.lock").read_bytes()
        ).hexdigest(),
        "generated_normalized_sha256": hashlib.sha256(text.encode()).hexdigest(),
        "generated_imports": imports,
        "checks": {
            **checks,
            "deterministic_generated_bytes": True,
            "matches_checked_in_output": True,
            "invalid_graphql_rejected_without_output": True,
            "core_distribution_absent": True,
        },
    }
    (output / "evidence.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {"evidence": str(output / "evidence.json"), "checks": report["checks"]},
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
