"""Real AST executors and gql sessions keep their native response behavior."""

import asyncio
import json
import subprocess
import sys
import tempfile
import unittest
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any, TypedDict

from gql import Client, GraphQLRequest
from gql.transport import Transport
from gql.transport.exceptions import TransportQueryError
from graphql import (
    ExecutionResult,
    GraphQLSchema,
    build_schema,
    execute_sync,  # pyright: ignore[reportUnknownVariableType]
    parse,
)
from typing_extensions import NotRequired

from graphql_desk import TypedDocument, graphql
from graphql_desk.factory_generation import generate_factory_artifacts
from graphql_desk.fragments import SchemaContext, index_fragments
from graphql_desk.operation_types import render_operation_types
from graphql_desk.source_graph import (
    FactoryBinding,
    ModuleSnapshot,
    ProjectBindings,
    resolve_project,
)
from scripts.client_bindings import (
    execute_core,
    execute_core_sync,
    execute_gql,
    execute_gql_sync,
    subscribe_gql,
)
from tests import test_factory_generation as factory_tests


class Variables(TypedDict):
    name: NotRequired[str | None]


class Result(TypedDict):
    hello: str | None


class NoVariables(TypedDict):
    pass


def schema() -> GraphQLSchema:
    result = build_schema(
        'type Query { hello(name: String = "Ada"): String fail: String } '
        "type Mutation { increment: Int! incrementBy(delta: Int!): Int! } "
        "type Subscription { tick: Int! }"
    )
    assert result.query_type is not None

    def hello(*_args: Any, name: str | None = "Ada") -> str:
        return "NULL" if name is None else name

    result.query_type.fields["hello"].resolve = hello
    return result


def document() -> TypedDocument[Result, Variables]:
    return TypedDocument(
        parse(
            "query Other { __typename } query Read($name:String) { hello(name:$name) }"
        ),
        "Read",
    )


class SyncSchemaTransport(Transport):
    """Test-only transport: the real gql client sends ASTs to a local executor."""

    def __init__(self, schema_: GraphQLSchema) -> None:
        self.schema = schema_
        self.requests: list[GraphQLRequest] = []
        self.responses: list[ExecutionResult] = []
        self.failure: RuntimeError | None = None

    def execute(
        self, request: GraphQLRequest, *args: Any, **kwargs: Any
    ) -> ExecutionResult:
        self.requests.append(request)
        if self.failure is not None:
            raise self.failure
        response = execute_sync(
            self.schema,
            request.document,
            variable_values=request.variable_values,
            operation_name=request.operation_name,
        )
        response.extensions = {"unchanged": True}
        self.responses.append(response)
        return response


