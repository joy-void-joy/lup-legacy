"""The hooks of a runtime started from a session's shell act for that runtime, not the session.

Every process a launched session starts inherits its `LUP_COORDINATION_MEMBER`,
so a `claude -p` or `codex exec` run from the session's shell — or a pipeline
opening sessions of its own — carries the session's id into its own hooks.
Taken at its word, its ending would end the session's row, its tool calls
would be handed the session's mail, and its prompts would read its transcript
as the session's rewound conversation and clear what the session said it was
doing. Each guard is
run here as its runtime runs it, the process running these cases standing in
for the spawned runtime, over a store whose session row names another live
process — this one's parent — as the runtime it answers for.
"""

import json
import os
import sys
from collections.abc import Callable
from pathlib import Path

import pytest
import sh

from lup.coordination.bare import store
from lup.coordination.bare.changes import changes
from lup.coordination.bare.runtime import Runtime, runtime_of
from lup.coordination.identity import MEMBER_ENV, mint_member_id
from lup.coordination.repository import RepositoryPeers
from lup.harness.generate import NativeHarnessComposition
from lup.providers import peer_delivery
from lup.providers.roster_prompt import DEPARTURE_SCRIPT, GUARD_SCRIPT
from lup.types import JsonObject
from lup_template.harness.composition import claude_target, codex_target
from tests.unit.repos import commit_file
from tests.unit.test_in_process_parity import DISPATCHERS, Session
from tests.unit.test_subagent_roster_hooks import plugin_hooks

RUNTIMES = pytest.mark.parametrize(
    ("target", "tree"),
    [
        pytest.param(claude_target, ".claude", id="claude"),
        pytest.param(codex_target, ".codex", id="codex"),
    ],
)

SPAWNED = runtime_of(os.getpid())
"""The runtime the guards run under: the process feeding their input."""

SESSION = runtime_of(os.getppid())
"""The runtime whose shell started it, which the session's row names."""


def launched(peers: RepositoryPeers, work: Path, runtime: Runtime) -> str:
    """A launched session on the roster, its row answering for *runtime*."""
    member = mint_member_id()
    peers.join(member, work, cli_name="lead")
    peers.describe(member, "orchestrating the release")

    def answering(found: store.Member) -> store.Member:
        """This member, naming the process it answers for."""
        settled = found.copy()
        settled["runtime"] = runtime
        return settled

    peers.revise(member, answering)
    return member


def repository(tmp_path: Path) -> tuple[Path, RepositoryPeers, str]:
    """A throwaway repository whose launched session runs under another process."""
    work = tmp_path / "repository"
    work.mkdir()
    sh.git("init", "-q", str(work))
    peers = RepositoryPeers(work)
    return work, peers, launched(peers, work, SESSION)


def ran(guard: Path, cwd: Path, member: str, payload: JsonObject) -> str:
    """One rendered guard, run by this process under the launcher's id it inherited."""
    return str(
        sh.sh(
            str(guard),
            _in=json.dumps(payload),
            _cwd=str(cwd),
            _env={**os.environ, MEMBER_ENV: member},
        )
    )


@RUNTIMES
def test_a_spawned_runtime_ending_leaves_the_session_standing(
    target: Callable[[Path], NativeHarnessComposition], tree: str, tmp_path: Path
) -> None:
    guard = plugin_hooks(target, tree, tmp_path / "plugin") / DEPARTURE_SCRIPT
    work, peers, member = repository(tmp_path)

    ran(
        guard,
        work,
        member,
        {"hook_event_name": "SessionEnd", "session_id": "spawned", "cwd": str(work)},
    )

    assert peers.live_ids() == [member]


@RUNTIMES
def test_the_session_s_mail_is_not_handed_to_a_runtime_spawned_from_its_shell(
    target: Callable[[Path], NativeHarnessComposition], tree: str, tmp_path: Path
) -> None:
    """And the spawned runtime's own mail reaches it, at its own next call."""
    guard = plugin_hooks(target, tree, tmp_path / "plugin") / peer_delivery.GUARD_SCRIPT
    work, peers, member = repository(tmp_path)
    peers.send(member, "for the orchestrator")
    call: JsonObject = {
        "hook_event_name": "PreToolUse",
        "session_id": "spawned",
        "cwd": str(work),
        "tool_name": "Read",
        "tool_input": {},
    }

    assert ran(guard, work, member, call) == ""
    assert len(peers.waiting(member).messages) == 1

    spawned = store.spawned_id(member, SPAWNED)
    peers.join(spawned, work)
    peers.send(spawned, "for the pipeline")
    told = json.loads(ran(guard, work, member, call))

    context = told["hookSpecificOutput"]["additionalContext"]
    assert "for the pipeline" in context
    assert "for the orchestrator" not in context
    assert peers.waiting(spawned).messages == []
    assert len(peers.waiting(member).messages) == 1


@RUNTIMES
def test_a_prompt_in_a_spawned_runtime_leaves_what_the_session_said(
    target: Callable[[Path], NativeHarnessComposition], tree: str, tmp_path: Path
) -> None:
    """Its transcript is another conversation, which is not the session's rewound one."""
    guard = plugin_hooks(target, tree, tmp_path / "plugin") / GUARD_SCRIPT
    work, peers, member = repository(tmp_path)
    own = tmp_path / "session.jsonl"
    own.write_text(
        json.dumps({"type": "user", "uuid": "u1", "parentUuid": None}) + "\n",
        encoding="utf-8",
    )
    changes(peers.root, member, work, str(own))
    spawned = tmp_path / "spawned.jsonl"
    spawned.write_text(own.read_text(encoding="utf-8"), encoding="utf-8")

    ran(
        guard,
        work,
        member,
        {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "spawned",
            "cwd": str(work),
            "transcript_path": str(spawned),
        },
    )

    [row] = [one for one in peers.present() if one.actor.id == member]
    assert row.description == "orchestrating the release"
    assert row.transcript == str(own)


@pytest.fixture
def session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Session:
    for name in [name for name in os.environ if name.startswith("LUP_")]:
        monkeypatch.delenv(name)
    return Session(tmp_path.resolve())


def test_what_a_spawned_runtime_writes_is_held_on_its_own_row(
    session: Session,
) -> None:
    """Recorded by the permission dispatcher, as the subagent case is."""
    commit_file(session.git, session.checkout, "cli.py", "value = 1\n", "seed")
    peers = RepositoryPeers(session.checkout)
    member = launched(peers, session.checkout, SESSION)
    spawned = store.spawned_id(member, SPAWNED)
    peers.join(spawned, session.checkout)
    session.environment[MEMBER_ENV] = member
    target = session.checkout / "cli.py"
    target.write_text("value = 2\n", encoding="utf-8")

    sh.Command(sys.executable)(
        "-I",
        "-S",
        str(DISPATCHERS["claude"].resolve()),
        _in=json.dumps(
            {
                "session_id": "spawned",
                "hook_event_name": "PostToolUse",
                "cwd": str(session.checkout),
                "tool_name": "Edit",
                "tool_input": {"file_path": str(target)},
            }
        ),
        _env=session.environment,
    )

    holders = {holder.id for claim in peers.holding(target) for holder in claim.holders}
    assert holders == {spawned}
