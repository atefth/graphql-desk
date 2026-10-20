import unittest
from dataclasses import FrozenInstanceError, replace
from unittest.mock import patch

import graphql_desk.source_graph as source_graph
from graphql_desk.source import extract_calls
from graphql_desk.source_graph import (
    DocumentIdentity,
    FactoryBinding,
    IncrementalResolver,
    ModuleSnapshot,
    ProjectBindings,
    ProjectSources,
    resolve_project,
    resolve_type_symbols,
)


def module(
    name: str, source: str, *, project: str = "app", package: bool = False
) -> ModuleSnapshot:
    return ModuleSnapshot(
        project, name, f"file:///{project}/{name}.py", 1, source, package
    )


class SourceGraphTests(unittest.TestCase):
    def test_captured_type_symbols_follow_classes_aliases_and_reexports(self) -> None:
        snapshots = (
            module("pkg", "from .exports import Public", package=True),
            module("pkg.exports", "from .types import Alias as Public"),
            module(
                "pkg.types",
                "from typing_extensions import TypedDict\n"
                "from enum import Enum\nclass Plain: pass\nAlias=Plain\n"
                "class Child(Plain): pass\n"
                "class Payload(TypedDict,total=False):\n value:str\n"
                "class Choice(str,Enum):\n RED='red'\n"
                "from datetime import datetime as External",
            ),
        )
        names = (
            "pkg.Public",
            "pkg.types.Child",
            "pkg.types.Payload",
            "pkg.types.Choice",
            "pkg.types.External",
            "absent.Type",
        )
        original_import = __import__

        def guard(name: str, *args: object, **kwargs: object) -> object:
            if name != "collections":  # ast.walk imports deque lazily on Python 3.10.
                raise AssertionError(name)
            return original_import(name)

        with (
            patch("builtins.open", side_effect=AssertionError("No files")),
            patch("builtins.__import__", side_effect=guard),
        ):
            results = resolve_type_symbols(names, modules=snapshots, project_id="app")
        facts = {result.symbol: result for result in results}
        self.assertEqual(facts["pkg.Public"].target, "pkg.types.Plain")
        for name in names[:4]:
            self.assertEqual(facts[name].status, "resolved")
        self.assertEqual(facts["pkg.types.External"].status, "external")
        self.assertEqual(facts["pkg.types.External"].target, "datetime.datetime")
        self.assertEqual(facts["absent.Type"].status, "external")
        self.assertEqual(
            results,
            resolve_type_symbols(
                tuple(reversed(names)) + names[:1],
                modules=tuple(reversed(snapshots)),
                project_id="app",
            ),
        )
        changed = resolve_type_symbols(
            names,
            modules=(*snapshots[:2], replace(snapshots[2], version=2)),
            project_id="app",
        )
        self.assertNotEqual(results[0].dependencies, changed[0].dependencies)
        with self.assertRaises(FrozenInstanceError):
            setattr(results[0], "status", "missing")
        # Document resolution and its cached syntax retain the preexisting behavior.
        self.assertEqual(
            resolve_project(snapshots),
            source_graph.IncrementalResolver().resolve(snapshots),
        )

    def test_explicit_type_aliases_reuse_captured_class_and_reexport_resolution(
        self,
    ) -> None:
        snapshots = (
            module("pkg", "from .aliases import Public", package=True),
            module(
                "pkg.aliases",
                "import typing as t\n"
                "import pkg.types as models\n"
                "Public:t.TypeAlias=models.Alias",
            ),
            module(
                "pkg.types",
                "from typing_extensions import TypeAlias as TA\n"
                "from enum import Enum\n"
                "from typing_extensions import TypedDict\n"
                "class Plain: pass\n"
                "class Payload(TypedDict):\n"
                " value:str\n"
                "class Choice(Enum):\n"
                " RED='red'\n"
                "Alias:TA=Plain\n"
                "Data:TA=Payload\n"
                "Color:TA=Choice\n"
                "Text:TA=str",
            ),
        )
        symbols = (
            "pkg.Public",
            "pkg.types.Alias",
            "pkg.types.Data",
            "pkg.types.Color",
            "pkg.types.Text",
        )
        with (
            patch("builtins.open", side_effect=AssertionError("No files")),
            patch(
                "importlib.import_module",
                side_effect=AssertionError("No project imports"),
            ),
        ):
            results = resolve_type_symbols(symbols, modules=snapshots, project_id="app")
        self.assertEqual([item.status for item in results], ["resolved"] * len(symbols))
        self.assertEqual(
            {item.symbol: item.target for item in results},
            {
                "pkg.Public": "pkg.types.Plain",
                "pkg.types.Alias": "pkg.types.Plain",
                "pkg.types.Data": "pkg.types.Payload",
                "pkg.types.Color": "pkg.types.Choice",
                "pkg.types.Text": "builtins.str",
            },
        )
        self.assertEqual(
            results,
            resolve_type_symbols(
                tuple(reversed(symbols)),
                modules=tuple(reversed(snapshots)),
                project_id="app",
            ),
        )
        self.assertNotEqual(
            results[0].dependencies,
            resolve_type_symbols(
                symbols,
                modules=(*snapshots[:2], replace(snapshots[2], version=2)),
                project_id="app",
            )[0].dependencies,
        )
        self.assertEqual(
            resolve_project(snapshots), IncrementalResolver().resolve(snapshots)
        )

    def test_explicit_type_aliases_reject_untrusted_markers_and_unsupported_targets(
        self,
    ) -> None:
        cases = (
            "Alias:Plain=Plain",
            "from typing import TypeAlias\nMarker=TypeAlias\nAlias:Marker=Plain",
            "import typing_extensions as t\nMarker=t.TypeAlias\nAlias:Marker=Plain",
            "TypeAlias=Plain\nAlias:TypeAlias=Plain",
            "TypeAlias='typing.TypeAlias'\nAlias:TypeAlias=Plain",
            "from typing import TypeAlias\nTypeAlias=Plain\nAlias:TypeAlias=Plain",
            "from typing import TypeAlias\nAlias:TypeAlias='Plain'",
            "from typing import TypeAlias\nAlias:TypeAlias=list[Plain]",
            "from typing import TypeAlias\nAlias:TypeAlias=Plain|None",
            "from typing import TypeAlias\nAlias:TypeAlias=make()",
            "from typing import TypeAlias\nvalue=42\nAlias:TypeAlias=value",
            "from typing import TypeAlias\nAlias:TypeAlias=Later\nclass Later: pass",
            "from typing import TypeAlias\nAlias:TypeAlias=Plain\nAlias=Plain",
            "from typing import TypeAlias\n"
            "Alias:TypeAlias=Plain\n"
            "globals()['Alias']=other",
        )
        for declaration in cases:
            with self.subTest(declaration=declaration):
                (result,) = resolve_type_symbols(
                    ("types.Alias",),
                    modules=(module("types", "class Plain: pass\n" + declaration),),
                    project_id="app",
                )
                self.assertEqual(result.status, "unresolved", result)
        for marker_module in ("typing", "typing_extensions"):
            with self.subTest(marker_module=marker_module):
                snapshots = (
                    module(marker_module, "class TypeAlias: pass"),
                    module(
                        "types",
                        f"from {marker_module} import TypeAlias\n"
                        "class Plain: pass\n"
                        "Alias:TypeAlias=Plain",
                    ),
                )
                (result,) = resolve_type_symbols(
                    ("types.Alias",), modules=snapshots, project_id="app"
                )
                self.assertEqual(result.status, "unresolved")
        cycle = (
            module("a", "from typing import TypeAlias\nfrom b import B\nA:TypeAlias=B"),
            module("b", "from typing import TypeAlias\nfrom a import A\nB:TypeAlias=A"),
        )
        (result,) = resolve_type_symbols(("a.A",), modules=cycle, project_id="app")
        self.assertEqual(result.status, "unresolved")
        # An ordinary variable annotation stays unsupported for mapping preflight.
        ordinary = (
            module(
                "values", "Text:str='text'\nfrom graphql_desk import gql\nq=gql(Text)"
            ),
        )
        (result,) = resolve_type_symbols(
            ("values.Text",), modules=ordinary, project_id="app"
        )
        self.assertEqual(result.status, "unresolved")
        self.assertEqual(resolve_project(ordinary).calls[0].source, "text")

    def test_captured_type_symbols_distinguish_known_failures_and_unknown_forms(
        self,
    ) -> None:
        cases = (
            ("class Plain: pass", "Missing", "missing"),
            ("VALUE=42", "VALUE", "not-type"),
            ("VALUE='private value'", "VALUE", "not-type"),
            ("def make(): pass", "make", "not-type"),
            ("class Plain: pass\nAlias=Plain\nAlias=other", "Alias", "unresolved"),
            ("from other import *\nclass Plain: pass", "Plain", "unresolved"),
            ("def __getattr__(name): pass", "Missing", "unresolved"),
            ("class Plain: pass\nglobals()['Plain']=other", "Plain", "unresolved"),
            ("@decorate\nclass Plain: pass", "Plain", "unresolved"),
            ("class Plain(metaclass=meta): pass", "Plain", "unresolved"),
            (
                "from typing import TypedDict\n"
                "class Plain(TypedDict,closed=True): pass",
                "Plain",
                "unresolved",
            ),
            ("class Plain(unknown.Base): pass", "Plain", "unresolved"),
            ("Base='builtins.str'\nclass Plain(Base): pass", "Plain", "unresolved"),
            ("Base='enum.Enum'\nclass Plain(Base): pass", "Plain", "unresolved"),
            ("Alias=NewType('Alias',str)", "Alias", "unresolved"),
            (
                "from typing import TypeAlias\nAlias:TypeAlias='Plain'",
                "Alias",
                "unresolved",
            ),
            ("class Plain(", "Plain", "unresolved"),
        )
        for source, name, status in cases:
            with self.subTest(source=source):
                (result,) = resolve_type_symbols(
                    ("types." + name,),
                    modules=(module("types", source),),
                    project_id="app",
                )
                self.assertEqual(result.status, status, result)
                if status == "not-type":
                    self.assertIsNone(result.target)
        cycle = (
            module("a", "from b import B as A"),
            module("b", "from a import A as B"),
        )
        (result,) = resolve_type_symbols(("a.A",), modules=cycle, project_id="app")
        self.assertEqual(result.status, "unresolved")
        (result,) = resolve_type_symbols(
            ("types.Outer.Inner",),
            modules=(module("types", "class Outer:\n class Inner: pass"),),
            project_id="app",
        )
        self.assertEqual(result.status, "unresolved")
        (result,) = resolve_type_symbols(
            ("foreign.Plain",),
            modules=(module("foreign", "class Plain: pass", project="other"),),
            project_id="app",
        )
        self.assertEqual(result.status, "external")
        (result,) = resolve_type_symbols(
            ("types.Plain",),
            modules=(
                module("types", "class Plain: pass"),
                module("other", "import types\ntypes.Plain=int"),
            ),
            project_id="app",
        )
        self.assertEqual(result.status, "unresolved")
        (result,) = resolve_type_symbols(
            ("types.Plain",),
            modules=(
                module("typing_extensions", "class TypedDict: pass"),
                module(
                    "types",
                    "from typing_extensions import TypedDict\n"
                    "class Plain(TypedDict,closed=True): pass",
                ),
            ),
            project_id="app",
        )
        self.assertEqual(result.status, "unresolved")
        for invalid in ("str", "pkg.class", "pkg.\u212a"):
            with self.assertRaises(ValueError):
                resolve_type_symbols((invalid,), modules=(), project_id="app")

    def test_namespace_dict_escapes_invalidate_type_and_document_facts(self) -> None:
        for escape in (
            "exec('Plain=42', types.__dict__)",
            "mutate(types.__dict__)",
            "namespace=types.__dict__\nmutate(namespace)",
            "namespace=types.__dict__\nexec('Plain=42',namespace)",
            "mutate({'namespace':types.__dict__})",
            "types.__dict__.clear()",
            "namespace=types.__dict__\nnamespace.clear()",
        ):
            with self.subTest(escape=escape):
                snapshots: tuple[ModuleSnapshot, ...] = (
                    module("types", "class Plain: pass"),
                    module("other", "import types\n" + escape),
                )
                (result,) = resolve_type_symbols(
                    ("types.Plain",), modules=snapshots, project_id="app"
                )
                self.assertEqual(result.status, "unresolved")
                snapshots = (
                    module("types", "from graphql_desk import gql\nTEXT='{ok}'"),
                    module("other", "import types\n" + escape),
                    module(
                        "main",
                        "from graphql_desk import gql\n"
                        "from types import TEXT\nq=gql(TEXT)",
                    ),
                )
                graph = resolve_project(snapshots)
                self.assertEqual(graph.calls, ())
                self.assertTrue(graph.diagnostics)
        for reflection in (
            "sys.modules['app.models'].Plain=42",
            "mutate(sys.modules)",
            "registry=sys.modules\nmutate(registry)",
        ):
            with self.subTest(reflection=reflection):
                snapshots = (
                    module("app", "", package=True),
                    module("app.models", "class Plain: pass"),
                    module("other", "import app.models\nimport sys\n" + reflection),
                )
                (result,) = resolve_type_symbols(
                    ("app.models.Plain",), modules=snapshots, project_id="app"
                )
                self.assertEqual(result.status, "unresolved")
        # Passing an ordinary exported value does not escape the module namespace.
        snapshots = (
            module("types", "class Plain: pass\nTEXT='{ok}'"),
            module("other", "import types\nprint(types.TEXT)"),
        )
        (result,) = resolve_type_symbols(
            ("types.Plain",), modules=snapshots, project_id="app"
        )
        self.assertEqual(result.status, "resolved")
        snapshots = (
            module("templates", "TEXT='{ok}'\ndef noop(): pass"),
            module(
                "main",
                "from graphql_desk import gql\nimport templates\n"
                "templates.noop()\nq=gql(templates.TEXT)",
            ),
        )
        self.assertEqual(len(resolve_project(snapshots).calls), 1)
        snapshots = (
            module(
                "main",
                "from graphql_desk import gql\ngql.scalar('String','x')\nq=gql('{ok}')",
            ),
        )
        self.assertEqual(len(resolve_project(snapshots).calls), 1)

    def test_rejected_call_ownership_without_invented_semantics(self) -> None:
        item = module(
            "main",
            "from graphql_desk import gql, init_graphql\n"
            "bad_source=gql(dynamic)\n"
            "bad_selector=gql('{ id }',operation=selector)\n"
            "bad_fragments=gql('{ id }',[unknown])\n"
            "bad_init=init_graphql(dynamic)\n"
            "good=gql('{ ok }')\n",
        )
        result = resolve_project((item,))
        self.assertEqual([call.binding for call in result.calls], ["good"])
        self.assertEqual(
            [call.binding for call in result.unavailable],
            ["bad_source", "bad_selector", "bad_fragments"],
        )
        for rejected in result.unavailable:
            self.assertEqual(rejected.module.uri, item.uri)
            self.assertEqual(rejected.module.version, item.version)
            self.assertEqual(len(rejected.diagnostics), 1)
            self.assertEqual(rejected.diagnostics[0].range, rejected.range)
            self.assertTrue(
                item.source[rejected.range.start : rejected.range.end].startswith(
                    "gql("
                )
            )
            self.assertFalse(hasattr(rejected, "source"))
            self.assertFalse(hasattr(rejected, "schema_name"))
            with self.assertRaises(FrozenInstanceError):
                setattr(rejected, "binding", "changed")
        self.assertEqual(ProjectSources((), ()).unavailable, ())
        resolver = IncrementalResolver()
        cold = resolver.resolve((item,))
        warm = resolver.resolve((item,))
        self.assertEqual(cold, result)
        self.assertEqual(warm, result)
        self.assertIs(cold.unavailable[0], warm.unavailable[0])
        probe = resolve_project(
            (
                module(
                    "probe",
                    "from graphql_desk import gql\ngql=other\nrejected=gql('{ id }')",
                ),
            )
        )
        self.assertEqual(probe.calls, ())
        self.assertEqual([call.binding for call in probe.unavailable], ["rejected"])

    def test_constants_reexports_relative_imports_and_contributions(self) -> None:
        constants = module("pkg.constants", 'HEAD = "query Q "\nTAIL = "{ id }"\n')
        bridge = module(
            "pkg.bridge",
            "from .constants import HEAD, TAIL\nSOURCE = HEAD + TAIL\n"
            "from graphql_desk import gql as query\n",
        )
        consumer = module(
            "pkg.consumer",
            'from .bridge import SOURCE, query\ndoc = query(SOURCE, operation="Q")\n',
        )
        result = resolve_project((consumer, bridge, constants))
        self.assertEqual(result.diagnostics, ())
        self.assertEqual(len(result.calls), 1)
        call = result.calls[0]
        self.assertEqual(call.source, "query Q { id }")
        self.assertEqual(call.binding, "doc")
        self.assertEqual(call.operation, "Q")
        self.assertEqual(
            [item.text for item in call.contributions], ["query Q ", "{ id }"]
        )
        for contribution in call.contributions:
            original = constants.source[
                contribution.range.start : contribution.range.end
            ]
            self.assertTrue(original.startswith('"'))
            self.assertTrue(original.endswith('"'))
        self.assertEqual(
            {item.module for item in call.dependencies},
            {"pkg.consumer", "pkg.bridge", "pkg.constants"},
        )
        updated = resolve_project(
            (
                consumer,
                bridge,
                replace(
                    constants,
                    version=2,
                    source='HEAD = "query New "\nTAIL = "{ name }"\n',
                ),
            )
        )
        self.assertEqual(updated.calls[0].source, "query New { name }")
        self.assertIn(2, {item.version for item in updated.calls[0].dependencies})

    def test_namespace_imports_aliases_same_line_and_packages(self) -> None:
        constants = module("pkg.constants", 'TEXT = "{ id }"')
        package = module("pkg", "from .constants import TEXT", package=True)
        for source in (
            "import graphql_desk as desk; import pkg.constants; "
            "alias = desk.gql; q = alias(pkg.constants.TEXT)",
            "from graphql_desk import graphql as g; from pkg import TEXT; q = g(TEXT)",
            "import graphql_desk; from pkg import constants as c; "
            "q = graphql_desk.graphql(c.TEXT)",
        ):
            with self.subTest(source=source):
                result = resolve_project((module("main", source), constants, package))
                self.assertEqual(result.diagnostics, ())
                self.assertEqual([call.source for call in result.calls], ["{ id }"])

    def test_distinct_projects_and_duplicate_identities(self) -> None:
        one = module("constants", 'TEXT = "{ one }"', project="one")
        two = module("constants", 'TEXT = "{ two }"', project="two")
        source = "from graphql_desk import gql\nfrom constants import TEXT\ngql(TEXT)"
        result = resolve_project(
            (
                one,
                two,
                module("main", source, project="one"),
                module("main", source, project="two"),
            )
        )
        self.assertEqual({call.source for call in result.calls}, {"{ one }", "{ two }"})
        for snapshots in ((one, one), (one, replace(one, module="alias"))):
            with self.subTest(snapshots=snapshots), self.assertRaises(ValueError):
                resolve_project(snapshots)

    def test_dynamic_sources_fragments_and_forward_references_are_diagnostics(
        self,
    ) -> None:
        for body in (
            'q=gql(TEXT)\nTEXT="{ id }"',
            "TEXT=other()\nq=gql(TEXT)",
            'TEXT="x"\nTEXT+="y"\nq=gql(TEXT)',
            'TEXT="x"\ndel TEXT\nq=gql(TEXT)',
            'q=gql(f"{dynamic}")',
            'q=gql("{}".format(value))',
            'q=gql("%s" % value)',
            'q=gql("{ id }", [fragment])',
            'q=gql("{ id }", *fragments)',
            'q=gql("{ id }", operation=selected)',
            "from missing import TEXT\nq=gql(TEXT)",
            'if flag:\n TEXT="{ id }"\nq=gql(TEXT)',
        ):
            with self.subTest(body=body):
                result = resolve_project(
                    (module("main", "from graphql_desk import gql\n" + body),)
                )
                self.assertEqual(result.calls, ())
                self.assertTrue(result.diagnostics)

    def test_import_cycles_are_diagnosed_at_known_graphql_use(self) -> None:
        first = module("a", "from b import TEXT")
        second = module("b", "from a import TEXT")
        main = module(
            "main", "from graphql_desk import gql\nfrom a import TEXT\ngql(TEXT)"
        )
        result = resolve_project((first, second, main))
        self.assertEqual(result.calls, ())
        self.assertIn("cycle", result.diagnostics[0].message)

    def test_unrelated_calls_and_lookalike_modules_are_not_candidates(self) -> None:
        main = module(
            "main",
            """import os
from datetime import datetime
os.path.join('a', 'b')
datetime.now()
def local(source):
 return source
alias = local
alias('{ id }')
""",
        )
        result = resolve_project((main,))
        self.assertEqual(result.calls, ())
        self.assertEqual(result.diagnostics, ())
        lookalike = module("graphql_desk", "def gql(source):\n return source")
        consumer = module("consumer", 'from graphql_desk import gql\ngql("{ id }")')
        result = resolve_project((lookalike, consumer))
        self.assertEqual(result.calls, ())
        self.assertEqual(result.diagnostics, ())

    def test_shadow_rebinding_and_namespace_mutation_never_succeed(self) -> None:
        for body in (
            'gql=other\ngql("{ id }")',
            'if flag:\n from other import gql\ngql("{ id }")',
            'try:\n pass\nexcept Exception as gql:\n pass\ngql("{ id }")',
            'match value:\n case {"q": gql}: pass\ngql("{ id }")',
            'marker=(gql := lambda *_: None)\ngql("{ id }")',
            'def f(x=(gql := other)): pass\ngql("{ id }")',
            'def f(x: (gql := other)): pass\ngql("{ id }")',
            'value: (gql := other) = 1\ngql("{ id }")',
            'from other import *\ngql("{ id }")',
        ):
            with self.subTest(body=body):
                result = resolve_project(
                    (module("main", "from graphql_desk import gql\n" + body),)
                )
                self.assertEqual(result.calls, ())
                self.assertTrue(result.diagnostics)
        for mutation in (
            "desk.gql=other",
            "alias=desk\nalias.gql=other",
            'setattr(desk, "gql", other)',
            "def patch():\n desk.gql=other",
        ):
            result = resolve_project(
                (
                    module(
                        "main",
                        "import graphql_desk as desk\n"
                        + mutation
                        + '\ndesk.gql("{ id }")',
                    ),
                )
            )
            self.assertEqual(result.calls, ())
            self.assertTrue(result.diagnostics)

    def test_nested_scopes_are_explicit_and_existing_extractor_is_unchanged(
        self,
    ) -> None:
        source = 'from graphql_desk import gql\ndef f():\n return gql("{ id }")'
        result = resolve_project((module("main", source),))
        self.assertEqual(result.calls, ())
        self.assertIn("Nested scopes", result.diagnostics[0].message)
        self.assertEqual(len(extract_calls(source).calls), 1)
        shadowed = resolve_project(
            (
                module(
                    "main",
                    'from graphql_desk import gql\ndef f(gql):\n return gql("{ id }")',
                ),
            )
        )
        self.assertEqual(shadowed.calls, ())
        self.assertEqual(shadowed.diagnostics, ())

    def test_unicode_crlf_and_opaque_code_are_not_executed(self) -> None:
        source = (
            'from graphql_desk import gql\r\nraise RuntimeError("never execute")\r\n'
            'text="🙂"; q=gql("{ café }")'
        )
        result = resolve_project((module("main", source),))
        self.assertEqual(len(result.calls), 1)
        call = result.calls[0]
        self.assertEqual(source[call.range.start : call.range.end], 'gql("{ café }")')
        invalid = resolve_project((module("bad", "def invalid("),))
        self.assertEqual(invalid.calls, ())
        self.assertEqual(invalid.diagnostics[0].code, "invalid-python")

    def test_namespace_reflection_and_escaped_references_cannot_publish_calls(
        self,
    ) -> None:
        for mutation in (
            "vars(desk)['gql']=other",
            "put=setattr; put(desk,'gql',other)",
            "def patch(mod):\n mod.gql=other\npatch(desk)",
            "def patch(mod):\n mod.gql=other\npatch(mod=desk)",
            "patch([desk])",
        ):
            with self.subTest(mutation=mutation):
                result = resolve_project(
                    (
                        module(
                            "main",
                            "import graphql_desk as desk\n"
                            + mutation
                            + '\ndesk.gql("{ id }")',
                        ),
                    )
                )
                self.assertEqual(result.calls, ())
                self.assertTrue(result.diagnostics)
        for mutation in (
            "globals()['gql']=other",
            "namespace=globals; namespace()['gql']=other",
            "gql.__code__=other.__code__",
            "patch(gql)",
        ):
            with self.subTest(mutation=mutation):
                result = resolve_project(
                    (
                        module(
                            "main",
                            "from graphql_desk import gql\n"
                            + mutation
                            + '\ngql("{ id }")',
                        ),
                    )
                )
                self.assertEqual(result.calls, ())
                self.assertTrue(result.diagnostics)

    def test_deep_traversal_discards_partial_module_calls(self) -> None:
        source = (
            'from graphql_desk import gql\ngql("{ ok }")\nq=gql('
            + "+".join(['"x"'] * 550)
            + ")"
        )
        result = resolve_project((module("main", source),))
        self.assertEqual(result.calls, ())
        self.assertIn("source-too-complex", {item.code for item in result.diagnostics})

    def test_invalid_supplied_modules_never_fall_back_to_installed_bindings(
        self,
    ) -> None:
        shadow = module("graphql_desk", "def broken(")
        main = module("main", 'from graphql_desk import gql\ngql("{ id }")')
        result = resolve_project((shadow, main))
        self.assertEqual(result.calls, ())
        self.assertEqual(result.diagnostics[0].code, "invalid-python")
        package = module("pkg", "def broken(", package=True)
        child = module("pkg.child", 'TEXT="{ id }"')
        main = module(
            "main",
            "from graphql_desk import gql\nfrom pkg.child import TEXT\ngql(TEXT)",
        )
        result = resolve_project((package, child, main))
        self.assertEqual(result.calls, ())
        self.assertTrue(result.diagnostics)

    def test_explicit_non_package_parent_cannot_resolve_child_modules(self) -> None:
        parent = module("pkg", "# ordinary module")
        child = module("pkg.constants", 'TEXT="{ id }"')
        for source in (
            "from graphql_desk import gql\nimport pkg.constants\n"
            "gql(pkg.constants.TEXT)",
            "from graphql_desk import gql\nfrom pkg.constants import TEXT\ngql(TEXT)",
            "from graphql_desk import gql\nfrom pkg import constants\n"
            "gql(constants.TEXT)",
        ):
            with self.subTest(source=source):
                result = resolve_project((parent, child, module("main", source)))
                self.assertEqual(result.calls, ())
                self.assertTrue(result.diagnostics)

    def test_builtin_reflection_import_identities_cannot_publish_calls(self) -> None:
        for mutation in (
            "import builtins\nbuiltins.globals()['gql']=other",
            "import builtins\ngetattr(builtins,'globals')()['gql']=other",
            "identity(globals)()['gql']=other",
            "from builtins import globals as namespace\n"
            "identity(namespace)()['gql']=other",
            "from builtins import globals as namespace\nnamespace()['gql']=other",
            "import builtins as b\nnamespace=b.globals\nnamespace()['gql']=other",
        ):
            with self.subTest(mutation=mutation):
                result = resolve_project(
                    (
                        module(
                            "main",
                            "from graphql_desk import gql\n"
                            + mutation
                            + '\ngql("{ id }")',
                        ),
                    )
                )
                self.assertEqual(result.calls, ())
                self.assertTrue(result.diagnostics)

    def test_exact_declared_factory_facts_and_project_default(self) -> None:
        facts = ProjectBindings(
            "app",
            ("main", "other"),
            "main",
            (FactoryBinding("generated.other", "graphql", "other"),),
        )
        snapshots = (
            module(
                "main",
                """from graphql_desk import gql
from generated.other import graphql as query
import generated.other as generated
one=gql("{ one }")
two=query("{ two }")
three=generated.graphql("{ three }")
from generated.lookalike import graphql as fake
fake("{ not_a_document }")
""",
            ),
        )
        result = resolve_project(snapshots, bindings=(facts,))
        self.assertEqual(result.diagnostics, ())
        self.assertEqual(
            [call.schema_name for call in result.calls], ["main", "other", "other"]
        )
        self.assertEqual(
            [call.binding for call in result.calls], ["one", "two", "three"]
        )
        baseline = resolve_project(
            (module("main", 'from graphql_desk import gql\ngql("{ id }")'),)
        )
        self.assertIsNone(baseline.calls[0].schema_name)

    def test_schema_initializers_constants_aliases_and_reexports(self) -> None:
        facts = ProjectBindings("app", ("main", "other"), "main")
        constants = module("constants", 'SCHEMA="other"')
        factory = module(
            "factory",
            "from graphql_desk import init_graphql as init\n"
            "from constants import SCHEMA\nquery=init(schema=SCHEMA)",
        )
        consumer = module("main", 'from factory import query as gql\ndoc=gql("{ id }")')
        result = resolve_project((constants, factory, consumer), bindings=(facts,))
        self.assertEqual(result.diagnostics, ())
        self.assertEqual(len(result.calls), 1)
        self.assertEqual(result.calls[0].schema_name, "other")
        self.assertEqual(
            {item.module for item in result.calls[0].dependencies},
            {"main", "factory", "constants"},
        )
        unconfigured = resolve_project(
            (
                module(
                    "main",
                    'import graphql_desk as desk\nquery=desk.init_graphql("unused")\n'
                    'query("{ id }")',
                ),
            )
        )
        self.assertEqual(unconfigured.diagnostics, ())
        self.assertEqual(unconfigured.calls[0].schema_name, "unused")
        keyword_baseline = resolve_project(
            (
                module(
                    "main",
                    "from graphql_desk import init_graphql\n"
                    'query=init_graphql("class")\nquery("{ id }")',
                ),
            )
        )
        self.assertEqual(keyword_baseline.diagnostics, ())
        self.assertEqual(keyword_baseline.calls[0].schema_name, "class")

    def test_configured_template_identity_resolves_stable_local_function(self) -> None:
        facts = ProjectBindings(
            "app", ("main",), "main", (FactoryBinding("templates", "custom"),)
        )
        templates = module(
            "templates", "def custom(source):\n raise RuntimeError('never execute')"
        )
        bridge = module("bridge", "from templates import custom as query")
        consumer = module("main", 'from bridge import query\nquery("{ id }")')
        result = resolve_project((templates, bridge, consumer), bindings=(facts,))
        self.assertEqual(result.diagnostics, ())
        self.assertEqual(result.calls[0].schema_name, "main")
        self.assertEqual(
            {item.module for item in result.calls[0].dependencies},
            {"templates", "bridge", "main"},
        )

    def test_ambiguous_defaults_unknown_schemas_and_dynamic_initializers_fail(
        self,
    ) -> None:
        facts = ProjectBindings("app", ("main", "other"))
        ambiguous = resolve_project(
            (module("main", 'from graphql_desk import gql\ngql("{ id }")'),),
            bindings=(facts,),
        )
        self.assertEqual(ambiguous.calls, ())
        self.assertIn("ambiguous", ambiguous.diagnostics[0].message)
        for initializer in (
            'init_graphql("missing")',
            "init_graphql(dynamic)",
            'init_graphql("main", "other")',
            "init_graphql(*schemas)",
            'init_graphql(schema="main", other=1)',
            'init_graphql("bad-name")',
        ):
            with self.subTest(initializer=initializer):
                result = resolve_project(
                    (
                        module(
                            "main",
                            "from graphql_desk import init_graphql\nquery="
                            + initializer
                            + '\nquery("{ id }")',
                        ),
                    ),
                    bindings=(facts,),
                )
                self.assertEqual(result.calls, ())
                self.assertTrue(result.diagnostics)
        explicit = resolve_project(
            (
                module(
                    "main",
                    "from graphql_desk import init_graphql\n"
                    'query=init_graphql("other")\nquery("{ id }")',
                ),
            ),
            bindings=(facts,),
        )
        self.assertEqual(explicit.diagnostics, ())
        self.assertEqual(explicit.calls[0].schema_name, "other")

    def test_binding_facts_are_validated_before_resolution(self) -> None:
        invalid = (
            (ProjectBindings("app", ()),),
            (ProjectBindings("app", ("main", "main")),),
            (ProjectBindings("app", ("bad-name",)),),
            (ProjectBindings("app", ("class",)),),
            (ProjectBindings("app", ("main",), "missing"),),
            (
                ProjectBindings(
                    "app", ("main",), factories=(FactoryBinding("a", "g", "missing"),)
                ),
            ),
            (
                ProjectBindings(
                    "app", ("main",), factories=(FactoryBinding("a-b", "g"),)
                ),
            ),
            (
                ProjectBindings(
                    "app", ("main",), factories=(FactoryBinding("a", "class"),)
                ),
            ),
            (
                ProjectBindings(
                    "app",
                    ("main",),
                    factories=(FactoryBinding("a", "g"), FactoryBinding("a", "g")),
                ),
            ),
            (
                ProjectBindings(
                    "app",
                    ("main",),
                    factories=(FactoryBinding("templates", "nested.gql"),),
                ),
            ),
            (ProjectBindings("app", ("main",)), ProjectBindings("app", ("main",))),
        )
        for bindings in invalid:
            with self.subTest(bindings=bindings), self.assertRaises(ValueError):
                resolve_project((), bindings=bindings)

    def test_declared_factories_cannot_bypass_invalid_missing_or_mutated_exports(
        self,
    ) -> None:
        facts = ProjectBindings(
            "app", ("main",), "main", (FactoryBinding("templates", "custom"),)
        )
        consumer = module("main", 'from templates import custom\ncustom("{ id }")')
        for source in (
            "def broken(",
            "# missing export",
            "def replace(fn): return 'not callable'\n@replace\n"
            "def custom(source): return source",
            'custom="not callable"',
            "def custom(source): return source\ncustom=other",
            "def custom(source): return source\ncustom.__code__=other.__code__",
        ):
            with self.subTest(source=source):
                result = resolve_project(
                    (module("templates", source), consumer), bindings=(facts,)
                )
                self.assertEqual(result.calls, ())
                self.assertTrue(result.diagnostics)
        mutated = resolve_project(
            (
                module(
                    "main",
                    "from generated.main import graphql as query\n"
                    'query.__code__=other.__code__\nquery("{ id }")',
                ),
            ),
            bindings=(
                ProjectBindings(
                    "app",
                    ("main",),
                    "main",
                    (FactoryBinding("generated.main", "graphql", "main"),),
                ),
            ),
        )
        self.assertEqual(mutated.calls, ())
        self.assertTrue(mutated.diagnostics)

    def test_project_factory_and_schema_facts_remain_isolated(self) -> None:
        source = 'from generated.schema import graphql\ngraphql("{ id }")'
        result = resolve_project(
            (
                module("main", source, project="one"),
                module("main", source, project="two"),
            ),
            bindings=(
                ProjectBindings(
                    "one",
                    ("a",),
                    "a",
                    (FactoryBinding("generated.schema", "graphql", "a"),),
                ),
                ProjectBindings(
                    "two",
                    ("b",),
                    "b",
                    (FactoryBinding("generated.schema", "graphql", "b"),),
                ),
            ),
        )
        self.assertEqual(result.diagnostics, ())
        self.assertEqual([call.schema_name for call in result.calls], ["a", "b"])

    def test_local_declared_factory_mutation_and_dynamic_exports_remain_guarded(
        self,
    ) -> None:
        facts = ProjectBindings(
            "app", ("main",), "main", (FactoryBinding("templates", "custom"),)
        )
        templates = module(
            "templates",
            "def custom(source): return source\ncustom.__code__=other.__code__\n"
            'custom("{ id }")',
        )
        result = resolve_project((templates,), bindings=(facts,))
        self.assertEqual(result.calls, ())
        self.assertTrue(result.diagnostics)
        package = module(
            "pkg", "def __getattr__(name): return replacement", package=True
        )
        child = module("pkg.child", 'TEXT="{ id }"')
        consumer = module(
            "main",
            "from graphql_desk import gql\nfrom pkg import child\ngql(child.TEXT)",
        )
        result = resolve_project((package, child, consumer))
        self.assertEqual(result.calls, ())
        self.assertTrue(result.diagnostics)

    def test_fragment_references_target_actual_imported_calls_in_caller_order(
        self,
    ) -> None:
        leaf = module(
            "leaf",
            'from graphql_desk import gql\nleaf=gql("fragment Leaf on User { id }")',
        )
        left = module(
            "left",
            "from graphql_desk import gql\nfrom leaf import leaf\n"
            'left=gql("fragment Left on User { ...Leaf name }", [leaf])',
        )
        bridge = module(
            "bridge", "from left import left as first\nfrom leaf import leaf as second"
        )
        consumer = module(
            "main",
            "from graphql_desk import gql\nfrom bridge import first, second\n"
            'doc=gql("query Q { user { ...Left ...Leaf } }", (first, second, first))',
        )
        result = resolve_project((consumer, bridge, left, leaf))
        self.assertEqual(result.diagnostics, ())
        self.assertEqual(len(result.calls), 3)
        by_binding = {call.binding: call for call in result.calls}
        document = by_binding["doc"]
        left_call = by_binding["left"]
        leaf_call = by_binding["leaf"]
        self.assertEqual(document.source, "query Q { user { ...Left ...Leaf } }")
        self.assertEqual(
            [reference.target for reference in document.fragments],
            [
                DocumentIdentity(left_call.module, left_call.range),
                DocumentIdentity(leaf_call.module, leaf_call.range),
                DocumentIdentity(left_call.module, left_call.range),
            ],
        )
        self.assertEqual(
            [
                consumer.source[ref.range.start : ref.range.end]
                for ref in document.fragments
            ],
            ["first", "second", "first"],
        )
        self.assertTrue(
            all(ref.reference_module == document.module for ref in document.fragments)
        )
        self.assertEqual(
            {item.module for item in document.dependencies},
            {"main", "bridge", "left", "leaf"},
        )
        self.assertEqual(
            left_call.fragments[0].target,
            DocumentIdentity(leaf_call.module, leaf_call.range),
        )
        updated = resolve_project(
            (
                consumer,
                bridge,
                left,
                replace(
                    leaf, version=2, source=leaf.source.replace("{ id }", "{ name }")
                ),
            )
        )
        updated_document = next(call for call in updated.calls if call.binding == "doc")
        self.assertEqual(updated_document.fragments[1].target.module.version, 2)
        self.assertIn(2, {item.version for item in updated_document.dependencies})

    def test_fragment_references_support_qualified_and_inline_document_values(
        self,
    ) -> None:
        fragments = module(
            "fragments",
            'from graphql_desk import gql\nfields=gql("fragment F on User { id }")',
        )
        consumer = module(
            "main",
            "from graphql_desk import gql\nimport fragments as f\n"
            'doc=gql("{ user { ...F ...G } }", [f.fields, '
            'gql("fragment G on User { name }")])',
        )
        result = resolve_project((consumer, fragments))
        self.assertEqual(result.diagnostics, ())
        self.assertEqual(len(result.calls), 3)
        identities = {
            DocumentIdentity(call.module, call.range) for call in result.calls
        }
        document = next(call for call in result.calls if call.binding == "doc")
        self.assertEqual(len(document.fragments), 2)
        self.assertTrue(all(ref.target in identities for ref in document.fragments))
        self.assertEqual(document.fragments[0].target.module.module, "fragments")
        self.assertEqual(document.fragments[1].target.module.module, "main")

    def test_fragment_reference_resolution_rejects_dynamic_values_and_cycles(
        self,
    ) -> None:
        for reference in ('"not a document"', "make()", "*items", "missing"):
            result = resolve_project(
                (
                    module(
                        "main",
                        'from graphql_desk import gql\ndoc=gql("{ id }", ['
                        + reference
                        + "])",
                    ),
                )
            )
            with self.subTest(reference=reference):
                self.assertEqual(result.calls, ())
                self.assertTrue(result.diagnostics)
        for source in (
            'from graphql_desk import gql\nf=gql("fragment F on User { ...F }", [f])',
            'from graphql_desk import gql\nitems=[]\ndoc=gql("{ id }", items)',
        ):
            result = resolve_project((module("main", source),))
            self.assertEqual(result.calls, ())
            self.assertTrue(result.diagnostics)
        a = module(
            "a",
            "from graphql_desk import gql\nfrom b import B\n"
            'A=gql("fragment A on User { ...B }", [B])',
        )
        b = module(
            "b",
            "from graphql_desk import gql\nfrom a import A\n"
            'B=gql("fragment B on User { ...A }", [A])',
        )
        result = resolve_project((a, b))
        self.assertEqual(result.calls, ())
        self.assertTrue(any("cycle" in item.message for item in result.diagnostics))

    def test_fragment_schema_identity_is_checked_and_unknown_metadata_propagates(
        self,
    ) -> None:
        source = """from graphql_desk import gql, init_graphql
a=init_graphql('a')
b=init_graphql('b')
fragment=a('fragment F on User { id }')
unknown=gql('fragment U on User { name }')
valid=a('{ user { ...F ...U } }', [fragment, unknown])
inherited=gql('{ user { ...F } }', [fragment])
invalid=b('{ user { ...F } }', [fragment])
"""
        result = resolve_project((module("main", source),))
        by_binding = {call.binding: call for call in result.calls}
        self.assertNotIn("invalid", by_binding)
        self.assertEqual(by_binding["valid"].schema_name, "a")
        self.assertEqual(by_binding["inherited"].schema_name, "a")
        self.assertIsNone(by_binding["unknown"].schema_name)
        self.assertTrue(
            any("different schema" in item.message for item in result.diagnostics)
        )

    def test_document_consumption_does_not_invalidate_static_authoring(self) -> None:
        source = """from graphql_desk import gql
fragment=gql('fragment F on User { id }')
document=gql('query Q { user { ...F } }', [fragment])
client.execute(document)
client.execute(document.ast)
"""
        result = resolve_project((module("main", source),))
        self.assertEqual(result.diagnostics, ())
        self.assertEqual(len(result.calls), 2)
        self.assertEqual(len(result.calls[1].fragments), 1)
        for mutation in (
            "fragment.ast.definitions=()",
            "tree=fragment.ast; tree.definitions=()",
            "items=[fragment]; items[0].ast.definitions=()",
            "items=[*[fragment]]; items[0].ast.definitions=()",
            "items={'f': fragment}; items['f'].ast.definitions=()",
            "setattr(fragment.ast, 'definitions', ())",
            "put=setattr; put(fragment.ast, 'definitions', ())",
        ):
            mutated = source.replace("client.execute(document)", mutation)
            with self.subTest(mutation=mutation):
                invalid = resolve_project((module("main", mutated),))
                self.assertEqual(invalid.calls, ())
                self.assertTrue(invalid.diagnostics)

    def test_python_reference_records_do_not_claim_graphql_fragment_validation(
        self,
    ) -> None:
        source = """from graphql_desk import gql
operation=gql('query NotAFragment { unknownField }')
document=gql('{ otherUnknownField }', [operation])
"""
        result = resolve_project((module("main", source),))
        self.assertEqual(result.diagnostics, ())
        self.assertEqual(len(result.calls), 2)
        self.assertEqual(len(result.calls[1].fragments), 1)

    def test_failed_module_traversal_discards_cached_dependent_references(self) -> None:
        fragment = module(
            "fragment",
            'from graphql_desk import gql\nf=gql("fragment F on User { id }")\n'
            "deep=gql(" + "+".join(['"x"'] * 550) + ")",
        )
        consumer = module(
            "main",
            "from graphql_desk import gql\nfrom fragment import f\n"
            'doc=gql("{ user { ...F } }", [f])',
        )
        independent = module(
            "independent", 'from graphql_desk import gql\nok=gql("{ ok }")'
        )
        result = resolve_project((consumer, fragment, independent))
        self.assertEqual([call.binding for call in result.calls], ["ok"])
        self.assertIn("source-too-complex", {item.code for item in result.diagnostics})
        self.assertEqual([call.binding for call in result.unavailable], ["doc"])
        rejected = result.unavailable[0]
        self.assertEqual(rejected.module.uri, consumer.uri)
        self.assertTrue(rejected.diagnostics)
        self.assertTrue(
            all(cause.module.uri == fragment.uri for cause in rejected.diagnostics)
        )
        resolver = IncrementalResolver()
        self.assertEqual(resolver.resolve((consumer, fragment, independent)), result)
        self.assertEqual(resolver.resolve((consumer, fragment, independent)), result)

    def test_imported_document_mutation_and_diagnostic_probes_create_no_references(
        self,
    ) -> None:
        fragment = module(
            "fragment",
            'from graphql_desk import gql\nf=gql("fragment F on User { id }")',
        )
        consumer = module(
            "main",
            "from graphql_desk import gql\nfrom fragment import f\n"
            'f.ast.definitions=()\ndoc=gql("{ user { ...F } }", [f])',
        )
        result = resolve_project((consumer, fragment))
        self.assertEqual(result.calls, ())
        self.assertTrue(result.diagnostics)
        consumer = module(
            "main",
            "from generated.main import graphql as query\nfrom fragment import f\n"
            'doc=query("{ user { ...F } }", [f])',
        )
        facts = ProjectBindings(
            "app",
            ("main",),
            factories=(FactoryBinding("generated.main", "graphql", "main"),),
        )
        result = resolve_project((consumer, fragment), bindings=(facts,))
        self.assertEqual(result.calls, ())
        self.assertTrue(any("ambiguous" in item.message for item in result.diagnostics))

    def test_imported_ast_projection_mutation_traces_document_origin(self) -> None:
        fragment = module(
            "fragment",
            'from graphql_desk import gql\nf=gql("fragment F on User { id }")\n'
            "tree=f.ast",
        )
        consumer = module(
            "main",
            "from graphql_desk import gql\nfrom fragment import f, tree\n"
            'tree.definitions=()\ndoc=gql("{ user { ...F } }", [f])',
        )
        result = resolve_project((consumer, fragment))
        self.assertEqual(result.calls, ())
        self.assertTrue(result.diagnostics)


