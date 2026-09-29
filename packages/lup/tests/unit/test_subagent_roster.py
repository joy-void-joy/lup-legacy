"""A native subagent on its session's roster: a row of its own, nested under it.

Written against what an orchestrating session with ten subagents met. The
subagents inherited the session's identity through the environment, so every
`coordination_describe` replaced the orchestrator's row, a lock held a file for
the whole session rather than for the one subagent writing it, and a
subagent's message to the session that dispatched it was refused as a message
to itself.

What tells the calling subagent apart is the argument its runtime's hook
stamps on every coordination call, which these cases carry the way the hook
writes it.
"""

import json
from datetime import timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from lup.channels.models import utc_now
from lup.coordination.bare import store
from lup.coordination.bare.departure import subagent_left
from lup.coordination.peer_tools import PeerListOutput, create_peer_tools
from lup.coordination.repository import RepositoryPeers
from lup.devtools.coordination import app as coordination_app
from lup.tools.mcp import LupMcpTool, ToolResponse, response_text
from lup.types import JsonObject

SESSION = "abc123"


def verbs(peers: RepositoryPeers, worktree: Path) -> dict[str, LupMcpTool]:
    """The session's coordination verbs, by name, as its one tool server serves them."""
    return {tool.name: tool for tool in create_peer_tools(peers, SESSION, worktree)}


def by(agent: str, worktree: Path, **arguments: str) -> JsonObject:
    """One call's arguments as the caller hook leaves them: blank *agent* is the session."""
    caller: JsonObject = {
        "agent_id": agent,
        "agent_type": "general-purpose",
        "cwd": str(worktree),
    }
    return {**arguments, store.CALLER_FIELD: caller}


def refusal(response: ToolResponse) -> str:
    """What a verb said when it refused, failing the test where it did not refuse."""
    assert response.get("is_error"), response_text(response)
    return response_text(response)


def answer(response: ToolResponse) -> JsonObject:
    """What a verb answered, failing the test where it refused."""
    assert not response.get("is_error"), response_text(response)
    found = json.loads(response_text(response))
    assert isinstance(found, dict)
    return found


@pytest.fixture
def worktree(tmp_path: Path) -> Path:
    tree = tmp_path / "dev"
    tree.mkdir()
    return tree


async def described(tools: dict[str, LupMcpTool], worktree: Path, *agents: str) -> None:
    """The session and each of *agents*, each having said what it is on."""
    await tools["coordination_describe"].handler(
        by("", worktree, description="orchestrating")
    )
    for agent in agents:
        await tools["coordination_describe"].handler(
            by(agent, worktree, description=f"building {agent}")
        )


async def test_a_subagent_describing_itself_leaves_its_sessions_row_alone(
    tmp_path: Path, worktree: Path
) -> None:
    peers = RepositoryPeers(tmp_path)
    tools = verbs(peers, worktree)

    await described(tools, worktree, "a0cacac5")

    rows = {view.member.actor.id: view for view in peers.listing()}
    assert rows[SESSION].doing == "orchestrating"
    child = rows[store.subagent_id(SESSION, "a0cacac5")]
    assert child.doing == "building a0cacac5"
    assert child.member.parent == SESSION
    assert child.member.kind == store.SUBAGENT_KIND


async def test_a_call_no_hook_stamped_acts_as_the_session(
    tmp_path: Path, worktree: Path
) -> None:
    """A session whose runtime carries no caller hook is the session it always was."""
    peers = RepositoryPeers(tmp_path)
    tools = verbs(peers, worktree)

    await tools["coordination_describe"].handler({"description": "orchestrating"})

    [row] = peers.listing()
    assert row.member.actor.id == SESSION
    assert row.doing == "orchestrating"


async def test_the_peers_listing_nests_each_subagent_under_its_session(
    tmp_path: Path, worktree: Path
) -> None:
    peers = RepositoryPeers(tmp_path)
    tools = verbs(peers, worktree)
    await described(tools, worktree, "a0cacac5", "b1dbdbd6")

    response = await tools["coordination_peers"].handler(by("", worktree))

    [session] = PeerListOutput.model_validate(answer(response)).peers
    assert session.address == "dev"
    assert sorted(child.doing for child in session.subagents) == [
        "building a0cacac5",
        "building b1dbdbd6",
    ]
    assert {child.address for child in session.subagents} == {
        store.subagent_id(SESSION, "a0cacac5"),
        store.subagent_id(SESSION, "b1dbdbd6"),
    }


async def test_a_subagent_must_say_what_it_is_on_before_it_reads_the_roster(
    tmp_path: Path, worktree: Path
) -> None:
    """Its own row is read the way every row is, so its own description is asked for."""
    peers = RepositoryPeers(tmp_path)
    tools = verbs(peers, worktree)
    await described(tools, worktree)

    assert "coordination_describe" in refusal(
        await tools["coordination_peers"].handler(by("a0cacac5", worktree))
    )


