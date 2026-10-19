"""Decoded string relations over explicit, versioned Python snapshots.

Spans use half-open Python character offsets. Nonlinear atoms are indivisible:
a nonempty lookup returns their entire opposite span; an empty lookup relates
only exact endpoints. Gaps have no origin and joins can have several origins.
Snapshot versions are caller-owned; they must change whenever source changes.
"""

import ast
import io
import re
import tokenize
import warnings
from dataclasses import dataclass, field
from typing import Literal

from .source import Span
from .source_graph import ModuleDependency, ModuleSnapshot, ResolvedCall

Encoding = Literal["codepoint", "utf-8", "utf-16"]


@dataclass(frozen=True)
class HostSpan:
    module: ModuleDependency
    range: Span


@dataclass(frozen=True)
class MappingAtom:
    decoded: Span
    host: HostSpan
    linear: bool = False


def _check(span: Span, length: int) -> None:
    if not 0 <= span.start <= span.end <= length:
        raise ValueError("Range is outside the snapshot")


def _project(query: Span, origin: Span, target: Span, linear: bool) -> tuple[Span, ...]:
    if query.start == query.end:
        point = query.start
        if linear and origin.start <= point <= origin.end:
            position = target.start + point - origin.start
            return (Span(position, position),)
        points: list[Span] = []
        if point == origin.start:
            points.append(Span(target.start, target.start))
        if point == origin.end:
            points.append(Span(target.end, target.end))
        return tuple(dict.fromkeys(points))
    if (
        origin.start == origin.end
        or query.start >= origin.end
        or query.end <= origin.start
    ):
        return ()
    if linear:
        return (
            Span(
                target.start + max(query.start, origin.start) - origin.start,
                target.start + min(query.end, origin.end) - origin.start,
            ),
        )
    return (target,)


@dataclass(frozen=True)
class SourceMap:
    text: str
    atoms: tuple[MappingAtom, ...]
    _lengths: tuple[tuple[ModuleDependency, int], ...] = field(repr=False)

    def to_host(self, decoded: Span) -> tuple[HostSpan, ...]:
        """Return separate origins; escapes are indivisible and gaps stay absent."""
        _check(decoded, len(self.text))
        return tuple(
            dict.fromkeys(
                HostSpan(atom.host.module, span)
                for atom in self.atoms
                for span in _project(
                    decoded, atom.decoded, atom.host.range, atom.linear
                )
            )
        )

    def to_decoded(self, module: ModuleDependency, host: Span) -> tuple[Span, ...]:
        lengths = dict(self._lengths)
        if module not in lengths:
            raise ValueError("No matching source-map module snapshot")
        _check(host, lengths[module])
        return tuple(
            dict.fromkeys(
                span
                for atom in self.atoms
                if atom.host.module == module
                for span in _project(host, atom.host.range, atom.decoded, atom.linear)
            )
        )


@dataclass(frozen=True)
class Position:
    line: int
    character: int


def _units(character: str, encoding: Encoding) -> int:
    if encoding == "codepoint":
        return 1
    if encoding == "utf-16":
        return 2 if ord(character) > 0xFFFF else 1
    try:
        return len(character.encode("utf-8"))
    except UnicodeError:
        raise ValueError("Text cannot be represented as UTF-8") from None


def convert_offset(
    text: str,
    offset: int,
    *,
    from_encoding: Encoding = "codepoint",
    to_encoding: Encoding = "utf-16",
) -> int:
    if from_encoding not in ("codepoint", "utf-8", "utf-16") or to_encoding not in (
        "codepoint",
        "utf-8",
        "utf-16",
    ):
        raise ValueError("Unknown offset encoding")
    if offset < 0:
        raise ValueError("Negative offset")
    consumed = converted = 0
    for character in text:
        if consumed == offset:
            return converted
        consumed += _units(character, from_encoding)
        converted += _units(character, to_encoding)
        if consumed > offset:
            raise ValueError("Offset splits a character")
    if consumed != offset:
        raise ValueError("Offset is outside the text")
    return converted


def _lines(text: str) -> tuple[tuple[int, int, int], ...]:
    lines: list[tuple[int, int, int]] = []
    start = 0
    for match in re.finditer(r"\r\n|\r|\n", text):
        lines.append((start, match.start(), match.end()))
        start = match.end()
    lines.append((start, len(text), len(text)))
    return tuple(lines)


def offset_to_position(
    text: str, offset: int, *, encoding: Encoding = "utf-16"
) -> Position:
    _check(Span(offset, offset), len(text))
    for line, (start, end, following) in enumerate(_lines(text)):
        if start <= offset <= end:
            return Position(
                line,
                convert_offset(text[start:end], offset - start, to_encoding=encoding),
            )
        if end < offset < following:
            raise ValueError("Offset splits a CRLF newline")
    raise ValueError("Offset is outside the text")


def position_to_offset(
    text: str, position: Position, *, encoding: Encoding = "utf-16"
) -> int:
    lines = _lines(text)
    if not 0 <= position.line < len(lines):
        raise ValueError("Line is outside the text")
    start, end, _ = lines[position.line]
    return start + convert_offset(
        text[start:end],
        position.character,
        from_encoding=encoding,
        to_encoding="codepoint",
    )


def _value(token: str) -> str:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", (DeprecationWarning, SyntaxWarning))
            value: object = ast.literal_eval(token)
    except (SyntaxError, ValueError, UnicodeError, RecursionError):
        raise ValueError("Unsupported Python string literal") from None
    if not isinstance(value, str):
        raise ValueError("Expected a Python string literal")
    return value


