"""The typed-document foundation for GraphQL Desk."""

from graphql import parse

from .fragment_helpers import mask_fragments, read_fragment, read_fragment_passthrough
from .runtime import PersistedDocument, gql, graphql, init_graphql, persisted_id
from .testing import unsafe_read_result
from .typed_document import TypedDocument

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
    "mask_fragments",
    "unsafe_read_result",
]
