"""Whether a Codex session can be *asked* rather than only refused.

This is the last structural difference between the two runtimes' review
experience.

A policy verdict of `ask` has nowhere to go on Codex's `PreToolUse`: the
compiled dispatcher runs as a command hook, that boundary carries no portable
ask effect, and `queued_review` spends the verdict as a denial that sends the
operator to another terminal. Issue #180 is that dead end reported from a real
session, where an explicitly authorized whole-file rewrite could not be
installed because "nobody who could approve it is reachable from this
session".

The generated plugin registers a second event for the interactive channel, and
the dispatcher is already written for it: on `PermissionRequest` an `allow`
becomes a native allow decision, and an `ask` **returns saying nothing** —
the dispatcher deliberately declining so the runtime's own approval flow can
proceed.

Both halves are now measured, on codex-cli 0.155.1, and both answered no:

* **The event does not fire in a session an application opens.** A live
  app-server turn under `approval_policy='on-request'` added nothing to the
  plugin's journal — 48 completed `PermissionRequest` records before, 48
  after. The 48 it already held came from elsewhere, so the event reaches a
  terminal and not this path.

* **Nothing reaches the client when the dispatcher declines.** The shell call
  was refused by `PreToolUse`, `queued_review` parked it, and the turn came
  back carrying the queue's own recovery text. No approval request arrived for
  the session's hooks to answer.

So a Codex session an application opens cannot be asked, only refused. #180 has
no native way out, the fail-closed denial is correct rather than a workaround,
and `review` is Codex's review surface rather than its fallback — which
is what makes that surface's diff rendering load-bearing.

Both arms are `xfail(strict=True)` rather than deleted or inverted. Deleting
them would lose the measurement; inverting them would assert a vendor gap as
though it were a contract. Strict xfail says what was true when it was read and
fails the day it stops being true, which is the only day anyone needs to know.

Neither arm runs without a Codex login, which a contained session does not
reach — a nested `codex exec` there answers 401, the credential sitting outside
what a session is granted. So they carry the integration marker this repository
deselects by default, and the run that settles them is the operator's to start
from a host terminal where Codex is signed in:

    uv run pytest tests/integration/test_codex_approval_request.py \
        -m integration -v

Deselected rather than failed, which keeps an unmeasured question out of
everybody else's gate.
"""

import json
from pathlib import Path

import pytest
from pydantic import BaseModel, Field

from lup.policy.hooks import (
    LupHookInput,
    LupHookMatcher,
    LupHookOutput,
    LupHooksConfig,
    allow_hook,
)
from lup.providers.codex.home import CodexWorktreeHomeStore
from lup.providers.codex import Codex, CodexTools
from lup.sessions.surface import Agent
from lup.workspace.paths import find_project_root

pytestmark = pytest.mark.integration

PROBE_MODEL = None
"""Whichever model the scoped home is configured with, rather than a pin.

A pinned model went stale and took all three arms down with it. The home is
seeded from the operator's own `config.toml`, so it carries their
`model_reasoning_effort` as well as their `model` — and a session that
overrides only the first sends the two to the API as a pair that never existed
together. On this machine that was `gpt-5.5` with `max`, which answers
`400 unsupported_value: 'max' is not supported with the 'gpt-5.5' model`.

Naming nothing leaves both the home's, which is consistent by construction and
cannot go stale. What this file measures is an approval boundary, and no part
of that question is about which model answers the prompt.
"""

ALLOWED = "echo lup-approval-control"
"""Classified `allow`, so the dispatcher answers with a native allow decision."""

ASKED = "chmod +x tmp/lup-approval-probe.sh"
"""Classified `ask` — "changing permissions requires approval".

Inert by construction: the file it names is never created, so the most a
granted approval buys is a `chmod` that fails on a missing path. The arm below
*grants* what arrives, so the command being harmless is load-bearing here
rather than incidental.
"""

INSTRUCTIONS = (
    "You are a shell assistant. Run exactly the command you are given with "
    "the shell tool, once. Never work around a refusal and never rewrite the "
    "command. Then report what happened by calling the submission tool — "
    "always finish by submitting, whether the command ran, was refused, or "
    "was left waiting on an approval nobody answered."
)


