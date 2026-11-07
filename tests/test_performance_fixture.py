"""Verify the benchmark corpus without timing the product."""

import ast
import hashlib
import unittest
from collections import Counter

from graphql import (
    FragmentDefinitionNode,
    FragmentSpreadNode,
    OperationDefinitionNode,
    OperationType,
    parse,
)

from scripts.make_performance_fixture import build_fixture


class PerformanceFixtureTests(unittest.TestCase):
    def test_contract_profiles_are_exact_and_reproducible(self) -> None:
        for profile, schemas, modules, type_count in (
            ("small", 1, 100, 500),
            ("large", 2, 2000, 2000),
        ):
            with self.subTest(profile=profile):
                files, manifest = build_fixture(profile)
                self.assertEqual((files, manifest), build_fixture(profile))
                self.assertEqual(manifest["seed"], 1)
                self.assertEqual(
                    manifest["sha256"],
                    {
                        name: hashlib.sha256(data).hexdigest()
                        for name, data in files.items()
                    },
                )
                schema_sources = [
                    data for name, data in files.items() if name.endswith(".graphql")
                ]
                self.assertEqual(len(schema_sources), schemas)
                for data in schema_sources:
                    self.assertEqual(
                        len(parse(data.decode("utf-8")).definitions), type_count
                    )

                variants: Counter[str] = Counter()
                operations: list[int] = []
                fragments: dict[str, str | None] = {}
                sources = [data for name, data in files.items() if name.endswith(".py")]
                self.assertEqual(len(sources), modules)
                for data in sources:
                    newline = "crlf" if b"\r\n" in data else "lf"
                    encoding = "ascii" if data.isascii() else "unicode"
                    variants[f"{newline}_{encoding}"] += 1
                    previous_operations = len(operations)
                    for statement in ast.parse(data).body:
                        if not isinstance(statement, ast.Assign):
                            continue
                        call = statement.value
                        assert isinstance(call, ast.Call) and isinstance(
                            call.func, ast.Name
                        )
                        self.assertEqual(call.func.id, "graphql")
                        literal = call.args[0]
                        assert isinstance(literal, ast.Constant) and isinstance(
                            literal.value, str
                        )
                        definitions = parse(literal.value, no_location=True).definitions
                        self.assertEqual(len(definitions), 1)
                        definition = definitions[0]
                        if isinstance(definition, OperationDefinitionNode):
                            self.assertEqual(definition.operation, OperationType.QUERY)
                            self.assertIsNotNone(definition.name)
                            operations.append(len(literal.value.encode("utf-8")))
                            continue
                        assert isinstance(definition, FragmentDefinitionNode)
                        edges = [
                            item.name.value
                            for item in definition.selection_set.selections
                            if isinstance(item, FragmentSpreadNode)
                        ]
                        self.assertLessEqual(len(edges), 1)
                        self.assertNotIn(definition.name.value, fragments)
                        fragments[definition.name.value] = edges[0] if edges else None
                    self.assertEqual(len(operations), previous_operations + 1)

                self.assertEqual(len(operations), modules)
                self.assertEqual(len(fragments), modules // 2)
                self.assertEqual(manifest["python_modules"], modules)
                self.assertEqual(manifest["operations"], len(operations))
                self.assertEqual(manifest["fragments"], len(fragments))
                self.assertEqual(variants, manifest["python_source_variants"])
                self.assertEqual(set(variants.values()), {modules // 4})
                self.assertTrue(all(2000 <= size <= 2100 for size in operations))
                self.assertEqual(
                    manifest["operation_utf8_bytes"],
                    {
                        "min": min(operations),
                        "max": max(operations),
                        "total": sum(operations),
                    },
                )
                depths: list[int] = []
                for start in fragments:
                    current: str | None = start
                    visited: set[str] = set()
                    while current is not None:
                        self.assertNotIn(current, visited)
                        visited.add(current)
                        current = fragments[current]
                    depths.append(len(visited))
                self.assertEqual(max(depths), 10)
                self.assertEqual(
                    manifest["max_fragment_chain_definitions"], max(depths)
                )
