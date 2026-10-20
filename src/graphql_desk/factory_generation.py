"""Pure retained-source factory artifacts; publication belongs to the caller."""

import ast
import keyword
import re
import symtable
import unicodedata
from dataclasses import dataclass
from typing import Any

from . import TypedDocument
from .config import ProjectConfig, ScalarMapping
from .document_validation import validate_indexed_document
from .fragment_helpers import render_fragment_helpers
from .fragment_refs import fragment_identity_key
from .fragments import FragmentIndex, IndexedDocument, SchemaContext
from .literal_ast import render_document_ast_types
from .operation_types import (
    render_indexed_fragment_reader_types,
    render_indexed_operation_types,
)
from .runtime import init_graphql
from .scalar_types import render_scalar_method
from .schema_types import mapped_type_declarations, render_schema_types
from .source import Span
from .source_graph import (
    DocumentIdentity,
    GraphDiagnostic,
    ModuleSnapshot,
    resolve_type_symbols,
)
from .source_maps import build_source_map
from .testing import render_unsafe_result_helper


@dataclass(frozen=True)
class GeneratedFile:
    relative_path: str
    text: str


@dataclass(frozen=True)
class FactoryArtifacts:
    files: tuple[GeneratedFile, ...]
    diagnostics: tuple[GraphDiagnostic, ...]


@dataclass(frozen=True)
class _SourceExport:
    owner: DocumentIdentity
    module: str
    symbol: str
    name: str


@dataclass(frozen=True)
class _SchemaArtifacts:
    artifacts: FactoryArtifacts
    exports: tuple[_SourceExport, ...]


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
    document: IndexedDocument, snapshot: ModuleSnapshot, annotation_symbols: set[str]
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
        try:
            symbols = symtable.symtable(
                snapshot.source, snapshot.module, "exec"
            ).get_symbols()
        except SyntaxError:
            symbols = []
        rebound = {
            symbol.get_name()
            for symbol in symbols
            if symbol.is_assigned() or symbol.is_annotated()
        }
        if not symbols:
            rebound.update(
                node.id for node in ast.walk(tree) if isinstance(node, ast.Name)
            )
        rebound.update(
            name
            for node in ast.walk(tree)
            if isinstance(node, ast.Global)
            for name in node.names
        )
        imported: list[str] = []
        pending: list[ast.AST] = [tree]
        while pending:
            node = pending.pop()
            if isinstance(
                node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)
            ):
                continue
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                imported.extend(
                    alias.asname
                    or (
                        alias.name.split(".")[0]
                        if isinstance(node, ast.Import)
                        else alias.name
                    )
                    for alias in node.names
                )
            pending.extend(ast.iter_child_nodes(node))
        for statement in tree.body:
            if statement.lineno >= call.lineno:
                break
            if isinstance(statement, ast.Import):
                for alias in statement.names:
                    local = alias.asname or alias.name
                    if (
                        alias.name in {"typing", "typing_extensions"}
                        and imported.count(local) == 1
                    ):
                        typing_names[local] = alias.name
            elif isinstance(statement, ast.ImportFrom) and statement.level == 0:
                if statement.module in {"typing", "typing_extensions"}:
                    typing_names.update(
                        {
                            alias.asname
                            or alias.name: f"{statement.module}.{alias.name}"
                            for alias in statement.names
                            if imported.count(alias.asname or alias.name) == 1
                        }
                    )
            if (
                not isinstance(statement, ast.AnnAssign)
                or not isinstance(statement.target, ast.Name)
                or statement.target.id != source.id
            ):
                continue
            annotation = statement.annotation
            marker = (
                annotation.value
                if isinstance(annotation, ast.Subscript)
                else annotation
            )
            if isinstance(marker, ast.Name):
                symbol = (
                    typing_names.get(marker.id) if marker.id not in rebound else None
                )
            elif isinstance(marker, ast.Attribute) and isinstance(
                marker.value, ast.Name
            ):
                root = marker.value.id
                namespace = typing_names.get(root) if root not in rebound else None
                symbol = f"{namespace}.{marker.attr}" if namespace else None
            else:
                symbol = None
            literal_source = symbol in annotation_symbols and (
                (
                    symbol in {"typing.Final", "typing_extensions.Final"}
                    and not isinstance(annotation, ast.Subscript)
                    and isinstance(statement.value, ast.Constant)
                    and statement.value.value == document.call.source
                )
                or (
                    symbol in {"typing.Literal", "typing_extensions.Literal"}
                    and isinstance(annotation, ast.Subscript)
                    and isinstance(annotation.slice, ast.Constant)
                    and annotation.slice.value == document.call.source
                )
            )
    messages: list[tuple[ast.expr, str]] = []
    if not literal_source:
        assert source is not None
        messages.append(
            (
                source,
                "Exact overload inference requires an inline string literal; "
                "resolved aliases and concatenations may widen to str in a checker",
            )
        )
    if fragments is not None and not isinstance(fragments, ast.Tuple):
        messages.append(
            (
                fragments,
                "Exact fragment overload inference requires a fixed tuple; "
                "lists and widened fragment collections use the untyped fallback",
            )
        )
    if operation is not None and not (
        isinstance(operation, ast.Constant)
        and isinstance(operation.value, (str, type(None)))
    ):
        messages.append(
            (operation, "Exact operation selection requires a literal keyword value")
        )
    return [
        GraphDiagnostic(
            document.call.module,
            "factory-precision",
            message,
            Span(
                offset(node.lineno, node.col_offset),
                offset(node.end_lineno, node.end_col_offset),
            ),
        )
        for node, message in messages
        if node.end_lineno is not None and node.end_col_offset is not None
    ]


