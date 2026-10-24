"""Generated scalar methods share schema input declarations and reject bad calls."""

import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from graphql import GraphQLSchema, build_schema

from graphql_desk.config import ScalarMapping
from graphql_desk.scalar_types import render_scalar_method
from graphql_desk.schema_types import render_schema_types


class ScalarTypesTests(unittest.TestCase):
    def schema(self) -> GraphQLSchema:
        return build_schema(
            "scalar Date enum Color { RED BLUE } enum State { ON OFF } "
            "input Filter { id: ID! colors: [Color!]! dates: [Date!]! "
            "child: Filter flag: Boolean! = true } "
            "input Choice @oneOf { id: ID name: String } "
            "type Query { name: String id: ID count: Int rate: Float "
            "alive: Boolean when: Date color: Color state: State "
            "search(filter: Filter, choice: Choice): String }"
        )

    def test_method_only_reuses_input_aliases_and_is_deterministic(self) -> None:
        schema = self.schema()
        mappings = (ScalarMapping("Date", "missing.Input", "missing.Output"),)
        result = render_scalar_method(schema, scalars=mappings)
        self.assertEqual(result, render_scalar_method(schema, scalars=mappings))
        self.assertNotIn("import ", result)
        self.assertNotIn("TypedDict(", result)
        self.assertIn("'Date'], value: DateInput) -> DateInput", result)
        self.assertNotIn("DateOutput", result)
        self.assertIn("'Choice'], value: Choice) -> Choice", result)
        self.assertIn("'Filter'], value: None) -> None", result)
        self.assertNotIn("'Query'", result)
        self.assertNotIn("value: object", result)
        self.assertNotIn("value: Any", result)
        self.assertNotIn("value =", result)
        # Pinned upstream includes specified scalars even when unused by a schema.
        minimal = render_scalar_method(build_schema("type Query { text: String }"))
        self.assertIn("'ID'], value: ID) -> ID", minimal)
        self.assertIn("'Float'], value: Float) -> Float", minimal)
        overridden = render_scalar_method(
            schema,
            scalars=(*mappings, ScalarMapping("Int", "builtins.str", "builtins.int")),
        )
        self.assertIn("'Int'], value: IntInput) -> IntInput", overridden)
        self.assertNotIn("'Int'], value: IntOutput", overridden)

    def test_shared_mapping_and_input_safety_failures_remain_errors(self) -> None:
        for schema, mappings in (
            (build_schema("scalar Date type Query { x: Date }"), ()),
            (
                build_schema("input class { x: Int } type Query { x(v:class): Int }"),
                (),
            ),
        ):
            with self.subTest(schema=schema), self.assertRaises(ValueError):
                render_scalar_method(schema, scalars=mappings)

    def test_actual_composed_protocol_positive_and_negative_both_checkers(self) -> None:
        schema = self.schema()
        mappings = (ScalarMapping("Date", "models.DateInput", "models.DateOutput"),)
        enums = (("State", "models.State"),)
        api = (
            render_schema_types(schema, scalars=mappings, enums=enums)
            + "\nfrom typing import Protocol as __graphql_desk_Protocol, "
            "Literal as __graphql_desk_Literal, "
            "overload as __graphql_desk_overload\n"
            "class Factory(__graphql_desk_Protocol):\n"
            + render_scalar_method(schema, scalars=mappings, enums=enums)
            + "\nfactory: Factory\n"
        )
        models = (
            "from enum import Enum\n"
            "class DateInput: ...\nclass DateOutput: ...\n"
            "class State(Enum):\n    ON = ...\n    OFF = ...\n"
        )
        positive = """from typing import Literal
from typing_extensions import assert_type
from api import factory,Filter,Choice,Color,State,DateInput
from minimal import factory as minimal
from models import DateInput as InputDate
assert_type(factory.scalar('Int',1),int)
assert_type(factory.scalar('Float',1.5),float)
assert_type(factory.scalar('String','text'),str)
assert_type(factory.scalar('Boolean',True),bool)
assert_type(factory.scalar('ID','id'),str)
assert_type(minimal.scalar('ID','id'),str)
assert_type(minimal.scalar('Float',1.5),float)
assert_type(minimal.scalar('ID',None),None)
assert_type(factory.scalar('Color','RED'),Literal['BLUE','RED'])
assert_type(factory.scalar('State',State.ON),State)
assert_type(factory.scalar('Date',InputDate()),DateInput)
assert_type(factory.scalar('Date',None),None)
assert_type(factory.scalar('Color',None),None)
assert_type(factory.scalar('Choice',None),None)
assert_type(factory.scalar('Filter',None),None)
value:Filter={'id':'1','colors':['RED'],'dates':[InputDate()],
 'flag':None,'child':{'id':'2','colors':[],'dates':[]}}
assert_type(factory.scalar('Filter',value),Filter)
assert_type(factory.scalar('Filter',{'id':'1','colors':[],'dates':[]}),Filter)
choice:Choice={'id':'1'}
assert_type(factory.scalar('Choice',choice),Choice)
def maybe_filter(value:Filter|None)->None:
 assert_type(factory.scalar('Filter',value),Filter|None)
def maybe_choice(value:Choice|None)->None:
 assert_type(factory.scalar('Choice',value),Choice|None)
def maybe_date(value:InputDate|None)->None:
 assert_type(factory.scalar('Date',value),InputDate|None)
color:Color='BLUE'
assert_type(factory.scalar('Color',color),Literal['BLUE','RED'])
"""
        negative = """from api import factory
from minimal import factory as minimal
from models import DateOutput
factory.scalar('Unknown',None) # error
factory.scalar('Query',{}) # error
factory.scalar('Int','1') # error
factory.scalar('Color','GREEN') # error
factory.scalar('State','ON') # error
factory.scalar('Date',DateOutput()) # error
factory.scalar('String') # error
factory.scalar() # error
factory.scalar('Filter',{'colors':[],'dates':[]}) # error
factory.scalar('Filter',{'id':None,'colors':[],'dates':[]}) # error
factory.scalar('Filter',{'id':'1','colors':[None],'dates':[]}) # error
factory.scalar('Filter',{'id':'1','colors':[],'dates':[DateOutput()]}) # error
factory.scalar('Filter',{'id':'1','colors':[],'dates':[],'child':{'id':1}}) # error
factory.scalar('Choice',{}) # error
factory.scalar('Choice',{'id':'1','name':'x'}) # error
factory.scalar('Choice',{'id':None}) # error
minimal.scalar('ID',1) # error
minimal.scalar('Float','1.5') # error
def dynamic(name:str)->None:
 factory.scalar(name,1) # error
"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "api.pyi").write_text(api, encoding="utf8")
            minimal = build_schema("type Query { text: String }")
            self.assertNotIn("ID", minimal.type_map)
            self.assertNotIn("Float", minimal.type_map)
            (root / "minimal.pyi").write_text(
                render_schema_types(minimal)
                + "\nfrom typing import Protocol as __graphql_desk_Protocol, "
                "Literal as __graphql_desk_Literal, "
                "overload as __graphql_desk_overload\n"
                "class Factory(__graphql_desk_Protocol):\n"
                + render_scalar_method(minimal)
                + "\nfactory: Factory\n",
                encoding="utf8",
            )
            (root / "models.pyi").write_text(models, encoding="utf8")
            for filename, source in (
                ("positive.py", positive),
                ("negative.py", negative),
            ):
                (root / filename).write_text(source, encoding="utf8")
                expected = {
                    number
                    for number, line in enumerate(source.splitlines(), 1)
                    if "# error" in line
                }
                checked = subprocess.run(
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
                        str(root / filename),
                        str(root / "api.pyi"),
                    ],
                    cwd=root,
                    capture_output=True,
                    text=True,
                    encoding="utf8",
                )
                self.assertEqual(
                    checked.returncode, bool(expected), checked.stdout + checked.stderr
                )
                actual = {
                    int(number)
                    for number in re.findall(
                        re.escape(filename) + r":(\d+): error:", checked.stdout
                    )
                }
                self.assertEqual(actual, expected, checked.stdout + checked.stderr)
                (root / "pyrightconfig.json").write_text(
                    json.dumps(
                        {
                            "include": [filename, "api.pyi"],
                            "pythonVersion": "3.10",
                            "typeCheckingMode": "strict",
                            "reportMissingModuleSource": False,
                        }
                    ),
                    encoding="utf8",
                )
                checked = subprocess.run(
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
                self.assertEqual(
                    checked.returncode, bool(expected), checked.stdout + checked.stderr
                )
                diagnostics = json.loads(checked.stdout)["generalDiagnostics"]
                self.assertTrue(
                    all(Path(item["file"]).name == filename for item in diagnostics),
                    diagnostics,
                )
                self.assertEqual(
                    {item["range"]["start"]["line"] + 1 for item in diagnostics},
                    expected,
                    diagnostics,
                )

    def test_external_stub_only_mapping_types_with_both_checkers(self) -> None:
        schema = build_schema(
            "scalar Date enum Color { RED BLUE } type Query { when: Date color: Color }"
        )
        mappings = (
            ScalarMapping("Date", "vendor_types.DateInput", "vendor_types.DateOutput"),
        )
        enums = (("Color", "vendor_types.Color"),)
        api = (
            render_schema_types(schema, scalars=mappings, enums=enums)
            + "\nfrom typing import Protocol as __graphql_desk_Protocol, "
            "Literal as __graphql_desk_Literal, "
            "overload as __graphql_desk_overload\n"
            "class Factory(__graphql_desk_Protocol):\n"
            + render_scalar_method(schema, scalars=mappings, enums=enums)
            + "\nfactory: Factory\n"
        )
        positive = """from typing_extensions import assert_type
