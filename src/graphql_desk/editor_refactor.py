"""Find safe Python insertion points without executing project code."""

import ast
import io
import re
import tokenize

from .source_maps import convert_offset


def extraction_contexts(source: str) -> dict[tuple[int, int], dict[str, object]]:
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError, UnicodeError, RecursionError):
        return {}

    lines = re.split(r"(?<=\n)|(?<=\r)(?!\n)", source)
    starts = [0]
    for line in lines:
        starts.append(starts[-1] + len(line))

    documents: set[str] = set()
    initializers: set[str] = set()
    module_aliases: set[str] = set()
    module_imports: set[str] = set()
    binding_lines: dict[str, int] = {}
    # A factory must have a single static binding throughout the source. This
    # deliberately rejects shadowing in nested scopes too, rather than guessing
    # which binding a transformed call will execute.
    bindings: dict[str, int] = {}

    def attribute_root(node: ast.AST) -> str | None:
        while isinstance(node, ast.Attribute):
            node = node.value
        return node.id if isinstance(node, ast.Name) else None

    def bound(name: str) -> None:
        bindings[name] = bindings.get(name, 0) + 1

    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in {"exec", "eval", "globals"}
        ):
            return {}
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            bound(node.id)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound(node.name)
        elif isinstance(node, ast.arg):
            bound(node.arg)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            bound(node.name)
        elif isinstance(node, (ast.MatchAs, ast.MatchStar)) and node.name:
            bound(node.name)
        elif isinstance(node, ast.MatchMapping) and node.rest:
            bound(node.rest)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                bound(alias.asname or alias.name.split(".")[0])
                if alias.name == "graphql_desk":
                    module_imports.add(alias.asname or alias.name)
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name == "*":
                    return {}
                bound(alias.asname or alias.name)

    mutated_modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(
            node.ctx, (ast.Store, ast.Del)
        ):
            root = attribute_root(node)
            if root in module_imports:
                mutated_modules.add(root)
        if isinstance(node, ast.Attribute) and node.attr == "__dict__":
            root = attribute_root(node)
            if root in module_imports:
                mutated_modules.add(root)
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id in {
                "setattr",
                "delattr",
            }:
                if node.args and isinstance(node.args[0], ast.Name):
                    if node.args[0].id in module_imports:
                        mutated_modules.add(node.args[0].id)
            if (
                isinstance(node.func, ast.Name)
                and node.func.id == "vars"
                and node.args
                and isinstance(node.args[0], ast.Name)
                and node.args[0].id in module_imports
            ):
                mutated_modules.add(node.args[0].id)

    for statement in tree.body:
        if (
            isinstance(statement, ast.ImportFrom)
            and statement.level == 0
            and statement.module == "graphql_desk"
        ):
            for alias in statement.names:
                if alias.name in {"gql", "graphql"}:
                    name = alias.asname or alias.name
                    if bindings.get(name) == 1:
                        documents.add(name)
                        binding_lines[name] = statement.lineno
                elif alias.name == "init_graphql":
                    name = alias.asname or alias.name
                    if bindings.get(name) == 1:
                        initializers.add(name)
                        binding_lines[name] = statement.lineno
        elif isinstance(statement, ast.Import):
            for alias in statement.names:
                name = alias.asname or alias.name.split(".")[0]
                if (
                    alias.name == "graphql_desk"
                    and bindings.get(name) == 1
                    and name not in mutated_modules
                ):
                    module_aliases.add(name)
                    binding_lines[name] = statement.lineno

    def builtin(
        node: ast.expr,
        names: set[str],
        *,
        module_document: bool = False,
        module_initializer: bool = False,
    ) -> bool:
        if isinstance(node, ast.Name):
            return node.id in names and binding_lines[node.id] < node.lineno
        return (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id in module_aliases
            and binding_lines.get(node.value.id, node.lineno) < node.lineno
            and (
                module_document
                and node.attr in {"gql", "graphql"}
                or module_initializer
                and node.attr == "init_graphql"
            )
        )

    factories: set[str] = set()
    for statement in tree.body:
        if (
            isinstance(statement, ast.Assign)
            and len(statement.targets) == 1
            and isinstance(statement.targets[0], ast.Name)
        ):
            if (
                bindings.get(statement.targets[0].id) == 1
                and isinstance(statement.value, ast.Call)
                and builtin(statement.value.func, initializers, module_initializer=True)
            ):
                factories.add(statement.targets[0].id)
                binding_lines[statement.targets[0].id] = statement.lineno

    def offset(line: int, column: int) -> int:
        return starts[line - 1] + len(
            lines[line - 1].encode("utf8")[:column].decode("utf8")
        )

    def span(node: ast.AST) -> tuple[int, int]:
        assert isinstance(node, (ast.expr, ast.stmt))
        assert node.end_lineno is not None and node.end_col_offset is not None
        return offset(node.lineno, node.col_offset), offset(
            node.end_lineno, node.end_col_offset
        )

    extractable: list[ast.stmt] = list(tree.body)
    function_assignments: set[int] = set()
    for definition in tree.body:
        if not isinstance(definition, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        scope: list[ast.AST] = list(definition.body)
        safe = True
        while scope:
            node = scope.pop()
            if isinstance(
                node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)
            ):
                continue
            if isinstance(node, (ast.Global, ast.Nonlocal)) or (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in {"locals", "vars"}
            ):
                safe = False
                break
            scope.extend(ast.iter_child_nodes(node))
        if safe:
            extractable.extend(definition.body)
            function_assignments.update(id(item) for item in definition.body)

    result: dict[tuple[int, int], dict[str, object]] = {}
    for statement in extractable:
        value: ast.expr | None
        if isinstance(statement, ast.Assign):
            if len(statement.targets) != 1 or not isinstance(
                statement.targets[0], ast.Name
            ):
                continue
            value = statement.value
        elif isinstance(statement, ast.AnnAssign) and isinstance(
            statement.target, ast.Name
        ):
            value = statement.value
        else:
            continue
        if not isinstance(value, ast.Call) or any(
            isinstance(arg, ast.Starred) for arg in value.args
        ):
            continue
        if not builtin(value.func, documents | factories, module_document=True):
            continue
        factory = ast.get_source_segment(source, value.func)
        if factory is None or not re.fullmatch(
            r"[A-Za-z_][A-Za-z_0-9]*(?:\.[A-Za-z_][A-Za-z_0-9]*)*", factory
        ):
            continue
        keywords = {item.arg: item.value for item in value.keywords}
        if (
            None in keywords
            or len(keywords) != len(value.keywords)
            or len(value.args) > 2
        ):
            continue
        source_arg = value.args[0] if value.args else keywords.get("source")
        if source_arg is None:
            continue
        fragments = value.args[1] if len(value.args) == 2 else keywords.get("fragments")
        if fragments is None:
            call_start, call_end = span(value)
            if source[call_end - 1] != ")":
                continue
            try:
                tokens = [
                    token.string
                    for token in tokenize.generate_tokens(
                        io.StringIO(source[call_start:call_end]).readline
                    )
                    if token.type
                    not in (
                        tokenize.NL,
                        tokenize.NEWLINE,
                        tokenize.COMMENT,
                        tokenize.INDENT,
                        tokenize.DEDENT,
                        tokenize.ENDMARKER,
                    )
                ]
            except (tokenize.TokenError, IndentationError):
                continue
            if len(tokens) < 2 or tokens[-1] != ")":
                continue
            insertion = call_end - 1
            mode = "new_trailing" if tokens[-2] == "," else "new"
        elif isinstance(fragments, (ast.List, ast.Tuple)):
            start, _ = span(fragments)
            if source[start] not in "([":
                continue
            insertion = start + 1
            mode = (
                "populated"
                if fragments.elts
                else (
                    "empty_tuple" if isinstance(fragments, ast.Tuple) else "empty_list"
                )
            )
        else:
            continue
        statement_start, _ = span(statement)
        line_index = statement.lineno - 1
        line_start = starts[line_index]
        indent = source[line_start:statement_start]
        if not re.fullmatch(r"[ \t]*", indent):
            continue
        while line_index and re.fullmatch(
            re.escape(indent) + r"#[^\r\n]*",
            lines[line_index - 1].rstrip("\r\n"),
        ):
            line_index -= 1
            line_start = starts[line_index]
        ending = re.search(r"\r\n|\r|\n", source[line_start:])
        result[span(value)] = {
            "statement_start": convert_offset(source, line_start),
            "fragment_insertion": convert_offset(source, insertion),
            "fragment_mode": mode,
            "indent": indent,
            "newline": ending.group() if ending else "\n",
            "factory": factory,
            "scope": "function" if id(statement) in function_assignments else "module",
        }
    return result
