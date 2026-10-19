import unittest
from dataclasses import replace

from graphql_desk.source import extract_calls
from graphql_desk.source_graph import (
    FactoryBinding,
    ModuleSnapshot,
    ProjectBindings,
    resolve_project,
)


def module(
    name: str, source: str, *, project: str = "app", package: bool = False
) -> ModuleSnapshot:
    return ModuleSnapshot(
        project, name, f"file:///{project}/{name}.py", 1, source, package
    )


class SourceGraphTests(unittest.TestCase):
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
