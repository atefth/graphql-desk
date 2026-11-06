import importlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from collections.abc import Callable
from pathlib import Path
from typing import cast
from unittest.mock import patch

from graphql import GraphQLSchema, build_schema, introspection_from_schema, print_schema

from graphql_desk import internal


class InternalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def schema(self, name: str, text: str) -> Path:
        path = self.root / (name + ".graphql")
        path.write_text(text, encoding="utf-8")
        return path

    def project(self, directory: Path, schema: Path) -> Path:
        directory.mkdir(exist_ok=True)
        config = directory / "pyproject.toml"
        config.write_text(
            "[tool.graphql-desk]\nschema = "
            + json.dumps(schema.as_posix())
            + '\nschema_output = "graphql_desk_generated/default.pyi"\n',
            encoding="utf-8",
        )
        return config

    def test_exports_and_constructors_are_inert_in_an_isolated_process(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                "-I",
                "-c",
                """
from pathlib import Path
from unittest.mock import patch
from graphql_desk import schema_loading, schema_url
with patch.object(Path, 'read_text', side_effect=AssertionError('file read')), \
     patch.object(schema_loading, 'load_local_schema', side_effect=AssertionError()), \
     patch.object(schema_url, 'load_from_url', side_effect=AssertionError('network')), \
     patch('threading.Thread.start', side_effect=AssertionError('background work')):
    from graphql_desk import internal
    assert all(getattr(internal, name) is not None for name in internal.__all__)
    internal.load_from_sdl(Path('missing.graphql'))
    internal.load_from_url(internal.SchemaOrigin(url='https://example.invalid/graphql'))
    internal.load_ref(internal.SchemaInput(origin=internal.SchemaOrigin(local_path=Path('missing.graphql'))))
    assert not hasattr(internal, 'setup_cache')
print('inert')
""",
            ],
            cwd=self.root,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "inert\n")
        self.assertEqual(result.stderr, "")

    def test_lazy_cached_file_loader_reload_and_failed_reload_retry(self) -> None:
        path = self.schema("schema", "type Query { old: String }")
        loader = internal.load(
            internal.LoadConfig(
                origin=internal.SchemaOrigin(local_path=Path(path.name)),
                root_path=self.root,
                name="api",
            )
        )
        first = loader.load()
        self.assertIs(loader.load(), first)
        self.assertEqual(first.introspection.get("name"), "api")
        path.write_text("type Query { newer: Int! }", encoding="utf-8")
        self.assertIs(loader.load(), first)
        second = loader.load(reload=True)
        self.assertIn("newer", print_schema(second.schema))
        self.assertIs(loader.load_schema(), second.schema)
        self.assertIs(loader.load_introspection(), second.introspection)
        path.write_text("INVALID PRIVATE_MARKER", encoding="utf-8")
        with self.assertRaises(internal.GraphQLDeskError) as error:
            loader.load(reload=True)
        self.assertNotIn("PRIVATE_MARKER", str(error.exception))
        path.write_text("type Query { recovered: Boolean }", encoding="utf-8")
        self.assertIn("recovered", print_schema(loader.load().schema))

    def test_assume_valid_is_a_boolean_hint_without_a_validation_bypass(self) -> None:
        path = self.schema("schema", "type Query { value: Int }")
        for hint in (False, True):
            config = internal.LoadConfig(
                origin=internal.SchemaOrigin(local_path=path), assume_valid=hint
            )
            self.assertIn("value", print_schema(internal.load(config).load().schema))
            path.write_text("type Query { missing: UnknownType }", encoding="utf-8")
            with self.assertRaises(internal.GraphQLDeskError):
                internal.load(config).load()
            path.write_text("type Query { value: Int }", encoding="utf-8")
        with self.assertRaises(internal.GraphQLDeskError):
            internal.BaseLoadConfig(assume_valid=cast(bool, "yes"))

    def test_file_edit_during_reload_is_not_cached_as_current(self) -> None:
        path = self.schema("schema", "type Query { original: Int }")
        original = build_schema("type Query { original: Int }")

        def competing_edit(_path: Path) -> GraphQLSchema:
            path.write_text("type Query { replacement: String! }", encoding="utf-8")
            return original

        loader = internal.load_from_sdl(path)
        with patch(
            "graphql_desk.internal.load_local_schema", side_effect=competing_edit
        ):
            with self.assertRaisesRegex(
                internal.GraphQLDeskError, "changed during load"
            ):
                loader.load()
        self.assertIn("replacement", print_schema(loader.load().schema))

    def test_named_ref_reload_isolates_failures_and_retains_output_metadata(
        self,
    ) -> None:
        first = self.schema("first", "type Query { first: String }")
        second = self.schema("second", "type Query { second: Int }")
        ref = internal.load_ref(
            internal.MultiSchemaInput(
                (
                    internal.SchemaInput(
                        name="first",
                        origin=internal.SchemaOrigin(local_path=first),
                        schema_output=self.root / "first.pyi",
                    ),
                    internal.SchemaInput(
                        name="second", origin=internal.SchemaOrigin(local_path=second)
                    ),
                )
            )
        )
        self.assertEqual(ref.version, 0)
        self.assertEqual(ref.multi, {"first": None, "second": None})
        self.assertIs(ref.load(), ref)
        self.assertIsNone(ref.current)
        initial = ref.version
        self.assertEqual(
            cast(internal.SchemaLoaderResult, ref.multi["first"]).schema_output,
            self.root / "first.pyi",
        )
        first.write_text("invalid PRIVATE_MARKER", encoding="utf-8")
        second.write_text("type Query { changed: Int }", encoding="utf-8")
        with self.assertRaises(internal.GraphQLDeskError):
            ref.load(reload=True)
        self.assertGreater(ref.version, initial)
        self.assertIsNone(ref.multi["first"])
        self.assertIn(
            "changed",
            print_schema(cast(internal.SchemaLoaderResult, ref.multi["second"]).schema),
        )
        first.write_text("type Query { fixed: Int }", encoding="utf-8")
        ref.load()
        self.assertIn(
            "fixed",
            print_schema(cast(internal.SchemaLoaderResult, ref.multi["first"]).schema),
        )

    def test_delayed_reference_load_cannot_replace_a_newer_result_or_clear_it(
        self,
    ) -> None:
        for first_fails in (False, True):
            with self.subTest(first_fails=first_fails):
                old = build_schema("type Query { old: String }")
                fresh = build_schema("type Query { fresh: Int }")
                reads = 0

                def read() -> GraphQLSchema:
                    nonlocal reads
                    reads += 1
                    if reads == 1:
                        if first_fails:
                            raise ValueError("PRIVATE_BACKEND_ERROR")
                        return old
                    return fresh

                loader = internal.SchemaLoader(read, name=None, interval=0)
                ref = internal.load_ref(
                    internal.SchemaInput(
                        origin=internal.SchemaOrigin(
                            local_path=self.root / "unused.graphql"
                        )
                    )
                )
                entered, release = threading.Event(), threading.Event()
                accept = cast(
                    Callable[
                        [
                            internal.SchemaInput,
                            internal.SchemaLoaderResult | internal.GraphQLDeskError,
                            internal.SchemaLoader,
                        ],
                        bool,
                    ],
                    getattr(ref, "_accept"),
                )

                def delayed(
                    item: internal.SchemaInput,
                    value: internal.SchemaLoaderResult | internal.GraphQLDeskError,
                    source: internal.SchemaLoader,
                ) -> bool:
                    if (
                        isinstance(value, internal.GraphQLDeskError)
                        or value.schema is old
                    ):
                        entered.set()
                        release.wait(3)
                    return accept(item, value, source)

                errors: list[internal.GraphQLDeskError] = []

                def older() -> None:
                    try:
                        ref.load(reload=True)
                    except internal.GraphQLDeskError as error:
                        errors.append(error)

                with (
                    patch("graphql_desk.internal.load", return_value=loader),
                    patch.object(ref, "_accept", side_effect=delayed),
                ):
                    thread = threading.Thread(target=older)
                    thread.start()
                    try:
                        self.assertTrue(entered.wait(3))
                        ref.load(reload=True)
                        version = ref.version
                    finally:
                        release.set()
                        thread.join(3)
                self.assertFalse(thread.is_alive())
                self.assertIs(loader.load_schema(), fresh)
                self.assertIs(
                    cast(internal.SchemaLoaderResult, ref.current).schema, fresh
                )
                self.assertEqual(ref.version, version)
                self.assertEqual(len(errors), int(first_fails))
                self.assertNotIn("PRIVATE_BACKEND_ERROR", repr(errors))

    def test_delayed_update_and_error_callbacks_do_not_publish_superseded_observations(
        self,
    ) -> None:
        for failure in (False, True):
            with self.subTest(failure=failure):
                old = build_schema("type Query { old: String }")
                fresh = build_schema("type Query { fresh: Int }")
                reads = 0

                def read() -> GraphQLSchema:
                    nonlocal reads
                    reads += 1
                    if failure and reads == 2:
                        raise ValueError("PRIVATE_BACKEND_ERROR")
                    return old if reads == 1 else fresh

                loader = internal.SchemaLoader(read, name=None, interval=0)
                ref = internal.load_ref(
                    internal.SchemaInput(
                        origin=internal.SchemaOrigin(
                            local_path=self.root / "unused.graphql"
                        )
                    )
                )
                callbacks: list[
                    tuple[internal.OnSchemaUpdate, internal.OnSchemaError | None]
                ] = []
                notifications: list[object] = []

                def subscribe(
                    updated: internal.OnSchemaUpdate,
                    failed: internal.OnSchemaError | None = None,
                ) -> Callable[[], None]:
                    callbacks.append((updated, failed))
                    return lambda: None

                with (
                    patch("graphql_desk.internal.load", return_value=loader),
                    patch.object(loader, "notify_on_update", side_effect=subscribe),
                ):
                    stop = ref.autoupdate(
                        lambda _ref, _input: notifications.append("updated"),
                        lambda _error, _input: notifications.append("failed"),
                    )
                self.addCleanup(stop)
                observation: internal.SchemaLoaderResult | internal.GraphQLDeskError
                try:
                    observation = loader.load(reload=failure)
                except internal.GraphQLDeskError as error:
                    observation = error
                entered, release = threading.Event(), threading.Event()
                accept = cast(
                    Callable[
                        [
                            internal.SchemaInput,
                            internal.SchemaLoaderResult | internal.GraphQLDeskError,
                            internal.SchemaLoader,
                        ],
                        bool,
                    ],
                    getattr(ref, "_accept"),
                )

                def delayed(
                    item: internal.SchemaInput,
                    value: internal.SchemaLoaderResult | internal.GraphQLDeskError,
                    source: internal.SchemaLoader,
                ) -> bool:
                    if value is observation:
                        entered.set()
                        release.wait(3)
                    return accept(item, value, source)

                def older() -> None:
                    if isinstance(observation, internal.GraphQLDeskError):
                        cast(internal.OnSchemaError, callbacks[0][1])(observation)
                    else:
                        callbacks[0][0](observation)

                with patch.object(ref, "_accept", side_effect=delayed):
                    thread = threading.Thread(target=older)
                    thread.start()
                    try:
                        self.assertTrue(entered.wait(3))
                        ref.load(reload=True)
                        version = ref.version
                    finally:
                        release.set()
                        thread.join(3)
                self.assertFalse(thread.is_alive())
                self.assertIs(
                    cast(internal.SchemaLoaderResult, ref.current).schema, fresh
                )
                self.assertEqual(ref.version, version)
                self.assertEqual(notifications, [])

    def test_reference_callback_error_is_reported_without_clearing_valid_state(
        self,
    ) -> None:
        schema = build_schema("type Query { value: Int }")
        loader = internal.SchemaLoader(lambda: schema, name=None, interval=0)
        ref = internal.load_ref(
            internal.SchemaInput(
                origin=internal.SchemaOrigin(local_path=self.root / "unused.graphql")
            )
        )
        updates = 0
        errors: list[str] = []

        def changed(_ref: internal.SchemaRef, _input: internal.SchemaInput) -> None:
            nonlocal updates
            updates += 1
            if updates == 1:
                raise ValueError("PRIVATE_CALLBACK_ERROR")

        with patch("graphql_desk.internal.load", return_value=loader):
            stop = ref.autoupdate(
                changed, lambda error, _input: errors.append(str(error))
            )
        self.addCleanup(stop)
        notify = cast(
            Callable[[internal.SchemaLoaderResult | internal.GraphQLDeskError], None],
            getattr(loader, "_notify"),
        )
        initial_version = ref.version
        notify(loader.load(reload=True))
        self.assertEqual(errors, ["Schema update callback failed"])
        self.assertIs(cast(internal.SchemaLoaderResult, ref.current).schema, schema)
        self.assertEqual(ref.version, initial_version + 1)
        version = ref.version
        notify(loader.load(reload=True))
        self.assertEqual(updates, 2)
        self.assertEqual(len(errors), 1)
        self.assertEqual(ref.version, version + 1)
        self.assertIs(cast(internal.SchemaLoaderResult, ref.current).schema, schema)

    def test_live_local_ref_error_recovery_and_idempotent_unsubscribe(self) -> None:
        path = self.schema("schema", "type Query { first: String }")
        ref = internal.load_ref(
            internal.SchemaInput(origin=internal.SchemaOrigin(local_path=path))
        )
        updated = threading.Event()
        failed = threading.Event()
        seen: list[str | None] = []

        def changed(value: internal.SchemaRef, item: internal.SchemaInput) -> None:
            seen.append(item.name)
            self.assertIs(value, ref)
            updated.set()

        stop = ref.autoupdate(changed, lambda _error, _input: failed.set())
        self.addCleanup(stop)
        self.assertIsNotNone(ref.current)
        path.write_text("invalid PRIVATE_MARKER", encoding="utf-8")
        self.assertTrue(failed.wait(3))
        self.assertIsNone(ref.current)
        path.write_text("type Query { recovered: Boolean }", encoding="utf-8")
        self.assertTrue(updated.wait(3))
        self.assertEqual(seen, [None])
        self.assertIn(
            "recovered",
            print_schema(cast(internal.SchemaLoaderResult, ref.current).schema),
        )
        stop()
        stop()

    def test_unsubscribe_during_inflight_read_prevents_late_callbacks(self) -> None:
        entered, release = threading.Event(), threading.Event()
        threads: list[threading.Thread] = []
        actual_thread = threading.Thread
        notifications: list[object] = []

        def read() -> GraphQLSchema:
            entered.set()
            release.wait(3)
            return build_schema("type Query { value: Int }")

        def thread(
            *, target: Callable[..., object], args: tuple[object, ...], daemon: bool
        ) -> threading.Thread:
            value = actual_thread(target=target, args=args, daemon=daemon)
            threads.append(value)
            return value

        loader = internal.SchemaLoader(read, name=None, interval=0.01)
        with patch("graphql_desk.internal.threading.Thread", side_effect=thread):
            stop = loader.notify_on_update(notifications.append, notifications.append)
            self.addCleanup(stop)
            self.assertTrue(entered.wait(3))
            stop()
            release.set()
            threads[0].join(3)
        self.assertFalse(threads[0].is_alive())
        self.assertEqual(notifications, [])

    def test_url_environment_capture_explicit_only_interval_and_redaction(self) -> None:
        environment = {"TOKEN": "PRIVATE_MARKER"}
        origin = internal.SchemaOrigin(
            url="https://example.invalid/graphql",
            headers=(("Authorization", "${TOKEN}"),),
        )
        schema = build_schema("type Query { value: Int }")
        with (
            patch("graphql_desk.internal._load_url", return_value=schema) as backend,
            patch("threading.Thread.start") as start,
        ):
            loader = internal.load_from_url(
                origin, environment=environment, fetch_interval=0
            )
            environment["TOKEN"] = "changed"
            stop = loader.notify_on_update(lambda _value: None)
            self.addCleanup(stop)
            backend.assert_not_called()
            start.assert_not_called()
            loader.load()
            self.assertEqual(
                backend.call_args.kwargs["environment"], {"TOKEN": "PRIVATE_MARKER"}
            )
            backend.side_effect = ValueError("PRIVATE_MARKER")
            with self.assertRaises(internal.GraphQLDeskError) as error:
                loader.load(reload=True)
            self.assertNotIn("PRIVATE_MARKER", str(error.exception))
        self.assertNotIn("PRIVATE_MARKER", repr(origin))
        self.assertIsNone(
            internal.get_url_config(internal.SchemaOrigin(local_path=self.root))
        )
        for interval in (-1, float("inf"), float("nan"), True):
            with (
                self.subTest(interval=interval),
                self.assertRaises(internal.GraphQLDeskError),
            ):
                internal.load_from_url(origin, fetch_interval=interval)

    def test_configured_url_zero_keeps_automatic_refresh_disabled(self) -> None:
        config = internal.parse_config(
            {
                "schema": "https://example.invalid/graphql",
                "schema_output": "graphql_desk_generated/default.pyi",
            },
            config_path=self.root / "pyproject.toml",
        )
        with (
            patch(
                "graphql_desk.internal._load_url",
                return_value=build_schema("type Query { value: Int }"),
            ) as backend,
            patch("threading.Thread.start") as start,
        ):
            ref = internal.load_ref(config)
            stop = ref.autoupdate(lambda _ref, _input: None)
            self.addCleanup(stop)
            self.assertIsNotNone(ref.current)
            backend.assert_called_once()
            start.assert_not_called()

    def test_simultaneous_callbacks_can_reload_other_schemas_without_deadlock(
        self,
    ) -> None:
        schema = build_schema("type Query { value: Int }")
        loaders = [
            internal.SchemaLoader(lambda: schema, name=name, interval=0.01)
            for name in ("first", "second")
        ]
        arrived = threading.Barrier(2)
        done = [threading.Event(), threading.Event()]
        errors: list[internal.GraphQLDeskError] = []

        def callback(index: int) -> internal.OnSchemaUpdate:
            def updated(_value: internal.SchemaLoaderResult) -> None:
                if not done[index].is_set():
                    arrived.wait(2)
                    loaders[1 - index].load(reload=True)
                    done[index].set()

            return updated

        stops = [
            loader.notify_on_update(callback(index), errors.append)
            for index, loader in enumerate(loaders)
        ]
        for stop in stops:
            self.addCleanup(stop)
        self.assertTrue(all(event.wait(3) for event in done))
        for stop in stops:
            stop()
        self.assertEqual(errors, [])

    def test_callback_failure_is_redacted_and_other_subscribers_continue(self) -> None:
        loader = internal.SchemaLoader(
            lambda: build_schema("type Query { value: Int }"), name=None, interval=0.01
        )
        failed, updated = threading.Event(), threading.Event()
        messages: list[str] = []

        def broken(_value: internal.SchemaLoaderResult) -> None:
            raise ValueError("PRIVATE_MARKER")

        def error(value: internal.GraphQLDeskError) -> None:
            messages.append(str(value))
            failed.set()

        first = loader.notify_on_update(broken, error)
        second = loader.notify_on_update(lambda _value: updated.set())
        self.addCleanup(first)
        self.addCleanup(second)
        self.assertTrue(failed.wait(3))
        self.assertTrue(updated.wait(3))
        first()
        second()
        self.assertTrue(messages)
        self.assertNotIn("PRIVATE_MARKER", repr(messages))

    def test_concurrent_callbacks_can_unsubscribe_the_whole_reference(self) -> None:
        schema = build_schema("type Query { value: Int }")
        loaders = [
            internal.SchemaLoader(lambda: schema, name=name, interval=0.01)
            for name in ("first", "second")
        ]
        ready = threading.Event()
        arrived = threading.Barrier(2)
        done = [threading.Event(), threading.Event()]
        stops: list[Callable[[], None]] = []

        def callback(index: int) -> internal.OnSchemaUpdate:
            def updated(_value: internal.SchemaLoaderResult) -> None:
                ready.wait(3)
                arrived.wait(2)
                for stop in stops:
                    stop()
                done[index].set()

            return updated

        for index, loader in enumerate(loaders):
            stops.append(loader.notify_on_update(callback(index)))
            self.addCleanup(stops[-1])
        ready.set()
        self.assertTrue(all(event.wait(3) for event in done))

    def test_partial_autoupdate_setup_tears_down_if_initial_error_callback_raises(
        self,
    ) -> None:
        path = self.schema("schema", "type Query { value: Int }")
        ref = internal.load_ref(
            internal.MultiSchemaInput(
                (
                    internal.SchemaInput(
                        name="good", origin=internal.SchemaOrigin(local_path=path)
                    ),
                    internal.SchemaInput(
                        name="bad",
                        origin=internal.SchemaOrigin(
                            local_path=self.root / "missing.graphql"
                        ),
                    ),
                )
            )
        )

        def failed(
            _error: internal.GraphQLDeskError, _input: internal.SchemaInput
        ) -> None:
            raise RuntimeError("consumer callback stopped setup")

        with patch.object(internal.SchemaLoader, "notify_on_update") as subscribe:
            with self.assertRaisesRegex(RuntimeError, "consumer callback"):
                ref.autoupdate(lambda _ref, _input: None, failed)
            subscribe.assert_called_once()
            subscribe.return_value.assert_called_once_with()

    def test_config_facade_discovery_dedup_names_and_explicit_project_roots(
        self,
    ) -> None:
        schema = self.schema("schema", "type Query { value: Int }")
        first = self.project(self.root / "first", schema)
        second = self.project(self.root / "second", schema)
        facts = internal.parse_config(
            internal.read_config_file(first), config_path=first
        )
        self.assertEqual(internal.get_schema_names_from_config(facts), {None})
        self.assertEqual(
            internal.get_schema_config_for_name(facts, None), facts.schemas[0]
        )
        self.assertIsNone(internal.get_schema_config_for_name(facts, "absent"))
        self.assertEqual(
            internal.find_config_file(first.parent / "child"), first.resolve()
        )
        self.assertEqual(internal.resolve_project_root(first), first.parent.resolve())
        self.assertEqual(len(internal.load_configs((first, first.parent, second))), 2)
        self.assertEqual(len(internal.load_configs(str(first))), 1)
        ref = internal.load_ref(facts)
        ref.load()
        self.assertIsNotNone(ref.current)
        for text in ("[invalid PRIVATE_MARKER", "[project]\nname = 'inactive'\n"):
            with self.assertRaises(internal.GraphQLDeskError) as error:
                internal.parse_config(text, config_path=first)
            self.assertNotIn("PRIVATE_MARKER", str(error.exception))
        diagnostic = internal.CheckerDiagnostic("checker unavailable", code="missing")
        self.assertIs(
            internal.CheckerIntegrationError(diagnostic).diagnostic, diagnostic
        )
        named = internal.parse_config(
            {
                "schemas": [
                    {
                        "name": "default",
                        "schema": schema.as_posix(),
                        "schema_output": "graphql_desk_generated/default.pyi",
                    },
                    {
                        "name": "other",
                        "schema": schema.as_posix(),
                        "schema_output": "graphql_desk_generated/other.pyi",
                    },
                ]
            },
            config_path=first,
        )
        self.assertEqual(
            internal.get_schema_names_from_config(named), {"default", "other"}
        )
        self.assertIsNone(internal.get_schema_config_for_name(named, None))
        named_ref = internal.load_ref(named)
        named_ref.load()
        self.assertIsNone(named_ref.current)
        self.assertEqual(set(named_ref.multi), {"default", "other"})

    def test_generated_subpath_declarations_work_with_both_checkers(self) -> None:
        source = cast(
            dict[str, object],
            cast(
                object,
                introspection_from_schema(
                    build_schema(
                        "enum State { NEW OLD } input Input { count: Int! } "
                        "type Query { value(input: Input): State }"
                    )
                ),
            ),
        )
        for suffix in (".py", ".pyi"):
            (self.root / ("generated" + suffix)).write_text(
                internal.output_introspection_file(source, file_type=suffix),
                encoding="utf-8",
            )
        (self.root / "consumer.py").write_text(
            "from generated import State, Input, introspection, schema_map\n"
            "from graphql_desk.internal import SchemaMap, IntrospectionResult\n"
            "valid: State = 'NEW'\n"
            "inputs: Input = {'count': 1}\n"
            "schema: SchemaMap = schema_map\n"
            "query: IntrospectionResult = introspection\n"
            "wrong: State = 'INVALID'\n"
            "bad_inputs: Input = {'count': 'invalid'}\n",
            encoding="utf-8",
        )
        (self.root / "pyrightconfig.json").write_text(
            json.dumps(
                {
                    "include": ["consumer.py"],
                    "typeCheckingMode": "strict",
                    "pythonVersion": "3.10",
                }
            ),
            encoding="utf-8",
        )
        environment = os.environ.copy()
        environment["PYRIGHT_PYTHON_CACHE_DIR"] = str(self.root / "pyright-cache")
        mypy = subprocess.run(
            [
                sys.executable,
                "-m",
                "mypy",
                "--strict",
                "--cache-dir",
                str(self.root / "mypy-cache"),
                "consumer.py",
            ],
            cwd=self.root,
            env=environment,
            capture_output=True,
            text=True,
        )
        self.assertEqual(mypy.returncode, 1, mypy.stdout + mypy.stderr)
        self.assertIn("consumer.py:7: error:", mypy.stdout)
        self.assertIn("consumer.py:8: error:", mypy.stdout)
        self.assertIn("Found 2 errors", mypy.stdout)
        pyright = subprocess.run(
            [
                sys.executable,
                "-m",
                "pyright",
                "--project",
                str(self.root),
                "--pythonpath",
                sys.executable,
                "--outputjson",
            ],
            cwd=self.root,
            env=environment,
            capture_output=True,
            text=True,
        )
        self.assertEqual(pyright.returncode, 1, pyright.stdout + pyright.stderr)
        report = json.loads(pyright.stdout)
        self.assertEqual(report["summary"]["errorCount"], 2, pyright.stdout)
        self.assertEqual(
            [value["range"]["start"]["line"] for value in report["generalDiagnostics"]],
            [6, 7],
        )

    def test_introspection_minification_schema_map_and_deterministic_python_pair(
        self,
    ) -> None:
        schema = build_schema("""
            "Description" enum State { OLD @deprecated(reason: "legacy") NEW }
            input Choice @oneOf { id: ID label: String }
            input Input { count: Int! = 0 choices: [[Choice!]] }
            interface Node { id: ID! }
            type Book implements Node { id: ID! state: State }
            union Result = Book
            type Query { search(input: Input): [Result!]! old: String @deprecated }
        """)
        source = cast(
            dict[str, object], cast(object, introspection_from_schema(schema))
        )
        original = json.dumps(source, sort_keys=True)
        compact = internal.minify_introspection(source)
        self.assertEqual(json.dumps(source, sort_keys=True), original)
        text = json.dumps(compact)
        self.assertNotIn('"description"', text)
        self.assertNotIn('"__Type"', text)
        self.assertIn('"isDeprecated": true', text)
        self.assertIn('"isOneOf": true', text)
        self.assertIn('"defaultValue": "0"', text)
        self.assertIn('"kind": "NON_NULL"', text)
        self.assertIn('"kind": "LIST"', text)
        lookup = internal.preprocess_introspection(source)
        self.assertEqual(lookup["query"], "Query")
        self.assertEqual(lookup["types"]["Result"]["possibleTypes"], ("Book",))
        self.assertEqual(lookup["types"]["Result"]["fields"], {})
        header = "# Project licence\n# Keep this comment\n"
        runtime = internal.output_introspection_file(
            source, file_type=".py", preamble=header
        )
        stub = internal.output_introspection_file(source, preamble=header)
        self.assertEqual(internal.extract_introspection_header(runtime), header)
        self.assertEqual(
            internal.output_introspection_file(
                source,
                file_type=".py",
                preamble=internal.extract_introspection_header(runtime),
            ),
            runtime,
        )
        self.assertIn("schema_map: SchemaMap", stub)
        self.assertIn("State = Literal[", stub)
        namespace: dict[str, object] = {}
        exec(compile(runtime, "generated.py", "exec"), namespace)
        self.assertEqual(namespace["schema_map"], lookup)
        self.assertEqual(namespace["introspection"], source)
        self.assertNotIn(
            "schema_map:",
            internal.output_introspection_file(source, should_preprocess=False),
        )
        self.assertIsNone(
            internal.extract_introspection_header(
                internal.output_introspection_file(source)
            )
        )
        self.assertIn("__schema", internal.IntrospectionResult.__required_keys__)

    def test_invalid_introspection_and_header_never_become_generated_code(self) -> None:
        source = cast(
            dict[str, object],
            cast(
                object,
                introspection_from_schema(build_schema("type Query { value: Int }")),
            ),
        )
        for header in (
            "import os\n",
            "# coding: unicode_escape\n",
            "# custom\0header\n",
        ):
            with self.assertRaises(ValueError):
                internal.output_introspection_file(source, preamble=header)
        with self.assertRaises(ValueError):
            internal.output_introspection_file(source, file_type=".ts")
        with self.assertRaises(ValueError):
            internal.minify_introspection({"errors": [{"message": "PRIVATE_MARKER"}]})
        with self.assertRaises(internal.GraphQLDeskError):
            internal.load_ref(
                internal.MultiSchemaInput(
                    (
                        internal.SchemaInput(
                            name="same",
                            origin=internal.SchemaOrigin(local_path=self.root),
                        ),
                    )
                    * 2
                )
            )
        self.assertEqual(importlib.import_module("graphql_desk.internal"), internal)
        self.assertIsNone(internal.resolve_project_root(self.root))
        self.assertFalse(os.path.exists(self.root / ".graphql-desk"))
