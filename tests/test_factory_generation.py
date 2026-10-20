import json
import re
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from graphql import build_schema

from graphql_desk.config import ScalarMapping
from graphql_desk.factory_generation import FactoryArtifacts, generate_factory_artifacts
from graphql_desk.fragments import SchemaContext, index_fragments
from graphql_desk.source_graph import (
    FactoryBinding,
    ModuleSnapshot,
    ProjectBindings,
    resolve_project,
)


class FactoryGenerationTests(unittest.TestCase):
    def test_generated_callable_scalar_methods_share_schema_input_types(self) -> None:
        context = SchemaContext(
            "main",
            build_schema(
                "scalar Date enum Color { RED BLUE } "
                "input Filter { id: ID! color: Color date: Date } "
                "type Query { greeting: String! search(filter: Filter): String }"
            ),
            "generation-1",
        )
        source = """from typing import Literal
from generated.main import graphql, Filter
from typing_extensions import assert_type
q = graphql('query Q { greeting }')
assert_type(q.operation_name, Literal['Q'])
value: Filter = {'id':'1','date':'today'}
assert_type(graphql.scalar('Filter',value),Filter)
assert_type(graphql.scalar('Date','today'),str)
assert_type(graphql.scalar('Date',None),None)
assert_type(graphql.scalar('Color','RED'),Literal['BLUE','RED'])
assert_type(graphql.scalar('Color',None),None)
def maybe_filter(value:Filter|None)->None:
 assert_type(graphql.scalar('Filter',value),Filter|None)
assert_type(graphql.scalar('Int',1),int)
assert_type(graphql.scalar('Float',1.5),float)
assert_type(graphql.scalar('ID','1'),str)
assert_type(graphql.scalar('Boolean',True),bool)
assert_type(graphql.scalar('String','text'),str)
assert graphql.scalar('Filter',value) is value
assert graphql.scalar('Date',None) is None
"""
        modules = (ModuleSnapshot("p", "app", "app.py", 1, source),)
        resolved = resolve_project(
            modules,
            bindings=(
                ProjectBindings(
                    "p",
                    ("main",),
                    factories=(FactoryBinding("generated.main", "graphql", "main"),),
                ),
            ),
        )
        index = index_fragments(resolved, schemas={("p", "main"): context})
        artifacts = generate_factory_artifacts(
            context,
            index,
            modules=modules,
            project_id="p",
            generated_package="generated",
            scalars=(ScalarMapping("Date", "builtins.str", "builtins.bytes"),),
        )
        self.assertFalse(artifacts.diagnostics)
        negative = """from generated.main import graphql
graphql.scalar('Unknown',None) # error
graphql.scalar('Query',{}) # error
graphql.scalar('Date',b'today') # error
graphql.scalar('Color','GREEN') # error
graphql.scalar('Filter',{}) # error
graphql.scalar('Filter',{'id':None}) # error
graphql.scalar('Int','1') # error
graphql.scalar('String') # error
def dynamic(name:str)->None:
 graphql.scalar(name,1) # error
"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write(root, artifacts, modules)
            self.check(root)
            runtime = subprocess.run(
                [sys.executable, str(root / "app.py")],
                cwd=root,
                capture_output=True,
                text=True,
            )
            self.assertEqual(runtime.returncode, 0, runtime.stdout + runtime.stderr)
            (root / "bad.py").write_text(negative, encoding="utf8")
            self.check(
                root,
                {
                    "bad.py": {
                        line
                        for line, text in enumerate(negative.splitlines(), 1)
                        if "# error" in text
                    }
                },
            )

    def context(self) -> SchemaContext:
        return SchemaContext(
            "main",
            build_schema(
                "type Book { id: ID! title: String } "
                "type Query { book(id: ID!): Book! greeting: String! }"
            ),
            "generation-1",
        )

    def generate(self, modules: tuple[ModuleSnapshot, ...]) -> FactoryArtifacts:
        context = self.context()
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
        return generate_factory_artifacts(
            context,
            index,
            modules=modules,
            project_id="p",
            generated_package="generated",
        )

    def write(
        self,
        root: Path,
        artifacts: FactoryArtifacts,
        modules: tuple[ModuleSnapshot, ...],
    ) -> None:
        self.assertTrue(artifacts.files, artifacts.diagnostics)
        for file in artifacts.files:
            path = root / file.relative_path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(file.text, encoding="utf8")
        for module in modules:
            path = root / (module.module.replace(".", "/") + ".py")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(module.source, encoding="utf8")

    def check(self, root: Path, errors: dict[str, set[int]] | None = None) -> None:
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
        self.assertEqual(mypy.returncode, 1 if errors else 0, mypy.stdout + mypy.stderr)
        if errors:
            observed: dict[str, set[int]] = {}
            for file, line in re.findall(
                r"([^/\\:\n]+\.py):(\d+): error:", mypy.stdout
            ):
                observed.setdefault(file, set()).add(int(line))
            self.assertEqual(observed, errors, mypy.stdout + mypy.stderr)
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
        self.assertEqual(
            pyright.returncode, 1 if errors else 0, pyright.stdout + pyright.stderr
        )
        if errors:
            observed = {}
            for diagnostic in json.loads(pyright.stdout)["generalDiagnostics"]:
                observed.setdefault(Path(diagnostic["file"]).name, set()).add(
                    diagnostic["range"]["start"]["line"] + 1
                )
            self.assertEqual(observed, errors, pyright.stdout + pyright.stderr)

    def test_same_original_source_infers_fragment_operation_and_literal_ast(
        self,
    ) -> None:
        leaf = ModuleSnapshot(
            "p",
            "leaf",
            "file:///leaf.py",
            1,
            "from generated.main import graphql\n"
            'book_fields=graphql("fragment Fields on Book { title }")\n'
            'other_fields=graphql("fragment OtherFields on Book { id }")\n',
        )
        source = (
            (
                "from typing import TYPE_CHECKING, TypeVar\n"
                "from typing_extensions import assert_type\n"
                "from graphql_desk import TypedDocument\n"
                "from generated.main import graphql\n"
                "from generated.sources.main import "
                "BookQueryResult, BookQueryVariables\n"
                "from leaf import book_fields\n"
                'book_query=graphql("query Book($id:ID!){book(id:$id){id ...Fields}}",'
                "(book_fields,))\n"
                'duplicate=graphql("query Book($id:ID!){book(id:$id){id ...Fields}}",'
                "(book_fields,))\n"
                'other=graphql("query A{greeting} query B{alias:greeting}",'
                'operation="B")\n'
                "R=TypeVar('R')\nV=TypeVar('V')\n"
                "def execute(document: TypedDocument[R,V], variables: V) -> R:\n"
                "    raise RuntimeError('checker boundary only')\n"
                "if TYPE_CHECKING:\n"
                "    variables: BookQueryVariables = {'id':'1'}\n"
                "    assert_type(execute(book_query, variables), BookQueryResult)\n"
                "    assert_type(book_query.ast.definitions[0].name.value, "
                "__import__('typing').Literal['Book'])\n"
                "assert type(book_query) is TypedDocument\n"
                "assert book_query.operation_name == 'Book'\n"
                "assert other.operation_name == 'B'\n"
                "assert book_query.schema_name == 'main'\n"
                "assert len(book_query.ast.definitions) == 2\n"
            )
            .replace("__import__('typing').Literal", "Literal")
            .replace("TYPE_CHECKING, TypeVar", "TYPE_CHECKING, Literal, TypeVar")
        )
        main = ModuleSnapshot("p", "main", "file:///main.py", 1, source)
        modules = (leaf, main)
        artifacts = self.generate(modules)
        self.assertEqual(artifacts.diagnostics, ())
        self.assertEqual(artifacts, self.generate(modules))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write(root, artifacts, modules)
            self.check(root)
            runtime = subprocess.run(
                [sys.executable, "main.py"], cwd=root, capture_output=True, text=True
            )
            self.assertEqual(runtime.returncode, 0, runtime.stdout + runtime.stderr)
            bad = (
                "from generated.sources.main import "
                "BookQueryResult, BookQueryVariables\n"
                "from main import book_query, other, execute\n"
                "from leaf import book_fields, other_fields\n"
                "from generated.main import graphql\n"
                "from typing_extensions import assert_type\n"
                "from graphql import DocumentNode\n"
                "from graphql_desk import TypedDocument\n"
                "variables: BookQueryVariables = {'id': 1}  # error\n"
                "valid: BookQueryVariables = {'id':'1'}\n"
                "result = execute(book_query, valid)\n"
                "hidden = result['book']['title']  # error\n"
                "execute(book_query, 1)  # error\n"
                "missing=graphql("
                "'query Book($id:ID!){book(id:$id){id ...Fields}}',())\n"
                "extra=graphql('query Book($id:ID!){book(id:$id){id ...Fields}}',"
                "(book_fields,book_fields))\n"
                "operation_doc=graphql("
                "'query Book($id:ID!){book(id:$id){id ...Fields}}',"
                "(other,))\n"
                "wrong_fragment=graphql("
                "'query Book($id:ID!){book(id:$id){id ...Fields}}',"
                "(other_fields,))\n"
                "for opaque in [missing,extra,operation_doc,wrong_fragment]:\n"
                "    assert_type(opaque.ast, DocumentNode)\n"
                "    execute(opaque, valid)  # error\n"
                "    precise: TypedDocument[BookQueryResult,BookQueryVariables] "
                "= opaque  # error\n"
                "    selected = opaque['book']  # error\n"
            )
            (root / "bad.py").write_text(bad, encoding="utf8")
            self.check(
                root,
                {
                    "bad.py": {
                        i
                        for i, line in enumerate(bad.splitlines(), 1)
                        if "# error" in line
                    }
                },
            )

    def test_opaque_fallback_preserves_dynamic_ast_access_without_precise_types(
        self,
    ) -> None:
        modules = (
            ModuleSnapshot(
                "p",
                "main",
                "file:///main.py",
                1,
                'from generated.main import graphql\nq=graphql("query Q{greeting}")\n',
            ),
        )
        artifacts = self.generate(modules)
        self.assertEqual(artifacts.diagnostics, ())
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write(root, artifacts, modules)
            boundary = (
                "from typing import TypeVar\n"
                "from typing_extensions import assert_type\n"
                "from graphql import DocumentNode\n"
                "from graphql_desk import TypedDocument\n"
                "from generated.sources.main import QResult, QVariables\n"
                "from generated.main import graphql\n"
                "R=TypeVar('R')\nV=TypeVar('V')\n"
                "def execute(doc: TypedDocument[R,V], variables: V) -> R: "
                "raise RuntimeError\n"
                "wrong_source=graphql('query Missing{greeting}')\n"
                "wrong_selector=graphql('query Q{greeting}',operation='Wrong')\n"
                "wrong_fragments=graphql('query Q{greeting}',[graphql('{greeting}')])\n"
                "for opaque in [wrong_source,wrong_selector,wrong_fragments]:\n"
                "    assert_type(opaque.ast,DocumentNode)\n"
                "    assert_type(opaque.operation_name,str | None)\n"
                "    assert_type(opaque.schema_name,str | None)\n"
                "    precise: TypedDocument[QResult,QVariables] = opaque  # error\n"
                "    execute(opaque,QVariables())  # error\n"
                "    value=opaque['greeting']  # error\n"
                "    opaque.ast=opaque.ast  # error\n"
                "    opaque.operation_name='Q'  # error\n"
                "    opaque.schema_name='main'  # error\n"
                "def dynamic(source: str) -> DocumentNode:\n"
                "    doc=graphql(source)\n"
                "    assert_type(doc.ast,DocumentNode)\n"
                "    assert_type(doc.operation_name,str | None)\n"
                "    assert_type(doc.schema_name,str | None)\n"
                "    execute(doc,QVariables())  # error\n"
                "    return doc.ast\n"
                "def compose(fragment_source: str, source: str) -> DocumentNode:\n"
                "    fragment=graphql(fragment_source)\n"
                "    doc=graphql(source,(fragment,))\n"
                "    assert_type(doc.ast,DocumentNode)\n"
                "    execute(doc,QVariables())  # error\n"
                "    return doc.ast\n"
            )
            (root / "boundary.py").write_text(boundary, encoding="utf8")
            runtime = (
                "from generated.main import graphql\n"
                "from graphql_desk import TypedDocument\n"
                "from graphql import build_schema\n"
                "from graphql import execute_sync  "
                "# pyright: ignore[reportUnknownVariableType]\n"
                "def dynamic(source: str) -> None:\n"
                "    doc=graphql(source)\n"
                "    assert type(doc) is TypedDocument\n"
                "    assert doc.operation_name=='Dynamic' and doc.schema_name=='main'\n"
                "    response=execute_sync("
                "build_schema('type Query{greeting:String!}'),"
                "doc.ast,root_value={'greeting':'hi'})\n"
                "    assert response.errors is None and "
                "response.data=={'greeting':'hi'}\n"
                "dynamic('query Dynamic{greeting}')\n"
                "def compose(fragment_source: str, source: str) -> None:\n"
                "    fragment=graphql(fragment_source)\n"
                "    doc=graphql(source,(fragment,))\n"
                "    assert type(doc) is TypedDocument\n"
                "    assert doc.ast.definitions[1] is fragment.ast.definitions[0]\n"
                "    response=execute_sync("
                "build_schema('type Query{greeting:String!}'),"
                "doc.ast,root_value={'greeting':'hi'})\n"
                "    assert response.errors is None and "
                "response.data=={'greeting':'hi'}\n"
                "compose('fragment F on Query{greeting}','query Composed{...F}')\n"
            )
            (root / "runtime.py").write_text(runtime, encoding="utf8")
            self.check(
                root,
                {
                    "boundary.py": {
                        i
                        for i, line in enumerate(boundary.splitlines(), 1)
                        if "# error" in line
                    }
                },
            )
            executed = subprocess.run(
                [sys.executable, "runtime.py"], cwd=root, capture_output=True, text=True
            )
            self.assertEqual(executed.returncode, 0, executed.stdout + executed.stderr)

    def test_primary_unmask_ast_refinement_and_annotated_literal_sources(self) -> None:
        modules = (
            ModuleSnapshot(
                "p",
                "main",
                "file:///main.py",
                1,
                "from typing import Final, Literal, TYPE_CHECKING\n"
                "from typing_extensions import assert_type\n"
                "from generated.main import graphql\n"
                'F: Final = "fragment Fields on Book @_unmask {title}"\n'
                "fields=graphql(F)\n"
                'Q: Literal["query Q{greeting}"] = "query Q{greeting}"\n'
                "q=graphql(Q)\n"
                'raw=graphql(r"query Raw{greeting}")\n'
                'adjacent=graphql("query Adjacent" "{greeting}")\n'
                'keyword=graphql(source="query Keyword{greeting}")\n'
                "escaped=graphql('query Escaped{book(id:\"café\"){id}}')\n"
                "if TYPE_CHECKING:\n"
                "    assert_type(fields.ast.definitions[0].directives, tuple[()])\n"
                "    assert_type(fields.ast.definitions[0].name.value, "
                "Literal['Fields'])\n"
                "    assert_type(q.operation_name, Literal['Q'])\n"
                "    assert_type(keyword.operation_name, Literal['Keyword'])\n"
                "    assert_type(escaped.operation_name, Literal['Escaped'])\n"
                "assert fields.ast.definitions[0].directives == ()\n",
            ),
        )
        artifacts = self.generate(modules)
        self.assertEqual(artifacts.diagnostics, ())
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write(root, artifacts, modules)
            self.check(root)
            runtime = subprocess.run(
                [sys.executable, "main.py"], cwd=root, capture_output=True, text=True
            )
            self.assertEqual(runtime.returncode, 0, runtime.stdout + runtime.stderr)

    def test_precision_diagnostics_are_explicit_and_offsets_use_physical_lines(
        self,
    ) -> None:
        modules = (
            ModuleSnapshot(
                "p",
                "main",
                "file:///main.py",
                1,
                "from generated.main import graphql\n"
                'text="a\u2028b"\n'
                'SOURCE="query Q{greeting}"\n'
                "alias=graphql(SOURCE)\n"
                'concat=graphql("query R"+"{greeting}")\n'
                'fields=graphql("fragment Fields on Book{title}")\n'
                'query=graphql("query S{book(id:\\"1\\"){...Fields}}",[fields])\n',
            ),
        )
        artifacts = self.generate(modules)
        self.assertTrue(artifacts.files)
        self.assertEqual(
            [d.code for d in artifacts.diagnostics], ["factory-precision"] * 3
        )
        self.assertTrue(any("tuple" in d.message for d in artifacts.diagnostics))
        self.assertTrue(any("widen" in d.message for d in artifacts.diagnostics))

    def test_invalid_graph_collision_and_snapshot_inputs_never_publish(self) -> None:
        for source in (
            'q=graphql("query Bad{missing}")',
            'foo_bar=graphql("query A{greeting}")\nFooBar=graphql("query B{greeting}")',
            'first=graphql("fragment F on Book{title}")\n'
            'second=graphql("fragment F on Book{title}")',
        ):
            with self.subTest(source=source):
                modules = (
                    ModuleSnapshot(
                        "p",
                        "main",
                        "file:///main.py",
                        1,
                        "from generated.main import graphql\n" + source,
                    ),
                )
                artifacts = self.generate(modules)
                self.assertEqual(artifacts.files, ())
                self.assertTrue(artifacts.diagnostics)
        modules = (
            ModuleSnapshot(
                "p",
                "main",
                "file:///main.py",
                1,
                "from graphql_desk import gql\nq=gql('{greeting}')",
            ),
        )
        context = self.context()
        index = index_fragments(
            resolve_project(modules), schemas={("p", None): context}
        )
        changed_literal = replace(
            modules[0], source=modules[0].source.replace("greeting", "book____")
        )
        self.assertEqual(len(changed_literal.source), len(modules[0].source))
        for snapshots in ((replace(modules[0], version=2),), (changed_literal,), ()):
            artifacts = generate_factory_artifacts(
                context,
                index,
                modules=snapshots,
                project_id="p",
                generated_package="generated",
            )
            self.assertEqual(artifacts.files, ())
            self.assertTrue(artifacts.diagnostics)
        with self.assertRaisesRegex(ValueError, "Project identity"):
            generate_factory_artifacts(
                context,
                index,
                modules=modules,
                project_id="",
                generated_package="generated",
            )
        for package in ("../outside", "safe.class", "safe.\u212a"):
            with self.assertRaises(ValueError):
                generate_factory_artifacts(
                    context,
                    index,
                    modules=modules,
                    project_id="p",
                    generated_package=package,
                )

    def test_empty_factory_and_package_source_companions_are_importable(self) -> None:
        empty = self.generate(())
        self.assertEqual(empty.diagnostics, ())
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write(root, empty, ())
            (root / "fixture.py").write_text(
                "from typing_extensions import assert_type\n"
                "from graphql import DocumentNode\n"
                "from generated.main import graphql\n"
                "assert_type(graphql('{greeting}').ast, DocumentNode)\n",
                encoding="utf8",
            )
            self.check(root)
        modules = (
            ModuleSnapshot(
                "p",
                "pkg",
                "file:///pkg/__init__.py",
                1,
                "from generated.main import graphql\n"
                'root=graphql("query Root{greeting}")',
                is_package=True,
            ),
            ModuleSnapshot(
                "p",
                "pkg.child",
                "file:///pkg/child.py",
                1,
                "from generated.main import graphql\n"
                'child=graphql("query Child{greeting}")',
            ),
        )
        artifacts = self.generate(modules)
        self.assertEqual(artifacts.diagnostics, ())
        files = {file.relative_path: file.text for file in artifacts.files}
        self.assertIn("RootResult", files["generated/sources/pkg/__init__.py"])
        self.assertIn("ChildResult", files["generated/sources/pkg/child.py"])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write(root, artifacts, ())
            (root / "fixture.py").write_text(
                "from generated.sources.pkg import RootResult\n"
                "from generated.sources.pkg.child import ChildResult\n"
                "root: RootResult = {'greeting':'root'}\n"
                "child: ChildResult = {'greeting':'child'}\n",
                encoding="utf8",
            )
            self.check(root)
            runtime = subprocess.run(
                [sys.executable, "fixture.py"], cwd=root, capture_output=True, text=True
            )
            self.assertEqual(runtime.returncode, 0, runtime.stdout + runtime.stderr)
        invalid = self.generate((replace(modules[0], is_package=False), modules[1]))
        self.assertEqual(invalid.files, ())
        self.assertTrue(invalid.diagnostics)

    def test_discovered_dynamic_source_blocks_artifact_publication(self) -> None:
        modules = (
            ModuleSnapshot(
                "p",
                "main",
                "file:///main.py",
                1,
                "from generated.main import graphql\n"
                "def external_source() -> str: return '{greeting}'\n"
                "source=external_source()\n"
                "q=graphql(source)\n",
            ),
        )
        artifacts = self.generate(modules)
        self.assertEqual(artifacts.files, ())
        self.assertTrue(artifacts.diagnostics)
        self.assertTrue(
            any(d.code == "source-not-static" for d in artifacts.diagnostics),
            artifacts.diagnostics,
        )

    def test_generated_factory_exports_only_graphql_with_import_alias_supported(
        self,
    ) -> None:
        modules = (
            ModuleSnapshot(
                "p",
                "main",
                "file:///main.py",
                1,
                "from generated.main import graphql as gql\n"
                'q=gql("query Q{greeting}")\n',
            ),
        )
        artifacts = self.generate(modules)
        self.assertEqual(artifacts.diagnostics, ())
        files = {file.relative_path: file.text for file in artifacts.files}
        self.assertNotIn("gql = graphql", files["generated/main.py"])
        self.assertNotIn("gql = graphql", files["generated/main.pyi"])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write(root, artifacts, modules)
            self.check(root)

    def test_signature_collision_diagnostic_points_to_its_actual_module_and_call(
        self,
    ) -> None:
        source = (
            "from generated.main import graphql\n"
            'first=graphql("fragment F on Book{title}")\n'
            'second=graphql("fragment F on Book{title}")\n'
        )
        modules = (
            ModuleSnapshot("p", "a", "file:///a.py", 1, source),
            ModuleSnapshot(
                "p",
                "z",
                "file:///z.py",
                1,
                "from generated.main import graphql\n"
                'unrelated=graphql("query Z{greeting}")\n',
            ),
        )
        artifacts = self.generate(modules)
        self.assertEqual(artifacts.files, ())
        self.assertEqual(len(artifacts.diagnostics), 1)
        diagnostic = artifacts.diagnostics[0]
        self.assertEqual(diagnostic.code, "factory-generation")
        self.assertEqual(diagnostic.module.module, "a")
        self.assertEqual(
            diagnostic.range.start, source.index("graphql(", source.index("second="))
        )
