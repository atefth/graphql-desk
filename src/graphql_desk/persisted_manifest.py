"""Collect validated, schema-specific persisted manifest entries."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from .fragments import FragmentIndex, IndexedDocument
from .persisted import editor_sha256_id, manifest_document_bytes
from .source import Span
from .source_graph import (
    DocumentIdentity,
    GraphDiagnostic,
    ModuleDependency,
    ProjectSources,
)

SchemaIdentity = tuple[str, str]


@dataclass(frozen=True)
class PersistedManifestEntry:
    id: str
    document: str
    module: ModuleDependency
    range: Span
    id_range: Span


@dataclass(frozen=True)
class PersistedCollection:
    manifests: Mapping[SchemaIdentity, tuple[PersistedManifestEntry, ...]]
    diagnostics: tuple[GraphDiagnostic, ...]


def collect_persisted_manifests(
    sources: ProjectSources,
    index: FragmentIndex,
    *,
    normalize: bool = True,
) -> PersistedCollection:
    """Resolve declarations to indexed operations without touching the filesystem."""
    indexed: dict[DocumentIdentity, IndexedDocument] = {}
    unavailable = {
        DocumentIdentity(item.call.module, item.call.range): item
        for item in index.unavailable
    }
    diagnostics: list[GraphDiagnostic] = []
    for document in index.documents:
        identity = DocumentIdentity(document.call.module, document.call.range)
        if identity in indexed:
            diagnostics.append(
                GraphDiagnostic(
                    document.call.module,
                    "persisted-document-unavailable",
                    "Persisted document identity is ambiguous",
                    document.call.range,
                )
            )
            continue
        indexed[identity] = document

    manifests: dict[SchemaIdentity, dict[str, PersistedManifestEntry]] = {}
    owners: dict[tuple[str, str, str], tuple[PersistedManifestEntry, str]] = {}
    declarations = sorted(
        sources.persisted,
        key=lambda item: (
            item.module.project,
            item.schema_name or "",
            item.module.module,
            item.module.uri,
            item.range.start,
            item.id,
        ),
    )
    for declaration in declarations:
        indexed_document = indexed.get(declaration.document)
        selected_operation = (
            indexed_document.selected_operation
            if indexed_document is not None
            else None
        )
        if (
            indexed_document is None
            or selected_operation is None
            or selected_operation.kind == "fragment"
            or selected_operation.document != declaration.document
        ):
            rejected = unavailable.get(declaration.document)
            reason = (
                rejected.diagnostics[0].message
                if rejected is not None and rejected.diagnostics
                else "document is unavailable or is not an operation"
            )
            diagnostics.append(
                GraphDiagnostic(
                    declaration.module,
                    "persisted-document-unavailable",
                    f"Persisted document {reason}",
                    declaration.range,
                )
            )
            continue
        schema_identity = indexed_document.schema_identity
        if (
            declaration.document.module.project != declaration.module.project
            or schema_identity[0] != declaration.module.project
            or declaration.schema_name != schema_identity[1]
        ):
            diagnostics.append(
                GraphDiagnostic(
                    declaration.module,
                    "persisted-schema-mismatch",
                    "Persisted declaration and document use different schemas",
                    declaration.range,
                )
            )
            continue

        identity = declaration.document
        external_fragments = tuple(
            fact.printed
            for fact in indexed_document.definitions
            if fact.kind == "fragment" and fact.document != identity
        )
        try:
            if declaration.id.startswith("sha256:"):
                expected_id = editor_sha256_id(
                    indexed_document.call.source, external_fragments
                )
                if declaration.id != expected_id:
                    diagnostics.append(
                        GraphDiagnostic(
                            declaration.module,
                            "persisted-hash-mismatch",
                            "Persisted SHA256 ID does not match the "
                            "editor document hash",
                            declaration.id_range,
                        )
                    )
                    continue
            text = manifest_document_bytes(
                indexed_document.call.source,
                external_fragments,
                normalize=normalize,
            )
        except (ValueError, TypeError, RecursionError):
            diagnostics.append(
                GraphDiagnostic(
                    declaration.module,
                    "persisted-document-unavailable",
                    "Persisted document could not be normalized",
                    declaration.range,
                )
            )
            continue

        entry = PersistedManifestEntry(
            declaration.id,
            text,
            declaration.module,
            declaration.range,
            declaration.id_range,
        )
        owner_key = (*schema_identity, declaration.id)
        previous = owners.get(owner_key)
        if previous is not None:
            previous_entry, previous_text = previous
            if previous_text == text:
                continue
            diagnostics.append(
                GraphDiagnostic(
                    declaration.module,
                    "persisted-id-conflict",
                    "Persisted ID conflicts with a different document declared at "
                    f"{previous_entry.module.uri}:{previous_entry.id_range.start}-"
                    f"{previous_entry.id_range.end}",
                    declaration.id_range,
                )
            )
            continue
        owners[owner_key] = entry, text
        manifests.setdefault(schema_identity, {})[declaration.id] = entry

    return PersistedCollection(
        {
            schema: tuple(entries[key] for key in sorted(entries))
            for schema, entries in sorted(manifests.items())
        },
        tuple(diagnostics),
    )
