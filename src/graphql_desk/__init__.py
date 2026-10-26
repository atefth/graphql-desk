"""The typed-document foundation for GraphQL Desk."""

from dataclasses import dataclass, field
from typing import Generic, TypeVar

from graphql import DocumentNode, parse

from .fragment_helpers import read_fragment, read_fragment_passthrough
from .runtime import PersistedDocument, gql, graphql, init_graphql, persisted_id
from .testing import unsafe_read_result

__all__ = [
    "TypedDocument",
    "PersistedDocument",
    "gql",
    "graphql",
    "init_graphql",
    "persisted_id",
    "parse",
    "read_fragment",
    "read_fragment_passthrough",
    "unsafe_read_result",
]

_Result = TypeVar("_Result")
_Variables = TypeVar("_Variables")


@dataclass(frozen=True)
class TypedDocument(Generic[_Result, _Variables]):
    """An AST with static result/variables types and optional operation metadata.

    Construction neither validates a schema nor checks the phantom type arguments
    against the AST. Executors consume ``ast`` and select ``operation_name``;
    their response data and error envelopes remain unchanged.
    """

    ast: DocumentNode
    operation_name: str | None = None
    schema_name: str | None = field(default=None, kw_only=True)
    persisted_id: str | None = field(default=None, kw_only=True)
