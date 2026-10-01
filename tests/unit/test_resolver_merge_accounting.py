"""What a join must account for, and what it is allowed to do.

Two obligations rather than two prohibitions. Content one parent contributed
that the joined tree no longer holds must be dispositioned; an edit outside
the conflict set must be declared. Neither forbids the merger anything — a
rewrite and a clean-file fix are both correct answers — and both make the
choice visible.
"""

from pathlib import Path

import pytest

from lup.harness.models import ResolveSpec, SkillInvocation
from lup.execution.process import ExitStatus, LaunchRequest, ProcessLauncher
from lup.resolver.join_tools import merge_problems
from lup.resolver.models import (
    AcceptanceCriterion,
    Concern,
    ConcernProgress,
    DeclaredEdit,
    DropCandidate,
    HunkDisposition,
    MergeReport,
    ResolvePhase,
    ResolveState,
    SourceSnapshot,
)
from lup.resolver.declaration import DeclarationDelta
from lup.resolver.state import StateTransitionError, validate_concern_admission
from lup.resolver.tools import agent_may_approve
from lup.devtools.harness.resolve import integration_branch
from tests.unit.repos import initialized_repo

PARENT = "a1b2c3d4e5f6"


def planned(identifier: str, title: str = "work", supersedes: str = "") -> Concern:
    return Concern(
        id=identifier,
        title=title,
        spec="do the thing",
        criteria=[AcceptanceCriterion(id=f"{identifier}-done", description="done")],
        supersedes=supersedes,
    )


def run_state(concerns: list[Concern]) -> ResolveState:
    return ResolveState(
        config_digest="digest",
        run_id="run-1",
        phase=ResolvePhase.WORKERS,
        source=SourceSnapshot(branch="dev", commit="source"),
        spec=ResolveSpec(
            id="resolve",
            worker_identity="resolver-worker",
            worker_skill=SkillInvocation(plugin="lup", skill="worker"),
            review_skill=SkillInvocation(plugin="lup", skill="review"),
            merge_skill=SkillInvocation(plugin="lup", skill="merge"),
        ),
        concerns=concerns,
        progress=[ConcernProgress(concern_id=item.id) for item in concerns],
    )


def candidate(path: str, missing: str = "value = compute()") -> DropCandidate:
    return DropCandidate(parent=PARENT, path=Path(path), missing=[missing])


def report(**overrides: object) -> MergeReport:
    fields = {"completed": True, "summary": "joined", **overrides}
    return MergeReport.model_validate(fields)


def test_lost_content_with_nothing_said_about_it_is_the_rejection() -> None:
    problems = merge_problems(report(), [], [candidate("src/api.py")])

    assert len(problems) == 1
    assert "src/api.py" in problems[0]


def test_a_declared_rewrite_settles_a_candidate() -> None:
    """A resolution that rewrites what it merges is correct, not suspect."""
    settled = report(
        dispositions=[
            HunkDisposition(
                path=Path("src/api.py"),
                parent=PARENT,
                fate="rewritten",
                rationale="folded into the signature the sibling introduced",
            )
        ],
        out_of_conflict_edits=[DeclaredEdit(path=Path("src/api.py"), rationale="same")],
    )

    assert merge_problems(settled, [], [candidate("src/api.py")]) == []


def test_a_disposition_keyed_by_the_abbreviation_the_merger_was_shown_settles() -> None:
    """The merger is handed twelve characters and was keyed against forty.

    `merge_turn` renders each candidate as `(from {parent[:12]})`, so echoing
    the parent back is echoing an abbreviation — which never equalled the full
    sha the check compared, while the refusal quoted those same twelve
    characters at it. No revision could converge: one observed merger
    dispositioned all three of its candidates with correct rationales, twice,
    and the run failed on the second. The fixtures missed it because this
    file's PARENT is itself twelve characters, so both spellings coincided.
    """
    full = "76d6060e49d0c0c128417733547232db1445c1dc"
    shown = full[:12]
    settled = report(
        dispositions=[
            HunkDisposition(
                path=Path("src/api.py"),
                parent=shown,
                fate="rewritten",
                rationale="folded into the constant the sibling introduced",
            )
        ],
        out_of_conflict_edits=[DeclaredEdit(path=Path("src/api.py"), rationale="same")],
    )
    owed = [DropCandidate(parent=full, path=Path("src/api.py"), missing=["gone"])]

    assert merge_problems(settled, [], owed) == []


