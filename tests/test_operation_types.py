import importlib.util
import json
import re
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from graphql import build_schema

from graphql_desk.config import ScalarMapping
from graphql_desk.fragment_refs import fragment_identity_key
from graphql_desk.fragments import (
    FragmentIndex,
    IndexedDocument,
    SchemaContext,
    index_fragments,
)
from graphql_desk.operation_types import (
    render_indexed_fragment_types,
    render_indexed_operation_types,
    render_operation_types,
)
from graphql_desk.source import Span
from graphql_desk.source_graph import DocumentIdentity, ModuleSnapshot, resolve_project

_SCHEMA = """
    enum Color { RED BLUE }
    input Filter { color: Color threshold: Int! = 10 }
    type User {
        id: ID! name: String color: Color!
        friends: [User!]! a: [String] b: [String!] c: [String]! d: [String!]!
    }
    type Query {
        user(id: ID!, limit: Int, filter: Filter): User
        greeting: String!
    }
    type Mutation { update: User! }
    type Subscription { events: [User!]! }
"""
_SOURCE = """
    query Book($id: ID!, $limit: Int! = 1, $filter: Filter) {
        user(id: $id, limit: $limit, filter: $filter) {
            key: id name color __typename friends { id } a b c d
        }
        user(id: $id, limit: $limit, filter: $filter) { friends { name } }
    }
"""


