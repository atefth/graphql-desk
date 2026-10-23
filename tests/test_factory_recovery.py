"""Retain proven independent declarations without erasing failed-owner evidence."""

import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from graphql_desk.factory_generation import (
    FactoryArtifacts,
    generate_factory_artifacts,
    generate_project_factory_artifacts,
)
from graphql_desk.fragments import (
    FragmentIndex,
    RejectedDocument,
    index_fragments,
)
from graphql_desk.project import project_bindings
from graphql_desk.source import Span
from graphql_desk.source_graph import (
    FactoryBinding,
    GraphDiagnostic,
    ModuleSnapshot,
    ProjectBindings,
    RejectedCall,
    resolve_project,
)
from graphql_desk.source_maps import build_source_map
from tests import test_factory_generation as factory_tests


class FactoryRecoveryTests(unittest.TestCase):
    def fixture(self) -> tuple[tuple[ModuleSnapshot, ...], FragmentIndex]:
        main = """from generated.main import graphql
from generated.sources.main import ValidResult,ValidVariables
from typing import TYPE_CHECKING,Literal,TypeVar
from typing_extensions import assert_type
from graphql import DocumentNode
from graphql_desk import TypedDocument
R=TypeVar('R')
V=TypeVar('V')
def execute(document:TypedDocument[R,V],variables:V)->R: raise RuntimeError

def runtime_source()->str:
 return 'query Dynamic{greeting}'
dynamic=graphql(runtime_source())
valid=graphql('query Good{greeting}')
if TYPE_CHECKING:
 assert_type(valid.operation_name,Literal['Good'])
 assert_type(execute(valid,ValidVariables()),ValidResult)
 assert_type(dynamic.ast,DocumentNode)
assert valid.operation_name=='Good' and dynamic.operation_name=='Dynamic'
"""
        broken = """from generated.main import graphql
from typing_extensions import assert_type
from graphql import DocumentNode
bad=graphql('fragment Bad on Book {')
dependent=graphql('query Dep{book(id:"1"){...Bad}}',(bad,))
assert_type(bad.ast,DocumentNode)
assert_type(dependent.ast,DocumentNode)
"""
        modules = (
            ModuleSnapshot("p", "main", "file:///main.py", 1, main),
            ModuleSnapshot("p", "broken", "file:///broken.py", 2, broken),
        )
        sources = resolve_project(
            modules,
            bindings=(
                ProjectBindings(
                    "p",
                    ("main",),
                    factories=(FactoryBinding("generated.main", "graphql", "main"),),
                ),
            ),
        )
        index = index_fragments(
            sources,
            schemas={("p", "main"): factory_tests.FactoryGenerationTests().context()},
        )
        return modules, index

    def generate(
        self, modules: tuple[ModuleSnapshot, ...], index: FragmentIndex
    ) -> FactoryArtifacts:
        return generate_factory_artifacts(
            factory_tests.FactoryGenerationTests().context(),
            index,
            modules=modules,
            project_id="p",
            generated_package="generated",
        )

    def test_actual_sources_keep_valid_types_and_opaque_failed_calls(self) -> None:
        modules, index = self.fixture()
        self.assertEqual([item.call.binding for item in index.documents], ["valid"])
        self.assertEqual([item.binding for item in index.rejected_calls], ["dynamic"])
        self.assertEqual(
            [item.call.binding for item in index.unavailable], ["bad", "dependent"]
        )
        original = index
        artifacts = self.generate(modules, index)
        self.assertTrue(artifacts.files, artifacts.diagnostics)
        self.assertEqual(artifacts.diagnostics, index.diagnostics)
        self.assertIs(index, original)
        self.assertFalse(
            any(
                item.relative_path.endswith("sources/broken.py")
                for item in artifacts.files
            )
        )
        main_aliases = next(
            item.text
            for item in artifacts.files
            if item.relative_path == "generated/sources/main.pyi"
        )
        self.assertIn("ValidResult", main_aliases)
        self.assertNotIn("Dynamic", main_aliases)
        helper = factory_tests.FactoryGenerationTests()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            helper.write(root, artifacts, modules)
            helper.check(root)
            negative = """from main import execute,dynamic
from broken import bad,dependent
execute(dynamic,{}) # error
execute(bad,{}) # error
execute(dependent,{}) # error
"""
            (root / "negative.py").write_text(negative, encoding="utf8")
            helper.check(root, {"negative.py": {3, 4, 5}})
            executed = subprocess.run(
                [sys.executable, "main.py"],
                cwd=root,
                capture_output=True,
                text=True,
                encoding="utf8",
            )
            self.assertEqual(executed.returncode, 0, executed.stdout + executed.stderr)

    def test_known_bad_source_selector_and_fragment_keep_valid_independent_calls(
        self,
    ) -> None:
        helper = factory_tests.FactoryGenerationTests()
        cases = (
            (
                "invalid source",
                "bad=graphql('query Bad{miss\\u0069ng}')\n",
                "graphql-validation",
                r"miss\u0069ng",
            ),
            (
                "unknown selector",
                "bad=graphql('query Bad{greeting}',operation='Wrong')\n",
                "invalid-operation-selection",
                "'Wrong'",
            ),
            (
                "operation used as fragment",
                "query_source=graphql('query Other{greeting}')\n"
                "bad=graphql('query Bad{greeting}',[query_source])\n",
                "operation-as-fragment",
                "query_source",
            ),
        )
        for label, bad_source, expected_code, expected_text in cases:
            with self.subTest(label=label):
                source = (
                    "from generated.main import graphql\n"
                    "from graphql import DocumentNode\n"
                    "from typing_extensions import assert_type\n"
                    "good=graphql('query Good{greeting}')\n"
                    + bad_source
                    + "assert_type(bad.ast, DocumentNode)\n"
                    + "bad['greeting'] # error\n"
                )
                modules = (ModuleSnapshot("p", "main", "file:///main.py", 1, source),)
                bindings = (
                    ProjectBindings(
                        "p",
                        ("main",),
                        "main",
                        (FactoryBinding("generated.main", "graphql", "main"),),
                    ),
                )
                sources = resolve_project(modules, bindings=bindings)
                index = index_fragments(
                    sources,
                    schemas={
                        ("p", "main"): helper.context(),
                    },
                )
                artifacts = self.generate(modules, index)
                self.assertTrue(artifacts.files, artifacts.diagnostics)
                diagnostics = [
                    item for item in artifacts.diagnostics if item.code == expected_code
                ]
                self.assertTrue(diagnostics)
                diagnostic = None
                if expected_code == "graphql-validation":
                    spans = sorted(
                        (item.range for item in diagnostics),
                        key=lambda span: (span.start, span.end),
                    )
                    self.assertEqual(
                        "".join(source[span.start : span.end] for span in spans),
                        expected_text,
                    )
                else:
                    self.assertEqual(len(diagnostics), 1)
                    diagnostic = diagnostics[0]
                    self.assertEqual(
                        source[diagnostic.range.start : diagnostic.range.end],
                        expected_text,
                    )
                companion = next(
                    item.text
                    for item in artifacts.files
                    if item.relative_path == "generated/sources/main.pyi"
                )
                self.assertIn("GoodResult", companion)
                if expected_code == "graphql-validation":
                    self.assertIn("BadResult", companion)
                    self.assertIn("BadUnavailable", companion)
                else:
                    self.assertNotIn("BadResult", companion)

                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    helper.write(root, artifacts, modules)
                    bad_line = next(
                        number
                        for number, line in enumerate(source.splitlines(), 1)
                        if "# error" in line
                    )
                    helper.check(root, {"main.py": {bad_line}})

                if expected_code == "invalid-operation-selection":
                    assert diagnostic is not None
                    rejected = index.unavailable[0]
                    self.assertEqual(rejected.call.operation, "Wrong")
                    self.assertEqual(rejected.call.operation_range, diagnostic.range)
                    self.assertTrue(
                        build_source_map(rejected.call, modules).to_host(
                            Span(0, len(rejected.call.source))
                        )
                    )

                if index.unavailable:
                    assert diagnostic is not None
                    bad_call = index.unavailable[0].call
                    bad_diagnostic = replace(diagnostic, range=bad_call.range)
                    corrupted = replace(
                        index,
                        diagnostics=(bad_diagnostic,),
                        unavailable=(
                            replace(
                                index.unavailable[0],
                                diagnostics=(bad_diagnostic,),
                            ),
                        ),
                    )
                    self.assertEqual(self.generate(modules, corrupted).files, ())
                    if expected_code == "invalid-operation-selection":
                        forged_range = Span(
                            bad_call.range.start, bad_call.range.start + 1
                        )
                        forged_call = replace(bad_call, operation_range=forged_range)
                        forged_diagnostic = replace(diagnostic, range=forged_range)
                        forged = replace(
                            index,
                            diagnostics=(forged_diagnostic,),
                            unavailable=(
                                replace(
                                    index.unavailable[0],
                                    call=forged_call,
                                    diagnostics=(forged_diagnostic,),
                                ),
                            ),
                        )
                        self.assertEqual(self.generate(modules, forged).files, ())

    def test_project_recovery_preserves_both_schemas_and_diagnostic_order(self) -> None:
        helper = factory_tests.FactoryGenerationTests()
        config, contexts, modules, _ = helper.multi_schema()
        modules = (
            replace(
                modules[0],
                source=modules[0].source
                + "\ndef runtime_source()->str: return 'query Dynamic{item{id}}'\n"
                "dynamic=alpha(runtime_source())\n",
            ),
            *modules[1:],
            ModuleSnapshot(
                "p",
                "broken",
                "file:///broken.py",
                1,
                "from generated.alpha import graphql\n"
                "bad=graphql('fragment Bad on Item {')\n",
            ),
        )
        sources = resolve_project(
            modules, bindings=(project_bindings(config, project_id="p"),)
        )
        index = index_fragments(
            sources, schemas={("p", item.schema_name): item for item in contexts}
        )
        artifacts = generate_project_factory_artifacts(
            config, contexts, index, modules=modules, project_id="p"
        )
        self.assertTrue(artifacts.files, artifacts.diagnostics)
        self.assertEqual(artifacts.diagnostics, index.diagnostics)
        permuted = generate_project_factory_artifacts(
            config,
            tuple(reversed(contexts)),
            replace(index, documents=tuple(reversed(index.documents))),
            modules=tuple(reversed(modules)),
            project_id="p",
        )
        self.assertEqual(permuted, artifacts)
        self.assertTrue(
            any(item.relative_path == "generated/alpha.pyi" for item in artifacts.files)
        )
        self.assertTrue(
            any(item.relative_path == "generated/beta.pyi" for item in artifacts.files)
        )
        self.assertFalse(
            any(
                item.relative_path.endswith("sources/broken.pyi")
                for item in artifacts.files
            )
        )
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

        captured = (
            replace(
                modules[0],
                source=modules[0].source
                + "\nfoo_bar=alpha('query Collision{item{id}}')\n"
                "fooBar=beta(runtime_source())\n",
            ),
            *modules[1:],
        )
        sources = resolve_project(
            captured, bindings=(project_bindings(config, project_id="p"),)
        )
        invalid = index_fragments(
            sources, schemas={("p", item.schema_name): item for item in contexts}
        )
        result = generate_project_factory_artifacts(
            config, contexts, invalid, modules=captured, project_id="p"
        )
        self.assertEqual(result.files, ())
        self.assertEqual(result.diagnostics, invalid.diagnostics)

    def test_unattributed_structural_stale_and_conflicting_owners_stay_fatal(
        self,
    ) -> None:
        modules, index = self.fixture()
        source = index.rejected_calls[0]
        owner = index.unavailable[0]
        unknown = replace(index.diagnostics[0], code="future-error")
        variants = [
            replace(index, documents=()),
            replace(index, rejected_calls=()),
            replace(index, rejected_calls=(replace(source, source_failure=False),)),
            replace(index, diagnostics=(*index.diagnostics, unknown)),
            replace(
                index,
                rejected_calls=(
                    replace(source, module=replace(source.module, version=99)),
                ),
            ),
            replace(index, rejected_calls=(replace(source, binding="wrong"),)),
            replace(index, rejected_calls=(replace(source, range=Span(0, 1)),)),
            replace(
                index,
                unavailable=(
                    replace(owner, call=replace(owner.call, source="changed")),
                    *index.unavailable[1:],
                ),
            ),
            replace(
                index,
                unavailable=(
                    replace(owner, call=replace(owner.call, schema_name="foreign")),
                    *index.unavailable[1:],
                ),
            ),
            replace(index, rejected_calls=(*index.rejected_calls, source)),
            replace(
                index,
                rejected_calls=(
                    *index.rejected_calls,
                    RejectedCall(
                        index.documents[0].call.module,
                        index.documents[0].call.range,
                        "valid",
                        source.diagnostics,
                    ),
                ),
            ),
        ]
        for code in (
            "source-too-complex",
            "invalid-python",
            "missing-schema-context",
            "schema-context-mismatch",
            "mixed-source-versions",
            "cross-schema-fragment",
            "cross-project-fragment",
            "missing-fragment-target",
            "fragment-reference-cycle",
            "conflicting-document",
        ):
            diagnostic = replace(source.diagnostics[0], code=code)
            variants.append(
                replace(
                    index,
                    diagnostics=(diagnostic, *index.diagnostics[1:]),
                    rejected_calls=(replace(source, diagnostics=(diagnostic,)),),
                )
            )
        dependent = index.unavailable[1]
        reference = dependent.call.fragments[0]
        diagnostic = dependent.diagnostics[0]
        escaped = Span(9999, 10000)
        variants.append(
            replace(
                index,
                diagnostics=tuple(
                    replace(item, range=escaped) if item == diagnostic else item
                    for item in index.diagnostics
                ),
                unavailable=(
                    owner,
                    replace(
                        dependent,
                        call=replace(
                            dependent.call,
                            fragments=(replace(reference, range=escaped),),
                        ),
                        diagnostics=tuple(
                            replace(item, range=escaped) if item == diagnostic else item
                            for item in dependent.diagnostics
                        ),
                    ),
                ),
            )
        )
        for invalid in variants:
            with self.subTest(index=invalid):
                result = self.generate(modules, invalid)
                self.assertEqual(result.files, ())
                self.assertEqual(result.diagnostics, invalid.diagnostics)
        for changed in (
            (replace(modules[0], version=99), modules[1]),
            (
                modules[0],
                replace(
                    modules[1],
                    source=modules[1].source.replace(
                        "fragment Bad on Book {", "fragment Sad on Book {"
                    ),
                ),
            ),
            (modules[0], replace(modules[1], uri="file:///different.py")),
            (*modules, modules[0]),
        ):
            with self.subTest(modules=changed):
                result = self.generate(changed, index)
                self.assertEqual(result.files, ())
                self.assertEqual(result.diagnostics, index.diagnostics)
        with self.assertRaises(ValueError):
            self.generate(modules, replace(index, diagnostics=()))

        for suffix in (
            "\nmissing=graphql(unknown)\n",
            "\ndependent_source=graphql('query Q{...Fields}',(dynamic,))\n",
        ):
            captured: tuple[ModuleSnapshot, ...] = (
                replace(modules[0], source=modules[0].source + suffix),
                modules[1],
            )
            sources = resolve_project(
                captured,
                bindings=(
                    ProjectBindings(
                        "p",
                        ("main",),
                        factories=(
                            FactoryBinding("generated.main", "graphql", "main"),
                        ),
                    ),
                ),
            )
            invalid = index_fragments(
                sources,
                schemas={
                    ("p", "main"): factory_tests.FactoryGenerationTests().context()
                },
            )
            self.assertTrue(
                any(not item.source_failure for item in invalid.rejected_calls)
            )
            result = self.generate(captured, invalid)
            self.assertEqual(result.files, ())
            self.assertEqual(result.diagnostics, invalid.diagnostics)

        for rejected_expression in ("conflicting('{greeting}')", "safe(conflicting)"):
            captured = (
                ModuleSnapshot(
                    "p",
                    "generated",
                    "file:///generated/__init__.py",
                    1,
                    "",
                    is_package=True,
                ),
                ModuleSnapshot(
                    "p",
                    "generated.main",
                    "file:///generated/main.py",
                    1,
                    "from graphql_desk import init_graphql\n"
                    "graphql=init_graphql('beta')",
                ),
                ModuleSnapshot(
                    "p",
                    "main",
                    "file:///main.py",
                    1,
                    "from graphql_desk import graphql as safe\n"
                    "from generated.main import graphql as conflicting\n"
                    "good=safe('{greeting}')\n" + f"bad={rejected_expression}\n",
                ),
            )
            sources = resolve_project(
                captured,
                bindings=(
                    ProjectBindings(
                        "p",
                        ("main", "beta"),
                        "main",
                        (FactoryBinding("generated.main", "graphql", "main"),),
                    ),
                ),
            )
            self.assertEqual([item.binding for item in sources.unavailable], ["bad"])
            self.assertFalse(sources.unavailable[0].source_failure)
            context = factory_tests.FactoryGenerationTests().context()
            invalid = index_fragments(
                sources, schemas={("p", None): context, ("p", "main"): context}
            )
            self.assertEqual(len(invalid.documents), 1)
            result = self.generate(captured, invalid)
            self.assertEqual(result.files, ())
            self.assertEqual(result.diagnostics, invalid.diagnostics)

    def test_known_signature_collision_fails_closed_but_distinct_sources_survive(
        self,
    ) -> None:
        modules, index = self.fixture()
        self.assertTrue(self.generate(modules, index).files)
        valid = index.documents[0]
        duplicated = replace(
            modules[0],
            source=modules[0].source
            + "\nother=graphql('query Good{greeting}',operation='Good')\n",
        )
        sources = resolve_project(
            (duplicated, modules[1]),
            bindings=(
                ProjectBindings(
                    "p",
                    ("main",),
                    factories=(FactoryBinding("generated.main", "graphql", "main"),),
                ),
            ),
        )
        actual = index_fragments(
            sources,
            schemas={("p", "main"): factory_tests.FactoryGenerationTests().context()},
        )
        other = next(item for item in actual.documents if item.call.binding == "other")
        diagnostic = GraphDiagnostic(
            other.call.module,
            "invalid-fragments",
            "Rejected captured owner",
            other.call.range,
        )
        collision = replace(
            actual,
            documents=tuple(item for item in actual.documents if item != other),
            diagnostics=(*actual.diagnostics, diagnostic),
            unavailable=(
                *actual.unavailable,
                RejectedDocument(replace(other.call, schema_name=None), (diagnostic,)),
            ),
        )
        self.assertEqual(valid.call.source, other.call.source)
        rejected = self.generate((duplicated, modules[1]), collision)
        self.assertEqual(rejected.files, ())
        self.assertEqual(rejected.diagnostics, collision.diagnostics)

        self.assertIsNone(valid.call.operation)
        self.assertEqual(other.call.operation, "Good")
        default_owner = next(
            item for item in actual.documents if item.call.binding == "valid"
        )
        default_error = replace(diagnostic, range=default_owner.call.range)
        reverse = replace(
            actual,
            documents=tuple(item for item in actual.documents if item != default_owner),
            diagnostics=(*actual.diagnostics, default_error),
            unavailable=(
                *actual.unavailable,
                RejectedDocument(default_owner.call, (default_error,)),
            ),
        )
        self.assertEqual(self.generate((duplicated, modules[1]), reverse).files, ())

        for first, second in (("q", "q"), ("foo_bar", "fooBar")):
            source = (
                "from generated.main import graphql\n"
                "def runtime_source()->str: return '{greeting}'\n"
                + f"{first}=graphql('query Good{{greeting}}')\n"
                + f"{second}=graphql(runtime_source())\n"
            )
            captured: tuple[ModuleSnapshot, ...] = (
                ModuleSnapshot("p", "main", "file:///main.py", 1, source),
            )
            sources = resolve_project(
                captured,
                bindings=(
                    ProjectBindings(
                        "p",
                        ("main",),
                        factories=(
                            FactoryBinding("generated.main", "graphql", "main"),
                        ),
                    ),
                ),
            )
            invalid = index_fragments(
                sources,
                schemas={
                    ("p", "main"): factory_tests.FactoryGenerationTests().context()
                },
            )
            self.assertTrue(invalid.rejected_calls[0].source_failure)
            with self.subTest(bindings=(first, second)):
                result = self.generate(captured, invalid)
                self.assertEqual(result.files, ())
                self.assertEqual(result.diagnostics, invalid.diagnostics)

        query = 'query WithFields{book(id:"1"){...Fields}}'
        captured = (
            ModuleSnapshot(
                "p",
                "fields",
                "file:///fields.py",
                1,
                "from generated.main import graphql\n"
                "fields=graphql('fragment Fields on Book{title}')\n",
            ),
            ModuleSnapshot(
                "p",
                "broken",
                "file:///broken.py",
                1,
                "from generated.main import graphql\n"
                "fields=graphql('fragment Fields on Book{')\n"
                + f"bad=graphql({query!r},(fields,))\n",
            ),
            ModuleSnapshot(
                "p",
                "main",
                "file:///main.py",
                1,
                "from generated.main import graphql\nfrom fields import fields\n"
                "from typing import TYPE_CHECKING,Literal\n"
                "from typing_extensions import assert_type\n"
                + f"valid=graphql({query!r},(fields,))\n"
                + "if TYPE_CHECKING:\n"
                " assert_type(valid.operation_name,Literal['WithFields'])\n",
            ),
        )
        sources = resolve_project(
            captured,
            bindings=(
                ProjectBindings(
                    "p",
                    ("main",),
                    factories=(FactoryBinding("generated.main", "graphql", "main"),),
                ),
            ),
        )
        independent = index_fragments(
            sources,
            schemas={("p", "main"): factory_tests.FactoryGenerationTests().context()},
        )
        result = self.generate(captured, independent)
        self.assertTrue(result.files, result.diagnostics)
        self.assertEqual(result.diagnostics, independent.diagnostics)
        good = next(
            item for item in independent.documents if item.call.binding == "valid"
        )
        bad = next(
            item for item in independent.unavailable if item.call.binding == "bad"
        )
        self.assertEqual(good.call.source, bad.call.source)
        self.assertNotEqual(good.call.fragments[0].target, bad.call.fragments[0].target)
        helper = factory_tests.FactoryGenerationTests()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            helper.write(root, result, captured)
            helper.check(root)
            executed = subprocess.run(
                [sys.executable, "main.py"],
                cwd=root,
                capture_output=True,
                text=True,
                encoding="utf8",
            )
            self.assertEqual(executed.returncode, 0, executed.stdout + executed.stderr)
