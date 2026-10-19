from typing import assert_type

from generated import GetUserResult, GetUserVariables, User, get_user

variables: GetUserVariables = {"id": "42"}
result: GetUserResult = get_user(variables)
assert_type(result["user"], User | None)

if result["user"] is not None:
    assert_type(result["user"]["name"], str)
    assert_type(result["user"]["nickname"], str | None)
    assert_type(result["user"]["createdAt"], str)
