"""Real parser companion imports and checker inference without casts."""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from graphql import GraphQLError

from graphql_desk.parse_generation import (
    render_captured_parse_companion,
    render_parse_companion,
)
from graphql_desk.source_graph import ModuleSnapshot
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

    def test_captured_owner_selection_isolates_syntax_and_dynamic_fallbacks(
        self,
    ) -> None:
        modules = (
            ModuleSnapshot(
                "p",
                "main",
                "file:///main.py",
                1,
                "from graphql_desk import parse\n"
                "good=parse('query Q { missing }')\n"
                "bad=parse('query {')\n"
                "again=parse('query {')\n"
                "dynamic=parse(unknown)",
            ),
            ModuleSnapshot(
                "p",
                "other",
                "file:///other.py",
                1,
                "from graphql_desk import parse\nbad=parse('query {')",
            ),
        )
        with patch("builtins.open", side_effect=AssertionError("No writes")):
            output = render_captured_parse_companion(
                modules, project_id="p", module="main"
            )
        self.assertIn("'query Q { missing }'", output.companion.stub)
        self.assertNotIn("_ParseLiteral['query {']", output.companion.stub)
        self.assertEqual(
            {item.code for item in output.diagnostics},
            {"parse-syntax", "parse-fallback"},
        )
        self.assertTrue(
            all(item.module.module == "main" for item in output.diagnostics)
        )
        syntax_ranges = [
            item.range for item in output.diagnostics if item.code == "parse-syntax"
        ]
        self.assertEqual(len(syntax_ranges), 2)
        self.assertNotEqual(*syntax_ranges)
        self.assertTrue(
            any("GraphQL request" in item.message for item in output.diagnostics)
        )
        self.assertEqual(
            output,
            render_captured_parse_companion(
                tuple(reversed(modules)), project_id="p", module="main"
            ),
        )
        with self.assertRaises(ValueError):
            render_captured_parse_companion(modules, project_id="p", module="missing")

    def test_captured_nesting_failures_preserve_independent_declarations(self) -> None:
        parser_limit = "{" + "a{" * 350 + "id" + "}" * 351
        declaration_limit = "{" + "a{" * 150 + "id" + "}" * 151
        module = ModuleSnapshot(
            "p",
            "main",
            "file:///main.py",
            1,
            "from graphql_desk import parse\n"
            "good=parse('{ id }')\n"
            f"too_deep=parse({parser_limit!r})\n"
            f"too_nested=parse({declaration_limit!r})\n"
            "also_good=parse('{ other }')\n",
        )
        output = render_captured_parse_companion(
            (module,), project_id="p", module="main"
        )
        self.assertEqual(
            output.companion.stub.count("def parse(source: _ParseLiteral["), 2
        )
        self.assertIn("_ParseLiteral['{ id }']", output.companion.stub)
        self.assertIn("_ParseLiteral['{ other }']", output.companion.stub)
        self.assertEqual(
            [item.code for item in output.diagnostics], ["parse-fallback"] * 2
        )
        self.assertTrue(
            all(item.range.end > item.range.start for item in output.diagnostics)
        )

    def test_captured_opt_in_reexports_type_original_source_without_schema_facts(
        self,
    ) -> None:
        source = "query Q { missing }"
        fixture = """from typing import TYPE_CHECKING, Literal
from typing_extensions import assert_type
from graphql import DocumentNode, Source
from graphql_desk import parse as root_parse
from shim import reader
from constants import TEXT
value=reader(TEXT)
assert_type(value.definitions[0].name.value,Literal["Q"])
assert_type(value.definitions[0].selection_set.selections[0].name.value,Literal["missing"])
assert reader is root_parse
assert_type(reader(Source(TEXT)),DocumentNode)
assert_type(reader(TEXT,no_location=True),DocumentNode)
assert_type(reader("scalar Date"),DocumentNode)
def dynamic(text:str)->DocumentNode:
 assert_type(reader(text),DocumentNode)
 return reader(text)
assert dynamic(TEXT).kind == "document"
if TYPE_CHECKING:
 wrong:Literal["Other"]=value.definitions[0].name.value
 value.nonexistent
"""
        modules = (
            ModuleSnapshot(
                "p",
                "parser_companion",
                "file:///parser_companion.py",
                1,
                render_parse_companion(()).runtime,
            ),
            ModuleSnapshot(
                "p",
                "shim",
                "file:///shim.py",
                1,
                "from parser_companion import parse as reader\n__all__=['reader']",
            ),
            ModuleSnapshot(
                "p",
                "constants",
                "file:///constants.py",
                1,
                f"from typing import Literal\nTEXT:Literal[{source!r}]={source!r}\n",
            ),
            ModuleSnapshot("p", "main", "file:///main.py", 1, fixture),
        )
        output = render_captured_parse_companion(modules, project_id="p", module="main")
        self.assertTrue(output.diagnostics)
        self.assertEqual({item.code for item in output.diagnostics}, {"parse-fallback"})
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for module in modules:
                (root / f"{module.module}.py").write_text(
                    module.source, encoding="utf8"
                )
            (root / "parser_companion.py").write_text(
                output.companion.runtime, encoding="utf8"
            )
            (root / "parser_companion.pyi").write_text(
                output.companion.stub, encoding="utf8"
            )
            errors = {
                index
                for index, line in enumerate(fixture.splitlines(), 1)
                if "wrong:" in line or "value.nonexistent" in line
            }
            factory_tests.FactoryGenerationTests().check(root, {"main.py": errors})
            result = subprocess.run(
                [sys.executable, "main.py"], cwd=root, capture_output=True, text=True
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

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