class OperationTypesTests(unittest.TestCase):
    def test_indexed_masked_payloads_capabilities_and_owner_guards(self) -> None:
        schema = build_schema(
            "type User { id: ID! name: String! friends: [User] } "
            "type Query { user: User! }"
        )
        context = SchemaContext("main", schema, "v1")

        def make(
            child_version: int = 1, *, conditional: bool = True
        ) -> tuple[FragmentIndex, IndexedDocument]:
            modules = (
                ModuleSnapshot(
                    "p",
                    "child",
                    "file:///child.py",
                    child_version,
                    "from graphql_desk import gql\n"
                    'c=gql("fragment C on User { name }")',
                ),
                ModuleSnapshot(
                    "p",
                    "fragment",
                    "file:///fragment.py",
                    1,
                    "from graphql_desk import gql\nfrom child import c\n"
                    'f=gql("fragment F on User { id ...C }",[c])',
                ),
                ModuleSnapshot(
                    "p",
                    "main",
                    "file:///main.py",
                    1,
                    "from graphql_desk import gql\nfrom fragment import f\n"
                    "from child import c\n"
                    'q=gql("query Q($flag:Boolean!){user{id ...F ...C '
                    '@include(if:$flag) friends{...F}}}",[f,c])',
                ),
            )
            if not conditional:
                modules = (
                    *modules[:-1],
                    replace(
                        modules[-1],
                        source=modules[-1]
                        .source.replace("($flag:Boolean!)", "")
                        .replace("@include(if:$flag)", ""),
                    ),
                )
            index = index_fragments(
                resolve_project(modules), schemas={("p", None): context}
            )
            self.assertEqual(index.diagnostics, ())
            return index, next(d for d in index.documents if d.call.binding == "q")

        index, document = make()
        output = render_indexed_operation_types(
            context, document, name="Read", fragment_index=index
        )
        self.assertEqual(output.count("ID = str"), 1)
        self.assertIn("ReadFragment_F", output)
        self.assertIn("ReadFragment_C", output)
        self.assertIn("closed=True", output)
        self.assertIn("_graphql_desk_φ", output)
        from graphql import graphql_sync  # pyright: ignore[reportUnknownVariableType]

        executed = graphql_sync(
            schema,
            "\n".join(fact.printed for fact in document.definitions),
            root_value={"user": {"id": "1", "name": "n", "friends": []}},
            variable_values={"flag": False},
        )
        self.assertIsNone(executed.errors)
        self.assertEqual(
            executed.data, {"user": {"id": "1", "name": "n", "friends": []}}
        )
        changed_index, changed_document = make(2)
        changed = render_indexed_operation_types(
            context, changed_document, name="Read", fragment_index=changed_index
        )
        self.assertNotEqual(output, changed)
        for bad in (
            replace(index, documents=()),
            replace(index, documents=index.documents + (index.documents[0],)),
            replace(
                index,
                documents=tuple(
                    replace(d, context_token="old") if d.call.binding == "f" else d
                    for d in index.documents
                ),
            ),
        ):
            with self.assertRaises(ValueError):
                render_indexed_operation_types(
                    context, document, name="Read", fragment_index=bad
                )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "generated.py").write_text(output, encoding="utf8")
            combined_index, combined_document = make(conditional=False)
            (root / "combined.py").write_text(
                render_indexed_operation_types(
                    context,
                    combined_document,
                    name="Combined",
                    fragment_index=combined_index,
                ),
                encoding="utf8",
            )
            fixture = (
                "from typing import cast\n"
                "from generated import ReadResult, ReadFragment_F, ReadFragment_C\n"
                "from typing import Literal\n"
                "from graphql_desk.fragment_refs import FragmentIdentity, FragmentRef\n"
                + "".join(
                    f"{d.primary_fragment.name}Identity = FragmentIdentity[Literal["
                    f"{fragment_identity_key(d, d.primary_fragment)!r}]]\n"
                    for d in index.documents
                    if d.primary_fragment is not None
                )
                + "C = CIdentity\nF = FIdentity\n"
                "def read_f(value: FragmentRef[F]) -> None: pass\n"
                "def read_c(value: FragmentRef[C]) -> None: pass\n"
                "from combined import CombinedResult\n"
                "combined = cast(CombinedResult, {})\n"
                "read_f(combined['user'])\n"
                "read_c(combined['user'])\n"
                "result = cast(ReadResult, {})\n"
                "read_f(result['user'])\n"
                "payload = cast(ReadFragment_F, {})\n"
                "identifier: str = payload['id']\n"
                "read_c(payload)\n"
                "child = cast(ReadFragment_C, {})\n"
                "name: str = child['name']\n"
                "hidden = result['user']['name']  # error\n"
                "read_c(result['user'])  # error\n"
                "read_f(child)  # error\n"
                "hidden_nested = payload['name']  # error\n"
            )
            (root / "fixture.py").write_text(fixture, encoding="utf8")
            expected = {
                i for i, line in enumerate(fixture.splitlines(), 1) if "# error" in line
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
                    str(root),
                ],
                cwd=root,
                capture_output=True,
                text=True,
                encoding="utf8",
            )
            self.assertEqual(mypy.returncode, 1, mypy.stdout + mypy.stderr)
            lines = {
                int(line.split(":")[1])
                for line in mypy.stdout.splitlines()
                if "fixture.py:" in line and ": error:" in line
            }
            self.assertEqual(lines, expected, mypy.stdout + mypy.stderr)
            (root / "pyrightconfig.json").write_text(
                json.dumps(
                    {
                        "include": ["fixture.py", "generated.py", "combined.py"],
                        "typeCheckingMode": "strict",
                        "pythonVersion": "3.10",
                    }
                ),
                encoding="utf8",
            )
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
                encoding="utf8",
            )
            diagnostics = json.loads(pyright.stdout)["generalDiagnostics"]
            self.assertEqual(
                {d["range"]["start"]["line"] + 1 for d in diagnostics},
                expected,
                pyright.stdout + pyright.stderr,
            )

    def test_masked_abstract_unmask_and_cross_consumer_owner_identity(self) -> None:
        schema = build_schema(
            "interface Node { id: ID! } "
            "type User implements Node { id: ID! name: String! } "
            "type Org implements Node { id: ID! } "
            "type Query { nodes: [Node] }"
        )
        context = SchemaContext("main", schema, "v1")
        sources = (
            ModuleSnapshot(
                "p",
                "leaf",
                "file:///leaf.py",
                1,
                'from graphql_desk import gql\nc=gql("fragment C on User { name }")',
            ),
            ModuleSnapshot(
                "p",
                "fragment",
                "file:///fragment.py",
                1,
                "from graphql_desk import gql\nfrom leaf import c\n"
                'f=gql("fragment F on Node @_unmask { id ...C }",[c])',
            ),
            ModuleSnapshot(
                "p",
                "first",
                "file:///first.py",
                1,
                "from graphql_desk import gql\nfrom fragment import f\n"
                'q=gql("query Q {nodes {kind:__typename ...F}}",[f])',
            ),
            ModuleSnapshot(
                "p",
                "second",
                "file:///second.py",
                8,
                "from graphql_desk import gql\nfrom fragment import f\n"
                'other=gql("query Other {nodes {...F}}",[f])',
            ),
        )
        index = index_fragments(
            resolve_project(sources), schemas={("p", None): context}
        )
        self.assertEqual(index.diagnostics, ())
        document = next(d for d in index.documents if d.call.binding == "q")
        other = next(d for d in index.documents if d.call.binding == "other")
        owner = next(d for d in index.documents if d.call.binding == "c")
        assert owner.primary_fragment is not None
        key = fragment_identity_key(owner, owner.primary_fragment)
        output = render_indexed_operation_types(
            context, document, name="Read", fragment_index=index
        )
        second = render_indexed_operation_types(
            context, other, name="Read", fragment_index=index
        )
        self.assertIn(key, output)
        self.assertIn(key, second)
        self.assertIn("'id': 'ID'", output)
        self.assertIn("'nodes': 'list[ReadResult_nodes | None] | None'", output)
        # @_unmask exposes F, but the nested C remains an opaque capability.
        operation_part = output.split("ReadFragment_C =")[0]
        self.assertNotIn("'name':", operation_part)
        self.assertIn("_graphql_desk_φ", operation_part)
        unmasked = render_indexed_operation_types(
            context,
            document,
            name="Read",
            fragment_index=index,
            disable_masking=True,
        )
        self.assertIn("'name': 'String'", unmasked)
        self.assertNotIn("_graphql_desk_φ", unmasked)
        self.assertNotIn("fragment_identity", unmasked)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "generated.py"
            path.write_text(output, encoding="utf8")
            spec = importlib.util.spec_from_file_location("masked_generated", path)
            assert spec is not None and spec.loader is not None
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            self.assertTrue(hasattr(module, "ReadFragment_F"))

    def test_standalone_indexed_fragment_payload_uses_actual_type_condition(
        self,
    ) -> None:
        schema = build_schema(
            "type User { id: ID! name: String! friends: [User] } "
            "type Query { greeting: String! }"
        )
        context = SchemaContext("main", schema, "v1")
        index = index_fragments(
            resolve_project(
                (
                    ModuleSnapshot(
                        "p",
                        "child",
                        "file:///child.py",
                        1,
                        "from graphql_desk import gql\n"
                        'c=gql("fragment C on User { name }")',
                    ),
                    ModuleSnapshot(
                        "p",
                        "main",
                        "file:///main.py",
                        1,
                        "from graphql_desk import gql\nfrom child import c\n"
                        'f=gql("fragment F on User { id ...C friends {...C} }",[c])',
                    ),
                )
            ),
            schemas={("p", None): context},
        )
        self.assertEqual(index.diagnostics, ())
        document = next(d for d in index.documents if d.call.binding == "f")
        with patch.object(
            schema,
            "get_root_type",
            side_effect=AssertionError(
                "Fragment rendering must not acquire an operation root"
            ),
        ):
            output = render_indexed_fragment_types(
                context, document, name="Payload", fragment_index=index
            )
        self.assertIn("PayloadResult = TypedDict", output)
        self.assertIn("'friends': 'list[PayloadResult_friends | None] | None'", output)
        self.assertNotIn("PayloadFragment_F", output)
        self.assertEqual(output.count("ID = str"), 1)
        unmasked = render_indexed_fragment_types(
            context, document, name="Payload", disable_masking=True
        )
        self.assertIn("'name': 'String'", unmasked)
        self.assertNotIn("_graphql_desk_φ", unmasked)
        assert document.primary_fragment is not None
        for wrong in (
            replace(document, primary_fragment=None),
            replace(
                document,
                primary_fragment=replace(
                    document.primary_fragment,
                    document=DocumentIdentity(document.call.module, Span(0, 1)),
                ),
            ),
            replace(document, call=replace(document.call, operation="Q")),
            replace(document, context_token="old"),
        ):
            with self.assertRaises(ValueError):
                render_indexed_fragment_types(
                    context, wrong, name="Payload", disable_masking=True
                )
        child = next(d for d in index.documents if d.call.binding == "c")
        assert child.primary_fragment is not None
        key = fragment_identity_key(child, child.primary_fragment)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "generated.py").write_text(output, encoding="utf8")
            fixture = (
                "from typing import Literal, cast\n"
                "from generated import PayloadResult, PayloadVariables\n"
                "from graphql_desk.fragment_refs import FragmentIdentity, FragmentRef\n"
                f"C = FragmentIdentity[Literal[{key!r}]]\n"
                "def read_c(value: FragmentRef[C]) -> None: pass\n"
                "result = cast(PayloadResult, {})\n"
                "identifier: str = result['id']\n"
                "read_c(result)\n"
                "variables: PayloadVariables = {}\n"
                "for friend in result['friends'] or []:\n"
                "    if friend is not None: read_c(friend)\n"
                "hidden = result['name']  # error\n"
            )
            (root / "fixture.py").write_text(fixture, encoding="utf8")
            mypy = subprocess.run(
                [
                    sys.executable,
                    "-X",
                    "utf8",
                    "-m",
                    "mypy",
                    "--strict",
                    "--no-incremental",
                    str(root),
                ],
                cwd=root,
                capture_output=True,
                text=True,
                encoding="utf8",
            )
            self.assertEqual(mypy.returncode, 1, mypy.stdout + mypy.stderr)
            self.assertIn("Found 1 error in 1 file", mypy.stdout)
            (root / "pyrightconfig.json").write_text(
                json.dumps(
                    {
                        "include": ["fixture.py", "generated.py"],
                        "typeCheckingMode": "strict",
                        "pythonVersion": "3.10",
                    }
                ),
                encoding="utf8",
            )
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
                encoding="utf8",
            )
            diagnostics = json.loads(pyright.stdout)["generalDiagnostics"]
            self.assertEqual(
                {d["range"]["start"]["line"] + 1 for d in diagnostics},
                {12},
                pyright.stdout + pyright.stderr,
            )
            spec = importlib.util.spec_from_file_location(
                "fragment_generated", root / "generated.py"
            )
            assert spec is not None and spec.loader is not None
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            self.assertEqual(module.PayloadVariables.__required_keys__, frozenset())

    def test_standalone_abstract_and_empty_fragment_payloads(self) -> None:
        schema = build_schema(
            "interface Node { id: ID! } interface Empty { id: ID! } "
            "type User implements Node { id: ID! name: String! } "
            "type Org implements Node { id: ID! } type Query { greeting: String }"
        )
        context = SchemaContext("main", schema, "v1")
        index = index_fragments(
            resolve_project(
                (
                    ModuleSnapshot(
                        "p",
                        "main",
                        "file:///main.py",
                        1,
                        "from graphql_desk import gql\n"
                        'f=gql("fragment F on Node {kind:__typename id '
                        '...on User{name}}")\n'
                        'empty=gql("fragment EmptyFields on Empty {id}")',
                    ),
                )
            ),
            schemas={("p", None): context},
        )
        self.assertEqual(index.diagnostics, ())
        document = next(d for d in index.documents if d.call.binding == "f")
        output = render_indexed_fragment_types(context, document, name="Payload")
        self.assertIn("PayloadResult = __graphql_desk_variant_0 |", output)
        self.assertIn("'kind': \"Literal['User']\"", output)
        self.assertIn("'kind': \"Literal['Org']\"", output)
        empty = next(d for d in index.documents if d.call.binding == "empty")
        self.assertIn(
            "PayloadResult = __graphql_desk_Never",
            render_indexed_fragment_types(context, empty, name="Payload"),
        )

    def test_indexed_leaf_and_fragment_free_modules_have_no_unused_private_imports(
        self,
    ) -> None:
        schema = build_schema("type Query { greeting: String! }")
        context = SchemaContext("main", schema, "v1")
        snapshot = ModuleSnapshot(
            "p",
            "main",
            "file:///main.py",
            1,
            "from graphql_desk import gql\n"
            'f=gql("fragment F on Query {greeting}")\n'
            'q=gql("query Q {greeting}")',
        )
        index = index_fragments(
            resolve_project((snapshot,)), schemas={("p", None): context}
        )
        self.assertEqual(index.diagnostics, ())
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for document in index.documents:
                renderer = (
                    render_indexed_fragment_types
                    if document.primary_fragment
                    else render_indexed_operation_types
                )
                output = renderer(context, document, name="Read", fragment_index=index)
                self.assertNotIn("Callable as __graphql_desk_Callable", output)
                self.assertNotIn("ReadOnly as __graphql_desk_ReadOnly", output)
                if document.primary_fragment is None:
                    self.assertNotIn("FragmentIdentity", output)
                (root / f"{document.call.binding}.py").write_text(
                    output, encoding="utf8"
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
            checked = subprocess.run(
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
            self.assertEqual(checked.returncode, 0, checked.stdout + checked.stderr)

    def test_indexed_named_fragments_use_actual_closure_scope_and_selected_operation(
        self,
    ) -> None:
        schema = build_schema(
            "scalar Date interface Node { id: ID! label: String when: Date } "
            "type User implements Node { "
            "id: ID! label: String! when: Date! name: String! } "
            "type Org implements Node { id: ID! label: String when: Date } "
            "type Query { nodes: [Node] greeting: String! }"
        )
        context = SchemaContext("main", schema, "generation-1")

        def snapshot(name: str, source: str) -> ModuleSnapshot:
            return ModuleSnapshot("p", name, f"file:///{name}.py", 7, source)

        sources = resolve_project(
            (
                snapshot(
                    "leaf",
                    "from graphql_desk import gql\n"
                    'f=gql("fragment Leaf on Node { id label when }")',
                ),
                snapshot(
                    "left",
                    "from graphql_desk import gql\nfrom leaf import f as leaf\n"
                    'f=gql("fragment Left on Node { ...Leaf }",[leaf])',
                ),
                snapshot(
                    "right",
                    "from graphql_desk import gql\nfrom leaf import f as leaf\n"
                    'f=gql("fragment Right on User { ...Leaf name }",[leaf])',
                ),
                snapshot(
                    "bridge", "from left import f as left\nfrom right import f as right"
                ),
                snapshot(
                    "main",
                    "from graphql_desk import gql\nfrom bridge import left,right\n"
                    'doc=gql("query Other { greeting } query Read($flag:Boolean!){'
                    'nodes{kind:__typename ...Left ...Right @include(if:$flag)}}",'
                    '[left,right,left],operation="Read")',
                ),
            )
        )
        self.assertEqual(sources.diagnostics, ())
        indexed = index_fragments(sources, schemas={("p", None): context})
        self.assertEqual(indexed.diagnostics, ())
        document = next(
            item for item in indexed.documents if item.call.binding == "doc"
        )
        mappings = (ScalarMapping("Date", "builtins.str", "datetime.datetime"),)
        with self.assertRaisesRegex(ValueError, "Masked fragment spreads"):
            render_indexed_operation_types(
                context, document, name="Read", scalars=mappings
            )
        from graphql import parse

        with patch("graphql_desk.operation_types.parse", wraps=parse) as parser:
            output = render_indexed_operation_types(
                context, document, name="Read", scalars=mappings, disable_masking=True
            )
        self.assertEqual(parser.call_count, 1)
        self.assertIn("'nodes': 'list[ReadResult_nodes | None] | None'", output)
        self.assertIn("'label': 'String | None'", output)
        self.assertIn("'when': 'DateOutput | None'", output)
        self.assertIn("'kind': \"Literal['User']\"", output)
        self.assertIn("closed=True", output)
        self.assertNotIn("'greeting':", output)
        from graphql import graphql_sync  # pyright: ignore[reportUnknownVariableType]

        for flag in (False, True):
            result = graphql_sync(
                schema,
                "\n\n".join(fact.printed for fact in document.definitions),
                root_value={
                    "nodes": [
                        None,
                        {
                            "__typename": "User",
                            "id": "1",
                            "label": "x",
                            "when": "unchanged",
                            "name": "n",
                        },
                    ]
                },
                variable_values={"flag": flag},
                operation_name="Read",
            )
            self.assertIsNone(result.errors)
            expected = {"kind": "User", "id": "1", "label": "x", "when": "unchanged"}
            if flag:
                expected["name"] = "n"
            self.assertEqual(result.data, {"nodes": [None, expected]})
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "generated.py").write_text(output, encoding="utf-8")
            (root / "fixture.py").write_text(
                "from datetime import datetime\nfrom generated import ReadResult\n"
                "base: ReadResult = {'nodes':[None,{'kind':'User','id':'1',"
                "'label':None,'when':None}]}\n"
                "full: ReadResult = {'nodes':[{'kind':'User','id':'1','label':'x',"
                "'when':datetime(2020,1,1),'name':'x'}]}\n"
                "org: ReadResult = {'nodes':[{'kind':'Org','id':'1',"
                "'label':None,'when':None}]}\n"
                "bad: ReadResult = {'nodes':[{'kind':'Org','id':'1','label':None,"
                "'when':None,'name':'x'}]}\n"
                "bad_date: ReadResult = {'nodes':[{'kind':'User','id':'1',"
                "'label':None,'when':'x'}]}\n",
                encoding="utf-8",
            )
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
            self.assertIn("Found 2 errors in 1 file", mypy.stdout)
            (root / "pyrightconfig.json").write_text(
                json.dumps(
                    {
                        "include": ["fixture.py", "generated.py"],
                        "typeCheckingMode": "strict",
                        "pythonVersion": "3.10",
                    }
                ),
                encoding="utf-8",
            )
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
            self.assertEqual(len(diagnostics), 2, diagnostics)
            self.assertEqual(
                {item["range"]["start"]["line"] for item in diagnostics}, {5, 6}
            )

    def test_indexed_adapter_rejects_context_and_selected_identity_mismatches(
        self,
    ) -> None:
        schema = build_schema(_SCHEMA)
        context = SchemaContext("main", schema, "generation-1")
        source = ModuleSnapshot(
            "p",
            "main",
            "file:///main.py",
            1,
            "from graphql_desk import gql\n"
            'doc=gql("query One {greeting} query Two {alias:greeting}",'
            'operation="Two")',
        )
        indexed = index_fragments(
            resolve_project((source,)), schemas={("p", None): context}
        )
        self.assertEqual(indexed.diagnostics, ())
        document = indexed.documents[0]
        selected = document.selected_operation
        assert selected is not None
        output = render_indexed_operation_types(context, document, name="Read")
        self.assertIn("'alias': 'String'", output)
        self.assertNotIn("'greeting':", output)
        for wrong_context in (
            replace(context, schema_name="other"),
            replace(context, token="old"),
        ):
            with self.subTest(context=wrong_context), self.assertRaises(ValueError):
                render_indexed_operation_types(wrong_context, document, name="Read")
        foreign = replace(
            selected, document=DocumentIdentity(document.call.module, Span(0, 1))
        )
        for wrong in (
            replace(document, selected_operation=None),
            replace(
                document,
                definitions=tuple(f for f in document.definitions if f != selected),
            ),
            replace(document, selected_operation=foreign, definitions=(foreign,)),
            replace(document, schema_identity=("other", "main")),
            replace(document, call=replace(document.call, schema_name="other")),
            replace(document, call=replace(document.call, operation="One")),
        ):
            with self.subTest(document=wrong), self.assertRaises(ValueError):
                render_indexed_operation_types(context, wrong, name="Read")

    def test_named_registry_validation_conditional_definitions_and_diamond(
        self,
    ) -> None:
        schema = build_schema(_SCHEMA)
        registry = "{greeting} fragment Unused on User {id}"
        self.assertIn(
            "'greeting': 'String'",
            render_operation_types(schema, registry, name="Read"),
        )
        for source in (
            '{user(id:"1"){...Missing}}',
            '{user(id:"1"){...F}} fragment F on User {...F}',
            '{user(id:"1"){...F}} fragment F on User {missing}',
            "{greeting} fragment Unused on User {missing}",
            '{user(id:"1"){...F}} fragment F on Query {greeting}',
            "{greeting} type Extra {value:String}",
        ):
            with self.subTest(source=source), self.assertRaises(ValueError):
                render_operation_types(
                    schema, source, name="Read", disable_masking=True
                )
        conditional_schema = build_schema(
            "directive @defer on FRAGMENT_DEFINITION | FRAGMENT_SPREAD " + _SCHEMA
        )
        conditional = render_operation_types(
            conditional_schema,
            '{user(id:"1"){...F}} fragment F on User @defer {id name}',
            name="Read",
            disable_masking=True,
        )
        self.assertIn("closed=True", conditional)
        self.assertIn(
            "closed=True",
            render_operation_types(
                conditional_schema,
                '{user(id:"1"){...F @defer}} fragment F on User {id name}',
                name="Read",
                disable_masking=True,
            ),
        )
        fragments = ["fragment F0 on User {id}"]
        for i in range(1, 19):
            fragments.append(f"fragment F{i} on User {{...F{i - 1} ...F{i - 1}}}")
        output = render_operation_types(
            schema,
            '{user(id:"1"){...F18}} ' + " ".join(fragments),
            name="Read",
            disable_masking=True,
        )
        self.assertEqual(output.count("'id': 'ID'"), 1)

    def test_conditional_blocks_and_repeated_composites_match_execution_and_checkers(
        self,
    ) -> None:
        from graphql import graphql_sync  # pyright: ignore[reportUnknownVariableType]

        schema = build_schema(
            "type User { id: ID! name: String! title: String! } "
            "type Query { user: User! }"
        )
        for source, expected_mypy_errors in (
            (
                "query($flag:Boolean!) { user { id "
                "... @include(if:$flag) { name title } } }",
                2,
            ),
            (
                "query($flag:Boolean!) { user { id } "
                "user @include(if:$flag) { name title } }",
                4,
            ),
        ):
            with (
                self.subTest(source=source),
                tempfile.TemporaryDirectory() as directory,
            ):
                for flag in (False, True):
                    result = graphql_sync(
                        schema,
                        source,
                        root_value={"user": {"id": "1", "name": "x", "title": "y"}},
                        variable_values={"flag": flag},
                    )
                    self.assertIsNone(result.errors)
                    self.assertEqual(
                        result.data,
                        {"user": {"id": "1", "name": "x", "title": "y"}}
                        if flag
                        else {"user": {"id": "1"}},
                    )
                output = render_operation_types(schema, source, name="Read")
                self.assertIn("closed=True", output)
                root = Path(directory)
                (root / "generated.py").write_text(output, encoding="utf-8")
                (root / "fixture.py").write_text(
                    "from typing_extensions import TypedDict\n"
                    "from generated import ReadResult\n"
                    "base: ReadResult = {'user': {'id':'1'}}\n"
                    "full: ReadResult = {'user': {'id':'1','name':'x','title':'y'}}\n"
                    "partial: ReadResult = {'user': {'id':'1','name':'x'}}\n"
                    "class Wide(TypedDict):\n    id: str\n    name: str\n"
                    "wider: Wide = {'id':'1','name':'x'}\n"
                    "partial_wider: ReadResult = {'user': wider}\n"
                    "def narrow(value: ReadResult) -> str:\n"
                    "    user = value['user']\n"
                    "    if 'name' in user:\n        return user['title']\n"
                    "    return user['id']\n",
                    encoding="utf-8",
                )
                runtime = subprocess.run(
                    [sys.executable, "-c", "import generated"],
                    cwd=root,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(runtime.returncode, 0, runtime.stderr)
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
                self.assertIn(
                    f"Found {expected_mypy_errors} errors in 1 file", mypy.stdout
                )
                (root / "pyrightconfig.json").write_text(
                    json.dumps(
                        {
                            "include": ["fixture.py", "generated.py"],
                            "pythonVersion": "3.10",
                            "typeCheckingMode": "strict",
                        }
                    ),
                    encoding="utf-8",
                )
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
                self.assertEqual(len(diagnostics), 2, diagnostics)
                self.assertEqual(
                    {item["range"]["start"]["line"] for item in diagnostics}, {4, 9}
                )

    def test_nested_conditions_abstract_scopes_aliases_and_wrappers(self) -> None:
        from graphql import graphql_sync  # pyright: ignore[reportUnknownVariableType]

        sdl = (
            "interface Node { id: ID! label: String } "
            "type User implements Node { id: ID! label: String! name: String! } "
            "type Query { nodes: [Node] }"
        )
        schema = build_schema(sdl)
        source = (
            "query($a:Boolean!,$b:Boolean!){ nodes { kind:__typename id label "
            "... on User @include(if:$a) { name ... @skip(if:$b) { strong:label } } } }"
        )
        output = render_operation_types(schema, source, name="Read")
        self.assertIn("'nodes': 'list[ReadResult_nodes | None] | None'", output)
        self.assertIn("'label': 'String | None'", output)
        self.assertIn("'strong': 'String'", output)
        self.assertIn("'kind': \"Literal['User']\"", output)
        for a, b in ((False, False), (False, True), (True, False), (True, True)):
            result = graphql_sync(
                schema,
                source,
                root_value={
                    "nodes": [
                        None,
                        {"__typename": "User", "id": "1", "label": "x", "name": "n"},
                    ]
                },
                variable_values={"a": a, "b": b},
            )
            self.assertIsNone(result.errors)
            expected = {"kind": "User", "id": "1", "label": "x"}
            if a:
                expected["name"] = "n"
                if not b:
                    expected["strong"] = "x"
            self.assertEqual(result.data, {"nodes": [None, expected]})
        alias = render_operation_types(
            schema,
            "{ nodes { kind:__typename ... @include(if:false) "
            "{ __typename:id label } } }",
            name="Alias",
        )
        self.assertIn("'__typename': 'ID'", alias)
        self.assertNotIn("'__typename': NotRequired", alias)
        self.assertIn(
            "closed=True",
            render_operation_types(
                build_schema(
                    "directive @defer(if: Boolean = true) on INLINE_FRAGMENT " + sdl
                ),
                "{ nodes { ... @defer { id label } } }",
                name="Deferred",
            ),
        )

    def test_conditional_shape_bounds_are_early_deduplicated_and_atomic(self) -> None:
        schema = build_schema("type Query { value: String }")
        blocks = [f"... @include(if:false) {{ f{i}:value }}" for i in range(7)]
        six = render_operation_types(
            schema, "{" + " ".join(blocks[:6]) + "}", name="Read"
        )
        self.assertEqual(six.count("closed=True"), 64)
        with self.assertRaisesRegex(ValueError, "64 presence alternatives"):
            render_operation_types(schema, "{" + " ".join(blocks) + "}", name="Read")
        abstract = build_schema(
            "interface Item { value: String } type A implements Item { value: String } "
            "type B implements Item { value: String } type Query { item: Item }"
        )
        with self.assertRaisesRegex(ValueError, "64 presence alternatives"):
            render_operation_types(
                abstract, "{ item {" + " ".join(blocks[:6]) + "} }", name="Read"
            )
        duplicate = render_operation_types(
            schema, "{" + " ".join([blocks[0]] * 100) + "}", name="Read"
        )
        self.assertEqual(duplicate.count("closed=True"), 2)
        nested = build_schema("type Item { value: String } type Query { item: Item }")
        children = " ".join(blocks[:6])
        four = " ".join(f"a{i}:item {{ {children} }}" for i in range(4))
        self.assertEqual(
            render_operation_types(nested, "{" + four + "}", name="Read").count(
                "closed=True"
            ),
            256,
        )
        with self.assertRaisesRegex(
            ValueError, "256 conditional presence alternatives"
        ):
            render_operation_types(
                nested, "{" + four + f" a4:item {{ {children} }}" + "}", name="Read"
            )
        self.assertIn(
            "ReadResult =", render_operation_types(schema, "{ value }", name="Read")
        )

    def test_conditional_occurrences_are_independent_and_preserve_guarantees(
        self,
    ) -> None:
        schema = build_schema(
            "type User { id: ID! name: String! } "
            "type Query { user: User! value: String! }"
        )
        output = render_operation_types(
            schema,
            "query($flag:Boolean!){ ... @include(if:$flag){ a:value } "
            "... @include(if:$flag){ b:value } }",
            name="Read",
        )
        self.assertEqual(output.count("closed=True"), 4)
        repeated = render_operation_types(
            schema,
            "{ user @include(if:false) { id } user @include(if:true) { name } }",
            name="Read",
        )
        self.assertEqual(repeated.count("'user':"), 3)
        self.assertNotIn("NotRequired[", repeated)
        guaranteed = render_operation_types(
            schema,
            "{ value ... @include(if:false) { value other:value } }",
            name="Read",
        )
        self.assertEqual(guaranteed.count("'value': 'String'"), 2)
        self.assertNotIn("NotRequired['String']", guaranteed)

    def test_field_directives_preserve_outer_nullability_and_presence(self) -> None:
        schema = build_schema(
            "directive @defer(if: Boolean = true) on FIELD "
            "directive @stream(initialCount: Int = 0) on FIELD "
            "type Query { value: String! maybe: String values: [String!]! }"
        )
        original_directives = schema.directives
        output = render_operation_types(
            schema,
            "{ skipped:value @skip(if:false) included:value @include(if:true) "
            "deferred:value @defer streamed:values @stream "
            "nullable:value @optional needed:maybe @_required "
            "both:maybe @optional @required items:values @_optional "
            "kind:__typename @include(if:false) fixed:__typename @optional }",
            name="Read",
        )
        for expected in (
            "'skipped': NotRequired['String']",
            "'included': NotRequired['String']",
            "'deferred': NotRequired['String']",
            "'streamed': 'list[String]'",
            "'nullable': 'String | None'",
            "'needed': 'String'",
            "'both': 'String'",
            "'items': 'list[String] | None'",
            "'kind': NotRequired[\"Literal['Query']\"]",
            "'fixed': \"Literal['Query']\"",
        ):
            self.assertIn(expected, output)
        self.assertIs(schema.directives, original_directives)

    def test_directive_validation_only_suppresses_allowed_unknown_names(self) -> None:
        schema = build_schema(
            "directive @required(count: Int!) on FIELD "
            "directive @custom(if: Boolean!) on FIELD type Query { value: String }"
        )
        for source, clients in (
            ("{ value @required }", ()),
            ("{ value @custom(if: 1) }", ("custom",)),
            ("query @custom(if: true) { value }", ("custom",)),
            ("{ value @skip }", ()),
            ("{ value @include(if: 1) }", ()),
            ("query($flag: Int!) { value @skip(if: $flag) }", ()),
            ("{ value @unknown }", ()),
            ("{ value @stream }", ()),
            ("{ value @skip(if: true) @skip(if: false) }", ()),
        ):
            with self.subTest(source=source), self.assertRaises(ValueError):
                render_operation_types(
                    schema, source, name="Read", client_directives=clients
                )
        output = render_operation_types(
            schema,
            "{ value @configured }",
            name="Read",
            client_directives=("configured",),
        )
        self.assertIn("'value': 'String | None'", output)
        for client in ("@bad", "bad-name", "", "café"):
            with self.subTest(client=client), self.assertRaises(ValueError):
                render_operation_types(
                    schema, "{ value }", name="Read", client_directives=(client,)
                )

    def test_duplicate_field_directives_have_order_independent_safe_policy(
        self,
    ) -> None:
        schema = build_schema(
            "type User { id: ID! name: String! } "
            "type Query { user: User value: String }"
        )
        for source in (
            "{ value @include(if:false) value }",
            "{ value value @include(if:false) }",
        ):
            output = render_operation_types(schema, source, name="Read")
            self.assertIn("'value': 'String | None'", output)
            self.assertNotIn("NotRequired['String", output)
        for source in (
            "{ value @required value @optional }",
            "{ value value @required }",
            "{ value @required value }",
        ):
            with self.subTest(source=source):
                self.assertIn(
                    "'value': 'String'",
                    render_operation_types(schema, source, name="Read"),
                )
        self.assertIn(
            "\"Literal['Query']\"",
            render_operation_types(
                schema,
                "{ kind:__typename @required kind:__typename @optional }",
                name="Read",
            ),
        )
        with self.assertRaises(ValueError):
            render_operation_types(schema, "{ ... @defer { value } }", name="Read")
        self.assertIn(
            "'user': NotRequired['ReadResult_user | None']",
            render_operation_types(
                schema, "{ user @include(if:true) { id } }", name="Read"
            ),
        )

    def test_conditional_execution_and_generated_checker_contracts(self) -> None:
        from graphql import graphql_sync  # pyright: ignore[reportUnknownVariableType]

        schema = build_schema(
            "type Query { value: String! maybe: String values: [String!]! }"
        )
        source = (
            "query($flag:Boolean!){ a:value @include(if:$flag) "
            "b:maybe @required c:values @optional }"
        )
        for flag in (False, True):
            result = graphql_sync(
                schema,
                "query($flag:Boolean!){ a:value @include(if:$flag) }",
                root_value={"value": "x"},
                variable_values={"flag": flag},
            )
            self.assertIsNone(result.errors)
            self.assertEqual(result.data, {"a": "x"} if flag else {})
        output = render_operation_types(schema, source, name="Read")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "generated.py").write_text(output, encoding="utf-8")
            runtime = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "from generated import ReadResult; "
                    "assert ReadResult.__required_keys__ == {'b','c'}; "
                    "assert ReadResult.__optional_keys__ == {'a'}",
                ],
                cwd=root,
                capture_output=True,
                text=True,
            )
            self.assertEqual(runtime.returncode, 0, runtime.stderr)
            (root / "fixture.py").write_text(
                "from generated import ReadResult\n"
                "good: ReadResult = {'b':'x','c':None}\n"
                "present: ReadResult = {'a':'x','b':'x','c':['x']}\n"
                "bad_optional: ReadResult = {'a':None,'b':'x','c':[]}\n"
                "bad_required: ReadResult = {'b':None,'c':[]}\n"
                "bad_item: ReadResult = {'b':'x','c':[None]}\n"
                "def guarded(value: ReadResult) -> str:\n"
                "    return value['a'] if 'a' in value else 'missing'\n"
                "def unguarded(value: ReadResult) -> str:\n"
                "    return value['a']\n",
                encoding="utf-8",
            )
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
            self.assertIn("Found 3 errors in 1 file", mypy.stdout)
            (root / "pyrightconfig.json").write_text(
                json.dumps(
                    {
                        "include": ["fixture.py", "generated.py"],
                        "typeCheckingMode": "strict",
                        "pythonVersion": "3.10",
                        "reportTypedDictNotRequiredAccess": "error",
                    }
                ),
                encoding="utf-8",
            )
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
            self.assertEqual(len(diagnostics), 4, diagnostics)
            self.assertEqual(
                {item["range"]["start"]["line"] for item in diagnostics}, {3, 4, 5, 9}
            )

    def test_abstract_variants_preserve_selection_scope_and_discriminators(
        self,
    ) -> None:
        schema = build_schema(
            "interface Node { id: ID! label: String next: Node } "
            "type User implements Node { "
            "id: ID! label: String! next: User name: String! } "
            "type Org implements Node { "
            "id: ID! label: String next: Node title: String! } "
            "union Search = User | Org "
            "type Query { node: Node search: [Search!]! }"
        )
        output = render_operation_types(
            schema,
            "{ node { kind: __typename id label ... on User { strong: label name } "
            "... on Org { title } next { __typename id } } "
            "search { ... on Node { id } ... on User { ... { name } } } }",
            name="Read",
        )
        self.assertIn("'kind': \"Literal['User']\"", output)
        self.assertIn("'kind': \"Literal['Org']\"", output)
        self.assertIn("'__typename': NotRequired[\"Literal['User']\"]", output)
        self.assertIn("'label': 'String | None'", output)
        self.assertIn("'strong': 'String'", output)
        self.assertIn("'search': 'list[ReadResult_search]'", output)
        self.assertIn("'__typename': \"Literal['User']\"", output)
        self.assertIn("'name': 'String'", output)
        self.assertEqual(
            render_operation_types(schema, "{ node { ... { id } } }", name="Read"),
            render_operation_types(schema, "{ node { id } }", name="Read"),
        )
        merged = render_operation_types(
            schema,
            "{ node { next { id } ... on User { next { name } } } }",
            name="Read",
        )
        self.assertIn("'id': 'ID',\n    'name': 'String',", merged)
        for source in (
            "{ node { label ... on User { label } } }",
            "{ node { ... on Query { node { id } } } }",
        ):
            with self.subTest(source=source), self.assertRaises(ValueError):
                render_operation_types(schema, source, name="Read")

    def test_abstract_runtime_metadata_does_not_add_response_keys(self) -> None:
        from graphql import graphql_sync  # pyright: ignore[reportUnknownVariableType]

        schema = build_schema(
            "interface Node { id: ID! } type User implements Node { id: ID! } "
            "type Query { node: Node! }"
        )
        source = "{ node { kind: __typename id } }"
        result = graphql_sync(
            schema, source, root_value={"node": {"__typename": "User", "id": "1"}}
        )
        self.assertIsNone(result.errors)
        self.assertEqual(result.data, {"node": {"kind": "User", "id": "1"}})
        alias_source = "{ node { __typename: id kind: __typename } }"
        alias_result = graphql_sync(
            schema,
            alias_source,
            root_value={"node": {"__typename": "User", "id": "1"}},
        )
        self.assertIsNone(alias_result.errors)
        self.assertEqual(
            alias_result.data, {"node": {"__typename": "1", "kind": "User"}}
        )
        alias_output = render_operation_types(schema, alias_source, name="Alias")
        self.assertIn("'__typename': 'ID'", alias_output)
        self.assertNotIn("NotRequired[", alias_output)
        output = render_operation_types(schema, source, name="Read")
        with tempfile.TemporaryDirectory() as directory:
            generated = Path(directory) / "generated.py"
            generated.write_text(output, encoding="utf-8")
            spec = importlib.util.spec_from_file_location("generated", generated)
            assert spec is not None and spec.loader is not None
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            branch = module.ReadResult_node
            self.assertEqual(branch.__required_keys__, {"kind", "id"})
            self.assertEqual(branch.__optional_keys__, {"__typename"})

    def test_abstract_checker_narrowing_and_uninhabited_outputs(self) -> None:
        schema = build_schema(
            "interface Node { id: ID! } "
            "type User implements Node { id: ID! name: String! } "
            "type Org implements Node { id: ID! title: String! } "
            "interface Empty { id: ID! } enum Never { VALUE } "
            "type Query { node: Node! empty: Empty empties: [Empty!]! }"
        )
        output = render_operation_types(
            schema,
            "{ node { kind: __typename ... on User { name } ... on Org { title } } "
            "empty { id } empties { id } }",
            name="Read",
        )
        self.assertIn("Never as __graphql_desk_Never", output)
        self.assertIn("ReadResult_empty = __graphql_desk_Never", output)
        self.assertIn("'empty': 'ReadResult_empty | None'", output)
        self.assertIn("'empties': 'list[ReadResult_empties]'", output)
        fixture = (
            "from generated import ReadResult\n"
            "def narrow(result: ReadResult) -> str:\n"
            "    node = result['node']\n"
            "    if node['kind'] == 'User':\n"
            "        return node['name']\n"
            "    return node['title']\n"
            "good: ReadResult = {'node': {'kind': 'User', 'name': 'x'}, "
            "'empty': None, 'empties': []}\n"
            "bad_branch: ReadResult = {'node': {'kind': 'User', 'title': 'x'}, "
            "'empty': None, 'empties': []}\n"
            "bad_empty: ReadResult = {'node': {'kind': 'Org', 'title': 'x'}, "
            "'empty': {}, 'empties': []}\n"
            "bad_list: ReadResult = {'node': {'kind': 'Org', 'title': 'x'}, "
            "'empty': None, 'empties': [{}]}\n"
            "bad_null: ReadResult = {'node': None, 'empty': None, 'empties': []}\n"
            "def aid(result: ReadResult) -> str:\n"
            "    node = result['node']\n"
            "    if '__typename' in node and node['__typename'] == 'User':\n"
            "        return node['name']\n"
            "    return 'other'\n"
            "from aliases import AliasResult\n"
            "alias: AliasResult = {'node': {'kind': 'User', '__typename': '123'}}\n"
            "bad_alias: AliasResult = {'node': {'kind': 'User', '__typename': 1}}\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "generated.py").write_text(output, encoding="utf-8")
            (root / "aliases.py").write_text(
                render_operation_types(
                    schema,
                    "{ node { __typename: id kind: __typename } }",
                    name="Alias",
                ),
                encoding="utf-8",
            )
            (root / "fixture.py").write_text(fixture, encoding="utf-8")
            runtime = subprocess.run(
                [sys.executable, "-c", "import generated"],
                cwd=root,
                capture_output=True,
                text=True,
            )
            self.assertEqual(runtime.returncode, 0, runtime.stderr)
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
            self.assertIn("Found 6 errors in 1 file", mypy.stdout)
            (root / "pyrightconfig.json").write_text(
                json.dumps(
                    {
                        "include": ["fixture.py", "generated.py", "aliases.py"],
                        "pythonVersion": "3.10",
                        "typeCheckingMode": "strict",
                    }
                ),
                encoding="utf-8",
            )
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
            self.assertEqual(len(diagnostics), 5, diagnostics)
            self.assertEqual(
                {item["range"]["start"]["line"] for item in diagnostics},
                {7, 8, 9, 10, 18},
            )

    def test_abstract_mapped_outputs_and_nullable_list_wrappers(self) -> None:
        schema = build_schema(
            "scalar Date interface Node { when: Date } "
            "type User implements Node { when: Date } "
            "interface Empty { id: ID! } "
            "type Query { nodes: [Node] empty: [Empty] }"
        )
        output = render_operation_types(
            schema,
            "{ nodes { when } empty { id } }",
            name="Read",
            scalars=(ScalarMapping("Date", "builtins.str", "datetime.datetime"),),
        )
        self.assertIn("'when': 'DateOutput | None'", output)
        self.assertIn("'nodes': 'list[ReadResult_nodes | None] | None'", output)
        self.assertIn("'empty': 'list[ReadResult_empty | None] | None'", output)

    def test_one_of_operation_defaults_and_graphql_literal_validation(self) -> None:
        schema = build_schema(
            "input Choice @oneOf { text: String count: Int } "
            "type Query { f(choice: Choice): Int }"
        )
        for source in (
            '{ f(choice: {text: "x"}) }',
            "{ f(choice: null) }",
            "query($choice: Choice) { f(choice: $choice) }",
            "query($text: String!) { f(choice: {text: $text}) }",
            'query($choice: Choice = {text: "x"}) { f(choice: $choice) }',
            'query($choice: Choice! = {text: "x"}) { f(choice: $choice) }',
        ):
            with self.subTest(source=source):
                output = render_operation_types(schema, source, name="Run")
                self.assertIn("RunResult =", output)
        self.assertIn(
            "'choice': NotRequired['Choice']",
            render_operation_types(
                schema,
                'query($choice: Choice! = {text: "x"}) { f(choice: $choice) }',
                name="Run",
            ),
        )
        for source in (
            "{ f(choice: {}) }",
            '{ f(choice: {text: "x", count: 1}) }',
            "{ f(choice: {text: null}) }",
            '{ f(choice: {unknown: "x"}) }',
            "query($choice: Choice = {}) { f(choice: $choice) }",
            "query($text: String) { f(choice: {text: $text}) }",
        ):
            with self.subTest(source=source), self.assertRaises(ValueError):
                render_operation_types(schema, source, name="Run")

    def test_mapped_types_runtime_and_checker_boundaries(self) -> None:
        schema = build_schema(
            "scalar Date scalar Loose scalar class scalar Literal scalar list "
            "enum Color { RED BLUE } "
            "input Filter { when: Date! nested: [Filter!] color: Color dates: [Date] } "
            "type Query { today(when: Date!, filter: Filter): Date! "
            "color: Color! all: [Date!]! }"
        )
        source = (
            "query Work($when: Date!, $filter: Filter) { "
            "today(when: $when, filter: $filter) color all }"
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            marker = root / "imported.txt"
            (root / "domain.py").write_text(
                "from enum import Enum\nfrom pathlib import Path\n"
                "Path('imported.txt').write_text('executed')\n"
                "class Color(Enum):\n RED = 'RED'\n BLUE = 'BLUE'\n"
            )
            output = render_operation_types(
                schema,
                source,
                name="Mapped",
                scalars=(
                    ScalarMapping("Date", "builtins.str", "datetime.datetime"),
                    ScalarMapping("Loose", "typing.Any", "typing.Any"),
                    ScalarMapping("class", "builtins.str", "builtins.str"),
                    ScalarMapping("Literal", "builtins.str", "builtins.str"),
                    ScalarMapping("list", "builtins.str", "builtins.str"),
                ),
                enums=(("Color", "domain.Color"),),
            )
            self.assertFalse(
                marker.exists(), "Rendering must not execute configured modules"
            )
            self.assertIn("'when': 'DateInput'", output)
            self.assertIn("'today': 'DateOutput'", output)
            self.assertIn("'all': 'list[DateOutput]'", output)
            self.assertIn(
                "'dates': NotRequired['list[DateInput | None] | None']", output
            )
            (root / "generated.py").write_text(output)
            runtime = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "import generated; from datetime import datetime; "
                    "assert generated.DateInput is str; "
                    "assert generated.DateOutput is datetime; "
                    "assert generated.classInput is str; "
                    "assert generated.LiteralOutput is str; "
                    "assert generated.listOutput is str; "
                    "assert generated.Color.RED.value == 'RED'",
                ],
                cwd=root,
                capture_output=True,
                text=True,
            )
            self.assertEqual(runtime.returncode, 0, runtime.stderr)
            self.assertTrue(
                marker.exists(),
                "Only importing generated declarations executes their imports",
            )
            (root / "fixture.py").write_text(
                "from datetime import datetime\n"
                "from generated import MappedVariables, MappedResult, Color\n"
                "good_vars: MappedVariables = {'when': 'x', "
                "'filter': {'when': 'x', 'color': Color.RED}}\n"
                "good_result: MappedResult = {'today': datetime(2020, 1, 1), "
                "'color': Color.RED, 'all': []}\n"
                "bad_vars: MappedVariables = {'when': datetime(2020, 1, 1)}\n"
                "bad_result: MappedResult = {'today': 'x', "
                "'color': Color.RED, 'all': []}\n"
                "bad_enum: MappedVariables = {'when': 'x', "
                "'filter': {'when': 'x', 'color': 'RED'}}\n"
                "bad_null: MappedVariables = {'when': None}\n"
            )
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
            self.assertIn("Found 4 errors in 1 file", mypy.stdout)
            (root / "pyrightconfig.json").write_text(
                json.dumps(
                    {
                        "include": ["fixture.py", "generated.py", "domain.py"],
                        "pythonVersion": "3.10",
                        "typeCheckingMode": "strict",
                    }
                )
            )
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
            self.assertEqual(len(diagnostics), 4, diagnostics)
            self.assertEqual(
                {item["range"]["start"]["line"] for item in diagnostics}, {4, 5, 6, 7}
            )

    def test_nested_operation_declaration_cannot_overwrite_mapping_alias(self) -> None:
        schema = build_schema(
            "scalar XResult_ type Child { id: ID! } "
            "type Query { child(v: XResult_!): Child! }"
        )
        with self.assertRaisesRegex(ValueError, "Colliding"):
            render_operation_types(
                schema,
                "query($v: XResult_!) { Output: child(v: $v) { id } }",
                name="X",
                scalars=(ScalarMapping("XResult_", "builtins.str", "builtins.str"),),
            )

    def test_wrappers_aliases_merges_and_variable_defaults(self) -> None:
        output = render_operation_types(build_schema(_SCHEMA), _SOURCE, name="Book")
        for expected in (
            "'user': 'BookResult_user | None'",
            "'key': 'ID'",
            "'name': 'String | None'",
            "'color': 'Color'",
            "\"Literal['User']\"",
            "'friends': 'list[BookResult_user_friends]'",
            "'a': 'list[String | None] | None'",
            "'b': 'list[String] | None'",
            "'c': 'list[String | None]'",
            "'d': 'list[String]'",
            "'limit': NotRequired['Int']",
            "'filter': NotRequired['Filter | None']",
        ):
            with self.subTest(expected=expected):
                self.assertIn(expected, output)
        self.assertEqual(output.count("BookResult_user_friends ="), 1)
        self.assertIn("'id': 'ID'", output)

    def test_query_mutation_subscription_and_operation_selection(self) -> None:
        schema = build_schema(_SCHEMA)
        for source, expected in (
            ("{ greeting }", "'greeting': 'String'"),
            ("mutation { update { id } }", "'update': 'RunResult_update'"),
            ("subscription { events { id } }", "'events': 'list[RunResult_events]'"),
        ):
            with self.subTest(source=source):
                self.assertIn(
                    expected, render_operation_types(schema, source, name="Run")
                )
        source = "query First { greeting } query Second { greeting }"
        with self.assertRaisesRegex(ValueError, "explicitly"):
            render_operation_types(schema, source, name="Run")
        self.assertIn(
            "RunResult =",
            render_operation_types(schema, source, name="Run", operation="Second"),
        )
        with self.assertRaisesRegex(ValueError, "Unknown selected"):
            render_operation_types(schema, source, name="Run", operation="Missing")

    def test_nullable_and_non_null_variable_defaults_stay_distinct(self) -> None:
        output = render_operation_types(
            build_schema(_SCHEMA),
            "query($a:Int=2,$b:Int!=1) { "
            'a:user(id:"1",limit:$a){id} b:user(id:"2",limit:$b){id} }',
            name="Run",
        )
        self.assertIn("'a': NotRequired['Int | None']", output)
        self.assertIn("'b': NotRequired['Int']", output)

    def test_invalid_and_unsupported_documents_fail(self) -> None:
        schema = build_schema(_SCHEMA)
        for source in (
            "{ missing }",
            "{ __schema { queryType { name } } }",
            "query Same { greeting } query Same { greeting }",
            '{ a:greeting a:user(id: "1") { id } }',
            "{ greeting @skip(if: 1) }",
            'fragment F on User { id } query Q { user(id: "1") { ...F } }',
        ):
            with self.subTest(source=source), self.assertRaises(ValueError):
                render_operation_types(schema, source, name="Run")
        custom = build_schema("scalar Date type Query { date: Date }")
        with self.assertRaisesRegex(ValueError, "Custom scalar"):
            render_operation_types(custom, "{ date }", name="Run")

    def test_unsafe_and_colliding_names_fail(self) -> None:
        schema = build_schema(_SCHEMA)
        for name in ("class", "bad-name", "__hidden", "", "𝑩ook"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                render_operation_types(schema, "{ greeting }", name=name)
        collision = build_schema("enum RunResult { A } type Query { value: RunResult }")
        with self.assertRaisesRegex(ValueError, "Colliding"):
            render_operation_types(collision, "{ value }", name="Run")
        nested = build_schema(
            "type N { b: N a_b: N a: N value: String } type Query { n: N }"
        )
        with self.assertRaisesRegex(ValueError, "Colliding"):
            render_operation_types(
                nested, "{ n { a_b { value } a { b { value } } } }", name="Run"
            )

    def test_unicode_normalization_cannot_overwrite_schema_declarations(self) -> None:
        schema = build_schema(
            "input BookResult { x: Int } type Query { value(input: BookResult): Int }"
        )
        with self.assertRaisesRegex(ValueError, "Unsafe Python operation name"):
            render_operation_types(
                schema, "query($v:BookResult){value(input:$v)}", name="𝑩ook"
            )
        self.assertIn(
            "CaféResult =",
            render_operation_types(schema, "{ value }", name="Café"),
        )

    def test_output_is_deterministic_and_preserves_keyword_keys(self) -> None:
        schema = build_schema(_SCHEMA)
        self.assertEqual(
            render_operation_types(schema, '{ user(id: "1") { id name } }', name="Run"),
            render_operation_types(schema, '{ user(id: "1") { name id } }', name="Run"),
        )
        self.assertIn(
            "'class': 'String'",
            render_operation_types(schema, "{ class:greeting }", name="Run"),
        )

    def test_runtime_and_generated_positive_negative_checker_contracts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            generated = root / "generated.py"
            generated.write_text(
                render_operation_types(build_schema(_SCHEMA), _SOURCE, name="Book")
            )
            spec = importlib.util.spec_from_file_location("generated", generated)
            assert spec is not None and spec.loader is not None
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            self.assertEqual(module.BookVariables.__required_keys__, {"id"})
            self.assertEqual(
                module.BookVariables.__optional_keys__, {"limit", "filter"}
            )
            self.assertEqual(
                module.BookResult_user_friends.__required_keys__, {"id", "name"}
            )
            fixture = root / "fixture.py"
            fixture.write_text(
                "from generated import (BookResult, BookVariables, "
                "BookResult_user_friends)\n"
                "ok: BookVariables = {'id':'1', 'filter': {'threshold': None}}\n"
                "result: BookResult = {'user': None}\n"
                "friend: BookResult_user_friends = {'id':'1','name':None}\n"
                "missing: BookVariables = {}\n"
                "null: BookVariables = {'id':'1','limit':None}\n"
                "enum: BookVariables = {'id':'1','filter':{'color':'GREEN'}}\n"
                "wrong: BookResult_user_friends = {'id':None,'name':None}\n"
            )
            mypy = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "mypy",
                    "--strict",
                    "--no-incremental",
                    "--python-version",
                    "3.10",
                    str(root),
                ],
                cwd=root,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(mypy.returncode, 1, mypy.stdout + mypy.stderr)
            self.assertIn("Found 4 errors in 1 file", mypy.stdout)
            config = root / "pyrightconfig.json"
            config.write_text(
                json.dumps(
                    {
                        "include": ["generated.py", "fixture.py"],
                        "pythonVersion": "3.10",
                        "typeCheckingMode": "strict",
                    }
                )
            )
            pyright = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pyright",
                    "--project",
                    str(config),
                    "--pythonpath",
                    sys.executable,
                    "--outputjson",
                ],
                cwd=root,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(pyright.returncode, 1, pyright.stdout + pyright.stderr)
            diagnostics = json.loads(pyright.stdout)["generalDiagnostics"]
            self.assertEqual(len(diagnostics), 4, diagnostics)
            self.assertEqual(
                {item["range"]["start"]["line"] for item in diagnostics}, {4, 5, 6, 7}
            )

    def test_repeated_nullability_merges_active_occurrences_and_preserves_wrappers(
        self,
    ) -> None:
        from graphql import graphql_sync  # pyright: ignore[reportUnknownVariableType]

        schema = build_schema(
            "directive @required on FIELD directive @optional on FIELD "
            "type User { id: ID! title: String } "
            "type Query { value: String matrix: [[String!]!]! user: User }"
        )
        source = """query($flag:Boolean!) {
            constant:value @required constant:value @optional
            bothNullable:value bothNullable:value @optional
            branch:value branch:value @required @include(if:$flag)
            only:value @optional @include(if:$flag)
            only:value @required @skip(if:$flag)
            matrix @optional matrix @required
            user { id } user @required { title }
            kind:__typename @required kind:__typename @optional
        }"""
        output = render_operation_types(schema, source, name="Read")
        self.assertIn("'constant': 'String'", output)
        self.assertIn("'bothNullable': 'String | None'", output)
        self.assertIn("'branch': 'String | None'", output)
        self.assertIn("'branch': 'String'", output)
        self.assertIn("'matrix': 'list[list[String]]'", output)
        conditional_source = (
            "query($flag:Boolean!) { user { id } "
            "user @required @include(if:$flag) { title } }"
        )
        conditional_output = render_operation_types(
            schema, conditional_source, name="Read"
        )
        for flag, user, expected_data in (
            (False, None, {"user": None}),
            (False, {"id": "1", "title": None}, {"user": {"id": "1"}}),
            (True, {"id": "1", "title": None}, {"user": {"id": "1", "title": None}}),
        ):
            executed = graphql_sync(
                schema,
                conditional_source,
                root_value={"user": user},
                variable_values={"flag": flag},
            )
            self.assertIsNone(executed.errors)
            self.assertEqual(executed.data, expected_data)
        fixture = (
            "from typing import Literal\n"
            "from typing_extensions import assert_type\n"
            "from generated import ReadResult\n"
            "base: ReadResult = {'constant':'yes','bothNullable':None,'bran"
            "ch':None,'matrix':[['x']],'user':{'id':'1','title':None},'kind"
            "':'Query'}\n"
            "nullable: ReadResult = {'constant':'yes','bothNullable':None,'"
            "branch':None,'only':None,'matrix':[['x']],'user':{'id':'1','ti"
            "tle':None},'kind':'Query'}\n"
            "full: ReadResult = {'constant':'yes','bothNullable':'v','branc"
            "h':'v','only':'v','matrix':[['x']],'user':{'id':'1','title':'t"
            "'},'kind':'Query'}\n"
            "def inspect(value: ReadResult) -> None:\n"
            "    assert_type(value['constant'], str)\n"
            "    assert_type(value['branch'], str | None)\n"
            "    assert_type(value['matrix'], list[list[str]])\n"
            "    assert_type(value['user']['id'], str)\n"
            "    assert_type(value['user']['title'], str | None)\n"
            "    assert_type(value['kind'], Literal['Query'])\n"
            "    if 'only' in value:\n"
            "        assert_type(value['only'], str | None)\n"
            "bad_constant: ReadResult = {'constant':None,'bothNullable':Non"
            "e,'branch':None,'matrix':[['x']],'user':{'id':'1','title':None"
            "},'kind':'Query'}  # BAD\n"
            "bad_outer: ReadResult = {'constant':'yes','bothNullable':None,"
            "'branch':None,'matrix':None,'user':{'id':'1','title':None},'ki"
            "nd':'Query'}  # BAD\n"
            "bad_inner: ReadResult = {'constant':'yes','bothNullable':None,"
            "'branch':None,'matrix':[None],'user':{'id':'1','title':None},'"
            "kind':'Query'}  # BAD\n"
            "bad_element: ReadResult = {'constant':'yes','bothNullable':Non"
            "e,'branch':None,'matrix':[[None]],'user':{'id':'1','title':Non"
            "e},'kind':'Query'}  # BAD\n"
            "bad_user: ReadResult = {'constant':'yes','bothNullable':None,'"
            "branch':None,'matrix':[['x']],'user':None,'kind':'Query'}  # B"
            "AD\n"
            "bad_selection: ReadResult = {'constant':'yes','bothNullable':N"
            "one,'branch':None,'matrix':[['x']],'user':{'id':'1'},'kind':'Q"
            "uery'}  # BAD\n"
            "bad_typename: ReadResult = {'constant':'yes','bothNullable':No"
            "ne,'branch':None,'matrix':[['x']],'user':{'id':'1','title':Non"
            "e},'kind':'Other'}  # BAD\n"
            "bad_alias: ReadResult = {'constant':'yes','bothNullable':None,"
            "'branch':None,'matrix':[['x']],'user':{'id':'1','title':None},"
            "'kind':'Query','value':'extra'}  # BAD\n"
        )
        fixture += """from composite import ReadResult as Composite
absent: Composite = {'user': None}
present_base: Composite = {'user': {'id': '1'}}
present_both: Composite = {'user': {'id': '1', 'title': None}}
bad_missing_id: Composite = {'user': {'title': 'x'}}  # BAD
bad_title: Composite = {'user': {'id': '1', 'title': False}}  # BAD
"""
        expected = {
            index
            for index, line in enumerate(fixture.splitlines(), 1)
            if "# BAD" in line
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "generated.py").write_text(output, encoding="utf-8")
            (root / "composite.py").write_text(conditional_output, encoding="utf-8")
            (root / "fixture.py").write_text(fixture, encoding="utf-8")
            runtime = subprocess.run(
                [sys.executable, "-c", "import generated"],
                cwd=root,
                capture_output=True,
                text=True,
            )
            self.assertEqual(runtime.returncode, 0, runtime.stderr)
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
            self.assertEqual(
                {
                    int(line)
                    for line in re.findall(r"fixture\.py:(\d+): error:", mypy.stdout)
                },
                expected,
                mypy.stdout,
            )
            self.assertNotIn("generated.py:", mypy.stdout)
            (root / "pyrightconfig.json").write_text(
                json.dumps(
                    {
                        "include": ["fixture.py", "generated.py"],
                        "pythonVersion": "3.10",
                        "typeCheckingMode": "strict",
                    }
                ),
                encoding="utf-8",
            )
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
            self.assertTrue(
                all(Path(item["file"]).name == "fixture.py" for item in diagnostics),
                diagnostics,
            )
            self.assertEqual(
                {item["range"]["start"]["line"] + 1 for item in diagnostics},
                expected,
                diagnostics,
            )

    def test_repeated_nullable_override_conditions_do_not_create_spurious_branches(
        self,
    ) -> None:
        schema = build_schema("type Query { value: String }")
        output = render_operation_types(
            schema,
            "{ "
            + " ".join(
                f"v{i}:value v{i}:value @optional @include(if:true)" for i in range(12)
            )
            + " }",
            name="Read",
        )
        self.assertNotIn("__graphql_desk_variant_", output)
        self.assertEqual(output.count(": 'String | None'"), 12)

    def test_guaranteed_non_null_repetitions_do_not_expand_nullable_conditionals(
        self,
    ) -> None:
        schema = build_schema("type Query { value: String fixed: String! }")
        for repeated in (
            "v{i}:value @required v{i}:value @optional @include(if:true)",
            "v{i}:value @optional @include(if:true) v{i}:value @required",
            "v{i}:fixed v{i}:fixed @optional @include(if:true)",
        ):
            with self.subTest(repeated=repeated):
                output = render_operation_types(
                    schema,
                    "{ " + " ".join(repeated.format(i=i) for i in range(12)) + " }",
                    name="Read",
                )
                self.assertNotIn("__graphql_desk_variant_", output)
                self.assertEqual(output.count(": 'String'"), 12)
