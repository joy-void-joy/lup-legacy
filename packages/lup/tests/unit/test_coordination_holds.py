"""A hold keeps the next tool call of every agent it covers waiting until it is lifted.

The operator pauses an agent, its session with everything it spawned, a
repository, or every repository; the budget governor holds one conversation
inside a limit. Each is a file in the store, and the hook of the agent it
covers reads it before each call: who it covers is worked out from the roster
at that moment, so an agent that arrives after a pause is under it too, and a
hold ends when it is lifted, when its own ``until`` passes, or when the member
it names leaves.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from lup.coordination.bare import holds as bare
from lup.coordination.bare import store
from lup.coordination.holds import (
    Hold,
    HoldOwner,
    HoldReason,
    HoldScope,
    held_calls,
    holding,
    lift,
    operator_pause,
    place,
    placed,
    standing,
    swept,
)
from lup.coordination.repository import RepositoryPeers


class Family:
    """One repository's roster: a session, its subagent and that subagent's fork,
    a runtime started from the session's shell, and an unrelated session."""

    def __init__(self, root: Path) -> None:
        self.peers = RepositoryPeers(root)
        tree = root / "tree"
        tree.mkdir(exist_ok=True)
        self.session = "a1b2c3d4e5f6"
        self.peers.join(self.session, tree, cli_name="lead")
        self.subagent = store.subagent_id(self.session, "a01")
        self.peers.join_subagent(self.session, store.Caller(agent_id="a01"))
        self.fork = store.subagent_id(self.session, "a02")
        self.peers.join_subagent(
            self.session, store.Caller(agent_id="a02", spawned_by="a01")
        )
        self.spawned = f"{store.spawned_prefix(self.session)}0f0f0f0f"
        self.peers.join(self.spawned, tree, spawned_by=self.session)
        self.stranger = "ffeeddccbbaa"
        self.peers.join(self.stranger, tree, cli_name="other")

    @property
    def root(self) -> Path:
        return self.peers.root

    def held(self, member: str, parent: str = "") -> bool:
        return bool(holding(self.root, member, parent))


@pytest.fixture
def family(tmp_path: Path) -> Family:
    return Family(tmp_path)


def test_nothing_placed_holds_nobody(family: Family) -> None:
    assert not any(
        family.held(member)
        for member in (family.session, family.subagent, family.spawned, family.stranger)
    )


def test_pausing_a_session_holds_its_subagents_and_not_what_its_shell_started(
    family: Family,
) -> None:
    place(family.root, operator_pause(HoldScope.AGENT, family.session))
    assert family.held(family.session)
    assert family.held(family.subagent)
    assert family.held(family.fork)
    assert not family.held(family.spawned)
    assert not family.held(family.stranger)


def test_pausing_a_subagent_holds_it_and_its_fork_and_not_its_session(
    family: Family,
) -> None:
    place(family.root, operator_pause(HoldScope.AGENT, family.subagent))
    assert family.held(family.subagent)
    assert family.held(family.fork)
    assert not family.held(family.session)


def test_a_hold_on_one_conversation_leaves_its_subagents_working(
    family: Family,
) -> None:
    place(
        family.root,
        Hold(
            scope=HoldScope.SELF,
            member=family.session,
            reason=HoldReason.SLOT,
            owner=HoldOwner.BUDGET,
            said="waiting for a slot",
        ),
    )
    assert family.held(family.session)
    assert not family.held(family.subagent)


def test_pausing_a_tree_holds_everything_the_session_spawned(family: Family) -> None:
    place(family.root, operator_pause(HoldScope.TREE, family.session))
    assert all(
        family.held(member)
        for member in (family.session, family.subagent, family.fork, family.spawned)
    )
    assert not family.held(family.stranger)


def test_a_subagent_the_roster_has_not_seen_is_held_under_its_session(
    family: Family,
) -> None:
    place(family.root, operator_pause(HoldScope.AGENT, family.session))
    newcomer = store.subagent_id(family.session, "a99")
    assert family.held(newcomer, parent=family.session)


def test_pausing_a_repository_holds_everyone(family: Family) -> None:
    place(family.root, operator_pause(HoldScope.REPOSITORY))
    assert family.held(family.stranger) and family.held(family.spawned)


