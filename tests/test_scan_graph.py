import ast
import unittest
from dataclasses import replace
from pathlib import Path

from graphql import (
    FragmentDefinitionNode,
    OperationDefinitionNode,
    build_schema,
    parse,
    print_ast,
)

from graphql_desk.fragments import (
    DefinitionFact,
    FragmentIndex,
    IndexedDocument,
    SchemaContext,
    index_fragments,
)
from graphql_desk.scan_graph import build_scan_graph
from graphql_desk.source import Span
from graphql_desk.source_graph import (
    DocumentIdentity,
    FragmentReference,
    GraphDiagnostic,
    ModuleDependency,
    ModuleSnapshot,
    ProjectSources,
    ResolvedCall,
    StringContribution,
    resolve_module_imports,
)


def indexed_source(
    project: str,
    module: str,
    source: str,
    graph: str,
    schema: str,
) -> tuple[ModuleSnapshot, ResolvedCall, IndexedDocument]:
    snapshot = ModuleSnapshot(
        project, module, f"file:///fixture/{project}/{module}.py", 1, source
    )
    dependency = ModuleDependency(project, module, snapshot.uri, 1)
    tree = ast.parse(source)
    literal = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    )
    line_starts = [0]
    for line in source.splitlines(keepends=True):
        line_starts.append(line_starts[-1] + len(line))
    assert literal.end_lineno is not None
    assert literal.end_col_offset is not None
    literal_range = Span(
        line_starts[literal.lineno - 1] + literal.col_offset,
        line_starts[literal.end_lineno - 1] + literal.end_col_offset,
    )
    call_range = Span(0, len(source))
    call = ResolvedCall(
        dependency,
        call_range,
        graph,
        (StringContribution(dependency, literal_range, graph),),
        (dependency,),
        schema_name=schema,
    )
    identity = DocumentIdentity(dependency, call_range)
    nodes = tuple(
        node
        for node in parse(graph).definitions
        if isinstance(node, (OperationDefinitionNode, FragmentDefinitionNode))
    )
    definitions = tuple(
        DefinitionFact(
            identity,
            "fragment"
            if isinstance(node, FragmentDefinitionNode)
            else node.operation.value,
            node.name.value if node.name else None,
            None,
            print_ast(node),
        )
        for node in nodes
    )
    indexed = IndexedDocument(
        call,
        (project, schema),
        "schema-token",
        next((fact for fact in definitions if fact.kind != "fragment"), None),
        next((fact for fact in definitions if fact.kind == "fragment"), None),
        definitions,
        (dependency,),
    )
    return snapshot, call, indexed


