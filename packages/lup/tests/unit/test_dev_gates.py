"""What a bracketed deferral's condition is resolved to, and when it wakes."""

from pathlib import Path

import pytest

import lup.devtools.dev.gates as gates
from lup.harness.codescan.markers import (
    MarkerComment,
    NoteKind,
)
from lup.execution.shell import git
from lup.devtools.dev.branches import delete_branch, get_integration_branch
from lup.devtools.dev.comments import FoundComment
from lup.devtools.dev.gates import (
    BranchInPlay,
    Gate,
    GateVerdict,
    PathGone,
    current_branch,
    inbound_notes,
    parse_gate,
    sweep_gates,
)


class AlwaysFires(Gate, frozen=True):
    """A gate with a fixed answer, so a sweep can be tested without a checkout."""

    keyword = "always"

    def asked(self) -> GateVerdict:
        return GateVerdict(fired=True, evidence=f"always, for {self.argument}")


class NeverFires(Gate, frozen=True):
    """The other fixed answer."""

    keyword = "never"

    def asked(self) -> GateVerdict:
        return GateVerdict(fired=False, evidence="never")


FIXED: list[type[Gate]] = [AlwaysFires, NeverFires]


def parked(
    condition: str | None, text: str = "park it", where: str = "a.py"
) -> FoundComment:
    """One deferred note carrying *condition*, as a scan would have found it."""
    return FoundComment(
        start_line=1,
        end_line=1,
        read_start=1,
        read_end=1,
        text=text,
        kind=NoteKind.defer,
        condition=condition,
        file=where,
        context="",
    )


def test_a_condition_naming_a_declared_keyword_resolves_to_that_gate() -> None:
    gate = parse_gate("branch:content-overhaul")
    assert isinstance(gate, BranchInPlay)
    assert gate.argument == "content-overhaul"
    assert gate.spelling() == "branch:content-overhaul"


def test_a_condition_stating_prose_stays_prose() -> None:
    # The spelling every deferral used before any of them could be resolved,
    # and the one a gate this checkout cannot see still has to be written in.
    assert parse_gate("content-overhaul lands") is None
    assert parse_gate("until the v2 API ships") is None


def test_prose_that_happens_to_contain_a_colon_is_still_prose() -> None:
    # A keyword is one word, which is the whole of what separates the two.
    assert parse_gate("when the v2 API ships: probably next quarter") is None


def test_a_colon_after_an_undeclared_keyword_is_prose_rather_than_an_error() -> None:
    # A gate this union does not declare is a real condition somebody stated,
    # not a typo — so it degrades to the listing rather than to a failure.
    assert parse_gate("milestone:4") is None


def test_a_deferral_stating_no_condition_resolves_to_nothing() -> None:
    assert parse_gate(None) is None


def test_whitespace_around_the_argument_is_not_part_of_it() -> None:
    gate = parse_gate("branch:  content-overhaul  ")
    assert gate is not None
    assert gate.argument == "content-overhaul"


def test_a_gate_naming_nothing_fires_rather_than_passing_quietly() -> None:
    # `branch:` with no branch is a typo, and the loud reading is the safe one:
    # a gate that silently never fires is indistinguishable from one that has
    # not come true yet, which is the failure this whole module exists to end.
    assert BranchInPlay(argument="").asked().fired
    assert PathGone(argument="").asked().fired


def test_a_path_gate_reads_the_tree_it_is_checked_against() -> None:
    present = PathGone(argument="pyproject.toml").asked()
    assert not present.fired
    assert "still exists" in present.evidence
    absent = PathGone(argument="no/such/file/anywhere.txt").asked()
    assert absent.fired


def test_a_branch_gate_fires_on_the_branch_it_names() -> None:
    # The moment the whole gate exists for. A note about a branch is written
    # for whoever works on that branch, and they are the last to see it: it
    # lives where its author stood, and their checkout carries no copy until
    # they merge. Waking on arrival only would reach them one step after the
    # step it was written to change.
    standing_on = current_branch()
    if not standing_on:
        pytest.skip("detached head — CI checks out a ref, not a branch")
    standing = BranchInPlay(argument=standing_on).asked()
    assert standing.fired
    assert "you are on" in standing.evidence


