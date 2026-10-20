"""Resolve module-level source facts from explicit snapshots, without imports.

Fragment bindings, nested scopes and decoded-character maps belong to
later slices. This graph never discovers files or executes project Python.
"""

import ast
import keyword
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Literal

from .source import Span

_REFLECTION = frozenset(
    f"builtins.{name}" for name in ("globals", "locals", "vars", "exec", "eval")
)
_SETTERS = frozenset({"builtins.setattr", "builtins.delattr"})
_TYPE_BASES = frozenset(
    {
        "builtins.object",
        "builtins.str",
        "builtins.int",
        "builtins.float",
        "builtins.bytes",
        "typing.TypedDict",
        "typing_extensions.TypedDict",
        "enum.Enum",
        "enum.IntEnum",
        "enum.Flag",
        "enum.IntFlag",
    }
)


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
class FactoryBinding:
    module: str
    export: str
    schema_name: str | None = None


@dataclass(frozen=True)
class ProjectBindings:
    project: str
    schemas: tuple[str, ...]
    default_schema: str | None = None
    factories: tuple[FactoryBinding, ...] = ()


@dataclass(frozen=True)
class DocumentIdentity:
    module: ModuleDependency
    call_range: Span


@dataclass(frozen=True)
class FragmentReference:
    target: DocumentIdentity
    reference_module: ModuleDependency
    range: Span


@dataclass(frozen=True)
class ResolvedCall:
    module: ModuleDependency
    range: Span
    source: str
    contributions: tuple[StringContribution, ...]
    dependencies: tuple[ModuleDependency, ...]
    operation: str | None = None
    binding: str | None = None
    schema_name: str | None = field(default=None, kw_only=True)
    fragments: tuple[FragmentReference, ...] = field(default=(), kw_only=True)


@dataclass(frozen=True)
class GraphDiagnostic:
    module: ModuleDependency
    code: str
    message: str
    range: Span


@dataclass(frozen=True)
class RejectedCall:
    """Recognized document call ownership without resolved semantic facts."""

    module: ModuleDependency
    range: Span
    binding: str | None
    diagnostics: tuple[GraphDiagnostic, ...]


@dataclass(frozen=True)
class ProjectSources:
    calls: tuple[ResolvedCall, ...]
    diagnostics: tuple[GraphDiagnostic, ...]
    unavailable: tuple[RejectedCall, ...] = ()


@dataclass(frozen=True)
class TypeSymbolResolution:
    symbol: str
    status: Literal["resolved", "external", "unresolved", "missing", "not-type"]
    target: str | None
    dependencies: tuple[ModuleDependency, ...]
    message: str | None = None


@dataclass(frozen=True)
class _Value:
    kind: str
    text: str
    contributions: tuple[StringContribution, ...] = ()
    dependencies: frozenset[ModuleDependency] = frozenset()
    schema_name: str | None = None
    document: DocumentIdentity | None = None


class _Unresolved(Exception):
    pass


class _MissingType(_Unresolved):
    pass


@dataclass(frozen=True)
class _Parsed:
    # Private syntax is read-only; all per-run binding/mutation state lives in _Module.
    tree: ast.Module | None
    error: str | None = None


def _parse(snapshot: ModuleSnapshot) -> _Parsed:
    try:
        tree = ast.parse(snapshot.source)
        compile(tree, snapshot.uri, "exec")
    except (SyntaxError, ValueError, UnicodeError, RecursionError) as error:
        return _Parsed(None, str(error))
    return _Parsed(tree)


