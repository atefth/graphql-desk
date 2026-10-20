"""GraphQL errors retain original imported and decoded Python source origins."""

import unittest
from dataclasses import replace

from graphql import build_schema

from graphql_desk.document_validation import validate_indexed_document
from graphql_desk.fragments import FragmentIndex, SchemaContext, index_fragments
from graphql_desk.source_graph import ModuleSnapshot, ProjectBindings, resolve_project


class DocumentValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.context = SchemaContext(
            "main",
            build_schema("type User { id: ID! } type Query { user(id: ID): User }"),
            "v1",
        )
        self.modules = (
            ModuleSnapshot(
                "p",
                "fragment",
                "file:///fragment.py",
                1,
                "from graphql_desk import graphql\n"
                'fragment=graphql("fragment F on User { mi" "ss\\x69ng }")\n',
            ),
            ModuleSnapshot(
                "p",
                "query",
                "file:///query.py",
                1,
                "from graphql_desk import graphql\nfrom fragment import fragment\n"
                'query=graphql("query Q($id: ID) { user(id: $id) { ...F } }", '
                "[fragment])\n",
            ),
        )
        sources = resolve_project(
            self.modules, bindings=(ProjectBindings("p", ("main",), "main"),)
        )
        self.assertEqual(sources.diagnostics, ())
        self.index = index_fragments(sources, schemas={("p", "main"): self.context})
        self.assertEqual(self.index.diagnostics, ())
        self.document = next(
            item for item in self.index.documents if item.call.binding == "query"
        )

    def test_imported_split_escaped_field_maps_to_original_host_parts(self) -> None:
        diagnostics = validate_indexed_document(
            self.context, self.document, fragment_index=self.index, modules=self.modules
        )
        self.assertTrue(diagnostics)
        self.assertTrue(all(item.module.module == "fragment" for item in diagnostics))
        self.assertTrue(
            all("Cannot query field 'missing'" in item.message for item in diagnostics)
        )
        source = self.modules[0].source
        parts = [source[item.range.start : item.range.end] for item in diagnostics]
        self.assertEqual("".join(parts), r"miss\x69ng")
        self.assertNotIn('" "', "".join(parts))

    def test_fields_arguments_and_variable_errors_use_shared_policy(self) -> None:
        for text, message in (
            ("{ unknown }", "Cannot query field 'unknown'"),
            ("{ user(bad: 1) { id } }", "Unknown argument 'bad'"),
            (
                "query($id: Boolean!) { user(id:$id) { id } }",
                "used in position expecting",
            ),
            ("{ user @client { id } }", None),
        ):
            with self.subTest(text=text):
                module = replace(
                    self.modules[1],
                    source="from graphql_desk import graphql\n"
                    f"query=graphql({text!r})\n",
                )
                sources = resolve_project(
                    (module,), bindings=(ProjectBindings("p", ("main",), "main"),)
                )
                index = index_fragments(sources, schemas={("p", "main"): self.context})
                self.assertEqual(index.diagnostics, ())
                errors = validate_indexed_document(
                    self.context,
                    index.documents[0],
                    fragment_index=index,
                    modules=(module,),
                )
                if message is None:
                    self.assertEqual(errors, ())
                else:
                    self.assertTrue(
                        any(message in item.message for item in errors), errors
                    )
                    self.assertTrue(
                        all(item.module.module == "query" for item in errors)
                    )

    def test_stale_source_owner_and_context_facts_reject(self) -> None:
        changed = replace(
            self.modules[0], source=self.modules[0].source.replace('mi"', 'no"')
        )
        for context, index, modules in (
            (replace(self.context, token="v2"), self.index, self.modules),
            (self.context, FragmentIndex((self.document,), ()), self.modules),
            (self.context, self.index, (changed, self.modules[1])),
            (
                self.context,
                self.index,
                (replace(self.modules[0], version=2), self.modules[1]),
            ),
        ):
            with (
                self.subTest(context=context.token, modules=modules),
                self.assertRaises(ValueError),
            ):
                validate_indexed_document(
                    context, self.document, fragment_index=index, modules=modules
                )
