"""Resolve module-level source facts from explicit snapshots, without imports.

Factories, fragment bindings, nested scopes and decoded-character maps belong to
later slices. This graph never discovers files or executes project Python.
"""

import ast
import keyword
import re
import unicodedata
from dataclasses import dataclass

from .source import Span

_REFLECTION = {
    f"builtins.{name}" for name in ("globals", "locals", "vars", "exec", "eval")
}


@dataclass(frozen=True)
class ModuleSnapshot:
    project: str
    module: str
    uri: str
    version: int
    source: str
    is_package: bool = False


@dataclass(frozen=True)
class ModuleDependency:
    project: str
    module: str
    uri: str
    version: int


@dataclass(frozen=True)
class StringContribution:
    module: ModuleDependency
    range: Span
    text: str


@dataclass(frozen=True)
class ResolvedCall:
    module: ModuleDependency
    range: Span
    source: str
    contributions: tuple[StringContribution, ...]
    dependencies: tuple[ModuleDependency, ...]
    operation: str | None = None
    binding: str | None = None


@dataclass(frozen=True)
class GraphDiagnostic:
    module: ModuleDependency
    code: str
    message: str
    range: Span


@dataclass(frozen=True)
class ProjectSources:
    calls: tuple[ResolvedCall, ...]
    diagnostics: tuple[GraphDiagnostic, ...]


@dataclass(frozen=True)
class _Value:
    kind: str
    text: str
    contributions: tuple[StringContribution, ...] = ()
    dependencies: frozenset[ModuleDependency] = frozenset()


class _Unresolved(Exception):
    pass


class _Module:
    def __init__(self, snapshot: ModuleSnapshot) -> None:
        self.snapshot = snapshot
        self.identity = ModuleDependency(
            snapshot.project, snapshot.module, snapshot.uri, snapshot.version
        )
        self.lines = re.split(r"(?<=\n)|(?<=\r)(?!\n)", snapshot.source)
        self.starts = [0]
        for line in self.lines:
            self.starts.append(self.starts[-1] + len(line))
        self.tree = ast.parse(snapshot.source)
        compile(self.tree, snapshot.uri, "exec")
        self.bindings: dict[
            str, list[tuple[tuple[int, int], ast.expr | tuple[str, str]]]
        ] = {}
        self.blocked: set[str] = set()
        self.star = False
        self.call_bindings: dict[int, str] = {}
        for statement in self.tree.body:
            if isinstance(statement, ast.Import):
                for alias in statement.names:
                    name = alias.asname or alias.name.split(".")[0]
                    module = alias.name if alias.asname else alias.name.split(".")[0]
                    self.add(name, statement, (module, ""))
            elif isinstance(statement, ast.ImportFrom):
                module = self.import_module(statement)
                for alias in statement.names:
                    if alias.name == "*":
                        self.star = True
                    else:
                        self.add(
                            alias.asname or alias.name,
                            statement,
                            (module, alias.name),
                        )
            elif isinstance(statement, (ast.Assign, ast.AnnAssign)):
                targets = (
                    statement.targets
                    if isinstance(statement, ast.Assign)
                    else [statement.target]
                )
                if (
                    len(targets) == 1
                    and isinstance(targets[0], ast.Name)
                    and statement.value is not None
                ):
                    self.add(targets[0].id, statement, statement.value)
                    if isinstance(statement.value, ast.Call):
                        self.call_bindings[id(statement.value)] = targets[0].id
                else:
                    self.blocked.update(self.writes(statement))
            else:
                self.blocked.update(self.writes(statement))
        # Global writes inside deferred code can invalidate module-level facts.
        for node in ast.walk(self.tree):
            if isinstance(node, (ast.Global, ast.Nonlocal)):
                self.blocked.update(node.names)
            elif isinstance(node, ast.NamedExpr):
                self.blocked.update(self.writes(node.target))
            elif isinstance(node, ast.ImportFrom) and any(
                alias.name == "*" for alias in node.names
            ):
                self.star = True

    def add(
        self, name: str, statement: ast.stmt, value: ast.expr | tuple[str, str]
    ) -> None:
        self.bindings.setdefault(name, []).append(
            ((statement.lineno, statement.col_offset), value)
        )

    def import_module(self, node: ast.ImportFrom) -> str:
        if not node.level:
            return node.module or ""
        package = self.snapshot.module.split(".")
        if not self.snapshot.is_package:
            package.pop()
        if node.level > len(package):
            return ""
        base = package[: len(package) - node.level + 1]
        return ".".join([*base, *([node.module] if node.module else [])])

    @staticmethod
    def writes(node: ast.AST) -> set[str]:
        class Writes(ast.NodeVisitor):
            def __init__(self) -> None:
                self.names: set[str] = set()

            def visit_Import(self, node: ast.Import) -> None:
                self.names.update(
                    alias.asname or alias.name.split(".")[0] for alias in node.names
                )

            def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
                self.names.update(alias.asname or alias.name for alias in node.names)

            def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
                if node.name:
                    self.names.add(node.name)
                self.generic_visit(node)

            def visit_MatchAs(self, node: ast.MatchAs) -> None:
                if node.name:
                    self.names.add(node.name)
                self.generic_visit(node)

            def visit_MatchStar(self, node: ast.MatchStar) -> None:
                if node.name:
                    self.names.add(node.name)

            def visit_MatchMapping(self, node: ast.MatchMapping) -> None:
                if node.rest:
                    self.names.add(node.rest)
                self.generic_visit(node)

            def visit_Name(self, node: ast.Name) -> None:
                if isinstance(node.ctx, (ast.Store, ast.Del)):
                    self.names.add(node.id)

            def visit_FunctionDef(
                self, node: ast.FunctionDef | ast.AsyncFunctionDef
            ) -> None:
                self.names.add(node.name)
                for expression in [*node.decorator_list, *node.args.defaults]:
                    self.visit(expression)
                for default in node.args.kw_defaults:
                    if default is not None:
                        self.visit(default)

            visit_AsyncFunctionDef = visit_FunctionDef

            def visit_ClassDef(self, node: ast.ClassDef) -> None:
                self.names.add(node.name)
                for expression in [*node.bases, *node.decorator_list]:
                    self.visit(expression)

        visitor = Writes()
        visitor.visit(node)
        return visitor.names

    def span(self, node: ast.AST) -> Span:
        assert isinstance(node, (ast.expr, ast.stmt))
        assert node.end_lineno is not None and node.end_col_offset is not None

        def offset(line: int, column: int) -> int:
            return self.starts[line - 1] + len(
                self.lines[line - 1].encode("utf-8")[:column].decode("utf-8")
            )

        return Span(
            offset(node.lineno, node.col_offset),
            offset(node.end_lineno, node.end_col_offset),
        )


