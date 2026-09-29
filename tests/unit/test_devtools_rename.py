# lup: ignore[import-re]
# ``re`` is imported only for the ``re.Pattern[str]`` annotation on the
# helper — the tests exercise the renamer's own regexes, not ad-hoc parsing.
"""Tests for the package renamer's matchers and leftover reporting.

The import matcher alone once shipped a broken downstream CLI: dotted
module-path string anchors like ``resources.files("lup_template.devtools")``
survived the rename and crashed at runtime with ``ModuleNotFoundError``.
These tests pin the string-anchor pass and the stale-reference report
that make such misses impossible or at least visible.
"""

import re
from pathlib import Path

from lup_template.devtools.dev.init import (
    PACKAGE_IMPORT_RE,
    PACKAGE_STRING_ANCHOR_RE,
    about_initialization,
    drop_stale_metadata,
    find_stale_references,
    is_renamer_module,
    rename_pattern_in_file,
)
from tests.unit.repos import initialized_repo


def apply_rename(tmp_path: Path, source: str, pattern: re.Pattern[str]) -> str:
    path = tmp_path / "mod.py"
    path.write_text(source)
    rename_pattern_in_file(path, pattern, "syra", dry_run=False)
    return path.read_text()


class TestStringAnchorMatcher:
    def test_renames_resources_files_anchor(self, tmp_path: Path) -> None:
        result = apply_rename(
            tmp_path,
            'ASSETS = resources.files("lup_template.devtools.dashboard")\n',
            PACKAGE_STRING_ANCHOR_RE,
        )
        assert result == 'ASSETS = resources.files("syra.devtools.dashboard")\n'

    def test_renames_single_quoted_entry_point_string(self, tmp_path: Path) -> None:
        result = apply_rename(
            tmp_path,
            "ENTRY = 'lup_template.devtools.main:app'\n",
            PACKAGE_STRING_ANCHOR_RE,
        )
        assert result == "ENTRY = 'syra.devtools.main:app'\n"

    def test_renames_mock_patch_target(self, tmp_path: Path) -> None:
        result = apply_rename(
            tmp_path,
            'with mock.patch("lup_template.agent.config.load"):\n    pass\n',
            PACKAGE_STRING_ANCHOR_RE,
        )
        assert 'mock.patch("syra.agent.config.load")' in result

    def test_leaves_bare_string_literal_alone(self, tmp_path: Path) -> None:
        source = 'NAME = "lup_template"\n'
        assert apply_rename(tmp_path, source, PACKAGE_STRING_ANCHOR_RE) == source

    def test_leaves_framework_marker_lines_alone(self, tmp_path: Path) -> None:
        source = 'EP = "lup_template.devtools.main:app"  # lup-devtools\n'
        assert apply_rename(tmp_path, source, PACKAGE_STRING_ANCHOR_RE) == source

    def test_leaves_unquoted_prose_alone(self, tmp_path: Path) -> None:
        source = '"""Doc table: `from lup_template.*` becomes target imports."""\n'
        assert apply_rename(tmp_path, source, PACKAGE_STRING_ANCHOR_RE) == source


class TestImportMatcherThroughSharedCore:
    def test_renames_from_import(self, tmp_path: Path) -> None:
        result = apply_rename(
            tmp_path,
            "from lup_template.agent.config import Settings\n",
            PACKAGE_IMPORT_RE,
        )
        assert result == "from syra.agent.config import Settings\n"

    def test_import_pass_ignores_string_anchors(self, tmp_path: Path) -> None:
        source = 'ASSETS = resources.files("lup_template.devtools.dev")\n'
        assert apply_rename(tmp_path, source, PACKAGE_IMPORT_RE) == source


class TestRenamerSelfExclusion:
    def test_recognizes_the_renamer_module(self) -> None:
        assert is_renamer_module(Path("src/pkg/devtools/dev/init.py"))

    def test_ignores_sibling_modules(self) -> None:
        assert not is_renamer_module(Path("src/pkg/devtools/dev/app.py"))


def checkout(tmp_path: Path) -> Path:
    """An empty repository to rename in: the report reads what git holds."""
    work = tmp_path / "checkout"
    initialized_repo(work, tmp_path / "no-hooks")
    return work


