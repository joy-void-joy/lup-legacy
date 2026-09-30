"""A session named for its work, registered under each runtime's prompt event, and run.

One rendering, two plugins. Neither holds a prompt for the ask: the runtime
that takes a name only from the hook's own answer is given it at the next
prompt, and the one that names a thread through its app-server is given it
by the ask's own process. What is asserted per runtime is the registration
and the compiled declaration, and — by running the rendered guard over a
store the typed writers produced, from inside a repository, with a stand-in
CLI on the path the way the runtime's own would be — what the session is
told, what its roster row answers to, and what the runtime was asked.
"""

import json
import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import TypedDict

import pytest
import sh

from lup.coordination.bare.naming import Naming, recalled, request_for
from lup.coordination.bare.store import current_name, member_of, session_actor
from lup.coordination.identity import MEMBER_ENV, mint_member_id
from lup.coordination.repository import RepositoryPeers
from lup.harness.generate import NativeHarnessComposition
from lup.harness.models import Artifact, HookSet, SessionNaming
from lup.providers.claude.harness import CLAUDE_PROMPT_EVENT, CLAUDE_SESSION_NAMING
from lup.providers.claude.model_choice import claude_model_arguments
from lup.providers.codex.builtins import CodexBuiltins
from lup.providers.codex.harness import CODEX_PROMPT_EVENT, CODEX_SESSION_NAMING
from lup.providers.roster_prompt import store_modules
from lup.providers.session_naming import (
    GUARD_SCRIPT,
    RUNTIME_ENTRY,
    NamingSpelling,
    naming_hook,
)
from lup.types import ModelTier
from lup_template.harness.catalog import declared_hook_set
from lup_template.harness.composition import claude_target, codex_target

RUNTIMES = pytest.mark.parametrize(
    ("target", "tree", "event", "host", "arguments"),
    [
        pytest.param(
            claude_target,
            ".claude",
            CLAUDE_PROMPT_EVENT,
            CLAUDE_SESSION_NAMING,
            ["--model", "opus", "--effort", "low", "--tools", ""],
            id="claude",
        ),
        pytest.param(
            codex_target,
            ".codex",
            CODEX_PROMPT_EVENT,
            CODEX_SESSION_NAMING,
            [
                "--model",
                "gpt-5.6-sol",
                "--config",
                'model_reasoning_effort="low"',
                *CodexBuiltins().arguments(),
            ],
            id="codex",
        ),
    ],
)

STAND_IN = """#!/usr/bin/env python3
import json
import sys
import time
from pathlib import Path

record = Path(__file__).with_name("asked.jsonl")
answer = json.loads(Path(__file__).with_name("answer.json").read_text())
kept = Path(__file__).with_name("kept.json")
held = Path(__file__).with_name("held")
arguments = sys.argv[1:]
if arguments[:1] == ["app-server"]:
    for line in sys.stdin:
        message = json.loads(line)
        result = {}
        if message.get("method") == "thread/name/set":
            with record.open("a") as log:
                log.write(json.dumps({"named": message["params"]}) + "\\n")
        if message.get("method") == "thread/read":
            with record.open("a") as log:
                log.write(json.dumps({"read": message["params"]}) + "\\n")
            name = json.loads(kept.read_text()) if kept.exists() else None
            result = {"thread": {"id": message["params"]["threadId"], "name": name}}
        if "id" in message:
            print(json.dumps({"id": message["id"], "result": result}), flush=True)
    sys.exit(0)
prompt = sys.stdin.read()
while held.exists():
    time.sleep(0.05)
with record.open("a") as log:
    log.write(json.dumps({"arguments": arguments, "prompt": prompt}) + "\\n")
if arguments[:1] == ["exec"]:
    Path(arguments[arguments.index("-o") + 1]).write_text(json.dumps(answer))
else:
    print(json.dumps({"is_error": False, "structured_output": answer}))
"""
"""One program standing in for both runtimes' CLIs, answering *answer.json*.

Records every ask with the prompt it was handed, and every thread it was
asked to read or name, beside itself: what the runtime would have been asked
is what these tests assert, since no model is reached from a unit test. A
thread read answers with the name in *kept.json*, or none. An ask waits for
as long as *held* exists, which is how a test holds the model mid-answer."""


