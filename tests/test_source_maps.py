"""Exact literal origins, boundaries and protocol coordinate conversions."""

import unittest
from dataclasses import replace

from graphql_desk.source import Span
from graphql_desk.source_graph import (
    ModuleSnapshot,
    ResolvedCall,
    resolve_parse_sources,
    resolve_project,
)
from graphql_desk.source_maps import (
    Position,
    SourceMap,
    build_source_map,
    convert_offset,
    offset_to_position,
    position_to_offset,
)


def snapshot(name: str, source: str) -> ModuleSnapshot:
    return ModuleSnapshot("app", name, f"file:///{name}.py", 1, source)


def mapped(expression: str) -> tuple[SourceMap, ResolvedCall, ModuleSnapshot]:
    module = snapshot(
        "main", "from graphql_desk import gql\nq = gql(" + expression + ")\n"
    )
    result = resolve_project((module,))
    assert not result.diagnostics, result.diagnostics
    call = result.calls[0]
    return build_source_map(call, (module,)), call, module


class SourceMapTests(unittest.TestCase):
    def test_parser_facts_use_the_same_versioned_literal_map(self) -> None:
        module = snapshot(
            "main", "from graphql_desk import parse\nq=parse('a' + '\\x62')"
        )
        (call,) = resolve_parse_sources((module,)).calls
        mapping = build_source_map(call, (module,))
        self.assertEqual(mapping.text, "ab")
        (origin,) = mapping.to_host(Span(1, 2))
        self.assertEqual(module.source[origin.range.start : origin.range.end], r"\x62")
        with self.assertRaisesRegex(ValueError, "stale"):
            build_source_map(call, (replace(module, version=2),))

    def test_decoding_corpus(self) -> None:
        cases = (
            ('"abc"', "abc"),
            ("u'abc'", "abc"),
            ('r"a\\nb"', "a\\nb"),
            ('"\\a\\b\\f\\n\\r\\t\\v"', "\a\b\f\n\r\t\v"),
            ('"\\101\\x42\\u0043\\U0001f600\\N{LATIN CAPITAL LETTER D}"', "ABC😀D"),
            ('"\\q\\8"', "\\q\\8"),
            ('"a\\\nb"', "ab"),
            ('"a\\\r\nb"', "ab"),
            ('"""a\r\nb\rc\nd"""', "a\nb\nc\nd"),
            ('r"""a\r\nb"""', "a\nb"),
            ('R"""a\\\r\nb"""', "a\\\nb"),
            ("'''a\nb'''", "a\nb"),
            ('"\\000\\377\\777"', "\x00ÿǿ"),
            ('"a\\"b\\\\c"', 'a"b\\c'),
            ('r"a\\"b"', 'a\\"b'),
            ('"😀é"', "😀é"),
            ('""', ""),
        )
        for expression, expected in cases:
            with self.subTest(expression=expression):
                mapping, _, _ = mapped(expression)
                self.assertEqual(mapping.text, expected)
                self.assertTrue(mapping.atoms)

    def test_linear_runs_and_escape_atomicity(self) -> None:
        mapping, _, module = mapped('"abc\\nxyz"')
        first, escape, last = mapping.atoms
        self.assertTrue(first.linear)
        self.assertTrue(last.linear)
        self.assertFalse(escape.linear)
        self.assertEqual(
            mapping.to_host(Span(1, 2))[0].range,
            Span(first.host.range.start + 1, first.host.range.start + 2),
        )
        self.assertEqual(mapping.to_host(Span(3, 4)), (escape.host,))
        self.assertEqual(
            mapping.to_decoded(
                escape.host.module,
                Span(escape.host.range.start, escape.host.range.start + 1),
            ),
            (Span(3, 4),),
        )
        middle = escape.host.range.start + 1
        self.assertEqual(
            mapping.to_decoded(escape.host.module, Span(middle, middle)), ()
        )
        quote = module.source.index('"')
        self.assertEqual(
            mapping.to_decoded(first.host.module, Span(quote, quote + 1)), ()
        )
        large, _, _ = mapped(repr("a" * 10000))
        self.assertEqual(len(large.atoms), 1)

    def test_adjacent_tokens_and_comment_gaps(self) -> None:
        mapping, _, module = mapped('("ab" # gap\n "cd") + "ef"')
        self.assertEqual(mapping.text, "abcdef")
        self.assertEqual(len(mapping.atoms), 3)
        self.assertEqual(len(mapping.to_host(Span(2, 2))), 2)
        self.assertEqual(len(mapping.to_host(Span(0, 6))), 3)
        gap = module.source.index("# gap")
        self.assertEqual(
            mapping.to_decoded(mapping.atoms[0].host.module, Span(gap, gap + 5)), ()
        )

    def test_continuation_and_empty_boundaries(self) -> None:
        mapping, _, _ = mapped('"a\\\r\nb"')
        continuation = mapping.atoms[1]
        self.assertEqual(continuation.decoded, Span(1, 1))
        self.assertEqual(len(mapping.to_host(Span(1, 1))), 2)
        self.assertNotIn(continuation.host, mapping.to_host(Span(0, 2)))
        self.assertEqual(
            mapping.to_decoded(continuation.host.module, continuation.host.range),
            (Span(1, 1),),
        )
        empty, _, _ = mapped('""')
        self.assertEqual(empty.to_host(Span(0, 0)), (empty.atoms[0].host,))

    def test_crlf_and_unknown_escapes_have_no_interior_boundary(self) -> None:
        for expression in ('"""a\r\nb"""', '"a\\qb"'):
            with self.subTest(expression=expression):
                mapping, _, _ = mapped(expression)
                atom = mapping.atoms[1]
                self.assertFalse(atom.linear)
                middle = atom.host.range.start + 1
                self.assertEqual(
                    mapping.to_decoded(atom.host.module, Span(middle, middle)), ()
                )
                self.assertEqual(
                    mapping.to_decoded(atom.host.module, Span(middle, middle + 1)),
                    (atom.decoded,),
                )

    def test_no_final_newline_and_cr_only_host(self) -> None:
        for newline in ("\n", "\r", "\r\n"):
            module = snapshot(
                "main", "from graphql_desk import gql" + newline + "q=gql('😀')"
            )
            result = resolve_project((module,))
            self.assertFalse(result.diagnostics)
            mapping = build_source_map(result.calls[0], (module,))
            start = module.source.index("😀")
            self.assertEqual(mapping.atoms[0].host.range, Span(start, start + 1))

    def test_imports_repeated_origins_and_dependencies(self) -> None:
        constant = snapshot(
            "constants", 'TEXT = "ab"\nraise RuntimeError("never execute")\n'
        )
        bridge = snapshot("bridge", "from constants import TEXT\n")
        main = snapshot(
            "main",
            "from graphql_desk import gql\nfrom bridge import TEXT\n"
            "q=gql(TEXT + TEXT + 'c')\n",
        )
        modules = (main, bridge, constant)
        result = resolve_project(modules)
        self.assertFalse(result.diagnostics)
        call = result.calls[0]
        mapping = build_source_map(call, modules)
        self.assertEqual(mapping.text, "ababc")
        atom = mapping.atoms[0]
        self.assertEqual(
            mapping.to_decoded(atom.host.module, atom.host.range),
            (Span(0, 2), Span(2, 4)),
        )
        self.assertEqual(len(mapping.to_host(Span(0, 5))), 2)
        with self.assertRaisesRegex(ValueError, "stale"):
            build_source_map(call, (main, replace(bridge, version=2), constant))

    def test_snapshot_and_text_verification(self) -> None:
        mapping, call, module = mapped('"abc"')
        bad_modules = (
            (),
            (replace(module, version=2),),
            (replace(module, uri="other"),),
            (replace(module, project="other"),),
            (module, module),
            (replace(module, source=module.source.replace("abc", "xyz")),),
        )
        for modules in bad_modules:
            with self.subTest(modules=modules), self.assertRaises(ValueError):
                build_source_map(call, modules)
        for bad in (
            replace(call, source="wrong"),
            replace(call, contributions=()),
            replace(call, range=Span(-1, 2)),
            replace(
                call, contributions=(replace(call.contributions[0], text="wrong"),)
            ),
        ):
            with self.assertRaises(ValueError):
                build_source_map(bad, (module,))
        with self.assertRaises(ValueError):
            mapping.to_host(Span(0, 4))
        with self.assertRaises(ValueError):
            mapping.to_decoded(replace(call.module, version=2), Span(0, 0))

    def test_coordinate_encodings(self) -> None:
        text = "a😀é"
        self.assertEqual(convert_offset(text, 2, to_encoding="utf-8"), 5)
        self.assertEqual(convert_offset(text, 2), 3)
        self.assertEqual(
            convert_offset(text, 5, from_encoding="utf-8", to_encoding="codepoint"), 2
        )
        for offset in (2, 3, 4):
            with self.assertRaises(ValueError):
                convert_offset(text, offset, from_encoding="utf-8")
        with self.assertRaises(ValueError):
            convert_offset(text, 2, from_encoding="utf-16")
        for offset in (-1, 4):
            with self.assertRaises(ValueError):
                convert_offset(text, offset)
        with self.assertRaises(ValueError):
            convert_offset("\ud800", 1, to_encoding="utf-8")

    def test_physical_lines_and_roundtrips(self) -> None:
        text = "a😀\r\nb\rc\nd\u2028z\n"
        for offset in range(len(text) + 1):
            if offset and text[offset - 1 : offset + 1] == "\r\n":
                with self.assertRaises(ValueError):
                    offset_to_position(text, offset)
                continue
            for encoding in ("codepoint", "utf-8", "utf-16"):
                position = offset_to_position(text, offset, encoding=encoding)
                self.assertEqual(
                    position_to_offset(text, position, encoding=encoding), offset
                )
        self.assertEqual(offset_to_position(text, len(text)), Position(4, 0))
        for position in (
            Position(-1, 0),
            Position(9, 0),
            Position(0, -1),
            Position(0, 2),
        ):
            with self.assertRaises(ValueError):
                position_to_offset(text, position)


if __name__ == "__main__":
    unittest.main()
