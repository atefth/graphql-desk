"""Shared directive/fragment policy retains ordinary validation and AST origins."""

import unittest

from graphql import FieldNode, Source, build_schema, parse

from graphql_desk.validation import validate_document


class ValidationTests(unittest.TestCase):
    def test_client_names_only_suppress_unknown_directive_errors(self) -> None:
        schema = build_schema("type Query { value: String }")
        source = Source("{ missing @client @local }", name="original.graphql")
        document = parse(source)
        errors = validate_document(schema, document, client_directives=("local",))
        self.assertEqual(len(errors), 1)
        self.assertIn("Cannot query field 'missing'", errors[0].message)
        nodes = errors[0].nodes
        assert nodes and isinstance(nodes[0], FieldNode) and nodes[0].loc is not None
        self.assertIs(nodes[0].loc.source, source)
        self.assertTrue(validate_document(schema, parse("{ value @unknown }")))
        for name in ("@local", "", "bad-name", "é"):
            with (
                self.subTest(name=name),
                self.assertRaisesRegex(ValueError, "GraphQL names"),
            ):
                validate_document(schema, document, client_directives=(name,))

    def test_schema_defined_client_directives_keep_argument_and_location_checks(
        self,
    ) -> None:
        schema = build_schema(
            "directive @client(flag: Boolean!) on FIELD type Query { value: String }"
        )
        self.assertFalse(
            validate_document(schema, parse("{ value @client(flag: true) }"))
        )
        self.assertIn(
            "@client(flag:)",
            validate_document(schema, parse("{ value @client }"))[0].message,
        )
        self.assertTrue(
            validate_document(schema, parse("query @client(flag: true) { value }"))
        )

    def test_unused_fragments_do_not_disable_variable_and_field_validation(
        self,
    ) -> None:
        schema = build_schema("type Query { value: String }")
        self.assertFalse(
            validate_document(schema, parse("fragment F on Query { value }"))
        )
        errors = validate_document(
            schema,
            parse("query Q($unused: Boolean) { value } fragment F on Query { value }"),
        )
        self.assertTrue(any("never used" in error.message for error in errors))
        self.assertFalse(
            any("Fragment 'F' is never used" in error.message for error in errors)
        )


if __name__ == "__main__":
    unittest.main()