def test_a_disposition_naming_a_different_parent_settles_nothing() -> None:
    """Forgiving the abbreviation must not forgive naming the wrong commit."""
    settled = report(
        dispositions=[
            HunkDisposition(
                path=Path("src/api.py"),
                parent="ffffffffffff",
                fate="rewritten",
                rationale="a real reason about the wrong parent",
            )
        ],
        out_of_conflict_edits=[DeclaredEdit(path=Path("src/api.py"), rationale="same")],
    )
    owed = [DropCandidate(parent=PARENT, path=Path("src/api.py"), missing=["gone"])]

    assert len(merge_problems(settled, [], owed)) == 1


def test_dispositioning_without_a_reason_does_not_settle_anything() -> None:
    silent = report(
        dispositions=[
            HunkDisposition(
                path=Path("src/api.py"), parent=PARENT, fate="dropped", rationale="  "
            )
        ]
    )

    problems = merge_problems(silent, [Path("src/api.py")], [candidate("src/api.py")])

    assert any("rationale" in problem for problem in problems)


def test_more_dispositions_than_candidates_is_never_a_problem() -> None:
    """Containment, not equality — a merger may account for more than it was asked."""
    generous = report(
        dispositions=[
            HunkDisposition(
                path=Path("src/other.py"),
                parent=PARENT,
                fate="kept",
                rationale="carried through unchanged",
            )
        ]
    )

    assert merge_problems(generous, [], []) == []


def test_a_clean_file_edited_without_declaring_it_is_the_rejection() -> None:
    """The canonical joint failure is fixed in a file that never conflicted.

    So the rule cannot be that changed files stay within conflicted files —
    it is that going outside is declared.
    """
    undeclared = report(
        dispositions=[
            HunkDisposition(
                path=Path("src/caller.py"),
                parent=PARENT,
                fate="rewritten",
                rationale="updated for the new signature",
            )
        ]
    )

    problems = merge_problems(undeclared, [Path("src/api.py")], [])

    assert any("src/caller.py" in problem for problem in problems)


def test_declaring_the_clean_file_edit_permits_it() -> None:
    declared = report(
        dispositions=[
            HunkDisposition(
                path=Path("src/caller.py"),
                parent=PARENT,
                fate="rewritten",
                rationale="updated for the new signature",
            )
        ],
        out_of_conflict_edits=[
            DeclaredEdit(
                path=Path("src/caller.py"),
                rationale="its call site still used the old arity",
            )
        ],
    )

    assert merge_problems(declared, [Path("src/api.py")], []) == []


def test_a_kept_hunk_inside_the_conflict_set_needs_no_declaration() -> None:
    kept = report(
        dispositions=[
            HunkDisposition(
                path=Path("src/api.py"),
                parent=PARENT,
                fate="kept",
                rationale="both sides agreed",
            )
        ]
    )

    assert merge_problems(kept, [Path("src/api.py")], []) == []


def test_the_containment_gate_names_the_paths_it_rejected_over() -> None:
    """A round that cannot read which path failed re-derives the same report."""
    reason = DeclarationDelta(
        undeclared=["src/new.py"], unswept=["src/stale.py"]
    ).reason

    assert "src/new.py" in reason
    assert "src/stale.py" in reason


def test_over_reporting_alone_passes_the_containment_gate() -> None:
    """Nothing changed undeclared is containment; equality cost 71 files."""
    assert DeclarationDelta().settled


def test_a_concern_may_join_a_run_that_has_already_started() -> None:
    """Discovering work mid-run was a choice between dropping it and restarting."""
    started = run_state([planned("a")])
    widened = run_state([planned("a"), planned("b")])

    validate_concern_admission(started, widened)


def test_an_admitted_concern_can_never_change() -> None:
    """Resume integrity is what append-only preserves and editing would break."""
    started = run_state([planned("a")])
    edited = run_state([planned("a", title="something else")])

    with pytest.raises(StateTransitionError, match="immutable"):
        validate_concern_admission(started, edited)


