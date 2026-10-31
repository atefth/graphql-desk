"""Conservative same-file result reads over captured Python and GraphQL ASTs."""

from __future__ import annotations

import ast
import io
import re
import tokenize
from bisect import bisect_right
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from inspect import Parameter, Signature
from typing import TypeAlias, cast

from graphql import (
    DocumentNode,
    FieldNode,
    FragmentDefinitionNode,
    FragmentSpreadNode,
    GraphQLCompositeType,
    GraphQLInterfaceType,
    GraphQLList,
    GraphQLNonNull,
    GraphQLObjectType,
    InlineFragmentNode,
    OperationDefinitionNode,
    SelectionSetNode,
    is_composite_type,
    parse,
)

from .fragments import FragmentIndex, IndexedDocument, SchemaContext
from .source import Span
from .source_graph import (
    DocumentIdentity,
    GraphDiagnostic,
    ModuleDependency,
    ModuleSnapshot,
    ResolvedCall,
)
from .source_maps import build_source_map
from .validation import validate_document

UNUSED_FIELD_CODE = "52005"
_RESERVED = frozenset({"id", "_id", "__typename"})
_REFLECTION = frozenset(
    {"exec", "eval", "globals", "locals", "vars", "setattr", "delattr"}
)
_IGNORE = re.compile(r"#\s*graphql-desk:\s*ignore\[\s*52005\s*\]\s*")


@dataclass(frozen=True)
class FieldUsageOptions:
    enabled: bool = True
    reserved_keys: tuple[str, ...] = ()
    unmasked_schemas: tuple[str, ...] = ()
    client_directives: tuple[str, ...] = ()


