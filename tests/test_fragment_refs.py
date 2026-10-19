"""Fragment capabilities are static views, not response transformations."""

import json
import re
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from typing import Literal, TypeVar, cast

from graphql import build_schema

from graphql_desk import TypedDocument, graphql
from graphql_desk.fragment_refs import (
    FragmentDocument,
    FragmentIdentity,
    FragmentRef,
    fragment_identity_key,
)
from graphql_desk.fragments import IndexedDocument, SchemaContext, index_fragments
from graphql_desk.literal_ast import render_literal_ast_types
from graphql_desk.source_graph import ModuleSnapshot, resolve_project

_T = TypeVar("_T")
_Identity = FragmentIdentity[Literal["test"]]


def identity(value: _T) -> _T:
    return value


class FragmentRefTests(unittest.TestCase):
    def documents(
        self,
        child_version: int = 1,
        *,
        project: str = "p",
        schema_name: str = "main",
        child_field: str = "title",
    ) -> dict[str, IndexedDocument]:
        schema = build_schema(
            "type User { id: ID! title: String! } type Query { user: User }"
        )
        snapshots = (
            ModuleSnapshot(
                project,
                "child",
                "file:///child.py",
                child_version,
                "from graphql_desk import gql\n"
                f"c=gql('fragment C on User {{ {child_field} }}')",
            ),
            ModuleSnapshot(
                project,
                "fragment",
                "file:///fragment.py",
                3,
                "from graphql_desk import gql\nfrom child import c\n"
                'f=gql("fragment F on User { id ...C }",[c])',
            ),
            ModuleSnapshot(
                project,
                "main",
                "file:///main.py",
                4,
                "from graphql_desk import gql\nfrom fragment import f\n"
                'q=gql("{ user { ...F } }",[f])',
            ),
        )
        sources = resolve_project(snapshots)
        self.assertEqual(sources.diagnostics, ())
        indexed = index_fragments(
            sources,
            schemas={(project, None): SchemaContext(schema_name, schema, "generation")},
        )
        self.assertEqual(indexed.diagnostics, ())
        return {item.call.binding or "": item for item in indexed.documents}

    def test_identity_includes_transitive_versions_and_canonical_dependency_set(
        self,
    ) -> None:
        owner = self.documents()["f"]
        fact = owner.primary_fragment
        assert fact is not None
        key = fragment_identity_key(owner, fact)
        updated = self.documents(child_version=2, child_field="id")["f"]
        assert updated.primary_fragment is not None
        self.assertEqual(owner.call.source, updated.call.source)
        self.assertEqual(owner.call.module, updated.call.module)
        self.assertEqual(owner.call.range, updated.call.range)
        self.assertNotEqual(
            key, fragment_identity_key(updated, updated.primary_fragment)
        )
        for changed_owner in (
            self.documents(project="other")["f"],
            self.documents(schema_name="other")["f"],
        ):
            assert changed_owner.primary_fragment is not None
            self.assertNotEqual(
                key,
                fragment_identity_key(changed_owner, changed_owner.primary_fragment),
            )
        self.assertEqual(
            key,
            fragment_identity_key(
                replace(
                    owner,
                    dependencies=(*reversed(owner.dependencies), *owner.dependencies),
                ),
                fact,
            ),
        )
        self.assertNotEqual(
            key, fragment_identity_key(replace(owner, context_token="other"), fact)
        )
        self.assertNotEqual(
            key,
            fragment_identity_key(replace(owner, schema_identity=("p", "other")), fact),
        )
        self.assertNotEqual(
            key,
            fragment_identity_key(
                replace(owner, call=replace(owner.call, binding="other")), fact
            ),
        )
        for changed in (
            replace(fact, name="Other"),
            replace(fact, type_condition="Other"),
        ):
            changed_owner = replace(
                owner,
                definitions=tuple(
                    changed if item == fact else item for item in owner.definitions
                ),
            )
            self.assertNotEqual(key, fragment_identity_key(changed_owner, changed))
        changed_dependencies = tuple(
            replace(item, uri="file:///other.py") if item.module == "child" else item
            for item in owner.dependencies
        )
        self.assertNotEqual(
            key,
            fragment_identity_key(
                replace(owner, dependencies=changed_dependencies), fact
            ),
        )
        self.assertRegex(key, r"^fragment-ref-v1:[0-9a-f]{64}$")

    def test_identity_rejects_foreign_or_inconsistent_facts(self) -> None:
        documents = self.documents()
        owner = documents["f"]
        fact = owner.primary_fragment
        assert fact is not None
        for wrong_owner, wrong_fact in (
            (documents["q"], fact),
            (replace(owner, definitions=()), fact),
            (owner, replace(fact, kind="query")),
            (replace(owner, schema_identity=("foreign", "main")), fact),
            (
                replace(
                    owner,
                    dependencies=(
                        *owner.dependencies,
                        replace(owner.call.module, version=99),
                    ),
                ),
                fact,
            ),
            (
                replace(
                    owner,
                    dependencies=(
                        *owner.dependencies,
                        replace(owner.call.module, project="foreign"),
                    ),
                ),
                fact,
            ),
        ):
            with (
                self.subTest(owner=wrong_owner, fact=wrong_fact),
                self.assertRaises(ValueError),
            ):
                fragment_identity_key(wrong_owner, wrong_fact)

    def test_both_checkers_preserve_capabilities_document_identity_and_literal_ast(
        self,
    ) -> None:
        owners = self.documents()
        a = owners["f"]
        b = owners["c"]
        assert a.primary_fragment is not None and b.primary_fragment is not None
        a_key = fragment_identity_key(a, a.primary_fragment)
        b_key = fragment_identity_key(b, b.primary_fragment)
        updated = self.documents(child_version=2, child_field="id")["f"]
        assert updated.primary_fragment is not None
        updated_key = fragment_identity_key(updated, updated.primary_fragment)
        api = (
            render_literal_ast_types("fragment F on User { id title }", name="Exact")
            + f"""
from collections.abc import Callable
from typing import Literal, TypeVar
from typing_extensions import TypedDict, ReadOnly
from graphql_desk import TypedDocument
from graphql_desk.fragment_refs import FragmentDocument, FragmentIdentity, FragmentRef
A = FragmentIdentity[Literal[{a_key!r}]]
B = FragmentIdentity[Literal[{b_key!r}]]
Changed = FragmentIdentity[Literal[{updated_key!r}]]
Payload = TypedDict('Payload', {{'title':str}})
Vars = TypedDict('Vars', {{}})
Combined = TypedDict('Combined', {{
    'class':str, '_graphql_desk_φ':ReadOnly[Callable[[A|B],None]]
}},closed=True)
class Present(FragmentRef[A],closed=True):
    id: str
Absent = TypedDict('Absent',{{'id':str}},closed=True)
Conditional = Present | Absent
class User(FragmentRef[A],closed=True):
    kind: Literal['User']
Org = TypedDict('Org',{{'kind':Literal['Org']}},closed=True)
Abstract = User | Org
ClosedRaw = TypedDict('ClosedRaw',{{'id':str}},closed=True)
class Raw(TypedDict):
    id: str
class Unknown(TypedDict):
    _graphql_desk_φ: ReadOnly[object]
class DocumentA(FragmentDocument[A,Payload,Vars]):
    ast: ExactDocument
document_a: DocumentA
document_b: FragmentDocument[B,Payload,Vars]
document_changed: FragmentDocument[Changed,Payload,Vars]
I=TypeVar('I')
R=TypeVar('R')
V=TypeVar('V')
T=TypeVar('T',bound=FragmentRef[A])
def read(document:FragmentDocument[I,R,V],value:FragmentRef[I])->R: ...
def execute(document:TypedDocument[R,V])->R: ...
def many(value:list[T|None]|None)->list[Payload|None]|None: ...
def combined()->Combined: ...
def only_a()->FragmentRef[A]: ...
def both(value:FragmentRef[A|B])->None: ...
def conditional()->Conditional: ...
def abstract()->Abstract: ...
def raw()->Raw: ...
def closed_raw()->ClosedRaw: ...
def unknown()->Unknown: ...
def need_a(document:FragmentDocument[A,Payload,Vars])->None: ...
def compose(
    source:Literal['query'],
    fragments:tuple[DocumentA,FragmentDocument[B,Payload,Vars]],
)->Combined: ...
"""
        )
        positive = """from typing import Literal
from typing_extensions import assert_type
from api import (
    combined,read,document_a,document_b,Payload,many,Present,abstract,
    need_a,execute,compose,Combined,
)
value=combined()
assert_type(value['class'],str)
assert_type(read(document_a,value),Payload)
assert_type(read(document_b,value),Payload)
assert_type(execute(document_a),Payload)
assert_type(document_a.ast.definitions[0].name.value,Literal['F'])
assert_type(compose('query',(document_a,document_b)),Combined)
def collection(values:list[Present|None])->None:
    assert_type(many(values),list[Payload|None]|None)
def conditional_result(value:Present)->None:
    assert_type(read(document_a,value),Payload)
union=abstract()
if union['kind']=='User':
    assert_type(read(document_a,union),Payload)
need_a(document_a)
"""
        negative = """from graphql_desk.fragment_refs import FragmentRef
from api import (
    combined,read,document_a,document_b,conditional,abstract,raw,closed_raw,
    unknown,need_a,both,only_a,compose,A,document_changed,
)
value=combined()
read(document_a,raw()) # error
read(document_a,closed_raw()) # error
read(document_a,unknown()) # error
read(document_b,only_a()) # error
need_a(document_b) # error
need_a(document_changed) # error
read(document_changed,only_a()) # error
both(only_a()) # error
print(value['title']) # error
read(document_a,conditional()) # error
read(document_a,abstract()) # error
value['_graphql_desk_φ']=lambda _:None # error
forged:FragmentRef[A]={{}} # error
compose('query',(document_a,)) # error
compose('query',(document_b,document_a)) # error
compose('query',[document_a,document_b]) # error
""".replace("{{}}", "{}")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "api.pyi").write_text(api, encoding="utf-8")
            (root / "api.py").write_text(
                "# Runtime companion; views are declaration-only.\n", encoding="utf-8"
            )
            for name, source in (("positive.py", positive), ("negative.py", negative)):
                (root / name).write_text(source, encoding="utf-8")
                expected = {
                    i
                    for i, line in enumerate(source.splitlines(), 1)
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
                        str(root / name),
                    ],
                    cwd=root,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                )
                self.assertEqual(
                    mypy.returncode, 1 if expected else 0, mypy.stdout + mypy.stderr
                )
                actual = {
                    int(line)
                    for line in re.findall(
                        re.escape(name) + r":(\d+): error:", mypy.stdout
                    )
                }
                self.assertEqual(actual, expected, mypy.stdout + mypy.stderr)
                (root / "pyrightconfig.json").write_text(
                    json.dumps(
                        {
                            "include": [name, "api.pyi"],
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
                    encoding="utf-8",
                )
                self.assertEqual(
                    pyright.returncode,
                    1 if expected else 0,
                    pyright.stdout + pyright.stderr,
                )
                diagnostics = json.loads(pyright.stdout)["generalDiagnostics"]
                self.assertEqual(
                    {item["range"]["start"]["line"] + 1 for item in diagnostics},
                    expected,
                    diagnostics,
                )

    def test_runtime_views_preserve_original_objects_without_phantom_keys(self) -> None:
        original = {"id": "1", "title": "x"}
        view = cast(FragmentRef[_Identity], original)
        self.assertIs(view, original)
        self.assertIs(identity(view), original)
        values = [original, None]
        viewed_list = cast(list[FragmentRef[_Identity] | None], values)
        self.assertIs(identity(viewed_list), values)
        self.assertIs(identity(None), None)
        document = graphql("fragment F on User { id title }")
        document_view = cast(
            FragmentDocument[_Identity, dict[str, str], dict[str, object]], document
        )
        self.assertIs(document_view, document)
        self.assertIs(document_view.ast, document.ast)
        self.assertIs(type(document_view), TypedDocument)
        self.assertEqual(original, {"id": "1", "title": "x"})
        self.assertEqual(FragmentRef.__required_keys__, {"_graphql_desk_φ"})
        self.assertEqual(getattr(FragmentRef, "__readonly_keys__"), {"_graphql_desk_φ"})
        self.assertNotIn("_graphql_desk_φ", original)