class ScanGraphTests(unittest.TestCase):
    def test_duplicate_module_and_document_identities_fail_closed(self) -> None:
        module = ModuleSnapshot("app", "queries", "file:///queries.py", 1, "pass\n")
        duplicate_module = replace(module, uri="file:///other-queries.py", version=2)
        with self.assertRaisesRegex(ValueError, "unique project/module identities"):
            build_scan_graph(
                ProjectSources((), ()),
                FragmentIndex((), ()),
                (module, duplicate_module),
                paths={("app", "queries"): Path("queries.py")},
                roots={"app": Path.cwd() / "scan-root"},
            )

        graph = "query Get { hello }"
        snapshot, call, indexed = indexed_source(
            "app", "queries", f"doc = graphql({graph!r})\n", graph, "shop"
        )
        with self.assertRaisesRegex(ValueError, "unique identities"):
            build_scan_graph(
                ProjectSources((call,), ()),
                FragmentIndex((indexed, indexed), ()),
                (snapshot,),
                paths={("app", "queries"): Path("queries.py")},
                roots={"app": Path.cwd() / "scan-root"},
            )

    def test_definitions_are_schema_qualified_deterministic_and_located(self) -> None:
        graph = (
            '\nquery Same($id: ID! = "x") { user(id: $id) { ...UserBits } }\n'
            "fragment UserBits on User { id }\n"
        )
        first = indexed_source(
            "app", "queries.one", f"doc = graphql({graph!r})\n", graph, "shop"
        )
        duplicate = indexed_source(
            "app",
            "queries.two",
            f"doc = graphql({graph!r})\n",
            graph,
            "shop",
        )
        other_schema = indexed_source(
            "app",
            "admin_query",
            f'doc = graphql("""{graph}""")\n',
            graph,
            "admin",
        )
        facts = (first, duplicate, other_schema)
        modules = tuple(item[0] for item in facts)
        sources = ProjectSources(tuple(item[1] for item in facts), ())
        index = FragmentIndex(tuple(item[2] for item in facts), ())
        root = Path.cwd() / "scan-fixture"
        paths = {
            ("app", snapshot.module): Path(f"{snapshot.module.replace('.', '/')}.py")
            for snapshot in modules
        }
        result = build_scan_graph(
            sources,
            index,
            modules,
            paths=paths,
            roots={"app": root},
        )
        reordered = build_scan_graph(
            ProjectSources(tuple(reversed(sources.calls)), ()),
            FragmentIndex(tuple(reversed(index.documents)), ()),
            tuple(reversed(modules)),
            paths=paths,
            roots={"app": root},
        )
        self.assertEqual(result, reordered)
        queries = [item for item in result.definitions if item.kind == "query"]
        self.assertEqual({item.schema_name for item in queries}, {"shop", "admin"})
        self.assertEqual(len({item.id for item in result.definitions}), 6)
        self.assertEqual(
            [item.id for item in queries if item.schema_name == "shop"],
            [
                "app:shop:query:Same",
                "app:shop:query:Same~2",
            ],
        )
        query = next(item for item in queries if item.schema_name == "admin")
        self.assertEqual(query.variables, (("id", "ID!", '"x"'),))
        self.assertEqual(query.fragment_spreads, ("UserBits",))
        import hashlib

        expected_hash = hashlib.sha256(
            print_ast(parse(graph).definitions[0]).encode("utf-8")
        ).hexdigest()[:16]
        self.assertEqual(query.hash, expected_hash)
        self.assertTrue(query.locations)
        self.assertEqual(query.locations[0].uri, other_schema[0].uri)
        self.assertEqual(query.locations[0].line, 2)

    def test_import_reachability_handles_entrypoint_override_and_closed_cycle(
        self,
    ) -> None:
        modules = (
            ModuleSnapshot(
                "app",
                "main",
                "file:///main.py",
                1,
                "from pkg import a\nimport left\nimport right\n",
            ),
            ModuleSnapshot("app", "pkg", "file:///pkg/__init__.py", 1, "", True),
            ModuleSnapshot("app", "pkg.a", "file:///pkg/a.py", 1, "from . import b\n"),
            ModuleSnapshot("app", "pkg.b", "file:///pkg/b.py", 1, "from . import a\n"),
            ModuleSnapshot(
                "app", "cycle.a", "file:///cycle/a.py", 1, "from . import b\n"
            ),
            ModuleSnapshot(
                "app", "cycle.b", "file:///cycle/b.py", 1, "from . import a\n"
            ),
            ModuleSnapshot(
                "app", "left", "file:///left.py", 1, "from leaf import item\n"
            ),
            ModuleSnapshot(
                "app", "right", "file:///right.py", 1, "from leaf import item\n"
            ),
            ModuleSnapshot("app", "leaf", "file:///leaf.py", 1, "item = 1\n"),
            ModuleSnapshot("app", "isolated", "file:///isolated.py", 1, "pass\n"),
            ModuleSnapshot(
                "app",
                "self_import",
                "file:///self_import.py",
                1,
                "import self_import\n",
            ),
        )
        root = Path.cwd() / "scan-root"
        paths = {
            ("app", item.module): Path(
                {
                    "pkg": "pkg/__init__.py",
                    "pkg.a": "pkg/a.py",
                    "pkg.b": "pkg/b.py",
                    "cycle.a": "cycle/a.py",
                    "cycle.b": "cycle/b.py",
                }.get(item.module, f"{item.module}.py")
            )
            for item in modules
        }
        sources = ProjectSources((), ())
        empty_index = FragmentIndex((), ())
        default = build_scan_graph(
            sources, empty_index, modules, paths=paths, roots={"app": root}
        )
        by_name = {item.name: item for item in default.modules}
        self.assertEqual(by_name["main"].distance, 0)
        self.assertEqual(by_name["pkg.a"].distance, 1)
        self.assertEqual(by_name["pkg.b"].distance, 2)
        self.assertEqual(by_name["leaf"].distance, 2)
        self.assertEqual(by_name["leaf"].imported_by, ("left", "right"))
        self.assertIsNone(by_name["cycle.a"].distance)
        self.assertEqual(by_name["isolated"].distance, 0)
        self.assertFalse(by_name["self_import"].entrypoint)
        self.assertIsNone(by_name["self_import"].distance)
        self.assertEqual(
            by_name["pkg.b"].reverse_reachable,
            ("main", "pkg.a", "pkg.b"),
        )
        self.assertEqual(by_name["pkg.b"].area, "pkg")
        overridden = build_scan_graph(
            sources,
            empty_index,
            modules,
            paths=paths,
            roots={"app": root},
            entrypoints={"app": ("main.py",)},
        )
        self.assertTrue(
            next(item for item in overridden.modules if item.name == "main").entrypoint
        )
        self.assertFalse(
            next(
                item for item in overridden.modules if item.name == "isolated"
            ).entrypoint
        )
        with self.assertRaisesRegex(ValueError, "unknown or excluded"):
            build_scan_graph(
                sources,
                empty_index,
                modules,
                paths=paths,
                roots={"app": root},
                entrypoints={"app": ("missing.py",)},
            )
        under_source_root = build_scan_graph(
            sources,
            empty_index,
            modules,
            paths={key: Path("src") / path for key, path in paths.items()},
            roots={"app": root},
            source_roots={"app": (Path("src"),)},
            entrypoints={"app": ("main.py",)},
        )
        main = next(item for item in under_source_root.modules if item.name == "main")
        self.assertTrue(main.entrypoint)
        with_root_and_nested_roots = build_scan_graph(
            sources,
            empty_index,
            modules,
            paths={key: Path("src") / path for key, path in paths.items()},
            roots={"app": root},
            source_roots={"app": (Path("."), Path("src"))},
            entrypoints={"app": ("main.py",)},
        )
        self.assertTrue(
            next(
                item
                for item in with_root_and_nested_roots.modules
                if item.name == "main"
            ).entrypoint
        )
        with self.assertRaisesRegex(ValueError, "unknown or excluded"):
            build_scan_graph(
                sources,
                empty_index,
                modules,
                paths={key: Path("src") / path for key, path in paths.items()},
                roots={"app": root},
                source_roots={"app": (Path("src"),)},
                entrypoints={"app": ("src/main.py",)},
            )
        ambiguous_modules = (
            ModuleSnapshot("app", "main", "file:///main.py", 1, "pass\n"),
            ModuleSnapshot("app", "src.main", "file:///src/main.py", 1, "pass\n"),
        )
        with self.assertRaisesRegex(ValueError, "unknown or excluded"):
            build_scan_graph(
                sources,
                empty_index,
                ambiguous_modules,
                paths={
                    ("app", "main"): Path("main.py"),
                    ("app", "src.main"): Path("src/main.py"),
                },
                roots={"app": root},
                source_roots={"app": (Path("."), Path("src"))},
                entrypoints={"app": ("main.py",)},
            )
        with self.assertRaisesRegex(ValueError, "escapes"):
            build_scan_graph(
                sources,
                empty_index,
                modules,
                paths={**paths, ("app", "main"): Path("../outside.py")},
                roots={"app": root},
            )

    def test_conflicting_same_schema_fragments_are_reported_without_resolution(
        self,
    ) -> None:
        operation = indexed_source(
            "app",
            "queries",
            'doc = graphql("query UsesShared { user { ...Shared } }")\n',
            "query UsesShared { user { ...Shared } }",
            "shop",
        )
        first = indexed_source(
            "app",
            "fragments.one",
            'doc = graphql("fragment Shared on User { id }")\n',
            "fragment Shared on User { id }",
            "shop",
        )
        second = indexed_source(
            "app",
            "fragments.two",
            'doc = graphql("fragment Shared on User { name }")\n',
            "fragment Shared on User { name }",
            "shop",
        )
        facts = (operation, first, second)
        snapshots = tuple(item[0] for item in facts)
        result = build_scan_graph(
            ProjectSources(tuple(item[1] for item in facts), ()),
            FragmentIndex(tuple(item[2] for item in facts), ()),
            snapshots,
            paths={
                ("app", snapshot.module): Path(
                    f"{snapshot.module.replace('.', '/')}.py"
                )
                for snapshot in snapshots
            },
            roots={"app": Path.cwd() / "scan-root"},
        )
        self.assertEqual(
            [item.code for item in result.diagnostics],
            ["ambiguous-fragment-name", "ambiguous-fragment-name"],
        )
        self.assertEqual(
            [item.name for item in result.definitions],
            ["Shared", "Shared", "UsesShared"],
        )
        self.assertEqual(
            [item.id for item in result.definitions],
            [
                "app:shop:fragment:Shared",
                "app:shop:fragment:Shared~2",
                "app:shop:query:UsesShared",
            ],
        )
        self.assertEqual(result.fragment_edges, ())
        stale = replace(first[1], source="fragment Shared on User { email }")
        stale_result = build_scan_graph(
            ProjectSources((stale,), ()),
            FragmentIndex((first[2],), ()),
            (first[0],),
            paths={("app", "fragments.one"): Path("fragments/one.py")},
            roots={"app": Path.cwd() / "scan-root"},
        )
        self.assertEqual(stale_result.definitions, ())
        self.assertEqual(stale_result.diagnostics[0].code, "stale-scan-index")

    def test_fragment_spreads_resolve_to_schema_qualified_edges(self) -> None:
        operation = indexed_source(
            "app",
            "queries",
            'doc = graphql("query Get { user { ...UserFields } }")\n',
            "query Get { user { ...UserFields } }",
            "shop",
        )
        shop_fragment = indexed_source(
            "app",
            "shop_fragments",
            'doc = graphql("fragment UserFields on User { id }")\n',
            "fragment UserFields on User { id }",
            "shop",
        )
        unimported_fragment = indexed_source(
            "app",
            "unused_fragments",
            'doc = graphql("fragment UserFields on User { email }")\n',
            "fragment UserFields on User { email }",
            "shop",
        )
        admin_fragment = indexed_source(
            "app",
            "admin_fragments",
            'doc = graphql("fragment UserFields on User { name }")\n',
            "fragment UserFields on User { name }",
            "admin",
        )
        operation_call = replace(
            operation[1],
            fragments=(
                FragmentReference(
                    DocumentIdentity(shop_fragment[1].module, shop_fragment[1].range),
                    operation[1].module,
                    Span(0, 1),
                ),
            ),
        )
        operation_index = replace(
            operation[2],
            call=operation_call,
            definitions=(*operation[2].definitions, *shop_fragment[2].definitions),
        )
        facts = (operation, shop_fragment, unimported_fragment, admin_fragment)
        modules = tuple(item[0] for item in facts)
        result = build_scan_graph(
            ProjectSources((operation_call, *(item[1] for item in facts[1:])), ()),
            FragmentIndex((operation_index, *(item[2] for item in facts[1:])), ()),
            modules,
            paths={
                ("app", snapshot.module): Path(f"{snapshot.module}.py")
                for snapshot in modules
            },
            roots={"app": Path.cwd() / "scan-root"},
        )
        operation_id = next(
            item.id for item in result.definitions if item.name == "Get"
        )
        shop_fragment_id = next(
            item.id
            for item in result.definitions
            if item.kind == "fragment" and item.schema_name == "shop"
        )
        self.assertEqual(
            [(edge.source_id, edge.target_id) for edge in result.fragment_edges],
            [(operation_id, shop_fragment_id)],
        )

    def test_definition_location_preserves_crlf_python_source(self) -> None:
        graph = "\nquery Get { user { id } }\n"
        snapshot, call, indexed = indexed_source(
            "app",
            "queries",
            f'doc = graphql("""{graph}""")\n'.replace("\n", "\r\n"),
            graph,
            "shop",
        )
        result = build_scan_graph(
            ProjectSources((call,), ()),
            FragmentIndex((indexed,), ()),
            (snapshot,),
            paths={("app", "queries"): Path("queries.py")},
            roots={"app": Path.cwd() / "scan-root"},
        )
        self.assertEqual(result.definitions[0].locations[0].line, 2)

    def test_definition_module_is_call_owner_when_literal_is_imported(self) -> None:
        graph = "query Get { hello }"
        origin, _, indexed = indexed_source(
            "app", "documents", f"doc = graphql({graph!r})\n", graph, "shop"
        )
        owner_source = "from documents import doc\n"
        owner = ModuleSnapshot(
            "app", "queries", "file:///fixture/app/queries.py", 1, owner_source
        )
        owner_dependency = ModuleDependency("app", "queries", owner.uri, 1)
        origin_dependency = ModuleDependency(
            "app", "documents", origin.uri, origin.version
        )
        call = replace(
            indexed.call,
            module=owner_dependency,
            range=Span(0, len(owner_source)),
            dependencies=(owner_dependency, origin_dependency),
        )
        indexed = replace(indexed, call=call)
        result = build_scan_graph(
            ProjectSources((call,), ()),
            FragmentIndex((indexed,), ()),
            (owner, origin),
            paths={
                ("app", "queries"): Path("queries.py"),
                ("app", "documents"): Path("documents.py"),
            },
            roots={"app": Path.cwd() / "scan-root"},
        )
        self.assertEqual(result.definitions[0].module, "queries")
        self.assertEqual(result.definitions[0].locations[0].module, "documents")

    def test_duplicate_imported_definition_ids_follow_call_source_order(self) -> None:
        graph = "query Same { user { ...Shared } }"
        origin, origin_call, origin_index = indexed_source(
            "app", "documents", f"doc = {graph!r}\n", graph, "shop"
        )
        first_fragment = indexed_source(
            "app",
            "first_fragment",
            'doc = "fragment Shared on User { id }"\n',
            "fragment Shared on User { id }",
            "shop",
        )
        second_fragment = indexed_source(
            "app",
            "second_fragment",
            'doc = "fragment Shared on User { name }"\n',
            "fragment Shared on User { name }",
            "shop",
        )
        consumer_source = (
            "from documents import doc\nfirst = gql(doc)\nsecond = gql(doc)\n"
        )
        consumer = ModuleSnapshot(
            "app", "queries", "file:///fixture/app/queries.py", 1, consumer_source
        )
        consumer_dependency = ModuleDependency(
            consumer.project, consumer.module, consumer.uri, consumer.version
        )
        origin_dependency = origin_call.module
        assert origin_call.contributions
        call_spans = tuple(
            Span(
                consumer_source.index(f"{name} = gql(doc)"),
                consumer_source.index(f"{name} = gql(doc)") + len(f"{name} = gql(doc)"),
            )
            for name in ("first", "second")
        )
        base_calls = tuple(
            replace(
                origin_call,
                module=consumer_dependency,
                range=span,
                dependencies=(
                    consumer_dependency,
                    origin_dependency,
                    (first_fragment if name == "first" else second_fragment)[1].module,
                ),
                contributions=(
                    StringContribution(
                        origin_dependency,
                        origin_call.contributions[0].range,
                        graph,
                    ),
                ),
                fragments=(
                    FragmentReference(
                        DocumentIdentity(
                            (first_fragment if name == "first" else second_fragment)[
                                1
                            ].module,
                            (first_fragment if name == "first" else second_fragment)[
                                1
                            ].range,
                        ),
                        consumer_dependency,
                        Span(0, 1),
                    ),
                ),
            )
            for name, span in zip(("first", "second"), call_spans)
        )
        indexed_documents: list[IndexedDocument] = []
        for name, call in zip(("first", "second"), base_calls):
            fragment_document = first_fragment if name == "first" else second_fragment
            identity = DocumentIdentity(call.module, call.range)
            definitions = (
                *tuple(
                    replace(fact, document=identity)
                    for fact in origin_index.definitions
                ),
                *fragment_document[2].definitions,
            )
            indexed_documents.append(
                replace(
                    origin_index,
                    call=call,
                    selected_operation=next(
                        fact for fact in definitions if fact.kind != "fragment"
                    ),
                    definitions=definitions,
                )
            )
        calls = (*base_calls, first_fragment[1], second_fragment[1])
        indexed_documents.extend((first_fragment[2], second_fragment[2]))
        modules = (consumer, origin, first_fragment[0], second_fragment[0])
        paths = {
            ("app", "queries"): Path("queries.py"),
            ("app", "documents"): Path("documents.py"),
            ("app", "first_fragment"): Path("first_fragment.py"),
            ("app", "second_fragment"): Path("second_fragment.py"),
        }
        roots = {"app": Path.cwd() / "scan-root"}
        result = build_scan_graph(
            ProjectSources(calls, ()),
            FragmentIndex(tuple(indexed_documents), ()),
            modules,
            paths=paths,
            roots=roots,
        )
        reordered = build_scan_graph(
            ProjectSources(tuple(reversed(calls)), ()),
            FragmentIndex(tuple(reversed(indexed_documents)), ()),
            modules,
            paths=paths,
            roots=roots,
        )
        self.assertEqual(result, reordered)
        self.assertEqual(
            [item.id for item in result.definitions if item.kind == "query"],
            ["app:shop:query:Same", "app:shop:query:Same~2"],
        )
        self.assertEqual(
            [(edge.source_id, edge.target_id) for edge in result.fragment_edges],
            [
                ("app:shop:query:Same", "app:shop:fragment:Shared"),
                ("app:shop:query:Same~2", "app:shop:fragment:Shared~2"),
            ],
        )

    def test_diagnostics_shared_with_fragment_index_are_emitted_once(self) -> None:
        graph = "query Get { hello }"
        snapshot, call, _ = indexed_source(
            "app", "queries", f"doc = graphql({graph!r})\n", graph, "shop"
        )
        shared = GraphDiagnostic(
            call.module,
            "source-note",
            "shared diagnostic",
            Span(0, 1),
        )
        sources = ProjectSources((call,), (shared,))
        index = index_fragments(
            sources,
            schemas={
                ("app", "shop"): SchemaContext(
                    "shop", build_schema("type Query { hello: String }"), "token"
                )
            },
        )
        result = build_scan_graph(
            sources,
            index,
            (snapshot,),
            paths={("app", "queries"): Path("queries.py")},
            roots={"app": Path.cwd() / "scan-root"},
        )
        self.assertEqual(result.diagnostics.count(shared), 1)

    def test_module_import_resolver_ignores_external_modules(self) -> None:
        module = ModuleSnapshot(
            "app", "main", "file:///main.py", 1, "import os\nfrom ext import X\n"
        )
        self.assertEqual(resolve_module_imports((module,)), ())
