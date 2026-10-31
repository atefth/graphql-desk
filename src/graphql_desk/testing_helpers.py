"""Static registry facts for fixture helpers; source is never executed."""

from __future__ import annotations

import ast
import hashlib
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .config import ProjectConfig, SchemaConfig
from .fragments import FragmentIndex, IndexedDocument, SchemaContext
from .operation_types import render_indexed_operation_types
from .source import Span
from .source_graph import (
    DocumentIdentity,
    GraphDiagnostic,
    ModuleDependency,
    ModuleSnapshot,
)

if TYPE_CHECKING:
    from .factory_generation import OperationProvenance


@dataclass(frozen=True)
class TestingHelperCall:
    module: ModuleDependency
    span: Span
    helper: str
    document: IndexedDocument | None
    fragments: tuple[IndexedDocument, ...]


@dataclass(frozen=True)
class TestingHelperFacts:
    calls: tuple[TestingHelperCall, ...]
    diagnostics: tuple[GraphDiagnostic, ...]


@dataclass(frozen=True)
class TestingHelperArtifacts:
    """Per-callsite type modules and partial package overlays."""

    files: tuple[tuple[str, str], ...]
    read_result_stub: str
    mask_fragments_stub: str
    diagnostics: tuple[GraphDiagnostic, ...]


@dataclass(frozen=True)
class _Import:
    module: str
    symbol: str


_Documents = dict[tuple[str, str, str], tuple[IndexedDocument, ...]]


def _absolute_import(snapshot: ModuleSnapshot, node: ast.ImportFrom) -> str:
    if not node.level:
        return node.module or ""
    package = snapshot.module.split(".")
    if not snapshot.is_package:
        package.pop()
    if node.level > len(package):
        return ""
    base = package[: len(package) - node.level + 1]
    if node.module:
        base.extend(node.module.split("."))
    return ".".join(base)


def _attribute_parts(node: ast.expr) -> tuple[str, tuple[str, ...]] | None:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    return node.id, tuple(reversed(parts))


class _ModuleFacts:
    def __init__(self, snapshot: ModuleSnapshot, tree: ast.Module) -> None:
        self.snapshot = snapshot
        self.lines = snapshot.source.splitlines(keepends=True)
        self.line_starts = [0]
        for line in self.lines:
            self.line_starts.append(self.line_starts[-1] + len(line))
        self.imports: dict[str, _Import] = {}
        self.aliases: dict[str, ast.expr] = {}
        self.helpers: dict[str, str] = {}
        self.parents = {
            child: parent
            for parent in ast.walk(tree)
            for child in ast.iter_child_nodes(parent)
        }
        bindings = _Bindings()
        bindings.visit(tree)
        self.ambiguous = {name for name, count in bindings.counts.items() if count > 1}
        for statement in tree.body:
            if isinstance(statement, ast.Import):
                for item in statement.names:
                    local = item.asname or item.name.split(".")[0]
                    module = item.name if item.asname else item.name.split(".")[0]
                    self.imports[local] = _Import(module, "")
            elif isinstance(statement, ast.ImportFrom):
                module = _absolute_import(snapshot, statement)
                for item in statement.names:
                    if item.name == "*":
                        continue
                    local = item.asname or item.name
                    self.imports[local] = _Import(module, item.name)
            elif isinstance(statement, ast.Assign) and len(statement.targets) == 1:
                if isinstance(statement.targets[0], ast.Name):
                    self.aliases[statement.targets[0].id] = statement.value
            elif isinstance(statement, ast.AnnAssign):
                if (
                    isinstance(statement.target, ast.Name)
                    and statement.value is not None
                ):
                    self.aliases[statement.target.id] = statement.value
        for local, target in self.imports.items():
            if target == _Import("graphql_desk.testing", "read_result"):
                self.helpers[local] = "read_result"
            elif target == _Import("graphql_desk.testing", "mask_fragments"):
                self.helpers[local] = "mask_fragments"
            elif target == _Import("graphql_desk", "mask_fragments"):
                self.helpers[local] = "mask_fragments"

    def is_shadowed(self, node: ast.AST, name: str) -> bool:
        parent = self.parents.get(node)
        function_scope = False
        while parent is not None:
            if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef)):
                function_scope = True
                if name in _local_bindings(parent):
                    return True
            elif isinstance(parent, ast.Lambda):
                function_scope = True
                if name in _lambda_bindings(parent):
                    return True
            elif isinstance(parent, ast.ClassDef) and not function_scope:
                if name in _class_bindings(parent):
                    return True
            elif isinstance(
                parent, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)
            ):
                if name in _comprehension_bindings(parent):
                    return True
            parent = self.parents.get(parent)
        return False


