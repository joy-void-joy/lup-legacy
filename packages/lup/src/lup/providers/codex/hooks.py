# lup: ignore[constant-declaration]
# Every constant here is a Codex app-server method or reply word, so the wire
# decides the value and a caller passing another would answer a request the
# server never sends.
"""Translate backend-neutral Lup hooks to Codex app-server approval replies.

The Claude twin of this module wraps SDK hook handlers. Codex has no such
registry: the seam it offers is the approval request the app-server sends
*back* to whoever opened the thread, once that thread was started under a
policy that asks. So a portable hook becomes a reply to a server-initiated
request, and the transport in :mod:`lup.providers.codex.app_server` already
routes every such request to one installed handler.

What each boundary carries decides what can be judged there, and the two
approval requests are not alike:

``item/commandExecution/requestApproval``
    carries the command and its working directory, so a shell rule is judged
    on exactly the text about to run. This is parity with the Claude path.

``item/fileChange/requestApproval``
    carries an item id, a thread and turn id, a timestamp, an optional reason
    and an optional ``grantRoot`` — and no path, no content, and no diff. Read
    off ``FileChangeRequestApprovalParams`` as codex-cli 0.155.1 generates it;
    ``grantRoot`` is the agent asking to write under a root for the rest of
    the session, not the root of the patch. An edit rule reads before-and-after
    text, so there is nothing here for one to read. The request is therefore
    decoded as the opaque operation it is and answered by whatever the policy
    does with an unknown tool, which for every policy this library ships is a
    refusal. Approving it because the content could not be inspected would be
    the one reading that turns a missing capability into a silent grant.

That is a property of *this* boundary and not of the protocol. The legacy
``applyPatchApproval`` carries ``fileChanges`` as a map from path to change,
where an add or a delete carries the whole ``content`` and an update carries a
``unified_diff`` — everything an edit rule wants — and clients are sent
``turn/diff/updated`` besides. So the content a Codex reviewer would need is
reachable, on boundaries this seam does not answer, and a future that judges a
file change on its text starts there rather than from the v2 request.
"""

import asyncio
import logging
import re  # lup: ignore[import-re] — native hook matchers are explicitly regexes
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from lup.providers.codex.native import (
    CodexBeforeToolEvent,
    CodexEventDecoder,
    CodexShellOperation,
    CodexUnknownOperation,
)
from lup.policy.hooks import LupHookInput, LupHookOutput, LupHooksConfig
from lup.policy.enforcement import NativeSemantics
from lup.policy.models import SemanticTool
from lup.types import JsonObject
from lup.sessions.errors import UnsupportedCapability

logger = logging.getLogger(__name__)

COMMAND_APPROVAL = "item/commandExecution/requestApproval"
"""The app-server request carrying a command about to run."""

FILE_CHANGE_APPROVAL = "item/fileChange/requestApproval"
"""The app-server request carrying a patch about to apply, minus its content."""

APPROVAL_METHODS = (COMMAND_APPROVAL, FILE_CHANGE_APPROVAL)
"""Every server request this seam answers."""


def codex_hook_approval_policy(
    hooks: LupHooksConfig | None,
) -> Literal["never", "on-request"]:
    """Permit lifecycle observers and explicitly scoped native approval hooks.

    An exact native method names the boundary its callback agrees to observe.
    A wildcard or a portable tool name instead asks for coverage this channel
    cannot provide. Mailbox observers are a separate tagged delivery contract;
    their neutral output never grants approval.
    """
    approvals = [] if hooks is None else hooks.pre_tool_use
    declared = {*APPROVAL_METHODS, "|".join(APPROVAL_METHODS)}
    policy: Literal["never", "on-request"] = "never"
    for matcher in approvals:
        if matcher.tag == "mailbox" and matcher.matcher in {None, "", "*"}:
            continue
        if matcher.matcher not in declared:
            raise UnsupportedCapability(
                "Codex PreToolUse hooks require an explicit native approval scope: "
                + ", ".join(APPROVAL_METHODS)
                + ". Universal pre-execution interception requires the generated policy dispatcher."
            )
        policy = "on-request"
    return policy


