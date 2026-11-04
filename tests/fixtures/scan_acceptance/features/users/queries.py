from features.shared.fragments import UserFields, UserIdentity
from graphql_desk_generated.default import graphql

users = graphql(
    """
    query Users($filter: Filter = { sort: NEWEST, limit: 5 }) {
      users(filter: $filter) @audit {
        id
        name
        nameAgain: name
        legacy @skip(if: false) @include(if: true)
        ...UserFields
        ...UserIdentity
      }
    }
    """,
    fragments=[UserFields, UserIdentity],
)

simple = graphql("query Simple { users { name } }")
user_card = graphql('query UserCard { user(id: "1") { name } }')
orphan_child = graphql("fragment OrphanChild on User { deprecatedOnly }")
orphan_parent = graphql(
    "fragment OrphanParent on User { ...OrphanChild }",
    fragments=[orphan_child],
)
