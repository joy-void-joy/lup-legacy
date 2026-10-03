"""The seam that turns a policy verdict into a live session's refusal.

The policies in :mod:`lup.policy.rules` answer with a
:class:`~lup.policy.models.Decision`; a session refuses through the portable
hooks of :mod:`lup.policy.hooks`. The mapping between them is not a choice a caller
should make twice — ``ask`` must reach a human and ``defer`` must reach
nobody, and a composition that guesses either one wrong turns a gate into a
silent grant. So it is decided here, once, and pinned by tests.

Generated plugins enforce the same policies from a subprocess dispatcher
assembled by :mod:`lup.policy.bundle`. This is the in-process counterpart:
same policies, same verdicts, delivered through the hook seam a session
already carries.
"""

import asyncio
import logging
from collections.abc import Callable

from pydantic import BaseModel, Field

from lup.policy.assets.host import ran_out, unjudged_reason
from lup.policy.kernel.decision import unjudged_recovery
from lup.policy.bundle import hook_deadline
from lup.policy.hooks import (
    LupHookInput,
    LupHookMatcher,
    LupHookOutput,
    LupHooksConfig,
    allow_hook,
    ask_hook,
    deny_hook,
)
from lup.policy.chain import UnknownToolPolicy
from lup.policy.contracts import DeclaredPolicies, DecisionPolicy
from lup.policy.refused_tools import RefusedTool, routed_for
from lup.policy.models import (
    Decision,
    EditBatch,
    FetchUrl,
    SemanticTool,
    ShellCommand,
)

logger = logging.getLogger(__name__)


type EscalationRelay = Callable[[str, str], None]
"""Where an escalation goes on a host that has no human to put it to.

Takes the agent's stated reason and the refusal it was answering. A relay
never grants: a host with nobody to ask cannot approve, and a marker that
resolved to a run would be the instrument for summoning a human turned into
the instrument for bypassing the table. What it does is make the request
arrive somewhere a person will read it, so the agent stops being the only
party that knows it is stuck.
"""

# lup: ignore[constant-declaration] — refusal wording, declared beside the
# verdict that returns it: these words are a true statement about what this
# arm just did, so a caller free to reword them is a caller free to tell an
# agent its request was relayed when it was not
RELAYED_NOTICE = (
    "\n\nThis has been relayed to whoever is supervising this run, with your "
    "stated reason. Nobody will answer it here — carry on with whatever does "
    "not depend on it. Raise a question only if you genuinely cannot proceed."
)


def policy_hook_output(
    decision: Decision,
    escapable: bool = False,
    relay: EscalationRelay | None = None,
    contained: bool = False,
) -> LupHookOutput:
    """Render one policy verdict as the portable hook decision.

    ``defer`` carries no decision at all: the kernel declined to judge, so
    the session's ambient permission flow applies rather than this hook
    granting what nothing approved.

    ``escapable`` is whether the runtime this reaches can put a single call
    outside the containment boundary at all. The composition happens here
    rather than at each adapter, so what a converter receives is already the
    placement its own runtime will honour — and a runtime with no such
    channel is handed the plain effect instead of an intent it would
    silently drop.

    ``contained`` is the session's measured placement, which decides what an
    approved crossing is described as — the host where the per-call sandbox
    is the only boundary, the container's own mounts where one is between.
    Composed here for the same reason ``escapable`` is: the kernel measures
    nothing, and a renderer that guessed would describe a crossing the
    session does not make.

    The two audiences get their own text. A question's reason is what the
    approver reads, so the recovery rides beside it as context for the agent;
    a refusal reaches the agent alone, so it carries both.
    """
    decision = decision.placed(escapable, contained=contained)
    match decision.effect:
        case "allow":
            return allow_hook(decision.sandbox, decision.as_kernel().headline())
        case "ask":
            kernel = decision.as_kernel()
            return ask_hook(kernel.headline(), decision.sandbox).model_copy(
                update={"additional_context": kernel.beside()}
            )
        case "deny" if decision.escalated and relay is not None:
            # The refusal stands — nothing here can approve what no human
            # saw. What changes is that the request reaches somebody. A
            # marker denied in silence made the documented escape hatch inert
            # in exactly the session that has no other route, so a worker
            # blocked on a stray temp file had to park a human question over
            # housekeeping, or give up.
            relay(decision.escalated, decision.reason)
            return deny_hook(decision.as_kernel().addressed() + RELAYED_NOTICE)
        case "deny":
            return deny_hook(decision.as_kernel().addressed())
        case "defer":
            return LupHookOutput(reason=decision.reason)


def unjudged_output(error: BaseException | None) -> LupHookOutput:
    """The refusal of a call nobody judged, as every runtime's hook gives it.

    ``error`` is what failed, or None where the judgement was still running
    when the hook had to answer. The same words the generated dispatchers
    refuse with, so an in-process session is told what a launched one is.
    A runtime proceeds past a callback that raised -- the Agent SDK runs the
    tool, measured on Claude Code 2.1.259 and 2.1.285 -- so a gate that
    failed has to answer with this rather than fail.
    """
    return policy_hook_output(
        Decision(
            effect="deny",
            reason=unjudged_reason(error, True),
            recovery=unjudged_recovery(ran_out(error)),
        )
    )


