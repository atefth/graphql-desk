"""Pure source-bound persisted-manifest collection tests."""

import unittest

from graphql_desk.fragments import (
    DefinitionFact,
    FragmentIndex,
    IndexedDocument,
)
from graphql_desk.persisted import editor_hash_input, editor_sha256_id
from graphql_desk.persisted_manifest import (
    PersistedCollection,
    collect_persisted_manifests,
)
from graphql_desk.source import Span
from graphql_desk.source_graph import (
    DocumentIdentity,
    GraphDiagnostic,
    ModuleDependency,
    PersistedDeclaration,
    ProjectSources,
    ResolvedCall,
)


def _module(name: str, project: str = "file:///project") -> ModuleDependency:
    return ModuleDependency(project, name, f"file:///project/{name}.py", 1)


def _document(
    name: str,
    source: str,
    *,
    schema: str = "default",
    project: str = "file:///project",
    call_start: int = 100,
    external: tuple[tuple[str, str, str], ...] = (),
) -> IndexedDocument:
    module = _module(name, project)
    call_range = Span(call_start, call_start + 20)
    call = ResolvedCall(
        module,
        call_range,
        source,
        (),
        (),
        schema_name=schema,
    )
    identity = DocumentIdentity(module, call_range)
    operation = DefinitionFact(identity, "query", "Q", None, source)
    facts = [operation]
    for module_name, fragment_name, printed in external:
        facts.append(
            DefinitionFact(
                DocumentIdentity(_module(module_name, project), Span(1, 2)),
                "fragment",
                fragment_name,
                "User",
                printed,
            )
        )
    return IndexedDocument(
        call,
        (project, schema),
        "schema-token",
        operation,
        None,
        tuple(facts),
        (),
    )


def _declaration(
    document: IndexedDocument | DocumentIdentity,
    *,
    id: str = "shared-id",
    module: str = "consumer",
    schema: str | None = "default",
    start: int = 10,
) -> PersistedDeclaration:
    project = (
        document.call.module.project
        if isinstance(document, IndexedDocument)
        else document.module.project
    )
    identity = (
        DocumentIdentity(document.call.module, document.call.range)
        if isinstance(document, IndexedDocument)
        else document
    )
    return PersistedDeclaration(
        _module(module, project),
        Span(start, start + 8),
        id,
        Span(start + 4, start + 7),
        identity,
        schema,
    )


def _collect(
    documents: tuple[IndexedDocument, ...],
    declarations: tuple[PersistedDeclaration, ...],
) -> PersistedCollection:
    return collect_persisted_manifests(
        ProjectSources((), (), persisted=declarations),
        FragmentIndex(documents, ()),
    )


class PersistedManifestTests(unittest.TestCase):
    def test_manifest_fragments_keep_indexed_transitive_order(self) -> None:
        document = _document(
            "operations",
            "query Q { user { ...A ...B } }",
            external=(
                ("a", "A", "fragment A on User { ...C }"),
                ("c", "C", "fragment C on User { id }"),
                ("b", "B", "fragment B on User { name }"),
            ),
        )
        result = _collect((document,), (_declaration(document),))
        entry = result.manifests[("file:///project", "default")][0]
        self.assertEqual(entry.id, "shared-id")
        self.assertLess(
            entry.document.index("fragment A"), entry.document.index("fragment C")
        )
        self.assertLess(
            entry.document.index("fragment C"), entry.document.index("fragment B")
        )
        self.assertEqual(result.diagnostics, ())

    def test_identical_ids_coalesce_and_conflicts_point_to_later_id(self) -> None:
        first = _document("first", "query Q { user { id } }")
        identical = _document("same", "query Q { user { id } }")
        conflict = _document("different", "query Q { user { name } }")
        result = _collect(
            (first, identical, conflict),
            (
                _declaration(first, module="consumer_a", start=10),
                _declaration(identical, module="consumer_b", start=20),
                _declaration(conflict, module="consumer_c", start=30),
            ),
        )
        entries = result.manifests[("file:///project", "default")]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].document, "query Q {\n  user {\n    id\n  }\n}")
        conflicts = [
            item for item in result.diagnostics if item.code == "persisted-id-conflict"
        ]
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0].module.module, "consumer_c")
        self.assertEqual(conflicts[0].range, Span(34, 37))
        self.assertIn("file:///project/consumer_a.py:14-17", conflicts[0].message)

    def test_same_id_is_isolated_per_schema(self) -> None:
        default = _document("default_ops", "query Q { user { id } }")
        other = _document("other_ops", "query Q { user { name } }", schema="other")
        result = _collect(
            (default, other),
            (_declaration(default), _declaration(other, schema="other")),
        )
        self.assertEqual(
            set(result.manifests),
            {("file:///project", "default"), ("file:///project", "other")},
        )
        self.assertEqual(result.diagnostics, ())

    def test_missing_and_schema_mismatched_documents_are_source_bound(self) -> None:
        actual = _document("actual", "query Q { user { id } }")
        missing = DocumentIdentity(_module("missing"), Span(4, 9))
        result = _collect(
            (actual,),
            (
                _declaration(missing, module="missing_consumer", start=40),
                _declaration(
                    actual,
                    module="wrong_schema_consumer",
                    schema="other",
                    start=50,
                ),
            ),
        )
        by_code = {item.code: item for item in result.diagnostics}
        self.assertEqual(by_code["persisted-document-unavailable"].range, Span(40, 48))
        self.assertEqual(by_code["persisted-schema-mismatch"].range, Span(50, 58))
        self.assertEqual(
            by_code["persisted-schema-mismatch"].module.module,
            "wrong_schema_consumer",
        )
        self.assertEqual(result.manifests, {})

    def test_unrelated_index_diagnostic_does_not_block_manifest(self) -> None:
        document = _document("operations", "query Q { user { id } }")
        unrelated = _module("unrelated")
        result = collect_persisted_manifests(
            ProjectSources((), (), persisted=(_declaration(document),)),
            FragmentIndex(
                (document,),
                (
                    GraphDiagnostic(
                        unrelated,
                        "invalid-document",
                        "Unrelated source has invalid GraphQL",
                        Span(0, 1),
                    ),
                ),
            ),
        )
        self.assertEqual(result.diagnostics, ())
        self.assertIn(("file:///project", "default"), result.manifests)

    def test_sha256_ids_use_editor_bytes_and_report_stale_id_at_literal(self) -> None:
        source = "query Q { user { ...F } } fragment F on User @_unmask { id }"
        document = _document("operations", source)
        declaration = _declaration(document, id=editor_sha256_id(source), start=60)
        result = _collect((document,), (declaration,))
        entry = result.manifests[("file:///project", "default")][0]
        self.assertIn("@_unmask", editor_hash_input(source))
        self.assertNotIn("@_unmask", entry.document)
        self.assertEqual(result.diagnostics, ())

        stale = _declaration(document, id="sha256:" + "0" * 64, start=80)
        invalid = _collect((document,), (stale,))
        diagnostic = invalid.diagnostics[0]
        self.assertEqual(diagnostic.code, "persisted-hash-mismatch")
        self.assertEqual(diagnostic.range, stale.id_range)
        self.assertEqual(invalid.manifests, {})


if __name__ == "__main__":
    unittest.main()
