"""The one index reader every sweep walks, held to one entry per file.

An index mid-merge names a conflicted path once per stage, and every sweep
that read it directly reported one file's findings three times. The reader
is the place that stops, so it is pinned here on a real merge rather than on
a listing a test wrote by hand.
"""

from pathlib import Path

import pytest
import sh

from lup.devtools.dev.tracked import tracked_files
from lup.execution.shell import git


def build_conflicted_checkout(work: Path) -> sh.Command:
    """A repository whose index holds `src/pkg/mod.py` at three stages.

    Both sides of a merge edit one line, the merge stops, and the working copy
    is restored from one side without being staged — so the file on disk reads
    clean while the index still carries every stage of it.
    """
    (work / "src/pkg").mkdir(parents=True)
    module = work / "src/pkg/mod.py"
    build = sh.Command("git").bake(
        "-C",
        str(work),
        "-c",
        "commit.gpgsign=false",
        "-c",
        "user.email=tracked@example.test",
        "-c",
        "user.name=Tracked Test",
        _tty_out=False,
    )
    build("init", "-b", "main")
    module.write_text("VALUE = 1\n", encoding="utf-8")
    (work / "README.md").write_text("# pkg\n", encoding="utf-8")
    build("add", "--all")
    build("commit", "-m", "base")
    build("switch", "-c", "theirs")
    module.write_text("VALUE = 2\n", encoding="utf-8")
    build("commit", "-am", "theirs")
    build("switch", "main")
    module.write_text("VALUE = 3\n", encoding="utf-8")
    build("commit", "-am", "ours")
    build("merge", "theirs", _ok_code=[0, 1])
    build("restore", "--ours", "--", "src/pkg/mod.py")
    return build


def test_a_path_mid_merge_is_listed_once_however_many_stages_it_holds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    work = tmp_path / "repo"
    build_conflicted_checkout(work)
    monkeypatch.chdir(work)
    assert len(git.lines("ls-files", "--unmerged")) == 3

    assert tracked_files() == ["README.md", "src/pkg/mod.py"]
    assert tracked_files(suffixes=(".py",)) == ["src/pkg/mod.py"]
    assert tracked_files(others=True, suffixes=(".py",)) == ["src/pkg/mod.py"]


def test_others_adds_the_untracked_files_the_ignore_rules_keep(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    work = tmp_path / "repo"
    build_conflicted_checkout(work)
    (work / ".gitignore").write_text("ignored.py\n", encoding="utf-8")
    (work / "NOTES.md").write_text("# notes\n", encoding="utf-8")
    (work / "src/pkg/fresh.py").write_text("FRESH = 1\n", encoding="utf-8")
    (work / "ignored.py").write_text("IGNORED = 1\n", encoding="utf-8")
    monkeypatch.chdir(work)

    assert tracked_files(suffixes=(".py",)) == ["src/pkg/mod.py"]
    assert sorted(tracked_files(others=True, suffixes=(".py",))) == [
        "src/pkg/fresh.py",
        "src/pkg/mod.py",
    ]
    assert sorted(tracked_files(others=True)) == [
        ".gitignore",
        "NOTES.md",
        "README.md",
        "src/pkg/fresh.py",
        "src/pkg/mod.py",
    ]


def test_a_checkout_named_by_path_is_listed_relative_to_itself(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A caller holding a checkout by path reads it from wherever it runs.

    A command can be invoked from a subdirectory, where git answers for that
    subtree alone, so a scan over a project root it was handed names the root
    rather than trusting the working directory to be it.
    """
    work = tmp_path / "repo"
    build_conflicted_checkout(work)
    (work / ".gitignore").write_text("ignored/\n", encoding="utf-8")
    (work / "ignored").mkdir()
    (work / "ignored/mod.py").write_text("IGNORED = 1\n", encoding="utf-8")
    monkeypatch.chdir(work / "src")

    assert sorted(tracked_files(others=True, suffixes=(".py",), root=work)) == [
        "src/pkg/mod.py"
    ]
    assert tracked_files(suffixes=(".py",)) == ["pkg/mod.py"]


def test_a_path_holding_a_non_ascii_byte_is_listed_as_it_is_spelled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A line listing quotes such a path, and a sweep would then skip it.

    `git ls-files` without `-z` writes `"docs/caf\\303\\251.md"`, quotes and
    octal escapes included, which matches no suffix and opens no file.
    """
    work = tmp_path / "repo"
    build_conflicted_checkout(work)
    accented = work / "docs" / "café.md"
    accented.parent.mkdir()
    accented.write_text("# Café\n", encoding="utf-8")
    monkeypatch.chdir(work)

    listed = tracked_files(others=True, suffixes=(".md",))

    assert "docs/café.md" in listed
    assert (work / listed[listed.index("docs/café.md")]).is_file()
