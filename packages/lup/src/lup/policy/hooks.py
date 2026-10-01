# lup: ignore[native-spelling]
# The seam names the native hook events it normalizes, spelled as they land.
"""SDK-agnostic hook utilities — the normalized hook seam and its factories.

SDK-agnostic hook models and factories: permission hooks, tool allowlists,
gates, nudges, capture.

This module owns the whole hook vocabulary: the normalized
:class:`LupHookInput` / :class:`LupHookOutput` models, the
:class:`LupHookMatcher`, the :class:`LupHooksConfig` structure, the
``allow_hook`` / ``deny_hook`` / ``block_hook`` decision constructors, and
``merge_hooks`` composition. Adapters translate their native hook payloads
into :class:`LupHookInput` at the seam, so every factory here reads typed
attributes rather than digging through a raw payload — backend-neutral by
construction.

Output helpers:
- allow_hook() — PreToolUse allow decision
- ask_hook() — PreToolUse approval-required decision
- deny_hook() — PreToolUse deny decision
- block_hook() — block decision (Stop or PreToolUse)

PreToolUse hooks:
- create_permission_hooks() — directory-based read/write access control
- create_tool_allowlist_hook() — restrict agent to specific tools
- create_tool_gate() — deny a tool (or Stop) until a condition unlocks it

PostToolUse hooks:
- create_nudge_hook() — inject system messages suggesting better alternatives
- create_capture_hook() — extract data from sub-agent tool responses

Composition:
- merge_hooks() to compose multiple hook sources

Examples:
    Compose permission and nudge hooks::

        >>> from lup.policy.hooks import create_permission_hooks, create_nudge_hook, merge_hooks
        >>> perms = create_permission_hooks(
        ...     rw_dirs=[Path("/data")], ro_dirs=[Path("/ref")]
        ... )
        >>> nudges = create_nudge_hook({"fetch_url": lambda inp: "Use WebFetch"})
        >>> combined = merge_hooks(perms, nudges)

    Restrict an agent to specific tools::

        >>> hooks = create_tool_allowlist_hook(["Read", "Grep", "WebSearch"])

    Gate a tool until another tool has run::

        >>> hooks = create_tool_gate(
        ...     gated_tool="StructuredOutput",
        ...     message="Call review() before finalizing output.",
        ...     on_unlock_tool="mcp__notes__review",
        ... )

    Capture data from a sub-agent's tool calls::

        >>> capture = create_capture_hook("WebSearch", extract_urls)
        >>> # After running the agent, `captured` contains extracted items
        >>> len(capture["captured"])
        5
"""

from collections.abc import Awaitable, Callable, Sequence
from itertools import accumulate
from pathlib import Path
from typing import Literal, TypedDict

from pydantic import BaseModel, Field
from pydantic.json_schema import SkipJsonSchema

from lup.policy.kernel.decision import SandboxPlacement
from lup.workspace.paths import path_is_under
from lup.types import JsonObject, ToolName

type LupHookEvent = Literal["PreToolUse", "PostToolUse", "Stop"]
"""The canonical hook-event names — the neutral seam every backend maps onto.

Claude Code's event vocabulary is adopted as the framework's lingua franca: an
adapter translates its backend's native lifecycle events into these names, so
the factories here register against one spelling regardless of engine."""

type LupHookDecision = Literal["allow", "ask", "deny", "block"]
"""A hook's verdict: allow the action, ask whoever can answer, deny it (a
PreToolUse permission refusal), or block it (the cross-event stop/redirect
decision).

``ask`` is what makes a denial recoverable. Without it every refusal is
terminal for an agent with no interactive human attached — a worker meeting
a genuine need outside its allowlist has no route at all, which is how a
merge worker once spent a whole run unable to stage its own resolutions.

``None`` is the fifth answer and means the hook declines to decide, so the
session's ambient permission flow applies untouched."""