async def test_a_lock_holds_a_path_for_one_subagent_against_its_sibling(
    tmp_path: Path, worktree: Path
) -> None:
    peers = RepositoryPeers(tmp_path)
    tools = verbs(peers, worktree)
    await described(tools, worktree, "a0cacac5", "b1dbdbd6")
    module = worktree / "cli.py"
    module.write_text("value = 1\n", encoding="utf-8")
    holder = store.subagent_id(SESSION, "a0cacac5")

    locked = answer(
        await tools["coordination_lock"].handler(
            by("a0cacac5", worktree, path=str(module))
        )
    )

    assert locked["holders"] == [holder]
    sibling = store.subagent_id(SESSION, "b1dbdbd6")
    assert store.claim_holders(peers.root, str(module), sibling, session=SESSION) == [
        holder
    ]
    assert store.claim_holders(peers.root, str(module), holder, session=SESSION) == []
    assert holder in refusal(
        await tools["coordination_release"].handler(
            by("b1dbdbd6", worktree, path=str(module))
        )
    )
    answer(
        await tools["coordination_release"].handler(
            by("a0cacac5", worktree, path=str(module))
        )
    )
    assert peers.holding(module) == []


async def test_a_subagent_is_not_asked_about_what_its_session_holds(
    tmp_path: Path, worktree: Path
) -> None:
    """The session dispatched it; the session writing under it is the hazard instead."""
    peers = RepositoryPeers(tmp_path)
    tools = verbs(peers, worktree)
    await described(tools, worktree, "a0cacac5")
    module = worktree / "cli.py"
    module.write_text("value = 1\n", encoding="utf-8")
    child = store.subagent_id(SESSION, "a0cacac5")

    peers.touched(SESSION, module)
    assert store.claim_holders(peers.root, str(module), child, session=SESSION) == []

    peers.touched(child, module)
    assert store.claim_holders(peers.root, str(module), SESSION, session=SESSION) == [
        child
    ]


async def test_a_subagent_reaches_its_own_session_and_not_itself(
    tmp_path: Path, worktree: Path
) -> None:
    peers = RepositoryPeers(tmp_path)
    tools = verbs(peers, worktree)
    await described(tools, worktree, "a0cacac5")
    child = store.subagent_id(SESSION, "a0cacac5")

    sent = answer(
        await tools["coordination_send"].handler(
            by("a0cacac5", worktree, address="dev", text="the CLI half is done")
        )
    )

    assert sent["outstanding"] == 1
    [message] = peers.waiting(SESSION).messages
    assert message.text == "the CLI half is done"
    assert "own address" in refusal(
        await tools["coordination_send"].handler(
            by("a0cacac5", worktree, address=child, text="hi")
        )
    )


async def test_a_message_to_a_subagent_waits_in_its_own_mailbox(
    tmp_path: Path, worktree: Path
) -> None:
    """And what a send reports outstanding is the recipient's queue, not the sender's."""
    peers = RepositoryPeers(tmp_path)
    tools = verbs(peers, worktree)
    await described(tools, worktree, "a0cacac5")
    child = store.subagent_id(SESSION, "a0cacac5")
    peers.send("user", "unrelated, and not the recipient's")

    sent = answer(
        await tools["coordination_send"].handler(
            by("", worktree, address=child, text="rebase before you commit")
        )
    )

    assert sent["delivery"] == "hook"
    assert sent["outstanding"] == 1
    assert [message.text for message in peers.waiting(child).messages] == [
        "rebase before you commit"
    ]
    taken = answer(
        await tools["coordination_mailbox"].handler(by("a0cacac5", worktree))
    )
    assert taken["messages"] == ["[message by agent] rebase before you commit"]
    assert peers.waiting(SESSION).messages == []


def test_an_id_reaches_its_member_whatever_somebody_else_is_called(
    tmp_path: Path,
) -> None:
    """A name that spells another member's id does not take that id's place."""
    peers = RepositoryPeers(tmp_path)
    peers.join(SESSION, tmp_path / "dev", cli_name="dev")
    peers.join("def456", tmp_path / "other", cli_name="other")

    peers.rename("def456", SESSION)

    found = peers.address(SESSION)
    assert found is not None and found.id == SESSION


