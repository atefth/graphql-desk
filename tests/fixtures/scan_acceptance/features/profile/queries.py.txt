from features.shared.fragments import UserFields
from graphql_desk_generated.default import graphql

profile_page = graphql(
    'query Profile { user(id: "1") { ...UserFields } }',
    fragments=[UserFields],
)
