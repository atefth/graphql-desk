import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from graphql import build_schema

from graphql_desk.config import parse_project_config
from graphql_desk.factory_generation import generate_project_factory_artifacts
from graphql_desk.fragment_helpers import mask_fragments
from graphql_desk.fragments import SchemaContext, index_fragments
from graphql_desk.source_graph import (
    FactoryBinding,
    ModuleSnapshot,
    ProjectBindings,
    resolve_project,
)
from graphql_desk.testing import read_result
from graphql_desk.testing_helpers import (
    render_testing_helper_artifacts,
    resolve_testing_helper_calls,
)


class TestingHelperCallTests(unittest.TestCase):
    def test_diagnostic_spans_use_cached_line_offsets_and_unicode_columns(self) -> None:
        source = (
            "# 😀 makes the first source line non-ASCII\n"
            "from graphql_desk.testing import read_result\n"
            "read_result(missing_document, {}, ())\n"
        )
        module = ModuleSnapshot("p", "main", "file:///main.py", 1, source)
        facts = resolve_testing_helper_calls(
            (module,), index_fragments(resolve_project((module,)), schemas={})
        )
        self.assertEqual(len(facts.diagnostics), 1)
        call = "read_result(missing_document, {}, ())"
        start = source.index(call)
        self.assertEqual(
            (facts.diagnostics[0].range.start, facts.diagnostics[0].range.end),
            (start, start + len(call)),
        )

    def test_helpers_preserve_runtime_identity_for_lists_tuples_and_none(self) -> None:
        fragments = [object()]
        values: tuple[object, ...] = (None, [], (), {"nested": [1]})
        for value in values:
            self.assertIs(read_result(object(), value, fragments), value)
            self.assertIs(mask_fragments(fragments, value), value)
        duplicate_registry = (object(), object())
        duplicate_value: object = {"nested": [1]}
        self.assertIs(
            mask_fragments(duplicate_registry, duplicate_value), duplicate_value
        )

    def test_static_tuple_overloads_and_masked_omissions_in_both_checkers(self) -> None:
        source = """from generated.main import graphql
from graphql_desk import mask_fragments
from graphql_desk.testing import read_result
card = graphql("fragment Card on Book { title }")
inner = graphql("fragment Inner on Book { id }")
outer = graphql("fragment Outer on Book { title ...Inner }", (inner,))
query = graphql(
    'query Read { book(id: "1") { ...Outer ...Card } }', (outer, card, inner)
)
masked = mask_fragments((card,), {"title": "Ada"})
masked_twice = mask_fragments((card, card), {"title": "Ada"})
partial = read_result(query, {"book": {"title": "Ada"}}, (outer,))
complete = read_result(query, {"book": {"title": "Ada", "id": "1"}}, (outer, inner))
empty = read_result(query, {"book": {}}, ())
dynamic = read_result(query, {"book": {"title": "Ada"}}, [outer])
"""
        modules = (ModuleSnapshot("p", "main", "file:///main.py", 1, source),)
        context = SchemaContext(
            "main",
            build_schema(
                "type Book { id: ID! title: String } "
                "type Query { book(id: ID!): Book! }"
            ),
            "v1",
        )
        config = parse_project_config(
            '[tool.graphql-desk]\ngenerated_package="generated"\n'
            '[[tool.graphql-desk.schemas]]\nname="main"\n'
            'schema="schema.graphql"\nschema_output="generated/main.pyi"\n',
            config_path=Path("E:/tmp/graphql-desk/pyproject.toml"),
        )
        assert config is not None
        resolved = resolve_project(
            modules,
            bindings=(
                ProjectBindings(
                    "p",
                    ("main",),
                    factories=(FactoryBinding("generated.main", "graphql", "main"),),
                ),
            ),
        )
        index = index_fragments(resolved, schemas={("p", "main"): context})
        self.assertEqual(index.diagnostics, ())
        generated = generate_project_factory_artifacts(
            config, (context,), index, modules=modules, project_id="p"
        )
        self.assertEqual(generated.diagnostics, ())
        helpers = render_testing_helper_artifacts(
            config,
            ((config.schemas[0], context),),
            index,
            modules,
            generated.provenance,
        )
        self.assertEqual(
            [item.code for item in helpers.diagnostics],
            ["testing-registry-not-static"],
        )
        self.assertEqual(len(helpers.files), 5)
        positive = (
            source
            + """
from typing_extensions import assert_type
from collections.abc import Sequence
from graphql_desk.fragment_refs import FragmentRef
from generated._documents.main.d0 import GraphqlDeskIdentity as CardIdentity
assert_type(masked, FragmentRef[CardIdentity])
assert_type(masked_twice, FragmentRef[CardIdentity])
assert_type(mask_fragments((card,), None), None)
assert_type(
    mask_fragments((card,), [{"title": "Ada"}, None]),
    Sequence[FragmentRef[CardIdentity] | None],
)
assert_type(partial["book"]["title"], str | None)
assert_type(complete["book"]["id"], str)
assert_type(dynamic, object)
"""
        )
        negative = (
            source
            + """
partial["book"]["id"] # error
empty["book"]["title"] # error
read_result(query, {"book": {"title": 1}}, (outer,)) # error
mask_fragments((card,), {"title": 1}) # error
"""
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for artifact in generated.files:
                target = root / artifact.relative_path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(artifact.text, encoding="utf8")
            for filename, text in helpers.files:
                target = root / filename
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(text, encoding="utf8")
            for module in modules:
                target = root / f"{module.module}.py"
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(module.source, encoding="utf8")
            for folder in ("graphql_desk-stubs", "mypy-stubs/graphql_desk"):
                root_stub = root / folder / "__init__.pyi"
                root_stub.parent.mkdir(parents=True, exist_ok=True)
                root_stub.write_text(
                    "from typing import overload as _graphql_desk_overload\n"
                    "from collections.abc import Sequence as _graphql_desk_Sequence\n"
                    + helpers.mask_fragments_stub
                    + "@_graphql_desk_overload\n"
                    + "def mask_fragments(fragments: _graphql_desk_Sequence[object], "
                    "data: object) -> object: ...\n",
                    encoding="utf8",
                )
                testing_stub = root_stub.parent / "testing.pyi"
                testing_stub.write_text(
                    "from typing import TypeVar\n"
                    "from collections.abc import Mapping, Sequence\n"
                    "from graphql_desk.fragment_refs import FixtureDocument\n"
                    "_R = TypeVar('_R')\n_V = TypeVar('_V')\n"
                    "_I = TypeVar('_I', bound=Mapping[str, object])\n"
                    "def unsafe_read_result(document: FixtureDocument[_R, _V, _I], "
                    "data: _I) -> _R: ...\n"
                    "from typing import overload as _graphql_desk_overload\n"
                    "from collections.abc import Sequence as _graphql_desk_Sequence\n"
                    + helpers.read_result_stub
                    + "@_graphql_desk_overload\n"
                    + "def read_result(document: object, data: object, "
                    "fragments: _graphql_desk_Sequence[object]) -> object: ...\n",
                    encoding="utf8",
                )
            (root / "mypy-stubs/graphql_desk/py.typed").write_text(
                "partial\n", encoding="utf8"
            )
            for number, fixture in enumerate((positive, negative)):
                path = root / f"check_{number}.py"
                path.write_text(fixture, encoding="utf8")
                expected = {
                    line
                    for line, text in enumerate(fixture.splitlines(), 1)
                    if "# error" in text
                }
                mypy = subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "mypy",
                        "--strict",
                        "--no-incremental",
                        "--python-version",
                        "3.10",
                        str(path),
                        *[str(root / filename) for filename, _ in helpers.files],
                    ],
                    cwd=root,
                    env={**os.environ, "MYPYPATH": str(root / "mypy-stubs")},
                    capture_output=True,
                    text=True,
                    encoding="utf8",
                )
                self.assertEqual(
                    mypy.returncode, bool(expected), mypy.stdout + mypy.stderr
                )
                (root / "pyrightconfig.json").write_text(
                    json.dumps(
                        {
                            "include": [path.name],
                            "stubPath": "graphql_desk-stubs",
                            "pythonVersion": "3.10",
                            "typeCheckingMode": "strict",
                            "reportMissingModuleSource": False,
                        }
                    ),
                    encoding="utf8",
                )
                pyright = subprocess.run(
                    [sys.executable, "-m", "pyright", str(path)],
                    cwd=root,
                    capture_output=True,
                    text=True,
                    encoding="utf8",
                )
                self.assertEqual(
                    pyright.returncode, bool(expected), pyright.stdout + pyright.stderr
                )

    def test_resolves_tuple_registry_aliases_and_rejects_list_precision(self) -> None:
        modules = (
            ModuleSnapshot(
                "p",
                "card",
                "file:///card.py",
                1,
                "from graphql_desk import gql\n"
                'card = gql("fragment Card on User { name }")\n',
            ),
            ModuleSnapshot(
                "p",
                "query",
                "file:///query.py",
                1,
                "from graphql_desk import gql\n"
                "from graphql_desk.testing import read_result as read\n"
                "from graphql_desk import mask_fragments\n"
                "from card import card\n"
                "REGISTRY = (card,)\n"
                'query = gql("query Read { user { id ...Card } }", [card])\n'
                'masked = mask_fragments(REGISTRY, {"name": "Ada"})\n'
                'result = read(query, {"user": {"id": "1"}}, REGISTRY)\n'
                "read(query, {}, [card])\n",
            ),
        )
        index = index_fragments(
            resolve_project(modules),
            schemas={
                ("p", None): SchemaContext(
                    "main",
                    build_schema(
                        "type User { id: ID! name: String! } type Query { user: User! }"
                    ),
                    "v1",
                )
            },
        )
        facts = resolve_testing_helper_calls(modules, index)
        self.assertEqual(
            [
                (item.helper, item.document.call.binding if item.document else None)
                for item in facts.calls
            ],
            [("mask_fragments", None), ("read_result", "query")],
        )
        self.assertEqual(len(facts.diagnostics), 1)
        self.assertEqual(facts.diagnostics[0].code, "testing-registry-not-static")
        primary = facts.calls[1].fragments[0].primary_fragment
        assert primary is not None
        self.assertEqual(primary.name, "Card")

    def test_rejects_rebound_helpers_and_function_shadowed_documents_or_registries(
        self,
    ) -> None:
        card = ModuleSnapshot(
            "p",
            "card",
            "file:///card.py",
            1,
            "from graphql_desk import gql\n"
            'card = gql("fragment Card on User { name }")\n',
        )
        query = ModuleSnapshot(
            "p",
            "query",
            "file:///query.py",
            1,
            "from graphql_desk import gql\nfrom card import card\n"
            'query = gql("query Read { user { id ...Card } }", (card,))\n'
            "REGISTRY = (card,)\n",
        )
        contexts: dict[tuple[str, str | None], SchemaContext] = {
            ("p", None): SchemaContext(
                "main",
                build_schema(
                    "type User { id: ID! name: String! } type Query { user: User! }"
                ),
                "v1",
            )
        }
        cases = (
            (
                "module helper rebind",
                "from graphql_desk.testing import read_result\n"
                "from query import query, REGISTRY\n"
                "def custom(*args): return None\n"
                "read_result = custom\n"
                "read_result(query, {}, REGISTRY)\n",
                (),
            ),
            (
                "function helper parameter",
                "from graphql_desk.testing import read_result\n"
                "from query import query, REGISTRY\n"
                "def run(read_result): return read_result(query, {}, REGISTRY)\n",
                (),
            ),
            (
                "function document parameter",
                "from graphql_desk.testing import read_result\n"
                "from query import query, REGISTRY\n"
                "def run(query): return read_result(query, {}, REGISTRY)\n",
                ("testing-document-not-static",),
            ),
            (
                "function registry parameter",
                "from graphql_desk.testing import read_result\n"
                "from query import query, REGISTRY\n"
                "def run(REGISTRY): return read_result(query, {}, REGISTRY)\n",
                ("testing-registry-not-static",),
            ),
            (
                "function document assignment",
                "from graphql_desk.testing import read_result\n"
                "from query import query, REGISTRY\n"
                "def run():\n    query = object()\n"
                "    return read_result(query, {}, REGISTRY)\n",
                ("testing-document-not-static",),
            ),
            (
                "function registry assignment",
                "from graphql_desk.testing import read_result\n"
                "from query import query, REGISTRY\n"
                "def run():\n    REGISTRY = []\n"
                "    return read_result(query, {}, REGISTRY)\n",
                ("testing-registry-not-static",),
            ),
            (
                "exception document capture",
                "from graphql_desk.testing import read_result\n"
                "from query import query, REGISTRY\n"
                "def run():\n    try: raise ValueError()\n"
                "    except ValueError as query:\n"
                "        return read_result(query, {}, REGISTRY)\n",
                ("testing-document-not-static",),
            ),
            (
                "match document capture",
                "from graphql_desk.testing import read_result\n"
                "from query import query, REGISTRY\n"
                "def run(value):\n    match value:\n"
                "        case query:\n"
                "            return read_result(query, {}, REGISTRY)\n",
                ("testing-document-not-static",),
            ),
            (
                "mapping rest helper capture",
                "from graphql_desk.testing import read_result\n"
                "from query import query, REGISTRY\n"
                "def run(value):\n    match value:\n"
                "        case {**read_result}:\n"
                "            return read_result(query, {}, REGISTRY)\n",
                (),
            ),
        )
        for number, (label, source, diagnostics) in enumerate(cases):
            with self.subTest(label=label):
                caller = ModuleSnapshot(
                    "p", f"caller_{number}", f"file:///caller_{number}.py", 1, source
                )
                modules = (card, query, caller)
                index = index_fragments(resolve_project(modules), schemas=contexts)
                facts = resolve_testing_helper_calls(modules, index)
                self.assertEqual(facts.calls, (), label)
                self.assertEqual(
                    tuple(item.code for item in facts.diagnostics), diagnostics, label
                )

    def test_rejects_fragment_outside_operation_closure(self) -> None:
        modules = (
            ModuleSnapshot(
                "p",
                "card",
                "file:///card.py",
                1,
                "from graphql_desk import gql\n"
                'card = gql("fragment Card on User { name }")\n',
            ),
            ModuleSnapshot(
                "p",
                "other",
                "file:///other.py",
                1,
                "from graphql_desk import gql\n"
                'other = gql("fragment Other on User { id }")\n',
            ),
            ModuleSnapshot(
                "p",
                "query",
                "file:///query.py",
                1,
                "from graphql_desk import gql\n"
                "from graphql_desk.testing import read_result\n"
                "from card import card\nfrom other import other\n"
                'query = gql("query Read { user { id ...Card } }", [card])\n'
                "read_result(query, {}, (other,))\n",
            ),
        )
        index = index_fragments(
            resolve_project(modules),
            schemas={
                ("p", None): SchemaContext(
                    "main",
                    build_schema(
                        "type User { id: ID! name: String! } type Query { user: User! }"
                    ),
                    "v1",
                )
            },
        )
        facts = resolve_testing_helper_calls(modules, index)
        self.assertEqual(facts.calls, ())
        self.assertEqual(
            [item.code for item in facts.diagnostics],
            ["testing-fragment-not-in-document"],
        )

    def test_resolves_imported_fixed_tuple_registries(self) -> None:
        modules = (
            ModuleSnapshot(
                "p",
                "card",
                "file:///card.py",
                1,
                "from graphql_desk import gql\n"
                'card = gql("fragment Card on User { name }")\n',
            ),
            ModuleSnapshot(
                "p",
                "query",
                "file:///query.py",
                1,
                "from graphql_desk import gql\nfrom card import card\n"
                'query = gql("query Read { user { id ...Card } }", (card,))\n',
            ),
            ModuleSnapshot(
                "p",
                "registries",
                "file:///registries.py",
                1,
                "from card import card\nFRAGMENTS = (card,)\n",
            ),
            ModuleSnapshot(
                "p",
                "caller",
                "file:///caller.py",
                1,
                "from graphql_desk.testing import read_result\n"
                "from query import query\nfrom registries import FRAGMENTS\n"
                "result = read_result(query, {}, FRAGMENTS)\n",
            ),
        )
        index = index_fragments(
            resolve_project(modules),
            schemas={
                ("p", None): SchemaContext(
                    "main",
                    build_schema(
                        "type User { id: ID! name: String! } type Query { user: User! }"
                    ),
                    "v1",
                )
            },
        )
        facts = resolve_testing_helper_calls(modules, index)
        self.assertEqual(facts.diagnostics, ())
        self.assertEqual(len(facts.calls), 1)
        primary = facts.calls[0].fragments[0].primary_fragment
        assert primary is not None
        self.assertEqual(primary.name, "Card")

    def test_rejects_same_named_fragment_from_another_schema(self) -> None:
        modules = (
            ModuleSnapshot(
                "p",
                "query",
                "file:///query.py",
                1,
                "from generated.main import graphql\n"
                'query = graphql("query Read { user { ...Card } } '
                'fragment Card on User { name }")\n',
            ),
            ModuleSnapshot(
                "p",
                "foreign",
                "file:///foreign.py",
                1,
                "from generated.beta import graphql\n"
                'foreign = graphql("fragment Card on User { id }")\n',
            ),
            ModuleSnapshot(
                "p",
                "caller",
                "file:///caller.py",
                1,
                "from graphql_desk.testing import read_result\n"
                "from query import query\nfrom foreign import foreign\n"
                "result = read_result(query, {}, (foreign,))\n",
            ),
        )
        resolved = resolve_project(
            modules,
            bindings=(
                ProjectBindings(
                    "p",
                    ("main", "beta"),
                    factories=(
                        FactoryBinding("generated.main", "graphql", "main"),
                        FactoryBinding("generated.beta", "graphql", "beta"),
                    ),
                ),
            ),
        )
        schemas: dict[tuple[str, str | None], SchemaContext] = {
            ("p", name): SchemaContext(
                name,
                build_schema(
                    "type User { id: ID! name: String! } type Query { user: User! }"
                ),
                f"{name}-v1",
            )
            for name in ("main", "beta")
        }
        index = index_fragments(resolved, schemas=schemas)
        facts = resolve_testing_helper_calls(modules, index)
        self.assertEqual(facts.calls, ())
        self.assertEqual(
            [item.code for item in facts.diagnostics],
            ["testing-fragment-not-in-document"],
        )


if __name__ == "__main__":
    unittest.main()
