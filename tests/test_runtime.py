import unittest
from dataclasses import FrozenInstanceError
from typing import TYPE_CHECKING, Any

from graphql import (
    FragmentDefinitionNode,
    build_schema,
    execute_sync,  # pyright: ignore[reportUnknownVariableType]
    print_ast,
    validate,
)
from graphql import (
    parse as core_parse,
)
from typing_extensions import assert_type

from graphql_desk import TypedDocument, gql, graphql, init_graphql, parse


class RuntimeTests(unittest.TestCase):
    def test_primary_fragment_unmask_is_removed_before_composition(self) -> None:
        source = "fragment Card on User @_unmask { id }"
        fragment = graphql(source)
        self.assertNotIn("@_unmask", print_ast(fragment.ast))
        assert fragment.ast.loc is not None
        self.assertEqual(fragment.ast.loc.source.body, source)
        document = graphql("{ user { ...Card } }", [fragment])
        self.assertIs(document.ast.definitions[1], fragment.ast.definitions[0])
        schema = build_schema("type User { id: ID! } type Query { user: User }")
        self.assertEqual(validate(schema, document.ast), [])
        self.assertEqual(
            execute_sync(schema, document.ast, root_value={"user": {"id": "1"}}).data,
            {"user": {"id": "1"}},
        )
        marked = graphql(
            "fragment Card on User @keep @_unmask @keep { id } "
            "fragment Other on User @_unmask { id }"
        )
        first, second = marked.ast.definitions
        assert isinstance(first, FragmentDefinitionNode)
        assert isinstance(second, FragmentDefinitionNode)
        assert first.directives is not None and second.directives is not None
        self.assertEqual(
            [item.name.value for item in first.directives], ["keep", "keep"]
        )
        self.assertEqual([item.name.value for item in second.directives], ["_unmask"])
        # The pinned upstream only strips the first fragment definition's marker.
        operation_first = graphql("{ user { ...Card } } " + source)
        self.assertIn("@_unmask", print_ast(operation_first.ast))

    def test_public_aliases_and_untyped_baseline(self) -> None:
        self.assertIs(gql, graphql)
        self.assertIs(parse, core_parse)
        document = graphql("query Hello { greeting }")
        assert_type(document, TypedDocument[Any, Any])
        self.assertEqual(document.operation_name, "Hello")
        self.assertIsNone(document.schema_name)
        self.assertIsNone(graphql("{ greeting }").operation_name)
        # Runtime parsing is intentionally schema-agnostic.
        self.assertEqual(len(graphql("{ fieldNotInASchema }").ast.definitions), 1)

    def test_operation_selection_and_execution(self) -> None:
        document = graphql(
            "query One { greeting } query Two { greeting }", operation="Two"
        )
        schema = build_schema("type Query { greeting: String! }")
        response = execute_sync(
            schema,
            document.ast,
            root_value={"greeting": "Hi"},
            operation_name=document.operation_name,
        )
        self.assertEqual(response.data, {"greeting": "Hi"})
        self.assertIsNone(response.errors)
        for source, operation in (
            ("query One { x } query Two { x }", None),
            ("query One { x }", "Missing"),
            ("{ x } query One { x }", "One"),
            ("query One { x } query One { y }", "One"),
        ):
            with self.subTest(source=source), self.assertRaises(ValueError):
                graphql(source, operation=operation)

    def test_transitive_composition_deduplicates_without_mutating(self) -> None:
        leaf = graphql("fragment Leaf on User { id }")
        left = graphql("fragment Left on User { ...Leaf name }", [leaf])
        right = graphql("fragment Right on User { ...Leaf age }", [leaf])
        document = graphql("query Q { user { ...Left ...Right } }", [left, right])
        self.assertEqual(len(document.ast.definitions), 4)
        self.assertIs(document.ast.definitions[1], left.ast.definitions[0])
        self.assertIs(document.ast.definitions[2], leaf.ast.definitions[0])
        self.assertEqual(len(left.ast.definitions), 2)
        self.assertEqual(len(right.ast.definitions), 2)
        self.assertEqual(len(leaf.ast.definitions), 1)
        first = document.ast.definitions[1]
        assert isinstance(first, FragmentDefinitionNode)
        self.assertEqual(first.name.value, "Left")
        # Canonically identical fragments deduplicate despite source locations.
        formatted = graphql("fragment Leaf on User {\n id\n }")
        self.assertEqual(
            len(graphql("{ user { ...Leaf } }", [leaf, formatted]).ast.definitions), 2
        )

    def test_fragment_only_primary_and_invalid_composition(self) -> None:
        dependency = graphql("fragment Dependency on User { id }")
        primary = graphql("fragment Primary on User { ...Dependency }", [dependency])
        self.assertIsNone(primary.operation_name)
        first = primary.ast.definitions[0]
        assert isinstance(first, FragmentDefinitionNode)
        self.assertEqual(first.name.value, "Primary")
        with self.assertRaisesRegex(ValueError, "Fragment-only"):
            graphql("fragment F on User { id }", operation="F")
        with self.assertRaisesRegex(ValueError, "cannot contain operations"):
            graphql("{ user { id } }", [graphql("{ user { id } }")])
        conflict = graphql("fragment Dependency on User { name }")
        with self.assertRaisesRegex(ValueError, "Conflicting"):
            graphql("{ user { ...Dependency } }", [dependency, conflict])
        with self.assertRaisesRegex(ValueError, "Duplicate fragment"):
            graphql("fragment F on User { id } fragment F on User { id }")
        for source in (
            "{ user { ...Missing } }",
            "fragment F on User { ...F }",
            "fragment F on User { ...G } fragment G on User { ...F }",
            "type Query { x: Int }",
        ):
            with self.subTest(source=source), self.assertRaises(ValueError):
                graphql(source)

    def test_schema_metadata_is_frozen_and_preserved_through_unbound_composition(
        self,
    ) -> None:
        factory = init_graphql("main")
        self.assertEqual(factory.schema_name, "main")
        with self.assertRaises(FrozenInstanceError):
            setattr(factory, "schema_name", "other")
        fragment = factory("fragment F on User { id }")
        middle = graphql("fragment G on User { ...F }", [fragment])
        self.assertEqual(middle.schema_name, "main")
        document = factory("query Q { user { ...G } }", [middle])
        self.assertEqual(document.schema_name, "main")
        self.assertEqual(document.operation_name, "Q")
        assert_type(document, TypedDocument[Any, Any])
        with self.assertRaisesRegex(ValueError, "different schema"):
            init_graphql("other")("{ user { ...G } }", [middle])
        other = init_graphql("other")("fragment Other on User { name }")
        with self.assertRaisesRegex(ValueError, "different schema"):
            graphql("{ user { ...F ...Other } }", [fragment, other])
        manual = TypedDocument[dict[str, str], dict[str, str]](
            core_parse("fragment Manual on User { id }")
        )
        self.assertEqual(
            factory("{ user { ...Manual } }", [manual]).schema_name, "main"
        )
        for name in ("", "bad-name", "with space", "Ã©"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                init_graphql(name)


if TYPE_CHECKING:
    graphql(1)  # type: ignore[arg-type]  # pyright: ignore[reportArgumentType]
    graphql("{ x }", 1)  # type: ignore[arg-type]  # pyright: ignore[reportArgumentType]
    init_graphql(1)  # type: ignore[arg-type]  # pyright: ignore[reportArgumentType]
    init_graphql("main")("{ x }", operation=1)  # type: ignore[arg-type]  # pyright: ignore[reportArgumentType]