def _generate_factory_artifacts(
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
) -> _SchemaArtifacts:
    """Generate one schema's retained runtime/stub files without writing them.

    The caller supplies current, trusted resolver/index snapshots. The general str
    fallback exposes AST metadata only, without trusted result/variable types.
    Checkers also use this opaque view for incorrect known constructor calls.
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
        return _SchemaArtifacts(FactoryArtifacts((), index.diagnostics), ())
    if index.unavailable:
        raise ValueError("Rejected indexed documents cannot generate factory artifacts")
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
    exports: list[_SourceExport] = []
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
        annotation_symbols = {
            item.symbol
            for item in resolve_type_symbols(
                (
                    "typing.Final",
                    "typing.Literal",
                    "typing_extensions.Final",
                    "typing_extensions.Literal",
                ),
                modules=modules,
                project_id=project_id,
            )
            if item.status == "external" and item.target == item.symbol
        }
        schema_text = render_schema_types(context.schema, scalars=scalars, enums=enums)
        symbols = tuple(
            {
                *(
                    symbol
                    for mapping in scalars
                    for symbol in (mapping.input, mapping.output)
                ),
                *(symbol for _, symbol in enums),
            }
        )
        if symbols:
            for resolution in resolve_type_symbols(
                symbols, modules=modules, project_id=project_id
            ):
                if resolution.status not in {"resolved", "external"}:
                    raise ValueError(
                        f"Configured mapping {resolution.symbol!r}: "
                        f"{resolution.message}"
                    )
        if "graphql" in context.schema.type_map:
            raise ValueError("Schema aliases collide with generated factory exports")
        rejected: set[DocumentIdentity] = set()
        for document in documents:
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
            diagnostics.extend(
                _precision_diagnostics(document, snapshot, annotation_symbols)
            )
            identity = DocumentIdentity(call.module, call.range)
            selected = document.selected_operation
            primary = document.primary_fragment
            if primary is not None:
                if (
                    primary.kind != "fragment"
                    or primary.name is None
                    or primary not in document.definitions
                    or primary.document != identity
                    or selected is not None
                    or call.operation is not None
                ):
                    raise ValueError("Indexed primary fragment identity differs")
            elif (
                selected is None
                or selected.kind == "fragment"
                or selected not in document.definitions
                or selected.document != identity
                or carrier(document).operation_name != selected.name
            ):
                raise ValueError("Indexed selected operation identity differs")
            errors = validate_indexed_document(
                context,
                document,
                fragment_index=index,
                modules=modules,
                client_directives=client_directives,
            )
            if errors:
                rejected.add(identity)
                diagnostics.extend(errors)
            carrier(document)
        representatives = {identity: identity for identity in owners}
        # Coalesced fragment views also make their consumers indistinguishable.
        changed = True
        while changed:
            previous_rejected = rejected.copy()
            previous_representatives = representatives.copy()
            for document in documents:
                identity = DocumentIdentity(document.call.module, document.call.range)
                if any(ref.target in rejected for ref in document.call.fragments):
                    rejected.add(identity)
            groups: dict[
                tuple[str, str | None, tuple[DocumentIdentity, ...]],
                list[DocumentIdentity],
            ] = {}
            for document in documents:
                selected = document.selected_operation
                group_signature = (
                    document.call.source,
                    selected.name if selected else None,
                    tuple(
                        representatives[ref.target] for ref in document.call.fragments
                    ),
                )
                groups.setdefault(group_signature, []).append(
                    DocumentIdentity(document.call.module, document.call.range)
                )
            for group in groups.values():
                if any(identity in rejected for identity in group):
                    rejected.update(group)
                    for identity in group:
                        representatives[identity] = group[0]
            changed = (
                rejected != previous_rejected
                or representatives != previous_representatives
            )
        for number, document in enumerate(documents):
            call = document.call
            error_call = call
            identity = DocumentIdentity(call.module, call.range)
            label = _pascal(call.binding) if call.binding else f"Inline{number}"
            if not _identifier(label):
                raise ValueError("Document binding has no safe generated alias")
            path = f"{base}/_documents/{context.schema_name}/d{number}"
            module = f"{generated_package}._documents.{context.schema_name}.d{number}"
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
            if identity in rejected:
                diagnostics.append(
                    GraphDiagnostic(
                        call.module,
                        "factory-unavailable",
                        "Document or indistinguishable constructor "
                        "depends on invalid GraphQL",
                        call.range,
                    )
                )
                representative = representatives[identity]
                if representative != identity:
                    module, name, _ = declarations[representative]
                else:
                    packages(path + ".py")
                    put(
                        path + ".py",
                        "from abc import ABC, abstractmethod\n"
                        "from graphql import DocumentNode\n"
                        f"class {name}Unavailable(ABC):\n"
                        '    """Annotation-only unavailable document view."""\n'
                        "    @property\n    @abstractmethod\n"
                        "    def ast(self) -> DocumentNode: raise NotImplementedError\n"
                        "    @property\n    @abstractmethod\n"
                        "    def operation_name(self) -> str | None: "
                        "raise NotImplementedError\n"
                        "    @property\n    @abstractmethod\n"
                        "    def schema_name(self) -> str | None: "
                        "raise NotImplementedError\n",
                    )
                    put(
                        path + ".pyi",
                        (
                            "from abc import ABC, abstractmethod\n"
                            "from graphql import DocumentNode\n"
                            f"class {name}Unavailable(ABC):\n"
                            "    @property\n    @abstractmethod\n"
                            "    def ast(self) -> DocumentNode: ...\n"
                            "    @property\n    @abstractmethod\n"
                            "    def operation_name(self) -> str | None: ...\n"
                            "    @property\n    @abstractmethod\n"
                            "    def schema_name(self) -> str | None: ...\n"
                        ),
                    )
            else:
                helpers = None
                if primary is not None:
                    reader = render_indexed_fragment_reader_types(
                        context,
                        document,
                        name=name,
                        scalars=scalars,
                        enums=enums,
                        client_directives=client_directives,
                        disable_masking=disable_masking,
                        fragment_index=index,
                        fixture_input=True,
                    )
                    helpers = render_fragment_helpers(
                        reader, name=name, identity_name=name + "Identity"
                    )
                    inputs, outputs, _ = mapped_type_declarations(
                        context.schema, scalars, enums
                    )
                    reserved = (
                        set(context.schema.type_map)
                        | set(inputs.values())
                        | set(outputs.values())
                    )
                    generated = {
                        name + "Identity",
                        name + "FragmentRef",
                        name + "Document",
                        helpers.reader_name,
                        helpers.function_name,
                        *(
                            f"{name}ReaderRef{number}"
                            for number in range(
                                len(
                                    {
                                        branch.typename
                                        for branch in reader.branches
                                        if branch.typename is not None
                                    }
                                )
                            )
                        ),
                    }
                    if reserved & generated:
                        raise ValueError(
                            "Schema aliases collide with fragment helper declarations"
                        )
                    text = reader.text
                else:
                    text = render_indexed_operation_types(
                        context,
                        document,
                        name=name,
                        scalars=scalars,
                        enums=enums,
                        client_directives=client_directives,
                        disable_masking=disable_masking,
                        fragment_index=index,
                        fixture_input=True,
                    )
                fixture_name = name + "ResultFixtureInput"
                unsafe_helper = render_unsafe_result_helper(
                    name=name, input_name=fixture_name, output_name=name + "Result"
                )
                if unsafe_helper.function_name in context.schema.type_map:
                    raise ValueError(
                        "Schema aliases collide with fixture helper declarations"
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
                put(
                    path + ".py",
                    text + (helpers.runtime if helpers else "") + unsafe_helper.runtime,
                )
                put(
                    path + "_ast.py",
                    '"""Literal AST views exist only in the matching stub."""\n',
                )
                put(path + "_ast.pyi", ast_text)
                fixture_base = (
                    f"__graphql_desk_FixtureDocument[{name}Result, "
                    f"{name}Variables, {fixture_name}]"
                )
                base_type = (
                    f"__graphql_desk_FragmentDocument[{name}Identity, "
                    f"{name}Result, {name}Variables, {name}ResultInput], {fixture_base}"
                    if primary is not None
                    else fixture_base
                )
                superclass_import = (
                    "from graphql_desk.fragment_refs import "
                    "FixtureDocument as __graphql_desk_FixtureDocument\n"
                ) + (
                    "from graphql_desk.fragment_refs import "
                    "ReadableFragmentDocument as __graphql_desk_FragmentDocument\n"
                    if primary is not None
                    else ""
                )
                stub = (
                    text
                    + (helpers.stub if helpers else "")
                    + unsafe_helper.stub
                    + "\n"
                    + superclass_import
                    + (
                        f"from .d{number}_ast import "
                        "ExactDocument as __graphql_desk_Ast\n"
                        f"\nclass {name}Document({base_type}):\n"
                        "    ast: __graphql_desk_Ast\n"
                        f"    operation_name: Literal[{runtime.operation_name!r}]\n"
                        f"    schema_name: Literal[{context.schema_name!r}]\n"
                    )
                )
                if helpers is not None:
                    stub += (
                        "    @property\n"
                        "    def _graphql_desk_reader(self) -> "
                        f"{helpers.reader_name}: ...\n"
                    )
                put(path + ".pyi", stub)
                if call.binding is not None:
                    exports.extend(
                        (
                            _SourceExport(
                                identity, module, fixture_name, label + "FixtureInput"
                            ),
                            _SourceExport(
                                identity,
                                module,
                                unsafe_helper.function_name,
                                "unsafe_read_" + call.binding + "_result",
                            ),
                        )
                    )
                if helpers is not None and call.binding is not None:
                    exports.append(
                        _SourceExport(
                            identity,
                            module,
                            helpers.function_name,
                            "read_" + call.binding,
                        )
                    )
            declarations[identity] = (module, name, label)
            if call.binding is not None:
                names = public_names.setdefault(call.module.module, set())
                if label in names:
                    raise ValueError("Normalized document binding aliases collide")
                names.add(label)
                for suffix in (
                    ("Result", "Variables", "FragmentRef", "Unavailable")
                    if identity in rejected
                    else ("Result", "Variables", "FragmentRef")
                ):
                    if suffix == "FragmentRef" and primary is None:
                        continue
                    exports.append(
                        _SourceExport(
                            identity,
                            module,
                            name + ("Unavailable" if identity in rejected else suffix),
                            label + suffix,
                        )
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
                canonical = owners[representatives[reference.target]]
                assert canonical.primary_fragment is not None
                key = fragment_identity_key(canonical, canonical.primary_fragment)
                reference_keys.append(key)
                owner_module, owner_name, _ = declarations[reference.target]
                alias = f"__graphql_desk_Fragment{number}_{len(reference_types)}"
                suffix = "Unavailable" if reference.target in rejected else "Document"
                reference_imports.append(
                    f"from {owner_module} import {owner_name}{suffix} as {alias}\n"
                )
                reference_types.append(alias)
            selected = document.selected_operation
            operation_name = selected.name if selected is not None else None
            signature = (document.call.source, operation_name, tuple(reference_keys))
            previous = signatures.get(signature)
            if previous is not None:
                if document.primary_fragment is not None and identity not in rejected:
                    raise ValueError(
                        "Identical fragment call signatures have "
                        "distinct owner identities"
                    )
                continue
            signatures[signature] = module
            imports.extend(reference_imports)
            return_alias = f"__graphql_desk_Document{number}"
            suffix = "Unavailable" if identity in rejected else "Document"
            imports.append(f"from {module} import {name}{suffix} as {return_alias}\n")
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
                "    @__graphql_desk_overload\n"
                "    def __call__(self, source: "
                f"__graphql_desk_Literal[{document.call.source!r}], "
                f"fragments: {fragments}{default}, *, "
                f"operation: {selector}) -> {return_alias}: ...\n"
            )
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
            "    def __call__(self, source: str, "
            "fragments: __graphql_desk_Iterable["
            "__graphql_desk_UnresolvedDocument] = (), "
            "*, operation: str | None = None) -> "
            "__graphql_desk_UnresolvedDocument: ...\n"
        )
        typing_import = (
            "from typing import Protocol as __graphql_desk_Protocol, "
            "Literal as __graphql_desk_Literal, "
            "overload as __graphql_desk_overload"
        )
        stub = (
            schema_text
            + (
                "\n# Dynamic/nonmatching calls expose AST metadata only.\n"
                "from graphql import DocumentNode as __graphql_desk_DocumentNode\n"
                "from collections.abc import Iterable as __graphql_desk_Iterable\n"
            )
            + typing_import
            + "\n"
            + "".join(imports)
            + "\nclass __graphql_desk_UnresolvedDocument(__graphql_desk_Protocol):\n"
            "    @property\n"
            "    def ast(self) -> __graphql_desk_DocumentNode: ...\n"
            "    @property\n"
            "    def operation_name(self) -> str | None: ...\n"
            "    @property\n"
            "    def schema_name(self) -> str | None: ...\n\n"
            "class __graphql_desk_Factory(__graphql_desk_Protocol):\n"
            + "\n".join(overloads)
        )
        stub += (
            ("    @__graphql_desk_overload\n" if overloads else "")
            + fallback
            + "\n"
            + render_scalar_method(context.schema, scalars=scalars, enums=enums)
            + "\ngraphql: __graphql_desk_Factory\n"
        )
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
            return _SchemaArtifacts(
                FactoryArtifacts((), (*diagnostics, diagnostic)), ()
            )
        raise ValueError("Cannot generate schema factory artifacts") from None
    return _SchemaArtifacts(
        FactoryArtifacts(
            tuple(GeneratedFile(path, files[path]) for path in sorted(files)),
            tuple(diagnostics),
        ),
        tuple(exports),
    )


def _assemble(
    parts: tuple[_SchemaArtifacts, ...],
    modules: tuple[ModuleSnapshot, ...],
    project_id: str,
    generated_package: str,
) -> FactoryArtifacts:
    diagnostics = tuple(item for part in parts for item in part.artifacts.diagnostics)
    if any(not part.artifacts.files for part in parts):
        return FactoryArtifacts((), diagnostics)
    files: dict[str, str] = {}
    exports = sorted(
        (item for part in parts for item in part.exports),
        key=lambda item: (item.owner.module.module, item.name),
    )
    snapshots = {(item.project, item.module): item for item in modules}
    owner = exports[0].owner if exports else None
    try:
        for part in parts:
            for file in part.artifacts.files:
                if (
                    file.relative_path in files
                    and files[file.relative_path] != file.text
                ):
                    raise ValueError("Generated schema artifact paths collide")
                files[file.relative_path] = file.text
        grouped: dict[str, list[_SourceExport]] = {}
        for export in exports:
            grouped.setdefault(export.owner.module.module, []).append(export)
        for module, aliases in grouped.items():
            owner = aliases[0].owner
            if len({item.name for item in aliases}) != len(aliases):
                raise ValueError("Normalized document binding aliases collide")
            snapshot = snapshots[(project_id, module)]
            components = module.split(".")
            for length in range(1, len(components)):
                parent = snapshots.get((project_id, ".".join(components[:length])))
                if parent is not None and not parent.is_package:
                    raise ValueError("Source companion has a non-package parent")
            base = generated_package.replace(".", "/")
            path = f"{base}/sources/{module.replace('.', '/')}"
            if snapshot.is_package:
                path += "/__init__"
            segments = path.split("/")
            for length in range(1, len(segments)):
                prefix = "/".join(segments[:length])
                files.setdefault(
                    prefix + "/__init__.py", '"""Generated GraphQL Desk package."""\n'
                )
                files.setdefault(prefix + "/__init__.pyi", "")
            text = "".join(
                f"from {item.module} import {item.symbol} as {item.name}\n"
                for item in aliases
            )
            text += f"\n__all__ = {[item.name for item in aliases]!r}\n"
            files[path + ".py"] = files[path + ".pyi"] = text
        paths: dict[str, str] = {}
        module_paths: dict[str, tuple[str, bool]] = {}
        for path in files:
            previous = paths.setdefault(path.casefold(), path)
            if previous != path:
                raise ValueError("Generated file paths collide ignoring case")
            if path.endswith(".py"):
                module_path = path.removesuffix(".py")
                package = module_path.endswith("/__init__")
                if package:
                    module_path = module_path.removesuffix("/__init__")
                identity = (module_path, package)
                previous_module = module_paths.setdefault(
                    module_path.casefold(), identity
                )
                if previous_module != identity:
                    raise ValueError("Generated module and package paths collide")
    except ValueError as error:
        if owner is None:
            raise
        return FactoryArtifacts(
            (),
            (
                *diagnostics,
                GraphDiagnostic(
                    owner.module, "factory-generation", str(error), owner.call_range
                ),
            ),
        )
    return FactoryArtifacts(
        tuple(GeneratedFile(path, files[path]) for path in sorted(files)), diagnostics
    )


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
    """Generate one schema's retained files; private documents are schema-scoped."""
    part = _generate_factory_artifacts(
        context,
        index,
        modules=modules,
        project_id=project_id,
        generated_package=generated_package,
        scalars=scalars,
        enums=enums,
        client_directives=client_directives,
        disable_masking=disable_masking,
    )
    return _assemble((part,), modules, project_id, generated_package)