class _Graph:
    def __init__(self, snapshots: tuple[ModuleSnapshot, ...]) -> None:
        self.modules: dict[tuple[str, str], _Module] = {}
        self.diagnostics: list[GraphDiagnostic] = []
        self.mutated: set[tuple[str, str]] = set()
        uris: set[tuple[str, str]] = set()
        keys: set[tuple[str, str]] = set()
        self.supplied = keys
        for snapshot in snapshots:
            key = (snapshot.project, snapshot.module)
            if (
                not snapshot.project
                or not snapshot.uri
                or not snapshot.module
                or unicodedata.normalize("NFKC", snapshot.module) != snapshot.module
                or any(
                    not part.isidentifier() or keyword.iskeyword(part)
                    for part in snapshot.module.split(".")
                )
            ):
                raise ValueError(
                    "Snapshots require project, URI and a valid dotted module name"
                )
            if key in keys or (snapshot.project, snapshot.uri) in uris:
                raise ValueError("Duplicate project module or URI identity")
            uris.add((snapshot.project, snapshot.uri))
            keys.add(key)
            try:
                self.modules[key] = _Module(snapshot)
            except (SyntaxError, ValueError, UnicodeError, RecursionError) as error:
                identity = ModuleDependency(
                    snapshot.project, snapshot.module, snapshot.uri, snapshot.version
                )
                self.diagnostics.append(
                    GraphDiagnostic(
                        identity,
                        "invalid-python",
                        str(error),
                        Span(0, len(snapshot.source)),
                    )
                )
        for module in self.modules.values():
            for node in ast.walk(module.tree):
                roots: list[ast.expr] = []
                if isinstance(node, (ast.Attribute, ast.Subscript)) and isinstance(
                    node.ctx, (ast.Store, ast.Del)
                ):
                    root = node.value
                    while isinstance(root, (ast.Attribute, ast.Subscript)):
                        root = root.value
                    roots.append(root)
                elif isinstance(node, ast.Call):
                    if self.namespace_reflection(module, node.func, set()):
                        module.star = True
                    try:
                        callee = self.expression(
                            module,
                            node.func,
                            (node.lineno, node.col_offset),
                            set(),
                        )
                        trusted = callee.kind == "callable"
                    except (_Unresolved, RecursionError):
                        trusted = False
                    if not trusted:
                        pending = [*node.args, *(item.value for item in node.keywords)]
                        while pending:
                            argument = pending.pop()
                            roots.append(argument)
                            if isinstance(argument, (ast.List, ast.Tuple, ast.Set)):
                                pending.extend(argument.elts)
                            elif isinstance(argument, ast.Dict):
                                pending.extend(
                                    item for item in argument.keys if item is not None
                                )
                                pending.extend(argument.values)
                for root in roots:
                    # Opaque code can expose or write these namespace capabilities.
                    if self.namespace_reflection(module, root, set()):
                        module.star = True
                    try:
                        value = self.expression(
                            module,
                            root,
                            (root.lineno, root.col_offset),
                            set(),
                            probe=True,
                        )
                    except (_Unresolved, RecursionError):
                        continue
                    if value.kind == "module":
                        self.mutated.add((module.snapshot.project, value.text))
                        if value.text == "builtins":
                            module.star = True
                    elif value.kind == "callable":
                        self.mutated.add((module.snapshot.project, "graphql_desk"))

    def namespace_reflection(
        self, module: _Module, node: ast.expr, seen: set[str]
    ) -> bool:
        try:
            value = self.expression(
                module, node, (len(module.snapshot.source) + 2, 0), set(), probe=True
            )
            if value.kind == "external" and value.text in _REFLECTION:
                return True
        except (_Unresolved, RecursionError):
            pass
        pending = [node]
        while pending:
            current = pending.pop()
            if not isinstance(current, ast.Name) or current.id in seen:
                continue
            if f"builtins.{current.id}" in _REFLECTION:
                return True
            seen.add(current.id)
            pending.extend(
                definition
                for _, definition in module.bindings.get(current.id, [])
                if isinstance(definition, ast.expr)
            )
        return False

    def name(
        self,
        module: _Module,
        name: str,
        before: tuple[int, int],
        active: set[tuple[str, str, str]],
        *,
        probe: bool = False,
    ) -> _Value:
        if not probe and (module.star or name in module.blocked):
            raise _Unresolved(f"Binding {name!r} is shadowed or mutation-dependent")
        definitions = module.bindings.get(name, [])
        if not definitions or (
            not probe and (len(definitions) != 1 or definitions[0][0] >= before)
        ):
            raise _Unresolved(
                f"Binding {name!r} is missing, rebound or not yet defined"
            )
        key = (module.snapshot.project, module.snapshot.module, name)
        if key in active:
            raise _Unresolved(f"Import or value cycle includes {name!r}")
        active = active | {key}
        fallback: _Value | None = None
        for _, definition in definitions:
            try:
                if isinstance(definition, tuple):
                    imported, symbol = definition
                    if not imported:
                        raise _Unresolved(
                            "Relative import escapes the supplied package"
                        )
                    value = self.export(module, imported, symbol, active, probe=probe)
                else:
                    value = self.expression(
                        module,
                        definition,
                        (definition.lineno, definition.col_offset),
                        active,
                        probe=probe,
                    )
            except _Unresolved:
                if not probe:
                    raise
                continue
            value = _Value(
                value.kind,
                value.text,
                value.contributions,
                value.dependencies | {module.identity},
            )
            if not probe or value.kind == "callable" or value.text == "graphql_desk":
                return value
            fallback = value
        if fallback is not None:
            return fallback
        raise _Unresolved(f"Binding {name!r} cannot be resolved")

    def export(
        self,
        caller: _Module,
        imported: str,
        symbol: str,
        active: set[tuple[str, str, str]],
        *,
        probe: bool = False,
    ) -> _Value:
        parts = imported.split(".")
        for index in range(1, len(parts) + 1):
            key = (caller.snapshot.project, ".".join(parts[:index]))
            if key in self.supplied and key not in self.modules:
                raise _Unresolved(f"Supplied module {key[1]!r} contains invalid Python")
            parent = self.modules.get(key)
            if (
                index < len(parts)
                and parent is not None
                and not parent.snapshot.is_package
            ):
                raise _Unresolved(f"Supplied module {key[1]!r} is not a package")
        if not probe and (caller.snapshot.project, imported) in self.mutated:
            raise _Unresolved(f"Module {imported!r} is mutated in the supplied project")
        target = self.modules.get((caller.snapshot.project, imported))
        if not symbol:
            dependencies = (
                frozenset({target.identity})
                if target
                else frozenset[ModuleDependency]()
            )
            return _Value("module", imported, dependencies=dependencies)
        if (
            target is None
            and imported == "graphql_desk"
            and symbol in {"graphql", "gql"}
        ):
            return _Value("callable", "graphql_desk.graphql")
        if target is not None and (
            symbol in target.bindings or symbol in target.blocked or target.star
        ):
            return self.name(
                target,
                symbol,
                (len(target.snapshot.source) + 2, 0),
                active,
                probe=probe,
            )
        if target is not None and "__getattr__" in target.blocked:
            raise _Unresolved(f"Dynamic module exports in {imported!r} are unresolved")
        submodule = f"{imported}.{symbol}"
        if (caller.snapshot.project, submodule) in self.modules:
            if target is not None and not target.snapshot.is_package:
                raise _Unresolved(f"Supplied module {imported!r} is not a package")
            dependencies = (
                frozenset({target.identity})
                if target
                else frozenset[ModuleDependency]()
            )
            return _Value("module", submodule, dependencies=dependencies)
        return _Value("external", f"{imported}.{symbol}")

    def expression(
        self,
        module: _Module,
        node: ast.expr,
        before: tuple[int, int],
        active: set[tuple[str, str, str]],
        *,
        probe: bool = False,
    ) -> _Value:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            contribution = StringContribution(
                module.identity, module.span(node), node.value
            )
            return _Value(
                "string", node.value, (contribution,), frozenset({module.identity})
            )
        if isinstance(node, ast.Name):
            return self.name(module, node.id, before, active, probe=probe)
        if isinstance(node, ast.Attribute):
            parent = self.expression(module, node.value, before, active, probe=probe)
            if parent.kind == "module":
                value = self.export(module, parent.text, node.attr, active, probe=probe)
                return _Value(
                    value.kind,
                    value.text,
                    value.contributions,
                    value.dependencies | parent.dependencies,
                )
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            left = self.expression(module, node.left, before, active, probe=probe)
            right = self.expression(module, node.right, before, active, probe=probe)
            if left.kind == right.kind == "string":
                return _Value(
                    "string",
                    left.text + right.text,
                    left.contributions + right.contributions,
                    left.dependencies | right.dependencies,
                )
        raise _Unresolved(
            "Use stable module constants, string literals or literal-only +"
        )

    def extract(
        self, module: _Module, node: ast.Call, callable_: _Value
    ) -> ResolvedCall:
        if any(isinstance(arg, ast.Starred) for arg in node.args) or any(
            item.arg is None for item in node.keywords
        ):
            raise _Unresolved("Starred arguments are not static")
        keywords = {item.arg: item.value for item in node.keywords}
        if len(node.args) > 2 or set(keywords) - {"source", "fragments", "operation"}:
            raise _Unresolved("Use source, fragments and operation arguments only")
        if (
            len(keywords) != len(node.keywords)
            or (node.args and "source" in keywords)
            or (len(node.args) == 2 and "fragments" in keywords)
        ):
            raise _Unresolved("Each argument must be supplied once")
        source_node = node.args[0] if node.args else keywords.get("source")
        if source_node is None:
            raise _Unresolved("A static source string is required")
        source = self.expression(
            module, source_node, (node.lineno, node.col_offset), set()
        )
        if source.kind != "string":
            raise _Unresolved("Source must resolve to a static string")
        fragments = node.args[1] if len(node.args) == 2 else keywords.get("fragments")
        if fragments is not None and not (
            isinstance(fragments, (ast.List, ast.Tuple)) and not fragments.elts
        ):
            raise _Unresolved(
                "Fragment bindings require the next project resolver slice"
            )
        operation = keywords.get("operation")
        if operation is not None and not (
            isinstance(operation, ast.Constant)
            and (isinstance(operation.value, str) or operation.value is None)
        ):
            raise _Unresolved("Operation selection must be a literal string or None")
        selector = operation.value if isinstance(operation, ast.Constant) else None
        assert isinstance(selector, str) or selector is None
        dependencies = tuple(
            sorted(
                source.dependencies | callable_.dependencies | {module.identity},
                key=lambda item: (item.project, item.module),
            )
        )
        return ResolvedCall(
            module.identity,
            module.span(node),
            source.text,
            source.contributions,
            dependencies,
            selector,
            module.call_bindings.get(id(node)),
        )


