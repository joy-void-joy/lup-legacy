"""A session's tools, declared once: built-in presets, exact names, MCP servers.

Each built-in preset compiles to what its provider is told, an exact list is
honoured name for name and a misspelt one is refused twice — by the checker
where it is written and by validation where it is read — and every server in
``lup.mcp`` compiles both ways: hosted in the process that opens a session, and
as the serve command a launched runtime starts, which validates back into the
same declaration and answers an MCP client over stdio.
"""

import asyncio
import json
import shutil
import sys
from pathlib import Path

import pytest
import sh
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from pydantic import ValidationError

from lup.mcp import (
    CodeIntel,
    Coordination,
    External,
    Group,
    HostedServer,
    Ledger,
    Sandbox,
    ServedServer,
    ServeLaunch,
    Toolset,
    hosted_servers,
    imported_tool,
)
from lup.ledger.models import LedgerEdge, LedgerNode
from lup.orchestration.reflection import ReviewGate
from lup.providers.claude import Claude, ClaudeTools
from lup.providers.claude.runtime import build_claude_options
from lup.providers.claude.selection import claude_config
from lup.providers.codex import Codex, CodexTools
from lup.providers.codex.builtins import CodexBuiltins
from lup.providers.codex.selection import codex_config
from lup.providers.selection import SessionRequest, SessionTools
from lup.tools.mcp import (
    LupMcpServerConfig,
    LupMcpTool,
    RawStdioServerConfig,
    lup_tool,
)
from lup.tools.toolsets import SessionNeeds, ToolGroup
from lup.workspace.context import SESSION_DIR_ENV

FIXTURES = Path(__file__).parent.parent / "fixtures"


def options(tools: ClaudeTools, system_prompt: str = "Be brief."):
    """The SDK options a Claude session with ``tools`` opens with."""
    return build_claude_options(
        Claude(tools=tools, system_prompt=system_prompt),
        servers={},
        binding=lambda: None,
        resume=None,
        session_id=None,
    )


def needs(root: Path, member: str = "probe-member") -> SessionNeeds:
    """A session's needs over a scratch directory, with a roster identity."""
    return SessionNeeds(
        session_dir=root / "session", root=root, gate=ReviewGate(), member=member
    )


def test_stock_is_claude_codes_preset_and_its_coding_prompt() -> None:
    compiled = options(ClaudeTools(builtin="stock"))

    assert compiled.tools == {"type": "preset", "preset": "claude_code"}
    assert compiled.system_prompt == {
        "type": "preset",
        "preset": "claude_code",
        "append": "Be brief.",
    }


def test_web_is_fetch_and_search_with_the_callers_prompt_alone() -> None:
    compiled = options(ClaudeTools(builtin="web"))

    assert compiled.tools == ["WebFetch", "WebSearch"]
    assert compiled.system_prompt == "Be brief."


def test_none_starts_claude_with_no_built_in() -> None:
    assert options(ClaudeTools(builtin="none")).tools == []


def test_an_exact_claude_list_is_the_roster_name_for_name() -> None:
    compiled = options(ClaudeTools(builtin=["Read", "Bash", "Read"]))

    assert compiled.tools == ["Read", "Bash"]
    assert compiled.system_prompt == "Be brief."


@pytest.mark.parametrize(
    ("builtin", "expected"),
    [
        (
            "stock",
            CodexBuiltins(
                shell=True, web=True, write=True, images=True, all_tools=True
            ),
        ),
        ("web", CodexBuiltins(web=True)),
        ("none", CodexBuiltins()),
        (["Bash", "apply_patch"], CodexBuiltins(shell=True, write=True)),
        (["WebSearch"], CodexBuiltins(web=True)),
    ],
)
def test_each_codex_grant_switches_on_exactly_its_facilities(
    builtin: str | list[str], expected: CodexBuiltins
) -> None:
    tools = CodexTools.model_validate({"builtin": builtin})

    assert Codex(tools=tools).builtins() == expected


def test_codex_web_is_live_search_and_no_shell() -> None:
    configuration = Codex().builtins().configuration()

    assert configuration["web_search"] == "live"
    features = configuration["features"]
    assert isinstance(features, dict)
    assert features["shell_tool"] is False
    assert features["standalone_web_search"] is True


def test_the_default_is_the_web_alone_on_both_providers() -> None:
    assert Claude().tools == ClaudeTools(builtin="web", mcp=[])
    assert options(ClaudeTools()).tools == ["WebFetch", "WebSearch"]
    assert Codex().builtins() == CodexBuiltins(web=True)


def test_a_portable_request_defaults_to_the_web_on_either_runtime(
    tmp_path: Path,
) -> None:
    request = SessionRequest(cwd=tmp_path)

    assert request.tools == SessionTools()
    assert claude_config(request).tools.builtin == "web"
    assert codex_config(request).tools.builtin == "web"
    assert codex_config(request).writable_roots == []