class Named(TypedDict):
    threadId: str
    name: str


class Read(TypedDict):
    threadId: str


class Asked(TypedDict, total=False):
    """One line of the stand-in's record: an ask and its prompt, a thread read or named."""

    arguments: list[str]
    prompt: str
    named: Named
    read: Read


class Heard(TypedDict, total=False):
    """What either runtime hands the hook, as far as these tests send it."""

    session_id: str
    cwd: str
    hook_event_name: str
    prompt: str
    session_title: str
    source: str


def rendered(tree: str) -> Path:
    """The plugin root one runtime's tree renders the hook under."""
    return Path(f"{tree}/plugins/lup")


def shipped(
    target: Callable[[Path], NativeHarnessComposition],
) -> dict[Path, Artifact]:
    """Every artifact one runtime's plugin carries, by the path it carries it at."""
    return {
        artifact.path: artifact
        for artifact in target(Path.cwd()).recipe.desired.artifacts
    }


def laid_out(artifacts: dict[Path, Artifact], plugin: Path, root: Path) -> Path:
    """The guard, the host half, its settings and the shipped package, as a plugin lays them out."""
    runtime = Path("hooks") / "runtime" / RUNTIME_ENTRY
    compiled = Path("hooks") / runtime.with_suffix(".json").name
    carried = [
        (
            Path("hooks") / "scripts" / GUARD_SCRIPT,
            artifacts[plugin / "hooks" / "scripts" / GUARD_SCRIPT],
        ),
        (runtime, artifacts[plugin / runtime]),
        (compiled, artifacts[plugin / compiled]),
        *[
            (Path("hooks") / "runtime" / module.path, module)
            for module in store_modules()
        ],
    ]
    for relative, artifact in carried:
        landed = root / relative
        landed.parent.mkdir(parents=True, exist_ok=True)
        landed.write_text(artifact.content)
    return root / "hooks" / "scripts" / GUARD_SCRIPT


class Session:
    """One session on a real roster, prompted through the rendered guard."""

    def __init__(
        self,
        target: Callable[[Path], NativeHarnessComposition],
        tree: str,
        event: str,
        tmp_path: Path,
        answer: str | None,
    ) -> None:
        self.guard = laid_out(shipped(target), rendered(tree), tmp_path / "plugin")
        self.event = event
        self.repository = tmp_path / "repository"
        self.repository.mkdir()
        sh.git("init", "-q", str(self.repository))
        self.peers = RepositoryPeers(self.repository)
        self.member = mint_member_id()
        self.peers.join(self.member, self.repository, cli_name="dev")
        self.bin = tmp_path / "bin"
        self.bin.mkdir()
        (self.bin / "answer.json").write_text(json.dumps({"name": answer}))
        for program in ("claude", "codex"):
            stand_in = self.bin / program
            stand_in.write_text(STAND_IN)
            stand_in.chmod(0o755)

    def prompted(self, prompt: str, title: str | None = None) -> str:
        """What the hook prints for one prompt, with *title* where the chrome reports one."""
        payload = Heard(
            session_id="root-session",
            cwd=str(self.repository),
            hook_event_name=self.event,
            prompt=prompt,
        )
        if title is not None:
            payload["session_title"] = title
        return self.heard(payload)

    def reopened(self) -> str:
        """What the hook prints as Codex starts this session again from its thread."""
        return self.heard(
            Heard(
                session_id="root-session",
                cwd=str(self.repository),
                hook_event_name="SessionStart",
                source="resume",
            )
        )

    def kept(self, name: str | None) -> None:
        """The name the thread already has, as a thread read answers it."""
        (self.bin / "kept.json").write_text(json.dumps(name))

    def hold(self) -> None:
        """Keep every ask from answering until :meth:`release`."""
        (self.bin / "held").touch()

    def release(self) -> None:
        """Let a held ask answer."""
        (self.bin / "held").unlink()

    def asking(self) -> bool:
        """Whether an ask this session started is still under way."""
        titling = recalled(self.peers.root, self.member)
        return titling is not None and bool(titling["asked"])

    def heard(self, payload: Heard) -> str:
        """What the rendered guard prints for one event's payload."""
        return str(
            sh.sh(
                str(self.guard),
                _in=json.dumps(payload),
                _cwd=str(self.repository),
                _env={
                    **os.environ,
                    MEMBER_ENV: self.member,
                    "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}",
                },
            )
        )

    def called(self) -> str:
        """What the roster calls this session now."""
        found = member_of(self.peers.root, session_actor(self.member))
        assert found is not None
        return current_name(found)

    def asked(self) -> list[Asked]:
        """Everything the stand-in CLI was asked, oldest first."""
        record = self.bin / "asked.jsonl"
        return (
            [json.loads(line) for line in record.read_text().splitlines()]
            if record.exists()
            else []
        )

    def concluded(self, count: int) -> list[Asked]:
        """The record once a detached ask has made *count* entries and concluded.

        Waited on rather than slept past, for at most fifteen seconds: the
        work is a process of the hook's own that outlives it.
        """
        for _ in range(300):
            titling = recalled(self.peers.root, self.member)
            if len(self.asked()) >= count and titling and not titling["asked"]:
                break
            time.sleep(0.05)
        return self.asked()


