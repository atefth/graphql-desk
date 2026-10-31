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
            "def read():\n    q = gql('x')\n",
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

    def test_rebound_builtin_and_initializer_are_rejected(self) -> None:
        for source in (
            "from graphql_desk import gql\ngql = custom\nq = gql('x')\n",
            "from graphql_desk import gql\nfrom other import gql\nq = gql('x')\n",
            "import graphql_desk as g\ng = custom\nq = g.gql('x')\n",
            "import graphql_desk as g\ng.gql = custom\nq = g.gql('x')\n",
            "import graphql_desk as g\nq = g.gql('x')\n",
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