class SemanticToolPolicy(DecisionPolicy[SemanticTool]):
    """Hold the declared policies and let each tool find its own.

    The tool answers rather than this class deciding, so a new kind of tool
    is a class beside the others rather than an arm here that a reader has
    to remember to add. What that costs a composition is stated where the
    tools implement it: an undeclared family asks rather than allows, and
    search and unclassified tools have no rule surface at all.
    """

    def __init__(
        self,
        *,
        fetch: DecisionPolicy[FetchUrl] | None = None,
        shell: DecisionPolicy[ShellCommand] | None = None,
        edit: DecisionPolicy[EditBatch] | None = None,
        refused_tools: list[RefusedTool] | None = None,
    ) -> None:
        self.policies = DeclaredPolicies(
            unknown=UnknownToolPolicy(refused_tools),
            fetch=fetch,
            shell=shell,
            edit=edit,
        )

    def decide(self, event: SemanticTool) -> Decision:
        return event.decide_under(self.policies)


type SemanticDecoder = Callable[[LupHookInput], SemanticTool]
"""Decode one native tool call into the semantic tool a policy judges.

Native tool names and payload fields are adapter knowledge, so the
composition root supplies this — ``lup.providers.claude.hooks`` has the
decoder for Claude sessions."""


class SandboxPosture(BaseModel, frozen=True):
    """What one session's own sandbox configuration means to the policy.

    Read from the configuration a session is opened with, never from the
    runtime it opens on. Those answer different questions: a runtime says
    whether a per-call escape channel exists at all, and only the session
    says whether it is open here. A policy handed the runtime's answer
    judges a host it does not have — a worker configured to forbid
    unsandboxed commands would be told it could escape, so every placement
    would be rendered onto the wire and dropped, leaving the call confined
    with the verdict unchanged and nothing anywhere saying so.

    Both fields default to the shape that claims least: a session that says
    nothing about its sandbox is judged as confining nothing and escaping
    nowhere.
    """

    active: bool = False
    """Whether an OS sandbox is asked to confine what this session runs.

    Asserted by configuration rather than observed, and the two come apart:
    a runtime whose sandbox cannot start on the host — a missing dependency,
    an unsupported platform — warns and runs the session anyway, so a request
    read back as an answer describes a boundary that may not be there. A
    runtime offering a fail-if-unavailable setting closes that at the source,
    and a session opened with one earns this rather than claiming it.

    Which is why it describes the session without deciding anything about it.
    A caller that spends this on the kernel's confined-host behaviour is
    buying a substitution as well: there, an unanswerable question rides the
    OS boundary instead of failing closed, so a host with no way to reach a
    human converts every guarded verdict into a run. Worth taking where the
    boundary is established and the questions can be asked; not something to
    infer from a session having set a flag."""

    escapable: bool = False
    """Whether this session may place one call outside that sandbox."""

    contained: bool = False
    """Whether the boundary this session was opened inside is a container.

    The profile's declaration rather than the runtime's sandbox, and it
    decides what an approved sandbox escalation is described as: on a host the
    per-call escape leaves the only boundary there is, while inside a
    container it lifts the per-call sandbox alone and the call lands in the
    same mount namespace, which the question has to say. Unstated reads as
    uncontained, the same answer the compiled dispatchers reach from a launch
    ledger that measured nothing."""


class NativeSemantics(BaseModel, frozen=True):
    """One runtime's call decoder together with the tools it has rules for.

    The two are one fact, and a caller asked for them separately can supply
    a decoder with no routed set. That mismatch is silent and inverts the
    enforcement: registered against nothing, the hook judges every call, and
    a tool this vocabulary cannot classify reaches the conservative ``ask``
    that then outranks an ``allow`` a directory ACL beside it granted. An
    adapter exports the pair it means, and an empty routed set — the shape
    that disables enforcement while looking like configuration — is refused
    at construction.
    """

    decode: SemanticDecoder
    routed_tools: list[str] = Field(min_length=1)
    escapable: bool = False
    """Whether a verdict from this seam can put one call outside the sandbox.

    An adapter fact rather than a policy one, so it arrives with the decoder
    and the routed set. Left false, a placement never reaches the wire — the
    conservative direction, and the right one for a seam that answers with a
    verdict alone and never rewrites the call it judges."""
    spawn_tools: list[str] = []
    """This runtime's spellings of a spawn, as its hooks name them.

    An adapter fact the policy needs on every path: a dispatcher's spawn
    branch asks the refusal table before judging the name, so a refusal of a
    spawn is in force rather than unreachable; and a seam that never sees a
    spawn is told by :meth:`spawning_refused` that its session should open
    without the tools that make one."""

    def also_refusing(self, refused: list[RefusedTool]) -> "NativeSemantics":
        """The same decoder, registered for the refused tools as well.

        A refusal reaches nothing it was not routed for, and the plugin widens
        its matcher from the same declaration — so an in-process session that
        took the adapter's pairing unchanged would enforce strictly less than
        the generated tree beside it.
        """
        return NativeSemantics(
            decode=self.decode,
            routed_tools=routed_for(self.routed_tools, refused, self.spawn_tools),
            escapable=self.escapable,
            spawn_tools=self.spawn_tools,
        )

    def spawning_refused(self, refused: list[RefusedTool]) -> bool:
        """Whether *refused* turns down a spawn outright, in any of this runtime's spellings.

        Outright meaning the whole tool: a refusal naming one subject of a
        spawn leaves the others to go out, so whatever makes them stays.
        """
        return any(
            rule.tool in self.spawn_tools and not rule.specifier for rule in refused
        )

    def escapes_from(self, sandbox: SandboxPosture) -> bool:
        """Whether one call of this session can actually reach outside it.

        Both halves have to hold and each answers its own question: the
        runtime supplies the channel, the session opens it. Composed here so
        that no caller has to remember a placement needs both — the one that
        remembered only the runtime is what put an escape on the wire for a
        session that would drop it.
        """
        return self.escapable and sandbox.escapable