@RUNTIMES
def test_the_prompt_event_registers_the_naming_hook_and_refuses_nothing(
    target: Callable[[Path], NativeHarnessComposition],
    tree: str,
    event: str,
    host: str,
    arguments: list[str],
) -> None:
    """Under the prompt event, never refusing, on the strongest tier at low effort.

    With the short budget every prompt-time fold has, since nothing in it
    waits on the model, and the declaration compiled in the runtime's words.
    """
    artifacts = shipped(target)
    plugin = rendered(tree)
    hooks = json.loads(artifacts[plugin / "hooks" / "hooks.json"].content)["hooks"]
    [entry] = [
        entry
        for group in hooks[event]
        for entry in group["hooks"]
        if GUARD_SCRIPT in entry["command"]
    ]
    settings: Naming = json.loads(
        artifacts[plugin / "hooks" / "session_naming.json"].content
    )

    assert "exit 2" not in entry["command"]
    assert entry["timeout"] == 10
    assert artifacts[plugin / "hooks" / "scripts" / GUARD_SCRIPT].executable
    assert artifacts[plugin / "hooks" / "runtime" / RUNTIME_ENTRY].content == host
    assert settings["arguments"] == arguments


@pytest.mark.parametrize("declined", ["peer_policy", "session_naming"])
def test_no_roster_or_no_naming_registers_nothing(declined: str) -> None:
    """A name is what a roster addresses, so either absence carries nothing."""
    source: HookSet = declared_hook_set().model_copy(update={declined: None})
    hook = naming_hook(
        rendered(".claude"),
        "CLAUDE_PLUGIN_ROOT",
        source,
        CLAUDE_PROMPT_EVENT,
        CLAUDE_SESSION_NAMING,
        "lup.providers.claude.assets.session_naming",
        NamingSpelling(
            chosen=lambda _tier, _effort: ["--model", "opus"],
            arguments=["--tools", ""],
        ),
    )

    assert hook.registered == {}
    assert hook.artifacts == []


@pytest.mark.parametrize(("tier", "refused"), [("fast", True), ("strongest", False)])
def test_an_effort_the_tier_s_model_does_not_take_is_refused_where_it_is_generated(
    tier: ModelTier, refused: bool
) -> None:
    """Compiled the way a session's effort is, so no CLI drops it at every ask.

    Claude's fast tier is haiku, whose catalog row lists no effort at all.
    """
    source: HookSet = declared_hook_set().model_copy(
        update={
            "session_naming": SessionNaming(
                tier=tier, effort="low", reason="measured quick enough"
            )
        }
    )

    def spelled() -> None:
        naming_hook(
            rendered(".claude"),
            "CLAUDE_PLUGIN_ROOT",
            source,
            CLAUDE_PROMPT_EVENT,
            CLAUDE_SESSION_NAMING,
            "lup.providers.claude.assets.session_naming",
            NamingSpelling(chosen=claude_model_arguments, arguments=["--tools", ""]),
        )

    if refused:
        with pytest.raises(ValueError, match="does not take effort"):
            spelled()
    else:
        spelled()