class _Module:
    def __init__(
        self, snapshot: ModuleSnapshot, parsed: _Parsed, *, type_symbols: bool = False
    ) -> None:
        self.snapshot = snapshot
        self.identity = ModuleDependency(
            snapshot.project, snapshot.module, snapshot.uri, snapshot.version
        )
        self.lines = re.split(r"(?<=\n)|(?<=\r)(?!\n)", snapshot.source)
        self.starts = [0]
        for line in self.lines:
            self.starts.append(self.starts[-1] + len(line))
        if parsed.tree is None:
            raise ValueError(parsed.error)
        self.tree = parsed.tree
        self.bindings: dict[
            str, list[tuple[tuple[int, int], ast.expr | tuple[str, str] | _Value]]
        ] = {}
        self.types: dict[str, ast.ClassDef] = {}
        self.annotated_types: dict[str, ast.AnnAssign] = {}
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
            elif type_symbols and isinstance(statement, ast.ClassDef):
                self.types[statement.name] = statement
                self.add(
                    statement.name,
                    statement,
                    _Value("type", f"{snapshot.module}.{statement.name}"),
                )
                if statement.decorator_list:
                    self.blocked.add(statement.name)
            elif isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self.add(statement.name, statement, _Value("opaque", statement.name))
                if statement.decorator_list:
                    self.blocked.add(statement.name)
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
                    value: ast.expr | _Value = statement.value
                    if type_symbols and isinstance(statement, ast.AnnAssign):
                        self.annotated_types[targets[0].id] = statement
                        value = _Value("unsupported", targets[0].id)
                    self.add(targets[0].id, statement, value)
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
        self, name: str, statement: ast.stmt, value: ast.expr | tuple[str, str] | _Value
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


def _validate_request(
    snapshots: tuple[ModuleSnapshot, ...], bindings: tuple[ProjectBindings, ...]
) -> tuple[dict[str, ProjectBindings], dict[tuple[str, str, str], FactoryBinding]]:
    projects: dict[str, ProjectBindings] = {}
    factories: dict[tuple[str, str, str], FactoryBinding] = {}
    for project in bindings:
        if not project.project or project.project in projects:
            raise ValueError("Project binding identities must be nonempty and unique")
        if not project.schemas or len(set(project.schemas)) != len(project.schemas):
            raise ValueError("Project schemas must be nonempty and unique")
        if any(
            re.fullmatch(r"[_A-Za-z][_0-9A-Za-z]*", name) is None
            or keyword.iskeyword(name)
            for name in project.schemas
        ):
            raise ValueError("Schema names must be valid configured binding names")
        if (
            project.default_schema is not None
            and project.default_schema not in project.schemas
        ):
            raise ValueError("Default schema must name a configured schema")
        projects[project.project] = project
        for factory in project.factories:
            if (
                not factory.module
                or any(
                    not part.isidentifier() or keyword.iskeyword(part)
                    for part in factory.module.split(".")
                )
                or unicodedata.normalize("NFKC", factory.module) != factory.module
                or not factory.export.isidentifier()
                or keyword.iskeyword(factory.export)
                or unicodedata.normalize("NFKC", factory.export) != factory.export
            ):
                raise ValueError(
                    "Factory identities must use canonical module/export names"
                )
            if (
                factory.schema_name is not None
                and factory.schema_name not in project.schemas
            ):
                raise ValueError("Factory schema must name a configured schema")
            factory_key = (project.project, factory.module, factory.export)
            if factory_key in factories or factory_key[1:] == (
                "graphql_desk",
                "init_graphql",
            ):
                raise ValueError("Duplicate or reserved factory identity")
            factories[factory_key] = factory
    uris: set[tuple[str, str]] = set()
    keys: set[tuple[str, str]] = set()
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
    return projects, factories


