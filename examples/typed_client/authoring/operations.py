"""Same-source factory inference using the repository's existing client bindings."""

import asyncio
from pathlib import Path

from client_bindings import execute_core_sync, execute_gql
from demo_generated.default import graphql
from demo_generated.sources.authoring.operations import (
    BookQueryVariables,
    PartialQueryVariables,
)
from gql import Client
from gql.transport.exceptions import TransportQueryError
from graphql import GraphQLResolveInfo, GraphQLSchema, build_schema
from typing_extensions import assert_type

from graphql_desk import read_fragment, read_fragment_passthrough

book_fields = graphql("fragment BookFields on Book { title tags }")
book_query = graphql(
    "query Book($id: ID!) { book(id: $id) { id ...BookFields } }", (book_fields,)
)
partial_query = graphql("query Partial { greeting warning }")
SavedBook = graphql.persisted("book-by-id-v1", book_query)


def run(schema: GraphQLSchema) -> None:
    variables = BookQueryVariables(id="1")
    response = execute_core_sync(schema, book_query, variables)
    assert response.errors is None
    assert response.data is not None
    book = response.data["book"]
    assert_type(book["id"], str)
    card = read_fragment(book_fields, book)
    assert_type(card["title"], str | None)
    assert_type(card["tags"], list[str])
    assert id(card) == id(book)
    passthrough = read_fragment_passthrough(book_fields, book)
    assert id(passthrough) == id(book)
    assert "_graphql_desk_φ" not in book
    assert SavedBook.ast is book_query.ast

    partial = execute_core_sync(schema, partial_query, PartialQueryVariables())
    assert partial.data is not None and partial.errors
    assert_type(partial.data["warning"], str | None)
    assert partial.data["warning"] is None

    asyncio.run(gql_example(schema, variables))
    print("typed client, fragment identity, partial errors: ok")


async def gql_example(schema: GraphQLSchema, variables: BookQueryVariables) -> None:
    # gql's local schema transport is async; no network request is made.
    async with Client(schema=schema) as session:
        result = await execute_gql(session, book_query, variables)
        assert result.data is not None and result.errors is None
        try:
            await execute_gql(session, partial_query, PartialQueryVariables())
        except TransportQueryError as error:
            assert error.errors and error.data is not None
            assert error.data["warning"] is None
        else:
            raise AssertionError("The client's native exception must propagate")


def book(
    _root: object, _info: GraphQLResolveInfo[object], id: str
) -> dict[str, object]:
    return {"id": id, "title": None, "tags": ["example"]}


def warning(_root: object, _info: GraphQLResolveInfo[object]) -> None:
    raise ValueError("Expected demonstration resolver failure")


def greeting(_root: object, _info: GraphQLResolveInfo[object]) -> str:
    return "Hello"


if __name__ == "__main__":
    schema = build_schema(
        (Path(__file__).parents[1] / "schema.graphql").read_text(encoding="utf8")
    )
    assert schema.query_type is not None
    schema.query_type.fields["book"].resolve = book
    schema.query_type.fields["greeting"].resolve = greeting
    schema.query_type.fields["warning"].resolve = warning
    run(schema)
