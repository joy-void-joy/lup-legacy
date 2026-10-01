# lup: ignore[dict-str-payload, os-environ, set-shape]
# Test fixtures and assertions construct these shapes deliberately.
"""Round-trip test of the ``tools serve`` subprocess with session-context env.

This is the wiring the Codex/OpenAI adapters depend on: each declared server
is started in a process of its own from relayed env vars, the reflection gate
crosses the process boundary through a flag file, and review plus metrics
artifacts land in the session directory where the parent process reads them.
Typed output is bound per turn by the runtime, not served by this process. No
LLM is involved.
"""

import asyncio
import json
import os
from pathlib import Path

import pytest
import sh
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from lup.mcp import HostedServer, ServeLaunch
from lup.observability.metrics import read_metrics_snapshots
from lup.workspace.context import (
    GATE_FLAG_ENV,
    OUTPUTS_DIR_ENV,
    REALTIME_DIR_ENV,
    SESSION_DIR_ENV,
    SESSION_ID_ENV,
)
from lup.orchestration.realtime.relay import MetaEvent, RealtimeMailbox, ReplyEvent
from lup.sessions.surface import Agent
from lup.sandbox.container import Sandbox
from lup.orchestration.subagents import create_run_subagent_tool
from lup.types import SubagentSpec

from lup.workspace.paths import project_root

from lup.coordination.identity import session_member_id
from lup.orchestration.reflection import ReviewGate
from lup.tools.toolsets import SessionNeeds, assembled
from lup.tools.toolsets import served_names as declared_served

from lup_template.agent.subagents import get_subagent_specs
from lup_template.agent.toolsets import (
    EXAMPLE_GROUP,
    NOTES_GROUP,
    declared_tool_groups,
    declared_tool_servers,
    session_needs,
)
from lup_template.harness.catalog import HARNESS_SESSION

pytestmark = pytest.mark.integration
SUBPROCESS_TIMEOUT_SECONDS = 20

LAUNCH = ServeLaunch(
    program=["uv", "run", "lup-devtools", "tools", "serve"], needs=session_needs
)
"""How this project's session factory starts a server it hosts, one per process."""


def declared(name: str) -> HostedServer:
    """The server this project declares under *name*."""
    return next(server for server in declared_tool_servers() if server.name == name)


def served_command(server: HostedServer, *options: str) -> list[str]:
    """The command line serving *server* alone, as the session factory writes it."""
    command = LAUNCH.command(server)
    return [command["command"], *command.get("args", []), *options]


def server_parameters(
    server: HostedServer, env: dict[str, str]
) -> StdioServerParameters:
    """The subprocess serving *server* alone, under the relayed session *env*."""
    program, *arguments = served_command(server)
    return StdioServerParameters(command=program, args=arguments, env=env)


def unused_subagent_factory(_spec: SubagentSpec) -> Agent:
    """Keep delegation construction real without executing a model session."""
    raise AssertionError("the registry-name test must not invoke a subagent")


async def test_serve_tools_session_round_trip(tmp_path: Path) -> None:
    session_dir = tmp_path / "session"
    gate_flag = tmp_path / "gate_flag"

    params = server_parameters(
        declared(NOTES_GROUP),
        {
            **os.environ,
            SESSION_DIR_ENV: str(session_dir),
            GATE_FLAG_ENV: str(gate_flag),
            OUTPUTS_DIR_ENV: str(tmp_path / "outputs"),
            "AGENT_SANDBOX_ENABLED": "false",
        },
    )

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await asyncio.wait_for(
                session.initialize(), timeout=SUBPROCESS_TIMEOUT_SECONDS
            )

            listed = await asyncio.wait_for(
                session.list_tools(), timeout=SUBPROCESS_TIMEOUT_SECONDS
            )
            # One server per process: the notes server serves its own group
            # and no other's, the example placeholder's included.
            names = {tool.name for tool in listed.tools}
            assert names == {"review", "run_subagent"}

            reviewed = await asyncio.wait_for(
                session.call_tool(
                    "review",
                    {
                        "assessment": "wiring test",
                        "confidence": 0.9,
                        "tool_audit": "none used",
                        "process_reflection": "n/a",
                        "skip_reviewer": True,
                    },
                ),
                timeout=SUBPROCESS_TIMEOUT_SECONDS,
            )
            assert reviewed.is_error is False
            assert gate_flag.exists()

    assert (session_dir / "review.json").exists()
    [snapshot] = read_metrics_snapshots(session_dir)
    assert snapshot.server == NOTES_GROUP
    assert snapshot.summary["by_tool"]["review"]["call_count"] == 1


def served_names(env: dict[str, str], server: HostedServer) -> set[str]:
    """Run ``tools serve --list`` for one declared server; return the names."""
    program, *arguments = served_command(server, "--list")
    out = sh.Command(program)(*arguments, _env=env)
    return {line for line in str(out).splitlines() if line}


