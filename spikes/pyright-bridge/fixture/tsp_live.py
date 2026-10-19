from generated import GetUserResult

result: GetUserResult = {"user": {"id": "42", "name": "Atef", "nickname": None, "createdAt": "now"}}
if result["user"] is not None:
    value = result["user"]["nickname"]