def test_a_concern_can_never_be_dropped() -> None:
    started = run_state([planned("a"), planned("b")])
    narrowed = run_state([planned("a")])

    with pytest.raises(StateTransitionError, match="append-only"):
        validate_concern_admission(started, narrowed)


def test_a_successor_must_name_a_concern_this_run_holds() -> None:
    started = run_state([planned("a")])
    orphaned = run_state([planned("a"), planned("b", supersedes="ghost")])

    with pytest.raises(StateTransitionError, match="no concern in this run"):
        validate_concern_admission(started, orphaned)


def test_superseding_leaves_the_predecessor_in_the_record() -> None:
    """A run is evidence of what was tried; a correction must not erase that."""
    started = run_state([planned("a")])
    corrected = run_state([planned("a"), planned("b", supersedes="a")])

    validate_concern_admission(started, corrected)


@pytest.fixture
def approval_repo(tmp_path: Path) -> Path:
    """A real repository: recoverability is Git's answer, not a fake's.

    A launcher can only say whether a path is tracked, which is the weaker
    question and lets a modified file read as recoverable. Asking Git itself
    is what makes the uncommitted-work case expressible.
    """
    work = tmp_path / "repo"
    (work / "src").mkdir(parents=True)
    git = initialized_repo(work, tmp_path / "no-hooks")
    (work / "src" / "old.py").write_text("value = 1\n", encoding="utf-8")
    (work / "src" / "edited.py").write_text("value = 2\n", encoding="utf-8")
    git("add", "src/old.py", "src/edited.py")
    git("commit", "-m", "chore: base")
    (work / "src" / "edited.py").write_text("value = 3\n", encoding="utf-8")
    (work / "scratch.txt").write_text("untracked\n", encoding="utf-8")
    return work


def test_an_agent_may_approve_removing_a_committed_file(approval_repo: Path) -> None:
    """The object store is the recovery, so the deletion is not permanent."""
    assert agent_may_approve("rm src/old.py", approval_repo)


def test_only_a_human_may_approve_removing_an_untracked_file(
    approval_repo: Path,
) -> None:
    """Nothing holds a copy, so nobody can undo it afterwards."""
    assert not agent_may_approve("rm scratch.txt", approval_repo)


def test_only_a_human_may_approve_removing_uncommitted_edits(
    approval_repo: Path,
) -> None:
    """Tracked is not recoverable: the object store holds the older text.

    Approving this discarded the working-copy change, which is precisely the
    work nothing could restore afterwards.
    """
    assert not agent_may_approve("rm src/edited.py", approval_repo)


def test_only_a_human_may_approve_removing_a_directory(approval_repo: Path) -> None:
    """Nothing in the command bounds what the directory holds."""
    assert not agent_may_approve("rm -rf src", approval_repo)


def test_only_a_human_may_approve_discarding_uncommitted_work(
    approval_repo: Path,
) -> None:
    assert not agent_may_approve("git reset --hard HEAD", approval_repo)
    assert not agent_may_approve("git checkout -- .", approval_repo)


def test_only_a_human_may_approve_anything_that_leaves_this_machine(
    approval_repo: Path,
) -> None:
    assert not agent_may_approve("git push --force origin main", approval_repo)


class BranchLauncher(ProcessLauncher):
    """Report one checked-out branch and refuse anything else."""

    def __init__(self, branch: str) -> None:
        self.branch = branch

    def launch(self, request: LaunchRequest) -> ExitStatus:
        assert request.arguments == ["git", "branch", "--show-current"]
        return ExitStatus(code=0, stdout=f"{self.branch}\n", stderr="")


def test_a_fresh_run_mints_its_own_review_branch() -> None:
    branch = integration_branch(BranchLauncher("dev"), Path("."), "r1")

    assert branch == "resolve/r1/review"


def test_a_run_launched_on_a_review_branch_advances_that_branch() -> None:
    """A nested run is resolving that branch's own feedback.

    Minting a second review branch strands the work somewhere nobody asked
    for and leaves the human two branches to reconcile.
    """
    branch = integration_branch(
        BranchLauncher("resolve/earlier/review"), Path("."), "r2"
    )

    assert branch == "resolve/earlier/review"


def test_a_branch_merely_named_like_one_does_not_count() -> None:
    branch = integration_branch(BranchLauncher("resolve/r1/wip"), Path("."), "r3")

    assert branch == "resolve/r3/review"
