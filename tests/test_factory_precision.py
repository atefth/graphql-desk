"""Factory precision warnings describe actual checker limitations and operands."""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from graphql_desk.source import Span
from graphql_desk.source_graph import ModuleSnapshot
from tests import test_factory_generation as factory_tests


class FactoryPrecisionTests(unittest.TestCase):
    def test_qualified_annotated_sources_infer_in_both_checkers(self) -> None:
        source = """import typing as t
import typing_extensions as te
from generated.main import graphql
from typing_extensions import assert_type
A: t.Final = 'query A{greeting}'
B: te.Final = 'query B{greeting}'
C: t.Literal['query C{greeting}'] = 'query C{greeting}'
D: te.Literal['query D{greeting}'] = 'query D{greeting}'
AA: t.Final = A
CC: te.Final = C
a=graphql(A)
b=graphql(B)
c=graphql(C)
d=graphql(D)
aa=graphql(AA)
cc=graphql(CC)
def local()->int:
 t=1
 return t
if t.TYPE_CHECKING:
 assert_type(a.operation_name,t.Literal['A'])
 assert_type(b.operation_name,t.Literal['B'])
 assert_type(c.operation_name,t.Literal['C'])
 assert_type(d.operation_name,t.Literal['D'])
 assert_type(aa.operation_name,t.Literal['A'])
 assert_type(cc.operation_name,t.Literal['C'])
assert a.operation_name=='A' and b.operation_name=='B'
assert c.operation_name=='C' and d.operation_name=='D'
"""
        modules = (ModuleSnapshot("p", "main", "file:///main.py", 1, source),)
        helper = factory_tests.FactoryGenerationTests()
        artifacts = helper.generate(modules)
        self.assertEqual(artifacts.diagnostics, ())
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            helper.write(root, artifacts, modules)
            helper.check(root)
            executed = subprocess.run(
                [sys.executable, "main.py"],
                cwd=root,
                capture_output=True,
                text=True,
                encoding="utf8",
            )
            self.assertEqual(executed.returncode, 0, executed.stdout + executed.stderr)

    def test_warnings_locate_operands_with_unicode_and_physical_newlines(self) -> None:
        source = (
            "from generated.main import graphql\r\n"
            "SOURCE='query Q{greeting}'\r\n"
            'note="café😀\u2028x"; q=graphql(SOURCE)\r\n'
            'concat=graphql("query R"+"{greeting}")\r\n'
            'fields=graphql("fragment Fields on Book{title}")\r\n'
            'query=graphql("query S{book(id:\\"1\\"){...Fields}}",[fields])\r\n'
        )
        modules = (ModuleSnapshot("p", "main", "file:///main.py", 1, source),)
        artifacts = factory_tests.FactoryGenerationTests().generate(modules)
        self.assertTrue(artifacts.files, artifacts.diagnostics)
        warnings = artifacts.diagnostics
        self.assertEqual([item.code for item in warnings], ["factory-precision"] * 3)
        operands = ("SOURCE", '"query R"+"{greeting}"', "[fields]")
        self.assertEqual(
            [source[item.range.start : item.range.end] for item in warnings],
            list(operands),
        )
        start = source.index("SOURCE)")
        self.assertEqual(warnings[0].range, Span(start, start + len("SOURCE")))
        self.assertTrue(all(item.module.uri == "file:///main.py" for item in warnings))

    def test_rebound_fake_and_mutated_annotation_imports_keep_warnings(self) -> None:
        for imports, declaration, tail, extra in (
            ("import typing as t", "t.Final", "t=object()", ()),
            ("import typing as t", "t.Final", "del t", ()),
            ("import typing as t", "t.Final", "import unknown as t", ()),
            ("from typing import Final as F", "F", "F=object()", ()),
            ("from unknown import Final as F", "F", "", ()),
            ("import typing as t", "t.Final", "if False:\n import unknown as t", ()),
            (
                "import typing as t",
                "t.Final",
                "try: pass\nexcept Exception as t: pass",
                (),
            ),
            ("from .typing import Final as F", "F", "", ()),
            (
                "import typing as t",
                "t.Final",
                "",
                (
                    ModuleSnapshot(
                        "p", "typing", "file:///typing.py", 1, "class Final: pass"
                    ),
                ),
            ),
            (
                "import typing_extensions as t",
                "t.Final",
                "",
                (
                    ModuleSnapshot(
                        "p",
                        "typing_extensions",
                        "file:///typing_extensions.py",
                        1,
                        "class Final: pass",
                    ),
                ),
            ),
            (
                "import typing as t",
                "t.Final",
                "",
                (
                    ModuleSnapshot(
                        "p",
                        "mutator",
                        "file:///mutator.py",
                        1,
                        "import typing\ntyping.Final=42",
                    ),
                ),
            ),
            (
                "import typing_extensions as t",
                "t.Literal['query Q{greeting}']",
                "",
                (
                    ModuleSnapshot(
                        "p",
                        "mutator",
                        "file:///mutator.py",
                        1,
                        "import typing_extensions\ntyping_extensions.Literal=42",
                    ),
                ),
            ),
        ):
            with self.subTest(imports=imports, tail=tail, extra=extra):
                source = (
                    "from generated.main import graphql\n"
                    + imports
                    + "\n"
                    + f"SOURCE:{declaration}='query Q{{greeting}}'\n"
                    + tail
                    + "\nq=graphql(SOURCE)\n"
                )
                modules = (
                    ModuleSnapshot("p", "main", "file:///main.py", 1, source),
                    *extra,
                )
                artifacts = factory_tests.FactoryGenerationTests().generate(modules)
                self.assertTrue(artifacts.files, artifacts.diagnostics)
                self.assertEqual(
                    [item.code for item in artifacts.diagnostics], ["factory-precision"]
                )
                warning = artifacts.diagnostics[0]
                self.assertEqual(
                    source[warning.range.start : warning.range.end], "SOURCE"
                )

    def test_widened_annotated_sources_keep_warnings(self) -> None:
        source = """import typing as t
from generated.main import graphql
A:t.Final[str]='query A{greeting}'
B:t.Literal['query B{greeting}','other']='query B{greeting}'
a=graphql(A)
b=graphql(B)
"""
        artifacts = factory_tests.FactoryGenerationTests().generate(
            (ModuleSnapshot("p", "main", "file:///main.py", 1, source),)
        )
        self.assertTrue(artifacts.files, artifacts.diagnostics)
        self.assertEqual(
            [item.code for item in artifacts.diagnostics], ["factory-precision"] * 2
        )

    def test_rebound_widened_and_dynamic_alias_chains_keep_call_diagnostics(
        self,
    ) -> None:
        for declarations, expected_code, expected_text in (
            (
                "ROOT: Final = 'query Q{greeting}'\n"
                "SOURCE: Final = ROOT\nSOURCE = 'query Q{greeting}'\n",
                "source-not-static",
                "graphql(SOURCE)",
            ),
            (
                "ROOT: Final = 'query Q{greeting}'\nSOURCE: Final[str] = ROOT\n",
                "factory-precision",
                "SOURCE",
            ),
            (
                "SOURCE: Final = runtime_source()\n",
                "source-not-static",
                "graphql(SOURCE)",
            ),
        ):
            with self.subTest(declarations=declarations):
                source = (
                    "from typing import Final\n"
                    "from generated.main import graphql\n"
                    + declarations
                    + "query = graphql(SOURCE)\n"
                )
                artifacts = factory_tests.FactoryGenerationTests().generate(
                    (ModuleSnapshot("p", "main", "file:///main.py", 1, source),)
                )
                self.assertEqual(
                    [item.code for item in artifacts.diagnostics], [expected_code]
                )
                warning = artifacts.diagnostics[0]
                self.assertEqual(
                    source[warning.range.start : warning.range.end], expected_text
                )

    def test_static_operation_alias_keeps_semantics_and_warns_at_operand(self) -> None:
        source = (
            "from generated.main import graphql\n"
            "SELECTED = 'Second'\n"
            'doc=graphql("query First{greeting} query Second{greeting}", '
            "operation=SELECTED)\n"
        )
        modules = (ModuleSnapshot("p", "main", "file:///main.py", 1, source),)
        artifacts = factory_tests.FactoryGenerationTests().generate(modules)

        self.assertTrue(artifacts.files, artifacts.diagnostics)
        warnings = [
            item for item in artifacts.diagnostics if item.code == "factory-precision"
        ]
        self.assertEqual(len(warnings), 1)
        warning = warnings[0]
        self.assertEqual(source[warning.range.start : warning.range.end], "SELECTED")
        self.assertIn("operation selection", warning.message.lower())
        self.assertTrue(
            any(
                "Second" in item.text and item.relative_path.endswith(".pyi")
                for item in artifacts.files
            )
        )
