from graphql_desk_generated.default import graphql

UserFields = graphql("fragment UserFields on User { name }")
UserIdentity = graphql("fragment UserIdentity on User { id }")
