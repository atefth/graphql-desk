"""Advanced Python utilities; no stability promise or work performed on import."""

from __future__ import annotations

import math
import os
import re
import threading
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TypeAlias, cast

from graphql import DocumentNode, GraphQLSchema, introspection_from_schema, parse

from .config import (
    ProjectConfig,
    ScalarMapping,
    SchemaConfig,
    SchemaOrigin,
    parse_project_config,
    parse_project_settings,
)
from .fragment_refs import FragmentDocument, FragmentIdentity, FragmentRef
from .internal_introspection import (
    IntrospectionResult,
    SchemaMap,
    extract_introspection_header,
    minify_introspection,
    output_introspection_file,
    preprocess_introspection,
)
from .project import (
    EffectiveProject,
    effective_project,
    find_project,
    select_schema,
    validate_project_ownership,
)
from .schema_loading import load_local_schema
from .schema_url import get_url_config
from .schema_url import load_from_url as _load_url

__all__ = [
    "BaseLoadConfig",
    "LoadConfig",
    "LoadConfigResult",
    "SchemaInput",
    "SingleSchemaInput",
    "MultiSchemaInput",
    "SchemaLoaderResult",
    "SchemaLoader",
    "SchemaRef",
    "OnSchemaUpdate",
    "OnSchemaError",
    "GraphQLDeskError",
    "CheckerDiagnostic",
    "CheckerIntegrationError",
    "ProjectConfig",
    "SchemaConfig",
    "SchemaOrigin",
    "ScalarMapping",
    "IntrospectionResult",
    "SchemaMap",
    "FragmentIdentity",
    "FragmentRef",
    "FragmentDocument",
    "DocumentNode",
    "parse",
    "load_from_sdl",
    "load_from_url",
    "get_url_config",
    "load",
    "load_ref",
    "parse_config",
    "validate_unique_output_locations",
    "get_schema_names_from_config",
    "get_schema_config_for_name",
    "load_config",
    "load_configs",
    "find_config_file",
    "read_config_file",
    "resolve_project_root",
    "minify_introspection",
    "preprocess_introspection",
    "output_introspection_file",
    "extract_introspection_header",
]


class GraphQLDeskError(ValueError):
    """An actionable advanced-API failure without captured credentials or source."""


class _LoadFailure(GraphQLDeskError):
    def __init__(self, message: str, token: object) -> None:
        self.token = token
        super().__init__(message)


@dataclass(frozen=True)
class CheckerDiagnostic:
    message: str
    path: Path | None = None
    code: str | int | None = None


class CheckerIntegrationError(GraphQLDeskError):
    def __init__(self, diagnostic: CheckerDiagnostic) -> None:
        self.diagnostic = diagnostic
        super().__init__(diagnostic.message)


@dataclass(frozen=True, kw_only=True)
class BaseLoadConfig:
    root_path: Path | None = None
    # Seconds: None retains upstream's 60-second URL default; zero disables it.
    fetch_interval: float | None = None
    assume_valid: bool = False

    def __post_init__(self) -> None:
        if type(self.assume_valid) is not bool:
            raise GraphQLDeskError("assume_valid must be a boolean")


@dataclass(frozen=True, kw_only=True)
class LoadConfig(BaseLoadConfig):
    origin: SchemaOrigin
    name: str | None = None


@dataclass(frozen=True, kw_only=True)
class SchemaInput:
    origin: SchemaOrigin
    name: str | None = None
    schema_output: Path | None = None
    persisted_output: Path | None = None
    cache_output: Path | None = None


SingleSchemaInput: TypeAlias = SchemaInput


@dataclass(frozen=True)
class MultiSchemaInput:
    schemas: tuple[SchemaInput, ...]


@dataclass(frozen=True)
class SchemaLoaderResult:
    schema: GraphQLSchema
    introspection: IntrospectionResult = field(repr=False)
    schema_output: Path | None = None
    persisted_output: Path | None = None
    cache_output: Path | None = None


OnSchemaUpdate: TypeAlias = Callable[[SchemaLoaderResult], None]
OnSchemaError: TypeAlias = Callable[[GraphQLDeskError], None]
LoadConfigResult: TypeAlias = EffectiveProject
_Stamp: TypeAlias = tuple[int, int, int, int, int] | tuple[str, int | None]
_callback_state = threading.local()


@dataclass
class _Subscription:
    updated: OnSchemaUpdate
    failed: OnSchemaError | None
    active: bool = True
    lock: threading.RLock = field(default_factory=threading.RLock)