class _Bindings(ast.NodeVisitor):
    """Count module-scope bindings while excluding nested lexical scopes."""

    def __init__(self) -> None:
        self.counts: dict[str, int] = {}

    def add(self, name: str) -> None:
        self.counts[name] = self.counts.get(name, 0) + 1

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, (ast.Store, ast.Del)):
            self.add(node.id)

    def visit_Import(self, node: ast.Import) -> None:
        for item in node.names:
            self.add(item.asname or item.name.split(".")[0])

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        for item in node.names:
            if item.name != "*":
                self.add(item.asname or item.name)

    def visit_FunctionDef(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self.add(node.name)
        for item in (*node.decorator_list, node.args, node.returns):
            if item is not None:
                self.visit(item)

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.add(node.name)
        for item in (*node.decorator_list, *node.bases, *node.keywords):
            self.visit(item)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        for item in (*node.args.defaults, *node.args.kw_defaults):
            if item is not None:
                self.visit(item)

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if node.name:
            self.add(node.name)
        self.generic_visit(node)

    def visit_MatchAs(self, node: ast.MatchAs) -> None:
        if node.name:
            self.add(node.name)
        if node.pattern:
            self.visit(node.pattern)

    def visit_MatchStar(self, node: ast.MatchStar) -> None:
        if node.name:
            self.add(node.name)

    def visit_MatchMapping(self, node: ast.MatchMapping) -> None:
        if node.rest:
            self.add(node.rest)
        self.generic_visit(node)


def _scope_bindings(node: ast.AST) -> set[str]:
    class Names(ast.NodeVisitor):
        def __init__(self) -> None:
            self.names: set[str] = set()

        def visit_Name(self, node: ast.Name) -> None:
            if isinstance(node.ctx, (ast.Store, ast.Del)):
                self.names.add(node.id)

        def visit_arg(self, node: ast.arg) -> None:
            self.names.add(node.arg)

        def visit_Import(self, node: ast.Import) -> None:
            self.names.update(
                alias.asname or alias.name.split(".")[0] for alias in node.names
            )

        def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
            self.names.update(
                alias.asname or alias.name for alias in node.names if alias.name != "*"
            )

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            if node is scope:
                for statement in node.body:
                    self.visit(statement)
            else:
                self.names.add(node.name)

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            if node is scope:
                for statement in node.body:
                    self.visit(statement)
            else:
                self.names.add(node.name)

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            if node is scope:
                for statement in node.body:
                    self.visit(statement)
            else:
                self.names.add(node.name)

        def visit_Lambda(self, node: ast.Lambda) -> None:
            if node is scope:
                self.visit(node.body)

        def visit_Global(self, node: ast.Global) -> None:
            self.names.update(node.names)

        def visit_Nonlocal(self, node: ast.Nonlocal) -> None:
            self.names.update(node.names)

        def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
            if node.name:
                self.names.add(node.name)
            for statement in node.body:
                self.visit(statement)

        def visit_MatchAs(self, node: ast.MatchAs) -> None:
            if node.name:
                self.names.add(node.name)
            if node.pattern:
                self.visit(node.pattern)

        def visit_MatchStar(self, node: ast.MatchStar) -> None:
            if node.name:
                self.names.add(node.name)

        def visit_MatchMapping(self, node: ast.MatchMapping) -> None:
            if node.rest:
                self.names.add(node.rest)
            self.generic_visit(node)

        def visit_ListComp(self, node: ast.ListComp) -> None:
            if node is scope:
                self.generic_visit(node)

        def visit_SetComp(self, node: ast.SetComp) -> None:
            if node is scope:
                self.generic_visit(node)

        def visit_DictComp(self, node: ast.DictComp) -> None:
            if node is scope:
                self.generic_visit(node)

        def visit_GeneratorExp(self, node: ast.GeneratorExp) -> None:
            if node is scope:
                self.generic_visit(node)

    scope = node
    visitor = Names()
    visitor.visit(node)
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        for argument in (
            *node.args.posonlyargs,
            *node.args.args,
            *node.args.kwonlyargs,
        ):
            visitor.names.add(argument.arg)
        if node.args.vararg is not None:
            visitor.names.add(node.args.vararg.arg)
        if node.args.kwarg is not None:
            visitor.names.add(node.args.kwarg.arg)
    elif isinstance(node, ast.Lambda):
        for argument in (
            *node.args.posonlyargs,
            *node.args.args,
            *node.args.kwonlyargs,
        ):
            visitor.names.add(argument.arg)
        if node.args.vararg is not None:
            visitor.names.add(node.args.vararg.arg)
        if node.args.kwarg is not None:
            visitor.names.add(node.args.kwarg.arg)
    return visitor.names


def _local_bindings(node: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    return _scope_bindings(node)


def _lambda_bindings(node: ast.Lambda) -> set[str]:
    return _scope_bindings(node)


def _class_bindings(node: ast.ClassDef) -> set[str]:
    return _scope_bindings(node)


def _comprehension_bindings(node: ast.AST) -> set[str]:
    generators = getattr(node, "generators", ())
    return {
        item.id
        for generator in generators
        for item in ast.walk(generator.target)
        if isinstance(item, ast.Name)
    }


def _resolve_doc(
    expression: ast.expr,
    current: _ModuleFacts,
    documents: _Documents,
    modules: dict[tuple[str, str], _ModuleFacts],
    seen: frozenset[tuple[str, str, str]] = frozenset(),
    use_site: ast.AST | None = None,
) -> IndexedDocument | None:
    if isinstance(expression, ast.Name):
        if (
            expression.id in current.ambiguous
            or use_site is not None
            and current.is_shadowed(use_site, expression.id)
        ):
            return None
        key = (current.snapshot.project, current.snapshot.module, expression.id)
        matches = documents.get(key, ())
        if len(matches) == 1:
            return matches[0]
        if matches:
            return None
        if key in seen:
            return None
        alias = current.aliases.get(expression.id)
        if alias is not None:
            return _resolve_doc(
                alias, current, documents, modules, seen | {key}, use_site
            )
        imported = current.imports.get(expression.id)
        if imported is not None:
            return _resolve_import_target(
                current.snapshot.project,
                imported.module,
                imported.symbol,
                documents,
                modules,
                seen | {key},
            )
        return None
    parts = _attribute_parts(expression)
    if parts is None:
        return None
    root, attributes = parts
    if (
        root in current.ambiguous
        or use_site is not None
        and current.is_shadowed(use_site, root)
    ):
        return None
    if root in current.aliases:
        resolved = _resolve_doc(
            current.aliases[root], current, documents, modules, seen, use_site
        )
        return resolved if not attributes else None
    imported = current.imports.get(root)
    if imported is None:
        return None
    if imported.symbol:
        symbol = ".".join((imported.symbol, *attributes))
        return _resolve_import_target(
            current.snapshot.project,
            imported.module,
            symbol,
            documents,
            modules,
            seen,
        )
    module = imported.module
    symbol_parts = list(attributes)
    for split in range(len(symbol_parts), -1, -1):
        target_module = ".".join((module, *symbol_parts[:split]))
        target_symbol = ".".join(symbol_parts[split:])
        if (current.snapshot.project, target_module) in modules:
            resolved = _resolve_import_target(
                current.snapshot.project,
                target_module,
                target_symbol,
                documents,
                modules,
                seen,
            )
            if resolved is not None:
                return resolved
    return None


def _resolve_import_target(
    project: str,
    module: str,
    symbol: str,
    documents: _Documents,
    modules: dict[tuple[str, str], _ModuleFacts],
    seen: frozenset[tuple[str, str, str]],
) -> IndexedDocument | None:
    key = (project, module, symbol)
    matches = documents.get(key, ())
    if len(matches) == 1:
        return matches[0]
    if matches:
        return None
    if key in seen:
        return None
    owner = modules.get((project, module))
    if owner is None or symbol in owner.ambiguous:
        return None
    imported = owner.imports.get(symbol)
    if imported is None:
        return None
    return _resolve_import_target(
        project,
        imported.module,
        imported.symbol,
        documents,
        modules,
        seen | {key},
    )


def _resolve_helper(
    expression: ast.expr, current: _ModuleFacts, use_site: ast.AST
) -> str | None:
    if isinstance(expression, ast.Name):
        if expression.id in current.ambiguous or current.is_shadowed(
            use_site, expression.id
        ):
            return None
        helper = current.helpers.get(expression.id)
        if helper is not None:
            return helper
        target = current.imports.get(expression.id)
        if target is not None:
            if target == _Import("graphql_desk.testing", "read_result"):
                return "read_result"
            if target in (
                _Import("graphql_desk.testing", "mask_fragments"),
                _Import("graphql_desk", "mask_fragments"),
            ):
                return "mask_fragments"
        return None
    parts = _attribute_parts(expression)
    if parts is None:
        return None
    root, attributes = parts
    if root in current.ambiguous or current.is_shadowed(use_site, root):
        return None
    target = current.imports.get(root)
    if target is None or not attributes:
        return None
    full = ".".join(
        part for part in (target.module, target.symbol, *attributes) if part
    )
    if full == "graphql_desk.testing.read_result":
        return "read_result"
    if full in ("graphql_desk.testing.mask_fragments", "graphql_desk.mask_fragments"):
        return "mask_fragments"
    return None


def _argument(node: ast.Call, position: int, name: str) -> ast.expr | None:
    if len(node.args) > position:
        return node.args[position]
    return next((item.value for item in node.keywords if item.arg == name), None)


def _tuple_registry(
    expression: ast.expr,
    current: _ModuleFacts,
    documents: _Documents,
    modules: dict[tuple[str, str], _ModuleFacts],
    seen: frozenset[tuple[str, str, str]] = frozenset(),
    use_site: ast.AST | None = None,
) -> tuple[IndexedDocument, ...] | None:
    if isinstance(expression, ast.Name):
        return _tuple_registry_name(
            expression.id, current, documents, modules, seen, use_site
        )
    parts = _attribute_parts(expression)
    if parts is not None:
        root, attributes = parts
        if root in current.ambiguous or current.is_shadowed(
            use_site or expression, root
        ):
            return None
        imported = current.imports.get(root)
        if imported is not None and attributes:
            if imported.symbol:
                target_module = imported.module
                target_symbol = ".".join((imported.symbol, *attributes))
            else:
                target_module = imported.module
                target_symbol = ""
                for split in range(len(attributes), -1, -1):
                    candidate = ".".join((imported.module, *attributes[:split]))
                    if (current.snapshot.project, candidate) in modules:
                        target_module = candidate
                        target_symbol = ".".join(attributes[split:])
                        break
            owner = modules.get((current.snapshot.project, target_module))
            if owner is not None and target_symbol:
                return _tuple_registry_name(
                    target_symbol,
                    owner,
                    documents,
                    modules,
                    seen,
                    None,
                )
    if not isinstance(expression, ast.Tuple) or any(
        isinstance(item, ast.Starred) for item in expression.elts
    ):
        return None
    values: list[IndexedDocument] = []
    for item in expression.elts:
        document = _resolve_doc(item, current, documents, modules, use_site=use_site)
        if document is None or document.primary_fragment is None:
            return None
        values.append(document)
    return tuple(values)


def _tuple_registry_name(
    name: str,
    current: _ModuleFacts,
    documents: _Documents,
    modules: dict[tuple[str, str], _ModuleFacts],
    seen: frozenset[tuple[str, str, str]],
    use_site: ast.AST | None,
) -> tuple[IndexedDocument, ...] | None:
    key = (current.snapshot.project, current.snapshot.module, name)
    if (
        key in seen
        or name in current.ambiguous
        or use_site is not None
        and current.is_shadowed(use_site, name)
    ):
        return None
    alias = current.aliases.get(name)
    if alias is not None:
        return _tuple_registry(
            alias, current, documents, modules, seen | {key}, use_site
        )
    imported = current.imports.get(name)
    if imported is None:
        return None
    if imported.symbol:
        target_module, target_symbol = imported.module, imported.symbol
    else:
        target_module, target_symbol = imported.module, ""
    owner = modules.get((current.snapshot.project, target_module))
    if owner is None or not target_symbol:
        return None
    return _tuple_registry_name(
        target_symbol,
        owner,
        documents,
        modules,
        seen | {key},
        None,
    )


def _span(facts: _ModuleFacts, node: ast.expr) -> Span:
    def offset(line: int, column: int) -> int:
        return facts.line_starts[line - 1] + len(
            facts.lines[line - 1].encode("utf-8")[:column].decode("utf-8")
        )

    return Span(
        offset(node.lineno, node.col_offset),
        offset(node.end_lineno or node.lineno, node.end_col_offset or node.col_offset),
    )


def resolve_testing_helper_calls(
    modules: tuple[ModuleSnapshot, ...], index: FragmentIndex
) -> TestingHelperFacts:
    """Resolve explicit helper calls and immutable fragment tuples only."""
    module_facts: dict[tuple[str, str], _ModuleFacts] = {}
    trees: dict[tuple[str, str], ast.Module] = {}
    for snapshot in modules:
        try:
            tree = ast.parse(snapshot.source)
            compile(tree, snapshot.uri, "exec")
        except (SyntaxError, ValueError, UnicodeError, RecursionError):
            continue
        key = (snapshot.project, snapshot.module)
        trees[key] = tree
        module_facts[key] = _ModuleFacts(snapshot, tree)
    documents_by_binding: dict[tuple[str, str, str], list[IndexedDocument]] = {}
    for item in index.documents:
        if item.call.binding is not None:
            binding_key = (
                item.call.module.project,
                item.call.module.module,
                item.call.binding,
            )
            documents_by_binding.setdefault(binding_key, []).append(item)
    documents: _Documents = {
        key: tuple(values) for key, values in documents_by_binding.items()
    }
    calls: list[TestingHelperCall] = []
    diagnostics: list[GraphDiagnostic] = []
    for key, tree in trees.items():
        facts = module_facts[key]
        snapshot = facts.snapshot
        identity = ModuleDependency(
            snapshot.project, snapshot.module, snapshot.uri, snapshot.version
        )
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or node.end_lineno is None:
                continue
            helper = _resolve_helper(node.func, facts, node)
            if helper is None:
                continue
            if helper == "read_result":
                document_expr = _argument(node, 0, "document")
                registry_expr = _argument(node, 2, "fragments")
                document = (
                    _resolve_doc(
                        document_expr, facts, documents, module_facts, use_site=node
                    )
                    if document_expr is not None
                    else None
                )
                if document is None or document.selected_operation is None:
                    diagnostics.append(
                        GraphDiagnostic(
                            identity,
                            "testing-document-not-static",
                            "read_result requires a statically resolved operation "
                            "document",
                            _span(facts, node),
                        )
                    )
                    continue
            else:
                document = None
                registry_expr = _argument(node, 0, "fragments")
            fragments = (
                _tuple_registry(
                    registry_expr, facts, documents, module_facts, use_site=node
                )
                if registry_expr is not None
                else None
            )
            if fragments is None:
                message = (
                    "Use a statically fixed tuple of fragment documents for precise "
                    "typing; "
                    "lists and dynamic registries stay conservatively masked."
                )
                diagnostics.append(
                    GraphDiagnostic(
                        identity,
                        "testing-registry-not-static",
                        message,
                        _span(facts, node),
                    )
                )
                continue
            if document is not None:
                if any(
                    item.primary_fragment is None
                    or item.schema_identity != document.schema_identity
                    or item.context_token != document.context_token
                    or item.primary_fragment not in document.definitions
                    for item in fragments
                ):
                    diagnostics.append(
                        GraphDiagnostic(
                            identity,
                            "testing-fragment-not-in-document",
                            "read_result registry contains a fragment outside the "
                            "operation closure",
                            _span(facts, node),
                        )
                    )
                    continue
            calls.append(
                TestingHelperCall(
                    identity,
                    _span(facts, node),
                    helper,
                    document,
                    fragments,
                )
            )
    return TestingHelperFacts(tuple(calls), tuple(diagnostics))


def registry_key(call: TestingHelperCall) -> str:
    """Stable key for an observed document and exact static registry tuple."""
    values: list[object] = [call.helper]
    if call.document is not None:
        values.append(
            (
                call.document.call.module.project,
                call.document.call.module.module,
                call.document.call.range.start,
                call.document.call.range.end,
            )
        )
    values.append(
        [
            (
                item.call.module.project,
                item.call.module.module,
                item.call.range.start,
                item.call.range.end,
            )
            for item in call.fragments
        ]
    )
    payload = json.dumps(values, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def render_testing_helper_artifacts(
    config: ProjectConfig,
    schemas: tuple[tuple[SchemaConfig, SchemaContext], ...],
    index: FragmentIndex,
    modules: tuple[ModuleSnapshot, ...],
    provenance: tuple[OperationProvenance, ...],
) -> TestingHelperArtifacts:
    """Render exact helper overloads for statically resolved tuple call sites."""
    facts = resolve_testing_helper_calls(modules, index)
    contexts = {settings.name: (settings, context) for settings, context in schemas}
    records = {
        (item.owner, item.schema_identity, item.context_token): item
        for item in provenance
    }
    files: dict[str, str] = {}
    result_imports: list[str] = []
    result_overloads: list[str] = []
    mask_imports: list[str] = []
    mask_overloads: list[str] = []
    diagnostics = list(facts.diagnostics)
    seen: set[str] = set()

    def project_tag(call: TestingHelperCall) -> str:
        return hashlib.sha256(call.module.project.encode("utf-8")).hexdigest()[:8]

    def path_for(document: IndexedDocument) -> tuple[str, str] | None:
        identity = DocumentIdentity(document.call.module, document.call.range)
        record = records.get(
            (identity, document.schema_identity, document.context_token)
        )
        if (
            record is None
            or record.status != "precise"
            or record.declaration_path is None
            or record.declaration_symbol is None
        ):
            return None
        module = record.declaration_path.removesuffix(".pyi").replace("/", ".")
        return module, record.declaration_symbol

    def unavailable(call: TestingHelperCall, description: str) -> None:
        diagnostics.append(
            GraphDiagnostic(
                call.module,
                "testing-helper-types-unavailable",
                f"Cannot produce precise helper types for {description}",
                call.span,
            )
        )

    for call in facts.calls:
        key = registry_key(call)
        if key in seen:
            continue
        seen.add(key)
        if call.helper == "read_result":
            assert call.document is not None
            selected = call.document.selected_operation
            if selected is None:
                continue
            schema = contexts.get(call.document.schema_identity[1])
            context = schema[1] if schema is not None else None
            settings = schema[0] if schema is not None else None
            document_type = path_for(call.document)
            if context is None or document_type is None:
                unavailable(call, "the operation document")
                continue
            fragments = frozenset(
                item.primary_fragment.name
                for item in call.fragments
                if item.primary_fragment is not None
                and item.primary_fragment.name is not None
            )
            try:
                rendered = render_indexed_operation_types(
                    context,
                    call.document,
                    name="GraphqlDeskReadResult",
                    scalars=settings.scalars if settings else (),
                    enums=settings.enums if settings else (),
                    client_directives=config.client_directives,
                    disable_masking=settings.disable_masking if settings else False,
                    fragment_index=index,
                    fixture_input=True,
                    unmasked_fragments=fragments,
                )
            except (ValueError, RecursionError) as error:
                diagnostics.append(
                    GraphDiagnostic(
                        call.module,
                        "testing-result-types-unavailable",
                        f"Cannot render exact read_result types: {error}",
                        call.span,
                    )
                )
                continue
            type_path = (
                f"{config.generated_package.replace('.', '/')}/testing/call_{key}.pyi"
            )
            files[type_path] = rendered
            type_module = type_path.removesuffix(".pyi").replace("/", ".")
            number = len(result_overloads)
            prefix = project_tag(call)
            document_alias = f"_GraphqlDeskOperation_{prefix}_{number}"
            fragment_aliases = [
                f"_GraphqlDeskFragment_{prefix}_{number}_{item_number}"
                for item_number, _ in enumerate(call.fragments)
            ]
            fragment_types = [path_for(item) for item in call.fragments]
            if any(item is None for item in fragment_types):
                unavailable(call, "one or more registry fragments")
                continue
            resolved_fragment_types = [
                item for item in fragment_types if item is not None
            ]
            fragment_tuple = (
                "tuple[" + ", ".join(fragment_aliases) + "]"
                if fragment_aliases
                else "tuple[()]"
            )
            result_alias = f"_GraphqlDeskResult_{prefix}_{number}"
            input_alias = f"_GraphqlDeskInput_{prefix}_{number}"
            result_imports.append(
                f"from {document_type[0]} import {document_type[1]} "
                f"as {document_alias}\n"
                + "".join(
                    f"from {module} import {symbol} "
                    f"as {fragment_aliases[item_number]}\n"
                    for item_number, (module, symbol) in enumerate(
                        resolved_fragment_types
                    )
                )
                + f"from {type_module} import GraphqlDeskReadResultResult as "
                f"{result_alias}, GraphqlDeskReadResultResultFixtureInput as "
                f"{input_alias}\n"
            )
            result_overloads.append(
                "@_graphql_desk_overload\n"
                f"def read_result(document: {document_alias}, data: {input_alias}, "
                f"fragments: {fragment_tuple}) -> "
                f"{result_alias}: ...\n"
                "@_graphql_desk_overload\n"
                f"def read_result(document: {document_alias}, data: None, "
                f"fragments: {fragment_tuple}) -> None: ...\n"
            )
        else:
            if not call.fragments:
                diagnostics.append(
                    GraphDiagnostic(
                        call.module,
                        "testing-empty-fragment-registry",
                        "mask_fragments requires at least one statically "
                        "resolved fragment",
                        call.span,
                    )
                )
                continue
            fragment_types = [path_for(item) for item in call.fragments]
            if any(item is None for item in fragment_types):
                unavailable(call, "one or more mask_fragments documents")
                continue
            resolved_types = list(
                dict.fromkeys(item for item in fragment_types if item is not None)
            )
            type_path = (
                f"{config.generated_package.replace('.', '/')}/testing/call_{key}.pyi"
            )
            type_module = type_path.removesuffix(".pyi").replace("/", ".")
            prefix = project_tag(call)
            number = len(mask_overloads)
            input_alias = f"_GraphqlDeskMaskInput_{prefix}_{number}"
            ref_alias = f"_GraphqlDeskMaskRef_{prefix}_{number}"
            type_imports = (
                "from typing_extensions import TypedDict as _GraphqlDeskTypedDict\n"
                "from graphql_desk.fragment_refs import FragmentRef as "
                "_GraphqlDeskFragmentRef\n"
            )
            input_bases: list[str] = []
            ref_identities: list[str] = []
            helper_imports = ""
            for item_number, (module, document_symbol) in enumerate(resolved_types):
                result_name = document_symbol.removesuffix("Document") + "Result"
                identity_name = document_symbol.removesuffix("Document") + "Identity"
                result_name_alias = f"_GraphqlDeskFragmentResult{item_number}"
                identity_name_alias = f"_GraphqlDeskFragmentIdentity{item_number}"
                helper_imports += (
                    f"from {module} import {result_name} as {result_name_alias}, "
                    f"{identity_name} as {identity_name_alias}\n"
                )
                input_bases.append(result_name_alias)
                ref_identities.append(identity_name_alias)
            ref_union = " | ".join(ref_identities)
            input_bases_text = ", ".join((*input_bases, "_GraphqlDeskTypedDict"))
            files[type_path] = (
                type_imports
                + helper_imports
                + f"class {input_alias}({input_bases_text}): ...\n"
                + f"{ref_alias} = _GraphqlDeskFragmentRef[{ref_union}]\n"
            )
            fragment_doc_aliases = [
                f"_GraphqlDeskFragmentDoc_{prefix}_{number}_{item_number}"
                for item_number in range(len(call.fragments))
            ]
            fragment_tuple = "tuple[" + ", ".join(fragment_doc_aliases) + "]"
            local_input = f"_GraphqlDeskMaskInput_{prefix}_{number}"
            local_ref = f"_GraphqlDeskMaskRef_{prefix}_{number}"
            mask_imports.append(
                f"from {type_module} import {input_alias} "
                f"as {local_input}, "
                f"{ref_alias} as {local_ref}\n"
                + "".join(
                    f"from {module} import {symbol} as "
                    f"{fragment_doc_aliases[item_number]}\n"
                    for item_number, (module, symbol) in enumerate(
                        item for item in fragment_types if item is not None
                    )
                )
            )
            mask_overloads.append(
                "@_graphql_desk_overload\n"
                + f"def mask_fragments(fragments: {fragment_tuple}, "
                f"data: _graphql_desk_Sequence[{local_input}]) -> "
                f"_graphql_desk_Sequence[{local_ref}]: ...\n"
                + "@_graphql_desk_overload\n"
                + f"def mask_fragments(fragments: {fragment_tuple}, "
                f"data: _graphql_desk_Sequence[{local_input} | None]) -> "
                f"_graphql_desk_Sequence[{local_ref} | None]: ...\n"
                + "@_graphql_desk_overload\n"
                + f"def mask_fragments(fragments: {fragment_tuple}, "
                f"data: {local_input}) -> {local_ref}: ...\n"
                + "@_graphql_desk_overload\n"
                + f"def mask_fragments(fragments: {fragment_tuple}, "
                "data: None) -> None: ...\n"
            )

    return TestingHelperArtifacts(
        tuple(sorted(files.items())),
        "".join(result_imports) + "".join(result_overloads),
        "".join(mask_imports) + "".join(mask_overloads),
        tuple(diagnostics),
    )
