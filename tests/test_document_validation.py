"""GraphQL errors retain original imported and decoded Python source origins."""

import unittest
from dataclasses import replace
from unittest.mock import patch

from graphql import build_schema, parse

from graphql_desk.document_validation import (
    registered_document_validator,
    validate_indexed_document,
    validate_registered_document,
)
from graphql_desk.fragments import FragmentIndex, SchemaContext, index_fragments
from graphql_desk.source_graph import (
    DocumentIdentity,
    ModuleSnapshot,
    ProjectBindings,
    resolve_project,
)
from graphql_desk.source_maps import build_source_map
from graphql_desk.validation import validate_document


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

        owners = {
            DocumentIdentity(item.call.module, item.call.range): item
            for item in self.index.documents
        }
        self.assertEqual(
            validate_registered_document(
                self.context, self.document, owners=owners, modules=self.modules
            ),
            diagnostics,
        )
        owner = DocumentIdentity(self.document.call.module, self.document.call.range)
        owners[owner] = replace(self.document, context_token="stale")
        with self.assertRaisesRegex(ValueError, "absent from its owner registry"):
            validate_registered_document(
                self.context, self.document, owners=owners, modules=self.modules
            )

    def test_factory_reuses_fragment_origins_but_validates_each_closure(self) -> None:
        owners = {
            DocumentIdentity(item.call.module, item.call.range): item
            for item in self.index.documents
        }
        expected = tuple(
            validate_indexed_document(
                self.context, item, fragment_index=self.index, modules=self.modules
            )
            for item in self.index.documents
        )
        validate = registered_document_validator(
            self.context, owners=owners, modules=self.modules, client_directives=()
        )
        with (
            patch(
                "graphql_desk.document_validation.parse", wraps=parse
            ) as parse_graphql,
            patch(
                "graphql_desk.document_validation.build_source_map",
                wraps=build_source_map,
            ) as map_source,
            patch(
                "graphql_desk.document_validation.validate_document",
                wraps=validate_document,
            ) as validate_closure,
        ):
            self.assertEqual(tuple(map(validate, self.index.documents)), expected)
        self.assertEqual(parse_graphql.call_count, 2)
        self.assertEqual(map_source.call_count, 2)
        self.assertEqual(validate_closure.call_count, 2)

        (reference,) = self.document.call.fragments
        fragment = owners[reference.target]
        owners[reference.target] = replace(
            fragment, call=replace(fragment.call, source=fragment.call.source + " ")
        )
        with self.assertRaisesRegex(ValueError, "Decoded text does not match"):
            validate(self.document)

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

    def test_deprecation_warnings_are_opt_in(self) -> None:
        context = SchemaContext(
            "main",
            build_schema(
                'type Query { old: String @deprecated(reason: "Use current") '
                "current: String }"
            ),
            "v1",
        )
        module = ModuleSnapshot(
            "p",
            "query",
            "file:///query.py",
            1,
            'from graphql_desk import graphql\nquery=graphql("{ old }")\n',
        )
        sources = resolve_project(
            (module,), bindings=(ProjectBindings("p", ("main",), "main"),)
        )
        index = index_fragments(sources, schemas={("p", "main"): context})
        document = index.documents[0]

        default = validate_indexed_document(
            context, document, fragment_index=index, modules=(module,)
        )
        self.assertEqual(default, ())
        warnings = validate_indexed_document(
            context,
            document,
            fragment_index=index,
            modules=(module,),
            include_deprecations=True,
        )
        self.assertEqual([item.code for item in warnings], ["graphql-deprecation"])
