"""Real parser companion imports and checker inference without casts."""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from graphql import GraphQLError

from graphql_desk.parse_generation import render_parse_companion
from tests import test_factory_generation as factory_tests


class ParseGenerationTests(unittest.TestCase):
    def test_pure_deterministic_deduplicated_schema_agnostic_declarations(self) -> None:
        sources = ("query Q { nonexistent }", "fragment F on Unknown { id }")
        with patch("builtins.open", side_effect=AssertionError("No writes")):
            output = render_parse_companion(sources)
        self.assertEqual(output, render_parse_companion((*reversed(sources), *sources)))
        self.assertEqual(output.stub.count("def parse(source: _ParseLiteral["), 2)
        ordinary = render_parse_companion(("scalar Date",))
        self.assertNotIn("@_overload", ordinary.stub)
        self.assertEqual(ordinary, render_parse_companion(()))
        with self.assertRaises(GraphQLError) as caught:
            render_parse_companion(("query {",))
        self.assertTrue(caught.exception.locations)

    def test_original_source_exact_literals_fallbacks_and_runtime_identity(
        self,
    ) -> None:
        source = "query Q { missing(arg: 1) }"
        companion = render_parse_companion((source, "fragment F on Unknown { id }"))
        fixture = f"""from typing import TYPE_CHECKING, Literal
from typing_extensions import assert_type
from graphql import DocumentNode, Source, GraphQLError
from graphql_desk import parse as root_parse
from parser_companion import parse
assert parse is root_parse
exact = parse({source!r})
assert_type(exact.kind, Literal["document"])
assert_type(exact.definitions[0].name.value, Literal["Q"])
assert_type(
 exact.definitions[0].selection_set.selections[0].name.value, Literal["missing"]
)
assert exact.loc is not None
assert exact.loc.start == 0
assert_type(parse(Source({source!r})), DocumentNode)
assert_type(parse({source!r}, True), DocumentNode)
assert_type(parse({source!r}, max_tokens=100), DocumentNode)
assert_type(parse({source!r}, experimental_fragment_arguments=True), DocumentNode)
assert_type(parse("scalar Date"), DocumentNode)
def dynamic(text: str, flag: bool) -> DocumentNode:
 assert_type(parse(text), DocumentNode)
 assert_type(parse({source!r}, no_location=flag), DocumentNode)
 return parse(text)
assert dynamic({source!r}, False).kind == "document"
assert parse({source!r}, no_location=True).loc is None
assert parse(
 "fragment F($v: Int) on T {{ f(v:$v) }}",
 experimental_fragment_arguments=True,
).kind == "document"
try:
 parse({source!r}, max_tokens=1)
except GraphQLError:
 pass
else:
 raise AssertionError("Token bound must retain runtime errors")
if TYPE_CHECKING:
 wrong: Literal["Other"] = exact.definitions[0].name.value
 exact.nonexistent
"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "parser_companion.py").write_text(
                companion.runtime, encoding="utf8"
            )
            (root / "parser_companion.pyi").write_text(companion.stub, encoding="utf8")
            (root / "fixture.py").write_text(fixture, encoding="utf8")
            errors = {
                index
                for index, line in enumerate(fixture.splitlines(), 1)
                if "wrong:" in line or "exact.nonexistent" in line
            }
            factory_tests.FactoryGenerationTests().check(root, {"fixture.py": errors})
            result = subprocess.run(
                [sys.executable, "fixture.py"], cwd=root, capture_output=True, text=True
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            # Also check the ordinary-only module, with no unused overload imports.
            ordinary = render_parse_companion(("scalar Date",))
            (root / "ordinary.py").write_text(ordinary.runtime, encoding="utf8")
            (root / "ordinary.pyi").write_text(ordinary.stub, encoding="utf8")
            (root / "fixture.py").write_text(
                "from graphql import DocumentNode\n"
                "from typing_extensions import assert_type\n"
                "from ordinary import parse\n"
                "assert_type(parse('scalar Date'),DocumentNode)\n",
                encoding="utf8",
            )
            factory_tests.FactoryGenerationTests().check(root)
