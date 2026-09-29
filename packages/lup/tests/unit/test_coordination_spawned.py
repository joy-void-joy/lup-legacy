"""A runtime started from a session's shell is somebody else, spawned by that session.

A launcher mints one id for the one runtime it starts, and exports it; every
process that runtime starts inherits it — a `claude -p`, a `codex exec`, a
pipeline run from the session's shell among them. So the id alone cannot say
which session a process belongs to: such a runtime's tool servers and hooks
joined, described, departed and read mail as the session whose shell started
it, and one outliving the session took its ended row over. The row names the
runtime it answers for, and a runtime that is neither that one nor the one
that started it answers as a member of its own, whose row names the session
it was spawned by.
"""

import asyncio
import os
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path

import pytest

from lup.channels.wait import wait_until
from lup.coordination.bare.runtime import Runtime, runtime_of
from lup.coordination.bare.store import (
    Member,
    adopt,
    member_of,
    named_runtime,
    own_member,
    session_actor,
    spawned_id,
)
from lup.coordination.identity import MEMBER_ENV, mint_member_id
from lup.coordination.peer_tools import RosterPulse, create_peer_tools
from lup.coordination.pulse import Pulse
from lup.coordination.repository import RepositoryPeers, RuntimeMember, runtime_member
from lup.mcp import opened_needs
from lup.mcp import serve
from lup.mcp.serve import context_needs
from lup.workspace.context import SessionContext


@contextmanager
def started() -> Iterator[subprocess.Popen[bytes]]:
    """A runtime this process started, stopped whatever happens."""
    child = subprocess.Popen(["sleep", "600"])
    try:
        yield child
    finally:
        child.kill()
        child.wait()


def exited() -> Runtime:
    """A runtime that ran, recorded while it did, and has since stopped."""
    with started() as child:
        return runtime_of(child.pid)


def answering_for(peers: RepositoryPeers, member: str, runtime: Runtime) -> None:
    """Put *runtime* on this member's row as the process the row answers for."""

    def recorded(found: Member) -> Member:
        """This member, answering for *runtime*."""
        settled = found.copy()
        settled["runtime"] = runtime
        return settled

    peers.revise(member, recorded)


def session(root: Path, name: str = "lead") -> tuple[RepositoryPeers, str]:
    """A repository whose session has joined, launched under a minted id."""
    peers = RepositoryPeers(root)
    member = mint_member_id()
    peers.join(member, root / "tree", cli_name=name)
    return peers, member


MINE = runtime_of(os.getpid())
"""The runtime these cases stand in for: the process running them."""

SHELL = runtime_of(os.getppid())
"""The runtime whose shell started this one, as a session's shell starts a `claude -p`."""


def test_a_row_naming_no_runtime_is_the_first_runtime_s_to_take(tmp_path: Path) -> None:
    peers, member = session(tmp_path)

    assert own_member(peers.root, member, MINE) == member


def test_the_runtime_a_row_names_answers_as_that_session(tmp_path: Path) -> None:
    peers, member = session(tmp_path)
    answering_for(peers, member, MINE)

    assert own_member(peers.root, member, MINE) == member


def test_a_runtime_started_beneath_the_session_answers_as_its_own_member(
    tmp_path: Path,
) -> None:
    peers, member = session(tmp_path)
    answering_for(peers, member, SHELL)

    spawned = own_member(peers.root, member, MINE)

    assert spawned == spawned_id(member, MINE)
    assert spawned.startswith(f"{member}_")
    assert own_member(peers.root, member, MINE) == spawned
    with started() as other:
        assert own_member(peers.root, member, runtime_of(other.pid)) not in (
            member,
            spawned,
        )


@pytest.mark.parametrize("left", [True, False])
def test_a_runtime_outliving_the_session_does_not_take_its_ended_row(
    tmp_path: Path, left: bool
) -> None:
    """The launcher's id was minted for one runtime, and a stopped one is still it."""
    peers, member = session(tmp_path)
    answering_for(peers, member, exited())
    if left:
        peers.leave(member)

    assert own_member(peers.root, member, MINE) == spawned_id(member, MINE)


def test_the_session_whose_runtime_started_the_first_to_answer_is_still_itself(
    tmp_path: Path,
) -> None:
    """A runtime spawned from the shell can get to a fresh store's row first."""
    peers, member = session(tmp_path)
    with started() as child:
        beneath = runtime_of(child.pid)
        answering_for(peers, member, beneath)

        assert own_member(peers.root, member, MINE) == member
        adopt(peers.root, session_actor(member), MINE)
        found = member_of(peers.root, session_actor(member))

        assert found is not None and found.get("runtime") == MINE
        assert own_member(peers.root, member, beneath) == spawned_id(member, beneath)


