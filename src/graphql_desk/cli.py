"""Explicit read-only diagnostics; importing this module performs no work."""

import argparse
import os
import sys
from collections.abc import Mapping, Sequence
from importlib import metadata
from pathlib import Path
from typing import NoReturn

from .project import effective_project, select_schema, validate_project_ownership
from .schema_loading import load_local_schema
from .schema_types import render_schema_types
from .schema_url import load_from_url


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        # argparse otherwise repeats arbitrary argument values, including secrets.
        raise ValueError("Invalid arguments; use --help.")


def _version(distribution: str) -> str:
    try:
        return metadata.version(distribution)
    except (metadata.PackageNotFoundError, OSError, ValueError):
        return "unavailable (distribution metadata not found)"


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="graphql-desk", description="GraphQL Desk diagnostics")
    parser.add_argument(
        "--version", action="version", version="%(prog)s " + _version("graphql-desk")
    )
    parser.add_argument(
        "-c",
        "--config",
        default=argparse.SUPPRESS,
        help="Select a pyproject.toml explicitly",
    )
    parser.add_argument(
        "--schema", default=argparse.SUPPRESS, help="Select one configured schema"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    doctor = commands.add_parser(
        "doctor", help="Report project readiness without writes"
    )
    doctor.add_argument(
        "-c",
        "--config",
        default=argparse.SUPPRESS,
        help="Select a pyproject.toml explicitly",
    )
    doctor.add_argument(
        "--schema", default=argparse.SUPPRESS, help="Select one configured schema"
    )
    return parser


def _doctor(
    config_path: Path | None,
    schema_name: str | None,
    *,
    environment: Mapping[str, str],
) -> int:
    print("GraphQL Desk doctor", file=sys.stderr)
    try:
        selected = effective_project(Path.cwd(), explicit_config=config_path)
        if selected is None:
            print(
                "configuration: unavailable; configure [tool.graphql-desk] "
                "or select --config PATH",
                file=sys.stderr,
            )
            return 1
        config = selected.config
        validate_project_ownership((config,))
        schemas = (
            (select_schema(config, binding=schema_name),)
            if schema_name is not None
            else config.schemas
        )
    except (OSError, ValueError, RuntimeError):
        print(
            "configuration: unavailable; check the selected project configuration "
            "and schema name",
            file=sys.stderr,
        )
        return 1
    print(f"configuration: ready ({selected.origin})", file=sys.stderr)
    for schema in schemas:
        try:
            if schema.origin.local_path is not None:
                loaded = load_local_schema(schema.origin.local_path)
            else:
                loaded = load_from_url(schema.origin, environment=environment)
        except (OSError, ValueError, RuntimeError):
            print(
                f"schema {schema.name}: unavailable; check schema contents, "
                "access and configured header environment",
                file=sys.stderr,
            )
        else:
            print(f"schema {schema.name}: ready", file=sys.stderr)
            for name, direction, symbol in (
                *((item.name, "scalar input", item.input) for item in schema.scalars),
                *((item.name, "scalar output", item.output) for item in schema.scalars),
                *((name, "enum", symbol) for name, symbol in schema.enums),
            ):
                if symbol == "typing.Any":
                    # Configuration has already restricted names to GraphQL identifiers.
                    print(
                        f"schema {schema.name}: explicit Any {direction} "
                        f"mapping {name}",
                        file=sys.stderr,
                    )
            try:
                render_schema_types(loaded, scalars=schema.scalars, enums=schema.enums)
            except (ValueError, TypeError, RecursionError):
                print(
                    f"schema {schema.name}: mapping declarations unavailable; "
                    "check scalar and enum mappings and schema declarations",
                    file=sys.stderr,
                )
            else:
                print(
                    f"schema {schema.name}: mapping declarations ready; "
                    "symbol compatibility remains unverified",
                    file=sys.stderr,
                )
    print(
        "interpreter: Python " + ".".join(map(str, sys.version_info[:3])),
        file=sys.stderr,
    )
    for checker in ("mypy", "pyright"):
        print(f"installed {checker} distribution: {_version(checker)}", file=sys.stderr)
    if any(schema.scalars or schema.enums for schema in schemas):
        print(
            "mapping symbols: unverified; source capture is not implemented",
            file=sys.stderr,
        )
    else:
        print("mapping symbols: not configured", file=sys.stderr)
    print(
        "generated ownership: unavailable; no verified ownership manifest",
        file=sys.stderr,
    )
    print(
        "active editor: unavailable; no authoritative editor session", file=sys.stderr
    )
    print("live bridge: unavailable; no verified bridge health", file=sys.stderr)
    print(
        "status: degraded; editor/bridge and generated ownership remain unverified",
        file=sys.stderr,
    )
    return 1


def main(argv: Sequence[str] | None = None) -> int:
    """Return an exit code; only the module/console entrypoint terminates a process.

    Doctor is deliberately degraded until editor, bridge and owned-generation
    health have authoritative inputs. Schema loading is explicit, never discovery.
    """
    try:
        arguments = _parser().parse_args(argv)
    except ValueError:
        print("Invalid arguments; use --help.", file=sys.stderr)
        return 2
    except SystemExit as error:
        return error.code if isinstance(error.code, int) else 2
    try:
        return _doctor(
            Path(arguments.config) if hasattr(arguments, "config") else None,
            arguments.schema if hasattr(arguments, "schema") else None,
            environment=dict(os.environ),
        )
    except KeyboardInterrupt:
        print("Doctor cancelled.", file=sys.stderr)
        return 130