class LupHookInput(BaseModel):
    """A normalized hook event, produced by an adapter from its native payload.

    Native decoders translate a provider hook payload into this model at the
    adapter seam, so the factories stay backend-neutral. Path-bearing tools
    (Write/Edit/Read/Glob/Grep) have their target directory resolved once,
    into :attr:`tool_path`, rather than re-extracted from :attr:`tool_input`
    by each factory.

    :attr:`cwd` is the directory the calling session is in, which a shell
    command's relative operands resolve against. It is carried rather than
    read from the process, because a hook does not always run where the
    session it judges is running.
    """

    event: LupHookEvent
    tool_name: str = ""
    tool_input: JsonObject = {}
    tool_path: str = ""
    tool_result: str = ""
    cwd: str = ""
    stop_hook_active: bool = False


class LupHookOutput(BaseModel):
    """A normalized hook decision. Adapters convert it to their native output."""

    decision: LupHookDecision | None = None
    reason: str = ""
    sandbox: SandboxPlacement = "ambient"
    """Where the call runs, which is a separate answer from whether it may.

    Neutral here on purpose: an adapter whose runtime can place one call
    spells the placement in its own words, and one whose runtime cannot
    renders the decision alone rather than a placement nothing honours."""
    system_message: str | None = None
    delivery_receipt: SkipJsonSchema[Callable[[], None] | None] = Field(
        default=None, exclude=True, repr=False
    )
    updated_input: JsonObject | None = Field(
        default=None,
        description=(
            "Tool arguments to run in place of the ones the agent supplied. "
            "This is the correcting route rather than the refusing one: a "
            "hook that knows the right call can make it, instead of denying "
            "and spending a turn explaining. PreToolUse only — no other "
            "event has an input left to rewrite."
        ),
    )
    additional_context: str = Field(
        default="",
        description=(
            "Text to put in front of the agent mid-turn, without deciding "
            "anything. This is the non-cooperative delivery route: the agent "
            "calls any tool and the message is in its context, so it cannot "
            "be forgotten because the agent was never involved in receiving "
            "it."
        ),
    )

    def delivered(self) -> None:
        """Acknowledge context only after its native transport accepted it."""
        if self.delivery_receipt is not None:
            self.delivery_receipt()
            self.delivery_receipt = None


type LupHookFn = Callable[[LupHookInput], Awaitable[LupHookOutput]]
"""Async function that receives a normalized hook event and returns a decision."""


class LupHookMatcher(BaseModel, arbitrary_types_allowed=True):
    """A hook handler with an optional tool-name matcher.

    The ``tag`` field lets adapters dispatch deterministically instead
    of guessing hook intent from ``matcher`` / caller arguments.
    """

    matcher: str | None = None
    hook: LupHookFn
    tag: str | None = None
    timeout: float | None = Field(
        default=None,
        description=(
            "Seconds the runtime gives this hook before it acts without its "
            "answer; unset leaves the runtime's own default"
        ),
    )


class LupHooksConfig(BaseModel):
    """Backend-neutral hook registration — a typed structure, not a bare dict.

    One list of matchers per supported event. :func:`merge_hooks` concatenates
    two configs event-by-event; adapters iterate :meth:`by_event` to translate
    each event's matchers into their native form.
    """

    pre_tool_use: list[LupHookMatcher] = []
    post_tool_use: list[LupHookMatcher] = []
    stop: list[LupHookMatcher] = []

    def for_event(self, event: LupHookEvent) -> list[LupHookMatcher]:
        """Return the matchers registered for *event*."""
        match event:
            case "PreToolUse":
                return self.pre_tool_use
            case "PostToolUse":
                return self.post_tool_use
            case "Stop":
                return self.stop

    def by_event(self) -> dict[LupHookEvent, list[LupHookMatcher]]:
        """Each event with its matchers, in declaration order; empty events drop."""
        events: dict[LupHookEvent, list[LupHookMatcher]] = {
            "PreToolUse": self.pre_tool_use,
            "PostToolUse": self.post_tool_use,
            "Stop": self.stop,
        }
        return {event: matchers for event, matchers in events.items() if matchers}


