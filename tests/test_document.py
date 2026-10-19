import unittest
from dataclasses import FrozenInstanceError
from typing import TypedDict

from graphql import (
    build_schema,
    execute_sync,  # pyright: ignore[reportUnknownVariableType]
    parse,
)

from graphql_desk import TypedDocument


class Result(TypedDict):
    greeting: str


class Variables(TypedDict):
    name: str


class TypedDocumentTests(unittest.TestCase):
    def test_ast_identity_and_default_operation(self) -> None:
        ast = parse("{ greeting }")
        document = TypedDocument[Result, Variables](ast)
        self.assertIs(document.ast, ast)
        self.assertIsNone(document.operation_name)
        with self.assertRaises(FrozenInstanceError):
            setattr(document, "operation_name", "Changed")

    def test_ast_executor_preserves_data_and_errors(self) -> None:
        schema = build_schema("type Query { greeting(name: String!): String! }")
        document = TypedDocument[Result, Variables](
            parse(
                "query Other { __typename } "
                "query Greeting($name: String!) { greeting(name: $name) }"
            ),
            operation_name="Greeting",
        )
        data = {"greeting": "Hello"}
        result = execute_sync(
            schema,
            document.ast,
            root_value=data,
            variable_values={"name": "Ada"},
            operation_name=document.operation_name,
        )
        self.assertEqual(result.data, data)
        self.assertIsNone(result.errors)
        failure = execute_sync(
            schema,
            document.ast,
            variable_values={},
            operation_name=document.operation_name,
        )
        self.assertIsNone(failure.data)
        self.assertTrue(failure.errors)


if __name__ == "__main__":
    unittest.main()