def test_a_branch_gate_fires_once_its_branch_is_in_the_integration_branch() -> None:
    # The integration branch is trivially its own ancestor, which is the one
    # landed branch whose name is stable enough to pin: any feature branch
    # named here would land eventually and turn this into a test about how old
    # the checkout is. Either reason is a pass — running this from the
    # integration branch itself reaches the same verdict by the other route.
    gate = BranchInPlay(argument=get_integration_branch())
    if not gate.visible():
        pytest.skip("this checkout cannot see the integration branch")
    landed = gate.asked()
    assert landed.fired
    assert get_integration_branch() in landed.evidence


def test_a_branch_this_checkout_cannot_see_stays_dormant() -> None:
    # The ruling that keeps the gate usable at all. A branch pruned after
    # landing and a branch never fetched are the same missing ref, and the
    # second is the ordinary case: a CI job clones the ref under test and
    # nothing else, so every feature branch is absent there. Firing on absence
    # would turn every build red for conditions none of which came true —
    # exactly the branch a reader learns to ignore.
    unseen = BranchInPlay(argument="no-such-branch-was-ever-pushed").asked()
    assert not unseen.fired
    assert "not in this checkout" in unseen.evidence


def test_a_checkout_missing_every_feature_branch_wakes_nothing() -> None:
    # The CI shape, stated as the property rather than as one branch's luck:
    # a sweep over gates naming branches none of which are present must come
    # back empty, not with one entry per gate.
    sweep = sweep_gates(
        [parked("branch:absent-one"), parked("branch:absent-two")],
    )
    assert sweep.asked == 2
    assert sweep.woken == []


def scratch_repo(root: Path) -> None:
    """A one-commit repository on `main`, which `dev delete` can be run inside.

    Built rather than borrowed, because the subject here is a branch being
    deleted and this repository's own branches are not available for that.
    """
    root.mkdir()
    git.out("-C", str(root), "init", "--initial-branch=main")
    git.out("-C", str(root), "config", "commit.gpgsign", "false")
    git.out("-C", str(root), "config", "core.hooksPath", str(root / "absent-hooks"))
    git.out("-C", str(root), "config", "user.email", "test@example.invalid")
    git.out("-C", str(root), "config", "user.name", "test")
    (root / "file.txt").write_text("base\n", encoding="utf-8")
    git.out("-C", str(root), "add", "file.txt")
    git.out("-C", str(root), "commit", "-m", "chore: base")


def worked_on(root: Path, branch: str, carries: str = "work.txt") -> None:
    """Cut *branch*, commit the file only it carries, and return to `main`.

    The file is named by the caller so a name cut a second time commits
    something the first branch did not already land.
    """
    git.out("-C", str(root), "checkout", "-q", "-b", branch)
    (root / carries).write_text("work\n", encoding="utf-8")
    git.out("-C", str(root), "add", carries)
    git.out("-C", str(root), "commit", "-m", f"feat: {branch}")
    git.out("-C", str(root), "checkout", "-q", "main")


def test_a_branch_deleted_after_it_landed_fires_from_what_was_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The branch a gate reading only live refs is silent about forever.

    Landing and deleting in one sweep, with no check in between, left nothing
    to read: containment lives on the ref, the deletion takes the ref, and
    absence is dormant by a ruling that has to stay. `dev delete` judges
    containment while the ref is still there, so the verdict is written down
    where it outlives the branch.
    """
    root = tmp_path / "landed"
    scratch_repo(root)
    worked_on(root, "topic")
    git.out("-C", str(root), "merge", "--ff-only", "topic")
    monkeypatch.chdir(root)

    delete_branch("topic", dry_run=False, force=False)

    verdict = BranchInPlay(argument="topic").asked()
    assert verdict.fired
    assert "reached main" in verdict.evidence


def test_a_branch_deleted_holding_work_the_integration_branch_lacks_is_dormant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Discarding is not landing, and the record says which one happened."""
    root = tmp_path / "discarded"
    scratch_repo(root)
    worked_on(root, "topic")
    monkeypatch.chdir(root)

    delete_branch("topic", dry_run=False, force=True)

    verdict = BranchInPlay(argument="topic").asked()
    assert not verdict.fired
    assert "not in this checkout" in verdict.evidence