class ShellAttempt(BaseModel):
    """What one session observed when it tried to run one command."""

    ran: bool = Field(description="True if the command executed and produced output")
    output: str = Field(description="What was printed, the refusal, or the wait")


class ApprovalWatch(BaseModel):
    """Every approval request one turn caused the server to send its client.

    Recorded and then **granted**, which is the correction that made this file
    measure anything. It recorded and denied at first, on the reasoning that a
    probe should grant nothing — but `CodexApprovalResponder` consults these
    same hooks for every approval the app-server sends, so denying everything
    starved the turn and all three arms died with `ProviderTurnError` before
    reaching an assertion. The control failing alongside the other two is what
    gave it away: a control that cannot pass is measuring the probe.

    Granting is safe because what it grants is fixed and inert: the only two
    commands this file puts to a session are an `echo` and a `chmod` on a path
    that does not exist.
    """

    arrivals: list[str] = []

    def hooks(self) -> LupHooksConfig:
        """The session hooks an arriving approval is routed through.

        `resolve_approval` answers an app-server approval request from the
        session's own declared hooks, so registering one is how a client
        observes that a request reached it at all. A session declaring none
        declines every request — the safe answer, and also one
        indistinguishable from nothing having arrived.
        """

        async def note(event: LupHookInput) -> LupHookOutput:
            self.arrivals.append(event.tool_name)
            return allow_hook(reason="the approval probe records what arrives")

        return LupHooksConfig(pre_tool_use=[LupHookMatcher(hook=note, tag="probe")])

    def arrived(self) -> bool:
        """Whether the app-server asked this client to decide anything."""
        return bool(self.arrivals)


def answering_session(cwd: Path, watch: ApprovalWatch) -> Codex:
    """A session whose approvals are live, which is what makes the arms possible.

    ``approval_policy`` is `on-request` rather than the `never` the control
    uses, and that is the point: `never` is the setting under which the
    interactive event is already known not to fire, so a probe keeping it
    would re-measure the answer we have.

    The sandbox stays at `workspace-write` rather than being opened, because an
    approval request is what a boundary *produces* — a session granted full
    access has nothing left to ask about.
    """
    return Codex(
        model=PROBE_MODEL,
        system_prompt=INSTRUCTIONS,
        cwd=cwd,
        sandbox_mode="workspace-write",
        tools=CodexTools(builtin=["Bash"]),
        approval_policy="on-request",
        hooks=watch.hooks(),
    )


def quiet_session(cwd: Path) -> Codex:
    """The control's session: the shape already known to complete a turn.

    `never` and full access, exactly as `test_codex_hook_firing.py` opens one.
    A control exists to prove the prompt reaches the shell, so it must not also
    be the first place a new configuration is tried — doing that is what left
    an earlier run's three failures indistinguishable from one another.
    """
    return Codex(
        model=PROBE_MODEL,
        system_prompt=INSTRUCTIONS,
        cwd=cwd,
        sandbox_mode="danger-full-access",
        tools=CodexTools(builtin=["Bash"]),
        approval_policy="never",
    )


