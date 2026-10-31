import unittest

from graphql_desk.editor_refactor import extraction_contexts
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
            "import sys as runtime\nimport graphql_desk as desk\n"
            "runtime.modules['graphql_desk'].gql = replacement\n"
            "q = desk.gql('x')\n",
            "from sys import modules as registry\n"
            "import graphql_desk as desk\n"
            "registry['graphql_desk'].gql = replacement\nq = desk.gql('x')\n",
            "import importlib as loader\nimport graphql_desk as desk\n"
            "loader.import_module('graphql_desk').gql = replacement\n"
            "q = desk.gql('x')\n",
            "from importlib import import_module as load_module\n"
            "import graphql_desk as desk\n"
            "load_module('graphql_desk').gql = replacement\nq = desk.gql('x')\n",
            "import graphql_desk as desk\n"
            "__import__('graphql_desk').gql = replacement\nq = desk.gql('x')\n",
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

    def test_function_local_extraction_rejects_scope_ambiguity(self) -> None:
        for source in (
            "from graphql_desk import gql\ndef read(gql):\n    q=gql('x')\n",
            "from graphql_desk import gql\n"
            "def read():\n    q=gql('x')\n    gql=custom\n",
            "from graphql_desk import gql\n"
            "def read():\n    if flag:\n        q=gql('x')\n",
            "from graphql_desk import gql\n"
            "def read():\n    def nested():\n        q=gql('x')\n",
            "from graphql_desk import gql\n"
            "def read():\n    global value\n    q=gql('x')\n",
            "from graphql_desk import gql\n"
            "def read():\n    value=locals()\n    q=gql('x')\n",
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
            "from graphql_desk import gql\nglobals()['gql'] = custom\nq = gql('x')\n",
            "q = gql('x')\nfrom graphql_desk import gql\n",
            "from graphql_desk import init_graphql as init\n"
            "q = f('x')\nf = init('s')\n",
        ):
            with self.subTest(source=source):
                self.assertEqual(extraction_contexts(source), {})
