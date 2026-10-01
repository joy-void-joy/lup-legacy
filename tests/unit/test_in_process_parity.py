"""The policy a program composes answers what a session's dispatcher answers.

`dev policy`, the everyday sweep and every nested agent judge with
`semantic_policy_for`; a session judges with the dispatcher generated from the
same declaration. The dispatcher reads facts about the host before it asks the
kernel anything -- the launch's measured lease, a peer's claim on a file, the
rule selection it was compiled with -- and a composition reading none of them
answers a question no session is ever asked.

Each case here is put to all three enforcement paths over one real repository:
the in-process policy, and each runtime's generated dispatcher run on the
payload its harness sends, so an answer one of them reaches and another does
not fails here rather than in somebody's session.
"""

import json
import os
import sys
from pathlib import Path
from typing import Literal

import pytest
import sh

from lup.coordination.identity import MEMBER_ENV, mint_member_id
from lup.coordination.repository import RepositoryPeers
from lup.harness.codescan.common import RuleSelection
from lup.harness.enforcement import measured_containment, semantic_policy_for
from lup.harness.models import HookSet
from lup.policy.models import (
    Decision,
    EditBatch,
    EditChange,
    SemanticTool,
    ShellCommand,
)
from lup.types import JsonObject
from lup_template.harness.catalog import declared_hook_set
from lup_template.harness.composition import TARGETS
from tests.unit.held import held_argv, holding
from tests.unit.native import claude_effect, codex_denial
from tests.unit.repos import commit_file, initialized_repo

type Runtime = Literal["claude", "codex"]

DISPATCHERS: dict[Runtime, Path] = {
    "claude": Path(".claude/plugins/lup/hooks/scripts/policy.py"),
    "codex": Path(".codex/plugins/lup/hooks/scripts/policy.py"),
}

NONCE = "parity"
"""The launch every case here claims to be, named by the ledger it wrote."""


class Session:
    """One checkout, the ledger its launch wrote, and the environment carrying both."""

    def __init__(self, base: Path) -> None:
        self.base = base
        self.checkout = base / "checkout"
        self.git = initialized_repo(self.checkout, base / "no-hooks")
        commit_file(self.git, self.checkout, "README.md", "checkout\n", "chore: base")
        self.ledger = base / "launch"
        self.held = False
        self.environment = {
            name: value
            for name, value in os.environ.items()
            if not name.startswith("LUP_")
        } | {
            "LUP_BOUNDARY_NONCE": NONCE,
            "LUP_BOUNDARY_ROOT": str(self.ledger),
            "PLUGIN_DATA": str(base / "plugin-data"),
        }

    def launched(self, writable: list[str], contained: bool) -> None:
        """Record what this launch measured, as the launcher writes it.

        A contained launch's record is then read through a read-only mount of
        its directory, as that launch holds it: the dispatchers for real, in a
        namespace of their own, and this process by the held fixture.
        """
        self.held = contained
        record = self.ledger / ".lup" / "preflight" / f"{NONCE}.json"
        record.parent.mkdir(parents=True, exist_ok=True)
        record.write_text(
            json.dumps(
                {
                    "contained": ["yes"] if contained else [],
                    "delivered": ["inside_placement"] if contained else [],
                    "unjudged_ambient": ["ask"],
                    "writable_roots": writable,
                    "read_only_roots": [],
                }
            ),
            encoding="utf-8",
        )

    def dispatched(
        self, runtime: Runtime, call: JsonObject, script: Path | None = None
    ) -> str:
        """One runtime's effect for a call, run as its harness runs the hook.

        *script* is a dispatcher compiled elsewhere, where the case needs one
        this checkout's committed tree is not.

        Codex has no ask at this boundary: it parks the question as a review
        and answers with a structured refusal naming who can release it, which
        is the same question put where Codex can carry one.
        """
        dispatching = [
            sys.executable,
            "-I",
            "-S",
            str(script or DISPATCHERS[runtime].resolve()),
        ]
        if self.held:
            holding()
            dispatching = held_argv(self.ledger / ".lup" / "preflight", dispatching)
        result = sh.Command(dispatching[0])(
            *dispatching[1:],
            _in=json.dumps(
                {
                    "session_id": "parity",
                    "hook_event_name": "PreToolUse",
                    "cwd": str(self.checkout),
                    **call,
                }
            ),
            _ok_code=[0, 2],
            _return_cmd=True,
            _env=self.environment,
        )
        assert isinstance(result, sh.RunningCommand)
        if runtime == "codex":
            if result.exit_code == 2:
                return "deny"
            if not result.stdout:
                return "allow"
            assert codex_denial(result)
            return "ask"
        # A deferral renders as no decision at all, leaving the runtime's own.
        rendered = json.loads(str(result))
        specific = (
            rendered["hookSpecificOutput"] if "hookSpecificOutput" in rendered else {}
        )
        if "permissionDecision" not in specific:
            return "defer"
        return claude_effect(rendered)

    def composed(self, monkeypatch: pytest.MonkeyPatch, event: SemanticTool) -> str:
        """The in-process effect, composed the way `dev policy` composes it."""
        return self.judged(monkeypatch, event).effect

    def judged(
        self,
        monkeypatch: pytest.MonkeyPatch,
        event: SemanticTool,
        hooks: HookSet | None = None,
    ) -> Decision:
        """The in-process verdict, in this session's environment."""
        for name, value in self.environment.items():
            if name.startswith("LUP_"):
                monkeypatch.setenv(name, value)
        held = measured_containment(self.checkout)
        policy = semantic_policy_for(
            hooks or declared_hook_set(),
            recovered=True,
            contained=held.contained,
            inside_placement=held.inside_placement,
        )
        return policy.decide(event)

    def command(self, command: str) -> ShellCommand:
        """One shell command, run from this checkout."""
        return ShellCommand(command=command, cwd=self.checkout)

    def edit(self, target: Path, before: str, after: str) -> EditBatch:
        """One edit of *target*, as the in-process policy is handed it."""
        return EditBatch(
            changes=[EditChange(path=target, before=before, after=after)],
            cwd=self.checkout,
        )


