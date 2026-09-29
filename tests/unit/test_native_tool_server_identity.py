"""A launched session's identity reaches the coordination server Codex starts for it."""

from pathlib import Path

import pytest

from lup.coordination.identity import MEMBER_ENV, NAME_ENV
from lup.coordination.peer_tools import RosterPulse
from lup.coordination.relay import MailboxRelay
from lup.harness.environment import tool_server_env
from lup.mcp import Coordination
from lup.mcp.serve import context_needs
from lup.workspace.context import SessionContext


def test_the_forwarded_identity_is_the_one_the_served_coordination_group_joins_as(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """What the launch forwards is enough for the server's pulse and relay.

    Codex starts a stdio server under a fixed base environment and forwards
    only what its ``env_vars`` names, so the served group sees the launcher's
    variables and nothing else of the environment it was launched from. The
    launcher's member id outranks the native session's own identity.
    """
    server = Coordination()
    parent = {MEMBER_ENV: "launched-recipient", NAME_ENV: "review-recipient"}
    assert set(parent) <= set(server.launcher_variables())
    for name in tool_server_env():
        if name in parent:
            monkeypatch.setenv(name, parent[name])
        else:
            monkeypatch.delenv(name, raising=False)
    context = SessionContext(
        session_dir=tmp_path / "session",
        gate_flag=tmp_path / "gate",
        session_id="native-thread",
        task_id="native-thread",
    )
    needs = context_needs(context, "native-thread")
    group = server.group()

    pulse, relay = group.companions(needs)

    assert isinstance(pulse, RosterPulse) and isinstance(relay, MailboxRelay)
    assert pulse.member_id == relay.member_id == parent[MEMBER_ENV]
    assert group.tools(needs)