def test_a_tier_below_the_strongest_says_why() -> None:
    """Every role asking below the strongest states the reason it does."""
    with pytest.raises(ValueError, match="without a reason"):
        SessionNaming(tier="balanced")
    assert SessionNaming().tier == "strongest"
    assert SessionNaming().effort == "low"


@pytest.mark.parametrize(
    ("target", "tree", "event"),
    [pytest.param(claude_target, ".claude", CLAUDE_PROMPT_EVENT, id="claude")],
)
def test_claude_names_the_session_without_holding_its_first_prompt(
    target: Callable[[Path], NativeHarnessComposition],
    tree: str,
    event: str,
    tmp_path: Path,
) -> None:
    """The first prompt goes on while the model is still answering.

    Held mid-answer, the ask is still under way when the hook has returned,
    and the roster still answers to its default; once it answers, the next
    prompt's answer carries the title, and the one after carries none.
    """
    session = Session(target, tree, event, tmp_path, "session-naming-hook")
    session.hold()

    first = session.prompted("Name each session after its work", title="dev")

    assert first == ""
    assert session.asking()
    assert session.called() == "dev"
    session.release()
    [ask] = session.concluded(1)
    assert session.called() == "session-naming-hook"
    assert ask.get("prompt") == request_for("Name each session after its work")
    assert ask.get("arguments", [])[:9] == [
        "--safe-mode",
        "-p",
        "--no-session-persistence",
        "--model",
        "opus",
        "--effort",
        "low",
        "--tools",
        "",
    ]
    second = session.prompted("And test it", title="dev")
    assert json.loads(second) == {
        "hookSpecificOutput": {
            "hookEventName": event,
            "sessionTitle": "session-naming-hook",
        }
    }
    assert session.prompted("And more", title="session-naming-hook") == ""
    assert len(session.asked()) == 1


@pytest.mark.parametrize(
    ("target", "tree", "event"),
    [pytest.param(claude_target, ".claude", CLAUDE_PROMPT_EVENT, id="claude")],
)
def test_claude_lets_a_rename_win_over_the_generated_name(
    target: Callable[[Path], NativeHarnessComposition],
    tree: str,
    event: str,
    tmp_path: Path,
) -> None:
    """A `/rename` made while the ask ran is never titled over, and the roster follows it.

    The title stays as the person set it; the roster takes it in its own shape.
    """
    session = Session(target, tree, event, tmp_path, "session-naming-hook")
    session.prompted("Name each session after its work", title="dev")
    session.concluded(1)

    renamed = session.prompted("Carry on", title="Naming Review")

    assert renamed == ""
    assert session.called() == "naming-review"
    assert session.prompted("And more", title="Naming Review") == ""
    assert len(session.asked()) == 1


@pytest.mark.parametrize(
    ("target", "tree", "event"),
    [pytest.param(claude_target, ".claude", CLAUDE_PROMPT_EVENT, id="claude")],
)
def test_claude_carries_a_roster_rename_to_the_title_once(
    target: Callable[[Path], NativeHarnessComposition],
    tree: str,
    event: str,
    tmp_path: Path,
) -> None:
    """A peer's rename reaches the chrome at the next prompt, and only then."""
    session = Session(target, tree, event, tmp_path, "session-naming-hook")
    session.prompted("Name each session after its work", title="dev")
    session.concluded(1)
    session.prompted("Carry on", title="dev")
    session.peers.rename(session.member, "chosen")

    pushed = session.prompted("Carry on", title="session-naming-hook")

    assert json.loads(pushed)["hookSpecificOutput"]["sessionTitle"] == "chosen"
    assert session.prompted("Carry on", title="somebody-renamed-it") == ""
    assert session.called() == "somebody-renamed-it"
    assert len(session.asked()) == 1