class IncrementalResolverTests(unittest.TestCase):
    def test_parse_and_project_reuse_with_exact_input_identity(self) -> None:
        resolver = IncrementalResolver()
        first = module("main", 'from graphql_desk import gql\ndoc = gql("{ id }")')
        other = replace(first, project="other", uri="file:///other.py")
        inputs = (first, other)
        expected = resolve_project(inputs)
        with (
            patch.object(
                source_graph, "_parse", wraps=getattr(source_graph, "_parse")
            ) as parse,
            patch.object(
                source_graph, "_resolve", wraps=getattr(source_graph, "_resolve")
            ) as resolve,
        ):
            self.assertEqual(resolver.resolve(inputs), expected)
            self.assertEqual((parse.call_count, resolve.call_count), (2, 2))
            self.assertEqual(resolver.resolve(inputs), expected)
            self.assertEqual((parse.call_count, resolve.call_count), (2, 2))
            changed = replace(first, source=first.source.replace("id", "name"))
            result = resolver.resolve((changed, other))
            self.assertEqual((parse.call_count, resolve.call_count), (3, 3))
            self.assertEqual(result.calls[0].source, "{ name }")
            # Older inputs are authoritative too; chronology belongs to the caller.
            self.assertEqual(resolver.resolve(inputs), expected)
            self.assertEqual((parse.call_count, resolve.call_count), (4, 4))
            resolver.clear()
            self.assertEqual(resolver.resolve(inputs), expected)
            self.assertEqual((parse.call_count, resolve.call_count), (6, 6))

    def test_unchanged_files_reparse_never_but_recompute_in_changed_project(
        self,
    ) -> None:
        resolver = IncrementalResolver()
        constants = module("constants", 'TEXT = "{ id }"')
        consumer = module(
            "consumer",
            "from constants import TEXT\nfrom graphql_desk import gql\ndoc = gql(TEXT)",
        )
        resolver.resolve((constants, consumer))
        with (
            patch.object(
                source_graph, "_parse", wraps=getattr(source_graph, "_parse")
            ) as parse,
            patch.object(
                source_graph, "_resolve", wraps=getattr(source_graph, "_resolve")
            ) as resolve,
        ):
            changed = replace(constants, source='TEXT = "{ name }"')
            result = resolver.resolve((changed, consumer))
            self.assertEqual(parse.call_count, 1)
            self.assertEqual(resolve.call_args.args[0], (changed, consumer))
            self.assertEqual(result, resolve_project((changed, consumer)))

    def test_binding_changes_recompute_without_parsing(self) -> None:
        resolver = IncrementalResolver()
        consumer = module(
            "main", 'from generated import graphql\ndoc = graphql("{ id }")'
        )
        first = ProjectBindings(
            "app",
            ("one", "two"),
            factories=(FactoryBinding("generated", "graphql", "one"),),
        )
        second = replace(
            first, factories=(FactoryBinding("generated", "graphql", "two"),)
        )
        resolver.resolve((consumer,), bindings=(first,))
        with (
            patch.object(
                source_graph, "_parse", wraps=getattr(source_graph, "_parse")
            ) as parse,
            patch.object(
                source_graph, "_resolve", wraps=getattr(source_graph, "_resolve")
            ) as resolve,
        ):
            result = resolver.resolve((consumer,), bindings=(second,))
            self.assertEqual((parse.call_count, resolve.call_count), (0, 1))
            self.assertEqual(result.calls[0].schema_name, "two")
        self.assertEqual(result, resolve_project((consumer,), bindings=(second,)))

    def test_negative_lookups_mutation_failures_and_reexports_recover(self) -> None:
        resolver = IncrementalResolver()
        consumer = module(
            "consumer", "from bridge import query, TEXT\ndoc = query(TEXT)"
        )
        bridge = module(
            "bridge", 'from graphql_desk import gql as query\nTEXT = "{ id }"'
        )
        mutation = module(
            "mutation", "import graphql_desk as desk\ndesk.gql = object()"
        )
        invalid = replace(bridge, source="def (")
        replacement = replace(
            bridge, source='from graphql_desk import gql as query\nTEXT = "{ name }"'
        )
        shadow = module("graphql_desk", "gql = object()")
        for inputs in (
            (consumer,),
            (consumer, bridge),
            (consumer, bridge, mutation),
            (consumer, bridge),
            (consumer, invalid),
            (consumer, replacement),
            (consumer, replacement, shadow),
            (consumer, replacement),
            (),
            (consumer, bridge),
        ):
            with self.subTest(inputs=inputs):
                self.assertEqual(resolver.resolve(inputs), resolve_project(inputs))

    def test_ordering_versions_packages_and_failed_parse_reuse(self) -> None:
        resolver = IncrementalResolver()
        good = module("main", 'from graphql_desk import gql\na = gql("{ id }")')
        dynamic = module("dynamic", "from graphql_desk import gql\na = gql(unknown)")
        invalid = module("broken", "def (")
        other = replace(good, project="other", uri="file:///other.py")
        other_bad = replace(invalid, project="other", uri="file:///other-bad.py")
        initial = (dynamic, other, invalid, good, other_bad)
        for inputs in (
            initial,
            tuple(reversed(initial)),
            (replace(good, version=2), other),
            (replace(good, uri="file:///new.py", is_package=True), other),
            initial,
        ):
            self.assertEqual(resolver.resolve(inputs), resolve_project(inputs))
        with patch.object(
            source_graph, "_parse", wraps=getattr(source_graph, "_parse")
        ) as parse:
            self.assertEqual(resolver.resolve(initial), resolve_project(initial))
            # The cold comparison parses; the cached request itself does not.
            self.assertEqual(parse.call_count, len(initial))

    def test_invalid_request_does_not_replace_cache(self) -> None:
        resolver = IncrementalResolver()
        item = module("main", 'from graphql_desk import gql\na = gql("{ id }")')
        expected = resolver.resolve((item,))
        for inputs, bindings in (
            ((item, item), ()),
            ((replace(item, module="bad-name"),), ()),
            ((item,), (ProjectBindings("unused", ()),)),
            ((item,), (ProjectBindings("unused", ("one",)),) * 2),
        ):
            with self.subTest(inputs=inputs, bindings=bindings):
                with self.assertRaises(ValueError) as cold:
                    resolve_project(inputs, bindings=bindings)
                with self.assertRaisesRegex(ValueError, str(cold.exception)):
                    resolver.resolve(inputs, bindings=bindings)
        with (
            patch.object(
                source_graph, "_parse", wraps=getattr(source_graph, "_parse")
            ) as parse,
            patch.object(
                source_graph, "_resolve", wraps=getattr(source_graph, "_resolve")
            ) as resolve,
        ):
            self.assertEqual(resolver.resolve((item,)), expected)
            self.assertEqual((parse.call_count, resolve.call_count), (0, 0))

    def test_failed_dependency_traversal_recovers_without_mutable_state_leaks(
        self,
    ) -> None:
        resolver = IncrementalResolver()
        fragment = module(
            "fragment",
            'from graphql_desk import gql\nf=gql("fragment F on User { id }")',
        )
        broken = replace(
            fragment,
            source=fragment.source + "\ndeep=gql(" + "+".join(['"x"'] * 550) + ")",
        )
        consumer = module(
            "main",
            "from graphql_desk import gql\nfrom fragment import f\n"
            'doc=gql("{ user { ...F } }", [f])',
        )
        for inputs in (
            (consumer, fragment),
            (consumer, broken),
            (consumer, broken),
            (consumer, fragment),
        ):
            self.assertEqual(resolver.resolve(inputs), resolve_project(inputs))

    def test_failed_update_is_atomic_and_removed_parses_are_not_retained(self) -> None:
        resolver = IncrementalResolver()
        original = module("main", 'from graphql_desk import gql\ndoc=gql("{ id }")')
        changed = replace(original, source=original.source.replace("id", "name"))
        expected = resolver.resolve((original,))
        with patch.object(
            source_graph, "_resolve", side_effect=RuntimeError("interrupted")
        ):
            with self.assertRaisesRegex(RuntimeError, "interrupted"):
                resolver.resolve((changed,))
        with (
            patch.object(
                source_graph, "_parse", wraps=getattr(source_graph, "_parse")
            ) as parse,
            patch.object(
                source_graph, "_resolve", wraps=getattr(source_graph, "_resolve")
            ) as resolve,
        ):
            self.assertEqual(resolver.resolve((original,)), expected)
            self.assertEqual((parse.call_count, resolve.call_count), (0, 0))
            resolver.resolve(())
            self.assertEqual(resolver.resolve((original,)), expected)
            self.assertEqual((parse.call_count, resolve.call_count), (1, 1))
