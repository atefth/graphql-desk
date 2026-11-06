"""Stdlib assertions, executed only by the isolated evaluation interpreter."""

import copy
import importlib.util
import json
import sys
from collections.abc import Callable
from datetime import datetime
from pathlib import Path


def rejected(operation: Callable[[], object]) -> None:
    try:
        operation()
    except Exception as error:
        assert type(error).__name__ == "ValidationError", type(error).__name__
    else:
        raise AssertionError("Invalid data was accepted")


def main() -> None:
    sys.dont_write_bytecode = True
    path = Path(sys.argv[1])
    spec = importlib.util.spec_from_file_location("evaluated_models", path)
    assert spec is not None and spec.loader is not None
    models = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = models
    spec.loader.exec_module(models)
    operation = getattr(models, "GetUser")
    filter_model = getattr(models, "UserFilter")
    status = getattr(models, "Status")
    response = {
        "viewer": {
            "__typename": "User",
            "id": "u1",
            "displayName": "Ada",
            "status": "ACTIVE",
            "createdAt": "2024-01-02T03:04:05Z",
            "note": None,
        }
    }
    original = copy.deepcopy(response)
    value = operation.model_validate(response)
    assert value.viewer.display_name == "Ada"
    assert value.viewer.status is status.ACTIVE
    assert isinstance(value.viewer.created_at, datetime)
    assert value.viewer.created_at.isoformat() == "2024-01-02T03:04:05+00:00"
    assert response == original
    assert value is not response and value.viewer is not response["viewer"]
    assert value.model_dump(mode="json", by_alias=True) == original
    assert value.model_dump()["viewer"]["display_name"] == "Ada"
    assert isinstance(value.model_dump()["viewer"]["created_at"], datetime)
    rejected(lambda: operation.model_validate({"viewer": []}))
    rejected(lambda: operation.model_validate({"viewer": {"id": "u1"}}))
    rejected(lambda: operation.Arguments())
    rejected(lambda: filter_model(limit="not-an-integer"))
    assert filter_model().model_dump(by_alias=True, exclude_unset=True) == {}
    assert filter_model(note=None).model_dump(by_alias=True, exclude_unset=True) == {
        "note": None
    }
    assert operation.Arguments(id="u1").model_dump(
        by_alias=True, exclude_unset=True
    ) == {"id": "u1"}
    assert operation.Arguments(id="u1", filter=None).model_dump(
        by_alias=True, exclude_unset=True
    ) == {"id": "u1", "filter": None}
    assert filter_model(limit="5").limit == 5
    assert value.model_copy() is not value
    assert value.model_copy().viewer is value.viewer
    assert value.model_copy(deep=True).viewer is not value.viewer
    print(
        json.dumps(
            {
                "valid_response": True,
                "wrong_type_rejected": True,
                "missing_required_rejected": True,
                "omitted_vs_null_preserved_with_exclude_unset": True,
                "alias_round_trip_requires_by_alias": True,
                "enum_to_json_string": True,
                "datetime_parsed_and_json_serialized": True,
                "integer_string_coercion": True,
                "input_dict_unmodified": True,
                "models_wrap_and_copy_data": True,
                "shallow_vs_deep_model_copy": True,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