@pytest.mark.parametrize(
    ("target", "tree", "event"),
    [pytest.param(claude_target, ".claude", CLAUDE_PROMPT_EVENT, id="claude")],
)
def test_claude_asks_no_more_than_declared_of_prompts_that_name_nothing(
    target: Callable[[Path], NativeHarnessComposition],
    tree: str,
    event: str,
    tmp_path: Path,
) -> None:
    """A greeting names nothing and the next prompt is asked again, to the limit."""
    session = Session(target, tree, event, tmp_path, None)

    for asked in range(1, 5):
        assert session.prompted("hi", title="dev") == ""
        session.concluded(min(asked, 3))
    assert len(session.asked()) == 3
    assert session.called() == "dev"


@pytest.mark.parametrize(
    ("target", "tree", "event"),
    [pytest.param(claude_target, ".claude", CLAUDE_PROMPT_EVENT, id="claude")],
)
def test_claude_never_asks_over_a_title_somebody_set(
    target: Callable[[Path], NativeHarnessComposition],
    tree: str,
    event: str,
    tmp_path: Path,
) -> None:
    """The chosen title is taken up instead, and no model is asked for another."""
    session = Session(target, tree, event, tmp_path, "never-asked")

    assert session.prompted("hi", title="somebody-chose-this") == ""
    assert session.prompted("Name each session", title="somebody-chose-this") == ""
    assert session.asked() == []
    assert session.called() == "somebody-chose-this"


@pytest.mark.parametrize(
    ("target", "tree", "event"),
    [pytest.param(codex_target, ".codex", CODEX_PROMPT_EVENT, id="codex")],
)
def test_codex_names_the_session_and_its_thread_without_holding_the_prompt(
    target: Callable[[Path], NativeHarnessComposition],
    tree: str,
    event: str,
    tmp_path: Path,
) -> None:
    """The hook answers nothing and returns; a process of its own names roster and thread.

    Held mid-answer, the ask is still under way when the hook has returned.
    """
    session = Session(target, tree, event, tmp_path, "session-naming-hook")
    session.hold()

    assert session.prompted("Name each session after its work") == ""
    assert session.asking()
    assert session.called() == "dev"
    session.release()
    ask, named = session.concluded(2)

    assert ask.get("prompt") == request_for("Name each session after its work")
    assert ask.get("arguments", [])[:2] == ["exec", "--ephemeral"]
    assert "features.hooks=false" in ask.get("arguments", [])
    assert "features.shell_tool=false" in ask.get("arguments", [])
    assert 'sandbox_mode="read-only"' in ask.get("arguments", [])
    assert 'model_reasoning_effort="low"' in ask.get("arguments", [])
    assert "gpt-5.6-sol" in ask.get("arguments", [])
    assert named == Asked(
        named=Named(threadId="root-session", name="session-naming-hook")
    )
    assert session.called() == "session-naming-hook"

    session.peers.rename(session.member, "chosen")
    assert session.prompted("Carry on") == ""
    assert session.concluded(3)[2:] == [
        Asked(named=Named(threadId="root-session", name="chosen"))
    ]


def test_only_the_runtime_that_hides_titles_listens_for_a_resume() -> None:
    """Codex reports no title to a hook, so it is told as a thread is reopened.

    Claude Code hands its prompt hook the title a reopened conversation kept,
    so it needs no second event, and registers none.
    """
    claude = json.loads(
        shipped(claude_target)[rendered(".claude") / "hooks" / "hooks.json"].content
    )["hooks"]
    codex = json.loads(
        shipped(codex_target)[rendered(".codex") / "hooks" / "hooks.json"].content
    )["hooks"]

    assert not [
        entry
        for group in claude.get("SessionStart", [])
        for entry in group["hooks"]
        if GUARD_SCRIPT in entry["command"]
    ]
    [group] = [
        group
        for group in codex["SessionStart"]
        if any(GUARD_SCRIPT in entry["command"] for entry in group["hooks"])
    ]
    assert group["matcher"] == "resume"


