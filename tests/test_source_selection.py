"""Finite source selection shares editor and CLI path policy."""

import tempfile
import unittest
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from unittest.mock import patch

from graphql_desk.config import ProjectConfig, parse_project_config
from graphql_desk.source_graph import ModuleSnapshot, resolve_project
from graphql_desk.source_selection import select_project_sources

_BASE = """[tool.graphql-desk]
schema="schema.graphql"
schema_output="graphql_desk_generated/default.pyi"
"""


class SourceSelectionTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()

    def config(self, extra: str = "", root: Path | None = None) -> ProjectConfig:
        directory = root or self.root
        directory.mkdir(parents=True, exist_ok=True)
        config = parse_project_config(
            _BASE + extra, config_path=directory / "pyproject.toml"
        )
        assert config is not None
        return config

    def file(self, relative: str) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("raise RuntimeError('must never execute')", encoding="utf-8")
        return path

    def symlink(self, link: Path, target: Path) -> None:
        try:
            link.symlink_to(target, target_is_directory=target.is_dir())
        except OSError as error:
            self.skipTest(f"Host does not permit test symlinks: {error}")

    def test_defaults_include_root_and_nested_files_without_reading_source(
        self,
    ) -> None:
        config = self.config()
        paths = tuple(
            self.file(p) for p in ("q.py", "pkg/q.py", "pkg/__init__.py", "q.txt")
        )
        with patch.object(
            Path, "read_text", side_effect=AssertionError("no source read")
        ):
            result = select_project_sources(config, paths)
        self.assertEqual(
            [(s.module, s.is_package) for s in result],
            [("pkg", True), ("pkg.q", False), ("q", False)],
        )
        with self.assertRaises(FrozenInstanceError):
            setattr(result[0], "module", "changed")
        records = tuple(
            ModuleSnapshot("p", s.module, s.path.as_uri(), 1, "", s.is_package)
            for s in result
        )
        self.assertEqual(resolve_project(records).diagnostics, ())

    def test_default_exclusions_and_owned_outputs_cannot_be_reincluded(self) -> None:
        config = self.config()
        hidden = (
            ".git/q.py",
            ".venv/q.py",
            "venv/q.py",
            "pkg/__pycache__/q.py",
            "site-packages/q.py",
            "build/q.py",
            "dist/q.py",
        )
        self.assertEqual(
            select_project_sources(config, tuple(self.file(p) for p in hidden)), ()
        )
        config = self.config('include=["**"]\nexclude=[]\n')
        paths = tuple(
            self.file(p)
            for p in (
                "graphql_desk_generated/new.py",
                "build/graphql-desk/omitted/q.py",
                "keep.py",
            )
        )
        self.assertEqual(
            [s.module for s in select_project_sources(config, paths)], ["keep"]
        )

    def test_component_globs_are_full_and_case_sensitive(self) -> None:
        config = self.config('include=["src/**/q?.py"]\nexclude=["**/blocked/**"]\n')
        paths = tuple(
            self.file(p)
            for p in (
                "src/q1.py",
                "src/a/q2.py",
                "src/a/b/q3.py",
                "src/Q4.py",
                "src/q12.py",
                "other/src/q5.py",
                "src/blocked/q6.py",
            )
        )
        result = select_project_sources(config, paths)
        self.assertEqual(
            {s.module for s in result}, {"src.q1", "src.a.q2", "src.a.b.q3"}
        )
        star = replace(config, include=("src/*.py",), exclude=())
        self.assertEqual(
            {s.module for s in select_project_sources(star, paths)},
            {"src.q1", "src.Q4", "src.q12"},
        )
        for pattern in ("src/q[1].py", "src/q{1}.py", "!src/q1.py", "src/q(1).py"):
            self.assertEqual(
                select_project_sources(replace(config, include=(pattern,)), paths), ()
            )

    def test_relative_candidates_unsaved_files_and_canonical_dedup(self) -> None:
        config = self.config()
        path = self.file("pkg/q.py")
        result = select_project_sources(
            config, (Path("pkg/q.py"), path, Path("pkg/../pkg/q.py"), Path("new.py"))
        )
        self.assertEqual({s.module for s in result}, {"pkg.q", "new"})
        self.assertFalse((self.root / "new.py").exists())
        with self.assertRaisesRegex(ValueError, "file"):
            select_project_sources(config, (path.parent,))

    def test_module_names_root_initializer_and_package_collision(self) -> None:
        config = self.config()
        for value in (
            "bad-name.py",
            "class.py",
            "\U0001d469ook.py",
            "pkg/bad-name/q.py",
            "__init__.py",
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                select_project_sources(config, (self.file(value),))
        valid = select_project_sources(config, (self.file("caf\u00e9.py"),))
        self.assertEqual(valid[0].module, "caf\u00e9")
        with self.assertRaisesRegex(ValueError, "same Python module"):
            select_project_sources(
                config, (self.file("pkg.py"), self.file("pkg/__init__.py"))
            )

    def test_overlapping_roots_choose_first_and_distinct_roots_collide(self) -> None:
        config = self.config('source_roots=[".", "src"]\n')
        path = self.file("src/q.py")
        result = select_project_sources(config, (path,))
        self.assertEqual(result[0].module, "src.q")
        self.assertEqual(result[0].source_root, self.root)
        reverse = replace(config, source_roots=tuple(reversed(config.source_roots)))
        self.assertEqual(select_project_sources(reverse, (path,))[0].module, "q")
        separate = self.config('source_roots=["a", "b"]\n')
        with self.assertRaisesRegex(ValueError, "same Python module"):
            select_project_sources(separate, (self.file("a/q.py"), self.file("b/q.py")))

    def test_workspace_and_explicit_cli_selection(self) -> None:
        config = self.config()
        path = self.file("q.py")
        other = self.root / "workspace"
        other.mkdir()
        with self.assertRaisesRegex(ValueError, "workspace"):
            select_project_sources(config, (path,), workspace=other)
        self.assertEqual(
            len(
                select_project_sources(
                    config, (path,), workspace=other, explicit_config=True
                )
            ),
            1,
        )
        changed = replace(config, source_roots=(self.root.parent,))
        with self.assertRaisesRegex(ValueError, "roots"):
            select_project_sources(changed, (path,))

    def test_nested_ownership_inactive_and_invalid_configuration(self) -> None:
        config = self.config()
        parent = self.file("parent.py")
        child = self.file("child/q.py")
        nested = self.config(root=child.parent)
        nested.path.write_text(_BASE, encoding="utf-8")
        self.assertEqual(
            [s.module for s in select_project_sources(config, (parent, child))],
            ["parent"],
        )
        self.assertEqual(select_project_sources(nested, (child,))[0].module, "q")
        nested.path.write_text("[project]\nname='inactive'", encoding="utf-8")
        self.assertEqual(
            {s.module for s in select_project_sources(config, (parent, child))},
            {"parent", "child.q"},
        )
        nested.path.write_text("[tool.graphql-desk]\nunknown=true", encoding="utf-8")
        with self.assertRaises(ValueError):
            select_project_sources(config, (parent, child))

    def test_entrypoints_unknown_excluded_and_ambiguous(self) -> None:
        config = self.config('entrypoints=["pkg/q.py"]\n')
        path = self.file("pkg/q.py")
        self.assertEqual(len(select_project_sources(config, (path,))), 1)
        for changed in (
            replace(config, entrypoints=("missing.py",)),
            replace(config, exclude=("pkg/**",)),
        ):
            with self.assertRaisesRegex(ValueError, "Entrypoint"):
                select_project_sources(changed, (path,))
        separate = self.config('source_roots=["a", "b"]\nentrypoints=["q.py"]\n')
        with self.assertRaises(ValueError):
            select_project_sources(separate, (self.file("a/q.py"), self.file("b/q.py")))

    def test_symlink_alias_escape_and_retarget_are_rechecked(self) -> None:
        config = self.config('source_roots=["src"]\n')
        source = self.file("src/q.py")
        alias = source.parent / "alias.py"
        self.symlink(alias, source)
        self.assertEqual(len(select_project_sources(config, (source, alias))), 1)
        outside = self.file("outside.py")
        alias.unlink()
        self.symlink(alias, outside)
        with self.assertRaisesRegex(ValueError, "symlink"):
            select_project_sources(config, (alias,))
        alias.unlink()
        self.symlink(alias, source.parent / "missing.py")
        with self.assertRaisesRegex(ValueError, "broken"):
            select_project_sources(config, (alias,))

    def test_root_file_rejects_and_generated_symlink_alias_excludes(self) -> None:
        config = self.config("exclude=[]\n")
        path = self.file("q.py")
        with self.assertRaisesRegex(ValueError, "directories"):
            select_project_sources(replace(config, source_roots=(path,)), (path,))
        self.assertEqual(
            select_project_sources(
                replace(config, source_roots=(self.root / "new.py",)),
                (self.root / "new.py",),
            ),
            (),
        )
        output = self.file("graphql_desk_generated/default.py")
        alias = self.root / "alias.py"
        self.symlink(alias, output)
        self.assertEqual(select_project_sources(config, (alias,)), ())


if __name__ == "__main__":
    unittest.main()