class SchemaLoader:
    """Lazy cached loads, with background updates only after explicit subscription."""

    def __init__(
        self,
        read: Callable[[], GraphQLSchema],
        *,
        name: str | None,
        local_path: Path | None = None,
        interval: float,
    ) -> None:
        if name is not None and not re.fullmatch(r"[_A-Za-z][_0-9A-Za-z]*", name):
            raise GraphQLDeskError("Invalid schema name")
        if isinstance(interval, bool) or not math.isfinite(interval) or interval < 0:
            raise GraphQLDeskError(
                "Schema refresh interval must be finite and nonnegative"
            )
        self.name = name
        self._read = read
        self._path = local_path
        self._interval = interval
        self._lock = threading.RLock()
        self._load_lock = threading.Lock()
        self._result: SchemaLoaderResult | None = None
        self._token = object()
        self._stamp: _Stamp | None = None
        self._subscribers: dict[int, _Subscription] = {}
        self._next_subscription = 0
        self._stop: threading.Event | None = None

    def _file_stamp(self) -> _Stamp:
        assert self._path is not None
        try:
            value = self._path.stat()
            return (
                value.st_dev,
                value.st_ino,
                value.st_size,
                value.st_mtime_ns,
                value.st_ctime_ns,
            )
        except OSError as error:
            return "error", error.errno

    def load(self, reload: bool = False) -> SchemaLoaderResult:
        """Return cached data, or explicitly retry the backend after any failed load."""
        with self._load_lock:
            with self._lock:
                if not reload and self._result is not None:
                    return self._result
                self._result = None
                token = self._token = object()
            before = self._file_stamp() if self._path is not None else None
            try:
                schema = self._read()
                data = cast(
                    IntrospectionResult, cast(object, introspection_from_schema(schema))
                )
                if self.name is not None:
                    data["name"] = self.name
                result = SchemaLoaderResult(schema, data)
            except (OSError, ValueError, TypeError, RuntimeError, RecursionError):
                raise _LoadFailure(
                    "Schema unavailable; check contents, access and header environment",
                    token,
                ) from None
            finally:
                if self._path is not None:
                    with self._lock:
                        after = self._file_stamp()
                        self._stamp = after if after == before else None
            if self._path is not None and self._stamp is None:
                raise _LoadFailure("Schema changed during load; retry", token)
            with self._lock:
                self._result = result
            return result

    def load_schema(self) -> GraphQLSchema:
        return self.load().schema

    def load_introspection(self) -> IntrospectionResult:
        return self.load().introspection

    def _notify(self, result: SchemaLoaderResult | GraphQLDeskError) -> None:
        with self._lock:
            subscribers = tuple(self._subscribers.values())
        for subscriber in subscribers:
            # Callbacks may reload other schemas; never hold a loader lock here.
            with subscriber.lock:
                with self._lock:
                    current = self._result
                    active = subscriber.active
                stale = (
                    current is not None
                    if isinstance(result, GraphQLDeskError)
                    else current is not result
                )
                if not active or stale:
                    continue
                callback = (
                    subscriber.failed
                    if isinstance(result, GraphQLDeskError)
                    else subscriber.updated
                )
                if callback is not None:
                    nested = bool(getattr(_callback_state, "active", False))
                    _callback_state.active = True
                    try:
                        if isinstance(result, GraphQLDeskError):
                            cast(OnSchemaError, callback)(result)
                        else:
                            cast(OnSchemaUpdate, callback)(result)
                    except Exception:
                        if subscriber.failed is not None and not isinstance(
                            result, GraphQLDeskError
                        ):
                            try:
                                subscriber.failed(
                                    GraphQLDeskError("Schema update callback failed")
                                )
                            except Exception:
                                pass
                    finally:
                        _callback_state.active = nested

    def _watch(self, stop: threading.Event) -> None:
        while not stop.wait(self._interval):
            if self._path is not None:
                stamp = self._file_stamp()
                with self._lock:
                    if stamp == self._stamp:
                        continue
                    self._stamp = stamp
            try:
                value: SchemaLoaderResult | GraphQLDeskError = self.load(reload=True)
            except GraphQLDeskError as error:
                value = error
            if not stop.is_set():
                self._notify(value)

    def notify_on_update(
        self, on_update: OnSchemaUpdate, on_error: OnSchemaError | None = None
    ) -> Callable[[], None]:
        with self._lock:
            token = self._next_subscription
            self._next_subscription += 1
            subscriber = _Subscription(on_update, on_error)
            self._subscribers[token] = subscriber
            if self._stop is None and self._interval > 0:
                if self._path is not None and self._stamp is None:
                    self._stamp = self._file_stamp()
                stop = self._stop = threading.Event()
                try:
                    threading.Thread(
                        target=self._watch, args=(stop,), daemon=True
                    ).start()
                except RuntimeError:
                    self._subscribers.pop(token)
                    self._stop = None
                    raise GraphQLDeskError("Cannot start schema updates") from None

        def unsubscribe() -> None:
            with self._lock:
                self._subscribers.pop(token, None)
                subscriber.active = False
                if not self._subscribers and self._stop is not None:
                    self._stop.set()
                    self._stop = None
            # Waiting from another callback can deadlock two concurrent teardowns.
            if not getattr(_callback_state, "active", False):
                with subscriber.lock:
                    pass

        return unsubscribe


