"""Which files the narrow check answers about, and which it must not miss.

The scope is the whole of what makes a narrowed run trustworthy. Ruff and
Pyright each answer about the files they are handed, so the run is exactly as
honest as the list — and the two ways a list goes wrong pull in opposite
directions. Naming a file that is gone turns a narrowed run into an error
about its own arguments; *not* naming a file that changed reports "clean"
about code nobody read, which is the failure this whole lane exists to avoid
committing.

Where the list starts is the other half. Read from a base's tip, a branch
answers for every commit the base took after the branch was cut; read from
where it left the base, it answers for its own.
"""

from pathlib import Path

import pytest
import sh
import typer

import lup.devtools.dev.check as check
from lup.devtools.dev import records
from lup.devtools.dev.check import (
    ChangeBase,
    ChangedScope,
    change_base,
    changed_scope,
)
from lup.devtools.dev.migrations import MigrationRecord
from lup.devtools.dev.reach import Spread
from lup.devtools.project import DevProject
from tests.unit.test_devtools_check_ledger import quiet


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A checkout with one commit, stood in as the working directory.

    The scope is read with git run against the current directory, the way the
    command reads it, rather than against a path passed in — so the fixture
    moves the process instead of parameterising the subject.
    """
    work = tmp_path / "repo"
    work.mkdir()
    git = sh.Command("git").bake("-C", str(work), _tty_out=False)
    git("init", "-b", "main")
    git("config", "user.email", "scope@example.com")
    git("config", "user.name", "scope")
    (work / "kept.py").write_text("kept = 1\n", encoding="utf-8")
    (work / "prose.md").write_text("prose\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-m", "base")
    monkeypatch.chdir(work)
    return work


def committed(repo: Path, name: str, text: str) -> None:
    """Write one file and commit it on whatever branch is checked out."""
    git = sh.Command("git").bake("-C", str(repo), _tty_out=False)
    (repo / name).write_text(text, encoding="utf-8")
    git("add", name)
    git("commit", "-m", name)


def moved_on(repo: Path) -> None:
    """`topic` records `main` as its base, and `main` takes a commit after the cut."""
    git = sh.Command("git").bake("-C", str(repo), _tty_out=False)
    git("switch", "-c", "topic")
    records.remember("topic", records.BranchRecord(base="main"), repo)
    committed(repo, "mine.py", "mine = 1\n")
    git("switch", "main")
    committed(repo, "theirs.py", "theirs = 1\n")
    git("switch", "topic")


def test_a_file_written_and_never_added_is_still_checked(repo: Path) -> None:
    """The half `git diff` does not report, and the half most worth reading.

    A module written five minutes ago is what its author is asking about. A
    scope built from tracked changes alone would leave it out and answer
    "clean" over the one file in the tree nobody has read.
    """
    (repo / "fresh.py").write_text("fresh = 1\n", encoding="utf-8")

    assert changed_scope("HEAD").checked == ["fresh.py"]


def test_a_changed_file_that_is_not_python_is_named_as_unread(repo: Path) -> None:
    """Ruff and Pyright are asked about Python, and the rest is said, not dropped."""
    (repo / "prose.md").write_text("changed prose\n", encoding="utf-8")

    assert changed_scope("HEAD") == ChangedScope(checked=[], unread=["prose.md"])


def test_a_deleted_file_is_not_named_to_a_checker(repo: Path) -> None:
    """A path that is gone would fail the run on its own argument list.

    `git diff` reports a deletion as a change, which is right for a note gate
    reading history and wrong for a checker that has to open the file.
    """
    (repo / "kept.py").unlink()

    assert changed_scope("HEAD") == ChangedScope(checked=[], unread=[])


def test_a_tracked_edit_and_a_new_file_are_both_in_scope(repo: Path) -> None:
    """The ordinary case: something edited, something added, both answered for."""
    (repo / "kept.py").write_text("kept = 2\n", encoding="utf-8")
    (repo / "fresh.py").write_text("fresh = 1\n", encoding="utf-8")

    assert changed_scope("HEAD").checked == ["fresh.py", "kept.py"]


def test_a_tree_that_changed_nothing_scopes_nothing(repo: Path) -> None:
    """Empty is a real answer, and the caller reports it rather than checking all."""
    assert changed_scope("HEAD") == ChangedScope(checked=[], unread=[])


def test_a_branch_answers_for_its_own_commits_not_its_base_s_later_ones(
    repo: Path,
) -> None:
    """The 108-file report: a base's tip read as where the branch started.

    `main` took `theirs.py` after `topic` was cut. Diffed against `main`'s
    tip, `topic` would answer for that file too, as though it had removed it;
    from the merge base with the base it records, it answers for `mine.py`.
    """
    moved_on(repo)

    base = change_base(None, "main")

    assert changed_scope(base.commit).checked == ["mine.py"]
    assert base.reached == "the merge base with main, the base topic records"


def test_a_named_ref_is_read_from_the_merge_base_with_it(repo: Path) -> None:
    """Naming the base outright answers the same question, not the tip's."""
    moved_on(repo)

    base = change_base("main", "main")

    assert changed_scope(base.commit).checked == ["mine.py"]
    assert base.reached == "the merge base with main"


def test_a_named_ref_nothing_resolves_refuses_by_the_flag_it_came_through(
    repo: Path,
) -> None:
    """A mistyped ref answering nothing would read as a branch that changed nothing."""
    with pytest.raises(typer.BadParameter, match="--since 'no-such-ref'"):
        change_base("no-such-ref", "main")


