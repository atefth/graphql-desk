"""Filesystem-only guard checks; no tool provisioning or evaluation runs."""

import importlib.util
import tempfile
import unittest
from collections.abc import Callable
from pathlib import Path
from typing import cast

_SCRIPT = Path(__file__).resolve().with_name("evaluate.py")
_SPEC = importlib.util.spec_from_file_location("saved_evaluation", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)
_TASK_ROOT = cast(Callable[[Path], Path], _MODULE.checked_task_root)


class GuardTests(unittest.TestCase):
    def setUp(self) -> None:
        # Only new harmless fixtures under the real repository's checked .tmp parent.
        parent = _TASK_ROOT(_SCRIPT.parents[2]).parent
        parent.mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(
            prefix="issue85-guard-", dir=parent
        )
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.repository = self.root / "repository"
        self.repository.mkdir()

    def link(self, target: Path, link: Path) -> None:
        try:
            link.symlink_to(target, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("Directory symlink creation is unavailable")

    def test_missing_task_is_resolved_without_creating_it(self) -> None:
        selected = _TASK_ROOT(self.repository)
        self.assertEqual(selected, self.repository.resolve() / ".tmp" / "issue85-eval")
        self.assertFalse((self.repository / ".tmp").exists())

    def test_existing_normal_task_is_allowed_without_modifying_its_data(self) -> None:
        task = self.repository / ".tmp" / "issue85-eval"
        task.mkdir(parents=True)
        marker = task / "keep.txt"
        marker.write_text("existing data", encoding="utf-8")
        self.assertEqual(_TASK_ROOT(self.repository), task.resolve())
        self.assertEqual(marker.read_text(encoding="utf-8"), "existing data")
        self.assertEqual(list(task.iterdir()), [marker])

    def test_redirected_parent_cannot_escape_repository(self) -> None:
        outside = self.root / "outside"
        outside.mkdir()
        self.link(outside, self.repository / ".tmp")
        with self.assertRaisesRegex(ValueError, "redirected"):
            _TASK_ROOT(self.repository)
        self.assertEqual(list(outside.iterdir()), [])

    def test_redirected_task_is_refused_even_if_target_is_inside_repository(
        self,
    ) -> None:
        temporary = self.repository / ".tmp"
        temporary.mkdir()
        target = self.repository / "another-location"
        target.mkdir()
        self.link(target, temporary / "issue85-eval")
        with self.assertRaisesRegex(ValueError, "redirected"):
            _TASK_ROOT(self.repository)
        self.assertEqual(list(target.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