def generate_project_factory_artifacts(
    config: ProjectConfig,
    contexts: tuple[SchemaContext, ...],
    index: FragmentIndex,
    *,
    modules: tuple[ModuleSnapshot, ...],
    project_id: str,
) -> FactoryArtifacts:
    """Assemble all configured schemas atomically from trusted captured facts.

    No configuration reload, schema loading, project imports or writes occur.
    The caller owns freshness. Index errors remain fatal for this artifact set.
    """
    settings = {item.name: item for item in config.schemas}
    schemas = {item.schema_name: item for item in contexts}
    if (
        not project_id
        or not settings
        or len(settings) != len(config.schemas)
        or len(schemas) != len(contexts)
        or settings.keys() != schemas.keys()
        or any(not _identifier(part) for part in config.generated_package.split("."))
        or any(item.project != project_id for item in modules)
    ):
        raise ValueError("Project schema/context/module identities differ")
    package = config.root.joinpath(*config.generated_package.split("."))
    if any(
        item.schema_output != package / f"{item.name}.pyi" for item in config.schemas
    ) or len({item.schema_output for item in config.schemas}) != len(config.schemas):
        raise ValueError("Configured schema output differs from generated package")
    if index.diagnostics:
        return FactoryArtifacts((), index.diagnostics)
    if index.unavailable:
        raise ValueError("Rejected indexed documents cannot generate project artifacts")
    identities = {
        DocumentIdentity(document.call.module, document.call.range)
        for document in index.documents
    }
    if len(identities) != len(index.documents):
        raise ValueError("Indexed document identities must be unique")
    if len({(item.project, item.module) for item in modules}) != len(modules):
        raise ValueError("Source module identities must be unique")
    for name in settings:
        init_graphql(name)
    if any(
        document.call.module.project != project_id
        or document.schema_identity[0] != project_id
        or document.schema_identity[1] not in settings
        for document in index.documents
    ):
        raise ValueError("Indexed document belongs to a foreign project or schema")
    parts = tuple(
        _generate_factory_artifacts(
            schemas[name],
            FragmentIndex(
                tuple(doc for doc in index.documents if doc.schema_identity[1] == name),
                (),
            ),
            modules=modules,
            project_id=project_id,
            generated_package=config.generated_package,
            scalars=settings[name].scalars,
            enums=settings[name].enums,
            client_directives=config.client_directives,
            disable_masking=settings[name].disable_masking,
        )
        for name in sorted(settings)
    )
    return _assemble(parts, modules, project_id, config.generated_package)
