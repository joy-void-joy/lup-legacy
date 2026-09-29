"""Work crossing to somebody else, and what refuses to cross without it.

Written against the failures the worked examples produced: a handoff whose
receiver had to re-derive what the sender already knew, a result nobody could
check because its source went unrecorded, and a scope that quietly took a lock
from a session still writing under it.
"""

from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from lup.coordination import watch as watching
from lup.coordination.briefs import (
    DetachedBrief,
    PeerBrief,
    PersonBrief,
    render_brief,
)
from lup.coordination.delegate import delegate
from lup.coordination.handoffs import Established, Handoff, hand_off
from lup.coordination.identity import member_ref, mint_member_id
from lup.coordination.refs import ActorRef
from lup.coordination.repository import RepositoryPeers
from lup.coordination.tasks import Task
from lup.coordination.wake import WakePath, Woken
from lup.ledger.journal import LedgerStore


def repository(root: Path) -> tuple[RepositoryPeers, LedgerStore, str]:
    """One repository with a session on its roster and a store to record in."""
    peers = RepositoryPeers(root)
    member = mint_member_id()
    peers.join(member, root / "tree", cli_name="sender")
    store = LedgerStore(root, ActorRef(kind="session", id=member))
    return peers, store, member


def test_a_handoff_with_nothing_open_is_refused_at_construction() -> None:
    """Somebody who has settled everything is finishing, not handing over — and
    the requirement is a field so nothing has to remember to check it.
    """
    with pytest.raises(ValidationError):
        Handoff(
            id="h1",
            title="the work",
            author=ActorRef(kind="session", id="a"),
            at=datetime.now(UTC),
            open_questions=[],
        )


def test_a_result_without_a_source_or_a_grade_is_refused() -> None:
    """An ungraded result is one the receiver must re-derive to trust, which is
    the cost the handoff exists to remove.
    """
    with pytest.raises(ValidationError):
        Established(statement="the parser handles nested quotes", source="", grade="M")
    with pytest.raises(ValidationError):
        Established(statement="the parser handles nested quotes", source="x", grade="")


def test_a_handoff_moves_the_tasks_it_names(tmp_path: Path) -> None:
    """The holder changes and the edge records which handoff moved it, so a task
    can be read back to the body of work it arrived with.
    """
    peers, store, _member = repository(tmp_path)
    receiver = mint_member_id()
    peers.join(receiver, tmp_path / "other", cli_name="receiver")
    task = store.record(Task, "finish the parser")

    result = hand_off(
        peers,
        store,
        "the parser work",
        open_questions=["whether escapes nest"],
        to="receiver",
        tasks=[task.id],
    )

    assert result.transferred == [task.id]
    [moved] = [found for found in store.read(Task) if found.id == task.id]
    assert moved.holder == "receiver"
    assert [edge.target for edge in store.out_of(result.handoff.id)] == [task.id]


def test_a_lock_the_sender_holds_moves_with_the_work(tmp_path: Path) -> None:
    """That is what a handoff is: the sender is done there and the receiver is
    not, so the claim should say the receiver's name.
    """
    peers, store, member = repository(tmp_path)
    receiver = mint_member_id()
    peers.join(receiver, tmp_path / "other", cli_name="receiver")
    scope = tmp_path / "packages" / "parser"
    scope.mkdir(parents=True)
    peers.lock(member, scope)

    result = hand_off(
        peers,
        store,
        "the parser work",
        open_questions=["whether escapes nest"],
        to="receiver",
        paths=[str(scope)],
    )

    assert result.locked == [str(scope)]
    assert not result.contested
    holders = {holder.id for claim in peers.holding(scope) for holder in claim.holders}
    assert holders == {receiver}


def test_a_lock_somebody_else_holds_is_contested_rather_than_taken(
    tmp_path: Path,
) -> None:
    """Only a holder can release a lock, so a scope naming somebody else's claim
    records the overlap and hands over anyway — refusing would make handing work
    over expensive enough to skip, and taking it would revoke a claim from a
    session still writing under it.
    """
    peers, store, _member = repository(tmp_path)
    third = mint_member_id()
    peers.join(third, tmp_path / "third", cli_name="third")
    receiver = mint_member_id()
    peers.join(receiver, tmp_path / "other", cli_name="receiver")
    scope = tmp_path / "packages" / "parser"
    scope.mkdir(parents=True)
    peers.lock(third, scope)

    result = hand_off(
        peers,
        store,
        "the parser work",
        open_questions=["whether escapes nest"],
        to="receiver",
        paths=[str(scope)],
    )

    assert result.contested == [str(scope)]
    assert not result.locked
    holders = {holder.id for claim in peers.holding(scope) for holder in claim.holders}
    assert third in holders


def test_a_name_nobody_answers_to_leaves_the_work_rather_than_refusing(
    tmp_path: Path,
) -> None:
    """Work is handed off at the end of a session as often as to somebody in
    particular, and refusing would drop the record exactly when it matters most.
    """
    _peers, store, _member = repository(tmp_path)
    peers = RepositoryPeers(tmp_path)
    task = store.record(Task, "finish the parser")

    result = hand_off(
        peers,
        store,
        "the parser work",
        open_questions=["whether escapes nest"],
        to="nobody",
        tasks=[task.id],
    )

    assert not result.to
    assert result.transferred == [task.id]
    assert "nobody" in result.note


