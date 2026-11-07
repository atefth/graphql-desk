"""Create the deterministic PERF-01 inputs without running a benchmark.

The manifest is written last. An incomplete fixture has no complete manifest.
Choose a new destination to retry an interrupted write.
"""

import argparse
import hashlib
import json
import random
from pathlib import Path

_REPOSITORY = Path(__file__).resolve().parents[1]
_PACKAGE = "graphql_desk_generated"
_SEED = 1
_FRAGMENT_DEPTH = 10
_PROFILES = {
    "small": (("alpha",), 100, 500),
    "large": (("alpha", "beta"), 1000, 2000),
}


def _schema(type_count: int, required_name: bool) -> tuple[str, int]:
    groups, remainder = divmod(type_count - 2, 6)
    fields = ["  user: User!"]
    name_type = "String!" if required_name else "String"
    definitions = [f"type User {{ id: ID! name: {name_type} }}"]
    for index in range(groups):
        tag = f"{index:04}"
        fields.append(
            f"  probe{tag}(filter: Filter{tag}, mode: Mode{tag}): Choice{tag}"
        )
        definitions.extend(
            (
                f"enum Mode{tag} {{ ON OFF }}",
                f"input Filter{tag} {{ enabled: Boolean = false labels: [String!] }}",
                f"interface Entity{tag} {{ id: ID! }}",
                f"type Record{tag} implements Entity{tag} "
                f"{{ id: ID! state: Mode{tag} }}",
                f"type Alternate{tag} implements Entity{tag} "
                "{ id: ID! description: String }",
                f"union Choice{tag} = Record{tag} | Alternate{tag}",
            )
        )
    for index in range(remainder):
        fields.append(f"  tail{index}: Tail{index}")
        definitions.append(f"type Tail{index} {{ id: ID! }}")
    return "\n".join(("type Query {", *fields, "}", *definitions, "")), groups


def _operation(name: str, fragment: str, groups: int, start: int, label: str) -> str:
    fields: list[str] = []
    for index in range(12):
        tag = f"{(start + index * 17) % groups:04}"
        fields.append(
            f"  sample{index:02}: probe{tag}("
            "filter: {enabled: $flag, labels: $labels}, "
            f"mode: ON) {{ __typename ... on Record{tag} {{ id state }} "
            f"... on Alternate{tag} {{ id description }} }}"
        )
    return "\n".join(
        (
            f"query {name}($flag: Boolean! = true, "
            f'$labels: [String!] = ["{label}"]) {{',
            f"  user {{ ...{fragment} id name }}",
            *fields,
            "}",
        )
    )


def build_fixture(profile: str) -> tuple[dict[str, bytes], dict[str, object]]:
    """Return project-relative input files and a manifest of their exact bytes."""
    schemas, operations, type_count = _PROFILES[profile]
    randomizer = random.Random(_SEED)
    files: dict[str, bytes] = {}
    config = ["[tool.graphql-desk]", f'generated_package="{_PACKAGE}"']
    operation_sizes: list[int] = []
    variants = {"lf_ascii": 0, "lf_unicode": 0, "crlf_ascii": 0, "crlf_unicode": 0}
    fragments = operations // 2
    for schema in schemas:
        sdl, groups = _schema(type_count, required_name=schema == "beta")
        files[f"{schema}.graphql"] = sdl.encode("utf-8")
        config.extend(
            (
                "",
                "[[tool.graphql-desk.schemas]]",
                f'name="{schema}"',
                f'schema="{schema}.graphql"',
                f'schema_output="{_PACKAGE}/{schema}.pyi"',
            )
        )
        # random() retains its seeded sequence across Python versions.
        shift = int(randomizer.random() * groups)
        prefix = schema.capitalize()
        for index in range(operations):
            module = f"call_{schema}_{index:04}"
            fragment = f"F{prefix}{index % fragments:04}"
            lines = [f"from {_PACKAGE}.{schema} import graphql"]
            if index < fragments:
                child = ""
                registry = ""
                if (index + 1) % _FRAGMENT_DEPTH:
                    lines.append(
                        f"from call_{schema}_{index + 1:04} import card as child_card"
                    )
                    child = f" ...F{prefix}{index + 1:04}"
                    registry = ", (child_card,)"
                body = f"fragment {fragment} on User {{ id{child} }}"
                lines.append(f"card = graphql({body!r}{registry})")
            else:
                lines.append(f"from call_{schema}_{index % fragments:04} import card")
            unicode = bool(index % 2)
            crlf = bool(index % 4 >= 2)
            operation = _operation(
                f"Q{prefix}{index:04}",
                fragment,
                groups,
                index + shift,
                "café 🐍" if unicode else "seed",
            )
            operation_sizes.append(len(operation.encode("utf-8")))
            newline = "\r\n" if crlf else "\n"
            variant = ("crlf" if crlf else "lf") + ("_unicode" if unicode else "_ascii")
            variants[variant] += 1
            lines.extend((f"query = graphql('''{operation}''', (card,))", ""))
            files[f"{module}.py"] = (
                "\n".join(lines).replace("\n", newline).encode("utf-8")
            )
    files["pyproject.toml"] = ("\n".join(config) + "\n").encode("utf-8")
    manifest: dict[str, object] = {
        "profile": profile,
        "seed": _SEED,
        "python_modules": len(operation_sizes),
        "operations": len(operation_sizes),
        "fragments": len(schemas) * fragments,
        "max_fragment_chain_definitions": _FRAGMENT_DEPTH,
        "operation_utf8_bytes": {
            "min": min(operation_sizes),
            "max": max(operation_sizes),
            "total": sum(operation_sizes),
        },
        "schemas": {
            name: {
                "application_types": type_count,
                "operations": operations,
                "fragments": fragments,
            }
            for name in schemas
        },
        "python_source_variants": variants,
        "sha256": {
            name: hashlib.sha256(data).hexdigest() for name, data in files.items()
        },
    }
    return files, manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("profile", choices=_PROFILES)
    parser.add_argument(
        "destination", type=Path, help="new directory inside the repository"
    )
    args = parser.parse_args()
    destination = args.destination.resolve()
    if destination == _REPOSITORY or not destination.is_relative_to(_REPOSITORY):
        parser.error("destination must be inside the repository")
    files, manifest = build_fixture(args.profile)
    try:
        destination.mkdir()
    except OSError as error:
        parser.error(str(error))
    for name, data in files.items():
        with (destination / name).open("xb") as stream:
            stream.write(data)
    with (destination / "manifest.json").open(
        "x", encoding="utf-8", newline="\n"
    ) as stream:
        json.dump(manifest, stream, indent=2, sort_keys=True)
        stream.write("\n")


if __name__ == "__main__":
    main()