def test_a_checkout_holding_neither_the_ref_nor_a_record_stays_dormant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The CI shape, which is what the dormancy ruling exists for.

    A job clones the ref under test and nothing else, so every feature branch
    is missing there — and a clone that never fetched a branch never deleted
    one either, so it holds no record of any landing to read.
    """
    root = tmp_path / "clone"
    scratch_repo(root)
    monkeypatch.chdir(root)

    verdict = BranchInPlay(argument="topic").asked()
    assert not verdict.fired
    assert "not in this checkout" in verdict.evidence


def test_a_clone_with_no_local_branch_still_judges_what_landed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other CI shape: every ref is there, and none of them is local.

    A pull request's checkout stands on a detached head and holds each branch
    as a remote-tracking ref alone. Both the subject and the branch it is
    judged against are read through ``origin/``: read locally, the second
    hands `merge-base --is-ancestor` a name resolving to nothing, which fails
    on its second argument. Every landing would then read as "has not
    reached" -- including the integration branch's own, one ref declining to
    have reached itself -- so the gate could never fire on a clone.
    """
    origin = tmp_path / "origin"
    scratch_repo(origin)
    worked_on(origin, "topic")
    git.out("-C", str(origin), "merge", "--ff-only", "topic")

    clone = tmp_path / "ci"
    git.out("clone", "--quiet", str(origin), str(clone))
    git.out("-C", str(clone), "switch", "--quiet", "--detach", "HEAD")
    git.out("-C", str(clone), "branch", "-D", "main")
    monkeypatch.chdir(clone)

    verdict = BranchInPlay(argument="topic").asked()
    assert verdict.fired
    assert "has reached" in verdict.evidence


def test_a_name_cut_again_answers_from_its_ref_rather_than_the_old_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The record is what absence is asked, and only absence.

    A name reused after its first branch landed carries that landing in its
    record, and a second branch of that name is a different branch. The ref is
    back, so it is read, and it answers for itself.
    """
    root = tmp_path / "reused"
    scratch_repo(root)
    worked_on(root, "topic")
    git.out("-C", str(root), "merge", "--ff-only", "topic")
    monkeypatch.chdir(root)
    delete_branch("topic", dry_run=False, force=False)

    worked_on(root, "topic", carries="second.txt")

    verdict = BranchInPlay(argument="topic").asked()
    assert not verdict.fired
    assert "has not reached main" in verdict.evidence


def planted_deferral(root: Path, names: str) -> None:
    """A one-commit repository holding one deferral aimed at *names*."""
    root.mkdir()
    git.out("-C", str(root), "init", "--initial-branch=main")
    git.out("-C", str(root), "config", "user.email", "test@example.invalid")
    git.out("-C", str(root), "config", "user.name", "test")
    (root / "sample.py").write_text(
        f"# lup: defer[branch:{names}]: waits on the branch it names\nvalue = 1\n",
        encoding="utf-8",
    )
    git.out("-C", str(root), "add", "sample.py")
    git.out("-C", str(root), "commit", "-m", "plant a deferral")


def test_a_note_on_another_ref_reaches_the_branch_it_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The half that makes any of this arrive in time.

    Planted in a repository this test builds, rather than read off whichever
    deferral happens to be in the tree. A test naming a live one goes red the
    day somebody resolves it — a gate test failing because the gate worked,
    which teaches the wrong lesson twice: the note is answered, and the
    mechanism under test is fine.
    """
    root = tmp_path / "planted"
    planted_deferral(root, "elsewhere")
    monkeypatch.setattr(gates, "project_root", lambda: root)

    inbound = inbound_notes("elsewhere", "HEAD")

    assert inbound, "the planted deferral naming `elsewhere` was not found"
    assert all(note.condition == "branch:elsewhere" for note in inbound)
    assert all(note.kind == NoteKind.defer for note in inbound)


