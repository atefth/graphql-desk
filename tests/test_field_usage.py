"""Usage warnings require typed provenance and remain conservative on escapes."""

import unittest

from graphql import build_schema

from graphql_desk.field_usage import FieldUsageOptions, field_usage_diagnostics
from graphql_desk.fragments import SchemaContext, index_fragments
from graphql_desk.source_graph import ModuleSnapshot, ProjectBindings, resolve_project

SCHEMA = build_schema(
    "type User { id: ID! _id: ID name: String email: String friends: [User!]! } "
    "type Query { user: User users: [User!]! other: User } "
    "type Mutation { user: User } type Subscription { user: User }"
)
PREFIX = """from typing import TypeVar
from graphql_desk import gql, TypedDocument
R = TypeVar("R")
V = TypeVar("V")
def execute(document: TypedDocument[R, V], variables: V) -> R: ...
query = gql("{ user { id name email } }")
"""


def module(name: str, source: str) -> ModuleSnapshot:
    return ModuleSnapshot("p", name, f"file:///project/{name}.py", 1, source)


def warnings(
    source: str,
    *,
    extra: tuple[ModuleSnapshot, ...] = (),
    options: FieldUsageOptions = FieldUsageOptions(),
) -> list[tuple[str, str]]:
    modules = (module("main", source), *extra)
    context = SchemaContext("default", SCHEMA, "schema")
    schemas: dict[tuple[str, str | None], SchemaContext] = {
        ("p", None): context,
        ("p", "default"): context,
    }
    sources = resolve_project(
        modules, bindings=(ProjectBindings("p", ("default",), "default"),)
    )
    index = index_fragments(sources, schemas=schemas)
    result = field_usage_diagnostics(modules, index, schemas, {"p": options})
    return [
        (source[item.range.start : item.range.end], item.message)
        for item in result
        if item.module.module == "main"
    ]


