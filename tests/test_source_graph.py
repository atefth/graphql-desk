import sys
import unittest
from dataclasses import FrozenInstanceError, replace
from unittest.mock import patch

import graphql_desk.source_graph as source_graph
from graphql_desk.source import extract_calls
from graphql_desk.source_graph import (
    DocumentIdentity,
    FactoryBinding,
    IncrementalResolver,
    ModuleSnapshot,
    ProjectBindings,
    ProjectSources,
    resolve_parse_sources,
    resolve_project,
    resolve_type_symbols,
)


def module(
    name: str, source: str, *, project: str = "app", package: bool = False
) -> ModuleSnapshot:
    return ModuleSnapshot(
        project, name, f"file:///{project}/{name}.py", 1, source, package
    )


class SourceGraphTests(unittest.TestCase):
    @unittest.skipUnless(sys.version_info >= (3, 12), "PEP 695 requires Python3.12")
    def test_captured_non_generic_pep695_type_aliases(self) -> None:
        models = module(
            "models",
            "class User: pass\n"
            "type UserAlias = User\n"
            "type ChainedAlias = UserAlias\n"
            "type MaybeUser = User | None\n"
            "type Text = str\n"
            "type Generic[T] = list[T]\n"
            "type MissingAlias = Missing\n",
        )
        exports = module("exports", "from models import UserAlias as Public")

        resolutions = resolve_type_symbols(
            (
                "models.UserAlias",
                "models.ChainedAlias",
                "models.MaybeUser",
                "models.Text",
                "models.Generic",
                "models.MissingAlias",
                "exports.Public",
            ),
            modules=(models, exports),
            project_id="app",
        )

        self.assertEqual(
            [(item.status, item.target) for item in resolutions],
            [
                ("resolved", "models.User"),
                ("resolved", "models.User"),
                ("unresolved", None),
                ("resolved", "models.MaybeUser"),
                ("unresolved", None),
                ("resolved", "builtins.str"),
                ("resolved", "models.User"),
            ],
        )

    def test_flat_union_aliases_keep_availability_but_no_base_capabilities(
        self,
    ) -> None:
        base = module("base", "class Known: pass")
        models = module(
            "models",
            "from typing import TypeAlias\nimport base,typing\n"
            "from typing_extensions import TypedDict\n"
            "class Data(TypedDict):\n value:str\n"
            "Text:TypeAlias=str|int\nReverse:TypeAlias=int|str\n"
            "Nullable:TypeAlias=str|int|None\n"
            "NullableReverse:TypeAlias=None|int|str\n"
            "GroupedLeft:TypeAlias=(str|int)|None\n"
            "GroupedRight:TypeAlias=str|(int|None)\n"
            "GroupedNested:TypeAlias=((str|int)|(None|bytes))\n"
            "GroupedQualified:TypeAlias=(base.Known|str)|None\n"
            "GroupedNullableDuplicate:TypeAlias=(str|None)|None\n"
            "UnionGrouped:TypeAlias=typing.Union[(str|int),None]\n"
            "ContainerUnion:TypeAlias=list[typing.Union[(str|int),None]]\n"
            "GroupedContainerUnion:TypeAlias=list[str|(int|None)]\n"
            "GroupedContainerUnionLeft:TypeAlias=list[(str|int)|None]\n"
            "Long:TypeAlias=bytes|str|int|None\n"
            "Known:TypeAlias=base.Known|str\nAlias:TypeAlias=Text\n"
            "Duplicate:TypeAlias=Data|Data\n",
        )
        exports = module("exports", "from models import Alias as Public")
        snapshots = (base, models, exports)
        symbols = (
            "models.Text",
            "models.Reverse",
            "models.Known",
            "models.Duplicate",
            "exports.Public",
            "models.Nullable",
            "models.NullableReverse",
            "models.Long",
            "models.GroupedLeft",
            "models.GroupedRight",
            "models.GroupedNested",
            "models.GroupedQualified",
            "models.GroupedNullableDuplicate",
            "models.UnionGrouped",
            "models.ContainerUnion",
            "models.GroupedContainerUnion",
            "models.GroupedContainerUnionLeft",
        )
        result = resolve_type_symbols(symbols, modules=snapshots, project_id="app")
        self.assertEqual([r.status for r in result], ["resolved"] * len(symbols))
        self.assertEqual(
            result,
            resolve_type_symbols(
                symbols, modules=tuple(reversed(snapshots)), project_id="app"
            ),
        )
        self.assertNotEqual(
            result,
            resolve_type_symbols(
                symbols,
                modules=(replace(base, version=2), models, exports),
                project_id="app",
            ),
        )
        for declaration in (
            "from exports import Public\nclass Bad(Public):pass",
            "from models import GroupedRight\nclass Bad(GroupedRight):pass",
            "from models import Duplicate\nclass Bad(Duplicate,total=False):pass",
        ):
            self.assertEqual(
                resolve_type_symbols(
                    ("bad.Bad",),
                    modules=(*snapshots, module("bad", declaration)),
                    project_id="app",
                )[0].status,
                "unresolved",
            )
        for declaration in (
            "Alias:TypeAlias=str|Missing",
            "Alias:TypeAlias=str|Alias",
            "Alias:TypeAlias=str|TypeAlias",
            "Alias:TypeAlias=str|'int'",
            "Alias:TypeAlias=str|list[int]",
            "Alias:TypeAlias=str|(int|list[str])",
            "Alias:TypeAlias=str|(Missing|None)",
            "Alias:TypeAlias=str|(value|None)\nvalue=42",
            "value=42\nAlias:TypeAlias=str|value",
            "Alias:TypeAlias=str|int\nint=other",
        ):
            with self.subTest(declaration=declaration):
                self.assertEqual(
                    resolve_type_symbols(
                        ("bad.Alias",),
                        modules=(
                            module(
                                "bad", "from typing import TypeAlias\n" + declaration
                            ),
                        ),
                        project_id="app",
                    )[0].status,
                    "unresolved",
                )
        external = module(
            "unknown_use",
            "from typing import TypeAlias\n"
            "from external import Value\nAlias:TypeAlias=str|Value",
        )
        self.assertEqual(
            resolve_type_symbols(
                ("unknown_use.Alias",), modules=(external,), project_id="app"
            )[0].status,
            "external",
        )
        for mutation in (
            "import builtins\nbuiltins.int=42",
            "import base\nbase.Known=42",
        ):
            target = "models.Text" if "builtins" in mutation else "models.Known"
            self.assertEqual(
                resolve_type_symbols(
                    (target,),
                    modules=(*snapshots, module("mutator", mutation)),
                    project_id="app",
                )[0].status,
                "unresolved",
            )

    def test_parser_sources_reuse_imports_constants_scopes_and_provenance(self) -> None:
        snapshots = (
            module("constants", 'TEXT="query Q { id }"'),
            module("companion", "from graphql_desk import parse as parse"),
            module("shim", "from companion import parse as reader"),
            module(
                "main",
                "from shim import reader as read\n"
                "from constants import TEXT\n"
                "value=read(TEXT)\n"
                "other=read(source=TEXT,no_location=False,max_tokens=None,experimental_fragment_arguments=False)",
            ),
        )
        baseline = resolve_project(snapshots)
        with (
            patch("builtins.open", side_effect=AssertionError("No files")),
            patch("importlib.import_module", side_effect=AssertionError("No imports")),
        ):
            facts = resolve_parse_sources(snapshots)
        self.assertEqual(facts.diagnostics, ())
        self.assertEqual([call.binding for call in facts.calls], ["value", "other"])
        self.assertEqual([call.source for call in facts.calls], ["query Q { id }"] * 2)
        self.assertEqual(
            {item.module for item in facts.calls[0].dependencies},
            {"main", "shim", "companion", "constants"},
        )
        self.assertEqual(facts.calls[0].contributions[0].module.module, "constants")
        self.assertEqual(facts, resolve_parse_sources(tuple(reversed(snapshots))))
        changed = (*snapshots[:1], replace(snapshots[1], version=2), *snapshots[2:])
        self.assertNotEqual(
            facts.calls[0].dependencies,
            resolve_parse_sources(changed).calls[0].dependencies,
        )
        self.assertEqual(resolve_project(snapshots), baseline)
        self.assertEqual(IncrementalResolver().resolve(snapshots), baseline)

    def test_parser_fallback_modes_do_not_poison_document_resolution(self) -> None:
        snapshots = (
            module(
                "main",
                "from graphql_desk import parse,gql\n"
                "from graphql import Source\n"
                "text=unknown()\n"
                "a=parse(text)\n"
                "b=parse(Source('query Q { id }'))\n"
                "c=parse('query Q { id }',True)\n"
                "d=parse('query Q { id }',max_tokens=20)\n"
                "e=parse('fragment F($v:Int) on T { f(v:$v) }',"
                "experimental_fragment_arguments=True)\n"
                "f=parse(*unknown)\n"
                "g=parse('query Q { id }',other=True)\n"
                "without_locations=parse('query Q { id }',no_location=True)\n"
                "def nested():\n"
                " return parse('query Q { id }')\n"
                "def shadow(parse):\n"
                " return parse('query Q { id }')\n"
                "valid=gql('{id}')",
            ),
        )
        documents = resolve_project(snapshots)
        self.assertEqual(len(documents.calls), 1)
        self.assertEqual(documents.diagnostics, ())
        facts = resolve_parse_sources(snapshots)
        self.assertEqual(len(facts.calls), 3)
        self.assertEqual(
            [item.binding for item in facts.calls],
            ["c", "without_locations", None],
        )
        self.assertEqual(len(facts.diagnostics), 6)
        self.assertEqual({item.code for item in facts.diagnostics}, {"parse-fallback"})
        self.assertEqual(resolve_project(snapshots), documents)

    def test_parser_identity_rejects_mutation_shadowing_and_untrusted_lookalikes(
        self,
    ) -> None:
        for source in (
            "from graphql_desk import parse\nparse=other\nx=parse('{id}')",
            "import graphql_desk as api\napi.parse=other\nx=api.parse('{id}')",
            "from graphql_desk import parse\nmutate(parse)\nx=parse('{id}')",
            "import graphql_desk as api\nmutate(api.__dict__)\nx=api.parse('{id}')",
            "from graphql_desk import parse\nfrom other import *\nx=parse('{id}')",
            "from constants import TEXT\nfrom graphql_desk import parse\nx=parse(TEXT)",
        ):
            with self.subTest(source=source):
                facts = resolve_parse_sources((module("main", source),))
                self.assertEqual(facts.calls, ())
                self.assertTrue(facts.diagnostics)
        lookalikes = (
            module("graphql_desk", "def parse(source): pass"),
            module("main", "from graphql_desk import parse\nx=parse('{id}')"),
        )
        self.assertEqual(resolve_parse_sources(lookalikes).calls, ())
        self.assertEqual(
            resolve_parse_sources(
                (module("main", "from unrelated import parse\nx=parse('{id}')"),)
            ).calls,
            (),
        )
        with self.assertRaises(ValueError):
            resolve_parse_sources((module("main", ""), module("main", "")))

    def test_captured_type_symbols_follow_classes_aliases_and_reexports(self) -> None:
        snapshots = (
            module("pkg", "from .exports import Public", package=True),
            module("pkg.exports", "from .types import Alias as Public"),
            module(
                "pkg.types",
                "from typing_extensions import TypedDict\n"
                "from enum import Enum\nclass Plain: pass\nAlias=Plain\n"
                "class Child(Plain): pass\n"
                "class Payload(TypedDict,total=False):\n value:str\n"
                "class Choice(str,Enum):\n RED='red'\n"
                "from datetime import datetime as External",
            ),
        )
        names = (
            "pkg.Public",
            "pkg.types.Child",
            "pkg.types.Payload",
            "pkg.types.Choice",
            "pkg.types.External",
            "absent.Type",
        )
        original_import = __import__

        def guard(name: str, *args: object, **kwargs: object) -> object:
            if name != "collections":  # ast.walk imports deque lazily on Python 3.10.
                raise AssertionError(name)
            return original_import(name)

        with (
            patch("builtins.open", side_effect=AssertionError("No files")),
            patch("builtins.__import__", side_effect=guard),
        ):
            results = resolve_type_symbols(names, modules=snapshots, project_id="app")
        facts = {result.symbol: result for result in results}
        self.assertEqual(facts["pkg.Public"].target, "pkg.types.Plain")
        for name in names[:4]:
            self.assertEqual(facts[name].status, "resolved")
        self.assertEqual(facts["pkg.types.External"].status, "external")
        self.assertEqual(facts["pkg.types.External"].target, "datetime.datetime")
        self.assertEqual(facts["absent.Type"].status, "external")
        self.assertEqual(
            results,
            resolve_type_symbols(
                tuple(reversed(names)) + names[:1],
                modules=tuple(reversed(snapshots)),
                project_id="app",
            ),
        )
        changed = resolve_type_symbols(
            names,
            modules=(*snapshots[:2], replace(snapshots[2], version=2)),
            project_id="app",
        )
        self.assertNotEqual(results[0].dependencies, changed[0].dependencies)
        with self.assertRaises(FrozenInstanceError):
            setattr(results[0], "status", "missing")
        # Document resolution and its cached syntax retain the preexisting behavior.
        self.assertEqual(
            resolve_project(snapshots),
            source_graph.IncrementalResolver().resolve(snapshots),
        )

    def test_unique_decorator_resolves_only_trusted_enum_classes(self) -> None:
        models = module(
            "models",
            "import enum\n"
            "from enum import Enum,unique as mark_unique\n"
            "@enum.unique\n"
            "class Qualified(enum.Enum):\n ONE='one'\n TWO='two'\n"
            "@mark_unique\n"
            "class Imported(Enum):\n ONE='one'\n TWO='two'\n"
            "@enum.unique()\n"
            "class Called(enum.Enum):\n ONE='one'\n"
            "@enum.unique\n"
            "class Plain: pass\n",
        )

        results = resolve_type_symbols(
            tuple(
                f"models.{name}"
                for name in ("Qualified", "Imported", "Called", "Plain")
            ),
            modules=(models,),
            project_id="app",
        )

        self.assertEqual(
            {item.symbol: (item.status, item.target) for item in results},
            {
                "models.Qualified": ("resolved", "models.Qualified"),
                "models.Imported": ("resolved", "models.Imported"),
                "models.Called": ("unresolved", None),
                "models.Plain": ("unresolved", None),
            },
        )
        mutated = module(
            "mutated",
            "import enum\n"
            "from enum import Enum\n"
            "@enum.unique\n"
            "class Choice(Enum):\n ONE='one'\n TWO='two'\n"
            "enum.unique=replace\n",
        )
        self.assertEqual(
            resolve_type_symbols(
                ("mutated.Choice",), modules=(mutated,), project_id="app"
            )[0].status,
            "unresolved",
        )

    def test_explicit_type_aliases_reuse_captured_class_and_reexport_resolution(
        self,
    ) -> None:
        snapshots = (
            module("pkg", "from .aliases import Public", package=True),
            module(
                "pkg.aliases",
                "import typing as t\n"
                "import pkg.types as models\n"
                "Public:t.TypeAlias=models.Alias",
            ),
            module(
                "pkg.types",
                "from typing_extensions import TypeAlias as TA\n"
                "from enum import Enum\n"
                "from typing_extensions import TypedDict\n"
                "class Plain: pass\n"
                "class Payload(TypedDict):\n"
                " value:str\n"
                "class Choice(Enum):\n"
                " RED='red'\n"
                "Alias:TA=Plain\n"
                "Data:TA=Payload\n"
                "Color:TA=Choice\n"
                "Text:TA=str",
            ),
        )
        symbols = (
            "pkg.Public",
            "pkg.types.Alias",
            "pkg.types.Data",
            "pkg.types.Color",
            "pkg.types.Text",
        )
        with (
            patch("builtins.open", side_effect=AssertionError("No files")),
            patch(
                "importlib.import_module",
                side_effect=AssertionError("No project imports"),
            ),
        ):
            results = resolve_type_symbols(symbols, modules=snapshots, project_id="app")
        self.assertEqual([item.status for item in results], ["resolved"] * len(symbols))
        self.assertEqual(
            {item.symbol: item.target for item in results},
            {
                "pkg.Public": "pkg.types.Plain",
                "pkg.types.Alias": "pkg.types.Plain",
                "pkg.types.Data": "pkg.types.Payload",
                "pkg.types.Color": "pkg.types.Choice",
                "pkg.types.Text": "builtins.str",
            },
        )
        self.assertEqual(
            results,
            resolve_type_symbols(
                tuple(reversed(symbols)),
                modules=tuple(reversed(snapshots)),
                project_id="app",
            ),
        )
        self.assertNotEqual(
            results[0].dependencies,
            resolve_type_symbols(
                symbols,
                modules=(*snapshots[:2], replace(snapshots[2], version=2)),
                project_id="app",
            )[0].dependencies,
        )
        self.assertEqual(
            resolve_project(snapshots), IncrementalResolver().resolve(snapshots)
        )

    def test_explicit_type_aliases_reject_untrusted_markers_and_unsupported_targets(
        self,
    ) -> None:
        cases = (
            "Alias:Plain=Plain",
            "from typing import TypeAlias\nMarker=TypeAlias\nAlias:Marker=Plain",
            "import typing_extensions as t\nMarker=t.TypeAlias\nAlias:Marker=Plain",
            "TypeAlias=Plain\nAlias:TypeAlias=Plain",
            "TypeAlias='typing.TypeAlias'\nAlias:TypeAlias=Plain",
            "from typing import TypeAlias\nTypeAlias=Plain\nAlias:TypeAlias=Plain",
            "from typing import TypeAlias\nAlias:TypeAlias='Plain'",
            "from typing import TypeAlias\nAlias:TypeAlias=list[list[Plain]]",
            "from typing import TypeAlias\nAlias:TypeAlias=make()",
            "from typing import TypeAlias\nvalue=42\nAlias:TypeAlias=value",
            "from typing import TypeAlias\nAlias:TypeAlias=Later\nclass Later: pass",
            "from typing import TypeAlias\nAlias:TypeAlias=Plain\nAlias=Plain",
            "from typing import TypeAlias\n"
            "Alias:TypeAlias=Plain\n"
            "globals()['Alias']=other",
        )
        for declaration in cases:
            with self.subTest(declaration=declaration):
                (result,) = resolve_type_symbols(
                    ("types.Alias",),
                    modules=(module("types", "class Plain: pass\n" + declaration),),
                    project_id="app",
                )
                self.assertEqual(result.status, "unresolved", result)
        for marker_module in ("typing", "typing_extensions"):
            with self.subTest(marker_module=marker_module):
                snapshots = (
                    module(marker_module, "class TypeAlias: pass"),
                    module(
                        "types",
                        f"from {marker_module} import TypeAlias\n"
                        "class Plain: pass\n"
                        "Alias:TypeAlias=Plain",
                    ),
                )
                (result,) = resolve_type_symbols(
                    ("types.Alias",), modules=snapshots, project_id="app"
                )
                self.assertEqual(result.status, "unresolved")
        cycle = (
            module("a", "from typing import TypeAlias\nfrom b import B\nA:TypeAlias=B"),
            module("b", "from typing import TypeAlias\nfrom a import A\nB:TypeAlias=A"),
        )
        (result,) = resolve_type_symbols(("a.A",), modules=cycle, project_id="app")
        self.assertEqual(result.status, "unresolved")
        # An ordinary variable annotation stays unsupported for mapping preflight.
        ordinary = (
            module(
                "values", "Text:str='text'\nfrom graphql_desk import gql\nq=gql(Text)"
            ),
        )
        (result,) = resolve_type_symbols(
            ("values.Text",), modules=ordinary, project_id="app"
        )
        self.assertEqual(result.status, "unresolved")
        self.assertEqual(resolve_project(ordinary).calls[0].source, "text")

    def test_inherited_typed_dict_provenance_survives_aliases_and_reexports(
        self,
    ) -> None:
        snapshots = (
            module(
                "base",
                "from typing_extensions import TypedDict\n"
                "class Base(TypedDict):\n required: str",
            ),
            module("exports", "from base import Base as Public"),
            module(
                "models",
                "from typing import TypeAlias\n"
                "import base\n"
                "from exports import Public\nAlias:TypeAlias=base.Base\n"
                "class Child(Alias,total=False):\n optional: int\n"
                "class Grandchild(Child,total=True):\n final: bool\n"
                "class Qualified(base.Base,total=False): pass\n"
                "class Reexported(Public,total=False): pass",
            ),
        )
        result = resolve_type_symbols(
            ("models.Grandchild",), modules=snapshots, project_id="app"
        )
        self.assertEqual(result[0].status, "resolved")
        self.assertEqual(
            [
                item.status
                for item in resolve_type_symbols(
                    ("models.Qualified", "models.Reexported"),
                    modules=snapshots,
                    project_id="app",
                )
            ],
            ["resolved", "resolved"],
        )
        self.assertEqual(
            result,
            resolve_type_symbols(
                ("models.Grandchild",),
                modules=tuple(reversed(snapshots)),
                project_id="app",
            ),
        )
        changed = (*snapshots[:2], replace(snapshots[2], version=2))
        self.assertNotEqual(
            result[0].dependencies,
            resolve_type_symbols(
                ("models.Grandchild",), modules=changed, project_id="app"
            )[0].dependencies,
        )
        for suffix in (
            "class Plain: pass\nclass Bad(Plain,total=False): pass",
            "class Plain: pass\nclass Bad(Public,Plain,total=False): pass",
            "class Bad(Public,metaclass=unknown): pass",
            "class Bad(Public,closed=True): pass",
            "class Bad(Public,total=unknown): pass",
            "Public=object\nclass Bad(Public,total=False): pass",
        ):
            with self.subTest(suffix=suffix):
                bad = (
                    *snapshots[:2],
                    module("models", "from exports import Public\n" + suffix),
                )
                self.assertEqual(
                    resolve_type_symbols(
                        ("models.Bad",), modules=bad, project_id="app"
                    )[0].status,
                    "unresolved",
                )
        shadowed = (
            module("typing_extensions", "class TypedDict: pass"),
            module(
                "models",
                "from typing_extensions import TypedDict\n"
                "class Base(TypedDict): pass\nclass Child(Base,total=False): pass",
            ),
        )
        self.assertEqual(
            resolve_type_symbols(("models.Child",), modules=shadowed, project_id="app")[
                0
            ].status,
            "unresolved",
        )
        mutated = (*snapshots, module("mutator", "import base\nbase.Base=object"))
        self.assertEqual(
            resolve_type_symbols(
                ("models.Grandchild",), modules=mutated, project_id="app"
            )[0].status,
            "unresolved",
        )

    def test_explicit_container_aliases_keep_trusted_constructor_and_element_facts(
        self,
    ) -> None:
        snapshots = (
            module(
                "base",
                "from typing_extensions import TypedDict\n"
                "class Known(TypedDict):\n value: str",
            ),
            module(
                "models",
                "from typing import TypeAlias,Sequence as Seq\n"
                "from builtins import tuple as Tup\nfrom base import Known\n"
                "Items:TypeAlias=list[Known]\nNames:TypeAlias=Tup[str,...]\n"
                "View:TypeAlias=Seq[Known]\nclass Bad(Items,total=False): pass",
            ),
            module("exports", "from models import Items as Public"),
        )
        symbols = ("models.Items", "models.Names", "models.View", "exports.Public")
        results = resolve_type_symbols(symbols, modules=snapshots, project_id="app")
        self.assertEqual([r.status for r in results], ["resolved"] * 4)
        self.assertEqual(
            results,
            resolve_type_symbols(
                tuple(reversed(symbols)),
                modules=tuple(reversed(snapshots)),
                project_id="app",
            ),
        )
        self.assertEqual(
            resolve_type_symbols(("models.Bad",), modules=snapshots, project_id="app")[
                0
            ].status,
            "unresolved",
        )
        changed = (*snapshots[:1], replace(snapshots[1], version=2), snapshots[2])
        self.assertNotEqual(
            results[0].dependencies,
            resolve_type_symbols(symbols, modules=changed, project_id="app")[
                0
            ].dependencies,
        )
        external = (
            module(
                "models",
                "from typing import TypeAlias\nfrom unknown import Value\n"
                "Alias:TypeAlias=list[Value]",
            ),
        )
        self.assertEqual(
            resolve_type_symbols(("models.Alias",), modules=external, project_id="app")[
                0
            ].status,
            "external",
        )
        for mutation in (
            "import builtins\nbuiltins.list=object",
            "import base\nbase.Known=42",
            "import typing\ntyping.Sequence=object",
        ):
            self.assertTrue(
                all(
                    r.status == "unresolved"
                    for r in resolve_type_symbols(
                        ("models.Items" if "builtins" in mutation else "models.View",),
                        modules=(*snapshots, module("mutator", mutation)),
                        project_id="app",
                    )
                )
            )
        builtin_argument = module(
            "models",
            "from typing import TypeAlias,Sequence\nAlias:TypeAlias=Sequence[str]",
        )
        for mutation in (
            "import builtins\nbuiltins.str=42",
            "import builtins\nbuiltins.__dict__.clear()",
        ):
            self.assertEqual(
                resolve_type_symbols(
                    ("models.Alias",),
                    modules=(builtin_argument, module("mutator", mutation)),
                    project_id="app",
                )[0].status,
                "unresolved",
            )
        cycle = (
            module(
                "a",
                "from typing import TypeAlias\nfrom b import B\nA:TypeAlias=list[B]",
            ),
            module(
                "b",
                "from typing import TypeAlias\nfrom a import A\n"
                "B:TypeAlias=tuple[A,...]",
            ),
        )
        self.assertEqual(
            resolve_type_symbols(("a.A",), modules=cycle, project_id="app")[0].status,
            "unresolved",
        )
        cases = (
            "value=42\nAlias:TypeAlias=list[value]",
            "Alias:TypeAlias=list[Missing]",
            "Alias:TypeAlias=list[Alias]",
            "class Box: pass\nAlias:TypeAlias=Box[str]",
            "from unknown import Box\nAlias:TypeAlias=Box[str]",
            "list=42\nAlias:TypeAlias=list[str]",
            "Alias:TypeAlias=list[str]\nlist=other",
            "Alias:TypeAlias=list[list[str]]",
            "Alias:TypeAlias=list[str,int]",
            "Alias:TypeAlias=list['str']",
            "Alias:TypeAlias=list[TypeAlias]",
            "from typing import Sequence\nAlias:TypeAlias=Sequence[str,int]",
            "Alias:list[str]=list[str]",
            "Alias=list[str]",
        )
        for declaration in cases:
            with self.subTest(declaration=declaration):
                captured = (
                    module("models", "from typing import TypeAlias\n" + declaration),
                )
                self.assertEqual(
                    resolve_type_symbols(
                        ("models.Alias",), modules=captured, project_id="app"
                    )[0].status,
                    "unresolved",
                )
        for constructor, syntax in (
            ("builtins", "list[str]"),
            ("typing", "Sequence[str]"),
        ):
            fake = (
                module(constructor, "class " + syntax.split("[")[0] + ": pass"),
                module(
                    "models",
                    f"from {constructor} import {syntax.split('[')[0]}\n"
                    "from typing_extensions import TypeAlias\n"
                    f"Alias:TypeAlias={syntax}",
                ),
            )
            self.assertEqual(
                resolve_type_symbols(("models.Alias",), modules=fake, project_id="app")[
                    0
                ].status,
                "unresolved",
            )

    def test_standard_class_decorators_preserve_identity_and_capabilities(self) -> None:
        snapshots = (
            module(
                "decorators",
                "from dataclasses import dataclass as dc\n"
                "from typing import final as done\n"
                "setter=dc.__setattr__",
            ),
            module(
                "models",
                "from typing import TypeAlias\n"
                "from decorators import dc,done\n"
                "import dataclasses as d\n"
                "@dc\nclass Plain: value:int\n"
                "@d.dataclass(frozen=True,slots=True)\n"
                "class Frozen(object): value:int\n"
                "@dc(frozen=False,slots=False)\nclass Mutable: value:int\n"
                "@done\nclass Final: pass\nAlias:TypeAlias=Final\n"
                "@done\n@dc(frozen=True,slots=True)\nclass Both: value:int\n"
                "@dc(frozen=True,slots=True)\n@done\nclass Reverse: value:int\n"
                "class Child(Plain): pass\nclass Bad(Final): pass",
            ),
            module("exports", "from models import Alias as Public,Plain as Data"),
            module(
                "consumer",
                "from exports import Public,Data\nimport models\n"
                "class Child(Data): pass\nclass Bad(Public): pass\n"
                "class QualifiedBad(models.Final): pass",
            ),
        )
        names = (
            "models.Plain",
            "models.Frozen",
            "models.Mutable",
            "models.Final",
            "models.Both",
            "models.Reverse",
            "models.Child",
            "exports.Public",
            "consumer.Child",
        )
        results = resolve_type_symbols(names, modules=snapshots, project_id="app")
        self.assertEqual([r.status for r in results], ["resolved"] * len(names))
        self.assertEqual(
            results,
            resolve_type_symbols(
                tuple(reversed(names)),
                modules=tuple(reversed(snapshots)),
                project_id="app",
            ),
        )
        for name in ("models.Bad", "consumer.Bad", "consumer.QualifiedBad"):
            self.assertEqual(
                resolve_type_symbols((name,), modules=snapshots, project_id="app")[
                    0
                ].status,
                "unresolved",
            )
        changed = (replace(snapshots[0], version=2), *snapshots[1:])
        self.assertNotEqual(
            results[0].dependencies,
            resolve_type_symbols(names, modules=changed, project_id="app")[
                0
            ].dependencies,
        )
        for namespace, mutation in (("dataclasses", "dataclass"), ("typing", "final")):
            name = "models.Plain" if namespace == "dataclasses" else "models.Final"
            self.assertEqual(
                resolve_type_symbols(
                    (name,),
                    modules=(
                        *snapshots,
                        module(
                            "mutator", f"import {namespace}\n{namespace}.{mutation}=42"
                        ),
                    ),
                    project_id="app",
                )[0].status,
                "unresolved",
            )
        for mutation in (
            "from dataclasses import dataclass as dc\ndc.__code__=unknown",
            "from decorators import dc\nopaque(dc)",
            "import typing\ntyping.final.__dict__.clear()",
            "from dataclasses import dataclass as dc\n"
            "dc.__setattr__('__code__',replacement.__code__)",
            "from decorators import dc\nsetter=dc.__setattr__\n"
            "setter('__code__',replacement.__code__)",
            "import typing\ntyping.final.__delattr__('__code__')",
            "from dataclasses import dataclass as dc\nopaque(dc.__setattr__)",
            "from decorators import dc\nsetter=dc.__setattr__\nopaque(setter)",
            "from decorators import setter\nopaque(setter)",
            "from decorators import setter\nbox=[setter]\nopaque(box)",
            "import typing\nopaque(typing.final.__delattr__)",
            "from decorators import setter\nsetter('__code__',replacement.__code__)",
        ):
            name = "models.Final" if "typing" in mutation else "models.Plain"
            self.assertEqual(
                resolve_type_symbols(
                    (name,),
                    modules=(*snapshots, module("mutator", mutation)),
                    project_id="app",
                )[0].status,
                "unresolved",
            )
        isolated = (
            *snapshots,
            replace(module("dataclasses", "dataclass=42"), project="other"),
        )
        self.assertEqual(
            resolve_type_symbols(("models.Plain",), modules=isolated, project_id="app")[
                0
            ].status,
            "resolved",
        )

    def test_standard_class_decorators_reject_untrusted_options_and_special_bases(
        self,
    ) -> None:
        declarations = (
            "@final()\nclass Bad: pass",
            "@dataclass(42)\nclass Bad: pass",
            "@dataclass(**{})\nclass Bad: pass",
            "@dataclass(frozen=1)\nclass Bad: pass",
            "flag=True\n@dataclass(frozen=flag)\nclass Bad: pass",
            "@dataclass(init=False)\nclass Bad: pass",
            "@dataclass(frozen=True,frozen=False)\nclass Bad: pass",
            "@dataclass\n@dataclass\nclass Bad: pass",
            "@final\n@final\nclass Bad: pass",
            "def custom(cls): return cls\n@custom\nclass Bad: pass",
            "from unknown import decorate\n@decorate\nclass Bad: pass",
            "dataclass=42\n@dataclass\nclass Bad: pass",
            "@dataclass\nclass Bad: pass\ndataclass=42",
            "@final\nclass Bad: pass\nfinal=42",
            "from typing_extensions import TypedDict\n"
            "@final\n"
            "class Bad(TypedDict): value:int",
            "from typing_extensions import TypedDict\n"
            "class Base(TypedDict): value:int\n"
            "@dataclass\n"
            "class Bad(Base): pass",
            "from enum import Enum\n@final\nclass Bad(Enum): VALUE=1",
            "from enum import Enum\n"
            "class Base(Enum): pass\n"
            "Alias=Base\n"
            "@final\n"
            "class Bad(Alias): pass",
            "@dataclass\n"
            "class Base: value:int\n"
            "@dataclass(frozen=True)\n"
            "class Bad(Base): pass",
            "class Base: pass\n@dataclass\nclass Bad(Base): pass",
        )
        for declaration in declarations:
            with self.subTest(declaration=declaration):
                self.assertEqual(
                    resolve_type_symbols(
                        ("models.Bad",),
                        modules=(
                            module(
                                "models",
                                "from dataclasses import dataclass\n"
                                "from typing import final\n" + declaration,
                            ),
                        ),
                        project_id="app",
                    )[0].status,
                    "unresolved",
                )
        for namespace, decorator in (("dataclasses", "dataclass"), ("typing", "final")):
            fake = (
                module(namespace, f"def {decorator}(cls): return cls"),
                module(
                    "models",
                    f"from {namespace} import {decorator}\n"
                    f"@{decorator}\n"
                    f"class Bad: pass",
                ),
            )
            self.assertEqual(
                resolve_type_symbols(("models.Bad",), modules=fake, project_id="app")[
                    0
                ].status,
                "unresolved",
            )
        special = (
            module(
                "base",
                "from enum import Enum\n"
                "from typing import TypeAlias\n"
                "from typing_extensions import TypedDict\n"
                "class E(Enum): pass\n"
                "Alias:TypeAlias=E\n"
                "class D(TypedDict): value:int",
            ),
            module("exports", "from base import Alias as EnumBase,D as DictBase"),
        )
        self.assertEqual(
            [
                result.status
                for result in resolve_type_symbols(
                    ("exports.EnumBase", "exports.DictBase"),
                    modules=special,
                    project_id="app",
                )
            ],
            ["resolved", "resolved"],
        )
        for base in ("EnumBase", "DictBase"):
            for decorator in ("dataclass", "final"):
                self.assertEqual(
                    resolve_type_symbols(
                        ("models.Bad",),
                        modules=(
                            *special,
                            module(
                                "models",
                                "from dataclasses import dataclass\n"
                                "from typing import final\n"
                                + f"from exports import {base}\n"
                                f"@{decorator}\n"
                                f"class Bad({base}): pass",
                            ),
                        ),
                        project_id="app",
                    )[0].status,
                    "unresolved",
                )

    def test_standard_collection_spellings_share_trust_and_operand_rules(self) -> None:
        for spelling in (
            "set",
            "frozenset",
            "typing.List",
            "typing.Iterable",
            "typing.Collection",
            "typing.MutableSequence",
            "typing.MutableSet",
            "typing.Set",
            "typing.FrozenSet",
            "typing.AbstractSet",
            "collections.abc.Iterable",
            "collections.abc.Collection",
            "collections.abc.MutableSequence",
            "collections.abc.MutableSet",
            "collections.abc.Sequence",
            "collections.abc.Set",
            "typing.Dict",
            "typing.MutableMapping",
            "collections.abc.MutableMapping",
        ):
            with self.subTest(spelling=spelling):
                namespace, _, constructor = spelling.rpartition(".")
                namespace = namespace or "builtins"
                imports = f"from {namespace} import {constructor}\n"
                arguments = (
                    "str,Known"
                    if spelling
                    in {
                        "typing.Dict",
                        "typing.MutableMapping",
                        "collections.abc.MutableMapping",
                    }
                    else "Known"
                )
                declaration = (
                    "from typing_extensions import TypeAlias\nfrom base import Known\n"
                    + (imports if "." in spelling else "")
                    + f"Alias:TypeAlias={constructor}[{arguments}]\n"
                    + "class Child(Alias): pass\nclass Bad(Alias,total=False): pass"
                )
                snapshots = (
                    module(
                        "base",
                        "from typing_extensions import TypedDict\n"
                        "class Known(TypedDict):\n value:int",
                    ),
                    module("models", declaration),
                    module("exports", "from models import Alias as Public"),
                    module(
                        "consumer",
                        "from exports import Public\nclass Child(Public): pass",
                    ),
                )
                names = (
                    "models.Alias",
                    "models.Child",
                    "exports.Public",
                    "consumer.Child",
                )
                results = resolve_type_symbols(
                    names, modules=snapshots, project_id="app"
                )
                self.assertEqual([r.status for r in results], ["resolved"] * len(names))
                self.assertEqual(
                    results,
                    resolve_type_symbols(
                        tuple(reversed(names)),
                        modules=tuple(reversed(snapshots)),
                        project_id="app",
                    ),
                )
                self.assertEqual(
                    resolve_type_symbols(
                        ("models.Bad",), modules=snapshots, project_id="app"
                    )[0].status,
                    "unresolved",
                )
                self.assertNotEqual(
                    results[0].dependencies,
                    resolve_type_symbols(
                        names,
                        modules=(replace(snapshots[0], version=2), *snapshots[1:]),
                        project_id="app",
                    )[0].dependencies,
                )
                self.assertEqual(
                    resolve_type_symbols(
                        ("models.Alias",),
                        modules=(
                            module(
                                "models",
                                declaration.replace(
                                    "from base import Known",
                                    "from unknown import Known",
                                ),
                            ),
                        ),
                        project_id="app",
                    )[0].status,
                    "external",
                )
                for mutation in (
                    f"import {namespace} as ns\nns.{constructor}=42",
                    "import base\nbase.Known=42",
                ):
                    self.assertEqual(
                        resolve_type_symbols(
                            ("models.Alias",),
                            modules=(*snapshots, module("mutator", mutation)),
                            project_id="app",
                        )[0].status,
                        "unresolved",
                    )
                for arguments_bad in (
                    "()",
                    "Known,list[int]",
                    "Known,42",
                    "Known,...",
                    "Alias",
                ):
                    changed = declaration.replace(
                        f"[{arguments}]", f"[{arguments_bad}]"
                    )
                    self.assertEqual(
                        resolve_type_symbols(
                            ("models.Alias",),
                            modules=(snapshots[0], module("models", changed)),
                            project_id="app",
                        )[0].status,
                        "unresolved",
                    )
                shadowed = declaration.replace(
                    "Alias:TypeAlias=", f"{constructor}=42\nAlias:TypeAlias="
                )
                self.assertEqual(
                    resolve_type_symbols(
                        ("models.Alias",),
                        modules=(snapshots[0], module("models", shadowed)),
                        project_id="app",
                    )[0].status,
                    "unresolved",
                )
                fake = (*snapshots, module(namespace, f"class {constructor}: pass"))
                self.assertEqual(
                    resolve_type_symbols(
                        ("models.Alias",), modules=fake, project_id="app"
                    )[0].status,
                    "unresolved",
                )
                isolated = (
                    *snapshots,
                    replace(module("base", "Known=42"), project="other"),
                )
                self.assertEqual(
                    resolve_type_symbols(
                        ("models.Alias",), modules=isolated, project_id="app"
                    )[0].status,
                    "resolved",
                )
        for constructor in ("Sequence", "Set"):
            for parent in ("", "abc=42", "from unknown import abc"):
                self.assertEqual(
                    resolve_type_symbols(
                        ("models.Alias",),
                        modules=(
                            replace(module("collections", parent), is_package=True),
                            module(
                                "models",
                                "from typing import TypeAlias\n"
                                + f"from collections.abc import {constructor}\n"
                                f"Alias:TypeAlias={constructor}[str]",
                            ),
                        ),
                        project_id="app",
                    )[0].status,
                    "unresolved",
                )

    def test_fixed_tuple_aliases_keep_each_named_operand_and_base_capability(
        self,
    ) -> None:
        snapshots = (
            module("keys", "class Key(str): pass"),
            module(
                "values",
                "from typing_extensions import TypedDict\n"
                "class Value(TypedDict):\n item:int",
            ),
            module(
                "models",
                "from typing import TypeAlias,Tuple as T\n"
                "import typing as t\nimport keys,values\n"
                "One:TypeAlias=tuple[keys.Key]\n"
                "Pair:TypeAlias=T[keys.Key,values.Value]\n"
                "Three:TypeAlias=t.Tuple[str,keys.Key,values.Value]\n"
                "Variable:TypeAlias=T[keys.Key,...]\nclass Child(Pair): pass\n"
                "class Bad(Pair,total=False): pass",
            ),
            module("exports", "from models import Pair as Public"),
            module(
                "consumer",
                "from exports import Public\nclass Child(Public): pass\n"
                "class Bad(Public,total=False): pass",
            ),
        )
        names = (
            "models.One",
            "models.Pair",
            "models.Three",
            "models.Variable",
            "models.Child",
            "exports.Public",
            "consumer.Child",
        )
        results = resolve_type_symbols(names, modules=snapshots, project_id="app")
        self.assertEqual(
            [result.status for result in results], ["resolved"] * len(names)
        )
        self.assertEqual(
            results,
            resolve_type_symbols(
                tuple(reversed(names)),
                modules=tuple(reversed(snapshots)),
                project_id="app",
            ),
        )
        for name in ("models.Bad", "consumer.Bad"):
            self.assertEqual(
                resolve_type_symbols((name,), modules=snapshots, project_id="app")[
                    0
                ].status,
                "unresolved",
            )
        for index in (0, 1):
            changed = list(snapshots)
            changed[index] = replace(changed[index], version=2)
            self.assertNotEqual(
                results[1].dependencies,
                resolve_type_symbols(names, modules=tuple(changed), project_id="app")[
                    1
                ].dependencies,
            )
        for arguments in ("Missing,str", "str,Missing", "str,int,Missing"):
            external = module(
                "models",
                "from typing import TypeAlias\nfrom unknown import Missing\n"
                "Alias:TypeAlias=tuple[" + arguments + "]",
            )
            self.assertEqual(
                resolve_type_symbols(
                    ("models.Alias",), modules=(external,), project_id="app"
                )[0].status,
                "external",
            )
        for mutation in (
            "import typing\ntyping.Tuple=object",
            "import keys\nkeys.Key=42",
            "import values\nvalues.Value=42",
        ):
            self.assertEqual(
                resolve_type_symbols(
                    ("models.Pair",),
                    modules=(*snapshots, module("mutator", mutation)),
                    project_id="app",
                )[0].status,
                "unresolved",
            )
        for mutation in (
            "import builtins\nbuiltins.tuple=object",
            "import keys\nkeys.Key=42",
        ):
            self.assertEqual(
                resolve_type_symbols(
                    ("models.One",),
                    modules=(*snapshots, module("mutator", mutation)),
                    project_id="app",
                )[0].status,
                "unresolved",
            )
        for declaration in (
            "Alias:TypeAlias=tuple[()]",
            "Alias:TypeAlias=Tuple[()]",
            "Alias:TypeAlias=tuple[...]",
            "Alias:TypeAlias=tuple[...,str]",
            "Alias:TypeAlias=tuple[str,int,...]",
            "Alias:TypeAlias=tuple[str,list[int]]",
            "Alias:TypeAlias=tuple[str,'int']",
            "Alias:TypeAlias=tuple[str,42]",
            "Alias:TypeAlias=tuple[str,TypeAlias]",
            "Alias:TypeAlias=tuple[Alias,str]",
            "tuple=42\nAlias:TypeAlias=tuple[str,int]",
            "Tuple=42\nAlias:TypeAlias=Tuple[str,int]",
        ):
            with self.subTest(declaration=declaration):
                self.assertEqual(
                    resolve_type_symbols(
                        ("models.Alias",),
                        modules=(
                            module(
                                "models",
                                "from typing import TypeAlias,Tuple\n" + declaration,
                            ),
                        ),
                        project_id="app",
                    )[0].status,
                    "unresolved",
                )
        cycle = (
            module(
                "a",
                "from typing import TypeAlias\nfrom b import B\n"
                "A:TypeAlias=tuple[str,B]",
            ),
            module(
                "b",
                "from typing import TypeAlias\nfrom a import A\n"
                "B:TypeAlias=tuple[A,int]",
            ),
        )
        self.assertEqual(
            resolve_type_symbols(("a.A",), modules=cycle, project_id="app")[0].status,
            "unresolved",
        )
        fake = (
            module("typing", "class Tuple: pass"),
            module(
                "models",
                "from typing_extensions import TypeAlias\nfrom typing import Tuple\n"
                "Alias:TypeAlias=Tuple[str,int]",
            ),
        )
        self.assertEqual(
            resolve_type_symbols(("models.Alias",), modules=fake, project_id="app")[
                0
            ].status,
            "unresolved",
        )
        isolated = (*snapshots, replace(module("keys", "Key=42"), project="other"))
        self.assertEqual(
            resolve_type_symbols(("models.Pair",), modules=isolated, project_id="app")[
                0
            ].status,
            "resolved",
        )

    def test_mapping_aliases_preserve_both_operands_and_constructor_trust(self) -> None:
        snapshots = (
            module(
                "base",
                "from typing_extensions import TypedDict\n"
                "class Known(TypedDict):\n value:str",
            ),
            module(
                "models",
                "from typing import TypeAlias,Mapping\nimport base\n"
                "import builtins as b\nimport typing as t\n"
                "import collections.abc as abc\n"
                "from collections.abc import Mapping as AbstractMapping\n"
                "Imported:TypeAlias=AbstractMapping[str,base.Known]\n"
                "Modern:TypeAlias=abc.Mapping[str,base.Known]\n"
                "Qualified:TypeAlias=b.dict[str,base.Known]\n"
                "QualifiedView:TypeAlias=t.Mapping[str,base.Known]\n"
                "D:TypeAlias=dict[str,base.Known]\n"
                "M:TypeAlias=Mapping[str,base.Known]\n"
                "class Child(D): pass\nclass View(M): pass\n"
                "class Bad(D,total=False): pass",
            ),
            module("exports", "from models import D as Public"),
            module("consumer", "from exports import Public\nclass Child(Public): pass"),
        )
        names = (
            "models.D",
            "models.M",
            "models.Qualified",
            "models.QualifiedView",
            "models.Modern",
            "models.Imported",
            "models.Child",
            "models.View",
            "exports.Public",
            "consumer.Child",
        )
        results = resolve_type_symbols(names, modules=snapshots, project_id="app")
        self.assertEqual([r.status for r in results], ["resolved"] * len(names))
        self.assertEqual(
            results,
            resolve_type_symbols(
                tuple(reversed(names)),
                modules=tuple(reversed(snapshots)),
                project_id="app",
            ),
        )
        self.assertEqual(
            resolve_type_symbols(("models.Bad",), modules=snapshots, project_id="app")[
                0
            ].status,
            "unresolved",
        )
        changed = (replace(snapshots[0], version=2), *snapshots[1:])
        self.assertNotEqual(
            results[0].dependencies,
            resolve_type_symbols(names, modules=changed, project_id="app")[
                0
            ].dependencies,
        )
        for operand in ("Missing,str", "str,Missing"):
            external = module(
                "models",
                "from typing import TypeAlias\nfrom unknown import Missing\n"
                "Alias:TypeAlias=dict[" + operand + "]",
            )
            self.assertEqual(
                resolve_type_symbols(
                    ("models.Alias",), modules=(external,), project_id="app"
                )[0].status,
                "external",
            )
        for mutation in (
            "import builtins\nbuiltins.dict=object",
            "import typing\ntyping.Mapping=object",
            "import base\nbase.Known=42",
            "import builtins\nbuiltins.str=42",
        ):
            for name in (
                ("models.D", "models.M")
                if "dict" not in mutation and "Mapping" not in mutation
                else (("models.D",) if "dict" in mutation else ("models.M",))
            ):
                self.assertEqual(
                    resolve_type_symbols(
                        (name,),
                        modules=(*snapshots, module("mutator", mutation)),
                        project_id="app",
                    )[0].status,
                    "unresolved",
                )
        for declaration in (
            "Alias:TypeAlias=dict[str]",
            "Alias:TypeAlias=dict[str,int,bool]",
            "Alias:TypeAlias=Mapping[str]",
            "Alias:TypeAlias=dict[str,list[int]]",
            "Alias:TypeAlias=dict['str',int]",
            "Alias:TypeAlias=dict[str,TypeAlias]",
            "Alias:TypeAlias=dict[Alias,str]",
            "value=42\nAlias:TypeAlias=dict[str,value]",
            "dict=42\nAlias:TypeAlias=dict[str,int]",
            "Mapping=42\nAlias:TypeAlias=Mapping[str,int]",
        ):
            with self.subTest(declaration=declaration):
                self.assertEqual(
                    resolve_type_symbols(
                        ("models.Alias",),
                        modules=(
                            module(
                                "models",
                                "from typing import TypeAlias,Mapping\n" + declaration,
                            ),
                        ),
                        project_id="app",
                    )[0].status,
                    "unresolved",
                )
        for namespace, constructor in (
            ("builtins", "dict"),
            ("typing", "Mapping"),
            ("collections.abc", "Mapping"),
        ):
            fake = (
                module(namespace, "class " + constructor + ": pass"),
                module(
                    "models",
                    "from typing_extensions import TypeAlias\nfrom "
                    + namespace
                    + " import "
                    + constructor
                    + "\nAlias:TypeAlias="
                    + constructor
                    + "[str,int]",
                ),
            )
            self.assertEqual(
                resolve_type_symbols(("models.Alias",), modules=fake, project_id="app")[
                    0
                ].status,
                "unresolved",
            )
        for mutation in (
            "import collections.abc\ncollections.abc.Mapping=object",
            "import collections\ncollections.abc=object",
            "import collections.abc as abc\nabc.Mapping=object",
        ):
            self.assertTrue(
                all(
                    result.status == "unresolved"
                    for result in resolve_type_symbols(
                        ("models.Modern", "models.Imported"),
                        modules=(*snapshots, module("mutator", mutation)),
                        project_id="app",
                    )
                ),
            )
        for parent in ("", "abc=42", "from unknown import abc"):
            self.assertEqual(
                resolve_type_symbols(
                    ("models.Alias",),
                    modules=(
                        replace(module("collections", parent), is_package=True),
                        module(
                            "models",
                            "from typing import TypeAlias\n"
                            "from collections.abc import Mapping\n"
                            "Alias:TypeAlias=Mapping[str,int]",
                        ),
                    ),
                    project_id="app",
                )[0].status,
                "unresolved",
            )
        isolated = (*snapshots, replace(module("models", "D=42"), project="other"))
        self.assertEqual(
            resolve_type_symbols(("models.D",), modules=isolated, project_id="app")[
                0
            ].status,
            "resolved",
        )

        cycle = (
            module(
                "a",
                "from typing import TypeAlias\nfrom b import B\n"
                "A:TypeAlias=dict[str,B]",
            ),
            module(
                "b",
                "from typing import TypeAlias,Mapping\nfrom a import A\n"
                "B:TypeAlias=Mapping[A,str]",
            ),
        )
        self.assertEqual(
            resolve_type_symbols(("a.A",), modules=cycle, project_id="app")[0].status,
            "unresolved",
        )

    def test_literal_aliases_use_only_trusted_flat_constants(self) -> None:
        snapshots = (
            module(
                "models",
                "from typing import TypeAlias,Literal as L\nimport typing as t\n"
                "Text:TypeAlias=t.Literal['hello','world']\n"
                "Values:TypeAlias=L[1,-2,True,False,None,b'hello']\n"
                "Alias:TypeAlias=Text\n"
                "from typing_extensions import Literal as LE\n"
                "Extra:TypeAlias=LE['hello']",
            ),
            module("exports", "from models import Alias as Public"),
        )
        symbols = (
            "models.Text",
            "models.Values",
            "models.Alias",
            "exports.Public",
            "models.Extra",
        )
        result = resolve_type_symbols(symbols, modules=snapshots, project_id="app")
        self.assertEqual([r.status for r in result], ["resolved"] * len(symbols))
        self.assertEqual(
            result,
            resolve_type_symbols(
                symbols, modules=tuple(reversed(snapshots)), project_id="app"
            ),
        )
        for source in (
            "import models\nclass Bad(models.Text): pass",
            "from exports import Public\nclass Bad(Public): pass",
            "from exports import Public\nclass Bad(Public,total=False): pass",
        ):
            self.assertEqual(
                resolve_type_symbols(
                    ("bad.Bad",),
                    modules=(*snapshots, module("bad", source)),
                    project_id="app",
                )[0].status,
                "unresolved",
            )
        for argument in (
            "()",
            "1.5",
            "1j",
            "VALUE",
            "missing.Value",
            "Literal['hello']",
            "[1]",
            "(1,(2,3))",
            "*VALUES",
            "-True",
            "--1",
            "-1.5",
            "+1",
        ):
            source = (
                "from typing import TypeAlias,Literal\nVALUE='hello'\n"
                "VALUES=('hello',)\nAlias:TypeAlias=Literal[" + argument + "]"
            )
            with self.subTest(argument=argument):
                self.assertEqual(
                    resolve_type_symbols(
                        ("bad.Alias",),
                        modules=(module("bad", source),),
                        project_id="app",
                    )[0].status,
                    "unresolved",
                )
        for prefix in ("Literal=42\n", "from unknown import Literal\n"):
            self.assertEqual(
                resolve_type_symbols(
                    ("bad.Alias",),
                    modules=(
                        module(
                            "bad",
                            "from typing import TypeAlias\n"
                            + prefix
                            + "Alias:TypeAlias=Literal['hello']",
                        ),
                    ),
                    project_id="app",
                )[0].status,
                "unresolved",
            )
        for namespace, marker in (
            ("typing", "typing_extensions"),
            ("typing_extensions", "typing"),
        ):
            fake = (
                module(namespace, "class Literal: pass"),
                module(
                    "bad",
                    f"from {marker} import TypeAlias\nfrom {namespace} import Literal\n"
                    "Alias:TypeAlias=Literal['hello']",
                ),
            )
            self.assertEqual(
                resolve_type_symbols(("bad.Alias",), modules=fake, project_id="app")[
                    0
                ].status,
                "unresolved",
            )
        late = module(
            "bad",
            "from typing import TypeAlias,Literal\n"
            "Alias:TypeAlias=Literal['hello']\nLiteral=other",
        )
        self.assertEqual(
            resolve_type_symbols(("bad.Alias",), modules=(late,), project_id="app")[
                0
            ].status,
            "unresolved",
        )
        ext_mutation = module(
            "mutator", "import typing_extensions\ntyping_extensions.Literal=42"
        )
        self.assertEqual(
            resolve_type_symbols(
                ("models.Extra",), modules=(*snapshots, ext_mutation), project_id="app"
            )[0].status,
            "unresolved",
        )
        for mutation in (
            "import typing\ntyping.Literal=42",
            "import models\nmodels.Text=42",
            "__builtins__['str']=42",
        ):
            extra = module("mutator", mutation)
            self.assertEqual(
                resolve_type_symbols(
                    ("exports.Public",), modules=(*snapshots, extra), project_id="app"
                )[0].status,
                "unresolved",
            )
            isolated = replace(extra, project="another")
            self.assertEqual(
                resolve_type_symbols(
                    symbols, modules=(*snapshots, isolated), project_id="app"
                ),
                result,
            )
        changed = (replace(snapshots[0], version=2), snapshots[1])
        self.assertNotEqual(
            result[0].dependencies,
            resolve_type_symbols(symbols, modules=changed, project_id="app")[
                0
            ].dependencies,
        )
        cycle = (
            module("a", "from b import B as A"),
            module("b", "from a import A as B"),
        )
        self.assertEqual(
            resolve_type_symbols(("a.A",), modules=cycle, project_id="app")[0].status,
            "unresolved",
        )

    def test_optional_aliases_reuse_trusted_subscription_and_nullable_facts(
        self,
    ) -> None:
        snapshots = (
            module(
                "models",
                "from typing import TypeAlias,Optional as Maybe\n"
                "import typing as t\nfrom typing_extensions import TypedDict\n"
                "class Data(TypedDict):\n value: str\n"
                "Text:TypeAlias=t.Optional[str]\nFlag:TypeAlias=Maybe[bool]\n"
                "DataMaybe:TypeAlias=Maybe[Data]\nAlias:TypeAlias=Text\n"
                "Items:TypeAlias=list[Alias]",
            ),
            module("exports", "from models import Alias as Public"),
        )
        symbols = (
            "models.Text",
            "models.Flag",
            "models.DataMaybe",
            "models.Alias",
            "models.Items",
            "exports.Public",
        )
        result = resolve_type_symbols(symbols, modules=snapshots, project_id="app")
        self.assertEqual([r.status for r in result], ["resolved"] * len(symbols))
        self.assertEqual(
            result,
            resolve_type_symbols(
                symbols, modules=tuple(reversed(snapshots)), project_id="app"
            ),
        )
        for source in (
            "import models\nclass Bad(models.Text): pass",
            "from exports import Public\nclass Bad(Public): pass",
            "from models import DataMaybe\nclass Bad(DataMaybe,total=False): pass",
        ):
            self.assertEqual(
                resolve_type_symbols(
                    ("bad.Bad",),
                    modules=(*snapshots, module("bad", source)),
                    project_id="app",
                )[0].status,
                "unresolved",
            )
        for declaration in (
            "value=42\nAlias:TypeAlias=Optional[value]",
            "Alias:TypeAlias=Optional[Missing]",
            "Alias:TypeAlias=Optional[Alias]",
            "Alias:TypeAlias=Optional[Later]\nclass Later: pass",
            "Optional=42\nAlias:TypeAlias=Optional[str]",
            "Alias:TypeAlias=Optional[str]\nOptional=other",
            "Alias:TypeAlias=Optional[str,int]",
            "Alias:TypeAlias=Optional[tuple[str,...]]",
            "Alias:TypeAlias=Optional['str']",
            "Alias:TypeAlias=Optional[None]",
            "Alias:TypeAlias=Optional[TypeAlias]",
        ):
            with self.subTest(declaration=declaration):
                self.assertEqual(
                    resolve_type_symbols(
                        ("bad.Alias",),
                        modules=(
                            module(
                                "bad",
                                "from typing import TypeAlias,Optional\n" + declaration,
                            ),
                        ),
                        project_id="app",
                    )[0].status,
                    "unresolved",
                )
        fake = (
            module("typing", "class Optional: pass"),
            module(
                "bad",
                "from typing_extensions import TypeAlias\nfrom typing import Optional\n"
                "Alias:TypeAlias=Optional[str]",
            ),
        )
        self.assertEqual(
            resolve_type_symbols(("bad.Alias",), modules=fake, project_id="app")[
                0
            ].status,
            "unresolved",
        )
        external = (
            module(
                "models",
                "from typing import TypeAlias,Optional\nfrom unknown import Value\n"
                "Alias:TypeAlias=Optional[Value]",
            ),
        )
        self.assertEqual(
            resolve_type_symbols(("models.Alias",), modules=external, project_id="app")[
                0
            ].status,
            "external",
        )
        for mutation in (
            "import typing\ntyping.Optional=42",
            "import builtins\nbuiltins.str=42",
            "import models\nmodels.Text=42",
        ):
            self.assertEqual(
                resolve_type_symbols(
                    ("exports.Public",),
                    modules=(*snapshots, module("mutator", mutation)),
                    project_id="app",
                )[0].status,
                "unresolved",
            )
        cycle = (
            module(
                "a",
                "from typing import TypeAlias,Optional\nfrom b import B\n"
                "A:TypeAlias=Optional[B]",
            ),
            module(
                "b",
                "from typing import TypeAlias,Optional\nfrom a import A\n"
                "B:TypeAlias=Optional[A]",
            ),
        )
        self.assertEqual(
            resolve_type_symbols(("a.A",), modules=cycle, project_id="app")[0].status,
            "unresolved",
        )
        changed = (replace(snapshots[0], version=2), snapshots[1])
        self.assertNotEqual(
            result[0].dependencies,
            resolve_type_symbols(symbols, modules=changed, project_id="app")[
                0
            ].dependencies,
        )

    def test_annotated_aliases_resolve_only_the_underlying_named_type(self) -> None:
        snapshots = (
            module(
                "models",
                "from typing import Annotated, TypeAlias\n"
                "import typing as t\n"
                "from typing_extensions import Annotated as A\n"
                "Text:TypeAlias=Annotated[str, object()]\n"
                "Alias:TypeAlias=t.Annotated[Text, missing.metadata]\n"
                "Extra:TypeAlias=A[Alias, another_call()]",
            ),
            module("exports", "from models import Extra as Public"),
        )
        symbols = ("models.Text", "models.Alias", "models.Extra", "exports.Public")
        result = resolve_type_symbols(symbols, modules=snapshots, project_id="app")
        self.assertEqual([item.status for item in result], ["resolved"] * len(symbols))
        self.assertEqual(
            result,
            resolve_type_symbols(
                symbols, modules=tuple(reversed(snapshots)), project_id="app"
            ),
        )
        self.assertEqual(
            resolve_type_symbols(
                ("bad.Bad",),
                modules=(
                    *snapshots,
                    module("bad", "from models import Extra\nclass Bad(Extra): pass"),
                ),
                project_id="app",
            )[0].status,
            "unresolved",
        )

        for declaration in (
            "Alias:TypeAlias=Annotated[str]",
            "Alias:TypeAlias=Annotated[Missing, 'metadata']",
            "Alias:TypeAlias=Annotated[42, 'metadata']",
            "Annotated=object\nAlias:TypeAlias=Annotated[str,'metadata']",
            "Alias:TypeAlias=Annotated[str,'metadata']\nAnnotated=object",
        ):
            with self.subTest(declaration=declaration):
                self.assertEqual(
                    resolve_type_symbols(
                        ("bad.Alias",),
                        modules=(
                            module(
                                "bad",
                                "from typing import TypeAlias,Annotated\n"
                                + declaration,
                            ),
                        ),
                        project_id="app",
                    )[0].status,
                    "unresolved",
                )

        fake = (
            module("typing", "class TypeAlias: pass\nclass Annotated: pass"),
            module(
                "bad",
                "from typing import TypeAlias,Annotated\n"
                "Alias:TypeAlias=Annotated[str,'metadata']",
            ),
        )
        self.assertEqual(
            resolve_type_symbols(("bad.Alias",), modules=fake, project_id="app")[
                0
            ].status,
            "unresolved",
        )

    def test_nullable_aliases_preserve_availability_without_base_capabilities(
        self,
    ) -> None:
        snapshots = (
            module(
                "models",
                "import typing\n"
                "from typing import TypeAlias, Union\n"
                "from typing_extensions import Union as ExtUnion\n"
                "from typing_extensions import TypedDict\n"
                "class Data(TypedDict):\n value: str\n"
                "Maybe:TypeAlias=str|None\nReverse:TypeAlias=None|bool\n"
                "Alias:TypeAlias=Maybe\nDataMaybe:TypeAlias=Data|None\n"
                "Items:TypeAlias=list[Maybe]\n"
                "NullableItems:TypeAlias=list[str|None]\n"
                "MixedItems:TypeAlias=list[str|int]\n"
                "TripleItems:TypeAlias=list[str|int|None]\n"
                "LegacyItems:TypeAlias=list[Union[str,None]]\n"
                "NestedLegacyItems:TypeAlias=list[Union[str|int,None]]\n"
                "ExtLegacyItems:TypeAlias=list[ExtUnion[bool,None]]\n"
                "NestedExtLegacyItems:TypeAlias=list[ExtUnion[bool|int,None]]\n"
                "QualifiedLegacyItems:TypeAlias=list[typing.Union[str,int,None]]\n"
                "LegacyScalar:TypeAlias=Union[str,int,None]\n"
                "NestedLegacyScalar:TypeAlias=Union[str|int,None]",
            ),
            module("exports", "from models import Alias as Public"),
        )
        symbols = (
            "models.Maybe",
            "models.Reverse",
            "models.Alias",
            "models.DataMaybe",
            "models.Items",
            "models.NullableItems",
            "models.MixedItems",
            "models.TripleItems",
            "models.LegacyItems",
            "models.NestedLegacyItems",
            "models.ExtLegacyItems",
            "models.NestedExtLegacyItems",
            "models.QualifiedLegacyItems",
            "models.LegacyScalar",
            "models.NestedLegacyScalar",
            "exports.Public",
        )
        result = resolve_type_symbols(symbols, modules=snapshots, project_id="app")
        self.assertEqual([r.status for r in result], ["resolved"] * len(symbols))
        self.assertEqual(
            result,
            resolve_type_symbols(
                symbols, modules=tuple(reversed(snapshots)), project_id="app"
            ),
        )
        for declaration in (
            "import models\nclass Bad(models.Maybe): pass",
            "from exports import Public\nclass Bad(Public): pass",
            "from models import DataMaybe\nclass Bad(DataMaybe,total=False): pass",
            "from models import Reverse\nclass Bad(Reverse): pass",
            "from models import LegacyScalar\nclass Bad(LegacyScalar): pass",
        ):
            self.assertEqual(
                resolve_type_symbols(
                    ("bad.Bad",),
                    modules=(*snapshots, module("bad", declaration)),
                    project_id="app",
                )[0].status,
                "unresolved",
            )
        for declaration in (
            "value=42\nAlias:TypeAlias=value|None",
            "Alias:TypeAlias=Missing|None",
            "Alias:TypeAlias=Alias|None",
            "Alias:TypeAlias=Later|None\nclass Later: pass",
            "Alias:TypeAlias=str|None\nstr=other",
            "Alias:TypeAlias=str|None\nAlias=other",
            "Alias:TypeAlias=TypeAlias|None",
            "Alias:TypeAlias='str'|None",
            "Alias:TypeAlias=list[str]|None",
            "Alias:TypeAlias=None|None",
            "Alias:TypeAlias=list[None]",
            "Alias:TypeAlias=list[None|None]",
            "Alias:TypeAlias=list[str|list[int]]",
            "Alias:TypeAlias=list[Union[str]]",
            "Alias:TypeAlias=list[Union[None,None]]",
            "Alias:TypeAlias=list[Union[str,list[int]]]",
            "Alias:TypeAlias=list[Union[str,'int']]",
            "Alias:TypeAlias=Union[str]",
            "Alias:TypeAlias=Union[None,None]",
            "Alias:TypeAlias=Union[str,list[int]]",
            "Alias:TypeAlias=Union['str',None]",
            "Alias:TypeAlias=Union[Union[str,int],None]",
            "import typing\ntyping.Union=object\n"
            "Alias:TypeAlias=list[typing.Union[str,None]]",
            "Marker=TypeAlias\nAlias:Marker=str|None",
        ):
            with self.subTest(declaration=declaration):
                self.assertEqual(
                    resolve_type_symbols(
                        ("bad.Alias",),
                        modules=(
                            module(
                                "bad",
                                "from typing import TypeAlias, Union\n" + declaration,
                            ),
                        ),
                        project_id="app",
                    )[0].status,
                    "unresolved",
                )
        external = (
            module(
                "models",
                "from typing import TypeAlias\nfrom unknown import Value\n"
                "Alias:TypeAlias=Value|None",
            ),
        )
        self.assertEqual(
            resolve_type_symbols(("models.Alias",), modules=external, project_id="app")[
                0
            ].status,
            "external",
        )
        for mutation in (
            "import builtins\nbuiltins.str=42",
            "import models\nmodels.Maybe=42",
            "__builtins__['str']=42",
        ):
            self.assertEqual(
                resolve_type_symbols(
                    ("exports.Public",),
                    modules=(*snapshots, module("mutator", mutation)),
                    project_id="app",
                )[0].status,
                "unresolved",
            )
        cycle = (
            module(
                "a", "from typing import TypeAlias\nfrom b import B\nA:TypeAlias=B|None"
            ),
            module(
                "b", "from typing import TypeAlias\nfrom a import A\nB:TypeAlias=A|None"
            ),
        )
        self.assertEqual(
            resolve_type_symbols(("a.A",), modules=cycle, project_id="app")[0].status,
            "unresolved",
        )
        changed = (replace(snapshots[0], version=2), snapshots[1])
        self.assertNotEqual(
            result[0].dependencies,
            resolve_type_symbols(symbols, modules=changed, project_id="app")[
                0
            ].dependencies,
        )

    def test_boolean_alias_availability_does_not_prove_subclassability(self) -> None:
        snapshots = (
            module(
                "models",
                "from typing import TypeAlias,Sequence\n"
                "Flag:TypeAlias=bool\nAlias:TypeAlias=Flag\n"
                "Items:TypeAlias=list[Flag]\n"
                "Flags:TypeAlias=tuple[bool,...]\nView:TypeAlias=Sequence[Alias]",
            ),
            module("exports", "from models import Alias as Public"),
        )
        symbols = (
            "models.Flag",
            "models.Alias",
            "models.Items",
            "models.Flags",
            "models.View",
            "exports.Public",
        )
        results = resolve_type_symbols(symbols, modules=snapshots, project_id="app")
        self.assertEqual([r.status for r in results], ["resolved"] * len(symbols))
        self.assertEqual(
            results,
            resolve_type_symbols(
                symbols, modules=tuple(reversed(snapshots)), project_id="app"
            ),
        )
        for declaration in (
            "class Bad(bool): pass",
            "from typing import TypeAlias\nFlag:TypeAlias=bool\nclass Bad(Flag): pass",
            "from exports import Public\nclass Bad(Public): pass",
            "from builtins import bool as Flag\nclass Bad(Flag): pass",
        ):
            with self.subTest(declaration=declaration):
                self.assertEqual(
                    resolve_type_symbols(
                        ("bad.Bad",),
                        modules=(*snapshots, module("bad", declaration)),
                        project_id="app",
                    )[0].status,
                    "unresolved",
                )
        for source in (
            "from typing import TypeAlias\nbool=42\nFlag:TypeAlias=bool",
            "from typing import TypeAlias\nFlag:TypeAlias=bool\nbool=other",
        ):
            self.assertEqual(
                resolve_type_symbols(
                    ("models.Flag",),
                    modules=(module("models", source),),
                    project_id="app",
                )[0].status,
                "unresolved",
            )
        for mutation in (
            "import builtins\nbuiltins.bool=42",
            "__builtins__['bool']=42",
        ):
            self.assertTrue(
                all(
                    r.status == "unresolved"
                    for r in resolve_type_symbols(
                        symbols,
                        modules=(*snapshots, module("mutator", mutation)),
                        project_id="app",
                    )
                )
            )

    def test_known_builtin_mutation_invalidates_all_type_reference_spellings(
        self,
    ) -> None:
        declaration = (
            "from typing import TypeAlias,Sequence\n"
            "import builtins as b\nfrom builtins import str as Text\n"
            "Bare:TypeAlias=str\nQualified:TypeAlias=b.str\n"
            "Imported:TypeAlias=Text\nItems:TypeAlias=Sequence[str]\n"
            "class Plain(str): pass\nclass QualifiedBase(b.str): pass\n"
            "class ImportedBase(Text): pass\n"
            "from graphql_desk import gql,parse\nq=gql('{id}')\na=parse('{id}')"
        )
        main = module("models", declaration)
        symbols = tuple(
            "models." + name
            for name in (
                "Bare",
                "Qualified",
                "Imported",
                "Items",
                "Plain",
                "QualifiedBase",
                "ImportedBase",
            )
        )
        for mutation in (
            "import builtins\nbuiltins.str=42",
            "import builtins as b\nsetattr(b,'str',42)",
            "import builtins\nnamespace=builtins.__dict__\nmutate(namespace)",
        ):
            with self.subTest(mutation=mutation):
                snapshots = (main, module("other", mutation))
                self.assertEqual(
                    [
                        r.status
                        for r in resolve_type_symbols(
                            symbols, modules=snapshots, project_id="app"
                        )
                    ],
                    ["unresolved"] * len(symbols),
                )
                self.assertEqual(resolve_project(snapshots), resolve_project((main,)))
                self.assertEqual(
                    resolve_parse_sources(snapshots), resolve_parse_sources((main,))
                )
                separate = (main, module("other", mutation, project="another"))
                self.assertEqual(
                    resolve_type_symbols(symbols, modules=separate, project_id="app"),
                    resolve_type_symbols(symbols, modules=(main,), project_id="app"),
                )

    def test_implicit_builtins_reflection_only_degrades_type_availability(self) -> None:
        main = module(
            "models",
            "from typing import TypeAlias\nAlias:TypeAlias=list[str]\n"
            "from graphql_desk import gql,parse\nq=gql('{id}')\na=parse('{id}')",
        )
        for mutation in (
            "__builtins__['list']=42",
            "namespace=__builtins__\nmutate(namespace)",
            "namespace=__builtins__\nnamespace['list']=42",
            "__builtins__={}",
            "def __builtins__(): pass",
            "import unknown as __builtins__",
            "import models\nnamespace=models.__builtins__\nmutate(namespace)",
            "def replace_namespace():\n global __builtins__\n"
            " import unknown as __builtins__",
            "import builtins\nbuiltins.list=42",
        ):
            with self.subTest(mutation=mutation):
                snapshots = (main, module("other", mutation))
                before = resolve_project(snapshots)
                parser_before = resolve_parse_sources(snapshots)
                self.assertEqual(before, resolve_project((main,)))
                self.assertEqual(parser_before, resolve_parse_sources((main,)))
                self.assertEqual(
                    resolve_type_symbols(
                        ("models.Alias",), modules=snapshots, project_id="app"
                    )[0].status,
                    "unresolved",
                )
                self.assertEqual(resolve_project(snapshots), before)
                self.assertEqual(resolve_parse_sources(snapshots), parser_before)
                self.assertEqual(len(before.calls), 1)
                self.assertEqual(len(parser_before.calls), 1)
        separate = (main, module("other", "__builtins__['list']=42", project="another"))
        self.assertEqual(
            resolve_type_symbols(("models.Alias",), modules=separate, project_id="app")[
                0
            ].status,
            "resolved",
        )

    def test_captured_type_symbols_distinguish_known_failures_and_unknown_forms(
        self,
    ) -> None:
        cases = (
            ("class Plain: pass", "Missing", "missing"),
            ("VALUE=42", "VALUE", "not-type"),
            ("VALUE='private value'", "VALUE", "not-type"),
            ("def make(): pass", "make", "not-type"),
            ("class Plain: pass\nAlias=Plain\nAlias=other", "Alias", "unresolved"),
            ("from other import *\nclass Plain: pass", "Plain", "unresolved"),
            ("def __getattr__(name): pass", "Missing", "unresolved"),
            ("class Plain: pass\nglobals()['Plain']=other", "Plain", "unresolved"),
            ("@decorate\nclass Plain: pass", "Plain", "unresolved"),
            ("class Plain(metaclass=meta): pass", "Plain", "unresolved"),
            (
                "from typing import TypedDict\n"
                "class Plain(TypedDict,closed=True): pass",
                "Plain",
                "unresolved",
            ),
            ("class Plain(unknown.Base): pass", "Plain", "unresolved"),
            ("Base='builtins.str'\nclass Plain(Base): pass", "Plain", "unresolved"),
            ("Base='enum.Enum'\nclass Plain(Base): pass", "Plain", "unresolved"),
            ("Alias=NewType('Alias',str)", "Alias", "unresolved"),
            (
                "from typing import TypeAlias\nAlias:TypeAlias='Plain'",
                "Alias",
                "unresolved",
            ),
            ("class Plain(", "Plain", "unresolved"),
        )
        for source, name, status in cases:
            with self.subTest(source=source):
                (result,) = resolve_type_symbols(
                    ("types." + name,),
                    modules=(module("types", source),),
                    project_id="app",
                )
                self.assertEqual(result.status, status, result)
                if status == "not-type":
                    self.assertIsNone(result.target)
        cycle = (
            module("a", "from b import B as A"),
            module("b", "from a import A as B"),
        )
        (result,) = resolve_type_symbols(("a.A",), modules=cycle, project_id="app")
        self.assertEqual(result.status, "unresolved")

        (result,) = resolve_type_symbols(
            ("types.Outer.Inner",),
            modules=(module("types", "class Outer:\n class Inner: pass"),),
            project_id="app",
        )
        self.assertEqual(result.status, "unresolved")
        (result,) = resolve_type_symbols(
            ("foreign.Plain",),
            modules=(module("foreign", "class Plain: pass", project="other"),),
            project_id="app",
        )
        self.assertEqual(result.status, "external")
        (result,) = resolve_type_symbols(
            ("types.Plain",),
            modules=(
                module("types", "class Plain: pass"),
                module("other", "import types\ntypes.Plain=int"),
            ),
            project_id="app",
        )
        self.assertEqual(result.status, "unresolved")
        (result,) = resolve_type_symbols(
            ("types.Plain",),
            modules=(
                module("typing_extensions", "class TypedDict: pass"),
                module(
                    "types",
                    "from typing_extensions import TypedDict\n"
                    "class Plain(TypedDict,closed=True): pass",
                ),
            ),
            project_id="app",
        )
        self.assertEqual(result.status, "unresolved")
        for invalid in ("str", "pkg.class", "pkg.\u212a"):
            with self.assertRaises(ValueError):
                resolve_type_symbols((invalid,), modules=(), project_id="app")

    def test_newtype_is_a_resolved_non_base_mapping_type(self) -> None:
        for declaration in (
            "from typing import NewType\nUserId=NewType('UserId',int)",
            "from typing import NewType as NT\nUserId=NT('UserId',int)",
            "import typing as t\nUserId=t.NewType('UserId',int)",
            "from typing_extensions import NewType\nUserId=NewType('UserId',int)",
        ):
            with self.subTest(declaration=declaration):
                models = module("models", declaration)
                (result,) = resolve_type_symbols(
                    ("models.UserId",), modules=(models,), project_id="app"
                )
                self.assertEqual(result.status, "resolved")
                self.assertEqual(result.target, "models.UserId")
                self.assertEqual(
                    result.dependencies,
                    (source_graph.ModuleDependency("app", "models", models.uri, 1),),
                )

        child = module("child", "from models import UserId\nclass Child(UserId): pass")
        self.assertEqual(
            resolve_type_symbols(
                ("child.Child",), modules=(models, child), project_id="app"
            )[0].status,
            "unresolved",
        )

        unsupported = (
            "from typing import NewType\nUserId=NewType(name,int)",
            "from typing import NewType\nUserId=NewType('UserId',factory())",
            "from unknown import NewType\nUserId=NewType('UserId',int)",
            "from typing import NewType\nUserId=NewType('UserId',int,extra=True)",
        )
        for declaration in unsupported:
            with self.subTest(declaration=declaration):
                (result,) = resolve_type_symbols(
                    ("models.UserId",),
                    modules=(module("models", declaration),),
                    project_id="app",
                )
                self.assertEqual(result.status, "unresolved")

        mutated = module("models", "import typing\ntyping.NewType=other")
        mapped_module = module(
            "mapped", "from typing import NewType\nUserId=NewType('UserId',int)"
        )
        (result,) = resolve_type_symbols(
            ("mapped.UserId",), modules=(mapped_module, mutated), project_id="app"
        )
        self.assertEqual(result.status, "unresolved")

    def test_namespace_dict_escapes_invalidate_type_and_document_facts(self) -> None:
        for escape in (
            "exec('Plain=42', types.__dict__)",
            "mutate(types.__dict__)",
            "namespace=types.__dict__\nmutate(namespace)",
            "namespace=types.__dict__\nexec('Plain=42',namespace)",
            "mutate({'namespace':types.__dict__})",
            "types.__dict__.clear()",
            "namespace=types.__dict__\nnamespace.clear()",
        ):
            with self.subTest(escape=escape):
                snapshots: tuple[ModuleSnapshot, ...] = (
                    module("types", "class Plain: pass"),
                    module("other", "import types\n" + escape),
                )
                (result,) = resolve_type_symbols(
                    ("types.Plain",), modules=snapshots, project_id="app"
                )
                self.assertEqual(result.status, "unresolved")
                snapshots = (
                    module("types", "from graphql_desk import gql\nTEXT='{ok}'"),
                    module("other", "import types\n" + escape),
                    module(
                        "main",
                        "from graphql_desk import gql\n"
                        "from types import TEXT\nq=gql(TEXT)",
                    ),
                )
                graph = resolve_project(snapshots)
                self.assertEqual(graph.calls, ())
                self.assertTrue(graph.diagnostics)
        for reflection in (
            "sys.modules['app.models'].Plain=42",
            "mutate(sys.modules)",
            "registry=sys.modules\nmutate(registry)",
        ):
            with self.subTest(reflection=reflection):
                snapshots = (
                    module("app", "", package=True),
                    module("app.models", "class Plain: pass"),
                    module("other", "import app.models\nimport sys\n" + reflection),
                )
                (result,) = resolve_type_symbols(
                    ("app.models.Plain",), modules=snapshots, project_id="app"
                )
                self.assertEqual(result.status, "unresolved")
        # Passing an ordinary exported value does not escape the module namespace.
        snapshots = (
            module("types", "class Plain: pass\nTEXT='{ok}'"),
            module("other", "import types\nprint(types.TEXT)"),
        )
        (result,) = resolve_type_symbols(
            ("types.Plain",), modules=snapshots, project_id="app"
        )
        self.assertEqual(result.status, "resolved")
        snapshots = (
            module("templates", "TEXT='{ok}'\ndef noop(): pass"),
            module(
                "main",
                "from graphql_desk import gql\nimport templates\n"
                "templates.noop()\nq=gql(templates.TEXT)",
            ),
        )
        self.assertEqual(len(resolve_project(snapshots).calls), 1)
        snapshots = (
            module(
                "main",
                "from graphql_desk import gql\ngql.scalar('String','x')\nq=gql('{ok}')",
            ),
        )
        self.assertEqual(len(resolve_project(snapshots).calls), 1)

    def test_source_failure_provenance_excludes_other_error_origins(self) -> None:
        snapshot = module(
            "main",
            "from graphql_desk import gql,init_graphql\n"
            "def runtime_source()->str: return '{hello}'\n"
            "source_error=gql(runtime_source())\n"
            "wrong_type=gql(gql)\n"
            "missing=gql(unknown)\n"
            "argument_error=gql('{hello}',unknown=1)\n"
            "fragment_error=gql('{hello}',fragments=unknown)\n"
            "selector_error=gql('{hello}',operation=unknown)\n"
            "dependent=gql('query Q{...F}',(source_error,))\n"
            "scoped=lambda:gql(runtime_source())\n"
            "good=gql('{hello}')\n",
        )
        result = resolve_project(
            (snapshot,), bindings=(ProjectBindings("app", ("one",), "one"),)
        )
        rejected = {item.binding: item for item in result.unavailable}
        self.assertEqual(
            {name for name, item in rejected.items() if item.source_failure},
            {"source_error", "wrong_type"},
        )
        self.assertFalse(rejected["dependent"].source_failure)
        self.assertFalse(rejected[None].source_failure)
        self.assertTrue(
            all(item.code == "source-not-static" for item in result.diagnostics)
        )
        self.assertEqual([item.binding for item in result.calls], ["good"])
        for item in rejected.values():
            with self.assertRaises(FrozenInstanceError):
                setattr(item, "source_failure", True)
        self.assertFalse(
            source_graph.RejectedCall(
                rejected["missing"].module,
                rejected["missing"].range,
                "missing",
                rejected["missing"].diagnostics,
            ).source_failure
        )
        with patch(
            "graphql_desk.source_graph._Graph.extract", side_effect=RecursionError
        ):
            excessive = resolve_project(
                (snapshot,), bindings=(ProjectBindings("app", ("one",), "one"),)
            )
        self.assertTrue(excessive.unavailable)
        self.assertTrue(all(not item.source_failure for item in excessive.unavailable))

    def test_rejected_call_ownership_without_invented_semantics(self) -> None:
        item = module(
            "main",
            "from graphql_desk import gql, init_graphql\n"
            "bad_source=gql(dynamic)\n"
            "bad_selector=gql('{ id }',operation=selector)\n"
            "bad_fragments=gql('{ id }',[unknown])\n"
            "bad_init=init_graphql(dynamic)\n"
            "good=gql('{ ok }')\n",
        )
        result = resolve_project((item,))
        self.assertEqual([call.binding for call in result.calls], ["good"])
        self.assertEqual(
            [call.binding for call in result.unavailable],
            ["bad_source", "bad_selector", "bad_fragments"],
        )
        for rejected in result.unavailable:
            self.assertEqual(rejected.module.uri, item.uri)
            self.assertEqual(rejected.module.version, item.version)
            self.assertEqual(len(rejected.diagnostics), 1)
            self.assertEqual(rejected.diagnostics[0].range, rejected.range)
            self.assertTrue(
                item.source[rejected.range.start : rejected.range.end].startswith(
                    "gql("
                )
            )
            self.assertFalse(hasattr(rejected, "source"))
            self.assertFalse(hasattr(rejected, "schema_name"))
            with self.assertRaises(FrozenInstanceError):
                setattr(rejected, "binding", "changed")
        self.assertEqual(ProjectSources((), ()).unavailable, ())
        resolver = IncrementalResolver()
        cold = resolver.resolve((item,))
        warm = resolver.resolve((item,))
        self.assertEqual(cold, result)
        self.assertEqual(warm, result)
        self.assertIs(cold.unavailable[0], warm.unavailable[0])
        probe = resolve_project(
            (
                module(
                    "probe",
                    "from graphql_desk import gql\ngql=other\nrejected=gql('{ id }')",
                ),
            )
        )
        self.assertEqual(probe.calls, ())
        self.assertEqual([call.binding for call in probe.unavailable], ["rejected"])

    def test_constants_reexports_relative_imports_and_contributions(self) -> None:
        constants = module("pkg.constants", 'HEAD = "query Q "\nTAIL = "{ id }"\n')
        bridge = module(
            "pkg.bridge",
            "from .constants import HEAD, TAIL\nSOURCE = HEAD + TAIL\n"
            "from graphql_desk import gql as query\n",
        )
        consumer = module(
            "pkg.consumer",
            'from .bridge import SOURCE, query\ndoc = query(SOURCE, operation="Q")\n',
        )
        result = resolve_project((consumer, bridge, constants))
        self.assertEqual(result.diagnostics, ())
        self.assertEqual(len(result.calls), 1)
        call = result.calls[0]
        self.assertEqual(call.source, "query Q { id }")
        self.assertEqual(call.binding, "doc")
        self.assertEqual(call.operation, "Q")
        self.assertEqual(
            [item.text for item in call.contributions], ["query Q ", "{ id }"]
        )
        for contribution in call.contributions:
            original = constants.source[
                contribution.range.start : contribution.range.end
            ]
            self.assertTrue(original.startswith('"'))
            self.assertTrue(original.endswith('"'))
        self.assertEqual(
            {item.module for item in call.dependencies},
            {"pkg.consumer", "pkg.bridge", "pkg.constants"},
        )
        updated = resolve_project(
            (
                consumer,
                bridge,
                replace(
                    constants,
                    version=2,
                    source='HEAD = "query New "\nTAIL = "{ name }"\n',
                ),
            )
        )
        self.assertEqual(updated.calls[0].source, "query New { name }")
        self.assertIn(2, {item.version for item in updated.calls[0].dependencies})

    def test_colocated_imports_resolve_aliases_reexports_and_module_namespaces(
        self,
    ) -> None:
        fragment = module(
            "fragments",
            'from graphql_desk import gql\nCard=gql("fragment Card on User { id }")',
        )
        reexport = module("reexports", "from fragments import Card as Public")
        direct = module(
            "direct",
            "from reexports import Public as card\n"
            'from graphql_desk import gql\nq=gql("{ user { ...Card } }", [card])',
        )
        namespace = module(
            "namespace",
            "import reexports as refs\n"
            "from graphql_desk import gql\n"
            'q=gql("{ user { ...Card } }", [refs.Public])',
        )
        result = resolve_project(
            (fragment, reexport, direct, namespace),
            bindings=(ProjectBindings("app", ("default",), "default"),),
            collect_document_imports=True,
        )
        card = next(call for call in result.calls if call.binding == "Card")
        identities = {
            item.module.module: item.document for item in result.document_imports
        }
        identity = DocumentIdentity(card.module, card.range)
        self.assertEqual(identities["reexports"], identity)
        self.assertEqual(identities["direct"], identity)
        self.assertEqual(identities["namespace"], identity)
        resolver = IncrementalResolver()
        bindings = (ProjectBindings("app", ("default",), "default"),)
        self.assertEqual(
            resolver.resolve((fragment, reexport), bindings=bindings).document_imports,
            (),
        )
        self.assertTrue(
            resolver.resolve(
                (fragment, reexport),
                bindings=bindings,
                collect_document_imports=True,
            ).document_imports
        )
        self.assertEqual(
            resolver.resolve((fragment, reexport), bindings=bindings).document_imports,
            (),
        )

    def test_namespace_imports_aliases_same_line_and_packages(self) -> None:
        constants = module("pkg.constants", 'TEXT = "{ id }"')
        package = module("pkg", "from .constants import TEXT", package=True)
        for source in (
            "import graphql_desk as desk; import pkg.constants; "
            "alias = desk.gql; q = alias(pkg.constants.TEXT)",
            "from graphql_desk import graphql as g; from pkg import TEXT; q = g(TEXT)",
            "import graphql_desk; from pkg import constants as c; "
            "q = graphql_desk.graphql(c.TEXT)",
        ):
            with self.subTest(source=source):
                result = resolve_project((module("main", source), constants, package))
                self.assertEqual(result.diagnostics, ())
                self.assertEqual([call.source for call in result.calls], ["{ id }"])

    def test_distinct_projects_and_duplicate_identities(self) -> None:
        one = module("constants", 'TEXT = "{ one }"', project="one")
        two = module("constants", 'TEXT = "{ two }"', project="two")
        source = "from graphql_desk import gql\nfrom constants import TEXT\ngql(TEXT)"
        result = resolve_project(
            (
                one,
                two,
                module("main", source, project="one"),
                module("main", source, project="two"),
            )
        )
        self.assertEqual({call.source for call in result.calls}, {"{ one }", "{ two }"})
        for snapshots in ((one, one), (one, replace(one, module="alias"))):
            with self.subTest(snapshots=snapshots), self.assertRaises(ValueError):
                resolve_project(snapshots)

    def test_dynamic_sources_fragments_and_forward_references_are_diagnostics(
        self,
    ) -> None:
        for body in (
            'q=gql(TEXT)\nTEXT="{ id }"',
            "TEXT=other()\nq=gql(TEXT)",
            'TEXT="x"\nTEXT+="y"\nq=gql(TEXT)',
            'TEXT="x"\ndel TEXT\nq=gql(TEXT)',
            'q=gql(f"{dynamic}")',
            'q=gql("{}".format(value))',
            'q=gql("%s" % value)',
            'q=gql("{ id }", [fragment])',
            'q=gql("{ id }", *fragments)',
            'q=gql("{ id }", operation=selected)',
            "from missing import TEXT\nq=gql(TEXT)",
            'if flag:\n TEXT="{ id }"\nq=gql(TEXT)',
        ):
            with self.subTest(body=body):
                result = resolve_project(
                    (module("main", "from graphql_desk import gql\n" + body),)
                )
                self.assertEqual(result.calls, ())
                self.assertTrue(result.diagnostics)

    def test_import_cycles_are_diagnosed_at_known_graphql_use(self) -> None:
        first = module("a", "from b import TEXT")
        second = module("b", "from a import TEXT")
        main = module(
            "main", "from graphql_desk import gql\nfrom a import TEXT\ngql(TEXT)"
        )
        result = resolve_project((first, second, main))
        self.assertEqual(result.calls, ())
        self.assertIn("cycle", result.diagnostics[0].message)

    def test_unrelated_calls_and_lookalike_modules_are_not_candidates(self) -> None:
        main = module(
            "main",
            """import os
from datetime import datetime
os.path.join('a', 'b')
datetime.now()
def local(source):
 return source
alias = local
alias('{ id }')
""",
        )
        result = resolve_project((main,))
        self.assertEqual(result.calls, ())
        self.assertEqual(result.diagnostics, ())
        lookalike = module("graphql_desk", "def gql(source):\n return source")
        consumer = module("consumer", 'from graphql_desk import gql\ngql("{ id }")')
        result = resolve_project((lookalike, consumer))
        self.assertEqual(result.calls, ())
        self.assertEqual(result.diagnostics, ())

    def test_shadow_rebinding_and_namespace_mutation_never_succeed(self) -> None:
        for body in (
            'gql=other\ngql("{ id }")',
            'if flag:\n from other import gql\ngql("{ id }")',
            'try:\n pass\nexcept Exception as gql:\n pass\ngql("{ id }")',
            'match value:\n case {"q": gql}: pass\ngql("{ id }")',
            'marker=(gql := lambda *_: None)\ngql("{ id }")',
            'def f(x=(gql := other)): pass\ngql("{ id }")',
            'def f(x: (gql := other)): pass\ngql("{ id }")',
            'value: (gql := other) = 1\ngql("{ id }")',
            'from other import *\ngql("{ id }")',
        ):
            with self.subTest(body=body):
                result = resolve_project(
                    (module("main", "from graphql_desk import gql\n" + body),)
                )
                self.assertEqual(result.calls, ())
                self.assertTrue(result.diagnostics)
        for mutation in (
            "desk.gql=other",
            "alias=desk\nalias.gql=other",
            'setattr(desk, "gql", other)',
            "def patch():\n desk.gql=other",
        ):
            result = resolve_project(
                (
                    module(
                        "main",
                        "import graphql_desk as desk\n"
                        + mutation
                        + '\ndesk.gql("{ id }")',
                    ),
                )
            )
            self.assertEqual(result.calls, ())
            self.assertTrue(result.diagnostics)

    def test_direct_functions_resolve_and_shadowed_callees_remain_untrusted(
        self,
    ) -> None:
        source = 'from graphql_desk import gql\ndef f():\n return gql("{ id }")'
        result = resolve_project((module("main", source),))
        self.assertEqual(len(result.calls), 1)
        self.assertIsNone(result.calls[0].binding)
        self.assertEqual(result.diagnostics, ())
        self.assertEqual(len(extract_calls(source).calls), 1)
        shadowed = resolve_project(
            (
                module(
                    "main",
                    'from graphql_desk import gql\ndef f(gql):\n return gql("{ id }")',
                ),
            )
        )
        self.assertEqual(shadowed.calls, ())
        self.assertEqual(shadowed.diagnostics, ())

    def test_function_inputs_never_substitute_module_globals_for_local_names(
        self,
    ) -> None:
        prefix = "from graphql_desk import gql,parse\nTEXT='{ id }'\n"
        bodies = (
            "def f(TEXT): return CALL(TEXT)",
            "def f():\n q=CALL(TEXT)\n TEXT='{ other }'\n return q",
            "def f():\n from other import TEXT\n return CALL(TEXT)",
            "def f():\n import other as TEXT\n return CALL(TEXT)",
            "def f():\n try: pass\n except Exception as TEXT: return CALL(TEXT)",
            "def f(value):\n match value:\n  case {'x':TEXT}: return CALL(TEXT)",
            "def f(): return CALL(TEXT:='{ other }')",
            "def f():\n global TEXT\n return CALL(TEXT)",
            "def outer(TEXT):\n def f(): return CALL(TEXT)\n return f",
            "def outer():\n TEXT='{ other }'\n def f():\n"
            "  nonlocal TEXT\n  return CALL(TEXT)\n return f",
            "class C:\n q=CALL(TEXT)",
            "f=lambda: CALL(TEXT)",
            "q=[CALL(TEXT) for _ in ()]",
            "def f(x=CALL(TEXT)): pass",
            "@CALL(TEXT)\ndef f(): pass",
            "def f(x:CALL(TEXT)): pass",
            "def f():\n def TEXT(): pass\n return CALL(TEXT)",
            "def f():\n class TEXT: pass\n return CALL(TEXT)",
        )
        for callable_ in ("gql", "parse"):
            resolve = resolve_project if callable_ == "gql" else resolve_parse_sources
            for body in bodies:
                with self.subTest(callable_=callable_, body=body):
                    result = resolve(
                        (module("main", prefix + body.replace("CALL", callable_)),)
                    )
                    self.assertEqual(result.calls, ())
                    self.assertTrue(result.diagnostics)
            for suffix in (
                "\nTEXT='{ other }'",
                f"\n{callable_}=other",
            ):
                source = prefix + f"def f(): return {callable_}(TEXT)" + suffix
                self.assertEqual(resolve((module("main", source),)).calls, ())
            source = (
                f"from graphql_desk import {callable_}\n"
                f"def f(): return {callable_}(TEXT)\nTEXT='{{id}}'"
            )
            self.assertEqual(resolve((module("main", source),)).calls, ())
            for local in (
                f"from other import {callable_}",
                f"import other as {callable_}",
            ):
                source = prefix + f"def f():\n {local}\n return {callable_}('{{id}}')"
                result = resolve((module("main", source),))
                self.assertEqual(result.calls, ())
                self.assertEqual(result.diagnostics, ())

    @unittest.skipIf(sys.version_info < (3, 12), "Generic functions require Python3.12")
    def test_generic_function_type_parameters_never_use_module_globals(self) -> None:
        for callable_ in ("gql", "parse"):
            resolve = resolve_project if callable_ == "gql" else resolve_parse_sources
            for declaration in (
                f"def f[TEXT](): return {callable_}(TEXT)",
                f"async def f[TEXT](): return {callable_}(TEXT)",
                f"def f[{callable_}](): return {callable_}('{{ id }}')",
                f"def f[T:{callable_}(TEXT)](): pass",
            ):
                source = (
                    f"from graphql_desk import {callable_}\nTEXT='{{ id }}'\n"
                    + declaration
                )
                with self.subTest(source=source):
                    self.assertEqual(resolve((module("main", source),)).calls, ())

    def test_nested_directives_do_not_disable_unrelated_outer_calls(self) -> None:
        for declaration in ("def outer():", "async def outer():"):
            for nested in (
                " def helper():\n  TEXT='local'\n  return TEXT\n",
                " def helper():\n  global OTHER\n  return OTHER\n",
                " async def helper():\n  nonlocal seed\n  return seed\n",
                " class Helper:\n  global OTHER\n  value=OTHER\n",
            ):
                source = (
                    "from graphql_desk import gql,parse\n"
                    "TEXT='{ id }'\nOTHER=1\n"
                    + declaration
                    + "\n seed=1\n"
                    + nested
                    + " return gql(TEXT),parse(TEXT)\n"
                )
                with self.subTest(source=source):
                    snapshots = (module("main", source),)
                    documents = resolve_project(snapshots)
                    parsers = resolve_parse_sources(snapshots)
                    self.assertEqual(documents.diagnostics, ())
                    self.assertEqual(parsers.diagnostics, ())
                    self.assertEqual(len(documents.calls), 1)
                    self.assertEqual(len(parsers.calls), 1)
                    self.assertEqual(
                        documents, IncrementalResolver().resolve(snapshots)
                    )
        for body in (
            " global OTHER\n return CALL(TEXT)",
            " seed=1\n def helper():\n  global TEXT\n  TEXT='changed'\n"
            " return CALL(TEXT)",
            " def helper():\n  return CALL(TEXT)\n return helper",
            " seed=1\n def helper():\n  nonlocal seed\n  return CALL(TEXT)\n"
            " return helper",
        ):
            for callable_, resolve in (
                ("gql", resolve_project),
                ("parse", resolve_parse_sources),
            ):
                source = (
                    f"from graphql_desk import {callable_}\nTEXT='{{ id }}'\n"
                    "OTHER=1\ndef outer():\n" + body.replace("CALL", callable_)
                )
                with self.subTest(source=source):
                    result = resolve((module("main", source),))
                    self.assertEqual(result.calls, ())
                    self.assertTrue(result.diagnostics)

    def test_function_globals_fragments_and_async_calls_keep_existing_identities(
        self,
    ) -> None:
        snapshots = (
            module(
                "leaf",
                "from graphql_desk import gql\nf=gql('fragment F on Query { id }')",
            ),
            module(
                "main",
                "from graphql_desk import gql,parse\nfrom leaf import f\n"
                "TEXT='query Q { ...F }'\n"
                "def build(): return gql(TEXT,(f,),operation='Q')\n"
                "async def other(): return parse('{ id }')",
            ),
        )
        result = resolve_project(snapshots)
        self.assertEqual(result.diagnostics, ())
        self.assertEqual(len(result.calls), 2)
        self.assertEqual(result.calls[1].operation, "Q")
        self.assertIsNone(result.calls[1].binding)
        self.assertEqual(result.calls[1].fragments[0].target.module.module, "leaf")
        self.assertEqual(result, source_graph.IncrementalResolver().resolve(snapshots))
        self.assertEqual(len(resolve_parse_sources(snapshots).calls), 1)
        for body in (
            "def f(frag): return gql('{...F}',(frag,))",
            "def f(operation): return gql('query Q{id}',operation=operation)",
        ):
            self.assertEqual(
                resolve_project(
                    (module("main", "from graphql_desk import gql\n" + body),)
                ).calls,
                (),
            )

    def test_persisted_declarations_resolve_schema_document_and_exact_spans(
        self,
    ) -> None:
        factory = module(
            "factory",
            "from graphql_desk import init_graphql\n"
            "main=init_graphql('main')\nother=init_graphql('other')",
        )
        documents = module(
            "documents",
            "from factory import main as graphql, other as other_graphql\n"
            "query=graphql('query Q { id }')\n"
            "persist=graphql.persisted\n"
            "registered=persist('opaque-id', query)\n"
            "other_query=other_graphql('query Other { id }')\n"
            "other_registered=other_graphql.persisted("
            "id='sha256:abc', document=other_query)",
        )
        bindings = ProjectBindings("app", ("main", "other"), "main")
        result = resolve_project((factory, documents), bindings=(bindings,))
        self.assertEqual(result.diagnostics, ())
        self.assertEqual(len(result.persisted), 2)
        query = next(call for call in result.calls if call.binding == "query")
        other_query = next(
            call for call in result.calls if call.binding == "other_query"
        )
        first, second = result.persisted
        self.assertEqual(first.id, "opaque-id")
        self.assertTrue(first.id_is_literal)
        self.assertEqual(first.schema_name, "main")
        self.assertEqual(first.document, DocumentIdentity(query.module, query.range))
        self.assertEqual(
            documents.source[first.id_range.start : first.id_range.end],
            "'opaque-id'",
        )
        self.assertEqual(
            documents.source[first.range.start : first.range.end],
            "persist('opaque-id', query)",
        )
        self.assertEqual(second.id, "sha256:abc")
        self.assertTrue(second.id_is_literal)
        self.assertEqual(second.schema_name, "other")
        self.assertEqual(
            second.document, DocumentIdentity(other_query.module, other_query.range)
        )
        self.assertEqual(
            documents.source[second.id_range.start : second.id_range.end],
            "'sha256:abc'",
        )
        self.assertEqual(
            result,
            IncrementalResolver().resolve((factory, documents), bindings=(bindings,)),
        )

    def test_persisted_declarations_reject_dynamic_ids_documents_and_schema_mismatch(
        self,
    ) -> None:
        source = (
            "from graphql_desk import init_graphql\n"
            "main=init_graphql('main')\nother=init_graphql('other')\n"
            "query=main('query Q { id }')\n"
            "other_query=other('query Other { id }')\n"
            "dynamic_id=make_id()\n"
            "bad_id=main.persisted(dynamic_id, query)\n"
            "bad_document=main.persisted('id', make_document())\n"
            "bad_schema=main.persisted('id', other_query)"
        )
        result = resolve_project(
            (module("main", source),),
            bindings=(ProjectBindings("app", ("main", "other"), "main"),),
        )
        self.assertEqual(result.persisted, ())
        diagnostics = [
            item for item in result.diagnostics if item.code == "persisted-not-static"
        ]
        self.assertEqual(len(diagnostics), 3)
        self.assertEqual(
            [source[item.range.start : item.range.end] for item in diagnostics],
            ["dynamic_id", "make_document()", "other_query"],
        )

    def test_untrusted_persisted_lookalikes_are_not_collected(self) -> None:
        result = resolve_project(
            (
                module(
                    "main",
                    "class Fake:\n"
                    " def persisted(self, id, document): return None\n"
                    "fake=Fake()\n"
                    "query=unknown('query Q { id }')\n"
                    "fake.persisted('id', query)",
                ),
            )
        )
        self.assertEqual(result.persisted, ())

    def test_unicode_crlf_and_opaque_code_are_not_executed(self) -> None:
        source = (
            'from graphql_desk import gql\r\nraise RuntimeError("never execute")\r\n'
            'text="🙂"; q=gql("{ café }")'
        )
        result = resolve_project((module("main", source),))
        self.assertEqual(len(result.calls), 1)
        call = result.calls[0]
        self.assertEqual(source[call.range.start : call.range.end], 'gql("{ café }")')
        invalid = resolve_project((module("bad", "def invalid("),))
        self.assertEqual(invalid.calls, ())
        self.assertEqual(invalid.diagnostics[0].code, "invalid-python")

    def test_namespace_reflection_and_escaped_references_cannot_publish_calls(
        self,
    ) -> None:
        for mutation in (
            "vars(desk)['gql']=other",
            "put=setattr; put(desk,'gql',other)",
            "def patch(mod):\n mod.gql=other\npatch(desk)",
            "def patch(mod):\n mod.gql=other\npatch(mod=desk)",
            "patch([desk])",
        ):
            with self.subTest(mutation=mutation):
                result = resolve_project(
                    (
                        module(
                            "main",
                            "import graphql_desk as desk\n"
                            + mutation
                            + '\ndesk.gql("{ id }")',
                        ),
                    )
                )
                self.assertEqual(result.calls, ())
                self.assertTrue(result.diagnostics)
        for mutation in (
            "globals()['gql']=other",
            "namespace=globals; namespace()['gql']=other",
            "gql.__code__=other.__code__",
            "patch(gql)",
        ):
            with self.subTest(mutation=mutation):
                result = resolve_project(
                    (
                        module(
                            "main",
                            "from graphql_desk import gql\n"
                            + mutation
                            + '\ngql("{ id }")',
                        ),
                    )
                )
                self.assertEqual(result.calls, ())
                self.assertTrue(result.diagnostics)

    def test_deep_traversal_discards_partial_module_calls(self) -> None:
        source = (
            'from graphql_desk import gql\ngql("{ ok }")\nq=gql('
            + "+".join(['"x"'] * 550)
            + ")"
        )
        result = resolve_project((module("main", source),))
        self.assertEqual(result.calls, ())
        self.assertIn("source-too-complex", {item.code for item in result.diagnostics})

    def test_invalid_supplied_modules_never_fall_back_to_installed_bindings(
        self,
    ) -> None:
        shadow = module("graphql_desk", "def broken(")
        main = module("main", 'from graphql_desk import gql\ngql("{ id }")')
        result = resolve_project((shadow, main))
        self.assertEqual(result.calls, ())
        self.assertEqual(result.diagnostics[0].code, "invalid-python")
        package = module("pkg", "def broken(", package=True)
        child = module("pkg.child", 'TEXT="{ id }"')
        main = module(
            "main",
            "from graphql_desk import gql\nfrom pkg.child import TEXT\ngql(TEXT)",
        )
        result = resolve_project((package, child, main))
        self.assertEqual(result.calls, ())
        self.assertTrue(result.diagnostics)

    def test_explicit_non_package_parent_cannot_resolve_child_modules(self) -> None:
        parent = module("pkg", "# ordinary module")
        child = module("pkg.constants", 'TEXT="{ id }"')
        for source in (
            "from graphql_desk import gql\nimport pkg.constants\n"
            "gql(pkg.constants.TEXT)",
            "from graphql_desk import gql\nfrom pkg.constants import TEXT\ngql(TEXT)",
            "from graphql_desk import gql\nfrom pkg import constants\n"
            "gql(constants.TEXT)",
        ):
            with self.subTest(source=source):
                result = resolve_project((parent, child, module("main", source)))
                self.assertEqual(result.calls, ())
                self.assertTrue(result.diagnostics)

    def test_builtin_reflection_import_identities_cannot_publish_calls(self) -> None:
        for mutation in (
            "import builtins\nbuiltins.globals()['gql']=other",
            "import builtins\ngetattr(builtins,'globals')()['gql']=other",
            "identity(globals)()['gql']=other",
            "from builtins import globals as namespace\n"
            "identity(namespace)()['gql']=other",
            "from builtins import globals as namespace\nnamespace()['gql']=other",
            "import builtins as b\nnamespace=b.globals\nnamespace()['gql']=other",
        ):
            with self.subTest(mutation=mutation):
                result = resolve_project(
                    (
                        module(
                            "main",
                            "from graphql_desk import gql\n"
                            + mutation
                            + '\ngql("{ id }")',
                        ),
                    )
                )
                self.assertEqual(result.calls, ())
                self.assertTrue(result.diagnostics)

    def test_exact_declared_factory_facts_and_project_default(self) -> None:
        facts = ProjectBindings(
            "app",
            ("main", "other"),
            "main",
            (FactoryBinding("generated.other", "graphql", "other"),),
        )
        snapshots = (
            module(
                "main",
                """from graphql_desk import gql
from generated.other import graphql as query
import generated.other as generated
one=gql("{ one }")
two=query("{ two }")
three=generated.graphql("{ three }")
from generated.lookalike import graphql as fake
fake("{ not_a_document }")
""",
            ),
        )
        result = resolve_project(snapshots, bindings=(facts,))
        self.assertEqual(result.diagnostics, ())
        self.assertEqual(
            [call.schema_name for call in result.calls], ["main", "other", "other"]
        )
        self.assertEqual(
            [call.binding for call in result.calls], ["one", "two", "three"]
        )
        baseline = resolve_project(
            (module("main", 'from graphql_desk import gql\ngql("{ id }")'),)
        )
        self.assertIsNone(baseline.calls[0].schema_name)

    def test_schema_initializers_constants_aliases_and_reexports(self) -> None:
        facts = ProjectBindings("app", ("main", "other"), "main")
        constants = module("constants", 'SCHEMA="other"')
        factory = module(
            "factory",
            "from graphql_desk import init_graphql as init\n"
            "from constants import SCHEMA\nquery=init(schema=SCHEMA)",
        )
        consumer = module("main", 'from factory import query as gql\ndoc=gql("{ id }")')
        result = resolve_project((constants, factory, consumer), bindings=(facts,))
        self.assertEqual(result.diagnostics, ())
        self.assertEqual(len(result.calls), 1)
        self.assertEqual(result.calls[0].schema_name, "other")
        self.assertEqual(
            {item.module for item in result.calls[0].dependencies},
            {"main", "factory", "constants"},
        )
        unconfigured = resolve_project(
            (
                module(
                    "main",
                    'import graphql_desk as desk\nquery=desk.init_graphql("unused")\n'
                    'query("{ id }")',
                ),
            )
        )
        self.assertEqual(unconfigured.diagnostics, ())
        self.assertEqual(unconfigured.calls[0].schema_name, "unused")
        keyword_baseline = resolve_project(
            (
                module(
                    "main",
                    "from graphql_desk import init_graphql\n"
                    'query=init_graphql("class")\nquery("{ id }")',
                ),
            )
        )
        self.assertEqual(keyword_baseline.diagnostics, ())
        self.assertEqual(keyword_baseline.calls[0].schema_name, "class")

    def test_configured_template_identity_resolves_stable_local_function(self) -> None:
        facts = ProjectBindings(
            "app", ("main",), "main", (FactoryBinding("templates", "custom"),)
        )
        templates = module(
            "templates", "def custom(source):\n raise RuntimeError('never execute')"
        )
        bridge = module("bridge", "from templates import custom as query")
        consumer = module("main", 'from bridge import query\nquery("{ id }")')
        result = resolve_project((templates, bridge, consumer), bindings=(facts,))
        self.assertEqual(result.diagnostics, ())
        self.assertEqual(result.calls[0].schema_name, "main")
        self.assertEqual(
            {item.module for item in result.calls[0].dependencies},
            {"templates", "bridge", "main"},
        )

    def test_ambiguous_defaults_unknown_schemas_and_dynamic_initializers_fail(
        self,
    ) -> None:
        facts = ProjectBindings("app", ("main", "other"))
        ambiguous = resolve_project(
            (module("main", 'from graphql_desk import gql\ngql("{ id }")'),),
            bindings=(facts,),
        )
        self.assertEqual(ambiguous.calls, ())
        self.assertIn("ambiguous", ambiguous.diagnostics[0].message)
        for initializer in (
            'init_graphql("missing")',
            "init_graphql(dynamic)",
            'init_graphql("main", "other")',
            "init_graphql(*schemas)",
            'init_graphql(schema="main", other=1)',
            'init_graphql("bad-name")',
        ):
            with self.subTest(initializer=initializer):
                result = resolve_project(
                    (
                        module(
                            "main",
                            "from graphql_desk import init_graphql\nquery="
                            + initializer
                            + '\nquery("{ id }")',
                        ),
                    ),
                    bindings=(facts,),
                )
                self.assertEqual(result.calls, ())
                self.assertTrue(result.diagnostics)
        explicit = resolve_project(
            (
                module(
                    "main",
                    "from graphql_desk import init_graphql\n"
                    'query=init_graphql("other")\nquery("{ id }")',
                ),
            ),
            bindings=(facts,),
        )
        self.assertEqual(explicit.diagnostics, ())
        self.assertEqual(explicit.calls[0].schema_name, "other")

    def test_binding_facts_are_validated_before_resolution(self) -> None:
        invalid = (
            (ProjectBindings("app", ()),),
            (ProjectBindings("app", ("main", "main")),),
            (ProjectBindings("app", ("bad-name",)),),
            (ProjectBindings("app", ("class",)),),
            (ProjectBindings("app", ("main",), "missing"),),
            (
                ProjectBindings(
                    "app", ("main",), factories=(FactoryBinding("a", "g", "missing"),)
                ),
            ),
            (
                ProjectBindings(
                    "app", ("main",), factories=(FactoryBinding("a-b", "g"),)
                ),
            ),
            (
                ProjectBindings(
                    "app", ("main",), factories=(FactoryBinding("a", "class"),)
                ),
            ),
            (
                ProjectBindings(
                    "app",
                    ("main",),
                    factories=(FactoryBinding("a", "g"), FactoryBinding("a", "g")),
                ),
            ),
            (
                ProjectBindings(
                    "app",
                    ("main",),
                    factories=(FactoryBinding("templates", "nested.gql"),),
                ),
            ),
            (ProjectBindings("app", ("main",)), ProjectBindings("app", ("main",))),
        )
        for bindings in invalid:
            with self.subTest(bindings=bindings), self.assertRaises(ValueError):
                resolve_project((), bindings=bindings)

    def test_declared_factories_cannot_bypass_invalid_missing_or_mutated_exports(
        self,
    ) -> None:
        facts = ProjectBindings(
            "app", ("main",), "main", (FactoryBinding("templates", "custom"),)
        )
        consumer = module("main", 'from templates import custom\ncustom("{ id }")')
        for source in (
            "def broken(",
            "# missing export",
            "def replace(fn): return 'not callable'\n@replace\n"
            "def custom(source): return source",
            'custom="not callable"',
            "def custom(source): return source\ncustom=other",
            "def custom(source): return source\ncustom.__code__=other.__code__",
        ):
            with self.subTest(source=source):
                result = resolve_project(
                    (module("templates", source), consumer), bindings=(facts,)
                )
                self.assertEqual(result.calls, ())
                self.assertTrue(result.diagnostics)
        mutated = resolve_project(
            (
                module(
                    "main",
                    "from generated.main import graphql as query\n"
                    'query.__code__=other.__code__\nquery("{ id }")',
                ),
            ),
            bindings=(
                ProjectBindings(
                    "app",
                    ("main",),
                    "main",
                    (FactoryBinding("generated.main", "graphql", "main"),),
                ),
            ),
        )
        self.assertEqual(mutated.calls, ())
        self.assertTrue(mutated.diagnostics)

    def test_project_factory_and_schema_facts_remain_isolated(self) -> None:
        source = 'from generated.schema import graphql\ngraphql("{ id }")'
        result = resolve_project(
            (
                module("main", source, project="one"),
                module("main", source, project="two"),
            ),
            bindings=(
                ProjectBindings(
                    "one",
                    ("a",),
                    "a",
                    (FactoryBinding("generated.schema", "graphql", "a"),),
                ),
                ProjectBindings(
                    "two",
                    ("b",),
                    "b",
                    (FactoryBinding("generated.schema", "graphql", "b"),),
                ),
            ),
        )
        self.assertEqual(result.diagnostics, ())
        self.assertEqual([call.schema_name for call in result.calls], ["a", "b"])

    def test_local_declared_factory_mutation_and_dynamic_exports_remain_guarded(
        self,
    ) -> None:
        facts = ProjectBindings(
            "app", ("main",), "main", (FactoryBinding("templates", "custom"),)
        )
        templates = module(
            "templates",
            "def custom(source): return source\ncustom.__code__=other.__code__\n"
            'custom("{ id }")',
        )
        result = resolve_project((templates,), bindings=(facts,))
        self.assertEqual(result.calls, ())
        self.assertTrue(result.diagnostics)
        package = module(
            "pkg", "def __getattr__(name): return replacement", package=True
        )
        child = module("pkg.child", 'TEXT="{ id }"')
        consumer = module(
            "main",
            "from graphql_desk import gql\nfrom pkg import child\ngql(child.TEXT)",
        )
        result = resolve_project((package, child, consumer))
        self.assertEqual(result.calls, ())
        self.assertTrue(result.diagnostics)

    def test_fragment_references_target_actual_imported_calls_in_caller_order(
        self,
    ) -> None:
        leaf = module(
            "leaf",
            'from graphql_desk import gql\nleaf=gql("fragment Leaf on User { id }")',
        )
        left = module(
            "left",
            "from graphql_desk import gql\nfrom leaf import leaf\n"
            'left=gql("fragment Left on User { ...Leaf name }", [leaf])',
        )
        bridge = module(
            "bridge", "from left import left as first\nfrom leaf import leaf as second"
        )
        consumer = module(
            "main",
            "from graphql_desk import gql\nfrom bridge import first, second\n"
            'doc=gql("query Q { user { ...Left ...Leaf } }", (first, second, first))',
        )
        result = resolve_project((consumer, bridge, left, leaf))
        self.assertEqual(result.diagnostics, ())
        self.assertEqual(len(result.calls), 3)
        by_binding = {call.binding: call for call in result.calls}
        document = by_binding["doc"]
        left_call = by_binding["left"]
        leaf_call = by_binding["leaf"]
        self.assertEqual(document.source, "query Q { user { ...Left ...Leaf } }")
        self.assertEqual(
            [reference.target for reference in document.fragments],
            [
                DocumentIdentity(left_call.module, left_call.range),
                DocumentIdentity(leaf_call.module, leaf_call.range),
                DocumentIdentity(left_call.module, left_call.range),
            ],
        )
        self.assertEqual(
            [
                consumer.source[ref.range.start : ref.range.end]
                for ref in document.fragments
            ],
            ["first", "second", "first"],
        )
        self.assertTrue(
            all(ref.reference_module == document.module for ref in document.fragments)
        )
        self.assertEqual(
            {item.module for item in document.dependencies},
            {"main", "bridge", "left", "leaf"},
        )
        self.assertEqual(
            left_call.fragments[0].target,
            DocumentIdentity(leaf_call.module, leaf_call.range),
        )
        updated = resolve_project(
            (
                consumer,
                bridge,
                left,
                replace(
                    leaf, version=2, source=leaf.source.replace("{ id }", "{ name }")
                ),
            )
        )
        updated_document = next(call for call in updated.calls if call.binding == "doc")
        self.assertEqual(updated_document.fragments[1].target.module.version, 2)
        self.assertIn(2, {item.version for item in updated_document.dependencies})

    def test_fragment_references_support_qualified_and_inline_document_values(
        self,
    ) -> None:
        fragments = module(
            "fragments",
            'from graphql_desk import gql\nfields=gql("fragment F on User { id }")',
        )
        consumer = module(
            "main",
            "from graphql_desk import gql\nimport fragments as f\n"
            'doc=gql("{ user { ...F ...G } }", [f.fields, '
            'gql("fragment G on User { name }")])',
        )
        result = resolve_project((consumer, fragments))
        self.assertEqual(result.diagnostics, ())
        self.assertEqual(len(result.calls), 3)
        identities = {
            DocumentIdentity(call.module, call.range) for call in result.calls
        }
        document = next(call for call in result.calls if call.binding == "doc")
        self.assertEqual(len(document.fragments), 2)
        self.assertTrue(all(ref.target in identities for ref in document.fragments))
        self.assertEqual(document.fragments[0].target.module.module, "fragments")
        self.assertEqual(document.fragments[1].target.module.module, "main")

    def test_fragment_reference_resolution_rejects_dynamic_values_and_cycles(
        self,
    ) -> None:
        for reference in ('"not a document"', "make()", "*items", "missing"):
            result = resolve_project(
                (
                    module(
                        "main",
                        'from graphql_desk import gql\ndoc=gql("{ id }", ['
                        + reference
                        + "])",
                    ),
                )
            )
            with self.subTest(reference=reference):
                self.assertEqual(result.calls, ())
                self.assertTrue(result.diagnostics)
        for source in (
            'from graphql_desk import gql\nf=gql("fragment F on User { ...F }", [f])',
            'from graphql_desk import gql\nitems=[]\ndoc=gql("{ id }", items)',
        ):
            result = resolve_project((module("main", source),))
            self.assertEqual(result.calls, ())
            self.assertTrue(result.diagnostics)
        a = module(
            "a",
            "from graphql_desk import gql\nfrom b import B\n"
            'A=gql("fragment A on User { ...B }", [B])',
        )
        b = module(
            "b",
            "from graphql_desk import gql\nfrom a import A\n"
            'B=gql("fragment B on User { ...A }", [A])',
        )
        result = resolve_project((a, b))
        self.assertEqual(result.calls, ())
        self.assertTrue(any("cycle" in item.message for item in result.diagnostics))

    def test_fragment_schema_identity_is_checked_and_unknown_metadata_propagates(
        self,
    ) -> None:
        source = """from graphql_desk import gql, init_graphql
a=init_graphql('a')
b=init_graphql('b')
fragment=a('fragment F on User { id }')
unknown=gql('fragment U on User { name }')
valid=a('{ user { ...F ...U } }', [fragment, unknown])
inherited=gql('{ user { ...F } }', [fragment])
invalid=b('{ user { ...F } }', [fragment])
"""
        result = resolve_project((module("main", source),))
        by_binding = {call.binding: call for call in result.calls}
        self.assertNotIn("invalid", by_binding)
        self.assertEqual(by_binding["valid"].schema_name, "a")
        self.assertEqual(by_binding["inherited"].schema_name, "a")
        self.assertIsNone(by_binding["unknown"].schema_name)
        self.assertTrue(
            any("different schema" in item.message for item in result.diagnostics)
        )

    def test_document_consumption_does_not_invalidate_static_authoring(self) -> None:
        source = """from graphql_desk import gql
fragment=gql('fragment F on User { id }')
document=gql('query Q { user { ...F } }', [fragment])
client.execute(document)
client.execute(document.ast)
"""
        result = resolve_project((module("main", source),))
        self.assertEqual(result.diagnostics, ())
        self.assertEqual(len(result.calls), 2)
        self.assertEqual(len(result.calls[1].fragments), 1)
        for mutation in (
            "fragment.ast.definitions=()",
            "tree=fragment.ast; tree.definitions=()",
            "items=[fragment]; items[0].ast.definitions=()",
            "items=[*[fragment]]; items[0].ast.definitions=()",
            "items={'f': fragment}; items['f'].ast.definitions=()",
            "setattr(fragment.ast, 'definitions', ())",
            "put=setattr; put(fragment.ast, 'definitions', ())",
        ):
            mutated = source.replace("client.execute(document)", mutation)
            with self.subTest(mutation=mutation):
                invalid = resolve_project((module("main", mutated),))
                self.assertEqual(invalid.calls, ())
                self.assertTrue(invalid.diagnostics)

    def test_python_reference_records_do_not_claim_graphql_fragment_validation(
        self,
    ) -> None:
        source = """from graphql_desk import gql
operation=gql('query NotAFragment { unknownField }')
document=gql('{ otherUnknownField }', [operation])
"""
        result = resolve_project((module("main", source),))
        self.assertEqual(result.diagnostics, ())
        self.assertEqual(len(result.calls), 2)
        self.assertEqual(len(result.calls[1].fragments), 1)

    def test_failed_module_traversal_discards_cached_dependent_references(self) -> None:
        fragment = module(
            "fragment",
            'from graphql_desk import gql\nf=gql("fragment F on User { id }")\n'
            "deep=gql(" + "+".join(['"x"'] * 550) + ")",
        )
        consumer = module(
            "main",
            "from graphql_desk import gql\nfrom fragment import f\n"
            'doc=gql("{ user { ...F } }", [f])',
        )
        independent = module(
            "independent", 'from graphql_desk import gql\nok=gql("{ ok }")'
        )
        result = resolve_project((consumer, fragment, independent))
        self.assertEqual([call.binding for call in result.calls], ["ok"])
        self.assertIn("source-too-complex", {item.code for item in result.diagnostics})
        self.assertEqual([call.binding for call in result.unavailable], ["doc"])
        rejected = result.unavailable[0]
        self.assertEqual(rejected.module.uri, consumer.uri)
        self.assertTrue(rejected.diagnostics)
        self.assertTrue(
            all(cause.module.uri == fragment.uri for cause in rejected.diagnostics)
        )
        resolver = IncrementalResolver()
        self.assertEqual(resolver.resolve((consumer, fragment, independent)), result)
        self.assertEqual(resolver.resolve((consumer, fragment, independent)), result)

    def test_imported_document_mutation_and_diagnostic_probes_create_no_references(
        self,
    ) -> None:
        fragment = module(
            "fragment",
            'from graphql_desk import gql\nf=gql("fragment F on User { id }")',
        )
        consumer = module(
            "main",
            "from graphql_desk import gql\nfrom fragment import f\n"
            'f.ast.definitions=()\ndoc=gql("{ user { ...F } }", [f])',
        )
        result = resolve_project((consumer, fragment))
        self.assertEqual(result.calls, ())
        self.assertTrue(result.diagnostics)
        consumer = module(
            "main",
            "from generated.main import graphql as query\nfrom fragment import f\n"
            'doc=query("{ user { ...F } }", [f])',
        )
        facts = ProjectBindings(
            "app",
            ("main",),
            factories=(FactoryBinding("generated.main", "graphql", "main"),),
        )
        result = resolve_project((consumer, fragment), bindings=(facts,))
        self.assertEqual(result.calls, ())
        self.assertTrue(any("ambiguous" in item.message for item in result.diagnostics))

    def test_imported_ast_projection_mutation_traces_document_origin(self) -> None:
        fragment = module(
            "fragment",
            'from graphql_desk import gql\nf=gql("fragment F on User { id }")\n'
            "tree=f.ast",
        )
        consumer = module(
            "main",
            "from graphql_desk import gql\nfrom fragment import f, tree\n"
            'tree.definitions=()\ndoc=gql("{ user { ...F } }", [f])',
        )
        result = resolve_project((consumer, fragment))
        self.assertEqual(result.calls, ())
        self.assertTrue(result.diagnostics)


