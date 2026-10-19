"""Fail when the installed runtime dependency closure needs a license review."""

import json
from importlib.metadata import distribution

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name


def runtime_inventory(root: str) -> list[dict[str, str]]:
    pending = [root]
    seen: set[str] = set()
    inventory: list[dict[str, str]] = []
    approved = {"graphql-core": "MIT"}
    while pending:
        name = canonicalize_name(pending.pop())
        if name in seen:
            continue
        seen.add(name)
        package = distribution(name)
        for raw in package.requires or []:
            requirement = Requirement(raw)
            if requirement.marker is None or requirement.marker.evaluate({"extra": ""}):
                pending.append(requirement.name)
        if name == canonicalize_name(root):
            continue
        metadata = package.metadata
        license_name = (
            metadata["License-Expression"]
            if "License-Expression" in metadata
            else metadata["License"]
            if "License" in metadata
            else ""
        )
        if approved.get(name) != license_name:
            raise ValueError(
                f"License review required: {name} {package.version}: {license_name}"
            )
        inventory.append(
            {"name": name, "version": package.version, "license": license_name}
        )
    return sorted(inventory, key=lambda item: item["name"])


if __name__ == "__main__":
    result = runtime_inventory("graphql-desk")
    assert result == [{"name": "graphql-core", "version": "3.3.0", "license": "MIT"}]
    print(json.dumps(result, indent=2))
