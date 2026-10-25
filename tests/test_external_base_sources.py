"""Static external class-base references used by mapped project types."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from graphql_desk.external_type_sources import (
    capture_external_type_sources,
    external_class_base_symbols,
)
from graphql_desk.source_graph import ModuleSnapshot, resolve_type_symbols


class ExternalBaseSourcesTests(unittest.TestCase):
    def test_finds_only_imported_class_base_symbols(self) -> None:
        source = ModuleSnapshot(
            "app",
            "models",
            "file:///app/models.py",
            1,
            "from vendor import BaseModel as Model\n"
            "import vendor.types as types\n"
            "import vendor.deep\n"
            "from local_models import LocalBase\n"
            "class User(Model): pass\n"
            "class Record(types.RecordBase): pass\n"
            "class Event(vendor.deep.EventBase): pass\n"
            "class Local(LocalBase): pass\n"
            "class Plain: pass\n",
        )

        local_base = ModuleSnapshot(
            "app",
            "local_models",
            "file:///app/local_models.py",
            1,
            "class LocalBase: pass\n",
        )
        self.assertEqual(
            external_class_base_symbols((source, local_base)),
            (
                "vendor.BaseModel",
                "vendor.deep.EventBase",
                "vendor.types.RecordBase",
            ),
        )

    def test_captured_stub_class_proves_local_subclass_without_importing_it(
        self,
    ) -> None:
        local = ModuleSnapshot(
            "app",
            "models",
            "file:///app/models.py",
            1,
            "from vendor import BaseModel as Base\nclass Date(Base): pass\n",
        )
        self.assertEqual(external_class_base_symbols((local,)), ("vendor.BaseModel",))

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            vendor = root / "vendor"
            vendor.mkdir()
            (vendor / "__init__.pyi").write_text(
                "from .types import BaseModel\n", encoding="utf-8"
            )
            (vendor / "types.pyi").write_text(
                "raise RuntimeError('must not execute')\nclass BaseModel: ...\n",
                encoding="utf-8",
            )

            external = capture_external_type_sources(
                external_class_base_symbols((local,)),
                search_roots=(root,),
                project_id="app#interpreter",
            )
            # External modules are parsed as snapshots; their source stays inert.
            with patch(
                "builtins.__import__",
                wraps=__import__,
            ) as imported:
                project_external = tuple(
                    ModuleSnapshot(
                        "app",
                        module.module,
                        module.uri,
                        module.version,
                        module.source,
                        module.is_package,
                    )
                    for module in external
                )
            resolved = resolve_type_symbols(
                ("models.Date",),
                modules=(local, *project_external),
                project_id="app",
            )[0]
            self.assertFalse(
                any(
                    call.args[0] in {"vendor", "vendor.types"}
                    for call in imported.call_args_list
                )
            )

        self.assertEqual({item.module for item in external}, {"vendor", "vendor.types"})
        self.assertEqual(
            (resolved.status, resolved.target), ("resolved", "models.Date")
        )


if __name__ == "__main__":
    unittest.main()
