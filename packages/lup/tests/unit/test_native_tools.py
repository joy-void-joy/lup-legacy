"""Explicit grants bound tool inventories and execution on both adapters."""

import asyncio
from pathlib import Path

import pytest
from pydantic import BaseModel, ValidationError

from lup.mcp import External, Toolset, hosted_servers, opened_needs
from lup.providers.claude import Claude, ClaudeTools
from lup.providers.claude.runtime import ClaudeSessionOpener, build_claude_options
from lup.providers.codex.app_server import CodexAppServer, RpcMessage
from lup.providers.codex import Codex, CodexTools
from lup.providers.codex.runtime import (
    CodexConversationState,
    CodexSessionOpener,
    CodexTurnChannel,
    codex_mcp_server,
    codex_serving,
)
from lup.providers.codex.builtins import CodexBuiltins
from lup.tools.mcp import LupMcpServerConfig, create_mcp_server, lup_tool
from lup.types import JsonObject


class Value(BaseModel):
    value: str


@pytest.mark.parametrize(
    "value", ["Bash(*)", "*", "", "mcp__ambient__tool", "UnknownTool"]
)
def test_invalid_and_inherited_native_grants_are_rejected(value: str) -> None:
    with pytest.raises(ValidationError):
        Claude.model_validate({"tools": {"builtin": [value]}})
    with pytest.raises(ValidationError):
        Codex.model_validate({"cwd": ".", "tools": {"builtin": [value]}})


def test_a_scalar_does_not_become_a_sequence_of_tool_letters() -> None:
    with pytest.raises(ValidationError, match="valid list"):
        ClaudeTools.model_validate({"builtin": "Read"})
    with pytest.raises(ValidationError, match="valid list"):
        CodexTools.model_validate({"builtin": "Bash"})


def test_presets_expand_and_exact_grants_stay_exact() -> None:
    assert ClaudeTools(builtin="none").roster() == []
    assert ClaudeTools(builtin="stock").roster() is None
    assert ClaudeTools().roster() == ["WebFetch", "WebSearch"]
    assert ClaudeTools(builtin=["Read", "Glob", "Write"]).roster() == [
        "Read",
        "Glob",
        "Write",
    ]
    assert ClaudeTools(builtin=["Read", "Read"]).roster() == ["Read"]
    assert CodexBuiltins.compile(["Bash"]).shell
    assert not CodexBuiltins.compile(["Bash"]).write
    assert CodexBuiltins.compile(["apply_patch"]).write
    assert CodexBuiltins.compile("web") == CodexBuiltins(web=True)
    assert CodexBuiltins.compile("none") == CodexBuiltins()
    assert CodexBuiltins.compile("stock").all_tools
    for grant in ("Read", "Write", "WebFetch", "Glob"):
        with pytest.raises(ValidationError, match="apply_patch"):
            CodexTools.model_validate({"builtin": [grant]})


@pytest.mark.parametrize(
    "overrides",
    [
        {"allowed_tools": ["Bash"]},
        {"extra_args": {"tools": "Bash"}},
        {"extra_args": {"settings": '{"permissions":{"allow":["Bash"]}}'}},
        {"setting_sources": ["user"]},
        {"plugin_dirs": ["plugin"]},
    ],
)
def test_claude_other_fields_cannot_inject_tools(overrides: JsonObject) -> None:
    with pytest.raises(ValidationError):
        Claude.model_validate(overrides)


def test_unsafe_model_copies_are_revalidated_at_each_provider_boundary() -> None:
    copied = Claude().model_copy(update={"allowed_tools": ["Write"]})
    with pytest.raises(ValidationError, match="outside this session's tools"):
        ClaudeSessionOpener(copied)
    with pytest.raises(ValidationError, match="outside this session's tools"):
        build_claude_options(
            copied, servers={}, binding=lambda: None, resume=None, session_id=None
        )
    copied_codex = Codex(cwd=Path(".")).model_copy(
        update={"tools": {"builtin": ["Read"]}}
    )
    with pytest.raises(ValidationError, match="apply_patch"):
        CodexSessionOpener(copied_codex)


