import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from graphql import build_schema

from graphql_desk.config import ScalarMapping
from graphql_desk.factory_generation import generate_factory_artifacts
from graphql_desk.fragments import SchemaContext, index_fragments
from graphql_desk.source_graph import (
    FactoryBinding,
    ModuleSnapshot,
    ProjectBindings,
    resolve_project,
)


class NewTypeMappingTests(unittest.TestCase):
    def test_newtype_scalar_mapping_flows_through_generated_factory(self) -> None:
        schema = build_schema("scalar UserId type Query { user(id: UserId!): UserId! }")
        context = SchemaContext("main", schema, "newtype-mapping")
        source = (
            "from typing import TYPE_CHECKING\n"
            "from typing_extensions import assert_type\n"
            "from generated.main import graphql\n"
            "from generated.sources.main import QueryResult, QueryVariables\n"
            "from local_types import UserId\n"
            "query = graphql('query Lookup($id: UserId!) { user(id: $id) }')\n"
            "value: UserId = UserId(7)\n"
            "assert graphql.scalar('UserId', value) is value\n"
            "if TYPE_CHECKING:\n"
            "    assert_type(graphql.scalar('UserId', value), UserId)\n"
            "    variables: QueryVariables = {'id': value}\n"
            "    result: QueryResult = {'user': value}\n"
            "    assert_type(result['user'], UserId)\n"
            "    wrong_input: QueryVariables = {'id': 7}\n"
            "    wrong_result: QueryResult = {'user': 7}\n"
            "    graphql.scalar('UserId', 7)\n"
        )
        modules = (
            ModuleSnapshot("p", "main", "file:///main.py", 1, source),
            ModuleSnapshot(
                "p",
                "local_types",
                "file:///local_types.py",
                1,
                "from typing import NewType\nUserId = NewType('UserId', int)\n",
            ),
        )
        sources = resolve_project(
            modules,
            bindings=(
                ProjectBindings(
                    "p",
                    ("main",),
                    factories=(FactoryBinding("generated.main", "graphql", "main"),),
                ),
            ),
        )
        index = index_fragments(sources, schemas={("p", "main"): context})
        mappings = (
            ScalarMapping("UserId", "local_types.UserId", "local_types.UserId"),
        )
        with patch(
            "importlib.import_module", side_effect=AssertionError("No project imports")
        ):
            artifacts = generate_factory_artifacts(
                context,
                index,
                modules=modules,
                project_id="p",
                generated_package="generated",
                scalars=mappings,
            )
        self.assertEqual(artifacts.diagnostics, ())

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for item in artifacts.files:
                output = root / item.relative_path
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_text(item.text, encoding="utf8")
            for module in modules:
                (root / (module.module + ".py")).write_text(
                    module.source, encoding="utf8"
                )
            (root / "pyrightconfig.json").write_text(
                json.dumps(
                    {
                        "include": ["."],
                        "typeCheckingMode": "strict",
                        "pythonVersion": "3.10",
                    }
                ),
                encoding="utf8",
            )
            expected_errors = {
                number
                for number, line in enumerate(source.splitlines(), 1)
                if "wrong_input:" in line
                or "wrong_result:" in line
                or "graphql.scalar('UserId', 7)" in line
            }
            mypy = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "mypy",
                    "--strict",
                    "--no-incremental",
                    str(root),
                ],
                cwd=root,
                capture_output=True,
                text=True,
            )
            self.assertEqual(mypy.returncode, 1, mypy.stdout + mypy.stderr)
            mypy_lines = {
                int(number)
                for filename, number in re.findall(
                    r"([^/\\:\n]+\.py):(\d+): error:", mypy.stdout
                )
                if filename == "main.py"
            }
            self.assertEqual(mypy_lines, expected_errors, mypy.stdout + mypy.stderr)
            pyright = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pyright",
                    "--project",
                    str(root),
                    "--pythonpath",
                    sys.executable,
                    "--outputjson",
                ],
                cwd=root,
                capture_output=True,
                text=True,
            )
            self.assertEqual(pyright.returncode, 1, pyright.stdout + pyright.stderr)
            diagnostics = json.loads(pyright.stdout)["generalDiagnostics"]
            self.assertEqual(
                {
                    item["range"]["start"]["line"] + 1
                    for item in diagnostics
                    if item["file"].endswith("main.py")
                },
                expected_errors,
                pyright.stdout + pyright.stderr,
            )
            runtime = subprocess.run(
                [sys.executable, "main.py"],
                cwd=root,
                capture_output=True,
                text=True,
            )
            self.assertEqual(runtime.returncode, 0, runtime.stdout + runtime.stderr)


if __name__ == "__main__":
    unittest.main()