def test_a_note_naming_a_different_branch_stays_on_its_own_ref(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Naming is what keeps this quiet, against the same plant."""
    root = tmp_path / "planted"
    planted_deferral(root, "elsewhere")
    monkeypatch.setattr(gates, "project_root", lambda: root)

    assert inbound_notes("some-other-branch", "HEAD") == []


def test_a_branch_nobody_left_a_note_about_gets_nothing() -> None:
    # Naming is what keeps this quiet. A check that surfaced every deferral on
    # the integration branch would put the same wall of text in front of every
    # branch in the repository.
    assert inbound_notes("some-branch-no-note-mentions", "HEAD") == []


def test_a_detached_head_asks_no_branch_question_rather_than_erroring() -> None:
    assert inbound_notes("", "HEAD") == []


def test_a_ref_that_does_not_resolve_is_quiet_rather_than_fatal() -> None:
    # `dev check` runs in checkouts that may not carry the integration branch
    # at all — a fresh clone of one branch, a CI job fetched shallow. Failing
    # there would refuse the whole check over a question it merely could not
    # put.
    assert inbound_notes("content-overhaul", "no-such-ref-anywhere") == []


def test_a_sweep_counts_only_the_gates_it_could_put() -> None:
    # Prose was never a question this checkout could ask, so folding it into
    # the total would report coverage the sweep does not have.
    sweep = sweep_gates(
        [parked("never:x"), parked("some prose"), parked(None)], declared=FIXED
    )
    assert sweep.asked == 1
    assert sweep.woken == []


def test_a_fired_gate_carries_the_note_and_the_evidence_whole() -> None:
    sweep = sweep_gates(
        [parked("always:reason", text="the whole instruction", where="b.py")],
        declared=FIXED,
    )
    assert sweep.asked == 1
    assert len(sweep.woken) == 1
    woken = sweep.woken[0]
    assert woken.file == "b.py"
    assert woken.gate == "always:reason"
    assert woken.evidence == "always, for reason"
    assert woken.text == "the whole instruction"
    assert "the whole instruction" in woken.reported()


def test_a_sweep_with_nothing_fired_says_how_many_it_asked() -> None:
    sweep = sweep_gates([parked("never:x"), parked("never:y")], declared=FIXED)
    assert sweep.lines() == ["woken deferrals: ok, 2 gate(s) asked"]


def test_a_sweep_that_fired_leads_with_the_failure() -> None:
    sweep = sweep_gates([parked("always:x"), parked("never:y")], declared=FIXED)
    assert sweep.lines()[0] == "woken deferrals: FAIL (1 of 2 gate(s) fired)"


def test_only_a_defer_note_can_carry_a_condition_at_all() -> None:
    # The coherence the marker model already enforces, pinned here because the
    # sweep reads `condition` off every note it is handed and would otherwise
    # be trusting an invariant nothing in this module states.
    with pytest.raises(ValueError):
        MarkerComment(
            start_line=1,
            end_line=1,
            read_start=1,
            read_end=1,
            text="x",
            kind=NoteKind.note,
            condition="branch:whatever",
        )


def test_a_note_the_working_tree_answered_as_solved_stays_answered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The documented resolution, read where the branch wrote it.

    Resolving a woken deferral is rewriting it as `solved:` with its words
    unchanged. The branch it names does that in its own tree, while the
    integration branch keeps the deferral until the branch lands — so a gate
    reading only the integration copy would stay red on the one branch that
    has done what the note asked, with no way to clear it from there.
    """
    root = tmp_path / "planted"
    planted_deferral(root, "elsewhere")
    (root / "sample.py").write_text(
        "# lup: solved: waits on the branch it names\nvalue = 1\n", encoding="utf-8"
    )
    monkeypatch.setattr(gates, "project_root", lambda: root)

    assert inbound_notes("elsewhere", "HEAD") == []


def test_a_solved_claim_in_other_words_leaves_the_note_standing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A claim answers the note whose words it carries, not whichever is near."""
    root = tmp_path / "planted"
    planted_deferral(root, "elsewhere")
    (root / "sample.py").write_text(
        "# lup: solved: some other note entirely\nvalue = 1\n", encoding="utf-8"
    )
    monkeypatch.setattr(gates, "project_root", lambda: root)

    assert [note.text for note in inbound_notes("elsewhere", "HEAD")] == [
        "waits on the branch it names"
    ]
