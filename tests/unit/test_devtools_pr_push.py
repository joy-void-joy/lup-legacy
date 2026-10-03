"""Behavior tests for how `dev pr push` gives a branch its remote.

`git push -u` records the relationship by writing `branch.<name>.remote` and
`branch.<name>.merge` into the repository's shared config. Where that file
cannot be written the flag fails without saying so: the push happens, git
prints `set up to track`, and the command exits 0 having recorded nothing —
so everything downstream reads a branch that was published as one that never
was, and whatever retries on that reading retries forever.

The destination is stated as a refspec instead, which needs no config write
at all, and what `-u` was for is recorded beside the branch's other facts.
"""

from pathlib import Path

import pytest
import sh
import typer

from lup.devtools.dev import branches, pr, records
from lup.execution.process import LocalProcessLauncher
from tests.unit.repos import commit_file, git_in, initialized_repo


class SilentGh:
    """A `gh` that answers no pull requests, so no forge is reached.

    The push is the subject here; whether a PR happens to exist is a second
    question this must not depend on the network to answer.
    """

    def out(self, *args: str) -> str:
        del args
        return "[]"


@pytest.fixture
def published(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A checkout on a topic branch with work to send, and a remote to send it to."""
    work = tmp_path / "repo"
    git = initialized_repo(work, tmp_path / "no-hooks")
    commit_file(git, work, "file.txt", "base\n", "chore: base")
    sh.Command("git")("init", "--bare", str(tmp_path / "origin.git"), _tty_out=False)
    git("remote", "add", "origin", str(tmp_path / "origin.git"))
    git("push", "origin", "main")
    git("checkout", "-q", "-b", "topic")
    commit_file(git, work, "mine.txt", "mine\n", "feat: mine")
    monkeypatch.setattr(pr, "gh", SilentGh())
    monkeypatch.chdir(work)
    return work


def shared_config(repo: Path) -> str:
    """The file whose keys name programs git runs on the host."""
    return (repo / ".git" / "config").read_text(encoding="utf-8")


def test_a_push_records_the_remote_it_sent_to(published: Path) -> None:
    """What `-u` claimed to write, written where lup can read it back."""
    pr.push(force=False, as_json=True, protected=["main"])

    assert records.recorded_upstream("topic", published) == "origin/topic"


def test_a_push_leaves_the_shared_config_exactly_as_it_was(published: Path) -> None:
    """The whole point: publishing a branch stops needing a writable config.

    Compared as text rather than by asking for the two tracking keys, because
    a push that wrote anything at all into this file is a push that needed it
    writable, whichever key it chose.
    """
    before = shared_config(published)

    pr.push(force=False, as_json=True, protected=["main"])

    assert shared_config(published) == before


def test_a_forced_push_records_the_remote_too(published: Path) -> None:
    """Both spellings reach the same recording, since either may be the first."""
    before = shared_config(published)

    pr.push(force=True, as_json=True, protected=["main"])

    assert records.recorded_upstream("topic", published) == "origin/topic"
    assert shared_config(published) == before


def test_a_forced_push_leaves_a_branch_others_build_on_alone(
    tmp_path: Path, published: Path
) -> None:
    """The shell policy asks before forcing one; this tool cannot ask, so refuses."""
    git = git_in(published, tmp_path / "no-hooks")
    git("switch", "-q", "main")
    git("commit", "-q", "--amend", "--allow-empty", "-m", "chore: rewritten")

    with pytest.raises(typer.Exit):
        pr.push(force=True, as_json=True, protected=["main"])

    assert "chore: base" in str(git("log", "-1", "--format=%s", "origin/main"))


def test_a_forced_push_refuses_to_overwrite_a_push_it_only_fetched(
    tmp_path: Path, published: Path
) -> None:
    """A lease alone passes once a fetch has moved the tracking ref; this does not."""
    pr.push(force=False, as_json=True, protected=["main"])
    other = tmp_path / "other"
    origin = str(tmp_path / "origin.git")
    sh.Command("git")("clone", "-q", "-b", "topic", origin, str(other))
    theirs = git_in(other, tmp_path / "no-hooks")
    commit_file(theirs, other, "theirs.txt", "x\n", "feat: theirs")
    theirs("push", "-q", "origin", "topic")
    git = git_in(published, tmp_path / "no-hooks")
    git("commit", "-q", "--amend", "-m", "feat: mine, reworded")
    git("fetch", "-q", "origin")

    with pytest.raises(typer.Exit):
        pr.push(force=True, as_json=True, protected=["main"])

    assert "feat: theirs" in str(git("log", "-1", "--format=%s", "origin/topic"))


def test_the_branch_actually_lands_on_the_remote(published: Path) -> None:
    """A refspec that records nothing would be no better than the flag.

    The remote-tracking ref is asked for as well as the remote's own branch,
    because everything that measures freshness counts against
    `refs/remotes/origin/<name>` and a push that moved only the far side
    leaves that count unanswerable.
    """
    pr.push(force=False, as_json=True, protected=["main"])

    git = sh.Command("git").bake("-C", str(published), _tty_out=False)
    assert "refs/heads/topic" in str(git("ls-remote", "--heads", "origin", "topic"))
    assert str(git("rev-parse", "refs/remotes/origin/topic")).strip()


def test_the_recorded_remote_answers_where_git_tracks_nothing(
    published: Path,
) -> None:
    """The reader that asks the record rather than `branch.<name>.merge`.

    Freshness is measured against the remote a branch answers to. Asked of
    git's tracking configuration alone, a branch published by a refspec
    answers nothing, and a checkout that is behind its own remote reports
    itself level with it.
    """
    pr.push(force=False, as_json=True, protected=["main"])

    remotes = branches.tracked_remotes(LocalProcessLauncher(), published)

    assert remotes.upstream == "origin/topic"


def test_a_branch_nobody_pushed_still_answers_to_nothing(published: Path) -> None:
    """The case that must not change: no push is still no remote."""
    remotes = branches.tracked_remotes(LocalProcessLauncher(), published)

    assert remotes.upstream == ""