def woken_receiver(
    peers: RepositoryPeers, root: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[str, list[str]]:
    """A receiver whose runtime accepts every wake, and every wake it was sent."""
    receiver = mint_member_id()
    peers.join(
        receiver,
        root / "other",
        cli_name="receiver",
        wake=WakePath(runtime="claude", handle=str(root / "receiver.sock")),
    )
    woken: list[str] = []

    def nudged(
        path: WakePath,
        message: str,
        cwd: Path | None = None,
        *,
        queue_timeout_seconds: float = 20.0,
    ) -> Woken:
        del path, cwd, queue_timeout_seconds
        woken.append(message)
        return Woken(reached=True)

    monkeypatch.setattr(watching, "wake", nudged)
    return receiver, woken


def test_a_handoff_the_wake_carried_is_not_handed_over_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The wake carries the mail whole, so the receiver's hook at its next call
    has nothing of it left to put in front of it a second time.
    """
    peers, store, _member = repository(tmp_path)
    receiver, woken = woken_receiver(peers, tmp_path, monkeypatch)

    result = hand_off(
        peers,
        store,
        "the parser work",
        open_questions=["whether escapes nest"],
        to="receiver",
    )

    assert result.woken
    assert len(woken) == 1 and "the parser work" in woken[0]
    assert peers.waiting(receiver).messages == []


def test_a_delegation_the_wake_carried_is_not_handed_over_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    peers, store, _member = repository(tmp_path)
    receiver, woken = woken_receiver(peers, tmp_path, monkeypatch)

    result = delegate(peers, store, "finish the parser", to="receiver")

    assert result.woken
    assert len(woken) == 1 and "finish the parser" in woken[0]
    assert peers.waiting(receiver).messages == []


def written(root: Path) -> tuple[Handoff, list[Task]]:
    """One recorded handoff and the task it carries, for the renderings."""
    peers, store, _member = repository(root)
    task = store.record(Task, "finish the parser", text="the escape cases remain")
    result = hand_off(
        peers,
        store,
        "the parser work",
        open_questions=["whether escapes nest"],
        text="what is left of the tokenizer",
        established=[
            Established(
                statement="quotes nest to depth two",
                source="tests/test_parser.py",
                grade="measured",
            )
        ],
        not_again=["a regex pass — it cannot see depth"],
        tasks=[task.id],
    )
    return result.handoff, [found for found in store.read(Task) if found.id == task.id]


def test_every_rendering_carries_the_grade_and_what_not_to_redo(
    tmp_path: Path,
) -> None:
    """The renderings differ in form and never in substance: a reader who got a
    different rendering must not have got less.
    """
    handoff, tasks = written(tmp_path)

    for audience in (PeerBrief(), DetachedBrief(), PersonBrief()):
        rendered = render_brief(handoff, tasks, audience)
        assert "measured" in rendered
        assert "quotes nest to depth two" in rendered
        assert "a regex pass" in rendered
        assert "whether escapes nest" in rendered
        assert handoff.id in rendered


def test_the_detached_rendering_inlines_what_a_peer_would_look_up(
    tmp_path: Path,
) -> None:
    """An agent with no repository can resolve nothing, so a task's own text has
    to arrive with it rather than behind an id.
    """
    handoff, tasks = written(tmp_path)

    detached = render_brief(handoff, tasks, DetachedBrief())
    peer = render_brief(handoff, tasks, PeerBrief())

    assert "the escape cases remain" in detached
    assert "the escape cases remain" not in peer
    assert "without the repository" in detached


def test_a_handoff_is_done_when_every_task_it_moved_is(tmp_path: Path) -> None:
    """Standing is read from the work rather than stored on the handoff, so a
    record cannot go on saying "transferred" after the receiver finished.
    """
    peers, store, _member = repository(tmp_path)
    receiver = mint_member_id()
    peers.join(receiver, tmp_path / "other", cli_name="receiver")
    task = store.record(Task, "finish the parser")
    result = hand_off(
        peers,
        store,
        "the parser work",
        open_questions=["whether escapes nest"],
        to="receiver",
        tasks=[task.id],
    )

    assert store.standing(result.handoff, [Task]).label == "held"

    [moved] = [found for found in store.read(Task) if found.id == task.id]
    store.amend(moved.completed())

    assert store.standing(result.handoff, [Task]).label == "done"
    # Without the classes the far end resolves as the base, which declines
    # `finished` — the honest reading for a build lacking the declaring module,
    # and the reason a caller that wants the real answer names the types.
    assert store.standing(result.handoff).label == "held"


def test_a_member_ref_is_what_the_roster_resolves(tmp_path: Path) -> None:
    """Guards the assumption the contest path rests on: a rival recorded on a
    touch is the same reference the roster hands back.
    """
    peers, _store, member = repository(tmp_path)

    assert peers.address("sender") == member_ref(member)