class FieldUsageTests(unittest.TestCase):
    def test_direct_typed_execution_aliases_and_leaf_reads(self) -> None:
        for read in (
            'print(result["user"]["name"])',
            "print(result.user.name)",
            'user = result["user"]\nname = user["name"]\nprint(name)',
            'alias = result\nprint(alias["user"]["name"])',
            'if result["user"] is not None:\n    print(result["user"]["name"])',
            'assert result["user"] is not None\nprint(result["user"]["name"])',
        ):
            with self.subTest(read=read):
                found = warnings(PREFIX + "result = execute(query, {})\n" + read)
                self.assertEqual([field for field, _ in found], ["user"])
                self.assertIn("user.email", found[0][1])

    def test_no_result_consumption_and_unknown_provenance_stay_silent(self) -> None:
        for use in (
            "result = execute(query, {})",
            'result = {"user": {"name": "x"}}\nprint(result["user"]["name"])',
            'result = untyped(query)\nprint(result["user"]["name"])',
            "def execute(document, variables): ...\n"
            'result = execute(query, {})\nprint(result["user"]["name"])',
            'result = wrap(execute(query, {}))\nprint(result["user"]["name"])',
        ):
            with self.subTest(use=use):
                self.assertEqual(warnings(PREFIX + use), [])

    def test_escaped_subtrees_dynamic_keys_and_unknown_methods_are_consumed(
        self,
    ) -> None:
        for use in (
            'send(result["user"])',
            "send(result)",
            'store["user"] = result["user"]',
            'stored = {"user": result["user"]}',
            'result["user"][key]',
            'result["user"].get("email")',
            'result["user"]["email"] = "new"',
            'store[send(result["user"])] = 1',
            'store[send(result["user"])].value = 1',
        ):
            with self.subTest(use=use):
                self.assertEqual(
                    warnings(
                        PREFIX
                        + 'result=execute(query, {})\nprint(result["user"]["name"])\n'
                        + use
                    ),
                    [],
                )

    def test_dynamic_access_only_suppresses_its_subtree(self) -> None:
        source = (
            PREFIX.replace(
                "{ user { id name email } }",
                "{ user { name email } other { name email } }",
            )
            + 'result = execute(query, {})\nprint(result["other"]["name"])\n'
            'result["user"][key]'
        )
        found = warnings(source)
        self.assertEqual([field for field, _ in found], ["other"])
        self.assertIn("other.email", found[0][1])

    def test_unknown_container_attributes_consume_nested_provenance(self) -> None:
        for container, use in (
            ('[result["user"]]', "send(box.pop())"),
            ('(result["user"],)', "send(box.count(value))"),
            ('[[result["user"]]]', "send(box.pop())"),
        ):
            with self.subTest(container=container):
                self.assertEqual(
                    warnings(
                        PREFIX
                        + "result = execute(query, {})\nbox = "
                        + container
                        + "\n"
                        + use
                        + '\nprint(result["user"]["name"])'
                    ),
                    [],
                )

    def test_comprehension_assignment_expressions_invalidate_outer_bindings(
        self,
    ) -> None:
        for expression in (
            "[(result := unrelated) for n in range(1)]",
            "{(result := unrelated) for n in range(1)}",
            "{n: (result := unrelated) for n in range(1)}",
            "((result := unrelated) for n in range(1))",
            "[n for n in range(1) if (result := unrelated)]",
        ):
            with self.subTest(expression=expression):
                self.assertEqual(
                    warnings(
                        PREFIX
                        + "result = execute(query, {})\nignored = "
                        + expression
                        + '\nprint(result["user"]["name"])'
                    ),
                    [],
                )

    def test_mutable_list_aliases_cannot_preserve_callable_or_document_identity(
        self,
    ) -> None:
        for mutation in (
            "clients[0] = untyped",
            "alias = clients\nalias[0] = untyped",
            "alias = clients\nalias.clear()\nalias.append(untyped)",
        ):
            with self.subTest(mutation=mutation):
                self.assertEqual(
                    warnings(
                        PREFIX
                        + "clients = [execute]\n"
                        + mutation
                        + "\nresult = clients[0](query, {})\n"
                        'print(result["user"]["name"])'
                    ),
                    [],
                )
        self.assertEqual(
            warnings(
                PREFIX + "documents = [query]\ndocuments[0] = unrelated\n"
                "result = execute(documents[0], {})\n"
                'print(result["user"]["name"])'
            ),
            [],
        )

    def test_class_method_consumption_is_analyzed_in_its_own_scope(self) -> None:
        source = (
            PREFIX + "class Screen:\n    def show(self):\n"
            "        result = execute(query, {})\n"
            '        print(result["user"]["name"])'
        )
        found = warnings(source)
        self.assertEqual([field for field, _ in found], ["user"])
        self.assertIn("user.email", found[0][1])

    def test_function_local_class_methods_use_module_scope_documents(self) -> None:
        source = (
            PREFIX
            + "def build_screen():\n    class Screen:\n"
            + "        def show(self):\n"
            + "            result = execute(query, {})\n"
            + '            print(result["user"]["name"])'
        )
        found = warnings(source)
        self.assertEqual([field for field, _ in found], ["user"])
        self.assertIn("user.email", found[0][1])

    def test_function_local_class_methods_use_stable_captured_documents(self) -> None:
        source = (
            PREFIX
            + "def build_screen():\n"
            + "    typed_execute = execute\n"
            + '    local_query = gql("{ user { id name email } }")\n'
            + "    class Screen:\n"
            + "        def show(self):\n"
            + "            result = typed_execute(local_query, {})\n"
            + '            print(result["user"]["name"])'
        )
        found = warnings(source)
        self.assertEqual([field for field, _ in found], ["user"])
        self.assertIn("user.email", found[0][1])

    def test_function_local_class_methods_skip_unsupported_captures(self) -> None:
        result_capture = (
            PREFIX
            + "def build_screen():\n    result = execute(query, {})\n"
            + "    class Screen:\n"
            + "        def show(self):\n"
            + '            print(result["user"]["name"])'
        )
        rebind_after_class = (
            PREFIX
            + "def build_screen():\n"
            + '    local_query = gql("{ user { id name email } }")\n'
            + "    class Screen:\n"
            + "        def show(self):\n"
            + "            result = execute(local_query, {})\n"
            + '            print(result["user"]["name"])\n'
            + "    local_query = unrelated\n"
        )
        nonlocal_rebind = (
            PREFIX
            + "def build_screen():\n"
            + '    local_query = gql("{ user { id name email } }")\n'
            + "    class Screen:\n"
            + "        def show(self):\n"
            + "            result = execute(local_query, {})\n"
            + '            print(result["user"]["name"])\n'
            + "    def replace_query():\n"
            + "        nonlocal local_query\n"
            + "        local_query = unrelated\n"
            + "    replace_query()\n"
        )
        comprehension_rebind = (
            PREFIX
            + "def build_screen():\n"
            + '    local_query = gql("{ user { id name email } }")\n'
            + "    class Screen:\n"
            + "        def show(self):\n"
            + "            result = execute(local_query, {})\n"
            + '            print(result["user"]["name"])\n'
            + "    [(local_query := unrelated) for _ in range(1)]\n"
        )
        self.assertEqual(warnings(result_capture), [])
        self.assertEqual(warnings(rebind_after_class), [])
        self.assertEqual(warnings(nonlocal_rebind), [])
        self.assertEqual(warnings(comprehension_rebind), [])

    def test_class_methods_do_not_resolve_implicit_class_cell_as_a_global(self) -> None:
        source = (
            PREFIX
            + "__class__ = execute\n"
            + "class Screen:\n    def show(self):\n"
            + "        result = __class__(query, {})\n"
            + '        print(result["user"]["name"])'
        )
        self.assertEqual(warnings(source), [])

    def test_class_expressions_conservatively_consume_result_subtrees(self) -> None:
        base = (
            PREFIX.replace(
                '"{ user { id name email } }"',
                '"{ user { id name email } other { id name email } }"',
            )
            + "result = execute(query, {})\n"
            + 'print(result["other"]["name"])\n'
        )
        metaclass = base + "class Screen(metaclass=choose(result['user'])): pass"
        annotation = (
            base
            + "class Screen:\n    if enabled:\n"
            + "        field: choose(result['user'])"
        )
        namespace_escape = (
            base
            + "class Screen(metaclass=CaptureMeta):\n"
            + "    stored = result['user']\n"
            + "    stored = None"
        )
        global_write = (
            PREFIX
            + "result = execute(query, {})\n"
            + "class Screen:\n    global result\n    result = unrelated\n"
            + 'print(result["user"]["name"])'
        )
        for source in (metaclass, annotation, namespace_escape):
            with self.subTest(source=source):
                found = warnings(source)
                self.assertEqual([field for field, _ in found], ["other"])
                self.assertIn("other.email", found[0][1])
        self.assertEqual(warnings(global_write), [])

    def test_type_checking_signature_imports_resolve_with_both_guard_forms(
        self,
    ) -> None:
        source = PREFIX.replace(
            "from typing import TypeVar", "from typing import TYPE_CHECKING, TypeVar"
        ).replace(
            "from graphql_desk import gql, TypedDocument",
            "from graphql_desk import gql",
        )
        source = source.replace(
            "def execute(document: TypedDocument[R, V], variables: V) -> R: ...",
            "if TYPE_CHECKING:\n    from graphql_desk import TypedDocument\n\n"
            "def execute(document: TypedDocument[R, V], variables: V) -> R: ...",
        )
        source += 'result = execute(query, {})\nprint(result["user"]["name"])'
        self.assertEqual([field for field, _ in warnings(source)], ["user"])

        qualified = source.replace(
            "from typing import TYPE_CHECKING, TypeVar",
            "import typing as t\nfrom typing import TypeVar",
        ).replace("if TYPE_CHECKING:", "if t.TYPE_CHECKING:")
        self.assertEqual([field for field, _ in warnings(qualified)], ["user"])

    def test_response_aliases_use_schema_names_for_reserved_fields(self) -> None:
        source = (
            PREFIX.replace(
                "{ user { id name email } }",
                "{ person: user { key: id other: _id kind: __typename "
                "label: name mail: email } }",
            )
            + 'result = execute(query, {})\nprint(result["person"]["label"])'
        )
        found = warnings(source)
        self.assertEqual([field for field, _ in found], ["user"])
        self.assertIn("person.mail", found[0][1])
        self.assertEqual(
            warnings(source, options=FieldUsageOptions(reserved_keys=("email",))), []
        )
        self.assertEqual(warnings(source, options=FieldUsageOptions(enabled=False)), [])

    def test_list_indices_iteration_comprehensions_and_nullable_guards(self) -> None:
        prefix = PREFIX.replace(
            "{ user { id name email } }", "{ users { id name email } }"
        )
        for use in (
            'print(result["users"][0]["name"])',
            'for user in result["users"]:\n    print(user["name"])',
            'names = [user["name"] for user in result["users"] if user is not None]',
            'names = {user["name"] for user in result["users"]}',
            'names = (user["name"] for user in result["users"])',
            'names = {user["id"]: user["name"] for user in result["users"]}',
        ):
            with self.subTest(use=use):
                self.assertEqual(
                    [
                        field
                        for field, _ in warnings(
                            prefix + "result=execute(query, {})\n" + use
                        )
                    ],
                    ["users"],
                )

    def test_async_client_and_typed_data_envelope(self) -> None:
        source = (
            PREFIX.replace(
                "from typing import TypeVar", "from typing import TypeVar, Protocol"
            ).replace(
                "def execute(document: TypedDocument[R, V], variables: V) -> R: ...",
                "class Response(Protocol[R]):\n    @property\n"
                "    def data(self) -> R | None: ...\n"
                "async def execute(document: TypedDocument[R, V], "
                "variables: V) -> Response[R]: ...",
            )
            + "async def show():\n    response = await execute(query, {})\n"
            "    if response.data is not None:\n"
            '        print(response.data["user"]["name"])'
        )
        self.assertEqual([field for field, _ in warnings(source)], ["user"])
        self.assertEqual(warnings(source.replace("await execute", "execute")), [])

    def test_tuple_return_and_local_tuple_unpacking(self) -> None:
        source = (
            PREFIX.replace("-> R: ...", "-> tuple[R, str]: ...")
            + "result, error = execute(query, {})\n"
            'user, label = result["user"], "ok"\nprint(user["name"])'
        )
        self.assertEqual([field for field, _ in warnings(source)], ["user"])

    def test_client_and_document_import_aliases_and_reexports(self) -> None:
        client = module("client", PREFIX.split("query =")[0])
        bridge = module("bridge", "from client import execute as run")
        source = (
            "from graphql_desk import gql\nfrom bridge import run as perform\n"
            'query = gql("{ user { id name email } }")\nalias = query\n'
            'result = perform(alias, {})\nprint(result["user"]["name"])'
        )
        self.assertEqual(
            [field for field, _ in warnings(source, extra=(client, bridge))], ["user"]
        )
        source = source.replace(
            "from bridge import run as perform", "import client"
        ).replace("perform(alias", "client.execute(alias")
        self.assertEqual(
            [field for field, _ in warnings(source, extra=(client,))], ["user"]
        )

    def test_helpers_require_resolved_import_identity(self) -> None:
        source = (
            PREFIX + "from graphql_desk.testing import read_result as fixture\n"
            'result = fixture(query, {}, ())\nprint(result["user"]["name"])'
        )
        self.assertEqual([field for field, _ in warnings(source)], ["user"])
        self.assertEqual(
            warnings(source.replace("graphql_desk.testing", "unrelated")), []
        )
        self.assertEqual(
            warnings(
                source.replace(
                    "result = fixture", "fixture = unknown\nresult = fixture"
                )
            ),
            [],
        )

    def test_scope_shadowing_overwrites_and_ambiguous_control_flow(self) -> None:
        source = (
            PREFIX + "result = execute(query, {})\ndef show(result):\n"
            '    print(result["user"]["name"])'
        )
        self.assertEqual(warnings(source), [])
        source = (
            PREFIX + "result = execute(query, {})\nresult = unrelated\n"
            'print(result["user"]["name"])'
        )
        self.assertEqual(warnings(source), [])
        source = (
            PREFIX + "result = execute(query, {})\nif condition:\n"
            '    value = result["user"]\nelse:\n    value = other\n'
            'print(value["name"])'
        )
        self.assertEqual(warnings(source), [])
        source = (
            PREFIX + "def show():\n    result = execute(query, {})\n"
            '    print(result["user"]["name"])\n    return result["user"]'
        )
        self.assertEqual(warnings(source), [])

    def test_mutations_subscriptions_invalid_documents_and_python_stay_silent(
        self,
    ) -> None:
        suffix = 'result = execute(query, {})\nprint(result["user"]["name"])'
        for prefix in (
            PREFIX.replace(
                "{ user { id name email } }", "mutation { user { name email } }"
            ),
            PREFIX.replace(
                "{ user { id name email } }", "subscription { user { name email } }"
            ),
            PREFIX.replace(
                "{ user { id name email } }", "{ user { missing name email } }"
            ),
            PREFIX + "def broken(:\n",
        ):
            self.assertEqual(warnings(prefix + suffix), [])
        source = PREFIX.replace(
            "{ user { id name email } }", "query mutationWord { user { name email } }"
        )
        self.assertEqual([field for field, _ in warnings(source + suffix)], ["user"])

    def test_exact_unicode_escaped_name_mapping_and_python_suppression(self) -> None:
        source = (
            "# 😀\r\n"
            + PREFIX.replace("{ user", "{ \\u0075ser").replace("\n", "\r\n")
            + 'result=execute(query, {})\r\nprint(result["user"]["name"])'
        )
        self.assertEqual([field for field, _ in warnings(source)], ["\\u0075", "ser"])
        source = (
            PREFIX.replace("query =", "# graphql-desk: ignore[52005]\nquery =")
            + 'result=execute(query, {})\nprint(result["user"]["name"])'
        )
        self.assertEqual(warnings(source), [])
        self.assertEqual(
            warnings(
                source.replace(
                    "# graphql-desk: ignore[52005]",
                    'note = "# graphql-desk: ignore[52005]"',
                )
            )[0][0],
            "user",
        )

    def test_fragment_reader_preserves_operation_and_fragment_provenance(self) -> None:
        source = (
            PREFIX.split("query =")[0]
            + """from graphql_desk import read_fragment as read
card = gql("fragment Card on User { name email }")
query = gql("{ user { id ...Card } }", [card])
result = execute(query, {})
user = read(card, result["user"])
print(user["name"])
"""
        )
        self.assertEqual([field for field, _ in warnings(source)], ["email"])
        unmasked = source.replace(
            "fragment Card on User", "fragment Card on User @_unmask"
        ).replace('user = read(card, result["user"])', 'user = result["user"]')
        self.assertEqual([field for field, _ in warnings(unmasked)], ["user"])
        configured = source.replace(
            'user = read(card, result["user"])', 'user = result["user"]'
        )
        self.assertEqual(
            [
                field
                for field, _ in warnings(
                    configured, options=FieldUsageOptions(unmasked_schemas=("default",))
                )
            ],
            ["user"],
        )
        listed = source.replace(
            "{ user { id ...Card } }", "{ users { id ...Card } }"
        ).replace(
            'user = read(card, result["user"])\nprint(user["name"])',
            'users = read(card, result["users"])\n'
            'for user in users:\n    print(user["name"])',
        )
        self.assertEqual([field for field, _ in warnings(listed)], ["email"])

    def test_exact_fragment_reader_establishes_its_declared_result_identity(
        self,
    ) -> None:
        source = """from graphql_desk import gql, read_fragment as read
card = gql("fragment Card on User { name email }")
def show(data):
    user = read(card, data)
    print(user["name"])
"""
        self.assertEqual([field for field, _ in warnings(source)], ["email"])
        self.assertEqual(
            warnings(
                source.replace(
                    "read_fragment as read", "read_fragment_passthrough as read"
                )
            ),
            [],
        )

    def test_masked_only_subtrees_are_not_reported_as_unused_leaves(self) -> None:
        source = (
            PREFIX.split("query =")[0]
            + """card = gql("fragment Card on User { name email }")
query = gql("{ user { ...Card } other { name } }", [card])
result = execute(query, {})
print(result["other"]["name"])
"""
        )
        self.assertEqual(warnings(source), [])

    def test_callbacks_boolean_fallbacks_and_dynamic_bindings_suppress_uncertainty(
        self,
    ) -> None:
        for use in (
            'callback = lambda: result["user"]["email"]',
            'def callback():\n    return result["user"]',
            'value = result["user"] or fallback\nuse(value)',
            'values = [result["user"]]\nfor value in values:\n    use(value)',
            "reflect = globals\nreflect()",
            'from builtins import setattr as set_attr\nset_attr(target, "x", value)',
            "from unknown import *",
        ):
            with self.subTest(use=use):
                self.assertEqual(
                    warnings(
                        PREFIX + "result=execute(query, {})\n"
                        'print(result["user"]["name"])\n' + use
                    ),
                    [],
                )

    def test_nested_callback_captures_escape_and_lambda_parameters_shadow(self) -> None:
        source = (
            PREFIX
            + """def show():
    result = execute(query, {})
    print(result["user"]["name"])
    def callback():
        return result["user"]
    return callback
"""
        )
        self.assertEqual(warnings(source), [])
        source = (
            PREFIX
            + """result = execute(query, {})
callback = lambda result: result["user"]["name"]
"""
        )
        self.assertEqual(warnings(source), [])

    def test_negative_list_indices_and_async_tuple_results(self) -> None:
        source = PREFIX.replace(
            "{ user { id name email } }", "{ users { name email } }"
        )
        self.assertEqual(
            [
                field
                for field, _ in warnings(
                    source
                    + 'result=execute(query, {})\nprint(result["users"][-1]["name"])'
                )
            ],
            ["users"],
        )
        source = (
            PREFIX.replace("def execute", "async def execute").replace(
                "-> R: ...", "-> tuple[R, str]: ..."
            )
            + """async def show():
    result, error = await execute(query, {})
    print(result["user"]["name"])
"""
        )
        self.assertEqual([field for field, _ in warnings(source)], ["user"])

    def test_multiple_unused_leaves_aggregate_once_at_their_selected_parent(
        self,
    ) -> None:
        source = PREFIX + 'result=execute(query, {})\nprint(result["user"]["id"])'
        found = warnings(source)
        self.assertEqual([field for field, _ in found], ["user"])
        self.assertIn("user.name, user.email", found[0][1])

    def test_mixed_escaped_and_used_bindings_of_same_document_are_conservative(
        self,
    ) -> None:
        source = (
            PREFIX
            + """first=execute(query, {})
second=execute(query, {})
print(first["user"]["name"])
send(second)
"""
        )
        self.assertEqual(warnings(source), [])

    def test_typed_boundaries_bind_keywords_and_reject_invalid_calls(self) -> None:
        source = PREFIX.replace("def execute(document:", "def execute(*, document:")
        suffix = '\nprint(result["user"]["name"])'
        self.assertEqual(
            [
                field
                for field, _ in warnings(
                    source + "result=execute(document=query, variables={})" + suffix
                )
            ],
            ["user"],
        )
        for call in (
            "execute(query, {})",
            "execute(document=query)",
            "execute(document=query, variables={}, extra=1)",
            "execute(*args, document=query, variables={})",
        ):
            with self.subTest(call=call):
                self.assertEqual(warnings(source + "result=" + call + suffix), [])

    def test_closure_before_assignment_does_not_invent_unused_fields(self) -> None:
        source = (
            PREFIX
            + """def show():
    def callback():
        return result["user"]["email"]
    result=execute(query, {})
    callback()
    print(result["user"]["name"])
"""
        )
        self.assertEqual(warnings(source), [])

    def test_statement_suppression_covers_multiline_graphql(self) -> None:
        source = (
            PREFIX.replace(
                'query = gql("{ user { id name email } }")',
                '# graphql-desk: ignore[52005]   \nquery = gql("""{\n'
                'user { id name email }\n}""")',
            )
            + 'result=execute(query, {})\nprint(result["user"]["name"])'
        )
        self.assertEqual(warnings(source), [])

    def test_same_shape_documents_keep_distinct_consumption_identity(self) -> None:
        source = (
            PREFIX
            + """second = gql("{ user { id name email } }")
first_result = execute(query, {})
second_result = execute(second, {})
print(first_result["user"]["name"])
print(second_result["user"]["email"])
"""
        )
        found = warnings(source)
        self.assertEqual([field for field, _ in found], ["user", "user"])
        self.assertIn("user.email", found[0][1])
        self.assertIn("user.name", found[1][1])


if __name__ == "__main__":
    unittest.main()