from api import factory
from vendor_types import Color, DateInput
assert_type(factory.scalar('Date', DateInput()), DateInput)
assert_type(factory.scalar('Color', Color.RED), Color)
"""
        negative = """from api import factory
from vendor_types import DateOutput
factory.scalar('Date', DateOutput()) # error
factory.scalar('Color', 'GREEN') # error
"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            packages = root / "packages"
            vendor = packages / "vendor_types"
            vendor.mkdir(parents=True)
            (root / "api.pyi").write_text(api, encoding="utf8")
            (vendor / "__init__.pyi").write_text(
                "from .models import Color as Color, DateInput as DateInput, "
                "DateOutput as DateOutput\n",
                encoding="utf8",
            )
            (vendor / "models.pyi").write_text(
                "from enum import Enum\n"
                "class Color(Enum):\n    RED = ...\n    BLUE = ...\n"
                "class DateInput: ...\nclass DateOutput: ...\n",
                encoding="utf8",
            )
            for filename, source in (
                ("positive.py", positive),
                ("negative.py", negative),
            ):
                (root / filename).write_text(source, encoding="utf8")
                expected = {
                    number
                    for number, line in enumerate(source.splitlines(), 1)
                    if "# error" in line
                }
                checked = subprocess.run(
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
                        str(root / filename),
                        str(root / "api.pyi"),
                    ],
                    cwd=root,
                    env={**os.environ, "MYPYPATH": str(packages)},
                    capture_output=True,
                    text=True,
                    encoding="utf8",
                )
                self.assertEqual(
                    checked.returncode, bool(expected), checked.stdout + checked.stderr
                )
                actual = {
                    int(number)
                    for number in re.findall(
                        re.escape(filename) + r":(\d+): error:", checked.stdout
                    )
                }
                self.assertEqual(actual, expected, checked.stdout + checked.stderr)
                (root / "pyrightconfig.json").write_text(
                    json.dumps(
                        {
                            "include": [filename, "api.pyi"],
                            "extraPaths": [str(packages)],
                            "pythonVersion": "3.10",
                            "typeCheckingMode": "strict",
                            "reportMissingModuleSource": False,
                        }
                    ),
                    encoding="utf8",
                )
                checked = subprocess.run(
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
                self.assertEqual(
                    checked.returncode, bool(expected), checked.stdout + checked.stderr
                )
                diagnostics = json.loads(checked.stdout)["generalDiagnostics"]
                self.assertTrue(
                    all(Path(item["file"]).name == filename for item in diagnostics),
                    diagnostics,
                )
                self.assertEqual(
                    {item["range"]["start"]["line"] + 1 for item in diagnostics},
                    expected,
                    diagnostics,
                )