# A composition root over policy and semantics together; on either one it would
# be that half constructing hooks out of the other.
def create_policy_hooks(
    policy: DecisionPolicy[SemanticTool],
    semantics: NativeSemantics,
    *,
    sandbox: SandboxPosture = SandboxPosture(),
    tag: str = "semantic_policy",
    relay: EscalationRelay | None = None,
    timeout: float | None = None,
) -> LupHooksConfig:
    """Create a PreToolUse hook that enforces *policy* on the tools it judges.

    **What:** Decodes each attempted call into its semantic tool, asks
    *policy* for a verdict, and answers with the portable decision that
    verdict maps to — so a denied call never runs and an ``ask`` reaches a
    human instead of the model.

    **When:** Use for a session composed in this process. Generated native
    plugins enforce the same policies from their own dispatcher; this is
    for the session an application builds and runs itself.

    **Why:** A verdict a caller only prints is documentation. Registered
    here it is the session's answer, produced by the same policy objects
    the generated dispatchers erase into rows.

    **Why scoped:** the routed set carried by *semantics* is what keeps this
    the same enforcement the plugin performs rather than a stricter one.
    Unregistered elsewhere, this hook sees every call, and a tool with no
    rule surface reaches :class:`UnknownToolPolicy`, whose conservative
    ``ask`` then outranks an ``allow`` a directory ACL beside it already
    granted — so composing this with an ACL denies the reads that ACL exists
    to permit.

    Args:
        policy: The judge for one decoded tool, typically a
            :class:`SemanticToolPolicy` over the fetch, shell, and edit rules.
        semantics: The adapter's decoder and the native tool names this
            policy has rules for, as the adapter exports them.
        sandbox: What the configuration this session is opened with confines
            and permits. Left unstated, no placement reaches the wire, which
            is the right answer for a session that declared no sandbox and
            the safe one for a session that declared one and forgot to say.
        tag: Matcher tag for adapter dispatch.
        timeout: What the runtime gives this hook before it acts without
            it, the declaration's ``policy_timeout``. The runtime is told it,
            and the verdict is due by :func:`~lup.policy.bundle.hook_deadline`
            of it: a judgement still running then is refused as the
            generated dispatchers refuse it, rather than leaving the runtime
            to decide what a hook that never answered meant. Unstated, the
            judgement is unbounded and the runtime keeps its own timeout.

    Returns:
        SDK-agnostic hooks configuration; combine via ``merge_hooks``.
    """

    deadline = None if timeout is None else hook_deadline(timeout)

    async def policy_hook(event: LupHookInput) -> LupHookOutput:
        # LupHookEvent adopts Claude's event names as the neutral seam's own
        # vocabulary, so this reads a lup spelling rather than a provider's.
        if event.event != "PreToolUse":  # lup: ignore[native-spelling]
            return LupHookOutput()
        # Judged on a worker thread so the wait for it can end: the verdict is
        # computed synchronously, and a wait on the session's own loop could not
        # be cut short while it ran. A judgement abandoned at the deadline runs
        # on to its end with nobody reading it.
        judging = asyncio.to_thread(
            policy.decide, semantics.decode(event).as_documents()
        )
        try:
            decision = await asyncio.wait_for(judging, deadline)
        except TimeoutError:
            logger.warning(
                "refusing %s: the policy did not judge it in time", event.tool_name
            )
            return unjudged_output(None)
        return policy_hook_output(
            decision,
            semantics.escapes_from(sandbox),
            relay,
            contained=sandbox.contained,
        )

    return LupHooksConfig(
        pre_tool_use=[
            LupHookMatcher(
                hook=policy_hook,
                matcher="|".join(semantics.routed_tools),
                tag=tag,
                timeout=timeout,
            )
        ]
    )