def hook_records(cwd: Path) -> list[dict[str, str]]:
    """Every hook invocation this checkout's plugin has journalled.

    Under `plugins/data/<plugin>/` inside the scoped home, where the
    dispatcher's own `PLUGIN_DATA` points — globbed rather than composed,
    because that directory is named for the installed plugin revision and this
    only needs to find it. An earlier draft read the home's root and would have
    found nothing whatever the session did. Metadata only, so nothing read here
    carries a command or a patch.
    """
    home = CodexWorktreeHomeStore().home_for(cwd)
    return [
        json.loads(line)
        for journal in home.glob("plugins/data/*/hook-events.jsonl")
        for line in journal.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def settled(cwd: Path, event: str) -> list[dict[str, str]]:
    """Completed invocations of one event, which are the ones that decided."""
    return [
        record
        for record in hook_records(cwd)
        if record.get("event_name") == event and record.get("phase") == "completed"
    ]


async def ask(agent: Agent, command: str) -> ShellAttempt:
    """Put one command to one already-configured agent."""
    result = await agent.ask(
        f"Run this command with the shell tool: {command}\n"
        "Then call the submission tool with `ran` set to whether it "
        "executed, and `output` set to what it printed, the refusal "
        "you were given, or the approval you were left waiting on.",
        ShellAttempt,
    )
    return result.output


async def test_the_probe_prompt_reaches_the_shell() -> None:
    """The control. Without it, a refusal and a reluctance look the same."""
    observed = await ask(quiet_session(find_project_root()), ALLOWED)

    assert observed.ran, observed.output
    assert "lup-approval-control" in observed.output


@pytest.mark.xfail(
    strict=True,
    reason=(
        "Measured on codex-cli 0.155.1 and answered no. A live app-server turn "
        "with `approval_policy='on-request'` added nothing to the plugin's "
        "journal: 48 completed PermissionRequest records before, 48 after. The "
        "48 it already held came from somewhere else, so the event fires for a "
        "terminal and not for a session an application opens. Strict, because "
        "the day this passes is the day Codex grew the channel and this file "
        "should say so loudly rather than quietly agreeing."
    ),
)
async def test_whether_permission_request_fires_in_an_app_server_session() -> None:
    """Arm one: does the interactive hook event reach the plugin here?

    The journal rather than the command's fate, because a command that ran
    proves nothing about which hook let it run: `PreToolUse` allowing it while
    `PermissionRequest` never fires looks exactly like both firing.

    Counted across the turn rather than asserted absolutely, because this
    checkout's journal already holds invocations from interactive sessions —
    what is being asked is whether the *app-server* path adds one.
    """
    root = find_project_root()
    before = len(settled(root, "PermissionRequest"))

    await ask(answering_session(root, ApprovalWatch()), ALLOWED)

    assert len(settled(root, "PermissionRequest")) > before, (
        "No PermissionRequest reached the plugin's dispatcher during a live "
        "app-server turn, so the interactive half of the boundary fires only "
        "for a terminal — and an `ask` in a session an application opened can "
        "only ever be spent as a denial. `review` is then permanently "
        "the review surface. Record it against the app-server rather than the "
        "plugin: the same journal holds invocations from interactive runs."
    )


@pytest.mark.xfail(
    strict=True,
    reason=(
        "Measured on codex-cli 0.155.1 and answered no, which settles #180 "
        "against a native way out. Nothing reached the client: the shell call "
        "was refused by PreToolUse with `changing permissions requires "
        "approval`, `queued_review` parked it, and the turn came back carrying "
        "the queue's own recovery text. No approval request arrived for the "
        "session's hooks to answer, so the dispatcher's deliberate silence on "
        "`ask` has nobody listening on the other side. The fail-closed denial "
        "is therefore correct rather than a workaround, and `review` is "
        "Codex's review surface rather than its fallback. Strict, so a vendor "
        "that grows the channel is heard immediately."
    ),
)
async def test_whether_a_declined_decision_reaches_the_client_as_an_approval() -> None:
    """Arm two, and the one that decided whether #180 has a way out.

    On `ask` the generated dispatcher returns saying nothing, which is it
    declining so the runtime's own approval flow can proceed. This asks
    whether that flow reaches whoever opened the thread.

    A failure here is not a defect in Lup, and the measured failure was not.
    What it found is that nothing is listening, which is a fact about the
    app-server's approval surface and is recorded as one.
    """
    root = find_project_root()
    watch = ApprovalWatch()

    observed = await ask(answering_session(root, watch), ASKED)

    assert watch.arrived(), (
        "The dispatcher declined to decide and no approval request reached "
        "the client, so nothing was listening: a Codex session cannot be "
        "asked, only refused. Record it against the app-server's approval "
        "surface rather than as a Lup defect. The turn reported: "
        f"ran={observed.ran}, {observed.output}"
    )