class _Graph:
    def __init__(
        self,
        snapshots: tuple[ModuleSnapshot, ...],
        bindings: tuple[ProjectBindings, ...],
        parses: dict[tuple[str, str], _Parsed] | None = None,
        *,
        type_symbols: bool = False,
    ) -> None:
        self.type_symbols = type_symbols
        self.reflected_type_projects: set[str] = set()
        self.documents: dict[DocumentIdentity, ResolvedCall] = {}
        self.active_documents: set[DocumentIdentity] = set()
        self.failed_modules: set[ModuleDependency] = set()
        self.ready = False
        self.bindings, self.factories = _validate_request(snapshots, bindings)
        self.modules: dict[tuple[str, str], _Module] = {}
        self.diagnostics: list[GraphDiagnostic] = []
        self.mutated: set[tuple[str, str]] = set()
        self.supplied = {(item.project, item.module) for item in snapshots}
        for snapshot in snapshots:
            key = (snapshot.project, snapshot.module)
            try:
                parsed = (
                    _parse(snapshot)
                    if parses is None
                    else parses[(snapshot.uri, snapshot.source)]
                )
                self.modules[key] = _Module(snapshot, parsed, type_symbols=type_symbols)
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
                if type_symbols and isinstance(node, (ast.Attribute, ast.Name)):
                    try:
                        registry = self.expression(
                            module,
                            node,
                            (node.lineno, node.col_offset),
                            set(),
                            probe=True,
                        )
                        if (
                            registry.kind == "external"
                            and registry.text == "sys.modules"
                        ):
                            # shortcut: registry access invalidates all project types;
                            # track literal keys when module-level precision is needed.
                            self.reflected_type_projects.add(module.snapshot.project)
                    except (_Unresolved, RecursionError):
                        pass
                roots: list[tuple[ast.expr, bool, bool]] = []
                if isinstance(node, (ast.Attribute, ast.Subscript)) and isinstance(
                    node.ctx, (ast.Store, ast.Del)
                ):
                    root = node.value
                    while isinstance(root, (ast.Attribute, ast.Subscript)):
                        root = root.value
                    roots.append((root, True, False))
                elif isinstance(node, ast.Call):
                    if node.args and self.namespace_reflection(
                        module, node.func, set(), identities=_SETTERS
                    ):
                        root = node.args[0]
                        while isinstance(root, (ast.Attribute, ast.Subscript)):
                            root = root.value
                        roots.append((root, True, False))
                    if self.namespace_reflection(module, node.func, set()):
                        module.star = True
                    try:
                        callee = self.expression(
                            module,
                            node.func,
                            (node.lineno, node.col_offset),
                            set(),
                        )
                        trusted = callee.kind in {"callable", "initializer"}
                    except (_Unresolved, RecursionError):
                        trusted = False
                    if not trusted:
                        if isinstance(node.func, ast.Attribute):
                            roots.append((node.func.value, False, True))
                        pending = [*node.args, *(item.value for item in node.keywords)]
                        while pending:
                            argument = pending.pop()
                            roots.append((argument, False, False))
                            if isinstance(argument, (ast.List, ast.Tuple, ast.Set)):
                                pending.extend(argument.elts)
                            elif isinstance(argument, ast.Dict):
                                pending.extend(
                                    item for item in argument.keys if item is not None
                                )
                                pending.extend(argument.values)
                for root, mutates, dict_receiver in roots:
                    # Opaque code can expose or write namespace capabilities.
                    if self.namespace_reflection(module, root, set()):
                        module.star = True
                    if mutates:
                        values = self.mutation_origins(module, root)
                    else:
                        values = self.mutation_origins(
                            module, root, escape_only=True, dict_only=dict_receiver
                        )
                    for value in values:
                        if value.kind == "module":
                            self.mutated.add((module.snapshot.project, value.text))
                            if value.text == "builtins":
                                module.star = True
                        elif value.kind in {"callable", "initializer"}:
                            self.mutated.add(
                                (module.snapshot.project, value.text.rsplit(".", 1)[0])
                            )
                        elif value.kind == "document-candidate" and mutates:
                            target = self.modules.get(
                                (module.snapshot.project, value.text)
                            )
                            if target is not None:
                                target.star = True
        self.ready = True

    def mutation_origins(
        self,
        module: _Module,
        node: ast.expr,
        *,
        escape_only: bool = False,
        dict_only: bool = False,
    ) -> list[_Value]:
        """Trace write/escape capabilities; method receivers require __dict__."""
        pending = [(module, node, not dict_only)]
        seen: set[tuple[ModuleDependency, str, bool]] = set()
        origins: list[_Value] = []
        while pending:
            owner, expression, capability = pending.pop()
            try:
                value = self.expression(
                    owner,
                    expression,
                    (len(owner.snapshot.source) + 2, 0),
                    set(),
                    probe=True,
                )
                if capability and value.kind in {
                    "module",
                    "callable",
                    "initializer",
                    "document-candidate",
                }:
                    origins.append(value)
                    continue
            except (_Unresolved, RecursionError):
                pass
            if isinstance(expression, ast.Name):
                key = (owner.identity, expression.id, capability)
                if key in seen:
                    continue
                seen.add(key)
                for _, definition in owner.bindings.get(expression.id, []):
                    if isinstance(definition, ast.expr):
                        pending.append((owner, definition, capability))
                    elif isinstance(definition, tuple) and definition[1]:
                        target = self.modules.get(
                            (owner.snapshot.project, definition[0])
                        )
                        if target is not None:
                            pending.append(
                                (
                                    target,
                                    ast.Name(id=definition[1], ctx=ast.Load()),
                                    capability,
                                )
                            )
            elif isinstance(expression, (ast.Attribute, ast.Subscript)):
                if escape_only:
                    if (
                        isinstance(expression, ast.Attribute)
                        and expression.attr == "__dict__"
                    ):
                        pending.append((owner, expression.value, True))
                    elif dict_only and isinstance(expression, ast.Subscript):
                        pending.append((owner, expression.value, capability))
                    continue
                if isinstance(expression, ast.Attribute):
                    try:
                        parent = self.expression(
                            owner,
                            expression.value,
                            (len(owner.snapshot.source) + 2, 0),
                            set(),
                            probe=True,
                        )
                        target = (
                            self.modules.get((owner.snapshot.project, parent.text))
                            if parent.kind == "module"
                            else None
                        )
                        if target is not None:
                            pending.append(
                                (
                                    target,
                                    ast.Name(id=expression.attr, ctx=ast.Load()),
                                    capability,
                                )
                            )
                    except (_Unresolved, RecursionError):
                        pass
                pending.append((owner, expression.value, capability))
            elif isinstance(expression, ast.Starred):
                pending.append((owner, expression.value, capability))
            elif isinstance(expression, (ast.List, ast.Tuple, ast.Set)):
                pending.extend(
                    (owner, element, capability) for element in expression.elts
                )
            elif isinstance(expression, ast.Dict):
                pending.extend(
                    (owner, value, capability) for value in expression.values
                )
        return origins

    def namespace_reflection(
        self,
        module: _Module,
        node: ast.expr,
        seen: set[str],
        *,
        identities: frozenset[str] = _REFLECTION,
    ) -> bool:
        try:
            value = self.expression(
                module, node, (len(module.snapshot.source) + 2, 0), set(), probe=True
            )
            if value.kind == "external" and value.text in identities:
                return True
        except (_Unresolved, RecursionError):
            pass
        pending = [node]
        while pending:
            current = pending.pop()
            if not isinstance(current, ast.Name) or current.id in seen:
                continue
            if f"builtins.{current.id}" in identities:
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
        factory = self.factories.get(
            (module.snapshot.project, module.snapshot.module, name)
        )
        if probe and factory is not None:
            return _Value("callable", f"{factory.module}.{factory.export}")
        if (
            not probe
            and factory is not None
            and (module.snapshot.project, module.snapshot.module) in self.mutated
        ):
            raise _Unresolved(
                "Configured factory namespace is mutated in the supplied project"
            )
        if not probe and (module.star or name in module.blocked):
            raise _Unresolved(f"Binding {name!r} is shadowed or mutation-dependent")
        if (
            self.type_symbols
            and name not in module.bindings
            and f"builtins.{name}" in _TYPE_BASES
        ):
            return _Value("type", f"builtins.{name}")
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
                if isinstance(definition, _Value):
                    value = definition
                    if self.type_symbols and name in module.annotated_types:
                        alias = module.annotated_types[name]
                        marker = self.expression(
                            module,
                            alias.annotation,
                            (alias.lineno, alias.col_offset),
                            active,
                        )
                        if not (
                            marker.kind == "external"
                            and marker.text
                            in {"typing.TypeAlias", "typing_extensions.TypeAlias"}
                            and isinstance(alias.value, (ast.Name, ast.Attribute))
                        ):
                            raise _Unresolved(
                                "Annotated type aliases require an external TypeAlias "
                                "marker and a supported named target"
                            )
                        value = self.expression(
                            module,
                            alias.value,
                            (alias.lineno, alias.col_offset),
                            active,
                        )
                        if value.kind not in {"type", "external"}:
                            raise _Unresolved(
                                "Explicit type alias target is not a supported "
                                "type declaration"
                            )
                    elif self.type_symbols and value.kind == "type":
                        declaration = module.types[name]
                        bases = [
                            self.expression(
                                module,
                                base,
                                (declaration.lineno, declaration.col_offset),
                                active,
                            )
                            for base in declaration.bases
                        ]
                        if any(
                            not (
                                base.kind == "type"
                                or base.kind == "external"
                                and base.text in _TYPE_BASES
                            )
                            for base in bases
                        ):
                            raise _Unresolved(
                                "Class bases are not statically established types"
                            )
                        typed_dict = any(
                            base.kind == "external"
                            and base.text
                            in {"typing.TypedDict", "typing_extensions.TypedDict"}
                            for base in bases
                        )
                        keywords = {"total"}
                        if any(
                            base.kind == "external"
                            and base.text == "typing_extensions.TypedDict"
                            for base in bases
                        ):
                            keywords.add("closed")
                        if declaration.keywords and not (
                            typed_dict
                            and all(
                                item.arg in keywords
                                and isinstance(item.value, ast.Constant)
                                and isinstance(item.value.value, bool)
                                for item in declaration.keywords
                            )
                        ):
                            raise _Unresolved(
                                "Class metaclass or keyword semantics are unresolved"
                            )
                elif isinstance(definition, tuple):
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
                schema_name=value.schema_name,
                document=value.document,
            )
            if factory is not None:
                value = self.declared_factory(
                    module.snapshot.project,
                    factory.module,
                    factory.export,
                    factory,
                    value,
                    probe=probe,
                )
            if (
                not probe
                or value.kind in {"callable", "initializer"}
                or value.text == "graphql_desk"
            ):
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
        factory = self.factories.get((caller.snapshot.project, imported, symbol))
        if target is None and imported == "graphql_desk" and symbol == "init_graphql":
            return _Value("initializer", "graphql_desk.init_graphql")
        builtin = (
            target is None
            and imported == "graphql_desk"
            and symbol in {"graphql", "gql"}
        )
        value: _Value | None = None
        if target is not None and (
            symbol in target.bindings or symbol in target.blocked or target.star
        ):
            try:
                value = self.name(
                    target,
                    symbol,
                    (len(target.snapshot.source) + 2, 0),
                    active,
                    probe=probe,
                )
            except _Unresolved:
                if factory is None or not probe:
                    raise

        if factory is not None or builtin:
            if (
                factory is not None
                and target is not None
                and value is None
                and not probe
            ):
                raise _Unresolved(
                    "Configured factory export is missing or not statically callable"
                )
            return self.declared_factory(
                caller.snapshot.project, imported, symbol, factory, value, probe=probe
            )

        if value is not None:
            return value
        if target is not None and (
            "__getattr__" in target.blocked or "__getattr__" in target.bindings
        ):
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
        if self.type_symbols and target is not None:
            raise _MissingType(f"Captured module {imported!r} has no export {symbol!r}")
        return _Value("external", f"{imported}.{symbol}")

    def declared_factory(
        self,
        project: str,
        imported: str,
        symbol: str,
        factory: FactoryBinding | None,
        value: _Value | None,
        *,
        probe: bool,
    ) -> _Value:
        if (
            not probe
            and value is not None
            and value.kind not in {"opaque", "external", "callable"}
        ):
            raise _Unresolved("Configured factory export is not a callable binding")
        schema = None if probe else self.factory_schema(project, factory)
        if (
            value is not None
            and not probe
            and value.schema_name is not None
            and schema != value.schema_name
        ):
            raise _Unresolved(
                "Configured factory conflicts with its resolved schema binding"
            )
        return _Value(
            "callable",
            f"{imported}.{symbol}",
            dependencies=value.dependencies if value else frozenset(),
            schema_name=schema,
        )

    def factory_schema(
        self, project: str, factory: FactoryBinding | None
    ) -> str | None:
        facts = self.bindings.get(project)
        if facts is None:
            return None
        schema = (
            factory.schema_name
            if factory and factory.schema_name
            else facts.default_schema
        )
        if schema is None:
            raise _Unresolved(
                "Schema binding is ambiguous; import an explicitly bound factory"
            )
        return schema

    def initialize(
        self, module: _Module, node: ast.Call, active: set[tuple[str, str, str]]
    ) -> _Value:
        if (
            len(node.args) == 1
            and not node.keywords
            and not isinstance(node.args[0], ast.Starred)
        ):
            argument = node.args[0]
        elif (
            not node.args
            and len(node.keywords) == 1
            and node.keywords[0].arg == "schema"
        ):
            argument = node.keywords[0].value
        else:
            raise _Unresolved("init_graphql requires one static schema argument")
        schema = self.expression(
            module, argument, (node.lineno, node.col_offset), active
        )
        if (
            schema.kind != "string"
            or re.fullmatch(r"[_A-Za-z][_0-9A-Za-z]*", schema.text) is None
        ):
            raise _Unresolved(
                "init_graphql requires a statically resolved valid schema name"
            )
        facts = self.bindings.get(module.snapshot.project)
        if facts is not None and schema.text not in facts.schemas:
            raise _Unresolved(f"Unknown configured schema: {schema.text!r}")
        return _Value(
            "callable",
            "graphql_desk.graphql",
            dependencies=schema.dependencies,
            schema_name=schema.text,
        )

    def expression(
        self,
        module: _Module,
        node: ast.expr,
        before: tuple[int, int],
        active: set[tuple[str, str, str]],
        *,
        probe: bool = False,
    ) -> _Value:
        if self.type_symbols and (
            isinstance(node, (ast.List, ast.Tuple, ast.Dict, ast.Set, ast.Lambda))
            or isinstance(node, ast.Constant)
            and not isinstance(node.value, str)
        ):
            return _Value("value", "")
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
                    schema_name=value.schema_name,
                    document=value.document,
                )
        if isinstance(node, ast.Call):
            callee = self.expression(module, node.func, before, active, probe=probe)
            if callee.kind == "initializer":
                if probe:
                    return _Value("callable", "graphql_desk.graphql")
                value = self.initialize(module, node, active)
                return _Value(
                    value.kind,
                    value.text,
                    dependencies=value.dependencies | callee.dependencies,
                    schema_name=value.schema_name,
                )
            if callee.kind == "callable":
                if probe or not self.ready:
                    return _Value("document-candidate", module.snapshot.module)
                document = self.document(module, node, callee)
                return _Value(
                    "document",
                    "",
                    dependencies=frozenset(document.dependencies),
                    schema_name=document.schema_name,
                    document=DocumentIdentity(module.identity, document.range),
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

    def document(
        self, module: _Module, node: ast.Call, callable_: _Value
    ) -> ResolvedCall:
        identity = DocumentIdentity(module.identity, module.span(node))
        cached = self.documents.get(identity)
        if cached is not None:
            if self.failed_modules.intersection(cached.dependencies):
                raise _Unresolved(
                    "Document depends on a module that failed source resolution"
                )
            return cached
        if identity in self.active_documents:
            raise _Unresolved("Static document binding cycle cannot be resolved")
        self.active_documents.add(identity)
        try:
            document = self.extract(module, node, callable_)
        finally:
            self.active_documents.remove(identity)
        self.documents[identity] = document
        return document

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
        references: list[FragmentReference] = []
        reference_dependencies: frozenset[ModuleDependency] = frozenset()
        schemas = (
            {callable_.schema_name} if callable_.schema_name is not None else set[str]()
        )
        if fragments is not None:
            if not isinstance(fragments, (ast.List, ast.Tuple)):
                raise _Unresolved("Fragment arguments require a literal list or tuple")
            for element in fragments.elts:
                if isinstance(element, ast.Starred):
                    raise _Unresolved("Starred fragment references are not static")
                value = self.expression(
                    module, element, (node.lineno, node.col_offset), set()
                )
                if value.kind != "document" or value.document is None:
                    raise _Unresolved(
                        "Fragment arguments require resolved static document bindings"
                    )
                references.append(
                    FragmentReference(
                        value.document, module.identity, module.span(element)
                    )
                )
                reference_dependencies |= value.dependencies
                if value.schema_name is not None:
                    schemas.add(value.schema_name)
            if len(schemas) > 1:
                raise _Unresolved(
                    "Fragment references belong to different schema bindings"
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
                source.dependencies
                | callable_.dependencies
                | reference_dependencies
                | {module.identity},
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
            schema_name=next(iter(schemas), None),
            fragments=tuple(references),
        )


def resolve_project(
    modules: tuple[ModuleSnapshot, ...], *, bindings: tuple[ProjectBindings, ...] = ()
) -> ProjectSources:
    """Resolve stable module calls in caller-selected snapshots; no discovery."""
    return _resolve(modules, bindings)


def _resolve(
    modules: tuple[ModuleSnapshot, ...],
    bindings: tuple[ProjectBindings, ...],
    parses: dict[tuple[str, str], _Parsed] | None = None,
) -> ProjectSources:
    graph = _Graph(modules, bindings, parses)
    calls: list[ResolvedCall] = []
    unavailable: list[RejectedCall] = []
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
                document_candidate = False

                def reject(diagnostic: GraphDiagnostic) -> None:
                    graph.diagnostics.append(diagnostic)
                    if document_candidate:
                        unavailable.append(
                            RejectedCall(
                                module.identity,
                                module.span(node),
                                module.call_bindings.get(id(node)),
                                (diagnostic,),
                            )
                        )

                try:
                    callable_ = graph.expression(
                        module, node.func, (node.lineno, node.col_offset), set()
                    )
                    if callable_.kind in {"callable", "initializer"}:
                        candidate = True
                        document_candidate = callable_.kind == "callable"
                        if self.scoped:
                            raise _Unresolved(
                                "Nested scopes require a later project resolver slice"
                            )
                        if callable_.kind == "initializer":
                            graph.expression(
                                module, node, (node.lineno, node.col_offset), set()
                            )
                        else:
                            calls.append(graph.document(module, node, callable_))
                except RecursionError:
                    if candidate:
                        reject(
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
                            candidate = origin.kind in {"callable", "initializer"}
                            document_candidate = origin.kind == "callable"
                        except (_Unresolved, RecursionError):
                            pass
                    if candidate:
                        reject(
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
        rejected_start = len(unavailable)
        try:
            Calls().visit(module.tree)
        except RecursionError:
            del calls[start:]
            del unavailable[rejected_start:]
            module.star = True
            graph.failed_modules.add(module.identity)
            graph.diagnostics.append(
                GraphDiagnostic(
                    module.identity,
                    "source-too-complex",
                    "Python syntax nesting exceeds the resolver traversal limit",
                    Span(0, len(module.snapshot.source)),
                )
            )
    current = tuple(
        call
        for call in calls
        if not graph.failed_modules.intersection(call.dependencies)
    )
    for call in calls:
        failed = graph.failed_modules.intersection(call.dependencies)
        if failed:
            unavailable.append(
                RejectedCall(
                    call.module,
                    call.range,
                    call.binding,
                    tuple(item for item in graph.diagnostics if item.module in failed),
                )
            )
    order = {(item.project, item.module): index for index, item in enumerate(modules)}
    unavailable.sort(
        key=lambda item: (
            order[(item.module.project, item.module.module)],
            item.range.start,
        )
    )
    return ProjectSources(current, tuple(graph.diagnostics), tuple(unavailable))


@dataclass(frozen=True)
class _ProjectResult:
    modules: tuple[ModuleSnapshot, ...]
    bindings: tuple[ProjectBindings, ...]
    result: ProjectSources


class IncrementalResolver:
    """Reuse parsing and unchanged projects across explicit synchronous requests.

    Changed projects rerun all semantic analysis, including failed and namespace
    lookups. The caller still owns freshness and publication authorization.
    """

    def __init__(self) -> None:
        self._parses: dict[tuple[str, str], _Parsed] = {}
        self._projects: dict[str, _ProjectResult] = {}

    def clear(self) -> None:
        """Release all retained inputs and results."""
        self._parses.clear()
        self._projects.clear()

    def resolve(
        self,
        modules: tuple[ModuleSnapshot, ...],
        *,
        bindings: tuple[ProjectBindings, ...] = (),
    ) -> ProjectSources:
        _validate_request(modules, bindings)
        parses: dict[tuple[str, str], _Parsed] = {}
        for snapshot in modules:
            key = (snapshot.uri, snapshot.source)
            if key not in parses:
                parses[key] = (
                    self._parses[key] if key in self._parses else _parse(snapshot)
                )
        grouped: dict[str, list[ModuleSnapshot]] = {}
        for snapshot in modules:
            grouped.setdefault(snapshot.project, []).append(snapshot)
        contexts = {item.project: (item,) for item in bindings}
        projects: dict[str, _ProjectResult] = {}
        for project, snapshots in grouped.items():
            selected = tuple(snapshots)
            context = contexts.get(project, ())
            previous = self._projects.get(project)
            if previous is not None and (previous.modules, previous.bindings) == (
                selected,
                context,
            ):
                projects[project] = previous
            else:
                projects[project] = _ProjectResult(
                    selected, context, _resolve(selected, context, parses)
                )
        order = {
            (item.project, item.module): index for index, item in enumerate(modules)
        }
        calls = [call for project in projects.values() for call in project.result.calls]
        diagnostics = [
            diagnostic
            for project in projects.values()
            for diagnostic in project.result.diagnostics
        ]
        calls.sort(key=lambda call: order[(call.module.project, call.module.module)])
        # Cold resolution reports every parse error before any call traversal error.
        diagnostics.sort(
            key=lambda item: (
                item.code != "invalid-python",
                order[(item.module.project, item.module.module)],
            )
        )
        unavailable = [
            item for project in projects.values() for item in project.result.unavailable
        ]
        unavailable.sort(
            key=lambda item: (
                order[(item.module.project, item.module.module)],
                item.range.start,
            )
        )
        result = ProjectSources(tuple(calls), tuple(diagnostics), tuple(unavailable))
        self._parses = parses
        self._projects = projects
        return result


def resolve_type_symbols(
    symbols: tuple[str, ...],
    *,
    modules: tuple[ModuleSnapshot, ...],
    project_id: str,
) -> tuple[TypeSymbolResolution, ...]:
    """Resolve captured local class exports; uncaptured symbols stay external.

    This establishes symbol availability only, not full checker type validity.
    Dependencies conservatively retain the entire supplied project snapshot,
    including negative lookups. No project files or Python objects are loaded.
    """
    if not project_id or any(
        len(symbol.split(".")) < 2
        or any(
            not part.isidentifier()
            or keyword.iskeyword(part)
            or unicodedata.normalize("NFKC", part) != part
            for part in symbol.split(".")
        )
        for symbol in symbols
    ):
        raise ValueError("Type symbols require a project and qualified canonical names")
    graph = _Graph(modules, (), type_symbols=True)
    dependencies = tuple(
        sorted(
            (
                ModuleDependency(
                    module.project,
                    module.module,
                    module.uri,
                    module.version,
                )
                for module in modules
                if module.project == project_id
            ),
            key=lambda item: (item.module, item.uri, item.version),
        )
    )
    results: list[TypeSymbolResolution] = []
    for symbol in sorted(set(symbols)):
        imported, _, name = symbol.rpartition(".")
        owner = next(
            (
                module
                for module in graph.modules.values()
                if module.snapshot.project == project_id
            ),
            None,
        )
        status: Literal["resolved", "external", "unresolved", "missing", "not-type"]
        target = None
        message = None
        if owner is None:
            captured = any(
                (project_id, ".".join(imported.split(".")[:length])) in graph.supplied
                for length in range(1, len(imported.split(".")) + 1)
            )
            status = "unresolved" if captured else "external"
            target = None if captured else symbol
            message = (
                "Captured Python source is invalid"
                if captured
                else "Symbol is outside the captured project snapshot"
            )
        else:
            try:
                if project_id in graph.reflected_type_projects:
                    raise _Unresolved(
                        "Module registry reflection prevents static type resolution"
                    )
                value = graph.export(owner, imported, name, set())
                target = value.text if value.kind in {"type", "external"} else None
                if value.kind == "type":
                    status = "resolved"
                elif value.kind == "external":
                    status = "external"
                    message = "Symbol leaves the captured project snapshot"
                elif value.kind in {
                    "opaque",
                    "string",
                    "value",
                    "module",
                    "callable",
                    "initializer",
                    "document",
                    "document-candidate",
                }:
                    status = "not-type"
                    message = "Captured export is not a supported type declaration"
                else:
                    status = "unresolved"
                    message = "Captured export has unsupported static type semantics"
            except _MissingType as error:
                status, message = "missing", str(error)
            except (_Unresolved, RecursionError) as error:
                status = "unresolved"
                message = (
                    str(error)
                    if isinstance(error, _Unresolved)
                    else "Excessive type alias graph"
                )
        results.append(
            TypeSymbolResolution(symbol, status, target, dependencies, message)
        )
    return tuple(results)
