from generated import GetUserVariables, get_user

missing_required_variable: GetUserVariables = {}  # Expected: missing required key 'id'.
wrong_variable_type: GetUserVariables = {"id": 42}  # Expected: int is not str.
result = get_user({"id": "42"})
result["user"]["missing"]  # Expected: unknown TypedDict key; user is also nullable.
