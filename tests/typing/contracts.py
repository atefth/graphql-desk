"""Checker-only contracts; intentional errors must keep their ignore necessary."""

from typing import TypedDict, TypeVar

from graphql import DocumentNode, parse
from typing_extensions import assert_type

from graphql_desk import TypedDocument

R = TypeVar("R")
V = TypeVar("V")


class Result(TypedDict):
    greeting: str


class Variables(TypedDict):
    name: str


class OtherResult(TypedDict):
    count: int


class WrongVariables(TypedDict):
    name: int


class MissingVariables(TypedDict):
    pass


def execute(document: TypedDocument[R, V], variables: V) -> R:
    raise NotImplementedError("Checker fixture only; no transport implementation")


document = TypedDocument[Result, Variables](parse("{ greeting }"))
assert_type(document.ast, DocumentNode)
assert_type(document.operation_name, str | None)
variables: Variables = {"name": "Ada"}
assert_type(execute(document, variables), Result)

# Both generic parameters stay correlated with the document at a client boundary.
wrong: WrongVariables = {"name": 1}
missing: MissingVariables = {}
execute(document, wrong)  # type: ignore[misc]  # pyright: ignore[reportArgumentType]
execute(document, missing)  # type: ignore[misc]  # pyright: ignore[reportArgumentType]
wrong_result: TypedDocument[OtherResult, Variables] = document  # type: ignore[assignment]  # pyright: ignore[reportAssignmentType]
wrong_variables: TypedDocument[Result, dict[str, int]] = document  # type: ignore[assignment]  # pyright: ignore[reportAssignmentType]
