"""Run the worked example in a disposable repository-local directory.

Requires the existing locked development environment; installs and fetches nothing.
"""

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def main() -> None:
    node = shutil.which("node")
    spec = importlib.util.find_spec("pyright")
    if node is None or spec is None or spec.origin is None:
        raise RuntimeError("Provision Node.js and the locked Pyright package first")
    pyright_script = Path(spec.origin).parent / "dist" / "index.js"
    if not pyright_script.is_file():
        raise RuntimeError("The installed Pyright package has no bundled checker")
    python_command = (sys.executable, "-B")
    pyright_command = (node, str(pyright_script))
    example = Path(__file__).resolve().parent
    repository = example.parents[1]
    temporary = repository / ".tmp" / "worked-example"
    if not temporary.resolve().is_relative_to(repository):
        raise ValueError("Example temp directory escapes the repository")
    temporary.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="run-", dir=temporary) as directory:
        root = Path(directory)
        for relative in (
            "pyproject.toml",
            "schema.graphql",
            "authoring/operations.py",
            "app.py",
        ):
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(example / relative, target)
        # Reuse the maintained, tested adapter instead of adding another client.
        shutil.copyfile(
            repository / "scripts/client_bindings.py", root / "client_bindings.py"
        )
        cache = root / "cache"
        cache.mkdir()
        environment = {
            **os.environ,
            "TEMP": str(root),
            "TMP": str(root),
            "TMPDIR": str(root),
            "MYPY_CACHE_DIR": str(cache / "mypy"),
            "PYRIGHT_PYTHON_CACHE_DIR": str(cache / "pyright"),
            "UV_CACHE_DIR": str(cache / "uv"),
            "PIP_CACHE_DIR": str(cache / "pip"),
            "RUFF_CACHE_DIR": str(cache / "ruff"),
            "NODE_COMPILE_CACHE": str(cache / "node-compile"),
            "npm_config_cache": str(cache / "npm"),
            "PYTHONDONTWRITEBYTECODE": "1",
        }

        def run(*arguments: str, code: int = 0) -> subprocess.CompletedProcess[str]:
            result = subprocess.run(
                list(arguments),
                cwd=root,
                env=environment,
                capture_output=True,
                text=True,
                encoding="utf8",
            )
            if result.returncode != code:
                raise RuntimeError(
                    " ".join(arguments) + "\n" + result.stdout + result.stderr
                )
            return result

        run(
            *python_command,
            "-m",
            "graphql_desk",
            "generate-output",
            "--output",
            "demo_generated/default.pyi",
        )
        run(*python_command, "-m", "graphql_desk", "generate-persisted")
        run(*python_command, "-m", "authoring.operations")
        run(*python_command, "-m", "app")
        doctor = run(*python_command, "-m", "graphql_desk", "doctor", code=1)
        assert "status: degraded" in doctor.stderr

        (root / "pyrightconfig.json").write_text(
            json.dumps(
                {
                    "include": ["authoring/operations.py", "app.py"],
                    "typeCheckingMode": "strict",
                    "pythonVersion": "3.10",
                }
            ),
            encoding="utf8",
        )
        run(
            *python_command,
            "-m",
            "mypy",
            "--strict",
            "--no-incremental",
            "authoring/operations.py",
            "app.py",
        )
        run(*pyright_command, "--pythonpath", sys.executable, "--outputjson")
        negative = (
            "from graphql import GraphQLSchema\n"
            "from client_bindings import execute_core_sync\n"
            "from authoring.operations import book_query\n"
            "from demo_generated.sources.authoring.operations import "
            "BookQueryVariables, BookQueryResult\n"
            "def wrong(schema:GraphQLSchema,value:BookQueryResult)->None:\n"
            " execute_core_sync(schema,book_query,BookQueryVariables()) # invalid\n"
            " execute_core_sync(schema,book_query,BookQueryVariables(id=1)) # invalid\n"
            " print(value['book']['title']) # invalid: still masked\n"
        )
        (root / "negative.py").write_text(negative, encoding="utf8")
        expected = {
            number
            for number, line in enumerate(negative.splitlines(), 1)
            if "# invalid" in line
        }
        mypy = run(
            *python_command,
            "-m",
            "mypy",
            "--strict",
            "--no-incremental",
            "negative.py",
            code=1,
        )
        locations = re.findall(r"([^\r\n]+\.pyi?):(\d+): error:", mypy.stdout)
        assert all(Path(path).name == "negative.py" for path, _ in locations), (
            mypy.stdout
        )
        assert {
            int(number)
            for number in re.findall(r"negative.py:(\d+): error:", mypy.stdout)
        } == expected, mypy.stdout
        pyright = run(
            *pyright_command,
            "--pythonpath",
            sys.executable,
            "--outputjson",
            "negative.py",
            code=1,
        )
        diagnostics = json.loads(pyright.stdout)["generalDiagnostics"]
        assert all(Path(item["file"]).name == "negative.py" for item in diagnostics), (
            pyright.stdout
        )
        assert {
            item["range"]["start"]["line"] + 1 for item in diagnostics
        } == expected, pyright.stdout

        run(
            *python_command,
            "-m",
            "graphql_desk",
            "generate-persisted",
            "--omit-runtime-source",
        )
        run(
            *python_command,
            "-m",
            "build",
            "--wheel",
            "--no-isolation",
            "--outdir",
            "dist",
        )
        (wheel,) = (root / "dist").glob("*.whl")
        run(*python_command, "-m", "graphql_desk", "check", "--artifact", str(wheel))
        run(
            *python_command,
            "-I",
            "-c",
            "import sys; sys.path.insert(0," + repr(str(wheel)) + "); "
            "import example_app; "
            "assert example_app.request({'id':'1'})['id']=='book-by-id-v1'",
        )
        print(
            "Example passed: generated typing, expected errors, clients, fragments, "
            "persisted wheel."
        )


if __name__ == "__main__":
    main()
