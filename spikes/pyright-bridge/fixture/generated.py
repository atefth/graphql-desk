from typing import TypedDict


class User(TypedDict):
    id: str
    name: str
    nickname: str | None
    createdAt: str  # DateTime scalar mapped to Python str for this spike.


class GetUserResult(TypedDict):
    user: User | None


class GetUserVariables(TypedDict):
    id: str


def get_user(variables: GetUserVariables) -> GetUserResult: ...