class ClientBindingsTests(unittest.TestCase):
    def test_generated_factory_infers_real_client_bindings_and_executes_original_ast(
        self,
    ) -> None:
        source = """from typing import TYPE_CHECKING
from typing_extensions import assert_type
from collections.abc import AsyncGenerator, AsyncIterator
from gql.client import SyncClientSession, AsyncClientSession
from graphql import GraphQLSchema
from generated.main import graphql
from generated.sources.authored import (
 DocResult as ReadResult, DocVariables as ReadVariables,
 UpdateResult, UpdateVariables, EventsResult, EventsVariables,
)
from client_bindings import (
 TypedResponse, execute_core_sync, execute_core, execute_gql_sync,
 execute_gql, subscribe_gql,
)
doc=graphql(
 "query Other { __typename } query Read($name:String){hello(name:$name)}",
 operation="Read",
)
update=graphql(
 "mutation Update($delta:Int!){incrementBy(delta:$delta)}", operation="Update"
)
events=graphql("subscription Events { tick }", operation="Events")
variables=ReadVariables(name="Grace")
update_variables=UpdateVariables(delta=2)
events_variables=EventsVariables()
def sync(schema:GraphQLSchema,session:SyncClientSession)->TypedResponse[ReadResult]:
 assert_type(execute_core_sync(schema,doc,variables),TypedResponse[ReadResult])
 response=execute_gql_sync(session,doc,variables)
 assert_type(response.data,ReadResult|None)
 assert_type(execute_core_sync(schema,update,update_variables),TypedResponse[UpdateResult])
 assert_type(execute_gql_sync(session,update,update_variables),TypedResponse[UpdateResult])
 return response
async def asynchronous(schema:GraphQLSchema,session:AsyncClientSession)->None:
 assert_type(await execute_core(schema,doc,variables),TypedResponse[ReadResult])
 assert_type(await execute_gql(session,doc,variables),TypedResponse[ReadResult])
 assert_type(
  await execute_core(schema,update,update_variables),TypedResponse[UpdateResult]
 )
 assert_type(
  await execute_gql(session,update,update_variables),TypedResponse[UpdateResult]
 )
 _event_stream: AsyncIterator[TypedResponse[EventsResult]] = subscribe_gql(
  session,events,events_variables
 )
 assert_type(
  subscribe_gql(session,events,events_variables),
  AsyncGenerator[TypedResponse[EventsResult],None],
 )
if TYPE_CHECKING:
 def wrong(schema:GraphQLSchema,session:SyncClientSession)->None:
  execute_core_sync(schema,doc,ReadVariables(name=1)) # error
  execute_gql_sync(session,doc,{"name":1}) # error
 async def wrong_async(schema:GraphQLSchema,session:AsyncClientSession)->None:
  await execute_core(schema,doc,ReadVariables(name=1)) # error
  await execute_gql(session,doc,{"name":1}) # error
  subscribe_gql(session,doc,ReadVariables(name=1)) # error
"""
        module = ModuleSnapshot("p", "authored", "file:///authored.py", 1, source)
        context = SchemaContext("main", schema(), "client-fixture")
        resolved = resolve_project(
            (module,),
            bindings=(
                ProjectBindings(
                    "p",
                    ("main",),
                    factories=(FactoryBinding("generated.main", "graphql", "main"),),
                ),
            ),
        )
        artifacts = generate_factory_artifacts(
            context,
            index_fragments(resolved, schemas={("p", "main"): context}),
            modules=(module,),
            project_id="p",
            generated_package="generated",
        )
        self.assertEqual(artifacts.diagnostics, ())
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            helper = factory_tests.FactoryGenerationTests()
            helper.write(root, artifacts, (module,))
            repository = Path(__file__).resolve().parents[1]
            (root / "client_bindings.py").write_text(
                (repository / "scripts/client_bindings.py").read_text(encoding="utf8"),
                encoding="utf8",
            )
            errors = {
                n for n, line in enumerate(source.splitlines(), 1) if "# error" in line
            }
            helper.check(root, {"authored.py": errors})
            runtime = f"""import sys
import asyncio
sys.path.insert(0,{str(repository)!r})
sys.path.insert(0,{str(root)!r})
from tests.test_client_bindings import schema,SyncSchemaTransport
from gql import Client
from authored import doc,variables,sync,update,update_variables,events,events_variables
from generated.sources.authored import DocVariables as ReadVariables
from client_bindings import (
 execute_core_sync,execute_core,execute_gql_sync,execute_gql,subscribe_gql
)
transport=SyncSchemaTransport(schema())
transport.schema.mutation_type.fields["incrementBy"].resolve = (
 lambda _root,info,delta: delta+1
)
with Client(schema=transport.schema,transport=transport) as session:
 response=sync(transport.schema,session)
 assert response is transport.responses[-2]
 assert response.data=={{"hello":"Grace"}}
 assert response.extensions=={{"unchanged":True}}
 assert transport.responses[-1].data=={{"incrementBy":3}}
 request=transport.requests[-2]
 assert request.document is doc.ast
 assert request.operation_name==doc.operation_name=="Read"
 assert request.variable_values is variables
 assert execute_gql_sync(session,doc,ReadVariables()).data=={{"hello":"Ada"}}
 assert execute_gql_sync(session,doc,ReadVariables(name=None)).data=={{"hello":"NULL"}}
 assert execute_core_sync(transport.schema,doc,variables).data=={{"hello":"Grace"}}
 assert execute_core_sync(transport.schema,doc,ReadVariables()).data=={{"hello":"Ada"}}
 assert execute_core_sync(
  transport.schema,doc,ReadVariables(name=None)
 ).data=={{"hello":"NULL"}}
 assert execute_core_sync(
  transport.schema,update,update_variables
 ).data=={{"incrementBy":3}}
async def verify_async():
 schema_=schema()
 assert schema_.mutation_type is not None
 schema_.mutation_type.fields["incrementBy"].resolve = (
  lambda _root,info,delta: delta+1
 )
 async with Client(schema=schema_,execute_timeout=None) as session:
  assert (await execute_core(schema_,doc,variables)).data=={{"hello":"Grace"}}
  assert (await execute_core(schema_,doc,ReadVariables())).data=={{"hello":"Ada"}}
  assert (
   await execute_core(schema_,doc,ReadVariables(name=None))
  ).data=={{"hello":"NULL"}}
  assert (await execute_core(schema_,update,update_variables)).data=={{"incrementBy":3}}
  assert (await execute_gql(session,doc,variables)).data=={{"hello":"Grace"}}
  assert (await execute_gql(session,doc,ReadVariables())).data=={{"hello":"Ada"}}
  assert (
   await execute_gql(session,doc,ReadVariables(name=None))
  ).data=={{"hello":"NULL"}}
  assert (await execute_gql(session,update,update_variables)).data=={{"incrementBy":3}}
  assert schema_.subscription_type is not None
  async def ticks(*_args):
   yield {{"tick":7}}
  schema_.subscription_type.fields["tick"].subscribe=ticks
  stream=subscribe_gql(session,events,events_variables)
  assert (await stream.__anext__()).data=={{"tick":7}}
  await stream.aclose()
asyncio.run(verify_async())
"""
            result = subprocess.run(
                [sys.executable, "-c", runtime],
                cwd=root,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_core_omitted_null_operation_and_partial_error_envelope(self) -> None:
        schema_ = schema()
        self.assertEqual(
            execute_core_sync(schema_, document(), Variables()).data, {"hello": "Ada"}
        )
        self.assertEqual(
            execute_core_sync(schema_, document(), Variables(name=None)).data,
            {"hello": "NULL"},
        )
        assert schema_.query_type is not None

        def failure(*_args: Any) -> None:
            raise ValueError("resolver failed")

        schema_.query_type.fields["fail"].resolve = failure
        failing = TypedDocument[dict[str, str | None], NoVariables](
            parse("{ hello fail }")
        )
        response = execute_core_sync(schema_, failing, NoVariables())
        self.assertIsInstance(response, ExecutionResult)
        self.assertEqual(response.data, {"hello": "Ada", "fail": None})
        assert response.errors is not None
        self.assertEqual(str(response.errors[0].original_error), "resolver failed")
        self.assertIsNone(response.extensions)

    def test_real_sync_client_preserves_ast_envelope_exceptions_and_mutation(
        self,
    ) -> None:
        schema_ = schema()
        transport = SyncSchemaTransport(schema_)
        with Client(schema=schema_, transport=transport) as session:
            doc = document()
            omitted = Variables()
            response = execute_gql_sync(session, doc, omitted)
            self.assertEqual(response.data, {"hello": "Ada"})
            self.assertIs(transport.requests[-1].variable_values, omitted)
            explicit_null = Variables(name=None)
            response = execute_gql_sync(session, doc, explicit_null)
            self.assertEqual(response.data, {"hello": "NULL"})
            self.assertIs(transport.requests[-1].variable_values, explicit_null)
            response = execute_gql_sync(session, doc, Variables(name="Grace"))
            self.assertIs(response, transport.responses[-1])
            self.assertIs(transport.requests[-1].document, doc.ast)
            self.assertEqual(transport.requests[-1].operation_name, "Read")
            self.assertEqual(response.data, {"hello": "Grace"})
            self.assertEqual(response.extensions, {"unchanged": True})
            assert schema_.mutation_type is not None
            count = [0]

            def increment(*_args: Any) -> int:
                count[0] += 1
                return count[0]

            schema_.mutation_type.fields["increment"].resolve = increment
            mutation = TypedDocument[dict[str, int], NoVariables](
                parse("mutation Update { increment }"), "Update"
            )
            self.assertEqual(
                execute_gql_sync(session, mutation, NoVariables()).data,
                {"increment": 1},
            )
            transport.failure = RuntimeError("native transport failure")
            with self.assertRaises(RuntimeError) as caught:
                execute_gql_sync(session, doc, Variables())
            self.assertIs(caught.exception, transport.failure)
            transport.failure = None
            assert schema_.query_type is not None

            def failure(*_args: Any) -> None:
                raise ValueError("partial failure")

            schema_.query_type.fields["fail"].resolve = failure
            failing = TypedDocument[dict[str, str | None], NoVariables](
                parse("{ hello fail }")
            )
            with self.assertRaises(TransportQueryError) as partial:
                execute_gql_sync(session, failing, NoVariables())
            original = transport.responses[-1]
            self.assertIs(partial.exception.data, original.data)
            self.assertIs(partial.exception.errors, original.errors)
            self.assertIs(partial.exception.extensions, original.extensions)

    def test_fragment_composition_is_standard_ast_client_input(self) -> None:
        fragment = graphql("fragment Greeting on Query { hello }")
        composed = graphql("query Read { ...Greeting }", [fragment])
        doc = TypedDocument[Result, NoVariables](composed.ast, composed.operation_name)
        transport = SyncSchemaTransport(schema())
        with Client(schema=transport.schema, transport=transport) as session:
            self.assertEqual(
                execute_gql_sync(session, doc, NoVariables()).data, {"hello": "Ada"}
            )
        self.assertIs(transport.requests[0].document, composed.ast)
        self.assertIs(composed.ast.definitions[1], fragment.ast.definitions[0])

    def test_generic_sync_async_and_subscription_checker_boundaries(self) -> None:
        source = "query Read($name:String!){hello(name:$name)}"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "generated.py").write_text(
                render_operation_types(schema(), source, name="Read"), encoding="utf-8"
            )
            (root / "client_bindings.py").write_text(
                (
                    Path(__file__).resolve().parents[1] / "scripts/client_bindings.py"
                ).read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            (root / "fixture.py").write_text(
                "from typing_extensions import assert_type\n"
                "from collections.abc import AsyncGenerator\n"
                "from gql.client import SyncClientSession, AsyncClientSession\n"
                "from graphql import parse, GraphQLSchema\n"
                "from graphql_desk import TypedDocument\n"
                "from generated import ReadResult, ReadVariables\n"
                "from client_bindings import TypedResponse, execute_core_sync, "
                "execute_core, execute_gql_sync, execute_gql, subscribe_gql\n"
                "doc = TypedDocument[ReadResult,ReadVariables]"
                f"(parse({source!r}),'Read')\n"
                "def sync(schema:GraphQLSchema, session:SyncClientSession)->None:\n"
                "    variables = ReadVariables(name='Ada')\n"
                "    assert_type(execute_core_sync(schema,doc,variables),"
                "TypedResponse[ReadResult])\n"
                "    assert_type(execute_gql_sync(session,doc,variables).data,"
                "ReadResult|None)\n"
                "    execute_gql_sync(session,doc,{'name':1})\n"
                "    execute_core_sync(schema,doc,{})\n"
                "async def asynchronous(schema:GraphQLSchema,"
                "session:AsyncClientSession)->None:\n"
                "    variables = ReadVariables(name='Ada')\n"
                "    assert_type(await execute_core(schema,doc,variables),"
                "TypedResponse[ReadResult])\n"
                "    assert_type(await execute_gql(session,doc,variables),"
                "TypedResponse[ReadResult])\n"
                "    assert_type(subscribe_gql(session,doc,variables),"
                "AsyncGenerator[TypedResponse[ReadResult],None])\n"
                "    await execute_gql(session,doc,{'name':None})\n"
                "    subscribe_gql(session,doc,{'name':1})\n",
                encoding="utf-8",
            )
            mypy = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "mypy",
                    "--strict",
                    "--no-incremental",
                    str(root),
                ],
                cwd=root,
                capture_output=True,
                text=True,
            )
            self.assertEqual(mypy.returncode, 1, mypy.stdout + mypy.stderr)
            self.assertIn("Found 4 errors in 1 file", mypy.stdout)
            (root / "pyrightconfig.json").write_text(
                json.dumps(
                    {
                        "include": ["."],
                        "typeCheckingMode": "strict",
                        "pythonVersion": "3.10",
                    }
                ),
                encoding="utf-8",
            )
            pyright = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pyright",
                    "--pythonpath",
                    sys.executable,
                    "--outputjson",
                ],
                cwd=root,
                capture_output=True,
                text=True,
            )
            self.assertEqual(pyright.returncode, 1, pyright.stdout + pyright.stderr)
            errors = json.loads(pyright.stdout)["generalDiagnostics"]
            self.assertEqual(len(errors), 4, errors)
            self.assertEqual(
                {error["range"]["start"]["line"] for error in errors}, {12, 13, 19, 20}
            )