def test_the_integration_branch_answers_for_its_uncommitted_work(repo: Path) -> None:
    """There is no base to leave from, so what changed is what is not committed."""
    sh.Command("git")("-C", str(repo), "branch", "topic", _tty_out=False)
    (repo / "kept.py").write_text("kept = 2\n", encoding="utf-8")

    base = change_base(None, "main")

    assert base.commit == "HEAD"
    assert changed_scope(base.commit).checked == ["kept.py"]


def test_a_change_no_scoped_check_reads_says_so_and_names_every_gate_left(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A Markdown-only change checked nothing, and the run has to say what it left.

    Reporting only what ran reads as a verdict on the change: the unread
    files are listed, and the suites are named as the gate runs them.
    """
    (repo / "prose.md").write_text("changed prose\n", encoding="utf-8")
    roots = [
        check.TestRoot(name="pytest", directory=repo),
        check.TestRoot(name="pytest (lib)", directory=repo / "lib"),
    ]

    check.run_changed(
        DevProject(package="app"), ChangeBase(commit="HEAD", reached="HEAD"), roots
    )
    printed = capsys.readouterr().out.splitlines()

    assert "No Python file changed, so neither ruff nor pyright ran." in printed
    # What does read every changed file is the conflict row, and it says so.
    assert "conflict markers: ok" in printed
    assert "Unread: 1 changed file(s) no scoped check reads:" in printed
    assert "  prose.md" in printed
    assert any(
        line.startswith("Not run: the test suites (pytest, pytest (lib)) and ")
        for line in printed
    )


def test_a_public_name_the_branch_removed_fails_the_narrowed_run(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The migrations row runs from the same base the scope is read from.

    A public name removed with no migration is a one-file mistake, and a
    narrowed run that skipped this row let it reach the whole gate. Ruff and
    Pyright are stubbed: what they say about the file is not the question.
    """
    git = sh.Command("git").bake("-C", str(repo), _tty_out=False)
    (repo / "src/app").mkdir(parents=True)
    (repo / "src/app/mod.py").write_text(
        "def kept() -> None: ...\n\n\ndef gone() -> None: ...\n", encoding="utf-8"
    )
    git("add", "-A")
    git("commit", "-m", "surface")
    git("switch", "-c", "topic")
    records.remember("topic", records.BranchRecord(base="main"), repo)
    (repo / "src/app/mod.py").write_text("def kept() -> None: ...\n", encoding="utf-8")
    for tool in ("ruff_format_check", "ruff_lint_check", "pyright_check"):
        monkeypatch.setattr(check, tool, quiet(tool))

    with pytest.raises(typer.Exit):
        check.run_changed(
            DevProject(package="app"),
            change_base(None, "main"),
            [],
            Spread(library=["src/app"], copied=[], generated=[]),
        )
    printed = capsys.readouterr().out.splitlines()

    assert "declared migrations: FAIL (1 gone with nothing to read)" in printed
    assert printed[-1] == "Failed: declared migrations"


def test_a_migration_declaration_the_migrations_row_read_is_not_named_unread(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A declaration the row parses was read, and saying otherwise sends a
    reader looking for the gate that checks it."""
    (repo / "migrations/pending").mkdir(parents=True)
    (repo / "migrations/pending/gone.toml").write_text(
        'subjects = ["app.gone"]\nreason = "it went"\n\n'
        '[[steps]]\ninstruction = "call kept instead"\n',
        encoding="utf-8",
    )
    (repo / "prose.md").write_text("changed prose\n", encoding="utf-8")
    for tool in ("ruff_format_check", "ruff_lint_check", "pyright_check"):
        monkeypatch.setattr(check, tool, quiet(tool))

    check.run_changed(
        DevProject(package="app"),
        ChangeBase(commit="HEAD", reached="HEAD"),
        [],
        Spread(library=["src/app"], copied=[], generated=[]),
        record=MigrationRecord(root=repo / "migrations"),
    )
    printed = capsys.readouterr().out.splitlines()

    assert "declared migrations: ok" in printed
    assert "Unread: 1 changed file(s) no scoped check reads:" in printed
    assert "  migrations/pending/gone.toml" not in printed


def test_a_changed_file_the_rules_refuse_fails_the_narrowed_run(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The anti-pattern rules run over what changed, however it landed.

    A file copied in with `cp` or taken whole in a merge passes no edit gate
    on the way in, and the narrowed run used to read it with ruff and pyright
    alone -- so it passed the loop and was first refused by the whole gate.
    """
    (repo / "kept.py").write_text(
        "from typing import Any\n\n\ndef f(x: Any) -> None: ...\n", encoding="utf-8"
    )
    for tool in ("ruff_format_check", "ruff_lint_check", "pyright_check"):
        monkeypatch.setattr(check, tool, quiet(tool))

    with pytest.raises(typer.Exit):
        check.run_changed(
            DevProject(package="app"), ChangeBase(commit="HEAD", reached="HEAD"), []
        )
    printed = capsys.readouterr().out.splitlines()

    assert any(line.startswith("antipatterns: FAIL") for line in printed)
    assert any(line.startswith("  kept.py:4 [missing any-type]") for line in printed)
    assert printed[-1] == "Failed: antipatterns"