@pytest.mark.parametrize(
    "key",
    [
        "features",
        "features.shell_tool",
        "tools",
        "mcp_servers",
        "web_search",
        "sandbox_mode",
    ],
)
def test_provider_config_cannot_override_tool_authority(key: str) -> None:
    with pytest.raises(ValueError, match="explicit session authority"):
        Codex(cwd=Path("."), provider_config={key: True})


async def test_claude_guard_denies_fabricated_tools_but_keeps_explicit_effectful_tools(
    tmp_path: Path,
) -> None:
    @lup_tool("Write the supplied value.")
    async def record(params: Value) -> Value:
        (tmp_path / "marker").write_text(params.value)
        return params

    options = build_claude_options(
        Claude(tools=ClaudeTools(builtin="none", mcp=[Toolset([record], name="app")])),
        servers={"app": create_mcp_server("app", tools=[record])},
        binding=lambda: None,
        resume="old-session",
        session_id=None,
    )
    assert options.tools == []
    assert options.strict_mcp_config
    assert options.setting_sources == []
    assert options.hooks is not None
    guard = options.hooks["PreToolUse"][0].hooks[0]
    for name in ("Bash", "Write", "Agent", "mcp__ambient__record"):
        result = await guard(
            {
                "hook_event_name": "PreToolUse",
                "tool_name": name,
                "tool_input": {},
                "tool_use_id": "call",
                "session_id": "old-session",
                "transcript_path": "",
                "cwd": str(tmp_path),
            },
            None,
            {"signal": None},
        )
        match result:
            case {"hookSpecificOutput": {"permissionDecision": "deny"}}:
                pass
            case _:
                pytest.fail(f"fabricated tool was not denied: {result}")
    allowed = await guard(
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "mcp__app__record",
            "tool_input": {"value": "authorized"},
            "tool_use_id": "call",
            "session_id": "old-session",
            "transcript_path": "",
            "cwd": str(tmp_path),
        },
        None,
        {"signal": None},
    )
    assert allowed == {}
    await record.handler({"value": "authorized"})
    assert (tmp_path / "marker").read_text() == "authorized"


async def test_codex_validates_explicit_app_calls_and_rejects_foreign_or_stale_turns(
    tmp_path: Path,
) -> None:
    effects: list[str] = []

    @lup_tool("Record one value.")
    async def record(params: Value) -> Value:
        effects.append(params.value)
        return params

    granted = CodexTools(builtin="none", mcp=[Toolset([record], name="app")])
    config = Codex(cwd=tmp_path, tools=granted)
    serving = codex_serving(
        hosted_servers(config.tools.mcp, opened_needs(tmp_path, tmp_path, {}))
    )
    state = CodexConversationState(
        config, CodexAppServer(Path("codex")), None, serving=serving
    )
    state.thread_id = "thread"
    state.channel = CodexTurnChannel("thread")
    state.channel.turn_id = "turn"

    async def call(thread: str, turn: str, value: str | int) -> JsonObject:
        result = await state.handle_server_request(
            RpcMessage(
                id=1,
                method="item/tool/call",
                params={
                    "threadId": thread,
                    "turnId": turn,
                    "callId": "call",
                    "tool": "lup_app_app__record",
                    "arguments": {"value": value},
                },
            )
        )
        assert isinstance(result, dict)
        return result

    assert (await call("other", "turn", "foreign"))["success"] is False
    assert (await call("thread", "old", "stale"))["success"] is False
    assert (await call("thread", "turn", 5))["success"] is False
    assert effects == []
    assert (await call("thread", "turn", "accepted"))["success"] is True
    assert effects == ["accepted"]


def test_app_tools_survive_no_built_ins_on_both_runtimes(
    tmp_path: Path,
) -> None:
    @lup_tool("Echo one value.")
    async def echo(params: Value) -> Value:
        return params

    needs = opened_needs(tmp_path, tmp_path, {})
    server = Toolset([echo], name="app")
    assert Claude(tools=ClaudeTools(builtin="none", mcp=[server])).tools.mcp == [server]
    granted = CodexTools(builtin="none", mcp=[server])
    served = codex_serving(hosted_servers(Codex(tools=granted).tools.mcp, needs))
    assert served.applications["lup_app_app__echo"] is echo
    codex = Codex(model="gpt-6-astra", tools=CodexTools(mcp=[Toolset([echo])]))
    applications = codex_serving(hosted_servers(codex.tools.mcp, needs)).applications
    assert applications["lup_app_tools__echo"] is echo
    claude = Claude(model="claude-opus-5", tools=ClaudeTools(mcp=[Toolset([echo])]))
    hosted = hosted_servers(claude.tools.mcp, needs)["tools"]
    assert isinstance(hosted, LupMcpServerConfig)
    assert hosted.tool_names == ["echo"]


