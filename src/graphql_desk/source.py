"""Conservative single-module discovery without evaluating Python code.

Only direct ``graphql_desk`` imports and inline string literals are supported.
Reexports, imported constants, generated factories and decoded-character maps
require later extractor slices. A rebound name is excluded throughout its scope.
"""

import ast
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Span:
    start: int
    end: int


@dataclass(frozen=True)
class ExtractedCall:
    source: str
    call_range: Span
    literal_range: Span
    operation: str | None = None


@dataclass(frozen=True)
class SourceDiagnostic:
    code: str
    message: str
    range: Span


@dataclass(frozen=True)
class ExtractionResult:
    calls: tuple[ExtractedCall, ...]
    diagnostics: tuple[SourceDiagnostic, ...]


class _Bindings(ast.NodeVisitor):
    def __init__(self) -> None:
        self.values: dict[str, set[str | None]] = {}
        self.conditional = 0

    def generic_visit(self, node: ast.AST) -> None:
        conditional = type(node).__name__ == "TryStar" or isinstance(
            node,
            (
                ast.If,
                ast.For,
                ast.AsyncFor,
                ast.While,
                ast.Try,
                ast.With,
                ast.AsyncWith,
                ast.Match,
            ),
        )
        self.conditional += conditional
        super().generic_visit(node)
        self.conditional -= conditional

    def bind(self, name: str, kind: str | None = None) -> None:
        self.values.setdefault(name, set()).add(kind)

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, (ast.Store, ast.Del)):
            self.bind(node.id)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if isinstance(node.ctx, (ast.Store, ast.Del)):
            root: ast.expr = node.value
            while isinstance(root, ast.Attribute):
                root = root.value
            if isinstance(root, ast.Name):
                self.bind(root.id)
        self.generic_visit(node)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self.bind(
                alias.asname or alias.name.split(".")[0],
                "module"
                if alias.name == "graphql_desk" and not self.conditional
                else None,
            )

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        for alias in node.names:
            self.bind(
                alias.asname or alias.name,
                "callable"
                if node.module == "graphql_desk"
                and node.level == 0
                and alias.name in {"graphql", "gql"}
                and not self.conditional
                else None,
            )

    def visit_FunctionDef(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self.bind(node.name)
        self.visit(node.args)
        for decorator in node.decorator_list:
            self.visit(decorator)
        if node.returns is not None:
            self.visit(node.returns)

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.bind(node.name)
        for expression in [*node.bases, *node.decorator_list]:
            self.visit(expression)
        for keyword in node.keywords:
            self.visit(keyword.value)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        self.visit(node.args)

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if node.name:
            self.bind(node.name)
        self.generic_visit(node)

    def visit_MatchAs(self, node: ast.MatchAs) -> None:
        if node.name:
            self.bind(node.name)
        self.generic_visit(node)

    def visit_MatchStar(self, node: ast.MatchStar) -> None:
        if node.name:
            self.bind(node.name)

    def visit_MatchMapping(self, node: ast.MatchMapping) -> None:
        if node.rest:
            self.bind(node.rest)
        self.generic_visit(node)


def _scope_bindings(nodes: list[ast.stmt]) -> dict[str, str | None]:
    bindings = _Bindings()
    for node in nodes:
        bindings.visit(node)
        for descendant in ast.walk(node):
            if isinstance(descendant, (ast.Global, ast.Nonlocal)):
                for name in descendant.names:
                    bindings.bind(name)
            elif isinstance(descendant, ast.Attribute) and isinstance(
                descendant.ctx, (ast.Store, ast.Del)
            ):
                # Nested code can mutate an imported module without rebinding its name.
                bindings.visit_Attribute(descendant)
    if "*" in bindings.values:
        return dict.fromkeys(bindings.values)
    return {
        name: next(iter(kinds)) if len(kinds) == 1 else None
        for name, kinds in bindings.values.items()
    }


class _Extractor(ast.NodeVisitor):
    def __init__(self, source: str) -> None:
        self.lines = re.split(r"(?<=\n)|(?<=\r)(?!\n)", source)
        self.starts = [0]
        for line in self.lines:
            self.starts.append(self.starts[-1] + len(line))
        self.bindings: dict[str, str | None] = {}
        self.allowed: dict[str, str | None] = {}
        self.calls: list[ExtractedCall] = []
        self.diagnostics: list[SourceDiagnostic] = []

    def generic_visit(self, node: ast.AST) -> None:
        if type(node).__name__ == "TypeAlias":
            self.unsupported_scope(node)
            return
        super().generic_visit(node)

    def position(self, line: int, byte_column: int) -> int:
        text = self.lines[line - 1]
        return self.starts[line - 1] + len(text.encode("utf-8")[:byte_column].decode())

    def span(self, node: ast.expr) -> Span:
        assert node.end_lineno is not None and node.end_col_offset is not None
        return Span(
            self.position(node.lineno, node.col_offset),
            self.position(node.end_lineno, node.end_col_offset),
        )

    def visit_Module(self, node: ast.Module) -> None:
        self.allowed = _scope_bindings(node.body)
        self.bindings = dict.fromkeys(self.allowed)
        self.generic_visit(node)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            name = alias.asname or alias.name.split(".")[0]
            self.bindings[name] = self.allowed.get(name)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        for alias in node.names:
            name = alias.asname or alias.name
            self.bindings[name] = self.allowed.get(name)

    def visit_FunctionDef(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        if getattr(node, "type_params", ()):
            self.unsupported_scope(node)
            return
        for expression in [*node.decorator_list, *node.args.defaults]:
            self.visit(expression)
        for default in node.args.kw_defaults:
            if default is not None:
                self.visit(default)
        previous = self.bindings
        previous_allowed = self.allowed
        self.allowed = _scope_bindings(node.body)
        self.bindings = {**previous, **dict.fromkeys(self.allowed)}
        arguments = [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]
        arguments += [arg for arg in (node.args.vararg, node.args.kwarg) if arg]
        for argument in arguments:
            self.bindings[argument.arg] = None
        for statement in node.body:
            self.visit(statement)
        self.bindings = previous
        self.allowed = previous_allowed

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Lambda(self, node: ast.Lambda) -> None:
        # shortcut: lambda/comprehension binding semantics need a later scope slice.
        self.unsupported_scope(node)

    def visit_ListComp(
        self, node: ast.ListComp | ast.SetComp | ast.DictComp | ast.GeneratorExp
    ) -> None:
        self.unsupported_scope(node)

    visit_SetComp = visit_ListComp
    visit_DictComp = visit_ListComp
    visit_GeneratorExp = visit_ListComp

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        # Class namespaces and method closures are deliberately not approximated.
        self.unsupported_scope(node)

    def unsupported_scope(self, node: ast.AST) -> None:
        for child in ast.walk(node):
            if isinstance(child, ast.Call) and self.recognized(child):
                self.diagnostics.append(
                    SourceDiagnostic(
                        "unsupported-scope",
                        "Move this call to a module or ordinary function scope.",
                        self.span(child),
                    )
                )

    def recognized(self, node: ast.Call) -> bool:
        function = node.func
        return (
            isinstance(function, ast.Name)
            and self.bindings.get(function.id) == "callable"
        ) or (
            isinstance(function, ast.Attribute)
            and function.attr in {"graphql", "gql"}
            and isinstance(function.value, ast.Name)
            and self.bindings.get(function.value.id) == "module"
        )

    def visit_Call(self, node: ast.Call) -> None:
        if self.recognized(node):
            self.extract(node)
        self.generic_visit(node)

    def extract(self, node: ast.Call) -> None:
        def reject(code: str, message: str) -> None:
            self.diagnostics.append(SourceDiagnostic(code, message, self.span(node)))

        if any(isinstance(arg, ast.Starred) for arg in node.args) or any(
            keyword.arg is None for keyword in node.keywords
        ):
            reject(
                "dynamic-arguments",
                "Inline arguments; *args and **kwargs are unresolved.",
            )
            return
        keywords = {keyword.arg: keyword.value for keyword in node.keywords}
        if len(node.args) > 2 or any(
            key not in {"source", "fragments", "operation"} for key in keywords
        ):
            reject(
                "unsupported-arguments",
                "Use source, fragments and operation arguments.",
            )
            return
        if (
            (node.args and "source" in keywords)
            or (len(node.args) == 2 and "fragments" in keywords)
            or len(keywords) != len(node.keywords)
        ):
            reject("duplicate-arguments", "Supply each argument only once.")
            return
        literal = node.args[0] if node.args else keywords.get("source")
        if not isinstance(literal, ast.Constant) or not isinstance(literal.value, str):
            reject(
                "dynamic-source",
                "Inline a static string literal for the GraphQL source.",
            )
            return
        fragments = node.args[1] if len(node.args) == 2 else keywords.get("fragments")
        if fragments is not None and not (
            isinstance(fragments, (ast.Tuple, ast.List)) and not fragments.elts
        ):
            reject(
                "unresolved-fragments",
                "Fragment references require project resolution.",
            )
            return
        selector = keywords.get("operation")
        if selector is not None and not (
            isinstance(selector, ast.Constant)
            and (isinstance(selector.value, str) or selector.value is None)
        ):
            reject("dynamic-operation", "Use a literal operation name or None.")
            return
        operation = selector.value if isinstance(selector, ast.Constant) else None
        assert isinstance(operation, str) or operation is None
        self.calls.append(
            ExtractedCall(literal.value, self.span(node), self.span(literal), operation)
        )


def extract_calls(source: str) -> ExtractionResult:
    """Extract stable imported calls, reporting unsupported arguments explicitly."""
    try:
        module = ast.parse(source)
        # Parsing alone accepts invalid scope constructs such as a top-level return.
        compile(module, "<graphql-desk>", "exec")
    except (SyntaxError, ValueError, UnicodeError, RecursionError) as error:
        return ExtractionResult(
            (), (SourceDiagnostic("invalid-python", str(error), Span(0, len(source))),)
        )
    extractor = _Extractor(source)
    try:
        extractor.visit(module)
    except RecursionError:
        return ExtractionResult(
            (),
            (
                SourceDiagnostic(
                    "source-too-complex",
                    "Python syntax nesting exceeds the extractor's recursion limit.",
                    Span(0, len(source)),
                ),
            ),
        )
    return ExtractionResult(tuple(extractor.calls), tuple(extractor.diagnostics))
