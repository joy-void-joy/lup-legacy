"""The operator pauses and resumes agents; nobody else can.

A pause holds an agent's next tool call until it is resumed, its subagents
with it, and a message to it waits without waking it. Freezing also stops the
commands its tools are running and interrupts its turn, where this process
can reach them, and says why where it cannot. A resume lifts only the pause it
names, continues what a freeze stopped, and wakes with a bare "continue" a
session that stopped because of the pause.
"""

import asyncio
import subprocess
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from typer.testing import CliRunner

from lup.coordination import watch as watching
from lup.coordination.bare import holds as bare
from lup.coordination.bare import store
from lup.coordination.bare.runtime import runtime_of, stat_fields
from lup.coordination.holds import (
    Hold,
    HoldOwner,
    HoldReason,
    HoldScope,
    holding,
    operator_pause,
    place,
)
from lup.coordination.identity import mint_member_id
from lup.coordination.mail import ActorMail
from lup.coordination.repository import RepositoryPeers
from lup.coordination.wake import WakePath, WakeRuntime, Woken
from lup.devtools.coordination import app as coordination_app
from lup.devtools.coordination import pausing
from lup.devtools.coordination.pausing import pause, resume
from lup.devtools.dashboard import supervision
from lup.devtools.dashboard.companion import KnownRepository
from lup.devtools.dashboard.live import held_rows
from lup.devtools.dashboard.pulse import DashboardPulse, LineFacts, StatusInput
from lup.devtools.dashboard.reviews import dashboard_app
from lup.devtools.dashboard.supervision import MessageRequest, reply
from lup.policy.enforcement import create_hold_hooks
from lup.policy.hooks import LupHookInput

BASE_URL = "http://127.0.0.1:8766"
TOKEN = "operator-capability"
WRITING = {"Authorization": f"Bearer {TOKEN}", "Origin": BASE_URL}


class Wakes:
    """Every wake a verb makes, answered as reached."""

    def __init__(self) -> None:
        self.made: list[tuple[str, str]] = []

    def __call__(
        self,
        path: WakePath,
        message: str,
        cwd: Path | None = None,
        *,
        queue_timeout_seconds: float = 20.0,
        priority: str = "next",
    ) -> Woken:
        del path, cwd, queue_timeout_seconds
        self.made.append((priority, message))
        return Woken(reached=True)


@pytest.fixture
def woken(monkeypatch: pytest.MonkeyPatch) -> Wakes:
    wakes = Wakes()
    monkeypatch.setattr(watching, "wake", wakes)
    monkeypatch.setattr(supervision, "wake", wakes)
    monkeypatch.setattr(pausing, "wake", wakes)
    return wakes


def session(
    peers: RepositoryPeers, root: Path, name: str, runtime: WakeRuntime = "claude"
) -> str:
    member = mint_member_id()
    peers.join(
        member,
        root / name,
        cli_name=name,
        wake=WakePath(runtime=runtime, handle=str(root / f"{name}.sock")),
    )
    return member


def test_pausing_a_session_holds_it_and_its_subagent_and_says_so_on_the_roster(
    tmp_path: Path,
) -> None:
    peers = RepositoryPeers(tmp_path)
    lead = session(peers, tmp_path, "lead")
    other = session(peers, tmp_path, "other")
    peers.join_subagent(lead, store.Caller(agent_id="a01"))

    paused = pause(peers, HoldScope.AGENT, lead)

    listed = {view.member.actor.id: view.held for view in peers.listing()}
    subagent = store.subagent_id(lead, "a01")
    assert set(paused.held) == {lead, subagent}
    assert listed[lead] == listed[subagent] == ["paused by the operator"]
    assert listed[other] == []
    assert "Paused lead" in paused.detail


def test_a_message_to_a_paused_agent_waits_without_waking_it(
    tmp_path: Path, woken: Wakes
) -> None:
    peers = RepositoryPeers(tmp_path)
    lead = session(peers, tmp_path, "lead")
    pause(peers, HoldScope.REPOSITORY)
    known = KnownRepository(repository=tmp_path, checkout=tmp_path)

    outcome = reply(known, lead, MessageRequest(text="the bound is 7"))

    assert woken.made == []
    assert not outcome.woken and "paused by the operator" in outcome.detail
    assert [message.text for message in peers.waiting(lead).messages] == [
        "the bound is 7"
    ]


def test_a_resume_lifts_only_the_pause_it_names(tmp_path: Path) -> None:
    peers = RepositoryPeers(tmp_path)
    lead = session(peers, tmp_path, "lead")
    pause(peers, HoldScope.REPOSITORY)

    with pytest.raises(LookupError, match="repository pause"):
        resume(peers, HoldScope.AGENT, lead)
    resumed = resume(peers, HoldScope.REPOSITORY)

    assert resumed.hold.scope is HoldScope.REPOSITORY
    assert holding(peers.root, lead) == []
    with pytest.raises(LookupError, match="nothing to resume"):
        resume(peers, HoldScope.REPOSITORY)


