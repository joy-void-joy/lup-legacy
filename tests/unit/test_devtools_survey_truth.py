"""What ``dev survey`` says about a forge it may not have reached.

A survey is read by ``/lup:land`` before it proposes a verb for every branch
in the repository, so a field that is structurally empty and a field that is
empty because the remote answered that way have to be told apart. These pin
the two places where they could not be.
"""

import json
from pathlib import Path

import pytest
import sh

from lup.devtools.dev import branches
from lup.devtools import utils
from tests.unit.repos import commit_file, initialized_repo


class Recorder:
    """A stand-in for the `gh` command that keeps the arguments it was given."""

    def __init__(self, output: str = "") -> None:
        self.calls: list[tuple[str, ...]] = []
        self.output = output

    def __call__(self, *arguments: str) -> str:
        self.calls.append(arguments)
        return self.output

    def out(self, *arguments: str) -> str:
        return self(*arguments)


PR_ROW = {
    "number": 169,
    "title": "fix(policy): type-check only the files the checker can read",
    "state": "OPEN",
    "mergedAt": None,
    "headRefName": "diagnostics-python-only",
    "url": "https://github.com/owner/name/pull/169",
}


@pytest.fixture
def gh_rows(monkeypatch: pytest.MonkeyPatch) -> Recorder:
    """A `gh` answering one PR row, with the repository slug readable."""
    monkeypatch.setattr(utils, "repository_slug", lambda: "owner/name")
    recorder = Recorder(output=json.dumps([PR_ROW]))
    monkeypatch.setattr(branches, "gh", recorder)
    return recorder


def test_the_survey_asks_for_the_field_it_declares(gh_rows: Recorder) -> None:
    """``PRStatus.url`` is declared, so the query that fills it has to ask."""
    statuses = branches.fetch_pr_status(["diagnostics-python-only"])

    arguments = gh_rows.calls[0]
    requested = arguments[arguments.index("--json") + 1]
    assert "url" in requested.split(",")  # lup: ignore[string-split] — gh field list
    assert statuses["diagnostics-python-only"].url == PR_ROW["url"]


class StubGit:
    """The two git readings a branchless survey makes of the checkout."""

    def out(self, *arguments: str) -> str:
        return "dev"

    def lines(self, *arguments: str) -> list[str]:
        return []