def edited(runtime: Runtime, target: Path, before: str, after: str) -> JsonObject:
    """The same edit, as each runtime's harness carries it."""
    if runtime == "claude":
        return {
            "tool_name": "Edit",
            "tool_input": {
                "file_path": str(target),
                "old_string": before.rstrip("\n"),
                "new_string": after.rstrip("\n"),
            },
        }
    return {
        "tool_name": "apply_patch",
        "tool_input": {
            "command": (
                f"*** Begin Patch\n*** Update File: {target}\n"
                f"@@\n-{before.rstrip()}\n+{after.rstrip()}\n*** End Patch"
            )
        },
    }


@pytest.fixture
def session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Session:
    for name in [name for name in os.environ if name.startswith("LUP_")]:
        monkeypatch.delenv(name)
    return Session(tmp_path.resolve())


def shell(command: str) -> JsonObject:
    """One shell call, as both runtimes carry it."""
    return {"tool_name": "Bash", "tool_input": {"command": command}}


@pytest.mark.parametrize(
    ("command", "leased", "contained", "effect"),
    [
        # A worktree cut after the container started: the checkout is not
        # among the roots the launch mounted writable.
        pytest.param("mkdir -p tmp/build", False, True, "ask", id="outside-lease"),
        pytest.param("mkdir -p tmp/build", True, True, "allow", id="inside-lease"),
        # The harness's scratchpad is its own at every placement.
        pytest.param(
            "touch /tmp/claude-1000/parity/scratchpad/out.txt",
            True,
            False,
            "allow",
            id="scratchpad-uncontained",
        ),
        # The machine's temporary root is the launch's own only in a container.
        pytest.param(
            "touch /tmp/parity-out.txt", True, True, "allow", id="tmp-contained"
        ),
        pytest.param(
            "touch /tmp/parity-out.txt", True, False, "ask", id="tmp-uncontained"
        ),
    ],
)
def test_a_write_the_launch_did_not_mount_is_judged_alike_everywhere(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
    command: str,
    leased: bool,
    contained: bool,
    effect: str,
    launch_record_held: None,
) -> None:
    """The lease is read from the ledger the launch wrote, by every path."""
    session.launched(
        [str(session.checkout)] if leased else [str(session.base / "elsewhere")],
        contained,
    )

    assert session.composed(monkeypatch, session.command(command)) == effect
    assert session.dispatched("claude", shell(command)) == effect
    assert session.dispatched("codex", shell(command)) == effect


@pytest.fixture
def linked(session: Session) -> Session:
    """A checkout whose scratch holds a link into its production source."""
    (session.checkout / "src").mkdir()
    (session.checkout / "src" / "a.py").write_text("value = 1\n", encoding="utf-8")
    (session.checkout / "tmp").mkdir()
    (session.checkout / "tmp" / "link").symlink_to(session.checkout / "src")
    return session