def test_a_resume_never_lifts_a_budget_hold(tmp_path: Path) -> None:
    peers = RepositoryPeers(tmp_path)
    lead = session(peers, tmp_path, "lead")
    place(
        peers.root,
        Hold(
            scope=HoldScope.SELF,
            member=lead,
            reason=HoldReason.RATE,
            owner=HoldOwner.BUDGET,
            said="over its rate",
        ),
    )
    pause(peers, HoldScope.AGENT, lead)

    resumed = resume(peers, HoldScope.AGENT, lead)

    assert [(each.member, each.said) for each in resumed.still] == [
        (lead, "over its rate")
    ]
    assert [hold.owner for hold in holding(peers.root, lead)] == [HoldOwner.BUDGET]


def test_a_session_refused_at_the_limit_is_told_to_continue_as_a_prompt(
    tmp_path: Path, woken: Wakes
) -> None:
    peers = RepositoryPeers(tmp_path)
    lead = session(peers, tmp_path, "lead")
    held_only = session(peers, tmp_path, "held-only")
    pause(peers, HoldScope.REPOSITORY)
    clock = iter([0.0, 100.0])
    refused = bare.held_call(
        peers.root,
        lead,
        bare.Waiting(tool="Bash", call="toolu_1"),
        began=0.0,
        seconds=60,
        clock=lambda: next(clock),
        pause=lambda _: None,
    )
    assert refused and [call.get("member") for call in bare.refused(peers.root)] == [
        lead
    ]

    resumed = resume(peers, HoldScope.REPOSITORY)

    assert resumed.woken == [lead] and held_only not in resumed.woken
    assert woken.made == [("next", "continue")]
    assert bare.refused(peers.root) == []
    prompts = [
        each for each in ActorMail(peers.root).latest_first() if each.message.prompt
    ]
    assert [(each.recipient.id, each.message.text) for each in prompts] == [
        (lead, "continue")
    ]


@pytest.fixture
def runtime() -> Iterator[subprocess.Popen[bytes]]:
    """A stand-in runtime with one command running in a process group of its own."""
    started = subprocess.Popen(["sh", "-c", "setsid sleep 600 & wait"])
    try:
        for _ in range(100):
            if any(
                fields[1] == str(started.pid)
                for entry in Path("/proc").iterdir()
                if entry.name.isdigit()
                for fields in [stat_fields(int(entry.name))]
                if len(fields) > 2
            ):
                break
            time.sleep(0.05)
        yield started
    finally:
        for group in pausing.tool_groups(started.pid):
            subprocess.run(["kill", "-KILL", f"-{group.pid}"], check=False)
        started.kill()
        started.wait()


def state(pid: int, settled: tuple[str, ...]) -> str:
    """The process's state once it reads as one of *settled*, or as it last read.

    A signal is delivered after `kill` returns, so the table is read until
    it shows the stop or the continue took, for up to five seconds.
    """
    read = ""
    for _ in range(100):
        fields = stat_fields(pid)
        read = fields[0] if fields else ""
        if read in settled:
            return read
        time.sleep(0.05)
    return read


def test_freezing_stops_a_session_s_commands_and_resuming_continues_them(
    tmp_path: Path, woken: Wakes, runtime: subprocess.Popen[bytes]
) -> None:
    peers = RepositoryPeers(tmp_path)
    lead = session(peers, tmp_path, "lead")
    store.adopt(peers.root, store.session_actor(lead), runtime_of(runtime.pid))

    frozen = pause(peers, HoldScope.AGENT, lead, freeze=True)
    groups = [group.pid for group in frozen.hold.frozen]

    assert frozen.frozen == [lead] and len(groups) == 1
    assert frozen.hold.frozen_sessions == [lead]
    assert state(groups[0], ("T",)) == "T"
    assert stat_fields(runtime.pid)[0] != "T"
    assert woken.made[0][0] == "now" and "paused by the operator" in woken.made[0][1]

    resumed = resume(peers, HoldScope.AGENT, lead)

    assert resumed.continued == groups
    assert state(groups[0], ("S", "R")) in ("S", "R")
    assert resumed.woken == [lead] and woken.made[-1] == ("next", "continue")