ACCEPT = "accept"
DECLINE = "decline"
"""The two decisions this seam ever returns.

``acceptForSession`` is deliberately never returned: it would grant every
later request of the same shape without judging it, which is the opposite of
what a per-call policy is for. ``cancel`` belongs to a human abandoning a
turn, and nothing here is a human.
"""


class CodexCommandApproval(BaseModel, frozen=True, extra="ignore"):
    """The fields of a command-execution approval this seam reads.

    Codex sends more than these — item and turn identifiers, the decisions it
    would accept, execpolicy amendment offers. They are ignored rather than
    modelled, because a policy judges the act and not the bookkeeping around
    it, and an unmodelled field cannot break validation when Codex adds one.
    """

    command: str = ""
    cwd: Path | None = None


def codex_approval_event(method: str, params: JsonObject) -> CodexBeforeToolEvent:
    """Decode one approval request into the operation it asks about.

    A command approval becomes the shell operation it names. Anything else — a
    file change, whose content this boundary does not carry, or a method this
    library has never seen — becomes the opaque operation it is, so the
    policy's unknown-tool arm answers for it rather than this function
    inventing a shape nobody sent.
    """
    if method == COMMAND_APPROVAL:
        approval = CodexCommandApproval.model_validate(params)
        return CodexBeforeToolEvent(
            operation=CodexShellOperation(command=approval.command, cwd=approval.cwd)
        )
    return CodexBeforeToolEvent(
        operation=CodexUnknownOperation(name=method, input=params)
    )


def codex_approval_semantic_tool(event: LupHookInput) -> SemanticTool:
    """Decode one approval request into the tool a semantic policy judges."""
    return (
        CodexEventDecoder()
        .decode(codex_approval_event(event.tool_name, event.tool_input))
        .tool
    )


CODEX_SEMANTICS = NativeSemantics(
    decode=codex_approval_semantic_tool,
    routed_tools=list(APPROVAL_METHODS),
)
"""What an in-process Codex session hands a semantic policy.

The routed set is the approval methods themselves, because that is the whole
vocabulary this boundary speaks: unlike the Claude hook, which sees a tool
roster, the app-server asks about acts.

That is also why the two sandbox facts split here. An approval reply accepts
or declines and rewrites nothing, so no verdict of this seam's places a call
and ``escapable`` stays false. The agent's own escape is a different matter
and it has one: Codex puts ``sandbox_permissions`` on the shell tool the
model calls — see :meth:`~lup.providers.codex.harness.CodexSpellings.escape_sandbox`
for the source it was read from.
"""


type ContextDelivery = Callable[[str], Awaitable[None]]
"""Deliver text to the active turn, raising unless its transport accepted it."""