def resolve_project(modules: tuple[ModuleSnapshot, ...]) -> ProjectSources:
    """Resolve stable module calls in caller-selected snapshots; no discovery."""
    graph = _Graph(modules)
    calls: list[ResolvedCall] = []
    for module in graph.modules.values():

        class Calls(ast.NodeVisitor):
            scoped = False
            shadowed: set[str] = set()

            def visit_Call(self, node: ast.Call) -> None:
                root = node.func
                while isinstance(root, ast.Attribute):
                    root = root.value
                if isinstance(root, ast.Name) and root.id in self.shadowed:
                    self.generic_visit(node)
                    return
                candidate = False
                try:
                    callable_ = graph.expression(
                        module, node.func, (node.lineno, node.col_offset), set()
                    )
                    if callable_.kind == "callable":
                        candidate = True
                        if self.scoped:
                            raise _Unresolved(
                                "Nested scopes require a later project resolver slice"
                            )
                        calls.append(graph.extract(module, node, callable_))
                except RecursionError:
                    if candidate:
                        graph.diagnostics.append(
                            GraphDiagnostic(
                                module.identity,
                                "source-too-complex",
                                "Static binding nesting exceeds the resolver limit",
                                module.span(node),
                            )
                        )
                except _Unresolved as error:
                    if not candidate:
                        try:
                            origin = graph.expression(
                                module,
                                node.func,
                                (node.lineno, node.col_offset),
                                set(),
                                probe=True,
                            )
                            candidate = origin.kind == "callable"
                        except (_Unresolved, RecursionError):
                            pass
                    if candidate:
                        graph.diagnostics.append(
                            GraphDiagnostic(
                                module.identity,
                                "source-not-static",
                                str(error),
                                module.span(node),
                            )
                        )
                self.generic_visit(node)

            def visit_FunctionDef(self, node: ast.AST) -> None:
                previous = self.scoped
                previous_shadowed = self.shadowed
                self.scoped = True
                local = {
                    item.id
                    for item in ast.walk(node)
                    if isinstance(item, ast.Name)
                    and isinstance(item.ctx, (ast.Store, ast.Del))
                }
                local.update(
                    item.arg for item in ast.walk(node) if isinstance(item, ast.arg)
                )
                self.shadowed = previous_shadowed | local
                self.generic_visit(node)
                self.shadowed = previous_shadowed
                self.scoped = previous

            visit_AsyncFunctionDef = visit_FunctionDef
            visit_ClassDef = visit_FunctionDef
            visit_Lambda = visit_FunctionDef
            visit_ListComp = visit_FunctionDef
            visit_SetComp = visit_FunctionDef
            visit_DictComp = visit_FunctionDef
            visit_GeneratorExp = visit_FunctionDef

        start = len(calls)
        try:
            Calls().visit(module.tree)
        except RecursionError:
            del calls[start:]
            graph.diagnostics.append(
                GraphDiagnostic(
                    module.identity,
                    "source-too-complex",
                    "Python syntax nesting exceeds the resolver traversal limit",
                    Span(0, len(module.snapshot.source)),
                )
            )
    return ProjectSources(tuple(calls), tuple(graph.diagnostics))
