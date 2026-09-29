"""A session's row is present while its runtime runs, and gone when it stops.

Written against two failures the roster had. Every session that ever joined
read as running forever, because the only record that ended a row was the one
a session wrote on its way out, and a killed session writes nothing — so a
row is heard from, its file's modification time touched while the session
lives. And a stopped session read as running whenever something else beat
for it: a runtime started from its shell, carrying its id. So the row names
its runtime process, a reader that can see that process asks it, one that
cannot tests the pulse a live server holds, and the clock decides only where
neither speaks. What is asserted is the read a peer makes — the listing, the
claims that expire with a session, the sweep that moves a stopped session's
file — and which server answers for which session.
"""

import asyncio
import os
import subprocess
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager, suppress
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from lup.channels.models import utc_now
from lup.channels.wait import wait_until
from lup.coordination.identity import mint_member_id
from lup.coordination.peer_tools import RosterPulse, create_peer_tools
from lup.coordination.bare.runtime import Runtime, runtime_of
from lup.coordination.bare.store import (
    DEPARTED_DIR,
    MEMBERS_DIR,
    Member,
    answered,
    beat,
    conversation_of,
    departed_path,
    member_of,
    member_path,
    pulse_path,
    session_actor,
    present,
)
from lup.coordination.pulse import Pulse, PulseHold
from lup.coordination.refs import ActorRef
from lup.coordination.repository import RepositoryPeers
from lup.coordination.roster import RosterMember
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


def record(peers: RepositoryPeers, member: str, runtime: Runtime) -> None:
    """Put *runtime* on this member's row as the process the row answers for."""

    def answered_by(found: Member) -> Member:
        """This member, answering for *runtime*."""
        settled = found.copy()
        settled["runtime"] = runtime
        return settled

    peers.revise(member, answered_by)


def foreign(runtime: Runtime) -> Runtime:
    """*runtime* as a process in another pid namespace, which no reader here can ask."""
    return Runtime(
        pid=runtime.get("pid", 0),
        started=runtime.get("started", ""),
        scope="another-namespace",
    )


@contextmanager
def sleeping() -> Iterator[subprocess.Popen[bytes]]:
    """A runtime that runs until the test stops it, and is stopped whatever happens."""
    sleeper = subprocess.Popen(["sleep", "600"])
    try:
        yield sleeper
    finally:
        sleeper.kill()
        sleeper.wait()


@asynccontextmanager
async def serving(companion: RosterPulse) -> AsyncIterator[None]:
    """*companion* running beside the test, stopped the way a server stops it."""
    task = asyncio.create_task(companion.run())
    try:
        yield
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


async def until(peers: RepositoryPeers, member: str, held: bool) -> bool:
    """Whether this member's pulse comes to be held, or free, within a few seconds."""
    actor = session_actor(member)
    found = await wait_until(
        lambda: True if answered(peers.root, actor) == held else None,
        wait_seconds=5.0,
        poll_interval_seconds=0.01,
    )
    return found is True


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
    record(peers, me, runtime_of(os.getpid()))
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


def test_a_session_whose_runtime_runs_is_present_however_long_it_was_silent(
    tmp_path: Path,
) -> None:
    """A sleeping machine stops every beat; it does not stop the process a beat was for."""
    peers, member = joined(tmp_path, "mine", INSTANTLY)
    record(peers, member, runtime_of(os.getpid()))
    silent_since(peers, member, timedelta(hours=3))

    assert row(peers, member).running
    assert member in peers.live_ids()
    assert peers.sweep() == []


def test_a_session_whose_runtime_stopped_reads_gone_at_once(tmp_path: Path) -> None:
    """Within the window, because the process was asked rather than the clock."""
    peers, member = joined(tmp_path, "mine", FOREVER)
    with sleeping() as runtime:
        record(peers, member, runtime_of(runtime.pid))

        assert row(peers, member).running

    gone = row(peers, member)

    assert not gone.running
    assert gone.error == "its runtime stopped"
    assert [one.actor.id for one in peers.sweep()] == [member]


