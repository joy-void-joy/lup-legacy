"""What a session nobody launched is known by: the id its runtime gave the process.

A natively opened tool server falling back to the name of the session it
opened — the one constant `harness` — would join every unlaunched session in
a worktree as one member while its hooks answer to the runtime's own session
id. What is asserted is that the server asks the runtime's adapter, that the
launcher's id outranks the answer, and that no answer serves no coordination
verbs at all.
"""

import pytest

from lup.coordination.identity import MEMBER_ENV
from lup.coordination.peer_tools import RosterPulse
from lup.coordination.relay import MailboxRelay
from lup.mcp import Coordination
from lup.coordination.bare.runtime import stdin_runtime
from lup.mcp.serve import (
    context_needs,
    harness_session_context,
    resolved_needs,
    serve_command,
)
from lup.providers.claude.identity import CLAUDE_SESSION_ENV
from lup.providers.identity import native_session_id
from lup.tools.mcp import ServerCompanion
from lup.tools.toolsets import SessionNeeds, assembled
from lup.workspace.context import SESSION_DIR_ENV
from lup_template.agent.config import settings
from lup_template.agent.toolsets import declared_tool_groups
from lup_template.harness.catalog import HARNESS_SESSION

SESSION_NEEDS = "lup_template.agent.toolsets.session_needs"
"""The needs hook every generated server entry names on its command line."""


def unlaunched(monkeypatch: pytest.MonkeyPatch) -> None:
    """No launcher minted an id, and no adapter relayed a session."""
    monkeypatch.delenv(MEMBER_ENV, raising=False)
    monkeypatch.delenv(SESSION_DIR_ENV, raising=False)


def opened(identity: str) -> SessionNeeds:
    """The session a native server opens under the harness name, known by *identity*."""
    return context_needs(harness_session_context(HARNESS_SESSION), identity)


def coordination_companions(identity: str) -> list[ServerCompanion]:
    """What runs beside the coordination server that session hosts."""
    server = Coordination().hosted(opened(identity))
    return server.companions if server is not None else []


def test_claude_hands_its_servers_the_session_id_and_codex_hands_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(CLAUDE_SESSION_ENV, "abc-123")

    assert native_session_id("claude") == "abc-123"
    assert native_session_id("codex") == ""
    assert native_session_id("openai") == ""

    monkeypatch.delenv(CLAUDE_SESSION_ENV)

    assert native_session_id("claude") == ""


def test_a_native_server_joins_under_the_id_its_runtime_gave_the_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unlaunched(monkeypatch)

    [pulse, relay] = coordination_companions("abc-123")

    assert isinstance(pulse, RosterPulse)
    assert pulse.member_id == "abc-123"
    assert isinstance(relay, MailboxRelay)
    assert relay.member_id == "abc-123"


def test_a_native_server_with_no_identity_serves_no_coordination_verbs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nothing, rather than a member every session of the worktree would share."""
    unlaunched(monkeypatch)
    needs = opened("")

    assert Coordination().hosted(needs) is None
    toolset = assembled(declared_tool_groups(), needs)
    assert "coordination" not in toolset.groups
    assert toolset.companions == {}


def test_the_launcher_s_id_outranks_the_runtime_s(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unlaunched(monkeypatch)
    monkeypatch.setenv(MEMBER_ENV, "launched1")

    [pulse, relay] = coordination_companions("abc-123")

    assert isinstance(pulse, RosterPulse)
    assert pulse.member_id == "launched1"
    assert isinstance(relay, MailboxRelay)
    assert relay.member_id == "launched1"


def test_serving_for_claude_lists_the_coordination_verbs_under_the_runtime_s_id(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The whole path: `--runtime claude --session harness`, no launcher, one runtime id.

    The needs hook sets the engine from the runtime it is given, which is
    restored after the test rather than left to the next one.
    """
    unlaunched(monkeypatch)
    monkeypatch.setattr(settings, "agent_sdk", settings.agent_sdk)
    monkeypatch.setattr(settings, "sandbox_enabled", False)
    monkeypatch.setenv(CLAUDE_SESSION_ENV, "abc-123")
    arguments = Coordination().served().arguments()

    def listed() -> str:
        """The tool names the generated coordination entry would serve."""
        serve_command(
            *arguments,
            session=HARNESS_SESSION,
            runtime="claude",
            needs=SESSION_NEEDS,
            list_only=True,
        )
        return capsys.readouterr().out

    with_identity = listed()
    monkeypatch.delenv(CLAUDE_SESSION_ENV)
    without = listed()

    assert "coordination_peers" in with_identity
    assert without == ""


def test_a_served_pulse_answers_for_the_process_feeding_the_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The server serves the session; the runtime at the other end of its input is it."""
    unlaunched(monkeypatch)
    monkeypatch.setenv(CLAUDE_SESSION_ENV, "abc-123")
    needs = resolved_needs(HARNESS_SESSION, "claude")
    assert needs is not None
    server = Coordination().hosted(needs)
    assert server is not None

    [pulse, _] = server.companions

    assert isinstance(pulse, RosterPulse)
    assert pulse.runtime == stdin_runtime()
    assert needs.runtime == stdin_runtime()
