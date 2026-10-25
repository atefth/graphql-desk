"""Real parser companion imports and checker inference without casts."""

import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from graphql import DocumentNode, GraphQLError, parse

from graphql_desk.literal_ast import render_document_ast_types
from graphql_desk.parse_generation import (
    render_captured_parse_companion,
    render_captured_parse_companions,
    render_parse_companion,
)
from graphql_desk.source_graph import (
    ModuleSnapshot,
    ParseSources,
    resolve_parse_sources,
)
from tests import test_factory_generation as factory_tests


class ParseGenerationTests(unittest.TestCase):
    def test_pure_deterministic_deduplicated_schema_agnostic_declarations(self) -> None:
        sources = ("query Q { nonexistent }", "fragment F on Unknown { id }")
        with patch("builtins.open", side_effect=AssertionError("No writes")):
            output = render_parse_companion(sources)
        self.assertEqual(output, render_parse_companion((*reversed(sources), *sources)))
        self.assertEqual(output.stub.count("def parse(source: _ParseLiteral["), 4)
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

    def test_generated_parser_import_reuses_its_captured_literal_contract(self) -> None:
        module = ModuleSnapshot(
            "p",
            "main",
            "file:///main.py",
            1,
            "from graphql_desk_generated.parsers.main import parse\n"
            "value = parse('query Q { id }')\n",
        )
        companion = render_captured_parse_companion(
            (module,),
            project_id="p",
            module="main",
            generated_package="graphql_desk_generated",
        )
        self.assertIn("Literal['query Q { id }']", companion.companion.stub)
        self.assertEqual(
            render_captured_parse_companions(
                (module,),
                project_id="p",
                generated_package="graphql_desk_generated",
            ),
            (companion,),
        )

    def test_generated_parser_import_is_bound_to_its_source_module(self) -> None:
        main = ModuleSnapshot(
            "p",
            "main",
            "file:///main.py",
            1,
            "from graphql_desk_generated.parsers.other import parse\n"
            "value = parse('query Q { id }')\n",
        )
        other = ModuleSnapshot("p", "other", "file:///other.py", 1, "value = 1\n")
        self.assertEqual(
            render_captured_parse_companions(
                (main, other),
                project_id="p",
                generated_package="graphql_desk_generated",
            ),
            (),
        )

    def test_captured_nesting_failures_preserve_independent_declarations(self) -> None:
        parser_limit = "{" + "a{" * 350 + "id" + "}" * 351
        declaration_limit = "{ nested { id } }"
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

        def render(document: DocumentNode, *, name: str) -> str:
            # Declaration recursion thresholds vary across Python versions.
            if document.loc and document.loc.source.body == declaration_limit:
                raise ValueError("Controlled declaration nesting failure")
            return render_document_ast_types(document, name=name)

        with patch(
            "graphql_desk.parse_generation.render_document_ast_types",
            side_effect=render,
        ):
            output = render_captured_parse_companion(
                (module,), project_id="p", module="main"
            )
        self.assertEqual(
            output.companion.stub.count("def parse(source: _ParseLiteral["), 4
        )
        self.assertIn("_ParseLiteral['{ id }']", output.companion.stub)
        self.assertIn("_ParseLiteral['{ other }']", output.companion.stub)
        self.assertEqual(
            [item.code for item in output.diagnostics], ["parse-fallback"] * 2
        )
        self.assertEqual(
            sum(
                item.message == "Controlled declaration nesting failure"
                for item in output.diagnostics
            ),
            1,
        )
        self.assertTrue(
            all(item.range.end > item.range.start for item in output.diagnostics)
        )

    def test_captured_syntax_origins_follow_each_literal_and_import(self) -> None:
        cases = (
            ("'query Q { missing( }'", "}"),
            (r"'query Q { missing( \x7d'", r"\x7d"),
            (r"r'query Q { missing( }'", "}"),
            ("'''# \U0001f600\r\nquery Q { missing( }'''", "}"),
            ("'query {'", ""),
            ("'{\U0001f600}'", "\U0001f600"),
        )
        for expression, expected in cases:
            with self.subTest(expression=expression):
                source = "from graphql_desk import parse\nq=parse(" + expression + ")"
                module = ModuleSnapshot("p", "main", "file:///main.py", 7, source)
                output = render_captured_parse_companion(
                    (module,), project_id="p", module="main"
                )
                (diagnostic,) = output.diagnostics
                self.assertEqual(diagnostic.module.version, 7)
                self.assertEqual(
                    source[diagnostic.range.start : diagnostic.range.end], expected
                )
                if not expected:
                    self.assertEqual(diagnostic.range.start, source.rindex("'"))
        constants = ModuleSnapshot(
            "p",
            "constants",
            "file:///constants.py",
            4,
            "PREFIX='query Q { missing( '\nTAIL='\\x7d'\n",
        )
        main = ModuleSnapshot(
            "p",
            "main",
            "file:///main.py",
            9,
            "from graphql_desk import parse\n"
            "from constants import PREFIX,TAIL\n"
            "a=parse(PREFIX+TAIL)\nb=parse('query Q { missing( }')\n"
            "good=parse('{ id }')\n",
        )
        with patch("graphql_desk.parse_generation.parse", wraps=parse) as parser:
            output = render_captured_parse_companion(
                (constants, main), project_id="p", module="main"
            )
        self.assertEqual(parser.call_count, 3)
        self.assertEqual(
            [d.module.module for d in output.diagnostics], ["constants", "main"]
        )
        for diagnostic, module in zip(output.diagnostics, (constants, main)):
            self.assertEqual(diagnostic.module.uri, module.uri)
            self.assertEqual(diagnostic.module.version, module.version)
            self.assertEqual(
                module.source[diagnostic.range.start : diagnostic.range.end],
                r"\x7d" if module is constants else "}",
            )
        self.assertIn("_ParseLiteral['{ id }']", output.companion.stub)
        self.assertEqual(
            output,
            render_captured_parse_companion(
                (main, constants), project_id="p", module="main"
            ),
        )

    def test_captured_syntax_without_trusted_map_falls_back_to_owner_call(self) -> None:
        module = ModuleSnapshot(
            "p",
            "main",
            "file:///main.py",
            1,
            "from graphql_desk import parse\nq=parse('{')",
        )
        (call,) = resolve_parse_sources((module,)).calls
        for changed in (
            replace(call, contributions=()),
            replace(call, dependencies=(replace(call.module, version=2),)),
            replace(
                call, contributions=(replace(call.contributions[0], text="wrong"),)
            ),
        ):
            with self.subTest(changed=changed):
                with patch(
                    "graphql_desk.parse_generation.resolve_parse_sources",
                    return_value=ParseSources((changed,), ()),
                ):
                    output = render_captured_parse_companion(
                        (module,), project_id="p", module="main"
                    )
                (diagnostic,) = output.diagnostics
                self.assertEqual(
                    (diagnostic.module, diagnostic.range), (call.module, call.range)
                )

    def test_captured_eof_preserves_multiple_contributing_boundaries(self) -> None:
        prefix = ModuleSnapshot("p", "prefix", "file:///prefix.py", 2, "TEXT='query {'")
        empty = ModuleSnapshot("p", "empty", "file:///empty.py", 3, "EMPTY=''")
        main = ModuleSnapshot(
            "p",
            "main",
            "file:///main.py",
            1,
            "from graphql_desk import parse\nfrom prefix import TEXT\n"
            "from empty import EMPTY\nq=parse(TEXT+EMPTY)",
        )
        output = render_captured_parse_companion(
            (prefix, empty, main), project_id="p", module="main"
        )
        self.assertEqual(
            [d.module.module for d in output.diagnostics], ["prefix", "empty"]
        )
        for diagnostic, module in zip(output.diagnostics, (prefix, empty)):
            self.assertEqual(diagnostic.range.start, module.source.rindex("'"))
            self.assertEqual(diagnostic.range.start, diagnostic.range.end)

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
without_locations=reader(TEXT,no_location=True)
assert_type(without_locations.loc,None)
assert_type(without_locations.definitions[0].name.value,Literal["Q"])
assert_type(reader("scalar Date"),DocumentNode)
def dynamic(text:str)->DocumentNode:
 assert_type(reader(text),DocumentNode)
 return reader(text)