def load_from_sdl(file: Path | str, *, name: str | None = None) -> SchemaLoader:
    """Construct a lazy file loader (SDL or introspection JSON), not an SDL parser."""
    path = Path(file)
    # Upstream's local watcher has a 250ms fallback; Python uses stdlib polling.
    return SchemaLoader(
        lambda: load_local_schema(path), name=name, local_path=path, interval=0.25
    )


def load_from_url(
    origin: SchemaOrigin,
    *,
    name: str | None = None,
    fetch_interval: float = 60,
    environment: Mapping[str, str] | None = None,
) -> SchemaLoader:
    if get_url_config(origin) is None:
        raise GraphQLDeskError("A valid HTTP(S) schema origin is required")
    captured = dict(environment or {})
    return SchemaLoader(
        lambda: _load_url(origin, environment=captured),
        name=name,
        interval=fetch_interval,
    )


def load(
    config: LoadConfig, *, environment: Mapping[str, str] | None = None
) -> SchemaLoader:
    if get_url_config(config.origin) is not None:
        return load_from_url(
            config.origin,
            name=config.name,
            fetch_interval=60
            if config.fetch_interval is None
            else config.fetch_interval,
            environment=environment,
        )
    if (
        config.origin.url is not None
        or config.origin.local_path is None
        or config.origin.headers
    ):
        raise GraphQLDeskError("A schema must have one valid file or HTTP(S) origin")
    path = config.origin.local_path
    if config.root_path is not None:
        path = config.root_path / path
    return load_from_sdl(path, name=config.name)


