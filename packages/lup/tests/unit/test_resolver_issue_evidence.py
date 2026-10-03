"""Taking a project's tracker issues as evidence a run can plan from.

An issue is already the repository's structured, reviewable record of what is
wrong. Read directly, it needs no transcribing into a `# lup:` note before a
run can act on it, and friction a run finds outlives the run — so evidence
makes no round trip through a human between two surfaces that cannot talk.
"""

from pathlib import Path

import pytest
import sh

import lup.devtools.dev.issues as issues_mod
import lup.devtools.utils as utils
from lup.devtools.dev.issues import IssueLabel, IssueRow
from lup.devtools.utils import slug_from_remote
from lup.resolver.core import planned_evidence
from lup.resolver.models import (
    InventoryNote,
    IssueEvidence,
    ResolveRequest,
    SourceSnapshot,
)


def issue(number: int) -> IssueEvidence:
    return IssueEvidence(
        number=number,
        url=f"https://example.test/issues/{number}",
        title=f"Issue {number}",
        body="what goes wrong",
    )


def request(notes: int, statements: int, issues: int) -> ResolveRequest:
    return ResolveRequest(
        source=SourceSnapshot(branch="dev", commit="a" * 40),
        notes=[
            InventoryNote(file=Path("a.py"), line=index + 1, text="note", context="ctx")
            for index in range(notes)
        ],
        statements=[f"statement {index}" for index in range(statements)],
        issues=[issue(index + 1) for index in range(issues)],
    )


@pytest.mark.parametrize(
    ("remote", "slug"),
    [
        # An ssh alias, the shape that defeats every `gh` query inferring it.
        ("alias:owner/name.git", "owner/name"),
        ("git@github.com:owner/name.git", "owner/name"),
        ("https://github.com/owner/name.git", "owner/name"),
        ("https://github.com/owner/name", "owner/name"),
        ("ssh://git@github.com/owner/name.git", "owner/name"),
        ("/a/local/path.git", "local/path"),
        ("name", ""),
    ],
)
def test_a_remote_names_its_repository_whatever_shape_it_is_written_in(
    remote: str, slug: str
) -> None:
    assert slug_from_remote(remote) == slug


def test_a_query_names_the_repository_it_means(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Named rather than inferred, because inference is what an alias defeats."""
    monkeypatch.setattr(utils, "repository_slug", lambda: "owner/name")

    assert utils.repository_arguments() == ["--repo", "owner/name"]


def test_a_checkout_with_no_readable_forge_names_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty slug is a project with no forge, not a broken flag to pass on."""
    monkeypatch.setattr(utils, "repository_slug", lambda: "")

    assert utils.repository_arguments() == []


def test_an_excluded_label_withholds_an_issue() -> None:
    row = IssueRow(
        number=1,
        url="https://example.test/1",
        title="Something",
        labels=[IssueLabel(name="resolver-skip")],
    )

    assert row.excluded_by("resolver-skip")
    assert not row.excluded_by("other")


def test_issue_positions_continue_past_the_notes_and_statements() -> None:
    # Appended rather than inserted: the indexes a planner already wrote are
    # persisted in run state, and a resumed run must read them as it meant them.
    evidence = request(notes=2, statements=1, issues=2)

    assert evidence.evidence_count() == 5

    cited = planned_evidence(evidence, [1, 2, 4])

    assert [note.line for note in cited.notes] == [2]
    assert cited.evidence == "statement 0"
    assert [item.number for item in cited.issues] == [2]


def test_a_run_can_be_planned_from_issues_alone() -> None:
    evidence = request(notes=0, statements=0, issues=1)

    assert evidence.evidence_count() == 1
    assert planned_evidence(evidence, [0]).issues == [issue(1)]


def test_evidence_of_no_kind_at_all_is_refused() -> None:
    with pytest.raises(ValueError, match="at least one piece of evidence"):
        ResolveRequest(source=SourceSnapshot(branch="dev", commit="a" * 40))


class TestATrackerThatDidNotAnswer:
    """A refused credential and a clean tracker are not the same reading.

    With an expired token, a listing that prints "0 open issue(s) in
    upstream/framework" and exits zero says exactly what a repository with
    nothing open says. Anything acting on that plans from an emptiness
    nobody established.
    """

    def refusing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Make the tracker call fail the way a bad credential fails."""

        class Refusing:
            """A `gh` answering every call the way an expired token does."""

            def out(self, *arguments: str) -> str:
                raise sh.ErrorReturnCode_1(
                    "gh issue list",
                    b"",
                    b"HTTP 401: Bad credentials (https://api.github.com/graphql)",
                )

        monkeypatch.setattr(issues_mod, "gh", Refusing())
        monkeypatch.setattr(issues_mod, "repository_slug", lambda: "owner/name")

    def test_an_unreachable_tracker_is_not_an_empty_one(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self.refusing(monkeypatch)

        answered = issues_mod.read_open_issues()

        assert answered.reached is False
        assert answered.issues == []

    def test_what_the_tracker_said_travels_with_the_refusal(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A reader who has to fix the credential is told which one refused."""
        self.refusing(monkeypatch)

        assert "401" in issues_mod.read_open_issues().why

    def test_a_caller_that_proceeds_either_way_still_gets_a_list(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Intake plans from the tree's own notes rather than not starting."""
        self.refusing(monkeypatch)

        assert issues_mod.fetch_open_issues() == []