class TestStaleReferenceReport:
    def test_reports_surviving_references_with_locations(self, tmp_path: Path) -> None:
        root = checkout(tmp_path)
        module = root / "src" / "pkg" / "mod.py"
        module.parent.mkdir(parents=True)
        module.write_text('"""Paths live under src/lup_template/devtools/."""\n')
        (root / "pyproject.toml").write_text('name = "x"\n')

        stale = find_stale_references(root)

        assert len(stale) == 1
        assert "mod.py:1:" in stale[0]
        assert "src/lup_template/devtools/" in stale[0]

    def test_reports_a_literal_left_in_a_passage(self, tmp_path: Path) -> None:
        """Passages are prose, so the import rewriting never reaches them.

        They name the application's paths through layout values; a literal
        one somebody wrote anyway would go on naming the old package in
        every generated page.
        """
        root = checkout(tmp_path)
        passage = root / "src" / "pkg" / "harness" / "content" / "guidance.passage.md"
        passage.parent.mkdir(parents=True)
        passage.write_text("No module under `src/lup_template/` imports an SDK.\n")
        (root / "src" / "pkg" / "notes.md").write_text("lup_template, in prose\n")

        (stale,) = find_stale_references(root)

        assert "guidance.passage.md:1:" in stale

    def test_skips_the_passages_whose_subject_is_initialization(
        self, tmp_path: Path
    ) -> None:
        """The init skill names the template's package on purpose."""
        root = checkout(tmp_path)
        skills = root / "src" / "pkg" / "harness" / "content" / "skills"
        skills.mkdir(parents=True)
        (skills / "init.passage.md").write_text("Renames `src/lup_template/`.\n")

        assert find_stale_references(root) == []

    def test_skips_the_renamer_module(self, tmp_path: Path) -> None:
        root = checkout(tmp_path)
        renamer = root / "src" / "pkg" / "devtools" / "dev" / "init.py"
        renamer.parent.mkdir(parents=True)
        renamer.write_text('OLD = "lup_template"\n')

        assert find_stale_references(root) == []

    def test_skips_what_the_ignore_rules_keep_out(self, tmp_path: Path) -> None:
        """A scratch tree under `src/` is nobody's to triage after a rename."""
        root = checkout(tmp_path)
        (root / ".gitignore").write_text("tmp/\n")
        scratch = root / "src" / "pkg" / "tmp" / "probe.py"
        scratch.parent.mkdir(parents=True)
        scratch.write_text("from lup_template.agent import core\n")
        kept = root / "tests" / "test_mod.py"
        kept.parent.mkdir(parents=True)
        kept.write_text('"""Exercises lup_template.agent.core."""\n')

        assert find_stale_references(root) == [
            '  tests/test_mod.py:1: """Exercises lup_template.agent.core."""'
        ]


def test_this_repository_s_passages_name_the_application_through_its_layout() -> None:
    """So the rename has nothing in them to report, but where initialization is the subject."""
    literal = [
        str(path)
        for path in Path("src").rglob("*.passage.md")
        if "lup_template" in path.read_text(encoding="utf-8")
        and not about_initialization(path)
    ]

    assert literal == []


class TestStaleInstallMetadata:
    """Left behind, the old name's metadata registers a second devtools app."""

    def test_the_old_names_egg_info_goes_with_the_rename(self, tmp_path: Path) -> None:
        stale = tmp_path / "src" / "lup_template.egg-info"
        stale.mkdir(parents=True)
        (stale / "entry_points.txt").write_text(
            "[lup.devtools]\napplication = lup_template.devtools.main:app\n"
        )

        assert drop_stale_metadata(tmp_path, dry_run=True) == [
            "  src/lup_template.egg-info: the old name's install metadata, removed"
        ]
        assert stale.is_dir()

        drop_stale_metadata(tmp_path, dry_run=False)

        assert not stale.exists()

    def test_a_checkout_never_installed_has_none_to_remove(
        self, tmp_path: Path
    ) -> None:
        assert drop_stale_metadata(tmp_path, dry_run=False) == []