def _literal(
    token: str, start: int, module: ModuleDependency, offset: int
) -> tuple[str, tuple[MappingAtom, ...]]:
    expected = _value(token)
    match = re.match(r"(?i)([ru]*)(\"{3}|'{3}|\"|')", token)
    if match is None:
        raise ValueError("Unsupported Python string literal")
    raw = "r" in match[1].lower()
    cursor = match.end()
    end = len(token) - len(match[2])
    atoms: list[MappingAtom] = []
    pieces: list[str] = []
    if cursor == end:
        atoms.append(
            MappingAtom(
                Span(offset, offset),
                HostSpan(module, Span(start + cursor, start + cursor)),
                True,
            )
        )
    simple = {
        "a": "\a",
        "b": "\b",
        "f": "\f",
        "n": "\n",
        "r": "\r",
        "t": "\t",
        "v": "\v",
        "\\": "\\",
        "'": "'",
        '"': '"',
    }
    while cursor < end:
        following = cursor + 1
        linear = False
        if token[cursor] == "\r":
            following += token[following : following + 1] == "\n"
            value = "\n"
        elif not raw and token[cursor] == "\\":
            character = token[following]
            following += 1
            if character in "\r\n":
                if character == "\r" and token[following : following + 1] == "\n":
                    following += 1
                value = ""
            elif character in simple:
                value = simple[character]
            elif character in "01234567":
                while (
                    following < min(cursor + 4, end) and token[following] in "01234567"
                ):
                    following += 1
                value = chr(int(token[cursor + 1 : following], 8))
            elif character in "xuUN":
                following = (
                    token.index("}", following) + 1
                    if character == "N"
                    else cursor + {"x": 4, "u": 6, "U": 10}[character]
                )
                value = _value('"' + token[cursor:following] + '"')
            else:
                value = token[cursor:following]
        else:
            while (
                following < end
                and token[following] != "\r"
                and (raw or token[following] != "\\")
            ):
                following += 1
            value = token[cursor:following]
            linear = True
        atoms.append(
            MappingAtom(
                Span(offset, offset + len(value)),
                HostSpan(module, Span(start + cursor, start + following)),
                linear,
            )
        )
        pieces.append(value)
        offset += len(value)
        cursor = following
    if "".join(pieces) != expected:
        raise ValueError("String decoding does not match Python")
    return expected, tuple(atoms)


def _tokens(source: str) -> tuple[tuple[int, Span], ...]:
    lines = _lines(source)
    normalized = source.replace("\r\n", "\n").replace("\r", "\n")

    def offset(position: tuple[int, int]) -> int:
        line, column = position
        start, end, following = lines[line - 1]
        return start + column if column <= end - start else following

    try:
        return tuple(
            (token.type, Span(offset(token.start), offset(token.end)))
            for token in tokenize.generate_tokens(io.StringIO(normalized).readline)
            if token.type
            not in (
                tokenize.ENDMARKER,
                tokenize.ENCODING,
                tokenize.INDENT,
                tokenize.DEDENT,
            )
        )
    except (tokenize.TokenError, IndentationError, IndexError):
        raise ValueError("Invalid Python snapshot") from None


def build_source_map(
    call: ResolvedCall, modules: tuple[ModuleSnapshot, ...]
) -> SourceMap:
    """Verify provenance and decode literal tokens without executing project code."""
    snapshots: dict[tuple[str, str], ModuleSnapshot] = {}
    uris: set[tuple[str, str]] = set()
    for snapshot in modules:
        key = (snapshot.project, snapshot.module)
        uri = (snapshot.project, snapshot.uri)
        if key in snapshots or uri in uris:
            raise ValueError("Duplicate module snapshot")
        snapshots[key] = snapshot
        uris.add(uri)
    required = {
        call.module,
        *call.dependencies,
        *(item.module for item in call.contributions),
    }
    selected: dict[ModuleDependency, str] = {}
    for identity in sorted(
        required, key=lambda item: (item.project, item.module, item.uri, item.version)
    ):
        found = snapshots.get((identity.project, identity.module))
        if (
            found is None
            or found.uri != identity.uri
            or found.version != identity.version
        ):
            raise ValueError("Missing or stale module snapshot")
        selected[identity] = found.source
    _check(call.range, len(selected[call.module]))
    if not call.contributions:
        raise ValueError("Missing string contributions")
    tokens: dict[ModuleDependency, tuple[tuple[int, Span], ...]] = {}
    pieces: list[str] = []
    atoms: list[MappingAtom] = []
    offset = 0
    for contribution in call.contributions:
        source = selected[contribution.module]
        _check(contribution.range, len(source))
        if contribution.module not in tokens:
            tokens[contribution.module] = _tokens(source)
        contained = [
            (kind, span)
            for kind, span in tokens[contribution.module]
            if contribution.range.start <= span.start
            and span.end <= contribution.range.end
        ]
        literals = [span for kind, span in contained if kind == tokenize.STRING]
        if (
            not literals
            or literals[0].start != contribution.range.start
            or literals[-1].end != contribution.range.end
            or any(
                kind
                not in (
                    tokenize.STRING,
                    tokenize.COMMENT,
                    tokenize.NL,
                    tokenize.NEWLINE,
                )
                for kind, _ in contained
            )
        ):
            raise ValueError("Contribution is not a string literal expression")
        decoded: list[str] = []
        for span in literals:
            value, literal_atoms = _literal(
                source[span.start : span.end], span.start, contribution.module, offset
            )
            decoded.append(value)
            atoms.extend(literal_atoms)
            offset += len(value)
        value = "".join(decoded)
        if value != contribution.text:
            raise ValueError("Contribution text does not match the snapshot")
        pieces.append(value)
    if "".join(pieces) != call.source:
        raise ValueError("Decoded text does not match the resolved call")
    return SourceMap(
        call.source,
        tuple(atoms),
        tuple((identity, len(source)) for identity, source in selected.items()),
    )
