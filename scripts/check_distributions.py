"""Build the sdist again, then smoke-test both wheels outside the source tree."""

import os
import subprocess
import sys
import tempfile
from pathlib import Path


def run(*arguments: str, cwd: Path | None = None) -> None:
    subprocess.run(arguments, cwd=cwd, check=True)


if __name__ == "__main__":
    root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory() as temporary:
        work = Path(temporary)
        rebuilt = work / "rebuilt"
        source_archives = list(root.glob("dist/*.tar.gz"))
        assert len(source_archives) == 1, source_archives
        source_archive = source_archives[0]
        run(
            "uv",
            "build",
            "--wheel",
            "--no-build-isolation",
            str(source_archive),
            "--out-dir",
            str(rebuilt),
        )
        direct_wheels = list(root.glob("dist/*.whl"))
        rebuilt_wheels = list(rebuilt.glob("*.whl"))
        assert len(direct_wheels) == len(rebuilt_wheels) == 1
        wheels = direct_wheels + rebuilt_wheels
        for index, wheel in enumerate(wheels):
            environment = work / f"install-{index}"
            run("uv", "venv", "--python", sys.executable, str(environment))
            python = environment / (
                "Scripts/python.exe" if os.name == "nt" else "bin/python"
            )
            run(
                "uv",
                "pip",
                "install",
                "--python",
                str(python),
                "--require-hashes",
                "-r",
                str(root / "runtime-requirements.txt"),
            )
            run(
                "uv", "pip", "install", "--python", str(python), "--no-deps", str(wheel)
            )
            run(
                str(python),
                "-I",
                "-c",
                "import graphql_desk; import importlib.metadata as m; "
                "assert m.version('graphql-desk'); "
                "assert any(str(f).endswith('/py.typed') "
                "for f in m.files('graphql-desk') or []); "
                "print(graphql_desk.__file__)",
                cwd=work,
            )
