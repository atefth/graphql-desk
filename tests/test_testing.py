"""Unsafe fixture views check direct fields and deliberately ignore masked values."""

import json
import re
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Callable
from pathlib import Path
from typing import cast

from graphql import build_schema

from graphql_desk.config import ScalarMapping
from graphql_desk.fragment_refs import fragment_identity_key
from graphql_desk.fragments import SchemaContext, index_fragments
from graphql_desk.operation_types import (
    render_indexed_fragment_reader_types,
    render_indexed_operation_types,
)
from graphql_desk.source_graph import ModuleSnapshot, resolve_project
from graphql_desk.testing import render_unsafe_result_helper, unsafe_read_result


class TestingHelpersTests(unittest.TestCase):
    def fixtures(self) -> dict[str, str]:
        context = SchemaContext(
            "main",
            build_schema(
                "interface Node { id: ID! } interface Lonely { title: String } "
                "scalar Date enum Color { RED BLUE } "
                "type User implements Node { id: ID! title: String! other: Int! "
                "when: Date! color: Color! friend: User friends: [User!]! } "
                "type Org implements Node { id: ID! count: Int! } "
                "type Query { user: User! node: Node! }"
            ),
            "fixture-v1",
        )
        sources = {
            "one": ("fragment One on User { title }", ()),
            "other": ("fragment Other on User { other }", ()),
            "aliased": ("fragment Aliased on User { label:title }", ()),
            "overlap": (
                "fragment Overlap on User { label:title ...Aliased }",
                ("aliased",),
            ),
            "masked": ("fragment Masked on User { ...One }", ("one",)),
            "nested": (
                "fragment Nested on User { id friend { ...One } friends { ...One } }",
                ("one",),
            ),
            "optional": ("fragment Optional on User { title @include(if:true) }", ()),
            "abstract": (
                "fragment Abstract on Node { __typename "
                "... on User { title } ... on Org { count } }",
                (),
            ),
            "empty": ("fragment Empty on Lonely { __typename }", ()),
            "query": (
                "query Q { direct:user { id title when color "
                "friend { id ...Masked } friends { id ...One } ...One ...Other } "
                "maskedOnly:user { ...One } "
                "conditional:user { id ...One @include(if:true) } "
                "node { __typename ...Abstract } }",
                ("one", "other", "masked", "abstract"),
            ),
        }
        snapshots = tuple(
            ModuleSnapshot(
                "p",
                module,
                f"file:///{module}.py",
                1,
                "from graphql_desk import gql\n"
                + "".join(f"from {dep} import {dep}\n" for dep in dependencies)
                + f"{module}=gql({source!r},[{','.join(dependencies)}])",
            )
            for module, (source, dependencies) in sources.items()
        )
        index = index_fragments(
            resolve_project(snapshots), schemas={("p", None): context}
        )
        self.assertEqual(index.diagnostics, ())
        mappings = (ScalarMapping("Date", "builtins.str", "datetime.date"),)
        files: dict[str, str] = {}
        for document in index.documents:
            module = document.call.module.module
            name = module.capitalize()
            primary = document.primary_fragment
            if primary is None:
                text = render_indexed_operation_types(
                    context,
                    document,
                    name=name,
                    scalars=mappings,
                    fragment_index=index,
                    fixture_input=True,
                )
                bases = (
                    f"__graphql_desk_Fixture[{name}Result, "
                    f"{name}Variables, {name}ResultFixtureInput]"
                )
                imports = ""
            else:
                reader = render_indexed_fragment_reader_types(
                    context,
                    document,
                    name=name,
                    scalars=mappings,
                    fragment_index=index,
                    fixture_input=True,
                )
                identity = name + "Identity"
                key = fragment_identity_key(document, primary)
                text = (
                    reader.text + "\nfrom graphql_desk.fragment_refs import "
                    "FragmentIdentity as __graphql_desk_Identity\n"
                    + f"{identity} = __graphql_desk_Identity[Literal[{key!r}]]\n"
                )
                imports = (
                    "from graphql_desk.fragment_refs import "
                    "ReadableFragmentDocument as __graphql_desk_Readable\n"
                )
                bases = (
                    f"__graphql_desk_Readable[{identity}, {name}Result, "
                    f"{name}Variables, {reader.input_name}], "
                    f"__graphql_desk_Fixture[{name}Result, {name}Variables, "
                    f"{name}ResultFixtureInput]"
                )
            helper = render_unsafe_result_helper(
                name=name,
                input_name=name + "ResultFixtureInput",
                output_name=name + "Result",
            )
            files[module + ".py"] = text + helper.runtime
            files[module + ".pyi"] = (
                text + helper.stub + "\nfrom graphql_desk.fragment_refs import "
                "FixtureDocument as __graphql_desk_Fixture\n"
                + imports
                + f"class Document({bases}): ...\ndocument: Document\n"
            )
        return files

    def test_identity_and_root_import_before_document_definition(self) -> None:
        class NoReads:
            def __getattribute__(self, name: str) -> object:
                raise AssertionError(name)

        call = cast(Callable[[object, object], object], unsafe_read_result)
        declaration = render_unsafe_result_helper(
            name="Query", input_name="QueryInput", output_name="QueryResult"
        )
        namespace: dict[str, object] = {}
        exec(declaration.runtime, namespace)
        bound = cast(Callable[[object], object], namespace[declaration.function_name])
        values: tuple[object, ...] = (
            None,
            dict[str, object](),
            list[object](),
            [dict[str, object](), None],
            NoReads(),
        )
        for value in values:
            self.assertIs(call(NoReads(), value), value)
            self.assertIs(bound(value), value)
        package = Path(__file__).resolve().parents[1] / "src" / "graphql_desk"
        checked = subprocess.run(
            [
                sys.executable,
                "-X",
                "utf8",
                "-c",
                "import sys,types\np=types.ModuleType('graphql_desk')\n"
                f"p.__path__=[{str(package)!r}]\n"
                "sys.modules['graphql_desk']=p\nimport graphql_desk.testing\n"
                "assert not hasattr(p,'TypedDocument')\n",
            ],
            capture_output=True,
            text=True,
            encoding="utf8",
        )
        self.assertEqual(checked.returncode, 0, checked.stdout + checked.stderr)

    def test_formatter_names_are_checked_and_output_is_deterministic(self) -> None:
        self.assertEqual(
            render_unsafe_result_helper(name="F", input_name="Input", output_name="R"),
            render_unsafe_result_helper(name="F", input_name="Input", output_name="R"),
        )
        for name, input_name, output_name in (
            ("class", "Input", "Result"),
            ("F", "bad.name", "Result"),
            ("F", "Input", "FUnsafeReadResult"),
            ("F", "__graphql_desk_identity_fragment", "Result"),
        ):
            with self.assertRaises(ValueError):
                render_unsafe_result_helper(
                    name=name, input_name=input_name, output_name=output_name
                )

    def test_real_generated_fixture_inputs_in_both_checkers(self) -> None:
        positive = """from datetime import date
from typing_extensions import assert_type
from graphql_desk import TypedDocument
from graphql_desk.testing import unsafe_read_result
from query import QueryUnsafeReadResult as query, QueryResult, QueryResultFixtureInput
from query import document as query_document
from one import OneUnsafeReadResult as one, OneResult
from nested import NestedUnsafeReadResult as nested, NestedResult
from optional import OptionalUnsafeReadResult as optional, OptionalResult
from masked import MaskedUnsafeReadResult as masked, MaskedResult
from abstract import AbstractUnsafeReadResult as abstract, AbstractResult
from overlap import OverlapUnsafeReadResult as overlap, OverlapResult
from query import QueryVariables
from one import document as one_document, OneIdentity, OneVariables
from one import OneResultInput, OneResultFixtureInput
from graphql_desk.fragment_refs import FragmentDocument, ReadableFragmentDocument

base:TypedDocument[QueryResult,QueryVariables]=query_document
fragment:FragmentDocument[OneIdentity,OneResult,OneVariables]=one_document
reader:ReadableFragmentDocument[OneIdentity,OneResult,OneVariables,OneResultInput]=one_document

def check(fixture:QueryResultFixtureInput, value:OneResultFixtureInput)->None:
 assert_type(unsafe_read_result(query_document,fixture),QueryResult)
 assert_type(unsafe_read_result(one_document,value),OneResult)
 assert_type(one({'title':'x'}),OneResult)
 assert_type(overlap({'label':'x'}),OverlapResult)
 assert_type(optional({}),OptionalResult)
 assert_type(masked({}),MaskedResult)
 assert_type(masked({'title':123,'unknown':False}),MaskedResult)
 assert_type(nested({'id':'x','friend':{'title':123},'friends':[{'title':False}]}),NestedResult)
 assert_type(nested({'id':'x','friend':None,'friends':[]}),NestedResult)
 assert_type(abstract({'__typename':'Bogus','title':'x'}),AbstractResult)
 assert_type(abstract({'__typename':'Org','title':'x'}),AbstractResult)
 assert_type(abstract({'__typename':'Org','count':1}),AbstractResult)
 assert_type(query({'direct':{'id':'x','title':'x','when':date.today(),'color':'GREEN','other':'unchecked','friend':{'id':'f','title':123},'friends':[{'id':'a','title':False}]},'maskedOnly':{},'conditional':{'id':'x','title':False},'node':{'__typename':'Bogus','title':123}}),QueryResult)
 assert_type(query({'direct':{'id':'x','title':'x','when':date.today(),'color':'RED','friend':None,'friends':[]},'maskedOnly':{'title':None},'conditional':{'id':'x'},'node':{'__typename':'Org'}}),QueryResult)
"""
        negative = """from datetime import date
from one import OneUnsafeReadResult as one
from masked import MaskedUnsafeReadResult as masked
from optional import OptionalUnsafeReadResult as optional
from nested import NestedUnsafeReadResult as nested
from abstract import AbstractUnsafeReadResult as abstract
from query import QueryUnsafeReadResult as query
from overlap import OverlapUnsafeReadResult as overlap
from graphql_desk.testing import unsafe_read_result
from one import document
from typing_extensions import TypedDict
one({}) # error
one({'title':1}) # error
optional({'title':1}) # error
overlap({'label':123}) # error
masked(None) # error
masked(1) # error
masked([]) # error
nested({'id':1,'friend':{},'friends':[]}) # error
nested({'id':'x','friends':[]}) # error
nested({'id':'x','friend':[],'friends':[]}) # error
nested({'id':'x','friend':{},'friends':[None]}) # error
abstract({'__typename':'User'}) # error
abstract({'__typename':1,'title':'x'}) # error
abstract({'__typename':'User','count':'wrong'}) # error
query(None) # error
query([]) # error
class Wrong(TypedDict):
 title:int
def wrong(value:Wrong)->None:
 unsafe_read_result(document,value) # error
"""
        bad_direct = (
            "{'id':'x','title':123,'when':date.today(),'color':'RED',"
            "'friend':None,'friends':[]}",
            "{'id':'x','title':'x','when':'input-scalar','color':'RED',"
            "'friend':None,'friends':[]}",
            "{'id':'x','title':'x','when':date.today(),'color':'RED',"
            "'friend':{},'friends':[]}",
            "{'id':'x','title':'x','when':date.today(),'color':'RED',"
            "'friend':None,'friends':[{}]}",
        )
        negative += "".join(
            "query({'direct':" + fields + ",'maskedOnly':{},'conditional':{'id':'x'},"
            "'node':{'__typename':'User'}}) # error\n"
            for fields in bad_direct
        )
        files = self.fixtures()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for filename, text in files.items():
                (root / filename).write_text(text, encoding="utf8")
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
                        *(str(root / f) for f in files if f.endswith(".pyi")),
                    ],
                    cwd=root,
                    capture_output=True,
                    text=True,
                    encoding="utf8",
                )
                self.assertEqual(
                    checked.returncode, bool(expected), checked.stdout + checked.stderr
                )
                self.assertEqual(
                    {
                        int(line)
                        for line in re.findall(
                            re.escape(filename) + r":(\d+): error:", checked.stdout
                        )
                    },
                    expected,
                    checked.stdout + checked.stderr,
                )
                (root / "pyrightconfig.json").write_text(
                    json.dumps(
                        {
                            "include": [filename, *files],
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