def allow_hook(
    sandbox: SandboxPlacement = "ambient", reason: str = ""
) -> LupHookOutput:
    """Create a generic allow decision, optionally placed and optionally said.

    Nothing is added to the agent's own channel. A permitted operation that
    says something to the agent on every call is how the channel meant for
    what matters stops being read, and the one thing an agent needs to hear
    about placement — that the boundary was insufficient — is a fact about
    the operation's result rather than about its permission.
    """
    return LupHookOutput(decision="allow", sandbox=sandbox, reason=reason)


def ask_hook(reason: str, sandbox: SandboxPlacement = "ambient") -> LupHookOutput:
    """Create a decision that defers to whoever is entitled to make it.

    The reason reaches the person answering. It is not repeated onto the
    agent's own channel, which would tell the requester what the reviewer
    was told and leave it nothing to do with the information.
    """
    return LupHookOutput(decision="ask", reason=reason, sandbox=sandbox)


def deny_hook(reason: str) -> LupHookOutput:
    """Create a generic deny decision."""
    return LupHookOutput(decision="deny", reason=reason)


def block_hook(reason: str) -> LupHookOutput:
    """Create a generic block decision."""
    return LupHookOutput(decision="block", reason=reason)


def merge_hooks(base: LupHooksConfig, additional: LupHooksConfig) -> LupHooksConfig:
    """Merge two hook configurations. Base hooks run first."""
    return LupHooksConfig(
        pre_tool_use=base.pre_tool_use + additional.pre_tool_use,
        post_tool_use=base.post_tool_use + additional.post_tool_use,
        stop=base.stop + additional.stop,
    )


type NudgeCheck = Callable[[LupHookInput], str | None]
"""Given a hook event, return a nudge message or None to skip."""


def create_permission_hooks(
    rw_dirs: list[Path],
    ro_dirs: list[Path],
) -> LupHooksConfig:
    """Create permission hooks with directory-based access control.

    Controls Read/Write/Edit/Glob/Grep access based on directory permissions:
    - Write/Edit: Only allowed in rw_dirs
    - Read/Glob/Grep: Allowed in rw_dirs + ro_dirs
    - Other tools: Allowed (filtered by allowed_tools in options)

    The matched tool names (Write/Edit/Read/Glob/Grep) are the canonical
    neutral vocabulary — each adapter maps its backend's native file tools onto
    these before the hook runs, so the match reads a single spelling.

    Args:
        rw_dirs: Directories where Write/Edit/Read are allowed.
        ro_dirs: Additional directories where only Read is allowed.

    Returns:
        SDK-agnostic hooks configuration.
    """
    all_readable = rw_dirs + ro_dirs

    async def permission_hook(event: LupHookInput) -> LupHookOutput:
        if event.event != "PreToolUse":
            return LupHookOutput()

        match event.tool_name:
            case "Write" | "Edit":
                if not event.tool_path:
                    return LupHookOutput()
                if path_is_under(event.tool_path, rw_dirs):
                    return allow_hook()
                return deny_hook(
                    f"{event.tool_name} denied. Allowed: {[str(d) for d in rw_dirs]}"
                )

            case "Read":
                if not event.tool_path:
                    return LupHookOutput()
                if path_is_under(event.tool_path, all_readable):
                    return allow_hook()
                return deny_hook(
                    f"Read denied. Allowed: {[str(d) for d in all_readable]}"
                )

            case "Glob" | "Grep":
                if not event.tool_path:
                    return deny_hook(
                        f"Path required for {event.tool_name}. "
                        f"Specify path in: {[str(d) for d in all_readable]}"
                    )
                if path_is_under(event.tool_path, all_readable):
                    return allow_hook()
                return deny_hook(
                    f"{event.tool_name} denied. "
                    f"Allowed: {[str(d) for d in all_readable]}"
                )

            case _:
                return allow_hook()

    return LupHooksConfig(
        pre_tool_use=[LupHookMatcher(hook=permission_hook, tag="permission")],
    )


