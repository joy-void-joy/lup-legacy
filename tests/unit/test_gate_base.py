"""The migrations gate reads its base where a clone has one, and says when it has none.

A CI clone holds the one branch it checks out, and the base detector exits
the process on finding no other local branch -- an exit that would take the
whole report with it, leaving a log of one line and an exit code naming no
check. So the gate judges the integration branch from the release branch, a
feature branch from its own base, reads the remote's copy where only the
remote carries it, and reports a checkout with no base at all as skipped.
"""

from pathlib import Path

import pytest
import sh

from lup.devtools.dev.migrations import gate_base
from lup.devtools.dev.release import release_subject
from tests.unit.repos import commit_file, initialized_repo


def out(work: Path, *arguments: str) -> str:
    return str(sh.Command("git")("-C", str(work), *arguments, _tty_out=False)).strip()


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """`main` with one commit, `dev` one commit ahead of it, standing on `dev`."""
    work = tmp_path / "repo"
    git = initialized_repo(work, tmp_path / "no-hooks")
    commit_file(git, work, "base.txt", "base\n", "chore: base")
    git("checkout", "-q", "-b", "dev")
    commit_file(git, work, "more.txt", "more\n", "feat: more")
    monkeypatch.chdir(work)
    return work


def test_the_integration_branch_is_judged_from_the_release_branch(repo: Path) -> None:
    """Before a first release there is no cut to read, so the branch answers."""
    assert gate_base("dev") == out(repo, "rev-parse", "main")


def test_a_release_cut_and_not_yet_landed_is_judged_from_its_commit(
    repo: Path,
) -> None:
    """The window a release opens between cutting it and landing it.

    Cutting empties the declared breaks into the changelog; the release branch
    only moves once the cut lands. Read from the branch in between, every
    break the release had just shipped came back undeclared — against a list
    that is empty exactly then, by design.

    The commit rather than the tag, because the tag is pushed last on purpose:
    the branch reaches CI and a reviewer carrying the cut and no tag, which is
    the whole of the window.
    """
    git = initialized_repo(repo, repo.parent / "no-hooks")
    commit_file(git, repo, "released.txt", "cut\n", release_subject("0.3.0", "0.4.0"))
    cut = out(repo, "rev-parse", "HEAD")
    commit_file(git, repo, "after.txt", "after\n", "feat: after the release")

    assert gate_base("dev") == cut
    assert gate_base("dev") != out(repo, "rev-parse", "main")

    # A pull request's checkout stands on no branch, which is the other half
    # of the same window: the push build read the cut and the request build
    # beside it did not, and reported every break the release had shipped.
    sh.Command("git")("-C", str(repo), "checkout", "-q", "--detach", _tty_out=False)

    assert gate_base("dev") == cut


def test_a_commit_that_is_not_a_release_is_not_read_as_one(repo: Path) -> None:
    """The subject is the mark, so an ordinary commit mentioning one is not it."""
    commit_file(
        initialized_repo(repo, repo.parent / "no-hooks"),
        repo,
        "notes.txt",
        "notes\n",
        "docs(changelog): what the release: 0.3.0 line means",
    )

    assert gate_base("dev") == out(repo, "rev-parse", "main")


def test_a_feature_branch_is_judged_from_its_own_base(repo: Path) -> None:
    sh.Command("git")("-C", str(repo), "checkout", "-q", "-b", "topic", _tty_out=False)
    commit_file(
        initialized_repo(repo, repo.parent / "no-hooks"),
        repo,
        "t.txt",
        "t\n",
        "feat: t",
    )

    assert gate_base("dev") == out(repo, "rev-parse", "dev")


def test_a_clone_holding_one_branch_reads_the_remote_copy_of_its_base(
    tmp_path: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What a CI checkout is: one branch, and the base only as `origin/main`."""
    clone = tmp_path / "clone"
    sh.Command("git")(
        "clone", "-q", "--branch", "dev", str(repo), str(clone), _tty_out=False
    )
    monkeypatch.chdir(clone)

    assert out(clone, "branch", "--format=%(refname:short)") == "dev"
    assert gate_base("dev") == out(repo, "rev-parse", "main")


def test_no_base_at_all_is_a_reading_rather_than_an_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    work = tmp_path / "alone"
    git = initialized_repo(work, tmp_path / "no-hooks")
    commit_file(git, work, "only.txt", "only\n", "chore: only")
    monkeypatch.chdir(work)

    assert gate_base("dev") is None