class SchemaRef:
    """Single/named schema state; callers own their update subscriptions."""

    def __init__(
        self,
        inputs: tuple[SchemaInput, ...],
        *,
        config: BaseLoadConfig,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        names = [item.name for item in inputs]
        if len(set(names)) != len(names):
            raise GraphQLDeskError("Schema reference names must be unique")
        self.current: SchemaLoaderResult | None = None
        self.multi: dict[str, SchemaLoaderResult | None] = {
            name: None for name in names if name is not None
        }
        self.version = 0
        self._lock = threading.RLock()
        self._inputs = inputs
        self._config = config
        self._environment = dict(environment or {})
        self._loaders: tuple[SchemaLoader, ...] | None = None

    def _get_loaders(self, config: BaseLoadConfig | None) -> tuple[SchemaLoader, ...]:
        with self._lock:
            selected = self._config if config is None else config
            if self._loaders is not None and selected != self._config:
                raise GraphQLDeskError(
                    "Schema reference load settings changed; create a new reference"
                )
            self._config = selected
            if self._loaders is None:
                self._loaders = tuple(
                    load(
                        LoadConfig(
                            origin=item.origin,
                            name=item.name,
                            root_path=selected.root_path,
                            fetch_interval=selected.fetch_interval,
                            assume_valid=selected.assume_valid,
                        ),
                        environment=self._environment,
                    )
                    for item in self._inputs
                )
            return self._loaders

    def _accept(
        self,
        item: SchemaInput,
        value: SchemaLoaderResult | GraphQLDeskError,
        loader: SchemaLoader,
    ) -> bool:
        # A caller can pause after load returns; only commit its current observation.
        with loader._lock:  # pyright: ignore[reportPrivateUsage]
            if isinstance(value, GraphQLDeskError):
                if (
                    not isinstance(value, _LoadFailure)
                    or value.token is not loader._token  # pyright: ignore[reportPrivateUsage]
                ):
                    return False
                result = None
            else:
                if value is not loader._result:  # pyright: ignore[reportPrivateUsage]
                    return False
                result = self._result(item, value)
            with self._lock:
                if item.name is None:
                    self.current = result
                else:
                    self.multi[item.name] = result
                self.version += 1
            return True

    @staticmethod
    def _result(item: SchemaInput, value: SchemaLoaderResult) -> SchemaLoaderResult:
        return replace(
            value,
            schema_output=item.schema_output,
            persisted_output=item.persisted_output,
            cache_output=item.cache_output,
        )

    def load(
        self, config: BaseLoadConfig | None = None, *, reload: bool = False
    ) -> SchemaRef:
        failure: GraphQLDeskError | None = None
        for item, loader in zip(self._inputs, self._get_loaders(config)):
            try:
                self._accept(item, loader.load(reload), loader)
            except GraphQLDeskError as error:
                self._accept(item, error, loader)
                failure = error
        if failure is not None:
            raise failure
        return self

    def autoupdate(
        self,
        on_update: Callable[[SchemaRef, SchemaInput], None],
        on_error: Callable[[GraphQLDeskError, SchemaInput], None] | None = None,
        *,
        config: BaseLoadConfig | None = None,
    ) -> Callable[[], None]:
        stops: list[Callable[[], None]] = []
        try:
            for item, loader in zip(self._inputs, self._get_loaders(config)):

                def updated(
                    value: SchemaLoaderResult,
                    item: SchemaInput = item,
                    loader: SchemaLoader = loader,
                ) -> None:
                    if self._accept(item, value, loader):
                        on_update(self, item)

                def failed(
                    error: GraphQLDeskError,
                    item: SchemaInput = item,
                    loader: SchemaLoader = loader,
                ) -> None:
                    if (
                        not isinstance(error, _LoadFailure)
                        or self._accept(item, error, loader)
                    ) and on_error is not None:
                        on_error(error, item)

                try:
                    self._accept(item, loader.load(), loader)
                except GraphQLDeskError as error:
                    failed(error)
                stops.append(loader.notify_on_update(updated, failed))
        except BaseException:
            for stop in stops:
                stop()
            raise

        def unsubscribe() -> None:
            for stop in stops:
                stop()

        return unsubscribe


def load_ref(
    input: SchemaInput | MultiSchemaInput | SchemaConfig | ProjectConfig,
    *,
    environment: Mapping[str, str] | None = None,
) -> SchemaRef:
    config = BaseLoadConfig()
    if isinstance(input, ProjectConfig):
        config = BaseLoadConfig(
            root_path=input.root, fetch_interval=input.schema_refresh_interval
        )
        schemas = input.schemas
    elif isinstance(input, SchemaConfig):
        schemas = (input,)
    else:
        schemas = ()
    if schemas:
        inputs = tuple(
            SchemaInput(
                origin=item.origin,
                name=None
                if len(schemas) == 1 and item.name == "default"
                else item.name,
                schema_output=item.schema_output,
                persisted_output=item.persisted_output,
                cache_output=item.cache_output,
            )
            for item in schemas
        )
    else:
        inputs = (
            input.schemas
            if isinstance(input, MultiSchemaInput)
            else (cast(SchemaInput, input),)
        )
    return SchemaRef(inputs, config=config, environment=environment)


def parse_config(
    settings: Mapping[str, object] | str, *, config_path: Path
) -> ProjectConfig:
    try:
        result = (
            parse_project_config(settings, config_path=config_path)
            if isinstance(settings, str)
            else parse_project_settings(settings, config_path=config_path)
        )
        if result is None:
            raise ValueError("No active project")
        return result
    except (OSError, ValueError, RuntimeError):
        raise GraphQLDeskError("Invalid GraphQL Desk project configuration") from None


def validate_unique_output_locations(projects: Iterable[ProjectConfig]) -> None:
    validate_project_ownership(projects)


def get_schema_names_from_config(config: ProjectConfig) -> set[str | None]:
    return {
        None if len(config.schemas) == 1 and item.name == "default" else item.name
        for item in config.schemas
    }


def get_schema_config_for_name(
    config: ProjectConfig, name: str | None
) -> SchemaConfig | None:
    try:
        return select_schema(config, binding=name)
    except ValueError:
        return None


def read_config_file(path: Path | str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        raise GraphQLDeskError("Cannot read UTF-8 project configuration") from None


def find_config_file(target: Path | str | None = None) -> Path | None:
    path = Path.cwd() if target is None else Path(target)
    try:
        config = find_project(path.parent if path.name == "pyproject.toml" else path)
    except (OSError, UnicodeError, ValueError, RuntimeError):
        raise GraphQLDeskError("Cannot discover project configuration") from None
    return config.path if config is not None else None


def load_config(target: Path | str | None = None) -> LoadConfigResult:
    path = Path.cwd() if target is None else Path(target)
    try:
        selected = (
            effective_project(path.parent, explicit_config=Path(path.name))
            if path.name == "pyproject.toml"
            else effective_project(path)
        )
        if selected is None:
            raise ValueError("No active project")
        return selected
    except (OSError, UnicodeError, ValueError, RuntimeError):
        raise GraphQLDeskError(
            "Cannot load project configuration; select a configured project"
        ) from None


def load_configs(
    targets: Path | str | Iterable[Path | str] | None = None,
) -> tuple[LoadConfigResult, ...]:
    results: dict[str, LoadConfigResult] = {}
    selected: Iterable[Path | str | None]
    selected = (
        (targets,) if targets is None or isinstance(targets, (Path, str)) else targets
    )
    for target in selected:
        result = load_config(target)
        results[os.path.normcase(str(result.config.path.resolve()))] = result
    validate_unique_output_locations(result.config for result in results.values())
    return tuple(results.values())


def resolve_project_root(target: Path | str | None = None) -> Path | None:
    try:
        return load_config(target).config.root
    except GraphQLDeskError:
        return None