def test_a_row_answering_for_another_runtime_is_not_adopted_away(
    tmp_path: Path,
) -> None:
    peers, member = session(tmp_path)
    answering_for(peers, member, SHELL)

    adopt(peers.root, session_actor(member), MINE)

    found = member_of(peers.root, session_actor(member))
    assert found is not None and found.get("runtime") == SHELL


def test_a_process_nothing_launched_answers_as_its_runtime_says(tmp_path: Path) -> None:
    peers, member = session(tmp_path)
    answering_for(peers, member, SHELL)

    assert own_member(peers.root, "", MINE) == ""
    assert runtime_member(tmp_path, "", "native-id", MINE) == RuntimeMember(
        member_id="native-id"
    )


def test_a_spawned_runtime_is_a_member_spawned_by_the_session(tmp_path: Path) -> None:
    peers, member = session(tmp_path)
    answering_for(peers, member, SHELL)

    assert runtime_member(tmp_path, member, "native-id", MINE) == RuntimeMember(
        member_id=spawned_id(member, MINE), spawned_by=member
    )
    assert runtime_member(tmp_path, member, "native-id", SHELL) == RuntimeMember(
        member_id=member
    )


async def test_a_spawned_runtime_s_verbs_describe_its_own_row(tmp_path: Path) -> None:
    peers, member = session(tmp_path)
    await described(peers, member, "orchestrating the release", SHELL)
    spawned = runtime_member(tmp_path, member, "", MINE)

    await described(
        peers,
        spawned.member_id,
        "running the eval pipeline",
        MINE,
        spawned.spawned_by,
    )

    rows = {row.actor.id: row for row in peers.present()}
    assert rows[member].description == "orchestrating the release"
    assert rows[member].spawned_by == ""
    assert rows[spawned.member_id].description == "running the eval pipeline"
    assert rows[spawned.member_id].spawned_by == member
    assert rows[spawned.member_id].cli_name == "lead-spawned"
    assert rows[spawned.member_id].running


async def described(
    peers: RepositoryPeers,
    member: str,
    description: str,
    runtime: Runtime,
    spawned_by: str = "",
) -> None:
    """*member*, running under *runtime*, saying what it is on through its own verb."""
    [describe] = [
        each
        for each in create_peer_tools(
            peers,
            member,
            Path.cwd(),
            runtime=runtime,
            spawned_by=spawned_by,
        )
        if each.name == "coordination_describe"
    ]
    await describe.handler({"description": description})


def test_a_tool_server_under_a_spawned_runtime_serves_its_own_member(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    peers, member = session(tmp_path)
    answering_for(peers, member, SHELL)
    monkeypatch.setenv(MEMBER_ENV, member)
    monkeypatch.setattr(serve, "project_root", lambda: tmp_path)
    context = SessionContext(
        session_dir=tmp_path / "session",
        outputs_dir=tmp_path / "outputs",
        gate_flag=tmp_path / "gate",
        session_id="native",
        task_id="native",
    )

    spawned = context_needs(context, "native", runtime=MINE)
    own = context_needs(context, "native", runtime=SHELL)

    assert (spawned.member, spawned.spawned_by) == (spawned_id(member, MINE), member)
    assert (own.member, own.spawned_by) == (member, "")


def test_a_session_opened_in_a_spawned_process_is_its_own_member(
    tmp_path: Path,
) -> None:
    """A pipeline run from the session's shell opens sessions of its own in process."""
    peers, member = session(tmp_path)
    answering_for(peers, member, SHELL)

    needs = opened_needs(tmp_path, tmp_path / "session", {MEMBER_ENV: member})

    assert (needs.member, needs.spawned_by) == (spawned_id(member, MINE), member)


async def test_the_session_s_pulse_takes_back_a_row_a_runtime_it_started_reached_first(
    tmp_path: Path,
) -> None:
    peers, member = session(tmp_path)
    with started() as child:
        answering_for(peers, member, runtime_of(child.pid))
        pulse = RosterPulse(
            root=tmp_path, member_id=member, pulse=Pulse(interval_seconds=0.01)
        )
        beating = asyncio.create_task(pulse.run())
        try:
            taken = await wait_until(
                lambda: (
                    True
                    if named_runtime(peers.root, session_actor(member)) == MINE
                    else None
                ),
                wait_seconds=5.0,
                poll_interval_seconds=0.01,
            )
        finally:
            beating.cancel()
            with suppress(asyncio.CancelledError):
                await beating

    assert taken