def test_dynamic_tool_names_cannot_shadow_another_explicit_handler(
    tmp_path: Path,
) -> None:
    @lup_tool("First handler.", name="c")
    async def first(params: Value) -> Value:
        return params

    @lup_tool("Second handler.", name="b__c")
    async def second(params: Value) -> Value:
        return params

    needs = opened_needs(tmp_path, tmp_path, {})
    shadowing = CodexTools(
        mcp=[Toolset([first], name="a__b"), Toolset([second], name="a")]
    )
    with pytest.raises(ValueError, match="collide"):
        codex_serving(hosted_servers(shadowing.mcp, needs))
    with pytest.raises(ValueError, match="uniquely"):
        CodexTools(mcp=[Toolset([first], name="a"), Toolset([second], name="a")])
    with pytest.raises(ValueError, match="names a tool twice"):
        Toolset([first, first])


def test_codex_starts_an_external_tool_group_as_its_own_subprocess(
    tmp_path: Path,
) -> None:
    group = External(name="group", server={"command": "uv", "args": ["run", "tools"]})
    config = Codex(cwd=tmp_path, tools=CodexTools(builtin="stock", mcp=[group]))

    needs = opened_needs(tmp_path, tmp_path, {})
    served = codex_serving(hosted_servers(config.tools.mcp, needs)).servers
    assert served["group"].command == "uv"
    assert served["group"].args == ["run", "tools"]


def test_codex_rejects_a_tool_group_it_cannot_launch() -> None:
    with pytest.raises(ValueError, match="subprocess"):
        codex_mcp_server("group", {"type": "sse", "url": "https://example.test"})


def test_codex_will_not_relaunch_a_hosted_tool_group_as_a_subprocess() -> None:
    """A hosted group is answered in this process, never started as a subprocess.

    A hosted server reads the process hosting it — the context variables
    scoping the session it answers inside, its clients, its caches. Relaunched
    as a subprocess it would not fail: it would answer every call from
    defaults, confidently, and nothing downstream could tell the difference.
    So its tools become the thread's dynamic tools, answered here.
    """

    @lup_tool("Echo one value.")
    async def echo(params: Value) -> Value:
        return params

    served = codex_serving({"group": create_mcp_server("group", tools=[echo])})

    assert served.servers == {}
    assert served.applications == {"lup_app_group__echo": echo}


async def test_unknown_inherited_model_is_refused_before_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = CodexAppServer(Path("codex"))

    async def request(method: str, params: JsonObject) -> JsonObject:
        assert method == "config/read"
        return {"config": {"model": "custom-unknown"}}

    monkeypatch.setattr(server, "request", request)
    state = CodexConversationState(
        Codex(cwd=tmp_path), server, None, models={"known": {}}
    )
    with pytest.raises(ValueError, match="inherited unknown model"):
        await state.ensure_thread()


@pytest.mark.parametrize("error", [RuntimeError, asyncio.CancelledError])
async def test_failed_or_cancelled_startup_closes_server_and_catalog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: type[BaseException]
) -> None:
    closed: list[bool] = []

    async def start(server: CodexAppServer) -> None:
        raise error("startup stopped")

    async def close(server: CodexAppServer) -> None:
        closed.append(True)

    monkeypatch.setattr(CodexAppServer, "start", start)
    monkeypatch.setattr(CodexAppServer, "close", close)
    monkeypatch.setattr(
        CodexBuiltins,
        "model_catalog",
        lambda self, executable, environment, model: {"models": [{"slug": "known"}]},
    )
    opener = CodexSessionOpener(
        Codex(cwd=tmp_path, environment={"CODEX_HOME": str(tmp_path)})
    )
    with pytest.raises(error, match="startup stopped"):
        async with opener.open_session():
            pytest.fail("failed startup yielded a session")
    assert closed == [True]
    assert list(tmp_path.glob("lup-native-*")) == []
