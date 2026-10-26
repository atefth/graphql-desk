import json
import re
import subprocess
import sys
import tempfile
import unittest
from dataclasses import asdict, replace
from pathlib import Path
from typing import cast
from unittest.mock import patch

from graphql import build_schema

from graphql_desk.config import ProjectConfig, ScalarMapping, parse_project_config
from graphql_desk.factory_generation import (
    ConfigDependency,
    FactoryArtifacts,
    SchemaCoordinateDependency,
    generate_factory_artifacts,
    generate_project_factory_artifacts,
)
from graphql_desk.fragments import FragmentIndex, SchemaContext, index_fragments
from graphql_desk.project import project_bindings
from graphql_desk.source_graph import (
    FactoryBinding,
    ModuleSnapshot,
    ProjectBindings,
    resolve_project,
)


class FactoryGenerationTests(unittest.TestCase):
    def multi_schema(
        self,
    ) -> tuple[
        ProjectConfig,
        tuple[SchemaContext, ...],
        tuple[ModuleSnapshot, ...],
        FragmentIndex,
    ]:
        config = parse_project_config(
            """[tool.graphql-desk]
generated_package="generated"
client_directives=["local"]
[[tool.graphql-desk.schemas]]
name="alpha"
schema="alpha.graphql"
schema_output="generated/alpha.pyi"
scalars={Token={input="builtins.str",output="builtins.int"}}
[[tool.graphql-desk.schemas]]
name="beta"
schema="beta.graphql"
schema_output="generated/beta.pyi"
disable_masking=true
scalars={Token={input="builtins.int",output="builtins.bytes"}}
""",
            config_path=Path(tempfile.gettempdir()) / "assembly-facts/pyproject.toml",
        )
        assert config is not None
        sdl = (
            "scalar Token input Filter { token:Token! } "
            "type Item { id:ID! value:Token! } type Query { item(input:Filter):Item! }"
        )
        contexts = tuple(
            SchemaContext(name, build_schema(sdl), "v1:" + name)
            for name in ("alpha", "beta")
        )
        source = """from typing import TYPE_CHECKING,TypeVar,Literal
from typing_extensions import assert_type
from graphql_desk import TypedDocument
from generated.alpha import graphql as alpha,Filter
from generated.beta import graphql as beta
from generated.sources.main import (
 AlphaQueryResult,BetaQueryResult,AlphaFieldsResult,BetaFieldsResult,
 AlphaQueryVariables,BetaQueryVariables)
from generated.sources.first.shared import SameResult as FirstResult
from generated.sources.second.shared import SameResult as SecondResult
alpha_fields=alpha('fragment Fields on Item { value }')
beta_fields=beta('fragment Fields on Item { value }')
alpha_query=alpha('query A {item {id ...Fields @local}}',(alpha_fields,))
beta_query=beta('query B {item {id ...Fields}}',(beta_fields,))
assert alpha_query.schema_name=='alpha'
assert beta_query.schema_name=='beta'
assert_type(alpha_query.schema_name,Literal['alpha'])
payload:Filter={'token':'input'}
assert alpha.scalar('Filter',payload) is payload
assert_type(alpha.scalar('Token','value'),str)
assert_type(beta.scalar('Token',1),int)
if TYPE_CHECKING:
 R=TypeVar('R')
 V=TypeVar('V')
 def execute(document:TypedDocument[R,V],variables:V)->R: raise RuntimeError
 assert_type(execute(alpha_query,AlphaQueryVariables()),AlphaQueryResult)
 assert_type(execute(beta_query,BetaQueryVariables()),BetaQueryResult)
 def inspect(a:AlphaQueryResult,b:BetaQueryResult,
             fa:AlphaFieldsResult,fb:BetaFieldsResult,
             one:FirstResult,two:SecondResult)->None:
  assert_type(a['item']['id'],str)
  assert_type(b['item']['value'],bytes)
  assert_type(fa['value'],int)
  assert_type(fb['value'],bytes)
  assert_type(one['item']['value'],int)
  assert_type(two['item']['value'],bytes)
"""
        modules = (
            ModuleSnapshot("p", "main", "file:///main.py", 1, source),
            ModuleSnapshot(
                "p", "first", "file:///first/__init__.py", 1, "", is_package=True
            ),
            ModuleSnapshot(
                "p", "second", "file:///second/__init__.py", 1, "", is_package=True
            ),
            ModuleSnapshot(
                "p",
                "first.shared",
                "file:///first/shared.py",
                1,
                "from generated.alpha import graphql\nsame=graphql('{item{id value}}')",
            ),
            ModuleSnapshot(
                "p",
                "second.shared",
                "file:///second/shared.py",
                1,
                "from generated.beta import graphql\nsame=graphql('{item{id value}}')",
            ),
        )
        resolved = resolve_project(
            modules, bindings=(project_bindings(config, project_id="p"),)
        )
        self.assertEqual(resolved.diagnostics, ())
        index = index_fragments(
            resolved, schemas={("p", item.schema_name): item for item in contexts}
        )
        self.assertEqual(index.diagnostics, ())
        return config, contexts, modules, index

    def test_project_assembly_merges_source_aliases_and_isolates_schema_files(
        self,
    ) -> None:
        config, contexts, modules, index = self.multi_schema()
        artifacts = generate_project_factory_artifacts(
            config, contexts, index, modules=modules, project_id="p"
        )
        self.assertEqual(artifacts.diagnostics, ())
        paths = {item.relative_path for item in artifacts.files}
        self.assertIn("generated/_documents/alpha/d0.pyi", paths)
        self.assertIn("generated/_documents/beta/d0.pyi", paths)
        self.assertIn("generated/sources/first/shared.pyi", paths)
        self.assertIn("generated/sources/second/shared.pyi", paths)
        main = next(
            item.text
            for item in artifacts.files
            if item.relative_path == "generated/sources/main.pyi"
        )
        self.assertIn("._documents.alpha.", main)
        self.assertIn("._documents.beta.", main)
        self.assertEqual(main.count(" as AlphaQueryResult"), 1)
        self.assertEqual(main.count(" as BetaQueryResult"), 1)
        permuted = generate_project_factory_artifacts(
            replace(config, schemas=tuple(reversed(config.schemas))),
            tuple(reversed(contexts)),
            replace(index, documents=tuple(reversed(index.documents))),
            modules=tuple(reversed(modules)),
            project_id="p",
        )
        self.assertEqual(artifacts, permuted)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write(root, artifacts, modules)
            self.check(root)
            runtime = subprocess.run(
                [sys.executable, str(root / "main.py")],
                cwd=root,
                capture_output=True,
                text=True,
            )
            self.assertEqual(runtime.returncode, 0, runtime.stdout + runtime.stderr)
            negative = """from main import beta_query,beta_fields
from generated.alpha import graphql as alpha
from generated.beta import graphql as beta
from generated.sources.main import AlphaQueryResult,BetaQueryResult
from graphql_desk import TypedDocument
from typing import TypeVar
R=TypeVar('R')
V=TypeVar('V')
def execute(document:TypedDocument[R,V],variables:V)->R: raise RuntimeError
alpha.scalar('Token',1) # error
beta.scalar('Token','value') # error
wrong=alpha('query A {item {id ...Fields @local}}',(beta_fields,))
execute(wrong,{}) # error
def inspect(a:AlphaQueryResult,b:BetaQueryResult)->None:
 a['item']['value'] # error
 value:int=b['item']['value'] # error
read:TypedDocument[AlphaQueryResult,dict[str,str]]=beta_query # error
"""
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
            bad_runtime = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "from main import alpha,beta_fields; "
                    "alpha('query A {item {id ...Fields @local}}',(beta_fields,))",
                ],
                cwd=root,
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(bad_runtime.returncode, 0)
            self.assertIn("different schema", bad_runtime.stderr)

    def test_project_assembly_context_index_and_alias_failures_are_atomic(self) -> None:
        config, contexts, modules, index = self.multi_schema()
        for invalid_contexts in (
            contexts[:1],
            contexts + contexts[:1],
            (*contexts, replace(contexts[0], schema_name="extra")),
        ):
            with self.subTest(contexts=invalid_contexts), self.assertRaises(ValueError):
                generate_project_factory_artifacts(
                    config, invalid_contexts, index, modules=modules, project_id="p"
                )
        for invalid in (
            replace(index, documents=index.documents + index.documents[:1]),
            replace(
                index,
                documents=(
                    replace(index.documents[0], schema_identity=("other", "alpha")),
                ),
            ),
            replace(
                index,
                documents=(
                    replace(index.documents[0], schema_identity=("p", "foreign")),
                ),
            ),
        ):
            with self.subTest(index=invalid), self.assertRaises(ValueError):
                generate_project_factory_artifacts(
                    config, contexts, invalid, modules=modules, project_id="p"
                )
        stale = replace(
            index,
            documents=(
                replace(index.documents[0], context_token="stale"),
                *index.documents[1:],
            ),
        )
        result = generate_project_factory_artifacts(
            config, contexts, stale, modules=modules, project_id="p"
        )
        self.assertEqual(result.files, ())
        self.assertTrue(result.diagnostics)
        collision_source = (
            "from generated.alpha import graphql as alpha\n"
            "from generated.beta import graphql as beta\n"
            "get_book=alpha('{item{id}}')\nGetBook=beta('{item{id}}')\n"
        )
        collision = (replace(modules[0], source=collision_source),)
        resolved = resolve_project(
            collision, bindings=(project_bindings(config, project_id="p"),)
        )
        invalid = index_fragments(
            resolved, schemas={("p", c.schema_name): c for c in contexts}
        )
        result = generate_project_factory_artifacts(
            config, contexts, invalid, modules=collision, project_id="p"
        )
        self.assertEqual(result.files, ())
        self.assertIn("aliases collide", result.diagnostics[-1].message)
        mixed = (
            replace(
                modules[0],
                source=modules[0].source.replace("(alpha_fields,)", "(beta_fields,)"),
            ),
            *modules[1:],
        )
        rejected = index_fragments(
            resolve_project(
                mixed, bindings=(project_bindings(config, project_id="p"),)
            ),
            schemas={("p", c.schema_name): c for c in contexts},
        )
        self.assertTrue(rejected.diagnostics)
        result = generate_project_factory_artifacts(
            config, contexts, rejected, modules=mixed, project_id="p"
        )
        self.assertEqual(result.files, ())
        self.assertEqual(result.diagnostics, rejected.diagnostics)

    def test_project_assembly_uses_captured_config_without_io_or_mapping_imports(
        self,
    ) -> None:
        config, contexts, _, _ = self.multi_schema()
        settings = replace(
            config.schemas[0],
            scalars=(ScalarMapping("Token", "absent.Input", "absent.Output"),),
        )
        config = replace(config, schemas=(settings, config.schemas[1]))
        with (
            patch("builtins.open", side_effect=AssertionError("No I/O")),
            patch.object(Path, "open", side_effect=AssertionError("No I/O")),
        ):
            result = generate_project_factory_artifacts(
                config, contexts, FragmentIndex((), ()), modules=(), project_id="p"
            )
        self.assertEqual(result.diagnostics, ())
        self.assertTrue(result.files)
        alpha = next(
            item.text
            for item in result.files
            if item.relative_path == "generated/alpha.pyi"
        )
        self.assertIn("from absent import Input", alpha)
        self.assertIn("from absent import Output", alpha)
        invalid = replace(
            config,
            schemas=(replace(settings, schema_output=config.root / "outside.pyi"),),
        )
        with self.assertRaises(ValueError):
            generate_project_factory_artifacts(
                invalid, contexts[:1], FragmentIndex((), ()), modules=(), project_id="p"
            )

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

    def test_semantic_failures_keep_independent_calls_and_importable_error_aliases(
        self,
    ) -> None:
        source = """from typing import TYPE_CHECKING,TypeVar
from typing_extensions import assert_type
from graphql import DocumentNode
from graphql_desk import TypedDocument
from generated.main import graphql
from generated.sources.main import (
 GoodResult,GoodVariables,BadResult,BadVariables,BadUnavailable,
 FieldsResult,FieldsFragmentRef,DependentResult,AgainResult,SameDependentResult)
fields=graphql('fragment F on Book { missing }')
same_fields=graphql('fragment F on Book { missing }')
bad=graphql('query Bad { missing }')
again=graphql('query Bad { missing }')
dependent=graphql('query Dependent {book(id:"1"){...F}}',(fields,))
same_dependent=graphql('query Dependent {book(id:"1"){...F}}',(same_fields,))
good=graphql('query Good {greeting}')
assert bad.ast.definitions
assert dependent.ast.definitions
if TYPE_CHECKING:
 R=TypeVar('R')
 V=TypeVar('V')
 def execute(document:TypedDocument[R,V],variables:V)->R: raise RuntimeError
 assert_type(execute(good,GoodVariables()),GoodResult)
 assert_type(bad,BadUnavailable)
 assert_type(bad,BadResult)
 assert_type(bad,BadVariables)
 assert_type(again,AgainResult)
 assert_type(fields,FieldsResult)
 assert_type(fields,FieldsFragmentRef)
 assert_type(dependent,DependentResult)
 assert_type(same_dependent,SameDependentResult)
 assert_type(bad.ast,DocumentNode)
"""
        modules = (ModuleSnapshot("p", "main", "file:///main.py", 1, source),)
        artifacts = self.generate(modules)
        self.assertTrue(artifacts.files)
        self.assertTrue(
            any(d.code == "graphql-validation" for d in artifacts.diagnostics)
        )
        companion = next(
            item.text
            for item in artifacts.files
            if item.relative_path == "generated/sources/main.pyi"
        )
        self.assertNotIn("unsafe_read_bad_result", companion)
        self.assertNotIn("BadFixtureInput", companion)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write(root, artifacts, modules)
            self.check(root)
            runtime = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "import inspect, main; from graphql_desk import TypedDocument; "
                    "assert inspect.isabstract(main.BadResult); "
                    "assert type(main.bad) is TypedDocument",
                ],
                cwd=root,
                capture_output=True,
                text=True,
            )
            self.assertEqual(runtime.returncode, 0, runtime.stdout + runtime.stderr)
            negative = """from main import bad,fields,dependent
from generated.sources.main import GoodResult,BadResult,BadVariables
from graphql_desk import TypedDocument
from typing import TypeVar
R=TypeVar('R')
V=TypeVar('V')
def execute(document:TypedDocument[R,V],variables:V)->R: raise RuntimeError
execute(bad,{}) # error
execute(fields,{}) # error
execute(dependent,{}) # error
precise:TypedDocument[GoodResult,dict[str,str]]=bad # error
bad['missing'] # error
result:BadResult={} # error
variables:BadVariables={} # error
bad.ast=bad.ast # error
BadResult() # error
"""
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
        config, contexts, project_modules, index = self.multi_schema()
        changed = replace(
            project_modules[0],
            source=project_modules[0].source.replace(
                "query A {item {id ...Fields @local}}", "query A {missing}"
            ),
        )
        project_modules = (changed, *project_modules[1:])
        index = index_fragments(
            resolve_project(
                project_modules, bindings=(project_bindings(config, project_id="p"),)
            ),
            schemas={("p", context.schema_name): context for context in contexts},
        )
        result = generate_project_factory_artifacts(
            config, contexts, index, modules=project_modules, project_id="p"
        )
        self.assertTrue(result.files)
        self.assertTrue(result.diagnostics)
        permuted = generate_project_factory_artifacts(
            replace(config, schemas=tuple(reversed(config.schemas))),
            tuple(reversed(contexts)),
            replace(index, documents=tuple(reversed(index.documents))),
            modules=tuple(reversed(project_modules)),
            project_id="p",
        )
        self.assertEqual(result, permuted)
        beta = next(
            file.text
            for file in result.files
            if file.relative_path == "generated/beta.pyi"
        )
        self.assertIn("__graphql_desk_Document", beta)
        invalid = replace(
            index,
            documents=(
                *(
                    replace(document, selected_operation=None)
                    if document.call.binding == "alpha_query"
                    else document
                    for document in index.documents
                ),
            ),
        )
        result = generate_project_factory_artifacts(
            config, contexts, invalid, modules=project_modules, project_id="p"
        )
        self.assertEqual(result.files, ())
        stale = replace(
            index,
            documents=(
                replace(index.documents[0], context_token="stale"),
                *index.documents[1:],
            ),
        )
        result = generate_project_factory_artifacts(
            config, contexts, stale, modules=project_modules, project_id="p"
        )
        self.assertEqual(result.files, ())

    def context(self) -> SchemaContext:
        return SchemaContext(
            "main",
            build_schema(
                "type Book { id: ID! title: String } "
                "type Query { book(id: ID!): Book! greeting: String! }"
            ),
            "generation-1",
        )

    def test_companion_paths_reject_case_and_module_package_collisions(self) -> None:
        for names in (("Case", "case"), ("foo", "Foo.bar")):
            with self.subTest(names=names):
                modules = tuple(
                    ModuleSnapshot(
                        "p",
                        name,
                        f"file:///{name}.py",
                        1,
                        "from generated.main import graphql\n"
                        "query=graphql('{greeting}')",
                    )
                    for name in names
                )
                artifacts = self.generate(modules)
                self.assertEqual(artifacts.files, ())
                self.assertEqual(artifacts.diagnostics[0].code, "factory-generation")

    def test_fragment_reader_schema_alias_collisions_reject_atomically(self) -> None:
        from graphql import build_schema

        module = ModuleSnapshot(
            "p",
            "main",
            "file:///main.py",
            1,
            "from generated.main import graphql\n"
            "fields=graphql('fragment Fields on Book { title }')",
        )
        for alias in (
            "GraphqlDeskReader",
            "GraphqlDeskReadFragment",
            "GraphqlDeskUnsafeReadResult",
        ):
            with self.subTest(alias=alias):
                context = SchemaContext(
                    "main",
                    build_schema(
                        f"enum {alias} {{ VALUE }}\n"
                        "type Book { title: String } type Query { book: Book }"
                    ),
                    "generation-1",
                )
                with patch.object(self, "context", return_value=context):
                    artifacts = self.generate((module,))
                self.assertEqual(artifacts.files, ())
                self.assertIn("collide", artifacts.diagnostics[0].message)

    def test_captured_mapping_types_are_checked_before_artifact_emission(self) -> None:
        context = self.context()
        source = (
            "from typing import TYPE_CHECKING\n"
            "from typing_extensions import assert_type\n"
            "from generated.main import graphql\n"
            "from generated.sources.main import QueryResult\n"
            "from local_types import Text\n"
            "query=graphql('{greeting}')\n"
            "value=Text('hello')\n"
            "assert graphql.scalar('String',value) is value\n"
            "if TYPE_CHECKING:\n"
            " result: QueryResult={'greeting':value}\n"
            " assert_type(result['greeting'],Text)\n"
        )
        for declaration, valid in (
            ("class Text(str): pass", True),
            (
                "from typing import TypeAlias\n"
                "class _Text(str): pass\n"
                "Text:TypeAlias=_Text",
                True,
            ),
            (
                "import typing_extensions as t\n"
                "class _Text(str): pass\n"
                "Text:t.TypeAlias=_Text",
                True,
            ),
            (
                "from typing_extensions import TypedDict\n"
                "from typing import TypeAlias\n"
                "class Base(TypedDict):\n required: str\n"
                "Alias:TypeAlias=Base\n"
                "class Text(Alias,total=False):\n optional: int",
                True,
            ),
            ("from typing import TypeAlias\nText:TypeAlias=list[str]", True),
            (
                "from typing import TypeAlias,Annotated\n"
                "Text:TypeAlias=Annotated[str,'graphql-desk']",
                True,
            ),
            ("from typing import TypeAlias\nText:TypeAlias=bool", True),
            ("from typing import TypeAlias\nText:TypeAlias=str|None", True),
            ("from typing import TypeAlias\nText:TypeAlias=str|int", True),
            ("from typing import TypeAlias\nText:TypeAlias=int|str", True),
            ("from typing import TypeAlias\nText:TypeAlias=str|int|None", True),
            (
                "from typing_extensions import TypeAlias,Literal\n"
                "Text:TypeAlias=Literal['hello','world']",
                True,
            ),
            (
                "import typing\nText:typing.TypeAlias=typing.Literal['hello','world']",
                True,
            ),
            (
                "from typing import TypeAlias,Literal as L\n"
                "Text:TypeAlias=L['hello',1,-2,True,False,None,b'hello']",
                True,
            ),
            (
                "from typing import TypeAlias,Literal\n"
                "Maybe:TypeAlias=Literal['hello']\nclass Text(Maybe): pass",
                False,
            ),
            (
                "from typing import TypeAlias\nimport typing\n"
                "Text:TypeAlias=typing.Optional[str]",
                True,
            ),
            (
                "from typing import TypeAlias,Optional as Maybe\n"
                "Text:TypeAlias=Maybe[str]",
                True,
            ),
            (
                "from typing import TypeAlias,Optional\nMaybe:TypeAlias=Optional[str]\n"
                "class Text(Maybe): pass",
                False,
            ),
            ("from typing import TypeAlias\nText:TypeAlias=None|str", True),
            (
                "from typing import TypeAlias\nMaybe:TypeAlias=str|None\n"
                "Text:TypeAlias=Maybe",
                True,
            ),
            ("from nullable_types import Maybe as Text\n__all__=['Text']", True),
            (
                "from typing import TypeAlias\nMaybe:TypeAlias=str|None\n"
                "class Text(Maybe): pass",
                False,
            ),
            ("from bool_types import Flag as Text\n__all__=['Text']", True),
            (
                "from typing import TypeAlias\nFlag:TypeAlias=bool\n"
                "Text:TypeAlias=Flag",
                True,
            ),
            ("from typing import TypeAlias\nText:TypeAlias=list[bool]", True),
            ("class Text(bool): pass", False),
            (
                "from typing import TypeAlias\nFlag:TypeAlias=bool\n"
                "class Text(Flag): pass",
                False,
            ),
            (
                "from typing import TypeAlias\nclass Known(str): pass\n"
                "Text:TypeAlias=tuple[Known,...]",
                True,
            ),
            (
                "from typing import TypeAlias,Sequence\nclass Known(str): pass\n"
                "Text:TypeAlias=Sequence[Known]",
                True,
            ),
            ("class _Text(str): pass\nText:_Text=_Text", False),
            (
                "from typing import TypeAlias\n"
                "class _Text(str): pass\n"
                "Marker=TypeAlias\nText:Marker=_Text",
                False,
            ),
            ("class Other: pass", False),
            ("def Text() -> str: return 'x'", False),
            ("Text='PRIVATE_VALUE'", False),
            ("@unknown\nclass Text: pass", False),
        ):
            with self.subTest(declaration=declaration):
                authored = source
                if "str|int|None" in declaration:
                    authored = source.replace(
                        "value=Text('hello')",
                        "value:Text='hello'\nnumber:Text=7\nnone:Text=None\n"
                        "assert graphql.scalar('String',number) is number\n"
                        "assert graphql.scalar('String',none) is none",
                    )
                    authored += (
                        " bad=graphql.scalar('String',1.5)\n"
                        " bad=graphql.scalar('String',[1])\n"
                    )
                elif "str|int" in declaration or "int|str" in declaration:
                    authored = source.replace(
                        "value=Text('hello')",
                        "value:Text='hello'\nnumber:Text=7\n"
                        "assert graphql.scalar('String',number) is number",
                    )
                    authored += (
                        " int_result:QueryResult={'greeting':number}\n"
                        " bad=graphql.scalar('String',1.5)\n"
                        " bad=graphql.scalar('String',[1])\n"
                    )
                elif "Literal" in declaration:
                    authored = source.replace(
                        "value=Text('hello')", "value:Text='hello'"
                    )
                    authored += " bad=graphql.scalar('String','other')\n"
                    if "True,False,None" in declaration:
                        authored = authored.replace(
                            "value:Text='hello'",
                            "value:Text='hello'\n"
                            "items:tuple[Text,...]=('hello',1,-2,True,False,None,b'hello')\n"
                            "for item in items:\n"
                            " assert graphql.scalar('String',item) is item",
                        )
                elif "Annotated" in declaration:
                    authored = source.replace(
                        "value=Text('hello')", "value:Text='hello'"
                    )
                    authored += " bad=graphql.scalar('String',42)\n"
                elif (
                    "|None" in declaration
                    or "None|" in declaration
                    or "nullable_types" in declaration
                    or "Optional" in declaration
                ):
                    authored = source.replace(
                        "value=Text('hello')",
                        "value:Text='hello'\nnone:Text=None\n"
                        "assert graphql.scalar('String',none) is none",
                    )
                    authored += (
                        " null_result:QueryResult={'greeting':none}\n"
                        " bad=graphql.scalar('String',42)\n"
                    )
                elif (
                    "Text:TypeAlias=bool" in declaration
                    or "Text:TypeAlias=Flag" in declaration
                    or "from bool_types" in declaration
                ):
                    authored = source.replace("value=Text('hello')", "value:Text=True")
                    authored += " bad=graphql.scalar('String',1)\n"
                elif "Text:TypeAlias=list[bool]" in declaration:
                    authored = source.replace(
                        "value=Text('hello')", "value:Text=[True]"
                    )
                elif "Text:TypeAlias=list" in declaration:
                    authored = source.replace(
                        "value=Text('hello')", "value:Text=['hello']"
                    )
                elif "tuple[Known" in declaration or "Sequence[Known" in declaration:
                    authored = source.replace(
                        "from local_types import Text",
                        "from local_types import Text,Known",
                    ).replace(
                        "value=Text('hello')",
                        "value:Text=(Known('hello'),)"
                        if "tuple[Known" in declaration
                        else "value:Text=[Known('hello')]",
                    )
                if (
                    "Text:TypeAlias=list" in declaration
                    or "Sequence[Known" in declaration
                ):
                    authored += " bad=graphql.scalar('String',[1])\n"
                elif "tuple[Known" in declaration:
                    authored += " bad=graphql.scalar('String',(1,))\n"
                modules: tuple[ModuleSnapshot, ...] = (
                    ModuleSnapshot(
                        "p",
                        "main",
                        "file:///main.py",
                        1,
                        source.replace("Text('hello')", "Text(required='hello')")
                        if "TypedDict" in declaration
                        else authored,
                    ),
                    ModuleSnapshot(
                        "p", "local_types", "file:///local_types.py", 1, declaration
                    ),
                )
                if "from bool_types" in declaration:
                    modules += (
                        ModuleSnapshot(
                            "p",
                            "bool_types",
                            "file:///bool_types.py",
                            1,
                            "from typing import TypeAlias\nFlag:TypeAlias=bool",
                        ),
                    )
                if "from nullable_types" in declaration:
                    modules += (
                        ModuleSnapshot(
                            "p",
                            "nullable_types",
                            "file:///nullable_types.py",
                            1,
                            "from typing import TypeAlias\nMaybe:TypeAlias=str|None",
                        ),
                    )
                resolved = resolve_project(
                    modules,
                    bindings=(
                        ProjectBindings(
                            "p",
                            ("main",),
                            factories=(
                                FactoryBinding("generated.main", "graphql", "main"),
                            ),
                        ),
                    ),
                )
                index = index_fragments(resolved, schemas={("p", "main"): context})
                with patch(
                    "importlib.import_module",
                    side_effect=AssertionError("No project import"),
                ):
                    artifacts = generate_factory_artifacts(
                        context,
                        index,
                        modules=modules,
                        project_id="p",
                        generated_package="generated",
                        scalars=(
                            ScalarMapping(
                                "String", "local_types.Text", "local_types.Text"
                            ),
                        ),
                    )
                if valid:
                    with tempfile.TemporaryDirectory() as directory:
                        root = Path(directory)
                        self.write(root, artifacts, modules)
                        errors = {
                            n
                            for n, line in enumerate(authored.splitlines(), 1)
                            if "bad=graphql.scalar" in line
                        }
                        self.check(root, {"main.py": errors} if errors else None)
                        runtime = subprocess.run(
                            [sys.executable, "main.py"],
                            cwd=root,
                            capture_output=True,
                            text=True,
                        )
                        self.assertEqual(
                            runtime.returncode, 0, runtime.stdout + runtime.stderr
                        )
                else:
                    self.assertEqual(artifacts.files, ())
                    self.assertIn(
                        "Configured mapping", artifacts.diagnostics[0].message
                    )
                    self.assertNotIn("PRIVATE_VALUE", artifacts.diagnostics[0].message)

    def test_literal_enum_mapping_preserves_checker_values_and_runtime_identity(
        self,
    ) -> None:
        source = """from typing import TYPE_CHECKING
from typing_extensions import assert_type
from generated.main import graphql
from generated.sources.main import QueryResult
from local_types import State
query=graphql('{greeting}')
value:State='HELLO'
assert graphql.scalar('State',value) is value
if TYPE_CHECKING:
 result:QueryResult={'greeting':value}
 assert_type(result['greeting'],State)
 bad=graphql.scalar('State','OTHER')
"""
        main = ModuleSnapshot("p", "main", "file:///main.py", 1, source)
        local = ModuleSnapshot(
            "p",
            "local_types",
            "file:///local_types.py",
            1,
            "from typing import TypeAlias,Literal\n"
            "State:TypeAlias=Literal['HELLO','WORLD']",
        )
        context = SchemaContext(
            "main",
            build_schema("enum State { HELLO WORLD } type Query { greeting:State! }"),
            "literal-enum",
        )
        modules = (main, local)
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
        with patch(
            "importlib.import_module", side_effect=AssertionError("No project imports")
        ):
            artifacts = generate_factory_artifacts(
                context,
                index_fragments(resolved, schemas={("p", "main"): context}),
                modules=modules,
                project_id="p",
                generated_package="generated",
                enums=(("State", "local_types.State"),),
            )
        self.assertEqual(artifacts.diagnostics, ())
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write(root, artifacts, modules)
            errors = {
                n for n, line in enumerate(source.splitlines(), 1) if "bad=" in line
            }
            self.check(root, {"main.py": errors})
            runtime = subprocess.run(
                [sys.executable, "main.py"], cwd=root, capture_output=True, text=True
            )
            self.assertEqual(runtime.returncode, 0, runtime.stdout + runtime.stderr)

    def test_stub_backed_external_mappings_flow_through_helpers_and_results(
        self,
    ) -> None:
        """Generated imports defer external mapping types to ordinary stub lookup."""
        context = SchemaContext(
            "main",
            build_schema(
                "scalar Date enum Color { RED BLUE } "
                "type Query { date(input: Date!): Date! color: Color! }"
            ),
            "external-mapping-stubs",
        )
        source = """from typing import TYPE_CHECKING
from typing_extensions import assert_type
from external_types import Color, DateInput, DateOutput
from generated.main import graphql
from generated.sources.main import QueryResult,QueryVariables
query=graphql('query Read($date:Date!) { date(input:$date) color }')
date=DateInput()
color=Color.RED
assert graphql.scalar('Date',date) is date
assert graphql.scalar('Color',color) is color
assert graphql.scalar('Date',None) is None
variables:QueryVariables={'date':date}
assert_type(variables['date'],DateInput)
if TYPE_CHECKING:
 result:QueryResult={'date':DateOutput(),'color':Color.RED}
 assert_type(result['date'],DateOutput)
 assert_type(result['color'],Color)
 bad_variables:QueryVariables={'date':DateOutput()} # error
 graphql.scalar('Date',DateOutput()) # error
 graphql.scalar('Color','RED') # error
"""
        modules = (ModuleSnapshot("p", "main", "file:///main.py", 1, source),)
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
        self.assertEqual(resolved.diagnostics, ())
        index = index_fragments(resolved, schemas={("p", "main"): context})
        with patch(
            "importlib.import_module", side_effect=AssertionError("No project import")
        ):
            artifacts = generate_factory_artifacts(
                context,
                index,
                modules=modules,
                project_id="p",
                generated_package="generated",
                scalars=(
                    ScalarMapping(
                        "Date", "external_types.DateInput", "external_types.DateOutput"
                    ),
                ),
                enums=(("Color", "external_types.Color"),),
            )
        self.assertEqual(artifacts.diagnostics, ())
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write(root, artifacts, modules)
            (root / "external_types.pyi").write_text(
                "from enum import Enum\n"
                "class DateInput: ...\n"
                "class DateOutput: ...\n"
                "class Color(Enum):\n    RED = ...\n    BLUE = ...\n",
                encoding="utf8",
            )
            (root / "external_types.py").write_text(
                "from enum import Enum\n"
                "class DateInput: pass\n"
                "class DateOutput: pass\n"
                "class Color(Enum):\n    RED=1\n    BLUE=2\n",
                encoding="utf8",
            )
            errors = {
                number
                for number, line in enumerate(source.splitlines(), 1)
                if "# error" in line
            }
            self.check(root, {"main.py": errors})
            runtime = subprocess.run(
                [sys.executable, "main.py"],
                cwd=root,
                capture_output=True,
                text=True,
            )
            self.assertEqual(runtime.returncode, 0, runtime.stdout + runtime.stderr)

    def test_external_mappings_are_isolated_between_project_schemas(self) -> None:
        config, _, _, _ = self.multi_schema()
        config = replace(
            config,
            schemas=(
                replace(
                    config.schemas[0],
                    scalars=(
                        ScalarMapping(
                            "Token", "alpha_types.TokenInput", "alpha_types.TokenOutput"
                        ),
                    ),
                    enums=(("Shade", "alpha_types.Shade"),),
                ),
                replace(
                    config.schemas[1],
                    scalars=(
                        ScalarMapping(
                            "Token", "beta_types.TokenInput", "beta_types.TokenOutput"
                        ),
                    ),
                    enums=(("Shade", "beta_types.Shade"),),
                ),
            ),
        )
        schema = build_schema(
            "scalar Token enum Shade { LIGHT DARK } "
            "type Item { value: Token! shade: Shade! } "
            "type Query { item(token: Token!, shade: Shade!): Item! }"
        )
        contexts = tuple(
            SchemaContext(name, schema, "external:" + name)
            for name in ("alpha", "beta")
        )
        source = """from typing import TYPE_CHECKING
from typing_extensions import assert_type
from alpha_types import (
 Shade as AlphaShade,
 TokenInput as AlphaInput,
 TokenOutput as AlphaOutput,
)
from beta_types import (
 Shade as BetaShade,
 TokenInput as BetaInput,
 TokenOutput as BetaOutput,
)
from generated.alpha import graphql as alpha
from generated.beta import graphql as beta
from generated.sources.main import (
 AlphaDocResult, AlphaDocVariables, BetaDocResult, BetaDocVariables
)
from graphql_desk import TypedDocument

alpha_doc = alpha(
 'query Alpha($token:Token!, $shade:Shade!) '
 '{item(token:$token, shade:$shade) {value shade}}'
)
beta_doc = beta(
 'query Beta($token:Token!, $shade:Shade!) '
 '{item(token:$token, shade:$shade) {value shade}}'
)
alpha_typed: TypedDocument[AlphaDocResult, AlphaDocVariables] = alpha_doc
beta_typed: TypedDocument[BetaDocResult, BetaDocVariables] = beta_doc
alpha_input: AlphaInput = AlphaInput()
beta_input: BetaInput = BetaInput()
assert alpha.scalar('Token', alpha_input) is alpha_input
assert beta.scalar('Token', beta_input) is beta_input
assert alpha.scalar('Shade', AlphaShade.LIGHT) is AlphaShade.LIGHT
assert beta.scalar('Shade', BetaShade.LIGHT) is BetaShade.LIGHT
alpha_variables: AlphaDocVariables = {'token': alpha_input, 'shade': AlphaShade.LIGHT}
beta_variables: BetaDocVariables = {'token': beta_input, 'shade': BetaShade.DARK}
assert_type(alpha_variables['token'], AlphaInput)
assert_type(beta_variables['token'], BetaInput)
if TYPE_CHECKING:
 def inspect(a: AlphaDocResult, b: BetaDocResult) -> None:
  assert_type(a['item']['value'], AlphaOutput)
  assert_type(a['item']['shade'], AlphaShade)
  assert_type(b['item']['value'], BetaOutput)
  assert_type(b['item']['shade'], BetaShade)
 bad_alpha: AlphaDocVariables = {'token': beta_input, 'shade': BetaShade.LIGHT} # error
 bad_beta: BetaDocVariables = {'token': alpha_input, 'shade': AlphaShade.LIGHT} # error
 alpha.scalar('Token', beta_input) # error
 beta.scalar('Token', alpha_input) # error
 alpha.scalar('Shade', BetaShade.LIGHT) # error
 beta.scalar('Shade', AlphaShade.LIGHT) # error
 wrong_schema: TypedDocument[AlphaDocResult, AlphaDocVariables] = beta_doc # error
"""
        modules = (
            ModuleSnapshot("p", "main", "file:///main.py", 1, source),
            ModuleSnapshot(
                "p",
                "alpha_types",
                "file:///alpha_types.py",
                1,
                "from enum import Enum\n"
                "class TokenInput: pass\nclass TokenOutput: pass\n"
                "class Shade(Enum):\n LIGHT = 1\n DARK = 2\n",
            ),
            ModuleSnapshot(
                "p",
                "beta_types",
                "file:///beta_types.py",
                1,
                "from enum import Enum\n"
                "class TokenInput: pass\nclass TokenOutput: pass\n"
                "class Shade(Enum):\n LIGHT = 1\n DARK = 2\n",
            ),
        )
        resolved = resolve_project(
            modules, bindings=(project_bindings(config, project_id="p"),)
        )
        self.assertEqual(resolved.diagnostics, ())
        index = index_fragments(
            resolved, schemas={("p", item.schema_name): item for item in contexts}
        )
        self.assertEqual(index.diagnostics, ())
        with patch(
            "importlib.import_module", side_effect=AssertionError("No project imports")
        ):
            artifacts = generate_project_factory_artifacts(
                config, contexts, index, modules=modules, project_id="p"
            )
        self.assertEqual(artifacts.diagnostics, ())
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write(root, artifacts, modules)
            for module in ("alpha_types", "beta_types"):
                (root / f"{module}.pyi").write_text(
                    "from enum import Enum\n"
                    "class TokenInput: ...\nclass TokenOutput: ...\n"
                    "class Shade(Enum):\n    LIGHT = ...\n    DARK = ...\n",
                    encoding="utf8",
                )
            errors = {
                number
                for number, line in enumerate(source.splitlines(), 1)
                if "# error" in line
            }
            self.check(root, {"main.py": errors})
            runtime = subprocess.run(
                [sys.executable, "main.py"], cwd=root, capture_output=True, text=True
            )
            self.assertEqual(runtime.returncode, 0, runtime.stdout + runtime.stderr)

    def test_standard_decorated_mappings_check_factory_values_and_final_bases(
        self,
    ) -> None:
        context = self.context()
        for label, declaration, value in (
            (
                "plain",
                "from dataclasses import dataclass\n@dataclass\nclass Text: value:int",
                "Text(value=1)",
            ),
            (
                "frozen",
                "import dataclasses\n"
                "@dataclasses.dataclass(frozen=True,slots=True)\n"
                "class Text(object): value:int",
                "Text(value=1)",
            ),
            (
                "mutable",
                "from dataclasses import dataclass\n"
                "@dataclass(frozen=False,slots=False)\n"
                "class Text: value:int",
                "Text(value=1)",
            ),
            ("final", "from typing import final\n@final\nclass Text: pass", "Text()"),
            (
                "final_outer",
                "from dataclasses import dataclass\n"
                "from typing import final\n"
                "@final\n"
                "@dataclass(frozen=True,slots=True)\n"
                "class Text: value:int",
                "Text(value=1)",
            ),
            (
                "final_inner",
                "from dataclasses import dataclass\n"
                "from typing import final\n"
                "@dataclass(frozen=True,slots=True)\n"
                "@final\n"
                "class Text: value:int",
                "Text(value=1)",
            ),
        ):
            with self.subTest(label=label):
                source = f"""from typing import TYPE_CHECKING
from typing_extensions import assert_type
from generated.main import graphql
from generated.sources.main import QueryResult
from exports import Text
query=graphql('{{greeting}}')
value={value}
assert graphql.scalar('String',value) is value
if TYPE_CHECKING:
 result:QueryResult={{'greeting':value}}
 assert_type(result['greeting'],Text)
 bad=graphql.scalar('String',42) # error
"""
                if label.startswith("final"):
                    source += " from local_types import Text as Direct\n"
                    source += " class BadDirect(Direct): pass # error\n"
                    source += " class BadReexport(Text): pass # error\n"
                    source += "runtime_child=type('RuntimeChild',(Text,),{})\n"
                    source += "assert issubclass(runtime_child,Text)\n"
                else:
                    declaration += "\nclass Direct(Text): pass"
                    source += "class Reexported(Text): pass\n"
                modules = (
                    ModuleSnapshot("p", "main", "file:///main.py", 1, source),
                    ModuleSnapshot(
                        "p", "local_types", "file:///local_types.py", 1, declaration
                    ),
                    ModuleSnapshot(
                        "p",
                        "exports",
                        "file:///exports.py",
                        1,
                        "from local_types import Text as Text",
                    ),
                )
                resolved = resolve_project(
                    modules,
                    bindings=(
                        ProjectBindings(
                            "p",
                            ("main",),
                            factories=(
                                FactoryBinding("generated.main", "graphql", "main"),
                            ),
                        ),
                    ),
                )
                with patch(
                    "importlib.import_module",
                    side_effect=AssertionError("No project import"),
                ):
                    artifacts = generate_factory_artifacts(
                        context,
                        index_fragments(resolved, schemas={("p", "main"): context}),
                        modules=modules,
                        project_id="p",
                        generated_package="generated",
                        scalars=(
                            ScalarMapping("String", "exports.Text", "exports.Text"),
                        ),
                    )
                self.assertEqual(artifacts.diagnostics, ())
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    self.write(root, artifacts, modules)
                    errors = {
                        n
                        for n, line in enumerate(source.splitlines(), 1)
                        if "# error" in line
                    }
                    self.check(root, {"main.py": errors})
                    runtime = subprocess.run(
                        [sys.executable, "main.py"],
                        cwd=root,
                        capture_output=True,
                        text=True,
                    )
                    self.assertEqual(
                        runtime.returncode, 0, runtime.stdout + runtime.stderr
                    )

    def test_unique_enum_mapping_preserves_generated_types_and_runtime_identity(
        self,
    ) -> None:
        context = SchemaContext(
            "main",
            build_schema("enum Color { RED BLUE } type Query { color:Color! }"),
            "unique-enum-mapping",
        )
        source = """from typing import TYPE_CHECKING
from typing_extensions import assert_type
from enum_types import Color
from generated.main import graphql
from generated.sources.main import QueryResult
query=graphql('query Read { color }')
value=Color.RED
assert graphql.scalar('Color',value) is value
if TYPE_CHECKING:
 result:QueryResult={'color':value}
 assert_type(result['color'],Color)
 bad=graphql.scalar('Color','RED') # error
"""
        modules = (
            ModuleSnapshot("p", "main", "file:///main.py", 1, source),
            ModuleSnapshot(
                "p",
                "enum_types",
                "file:///enum_types.py",
                1,
                "import enum\n"
                "@enum.unique\n"
                "class Color(enum.Enum):\n RED='red'\n BLUE='blue'\n",
            ),
        )
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
        self.assertEqual(resolved.diagnostics, ())
        with patch(
            "importlib.import_module", side_effect=AssertionError("No project imports")
        ):
            artifacts = generate_factory_artifacts(
                context,
                index_fragments(resolved, schemas={("p", "main"): context}),
                modules=modules,
                project_id="p",
                generated_package="generated",
                enums=(("Color", "enum_types.Color"),),
            )
        self.assertEqual(artifacts.diagnostics, ())
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write(root, artifacts, modules)
            errors = {
                number
                for number, line in enumerate(source.splitlines(), 1)
                if "# error" in line
            }
            self.check(root, {"main.py": errors})
            runtime = subprocess.run(
                [sys.executable, "main.py"],
                cwd=root,
                capture_output=True,
                text=True,
            )
            self.assertEqual(runtime.returncode, 0, runtime.stdout + runtime.stderr)

    def test_standard_collection_aliases_check_original_values_and_class_bases(
        self,
    ) -> None:
        context = self.context()
        for spelling, value, wrong in (
            ("set", "{1,2}", "{'bad'}"),
            ("frozenset", "frozenset({1,2})", "frozenset({'bad'})"),
            ("typing.List", "[1,2]", "['bad']"),
            ("typing.Set", "{1,2}", "{'bad'}"),
            ("typing.FrozenSet", "frozenset({1,2})", "frozenset({'bad'})"),
            ("typing.AbstractSet", "{1,2}", "{'bad'}"),
            ("collections.abc.Sequence", "[1,2]", "['bad']"),
            ("collections.abc.Set", "{1,2}", "{'bad'}"),
            ("typing.Dict", "{'ok':1}", "{1:'bad'}"),
        ):
            with self.subTest(spelling=spelling):
                imports = "from typing import TypeAlias\n"
                namespace, _, constructor = spelling.rpartition(".")
                if namespace:
                    imports += f"from {namespace} import {constructor}\n"
                arguments = "str,int" if spelling == "typing.Dict" else "int"
                declaration = (
                    imports + f"Text:TypeAlias={constructor}[{arguments}]\n"
                    "class Direct(Text): pass\n"
                )
                source = f"""from typing import TYPE_CHECKING
from typing_extensions import assert_type
from generated.main import graphql
from generated.sources.main import QueryResult
from exports import Text
query=graphql('{{greeting}}')
value:Text={value}
assert graphql.scalar('String',value) is value
class Reexported(Text): pass
if TYPE_CHECKING:
 result:QueryResult={{'greeting':value}}
 assert_type(result['greeting'],Text)
 bad=graphql.scalar('String',{wrong}) # error
 bad2=graphql.scalar('String',42) # error
"""
                modules = (
                    ModuleSnapshot("p", "main", "file:///main.py", 1, source),
                    ModuleSnapshot(
                        "p", "local_types", "file:///local_types.py", 1, declaration
                    ),
                    ModuleSnapshot(
                        "p",
                        "exports",
                        "file:///exports.py",
                        1,
                        "from local_types import Text as Text",
                    ),
                )
                resolved = resolve_project(
                    modules,
                    bindings=(
                        ProjectBindings(
                            "p",
                            ("main",),
                            factories=(
                                FactoryBinding("generated.main", "graphql", "main"),
                            ),
                        ),
                    ),
                )
                with patch(
                    "importlib.import_module",
                    side_effect=AssertionError("No project import"),
                ):
                    artifacts = generate_factory_artifacts(
                        context,
                        index_fragments(resolved, schemas={("p", "main"): context}),
                        modules=modules,
                        project_id="p",
                        generated_package="generated",
                        scalars=(
                            ScalarMapping("String", "exports.Text", "exports.Text"),
                        ),
                    )
                self.assertEqual(artifacts.diagnostics, ())
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    self.write(root, artifacts, modules)
                    errors = {
                        n
                        for n, line in enumerate(source.splitlines(), 1)
                        if "# error" in line
                    }
                    self.check(root, {"main.py": errors})
                    runtime = subprocess.run(
                        [sys.executable, "main.py"],
                        cwd=root,
                        capture_output=True,
                        text=True,
                    )
                    self.assertEqual(
                        runtime.returncode, 0, runtime.stdout + runtime.stderr
                    )

    def test_fixed_tuple_aliases_check_original_factory_values_and_class_bases(
        self,
    ) -> None:
        context = self.context()
        for constructor in ("tuple", "Tuple"):
            for arguments, value, wrong in (
                ("int", "(1,)", "('bad',)"),
                ("float,float", "(1.0,2.0)", "(1.0,'bad')"),
                ("str,int,float", "('x',2,3.0)", "('x',2,'bad')"),
                ("str,...", "('x','y')", "('x',2)"),
            ):
                with self.subTest(constructor=constructor, arguments=arguments):
                    imports = "from typing import TypeAlias\n"
                    if constructor == "Tuple":
                        imports += "from typing import Tuple\n"
                    declaration = (
                        imports + f"Text:TypeAlias={constructor}[{arguments}]\n"
                        "class Direct(Text): pass\n"
                    )
                    source = f"""from typing import TYPE_CHECKING
from typing_extensions import assert_type
from generated.main import graphql
from generated.sources.main import QueryResult
from exports import Text
query=graphql('{{greeting}}')
value:Text={value}
assert graphql.scalar('String',value) is value
class Reexported(Text): pass
if TYPE_CHECKING:
 result:QueryResult={{'greeting':value}}
 assert_type(result['greeting'],Text)
 bad=graphql.scalar('String',{wrong}) # error
 bad2=graphql.scalar('String',[]) # error
"""
                    if "..." not in arguments:
                        source += " bad3=graphql.scalar('String',()) # error\n"
                    modules = (
                        ModuleSnapshot("p", "main", "file:///main.py", 1, source),
                        ModuleSnapshot(
                            "p", "local_types", "file:///local_types.py", 1, declaration
                        ),
                        ModuleSnapshot(
                            "p",
                            "exports",
                            "file:///exports.py",
                            1,
                            "from local_types import Text as Text",
                        ),
                    )
                    resolved = resolve_project(
                        modules,
                        bindings=(
                            ProjectBindings(
                                "p",
                                ("main",),
                                factories=(
                                    FactoryBinding("generated.main", "graphql", "main"),
                                ),
                            ),
                        ),
                    )
                    with patch(
                        "importlib.import_module",
                        side_effect=AssertionError("No project import"),
                    ):
                        artifacts = generate_factory_artifacts(
                            context,
                            index_fragments(resolved, schemas={("p", "main"): context}),
                            modules=modules,
                            project_id="p",
                            generated_package="generated",
                            scalars=(
                                ScalarMapping("String", "exports.Text", "exports.Text"),
                            ),
                        )
                    self.assertEqual(artifacts.diagnostics, ())
                    with tempfile.TemporaryDirectory() as directory:
                        root = Path(directory)
                        self.write(root, artifacts, modules)
                        errors = {
                            n
                            for n, line in enumerate(source.splitlines(), 1)
                            if "# error" in line
                        }
                        self.check(root, {"main.py": errors})
                        runtime = subprocess.run(
                            [sys.executable, "main.py"],
                            cwd=root,
                            capture_output=True,
                            text=True,
                        )
                        self.assertEqual(
                            runtime.returncode, 0, runtime.stdout + runtime.stderr
                        )

    def test_mapping_aliases_type_original_scalar_calls_and_valid_class_bases(
        self,
    ) -> None:
        context = self.context()
        for constructor in (
            "dict",
            "typing.Mapping",
            "collections.abc.Mapping",
            "typing.MutableMapping",
            "collections.abc.MutableMapping",
        ):
            with self.subTest(constructor=constructor):
                imports = "from typing import TypeAlias\n"
                expression = constructor
                if constructor != "dict":
                    namespace, expression = constructor.rsplit(".", 1)
                    imports += f"from {namespace} import {expression}\n"
                declaration = (
                    imports
                    + f"Text:TypeAlias={expression}[str,object]\n"
                    + "class Direct(Text): pass\n"
                )
                source = """from typing import TYPE_CHECKING
from typing_extensions import assert_type
from generated.main import graphql
from generated.sources.main import QueryResult
from exports import Text
query=graphql('{greeting}')
value:Text={'ok':True,'nested':{'x':1}}
assert graphql.scalar('String',value) is value
class Reexported(Text): pass
if TYPE_CHECKING:
 result:QueryResult={'greeting':value}
 assert_type(result['greeting'],Text)
 bad=graphql.scalar('String',{1:'bad'}) # error
 bad2=graphql.scalar('String',[]) # error
"""
                modules = (
                    ModuleSnapshot("p", "main", "file:///main.py", 1, source),
                    ModuleSnapshot(
                        "p", "local_types", "file:///local_types.py", 1, declaration
                    ),
                    ModuleSnapshot(
                        "p",
                        "exports",
                        "file:///exports.py",
                        1,
                        "from local_types import Text as Text",
                    ),
                )
                resolved = resolve_project(
                    modules,
                    bindings=(
                        ProjectBindings(
                            "p",
                            ("main",),
                            factories=(
                                FactoryBinding("generated.main", "graphql", "main"),
                            ),
                        ),
                    ),
                )
                with patch(
                    "importlib.import_module",
                    side_effect=AssertionError("No project import"),
                ):
                    artifacts = generate_factory_artifacts(
                        context,
                        index_fragments(resolved, schemas={("p", "main"): context}),
                        modules=modules,
                        project_id="p",
                        generated_package="generated",
                        scalars=(
                            ScalarMapping("String", "exports.Text", "exports.Text"),
                        ),
                    )
                self.assertEqual(artifacts.diagnostics, ())
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    self.write(root, artifacts, modules)
                    errors = {
                        n
                        for n, line in enumerate(source.splitlines(), 1)
                        if "# error" in line
                    }
                    self.check(root, {"main.py": errors})
                    runtime = subprocess.run(
                        [sys.executable, "main.py"],
                        cwd=root,
                        capture_output=True,
                        text=True,
                    )
                    self.assertEqual(
                        runtime.returncode, 0, runtime.stdout + runtime.stderr
                    )

    def test_collection_abc_aliases_type_mapped_scalar_values(self) -> None:
        context = SchemaContext(
            "main",
            build_schema("scalar Custom type Query { value: Custom! }"),
            "collection-alias",
        )
        for constructor, value, invalid in (
            ("typing.Iterable", "['value']", "[1]"),
            ("collections.abc.Iterable", "['value']", "[1]"),
            ("typing.Collection", "['value']", "[1]"),
            ("collections.abc.Collection", "['value']", "[1]"),
            ("typing.MutableSequence", "['value']", "[1]"),
            ("collections.abc.MutableSequence", "['value']", "[1]"),
            ("typing.MutableSet", "{'value'}", "{1}"),
            ("collections.abc.MutableSet", "{'value'}", "{1}"),
        ):
            with self.subTest(constructor=constructor):
                namespace, name = constructor.rsplit(".", 1)
                declaration = (
                    "from typing import TypeAlias\n"
                    f"from {namespace} import {name}\n"
                    f"ValueType:TypeAlias={name}[str]\n"
                )
                source = f"""from typing import TYPE_CHECKING
from typing_extensions import assert_type
from generated.main import graphql
from generated.sources.main import QueryResult
from local_types import ValueType
query=graphql('{{value}}')
value:ValueType={value}
assert graphql.scalar('Custom',value) is value
if TYPE_CHECKING:
 result:QueryResult={{'value':value}}
 assert_type(result['value'],ValueType)
 wrong: str=result['value'] # error
 invalid:ValueType={invalid} # error
"""
                modules = (
                    ModuleSnapshot("p", "main", "file:///main.py", 1, source),
                    ModuleSnapshot(
                        "p", "local_types", "file:///local_types.py", 1, declaration
                    ),
                )
                resolved = resolve_project(
                    modules,
                    bindings=(
                        ProjectBindings(
                            "p",
                            ("main",),
                            factories=(
                                FactoryBinding("generated.main", "graphql", "main"),
                            ),
                        ),
                    ),
                )
                self.assertEqual(resolved.diagnostics, ())
                with patch(
                    "importlib.import_module",
                    side_effect=AssertionError("No project import"),
                ):
                    artifacts = generate_factory_artifacts(
                        context,
                        index_fragments(resolved, schemas={("p", "main"): context}),
                        modules=modules,
                        project_id="p",
                        generated_package="generated",
                        scalars=(
                            ScalarMapping(
                                "Custom",
                                "local_types.ValueType",
                                "local_types.ValueType",
                            ),
                        ),
                    )
                self.assertEqual(artifacts.diagnostics, ())
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    self.write(root, artifacts, modules)
                    errors = {
                        number
                        for number, line in enumerate(source.splitlines(), 1)
                        if "# error" in line
                    }
                    self.check(root, {"main.py": errors})
                    runtime = subprocess.run(
                        [sys.executable, "main.py"],
                        cwd=root,
                        capture_output=True,
                        text=True,
                    )
                    self.assertEqual(
                        runtime.returncode, 0, runtime.stdout + runtime.stderr
                    )

    def test_container_alias_union_types_original_scalar_and_generated_values(
        self,
    ) -> None:
        context = SchemaContext(
            "main",
            build_schema(
                "type Query { greeting: String! echo(value: String!): String! }"
            ),
            "container-union-alias",
        )
        source = """from typing import TYPE_CHECKING
from typing_extensions import assert_type
from generated.main import graphql
from generated.sources.main import QueryResult,QueryVariables
from local_types import ValueList
query=graphql('query Read($value:String!){greeting echo(value:$value)}')
value:ValueList=['hello',1,None]
assert graphql.scalar('String',value) is value
variables:QueryVariables={'value':value}
if TYPE_CHECKING:
 result:QueryResult={'greeting':value,'echo':value}
 assert_type(result['greeting'],ValueList)
 assert_type(result['echo'],ValueList)
 assert_type(variables['value'],ValueList)
 wrong: list[str] = result['greeting'] # error
 bad_variables:QueryVariables={'value':[1.5]} # error
 bad_scalar=graphql.scalar('String',[1.5]) # error
"""
        modules = (
            ModuleSnapshot("p", "main", "file:///main.py", 1, source),
            ModuleSnapshot(
                "p",
                "local_types",
                "file:///local_types.py",
                1,
                "from typing import TypeAlias,Union\n"
                "ValueList:TypeAlias=list[Union[str|int,None]]\n",
            ),
        )
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
        self.assertEqual(resolved.diagnostics, ())
        with patch(
            "importlib.import_module", side_effect=AssertionError("No project import")
        ):
            artifacts = generate_factory_artifacts(
                context,
                index_fragments(resolved, schemas={("p", "main"): context}),
                modules=modules,
                project_id="p",
                generated_package="generated",
                scalars=(
                    ScalarMapping(
                        "String", "local_types.ValueList", "local_types.ValueList"
                    ),
                ),
            )
        self.assertEqual(artifacts.diagnostics, ())
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write(root, artifacts, modules)
            errors = {
                number
                for number, line in enumerate(source.splitlines(), 1)
                if "# error" in line
            }
            self.check(root, {"main.py": errors})
            runtime = subprocess.run(
                [sys.executable, "main.py"],
                cwd=root,
                capture_output=True,
                text=True,
            )
            self.assertEqual(runtime.returncode, 0, runtime.stdout + runtime.stderr)

    def test_alias_bases_preserve_checker_evidence_and_runtime_behavior(
        self,
    ) -> None:
        for source in (
            "class Bad(bool): pass\n",
            "from typing import TypeAlias\nFlag:TypeAlias=bool\n"
            "class Bad(Flag): pass\n",
            "from typing import TypeAlias\nMaybe:TypeAlias=str|None\n"
            "class Bad(Maybe): pass\n",
            "from typing import TypeAlias\nMaybe:TypeAlias=str|int\n"
            "class Bad(Maybe): pass\n",
            "from typing import TypeAlias\nMaybe:TypeAlias=str|str\n"
            "class Bad(Maybe): pass\n",
            "from typing import TypeAlias\nMaybe:TypeAlias=str|None\n"
            "Alias:TypeAlias=Maybe\nclass Bad(Alias): pass\n",
            "from typing import TypeAlias,Optional\nMaybe:TypeAlias=Optional[str]\n"
            "class Bad(Maybe): pass\n",
            "from typing import TypeAlias,Optional\nMaybe:TypeAlias=Optional[str]\n"
            "Alias:TypeAlias=Maybe\nclass Bad(Alias): pass\n",
            "from typing import TypeAlias,Literal\nMaybe:TypeAlias=Literal['hello']\n"
            "class Bad(Maybe): pass\n",
            "from typing import TypeAlias,Literal\nMaybe:TypeAlias=Literal['hello']\n"
            "Alias:TypeAlias=Maybe\nclass Bad(Alias): pass\n",
        ):
            with (
                self.subTest(source=source),
                tempfile.TemporaryDirectory() as directory,
            ):
                root = Path(directory)
                (root / "bad.py").write_text(source, encoding="utf8")
                errors = {
                    n
                    for n, line in enumerate(source.splitlines(), 1)
                    if line.startswith("class Bad")
                }
                if "Literal" in source or "str|str" in source:
                    # Pyright accepts these bases; mypy and preflight reject.
                    # Duplicate unions also reduce to a valid base at runtime.
                    (root / "pyrightconfig.json").write_text(
                        json.dumps(
                            {"typeCheckingMode": "strict", "include": ["bad.py"]}
                        ),
                        encoding="utf8",
                    )
                    mypy = subprocess.run(
                        [sys.executable, "-m", "mypy", "--strict", "bad.py"],
                        cwd=root,
                        capture_output=True,
                        text=True,
                    )
                    self.assertEqual(mypy.returncode, 1, mypy.stdout + mypy.stderr)
                    self.assertTrue(
                        any(f"bad.py:{line}: error:" in mypy.stdout for line in errors)
                    )
                    pyright = subprocess.run(
                        [
                            sys.executable,
                            "-m",
                            "pyright",
                            "--pythonpath",
                            sys.executable,
                            "--outputjson",
                            "bad.py",
                        ],
                        cwd=root,
                        capture_output=True,
                        text=True,
                    )
                    self.assertEqual(
                        pyright.returncode, 0, pyright.stdout + pyright.stderr
                    )
                    self.assertEqual(
                        json.loads(pyright.stdout)["summary"]["errorCount"], 0
                    )
                else:
                    self.check(root, {"bad.py": errors})
                runtime = subprocess.run(
                    [sys.executable, "bad.py"], cwd=root, capture_output=True, text=True
                )
                if "str|str" in source:
                    self.assertEqual(runtime.returncode, 0, runtime.stderr)
                else:
                    self.assertNotEqual(runtime.returncode, 0)
                    self.assertIn("TypeError", runtime.stderr)
                if "bool" in source:
                    self.assertIn("bool", runtime.stderr)

    def test_direct_function_and_async_calls_infer_in_original_source(self) -> None:
        source = """import asyncio
from typing import Literal
from typing_extensions import assert_type
from generated.main import graphql
fields=graphql("fragment F on Query { greeting }")
def build() -> None:
 seed=1
 def helper() -> int:
  nonlocal seed
  return seed
 class Helper:
  global unrelated
  value=1
 assert helper()==Helper.value
 doc=graphql("query Q { ...F }",(fields,),operation="Q")
 assert_type(doc.ast.definitions[0].name.value,Literal["Q"])
 assert doc.operation_name=="Q"
 assert len(doc.ast.definitions)==2
async def async_build() -> None:
 def helper() -> str:
  TEXT="local"
  return TEXT
 async def other() -> int:
  global unrelated
  return 1
 assert helper()=="local"
 assert await other()==1
 doc=graphql("query Async { greeting }")
 assert_type(doc.ast.definitions[0].name.value,Literal["Async"])
 assert doc.operation_name=="Async"
build()
asyncio.run(async_build())
"""
        modules = (ModuleSnapshot("p", "main", "file:///main.py", 1, source),)
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

    def test_provenance_keeps_selected_operations_and_actual_coalesced_owner(
        self,
    ) -> None:
        source = """from generated.main import graphql
first=graphql('query A {greeting} query B {book(id:"1"){title}}',operation='A')
second=graphql('query A {greeting} query B {book(id:"1"){title}}',operation='B')
again=graphql('query A {greeting} query B {book(id:"1"){title}}',operation='B')
implicit=graphql('query Single {greeting}')
invalid=graphql('query Invalid {missing}')
invalid_again=graphql('query Invalid {missing}')
"""
        modules = (ModuleSnapshot("p", "main", "file:///main.py", 7, source),)
        result = self.generate(modules)
        self.assertTrue(result.files, result.diagnostics)
        records = {record.binding: record for record in result.provenance}
        self.assertEqual(len(records), 6)
        self.assertEqual(records["first"].selector, "A")
        self.assertEqual(records["first"].operation_name, "A")
        self.assertEqual(records["first"].operation_kind, "query")
        self.assertEqual(records["second"].operation_name, "B")
        self.assertEqual(records["again"].canonical_owner, records["second"].owner)
        self.assertNotEqual(records["again"].owner, records["second"].owner)
        self.assertNotEqual(
            records["again"].declaration_path, records["second"].declaration_path
        )
        self.assertEqual(records["implicit"].selector, None)
        self.assertEqual(records["implicit"].operation_name, "Single")
        self.assertEqual(records["invalid"].status, "unavailable")
        self.assertEqual(
            records["invalid"].declaration_symbol, "GraphqlDeskUnavailable"
        )
        self.assertEqual(
            records["invalid_again"].canonical_owner, records["invalid"].owner
        )
        self.assertEqual(
            records["invalid_again"].declaration_path,
            records["invalid"].declaration_path,
        )
        paths = {file.relative_path for file in result.files}
        for record in result.provenance:
            self.assertEqual(record.owner.module.version, 7)
            self.assertEqual(record.owner.module.uri, "file:///main.py")
            call_text = source[
                record.owner.call_range.start : record.owner.call_range.end
            ]
            self.assertTrue(call_text.startswith("graphql("))
            self.assertIn(record.declaration_path, paths)
            self.assertEqual(record.schema_identity, ("p", "main"))
            self.assertEqual(record.context_token, "generation-1")
            if record.status == "unavailable":
                self.assertEqual(record.schema_dependencies, "unknown")
                self.assertEqual(record.config_dependencies, "unknown")
            else:
                schema_dependencies = cast(
                    tuple[SchemaCoordinateDependency, ...], record.schema_dependencies
                )
                self.assertTrue(schema_dependencies)
                self.assertTrue(
                    all(
                        re.fullmatch(r"[a-f0-9]{64}", item.fingerprint)
                        for item in schema_dependencies
                    )
                )
                config_dependencies = cast(
                    tuple[ConfigDependency, ...], record.config_dependencies
                )
                self.assertIsInstance(config_dependencies, tuple)
            self.assertIsInstance(record.mapping_dependencies, tuple)
            self.assertTrue(record.source_dependencies_known)
            for export in record.source_exports:
                self.assertEqual(export.relative_path, "generated/sources/main.pyi")
                text = next(
                    file.text
                    for file in result.files
                    if file.relative_path == export.relative_path
                )
                self.assertIn(f" import {export.symbol} as {export.alias}\n", text)
        serialized = json.dumps([asdict(record) for record in result.provenance])
        self.assertNotIn("query A", serialized)
        self.assertNotIn("query Invalid", serialized)

    def test_provenance_captures_direct_fragments_and_full_owner_dependency_closure(
        self,
    ) -> None:
        modules = (
            ModuleSnapshot(
                "p",
                "leaf",
                "file:///leaf.py",
                3,
                "from generated.main import graphql\n"
                "leaf=graphql('fragment Leaf on Book {title}')",
            ),
            ModuleSnapshot(
                "p",
                "outer",
                "file:///outer.py",
                4,
                "from generated.main import graphql\nfrom leaf import leaf\n"
                "outer=graphql('fragment Outer on Book {...Leaf}',(leaf,))",
            ),
            ModuleSnapshot(
                "p",
                "main",
                "file:///main/__init__.py",
                5,
                "from generated.main import graphql\nfrom outer import outer\n"
                "query=graphql('query Read {book(id:\"1\"){...Outer}}',(outer,))",
                is_package=True,
            ),
        )
        result = self.generate(modules)
        self.assertEqual(result.diagnostics, ())
        records = {record.binding: record for record in result.provenance}
        query, outer, leaf = (records[name] for name in ("query", "outer", "leaf"))
        self.assertEqual(query.direct_fragments, (outer.owner,))
        self.assertEqual(
            query.definition_owners, (query.owner, outer.owner, leaf.owner)
        )
        self.assertEqual(outer.direct_fragments, (leaf.owner,))
        self.assertEqual(leaf.direct_fragments, ())
        self.assertEqual(leaf.operation_kind, "fragment")
        self.assertEqual(leaf.operation_name, "Leaf")
        self.assertEqual(
            {item.module for item in query.dependencies}, {"leaf", "outer", "main"}
        )
        self.assertTrue(query.source_exports)
        self.assertTrue(
            all(
                export.relative_path == "generated/sources/main/__init__.pyi"
                for export in query.source_exports
            )
        )

    def test_project_provenance_retains_failed_index_owners_without_declarations(
        self,
    ) -> None:
        config, contexts, modules, _ = self.multi_schema()
        main = replace(
            modules[0],
            source=modules[0].source + "\nsyntax=alpha('query Broken {item {')\n"
            "selector=beta('query Existing {item{id}}',operation='Missing')\n"
            "dynamic=alpha(f'query Dynamic {{ {unknown} }}')\n",
        )
        modules = (main, *modules[1:])
        index = index_fragments(
            resolve_project(
                modules, bindings=(project_bindings(config, project_id="p"),)
            ),
            schemas={("p", context.schema_name): context for context in contexts},
        )
        self.assertEqual(len(index.unavailable), 2)
        self.assertEqual(len(index.rejected_calls), 1)
        result = generate_project_factory_artifacts(
            config, contexts, index, modules=modules, project_id="p"
        )
        self.assertTrue(result.files, result.diagnostics)
        self.assertEqual(len(result.provenance), len(index.documents) + 3)
        records = {
            record.binding: record
            for record in result.provenance
            if record.owner.module.module == "main"
        }
        self.assertEqual(records["alpha_query"].status, "precise")
        self.assertEqual(records["syntax"].schema_identity, ("p", "alpha"))
        self.assertEqual(records["selector"].schema_identity, ("p", "beta"))
        self.assertEqual(records["selector"].selector, "Missing")
        self.assertTrue(records["selector"].selector_known)
        self.assertEqual(records["dynamic"].status, "rejected")
        self.assertIsNone(records["dynamic"].schema_identity)
        self.assertFalse(records["dynamic"].selector_known)
        for name in ("syntax", "selector", "dynamic"):
            record = records[name]
            self.assertIsNone(record.declaration_path)
            self.assertIsNone(record.declaration_symbol)
            self.assertIsNone(record.canonical_owner)
            self.assertIsNone(record.definition_owners)
            self.assertEqual(record.source_exports, ())
            self.assertFalse(record.source_dependencies_known)

    def test_provenance_includes_captured_scalar_mapping_dependencies(self) -> None:
        config, contexts, modules, index = self.multi_schema()
        modules = (
            *modules,
            ModuleSnapshot(
                "p",
                "local_types",
                "file:///local_types.py",
                8,
                "class Token(str): pass",
            ),
        )
        config = replace(
            config,
            schemas=(
                replace(
                    config.schemas[0],
                    scalars=(
                        ScalarMapping(
                            "Token", "local_types.Token", "local_types.Token"
                        ),
                    ),
                ),
                config.schemas[1],
            ),
        )
        result = generate_project_factory_artifacts(
            config, contexts, index, modules=modules, project_id="p"
        )
        self.assertEqual(result.diagnostics, ())
        self.assertTrue(result.provenance)
        for record in result.provenance:
            if record.schema_identity == ("p", "alpha"):
                self.assertIn(
                    ("local_types", 8),
                    {
                        (dependency.module, dependency.version)
                        for dependency in record.dependencies
                    },
                )

    def test_provenance_records_precise_schema_and_used_config_facts(self) -> None:
        sdl = (
            "scalar Token enum Color { RED BLUE } scalar Other "
            "type Query { token: Token first: String second: ID "
            "search(filter: Filter!): Color } input Filter { token: Token }"
        )
        schema = build_schema(sdl)
        context = SchemaContext("main", schema, "schema-1")
        source = """from generated.main import graphql
token=graphql('{token}')
first=graphql('{first}')
second=graphql('{second}')
name=graphql('{__typename}')
fragment=graphql('fragment F on Query {first}')
with_fragment=graphql('query WithFragment {...F}',(fragment,))
search=graphql('query Search($filter:Filter!){search(filter:$filter)}')
"""
        modules = (ModuleSnapshot("p", "main", "file:///main.py", 1, source),)
        scalars = (
            ScalarMapping("Token", "builtins.str", "builtins.bytes"),
            ScalarMapping("Other", "builtins.int", "builtins.int"),
        )
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
        result = generate_factory_artifacts(
            context,
            index,
            modules=modules,
            project_id="p",
            generated_package="generated",
            scalars=scalars,
            enums=(("Color", "builtins.str"),),
        )
        self.assertEqual(result.diagnostics, ())
        records = {record.binding: record for record in result.provenance}
        token_config_dependencies = cast(
            tuple[ConfigDependency, ...], records["token"].config_dependencies
        )
        token_config = {
            (item.kind, item.name, item.value) for item in token_config_dependencies
        }
        self.assertIn(("scalar-output", "Token", "builtins.bytes"), token_config)
        self.assertNotIn(("scalar-input", "Token", "builtins.str"), token_config)
        self.assertNotIn(("scalar-input", "Other", "builtins.int"), token_config)
        self.assertEqual(records["name"].config_dependencies, ())
        self.assertIn(
            ("disable-masking", "disable_masking", False),
            {
                (item.kind, item.name, item.value)
                for item in cast(
                    tuple[ConfigDependency, ...],
                    records["with_fragment"].config_dependencies,
                )
            },
        )
        search_coordinates = {
            item.coordinate
            for item in cast(
                tuple[SchemaCoordinateDependency, ...],
                records["search"].schema_dependencies,
            )
        }
        self.assertIn("field:Query.search", search_coordinates)
        self.assertIn("input:Filter.token", search_coordinates)
        self.assertIn("scalar:Token", search_coordinates)
        search_config = {
            (item.kind, item.name, item.value)
            for item in cast(
                tuple[ConfigDependency, ...], records["search"].config_dependencies
            )
        }
        self.assertIn(("scalar-input", "Token", "builtins.str"), search_config)
        self.assertNotIn(("scalar-output", "Token", "builtins.bytes"), search_config)

        changed_context = SchemaContext(
            "main", build_schema(sdl.replace("first: String", "first: Int")), "schema-2"
        )
        changed_index = index_fragments(
            resolved, schemas={("p", "main"): changed_context}
        )
        changed = generate_factory_artifacts(
            changed_context,
            changed_index,
            modules=modules,
            project_id="p",
            generated_package="generated",
            scalars=scalars,
            enums=(("Color", "builtins.str"),),
        )
        old = {record.binding: record for record in result.provenance}
        new = {record.binding: record for record in changed.provenance}
        old_facts = {
            item.coordinate: item.fingerprint
            for item in cast(
                tuple[SchemaCoordinateDependency, ...],
                old["first"].schema_dependencies,
            )
        }
        new_facts = {
            item.coordinate: item.fingerprint
            for item in cast(
                tuple[SchemaCoordinateDependency, ...],
                new["first"].schema_dependencies,
            )
        }
        self.assertNotEqual(
            old_facts["field:Query.first"], new_facts["field:Query.first"]
        )
        old_sibling = {
            item.coordinate: item.fingerprint
            for item in cast(
                tuple[SchemaCoordinateDependency, ...],
                old["second"].schema_dependencies,
            )
        }
        new_sibling = {
            item.coordinate: item.fingerprint
            for item in cast(
                tuple[SchemaCoordinateDependency, ...],
                new["second"].schema_dependencies,
            )
        }
        self.assertEqual(old_sibling, new_sibling)

    def test_failed_generation_and_assembly_never_return_provenance(self) -> None:
        source = (
            "from generated.main import graphql\n"
            "first=graphql('fragment F on Book {title}')\n"
            "second=graphql('fragment F on Book {title}')\n"
        )
        result = self.generate(
            (ModuleSnapshot("p", "main", "file:///main.py", 1, source),)
        )
        self.assertEqual(result.files, ())
        self.assertEqual(result.provenance, ())
        modules = tuple(
            ModuleSnapshot(
                "p",
                name,
                f"file:///{name}.py",
                1,
                "from generated.main import graphql\nq=graphql('{greeting}')",
            )
            for name in ("Case", "case")
        )
        result = self.generate(modules)
        self.assertEqual(result.files, ())
        self.assertTrue(result.diagnostics)
        self.assertEqual(result.provenance, ())

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
            relative = module.module.replace(".", "/")
            path = root / (relative + ("/__init__.py" if module.is_package else ".py"))
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
                "from collections.abc import Sequence\n"
                "from typing_extensions import assert_type\n"
                "from graphql_desk import "
                "TypedDocument, read_fragment, unsafe_read_result\n"
                "from graphql_desk.testing import "
                "unsafe_read_result as testing_unsafe_read_result\n"
                "from generated.main import graphql\n"
                "from generated.sources.main import "
                "BookQueryResult, BookQueryVariables, BookQueryFixtureInput, "
                "unsafe_read_book_query_result\n"
                "from generated.sources.leaf import "
                "read_book_fields, BookFieldsResult, unsafe_read_book_fields_result\n"
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
                "    assert_type(read_book_fields({'title':'x'}), BookFieldsResult)\n"
                "    assert_type(unsafe_read_book_fields_result({'title':'x'}), "
                "BookFieldsResult)\n"
                "    input_fixture: BookQueryFixtureInput="
                "{'book':{'id':'1','title':99}}\n"
                "    assert_type(unsafe_read_result(book_query,input_fixture), "
                "BookQueryResult)\n"
                "    assert_type(unsafe_read_book_query_result("
                "{'book':{'id':'1','title':99}}), BookQueryResult)\n"
                "    assert_type(read_fragment(book_fields, "
                "execute(book_query, variables)['book']), BookFieldsResult)\n"
                "    assert_type(book_query.ast.definitions[0].name.value, "
                "__import__('typing').Literal['Book'])\n"
                "assert type(book_query) is TypedDocument\n"
                "assert book_query.operation_name == 'Book'\n"
                "assert other.operation_name == 'B'\n"
                "assert book_query.schema_name == 'main'\n"
                "assert len(book_query.ast.definitions) == 2\n"
                "data: BookFieldsResult = {'title':'x'}\n"
                "assert read_book_fields(data) is data\n"
                "assert read_fragment(book_fields, data) is data\n"
                "nullable_data: list[BookFieldsResult | None] = [data, None]\n"
                "assert_type(read_book_fields(nullable_data), "
                "Sequence[BookFieldsResult | None])\n"
                "assert read_book_fields(nullable_data) is nullable_data\n"
                "fixture: BookQueryFixtureInput={'book':{'id':'1','title':99}}\n"
                "if not TYPE_CHECKING:\n"
                " assert unsafe_read_result(book_query,fixture) is fixture\n"
                " assert unsafe_read_book_query_result(fixture) is fixture\n"
                "assert testing_unsafe_read_result is unsafe_read_result\n"
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
                "from generated.sources.main import unsafe_read_book_query_result\n"
                "from generated.sources.leaf import unsafe_read_book_fields_result\n"
                "unsafe_read_book_query_result({'book':{'id':1}}) # error\n"
                "unsafe_read_book_query_result({'book':{'title':'x'}}) # error\n"
                "unsafe_read_book_fields_result({'title':99}) # error\n"
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

    def test_conditional_operations_keep_public_factory_and_alias_types(self) -> None:
        context = SchemaContext(
            "main",
            build_schema("type Query { values: [[String!]!]! loose: String }"),
            "conditional-operations",
        )
        source = """from typing import TYPE_CHECKING, TypeVar
from typing_extensions import assert_type
from graphql_desk import TypedDocument
from generated.main import graphql
from generated.sources.main import ReadResult, ReadVariables, FieldsResult
fragment=graphql("fragment Details on Query @_unmask "
                 "{ pair:values @required @optional label:loose @_required }")
read=graphql("query Read($show:Boolean!) { fixed:values @optional "
             "...Details @include(if:$show) "
             "... @defer(if:false) { later:loose @_optional } }",(fragment,))
fields=graphql("query Fields { included:values @include(if:true) "
               "skipped:values @skip(if:false) deferred:values @defer(if:false) }")
R=TypeVar('R')
V=TypeVar('V')
def execute(document:TypedDocument[R,V],variables:V)->R: raise RuntimeError
def inspect(result:ReadResult, value:FieldsResult)->None:
 assert_type(result['fixed'],list[list[str]] | None)
 if 'pair' in result:
  assert_type(result['pair'],list[list[str]])
  assert_type(result['label'],str)
 if 'later' in result:
  assert_type(result['later'],str | None)
 if 'included' in value:
  assert_type(value['included'],list[list[str]])
if TYPE_CHECKING:
 assert_type(execute(read,ReadVariables(show=True)),ReadResult)
 absent:ReadResult={'fixed':None}
 present:ReadResult={'fixed':[['x']],'pair':[],'label':'x','later':None}
 literal_absent:FieldsResult={}
 literal_present:FieldsResult={'included':[],'skipped':[],'deferred':[]}
 partial:ReadResult={'fixed':None,'pair':[]} # error
 required_null:ReadResult={'fixed':None,'pair':[],'label':None} # error
 precedence:ReadResult={'fixed':None,'pair':None,'label':'x'} # error
 inner_null:ReadResult={'fixed':[None]} # error
 item_null:ReadResult={'fixed':[[None]]} # error
 missing:ReadResult={} # error
 extra:ReadResult={'fixed':None,'unknown':'x'} # error
 optional_null:FieldsResult={'deferred':None} # error
 wrong_variable:ReadVariables={'show':1} # error
"""
        for disable_masking in (False, True):
            with self.subTest(disable_masking=disable_masking):
                config = parse_project_config(
                    '[tool.graphql-desk]\ngenerated_package="generated"\n'
                    'client_directives=["defer"]\n'
                    '[[tool.graphql-desk.schemas]]\nname="main"\n'
                    'schema="schema.graphql"\n'
                    'schema_output="generated/main.pyi"\n'
                    f"disable_masking={str(disable_masking).lower()}\n",
                    config_path=Path(tempfile.gettempdir()) / "pyproject.toml",
                )
                assert config is not None
                authored = (
                    source.replace(" @_unmask", "") if disable_masking else source
                )
                modules = (ModuleSnapshot("p", "main", "file:///main.py", 1, authored),)
                resolved = resolve_project(
                    modules, bindings=(project_bindings(config, project_id="p"),)
                )
                index = index_fragments(resolved, schemas={("p", "main"): context})
                artifacts = generate_project_factory_artifacts(
                    config, (context,), index, modules=modules, project_id="p"
                )
                self.assertEqual(artifacts.diagnostics, ())
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    self.write(root, artifacts, modules)
                    self.check(
                        root,
                        {
                            "main.py": {
                                n
                                for n, line in enumerate(authored.splitlines(), 1)
                                if "# error" in line
                            }
                        },
                    )
                    runtime = subprocess.run(
                        [
                            sys.executable,
                            "-c",
                            "from main import read,fields\n"
                            "from graphql import print_ast\n"
                            "from generated.sources.main import FieldsResult\n"
                            "assert FieldsResult.__required_keys__ == set()\n"
                            "assert FieldsResult.__optional_keys__ == "
                            "{'included','skipped','deferred'}\n"
                            "text=print_ast(read.ast)\n"
                            "assert '@include(if: $show)' in text\n"
                            "assert '@defer(if: false)' in text\n"
                            "assert '@required' in text and '@optional' in text\n"
                            "assert '@_required' in text and '@_optional' in text\n"
                            "assert '@_unmask' not in text\n"
                            "assert '@skip(if: false)' in print_ast(fields.ast)\n",
                        ],
                        cwd=root,
                        capture_output=True,
                        text=True,
                    )
                    self.assertEqual(
                        runtime.returncode, 0, runtime.stdout + runtime.stderr
                    )
                rejected = generate_project_factory_artifacts(
                    replace(config, client_directives=()),
                    (context,),
                    index,
                    modules=modules,
                    project_id="p",
                )
                self.assertEqual(
                    [
                        d.message
                        for d in rejected.diagnostics
                        if d.code == "graphql-validation"
                    ],
                    ["Unknown directive '@defer'."] * 2,
                )
                self.assertEqual(
                    sum(d.code == "factory-unavailable" for d in rejected.diagnostics),
                    2,
                )

    def test_generated_factory_variable_defaults_preserve_requiredness(self) -> None:
        context = SchemaContext(
            "main",
            build_schema(
                "type Book { id: ID! } "
                "type Query { book(id: ID!, take: Int, filter: String, "
                "label: String): Book! }"
            ),
            "variable-defaults",
        )
        source = """from typing import TYPE_CHECKING
from typing import TypeVar
from typing_extensions import assert_type
from graphql_desk import TypedDocument
from generated.main import graphql
from generated.sources.main import QueryResult, QueryVariables
query=graphql(
 "query Defaults($id:ID!,$take:Int! = 3,$filter:String,$label:String = \\\"x\\\") { "
 "book(id:$id,take:$take,filter:$filter,label:$label) { id } }")
R=TypeVar('R')
V=TypeVar('V')
def execute(document:TypedDocument[R,V],variables:V)->R: raise RuntimeError
if TYPE_CHECKING:
 required: QueryVariables = {"id":"1"}
 supplied: QueryVariables = {"id":"1","take":4,"filter":None,"label":None}
 assert_type(execute(query,required),QueryResult)
 assert_type(execute(query,supplied),QueryResult)
 missing: QueryVariables = {"take":4} # error
 nullable_for_non_null: QueryVariables = {"id":"1","take":None} # error
 wrong_nullable: QueryVariables = {"id":"1","filter":1} # error
 wrong_defaulted: QueryVariables = {"id":"1","label":1} # error
"""
        modules = (ModuleSnapshot("p", "main", "file:///main.py", 1, source),)
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
        artifacts = generate_factory_artifacts(
            context,
            index,
            modules=modules,
            project_id="p",
            generated_package="generated",
        )
        self.assertEqual(artifacts.diagnostics, ())
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write(root, artifacts, modules)
            errors = {
                n for n, line in enumerate(source.splitlines(), 1) if "# error" in line
            }
            self.check(root, {"main.py": errors})
            runtime = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "from generated.sources.main import QueryVariables as V\n"
                    "assert V.__required_keys__ == {'id'}\n"
                    "assert V.__optional_keys__ == {'take','filter','label'}\n",
                ],
                cwd=root,
                capture_output=True,
                text=True,
            )
            self.assertEqual(runtime.returncode, 0, runtime.stdout + runtime.stderr)

    def test_abstract_typename_ownership_is_local_to_each_concrete_type(self) -> None:
        sdl = (
            "interface Node { id: ID! child: Node! } "
            "type User implements Node { id: ID! child: Node! name: String! } "
            "type Org implements Node { id: ID! child: Node! title: String! } "
            "union Search = User | Org "
            "type Query { nodes: [Node] search: [Search!]! }"
        )
        context = SchemaContext(
            "main",
            build_schema(sdl),
            "abstract-typename",
        )
        source = """from typing import TYPE_CHECKING, Literal, TypeVar
from typing_extensions import assert_type
from graphql_desk import TypedDocument
from generated.main import graphql
from generated.sources.main import InterfaceQueryResult as Interface
from generated.sources.main import UnionQueryResult as Union
from generated.sources.main import MaskedQueryResult as Masked
from generated.sources.main import ConditionalQueryResult as Conditional
from generated.sources.main import NestedQueryResult as Nested
from generated.sources.main import CanonicalQueryResult as Canonical
from generated.sources.main import InterfaceQueryVariables, UnionQueryVariables
interface_query=graphql("query I {nodes {kind:__typename "
                        "... on User {__typename name} ... on Org {title}}}")
union_query=graphql("query U {search {kind:__typename "
                    "... on User {__typename:id} ... on Org {title}}}")
deep=graphql("fragment Deep on Org {__typename:id}")
hidden=graphql("fragment Hidden on Org {...Deep}",(deep,))
masked_query=graphql("query M {search {kind:__typename "
                     "... on User {__typename} ...Hidden}}",(hidden,))
conditional_query=graphql("query C($show:Boolean!) {search {kind:__typename "
    "... on User {__typename} ...Hidden @include(if:$show)}}",(hidden,))
nested_hidden=graphql("fragment Nested on Node "
                     "{child {... on Org {__typename:id}}}")
nested_query=graphql("query N {nodes {child {kind:__typename "
    "... on User {__typename}} ...Nested}}",(nested_hidden,))
canonical_hidden=graphql("fragment Canonical on Org {__typename}")
canonical_query=graphql("query K {search {kind:__typename "
    "... on User {__typename} ...Canonical}}",(canonical_hidden,))
R=TypeVar('R')
V=TypeVar('V')
def execute(document:TypedDocument[R,V],variables:V)->R: raise RuntimeError
def inspect(result:Interface)->None:
 nodes=result['nodes']
 if nodes is not None:
  for node in nodes:
   if node is not None and '__typename' in node:
    if node['__typename']=='Org':
     assert_type(node['title'],str)
     assert_type(node['kind'],Literal['Org'])
    else:
     assert_type(node['name'],str)
def aliases(result:Union)->None:
 for node in result['search']:
  if node['kind']=='User':
   assert_type(node['__typename'],str)
  elif '__typename' in node:
   assert_type(node['__typename'],Literal['Org'])
def masked_aliases(result:Masked|Conditional)->None:
 for node in result['search']:
  if node['kind']=='Org':
   _=node['__typename'] # error
def nested_aliases(result:Nested)->None:
 if result['nodes'] is not None:
  for node in result['nodes']:
   if node is not None:
    child=node['child']
    if child['kind']=='Org':
     _=child['__typename'] # error
def canonical_aid(result:Canonical)->None:
 for node in result['search']:
  if node['kind']=='Org' and '__typename' in node:
   assert_type(node['__typename'],Literal['Org'])
if TYPE_CHECKING:
 assert_type(execute(interface_query,InterfaceQueryVariables()),Interface)
 assert_type(execute(union_query,UnionQueryVariables()),Union)
 absent:Interface={'nodes':[None,{'kind':'Org','title':'t'}]}
 present:Interface={'nodes':[{'kind':'Org','__typename':'Org','title':'t'}]}
 nullable:Interface={'nodes':None}
 alias:Union={'search':[{'kind':'User','__typename':'id'}]}
 aid:Union={'search':[{'kind':'Org','__typename':'Org','title':'t'}]}
 bad_aid:Interface={'nodes':[{'kind':'Org','__typename':'User','title':'t'}]} # error
 missing_selected:Interface={'nodes':[{'kind':'User','name':'n'}]} # error
 null_list:Union={'search':None} # error
 null_item:Union={'search':[None]} # error
 impossible:Union={'search':[{'kind':'Other','title':'t'}]} # error
 wrong_alias:Union={'search':[{'kind':'User','__typename':1}]} # error
"""
        modules = (ModuleSnapshot("p", "main", "file:///main.py", 1, source),)
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
        )
        self.assertEqual(artifacts.diagnostics, ())
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write(root, artifacts, modules)
            self.check(
                root,
                {
                    "main.py": {
                        n
                        for n, line in enumerate(source.splitlines(), 1)
                        if "# error" in line
                    }
                },
            )
            runtime = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "from graphql import build_schema,execute_sync\n"
                    "from main import interface_query,union_query,"
                    "masked_query,conditional_query,nested_query,canonical_query\n"
                    "from generated.sources.main import "
                    "MaskedQueryResult,ConditionalQueryResult,NestedQueryResult\n"
                    "from typing import Literal,get_args,get_type_hints\n"
                    f"schema=build_schema({sdl!r})\n"
                    "user={'__typename':'User','id':'u','name':'n'}\n"
                    "org={'__typename':'Org','id':'User','title':'t'}\n"
                    "user['child']=org\norg['child']=org\n"
                    "value={'nodes':[None,user,org],'search':[user,org]}\n"
                    "a=execute_sync(schema,interface_query.ast,root_value=value)\n"
                    "b=execute_sync(schema,union_query.ast,root_value=value)\n"
                    "assert a.errors is None and b.errors is None\n"
                    "assert a.data=={'nodes':[None,{'kind':'User',"
                    "'__typename':'User','name':'n'},{'kind':'Org','title':'t'}]}\n"
                    "assert b.data=={'search':[{'kind':'User','__typename':'u'},"
                    "{'kind':'Org','title':'t'}]}\n"
                    "for result in (MaskedQueryResult,ConditionalQueryResult):\n"
                    " items=get_args(get_type_hints(result)['search'])[0]\n"
                    " for branch in get_args(items):\n"
                    "  hints=get_type_hints(branch)\n"
                    "  if hints['kind']==Literal['Org']:\n"
                    "   assert '__typename' not in hints\n"
                    "for doc in (masked_query,conditional_query):\n"
                    " for show in (False,True):\n"
                    "  c=execute_sync(schema,doc.ast,root_value=value,"
                    "variable_values={'show':show})\n"
                    "  assert c.errors is None\n"
                    "  expected={'kind':'Org'}\n"
                    "  if doc is masked_query or show:\n"
                    "   expected['__typename']='User'\n"
                    "  assert c.data=={'search':[{'kind':'User',"
                    "'__typename':'User'},expected]}\n"
                    "nodes=get_args(get_type_hints(NestedQueryResult)['nodes'])[0]\n"
                    "for parent in get_args(get_args(nodes)[0]):\n"
                    " if parent is type(None): continue\n"
                    " for child in get_args(get_type_hints(parent)['child']):\n"
                    "  hints=get_type_hints(child)\n"
                    "  if hints['kind']==Literal['Org']:\n"
                    "   assert '__typename' not in hints\n"
                    "org['id']='not-Org'\n"
                    "d=execute_sync(schema,nested_query.ast,root_value=value)\n"
                    "assert d.errors is None\n"
                    "assert d.data=={'nodes':[None,"
                    "{'child':{'kind':'Org','__typename':'not-Org'}},"
                    "{'child':{'kind':'Org','__typename':'not-Org'}}]}\n"
                    "e=execute_sync(schema,canonical_query.ast,root_value=value)\n"
                    "assert e.errors is None\n"
                    "assert e.data=={'search':[{'kind':'User','__typename':'User'},"
                    "{'kind':'Org','__typename':'Org'}]}\n",
                ],
                cwd=root,
                capture_output=True,
                text=True,
            )
            self.assertEqual(runtime.returncode, 0, runtime.stdout + runtime.stderr)

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

    def test_dynamic_call_is_diagnosed_without_losing_valid_sibling_types(self) -> None:
        modules = (
            ModuleSnapshot(
                "p",
                "main",
                "file:///main.py",
                1,
                "from generated.main import graphql\n"
                "def query_source() -> str: return '{ greeting }'\n"
                "good=graphql('{ greeting }')\n"
                "bad=graphql(query_source())\n",
            ),
        )
        artifacts = self.generate(modules)
        self.assertTrue(
            any(item.code == "source-not-static" for item in artifacts.diagnostics),
            artifacts.diagnostics,
        )
        files = {item.relative_path: item.text for item in artifacts.files}
        self.assertIn("GoodResult", files["generated/sources/main.pyi"])
        self.assertIn("Literal['{ greeting }']", files["generated/main.pyi"])
        self.assertIn("__graphql_desk_UnresolvedDocument", files["generated/main.pyi"])

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write(root, artifacts, modules)
            source = """from graphql import DocumentNode
from graphql_desk import TypedDocument
from generated.main import graphql
from generated.sources.main import GoodResult, GoodVariables

def query_source() -> str: return '{ greeting }'
good = graphql('{ greeting }')
bad = graphql(query_source())
good_typed: TypedDocument[GoodResult, GoodVariables] = good
bad_ast: DocumentNode = bad.ast
bad_typed: TypedDocument[GoodResult, GoodVariables] = bad # error
"""
            (root / "consumer.py").write_text(source, encoding="utf8")
            self.check(
                root,
                {
                    "consumer.py": {
                        next(
                            number
                            for number, line in enumerate(source.splitlines(), 1)
                            if "# error" in line
                        )
                    }
                },
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