def test_a_hold_lapses_at_its_until_and_ends_when_its_member_leaves(
    family: Family,
) -> None:
    place(
        family.root,
        Hold(
            scope=HoldScope.SELF,
            member=family.stranger,
            reason=HoldReason.WINDOW,
            owner=HoldOwner.BUDGET,
            said="window used up until 14:20",
            until=datetime.now(UTC) - timedelta(seconds=1),
        ),
    )
    place(family.root, operator_pause(HoldScope.AGENT, family.spawned))
    assert not family.held(family.stranger)
    store.depart(family.root, store.session_actor(family.spawned))
    assert not family.held(family.spawned)
    assert standing(family.root) == []
    assert {hold.member for hold in swept(family.root)} == {
        family.stranger,
        family.spawned,
    }
    assert placed(family.root) == []


def test_a_budget_release_never_ends_the_pause_and_a_resume_never_ends_a_budget_hold(
    family: Family,
) -> None:
    place(family.root, operator_pause(HoldScope.AGENT, family.session))
    place(
        family.root,
        Hold(
            scope=HoldScope.SELF,
            member=family.session,
            reason=HoldReason.RATE,
            owner=HoldOwner.BUDGET,
            said="over its rate",
        ),
    )
    lifted = lift(
        family.root,
        HoldOwner.BUDGET,
        HoldReason.RATE,
        HoldScope.SELF,
        family.session,
    )
    assert lifted is not None and lifted.owner is HoldOwner.BUDGET
    assert [hold.reason for hold in holding(family.root, family.session)] == [
        HoldReason.PAUSED
    ]


def test_placing_a_hold_again_replaces_it(family: Family) -> None:
    first = Hold(
        scope=HoldScope.SELF,
        member=family.session,
        reason=HoldReason.WINDOW,
        owner=HoldOwner.BUDGET,
        said="window used up until 14:20",
    )
    place(family.root, first)
    place(family.root, first.model_copy(update={"said": "window used up until 15:00"}))
    assert [hold.said for hold in placed(family.root)] == ["window used up until 15:00"]


def test_the_operator_s_words_are_said_first(family: Family) -> None:
    place(
        family.root,
        Hold(
            scope=HoldScope.SELF,
            member=family.session,
            reason=HoldReason.CAP,
            owner=HoldOwner.BUDGET,
            said="spend cap reached",
            placed=datetime.now(UTC) - timedelta(hours=1),
        ),
    )
    place(family.root, operator_pause(HoldScope.REPOSITORY))
    covering = bare.covering(family.root, family.session)
    assert bare.refusal(covering) == ("paused by the operator; this call didn't run")


class Clock:
    """A monotonic clock that moves only when the waiter sleeps on it."""

    def __init__(self, during=None) -> None:
        self.now = 1000.0
        self.slept: list[float] = []
        self.during = during

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        if self.during is not None:
            self.during(len(self.slept))
        self.now += seconds


def test_a_call_nothing_holds_goes_on_at_once_and_leaves_no_mark(
    family: Family,
) -> None:
    clock = Clock()
    held = bare.held_call(
        family.root,
        family.session,
        bare.Waiting(tool="Bash", call="toolu_1"),
        began=clock.now,
        seconds=60,
        clock=clock,
        pause=clock.sleep,
    )
    assert held == [] and clock.slept == []
    assert held_calls(family.root) == []


def test_a_held_call_is_marked_while_it_waits_and_goes_on_once_resumed(
    family: Family,
) -> None:
    place(family.root, operator_pause(HoldScope.AGENT, family.session))
    seen = []

    def resumed_on_third_look(looks: int) -> None:
        seen.append([held.member for held in held_calls(family.root)])
        if looks == 3:
            lift(
                family.root,
                HoldOwner.OPERATOR,
                HoldReason.PAUSED,
                HoldScope.AGENT,
                family.session,
            )

    clock = Clock(resumed_on_third_look)
    held = bare.held_call(
        family.root,
        family.subagent,
        bare.Waiting(tool="Read", call="toolu_2"),
        began=clock.now,
        seconds=3600,
        parent=family.session,
        clock=clock,
        pause=clock.sleep,
    )
    assert held == []
    assert seen == [[family.subagent]] * 3
    assert held_calls(family.root) == []


def test_a_call_still_held_at_the_limit_is_handed_the_holds_and_unmarked(
    family: Family,
) -> None:
    place(family.root, operator_pause(HoldScope.REPOSITORY))
    clock = Clock()
    held = bare.held_call(
        family.root,
        family.session,
        bare.Waiting(tool="Bash", call="toolu_3"),
        began=clock.now,
        seconds=2.5,
        clock=clock,
        pause=clock.sleep,
    )
    assert [hold.get("reason") for hold in held] == [store.PAUSED_REASON]
    assert clock.slept == [1.0, 1.0, 0.5]
    assert held_calls(family.root) == []
