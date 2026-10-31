"""Checker-only fragment capabilities; response dictionaries remain unchanged."""

import hashlib
import json
from collections.abc import Callable, Mapping
from typing import Generic, TypeVar

from typing_extensions import ReadOnly, TypedDict

from .fragments import DefinitionFact, IndexedDocument
from .source_graph import DocumentIdentity, ModuleDependency
from .typed_document import TypedDocument

_Key = TypeVar("_Key")
_Identity = TypeVar("_Identity")
_Identity_contra = TypeVar("_Identity_contra", contravariant=True)
_Result = TypeVar("_Result")
_Variables = TypeVar("_Variables")
_ReadInput = TypeVar("_ReadInput", bound=Mapping[str, object])


class FragmentIdentity(Generic[_Key]):
    """Invariant nominal family for generated literal identity keys."""


class FragmentRef(TypedDict, Generic[_Identity_contra]):
    """A required phantom capability, never a key inserted into response data.

    The private Unicode key cannot be a GraphQL response alias. ReadOnly allows
    callable contravariance: Ref[A | B] satisfies both Ref[A] and Ref[B]. Making
    this slot optional would let unrelated closed dictionaries pass as refs.
    Construction, key access and runtime validation are not supported contracts.
    """

    _graphql_desk_φ: ReadOnly[Callable[[_Identity_contra], None]]


class FragmentDocument(
    TypedDocument[_Result, _Variables], Generic[_Identity, _Result, _Variables]
):
    """Annotation-only identity view of an existing TypedDocument.

    Generated bindings establish the view. They do not construct this subclass,
    add attributes or promise isinstance/class identity at runtime.
    """


class ReadableFragmentDocument(
    FragmentDocument[_Identity, _Result, _Variables],
    Generic[_Identity, _Result, _Variables, _ReadInput],
):
    """Annotation-only fragment view with a separate readonly input shape."""


class FixtureDocument(
    TypedDocument[_Result, _Variables], Generic[_Result, _Variables, _ReadInput]
):
    """Annotation-only document view with its unchecked-fragment fixture input."""


def fragment_identity_key(owner: IndexedDocument, definition: DefinitionFact) -> str:
    """Return an opaque key for a local fragment and its complete snapshot closure.

    Use the fragment's owning indexed document, not its consuming operation.
    Caller-owned context tokens and dependency versions establish identity;
    this does not verify external source/config/schema freshness.
    """
    call = owner.call
    if (
        definition not in owner.definitions
        or definition.document != DocumentIdentity(call.module, call.range)
        or definition.kind != "fragment"
        or not definition.name
        or not definition.type_condition
        or owner.schema_identity[0] != call.module.project
        or call.schema_name not in (None, owner.schema_identity[1])
    ):
        raise ValueError("Fragment identity requires its owning indexed definition")
    dependencies: dict[tuple[str, str], ModuleDependency] = {}
    for dependency in (call.module, *owner.dependencies):
        key = (dependency.project, dependency.module)
        if dependency.project != call.module.project or (
            key in dependencies and dependencies[key] != dependency
        ):
            raise ValueError("Fragment dependency snapshot identities conflict")
        dependencies[key] = dependency
    data = [
        "graphql-desk.fragment-ref.v1",
        owner.schema_identity,
        owner.context_token,
        (
            call.module.project,
            call.module.module,
            call.module.uri,
            call.module.version,
            call.range.start,
            call.range.end,
        ),
        call.binding,
        definition.name,
        definition.type_condition,
        [
            (item.project, item.module, item.uri, item.version)
            for item in sorted(
                dependencies.values(),
                key=lambda item: (item.project, item.module, item.uri, item.version),
            )
        ],
    ]
    encoded = json.dumps(data, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    return "fragment-ref-v1:" + hashlib.sha256(encoded).hexdigest()
