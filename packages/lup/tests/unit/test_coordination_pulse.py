"""A session's row is present while its pulse says so, and gone when it stops.

Written against the failure the roster had: every session that ever joined
read as running forever, because the only record that ended a row was the one
a session wrote on its way out, and a killed session writes nothing. The pulse
is the member's own file now — its modification time, touched while the
session lives — so there is one fact rather than a record and a stamp beside
it, and no way for the two to disagree. What is asserted is the read a peer
makes: the listing, the claims that expire with a session, and the sweep that
moves a stopped session's file where a listing agrees with a stat.
"""

import asyncio
import os
from contextlib import suppress
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from lup.channels.models import utc_now
from lup.coordination.identity import mint_member_id
from lup.coordination.peer_tools import RosterPulse
from lup.coordination.bare.store import (
    DEPARTED_DIR,
    MEMBERS_DIR,
    beat,
    conversation_of,
    departed_path,
    member_path,
    session_actor,
    present,
)
from lup.coordination.pulse import Pulse
from lup.coordination.refs import ActorRef
from lup.coordination.repository import RepositoryPeers
from lup.coordination.roster import RosterMember
from lup.coordination.wake import WakePath
from lup.coordination.meeting import coordination_root

FOREVER = Pulse(stale_after_seconds=3600.0)
"""A window nothing in a test outlives, for the rows that must read as present."""

INSTANTLY = Pulse(stale_after_seconds=0.0)
"""A window nothing fits in, for the rows that must read as gone."""


def joined(root: Path, name: str, pulse: Pulse) -> tuple[RepositoryPeers, str]:
    """One repository's peers, with a session on the roster and this pulse."""
    peers = RepositoryPeers(root, pulse=pulse)
    member = mint_member_id()
    peers.join(member, root / "tree", cli_name=name)
    return peers, member


def row(
    peers: RepositoryPeers, member: str, at: datetime | None = None
) -> RosterMember:
    """The one row the listing holds for this member, as the pulse leaves it."""
    [found] = [one for one in peers.present(at) if one.actor.id == member]
    return found


def silent_since(peers: RepositoryPeers, member: str, ago: timedelta) -> None:
    """Put this member's file back in time, the way a stopped session leaves it.

    The modification time *is* the pulse, so backdating the file is the whole
    of what a session that stopped beating looks like — which is why nothing
    has to be appended anywhere to arrange one.
    """
    when = (utc_now() - ago).timestamp()
    os.utime(member_path(peers.root, session_actor(member)), (when, when))


def heard(peers: RepositoryPeers, member: str) -> datetime | None:
    """When this member was last heard from, as every reader reads it."""
    return row(peers, member).heard


def test_a_beat_touches_the_member_s_own_file_and_nothing_else(
    tmp_path: Path,
) -> None:
    """One fact rather than two: there is no stamp beside the row to disagree."""
    peers, member = joined(tmp_path, "mine", FOREVER)
    said = row(peers, member).description

    silent_since(peers, member, timedelta(hours=1))
    before = utc_now()
    beat(peers.root, session_actor(member))
    when = heard(peers, member)

    assert when is not None
    assert before - timedelta(seconds=1) <= when <= utc_now() + timedelta(seconds=1)
    assert row(peers, member).description == said
    assert not (peers.root / "heartbeats").exists()


def test_a_beat_for_a_member_that_never_joined_writes_nothing(tmp_path: Path) -> None:
    """A pulse is a member saying it is still here, and there is no member."""
    peers, _ = joined(tmp_path, "mine", FOREVER)

    beat(peers.root, session_actor("nobody"))

    assert not member_path(peers.root, session_actor("nobody")).exists()


def test_a_session_just_joined_is_present_without_having_beaten(
    tmp_path: Path,
) -> None:
    """Writing the file is being heard, so a session needs no beat to be seen."""
    peers, member = joined(tmp_path, "mine", FOREVER)

    assert row(peers, member).running


def test_a_session_whose_pulse_stopped_reads_as_gone_and_says_since_when(
    tmp_path: Path,
) -> None:
    peers, member = joined(tmp_path, "mine", INSTANTLY)

    gone = row(peers, member)

    assert not gone.running
    assert gone.error.startswith("unheard since ")
    assert member not in peers.live_ids()


def test_a_beat_keeps_a_session_present_past_its_last_write(tmp_path: Path) -> None:
    """The file says what a session is; its time says when it last said so."""
    peers, member = joined(tmp_path, "mine", Pulse(stale_after_seconds=60.0))
    silent_since(peers, member, timedelta(hours=1))

    assert not row(peers, member).running

    peers.beat(member)

    assert row(peers, member).running


def test_a_reader_never_reads_its_own_row_as_absent(tmp_path: Path) -> None:
    """The reader is manifestly here, and its own beat may land after the read.

    Named at the fold, because that is where a reader knows which row is its
    own: a listing asked about the population has no such member, and one
    reading on behalf of a session says so.
    """
    peers, member = joined(tmp_path, "mine", INSTANTLY)

    assert not row(peers, member).running
    assert [
        one.get("running")
        for one in present(peers.root, mine=member, window=0.0)
        if one.get("id") == member
    ] == [True]


