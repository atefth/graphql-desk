"""ID-only application entrypoint; it never imports the authoring package."""

from __future__ import annotations

from typing import TYPE_CHECKING

from demo_generated.persisted.default import SavedBook

if TYPE_CHECKING:
    from demo_generated.persisted.default import SavedBookVariables


def request(variables: SavedBookVariables) -> dict[str, object]:
    # This is a wire payload example, not an APQ implementation or server registry.
    return {"id": SavedBook.persisted_id, "variables": variables}


if __name__ == "__main__":
    assert request({"id": "1"})["id"] == "book-by-id-v1"
    print("ID-only request: ok")