@pytest.mark.parametrize(
    ("declared", "misspelt"),
    [(ClaudeTools, ["Reed"]), (CodexTools, ["Read"]), (ClaudeTools, "everything")],
)
def test_a_name_the_provider_does_not_ship_is_refused(
    declared: type[ClaudeTools] | type[CodexTools], misspelt: str | list[str]
) -> None:
    with pytest.raises(ValidationError):
        declared.model_validate({"builtin": misspelt})


def test_a_misspelt_built_in_is_a_type_error_where_it_is_written(
    tmp_path: Path,
) -> None:
    """The Literal is the checker's too, so the typo never reaches a session."""
    program = shutil.which("pyright")
    if program is None:
        pytest.skip("pyright is not installed in this environment")
    written = tmp_path / "declared.py"
    written.write_text(
        "from lup.providers.claude import ClaudeTools\n"
        "from lup.providers.codex import CodexTools\n"
        'ClaudeTools(builtin=["Read", "Grep"])\n'
        'CodexTools(builtin=["Bash"])\n'
        'ClaudeTools(builtin=["Reed"])\n'
        'CodexTools(builtin=["Read"])\n'
    )
    report = sh.Command(program)(
        "--outputjson",
        "--pythonpath",
        sys.executable,
        str(written),
        _ok_code=[0, 1],
        _cwd=str(tmp_path),
    )
    diagnostics = json.loads(str(report))["generalDiagnostics"]
    errors = [
        entry["range"]["start"]["line"]
        for entry in diagnostics
        if entry["severity"] == "error"
    ]

    assert errors == [4, 5]


def test_two_servers_under_one_name_are_refused() -> None:
    with pytest.raises(ValidationError, match="named uniquely"):
        ClaudeTools(mcp=[Coordination(), Coordination()])


def test_allowed_tools_are_judged_against_the_declared_servers() -> None:
    @lup_tool("Record one value.")
    async def record(params: SessionTools) -> SessionTools:
        return params

    tools = ClaudeTools(builtin="none", mcp=[Toolset([record], name="app")])

    assert Claude(tools=tools, allowed_tools=["mcp__app__record"])
    with pytest.raises(ValidationError, match="outside this session's tools"):
        Claude(tools=tools, allowed_tools=["mcp__app__other"])
    with pytest.raises(ValidationError, match="outside this session's tools"):
        Claude(tools=tools, allowed_tools=["Bash"])


def test_plugin_directories_need_the_stock_tools(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="builtin='stock'"):
        Claude(plugin_dirs=[tmp_path])

    assert Claude(tools=ClaudeTools(builtin="stock"), plugin_dirs=[tmp_path])


class FakeContainer:
    """A container the session was given, which the sandbox server serves."""

    def __init__(self, tools: list[LupMcpTool]) -> None:
        self.tools = tools

    def create_tools(self, usage_notes: str = "") -> list[LupMcpTool]:
        return self.tools

    def stop(self) -> None:
        return None


@lup_tool("Run nothing.")
async def execute_code(params: SessionTools) -> SessionTools:
    return params


def probe_group() -> ToolGroup:
    """A project's group, built over the session it is handed."""
    return ToolGroup(name="probe", tools=lambda needs: [execute_code])


def launched_back(server: HostedServer) -> HostedServer:
    """The server a serve command's arguments name, validated back."""
    command = server.launched(ServeLaunch(program=["serve"], session="s"))
    arguments = command["args"] if "args" in command else []
    assert arguments[:2] == ["--session", "s"]
    spec = arguments[2:]
    fields = json.loads(spec[1]) if len(spec) > 1 else {}
    return ServedServer.model_validate({"server": spec[0], "fields": fields}).built()


def test_coordination_hosts_the_roster_verbs_and_serves_as_itself(
    tmp_path: Path,
) -> None:
    hosted = Coordination().hosted(needs(tmp_path))

    assert isinstance(hosted, LupMcpServerConfig)
    assert hosted.name == "coordination"
    assert "coordination_send" in hosted.tool_names
    assert hosted.companions
    assert launched_back(Coordination()) == Coordination()
    assert Coordination().hosted(needs(tmp_path, member="")) is None


def test_a_ledger_crosses_to_its_subprocess_with_its_kinds(tmp_path: Path) -> None:
    ledger = Ledger(nodes=[LedgerNode], edges=[LedgerEdge], name="records")
    hosted = ledger.hosted(needs(tmp_path))

    assert isinstance(hosted, LupMcpServerConfig)
    assert hosted.name == "records"
    assert hosted.tool_names
    assert launched_back(ledger) == ledger


def test_codeintel_is_hosted_where_a_language_server_is_installed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import lup.devtools.dev.pyright_oracle as oracle

    monkeypatch.setattr(oracle, "langserver_path", lambda: tmp_path / "langserver")
    hosted = CodeIntel().hosted(needs(tmp_path))
    assert isinstance(hosted, LupMcpServerConfig)
    assert hosted.tool_names
    assert launched_back(CodeIntel()) == CodeIntel()

    monkeypatch.setattr(oracle, "langserver_path", lambda: None)
    assert CodeIntel().hosted(needs(tmp_path)) is None
    with pytest.raises(ValueError, match="a language server"):
        hosted_servers([CodeIntel()], needs(tmp_path))


