"""Shared executable-document validation policy; no discovery or execution."""

import re

from graphql import (
    DirectiveNode,
    DocumentNode,
    GraphQLError,
    GraphQLSchema,
    KnownDirectivesRule,
    Node,
    NoUnusedFragmentsRule,
    specified_rules,
    validate,
)
from graphql.validation.rules.custom.no_deprecated import NoDeprecatedCustomRule

# GraphQLSP's pinned built-in unknown-directive suppression list.
_CLIENT_DIRECTIVES = frozenset(
    {
        "populate",
        "client",
        "unmask",
        "_unmask",
        "_optional",
        "_relayPagination",
        "_simplePagination",
        "_required",
        "optional",
        "required",
        "arguments",
        "argumentDefinitions",
        "connection",
        "refetchable",
        "relay",
        "inline",
    }
)


def validate_document(
    schema: GraphQLSchema,
    document: DocumentNode,
    *,
    client_directives: tuple[str, ...] = (),
) -> tuple[GraphQLError, ...]:
    """Validate supplied AST nodes, preserving their original source locations.

    Client names suppress only unknown-directive errors. Directives defined by
    the schema retain ordinary argument/location validation. Fragment-only and
    explicitly composed documents may contain otherwise unused fragments.
    """
    if any(
        re.fullmatch(r"[_A-Za-z][_0-9A-Za-z]*", item) is None
        for item in client_directives
    ):
        raise ValueError("Client directives must be GraphQL names without @")
    client_names = _CLIENT_DIRECTIVES | set(client_directives)

    class KnownClientDirectives(KnownDirectivesRule):
        def enter_directive(
            self,
            node: DirectiveNode,
            key: object,
            parent: object,
            path: object,
            ancestors: list[Node],
        ) -> None:
            if node.name.value in client_names and not schema.get_directive(
                node.name.value
            ):
                return
            super().enter_directive(node, key, parent, path, ancestors)

    return tuple(
        validate(
            schema,
            document,
            [
                KnownClientDirectives if rule is KnownDirectivesRule else rule
                for rule in specified_rules
                if rule is not NoUnusedFragmentsRule
            ],
        )
    )


def validate_deprecations(
    schema: GraphQLSchema, document: DocumentNode
) -> tuple[GraphQLError, ...]:
    """Return GraphiQL-compatible deprecated field/argument warnings."""
    return tuple(validate(schema, document, (NoDeprecatedCustomRule,)))