def create_git_inspection_hook() -> LupHooksConfig:
    """Permit workers to inspect Git while reserving mutations for orchestration."""
    # Settling the index is not mutating history: `add` and `rm` create no
    # commit and move no branch, and a worker assigned a merge cannot finish
    # one without them. History verbs stay reserved for the orchestrator.
    #
    # A refusal here is recoverable rather than terminal. A worker that meets
    # a genuine need outside this list promotes its command with
    # `# lup: escalate: <why>` exactly as the shell lattice allows, and the
    # verdict becomes an ask carrying that reason — which is what a merge
    # worker unable to stage its own resolutions had no route to.
    inspection_commands = dict.fromkeys(
        [
            "status",
            "log",
            "diff",
            "show",
            "rev-parse",
            "ls-files",
            "grep",
            # Computes an ancestor; writes nothing. A merge worker needs it to
            # tell an already-joined parent from an unmerged one, and reading
            # `merge` in the name as history mutation denied exactly that.
            "merge-base",
        ]
    )
    index_commands = dict.fromkeys(["add", "rm"])
    shell_wrappers = dict.fromkeys(["bash", "dash", "fish", "sh", "zsh"])

    def hands_off(subcommand: str, arguments: list[str]) -> str | None:
        """The option by which an inspection verb runs a program or writes a file.

        Two do, and both ride on verbs this hook lets through: `git grep -O
        <program>` (`--open-files-in-pager`) runs a program over the files it
        matched, and `--output=<file>` makes `diff`, `log` and `show` write
        their report into a file. A long option is caught by any prefix git
        would take for it, since git accepts an unambiguous abbreviation, and
        `-O` inside a cluster as well as alone.
        """

        def spells(word: str, option: str) -> bool:
            """Whether git reads this word as that long option."""
            return any(
                word == prefix or word.startswith(f"{prefix}=")
                for prefix in accumulate(option)
                if len(prefix) > 2
            )

        for word in arguments:
            if word == "--":
                return None
            if spells(word, "--output"):
                return word
            if subcommand == "grep" and (
                spells(word, "--open-files-in-pager")
                or (not word.startswith("--") and word.startswith("-") and "O" in word)
            ):
                return word
        return None

    async def git_inspection_hook(event: LupHookInput) -> LupHookOutput:
        if event.event != "PreToolUse":
            return LupHookOutput()
        if (
            event.tool_name in {"Edit", "Write"}
            and ".git" in Path(event.tool_path).parts
        ):
            return deny_hook("resolver workers cannot edit Git metadata")
        if event.tool_name != "Bash":
            return LupHookOutput()
        match event.tool_input:
            case {"command": str(command)}:
                from lup.policy.kernel.shell import ESCALATE_RE
                from lup.policy.kernel.words import command_words
                from lup.policy.rules import parse_shell_segments

                escalation = ESCALATE_RE.match(command)
                if escalation is not None and escalation.group("why").strip():
                    return ask_hook(escalation.group("why").strip())
                segments = parse_shell_segments(command)
                if segments is None:
                    return deny_hook(
                        "resolver workers cannot use opaque shell composition"
                    )
                for segment in segments:
                    words = command_words(segment.words)
                    if not words:
                        return deny_hook("resolver worker command is empty")
                    executable = Path(words[0]).name
                    if executable in shell_wrappers:
                        return deny_hook(
                            "resolver workers cannot hide commands in a shell wrapper"
                        )
                    if executable == "git" and (
                        len(words) < 2
                        or words[1] not in {**inspection_commands, **index_commands}
                    ):
                        return deny_hook(
                            "resolver workers may inspect Git and settle its "
                            "index, but cannot mutate history"
                        )
                    handed = (
                        hands_off(words[1], words[2:]) if executable == "git" else None
                    )
                    if handed is not None:
                        return deny_hook(
                            f"resolver workers may inspect Git, but `{handed}`"
                            " runs a program or writes a file"
                        )
                return allow_hook()
            case _:
                return deny_hook("resolver worker shell command is missing")

    return LupHooksConfig(
        pre_tool_use=[LupHookMatcher(hook=git_inspection_hook, tag="git-inspection")]
    )


