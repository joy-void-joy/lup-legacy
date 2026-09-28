"""One declaration, two compilations: what each launch field means to both outputs.

A field of :class:`~lup.providers.claude.Claude` or
:class:`~lup.providers.codex.Codex` is read by the SDK options a session
opened in process starts with and by the command a launched CLI runs. Each
test here holds one field to one meaning across both, and the defaults a
launch fills in (decision 22 of the DX overhaul) to what a launch assumes and
what a session opened here does not.
"""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

import lup.launch.declaration as declaration
import lup.providers.claude.runtime as claude_runtime
from lup.coordination.identity import MEMBER_ENV, NAME_ENV, LaunchedMember
from lup.harness.environment import tool_server_env
from lup.harness.image import ContainerClient
from lup.harness.models import HookSandbox, HookSet
from lup.launch.compilation import allowance_environment
from lup.launch.declaration import (
    InnerSandbox,
    LaunchCommand,
    Latest,
    Member,
    Mount,
    OuterContainer,
    Pick,
    Recording,
    Reopen,
    NoSandbox,
    resumption,
)
from lup.launch.foreground import between_steps, run_in_foreground
from lup.launch.refusal import LaunchRefused
from lup.policy.hooks import LupHooksConfig
from lup.policy.enforcement import SandboxPosture
from lup.providers.claude import Claude, ClaudeTools
from lup.providers.claude.launch import (
    claude_account_environment,
    claude_arguments,
    claude_launched,
    claude_settings,
    compiled_claude,
)
from lup.providers.claude.login import CLAUDE_CONFIG_DIR
from lup.providers.claude.runtime import (
    ClaudeSessionOpener,
    build_claude_options,
    resumed_session,
)
from lup.providers.codex import Codex, CodexTools
from lup.providers.codex.launch import (
    codex_account_environment,
    codex_arguments,
    codex_envelope,
    codex_launched,
    codex_sandbox_mode,
)
from lup.providers.codex.login import CODEX_HOME
from lup.sessions.events import SessionId, SessionSummary
from lup.sessions.recursion import MAX_RECURSIVE_AGENT_ENV
from lup.tools.mcp import RawStdioServerConfig

MEMBER = LaunchedMember(member_id="member-1", cli_name="reviewer")


def policy() -> HookSet:
    """A policy declaring an OS sandbox, the way a harness plugin declares one."""
    return HookSet(
        id="hooks.probe",
        policy_ids=[],
        sandbox=HookSandbox(excluded_commands=["git *"], writable_paths=["/tmp"]),
    )


def engine(monkeypatch: pytest.MonkeyPatch, present: bool) -> None:
    """Answer the container probe as a host with an engine, or with none."""
    client = (
        ContainerClient(binary="podman", client="podman", server="podman")
        if present
        else None
    )
    monkeypatch.setattr(declaration, "detected_client", lambda: client)


def test_a_session_opened_here_is_unconfined_and_reaches_only_the_web() -> None:
    """Decision 22: no sandbox by default, and no file or shell tool without one."""
    assert Claude().sandbox == NoSandbox()
    assert Claude().tools.roster() == ["WebFetch", "WebSearch"]
    assert Codex().sandbox == NoSandbox()
    assert Codex().tools.builtin == "web"


