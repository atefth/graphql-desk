"""Installed package smoke checks."""

import unittest
from importlib.resources import files


class PackageTests(unittest.TestCase):
    def test_typing_marker_is_distributed(self) -> None:
        self.assertTrue(files("graphql_desk").joinpath("py.typed").is_file())