def test_a_held_pulse_keeps_a_session_whole_across_a_sleep_its_readers_cannot_see_past(
    tmp_path: Path,
) -> None:
    """A peer in another container cannot ask the process, and can ask the lock it holds."""
    peers, member = joined(tmp_path, "mine", INSTANTLY)
    record(peers, member, foreign(runtime_of(os.getpid())))
    changed = tmp_path / "tree" / "a.py"
    changed.parent.mkdir(parents=True, exist_ok=True)
    changed.write_text("x = 1", encoding="utf-8")
    peers.touched(member, changed)
    peers.describe(member, "mid-rebase")
    silent_since(peers, member, timedelta(hours=3))
    hold = PulseHold(pulse_path(peers.root, session_actor(member)))

    assert hold.take()
    assert answered(peers.root, session_actor(member))
    assert row(peers, member).running
    assert peers.sweep() == []
    assert [claim.path for claim in peers.held()] == [str(changed)]
    assert row(peers, member).description == "mid-rebase"

    hold.release()

    assert not answered(peers.root, session_actor(member))
    assert not row(peers, member).running


def test_a_pulse_nobody_holds_leaves_a_session_to_the_clock(tmp_path: Path) -> None:
    """A holder gone without a word is a server that died, which the clock still judges."""
    peers, member = joined(tmp_path, "mine", FOREVER)
    record(peers, member, foreign(runtime_of(os.getpid())))
    hold = PulseHold(pulse_path(peers.root, session_actor(member)))
    assert hold.take()
    hold.release()

    assert row(peers, member).running

    peers.pulse = INSTANTLY

    assert row(peers, member).error.startswith("unheard since ")


async def test_the_companion_ends_its_session_when_its_runtime_stops(
    tmp_path: Path,
) -> None:
    """A server its runtime left behind stops answering, and says why, within a beat."""
    peers, member = joined(tmp_path, "mine", FOREVER)
    with sleeping() as runtime:
        companion = RosterPulse(
            root=tmp_path,
            member_id=member,
            pulse=Pulse(interval_seconds=0.01),
            runtime=runtime_of(runtime.pid),
        )
        async with serving(companion):
            assert await until(peers, member, held=True)

            runtime.kill()
            runtime.wait()

            assert await until(peers, member, held=False)
            [stub] = [one for one in peers.present() if one.actor.id == member]

    assert not stub.running
    assert stub.error == "its runtime stopped"
    assert departed_path(peers.root, session_actor(member)).is_file()


async def test_the_companion_ends_its_session_as_it_stops_if_its_runtime_went_first(
    tmp_path: Path,
) -> None:
    """A runtime killed outright closes the server's input before any beat notices."""
    peers, member = joined(tmp_path, "mine", FOREVER)
    with sleeping() as runtime:
        companion = RosterPulse(
            root=tmp_path,
            member_id=member,
            pulse=Pulse(interval_seconds=3600.0),
            runtime=runtime_of(runtime.pid),
        )
        async with serving(companion):
            assert await until(peers, member, held=True)
            runtime.kill()
            runtime.wait()

    assert departed_path(peers.root, session_actor(member)).is_file()
    assert not answered(peers.root, session_actor(member))


async def test_a_companion_stopping_under_a_live_runtime_leaves_the_session_standing(
    tmp_path: Path,
) -> None:
    """A server restarted by its runtime is not the session ending."""
    peers, member = joined(tmp_path, "mine", INSTANTLY)
    companion = RosterPulse(
        root=tmp_path, member_id=member, pulse=Pulse(interval_seconds=0.01)
    )
    async with serving(companion):
        assert await until(peers, member, held=True)

    assert member_path(peers.root, session_actor(member)).is_file()
    assert not answered(peers.root, session_actor(member))
    found = member_of(peers.root, session_actor(member))
    assert found is not None
    assert found.get("runtime") == runtime_of(os.getpid())
    assert row(peers, member).running