assert dynamic(TEXT).kind == "document"
def scoped() -> None:
 seed=1
 def helper() -> int:
  nonlocal seed
  return seed
 class Helper:
  global unrelated
  value=1
 assert helper()==Helper.value
 item=reader(TEXT)
 assert_type(item.definitions[0].name.value,Literal["Q"])
 assert item.kind=="document"
async def async_scoped() -> None:
 def helper() -> str:
  TEXT="local"
  return TEXT
 async def other() -> int:
  global unrelated
  return 1
 assert helper()=="local"
 assert await other()==1
 item=reader("query Async { missing }")
 assert_type(item.definitions[0].name.value,Literal["Async"])
 assert item.kind=="document"
scoped()
import asyncio
asyncio.run(async_scoped())
if TYPE_CHECKING:
 wrong:Literal["Other"]=value.definitions[0].name.value
 wrong_location:Literal[False]=without_locations.loc
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
                if "wrong:" in line
                or "wrong_location:" in line
                or "value.nonexistent" in line
            }
            factory_tests.FactoryGenerationTests().check(root, {"main.py": errors})
            result = subprocess.run(
                [sys.executable, "main.py"], cwd=root, capture_output=True, text=True
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_captured_no_location_only_call_keeps_its_literal_ast_type(self) -> None:
        fixture = """from typing import Literal
from typing_extensions import assert_type
from parser_companion import parse
value=parse('query Q { id }', no_location=True)
assert_type(value.loc, None)
assert_type(value.definitions[0].name.value, Literal["Q"])
assert_type(value.definitions[0].selection_set.selections[0].name.value, Literal["id"])
"""
        modules = (
            ModuleSnapshot(
                "p",
                "parser_companion",
                "file:///parser_companion.py",
                1,
                render_parse_companion(()).runtime,
            ),
            ModuleSnapshot("p", "main", "file:///main.py", 1, fixture),
        )
        output = render_captured_parse_companion(modules, project_id="p", module="main")
        self.assertEqual(output.diagnostics, ())
        self.assertIn("Parse0NoLocationDocument", output.companion.stub)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "main.py").write_text(fixture, encoding="utf8")
            (root / "parser_companion.py").write_text(
                output.companion.runtime, encoding="utf8"
            )
            (root / "parser_companion.pyi").write_text(
                output.companion.stub, encoding="utf8"
            )
            factory_tests.FactoryGenerationTests().check(root, {})

    def test_original_source_exact_literals_fallbacks_and_runtime_identity(
        self,
    ) -> None:
        source = "query Q { missing(arg: 1) }"
        companion = render_parse_companion((source, "fragment F on Unknown { id }"))
        fixture = f"""from typing import TYPE_CHECKING, Literal
from typing_extensions import assert_type
from graphql import DocumentNode, Source, GraphQLError
from graphql.language import Location
from graphql_desk import parse as root_parse
from parser_companion import parse
assert parse is root_parse
exact = parse({source!r})
assert_type(exact.loc, Location)
assert_type(exact.loc.start, int)
assert_type(exact.loc.end, int)
assert_type(exact.kind, Literal["document"])
assert_type(exact.definitions[0].name.value, Literal["Q"])
assert_type(
 exact.definitions[0].selection_set.selections[0].name.value, Literal["missing"]
)
assert exact.loc is not None
assert exact.loc.start == 0
assert exact.loc.end == len({source!r})
assert exact.loc.source.body == {source!r}
assert exact.loc.source.body[exact.loc.start:exact.loc.end] == {source!r}
operation = exact.definitions[0]
assert operation.loc is not None
assert operation.loc.source.body[operation.loc.start:operation.loc.end] == {source!r}
field = operation.selection_set.selections[0]
assert field.loc is not None
assert field.loc.source.body[field.loc.start:field.loc.end] == "missing(arg: 1)"
assert_type(parse(Source({source!r})), DocumentNode)
assert_type(parse({source!r}, True).loc, None)
assert_type(parse({source!r}, max_tokens=100), DocumentNode)
assert_type(parse({source!r}, experimental_fragment_arguments=True), DocumentNode)
assert_type(parse("scalar Date"), DocumentNode)
without_locations = parse({source!r}, no_location=True)
assert_type(without_locations.loc, None)
assert_type(without_locations.definitions[0].name.value, Literal["Q"])
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
 wrong_location: Literal[False] = without_locations.loc
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
                if "wrong:" in line
                or "wrong_location:" in line
                or "exact.nonexistent" in line
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
