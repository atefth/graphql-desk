"""Pure retained-source factory artifacts; publication belongs to the caller."""

import ast
import keyword
import re
import unicodedata
from dataclasses import dataclass
from typing import Any

from . import TypedDocument
from .config import ScalarMapping
from .fragment_refs import fragment_identity_key
from .fragments import FragmentIndex, IndexedDocument, SchemaContext
from .literal_ast import render_document_ast_types
from .operation_types import (
    render_indexed_fragment_types,
    render_indexed_operation_types,
)
from .runtime import init_graphql
from .schema_types import render_schema_types
from .source_graph import DocumentIdentity, GraphDiagnostic, ModuleSnapshot
from .source_maps import build_source_map


@dataclass(frozen=True)
class GeneratedFile:
    relative_path: str
    text: str


@dataclass(frozen=True)
class FactoryArtifacts:
    files: tuple[GeneratedFile, ...]
    diagnostics: tuple[GraphDiagnostic, ...]


def _identifier(value: str) -> bool:
    return (
        value.isidentifier()
        and not keyword.iskeyword(value)
        and unicodedata.normalize("NFKC", value) == value
        and not value.startswith("__")
    )


def _pascal(value: str) -> str:
    return "".join(part[:1].upper() + part[1:] for part in value.split("_") if part)


def _precision_diagnostics(
    document: IndexedDocument, snapshot: ModuleSnapshot
) -> list[GraphDiagnostic]:
    """Check host syntax at an already resolved call, without resolving names again."""
    tree = ast.parse(snapshot.source)
    lines = re.split(r"(?<=\n)|(?<=\r)(?!\n)", snapshot.source)

    def offset(line: int, column: int) -> int:
        return sum(len(part) for part in lines[: line - 1]) + len(
            lines[line - 1].encode("utf8")[:column].decode("utf8")
        )

    candidates = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and node.end_lineno is not None
        and node.end_col_offset is not None
        and offset(node.lineno, node.col_offset) == document.call.range.start
        and offset(node.end_lineno, node.end_col_offset) == document.call.range.end
    ]
    if len(candidates) != 1:
        raise ValueError("Indexed call is absent from its supplied source snapshot")
    call = candidates[0]
    source = (
        call.args[0]
        if call.args
        else next((item.value for item in call.keywords if item.arg == "source"), None)
    )
    fragments = (
        call.args[1]
        if len(call.args) > 1
        else next(
            (item.value for item in call.keywords if item.arg == "fragments"), None
        )
    )
    operation = next(
        (item.value for item in call.keywords if item.arg == "operation"), None
    )
    literal_source = isinstance(source, ast.Constant) and isinstance(source.value, str)
    if isinstance(source, ast.Name):
        typing_names: dict[str, str] = {}
        for statement in tree.body:
            if statement.lineno >= call.lineno:
                break
            if isinstance(statement, ast.ImportFrom) and statement.module in {
                "typing",
                "typing_extensions",
            }:
                typing_names.update(
                    {
                        alias.asname or alias.name: alias.name
                        for alias in statement.names
                    }
                )
            if (
                not isinstance(statement, ast.AnnAssign)
                or not isinstance(statement.target, ast.Name)
                or statement.target.id != source.id
            ):
                continue
            annotation = statement.annotation
            literal_source = (
                isinstance(annotation, ast.Name)
                and typing_names.get(annotation.id) == "Final"
                and isinstance(statement.value, ast.Constant)
                and statement.value.value == document.call.source
            ) or (
                isinstance(annotation, ast.Subscript)
                and isinstance(annotation.value, ast.Name)
                and typing_names.get(annotation.value.id) == "Literal"
                and isinstance(annotation.slice, ast.Constant)
                and annotation.slice.value == document.call.source
            )
    messages: list[str] = []
    if not literal_source:
        messages.append(
            "Exact overload inference requires an inline string literal; "
            "resolved aliases and concatenations may widen to str in a checker"
        )
    if fragments is not None and not isinstance(fragments, ast.Tuple):
        messages.append(
            "Exact fragment overload inference requires a fixed tuple; "
            "lists and widened fragment collections use the untyped fallback"
        )
    if operation is not None and not (
        isinstance(operation, ast.Constant)
        and isinstance(operation.value, (str, type(None)))
    ):
        messages.append("Exact operation selection requires a literal keyword value")
    return [
        GraphDiagnostic(
            document.call.module, "factory-precision", message, document.call.range
        )
        for message in messages
    ]


