"""Compose actual source-resolver records without inventing fragment references."""

import unittest
from dataclasses import FrozenInstanceError, replace
from unittest.mock import patch

from graphql import build_schema, print_ast

from graphql_desk import graphql
from graphql_desk.fragments import FragmentIndex, SchemaContext, index_fragments
from graphql_desk.source_graph import (
    DocumentIdentity,
    ModuleSnapshot,
    ProjectBindings,
    ProjectSources,
    resolve_project,
)

_SCHEMA = """
interface Node { id: ID! }
type User implements Node { id: ID! name: String friend: User }
type Org implements Node { id: ID! title: String }
type Query { user: User node: Node hello: String }
"""


def module(
    name: str, source: str, version: int = 1, project: str = "p"
) -> ModuleSnapshot:
    return ModuleSnapshot(
        project,
        name,
        f"file:///{project}/{name}.py",
        version,
        "from graphql_desk import graphql\n" + source,
    )


class FragmentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.schema = build_schema(_SCHEMA)
        self.context = SchemaContext("default", self.schema, "schema-generation-1")
        self.schemas: dict[tuple[str, str | None], SchemaContext] = {
            ("p", "default"): self.context
        }

    def resolve(self, *modules: ModuleSnapshot) -> ProjectSources:
        result = resolve_project(
            tuple(modules),
            bindings=(
                ProjectBindings("p", ("default",), "default"),
                ProjectBindings("other", ("default",), "default"),
            ),
        )
        self.assertEqual(result.diagnostics, ())
        return result

    def names(self, result: FragmentIndex, binding: str) -> list[str | None]:
        document = next(
            item for item in result.documents if item.call.binding == binding
        )
        return [fact.name for fact in document.definitions]

    def test_imported_diamond_matches_runtime_order_dedup_and_primary(self) -> None:
        sources = self.resolve(
            module("leaf", 'leaf=graphql("fragment Leaf on User { id }")'),
            module(
                "left",
                "from leaf import leaf\n"
                'left=graphql("fragment Left on User { ...Leaf name }",[leaf])',
            ),
            module(
                "right",
                "from leaf import leaf\n"
                'right=graphql("fragment Right on User { ...Leaf }",[leaf])',
            ),
            module(
                "query",
                "from left import left\n"
                "from right import right\n"
                'q=graphql("query Q { user { ...Left ...Right } }",[left,right,left])',
            ),
        )
        result = index_fragments(sources, schemas=self.schemas)
        self.assertEqual(result.diagnostics, ())
        self.assertEqual(self.names(result, "q"), ["Q", "Left", "Leaf", "Right"])
        leaf = graphql("fragment Leaf on User { id }")
        left = graphql("fragment Left on User { ...Leaf name }", [leaf])
        right = graphql("fragment Right on User { ...Leaf }", [leaf])
        runtime = graphql("query Q { user { ...Left ...Right } }", [left, right, left])
        q = next(item for item in result.documents if item.call.binding == "q")
        self.assertEqual(
            "\n\n".join(fact.printed for fact in q.definitions), print_ast(runtime.ast)
        )
        self.assertIsNone(q.primary_fragment)
        assert q.selected_operation is not None
        self.assertEqual(q.selected_operation.name, "Q")
        indexed_left = next(
            item for item in result.documents if item.call.binding == "left"
        )
        assert indexed_left.primary_fragment is not None
        self.assertEqual(indexed_left.primary_fragment.name, "Left")
        self.assertEqual(indexed_left.primary_fragment.type_condition, "User")
        self.assertEqual(q.schema_identity, ("p", "default"))
        self.assertEqual(q.context_token, "schema-generation-1")
        self.assertIs(
            q.call, next(call for call in sources.calls if call.binding == "q")
        )
        with self.assertRaises(FrozenInstanceError):
            setattr(q, "context_token", "changed")

    def test_inline_and_reexported_documents_use_actual_targets(self) -> None:
        sources = self.resolve(
            module("definitions", 'fragment=graphql("fragment F on User { id }")'),
            module("exports", "from definitions import fragment as exported"),
            module(
                "query",
                "import exports as alias\n"
                'q=graphql("{ user { ...F ...G } }",[alias.exported,'
                'graphql("fragment G on User { name }")])',
            ),
        )
        result = index_fragments(sources, schemas=self.schemas)
        self.assertEqual(result.diagnostics, ())
        self.assertEqual(self.names(result, "q"), [None, "F", "G"])
        q = next(item for item in result.documents if item.call.binding == "q")
        self.assertEqual(
            {dep.module for dep in q.dependencies}, {"definitions", "exports", "query"}
        )

    def test_duplicate_local_and_conflicting_imported_fragments_fail(self) -> None:
        for text in (
            'bad=graphql("fragment F on User { id } fragment F on User { id }")',
            'a=graphql("fragment F on User { id }")\n'
            'b=graphql("fragment F on User { name }")\n'
            'bad=graphql("{ user { ...F } }",[a,b])',
        ):
            sources = self.resolve(module("q", text))
            result = index_fragments(sources, schemas=self.schemas)
            self.assertFalse(
                any(item.call.binding == "bad" for item in result.documents)
            )
            self.assertTrue(result.diagnostics)
        identical = self.resolve(
            module(
                "q",
                'a=graphql("fragment F on User { id }")\n'
                'b=graphql("fragment F on User {\\n'
                " id\\n"
                '}")\n'
                'q=graphql("{ user { ...F } }",[a,b])',
            )
        )
        result = index_fragments(identical, schemas=self.schemas)
        self.assertEqual(result.diagnostics, ())
        self.assertEqual(self.names(result, "q"), [None, "F"])

    def test_invalid_graphql_fragments_do_not_remove_unrelated_valid_documents(
        self,
    ) -> None:
        for source in (
            "fragment F on Missing { id }",
            "fragment F on String { id }",
            "fragment F on User { ...Missing }",
            "fragment F on User { ...F }",
            "fragment F on User { ...G } fragment G on User { ...F }",
            "fragment F on User { ... on Org { title } }",
            "fragment",
        ):
            with self.subTest(source=source):
                sources = self.resolve(
                    module("q", f'bad=graphql({source!r})\ngood=graphql("{{ hello }}")')
                )
                result = index_fragments(sources, schemas=self.schemas)
                self.assertEqual(
                    [item.call.binding for item in result.documents], ["good"]
                )
                self.assertTrue(result.diagnostics)

    def test_operation_targets_and_selection_are_explicit(self) -> None:
        sources = self.resolve(
            module(
                "q",
                'op=graphql("query Other { hello }")\nbad=graphql("{ hello }",[op])\n'
                'selected=graphql("fragment F on User { id } query A { hello } '
                'query B { hello }",operation="B")',
            )
        )
        result = index_fragments(sources, schemas=self.schemas)
        self.assertFalse(any(item.call.binding == "bad" for item in result.documents))
        self.assertIn(
            "operation-as-fragment", {item.code for item in result.diagnostics}
        )
        selected = next(
            item for item in result.documents if item.call.binding == "selected"
        )
        self.assertIsNone(selected.primary_fragment)
        assert selected.selected_operation is not None
        self.assertEqual(selected.selected_operation.name, "B")
        self.assertEqual([fact.name for fact in selected.definitions], ["F", "A", "B"])
        for source, selector in (
            ("query A { hello } query B { hello }", None),
            ("query A { hello }", "Missing"),
            ("{ hello } query A { hello }", "A"),
            ("query A { hello } query A { hello }", "A"),
            ("fragment F on User { id }", "F"),
        ):
            with self.subTest(source=source):
                records = self.resolve(
                    module("q", f"bad=graphql({source!r},operation={selector!r})")
                )
                invalid = index_fragments(records, schemas=self.schemas)
                self.assertEqual(invalid.documents, ())
                self.assertTrue(invalid.diagnostics)

    def test_none_schema_requires_explicit_context_and_tokens_must_match(self) -> None:
        records = resolve_project((module("q", 'q=graphql("{ hello }")'),))
        self.assertIsNone(records.calls[0].schema_name)
        missing = index_fragments(records, schemas=self.schemas)
        self.assertEqual(missing.documents, ())
        self.assertEqual(missing.diagnostics[0].code, "missing-schema-context")
        explicit = index_fragments(records, schemas={("p", None): self.context})
        self.assertEqual(explicit.diagnostics, ())
        self.assertEqual(explicit.documents[0].schema_identity, ("p", "default"))
        wrong = index_fragments(
            self.resolve(module("q", 'q=graphql("{ hello }")')),
            schemas={("p", "default"): replace(self.context, schema_name="other")},
        )
        self.assertEqual(wrong.documents, ())
        fragment, query = self.resolve(
            module(
                "q",
                'f=graphql("fragment F on User { id }")\n'
                'q=graphql("{ user { ...F } }",[f])',
            )
        ).calls
        mismatch = index_fragments(
            ProjectSources((replace(fragment, schema_name=None), query), ()),
            schemas={
                **self.schemas,
                ("p", None): replace(self.context, token="new-generation"),
            },
        )
        self.assertFalse(any(d.call.binding == "q" for d in mismatch.documents))
        self.assertIn("schema-context-mismatch", {d.code for d in mismatch.diagnostics})

    def test_versioned_unsaved_dependency_changes_recompute_without_mutation(
        self,
    ) -> None:
        query = module(
            "query", 'from leaf import leaf\nq=graphql("{ user { ...F } }",[leaf])', 3
        )
        old = self.resolve(
            module("leaf", 'leaf=graphql("fragment F on User { id }")', 1), query
        )
        new = self.resolve(
            module("leaf", 'leaf=graphql("fragment F on User { name }")', 2), query
        )
        first = index_fragments(old, schemas=self.schemas)
        second = index_fragments(new, schemas=self.schemas)
        self.assertEqual(first.diagnostics, ())
        self.assertEqual(second.diagnostics, ())
        old_q = next(item for item in first.documents if item.call.binding == "q")
        new_q = next(item for item in second.documents if item.call.binding == "q")
        self.assertIn("id", old_q.definitions[1].printed)
        self.assertIn("name", new_q.definitions[1].printed)
        self.assertEqual(
            {d.version for d in new_q.dependencies if d.module == "leaf"}, {2}
        )
        self.assertEqual(
            {d.version for d in old_q.dependencies if d.module == "leaf"}, {1}
        )
        stale = ProjectSources((old.calls[-1], *new.calls[:-1]), ())
        invalid = index_fragments(stale, schemas=self.schemas)
        self.assertFalse(any(item.call.binding == "q" for item in invalid.documents))

    def test_guarded_invalid_reference_records_cannot_publish(self) -> None:
        sources = self.resolve(
            module(
                "q",
                'f=graphql("fragment F on User { id }")\n'
                'q=graphql("{ user { ...F } }",[f])',
            )
        )
        fragment, query = sources.calls
        reference = query.fragments[0]
        for target in (
            replace(
                reference.target,
                module=replace(reference.target.module, project="other"),
            ),
            replace(
                reference.target, module=replace(reference.target.module, version=99)
            ),
            DocumentIdentity(query.module, query.range),
        ):
            with self.subTest(target=target):
                changed = replace(query, fragments=(replace(reference, target=target),))
                result = index_fragments(
                    ProjectSources((fragment, changed), ()), schemas=self.schemas
                )
                self.assertFalse(
                    any(item.call.binding == "q" for item in result.documents)
                )
                self.assertTrue(result.diagnostics)

    def test_schema_and_project_contexts_are_isolated(self) -> None:
        sources = self.resolve(
            module("one", 'f=graphql("fragment F on User { id }")'),
            module("two", 'f=graphql("fragment F on User { name }")', project="other"),
        )
        result = index_fragments(
            sources, schemas={**self.schemas, ("other", "default"): self.context}
        )
        self.assertEqual(len(result.documents), 2)
        self.assertEqual(result.diagnostics, ())
        fragment, query = self.resolve(
            module(
                "q",
                'f=graphql("fragment F on User { id }")\n'
                'q=graphql("{ user { ...F } }",[f])',
            )
        ).calls
        changed = replace(fragment, schema_name="second")
        reference = replace(
            query.fragments[0], target=DocumentIdentity(changed.module, changed.range)
        )
        result = index_fragments(
            ProjectSources((changed, replace(query, fragments=(reference,))), ()),
            schemas={
                **self.schemas,
                ("p", "second"): replace(self.context, schema_name="second"),
            },
        )
        self.assertFalse(any(item.call.binding == "q" for item in result.documents))

    def test_parse_once_and_complexity_failure_discard_affected_dependents(
        self,
    ) -> None:
        sources = self.resolve(
            module(
                "q",
                'f=graphql("fragment F on User { id }")\n'
                'q=graphql("{ user { ...F } }",[f,f])',
            )
        )
        from graphql import parse

        with patch("graphql_desk.fragments.parse", wraps=parse) as parser:
            result = index_fragments(sources, schemas=self.schemas)
        self.assertEqual(result.diagnostics, ())
        self.assertEqual(parser.call_count, len(sources.calls))
        with patch("graphql_desk.fragments.print_ast", side_effect=RecursionError):
            result = index_fragments(sources, schemas=self.schemas)
        self.assertEqual(result.documents, ())
        self.assertIn("source-too-complex", {item.code for item in result.diagnostics})

    def test_source_diagnostics_preserved_and_validation_complexity_fails_closed(
        self,
    ) -> None:
        sources = resolve_project(
            (module("q", 'bad=graphql(dynamic)\ngood=graphql("{ hello }")'),),
            bindings=(ProjectBindings("p", ("default",), "default"),),
        )
        self.assertTrue(sources.diagnostics)
        result = index_fragments(sources, schemas=self.schemas)
        self.assertEqual(result.diagnostics, sources.diagnostics)
        self.assertEqual([d.call.binding for d in result.documents], ["good"])
        with patch("graphql_desk.fragments.validate", side_effect=RecursionError):
            failed = index_fragments(sources, schemas=self.schemas)
        self.assertEqual(failed.documents, ())
        self.assertIn("source-too-complex", {d.code for d in failed.diagnostics})


if __name__ == "__main__":
    unittest.main()
