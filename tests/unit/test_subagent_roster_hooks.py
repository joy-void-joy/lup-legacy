"""A native subagent's own roster row, carried by the hooks both runtimes run.

A tool server cannot tell one session's conversations apart — they share it
on Claude Code, and on Codex each subagent's copy starts under the session's
environment — and the hook payload can: both runtimes put the subagent's
``agent_id`` on every tool event fired inside it (measured on Claude Code
2.1.283 and Codex 0.155.1 and 0.158.0) and leave it off the session's own. So what makes a subagent a row of its own
is carried by generated hooks — a caller hook stamping it into each
coordination call, the departure hook ending the row when the subagent stops,
the delivery hook handing it its own mail, and the permission dispatcher
recording what it changes and judging whom a native send reaches.

Each is run here as its runtime runs it, from the tree generation renders
laid out whole, over a store the typed writers produced.
"""

import json
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import TypedDict

import pytest
import sh
from pydantic import BaseModel

from lup.coordination.bare import store
from lup.coordination.identity import MEMBER_ENV, mint_member_id
from lup.coordination.policy import COORDINATION_SERVER
from lup.coordination.repository import RepositoryPeers
from lup.harness.generate import NativeHarnessComposition
from lup.harness.generation import plugin_served_tool
from lup.providers import coordination_caller, peer_delivery
from lup.providers.roster_prompt import DEPARTURE_SCRIPT
from lup.types import JsonObject
from lup_template.harness.catalog import declared_plugin
from lup_template.harness.composition import claude_target, codex_target
from tests.unit.repos import commit_file
from tests.unit.test_in_process_parity import DISPATCHERS, Session, edited
from tests.unit.test_roster_prompt_hook import rendered, shipped

SESSION = "abc123"

CODEX_TOOLS = f"mcp__{COORDINATION_SERVER}__"
CLAUDE_TOOLS = f"{plugin_served_tool(declared_plugin().name, COORDINATION_SERVER)}__"

RUNTIMES = pytest.mark.parametrize(
    ("target", "tree", "tools", "decision"),
    [
        pytest.param(claude_target, ".claude", CLAUDE_TOOLS, None, id="claude"),
        pytest.param(codex_target, ".codex", CODEX_TOOLS, "allow", id="codex"),
    ],
)
"""Each runtime, how it names a coordination tool, and what its rewrite needs.

Claude Code applies a PreToolUse ``updatedInput`` carrying no decision, and the
coordination tools keep whatever permission the project gave them — measured
on 2.1.283, where the rewrite reached the tool server even for a key the
tool's schema forbids. Codex applies one only beside ``permissionDecision:
"allow"`` — measured on 0.158.0, where the same rewrite with no decision was
dropped and the call ran as the model wrote it.
"""


class Hook(TypedDict):
    """One registered command hook, as far as these cases read it."""

    command: str


class Group(TypedDict, total=False):
    """One matcher group under an event."""

    matcher: str
    hooks: list[Hook]


class Registered(BaseModel):
    """A plugin's hooks file, read back into the shape both runtimes share."""

    hooks: dict[str, list[Group]]


def hooks_of(
    target: Callable[[Path], NativeHarnessComposition], tree: str
) -> dict[str, list[Group]]:
    """One runtime's registered hooks, as its plugin ships them."""
    artifacts = shipped(target)
    return Registered.model_validate_json(
        artifacts[rendered(tree) / "hooks" / "hooks.json"].content
    ).hooks


def commands(groups: list[Group]) -> list[str]:
    """Every command a list of hook groups runs."""
    return [hook["command"] for group in groups for hook in group.get("hooks", [])]


def plugin_hooks(
    target: Callable[[Path], NativeHarnessComposition], tree: str, into: Path
) -> Path:
    """Every file one runtime's plugin carries under ``hooks/``, laid out as it lays them.

    Whole rather than the guard and its entry, because a host half imports
    the kernel and the store beside it, and a copy of fewer files would test
    a layout no plugin has. Hands back the scripts directory the runtime runs
    a guard from.
    """
    plugin = rendered(tree)
    for path, artifact in shipped(target).items():
        if path.is_relative_to(plugin / "hooks"):
            landed = into / path.relative_to(plugin)
            landed.parent.mkdir(parents=True, exist_ok=True)
            landed.write_text(artifact.content, encoding="utf-8")
    return into / "hooks" / "scripts"


