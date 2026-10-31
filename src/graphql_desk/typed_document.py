"""Shared runtime carrier for schema-typed GraphQL documents."""

from dataclasses import dataclass, field
from typing import Generic, TypeVar

from graphql import DocumentNode

_Result = TypeVar("_Result")
_Variables = TypeVar("_Variables")


@dataclass(frozen=True)
class TypedDocument(Generic[_Result, _Variables]):
    """An AST with static result/variables types and optional operation metadata.

    Construction neither validates a schema nor checks the phantom type arguments
    against the AST. Executors consume ``ast`` and select ``operation_name``;
    response data and error envelopes remain unchanged.
    """

    ast: DocumentNode
    operation_name: str | None = None
    schema_name: str | None = field(default=None, kw_only=True)
    persisted_id: str | None = field(default=None, kw_only=True)