def test_a_spawned_agent_is_answered_for_by_its_spawner_not_by_a_pulse(
    tmp_path: Path,
) -> None:
    """Only a session answers for itself; a worker's finish is its process's word."""
    peers = RepositoryPeers(tmp_path, pulse=INSTANTLY)
    peers.cohort.roster.spawned(ActorRef(kind="worker", id="w1"), task="review")

    assert row(peers, "w1").running


def test_a_gone_session_no_longer_holds_its_claims(tmp_path: Path) -> None:
    """Expiry is the roster's, and the roster reads the pulse."""
    peers, member = joined(tmp_path, "mine", FOREVER)
    changed = tmp_path / "tree" / "a.py"
    changed.parent.mkdir(parents=True, exist_ok=True)
    changed.write_text("x = 1", encoding="utf-8")
    peers.touched(member, changed)

    assert [claim.path for claim in peers.held()] == [str(changed)]

    peers.pulse = INSTANTLY

    assert peers.held() == []


def test_a_sweep_moves_the_file_and_the_next_join_revives_the_row(
    tmp_path: Path,
) -> None:
    """A listing that reads the directory agrees with one that stats the file."""
    peers, member = joined(tmp_path, "mine", INSTANTLY)

    retired = peers.sweep()

    assert [one.actor.id for one in retired] == [member]
    assert not member_path(peers.root, session_actor(member)).exists()
    assert departed_path(peers.root, session_actor(member)).is_file()
    [recorded] = [one for one in peers.present() if one.actor.id == member]
    assert not recorded.running
    assert recorded.error.startswith("unheard since ")
    assert peers.sweep() == []

    peers.pulse = FOREVER
    peers.join(member, tmp_path / "tree", cli_name="mine")

    assert row(peers, member).running
    assert row(peers, member).error == ""
    assert peers.called(member) == "mine"


def test_a_sweep_deletes_a_departure_past_the_retention_window(
    tmp_path: Path,
) -> None:
    """The store is the population rather than its history."""
    peers, member = joined(tmp_path, "mine", FOREVER)
    peers.leave(member, summary="landed it")

    assert departed_path(peers.root, session_actor(member)).is_file()

    peers.sweep(now=utc_now() + timedelta(seconds=peers.retention.departed_seconds * 2))

    assert not departed_path(peers.root, session_actor(member)).exists()
    assert peers.present() == []


async def test_the_server_companion_sweeps_and_beats_while_it_serves(
    tmp_path: Path,
) -> None:
    """A tool server's lifetime is the session's, and the beat is how others read it."""
    peers, stale = joined(tmp_path, "stale", INSTANTLY)
    me = mint_member_id()
    companion = RosterPulse(
        root=tmp_path,
        member_id=me,
        pulse=Pulse(interval_seconds=0.01, stale_after_seconds=0.0),
    )

    serving = asyncio.create_task(companion.run())
    await asyncio.sleep(0.05)
    serving.cancel()
    with suppress(asyncio.CancelledError):
        await serving

    assert member_path(peers.root, session_actor(me)).is_file()
    assert departed_path(peers.root, session_actor(stale)).is_file()
    [recorded] = [one for one in peers.present() if one.actor.id == stale]
    assert not recorded.running
    assert recorded.error.startswith("unheard since ")


async def test_the_companion_puts_back_a_row_the_session_outlived(
    tmp_path: Path,
) -> None:
    """A finish written while the process lives on is undone by the next tick."""
    peers, me = joined(tmp_path, "mine", FOREVER)
    peers.leave(me)
    assert not member_path(peers.root, session_actor(me)).exists()
    companion = RosterPulse(
        root=tmp_path, member_id=me, pulse=Pulse(interval_seconds=0.01)
    )

    serving = asyncio.create_task(companion.run())
    await asyncio.sleep(0.05)
    serving.cancel()
    with suppress(asyncio.CancelledError):
        await serving

    assert row(peers, me).running
    assert len([one for one in peers.present() if one.actor.id == me]) == 1


async def test_a_delayed_owned_pulse_preserves_its_bound_native_route(
    tmp_path: Path,
) -> None:
    peers, me = RepositoryPeers(tmp_path), mint_member_id()
    route = WakePath(
        runtime="codex",
        handle="native-thread",
        session="native-thread",
        home=str(tmp_path / "native-home"),
        scope="recipient-scope",
    )
    peers.join(me, tmp_path, wake=route)
    silent_since(peers, me, timedelta(seconds=peers.pulse.stale_after_seconds + 10))
    companion = RosterPulse(
        root=tmp_path,
        member_id=me,
        pulse=Pulse(interval_seconds=0.01),
        wake=WakePath(runtime="codex"),
    )
    serving = asyncio.create_task(companion.run())
    try:
        await asyncio.sleep(0.05)
        assert row(peers, me).running
        assert row(peers, me).wake == route
        assert not departed_path(peers.root, session_actor(me)).exists()
    finally:
        serving.cancel()
        with suppress(asyncio.CancelledError):
            await serving