def create_tool_allowlist_hook(
    allowed_tools: list[ToolName],
) -> LupHooksConfig:
    """Create a PreToolUse hook that restricts the agent to an allowed tool set.

    **What:** Denies every tool call whose name is not in *allowed_tools*,
    answering with the full list of tools that ARE available so the agent
    can re-plan instead of retrying blindly. Allowed tools get an explicit
    allow decision.

    **When:** Use whenever ``permission_mode="bypassPermissions"`` is set —
    the SDK's ``allowed_tools`` option is ignored in that mode, so a
    PreToolUse hook is the only enforcement point.

    **Why:** Makes tool availability a structural guarantee instead of a
    prompt rule: excluded tools cannot run, and the denial message turns a
    dead end into a redirect.
    """
    allowed = frozenset(allowed_tools)  # lup: ignore[frozenset-shape] — membership
    available = ", ".join(sorted(allowed))

    async def allowlist_hook(event: LupHookInput) -> LupHookOutput:
        if event.event != "PreToolUse":
            return LupHookOutput()

        if event.tool_name in allowed:
            return allow_hook()
        return deny_hook(
            f"Tool '{event.tool_name}' is not available in this session. "
            f"Available tools: {available}"
        )

    return LupHooksConfig(
        pre_tool_use=[LupHookMatcher(hook=allowlist_hook, tag="allowlist")],
    )


def create_large_read_hook(default_limit: int = 2000) -> LupHooksConfig:
    """Create a PreToolUse hook that bounds a Read that named no bound.

    **What:** A ``Read`` call carrying no ``limit`` is rewritten to carry
    *default_limit*, so a call the agent meant as "open this file" cannot
    come back as an entire one.

    **When:** Use in any session where the agent reads files it did not
    write — logs, fetched pages, spilled tool output — and therefore cannot
    know a file's size before asking for it.

    **Why:** An unbounded Read is the one call that can exhaust a context
    window in a single step, and no amount of care lets the agent avoid it,
    because the size it needed to know is what the call returns. Correcting
    the arguments keeps the read working; denying it would spend a turn
    teaching a parameter that the next unfamiliar file omits again.

    Args:
        default_limit: Line count injected into a Read that omits one.

    Returns:
        Hooks configuration with a PreToolUse read-limit hook.
    """

    async def read_limit_hook(event: LupHookInput) -> LupHookOutput:
        if event.event != "PreToolUse":
            return LupHookOutput()

        if event.tool_name != "Read":
            return LupHookOutput()

        if "limit" in event.tool_input:
            return LupHookOutput()

        return LupHookOutput(updated_input={**event.tool_input, "limit": default_limit})

    return LupHooksConfig(
        pre_tool_use=[LupHookMatcher(hook=read_limit_hook, tag="read-limit")],
    )


def create_nudge_hook(
    nudges: dict[ToolName, NudgeCheck],
) -> LupHooksConfig:
    """Create a PostToolUse hook that nudges the agent toward better alternatives.

    **What:** After a tool in *nudges* runs, calls its check function with
    the full hook input; when the check returns a message, that message is
    injected as a system message. The tool result itself is untouched, and
    the agent remains free to ignore the nudge.

    **When:** Use when an alternative tool or approach exists but a
    PreToolUse denial would be too restrictive — the original tool still
    works, just suboptimally (e.g. WebFetch on a site with a structured
    API, or Bash where a dedicated tool exists). For hard constraints,
    use :func:`create_tool_gate` instead.

    **Why:** Mid-turn guidance lands where prompt rules don't: the nudge
    arrives exactly at the suboptimal call, with the call's own context,
    instead of being a standing instruction the agent must remember.

    Args:
        nudges: Mapping of tool_name to a check function. The check receives
            the hook input and returns a nudge message string, or None to skip.

    Returns:
        SDK-agnostic hooks configuration with a PostToolUse nudge hook.
    """

    async def nudge_hook(event: LupHookInput) -> LupHookOutput:
        if event.event != "PostToolUse":
            return LupHookOutput()

        if event.tool_name not in nudges:
            return LupHookOutput()

        message = nudges[event.tool_name](event)
        if message is None:
            return LupHookOutput()

        return LupHookOutput(system_message=message)

    return LupHooksConfig(
        post_tool_use=[LupHookMatcher(hook=nudge_hook, tag="nudge")],
    )


