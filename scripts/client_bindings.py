"""Worked user-side bindings; these are not a GraphQL Desk transport or client."""

from collections.abc import AsyncGenerator
from inspect import isawaitable
from typing import Any, Protocol, TypeVar, cast

from gql import GraphQLRequest
from gql.client import AsyncClientSession, SyncClientSession
from graphql import GraphQLError, GraphQLSchema
from graphql import execute as core_execute  # pyright: ignore[reportUnknownVariableType]
from graphql import (
    execute_sync as core_execute_sync,  # pyright: ignore[reportUnknownVariableType]
)

from graphql_desk import TypedDocument

R = TypeVar("R")
V = TypeVar("V")
R_co = TypeVar("R_co", covariant=True)


class TypedResponse(Protocol[R_co]):
    """Read-only view of an unchanged GraphQL ExecutionResult envelope."""

    @property
    def data(self) -> R_co | None: ...

    @property
    def errors(self) -> list[GraphQLError] | None: ...

    @property
    def extensions(self) -> dict[str, Any] | None: ...


def _request(document: TypedDocument[R, V], variables: V) -> GraphQLRequest:
    return GraphQLRequest(
        document.ast,
        variable_values=cast(dict[str, Any] | None, variables),
        operation_name=document.operation_name,
    )


def execute_core_sync(
    schema: GraphQLSchema, document: TypedDocument[R, V], variables: V
) -> TypedResponse[R]:
    return cast(
        TypedResponse[R],
        core_execute_sync(
            schema,
            document.ast,
            variable_values=cast(dict[str, Any] | None, variables),
            operation_name=document.operation_name,
        ),
    )


async def execute_core(
    schema: GraphQLSchema, document: TypedDocument[R, V], variables: V
) -> TypedResponse[R]:
    response = core_execute(
        schema,
        document.ast,
        variable_values=cast(dict[str, Any] | None, variables),
        operation_name=document.operation_name,
    )
    if isawaitable(response):
        response = await response
    return cast(TypedResponse[R], response)


def execute_gql_sync(
    session: SyncClientSession, document: TypedDocument[R, V], variables: V
) -> TypedResponse[R]:
    return cast(
        TypedResponse[R],
        session.execute(_request(document, variables), get_execution_result=True),
    )


async def execute_gql(
    session: AsyncClientSession, document: TypedDocument[R, V], variables: V
) -> TypedResponse[R]:
    return cast(
        TypedResponse[R],
        await session.execute(_request(document, variables), get_execution_result=True),
    )


async def subscribe_gql(
    session: AsyncClientSession, document: TypedDocument[R, V], variables: V
) -> AsyncGenerator[TypedResponse[R], None]:
    stream = session.subscribe(_request(document, variables), get_execution_result=True)
    try:
        async for response in stream:
            yield cast(TypedResponse[R], response)
    finally:
        await stream.aclose()
