"""Pinned byte contracts for editor IDs and persisted manifests."""

import unittest

from graphql_desk.persisted import (
    editor_hash_input,
    editor_sha256_id,
    manifest_document_bytes,
)


class PersistedBytesTests(unittest.TestCase):
    def test_nested_upstream_editor_hash_fixture(self) -> None:
        source = """query GetPokemonNested {
  pokemon(id: "x") {
    ...PokemonAttacks
  }
}"""
        fragment = """fragment PokemonAttacks on Pokemon {
  attacks {
    fast {
      name
      damage
    }
  }
}"""
        expected = """query GetPokemonNested {
  pokemon(id: "x") {
    ...PokemonAttacks
  }
}

fragment PokemonAttacks on Pokemon {
  attacks {
    fast {
      name
      damage
    }
  }
}"""
        self.assertEqual(editor_hash_input(source, [fragment]), expected)
        self.assertEqual(
            editor_sha256_id(source, [fragment]),
            "sha256:2cb43b8f11334b38ac959dd27b697b6a66d9cf0891929f138b2148c6c7ff33ca",
        )

    def test_transitive_fragments_keep_resolver_order(self) -> None:
        source = "query Q { user { ...A ...B } }"
        fragments = [
            "fragment A on User { ...C }",
            "fragment C on User { id }",
            "fragment B on User { name }",
        ]
        combined = editor_hash_input(source, fragments)
        self.assertLess(combined.index("fragment A"), combined.index("fragment C"))
        self.assertLess(combined.index("fragment C"), combined.index("fragment B"))
        self.assertEqual(
            editor_sha256_id(source, fragments),
            "sha256:1310523da0d22b208997c4a20644e733e32b91dbd3586a78766d1c38a4a99cd9",
        )

    def test_editor_and_manifest_keep_upstream_unmask_difference(self) -> None:
        inline = "fragment F on User @_unmask { name }"
        query = "query Q { user { ...F } } " + inline
        external = "fragment F on User @_unmask { name }"
        self.assertIn("@_unmask", editor_hash_input(query))
        self.assertNotIn("@_unmask", manifest_document_bytes(query))
        base = "query Q { user { ...F } }"
        self.assertNotIn("@_unmask", editor_hash_input(base, [external]))
        self.assertNotIn("@_unmask", manifest_document_bytes(base, [external]))

    def test_manifest_modes_and_deduplicated_external_fragments(self) -> None:
        source = "# keep comment\nquery Q{user{...F}}"
        external = "fragment F on User { id }"
        self.assertEqual(
            manifest_document_bytes(source, [external, "fragment F on User { id }"]),
            "query Q {\n  user {\n    ...F\n  }\n}\n\nfragment F on User {\n  id\n}",
        )
        self.assertEqual(
            manifest_document_bytes(source, [external], normalize=False),
            source + "\n\nfragment F on User {\n  id\n}",
        )

    def test_conflicts_and_non_fragment_dependencies_fail(self) -> None:
        with self.assertRaisesRegex(ValueError, "Conflicting fragment"):
            editor_hash_input(
                "query Q { user { ...F } }",
                ["fragment F on User { id }", "fragment F on User { name }"],
            )
        with self.assertRaisesRegex(ValueError, "must be fragments"):
            manifest_document_bytes("query Q { user { id } }", ["query Other { id }"])


if __name__ == "__main__":
    unittest.main()