@pytest.mark.parametrize("departed", [False, True])
async def test_the_owner_recovers_its_bound_route_after_another_peer_sweeps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, departed: bool
) -> None:
    peers, me = RepositoryPeers(tmp_path), mint_member_id()
    route = WakePath(
        runtime="codex",
        handle="native-thread",
        session="native-thread",
        home=str(tmp_path / "native-home"),
        scope="recipient-scope",
    )
    monkeypatch.setattr(
        "lup.coordination.peer_tools.execution_scope", lambda: route.scope
    )
    peers.join(me, tmp_path, wake=route)
    silent_since(peers, me, timedelta(seconds=peers.pulse.stale_after_seconds + 10))
    if departed:
        RepositoryPeers(tmp_path).sweep()
        assert not member_path(peers.root, session_actor(me)).exists()
        assert row(peers, me).wake == route
    companion = RosterPulse(
        root=tmp_path,
        member_id=me,
        pulse=Pulse(interval_seconds=0.01),
        wake=WakePath(runtime="codex"),
    )
    serving = asyncio.create_task(companion.run())
    try:
        await asyncio.sleep(0.05)
        assert row(peers, me).running
        assert row(peers, me).wake == route
    finally:
        serving.cancel()
        with suppress(asyncio.CancelledError):
            await serving


@pytest.mark.parametrize(
    "changed",
    [
        "scope",
        "unknown-scope",
        "worktree",
        "runtime",
        "handle",
        "session",
        "home",
        "startup-scope",
        "another-member",
        "expired",
    ],
)
async def test_a_rejoining_pulse_does_not_inherit_a_foreign_or_replaced_route(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, changed: str
) -> None:
    peers, me = RepositoryPeers(tmp_path), mint_member_id()
    route = WakePath(
        runtime="codex",
        handle="old-thread",
        session="old-thread",
        home=str(tmp_path / "old-home"),
        scope="recipient-scope",
    )
    startup = WakePath(runtime="codex")
    scope = route.scope
    worktree, recorded = tmp_path, me
    match changed:
        case "scope":
            scope = "another-scope"
        case "unknown-scope":
            scope = ""
        case "worktree":
            worktree = tmp_path / "another-worktree"
        case "runtime":
            startup = WakePath(runtime="claude")
        case "handle":
            startup = WakePath(runtime="codex", handle="new-thread")
        case "session":
            startup = WakePath(runtime="codex", session="new-thread")
        case "home":
            startup = WakePath(runtime="codex", home=str(tmp_path / "new-home"))
        case "startup-scope":
            startup = WakePath(runtime="codex", scope="another-scope")
        case "another-member":
            recorded = mint_member_id()
    monkeypatch.setattr("lup.coordination.peer_tools.execution_scope", lambda: scope)
    peers.join(recorded, worktree, wake=route)
    silent_since(
        peers, recorded, timedelta(seconds=peers.pulse.stale_after_seconds + 10)
    )
    RepositoryPeers(tmp_path).sweep()
    if changed == "expired":
        peers.sweep(
            now=utc_now() + timedelta(seconds=peers.retention.departed_seconds * 2)
        )
    companion = RosterPulse(
        root=tmp_path,
        member_id=me,
        pulse=Pulse(interval_seconds=0.01),
        wake=startup,
    )
    serving = asyncio.create_task(companion.run())
    try:
        await asyncio.sleep(0.05)
        assert row(peers, me).running
        assert row(peers, me).wake == startup
    finally:
        serving.cancel()
        with suppress(asyncio.CancelledError):
            await serving


async def test_the_companion_writes_nothing_where_no_session_ever_joined(
    tmp_path: Path,
) -> None:
    """A session that never coordinates leaves no sign of having been able to."""
    companion = RosterPulse(
        root=tmp_path, member_id="abc", pulse=Pulse(interval_seconds=0.01)
    )

    serving = asyncio.create_task(companion.run())
    await asyncio.sleep(0.05)
    serving.cancel()
    with suppress(asyncio.CancelledError):
        await serving

    assert not coordination_root(tmp_path).exists()


def test_the_listing_puts_the_present_first_whatever_the_files_say(
    tmp_path: Path,
) -> None:
    peers, stale = joined(tmp_path, "stale", Pulse(stale_after_seconds=60.0))
    silent_since(peers, stale, timedelta(hours=1))
    fresh = mint_member_id()
    peers.join(fresh, tmp_path / "tree", cli_name="fresh")

    listed = [(view.cli_name, view.member.running) for view in peers.recent()]

    assert listed[0] == ("fresh", True)
    assert ("stale", False) in listed


def test_the_store_holds_the_population_rather_than_its_history(
    tmp_path: Path,
) -> None:
    """Two directories, one file each, and nothing that grows per call."""
    peers, member = joined(tmp_path, "mine", FOREVER)
    for _ in range(20):
        peers.beat(member)
        peers.describe(member, "still on it")

    mine = conversation_of(session_actor(member))
    assert sorted(
        path.name
        for path in (peers.root / MEMBERS_DIR).iterdir()
        if path.name.startswith(mine)
    ) == [
        f"{mine}.json",
        f"{mine}.lock",
    ]
    assert not (peers.root / DEPARTED_DIR).exists()