class CodexApprovalResponder(BaseModel, frozen=True, arbitrary_types_allowed=True):
    """Answer app-server approval requests from portable hook registrations.

    One of these exists per conversation, because the transport installs one
    handler for the whole session and a session's hooks are fixed when it
    opens.
    """

    hooks: LupHooksConfig = Field(description="What this session declared")
    deliver_context: ContextDelivery | None = None
    delivery_lock: asyncio.Lock = Field(default_factory=asyncio.Lock)

    def handles(self, method: str) -> bool:
        """Whether an approval request is one this responder answers."""
        return method in APPROVAL_METHODS

    def hook_input(self, method: str, params: JsonObject) -> LupHookInput:
        """Present one approval request in the portable hook vocabulary."""
        approval = (
            CodexCommandApproval.model_validate(params)
            if method == COMMAND_APPROVAL
            else None
        )
        return LupHookInput(
            event="PreToolUse",
            tool_name=method,
            tool_input=params,
            tool_path=str(approval.cwd) if approval and approval.cwd else "",
        )

    def context(self, outputs: list[LupHookOutput]) -> str:
        """The complete model-facing feedback from one batch of hooks."""
        messages = [
            text
            for output in outputs
            for text in (
                output.reason if output.decision in {"deny", "block", "ask"} else "",
                output.additional_context,
                output.system_message or "",
            )
            if text
        ]
        return "\n\n".join(messages)

    async def deliver(self, outputs: list[LupHookOutput]) -> bool:
        messages = self.context(outputs)
        if not messages:
            return True
        if self.deliver_context is None:
            return False
        try:
            await self.deliver_context(messages)
        except Exception:
            logger.exception(
                "Codex did not accept hook context; delivery stays pending"
            )
            return False
        for output in outputs:
            output.delivered()
        return True

    async def evaluate(self, event: LupHookInput) -> list[LupHookOutput]:
        return [
            await matcher.hook(event)
            for matcher in self.hooks.for_event(event.event)
            if event.event == "Stop"
            or matcher.matcher in {None, "", "*"}
            # lup: ignore[re-call] — native matcher language
            or re.search(matcher.matcher or "", event.tool_name) is not None
        ]

    async def deliver_pending(self) -> None:
        """Deliver actor mail at native activity that asks for no approval."""
        async with self.delivery_lock:
            outputs = [
                await matcher.hook(LupHookInput(event="PreToolUse"))
                for matcher in self.hooks.pre_tool_use
                if matcher.tag == "mailbox" and matcher.matcher in {None, "", "*"}
            ]
            await self.deliver(outputs)

    async def decide(self, method: str, params: JsonObject) -> str:
        async with self.delivery_lock:
            return await self.decide_exclusive(method, params)

    async def decide_exclusive(self, method: str, params: JsonObject) -> str:
        """Run every registered PreToolUse hook and reply with one decision.

        An ``ask`` reaching here has nobody to ask: the app-server is a
        program and this session was opened without a human attached, so it
        declines rather than approving something whose approval was never
        given. That is the same fail-closed reading the generated Codex
        dispatcher takes, reached the same way.

        The approval reply carries only a decision. Context and refusal
        reasons reach the active turn through its steering transport, and
        durable mailbox receipts advance only once that transport accepts them.
        """
        if not self.handles(method):
            return DECLINE
        matchers = [
            matcher
            for matcher in self.hooks.pre_tool_use
            if matcher.matcher in {None, "", "*"}
            # lup: ignore[re-call] — native matcher language
            or re.search(matcher.matcher or "", method) is not None
        ]
        outputs = [
            await matcher.hook(self.hook_input(method, params)) for matcher in matchers
        ]
        if not outputs:
            return DECLINE
        for output in outputs:
            if output.updated_input is not None:
                output.decision = "deny"
                output.reason = (output.reason + "\n" if output.reason else "") + (
                    "Codex app-server approvals cannot rewrite tool input; the original call is declined."
                )
        refused = [
            item for item in outputs if item.decision in ("deny", "block", "ask")
        ]
        for item in refused:
            # A question declined here is turned back to the agent, so the
            # recovery that rode beside it belongs in the record too.
            told = [text for text in (item.reason, item.additional_context) if text]
            logger.info("declining %s: %s", method, "\n".join(told))
        delivered = await self.deliver(outputs)
        authorized = any(
            output.decision == "allow" and matcher.tag != "mailbox"
            for matcher, output in zip(matchers, outputs, strict=True)
        )
        return DECLINE if refused or not delivered or not authorized else ACCEPT


type ApprovalHandler = Callable[[str, JsonObject], Awaitable[str]]


def build_codex_approval_handler(hooks: LupHooksConfig) -> ApprovalHandler:
    """Close one approval responder over a session's portable hooks."""
    responder = CodexApprovalResponder(hooks=hooks)

    async def respond(method: str, params: JsonObject) -> str:
        return await responder.decide(method, params)

    return respond