def test_sandbox_serves_a_given_container_or_starts_one_of_its_own(
    tmp_path: Path,
) -> None:
    given = needs(tmp_path).model_copy(
        update={"sandbox": FakeContainer([execute_code])}
    )
    hosted = Sandbox().hosted(given)
    assert isinstance(hosted, LupMcpServerConfig)
    assert hosted.tool_names == ["execute_code"]
    assert hosted.companions == []

    started = Sandbox().hosted(needs(tmp_path))
    assert isinstance(started, LupMcpServerConfig)
    assert started.tool_names
    assert [type(companion).__name__ for companion in started.companions] == [
        "ContainerLifetime"
    ]
    assert launched_back(Sandbox()) == Sandbox()


@pytest.fixture
def probe_echo(monkeypatch: pytest.MonkeyPatch) -> LupMcpTool:
    """A module-level tool, importable here and in a served subprocess."""
    monkeypatch.syspath_prepend(str(FIXTURES))
    return imported_tool("native_mcp_probe:echo")


def test_a_toolset_serves_module_level_tools_by_their_import_path(
    tmp_path: Path, probe_echo: LupMcpTool
) -> None:
    toolset = Toolset([probe_echo], name="probe")
    hosted = toolset.hosted(needs(tmp_path))

    assert isinstance(hosted, LupMcpServerConfig)
    assert hosted.tool_names == ["echo"]
    assert toolset.tool_names() == ["echo"]
    assert launched_back(toolset) == toolset


def test_a_toolset_of_a_closure_is_hosted_but_cannot_be_served(
    tmp_path: Path,
) -> None:
    @lup_tool("Answer from this process alone.")
    async def local(params: SessionTools) -> SessionTools:
        return params

    toolset = Toolset([local])

    assert toolset.hosted(needs(tmp_path)) is not None
    with pytest.raises(ValueError, match="not a module-level @lup_tool"):
        toolset.launched(ServeLaunch(program=["serve"]))


def test_a_group_takes_its_builders_name_and_serves_by_its_path(
    tmp_path: Path,
) -> None:
    group = Group(builder=probe_group)
    hosted = group.hosted(needs(tmp_path))

    assert group.name == "probe"
    assert isinstance(hosted, LupMcpServerConfig)
    assert hosted.tool_names == ["execute_code"]
    assert launched_back(group) == group


def test_an_always_loaded_server_crosses_to_its_subprocess_as_declared(
    probe_echo: LupMcpTool,
) -> None:
    """The servers built by their own constructors take the flag too."""
    toolset = Toolset([probe_echo], name="probe", always_load=True)
    group = Group(probe_group, always_load=True)

    assert toolset.always_load and group.always_load
    assert launched_back(toolset) == toolset
    assert launched_back(group) == group


def test_an_external_server_is_its_transport_both_ways(tmp_path: Path) -> None:
    transport = RawStdioServerConfig(command="probe", args=["--stdio"])
    external = External(name="probe", server=transport)

    assert external.hosted(needs(tmp_path)) == transport
    assert external.launched(ServeLaunch()) == transport


def test_the_needs_hook_is_named_on_the_command_line() -> None:
    command = ServeLaunch(program=["serve"], needs=grant_nothing).command(
        Coordination()
    )

    assert command["command"] == "serve"
    assert "args" in command
    assert command["args"][:2] == ["--needs", f"{__name__}.grant_nothing"]


def grant_nothing(needs: SessionNeeds, runtime: str | None) -> SessionNeeds:
    """A needs hook that adds nothing, for the command it is named on."""
    return needs


def test_serve_answers_an_mcp_client_over_stdio(
    tmp_path: Path, probe_echo: LupMcpTool
) -> None:
    """The whole path: a declaration, its command, a process, a handshake."""
    launch = ServeLaunch(
        environment={
            "PYTHONPATH": str(FIXTURES),
            SESSION_DIR_ENV: str(tmp_path / "session"),
        }
    )
    command = Toolset([probe_echo], name="probe").launched(launch)
    assert "command" in command
    parameters = StdioServerParameters(
        command=command["command"],
        args=command["args"] if "args" in command else [],
        env=command["env"] if "env" in command else None,
    )

    async def handshake() -> tuple[list[str], str]:
        async with stdio_client(parameters) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                listed = await session.list_tools()
                called = await session.call_tool("echo", {"value": "ping"})
        content = called.content[0]
        text = content.text if content.type == "text" else ""
        return [tool.name for tool in listed.tools], text

    names, answer = asyncio.run(asyncio.wait_for(handshake(), timeout=60))

    assert names == ["echo"]
    assert json.loads(answer) == {"value": "ping"}
