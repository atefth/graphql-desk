import ast
import unittest

from graphql_desk.factory_generation import GeneratedFile
from graphql_desk.quarantine import quarantine_generated_stubs


def owner(start: int) -> dict[str, object]:
    return {
        "module": {
            "project": "p",
            "module": "queries",
            "uri": "file:///queries.py",
            "version": 1,
        },
        "codepoint": {"start": start, "end": start + 8},
        "utf16": {"start": start, "end": start + 8},
    }


def operation(
    identity: dict[str, object],
    symbol: str,
    *,
    path: str = "generated/d0.pyi",
    canonical_owner: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "owner": identity,
        "canonical_owner": canonical_owner or identity,
        "declaration_path": path,
        "declaration_symbol": symbol,
        "status": "precise",
    }


def class_node(source: str, name: str) -> ast.ClassDef:
    return next(
        node
        for node in ast.parse(source).body
        if isinstance(node, ast.ClassDef) and node.name == name
    )


def base_arguments(source: str, class_name: str, alias: str) -> list[ast.expr]:
    declaration = class_node(source, class_name)
    base = next(
        item
        for item in declaration.bases
        if isinstance(item, ast.Subscript)
        and isinstance(item.value, ast.Name)
        and item.value.id == alias
    )
    return list(base.slice.elts) if isinstance(base.slice, ast.Tuple) else [base.slice]


def is_object(node: ast.expr) -> bool:
    return isinstance(node, ast.Name) and node.id == "object"


class QuarantineTests(unittest.TestCase):
    def test_quarantines_selected_operation_and_preserves_sibling(self) -> None:
        source = (
            "from graphql_desk.fragment_refs import FixtureDocument as "
            "__graphql_desk_FixtureDocument\n"
            "class FirstDocument(__graphql_desk_FixtureDocument["
            "FirstResult, FirstVariables, FirstInput]): ...\n"
            "class SecondDocument(__graphql_desk_FixtureDocument["
            "SecondResult, SecondVariables, SecondInput]): ...\n"
        )
        first, second = owner(1), owner(20)
        file = GeneratedFile("generated/d0.pyi", source)
        result = quarantine_generated_stubs(
            (file,),
            (operation(first, "FirstDocument"), operation(second, "SecondDocument")),
            (first,),
        )
        output = result[0].text
        self.assertTrue(
            all(
                is_object(arg)
                for arg in base_arguments(
                    output, "FirstDocument", "__graphql_desk_FixtureDocument"
                )
            )
        )
        self.assertEqual(
            [
                ast.unparse(arg)
                for arg in base_arguments(
                    output, "SecondDocument", "__graphql_desk_FixtureDocument"
                )
            ],
            ["SecondResult", "SecondVariables", "SecondInput"],
        )
        self.assertIn("class SecondDocument", output)

    def test_quarantines_fragment_generics_but_preserves_fragment_identity(
        self,
    ) -> None:
        source = (
            "from graphql_desk.fragment_refs import FixtureDocument as "
            "__graphql_desk_FixtureDocument\n"
            "from graphql_desk.fragment_refs import ReadableFragmentDocument as "
            "__graphql_desk_FragmentDocument\n"
            "class FieldsDocument(__graphql_desk_FragmentDocument["
            "FieldsIdentity, FieldsResult, FieldsVariables, FieldsInput], "
            "__graphql_desk_FixtureDocument["
            "FieldsResult, FieldsVariables, FieldsFixtureInput]): ...\n"
        )
        identity = owner(10)
        result = quarantine_generated_stubs(
            (GeneratedFile("generated/d1.pyi", source),),
            (operation(identity, "FieldsDocument", path="generated/d1.pyi"),),
            (identity,),
        )[0].text
        fragment = base_arguments(
            result, "FieldsDocument", "__graphql_desk_FragmentDocument"
        )
        self.assertEqual(ast.unparse(fragment[0]), "FieldsIdentity")
        self.assertTrue(all(is_object(arg) for arg in fragment[1:]))
        fixture = base_arguments(
            result, "FieldsDocument", "__graphql_desk_FixtureDocument"
        )
        self.assertTrue(all(is_object(arg) for arg in fixture))

    def test_quarantines_all_records_in_a_canonical_declaration_group(self) -> None:
        source = (
            "from graphql_desk.fragment_refs import FixtureDocument as "
            "__graphql_desk_FixtureDocument\n"
            "class FirstDocument(__graphql_desk_FixtureDocument[A, B, C]): ...\n"
            "class AliasDocument(__graphql_desk_FixtureDocument[D, E, F]): ...\n"
        )
        first, alias = owner(1), owner(20)
        canonical = owner(100)
        result = quarantine_generated_stubs(
            (GeneratedFile("generated/d0.pyi", source),),
            (
                operation(first, "FirstDocument", canonical_owner=canonical),
                operation(alias, "AliasDocument", canonical_owner=canonical),
            ),
            (first,),
        )[0].text
        for symbol in ("FirstDocument", "AliasDocument"):
            self.assertTrue(
                all(
                    is_object(arg)
                    for arg in base_arguments(
                        result, symbol, "__graphql_desk_FixtureDocument"
                    )
                )
            )

    def test_unknown_ambiguous_missing_and_unsupported_records_fail_closed(
        self,
    ) -> None:
        identity = owner(1)
        record = operation(identity, "QueryDocument")
        missing_file = GeneratedFile("generated/other.pyi", "class Other: ...\n")
        with self.assertRaises(ValueError):
            quarantine_generated_stubs((missing_file,), (record,), (identity,))
        with self.assertRaises(ValueError):
            quarantine_generated_stubs((missing_file,), (record,), (owner(50),))

        ambiguous = GeneratedFile(
            "generated/d0.pyi",
            "class QueryDocument(Fixture[Result, Variables, Input]): ...\n"
            "class QueryDocument(Fixture[Other, Vars, Data]): ...\n",
        )
        with self.assertRaises(ValueError):
            quarantine_generated_stubs((ambiguous,), (record,), (identity,))

        unsupported = GeneratedFile(
            "generated/d0.pyi", "class QueryDocument(Other[Result]): ...\n"
        )
        with self.assertRaises(ValueError):
            quarantine_generated_stubs((unsupported,), (record,), (identity,))

    def test_empty_selection_is_a_no_op(self) -> None:
        file = GeneratedFile("generated/d0.pyi", "class QueryDocument: ...\n")
        self.assertEqual(
            quarantine_generated_stubs((file,), (), ()),
            (file,),
        )


if __name__ == "__main__":
    unittest.main()