def generate_factory_artifacts(
    context: SchemaContext,
    index: FragmentIndex,
    *,
    modules: tuple[ModuleSnapshot, ...],
    project_id: str,
    generated_package: str,
    scalars: tuple[ScalarMapping, ...] = (),
    enums: tuple[tuple[str, str], ...] = (),
    client_directives: tuple[str, ...] = (),
    disable_masking: bool = False,
) -> FactoryArtifacts:
    """Generate one schema's retained runtime/stub files without writing them.

    The caller supplies current, trusted resolver/index snapshots. The general str
    fallback is explicitly Any: checkers also use it for incorrect known calls.
    Index errors prevent publication; precision diagnostics accompany usable
    artifacts and identify syntax that cannot reliably select precise overloads.
    """
    if not project_id:
        raise ValueError("Project identity must not be empty")
    if not all(_identifier(part) for part in generated_package.split(".")):
        raise ValueError("Generated package must be a normalized dotted Python name")
    if context.schema_name in {"sources", "persisted", "_documents"}:
        raise ValueError("Schema binding collides with a generated package namespace")
    if not _identifier(context.schema_name):
        raise ValueError("Schema binding must be a normalized Python name")
    if index.diagnostics:
        return FactoryArtifacts((), index.diagnostics)
    snapshots = {(item.project, item.module): item for item in modules}
    if len(snapshots) != len(modules):
        raise ValueError("Source module identities must be unique")
    documents = sorted(
        index.documents, key=lambda d: (d.call.module.module, d.call.range.start)
    )
    owners = {DocumentIdentity(d.call.module, d.call.range): d for d in documents}
    if len(owners) != len(documents):
        raise ValueError("Indexed document identities must be unique")
    files: dict[str, str] = {}
    diagnostics: list[GraphDiagnostic] = []
    base = generated_package.replace(".", "/")
    declarations: dict[DocumentIdentity, tuple[str, str, str]] = {}
    carriers: dict[DocumentIdentity, TypedDocument[Any, Any]] = {}
    active: set[DocumentIdentity] = set()
    signatures: dict[tuple[str, str | None, tuple[str, ...]], str] = {}
    overloads: list[str] = []
    error_call = documents[0].call if documents else None
    imports: list[str] = []
    aliases: dict[str, list[str]] = {}
    public_names: dict[str, set[str]] = {}

    def put(path: str, text: str) -> None:
        if path in files and files[path] != text:
            raise ValueError(f"Generated file collision: {path}")
        files[path] = text

    def packages(path: str) -> None:
        parts = path.split("/")
        for length in range(1, len(parts)):
            prefix = "/".join(parts[:length])
            files.setdefault(
                f"{prefix}/__init__.py", '"""Generated GraphQL Desk package."""\n'
            )
            files.setdefault(f"{prefix}/__init__.pyi", "")

    def carrier(document: IndexedDocument) -> TypedDocument[Any, Any]:
        identity = DocumentIdentity(document.call.module, document.call.range)
        if identity in carriers:
            return carriers[identity]
        if identity in active:
            raise ValueError("Cyclic indexed fragment ownership")
        active.add(identity)
        supplied: list[TypedDocument[Any, Any]] = []
        for reference in document.call.fragments:
            owner = owners.get(reference.target)
            if owner is None or owner.primary_fragment is None:
                raise ValueError("Supplied fragment has no indexed primary owner")
            supplied.append(carrier(owner))
        value = init_graphql(context.schema_name)(
            document.call.source, supplied, operation=document.call.operation
        )
        active.remove(identity)
        carriers[identity] = value
        return value

    try:
        schema_text = render_schema_types(context.schema, scalars=scalars, enums=enums)
        if "graphql" in context.schema.type_map:
            raise ValueError("Schema aliases collide with generated factory exports")
        for number, document in enumerate(documents):
            call = document.call
            error_call = call
            snapshot = snapshots.get((call.module.project, call.module.module))
            if (
                call.module.project != project_id
                or document.schema_identity != (project_id, context.schema_name)
                or document.context_token != context.token
                or snapshot is None
                or (snapshot.uri, snapshot.version)
                != (call.module.uri, call.module.version)
            ):
                raise ValueError("Indexed document/source/schema identities differ")
            if not all(_identifier(part) for part in call.module.module.split(".")):
                raise ValueError(
                    "Source module must be a normalized dotted Python name"
                )
            for dependency in document.dependencies:
                dependency_source = snapshots.get(
                    (dependency.project, dependency.module)
                )
                if dependency_source is None or (
                    dependency_source.uri,
                    dependency_source.version,
                ) != (dependency.uri, dependency.version):
                    raise ValueError("Indexed dependency source snapshot differs")
            build_source_map(call, modules)
            diagnostics.extend(_precision_diagnostics(document, snapshot))
            identity = DocumentIdentity(call.module, call.range)
            label = _pascal(call.binding) if call.binding else f"Inline{number}"
            if not _identifier(label):
                raise ValueError("Document binding has no safe generated alias")
            path = f"{base}/_documents/d{number}"
            module = f"{generated_package}._documents.d{number}"
            name = "GraphqlDesk"
            while any(
                name + suffix in context.schema.type_map
                for suffix in (
                    "Result",
                    "Variables",
                    "Document",
                    "Identity",
                    "FragmentRef",
                )
            ):
                name += "Generated"
            primary = document.primary_fragment
            render = (
                render_indexed_fragment_types
                if primary is not None
                else render_indexed_operation_types
            )
            text = render(
                context,
                document,
                name=name,
                scalars=scalars,
                enums=enums,
                client_directives=client_directives,
                disable_masking=disable_masking,
                fragment_index=index,
            )
            if primary is not None:
                key = fragment_identity_key(document, primary)
                text += (
                    "\nfrom graphql_desk.fragment_refs import "
                    "FragmentIdentity as __graphql_desk_Identity, "
                    "FragmentRef as __graphql_desk_Ref\n"
                    f"{name}Identity = __graphql_desk_Identity[Literal[{key!r}]]\n"
                    f"{name}FragmentRef = __graphql_desk_Ref[{name}Identity]\n"
                )
            runtime = carrier(document)
            ast_text = render_document_ast_types(runtime.ast, name="Exact")
            packages(path + ".py")
            put(path + ".py", text)
            put(
                path + "_ast.py",
                '"""Literal AST views exist only in the matching stub."""\n',
            )
            put(path + "_ast.pyi", ast_text)
            base_type = (
                f"__graphql_desk_FragmentDocument[{name}Identity, "
                f"{name}Result, {name}Variables]"
                if primary is not None
                else f"__graphql_desk_TypedDocument[{name}Result, {name}Variables]"
            )
            superclass_import = (
                "from graphql_desk.fragment_refs import "
                "FragmentDocument as __graphql_desk_FragmentDocument\n"
                if primary is not None
                else "from graphql_desk import "
                "TypedDocument as __graphql_desk_TypedDocument\n"
            )
            stub = (
                text
                + "\n"
                + superclass_import
                + (
                    f"from .d{number}_ast import ExactDocument as __graphql_desk_Ast\n"
                    f"\nclass {name}Document({base_type}):\n"
                    "    ast: __graphql_desk_Ast\n"
                    f"    operation_name: Literal[{runtime.operation_name!r}]\n"
                    f"    schema_name: Literal[{context.schema_name!r}]\n"
                )
            )
            put(path + ".pyi", stub)
            declarations[identity] = (module, name, label)
            if call.binding is not None:
                names = public_names.setdefault(call.module.module, set())
                if label in names:
                    raise ValueError("Normalized document binding aliases collide")
                names.add(label)
                source_aliases = aliases.setdefault(call.module.module, [])
                for suffix in ("Result", "Variables", "FragmentRef"):
                    if suffix == "FragmentRef" and primary is None:
                        continue
                    source_aliases.append(
                        f"from {module} import {name}{suffix} as {label}{suffix}\n"
                    )
        for number, document in enumerate(documents):
            error_call = document.call
            identity = DocumentIdentity(document.call.module, document.call.range)
            module, name, _ = declarations[identity]
            reference_types: list[str] = []
            reference_imports: list[str] = []
            reference_keys: list[str] = []
            for reference in document.call.fragments:
                owner = owners[reference.target]
                assert owner.primary_fragment is not None
                key = fragment_identity_key(owner, owner.primary_fragment)
                reference_keys.append(key)
                owner_module, owner_name, _ = declarations[reference.target]
                alias = f"__graphql_desk_Fragment{number}_{len(reference_types)}"
                reference_imports.append(
                    f"from {owner_module} import {owner_name}Document as {alias}\n"
                )
                reference_types.append(alias)
            selected = document.selected_operation
            operation_name = selected.name if selected is not None else None
            signature = (document.call.source, operation_name, tuple(reference_keys))
            previous = signatures.get(signature)
            if previous is not None:
                if document.primary_fragment is not None:
                    raise ValueError(
                        "Identical fragment call signatures have "
                        "distinct owner identities"
                    )
                continue
            signatures[signature] = module
            imports.extend(reference_imports)
            return_alias = f"__graphql_desk_Document{number}"
            imports.append(f"from {module} import {name}Document as {return_alias}\n")
            fragments = (
                "tuple[" + ", ".join(reference_types) + "]"
                if reference_types
                else "tuple[()]"
            )
            default = "" if reference_types else " = ()"
            selector = f"__graphql_desk_Literal[{operation_name!r}]"
            if document.call.operation is None:
                if operation_name is not None:
                    selector = f"__graphql_desk_Literal[{operation_name!r}, None]"
                selector += " = None"
            overloads.append(
                "@__graphql_desk_overload\n"
                "def graphql(source: "
                f"__graphql_desk_Literal[{document.call.source!r}], "
                f"fragments: {fragments}{default}, *, "
                f"operation: {selector}) -> {return_alias}: ...\n"
            )
        for source_module, source_aliases in aliases.items():
            error_call = next(
                item.call
                for item in documents
                if item.call.module.module == source_module
            )
            snapshot = snapshots[(project_id, source_module)]
            path = f"{base}/sources/{source_module.replace('.', '/')}"
            parts = source_module.split(".")
            for length in range(1, len(parts)):
                parent = snapshots.get((project_id, ".".join(parts[:length])))
                if parent is not None and not parent.is_package:
                    raise ValueError("Source companion has a non-package parent")
            if snapshot.is_package:
                path += "/__init__"
            packages(path + ".py")
            if snapshot.is_package:
                files.pop(path + ".py", None)
                files.pop(path + ".pyi", None)
            exported = [line.strip().rsplit(" as ", 1)[1] for line in source_aliases]
            text = "".join(source_aliases) + f"\n__all__ = {exported!r}\n"
            put(path + ".py", text)
            put(path + ".pyi", text)
        path = f"{base}/{context.schema_name}"
        packages(path + ".py")
        put(
            path + ".py",
            schema_text
            + (
                "\nfrom graphql_desk import "
                "init_graphql as __graphql_desk_init_graphql\n"
                f"graphql = __graphql_desk_init_graphql({context.schema_name!r})\n"
            ),
        )
        fallback = (
            "def graphql(source: str, "
            "fragments: __graphql_desk_Iterable[__graphql_desk_TypedDocument["
            "__graphql_desk_Any, __graphql_desk_Any]] = (), "
            "*, operation: str | None = None) -> __graphql_desk_TypedDocument["
            "__graphql_desk_Any, __graphql_desk_Any]: ...\n"
        )
        typing_import = "from typing import Any as __graphql_desk_Any"
        if overloads:
            typing_import += (
                ", Literal as __graphql_desk_Literal, "
                "overload as __graphql_desk_overload"
            )
        stub = (
            schema_text
            + (
                "\n# Dynamic/nonmatching calls retain the untyped baseline.\n"
                "from collections.abc import Iterable as __graphql_desk_Iterable\n"
                "from graphql_desk import "
                "TypedDocument as __graphql_desk_TypedDocument\n"
            )
            + typing_import
            + "\n"
            + "".join(imports)
            + "\n"
            + "\n".join(overloads)
        )
        stub += ("@__graphql_desk_overload\n" if overloads else "") + fallback + "\n"
        put(path + ".pyi", stub)
    except (ValueError, RecursionError, KeyError) as error:
        if documents:
            assert error_call is not None
            call = error_call
            diagnostic = GraphDiagnostic(
                call.module,
                "factory-generation",
                str(error)
                if isinstance(error, ValueError)
                else "Invalid or excessive indexed factory graph",
                call.range,
            )
            return FactoryArtifacts((), (*diagnostics, diagnostic))
        raise ValueError("Cannot generate schema factory artifacts") from None
    return FactoryArtifacts(
        tuple(GeneratedFile(path, files[path]) for path in sorted(files)),
        tuple(diagnostics),
    )
