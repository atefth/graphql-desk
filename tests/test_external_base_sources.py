"""Static external class-base references used by mapped project types."""

import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from graphql import build_schema

from graphql_desk.config import ScalarMapping
from graphql_desk.external_type_sources import (
    capture_external_type_sources,
    external_class_base_symbols,
)
from graphql_desk.factory_generation import generate_factory_artifacts
from graphql_desk.fragments import SchemaContext, index_fragments
from graphql_desk.source_graph import (
    FactoryBinding,
    ModuleSnapshot,
    ProjectBindings,
    resolve_project,
    resolve_type_symbols,
)


class ExternalBaseSourcesTests(unittest.TestCase):
    def test_finds_only_imported_class_base_symbols(self) -> None:
        source = ModuleSnapshot(
            "app",
            "models",
            "file:///app/models.py",
            1,
            "from vendor import BaseModel as Model\n"
            "import vendor.types as types\n"
            "import vendor.deep\n"
            "from local_models import LocalBase\n"
            "class User(Model): pass\n"
            "class Record(types.RecordBase): pass\n"
            "class Event(vendor.deep.EventBase): pass\n"
            "class Local(LocalBase): pass\n"
            "class Plain: pass\n",
        )

        local_base = ModuleSnapshot(
            "app",
            "local_models",
            "file:///app/local_models.py",
            1,
            "class LocalBase: pass\n",
        )
        self.assertEqual(
            external_class_base_symbols((source, local_base)),
            (
                "vendor.BaseModel",
                "vendor.deep.EventBase",
                "vendor.types.RecordBase",
            ),
        )

    def test_captured_stub_class_proves_local_subclass_without_importing_it(
        self,
    ) -> None:
        local = ModuleSnapshot(
            "app",
            "models",
            "file:///app/models.py",
            1,
            "from vendor import BaseModel as Base\nclass Date(Base): pass\n",
        )
        self.assertEqual(external_class_base_symbols((local,)), ("vendor.BaseModel",))

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            vendor = root / "vendor"
            vendor.mkdir()
            (vendor / "__init__.pyi").write_text(
                "from .types import BaseModel\n", encoding="utf-8"
            )
            (vendor / "types.pyi").write_text(
                "raise RuntimeError('must not execute')\nclass BaseModel: ...\n",
                encoding="utf-8",
            )

            external = capture_external_type_sources(
                external_class_base_symbols((local,)),
                search_roots=(root,),
                project_id="app#interpreter",
            )
            # External modules are parsed as snapshots; their source stays inert.
            with patch(
                "builtins.__import__",
                wraps=__import__,
            ) as imported:
                project_external = tuple(
                    ModuleSnapshot(
                        "app",
                        module.module,
                        module.uri,
                        module.version,
                        module.source,
                        module.is_package,
                    )
                    for module in external
                )
            resolved = resolve_type_symbols(
                ("models.Date",),
                modules=(local, *project_external),
                project_id="app",
            )[0]
            self.assertFalse(
                any(
                    call.args[0] in {"vendor", "vendor.types"}
                    for call in imported.call_args_list
                )
            )

        self.assertEqual({item.module for item in external}, {"vendor", "vendor.types"})
        self.assertEqual(
            (resolved.status, resolved.target), ("resolved", "models.Date")
        )

    def test_generated_mapping_accepts_local_subclasses_of_external_stub_base(
        self,
    ) -> None:
        source = """from typing import TypeVar
from typing_extensions import assert_type
from graphql_desk import TypedDocument
from graphql_desk_generated.main import graphql
from graphql_desk_generated.sources.main import QueryResult, QueryVariables
from models import DateInput, DateOutput
R = TypeVar("R")
V = TypeVar("V")
def execute(document: TypedDocument[R, V], variables: V) -> R: raise RuntimeError
query = graphql("query($value: Date!) { when(value: $value) }")
variables: QueryVariables = {"value": DateInput()}
result: QueryResult = execute(query, variables)
assert_type(result["when"], DateOutput)
wrong_variables: QueryVariables = {"value": DateOutput()}  # error
wrong_output: str = result["when"]  # error
"""
        main = ModuleSnapshot("app", "main", "file:///main.py", 1, source)
        models = ModuleSnapshot(
            "app",
            "models",
            "file:///models.py",
            1,
            "from vendor import BaseModel\n"
            "class DateInput(BaseModel): ...\n"
            "class DateOutput(BaseModel): ...\n",
        )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            packages = root / "packages"
            vendor_stub = packages / "vendor-stubs"
            vendor_stub.mkdir(parents=True)
            external_base = "class BaseModel: ...\n"
            (vendor_stub / "__init__.pyi").write_text(external_base, encoding="utf8")
            (vendor_stub / "py.typed").write_text("", encoding="utf8")
            checker_stubs = packages / "checker"
            checker_vendor = checker_stubs / "vendor"
            checker_vendor.mkdir(parents=True)
            (checker_vendor / "__init__.pyi").write_text(external_base, encoding="utf8")
            external = capture_external_type_sources(
                external_class_base_symbols((models,)),
                search_roots=(packages,),
                project_id="app#interpreter",
            )
            modules = (
                main,
                models,
                *(
                    ModuleSnapshot(
                        "app",
                        item.module,
                        item.uri,
                        item.version,
                        item.source,
                        item.is_package,
                    )
                    for item in external
                ),
            )
            project = resolve_project(
                modules,
                bindings=(
                    ProjectBindings(
                        "app",
                        ("main",),
                        factories=(
                            FactoryBinding(
                                "graphql_desk_generated.main", "graphql", "main"
                            ),
                        ),
                    ),
                ),
            )
            context = SchemaContext(
                "main",
                build_schema("scalar Date type Query { when(value: Date!): Date! }"),
                "test-schema",
            )
            with patch(
                "importlib.import_module",
                side_effect=AssertionError("Generation must not import project code"),
            ):
                artifacts = generate_factory_artifacts(
                    context,
                    index_fragments(project, schemas={("app", "main"): context}),
                    modules=modules,
                    project_id="app",
                    generated_package="graphql_desk_generated",
                    scalars=(
                        ScalarMapping("Date", "models.DateInput", "models.DateOutput"),
                    ),
                )
            self.assertEqual(artifacts.diagnostics, ())

            for artifact in artifacts.files:
                path = root / artifact.relative_path
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(artifact.text, encoding="utf8")
            for module in (main, models):
                path = root / (module.module.replace(".", "/") + ".py")
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(module.source, encoding="utf8")

            errors = {
                number
                for number, line in enumerate(source.splitlines(), 1)
                if "# error" in line
            }
            mypy = subprocess.run(
                [
                    sys.executable,
                    "-X",
                    "utf8",
                    "-m",
                    "mypy",
                    "--strict",
                    "--no-incremental",
                    "--python-version",
                    "3.10",
                    str(root / "main.py"),
                ],
                cwd=root,
                env={**os.environ, "MYPYPATH": str(checker_stubs)},
                capture_output=True,
                text=True,
                encoding="utf8",
            )
            self.assertEqual(mypy.returncode, 1, mypy.stdout + mypy.stderr)
            self.assertEqual(
                {
                    int(number)
                    for number in re.findall(r"main\.py:(\d+): error:", mypy.stdout)
                },
                errors,
                mypy.stdout + mypy.stderr,
            )

            (root / "pyrightconfig.json").write_text(
                json.dumps(
                    {
                        "include": ["main.py", "graphql_desk_generated"],
                        "extraPaths": [str(checker_stubs)],
                        "pythonVersion": "3.10",
                        "typeCheckingMode": "strict",
                        "reportMissingModuleSource": False,
                    }
                ),
                encoding="utf8",
            )
            pyright = subprocess.run(
                [
                    sys.executable,
                    "-X",
                    "utf8",
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
                encoding="utf8",
            )
            self.assertEqual(pyright.returncode, 1, pyright.stdout + pyright.stderr)
            self.assertEqual(
                {
                    item["range"]["start"]["line"] + 1
                    for item in json.loads(pyright.stdout)["generalDiagnostics"]
                    if Path(item["file"]).name == "main.py"
                },
                errors,
                pyright.stdout + pyright.stderr,
            )


if __name__ == "__main__":
    unittest.main()
