"""Identity readers and static fragment helper declarations."""

from __future__ import annotations

import keyword
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, TypeVar, overload

from typing_extensions import Never, TypedDict

if TYPE_CHECKING:
    from .fragment_refs import FragmentRef, ReadableFragmentDocument
    from .operation_types import FragmentReaderTypes

_Identity = TypeVar("_Identity")
_Result = TypeVar("_Result")
_Variables = TypeVar("_Variables")
_ReadInput = TypeVar("_ReadInput", bound=Mapping[str, object])
_Input = TypeVar("_Input", bound=Mapping[str, object])
_Output = TypeVar("_Output")
_Reader_co = TypeVar("_Reader_co", covariant=True)
_Value = TypeVar("_Value")


class FragmentAbsent(TypedDict, closed=True):
    pass


class _CallableDocument(Protocol[_Reader_co]):
    @property
    def _graphql_desk_reader(self) -> _Reader_co: ...


@overload
def read_fragment(
    document: ReadableFragmentDocument[_Identity, _Result, _Variables, _ReadInput],
    data: None,
) -> None: ...


@overload
def read_fragment(
    document: ReadableFragmentDocument[_Identity, _Result, _Variables, _ReadInput],
    data: Sequence[Never],
) -> Sequence[Never]: ...


@overload
def read_fragment(
    document: ReadableFragmentDocument[_Identity, _Result, _Variables, _ReadInput],
    data: Sequence[None],
) -> Sequence[None]: ...


@overload
def read_fragment(
    document: _CallableDocument[Callable[[FragmentAbsent], _Output]],
    data: FragmentAbsent,
) -> _Output: ...


@overload
def read_fragment(
    document: _CallableDocument[Callable[[_Input], _Output]], data: _Input
) -> _Output: ...


@overload
def read_fragment(
    document: _CallableDocument[Callable[[_Input], _Output]], data: Sequence[_Input]
) -> Sequence[_Output]: ...


@overload
def read_fragment(
    document: _CallableDocument[Callable[[_Input], _Output]],
    data: Sequence[_Input | None],
) -> Sequence[_Output | None]: ...


@overload
def read_fragment(
    document: _CallableDocument[Callable[[_Input], _Output]],
    data: Sequence[_Input] | None,
) -> Sequence[_Output] | None: ...


@overload
def read_fragment(
    document: _CallableDocument[Callable[[_Input], _Output]],
    data: Sequence[_Input | None] | None,
) -> Sequence[_Output | None] | None: ...


@overload
def read_fragment(
    document: ReadableFragmentDocument[_Identity, _Result, _Variables, _ReadInput],
    data: _ReadInput | FragmentRef[_Identity] | FragmentAbsent,
) -> _Result | FragmentAbsent: ...


@overload
def read_fragment(
    document: ReadableFragmentDocument[_Identity, _Result, _Variables, _ReadInput],
    data: Sequence[_ReadInput | FragmentRef[_Identity] | FragmentAbsent],
) -> Sequence[_Result | FragmentAbsent]: ...


@overload
def read_fragment(
    document: ReadableFragmentDocument[_Identity, _Result, _Variables, _ReadInput],
    data: Sequence[_ReadInput | FragmentRef[_Identity] | FragmentAbsent | None],
) -> Sequence[_Result | FragmentAbsent | None]: ...


@overload
def read_fragment(
    document: ReadableFragmentDocument[_Identity, _Result, _Variables, _ReadInput],
    data: Sequence[_ReadInput | FragmentRef[_Identity] | FragmentAbsent | None] | None,
) -> Sequence[_Result | FragmentAbsent | None] | None: ...


def read_fragment(document: object, data: object) -> object:
    """Return the original data without inspecting document or response metadata.

    Generated bindings supply static input/output views. Nullable abstract arrays
    and generic raw literals can retain wider result unions than bound helpers;
    contextual fallback inputs also retain the closed absent alternative.
    """
    return data


def identity_fragment(data: _Value) -> _Value:
    return data


@dataclass(frozen=True)
class FragmentHelperDeclarations:
    runtime: str
    stub: str
    reader_name: str
    function_name: str


