"""The runtime license allowlist must fail closed."""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from scripts.check_runtime_licenses import runtime_inventory


class RuntimeLicenseTests(unittest.TestCase):
    def inventory(self, dependency: SimpleNamespace) -> list[dict[str, str]]:
        packages = {
            "graphql-desk": SimpleNamespace(
                requires=["graphql-core==3.3.0", 'ignored; extra == "optional"']
            ),
            "graphql-core": dependency,
        }
        with patch("scripts.check_runtime_licenses.distribution", packages.__getitem__):
            return runtime_inventory("graphql-desk")

    def test_approved_license_and_optional_dependency(self) -> None:
        self.assertEqual(
            self.inventory(
                SimpleNamespace(
                    requires=[], version="3.3.0", metadata={"License-Expression": "MIT"}
                )
            ),
            [{"name": "graphql-core", "version": "3.3.0", "license": "MIT"}],
        )

    def test_missing_or_changed_license_requires_review(self) -> None:
        for metadata in ({}, {"License": "GPL-3.0-only"}):
            with self.subTest(metadata=metadata), self.assertRaises(ValueError):
                self.inventory(
                    SimpleNamespace(requires=[], version="3.3.0", metadata=metadata)
                )


if __name__ == "__main__":
    unittest.main()