def repository(tmp_path: Path) -> tuple[Path, RepositoryPeers]:
    """A throwaway repository whose session has joined its roster."""
    work = tmp_path / "repository"
    work.mkdir()
    sh.git("init", "-q", str(work))
    peers = RepositoryPeers(work)
    peers.join(SESSION, work, cli_name="orchestrator")
    return work, peers


def rollout_head(thread: str, agent_path: str) -> str:
    """The first line of a Codex rollout, as far as a spawn's name is read from it.

    Measured on 0.158.0: a subagent's rollout opens with its ``session_meta``,
    whose ``id`` is the ``agent_id`` its events carry and whose ``agent_path``
    is ``/root/<task_name>`` under the name the spawn went out with.
    """
    spawn = {"parent_thread_id": "01a0e915", "depth": 1, "agent_path": agent_path}
    meta = {"id": thread, "agent_path": agent_path, "agent_nickname": "Popper"}
    source = {"source": {"subagent": {"thread_spawn": spawn}}}
    return json.dumps({"type": "session_meta", "payload": {**meta, **source}}) + "\n"


def ran(guard: Path, cwd: Path, payload: JsonObject) -> str:
    """One rendered guard, run as the runtime runs it for this session."""
    return str(
        sh.sh(
            str(guard),
            _in=json.dumps(payload),
            _cwd=str(cwd),
            _env={**os.environ, MEMBER_ENV: SESSION},
        )
    )


@RUNTIMES
def test_the_caller_hook_is_registered_for_the_coordination_tools_alone(
    target: Callable[[Path], NativeHarnessComposition],
    tree: str,
    tools: str,
    decision: str | None,
) -> None:
    [group] = [
        group
        for group in hooks_of(target, tree)["PreToolUse"]
        if any(coordination_caller.GUARD_SCRIPT in line for line in commands([group]))
    ]
    assert group.get("matcher") == f"{tools}.*"
    assert all("exit 2" not in line for line in commands([group]))


@RUNTIMES
def test_the_rendered_caller_hook_stamps_the_calling_subagent(
    target: Callable[[Path], NativeHarnessComposition],
    tree: str,
    tools: str,
    decision: str | None,
    tmp_path: Path,
) -> None:
    """What the agent wrote in the field is overwritten, never read."""
    scripts = plugin_hooks(target, tree, tmp_path / "plugin")
    guard = scripts / coordination_caller.GUARD_SCRIPT
    work = tmp_path / "work"
    work.mkdir()
    transcript = tmp_path / "session.jsonl"
    spawned = transcript.with_suffix("") / "subagents" / "agent-a0cacac5.meta.json"
    spawned.parent.mkdir(parents=True)
    spawned.write_text(json.dumps({"name": "builder"}), encoding="utf-8")
    transcript.write_text(rollout_head("a0cacac5", "/root/builder"), encoding="utf-8")
    payload: JsonObject = {
        "hook_event_name": "PreToolUse",
        "session_id": "native",
        "transcript_path": str(transcript),
        "cwd": str(work),
        "tool_name": f"{tools}coordination_describe",
        "tool_input": {
            "description": "building the CLI",
            store.CALLER_FIELD: {"agent_id": "forged"},
        },
        "agent_id": "a0cacac5",
        "agent_type": "general-purpose",
    }

    answer = json.loads(ran(guard, work, payload))["hookSpecificOutput"]

    caller = answer["updatedInput"][store.CALLER_FIELD]
    assert answer["hookEventName"] == "PreToolUse"
    assert answer["updatedInput"]["description"] == "building the CLI"
    assert caller["agent_id"] == "a0cacac5"
    assert caller["agent_type"] == "general-purpose"
    assert caller["cwd"] == str(work)
    assert answer.get("permissionDecision") == decision
    # What the spawn was called is read where each runtime records it: Claude
    # Code beside the session's transcript, Codex atop the subagent's own.
    assert caller.get("name", "") == "builder"

    root = {key: value for key, value in payload.items() if not key.startswith("agent")}
    stamped = json.loads(ran(guard, work, root))["hookSpecificOutput"]
    assert stamped["updatedInput"][store.CALLER_FIELD]["agent_id"] == ""