def render_fragment_helpers(
    reader_types: FragmentReaderTypes, *, name: str, identity_name: str
) -> FragmentHelperDeclarations:
    """Format trusted renderer branch facts; no schema traversal or execution."""
    identifiers = [
        name,
        identity_name,
        reader_types.input_name,
        reader_types.output_name,
    ]
    for branch in reader_types.branches:
        identifiers.extend((branch.input_name, branch.output_name))
        if branch.narrow_input_name is not None:
            identifiers.append(branch.narrow_input_name)
        if (branch.typename is None) != (branch.narrow_input_name is None):
            raise ValueError("Fragment reader typename requires its narrow input")
    if any(
        not value.isidentifier()
        or keyword.iskeyword(value)
        or unicodedata.normalize("NFKC", value) != value
        for value in identifiers
    ):
        raise ValueError("Unsafe fragment reader declaration names")
    reader_name = name + "Reader"
    function_name = name + "ReadFragment"
    by_typename: dict[str, list[tuple[str, str]]] = {}
    for branch in reader_types.branches:
        if branch.typename is not None:
            assert branch.narrow_input_name is not None
            by_typename.setdefault(branch.typename, []).append(
                (branch.narrow_input_name, branch.output_name)
            )
    generated_names = {
        reader_name,
        function_name,
        *(f"{name}ReaderRef{number}" for number in range(len(by_typename))),
    }
    imported_names = {
        "__graphql_desk_Sequence",
        "__graphql_desk_Protocol",
        "__graphql_desk_overload",
        "__graphql_desk_Never",
        "__graphql_desk_Ref",
        "__graphql_desk_Callable",
        "__graphql_desk_Literal",
        "__graphql_desk_ReadOnly",
        "__graphql_desk_TypedDict",
        "__graphql_desk_FragmentAbsent",
        "__graphql_desk_identity_fragment",
    }
    if (generated_names | imported_names) & set(identifiers):
        raise ValueError("Fragment helper declarations collide with renderer aliases")
    absence = not any(branch.accepts_empty for branch in reader_types.branches)
    imports = (
        "\nfrom collections.abc import Sequence as __graphql_desk_Sequence\n"
        "from typing import Protocol as __graphql_desk_Protocol, "
        "overload as __graphql_desk_overload\n"
        "from typing_extensions import Never as __graphql_desk_Never\n"
        "from graphql_desk.fragment_refs import FragmentRef as __graphql_desk_Ref\n"
    )
    if by_typename:
        imports += (
            "from collections.abc import Callable as __graphql_desk_Callable\n"
            "from typing import Literal as __graphql_desk_Literal\n"
            "from typing_extensions import ReadOnly as __graphql_desk_ReadOnly, "
            "TypedDict as __graphql_desk_TypedDict\n"
        )
    if absence:
        imports += (
            "from graphql_desk.fragment_helpers import "
            "FragmentAbsent as __graphql_desk_FragmentAbsent\n"
        )
    scalar: list[tuple[str, str]] = []
    empty = "__graphql_desk_FragmentAbsent"
    if absence:
        scalar.append((empty, empty))
    definitions = ""
    narrowed: list[tuple[str, str]] = []
    for number, (typename, branches) in enumerate(sorted(by_typename.items())):
        reference = f"{name}ReaderRef{number}"
        definitions += (
            f"\n{reference} = __graphql_desk_TypedDict({reference!r}, {{\n"
            "    '__typename': __graphql_desk_ReadOnly["
            f"__graphql_desk_Literal[{typename!r}]],\n"
            "    '_graphql_desk_φ': __graphql_desk_ReadOnly["
            f"__graphql_desk_Callable[[{identity_name}], None]],\n"
            "})\n"
        )
        inputs = " | ".join(dict.fromkeys(input_ for input_, _ in branches))
        outputs = " | ".join(dict.fromkeys(output for _, output in branches))
        narrowed.append((f"{inputs} | {reference}", outputs))
    scalar.extend(narrowed)
    raw = f"__graphql_desk_Ref[{identity_name}]"
    if reader_types.branches:
        raw = f"{reader_types.input_name} | {raw}"
    result = reader_types.output_name
    scalar.append((raw, result))
    callback = scalar.copy()
    if absence:
        callback.append((f"{raw} | {empty}", f"{result} | {empty}"))
    protocol = f"\nclass {reader_name}(__graphql_desk_Protocol):\n"
    for input_, output in callback:
        if len(callback) > 1:
            protocol += "    @__graphql_desk_overload\n"
        protocol += f"    def __call__(self, data: {input_}) -> {output}: ...\n"
    sequence = "__graphql_desk_Sequence"
    never = "__graphql_desk_Never"
    signatures = [
        ("None", "None"),
        (f"{sequence}[{never}]", f"{sequence}[{never}]"),
        (f"{sequence}[None]", f"{sequence}[None]"),
        *scalar,
    ]
    if absence:
        signatures.append((f"{sequence}[{empty}]", f"{sequence}[{empty}]"))
    signatures.extend(
        (f"{sequence}[{input_}]", f"{sequence}[{output}]")
        for input_, output in narrowed
    )
    signatures.append((f"{sequence}[{raw}]", f"{sequence}[{result}]"))
    if absence:
        raw += f" | {empty}"
        result += f" | {empty}"
        signatures.append((f"{sequence}[{raw}]", f"{sequence}[{result}]"))
    # shortcut: nullable arrays retain all result/absence alternatives; refine
    # when checker-valid overloads can exclude their shared all-None input.
    signatures.append((f"{sequence}[{raw} | None]", f"{sequence}[{result} | None]"))
    functions = "\n" + "".join(
        "@__graphql_desk_overload\n"
        f"def {function_name}(data: {input_}) -> {output}: ...\n"
        for input_, output in signatures
    )
    return FragmentHelperDeclarations(
        runtime=(
            "\nfrom graphql_desk.fragment_helpers import "
            "identity_fragment as __graphql_desk_identity_fragment\n"
            f"{function_name} = __graphql_desk_identity_fragment\n"
        ),
        stub=imports + definitions + protocol + functions,
        reader_name=reader_name,
        function_name=function_name,
    )