def test_every_verb_taking_an_id_reaches_a_subagent_row(tmp_path: Path) -> None:
    peers = RepositoryPeers(tmp_path)
    peers.join(SESSION, tmp_path / "dev")
    child = peers.join_subagent(
        SESSION, store.Caller(agent_id="a0cacac5", agent_type="Explore")
    )
    locked = tmp_path / "dev"
    locked.mkdir()

    peers.describe(child.id, "reading the store")
    peers.rename(child.id, "reader")
    peers.lock(child.id, locked)

    found = peers.address("reader")
    assert found == child
    assert peers.address(child.id) == child
    row = peers.row(child.id)
    assert row is not None and row.description == "reading the store"
    assert peers.holds(child.id, locked)


def test_the_same_runtime_id_under_two_sessions_is_two_rows(tmp_path: Path) -> None:
    """A subagent's row is its session's, so a caller naming an id reaches only its own."""
    peers = RepositoryPeers(tmp_path)
    peers.join(SESSION, tmp_path / "dev")
    peers.join("def456", tmp_path / "other")
    caller = store.Caller(agent_id="a0cacac5", agent_type="general-purpose")

    mine = peers.join_subagent(SESSION, caller)
    theirs = peers.join_subagent("def456", caller)

    assert mine != theirs
    assert {row.member.parent for row in peers.listing() if row.member.parent} == {
        SESSION,
        "def456",
    }


def test_a_subagents_row_ends_with_its_session(tmp_path: Path) -> None:
    peers = RepositoryPeers(tmp_path)
    peers.join(SESSION, tmp_path / "dev")
    child = peers.join_subagent(SESSION, store.Caller(agent_id="a0cacac5"))

    peers.leave(SESSION, summary="landed")

    assert child.id not in peers.live_ids()
    assert not store.member_path(
        peers.root, store.subagent_actor(SESSION, "a0cacac5")
    ).exists()


def test_a_subagent_lapses_with_its_sessions_pulse(tmp_path: Path) -> None:
    """It beats for nothing itself: it is here while the session it runs in is."""
    peers = RepositoryPeers(tmp_path)
    peers.join(SESSION, tmp_path / "dev")
    child = peers.join_subagent(SESSION, store.Caller(agent_id="a0cacac5"))
    later = utc_now() + timedelta(hours=1)

    lapsed = {member.actor.id for member in peers.lapsed(later)}

    assert lapsed == {SESSION, child.id}
    peers.sweep(later)
    assert peers.live_ids() == []


def test_a_subagent_ending_its_turn_leaves_the_roster_and_its_mail_with_its_session(
    tmp_path: Path,
) -> None:
    """What it never read is forwarded to the session that dispatched it."""
    peers = RepositoryPeers(tmp_path)
    peers.join(SESSION, tmp_path / "dev")
    child = peers.join_subagent(SESSION, store.Caller(agent_id="a0cacac5"))
    peers.send(child.id, "one more thing")

    assert subagent_left(peers.root, SESSION, "a0cacac5")

    assert child.id not in peers.live_ids()
    assert SESSION in peers.live_ids()
    [forwarded] = peers.waiting(SESSION).messages
    assert child.id in forwarded.text
    assert "one more thing" in forwarded.text
    assert peers.waiting(child.id).messages == []


def test_the_console_roster_indents_a_subagent_under_its_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(coordination_app, "project_root", lambda: tmp_path)
    peers = RepositoryPeers(tmp_path)
    peers.join(SESSION, tmp_path / "dev", cli_name="dev")
    child = peers.join_subagent(SESSION, store.Caller(agent_id="a0cacac5"))
    peers.describe(child.id, "reading the store")

    result = CliRunner().invoke(coordination_app.create_coordination_app(), ["roster"])

    assert result.exit_code == 0, result.output
    session_line, child_line = result.output.splitlines()
    assert session_line.startswith(f"dev ({SESSION}) — ")
    assert child_line.startswith(f"  {child.id} — ")
    assert "reading the store" in child_line


async def test_a_subagent_is_called_what_its_spawn_called_it(
    tmp_path: Path, worktree: Path
) -> None:
    """Named from the spawn, numbered where a live row answers to that already.

    The id reaches it whatever it ends up called, which is what settles two
    sessions' subagents spawned under one name.
    """
    peers = RepositoryPeers(tmp_path)
    peers.join("def456", tmp_path / "other", cli_name="builder")
    tools = verbs(peers, worktree)
    arguments = by("a0cacac5", worktree, description="building the CLI")
    caller = arguments[store.CALLER_FIELD]
    assert isinstance(caller, dict)
    caller["name"] = "builder"

    await tools["coordination_describe"].handler(arguments)

    child = store.subagent_id(SESSION, "a0cacac5")
    assert peers.called(child) == "builder-2"
    assert peers.address("builder-2") == peers.actor(child)
    assert peers.address(child) == peers.actor(child)
    found = peers.address("builder")
    assert found is not None and found.id == "def456"