@pytest.mark.parametrize(
    ("head", "named"),
    [
        pytest.param(rollout_head("a0cacac5", "/root/builder"), "builder", id="own"),
        pytest.param(
            rollout_head("a0cacac5", "/root/lead/builder"), "builder", id="nested"
        ),
        # The session's rollout, as SubagentStop's transcript_path names it.
        pytest.param(rollout_head("01a0e915", "/root/builder"), "", id="another"),
        pytest.param("", "", id="empty"),
        pytest.param("not json\n", "", id="unreadable"),
    ],
)
def test_a_codex_subagent_is_named_from_its_own_rollout_alone(
    tmp_path: Path, head: str, named: str
) -> None:
    guard = plugin_hooks(codex_target, ".codex", tmp_path / "plugin")
    work = tmp_path / "work"
    work.mkdir()
    rollout = tmp_path / "rollout-a0cacac5.jsonl"
    rollout.write_text(head + '{"type": "response_item"}\n', encoding="utf-8")
    payload: JsonObject = {
        "hook_event_name": "PreToolUse",
        "transcript_path": str(rollout),
        "cwd": str(work),
        "tool_name": f"{CODEX_TOOLS}coordination_describe",
        "tool_input": {"description": "building the CLI"},
        "agent_id": "a0cacac5",
        "agent_type": "default",
    }

    answer = ran(guard / coordination_caller.GUARD_SCRIPT, work, payload)

    caller = json.loads(answer)["hookSpecificOutput"]["updatedInput"]
    assert caller[store.CALLER_FIELD].get("name", "") == named


@RUNTIMES
def test_a_subagent_stopping_ends_its_own_row_and_not_its_sessions(
    target: Callable[[Path], NativeHarnessComposition],
    tree: str,
    tools: str,
    decision: str | None,
    tmp_path: Path,
) -> None:
    hooks = hooks_of(target, tree)
    assert any(DEPARTURE_SCRIPT in line for line in commands(hooks["SubagentStop"]))
    guard = plugin_hooks(target, tree, tmp_path / "plugin") / DEPARTURE_SCRIPT
    work, peers = repository(tmp_path)
    child = peers.join_subagent(SESSION, store.Caller(agent_id="a0cacac5"))
    peers.send(child.id, "one more thing")

    ended = ran(
        guard,
        work,
        {
            "hook_event_name": "SubagentStop",
            "session_id": "native",
            "cwd": str(work),
            "agent_id": "a0cacac5",
            "agent_type": "general-purpose",
        },
    )

    assert ended == ""
    assert peers.live_ids() == [SESSION]
    [forwarded] = peers.waiting(SESSION).messages
    assert "one more thing" in forwarded.text


@RUNTIMES
def test_mail_reaches_a_subagent_at_its_own_next_call(
    target: Callable[[Path], NativeHarnessComposition],
    tree: str,
    tools: str,
    decision: str | None,
    tmp_path: Path,
) -> None:
    """And the session's own mail waits for the session's own call."""
    scripts = plugin_hooks(target, tree, tmp_path / "plugin")
    guard = scripts / peer_delivery.GUARD_SCRIPT
    work, peers = repository(tmp_path)
    child = peers.join_subagent(SESSION, store.Caller(agent_id="a0cacac5"))
    peers.send(child.id, "rebase before you commit")
    peers.send(SESSION, "for the orchestrator")
    call: JsonObject = {
        "hook_event_name": "PreToolUse",
        "session_id": "native",
        "cwd": str(work),
        "tool_name": "Read",
        "tool_input": {},
    }

    told = json.loads(ran(guard, work, {**call, "agent_id": "a0cacac5"}))

    context = told["hookSpecificOutput"]["additionalContext"]
    assert "rebase before you commit" in context
    assert "for the orchestrator" not in context
    assert peers.waiting(child.id).messages == []
    assert len(peers.waiting(SESSION).messages) == 1
    assert "for the orchestrator" in ran(guard, work, call)