class CaptureHook[T](TypedDict):
    """A capture hook and the list it appends to as responses arrive.

    A ``TypedDict`` rather than a model because ``captured`` is live: pydantic
    would revalidate it into a copy, and the caller would then watch a list
    nothing writes to.
    """

    hooks: LupHooksConfig
    captured: list[T]


def create_capture_hook[T](
    tool_name: ToolName,
    extract: Callable[[LupHookInput], list[T]],
) -> CaptureHook[T]:
    """Create a PostToolUse hook that captures data from tool responses.

    Extracts data from a sub-agent's tool responses into a shared list.

    Args:
        tool_name: The tool name to capture from (e.g., "WebSearch").
        extract: Function that examines the hook input and returns items to capture.

    Returns:
        A `CaptureHook` carrying the hook config and the shared accumulator list.
    """
    captured: list[T] = []

    async def capture_hook(event: LupHookInput) -> LupHookOutput:
        if event.event != "PostToolUse":
            return LupHookOutput()
        if event.tool_name != tool_name:
            return LupHookOutput()

        items = extract(event)
        captured.extend(items)
        return LupHookOutput()

    return CaptureHook(
        hooks=LupHooksConfig(
            post_tool_use=[LupHookMatcher(hook=capture_hook, tag="capture")]
        ),
        captured=captured,
    )