@pytest.mark.parametrize(
    ("target", "tree", "event"),
    [pytest.param(claude_target, ".claude", CLAUDE_PROMPT_EVENT, id="claude")],
)
def test_claude_takes_up_a_title_set_where_the_session_is_looked_at(
    target: Callable[[Path], NativeHarnessComposition],
    tree: str,
    event: str,
    tmp_path: Path,
) -> None:
    """A resumed conversation keeps its name: its own title, then a `/rename`.

    The launch leaves a reopened conversation the title it last had, and the
    roster follows both it and a later rename. Nothing is asked for either:
    the title was chosen, and the chrome already shows it, so nothing is
    handed back.
    """
    session = Session(target, tree, event, tmp_path, "never-asked")

    assert session.prompted("continue", title="Fix Login Redirect") == ""
    assert session.called() == "fix-login-redirect"
    assert session.prompted("carry on", title="my-own-title") == ""
    assert session.called() == "my-own-title"
    assert session.asked() == []


@pytest.mark.parametrize(
    ("target", "tree", "event"),
    [pytest.param(claude_target, ".claude", CLAUDE_PROMPT_EVENT, id="claude")],
)
def test_claude_leaves_a_title_the_roster_had_to_number_as_it_was_set(
    target: Callable[[Path], NativeHarnessComposition],
    tree: str,
    event: str,
    tmp_path: Path,
) -> None:
    """A live session already answers to the title, so the roster numbers it.

    The chrome keeps the title as the person set it: the number is the
    roster's, to keep two sessions apart, and nobody asked the chrome to move.
    """
    session = Session(target, tree, event, tmp_path, "never-asked")
    session.peers.join(mint_member_id(), session.repository, cli_name="taken")

    assert session.prompted("carry on", title="Taken") == ""
    assert session.called() == "taken-2"
    assert session.prompted("carry on", title="Taken") == ""


@pytest.mark.parametrize(
    ("target", "tree", "event"),
    [pytest.param(codex_target, ".codex", CODEX_PROMPT_EVENT, id="codex")],
)
def test_codex_takes_up_the_name_a_reopened_thread_already_has(
    target: Callable[[Path], NativeHarnessComposition],
    tree: str,
    event: str,
    tmp_path: Path,
) -> None:
    """A resumed session keeps its name: heard as the thread is reopened, read at
    the next prompt, and never asked for.

    ``thread/read`` answers the title Codex generated for a thread nobody
    named, so the roster takes it up in its own shape; the thread keeps its
    name as it stands, and is told nothing.
    """
    session = Session(target, tree, event, tmp_path, "never-asked")
    session.kept("Fix expired MCP authentication token")

    assert session.reopened() == ""
    assert session.prompted("continue") == ""
    [read] = session.concluded(1)

    assert read == Asked(read=Read(threadId="root-session"))
    assert session.called() == "fix-expired-mcp-authentication-token"
    assert session.prompted("carry on") == ""
    assert session.concluded(1) == [read]


@pytest.mark.parametrize(
    ("target", "tree", "event"),
    [pytest.param(codex_target, ".codex", CODEX_PROMPT_EVENT, id="codex")],
)
@pytest.mark.parametrize("kept", [None, "repository-2"], ids=["unnamed", "default"])
def test_codex_names_a_reopened_thread_that_had_no_name(
    target: Callable[[Path], NativeHarnessComposition],
    tree: str,
    event: str,
    tmp_path: Path,
    kept: str | None,
) -> None:
    """A thread with no name of its own, or only its worktree's, is asked for one."""
    session = Session(target, tree, event, tmp_path, "session-naming-hook")
    session.kept(kept)

    session.reopened()
    session.prompted("Name each session after its work")
    read, ask, named = session.concluded(3)

    assert read == Asked(read=Read(threadId="root-session"))
    assert ask.get("arguments", [])[:2] == ["exec", "--ephemeral"]
    assert named == Asked(
        named=Named(threadId="root-session", name="session-naming-hook")
    )
    assert session.called() == "session-naming-hook"
