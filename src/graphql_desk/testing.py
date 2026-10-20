"""Fixture identities; fragment-owned values deliberately receive no checking."""

from __future__ import annotations

import keyword
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, TypeVar, cast

if TYPE_CHECKING:
    from .fragment_refs import FixtureDocument

_Result = TypeVar("_Result")
_Variables = TypeVar("_Variables")
_Input = TypeVar("_Input", bound=Mapping[str, object])


def unsafe_read_result(
    document: FixtureDocument[_Result, _Variables, _Input], data: _Input
) -> _Result:
    """Return data unchanged; generated inputs check only direct selected fields.

    This does not establish that masked fragments exist or have valid values.
    Use generated bound helpers for literal fixture context in both checkers.
    """
    # shortcut: root literals need bound helpers until mypy infers fixture inputs.
    return cast("_Result", data)


@dataclass(frozen=True)
class UnsafeResultHelperDeclarations:
    runtime: str
    stub: str
    function_name: str


def render_unsafe_result_helper(
    *, name: str, input_name: str, output_name: str
) -> UnsafeResultHelperDeclarations:
    """Format a bound identity helper for trusted generated fixture aliases."""
    identifiers = (name, input_name, output_name)
    if any(
        not value.isidentifier()
        or keyword.iskeyword(value)
        or unicodedata.normalize("NFKC", value) != value
        for value in identifiers
    ):
        raise ValueError("Unsafe fixture helper declaration names")
    function_name = name + "UnsafeReadResult"
    if {function_name, "__graphql_desk_identity_fragment"} & set(identifiers):
        raise ValueError(
            "Fixture helper declaration collides with input/output aliases"
        )
    return UnsafeResultHelperDeclarations(
        runtime=(
            "\nfrom graphql_desk.fragment_helpers import "
            "identity_fragment as __graphql_desk_identity_fragment\n"
            f"{function_name} = __graphql_desk_identity_fragment\n"
        ),
        stub=f"\ndef {function_name}(data: {input_name}) -> {output_name}: ...\n",
        function_name=function_name,
    )