def create_tool_gate(
    *,
    gated_tool: ToolName | Sequence[ToolName] | None = None,
    message: str | Callable[[], str],
    unlocked: Callable[[LupHookInput], bool] | None = None,
    on_unlock_tool: ToolName | None = None,
    event: Literal["PreToolUse", "Stop"] = "PreToolUse",
    style: Literal["deny", "block"] = "deny",
    allow_when_unlocked: bool = False,
    tag: str = "tool_gate",
) -> LupHooksConfig:
    """Create a hook that denies a tool (or Stop) until a condition unlocks it.

    **What:** Registers a hook on *event* that answers with an
    agent-readable denial *message* while the gate is locked, and passes
    through (or explicitly allows) once it is unlocked. The gate is
    unlocked when ``unlocked(input)`` returns True, or — with
    *on_unlock_tool* — once that tool has run (tracked via an internal
    PostToolUse hook).

    **When:** Reach for this whenever the agent must do A before it may
    do B: reflect before finalizing output, read pending events before
    sleeping, call sleep instead of ending the turn. Presets built on
    this primitive: :func:`lup.orchestration.reflection.create_reflection_gate`,
    :func:`lup.orchestration.realtime.scheduler.create_stop_guard`,
    :func:`lup.orchestration.realtime.scheduler.create_pending_event_guard`, and
    :func:`lup.orchestration.realtime.scheduler.create_meta_before_sleep_guard`.

    **Why:** The denial message is the one channel that reliably
    redirects the agent mid-turn — it states what to do instead, making
    the workflow constraint structural rather than a prompt rule the
    agent can skip.

    Args:
        gated_tool: Tool name(s) to gate. Required for
            ``event="PreToolUse"``; ignored for ``event="Stop"``.
        message: Denial text shown to the agent, or a zero-argument
            callable evaluated at denial time (for dynamic state such as
            unread-event counts).
        unlocked: Predicate over the raw hook input. Return True to let
            the call through. Receiving the input lets gates honor
            per-call escape hatches (a ``force`` flag in ``tool_input``)
            or event fields (``stop_hook_active``).
        on_unlock_tool: Tool name whose use unlocks the gate. Adds a
            PostToolUse hook that records the call; combined with
            *unlocked* via OR. The internal flag never resets — for
            per-cycle gates, track the state yourself and pass *unlocked*.
        event: Hook event to gate: ``"PreToolUse"`` (default) or ``"Stop"``.
        style: Locked response shape. ``"deny"`` uses the permission
            decision; ``"block"`` uses the cross-event block decision
            (required for Stop). A PreToolUse gate refuses on the
            permission channel either way — that is the only channel the
            event reads — so the two differ for Stop alone.
        allow_when_unlocked: When True, return an explicit allow decision
            once unlocked instead of passing through to later hooks
            (PreToolUse only).
        tag: Matcher tag for adapter dispatch. Subprocess backends cannot
            run the in-process ``unlocked`` closure; they regenerate known
            gates as external hook scripts by tag, so presets with a
            file-representable condition pass their own
            (e.g. ``"reflection_gate"``).

    Returns:
        SDK-agnostic hooks configuration; combine via ``merge_hooks``.
    """
    if unlocked is None and on_unlock_tool is None:
        raise ValueError("create_tool_gate requires unlocked and/or on_unlock_tool")
    if event == "PreToolUse" and gated_tool is None:
        raise ValueError("create_tool_gate requires gated_tool for PreToolUse gates")

    unlock_seen = False

    async def gate_hook(input_data: LupHookInput) -> LupHookOutput:
        if input_data.event != event:
            return LupHookOutput()
        if unlock_seen or (unlocked is not None and unlocked(input_data)):
            if allow_when_unlocked and event == "PreToolUse":
                return allow_hook()
            return LupHookOutput()
        text = message() if callable(message) else message
        match style:
            case "deny":
                return deny_hook(text)
            case "block":
                return block_hook(text)

    async def unlock_hook(input_data: LupHookInput) -> LupHookOutput:
        nonlocal unlock_seen
        if input_data.event == "PostToolUse":
            unlock_seen = True
        return LupHookOutput()

    hooks: LupHooksConfig
    match event:
        case "Stop":
            hooks = LupHooksConfig(stop=[LupHookMatcher(hook=gate_hook, tag=tag)])
        case "PreToolUse":
            names = (
                [gated_tool] if isinstance(gated_tool, str) else list(gated_tool or [])
            )
            hooks = LupHooksConfig(
                pre_tool_use=[
                    LupHookMatcher(matcher=name, hook=gate_hook, tag=tag)
                    for name in names
                ]
            )

    if on_unlock_tool is not None:
        unlock_config = LupHooksConfig(
            post_tool_use=[
                LupHookMatcher(
                    matcher=on_unlock_tool, hook=unlock_hook, tag=f"{tag}_unlock"
                )
            ]
        )
        hooks = merge_hooks(hooks, unlock_config)
    return hooks


def create_completion_guard(
    output_exists: Callable[[], bool],
    *,
    output_tool_name: ToolName = "mcp__notes__submit_output",
    max_blocks: int = 3,
) -> LupHooksConfig:
    """Create a Stop hook that blocks finishing until output is submitted.

    Output submission happens through a turn-bound tool, so a
    backend's native finalization does not guarantee a result exists. On
    backends with a stop event, this hook pushes the agent back with a
    corrective message when it tries to finish without submitting.

    After ``max_blocks`` consecutive blocks the stop is allowed through —
    a confused agent must not loop forever. The orchestration layer then
    sees the missing output file and surfaces the failure.

    Args:
        output_exists: Returns True once the final output has been submitted.
        output_tool_name: Tool named in the corrective message.
        max_blocks: Consecutive blocks before giving up.

    Returns:
        SDK-agnostic hooks configuration with a Stop hook.
    """
    blocks = 0

    async def completion_guard_hook(input_data: LupHookInput) -> LupHookOutput:
        nonlocal blocks
        if input_data.event != "Stop":
            return LupHookOutput()
        if output_exists():
            return LupHookOutput()
        if blocks >= max_blocks:
            return LupHookOutput()
        blocks += 1
        return block_hook(
            f"No final output has been submitted. Call {output_tool_name} "
            f"with your structured output before finishing. "
            f"(attempt {blocks}/{max_blocks})"
        )

    return LupHooksConfig(
        stop=[LupHookMatcher(hook=completion_guard_hook, tag="completion_guard")],
    )
