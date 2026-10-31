import sys
import unittest

from graphql_desk.editor_refactor import extraction_contexts
from graphql_desk.source_graph import FactoryBinding
from graphql_desk.source_maps import convert_offset


class EditorRefactorTests(unittest.TestCase):
    def test_module_assignment_and_existing_tuple_use_utf16_offsets(self) -> None:
        source = (
            "# 😀\nfrom graphql_desk import gql\nuser_fields = 1\n"
            "query = gql('{ user { name } }', (card,))\n"
        )
        contexts = extraction_contexts(source)
        context = next(iter(contexts.values()))
        self.assertEqual(context["factory"], "gql")
        self.assertEqual(context["indent"], "")
        self.assertEqual(context["fragment_mode"], "populated")
        self.assertEqual(
            context["statement_start"],
            convert_offset(source, source.index("query =")),
        )
        self.assertEqual(
            context["fragment_insertion"],
            convert_offset(source, source.index("(card,") + 1),
        )

    def test_new_and_empty_composition_and_unsafe_statement(self) -> None:
        for call, mode in (
            ("gql(source='x', operation='Q')", "new"),
            ("gql('x', fragments=[])", "empty_list"),
            ("gql('x', ())", "empty_tuple"),
        ):
            with self.subTest(call=call):
                context = next(
                    iter(
                        extraction_contexts(
                            f"from graphql_desk import gql\nq = {call}\n"
                        ).values()
                    )
                )
                self.assertEqual(context["fragment_mode"], mode)
        for source in (
            "if flag: q = gql('x')\n",
            "q = gql('x', fragments=dynamic)\n",
            "q = get_factory()('x')\n",
            "consume(gql('x'))\n",
            "from templates import custom\nq = custom(source='x')\n",
        ):
            self.assertEqual(extraction_contexts(source), {}, source)

    def test_known_schema_initializer_is_supported(self) -> None:
        source = (
            "from graphql_desk import init_graphql as init\n"
            "factory = init('default')\nq = factory('x')\n"
        )
        contexts = extraction_contexts(source)
        self.assertEqual(len(contexts), 1)
        self.assertEqual(next(iter(contexts.values()))["factory"], "factory")

    def test_configured_factory_imports_are_supported_only_when_configured(
        self,
    ) -> None:
        factories = (
            FactoryBinding("custom_templates", "make_document", "default", True),
            FactoryBinding(
                "graphql_desk_generated.default", "graphql", "default", True
            ),
        )
        for source, expected in (
            (
                "from custom_templates import make_document as custom\n"
                "query = custom('{ user { id } }')\n",
                "custom",
            ),
            (
                "from graphql_desk_generated.default import graphql as typed\n"
                "query = typed('{ user { id } }')\n",
                "typed",
            ),
        ):
            with self.subTest(source=source):
                contexts = extraction_contexts(source, factories)
                self.assertEqual(len(contexts), 1)
                self.assertEqual(next(iter(contexts.values()))["factory"], expected)

        self.assertEqual(
            extraction_contexts(
                "from custom_templates import make_document as custom\n"
                "query = custom('{ user { id } }')\n"
            ),
            {},
        )
        self.assertEqual(
            extraction_contexts(
                "from custom_templates import make_document as custom\n"
                "query = custom('{ user { id } }')\n",
                (FactoryBinding("custom_templates", "make_document", "default"),),
            ),
            {},
        )
        self.assertEqual(
            extraction_contexts(
                "from custom_templates import make_document as custom\n"
                "custom = replacement\nquery = custom('{ user { id } }')\n",
                factories,
            ),
            {},
        )
        self.assertEqual(
            extraction_contexts(
                "from other_templates import make_document as custom\n"
                "query = custom('{ user { id } }')\n",
                factories,
            ),
            {},
        )
        self.assertEqual(
            extraction_contexts(
                "from .custom_templates import make_document as custom\n"
                "query = custom('{ user { id } }')\n",
                factories,
            ),
            {},
        )

    def test_module_qualified_graphql_desk_factories_are_supported(self) -> None:
        for source, expected in (
            (
                "import graphql_desk as desk\n"
                "query = desk.gql('query { user { id } }')\n",
                "desk.gql",
            ),
            (
                "import graphql_desk\n"
                "async def fetch():\n"
                "    query = graphql_desk.graphql('query { user { id } }')\n",
                "graphql_desk.graphql",
            ),
            (
                "import graphql_desk as desk\n"
                "factory = desk.init_graphql('default')\n"
                "query = factory('query { user { id } }')\n",
                "factory",
            ),
        ):
            with self.subTest(source=source):
                contexts = extraction_contexts(source)
                self.assertEqual(len(contexts), 1)
                self.assertEqual(next(iter(contexts.values()))["factory"], expected)

    def test_module_qualified_factory_mutation_and_rebinding_are_rejected(self) -> None:
        for source in (
            "import graphql_desk as desk\ndesk = other\nq = desk.gql('x')\n",
            "import graphql_desk as desk\ndesk.gql = other\nq = desk.gql('x')\n",
            "import graphql_desk as desk\n"
            "def patch():\n    del desk.gql\n"
            "q = desk.gql('x')\n",
            "import graphql_desk as desk\n"
            "setattr(desk, 'gql', other)\nq = desk.gql('x')\n",
            "import builtins\nimport graphql_desk as desk\n"
            "builtins.setattr(desk, 'gql', other)\nq = desk.gql('x')\n",
            "from builtins import setattr as set_attr\n"
            "import graphql_desk as desk\n"
            "set_attr(desk, 'gql', other)\nq = desk.gql('x')\n",
            "import graphql_desk as desk\nmutate(desk)\nq = desk.gql('x')\n",
            "import graphql_desk as desk\nmutate((desk,))\nq = desk.gql('x')\n",
            "import graphql_desk as desk\nimport graphql_desk as api\n"
            "mutate(api)\nq = desk.gql('x')\n",
            "import graphql_desk as desk\nmutate(desk.__dict__)\nq = desk.gql('x')\n",
            "import sys\nimport graphql_desk as desk\n"
            "sys.modules['graphql_desk'].gql = replacement\n"
            "q = desk.gql('x')\n",
            "import sys\nruntime = sys\nimport graphql_desk as desk\n"
            "runtime.modules['graphql_desk'].gql = replacement\n"
            "q = desk.gql('x')\n",
            "import sys\nruntime = sys\nregistry = runtime\n"
            "import graphql_desk as desk\n"
            "registry.modules['graphql_desk'].gql = replacement\n"
            "q = desk.gql('x')\n",
            "import graphql_desk as desk\n"
            "__builtins__['__import__']('graphql_desk').gql = replacement\n"
            "q = desk.gql('x')\n",
            "import sys as runtime\nimport graphql_desk as desk\n"
            "runtime.modules['graphql_desk'].gql = replacement\n"
            "q = desk.gql('x')\n",
            "import sys as runtime\nimport graphql_desk as desk\n"
            "getattr(runtime, 'modules')['graphql_desk'].gql = replacement\n"
            "q = desk.gql('x')\n",
            "from builtins import getattr as get_attr\n"
            "import sys as runtime\nimport graphql_desk as desk\n"
            "get_attr(runtime, 'modules')['graphql_desk'].gql = replacement\n"
            "q = desk.gql('x')\n",
            "import builtins as b\nimport sys as runtime\n"
            "import graphql_desk as desk\n"
            "b.getattr(runtime, 'modules')['graphql_desk'].gql = replacement\n"
            "q = desk.gql('x')\n",
            "import sys as runtime\nimport graphql_desk as desk\n"
            "vars(runtime)['modules']['graphql_desk'].gql = replacement\n"
            "q = desk.gql('x')\n",
            "from builtins import vars as get_vars\n"
            "import sys as runtime\nimport graphql_desk as desk\n"
            "get_vars(runtime)['modules']['graphql_desk'].gql = replacement\n"
            "q = desk.gql('x')\n",
            "import builtins as b\nimport sys as runtime\n"
            "import graphql_desk as desk\n"
            "b.vars(runtime)['modules']['graphql_desk'].gql = replacement\n"
            "q = desk.gql('x')\n",
            "import sys as runtime\nimport graphql_desk as desk\n"
            "vars()['runtime'].modules['graphql_desk'].gql = replacement\n"
            "q = desk.gql('x')\n",
            "from builtins import vars as get_vars\n"
            "import sys as runtime\nimport graphql_desk as desk\n"
            "get_vars()['runtime'].modules['graphql_desk'].gql = replacement\n"
            "q = desk.gql('x')\n",
            "import builtins as b\nimport sys as runtime\n"
            "import graphql_desk as desk\n"
            "b.vars()['runtime'].modules['graphql_desk'].gql = replacement\n"
            "q = desk.gql('x')\n",
            "from builtins import globals as get_globals\n"
            "import sys as runtime\nimport graphql_desk as desk\n"
            "get_globals()['runtime'].modules['graphql_desk'].gql = replacement\n"
            "q = desk.gql('x')\n",
            "g = globals\nimport sys as runtime\n"
            "import graphql_desk as desk\n"
            "g()['runtime'].modules['graphql_desk'].gql = replacement\n"
            "q = desk.gql('x')\n",
            "import builtins as b\nimport sys as runtime\n"
            "import graphql_desk as desk\n"
            "b.globals()['runtime'].modules['graphql_desk'].gql = replacement\n"
            "q = desk.gql('x')\n",
            "import builtins\nbi = builtins\nimport sys as runtime\n"
            "import graphql_desk as desk\n"
            "bi.locals()['runtime'].modules['graphql_desk'].gql = replacement\n"
            "q = desk.gql('x')\n",
            "from builtins import locals as get_locals\n"
            "namespace = get_locals\nimport sys as runtime\n"
            "import graphql_desk as desk\n"
            "namespace()['runtime'].modules['graphql_desk'].gql = replacement\n"
            "q = desk.gql('x')\n",
            "namespace = locals\nimport sys as runtime\n"
            "import graphql_desk as desk\n"
            "namespace()['runtime'].modules['graphql_desk'].gql = replacement\n"
            "q = desk.gql('x')\n",
            "import sys as runtime\nimport graphql_desk as desk\n"
            "runtime.__dict__['modules']['graphql_desk'].gql = replacement\n"
            "q = desk.gql('x')\n",
            "from sys import modules as registry\n"
            "import graphql_desk as desk\n"
            "registry['graphql_desk'].gql = replacement\nq = desk.gql('x')\n",
            "import importlib as loader\nimport graphql_desk as desk\n"
            "loader.import_module('graphql_desk').gql = replacement\n"
            "q = desk.gql('x')\n",
            "import importlib\nloader = importlib\n"
            "import graphql_desk as desk\n"
            "loader.import_module('graphql_desk').gql = replacement\n"
            "q = desk.gql('x')\n",
            "from importlib import import_module as load_module\n"
            "import graphql_desk as desk\n"
            "load_module('graphql_desk').gql = replacement\nq = desk.gql('x')\n",
            "from importlib import import_module as load_module\n"
            "importer = load_module\nimport graphql_desk as desk\n"
            "importer('graphql_desk').gql = replacement\nq = desk.gql('x')\n",
            "import sys as runtime\nregistry = runtime.modules\n"
            "import graphql_desk as desk\n"
            "registry['graphql_desk'].gql = replacement\nq = desk.gql('x')\n",
            "from builtins import getattr as get_attr\n"
            "lookup = get_attr\nimport sys as runtime\n"
            "import graphql_desk as desk\n"
            "lookup(runtime, 'modules')['graphql_desk'].gql = replacement\n"
            "q = desk.gql('x')\n",
            "from builtins import getattr as lookup\n"
            "from builtins import vars as lookup\n"
            "import sys as runtime\nimport graphql_desk as desk\n"
            "lookup(runtime)['modules']['graphql_desk'].gql = replacement\n"
            "q = desk.gql('x')\n",
            "import builtins\nbi = builtins\nimport graphql_desk as desk\n"
            "bi.__import__('graphql_desk').gql = replacement\n"
            "q = desk.gql('x')\n",
            "import graphql_desk as desk\n"
            "__import__('graphql_desk').gql = replacement\nq = desk.gql('x')\n",
            "from builtins import __import__ as load\n"
            "import graphql_desk as desk\n"
            "load('graphql_desk').gql = replacement\nq = desk.gql('x')\n",
            "import builtins as b\nimport graphql_desk as desk\n"
            "b.__import__('graphql_desk').gql = replacement\n"
            "q = desk.gql('x')\n",
            "import graphql_desk as desk\nfrom other import *\nq = desk.gql('x')\n",
        ):
            with self.subTest(source=source):
                self.assertEqual(extraction_contexts(source), {}, source)

    def test_function_local_assignments_keep_suite_indentation_and_comment(
        self,
    ) -> None:
        source = (
            "# 😀\r\nfrom graphql_desk import gql\r\ndef read():\r\n"
            "    # local query\r\n"
            "    query: object = gql('query { user { name } }', ())\r\n"
            "async def fetch():\r\n    query = gql('{ user { id } }')\r\n"
        )
        contexts = extraction_contexts(source)
        self.assertEqual(len(contexts), 2)
        local, async_local = contexts.values()
        self.assertEqual(local["indent"], "    ")
        self.assertEqual(local["newline"], "\r\n")
        self.assertEqual(local["fragment_mode"], "empty_tuple")
        self.assertEqual(local["scope"], "function")
        self.assertEqual(
            local["statement_start"],
            convert_offset(source, source.index("    # local query")),
        )
        self.assertEqual(async_local["indent"], "    ")
        self.assertEqual(async_local["fragment_mode"], "new")
        self.assertEqual(async_local["scope"], "function")
        module = extraction_contexts("from graphql_desk import gql\nq=gql('x')\n")
        self.assertEqual(next(iter(module.values()))["scope"], "module")

    def test_function_local_conditional_suites_are_extractable(self) -> None:
        source = (
            "# 😀\r\n"
            "from graphql_desk import gql\r\n"
            "def read(enabled):\r\n"
            "    if enabled:\r\n"
            "        # primary branch\r\n"
            "        query = gql('query Read { user { id name } }')\r\n"
            "    elif not enabled:\r\n"
            "        query = gql('query Other { user { id } }')\r\n"
            "    else:\r\n"
            "        query = gql('query Fallback { user { name } }')\r\n"
        )
        contexts = extraction_contexts(source)
        self.assertEqual(len(contexts), 3)
        self.assertEqual({item["scope"] for item in contexts.values()}, {"function"})
        self.assertEqual({item["indent"] for item in contexts.values()}, {"        "})
        self.assertEqual({item["newline"] for item in contexts.values()}, {"\r\n"})

    def test_nested_function_suites_are_extractable(self) -> None:
        source = (
            "from graphql_desk import gql\n"
            "def outer():\n"
            "    def read():\n"
            "        query = gql('query Read { user { id name } }')\n"
            "        if enabled:\n"
            "            async def fetch():\n"
            "                query = gql('query Fetch { user { id } }')\n"
        )
        contexts = extraction_contexts(source)
        self.assertEqual(len(contexts), 2)
        self.assertEqual({item["scope"] for item in contexts.values()}, {"function"})
        self.assertEqual(
            {item["indent"] for item in contexts.values()},
            {"        ", "                "},
        )

    def test_function_local_loop_exception_with_and_match_suites_are_extractable(
        self,
    ) -> None:
        source = (
            "from graphql_desk import gql\n"
            "def read(items, ready, enabled):\n"
            "    for item in items:\n"
            "        loop_query = gql('query Loop { user { id } }')\n"
            "    else:\n"
            "        for_else_query = gql('query ForElse { user { id } }')\n"
            "    while ready:\n"
            "        while_query = gql('query While { user { id } }')\n"
            "        break\n"
            "    else:\n"
            "        while_else_query = gql('query WhileElse { user { id } }')\n"
            "    try:\n"
            "        try_query = gql('query Try { user { id } }')\n"
            "    except ValueError:\n"
            "        except_query = gql('query Except { user { id } }')\n"
            "    else:\n"
            "        try_else_query = gql('query TryElse { user { id } }')\n"
            "    finally:\n"
            "        finally_query = gql('query Finally { user { id } }')\n"
            "    with manager() as value:\n"
            "        with_query = gql('query With { user { id } }')\n"
            "    match enabled:\n"
            "        case True:\n"
            "            match_query = gql('query Match { user { id } }')\n"
            "        case _:\n"
            "            match_else_query = gql('query MatchElse { user { id } }')\n"
            "async def read_async(items):\n"
            "    async for item in items:\n"
            "        async_for_query = gql('query AsyncFor { user { id } }')\n"
            "    async with manager() as value:\n"
            "        async_with_query = gql('query AsyncWith { user { id } }')\n"
        )
        contexts = extraction_contexts(source)
        self.assertEqual(len(contexts), 13)
        self.assertEqual({item["scope"] for item in contexts.values()}, {"function"})
        self.assertEqual(
            {item["indent"] for item in contexts.values()}, {"        ", "            "}
        )
        self.assertEqual({item["newline"] for item in contexts.values()}, {"\n"})
        if sys.version_info >= (3, 11):
            except_star = (
                "from graphql_desk import gql\n"
                "def read():\n"
                "    try:\n"
                "        pass\n"
                "    except* ValueError:\n"
                "        query = gql('query ExceptStar { user { id } }')\n"
            )
            self.assertEqual(len(extraction_contexts(except_star)), 1)

    def test_function_local_extraction_rejects_scope_ambiguity(self) -> None:
        for source in (
            "from graphql_desk import gql\ndef read(gql):\n    q=gql('x')\n",
            "from graphql_desk import gql\n"
            "def read():\n    q=gql('x')\n    gql=custom\n",
            "from graphql_desk import gql\n"
            "def read():\n    global value\n    q=gql('x')\n",
            "from graphql_desk import gql\n"
            "def read():\n    value=locals()\n    q=gql('x')\n",
            "from graphql_desk import gql\n"
            "def read(items):\n"
            "    for item in items:\n"
            "        value=locals()\n"
            "        q=gql('x')\n",
            "from graphql_desk import gql\n"
            "def read(flag):\n"
            "    if flag:\n"
            "        class Nested:\n"
            "            q=gql('x')\n",
        ):
            with self.subTest(source=source):
                self.assertEqual(extraction_contexts(source), {}, source)

    def test_nested_function_extraction_rejects_ambiguous_bodies(self) -> None:
        for source in (
            "from graphql_desk import gql\n"
            "def outer():\n    def nested():\n"
            "        global value\n        q=gql('x')\n",
            "from graphql_desk import gql\n"
            "def outer():\n    def nested():\n"
            "        value=locals()\n        q=gql('x')\n",
            "from graphql_desk import gql\n"
            "def outer():\n    class Nested:\n"
            "        def read(self):\n            q=gql('x')\n",
        ):
            with self.subTest(source=source):
                self.assertEqual(extraction_contexts(source), {}, source)

    def test_rebound_builtin_and_initializer_are_rejected(self) -> None:
        for source in (
            "from graphql_desk import gql\ngql = custom\nq = gql('x')\n",
            "from graphql_desk import gql\nfrom other import gql\nq = gql('x')\n",
            "import graphql_desk as g\ng = custom\nq = g.gql('x')\n",
            "import graphql_desk as g\ng.gql = custom\nq = g.gql('x')\n",
            "from graphql_desk import init_graphql as init\n"
            "init = custom\nf = init('s')\nq = f('x')\n",
            "from graphql_desk import init_graphql as init\n"
            "f = init('s')\nf = custom\nq = f('x')\n",
            "from graphql_desk import gql\nfrom other import *\nq = gql('x')\n",
            "from graphql_desk import gql\nexec(code)\nq = gql('x')\n",
            "from graphql_desk import gql\neval(code)\nq = gql('x')\n",
            "from graphql_desk import gql\nrun = exec\nrun('gql = custom')\n"
            "q = gql('x')\n",
            "from graphql_desk import gql\nrun = eval\nrun('gql')\nq = gql('x')\n",
            "from builtins import exec as run\n"
            "from graphql_desk import gql\nrun('gql = custom')\nq = gql('x')\n",
            "from builtins import eval as run\n"
            "runner = run\nfrom graphql_desk import gql\n"
            "runner('gql')\nq = gql('x')\n",
            "import builtins as b\n"
            "from graphql_desk import gql\nb.exec('gql = custom')\nq = gql('x')\n",
            "import builtins\nbi = builtins\n"
            "from graphql_desk import gql\nrunner = bi.eval\n"
            "runner('gql')\nq = gql('x')\n",
            "from graphql_desk import gql\nglobals()['gql'] = custom\nq = gql('x')\n",
            "from graphql_desk import gql\n"
            "__builtins__['exec']('gql = custom')\nq = gql('x')\n",
            "q = gql('x')\nfrom graphql_desk import gql\n",
            "from graphql_desk import init_graphql as init\n"
            "q = f('x')\nf = init('s')\n",
        ):
            with self.subTest(source=source):
                self.assertEqual(extraction_contexts(source), {})
