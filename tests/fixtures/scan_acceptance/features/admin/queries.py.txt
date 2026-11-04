from graphql_desk_generated.admin import graphql

dashboard = graphql("query AdminDashboard { users { id } }")
