import unittest
from dataclasses import FrozenInstanceError

from graphql_desk.source import extract_calls


class SourceTests(unittest.TestCase):
    def test_import_aliases_qualified_calls_and_literal_decoding(self) -> None:
        source = '''from graphql_desk import graphql as query, gql
import graphql_desk as desk
a = query("query " "A { id }", operation="A")
b = gql(r"query B { field(arg: \\n) }")
c = desk.graphql("""query C {
  id
}""", [], operation=None)
'''
        result = extract_calls(source)
        self.assertEqual(len(result.calls), 3)
        self.assertEqual(result.diagnostics, ())
        self.assertEqual(result.calls[0].source, "query A { id }")
        self.assertEqual(result.calls[0].operation, "A")
        self.assertIn("\\n", result.calls[1].source)
        self.assertIn("\n", result.calls[2].source)
        for call in result.calls:
            self.assertTrue(
                source[call.call_range.start : call.call_range.end].endswith(")")
            )
            self.assertTrue(source[call.literal_range.start : call.literal_range.end])

    def test_unicode_crlf_offsets_are_characters(self) -> None:
        source = 'from graphql_desk import gql\r\ntext="🙂"; result=gql("{ café }")\r\n'
        call = extract_calls(source).calls[0]
        self.assertEqual(
            source[call.call_range.start : call.call_range.end], 'gql("{ café }")'
        )
        self.assertEqual(
            source[call.literal_range.start : call.literal_range.end], '"{ café }"'
        )

    def test_nonphysical_unicode_line_separator_preserves_offsets(self) -> None:
        for newline in ("\n", "\r\n", "\r"):
            source = (
                f'from graphql_desk import gql{newline}text="a\u2028b"; gql("{{ id }}")'
            )
            call = extract_calls(source).calls[0]
            self.assertEqual(
                source[call.call_range.start : call.call_range.end], 'gql("{ id }")'
            )

    def test_bindings_and_lexical_shadowing(self) -> None:
        cases = [
            'gql("{ id }")\nfrom graphql_desk import gql',
            'from graphql_desk import gql\ngql = other\ngql("{ id }")',
            'import graphql_desk as desk\ndesk = other\ndesk.gql("{ id }")',
            'from graphql_desk import gql\ndef f(gql):\n return gql("{ id }")',
            'from graphql_desk import gql\ndef f():\n gql("{ id }")\n gql = other',
            (
                "from graphql_desk import gql\ndef f():\n gql = other\n"
                ' def inner():\n  return gql("{ id }")'
            ),
            'if flag:\n from graphql_desk import gql\ngql("{ id }")',
            'from graphql_desk import gql\nfrom other import *\ngql("{ id }")',
            'from other import gql\ngql("{ id }")',
            (
                "try:\n pass\nexcept* ValueError:\n"
                ' from graphql_desk import gql\ngql("{ id }")'
            ),
            (
                "from graphql_desk import gql\n"
                'def f(x=(gql := other)):\n pass\ngql("{ id }")'
            ),
            (
                "from graphql_desk import gql\n"
                'f = lambda x=(gql := other): x\ngql("{ id }")'
            ),
            (
                "from graphql_desk import gql\n"
                'class C((gql := other)):\n pass\ngql("{ id }")'
            ),
            'import graphql_desk as desk\ndesk.gql = other\ndesk.gql("{ id }")',
            (
                "import graphql_desk as desk\ndef patch():\n desk.gql = other\n"
                'patch()\ndesk.gql("{ id }")'
            ),
            (
                "import graphql_desk as desk\ndef patch():\n del desk.gql\n"
                'patch()\ndesk.gql("{ id }")'
            ),
        ]
        for source in cases:
            with self.subTest(source=source):
                self.assertEqual(extract_calls(source).calls, ())

    def test_nested_functions_and_local_imports(self) -> None:
        source = """from graphql_desk import gql
def outer(unrelated):
 def inner():
  return gql("{ id }")
 return inner
def local():
 from graphql_desk import graphql as query
 return query("{ name }")
"""
        self.assertEqual(len(extract_calls(source).calls), 2)

    def test_unsupported_arguments_never_return_successful_calls(self) -> None:
        cases = {
            "gql(value)": "dynamic-source",
            'gql(f"{value}")': "dynamic-source",
            'gql("a" + "b")': "dynamic-source",
            'gql("{ id }", [fragment])': "unresolved-fragments",
            'gql("{ id }", operation=selector)': "dynamic-operation",
            'gql("{ id }", operation=1)': "dynamic-operation",
            "gql(*args)": "dynamic-arguments",
            'gql("{ id }", **kwargs)': "dynamic-arguments",
            'gql("{ id }", source="duplicate")': "duplicate-arguments",
            'gql("{ id }", unexpected=True)': "unsupported-arguments",
        }
        for call, code in cases.items():
            with self.subTest(call=call):
                result = extract_calls("from graphql_desk import gql\n" + call)
                self.assertEqual(result.calls, ())
                self.assertEqual(result.diagnostics[0].code, code)

    def test_keyword_source_and_empty_fragments(self) -> None:
        result = extract_calls(
            'from graphql_desk import gql\ngql(source="{ id }", fragments=())'
        )
        self.assertEqual(len(result.calls), 1)

    def test_unsupported_scope_reports_candidate(self) -> None:
        for expression in (
            'lambda: gql("{ id }")',
            '[gql("{ id }") for x in items]',
            'None\nclass C:\n query=gql("{ id }")',
        ):
            result = extract_calls("from graphql_desk import gql\nx=" + expression)
            self.assertEqual(result.calls, ())
            self.assertEqual(result.diagnostics[0].code, "unsupported-scope")

    def test_invalid_python_and_frozen_records(self) -> None:
        result = extract_calls("gql(\x00")
        self.assertEqual(result.diagnostics[0].code, "invalid-python")
        self.assertEqual(result.calls, ())
        with self.assertRaises(FrozenInstanceError):
            setattr(result.diagnostics[0], "code", "changed")
        for source in (
            'from graphql_desk import gql\nreturn gql("{ id }")',
            'from graphql_desk import gql\ngql("\ud800")',
        ):
            self.assertEqual(
                extract_calls(source).diagnostics[0].code, "invalid-python"
            )

    def test_complex_syntax_returns_diagnostic_without_partial_success(self) -> None:
        source = 'from graphql_desk import gql\ngql("{ id }")\nx=' + "1+" * 700 + "1"
        result = extract_calls(source)
        self.assertEqual(result.calls, ())
        self.assertTrue(result.diagnostics)

    def test_newer_generic_function_scope_is_not_guessed(self) -> None:
        source = 'from graphql_desk import gql\ndef f[gql]():\n return gql("{ id }")'
        result = extract_calls(source)
        self.assertEqual(result.calls, ())
        self.assertTrue(result.diagnostics)


if __name__ == "__main__":
    unittest.main()