def local_survey(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Answer every collaborator a survey of an empty checkout consults."""
    monkeypatch.setattr(branches, "git", StubGit())
    monkeypatch.setattr(branches, "get_integration_branch", lambda: "dev")
    monkeypatch.setattr(branches, "parse_branches", list)
    monkeypatch.setattr(branches, "parse_worktrees", dict)
    monkeypatch.setattr(branches, "build_containment", lambda names: {})
    monkeypatch.setattr(branches, "parse_remote_branches", list)
    monkeypatch.setattr(branches, "fetch_pr_status", lambda names: {})
    monkeypatch.setattr(branches, "live_lease_branches", lambda path: {})
    monkeypatch.setattr(branches, "project_root", lambda: tmp_path)
    monkeypatch.setattr(
        branches,
        "integration_standing",
        lambda integration: branches.IntegrationStanding(branch=integration),
    )


def failing_fetch() -> None:
    raise sh.ErrorReturnCode_128(
        "git fetch --prune origin",
        b"",
        b"fatal: Could not read from remote repository.\n",
    )


def test_a_fetch_that_failed_is_not_a_repository_with_nothing_stranded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The two readings of ``remote_branches: []`` are told apart by a field."""
    local_survey(monkeypatch, tmp_path)
    monkeypatch.setattr(branches, "origin_auth_complaint", lambda: "")
    monkeypatch.setattr(branches, "fetch_remote_tracking", failing_fetch)

    branches.survey(as_json=True)

    reported = json.loads(capsys.readouterr().out)
    assert reported["remote_branches"] == []
    assert reported["remotes_fetched"] is False
    assert "Could not read from remote repository." in reported["fetch_complaint"]


def test_a_remote_that_answered_says_its_empty_list_means_something(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    local_survey(monkeypatch, tmp_path)
    monkeypatch.setattr(branches, "origin_auth_complaint", lambda: "")
    monkeypatch.setattr(branches, "fetch_remote_tracking", lambda: None)

    branches.survey(as_json=True)

    reported = json.loads(capsys.readouterr().out)
    assert reported["remotes_fetched"] is True
    assert reported["fetch_complaint"] == ""


def test_a_remote_no_credential_reaches_is_reported_the_same_way(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A refused credential leaves the list just as unread as a failed fetch."""
    local_survey(monkeypatch, tmp_path)
    monkeypatch.setattr(branches, "origin_auth_complaint", lambda: "ssh refused")

    branches.survey(as_json=True)

    reported = json.loads(capsys.readouterr().out)
    assert reported["remotes_fetched"] is False
    assert reported["fetch_complaint"] == "ssh refused"


def test_the_human_table_says_the_remotes_went_unread(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A reader of the table sees what a reader of the JSON sees."""
    local_survey(monkeypatch, tmp_path)
    monkeypatch.setattr(branches, "origin_auth_complaint", lambda: "")
    monkeypatch.setattr(branches, "fetch_remote_tracking", failing_fetch)

    branches.survey(as_json=False)

    assert "were not read" in capsys.readouterr().out


@pytest.fixture
def carried(tmp_path: Path) -> Path:
    """A project whose scaffold carrier `dev update` has already merged."""
    work = tmp_path / "repo"
    git = initialized_repo(work, tmp_path / "no-hooks")
    commit_file(git, work, "file.txt", "base\n", "chore: base")
    git("checkout", "-q", "-b", "lup-scaffold")
    commit_file(git, work, "copied.txt", "copied\n", "chore: compile the scaffold")
    git("checkout", "-q", "main")
    git("merge", "-q", "--no-ff", "--no-edit", "lup-scaffold")
    return work


def surveyed_rows(capsys: pytest.CaptureFixture[str]) -> dict[str, dict[str, str]]:
    """Each local row of a survey printed as JSON, by branch name."""
    reported = json.loads(capsys.readouterr().out)
    return {row["name"]: row for row in reported["branches"]}


def test_the_scaffold_carrier_is_kept_once_its_merge_landed(
    carried: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A merged carrier is the next update's merge base, not a spent branch."""
    monkeypatch.chdir(carried)
    monkeypatch.setattr(branches, "origin_auth_complaint", lambda: "no origin")

    branches.survey(as_json=True, scaffold="lup-scaffold")

    row = surveyed_rows(capsys)["lup-scaffold"]
    assert row["disposition"] == "KEEP"
    assert "scaffold carrier" in row["reason"]


def test_an_undeclared_carrier_reads_as_the_merged_branch_it_is(
    carried: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The precondition, so the row above cannot be kept for another reason."""
    monkeypatch.chdir(carried)
    monkeypatch.setattr(branches, "origin_auth_complaint", lambda: "no origin")

    branches.survey(as_json=True)

    assert surveyed_rows(capsys)["lup-scaffold"]["disposition"] == "DELETE"


@pytest.fixture
def diverged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A checkout whose main and origin's each hold a commit the other lacks.

    Beside it, a branch that was pushed and one that never was. The forge's
    commit is pushed from a second clone, as a merge there would be, and this
    checkout's own commit is never pushed.
    """
    work = tmp_path / "repo"
    git = initialized_repo(work, tmp_path / "no-hooks")
    commit_file(git, work, "file.txt", "base\n", "chore: base")
    origin = tmp_path / "origin.git"
    sh.Command("git")("init", "--bare", "-q", "-b", "main", str(origin))
    git("remote", "add", "origin", str(origin))
    git("branch", "pushed")
    git("push", "-q", "origin", "main", "pushed")
    git("checkout", "-q", "-b", "unpublished")
    commit_file(git, work, "local.txt", "local\n", "feat: only here")
    git("checkout", "-q", "main")

    forge = tmp_path / "forge"
    sh.Command("git")("clone", "-q", str(origin), str(forge), _tty_out=False)
    elsewhere = initialized_repo(forge, tmp_path / "no-hooks")
    commit_file(elsewhere, forge, "theirs.txt", "theirs\n", "feat: landed on the forge")
    elsewhere("push", "-q", "origin", "main")

    commit_file(git, work, "ours.txt", "ours\n", "feat: never pushed")
    monkeypatch.chdir(work)
    monkeypatch.setattr(branches, "fetch_pr_status", lambda names: {})
    return work


def test_a_branch_no_remote_carries_says_so(
    diverged: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Work on no remote is one disk away from gone, and a row has to show it."""
    branches.survey(as_json=True)

    rows = surveyed_rows(capsys)
    assert rows["pushed"]["on_remote"] is True
    assert rows["unpublished"]["on_remote"] is False


def test_the_integration_branch_is_measured_against_origin_s_copy(
    diverged: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Both directions, since each dispositions every row wrongly in its own way.

    Commits only origin's copy holds make work merged on the forge read as
    unlanded here; commits only the local copy holds are on no remote at all.
    """
    branches.survey(as_json=True)

    standing = json.loads(capsys.readouterr().out)["integration_remote"]
    assert standing["remote"] == "origin/main"
    assert standing["ahead"] == 1
    assert standing["behind"] == 1


def test_the_table_names_a_diverged_integration_branch_and_unpublished_work(
    diverged: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    branches.survey(as_json=False)

    out = capsys.readouterr().out
    assert "origin/main holds 1 commit(s) main lacks" in out
    assert "main holds 1 commit(s) origin/main lacks" in out
    assert "1 branch(es) holding unlanded work on no remote" in out
    assert "holds it: unpublished" in out