@pytest.mark.parametrize("launched", [claude_launched, codex_launched])
def test_a_launch_opens_in_the_container_where_an_engine_answers(
    launched: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine(monkeypatch, present=True)
    agent = (
        claude_launched(Claude())
        if launched is claude_launched
        else codex_launched(Codex())
    )

    assert agent.sandbox == OuterContainer()
    assert agent.tools.builtin == "stock"
    assert agent.identity == Member()
    assert agent.record == Recording(transcript=True)


def test_a_launch_without_an_engine_says_so_and_opens_inner(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    engine(monkeypatch, present=False)

    assert claude_launched(Claude()).sandbox == InnerSandbox()
    said = capsys.readouterr()
    assert "No working Docker or Podman" in said.out + said.err
    assert "sandbox=InnerSandbox()" in said.out + said.err


def test_what_a_declaration_says_a_launch_keeps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only a field left unset takes the launch's default, even one set to its default."""
    engine(monkeypatch, present=True)
    agent = claude_launched(
        Claude(
            sandbox=NoSandbox(),
            tools=ClaudeTools(builtin="web"),
            permission_mode="plan",
        )
    )

    assert agent.sandbox == NoSandbox()
    assert agent.tools.builtin == "web"
    assert agent.permission_mode == "plan"


def test_a_launch_leaves_the_permission_mode_to_the_person() -> None:
    """``bypassPermissions`` is a program's default; a person's CLI keeps its own."""
    assert claude_launched(Claude(sandbox=NoSandbox())).permission_mode is None


def test_the_inner_sandbox_is_one_settings_document_in_both_outputs(
    tmp_path: Path,
) -> None:
    """What the SDK is handed as ``settings`` is what ``--settings`` carries."""
    agent = Claude(
        model="opus",
        effort="ultra",
        policy=policy(),
        sandbox=InnerSandbox(mounts=[Mount(path=tmp_path / "notes", writable=True)]),
        tools=ClaudeTools(builtin="stock"),
    )
    opened = build_claude_options(
        agent, servers={}, binding=lambda: None, resume=None, session_id="s"
    )
    arguments = claude_arguments(agent, MEMBER, None, [])
    launched = json.loads(arguments[arguments.index("--settings") + 1])

    assert opened.settings is not None
    assert json.loads(opened.settings) == launched == claude_settings(agent)
    assert launched["ultracode"] is True
    assert launched["sandbox"]["enabled"] is True
    assert launched["sandbox"]["allowUnsandboxedCommands"] is False
    assert launched["sandbox"]["excludedCommands"] == ["git *"]
    assert launched["sandbox"]["filesystem"]["allowWrite"] == [
        "/tmp",
        str(tmp_path / "notes"),
    ]


@pytest.mark.parametrize("sandbox", [OuterContainer(), NoSandbox()])
def test_the_container_and_no_sandbox_stand_claude_down(
    sandbox: OuterContainer | NoSandbox,
) -> None:
    assert claude_settings(Claude(sandbox=sandbox))["sandbox"] == {"enabled": False}


def test_an_escapable_sandbox_says_nothing_the_cli_does_not_already() -> None:
    settings = claude_settings(Claude(sandbox=InnerSandbox(escapable=True)))
    sandbox = settings["sandbox"]

    assert isinstance(sandbox, dict)
    assert "allowUnsandboxedCommands" not in sandbox
    assert InnerSandbox(escapable=True).enforcement() == SandboxPosture(
        active=True, escapable=True
    )


def test_codex_refuses_a_way_out_of_its_envelope() -> None:
    with pytest.raises(ValueError, match="no way out for one"):
        Codex(sandbox=InnerSandbox(escapable=True))
    with pytest.raises(ValueError, match="no way out for one"):
        Codex(sandbox=InnerSandbox(excluded_commands=["git *"]))


@pytest.mark.parametrize(
    ("sandbox", "declared", "expected"),
    [
        (InnerSandbox(), None, "workspace-write"),
        (InnerSandbox(), "read-only", "read-only"),
        (OuterContainer(), None, "danger-full-access"),
        (NoSandbox(), None, None),
        (NoSandbox(), "read-only", "read-only"),
    ],
)
def test_codex_reconciles_its_wall_and_its_mode_one_way_for_both_outputs(
    sandbox: InnerSandbox | OuterContainer | NoSandbox,
    declared: str | None,
    expected: str | None,
) -> None:
    agent = Codex.model_validate({"sandbox": sandbox, "sandbox_mode": declared})

    assert codex_sandbox_mode(agent.sandbox, agent.sandbox_mode) == expected


def test_codex_launches_its_inner_envelope_without_a_declared_policy(
    tmp_path: Path,
) -> None:
    """A declared wall is the wall, whether or not a policy also declares one."""
    envelope = codex_envelope(None, {}, [], mode="read-only", tree=tmp_path)

    assert envelope[:2] == ["--sandbox", "read-only"]


def test_a_plugin_is_the_first_directory_both_outputs_load(tmp_path: Path) -> None:
    plugin = tmp_path / "plugin"
    agent = Claude(plugin=plugin, tools=ClaudeTools(builtin="stock"))
    compiled = compiled_claude(agent)
    opened = build_claude_options(
        compiled, servers={}, binding=lambda: None, resume=None, session_id="s"
    )
    arguments = claude_arguments(compiled, MEMBER, None, [])

    assert compiled.plugin_dirs == [plugin]
    assert [entry["path"] for entry in opened.plugins] == [str(plugin)]
    assert arguments[arguments.index("--plugin-dir") + 1] == str(plugin)


def test_a_session_opened_here_with_a_plugin_takes_the_stock_tools(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="builtin='stock'"):
        ClaudeSessionOpener(Claude(plugin=tmp_path)).compiled()


def test_a_policy_judges_a_session_opened_here_and_a_launched_one() -> None:
    """In process it becomes hooks; launched, the plugin's dispatcher judges it."""
    agent = Claude(policy=policy())
    compiled = ClaudeSessionOpener(agent).compiled()

    assert compiled.hooks is not None
    assert [matcher.tag for matcher in compiled.hooks.pre_tool_use] == [
        "semantic_policy"
    ]
    assert "--settings" in claude_arguments(agent, MEMBER, None, [])


def test_a_launch_refuses_callbacks_it_cannot_carry() -> None:
    with pytest.raises(LaunchRefused, match="hooks are callbacks"):
        claude_arguments(Claude(hooks=LupHooksConfig()), MEMBER, None, [])
    with pytest.raises(LaunchRefused, match="hooks are callbacks"):
        codex_arguments(Codex(hooks=LupHooksConfig()), [], [])


def test_a_different_policy_beside_a_plugin_harness_is_refused() -> None:
    from lup_template.harness.catalog import portable_harness

    harness = portable_harness()
    with pytest.raises(ValueError, match="plugin already enforces"):
        Claude(plugin=harness, policy=policy())
    assert Claude(plugin=harness).enforced_policy() == harness.declared_hooks


def test_an_identity_joins_the_roster_in_both_outputs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        claude_runtime, "launched_member", lambda _root, _name=None: MEMBER
    )
    agent = Claude(cwd=tmp_path, identity=Member(name="reviewer", inboxes=None))
    compiled = ClaudeSessionOpener(agent).compiled()
    arguments = claude_arguments(agent, MEMBER, None, [])

    assert compiled.environment[MEMBER_ENV] == "member-1"
    assert compiled.environment[NAME_ENV] == "reviewer"
    assert arguments[arguments.index("--name") + 1] == "reviewer"


@pytest.mark.parametrize(
    ("resume", "claude", "codex"),
    [
        (Latest(), ["--continue"], ["resume", "--last"]),
        (Pick(), ["--resume"], ["resume"]),
        (
            Reopen(session=SessionId(value="abc")),
            ["--resume", "abc"],
            ["resume", "abc"],
        ),
        (None, [], []),
    ],
)
def test_a_reopening_leads_each_launch_in_its_runtimes_words(
    resume: Latest | Pick | Reopen | None, claude: list[str], codex: list[str]
) -> None:
    claude_words = claude_arguments(Claude(resume=resume), MEMBER, None, [])
    codex_words = codex_arguments(Codex(resume=resume), [], [])

    assert claude_words[: len(claude)] == claude
    assert codex_words[: len(codex)] == codex
    assert resumption(Claude(resume=resume).resume).wanted() is (resume is not None)


async def test_latest_opens_the_newest_session_here_and_a_picker_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    newest = SessionSummary(id=SessionId(value="newest"), updated_at=datetime.now(UTC))

    async def sessions(_self: Claude) -> list[SessionSummary]:
        return [newest]

    monkeypatch.setattr(Claude, "sessions", sessions)
    agent = Claude(cwd=tmp_path)

    assert await resumed_session(agent, Latest()) == SessionId(value="newest")
    with pytest.raises(ValueError, match="only a launched terminal"):
        await resumed_session(agent, Pick())


@pytest.mark.parametrize(
    ("declared", "inherited", "expected"),
    [
        (None, {}, "-1"),
        (2, {}, "2"),
        (None, {MAX_RECURSIVE_AGENT_ENV: "3"}, "2"),
        (5, {MAX_RECURSIVE_AGENT_ENV: "3"}, "2"),
        (1, {MAX_RECURSIVE_AGENT_ENV: "3"}, "1"),
        (-1, {MAX_RECURSIVE_AGENT_ENV: "3"}, "2"),
    ],
)
def test_a_declared_allowance_narrows_and_never_widens(
    declared: int | None, inherited: dict[str, str], expected: str
) -> None:
    assert allowance_environment(declared, inherited) == {
        MAX_RECURSIVE_AGENT_ENV: expected
    }


def test_a_home_named_outright_is_the_accounts_in_both_runtimes(tmp_path: Path) -> None:
    assert claude_account_environment(Claude(home=tmp_path)) == {
        CLAUDE_CONFIG_DIR: str(tmp_path)
    }
    assert codex_account_environment(Codex(home=tmp_path)) == {
        CODEX_HOME: str(tmp_path)
    }


def test_declared_servers_are_the_launched_sessions_whole_roster() -> None:
    from lup.mcp import Coordination

    claude_words = claude_arguments(
        Claude(tools=ClaudeTools(mcp=[Coordination()])), MEMBER, None, []
    )
    codex_words = codex_arguments(Codex(tools=CodexTools(mcp=[Coordination()])), [], [])
    servers = json.loads(claude_words[claude_words.index("--mcp-config") + 1])

    assert "--strict-mcp-config" in claude_words
    assert list(servers["mcpServers"]) == ["coordination"]
    assert any(
        word.startswith("mcp_servers.coordination.command=") for word in codex_words
    )


def test_a_launched_codex_hands_the_servers_lup_hosts_what_its_launcher_exported() -> (
    None
):
    """Codex starts a stdio server under a fixed base environment, forwarding nothing else.

    So a server lup hosts names the roster identity and recursion allowance in
    ``env_vars``, or its coordination server joins the roster under no id and
    a nested agent its tools open spends no allowance. A server passed through
    as declared is handed none of it.
    """
    from lup.mcp import Coordination, External

    words = codex_arguments(
        Codex(
            tools=CodexTools(
                mcp=[
                    Coordination(),
                    External(name="other", server=RawStdioServerConfig(command="x")),
                ]
            )
        ),
        [],
        [],
    )

    assert set(tool_server_env()) == {MEMBER_ENV, NAME_ENV, MAX_RECURSIVE_AGENT_ENV}
    assert f"mcp_servers.coordination.env_vars={json.dumps(tool_server_env())}" in words
    assert not any(word.startswith("mcp_servers.other.env_vars=") for word in words)


def test_an_always_loaded_server_skips_tool_search_in_both_claude_outputs() -> None:
    """A server a session calls on most turns should not cost a search each time.

    Claude Code defers MCP tools behind its tool search and exempts a server
    whose config says ``alwaysLoad``, whatever its transport. A session opened
    here says it in the SDK's options, which hand Claude Code each server but
    its instance, and a launched one in ``--mcp-config``. Codex names no such
    control, so its launch carries nothing for it.
    """
    from lup.mcp import Coordination, External
    from lup.tools.mcp import (
        RawHttpServerConfig,
        RawStdioServerConfig,
        create_mcp_server,
    )

    remote = RawHttpServerConfig(type="http", url="https://tools.example/mcp")
    quiet = RawStdioServerConfig(command="quiet")
    declared = [
        Coordination(always_load=True),
        External(name="remote", server=remote, always_load=True),
        External(name="quiet", server=quiet),
    ]
    agent = Claude(tools=ClaudeTools(mcp=declared))
    opened = build_claude_options(
        agent,
        servers={
            "coordination": create_mcp_server("coordination"),
            "remote": remote,
            "quiet": quiet,
        },
        binding=lambda: None,
        resume=None,
        session_id="s",
    )
    assert isinstance(opened.mcp_servers, dict)
    handed = json.loads(
        json.dumps(
            {
                name: {key: value for key, value in server.items() if key != "instance"}
                for name, server in opened.mcp_servers.items()
            }
        )
    )
    words = claude_arguments(agent, MEMBER, None, [])
    launched = json.loads(words[words.index("--mcp-config") + 1])["mcpServers"]
    codex_words = codex_arguments(Codex(tools=CodexTools(mcp=declared)), [], [])

    assert handed["coordination"]["type"] == "sdk"
    for servers in (handed, launched):
        assert servers["coordination"]["alwaysLoad"] is True
        assert servers["remote"]["alwaysLoad"] is True
        assert "alwaysLoad" not in servers["quiet"]
    assert [word for word in codex_words if "alwaysLoad" in word] == []


class Step:
    """A lifecycle step that writes down when it ran."""

    def __init__(self, name: str, seen: list[str]) -> None:
        self.name = name
        self.seen = seen

    def before(self) -> None:
        self.seen.append(f"{self.name}:before")

    def after(self, succeeded: bool) -> None:
        self.seen.append(f"{self.name}:after:{succeeded}")


def test_steps_wrap_the_session_in_the_order_given() -> None:
    seen: list[str] = []

    def session() -> int:
        seen.append("session")
        return 0

    status = between_steps([Step("checkpoint", seen), Step("base", seen)], session)

    assert status == 0
    assert seen == [
        "checkpoint:before",
        "base:before",
        "session",
        "base:after:True",
        "checkpoint:after:True",
    ]


def test_after_steps_run_when_the_session_fails() -> None:
    seen: list[str] = []

    def session() -> int:
        raise LaunchRefused("the boundary could not be verified")

    with pytest.raises(LaunchRefused):
        between_steps([Step("checkpoint", seen)], session)

    assert seen == ["checkpoint:before", "checkpoint:after:False"]


def test_a_failing_exit_status_is_an_unsuccessful_session() -> None:
    seen: list[str] = []

    assert between_steps([Step("checkpoint", seen)], lambda: 3) == 3
    assert seen == ["checkpoint:before", "checkpoint:after:False"]


def test_the_cli_runs_in_the_foreground_and_its_status_comes_back(
    tmp_path: Path,
) -> None:
    program = tmp_path / "stub-cli"
    program.write_text(
        '#!/bin/sh\necho "$@" > "$PWD/argv"\necho "$LAUNCHED" > "$PWD/env"\nexit 3\n'
    )
    program.chmod(0o755)
    command = LaunchCommand(
        argv=[str(program), "--flag", "word"],
        env={"LAUNCHED": "yes", "PATH": "/usr/bin:/bin"},
        cwd=tmp_path,
    )

    assert run_in_foreground(command) == 3
    assert (tmp_path / "argv").read_text().strip() == "--flag word"
    assert (tmp_path / "env").read_text().strip() == "yes"
    assert str(command) == f"{program} --flag word"


def test_a_missing_cli_is_a_refusal_naming_it(tmp_path: Path) -> None:
    command = LaunchCommand(argv=["definitely-not-a-cli"], env={}, cwd=tmp_path)

    with pytest.raises(LaunchRefused, match="definitely-not-a-cli"):
        run_in_foreground(command)


def test_a_plugin_built_elsewhere_names_what_the_harness_would_have() -> None:
    """A built plugin carries no roster and no image; a declaration names them beside it.

    Unnamed, the harness's own are read where the plugin is one, and nothing
    beyond the runtime's own probes and lup's default image where it is not.
    """
    from lup.harness.image import Image
    from lup.harness.requirements import Manifest
    from lup.launch.declaration import declared_image, declared_requirements
    from lup_template.harness.catalog import portable_harness

    harness = portable_harness()
    image = Image().model_copy(update={"config_home": "/elsewhere"})
    built = Path("/plugins/built")

    assert declared_requirements(harness, None) == harness.requirements
    assert declared_requirements(built, None) == Manifest()
    assert declared_requirements(harness, Manifest()) == Manifest()
    assert declared_image(harness, OuterContainer()) == harness.image
    assert declared_image(built, OuterContainer()) == Image()
    assert declared_image(harness, OuterContainer(image=image)) == image
    assert declared_image(harness, InnerSandbox()) == harness.image


@pytest.mark.parametrize("wall", [OuterContainer(), InnerSandbox(), NoSandbox()])
def test_every_wall_reaches_its_mounts_and_widens_to_more(
    wall: OuterContainer | InnerSandbox | NoSandbox,
) -> None:
    """The policy reads a mount as the session's own whichever wall it opens behind."""
    declared = Mount(path=Path("/work/other"), writable=True)
    added = Mount(path=Path("/work/answers"))
    mounted = wall.model_copy(update={"mounts": [declared]})

    assert mounted.roots() == [declared.root()]
    assert mounted.widened([added]).roots() == [declared.root(), added.root()]