def native_send(target: str, work: Path, agent: str = "") -> JsonObject:
    """One native send, as Claude Code carries it, from the session or a subagent."""
    payload: JsonObject = {
        "session_id": "native",
        "hook_event_name": "PreToolUse",
        "tool_name": "SendMessage",
        "tool_input": {"to": target, "message": "the CLI half is done"},
        "cwd": str(work),
    }
    if agent:
        payload["agent_id"] = agent
        payload["agent_type"] = "general-purpose"
    return json.loads(
        str(
            sh.Command(sys.executable)(
                "-I",
                "-S",
                str(DISPATCHERS["claude"].resolve()),
                _in=json.dumps(payload),
                _env={
                    "PATH": "/usr/bin:/bin",
                    "HOME": str(Path.home()),
                    MEMBER_ENV: SESSION,
                },
            )
        )
    )


def test_a_native_send_inside_one_session_stays_local(tmp_path: Path) -> None:
    """It never leaves the session, so there is no record another worktree misses."""
    work, peers = repository(tmp_path)
    peers.join(mint_member_id(), work, cli_name="elsewhere")
    child = peers.join_subagent(SESSION, store.Caller(agent_id="a0cacac5"))

    assert native_send("orchestrator", work, agent="a0cacac5") == {}
    assert native_send(child.id, work) == {}
    refused = native_send("elsewhere", work, agent="a0cacac5")["hookSpecificOutput"]
    assert isinstance(refused, dict)
    assert refused["permissionDecision"] == "deny"
    assert "coordination_send" in str(refused["permissionDecisionReason"])


@pytest.fixture
def session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Session:
    for name in [name for name in os.environ if name.startswith("LUP_")]:
        monkeypatch.delenv(name)
    return Session(tmp_path.resolve())


@pytest.mark.parametrize(
    ("holder", "effect"),
    [
        pytest.param("sibling", "ask", id="its-sibling-holds-it"),
        pytest.param("session", "allow", id="its-session-holds-it"),
    ],
)
def test_a_subagents_edit_is_judged_against_its_own_row_everywhere(
    session: Session, holder: str, effect: str
) -> None:
    """A lock holds a file for one subagent against its sibling, on both runtimes."""
    commit_file(session.git, session.checkout, "cli.py", "value = 1\n", "seed")
    peers = RepositoryPeers(session.checkout)
    member = mint_member_id()
    peers.join(member, session.checkout, cli_name="orchestrator")
    sibling = peers.join_subagent(member, store.Caller(agent_id="b1dbdbd6"))
    peers.lock(sibling.id if holder == "sibling" else member, session.checkout)
    session.environment[MEMBER_ENV] = member
    target = session.checkout / "cli.py"

    for runtime in DISPATCHERS:
        call = {
            **edited(runtime, target, "value = 1\n", "value = 2\n"),
            "agent_id": "a0cacac5",
            "agent_type": "general-purpose",
        }
        assert session.dispatched(runtime, call) == effect


def test_what_a_subagent_writes_is_held_on_its_own_row(session: Session) -> None:
    commit_file(session.git, session.checkout, "cli.py", "value = 1\n", "seed")
    peers = RepositoryPeers(session.checkout)
    member = mint_member_id()
    peers.join(member, session.checkout, cli_name="orchestrator")
    session.environment[MEMBER_ENV] = member
    target = session.checkout / "cli.py"

    sh.Command(sys.executable)(
        "-I",
        "-S",
        str(DISPATCHERS["claude"].resolve()),
        _in=json.dumps(
            {
                "session_id": "parity",
                "hook_event_name": "PostToolUse",
                "cwd": str(session.checkout),
                "tool_name": "Edit",
                "tool_input": {"file_path": str(target)},
                "agent_id": "a0cacac5",
                "agent_type": "general-purpose",
            }
        ),
        _env=session.environment,
    )

    child = store.subagent_id(member, "a0cacac5")
    [claim] = peers.holding(target)
    assert [holder.id for holder in claim.holders] == [child]
    row = peers.row(child)
    assert row is not None and row.parent == member
