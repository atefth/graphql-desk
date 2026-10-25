"""Static capture for external mapping type availability."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from graphql_desk.external_type_sources import (
    capture_external_type_sources,
    interpreter_type_roots,
)
from graphql_desk.source_graph import resolve_type_symbols


class ExternalTypeSourcesTests(unittest.TestCase):
    def test_interpreter_roots_exclude_workspace_and_keep_unique_canonical_paths(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            environment = root / "environment"
            workspace.mkdir()
            environment.mkdir()
            roots = interpreter_type_roots(
                (str(workspace), str(environment), str(environment)),
                excluded_roots=(workspace,),
            )
        self.assertEqual(roots, (environment,))

    def test_captures_stub_class_and_package_reexport_without_execution(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = root / "vendor_types"
            package.mkdir()
            (package / "__init__.pyi").write_text(
                "from .values import Date\n", encoding="utf-8"
            )
            (package / "values.pyi").write_text(
                "raise RuntimeError('must not execute')\nclass Date: ...\n",
                encoding="utf-8",
            )

            modules = capture_external_type_sources(
                ("vendor_types.Date",),
                search_roots=(root,),
                project_id="environment",
            )

        self.assertEqual(
            {item.module for item in modules}, {"vendor_types", "vendor_types.values"}
        )
        self.assertEqual(
            resolve_type_symbols(
                ("vendor_types.Date",), modules=modules, project_id="environment"
            )[0].status,
            "resolved",
        )

    def test_workspace_module_candidate_blocks_unrelated_interpreter_package(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            environment = root / "site-packages"
            workspace.mkdir()
            environment.mkdir()
            (workspace / "vendor_types.py").write_text(
                "class ProjectDate: ...\n", encoding="utf-8"
            )
            (environment / "vendor_types.pyi").write_text(
                "class InstalledDate: ...\n", encoding="utf-8"
            )
            modules = capture_external_type_sources(
                ("vendor_types.InstalledDate",),
                search_roots=(environment,),
                project_id="environment",
                shadow_roots=(workspace,),
            )
        self.assertEqual(modules, ())

    def test_missing_external_symbol_remains_unverified(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            modules = capture_external_type_sources(
                ("not_installed.Type",),
                search_roots=(Path(directory),),
                project_id="environment",
            )
        self.assertEqual(modules, ())
        self.assertEqual(
            resolve_type_symbols(
                ("not_installed.Type",), modules=modules, project_id="environment"
            )[0].status,
            "external",
        )

    def test_pep561_stub_only_package_supersedes_runtime_package(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = root / "foo"
            runtime.mkdir()
            (runtime / "__init__.py").write_text(
                "from .bar import Type\n", encoding="utf-8"
            )
            (runtime / "bar.py").write_text(
                "class Type:\n    runtime_marker: int\n", encoding="utf-8"
            )
            stubs = root / "foo-stubs"
            stubs.mkdir()
            stub_init = stubs / "__init__.pyi"
            stub_init.write_text("from .bar import Type\n", encoding="utf-8")
            stub_module = stubs / "bar.pyi"
            stub_module.write_text(
                "class Type:\n    stub_marker: str\n", encoding="utf-8"
            )

            modules = capture_external_type_sources(
                ("foo.bar.Type",), search_roots=(root,), project_id="environment"
            )

        self.assertEqual({item.module for item in modules}, {"foo", "foo.bar"})
        self.assertEqual(
            {item.module: item.uri for item in modules},
            {
                "foo": stub_init.resolve().as_uri(),
                "foo.bar": stub_module.resolve().as_uri(),
            },
        )
        self.assertIn("stub_marker: str", modules[1].source)
        self.assertNotIn("runtime_marker", "\n".join(item.source for item in modules))
        self.assertEqual(
            resolve_type_symbols(
                ("foo.bar.Type",), modules=modules, project_id="environment"
            )[0].status,
            "resolved",
        )

    def test_pep561_stub_only_nested_package(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stubs = root / "foo-stubs"
            nested = stubs / "bar"
            nested.mkdir(parents=True)
            root_init = stubs / "__init__.pyi"
            nested_init = nested / "__init__.pyi"
            root_init.write_text("from .bar import Type\n", encoding="utf-8")
            nested_init.write_text("class Type: ...\n", encoding="utf-8")

            modules = capture_external_type_sources(
                ("foo.bar.Type",), search_roots=(root,), project_id="environment"
            )

        self.assertEqual(
            {item.module: item.uri for item in modules},
            {
                "foo": root_init.resolve().as_uri(),
                "foo.bar": nested_init.resolve().as_uri(),
            },
        )
        self.assertEqual(
            resolve_type_symbols(
                ("foo.bar.Type",), modules=modules, project_id="environment"
            )[0].status,
            "resolved",
        )

    def test_complete_stub_package_does_not_fall_back_to_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = root / "foo"
            runtime.mkdir()
            (runtime / "bar.py").write_text(
                "class Type:\n    runtime_marker: int\n", encoding="utf-8"
            )
            stubs = root / "foo-stubs"
            stubs.mkdir()
            (stubs / "__init__.pyi").write_text(
                "from .bar import Type\n", encoding="utf-8"
            )

            modules = capture_external_type_sources(
                ("foo.bar.Type",), search_roots=(root,), project_id="environment"
            )

        self.assertEqual({item.module for item in modules}, {"foo"})
        self.assertNotIn("runtime_marker", "\n".join(item.source for item in modules))

    def test_partial_stub_package_may_fall_back_to_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = root / "foo"
            runtime.mkdir()
            runtime_module = runtime / "bar.py"
            runtime_module.write_text(
                "class Type:\n    runtime_marker: int\n", encoding="utf-8"
            )
            stubs = root / "foo-stubs"
            stubs.mkdir()
            (stubs / "__init__.pyi").write_text(
                "from .bar import Type\n", encoding="utf-8"
            )
            (stubs / "py.typed").write_text("partial\n", encoding="utf-8")

            modules = capture_external_type_sources(
                ("foo.bar.Type",), search_roots=(root,), project_id="environment"
            )

        self.assertEqual(
            {item.module: item.uri for item in modules}["foo.bar"],
            runtime_module.resolve().as_uri(),
        )
        self.assertIn("runtime_marker: int", modules[1].source)

    def test_namespace_only_stub_root_does_not_fall_back_to_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = root / "foo"
            runtime.mkdir()
            (runtime / "bar.py").write_text("class Type: ...\n", encoding="utf-8")
            namespace_stubs = root / "foo-stubs"
            namespace_stubs.mkdir()
            (namespace_stubs / "bar.pyi").write_text(
                "class Type: ...\n", encoding="utf-8"
            )

            modules = capture_external_type_sources(
                ("foo.bar.Type",), search_roots=(root,), project_id="environment"
            )

        self.assertEqual(modules, ())

    def test_invalid_or_ambiguous_external_modules_fail_closed(self) -> None:
        with (
            tempfile.TemporaryDirectory() as first,
            tempfile.TemporaryDirectory() as second,
        ):
            first_root, second_root = Path(first), Path(second)
            (first_root / "external.py").write_text(
                "class Type: ...\n", encoding="utf-8"
            )
            (second_root / "external.py").write_text(
                "class Type: ...\n", encoding="utf-8"
            )
            with self.assertRaisesRegex(ValueError, "qualified canonical"):
                capture_external_type_sources(
                    ("external.bad-name",),
                    search_roots=(first_root,),
                    project_id="environment",
                )
            with self.assertRaisesRegex(ValueError, "Ambiguous external type module"):
                capture_external_type_sources(
                    ("external.Type",),
                    search_roots=(first_root, second_root),
                    project_id="environment",
                )

    def test_external_import_traversal_is_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = root / "external"
            package.mkdir()
            (package / "__init__.pyi").write_text(
                "from . import child\nclass Type: ...\n", encoding="utf-8"
            )
            (package / "child.pyi").write_text("VALUE: int\n", encoding="utf-8")
            with patch("graphql_desk.external_type_sources._MAX_MODULES", 1):
                with self.assertRaisesRegex(ValueError, "source limit"):
                    capture_external_type_sources(
                        ("external.Type",),
                        search_roots=(root,),
                        project_id="environment",
                    )


if __name__ == "__main__":
    unittest.main()