def test_served_group_names_match_toolset_registry(tmp_path: Path) -> None:
    """Each served server must list exactly what the toolsets registry builds.

    The declaration is the single source of tool groups for every backend,
    and a stdio server is one of its entries validated back from its command
    line — so each server lists precisely its group's registry tools. A
    session starts one process per group it builds, never the example
    placeholder, which serves only when started by name; together those
    processes serve every group's tools but the example's.
    """
    session_dir = tmp_path / "session"
    realtime_dir = session_dir / "realtime"
    env = {
        **os.environ,
        SESSION_DIR_ENV: str(session_dir),
        GATE_FLAG_ENV: str(tmp_path / "gate_flag"),
        OUTPUTS_DIR_ENV: str(tmp_path / "outputs"),
        SESSION_ID_ENV: "registry-match",
        REALTIME_DIR_ENV: str(realtime_dir),
    }

    declaration = declared_tool_groups()
    needs = SessionNeeds(
        session_dir=session_dir,
        root=project_root(),
        gate=ReviewGate(flag_path=tmp_path / "gate_flag"),
        outputs_dir=tmp_path / "outputs",
        sandbox=Sandbox(
            session_id="registry-match", shared_dir=session_dir / "sandbox_shared"
        ),
        realtime_dir=realtime_dir,
        subagent_tool=create_run_subagent_tool(
            get_subagent_specs(), factory_recipe=unused_subagent_factory
        ),
        # The same identity the subprocess resolves from the relayed session
        # id, so both sides build the groups that wait on one.
        member=session_member_id("registry-match"),
    )
    groups = assembled(declaration, needs).groups
    launched = declared_served(declaration, needs)
    assert EXAMPLE_GROUP not in launched

    listed = {
        server.name: served_names(env, server)
        for server in declared_tool_servers()
        if server.name in (*launched, EXAMPLE_GROUP)
    }
    assert set(listed) == {*launched, EXAMPLE_GROUP}
    for name, names in listed.items():
        assert names == {tool.name for tool in groups[name]}

    default_expected = {
        tool.name
        for name, tools in groups.items()
        if name != EXAMPLE_GROUP
        for tool in tools
    }
    assert {tool for name in launched for tool in listed[name]} == default_expected


async def test_native_tree_server_command_serves_the_agent_tools(
    tmp_path: Path,
) -> None:
    """The command line the generated trees declare must reach real tools.

    A native runtime relays no session context, so the declaration names a
    session instead and the server opens it. Everything but that name and the
    notes location is taken from the artifact a runtime would read, and the
    project root stands in for the substitution the runtime performs.
    """
    root = project_root()
    declaration = json.loads((root / ".claude/plugins/lup/.mcp.json").read_text())
    entry = declaration["mcpServers"][NOTES_GROUP]
    args = [
        "harness-round-trip"
        if word == HARNESS_SESSION
        else word.replace("${CLAUDE_PROJECT_DIR}", str(root))
        for word in entry["args"]
    ]

    params = StdioServerParameters(
        command=entry["command"],
        args=args,
        env={
            **os.environ,
            "AGENT_NOTES_PATH": str(tmp_path / "notes"),
            "AGENT_SANDBOX_ENABLED": "false",
        },
    )

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await asyncio.wait_for(
                session.initialize(), timeout=SUBPROCESS_TIMEOUT_SECONDS
            )
            listed = await asyncio.wait_for(
                session.list_tools(), timeout=SUBPROCESS_TIMEOUT_SECONDS
            )
            assert {tool.name for tool in listed.tools} == {"review", "run_subagent"}


async def test_serve_tools_realtime_session_group(tmp_path: Path) -> None:
    """The relay round-trip the Codex realtime mode depends on.

    The session tool group is served only when the realtime directory is
    relayed; tool calls inside the subprocess must land as mailbox
    artifacts the parent process can consume.
    """
    session_dir = tmp_path / "session"
    realtime_dir = session_dir / "realtime"

    params = server_parameters(
        declared("session"),
        {
            **os.environ,
            SESSION_DIR_ENV: str(session_dir),
            GATE_FLAG_ENV: str(tmp_path / "gate_flag"),
            OUTPUTS_DIR_ENV: str(tmp_path / "outputs"),
            REALTIME_DIR_ENV: str(realtime_dir),
            "AGENT_SANDBOX_ENABLED": "false",
        },
    )

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await asyncio.wait_for(
                session.initialize(), timeout=SUBPROCESS_TIMEOUT_SECONDS
            )

            listed = await asyncio.wait_for(
                session.list_tools(), timeout=SUBPROCESS_TIMEOUT_SECONDS
            )
            names = {tool.name for tool in listed.tools}
            assert {
                "reply",
                "schedule_action",
                "debounce",
                "sleep",
                "remind",
                "context",
                "meta",
            } == names

            premature = await asyncio.wait_for(
                session.call_tool("sleep", {"seconds": 60}),
                timeout=SUBPROCESS_TIMEOUT_SECONDS,
            )
            assert premature.is_error is True

            replied = await asyncio.wait_for(
                session.call_tool(
                    "reply",
                    {"messages": [{"message": "hello from the subprocess"}]},
                ),
                timeout=SUBPROCESS_TIMEOUT_SECONDS,
            )
            assert replied.is_error is False

            await asyncio.wait_for(
                session.call_tool("meta", {"thought": "relay wiring test"}),
                timeout=SUBPROCESS_TIMEOUT_SECONDS,
            )
            recorded = await asyncio.wait_for(
                session.call_tool("sleep", {"seconds": 60}),
                timeout=SUBPROCESS_TIMEOUT_SECONDS,
            )
            assert recorded.is_error is False

    mailbox = RealtimeMailbox(realtime_dir)
    events = mailbox.read_new_events()
    assert [type(e) for e in events] == [ReplyEvent, MetaEvent]

    request = mailbox.consume_sleep_request()
    assert request is not None
    assert request.seconds == 60