@pytest.mark.parametrize(
    "command",
    [
        pytest.param("touch tmp/link/b.py", id="path-verb"),
        pytest.param("echo x > tmp/link/b.py", id="redirect"),
    ],
)
def test_a_shell_write_through_a_link_asks_everywhere(
    linked: Session, monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    """The spelling reads as scratch and the write lands in production.

    The host resolves each target and the kernel says whether the landing
    changes the role, on every path; a composition that resolved nothing
    would grant the scratch spelling what the landing never earned.
    """
    assert linked.composed(monkeypatch, linked.command(command)) == "ask"
    assert linked.dispatched("claude", shell(command)) == "ask"
    assert linked.dispatched("codex", shell(command)) == "ask"


def test_an_edit_through_a_link_is_judged_where_it_lands_everywhere(
    linked: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every path resolves an edited file before any gate reads its role.

    So the scratch spelling earns nothing: the edit meets the gates of the
    file it lands on, the same answer as spelling that file directly, and an
    edit production refuses is not allowed for arriving through scratch.
    """
    before, after = "value = 1\n", "from typing import Any\n"
    through = linked.checkout / "tmp" / "link" / "a.py"
    landing = linked.checkout / "src" / "a.py"

    effect = linked.composed(monkeypatch, linked.edit(landing, before, after))
    assert effect != "allow"
    assert linked.composed(monkeypatch, linked.edit(through, before, after)) == effect
    for runtime in DISPATCHERS:
        call = edited(runtime, through, before, after)
        assert linked.dispatched(runtime, call) == effect


@pytest.mark.parametrize(
    ("asker", "effect"),
    [
        pytest.param("stranger", "ask", id="another-session-holds-it"),
        pytest.param("holder", "allow", id="its-own-claim"),
    ],
)
def test_an_edit_under_a_peers_claim_is_asked_about_everywhere(
    session: Session, monkeypatch: pytest.MonkeyPatch, asker: str, effect: str
) -> None:
    """A live session holding the path is a question on every path.

    Read from the roster the store keeps under the shared git directory, as
    the asker the environment names, so a session is never asked about its
    own work and `dev policy` shows the question a session would be shown.
    """
    commit_file(session.git, session.checkout, "a.py", "value = 1\n", "seed")
    peers = RepositoryPeers(session.checkout)
    members = {"holder": mint_member_id(), "stranger": mint_member_id()}
    peers.join(members["holder"], session.checkout, cli_name="feat-holding")
    peers.join(members["stranger"], session.checkout, cli_name="feat-asking")
    peers.lock(members["holder"], session.checkout)
    session.environment[MEMBER_ENV] = members[asker]
    target = session.checkout / "a.py"
    before, after = "value = 1\n", "value = 2\n"

    verdict = session.judged(monkeypatch, session.edit(target, before, after))
    assert verdict.effect == effect
    assert ("feat-holding" in verdict.reason) == (effect == "ask")
    for runtime in DISPATCHERS:
        call = edited(runtime, target, before, after)
        assert session.dispatched(runtime, call) == effect


def test_a_command_is_not_asked_about_a_peers_claim_anywhere(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A command's writes are attributed after it runs, on every path.

    So a heredoc carrying its content meets the edit gates and not the claim:
    the question is an edit tool's, and the dispatchers never ask it of a
    command.
    """
    commit_file(session.git, session.checkout, "notes.md", "one\n", "seed")
    peers = RepositoryPeers(session.checkout)
    holder, stranger = mint_member_id(), mint_member_id()
    peers.join(holder, session.checkout, cli_name="feat-holding")
    peers.join(stranger, session.checkout, cli_name="feat-asking")
    peers.lock(holder, session.checkout)
    session.environment[MEMBER_ENV] = stranger
    command = "echo two >> notes.md"

    effect = session.composed(monkeypatch, session.command(command))
    assert effect == "allow"
    for runtime in DISPATCHERS:
        assert session.dispatched(runtime, shell(command)) == effect


def compiled(runtime: Runtime, selection: RuleSelection, into: Path) -> Path:
    """One runtime's hook tree compiled against *selection*, laid out under *into*.

    Every file the recipe places under the plugin's hooks directory, so the
    dispatcher runs beside the runtime, kernel and data generation gave it.
    """
    hooks = DISPATCHERS[runtime].parent.parent
    build = TARGETS.builders[runtime]
    for artifact in build(Path.cwd(), selection).recipe.desired.artifacts:
        if artifact.path.is_relative_to(hooks):
            landed = into / artifact.path
            landed.parent.mkdir(parents=True, exist_ok=True)
            landed.write_text(artifact.content, encoding="utf-8")
    return into / DISPATCHERS[runtime]


@pytest.mark.parametrize(
    ("retired", "effect"),
    [
        pytest.param([], "deny", id="held"),
        pytest.param(["any-type"], "allow", id="retired"),
    ],
)
def test_a_retired_rule_is_retired_on_every_path(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
    retired: list[str],
    effect: str,
) -> None:
    """The selection a plugin is compiled with is the one a composition judges by.

    Each tree is compiled here against the selection, the way a launch that
    retires rules compiles it, and the composition is handed the same hook
    set: a rule the project dropped neither fires in one and not the other.
    """
    selection = RuleSelection(retired=retired)
    hooks = declared_hook_set().model_copy(update={"rules": selection})
    commit_file(session.git, session.checkout, "a.py", "value = 1\n", "seed")
    target = session.checkout / "a.py"
    before, after = "value = 1\n", "from typing import Any\n"

    rewrite = "sed -i 's/value = 1/from typing import Any/' a.py"

    edit = session.edit(target, before, after)
    assert session.judged(monkeypatch, edit, hooks).effect == effect
    command = session.command(rewrite)
    assert session.judged(monkeypatch, command, hooks).effect == effect
    for runtime in DISPATCHERS:
        script = compiled(runtime, selection, session.base / "plugins")
        call = edited(runtime, target, before, after)
        assert session.dispatched(runtime, call, script) == effect
        assert session.dispatched(runtime, shell(rewrite), script) == effect