class _Writes(ast.NodeVisitor):
    """Bindings here, excluding nested scopes and comprehension iteration targets."""

    def __init__(self, *, include_annotations: bool = False) -> None:
        self.names: dict[str, int] = {}
        self.star = False
        self.include_annotations = include_annotations

    def add(self, name: str) -> None:
        self.names[name] = self.names.get(name, 0) + 1

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, (ast.Store, ast.Del)):
            self.add(node.id)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if isinstance(node.ctx, (ast.Store, ast.Del)):
            root: ast.expr = node.value
            while isinstance(root, (ast.Attribute, ast.Subscript)):
                root = root.value
            if isinstance(root, ast.Name):
                self.add(root.id)
        self.generic_visit(node)

    def visit_Import(self, node: ast.Import) -> None:
        for item in node.names:
            self.add(item.asname or item.name.split(".")[0])

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        for item in node.names:
            self.star |= item.name == "*"
            self.add(item.asname or item.name)

    def visit_FunctionDef(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self.add(node.name)
        for expression in (
            *node.decorator_list,
            *node.args.defaults,
            *(item for item in node.args.kw_defaults if item is not None),
        ):
            self.visit(expression)
        if self.include_annotations:
            for argument in (
                *node.args.posonlyargs,
                *node.args.args,
                *node.args.kwonlyargs,
            ):
                if argument.annotation is not None:
                    self.visit(argument.annotation)
            if node.args.vararg and node.args.vararg.annotation is not None:
                self.visit(node.args.vararg.annotation)
            if node.args.kwarg and node.args.kwarg.annotation is not None:
                self.visit(node.args.kwarg.annotation)
            if node.returns is not None:
                self.visit(node.returns)

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.add(node.name)
        for expression in (*node.decorator_list, *node.bases):
            self.visit(expression)
        for keyword in node.keywords:
            self.visit(keyword.value)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        for expression in (
            *node.args.defaults,
            *(item for item in node.args.kw_defaults if item is not None),
        ):
            self.visit(expression)

    def visit_ListComp(self, node: ast.AST) -> None:
        for name in _ComprehensionWalrusWrites().collect(node):
            self.add(name)

    visit_SetComp = visit_ListComp
    visit_DictComp = visit_ListComp
    visit_GeneratorExp = visit_ListComp

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if node.name:
            self.add(node.name)
        self.generic_visit(node)

    def visit_MatchAs(self, node: ast.MatchAs) -> None:
        if node.name:
            self.add(node.name)
        self.generic_visit(node)

    def visit_MatchStar(self, node: ast.MatchStar) -> None:
        if node.name:
            self.add(node.name)

    def visit_MatchMapping(self, node: ast.MatchMapping) -> None:
        if node.rest:
            self.add(node.rest)
        self.generic_visit(node)


class _ComprehensionWalrusWrites(ast.NodeVisitor):
    def __init__(self) -> None:
        self.names: set[str] = set()

    def collect(self, node: ast.AST) -> set[str]:
        self.visit(node)
        return self.names

    def visit_NamedExpr(self, node: ast.NamedExpr) -> None:
        self.names.add(node.target.id)
        self.visit(node.value)

    def visit_FunctionDef(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        pass

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        pass

    def visit_Lambda(self, node: ast.Lambda) -> None:
        for expression in (
            *node.args.defaults,
            *(item for item in node.args.kw_defaults if item is not None),
        ):
            self.visit(expression)


class _OuterWrites(ast.NodeVisitor):
    def __init__(self) -> None:
        self.names: set[str] = set()

    def visit_Global(self, node: ast.Global) -> None:
        self.names.update(node.names)

    def visit_Nonlocal(self, node: ast.Nonlocal) -> None:
        self.names.update(node.names)

    def visit_FunctionDef(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        return

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        return

    def visit_Lambda(self, node: ast.Lambda) -> None:
        return


class _NonlocalCaptures(ast.NodeVisitor):
    def __init__(self, function: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self.scopes = [self._locals(function)]
        self.names: set[str] = set()

    @staticmethod
    def _locals(function: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
        names = _local_names(function)
        declarations = _OuterWrites()
        for statement in function.body:
            declarations.visit(statement)
        return names - declarations.names

    def visit_Nonlocal(self, node: ast.Nonlocal) -> None:
        for name in node.names:
            for depth in range(len(self.scopes) - 1, -1, -1):
                if name in self.scopes[depth]:
                    if depth == 0:
                        self.names.add(name)
                    break

    def visit_FunctionDef(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self.scopes.append(self._locals(node))
        try:
            for statement in node.body:
                self.visit(statement)
        finally:
            self.scopes.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        for statement in node.body:
            self.visit(statement)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        return


class _ClassAnnotations(ast.NodeVisitor):
    def __init__(self, flow: _Flow, env: dict[str, _Value]) -> None:
        self.flow = flow
        self.env = env

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        _consume(self.flow.expression(node.annotation, self.env))

    def visit_FunctionDef(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        return

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        return

    def visit_Lambda(self, node: ast.Lambda) -> None:
        return


def _local_names(node: ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda) -> set[str]:
    writes = _Writes()
    if not isinstance(node, ast.Lambda):
        for statement in node.body:
            writes.visit(statement)
    names = set(writes.names)
    names.update(
        item.arg
        for item in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs)
    )
    if node.args.vararg:
        names.add(node.args.vararg.arg)
    if node.args.kwarg:
        names.add(node.args.kwarg.arg)
    return names


def _captures(statements: list[ast.stmt]) -> set[str]:
    class Captures(ast.NodeVisitor):
        def __init__(self) -> None:
            self.names: set[str] = set()

        def visit_FunctionDef(
            self, node: ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda
        ) -> None:
            local = _local_names(node)
            self.names.update(
                child.id
                for child in ast.walk(node)
                if isinstance(child, ast.Name)
                and isinstance(child.ctx, ast.Load)
                and child.id not in local
            )

        visit_AsyncFunctionDef = visit_FunctionDef
        visit_Lambda = visit_FunctionDef

    captures = Captures()
    for statement in statements:
        captures.visit(statement)
    return captures.names


class _Module:
    def __init__(self, snapshot: ModuleSnapshot, tree: ast.Module) -> None:
        self.snapshot = snapshot
        self.identity = ModuleDependency(
            snapshot.project, snapshot.module, snapshot.uri, snapshot.version
        )
        self.tree = tree
        self.lines = re.split(r"(?<=\n)|(?<=\r)(?!\n)", snapshot.source)
        self.starts = [0]
        for line in self.lines:
            self.starts.append(self.starts[-1] + len(line))
        self.exports: dict[str, str | ast.AST] = {}
        writes = _Writes()
        writes.visit(tree)
        self.blocked = {name for name, count in writes.names.items() if count != 1}
        self.star = writes.star
        checking_names = {
            name
            for item in tree.body
            if isinstance(item, (ast.Import, ast.ImportFrom))
            for name, imported in self.imports(item).items()
            if imported in ("typing.TYPE_CHECKING", "typing_extensions.TYPE_CHECKING")
            and name not in self.blocked
        }
        typing_modules = {
            name
            for item in tree.body
            if isinstance(item, ast.Import)
            for name, imported in self.imports(item).items()
            if imported in ("typing", "typing_extensions") and name not in self.blocked
        }
        exports = list(tree.body)
        for item in tree.body:
            if not isinstance(item, ast.If):
                continue
            test = item.test
            is_type_checking = (
                isinstance(test, ast.Name) and test.id in checking_names
            ) or (
                isinstance(test, ast.Attribute)
                and test.attr == "TYPE_CHECKING"
                and isinstance(test.value, ast.Name)
                and test.value.id in typing_modules
            )
            if is_type_checking:
                exports.extend(item.body)
        for item in exports:
            if isinstance(item, (ast.Import, ast.ImportFrom)):
                self.exports.update(self.imports(item))
            elif isinstance(
                item, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
            ):
                self.exports[item.name] = item
            elif isinstance(item, ast.Assign):
                for target in item.targets:
                    if isinstance(target, ast.Name):
                        self.exports[target.id] = item.value
            elif isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                if item.value is not None:
                    self.exports[item.target.id] = item.value
        self.ignored_lines: set[int] = set()
        for token in tokenize.generate_tokens(io.StringIO(snapshot.source).readline):
            if token.type == tokenize.COMMENT and _IGNORE.fullmatch(token.string):
                self.ignored_lines.add(token.start[0])

    def imports(self, node: ast.Import | ast.ImportFrom) -> dict[str, str]:
        if isinstance(node, ast.Import):
            return {
                item.asname or item.name.split(".")[0]: item.name
                if item.asname
                else item.name.split(".")[0]
                for item in node.names
            }
        parts = self.snapshot.module.split(".")
        if not self.snapshot.is_package:
            parts.pop()
        if node.level > len(parts):
            return {}
        prefix = parts[: len(parts) - node.level + 1] if node.level else []
        if node.module:
            prefix.append(node.module)
        return {
            item.asname or item.name: ".".join((*prefix, item.name))
            for item in node.names
            if item.name != "*"
        }

    def span(self, node: ast.expr) -> Span:
        def offset(line: int, column: int) -> int:
            return self.starts[line - 1] + len(
                self.lines[line - 1].encode("utf-8")[:column].decode("utf-8")
            )

        return Span(
            offset(node.lineno, node.col_offset),
            offset(
                node.end_lineno or node.lineno, node.end_col_offset or node.col_offset
            ),
        )


_Target: TypeAlias = str | tuple[_Module, ast.AST] | None


class _Symbols:
    def __init__(self, modules: dict[tuple[str, str], _Module]) -> None:
        self.modules = modules

    def imported(
        self, project: str, name: str, seen: frozenset[tuple[str, str]] = frozenset()
    ) -> _Target:
        key = (project, name)
        if key in seen:
            return None
        parts = name.split(".")
        for split in range(len(parts), 0, -1):
            owner = self.modules.get((project, ".".join(parts[:split])))
            if owner is not None:
                if split == len(parts):
                    return name
                if len(parts) != split + 1:
                    return None
                return self.expression(
                    owner, ast.Name(id=parts[-1], ctx=ast.Load()), seen | {key}
                )
        return name

    def expression(
        self,
        owner: _Module,
        node: ast.expr,
        seen: frozenset[tuple[str, str]] = frozenset(),
    ) -> _Target:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            try:
                return self.expression(
                    owner, ast.parse(node.value, mode="eval").body, seen
                )
            except (SyntaxError, ValueError, RecursionError):
                return None
        if isinstance(node, ast.Attribute):
            base = self.expression(owner, node.value, seen)
            return (
                self.imported(owner.snapshot.project, f"{base}.{node.attr}", seen)
                if isinstance(base, str)
                else None
            )
        if not isinstance(node, ast.Name) or owner.star or node.id in owner.blocked:
            return None
        key = (owner.snapshot.module, node.id)
        if key in seen:
            return None
        value = owner.exports.get(node.id)
        if isinstance(value, str):
            return self.imported(owner.snapshot.project, value, seen | {key})
        if isinstance(value, (ast.Name, ast.Attribute)):
            return self.expression(owner, value, seen | {key})
        if isinstance(value, ast.AST):
            return owner, value
        if node.id in {"tuple", "list", "property"}:
            return f"builtins.{node.id}"
        return None


@dataclass
class _Node:
    path: tuple[str, ...]
    depth: int = 0
    leaf: bool = False
    children: dict[str, _Node] = field(default_factory=dict[str, "_Node"])
    fields: list[tuple[ResolvedCall, FieldNode]] = field(
        default_factory=list[tuple[ResolvedCall, FieldNode]]
    )


@dataclass
class _State:
    document: IndexedDocument
    root: _Node
    used: set[tuple[str, ...]] = field(default_factory=set[tuple[str, ...]])
    escaped: set[tuple[str, ...]] = field(default_factory=set[tuple[str, ...]])
    accesses: int = 0


@dataclass(frozen=True)
class _Path:
    state: _State
    node: _Node
    depth: int
    wrappers: tuple[str, ...] = ()


@dataclass(frozen=True)
class _Reference:
    paths: tuple[_Path, ...]


@dataclass(frozen=True)
class _Document:
    document: IndexedDocument


@dataclass(frozen=True)
class _Callable:
    target: _Target


@dataclass(frozen=True)
class _Awaited:
    value: _Value


_Value: TypeAlias = (
    _Reference | _Document | _Callable | _Awaited | tuple["_Value", ...] | None
)


@dataclass(frozen=True)
class _Return:
    wrappers: tuple[str, ...] = ()
    items: tuple[_Return | None, ...] | None = None
    awaited: bool = False


def _annotation(node: ast.expr | None) -> ast.expr | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        try:
            return ast.parse(node.value, mode="eval").body
        except (SyntaxError, ValueError, RecursionError):
            return None
    return node


def _optional(node: ast.expr | None) -> ast.expr | None:
    node = _annotation(node)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        for value, other in ((node.left, node.right), (node.right, node.left)):
            if isinstance(other, ast.Constant) and other.value is None:
                return _optional(value)
    return node


def _args(node: ast.Subscript) -> tuple[ast.expr, ...]:
    return (
        tuple(node.slice.elts) if isinstance(node.slice, ast.Tuple) else (node.slice,)
    )


def _return_shape(
    owner: _Module, annotation: ast.expr | None, parameter: str, symbols: _Symbols
) -> _Return | None:
    annotation = _optional(annotation)
    if isinstance(annotation, ast.Name) and annotation.id == parameter:
        return _Return()
    if not isinstance(annotation, ast.Subscript):
        return None
    base = symbols.expression(owner, annotation.value)
    arguments = _args(annotation)
    if base == "typing.Optional" and len(arguments) == 1:
        return _return_shape(owner, arguments[0], parameter, symbols)
    if (
        base in ("typing.Awaitable", "collections.abc.Awaitable")
        and len(arguments) == 1
    ):
        inner = _return_shape(owner, arguments[0], parameter, symbols)
        if inner is not None:
            return replace(inner, awaited=True)
    if base in ("builtins.tuple", "typing.Tuple"):
        items = tuple(
            _return_shape(owner, item, parameter, symbols) for item in arguments
        )
        if any(item is not None for item in items) and not any(
            isinstance(item, ast.Constant) and item.value is Ellipsis
            for item in arguments
        ):
            return _Return(items=items)
    if not isinstance(base, tuple) or not isinstance(base[1], ast.ClassDef):
        return None
    class_owner, definition = base
    assert isinstance(definition, ast.ClassDef)
    parameters: tuple[ast.expr, ...] = ()
    for parent in definition.bases:
        if isinstance(parent, ast.Subscript) and symbols.expression(
            class_owner, parent.value
        ) in ("typing.Generic", "typing.Protocol", "typing_extensions.Protocol"):
            parameters = _args(parent)
    if len(parameters) != len(arguments) or definition.decorator_list:
        return None
    data_type: ast.expr | None = None
    for member in definition.body:
        if (
            isinstance(member, ast.AnnAssign)
            and isinstance(member.target, ast.Name)
            and member.target.id == "data"
        ):
            if data_type is not None:
                return None
            data_type = member.annotation
        elif isinstance(member, ast.FunctionDef) and member.name == "data":
            if (
                data_type is not None
                or len(member.decorator_list) != 1
                or symbols.expression(class_owner, member.decorator_list[0])
                != "builtins.property"
            ):
                return None
            data_type = member.returns
    for name, argument in zip(parameters, arguments, strict=True):
        if (
            isinstance(name, ast.Name)
            and isinstance(argument, ast.Name)
            and argument.id == parameter
        ):
            inner = _return_shape(class_owner, data_type, name.id, symbols)
            if inner is not None and inner.items is None:
                return _Return(("data", *inner.wrappers))
    return None


def _signature(target: _Target, symbols: _Symbols) -> tuple[str, _Return] | None:
    if not isinstance(target, tuple):
        return None
    owner, function = target
    if (
        not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef))
        or function.decorator_list
        or function.args.vararg
        or function.args.kwarg
    ):
        return None
    parameters = (
        *function.args.posonlyargs,
        *function.args.args,
        *function.args.kwonlyargs,
    )
    matches: list[tuple[str, _Return]] = []
    for argument in parameters:
        annotation = _annotation(argument.annotation)
        if not isinstance(annotation, ast.Subscript) or symbols.expression(
            owner, annotation.value
        ) not in (
            "graphql_desk.TypedDocument",
            "graphql_desk.typed_document.TypedDocument",
        ):
            continue
        parts = _args(annotation)
        if len(parts) != 2 or not isinstance(parts[0], ast.Name):
            continue
        result_parameter = parts[0].id
        parameter = symbols.expression(owner, parts[0])
        is_typevar = (
            isinstance(parameter, tuple)
            and isinstance(parameter[1], ast.Call)
            and symbols.expression(parameter[0], parameter[1].func)
            in ("typing.TypeVar", "typing_extensions.TypeVar")
        )
        is_typevar |= any(
            getattr(item, "name", None) == result_parameter
            for item in getattr(function, "type_params", ())
        )
        if not is_typevar:
            continue
        shape = _return_shape(owner, function.returns, result_parameter, symbols)
        if shape is None:
            continue
        if isinstance(function, ast.AsyncFunctionDef):
            if shape.awaited:
                continue
            shape = replace(shape, awaited=True)
        matches.append((argument.arg, shape))
    return matches[0] if len(matches) == 1 else None


def _bind_call(
    function: ast.FunctionDef | ast.AsyncFunctionDef,
    values: list[_Value],
    keywords: dict[str | None, _Value],
) -> dict[str, _Value] | None:
    positional = (*function.args.posonlyargs, *function.args.args)
    first_default = len(positional) - len(function.args.defaults)
    parameters = [
        Parameter(
            argument.arg,
            Parameter.POSITIONAL_ONLY
            if position < len(function.args.posonlyargs)
            else Parameter.POSITIONAL_OR_KEYWORD,
            default=None if position >= first_default else Parameter.empty,
        )
        for position, argument in enumerate(positional)
    ]
    parameters.extend(
        Parameter(
            argument.arg,
            Parameter.KEYWORD_ONLY,
            default=None if default is not None else Parameter.empty,
        )
        for argument, default in zip(
            function.args.kwonlyargs, function.args.kw_defaults, strict=True
        )
    )
    if None in keywords:
        return None
    try:
        bound = Signature(parameters).bind(
            *values,
            **{name: value for name, value in keywords.items() if name is not None},
        )
    except TypeError:
        return None
    return cast(dict[str, _Value], bound.arguments)


def _type_shape(type_: object) -> tuple[int, object]:
    depth = 0
    while isinstance(type_, (GraphQLNonNull, GraphQLList)):
        depth += int(isinstance(type_, GraphQLList))
        # graphql-core's wrapper annotations contain unparameterized generics.
        type_ = cast(object, type_.of_type)
    return depth, type_


def _state(
    document: IndexedDocument,
    syntax: Mapping[DocumentIdentity, tuple[ResolvedCall, DocumentNode]],
    context: SchemaContext,
    options: FieldUsageOptions,
) -> _State | None:
    selected = document.selected_operation or document.primary_fragment
    if selected is None or selected.kind not in ("query", "fragment"):
        return None
    definitions: dict[
        tuple[DocumentIdentity, str | None],
        tuple[ResolvedCall, OperationDefinitionNode | FragmentDefinitionNode],
    ] = {}
    fragments: dict[str, tuple[ResolvedCall, FragmentDefinitionNode]] = {}
    for fact in document.definitions:
        call, parsed = syntax[fact.document]
        for node in parsed.definitions:
            if isinstance(node, (OperationDefinitionNode, FragmentDefinitionNode)):
                name = node.name.value if node.name else None
                definitions[(fact.document, name)] = (call, node)
                if isinstance(node, FragmentDefinitionNode):
                    fragments[node.name.value] = call, node
    call, node = definitions[(selected.document, selected.name)]
    validation_nodes = (
        node,
        *(item[1] for item in fragments.values() if item[1] is not node),
    )
    if validate_document(
        context.schema,
        DocumentNode(definitions=validation_nodes),
        client_directives=options.client_directives,
    ):
        return None
    parent = (
        context.schema.query_type
        if isinstance(node, OperationDefinitionNode)
        else context.schema.get_type(node.type_condition.name.value)
    )
    if not is_composite_type(parent):
        return None
    root = _Node(())

    def collect(
        selection: SelectionSetNode,
        type_: GraphQLCompositeType,
        target: _Node,
        owner: ResolvedCall,
        active: frozenset[str],
    ) -> None:
        for item in selection.selections:
            if isinstance(item, FieldNode):
                name = item.alias.value if item.alias else item.name.value
                child = target.children.setdefault(name, _Node((*target.path, name)))
                child.leaf |= item.selection_set is None
                child.fields.append((owner, item))
                if item.name.value == "__typename":
                    continue
                if (
                    not isinstance(type_, (GraphQLObjectType, GraphQLInterfaceType))
                    or item.name.value not in type_.fields
                ):
                    continue
                field_type = cast(object, type_.fields[item.name.value].type)
                child.depth, named = _type_shape(field_type)
                if item.selection_set is not None and is_composite_type(named):
                    collect(item.selection_set, named, child, owner, active)
            elif isinstance(item, InlineFragmentNode):
                condition = (
                    context.schema.get_type(item.type_condition.name.value)
                    if item.type_condition
                    else type_
                )
                if is_composite_type(condition):
                    collect(item.selection_set, condition, target, owner, active)
            elif isinstance(item, FragmentSpreadNode) and item.name.value not in active:
                fragment_owner, fragment = fragments[item.name.value]
                if document.schema_identity[
                    1
                ] not in options.unmasked_schemas and not any(
                    d.name.value == "_unmask" for d in fragment.directives or ()
                ):
                    continue
                condition = context.schema.get_type(fragment.type_condition.name.value)
                if is_composite_type(condition):
                    collect(
                        fragment.selection_set,
                        condition,
                        target,
                        fragment_owner,
                        active | {item.name.value},
                    )

    collect(node.selection_set, parent, root, call, frozenset())
    return _State(document, root)


def _consume(value: _Value) -> None:
    if isinstance(value, _Reference):
        for path in value.paths:
            path.state.escaped.add(path.node.path)
            path.state.accesses += 1
    elif isinstance(value, tuple):
        for item in value:
            _consume(item)
    elif isinstance(value, _Awaited):
        _consume(value.value)


class _Flow:
    def __init__(
        self,
        owner: _Module,
        symbols: _Symbols,
        documents: dict[DocumentIdentity, IndexedDocument],
        states: dict[DocumentIdentity, _State],
    ) -> None:
        self.owner = owner
        self.symbols = symbols
        self.documents = documents
        self.states = states
        self.functions: list[ast.FunctionDef | ast.AsyncFunctionDef] = []
        self.class_functions: set[int] = set()
        self.function_classes: dict[int, ast.ClassDef] = {}
        self.class_stack: list[ast.ClassDef] = []
        self.unsafe_local_class_functions: set[int] = set()
        self.function_captures: dict[int, dict[str, _Value]] = {}
        self.class_capture_scope: dict[str, _Value] | None = None
        self.current_function: ast.FunctionDef | ast.AsyncFunctionDef | None = None
        self.current_class: ast.ClassDef | None = None
        self.module_env: dict[str, _Value] = {}
        self.class_definitions = {
            name: target
            for name, target in owner.exports.items()
            if isinstance(target, ast.ClassDef) and name not in owner.blocked
        }
        self.mutated_classes: set[int] = set()

        def resolve_class(
            name: str, seen: frozenset[str] = frozenset()
        ) -> ast.ClassDef | None:
            if name in seen:
                return None
            target = owner.exports.get(name)
            if isinstance(target, ast.ClassDef):
                return target
            if isinstance(target, ast.Name):
                return resolve_class(target.id, seen | {name})
            return None

        def mark_mutated_class(node: ast.expr) -> None:
            if isinstance(node, ast.Name):
                target = resolve_class(node.id)
                if target is not None:
                    self.mutated_classes.add(id(target))

        mutator_names = {"setattr", "delattr"}

        class _MutationBindings(ast.NodeVisitor):
            def __init__(self) -> None:
                self.assignments: list[tuple[str, ast.expr]] = []
                self.attribute_writes: list[ast.expr] = []
                self.imported_mutators: set[str] = set()
                self.imported_modules: set[str] = set()
                self.calls: list[ast.Call] = []
                self.functions: list[ast.FunctionDef | ast.AsyncFunctionDef] = []
                self.classes: list[ast.ClassDef] = []

            def visit_Assign(self, node: ast.Assign) -> None:
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        self.assignments.append((target.id, node.value))
                    elif isinstance(target, ast.Attribute):
                        self.attribute_writes.append(target.value)
                self.generic_visit(node)

            def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
                if isinstance(node.target, ast.Name) and node.value is not None:
                    self.assignments.append((node.target.id, node.value))
                elif isinstance(node.target, ast.Attribute):
                    self.attribute_writes.append(node.target.value)
                self.generic_visit(node)

            def visit_AugAssign(self, node: ast.AugAssign) -> None:
                if isinstance(node.target, ast.Attribute):
                    self.attribute_writes.append(node.target.value)
                self.generic_visit(node)

            def visit_Delete(self, node: ast.Delete) -> None:
                self.attribute_writes.extend(
                    target.value
                    for target in node.targets
                    if isinstance(target, ast.Attribute)
                )
                self.generic_visit(node)

            def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
                if node.module == "builtins":
                    self.imported_mutators.update(
                        item.asname or item.name
                        for item in node.names
                        if item.name in mutator_names
                    )

            def visit_Import(self, node: ast.Import) -> None:
                self.imported_modules.update(
                    item.asname or item.name.split(".")[0]
                    for item in node.names
                    if item.name == "builtins"
                )

            def visit_Call(self, node: ast.Call) -> None:
                self.calls.append(node)
                self.generic_visit(node)

            def visit_FunctionDef(
                self, node: ast.FunctionDef | ast.AsyncFunctionDef
            ) -> None:
                self.functions.append(node)
                for expression in (
                    *node.decorator_list,
                    *node.args.defaults,
                    *(item for item in node.args.kw_defaults if item is not None),
                    *(
                        argument.annotation
                        for argument in (
                            *node.args.posonlyargs,
                            *node.args.args,
                            *node.args.kwonlyargs,
                            *(
                                (node.args.vararg,)
                                if node.args.vararg is not None
                                else ()
                            ),
                            *(
                                (node.args.kwarg,)
                                if node.args.kwarg is not None
                                else ()
                            ),
                        )
                        if argument.annotation is not None
                    ),
                    *((node.returns,) if node.returns is not None else ()),
                ):
                    self.visit(expression)

            visit_AsyncFunctionDef = visit_FunctionDef

            def visit_ClassDef(self, node: ast.ClassDef) -> None:
                self.classes.append(node)
                for expression in (*node.decorator_list, *node.bases):
                    self.visit(expression)
                for keyword in node.keywords:
                    self.visit(keyword.value)

        def scope_bindings(statements: list[ast.stmt]) -> _MutationBindings:
            bindings = _MutationBindings()
            for statement in statements:
                bindings.visit(statement)
            return bindings

        def direct_mutator_expression(
            node: ast.expr, aliases: set[str], modules: set[str]
        ) -> bool:
            return (
                isinstance(node, ast.Name)
                and node.id in aliases
                or isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id in modules
                and node.attr in mutator_names
            )

        def scan_scope(
            node: ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef,
            inherited_aliases: set[str],
            inherited_modules: set[str],
            *,
            method_aliases: set[str] | None = None,
            method_modules: set[str] | None = None,
        ) -> None:
            statements = node.body
            bindings = scope_bindings(statements)
            writes = _Writes()
            for statement in statements:
                writes.visit(statement)
            bound = set(writes.names)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                bound.update(_local_names(node))
            aliases = inherited_aliases - bound
            modules = inherited_modules - bound
            aliases.update(bindings.imported_mutators)
            modules.update(bindings.imported_modules)
            for target in bindings.attribute_writes:
                mark_mutated_class(target)
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                positional = (*node.args.posonlyargs, *node.args.args)
                defaults = positional[len(positional) - len(node.args.defaults) :]
                aliases.update(
                    argument.arg
                    for argument, default in zip(
                        defaults, node.args.defaults, strict=True
                    )
                    if direct_mutator_expression(
                        default, inherited_aliases, inherited_modules
                    )
                )
                aliases.update(
                    argument.arg
                    for argument, default in zip(
                        node.args.kwonlyargs, node.args.kw_defaults, strict=True
                    )
                    if default is not None
                    and direct_mutator_expression(
                        default, inherited_aliases, inherited_modules
                    )
                )
            changed = True
            while changed:
                before = len(aliases)
                aliases.update(
                    name
                    for name, expression in bindings.assignments
                    if direct_mutator_expression(expression, aliases, modules)
                )
                changed = len(aliases) != before
            if isinstance(node, ast.Module):
                aliases.update(mutator_names - bound)
                if "builtins" not in bound:
                    modules.add("builtins")
            for call in bindings.calls:
                called_function = call.func
                is_mutator = (
                    isinstance(called_function, ast.Name)
                    and called_function.id in aliases
                ) or (
                    isinstance(called_function, ast.Attribute)
                    and isinstance(called_function.value, ast.Name)
                    and called_function.value.id in modules
                    and called_function.attr in mutator_names
                )
                if is_mutator and call.args:
                    mark_mutated_class(call.args[0])
            if isinstance(node, ast.ClassDef):
                child_function_aliases = method_aliases or inherited_aliases
                child_function_modules = method_modules or inherited_modules
            else:
                child_function_aliases = aliases
                child_function_modules = modules
            for nested_function in bindings.functions:
                scan_scope(
                    nested_function,
                    child_function_aliases,
                    child_function_modules,
                )
            for class_node in bindings.classes:
                scan_scope(
                    class_node,
                    aliases,
                    modules,
                    method_aliases=aliases,
                    method_modules=modules,
                )

        scan_scope(owner.tree, set(), set())
        self.in_function = False
        self.class_depth = 0
        self.captured: set[str] = set()
        self.current_function_locals: set[str] = set()

    def target_value(self, target: _Target) -> _Value:
        if isinstance(target, tuple) and isinstance(target[1], ast.Call):
            owner, node = target
            assert isinstance(node, ast.Call)
            document = self.documents.get(
                DocumentIdentity(owner.identity, owner.span(node))
            )
            return _Document(document) if document else None
        return _Callable(target) if target is not None else None

    def select(self, value: _Value, key: str | int) -> _Value:
        if isinstance(value, tuple):
            if isinstance(key, int) and -len(value) <= key < len(value):
                return value[key]
            _consume(value)
            return None
        if not isinstance(value, _Reference):
            return None
        paths: list[_Path] = []
        for path in value.paths:
            if path.wrappers:
                if key == path.wrappers[0]:
                    paths.append(
                        _Path(path.state, path.node, path.depth, path.wrappers[1:])
                    )
                else:
                    _consume(_Reference((path,)))
                continue
            if isinstance(key, int) and path.depth > 0:
                paths.append(_Path(path.state, path.node, path.depth - 1))
                continue
            child = (
                path.node.children.get(key)
                if isinstance(key, str) and not path.depth
                else None
            )
            if child is None:
                _consume(_Reference((path,)))
                continue
            paths.append(_Path(path.state, child, child.depth))
            if child.leaf:
                path.state.used.add(child.path)
                path.state.accesses += 1
        return _Reference(tuple(paths)) if paths else None

    def shape(self, state: _State, shape: _Return | None) -> _Value:
        if shape is None:
            return None
        if shape.awaited:
            return _Awaited(self.shape(state, replace(shape, awaited=False)))
        if shape.items is not None:
            return tuple(self.shape(state, item) for item in shape.items)
        return _Reference((_Path(state, state.root, 0, shape.wrappers),))

    def _super_method(
        self, name: str
    ) -> tuple[ast.ClassDef, ast.FunctionDef | ast.AsyncFunctionDef] | None:
        if (
            self.current_class is None
            or self.current_function is None
            or id(self.current_class)
            not in {id(item) for item in self.class_definitions.values()}
            or self.current_function.decorator_list
            or isinstance(self.current_function, ast.AsyncFunctionDef)
            or not (
                self.current_function.args.posonlyargs
                or self.current_function.args.args
            )
        ):
            return None
        current = self.current_class
        visited: set[int] = {id(current)}
        while True:
            if (
                current.decorator_list
                or current.keywords
                or len(current.bases) != 1
                or not isinstance(current.bases[0], ast.Name)
            ):
                return None
            base = self.class_definitions.get(current.bases[0].id)
            if (
                base is None
                or id(base) in visited
                or id(current) in self.mutated_classes
                or id(base) in self.mutated_classes
            ):
                return None
            visited.add(id(base))
            if any(
                not isinstance(
                    item,
                    (
                        ast.FunctionDef,
                        ast.AsyncFunctionDef,
                        ast.Assign,
                        ast.AnnAssign,
                        ast.Import,
                        ast.ImportFrom,
                        ast.Pass,
                    ),
                )
                for item in base.body
            ):
                return None
            members = [
                item
                for item in base.body
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
                and item.name == name
            ]
            conflicting_bindings = False
            for item in base.body:
                if members and item is members[0]:
                    continue
                writes = _Writes()
                writes.visit(item)
                if name in writes.names:
                    conflicting_bindings = True
                    break
            if members:
                if (
                    len(members) != 1
                    or conflicting_bindings
                    or members[0].decorator_list
                    or isinstance(members[0], ast.AsyncFunctionDef)
                    or members[0].args.vararg is not None
                    or members[0].args.kwarg is not None
                ):
                    return None
                return base, members[0]
            current = base

    def _call_super_method(
        self, node: ast.Call, env: dict[str, _Value]
    ) -> tuple[bool, _Value]:
        function = node.func
        if not (
            isinstance(function, ast.Attribute)
            and isinstance(function.value, ast.Call)
            and isinstance(function.value.func, ast.Name)
            and function.value.func.id == "super"
            and "super" not in env
            and not function.value.args
            and not function.value.keywords
            and not any(isinstance(item, ast.Starred) for item in node.args)
            and all(item.arg is not None for item in node.keywords)
        ):
            return False, None
        target = self._super_method(function.attr)
        if target is None:
            return False, None
        class_node, method = target
        parameters = (*method.args.posonlyargs, *method.args.args)
        if not parameters or parameters[0].arg in {item.arg for item in node.keywords}:
            return False, None
        values = [self.expression(item, env) for item in node.args]
        keywords = {
            item.arg: self.expression(item.value, env) for item in node.keywords
        }
        bound = _bind_call(method, [None, *values], keywords)
        if bound is None:
            for value in (*values, *keywords.values()):
                _consume(value)
            return True, None
        declarations = _OuterWrites()
        for statement in method.body:
            declarations.visit(statement)
        if declarations.names:
            for value in bound.values():
                _consume(value)
            return True, None
        local = dict(self.module_env)
        for name in _local_names(method):
            local[name] = None
        local.update(bound)
        local[parameters[0].arg] = None
        local["__class__"] = None
        previous = (
            self.current_function,
            self.current_class,
            self.current_function_locals,
            self.captured,
            self.in_function,
        )
        self.current_function = method
        self.current_class = class_node
        self.current_function_locals = _local_names(method)
        self.captured = _captures(method.body)
        self.in_function = True
        try:
            self.block(method.body, local)
        finally:
            (
                self.current_function,
                self.current_class,
                self.current_function_locals,
                self.captured,
                self.in_function,
            ) = previous
        return True, None

    def element(self, value: _Value) -> _Value:
        if isinstance(value, _Reference) and all(
            path.depth > 0 and not path.wrappers for path in value.paths
        ):
            return self.select(value, 0)
        _consume(value)
        return None

    def condition(self, node: ast.expr, env: dict[str, _Value]) -> None:
        if isinstance(node, ast.BoolOp):
            for value in node.values:
                self.condition(value, env)
        else:
            self.expression(node, env)

    def expression(self, node: ast.expr, env: dict[str, _Value]) -> _Value:
        if isinstance(node, ast.Name):
            if node.id not in env and node.id in _REFLECTION:
                return _Callable(f"builtins.{node.id}")
            return env.get(node.id)
        if isinstance(node, ast.Await):
            value = self.expression(node.value, env)
            if isinstance(value, _Awaited):
                return value.value
            _consume(value)
            return None
        if isinstance(node, ast.Attribute):
            base = self.expression(node.value, env)
            if isinstance(base, _Callable) and isinstance(base.target, str):
                return self.target_value(
                    self.symbols.imported(
                        self.owner.snapshot.project, f"{base.target}.{node.attr}"
                    )
                )
            return self.select(base, node.attr)
        if isinstance(node, ast.Subscript):
            base = self.expression(node.value, env)
            if (
                isinstance(node.slice, ast.Constant)
                and isinstance(node.slice.value, (str, int))
                and not isinstance(node.slice.value, bool)
            ):
                return self.select(base, node.slice.value)
            if (
                isinstance(node.slice, ast.UnaryOp)
                and isinstance(node.slice.op, ast.USub)
                and isinstance(node.slice.operand, ast.Constant)
                and isinstance(node.slice.operand.value, int)
                and not isinstance(node.slice.operand.value, bool)
            ):
                return self.select(base, -node.slice.operand.value)
            _consume(base)
            _consume(self.expression(node.slice, env))
            return None
        if isinstance(node, ast.Tuple):
            return tuple(self.expression(item, env) for item in node.elts)
        if isinstance(node, ast.List):
            # Mutable containers can change through aliases outside tracked expressions.
            for list_element in node.elts:
                _consume(self.expression(list_element, env))
            return None
        if isinstance(node, ast.Call):
            handled, value = self._call_super_method(node, env)
            if handled:
                return value
            document = self.documents.get(
                DocumentIdentity(self.owner.identity, self.owner.span(node))
            )
            if document is not None:
                return _Document(document)
            function = self.expression(node.func, env)
            values = [self.expression(item, env) for item in node.args]
            keywords = {
                item.arg: self.expression(item.value, env) for item in node.keywords
            }
            target = function.target if isinstance(function, _Callable) else None
            if (
                isinstance(target, str)
                and target.startswith("builtins.")
                and target[9:] in _REFLECTION
            ):
                for reflected_state in self.states.values():
                    reflected_state.escaped.add(())
                return None

            def argument(position: int, name: str) -> _Value:
                return (
                    values[position] if len(values) > position else keywords.get(name)
                )

            if target in ("graphql_desk.testing.read_result",):
                source = argument(0, "document")
                if isinstance(source, _Document):
                    state = self.states.get(
                        DocumentIdentity(
                            source.document.call.module, source.document.call.range
                        )
                    )
                    if (
                        state is not None
                        and state.document.selected_operation is not None
                    ):
                        return self.shape(state, _Return())
            if target in (
                "graphql_desk.read_fragment",
                "graphql_desk.fragment_helpers.read_fragment",
                "graphql_desk.read_fragment_passthrough",
                "graphql_desk.fragment_helpers.read_fragment_passthrough",
            ):
                source, data = argument(0, "document"), argument(1, "data")
                if isinstance(source, _Document):
                    identity = DocumentIdentity(
                        source.document.call.module, source.document.call.range
                    )
                    state = self.states.get(identity)
                    if (
                        state is not None
                        and state.document.primary_fragment is not None
                    ):
                        passthrough = target in (
                            "graphql_desk.read_fragment_passthrough",
                            "graphql_desk.fragment_helpers.read_fragment_passthrough",
                        )
                        if not isinstance(data, _Reference):
                            return data if passthrough else self.shape(state, _Return())
                        if all(
                            any(
                                f.document == identity
                                for f in p.state.document.definitions
                            )
                            and p.state.document.schema_identity
                            == state.document.schema_identity
                            for p in data.paths
                        ):
                            return _Reference(
                                (
                                    *data.paths,
                                    _Path(state, state.root, data.paths[0].depth),
                                )
                            )
                        if passthrough:
                            return data
            if target in (
                "graphql_desk.identity_fragment",
                "graphql_desk.fragment_helpers.identity_fragment",
            ):
                return argument(0, "data")
            signature = _signature(target, self.symbols)
            if (
                signature is not None
                and not any(isinstance(item, ast.Starred) for item in node.args)
                and None not in keywords
            ):
                assert isinstance(target, tuple) and isinstance(
                    target[1], (ast.FunctionDef, ast.AsyncFunctionDef)
                )
                name, shape = signature
                bound = _bind_call(target[1], values, keywords)
                source = bound.get(name) if bound is not None else None
                if isinstance(source, _Document):
                    state = self.states.get(
                        DocumentIdentity(
                            source.document.call.module, source.document.call.range
                        )
                    )
                    for item in (*values, *keywords.values()):
                        _consume(item)
                    if (
                        state is not None
                        and state.document.selected_operation is not None
                    ):
                        return self.shape(state, shape)
            _consume(function)
            for item in (*values, *keywords.values()):
                _consume(item)
                if isinstance(item, _Document):
                    state = self.states.get(
                        DocumentIdentity(
                            item.document.call.module, item.document.call.range
                        )
                    )
                    if state is not None:
                        state.escaped.add(())
            return None
        if isinstance(
            node, (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)
        ):
            rebound = {
                child.target.id
                for child in ast.walk(node)
                if isinstance(child, ast.NamedExpr)
            }
            # Assignment expressions can rebind the enclosing scope even in generators.
            for name in rebound:
                _consume(env.get(name))
            local = dict(env)
            for generator in node.generators:
                value = self.expression(generator.iter, local)
                self.bind(generator.target, self.element(value), local)
                for condition in generator.ifs:
                    self.condition(condition, local)
            if isinstance(node, ast.DictComp):
                _consume(self.expression(node.key, local))
                _consume(self.expression(node.value, local))
            else:
                _consume(self.expression(node.elt, local))
            for name in rebound:
                _consume(local.get(name))
                env[name] = None
            return None
        if (
            isinstance(node, ast.Compare)
            and all(isinstance(op, (ast.Is, ast.IsNot)) for op in node.ops)
            and all(
                isinstance(item, ast.Constant) and item.value is None
                for item in node.comparators
            )
        ):
            self.expression(node.left, env)
            return None
        if isinstance(node, ast.BoolOp):
            for operand in node.values:
                _consume(self.expression(operand, env))
            return None
        if isinstance(node, ast.Lambda):
            parameters = {
                argument.arg
                for argument in (
                    *node.args.posonlyargs,
                    *node.args.args,
                    *node.args.kwonlyargs,
                )
            }
            for child in ast.walk(node.body):
                if isinstance(child, ast.Name) and child.id not in parameters:
                    _consume(env.get(child.id))
            return None
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            self.expression(node.operand, env)
            return None
        if isinstance(node, ast.NamedExpr):
            value = self.expression(node.value, env)
            self.bind(node.target, value, env)
            return value
        if isinstance(node, ast.IfExp):
            self.condition(node.test, env)
            first, second = (
                self.expression(node.body, env),
                self.expression(node.orelse, env),
            )
            if first == second:
                return first
            _consume(first)
            _consume(second)
            return None
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.expr):
                _consume(self.expression(child, env))
        return None

    def bind(self, node: ast.expr, value: _Value, env: dict[str, _Value]) -> None:
        if isinstance(node, ast.Name):
            if self.class_depth:
                _consume(value)
            env[node.id] = value
            if node.id in self.captured:
                _consume(value)
        elif isinstance(node, (ast.Tuple, ast.List)) and not any(
            isinstance(item, ast.Starred) for item in node.elts
        ):
            if isinstance(value, tuple) and len(node.elts) == len(value):
                for target, item in zip(node.elts, value, strict=True):
                    self.bind(target, item, env)
            elif isinstance(value, _Reference) and all(
                path.depth > 0 for path in value.paths
            ):
                for target in node.elts:
                    self.bind(target, self.select(value, 0), env)
            else:
                _consume(value)
                for target in node.elts:
                    self.bind(target, None, env)
        else:
            _consume(value)
            if isinstance(node, (ast.Attribute, ast.Subscript)):
                _consume(self.expression(node.value, env))
                if isinstance(node, ast.Subscript):
                    _consume(self.expression(node.slice, env))
                root = node.value
                while isinstance(root, (ast.Attribute, ast.Subscript)):
                    root = root.value
                if isinstance(root, ast.Name) and isinstance(
                    env.get(root.id), _Callable
                ):
                    env[root.id] = None

    def merge(
        self,
        env: dict[str, _Value],
        first: dict[str, _Value],
        second: dict[str, _Value],
    ) -> None:
        for name in first.keys() | second.keys():
            left, right = first.get(name), second.get(name)
            if left == right:
                env[name] = left
            else:
                _consume(left)
                _consume(right)
                env[name] = None

    def block(self, statements: list[ast.stmt], env: dict[str, _Value]) -> None:
        for position, node in enumerate(statements):
            if isinstance(node, ast.Assign):
                value = self.expression(node.value, env)
                for target in node.targets:
                    self.bind(target, value, env)
            elif isinstance(node, ast.AnnAssign):
                self.bind(
                    node.target,
                    self.expression(node.value, env) if node.value else None,
                    env,
                )
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                for name, imported_target in self.owner.imports(node).items():
                    env[name] = self.target_value(
                        self.symbols.imported(
                            self.owner.snapshot.project, imported_target
                        )
                    )
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                env[node.name] = (
                    _Callable((self.owner, node)) if not node.decorator_list else None
                )
                for default in (
                    *node.args.defaults,
                    *node.args.kw_defaults,
                    *node.decorator_list,
                ):
                    if default is not None:
                        _consume(self.expression(default, env))
                annotation_writes = _Writes()
                for argument in (
                    *node.args.posonlyargs,
                    *node.args.args,
                    *node.args.kwonlyargs,
                ):
                    if argument.annotation is not None:
                        annotation_writes.visit(argument.annotation)
                if node.args.vararg and node.args.vararg.annotation is not None:
                    annotation_writes.visit(node.args.vararg.annotation)
                if node.args.kwarg and node.args.kwarg.annotation is not None:
                    annotation_writes.visit(node.args.kwarg.annotation)
                if node.returns is not None:
                    annotation_writes.visit(node.returns)
                for name in annotation_writes.names:
                    _consume(env.get(name))
                    env[name] = None
                if not self.in_function or self.class_depth:
                    self.functions.append(node)
                    if self.class_depth:
                        self.class_functions.add(id(node))
                        if self.class_stack:
                            self.function_classes[id(node)] = self.class_stack[-1]
                        captures = _captures([node]) & self.current_function_locals
                        if captures:
                            if self.class_capture_scope is None or not captures <= set(
                                self.class_capture_scope
                            ):
                                self.unsafe_local_class_functions.add(id(node))
                            else:
                                self.function_captures[id(node)] = {
                                    name: self.class_capture_scope[name]
                                    for name in captures
                                }
            elif isinstance(node, ast.ClassDef):
                for expression in (*node.decorator_list, *node.bases):
                    _consume(self.expression(expression, env))
                for keyword in node.keywords:
                    _consume(self.expression(keyword.value, env))
                class_env = dict(env)
                annotations = _ClassAnnotations(self, class_env)
                for statement in node.body:
                    annotations.visit(statement)
                previous_capture_scope = self.class_capture_scope
                self.class_capture_scope = None
                if (
                    self.in_function
                    and self.class_depth == 0
                    and self.current_function is not None
                    and node in self.current_function.body
                ):
                    capture_names = _captures(node.body) & self.current_function_locals
                    later_writes = _Writes(include_annotations=True)
                    for statement in statements[position + 1 :]:
                        later_writes.visit(statement)
                    invalid_captures = capture_names & set(later_writes.names)
                    nonlocal_captures = _NonlocalCaptures(self.current_function)
                    for statement in self.current_function.body:
                        nonlocal_captures.visit(statement)
                    invalid_captures.update(capture_names & nonlocal_captures.names)
                    for child in ast.walk(node):
                        if isinstance(child, ast.Call) and (
                            isinstance(child.func, ast.Name)
                            and child.func.id in {"exec", "eval", "locals", "vars"}
                            or isinstance(child.func, ast.Attribute)
                            and child.func.attr in {"exec", "eval", "locals", "vars"}
                        ):
                            invalid_captures.update(capture_names)
                    safe_captures = {
                        name: env[name]
                        for name in capture_names - invalid_captures
                        if isinstance(env.get(name), (_Document, _Callable))
                    }
                    self.class_capture_scope = safe_captures
                self.class_depth += 1
                self.class_stack.append(node)
                try:
                    self.block(node.body, class_env)
                finally:
                    self.class_stack.pop()
                    self.class_depth -= 1
                    self.class_capture_scope = previous_capture_scope
                writes = _Writes()
                outer_writes = _OuterWrites()
                for statement in node.body:
                    writes.visit(statement)
                    outer_writes.visit(statement)
                for name in writes.names:
                    _consume(class_env.get(name))
                for name in outer_writes.names & writes.names.keys():
                    _consume(env.get(name))
                    env[name] = None
                env[node.name] = None
            elif isinstance(node, ast.Expr):
                _consume(self.expression(node.value, env))
            elif isinstance(node, ast.Return):
                if node.value:
                    _consume(self.expression(node.value, env))
            elif isinstance(node, ast.If):
                self.condition(node.test, env)
                left, right = dict(env), dict(env)
                self.block(node.body, left)
                self.block(node.orelse, right)
                self.merge(env, left, right)
            elif isinstance(node, (ast.For, ast.AsyncFor)):
                value = self.expression(node.iter, env)
                local = dict(env)
                self.bind(node.target, self.element(value), local)
                self.block(node.body, local)
                self.block(node.orelse, local)
                later = {
                    child.id
                    for statement in statements[position + 1 :]
                    for child in ast.walk(statement)
                    if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load)
                }
                for name in local.keys() | env.keys():
                    if name not in later and local.get(name) != env.get(name):
                        local[name] = env.get(name)
                self.merge(env, env.copy(), local)
            elif isinstance(node, ast.Assert):
                self.condition(node.test, env)
                if node.msg:
                    _consume(self.expression(node.msg, env))
            elif isinstance(node, ast.Pass):
                continue
            else:
                # Unknown control flow or mutation may use any part of a result.
                for child in ast.walk(node):
                    if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load):
                        _consume(env.get(child.id))
                writes = _Writes()
                writes.visit(node)
                for name in writes.names:
                    env[name] = None

    def run(self) -> None:
        env: dict[str, _Value] = {}
        self.captured = _captures(self.owner.tree.body)
        self.block(self.owner.tree.body, env)
        self.module_env = dict(env)
        functions, self.functions = self.functions, []
        self.in_function = True
        position = 0
        while position < len(functions):
            function = functions[position]
            position += 1
            if id(function) in self.unsafe_local_class_functions:
                continue
            local = dict(env)
            names = _local_names(function)
            self.current_function_locals = names
            self.captured = _captures(function.body)
            for name in names:
                local[name] = None
            local.update(self.function_captures.get(id(function), {}))
            if id(function) in self.class_functions:
                local["__class__"] = None
            declarations = _OuterWrites()
            for statement in function.body:
                declarations.visit(statement)
            if declarations.names:
                for value in local.values():
                    _consume(value)
                continue
            self.current_function = function
            self.current_class = self.function_classes.get(id(function))
            self.block(function.body, local)
            self.current_function = None
            self.current_class = None
            functions.extend(self.functions)
            self.functions.clear()


def field_usage_diagnostics(
    modules: tuple[ModuleSnapshot, ...],
    index: FragmentIndex,
    schemas: Mapping[tuple[str, str | None], SchemaContext],
    options: Mapping[str, FieldUsageOptions] | None = None,
) -> tuple[GraphDiagnostic, ...]:
    """Report only proven same-file reads; unfamiliar Python remains silent."""
    parsed: dict[tuple[str, str], _Module] = {}
    for snapshot in modules:
        try:
            tree = ast.parse(snapshot.source)
            compile(tree, snapshot.uri, "exec")
            parsed[(snapshot.project, snapshot.module)] = _Module(snapshot, tree)
        except (
            SyntaxError,
            ValueError,
            UnicodeError,
            RecursionError,
            tokenize.TokenError,
        ):
            continue
    symbols = _Symbols(parsed)
    documents = {
        DocumentIdentity(item.call.module, item.call.range): item
        for item in index.documents
    }
    syntax = {
        identity: (document.call, parse(document.call.source))
        for identity, document in documents.items()
    }
    local_documents: dict[ModuleDependency, list[IndexedDocument]] = {}
    for document in documents.values():
        local_documents.setdefault(document.call.module, []).append(document)
    diagnostics: list[GraphDiagnostic] = []
    for owner in parsed.values():
        policy = (options or {}).get(owner.snapshot.project, FieldUsageOptions())
        if not policy.enabled or owner.star:
            continue
        states: dict[DocumentIdentity, _State] = {}
        try:
            for document in local_documents.get(owner.identity, ()):
                identity = DocumentIdentity(document.call.module, document.call.range)
                context = schemas.get(document.schema_identity)
                if context is not None:
                    candidate = _state(document, syntax, context, policy)
                    if candidate is not None:
                        states[identity] = candidate
            if not states:
                continue
            _Flow(owner, symbols, documents, states).run()
        except (RecursionError, ValueError):
            continue
        reserved = _RESERVED | set(policy.reserved_keys)
        for state in states.values():
            if not state.accesses or () in state.escaped:
                continue
            groups: dict[tuple[str, ...], list[_Node]] = {}
            nodes_by_path: dict[tuple[str, ...], _Node] = {}

            def collect(node: _Node) -> None:
                nodes_by_path[node.path] = node
                if any(node.path[: len(prefix)] == prefix for prefix in state.escaped):
                    return
                if node.children:
                    for child in node.children.values():
                        collect(child)
                elif (
                    node.leaf
                    and node.path not in state.used
                    and any(
                        field.name.value not in reserved for _, field in node.fields
                    )
                ):
                    groups.setdefault(node.path[:-1], []).append(node)

            collect(state.root)
            for parent_path, nodes in groups.items():
                names = ", ".join(".".join(node.path) for node in nodes)
                message = (
                    f"Selected fields have no observed same-file read: {names}. "
                    "This is a conservative usage heuristic."
                )
                parent = nodes_by_path.get(parent_path)
                locations = [parent] if parent is not None and parent.fields else nodes
                for node in locations:
                    for call, selection in node.fields:
                        call_line = bisect_right(owner.starts, call.range.start)
                        if call.module == owner.identity and (
                            call_line in owner.ignored_lines
                            or call_line - 1 in owner.ignored_lines
                        ):
                            continue
                        if (
                            selection.name.value in reserved
                            or selection.name.loc is None
                        ):
                            continue
                        source_map = build_source_map(call, modules)
                        for origin in source_map.to_host(
                            Span(selection.name.loc.start, selection.name.loc.end)
                        ):
                            if origin.module != owner.identity:
                                continue
                            line = bisect_right(owner.starts, origin.range.start)
                            if (
                                line in owner.ignored_lines
                                or line - 1 in owner.ignored_lines
                            ):
                                continue
                            diagnostic = GraphDiagnostic(
                                origin.module, UNUSED_FIELD_CODE, message, origin.range
                            )
                            if diagnostic not in diagnostics:
                                diagnostics.append(diagnostic)
    return tuple(diagnostics)
