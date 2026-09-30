"""The recursive-agent allowance across sessions and MCP subprocesses."""

from pathlib import Path

import pytest

from lup.providers.claude import Claude
from lup.providers.codex import Codex
from lup.sessions.recursion import (
    MAX_RECURSIVE_AGENT_ENV,
    RecursiveAgentAllowance,
    RecursiveAgentLimitError,
    child_recursive_agent_allowance,
    recursive_agent_allowance,
    recursive_agent_scope,
)
from lup.tools.mcp import RawStdioServerConfig, relay_recursive_agent_to_mcp


def test_unlimited_recursion_remains_unlimited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(MAX_RECURSIVE_AGENT_ENV, "-1")

    assert child_recursive_agent_allowance().remaining == -1


def test_each_opened_level_consumes_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(MAX_RECURSIVE_AGENT_ENV, "2")

    child = child_recursive_agent_allowance()
    with recursive_agent_scope(child):
        grandchild = child_recursive_agent_allowance()

    assert child.remaining == 1
    assert grandchild.remaining == 0


def test_an_explicit_allowance_ignores_unrelated_child_environment() -> None:
    child = child_recursive_agent_allowance(
        {MAX_RECURSIVE_AGENT_ENV: "2", "AGENT_MODEL": "example"}
    )

    assert child.remaining == 1


def test_zero_refuses_another_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(MAX_RECURSIVE_AGENT_ENV, "0")

    assert recursive_agent_allowance().remaining == 0
    with pytest.raises(RecursiveAgentLimitError, match=MAX_RECURSIVE_AGENT_ENV):
        child_recursive_agent_allowance()


def test_an_active_session_scope_overrides_the_process_relay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(MAX_RECURSIVE_AGENT_ENV, "9")

    with recursive_agent_scope(RecursiveAgentAllowance(remaining=0)):
        assert recursive_agent_allowance().remaining == 0


def test_stdio_mcp_inherits_the_same_remaining_allowance() -> None:
    server = RawStdioServerConfig(command="uv", args=["run", "tools"], env={"A": "b"})

    relayed = relay_recursive_agent_to_mcp(server, {MAX_RECURSIVE_AGENT_ENV: "3"})

    assert relayed == {
        "command": "uv",
        "args": ["run", "tools"],
        "env": {"A": "b", MAX_RECURSIVE_AGENT_ENV: "3"},
    }


@pytest.mark.parametrize("provider", ["claude", "codex"])
async def test_provider_session_opening_refuses_at_zero(
    provider: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(MAX_RECURSIVE_AGENT_ENV, "0")
    agent = (
        Claude(model="claude-fable-5")
        if provider == "claude"
        else Codex(model="gpt-5.6-sol", cwd=tmp_path)
    )

    with pytest.raises(RecursiveAgentLimitError):
        async with agent.open():
            pass