async def test_a_runtime_carrying_another_session_s_id_never_answers_for_it(
    tmp_path: Path,
) -> None:
    """A runtime started from a session's shell inherits its id, and can outlive it.

    Its tool server joins under that id like any other. Answering for the row
    whenever the row stood, or putting a finished one back, kept a session the
    person had stopped reading as running for as long as the other runtime
    lived. It met the session alive, so it is somebody else for good — once
    the session has stopped as much as before.
    """
    peers, member = joined(tmp_path, "mine", FOREVER)
    with sleeping() as session:
        own = RosterPulse(
            root=tmp_path,
            member_id=member,
            pulse=Pulse(interval_seconds=0.01),
            runtime=runtime_of(session.pid),
        )
        async with serving(own):
            assert await until(peers, member, held=True)
        silent_since(peers, member, timedelta(minutes=10))
        before = heard(peers, member)
        inherited = RosterPulse(
            root=tmp_path, member_id=member, pulse=Pulse(interval_seconds=0.01)
        )
        async with serving(inherited):
            await asyncio.sleep(0.1)

            assert not answered(peers.root, session_actor(member))
            assert heard(peers, member) == before

            peers.leave(member)
            session.kill()
            session.wait()
            await asyncio.sleep(0.1)

            assert not member_path(peers.root, session_actor(member)).exists()
            assert not answered(peers.root, session_actor(member))

    [stub] = [one for one in peers.present() if one.actor.id == member]
    assert not stub.running


@pytest.mark.parametrize("left", [True, False])
async def test_a_runtime_resuming_a_stopped_session_s_id_answers_for_it(
    tmp_path: Path, left: bool
) -> None:
    """Claude Code resumes a conversation under its own session id, which an unlaunched session is known by.

    The runtime the row names had stopped before this one met it — having left
    cleanly, or been killed with its row still standing — so the runtime
    carrying its id now is its successor rather than somebody beside it.
    """
    peers, member = joined(tmp_path, "mine", FOREVER)
    with sleeping() as first:
        record(peers, member, runtime_of(first.pid))
    if left:
        peers.leave(member)
    successor = RosterPulse(
        root=tmp_path, member_id=member, pulse=Pulse(interval_seconds=0.01)
    )
    async with serving(successor):
        assert await until(peers, member, held=True)

    found = member_of(peers.root, session_actor(member))
    assert found is not None
    assert found.get("runtime") == runtime_of(os.getpid())
    assert row(peers, member).running


async def test_one_runtime_s_servers_answer_for_its_session_one_at_a_time(
    tmp_path: Path,
) -> None:
    """Codex's shape: a server per conversation, every one of them under one runtime.

    The one holding the pulse answers; the rest wait, and one takes over only
    where the holder stopped while the runtime runs on — which is still the
    session running.
    """
    peers, member = joined(tmp_path, "mine", FOREVER)
    first = asyncio.create_task(
        RosterPulse(
            root=tmp_path, member_id=member, pulse=Pulse(interval_seconds=0.01)
        ).run()
    )
    assert await until(peers, member, held=True)
    second = RosterPulse(
        root=tmp_path, member_id=member, pulse=Pulse(interval_seconds=0.01)
    )
    async with serving(second):
        await asyncio.sleep(0.05)
        first.cancel()
        with suppress(asyncio.CancelledError):
            await first

        assert await until(peers, member, held=True)

    assert not answered(peers.root, session_actor(member))
    assert row(peers, member).running


async def test_a_session_s_first_call_names_its_runtime_before_another_can(
    tmp_path: Path,
) -> None:
    """In a store nobody joined yet, a session's first call puts its row down, not its pulse.

    That row names the runtime, so a runtime carrying the same id whose pulse
    beats before the session's own cannot take the row for its own.
    """
    peers = RepositoryPeers(tmp_path, pulse=FOREVER)
    member = mint_member_id()
    with sleeping() as runtime:
        tools = {
            tool.name: tool
            for tool in create_peer_tools(
                peers, member, tmp_path / "tree", runtime=runtime_of(runtime.pid)
            )
        }
        await tools["coordination_describe"].handler({"description": "first"})
        found = member_of(peers.root, session_actor(member))

        assert found is not None
        assert found.get("runtime") == runtime_of(runtime.pid)

        inherited = RosterPulse(
            root=tmp_path, member_id=member, pulse=Pulse(interval_seconds=0.01)
        )
        async with serving(inherited):
            await asyncio.sleep(0.05)

            assert not answered(peers.root, session_actor(member))