def test_what_cannot_be_frozen_is_paused_and_says_why(tmp_path: Path) -> None:
    peers = RepositoryPeers(tmp_path)
    lead = session(peers, tmp_path, "lead")
    codex = session(peers, tmp_path, "codex", runtime="codex")
    peers.join_subagent(lead, store.Caller(agent_id="a01"))
    subagent = store.subagent_id(lead, "a01")

    alone = pause(peers, HoldScope.AGENT, subagent, freeze=True)
    everyone = pause(peers, HoldScope.REPOSITORY, freeze=True)
    why = {each.member: each.why for each in [*alone.unfrozen, *everyone.unfrozen]}

    assert "subagent's commands run in its session's runtime" in why[subagent]
    assert "every other session there" in why[codex]
    assert "records no runtime process" in why[lead]
    assert set(everyone.held) == {lead, codex, subagent}
    assert everyone.frozen == []
    rows = held_rows(peers.root)
    assert not any(held.freeze for member in rows for held in rows[member])
    assert resume(peers, HoldScope.REPOSITORY).woken == []


def client(root: Path) -> AsyncClient:
    app = dashboard_app(BASE_URL, TOKEN, (root,))
    return AsyncClient(transport=ASGITransport(app=app), base_url=BASE_URL)


async def test_the_page_pauses_and_resumes_an_agent_and_a_repository(
    tmp_path: Path,
) -> None:
    peers = RepositoryPeers(tmp_path)
    lead = session(peers, tmp_path, "lead")
    key = KnownRepository(repository=tmp_path, checkout=tmp_path).key()

    async with client(tmp_path) as http:
        paused = await http.post(
            f"/api/repositories/{key}/sessions/{lead}/pause", headers=WRITING, json={}
        )
        again = await http.post(
            f"/api/repositories/{key}/sessions/{lead}/resume", headers=WRITING, json={}
        )
        twice = await http.post(
            f"/api/repositories/{key}/sessions/{lead}/resume", headers=WRITING, json={}
        )
        repository = await http.post(
            f"/api/repositories/{key}/pause", headers=WRITING, json={"freeze": False}
        )
        everything = await http.post("/api/resume", headers=WRITING, json={})

    assert paused.status_code == 200 and paused.json()["held"] == 1
    assert again.status_code == 200 and "Resumed lead" in again.json()["detail"]
    assert twice.status_code == 409 and "nothing to resume" in twice.json()["detail"]
    assert repository.status_code == 200 and repository.json()["repositories"] == [key]
    assert everything.status_code == 200 and holding(peers.root, lead) == []


def test_the_status_line_counts_the_agents_held() -> None:
    pulse = DashboardPulse(
        url="http://127.0.0.1:8766", pid=1, beat=datetime.now(UTC), held=2
    )
    line = LineFacts.of(pulse, StatusInput(), datetime.now(UTC)).fitted(0)

    assert "⏸2" in line.plain()


@pytest.mark.parametrize("verb", ["pause", "resume"])
def test_an_agent_s_shell_cannot_pause_or_resume_anyone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verb: str
) -> None:
    monkeypatch.setattr(coordination_app, "project_root", lambda: tmp_path)
    monkeypatch.setenv("LUP_COORDINATION_MEMBER", "an-agent")

    result = CliRunner().invoke(
        coordination_app.create_coordination_app(), [verb, "--repository"]
    )

    assert result.exit_code != 0
    assert "an agent's shell cannot" in result.output
    assert bare.placed_holds(RepositoryPeers(tmp_path).root) == []


def test_the_operator_s_terminal_pauses_and_resumes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(coordination_app, "project_root", lambda: tmp_path)
    monkeypatch.delenv("LUP_COORDINATION_MEMBER", raising=False)
    peers = RepositoryPeers(tmp_path)
    session(peers, tmp_path, "lead")
    commands = coordination_app.create_coordination_app()

    paused = CliRunner().invoke(commands, ["pause", "lead", "--tree"])
    listed = CliRunner().invoke(commands, ["held"])
    resumed = CliRunner().invoke(commands, ["resume", "lead", "--tree"])

    assert paused.exit_code == 0 and "Paused lead" in paused.output
    assert "operator tree hold on lead" in listed.output
    assert resumed.exit_code == 0 and "Resumed lead" in resumed.output


def test_a_session_opened_here_is_held_and_refused_at_the_limit(
    tmp_path: Path,
) -> None:
    peers = RepositoryPeers(tmp_path)
    lead = session(peers, tmp_path, "lead")
    place(peers.root, operator_pause(HoldScope.AGENT, lead))
    hooks = create_hold_hooks(peers.root, lead, seconds=1.5, timeout=60, poll=0.2)
    [matcher] = hooks.pre_tool_use

    started = time.monotonic()
    output = asyncio.run(
        matcher.hook(LupHookInput(event="PreToolUse", tool_name="Read"))
    )

    assert output.decision == "deny"
    assert output.reason == (
        "refused: paused by the operator; this call didn't run\n→ retry it"
    )
    assert 1.0 <= time.monotonic() - started < 5
    assert matcher.matcher == "" and matcher.timeout == 60
