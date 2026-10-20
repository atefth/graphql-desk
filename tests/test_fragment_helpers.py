"""Readers keep response identity and validate generated static input views."""

import json
import re
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import cast

from graphql import build_schema

from graphql_desk.fragment_helpers import read_fragment, render_fragment_helpers
from graphql_desk.fragment_refs import fragment_identity_key
from graphql_desk.fragments import SchemaContext, index_fragments
from graphql_desk.operation_types import (
    FragmentReaderTypes,
    render_indexed_fragment_reader_types,
    render_indexed_operation_types,
)
from graphql_desk.source_graph import ModuleSnapshot, resolve_project


class FragmentHelpersTests(unittest.TestCase):
    def fixtures(self) -> tuple[dict[str, str], FragmentReaderTypes]:
        context = SchemaContext(
            "main",
            build_schema(
                "interface Node { id: ID! } interface Lonely { title: String } "
                "type User implements Node { id: ID! title: String! name: String! "
                "friend: User friends: [User!]! } "
                "type Org implements Node { id: ID! count: Int! } "
                "type Query { user: User! node: Node! }"
            ),
            "reader-v1",
        )
        fragments = {
            "inner": "fragment Inner on User { name }",
            "required": "fragment Required on User { title }",
            "optional": "fragment Optional on User { title @include(if:$flag) }",
            "outer": "fragment Outer on User { title "
            "friend { title @include(if:$flag) ...Inner } friends { ...Inner } }",
            "abstract": "fragment Abstract on Node { __typename "
            "... on User { name } ... on Org { count } }",
            "sparse": "fragment Sparse on Node { __typename ... on User { name } }",
            "correlated": "fragment Correlated on User { __typename "
            "...Inner @include(if:$flag) }",
            "vacant": "fragment Vacant on Lonely { __typename }",
        }
        snapshots = tuple(
            ModuleSnapshot(
                "p",
                module,
                f"file:///{module}.py",
                1,
                "from graphql_desk import gql\n"
                + (
                    "from inner import inner\n"
                    if module in {"outer", "correlated"}
                    else ""
                )
                + f"{module}=gql({source!r}"
                + (",[inner])" if module in {"outer", "correlated"} else ")"),
            )
            for module, source in fragments.items()
        )
        query = ModuleSnapshot(
            "p",
            "query",
            "file:///query.py",
            1,
            "from graphql_desk import gql\nfrom inner import inner\n"
            "from required import required\nfrom abstract import abstract\n"
            "query=gql('query Q($flag:Boolean!){ "
            "conditional:user{...Required @include(if:$flag)} "
            "outer:user{title friend{title ...Inner} friends{...Inner}} "
            "node{__typename ...Abstract}}',[required,inner,abstract])",
        )
        index = index_fragments(
            resolve_project((*snapshots, query)), schemas={("p", None): context}
        )
        self.assertEqual(index.diagnostics, ())
        files: dict[str, str] = {}
        last: FragmentReaderTypes | None = None
        for document in index.documents:
            module = document.call.module.module
            if module == "query":
                files["query.py"] = (
                    render_indexed_operation_types(
                        context, document, name="Query", fragment_index=index
                    )
                    + "\nAbsent = __graphql_desk_variant_0\n"
                )
                continue
            name = module.capitalize()
            rendered = render_indexed_fragment_reader_types(
                context, document, name=name, fragment_index=index
            )
            primary = document.primary_fragment
            assert primary is not None
            identity = name + "Identity"
            key = fragment_identity_key(document, primary)
            text = (
                rendered.text
                + "\nfrom graphql_desk.fragment_refs import FragmentIdentity "
                "as __graphql_desk_Identity\n"
                + f"{identity} = __graphql_desk_Identity[Literal[{key!r}]]\n"
            )
            declarations = render_fragment_helpers(
                rendered, name=name, identity_name=identity
            )
            files[module + ".py"] = text + declarations.runtime
            files[module + ".pyi"] = (
                text + declarations.stub + "\nfrom graphql_desk.fragment_refs import "
                "ReadableFragmentDocument as __graphql_desk_Readable\n"
                + f"class Document(__graphql_desk_Readable[{identity}, "
                + f"{rendered.output_name}, {name}Variables, "
                + f"{rendered.input_name}]):\n"
                + "    @property\n"
                + "    def _graphql_desk_reader(self) -> "
                + f"{declarations.reader_name}: ...\n"
                + "document: Document\n"
                + "".join(
                    f"{branch.typename}Result = {branch.output_name}\n"
                    for branch in rendered.branches
                    if branch.typename is not None
                    and sum(
                        other.typename == branch.typename for other in rendered.branches
                    )
                    == 1
                )
            )
            if rendered.branches:
                last = rendered
        assert last is not None
        return files, last

    def test_identity_does_not_read_documents_or_data(self) -> None:
        class NoReads:
            def __getattribute__(self, name: str) -> object:
                raise AssertionError(name)

        call = cast(Callable[[object, object], object], read_fragment)
        files, rendered = self.fixtures()
        self.assertTrue(files)
        declarations = render_fragment_helpers(
            rendered, name="Check", identity_name="CheckIdentity"
        )
        namespace: dict[str, object] = {}
        exec(declarations.runtime, namespace)
        bound = cast(Callable[[object], object], namespace[declarations.function_name])
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

    def test_names_and_branch_metadata_are_closed(self) -> None:
        _, rendered = self.fixtures()
        self.assertEqual(
            render_fragment_helpers(rendered, name="F", identity_name="Identity"),
            render_fragment_helpers(rendered, name="F", identity_name="Identity"),
        )
        for changed in (
            replace(rendered, output_name="class"),
            replace(rendered, input_name="bad.name"),
            replace(
                rendered,
                branches=(replace(rendered.branches[0], narrow_input_name=None),),
            ),
        ):
            with self.assertRaises(ValueError):
                render_fragment_helpers(changed, name="F", identity_name="Identity")
        with self.assertRaises(ValueError):
            render_fragment_helpers(rendered, name="class", identity_name="Identity")
        with self.assertRaises(ValueError):
            render_fragment_helpers(rendered, name="F", identity_name="FReader")
        with self.assertRaises(ValueError):
            render_fragment_helpers(
                rendered, name="F", identity_name="__graphql_desk_Ref"
            )

    def test_can_import_before_root_document_class_exists(self) -> None:
        package = Path(__file__).resolve().parents[1] / "src" / "graphql_desk"
        source = (
            "import sys,types\n"
            "package=types.ModuleType('graphql_desk')\n"
            f"package.__path__=[{str(package)!r}]\n"
            "sys.modules['graphql_desk']=package\n"
            "import graphql_desk.fragment_helpers\n"
            "assert not hasattr(package,'TypedDocument')\n"
        )
        checked = subprocess.run(
            [sys.executable, "-X", "utf8", "-c", source],
            capture_output=True,
            text=True,
            encoding="utf8",
        )
        self.assertEqual(checked.returncode, 0, checked.stdout + checked.stderr)

    def test_actual_rendered_bound_and_generic_helpers_both_checkers(self) -> None:
        files, _ = self.fixtures()
        positive = """from collections.abc import Sequence
from typing_extensions import Never, assert_type
from graphql_desk.fragment_helpers import read_fragment, FragmentAbsent
from query import QueryResult, Absent
from required import RequiredReadFragment as read_required, RequiredResult
from required import document as required
from optional import OptionalReadFragment as read_optional, OptionalResult
from optional import document as optional
from outer import OuterReadFragment as read_outer, OuterResult
from outer import OuterResult_friends
from inner import InnerReadFragment as read_inner, InnerResult
from abstract import AbstractReadFragment as read_abstract, AbstractResult
from abstract import document as abstract, UserResult, OrgResult
from sparse import SparseReadFragment as read_sparse, OrgResult as SparseOrg
from correlated import CorrelatedReadFragment as read_correlated, CorrelatedResult
from vacant import VacantReadFragment as read_vacant
from vacant import document as vacant
from required import RequiredIdentity, RequiredVariables
from graphql_desk.fragment_refs import FragmentDocument
base:FragmentDocument[RequiredIdentity,RequiredResult,RequiredVariables]=required

def check(query:QueryResult, absent:Absent)->None:
 assert_type(read_required(absent),FragmentAbsent)
 assert_type(read_optional(absent),OptionalResult)
 assert_type(read_required(query['conditional']),RequiredResult|FragmentAbsent)
 assert_type(read_required([query['conditional'],None]),Sequence[RequiredResult|FragmentAbsent|None])
 assert_type(read_fragment(required,[query['conditional'],None]),Sequence[RequiredResult|FragmentAbsent|None])
 assert_type(read_required({}),FragmentAbsent)
 assert_type(read_optional({}),OptionalResult)
 assert_type(read_fragment(required,{}),FragmentAbsent)
 assert_type(read_fragment(optional,{}),OptionalResult)
 assert_type(read_optional({'title':'t'}),OptionalResult)
 assert_type(read_optional([{'title':'t'},None]),Sequence[OptionalResult|None])
 assert_type(read_required(None),None)
 assert_type(read_required([]),Sequence[Never])
 assert_type(read_required([None]),Sequence[None])
 assert_type(read_fragment(required,[]),Sequence[Never])
 assert_type(read_fragment(required,None),None)
 assert_type(read_outer(query['outer']),OuterResult)
 assert_type(read_outer(query['outer'])['friends'],list[OuterResult_friends])
 assert_type(read_inner(read_outer(query['outer'])['friends'][0]),InnerResult)
 assert_type(read_abstract({'__typename':'User','name':'n'}),UserResult)
 assert_type(read_abstract([{'__typename':'Org','count':1}]),Sequence[OrgResult])
 assert_type(read_sparse({'__typename':'Org'}),SparseOrg)
 assert_type(read_correlated({'__typename':'User'}),CorrelatedResult)
 assert_type(read_vacant(None),None)
 assert_type(read_vacant({}),FragmentAbsent)
 assert_type(read_vacant([]),Sequence[Never])
 assert_type(read_fragment(vacant,{}),FragmentAbsent)
 if query['node']['__typename']=='User':
  assert_type(read_abstract(query['node']),UserResult)
  assert_type(read_fragment(abstract,query['node']),UserResult)
  assert_type(read_fragment(abstract,[query['node'],None]),Sequence[UserResult|None])
  assert_type(read_abstract([query['node'],None]),Sequence[AbstractResult|FragmentAbsent|None])

def nullable(values:list[OptionalResult|None]|None)->None:
 assert_type(read_optional(values),Sequence[OptionalResult|None]|None)
 assert_type(read_fragment(optional,values),Sequence[OptionalResult|None]|None)
"""
        negative = """from typing import Literal
from graphql_desk.fragment_refs import FragmentIdentity, FragmentRef
from graphql_desk.fragment_helpers import read_fragment
from graphql_desk import TypedDocument
from optional import OptionalReadFragment as optional, document
from query import Absent
from required import RequiredReadFragment as required
from outer import OuterReadFragment as outer
from sparse import SparseReadFragment as sparse
def bad(absent:Absent)->None:
 required(absent)['title'] # error
 required({'title':1}) # error
 outer({'title':'x','friend':{'title':'y'},'friends':[]}) # error
 outer({'title':'x','friend':None,'friends':[{}]}) # error
 sparse({'__typename':'Org'})['name'] # error
Other=FragmentIdentity[Literal['other']]
def wrong(ref:FragmentRef[Other])->None:
 optional(ref) # error
 read_fragment(document,ref) # error
def operation(doc:TypedDocument[dict[str,str],dict[str,str]])->None:
 read_fragment(doc,{}) # error
"""
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
                        *(str(root / item) for item in files if item.endswith(".pyi")),
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