class IncrementalResolverTests(unittest.TestCase):
    def test_parse_and_project_reuse_with_exact_input_identity(self) -> None:
        resolver = IncrementalResolver()
        first = module("main", 'from graphql_desk import gql\ndoc = gql("{ id }")')
        other = replace(first, project="other", uri="file:///other.py")
        inputs = (first, other)
        expected = resolve_project(inputs)
        with (
            patch.object(
                source_graph, "_parse", wraps=getattr(source_graph, "_parse")
            ) as parse,
            patch.object(
                source_graph, "_resolve", wraps=getattr(source_graph, "_resolve")
            ) as resolve,
        ):
            self.assertEqual(resolver.resolve(inputs), expected)
            self.assertEqual((parse.call_count, resolve.call_count), (2, 2))
            self.assertEqual(resolver.resolve(inputs), expected)
            self.assertEqual((parse.call_count, resolve.call_count), (2, 2))
            changed = replace(first, source=first.source.replace("id", "name"))
            result = resolver.resolve((changed, other))
            self.assertEqual((parse.call_count, resolve.call_count), (3, 3))
            self.assertEqual(result.calls[0].source, "{ name }")
            # Older inputs are authoritative too; chronology belongs to the caller.
            self.assertEqual(resolver.resolve(inputs), expected)
            self.assertEqual((parse.call_count, resolve.call_count), (4, 4))
            resolver.clear()
            self.assertEqual(resolver.resolve(inputs), expected)
            self.assertEqual((parse.call_count, resolve.call_count), (6, 6))

    def test_unchanged_files_reparse_never_but_recompute_in_changed_project(
        self,
    ) -> None:
        resolver = IncrementalResolver()
        constants = module("constants", 'TEXT = "{ id }"')
        consumer = module(
            "consumer",
            "from constants import TEXT\nfrom graphql_desk import gql\ndoc = gql(TEXT)",
        )
        resolver.resolve((constants, consumer))
        with (
            patch.object(
                source_graph, "_parse", wraps=getattr(source_graph, "_parse")
            ) as parse,
            patch.object(
                source_graph, "_resolve", wraps=getattr(source_graph, "_resolve")
            ) as resolve,
        ):
            changed = replace(constants, source='TEXT = "{ name }"')
            result = resolver.resolve((changed, consumer))
            self.assertEqual(parse.call_count, 1)
            self.assertEqual(resolve.call_args.args[0], (changed, consumer))
            self.assertEqual(result, resolve_project((changed, consumer)))

    def test_binding_changes_recompute_without_parsing(self) -> None:
        resolver = IncrementalResolver()
        consumer = module(
            "main", 'from generated import graphql\ndoc = graphql("{ id }")'
        )
        first = ProjectBindings(
            "app",
            ("one", "two"),
            factories=(FactoryBinding("generated", "graphql", "one"),),
        )
        second = replace(
            first, factories=(FactoryBinding("generated", "graphql", "two"),)
        )
        resolver.resolve((consumer,), bindings=(first,))
        with (
            patch.object(
                source_graph, "_parse", wraps=getattr(source_graph, "_parse")
            ) as parse,
            patch.object(
                source_graph, "_resolve", wraps=getattr(source_graph, "_resolve")
            ) as resolve,
        ):
            result = resolver.resolve((consumer,), bindings=(second,))
            self.assertEqual((parse.call_count, resolve.call_count), (0, 1))
            self.assertEqual(result.calls[0].schema_name, "two")
        self.assertEqual(result, resolve_project((consumer,), bindings=(second,)))

    def test_negative_lookups_mutation_failures_and_reexports_recover(self) -> None:
        resolver = IncrementalResolver()
        consumer = module(
            "consumer", "from bridge import query, TEXT\ndoc = query(TEXT)"
        )
        bridge = module(
            "bridge", 'from graphql_desk import gql as query\nTEXT = "{ id }"'
        )
        mutation = module(
            "mutation", "import graphql_desk as desk\ndesk.gql = object()"
        )
        invalid = replace(bridge, source="def (")
        replacement = replace(
            bridge, source='from graphql_desk import gql as query\nTEXT = "{ name }"'
        )
        shadow = module("graphql_desk", "gql = object()")
        for inputs in (
            (consumer,),
            (consumer, bridge),
            (consumer, bridge, mutation),
            (consumer, bridge),
            (consumer, invalid),
            (consumer, replacement),
            (consumer, replacement, shadow),
            (consumer, replacement),
            (),
            (consumer, bridge),
        ):
            with self.subTest(inputs=inputs):
                self.assertEqual(resolver.resolve(inputs), resolve_project(inputs))

    def test_ordering_versions_packages_and_failed_parse_reuse(self) -> None:
        resolver = IncrementalResolver()
        good = module("main", 'from graphql_desk import gql\na = gql("{ id }")')
        dynamic = module("dynamic", "from graphql_desk import gql\na = gql(unknown)")
        invalid = module("broken", "def (")
        other = replace(good, project="other", uri="file:///other.py")
        other_bad = replace(invalid, project="other", uri="file:///other-bad.py")
        initial = (dynamic, other, invalid, good, other_bad)
        for inputs in (
            initial,
            tuple(reversed(initial)),
            (replace(good, version=2), other),
            (replace(good, uri="file:///new.py", is_package=True), other),
            initial,
        ):
            self.assertEqual(resolver.resolve(inputs), resolve_project(inputs))
        with patch.object(
            source_graph, "_parse", wraps=getattr(source_graph, "_parse")
        ) as parse:
            self.assertEqual(resolver.resolve(initial), resolve_project(initial))
            # The cold comparison parses; the cached request itself does not.
            self.assertEqual(parse.call_count, len(initial))

    def test_invalid_request_does_not_replace_cache(self) -> None:
        resolver = IncrementalResolver()
        item = module("main", 'from graphql_desk import gql\na = gql("{ id }")')
        expected = resolver.resolve((item,))
        for inputs, bindings in (
            ((item, item), ()),
            ((replace(item, module="bad-name"),), ()),
            ((item,), (ProjectBindings("unused", ()),)),
            ((item,), (ProjectBindings("unused", ("one",)),) * 2),
        ):
            with self.subTest(inputs=inputs, bindings=bindings):
                with self.assertRaises(ValueError) as cold:
                    resolve_project(inputs, bindings=bindings)
                with self.assertRaisesRegex(ValueError, str(cold.exception)):
                    resolver.resolve(inputs, bindings=bindings)
        with (
            patch.object(
                source_graph, "_parse", wraps=getattr(source_graph, "_parse")
            ) as parse,
            patch.object(
                source_graph, "_resolve", wraps=getattr(source_graph, "_resolve")
            ) as resolve,
        ):
            self.assertEqual(resolver.resolve((item,)), expected)
            self.assertEqual((parse.call_count, resolve.call_count), (0, 0))

    def test_failed_dependency_traversal_recovers_without_mutable_state_leaks(
        self,
    ) -> None:
        resolver = IncrementalResolver()
        fragment = module(
            "fragment",
            'from graphql_desk import gql\nf=gql("fragment F on User { id }")',
        )
        broken = replace(
            fragment,
            source=fragment.source + "\ndeep=gql(" + "+".join(['"x"'] * 550) + ")",
        )
        consumer = module(
            "main",
            "from graphql_desk import gql\nfrom fragment import f\n"
            'doc=gql("{ user { ...F } }", [f])',
        )
        for inputs in (
            (consumer, fragment),
            (consumer, broken),
            (consumer, broken),
            (consumer, fragment),
        ):
            self.assertEqual(resolver.resolve(inputs), resolve_project(inputs))

    def test_failed_update_is_atomic_and_removed_parses_are_not_retained(self) -> None:
        resolver = IncrementalResolver()
        original = module("main", 'from graphql_desk import gql\ndoc=gql("{ id }")')
        changed = replace(original, source=original.source.replace("id", "name"))
        expected = resolver.resolve((original,))
        with patch.object(
            source_graph, "_resolve", side_effect=RuntimeError("interrupted")
        ):
            with self.assertRaisesRegex(RuntimeError, "interrupted"):
                resolver.resolve((changed,))
        with (
            patch.object(
                source_graph, "_parse", wraps=getattr(source_graph, "_parse")
            ) as parse,
            patch.object(
                source_graph, "_resolve", wraps=getattr(source_graph, "_resolve")
            ) as resolve,
        ):
            self.assertEqual(resolver.resolve((original,)), expected)
            self.assertEqual((parse.call_count, resolve.call_count), (0, 0))
            resolver.resolve(())
            self.assertEqual(resolver.resolve((original,)), expected)
            self.assertEqual((parse.call_count, resolve.call_count), (1, 1))