class AsyncClientBindingsTests(unittest.IsolatedAsyncioTestCase):
    async def test_async_core_and_real_client_execution(self) -> None:
        schema_ = schema()
        assert schema_.query_type is not None

        async def hello(*_args: Any, name: str | None = "Ada") -> str:
            await asyncio.sleep(0)
            return "NULL" if name is None else name

        schema_.query_type.fields["hello"].resolve = hello
        self.assertEqual(
            (await execute_core(schema_, document(), Variables())).data,
            {"hello": "Ada"},
        )
        async with Client(schema=schema_, execute_timeout=None) as session:
            self.assertEqual(
                (await execute_gql(session, document(), Variables())).data,
                {"hello": "Ada"},
            )
            self.assertEqual(
                (await execute_gql(session, document(), Variables(name=None))).data,
                {"hello": "NULL"},
            )
            self.assertEqual(
                (await execute_gql(session, document(), Variables(name="Grace"))).data,
                {"hello": "Grace"},
            )

    async def test_real_async_client_selects_mutation_operation(self) -> None:
        schema_ = schema()
        assert schema_.mutation_type is not None
        calls: list[int] = []

        async def increment(*_args: Any) -> int:
            calls.append(1)
            await asyncio.sleep(0)
            return len(calls)

        schema_.mutation_type.fields["increment"].resolve = increment
        doc = TypedDocument[dict[str, int], NoVariables](
            parse("mutation Other { other: increment } mutation Update { increment }"),
            "Update",
        )
        async with Client(schema=schema_, execute_timeout=None) as session:
            response = await execute_gql(session, doc, NoVariables())

        self.assertEqual(response.data, {"increment": 1})
        self.assertEqual(calls, [1])

    async def test_subscription_yields_envelopes_and_closes_original_source(
        self,
    ) -> None:
        schema_ = schema()
        closed = asyncio.Event()

        async def events(*_args: Any) -> AsyncGenerator[dict[str, int], None]:
            try:
                for value in range(3):
                    yield {"tick": value}
            finally:
                closed.set()

        assert schema_.subscription_type is not None
        schema_.subscription_type.fields["tick"].subscribe = events
        doc = TypedDocument[dict[str, int], NoVariables](
            parse("subscription Events { tick }"), "Events"
        )
        async with Client(schema=schema_, execute_timeout=None) as session:
            stream = subscribe_gql(session, doc, NoVariables())
            response = await stream.__anext__()
            self.assertIsInstance(response, ExecutionResult)
            self.assertEqual(response.data, {"tick": 0})
            await stream.aclose()
            await asyncio.wait_for(closed.wait(), 1)

    async def test_cancellation_and_subscription_errors_propagate(self) -> None:
        schema_ = schema()
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def pending(*_args: Any, **_kwargs: Any) -> str:
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
            return "unreachable"

        assert schema_.query_type is not None
        schema_.query_type.fields["hello"].resolve = pending
        async with Client(schema=schema_, execute_timeout=None) as session:
            task = asyncio.create_task(execute_gql(session, document(), Variables()))
            await asyncio.wait_for(started.wait(), 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            await asyncio.wait_for(cancelled.wait(), 1)
        failure = RuntimeError("original subscription failure")

        async def broken(*_args: Any) -> AsyncGenerator[dict[str, int], None]:
            yield {"tick": 1}
            raise failure

        assert schema_.subscription_type is not None
        schema_.subscription_type.fields["tick"].subscribe = broken
        doc = TypedDocument[dict[str, int], NoVariables](parse("subscription {tick}"))
        async with Client(schema=schema_, execute_timeout=None) as session:
            stream = subscribe_gql(session, doc, NoVariables())
            await stream.__anext__()
            with self.assertRaises(RuntimeError) as caught:
                await stream.__anext__()
            self.assertIs(caught.exception, failure)
