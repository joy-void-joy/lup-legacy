"""The order that settles a classified verdict into the one a session runs.

Classification answers what the rules say about an operation. It does not
answer what happens, because one verdict means different things in different
sessions: a question needs somebody eligible to answer it, work nobody judged
needs to know whether a containment boundary sits beneath it, a placement is
only worth declaring where some channel can carry it out, and a loss a proven
capture puts back is not a loss anybody needs to be asked about.

Those facts are an ordered list read the way `.gitignore` reads patterns:
every row is offered the running verdict, a row that rewrites hands its
result to the next, and the first row that settles ends the pass. A rule
about the order — *a hard prohibition is not moved by asking*, *an approval
cannot manufacture a capability* — is then a row that says so, rather than an
arm somebody has to place correctly.

The pass runs twice over one operation. **Preliminary** settlement reads what
is known before anything is captured or locked, and is what parks a question:
an independent review requirement is put to a reviewer without a mutation
lease held, because a question waiting on a person must not hold a lock the
rest of the session needs. **Dynamic** settlement runs after approval and
after capture, over the same rows with measured evidence in place, and is the
only pass in which recovery can discharge anything. Both are this one order;
what differs is the facts it is given, which is why a row never asks which
pass it is in.
"""

from collections.abc import Iterator

from .decision import (
    RESHAPE_HINT,
    SANDBOX_TRAPPED_REASON,
    KernelDecision,
    contributions,
    joined_decision,
    joined_placement,
    recovery_dischargeable,
)
from .diagnostic import Step, step
from .escalation import EscalationRequest
from .roles import is_session_scratch_target, is_temporary_root_target
from .rows import DisplacedTargetRow, TargetLandingRow
from .semantics import CheckpointEvidence, Reach, UnjudgedAmbient

# Every sentence below is one settlement row's own wording, declared beside the
# row that returns it: a caller passing different words would be stating a
# different settlement, not configuring this one. The kernel is compiled
# hermetically into a dispatcher that takes no arguments, so there is no caller
# to reach in any case.
# lup: ignore[constant-declaration] — a row's own wording
REDUNDANT_DECISION = " — a reviewer was already going to see this"
# lup: ignore[constant-declaration] — a row's own wording
HANDED_OVER_DECISION = (
    " — this is the runtime's own to decide, so a stated reason changes nothing"
)
# lup: ignore[constant-declaration] — a row's own wording
REDUNDANT_SANDBOX = " — this was already placed on the host"
# lup: ignore[constant-declaration] — a row's own wording
ESCALATED_PREFIX = "escalated ({reason}): "
# lup: ignore[constant-declaration] — a row's own wording
CONTAINED_READ = " — run inside the containment boundary, which confines it"
# lup: ignore[constant-declaration] — a row's own wording
RECOVERED_LOSS = "the paths it changes are captured and can be restored"
# lup: ignore[constant-declaration] — a row's own wording
CHECKPOINT_FAILED = " — the snapshot that would have made this undoable failed"
# lup: ignore[constant-declaration] — a row's own wording
NO_REVIEWER = ", and nobody who could approve it is reachable from this session"
# lup: ignore[constant-declaration] — a row's own wording
CONTAINED_JUDGED = "what it changes stays inside this container"


class SettlementFacts:
    """One classified verdict, and everything about the session judging it.

    Carried as one value so a row cannot read a fact the row beside it was
    not offered, and so the running verdict travels with the facts that
    settle it rather than beside them.
    """

    decision: KernelDecision
    escalation: EscalationRequest | None
    contained: bool
    inside_placement: bool
    sandbox_confined: bool
    host_executor: bool
    human_execution: bool
    reviewable: bool
    checkpoint: CheckpointEvidence
    unjudged_ambient: UnjudgedAmbient
    unleased: list[str]
    readonly: list[str]
    guarded: list[str]
    """Git's pointers and refs a write names, held as read-only is but unmounted.

    The same refusal, from a different source: ``readonly`` is what a launch
    bound read-only and measured, while these are recognized by what they are,
    because `git worktree remove` has to be able to unlink them and a bind
    would refuse it."""
    displaced: list[DisplacedTargetRow]
    landings: list[TargetLandingRow]
    hint: tuple[Step, ...]

    def __init__(
        self,
        decision: KernelDecision,
        escalation: EscalationRequest | None = None,
        contained: bool = False,
        inside_placement: bool = False,
        sandbox_confined: bool = False,
        host_executor: bool = False,
        human_execution: bool = False,
        reviewable: bool = True,
        checkpoint: CheckpointEvidence = "absent",
        unjudged_ambient: UnjudgedAmbient = "ask",
        unleased: list[str] | None = None,
        readonly: list[str] | None = None,
        displaced: list[DisplacedTargetRow] | None = None,
        landings: list[TargetLandingRow] | None = None,
        hint: tuple[Step, ...] = (),
        guarded: list[str] | None = None,
    ) -> None:
        self.decision = decision
        self.escalation = escalation
        self.contained = contained
        self.inside_placement = inside_placement
        self.sandbox_confined = sandbox_confined
        self.host_executor = host_executor
        self.human_execution = human_execution
        self.reviewable = reviewable
        self.checkpoint = checkpoint
        self.unjudged_ambient = unjudged_ambient
        self.unleased = unleased or []
        self.readonly = readonly or []
        self.guarded = guarded or []
        self.displaced = displaced or []
        self.landings = landings or []
        self.hint = hint

    def asks(self, kind: str) -> bool:
        """Whether the operation carried an escalation request of one kind."""
        return self.escalation is not None and kind in self.escalation.kinds

    def bounded(self) -> bool:
        """Whether anything confines every effect this operation can have.

        The one question the order asks about boundaries, derived rather than
        supplied, because a caller free to compute it is a caller free to
        compute it differently — and two of them did. The kernel took
        ``confined`` as its own argument, the shell path filled it from the
        native sandbox alone, and a container measured per launch reached the
        row named for it and settled nothing.

        Two mechanisms, joined here and nowhere else. The native sandbox
        confines one call at a time and can be told to leave some alone, so
        its term arrives already net of that exclusion. A container confines
        the process and was never asked, so no per-command lever reduces it —
        but the placement it promises is a claim, and a claim no probe
        confirmed is not evidence, so the launch's measurement of it is what
        makes containment count.
        """
        return self.sandbox_confined or self.container_private()

    def container_private(self) -> bool:
        """Whether a path this launch touches outside every lease is still its own.

        The half of :meth:`bounded` that a *filesystem* claim rests on, named
        because two rows want different halves. Anything confines the effects
        of an operation, which is the question above; this one asks whether
        the machine's own directories are shared, and only a container
        measured to place its work inside itself answers yes.

        The native sandbox cannot. It confines one call at a time on the
        operator's own filesystem, so a temporary directory it leaves
        writable is the host's, holding other programs' files — the same
        word, and the opposite fact.
        """
        return self.contained and self.inside_placement

    def stays_inside(self, reach: Reach | None) -> bool:
        """Whether harm of this reach, against these targets, stays in the container.

        Asked only where :meth:`container_private` holds, and answered from
        what the host measured about each path the operation names. Harm that
        lands in the container stays there unless a path the operation names
        is one the host lent it from elsewhere -- the session's own checkout is
        the tree it was opened to work in, and a kill or an export does not
        reach a sibling project. Harm that lands on the paths themselves stays
        only where every one is the container's own, and an operation naming
        none has said nothing a container could hold. Every other reach is the
        answer no: that is what naming it was for.
        """
        match reach:
            case "container":
                return all(row["lands"] != "host" for row in self.landings)
            case "mount":
                return bool(self.landings) and all(
                    row["lands"] == "container" for row in self.landings
                )
            case _:
                return False

    def lands_inside(self, target: str) -> bool:
        """Whether the host measured one named path as the container's own."""
        return any(
            row["path"] == target and row["lands"] == "container"
            for row in self.landings
        )

    def rewritten(self, decision: KernelDecision) -> "SettlementFacts":
        """These same facts, with a row's rewrite standing as the verdict."""
        return SettlementFacts(
            decision,
            escalation=self.escalation,
            contained=self.contained,
            inside_placement=self.inside_placement,
            sandbox_confined=self.sandbox_confined,
            host_executor=self.host_executor,
            human_execution=self.human_execution,
            reviewable=self.reviewable,
            checkpoint=self.checkpoint,
            unjudged_ambient=self.unjudged_ambient,
            unleased=self.unleased,
            readonly=self.readonly,
            displaced=self.displaced,
            landings=self.landings,
            hint=self.hint,
            guarded=self.guarded,
        )


class SettlementRule:
    """One row of the order, and whether reaching it ends the pass.

    ``settles`` is the difference between the two kinds of row. A settling
    row is the answer: nothing after it is read. A rewriting row changes what
    the rows after it are judging, which is how an escalation request and a
    missing channel compose without either knowing about the other.
    """

    settles: bool = True
    id: str = ""

    def reached(self, facts: SettlementFacts) -> KernelDecision | None:
        """This row's verdict, or ``None`` where it has nothing to say."""
        raise NotImplementedError


class HardProhibition(SettlementRule):
    """A policy invariant, which asking about does not move.

    First, because every row after it is about who could answer or what could
    be proven, and the answer here is nobody and nothing. A hard prohibition
    is not a rule's judgement that a person with more context might overrule
    — it is the shape of the thing being refused, and an approval does not
    change a shape.
    """

    id = "hard-prohibition"

    def reached(self, facts: SettlementFacts) -> KernelDecision | None:
        # Only another route moves it, so the hint says so rather than
        # pointing at a reviewer or a relay the refusal would meet again.
        if facts.decision.effect == "deny" and facts.decision.hard:
            return facts.decision.advising(RESHAPE_HINT)
        return None


class MissingCapability(SettlementRule):
    """A guarantee the runtime cannot deliver, which approval cannot create.

    Second for the same reason the first row is first: no reviewer answers
    it. What separates it from a prohibition is what the agent should do
    about it — a refusal that reads as policy sends the agent to argue with a
    rule, when the thing to fix is a channel the profile does not have.
    """

    id = "missing-capability"

    def reached(self, facts: SettlementFacts) -> KernelDecision | None:
        if facts.decision.cause == "capability":
            return facts.decision
        return None


class SandboxEscalation(SettlementRule):
    """The agent asked for the launcher's host, which is always reviewed.

    A placement rather than a permission, and the one crossing that is never
    unprompted on request: ``allow outside`` exists, but only as a rule's own
    declaration about an operation nobody had to ask about. An agent asking
    to leave gets a question, whatever the effect was inside — because what
    the reviewer is being shown is not "may this run" but "may this run
    *there*", and the second question has a different answer.

    A refusal is not moved by it. Combined with a decision escalation the
    refusal has already become a question by the time this row is read, so
    ``escalate[decision,sandbox]`` over an ordinary deny reaches ask outside
    and ``escalate[sandbox]`` alone over the same deny stays refused.

    Where no automated channel exists the question is still worth asking, so
    long as somebody can carry the answer out: an approved operation is
    rendered for the launcher's owner to run and confirm. Only where neither
    a channel nor a person is available does this fall through to
    :class:`TrappedPlacement` below.
    """

    settles = False
    id = "sandbox-escalation"

    def reached(self, facts: SettlementFacts) -> KernelDecision | None:
        if not facts.asks("sandbox"):
            return None
        decision = facts.decision
        if decision.effect == "deny":
            return None
        if decision.sandbox == "outside" and decision.effect == "ask":
            return decision.revised(reason=decision.reason + REDUNDANT_SANDBOX)
        assert facts.escalation is not None
        prefix = ESCALATED_PREFIX.format(reason=facts.escalation.reason)
        return decision.revised(
            effect="ask",
            reason=prefix + decision.reason,
            sandbox="outside",
            escalated=facts.escalation.reason,
            purpose=decision.purpose or "policy_override",
            abstention=None,
        ).advising(facts.escalation.notice())


class DecisionEscalation(SettlementRule):
    """A stated reason turns anything not already permitted into a question.

    The agent asked to be judged, so a refusal becomes the question it asked
    for, carrying the reason it gave — the person sees intent at the moment
    of judgement rather than a bare rule name. An abstention becomes the same
    question, at the placement it already had, because an operation nobody
    judged is exactly what a reviewer is for.

    Nothing is done to a verdict that already permits, beyond saying so: a
    stated reason over something permitted would buy a prompt for nothing,
    and an agent that wrote one deserves to learn it was unnecessary rather
    than to have it silently work.

    Nor to a verdict handed to the runtime's own mode, and for the same
    reason. A change too large for the small-change gate is the runtime's to
    answer as the operator set it up to, and a question lup put in its place
    would be a review nobody asked this policy to hold -- so the handoff
    stands, and the agent learns the reason was not needed.
    """

    settles = False
    id = "decision-escalation"

    def reached(self, facts: SettlementFacts) -> KernelDecision | None:
        if not facts.asks("decision"):
            return None
        assert facts.escalation is not None
        decision = facts.decision
        notice = facts.escalation.notice()
        if decision.effect == "allow":
            return decision.revised(
                reason=decision.reason + REDUNDANT_DECISION,
                visibility="notice",
            ).advising(notice)
        if decision.effect == "defer" and decision.abstention == "provider_native":
            return decision.revised(
                reason=decision.reason + HANDED_OVER_DECISION,
                visibility="notice",
            ).advising(notice)
        prefix = ESCALATED_PREFIX.format(reason=facts.escalation.reason)
        return decision.revised(
            effect="ask",
            reason=prefix + decision.reason,
            escalated=facts.escalation.reason,
            purpose=decision.purpose or "policy_override",
            cause=None,
            abstention=None,
        ).advising(notice)


class TrappedPlacement(SettlementRule):
    """An operation that has to reach the host where nothing can carry it.

    Not advice: run inside, it fails on whatever it touches first, and the
    failure reads as a broken repository rather than as a boundary. Refused
    here as a capability-blocked refusal — no approval builds a channel, and
    a question whose every answer leaves the operation with nowhere to go is
    a question nobody should be shown.

    Reached after both escalation rows, so an agent that asked for the host
    is told the profile has none rather than told its request was denied on
    the merits.
    """

    id = "trapped-placement"

    def reached(self, facts: SettlementFacts) -> KernelDecision | None:
        if facts.decision.sandbox != "outside":
            return None
        if facts.host_executor:
            return None
        if facts.decision.effect == "ask" and facts.human_execution:
            return None
        return facts.decision.revised(
            effect="deny",
            reason=SANDBOX_TRAPPED_REASON,
            cause="capability",
            capability="host_executor",
            rule=self.id,
        )


class UnleasedWrite(SettlementRule):
    """A write the measured boundary does not cover, wherever the session sits.

    The lease is what a launch actually mounted writable, and it is a snapshot:
    the read-only overlays a launch punches -- a worker's over its siblings,
    any session's over `config`, `hooks/` and the human-owned paths -- are
    enumerated when the container starts, and a container's mount namespace is
    fixed from then on. So a worktree cut *after* that gets the writable base
    with no overlay over it, and a mount table cannot close that -- there is no
    remount to make.

    Which is why the judgement does. The mount table was never the barrier
    here: the shared administrative directory is mounted writable on purpose,
    because no session could cut a worktree otherwise, and what guards the keys
    inside it is a rule holding an approval question against them by name. This
    is the same arrangement applied to the same gap.

    Read against ``allow`` and ``defer`` only. A judged refusal is somebody's
    answer and asking about it would be offering to overturn it, and an ask
    already reaches a reviewer -- who is shown the operation, and can see for
    themselves where it points.

    Not the write row spelled twice, though it reads like it. ``WritesPath``
    decides on the *declaration*: which tree a spelling puts the target in,
    read off the words before anything runs. This decides on the
    *measurement*: where the launch actually mounted writable, resolved
    against the filesystem. ``mkdir -p tmp/build`` is the case that separates
    them -- declared scratch, so that row allows it at every placement, and
    outside the lease of a worktree cut after the container started, which
    only a measurement can know.
    """

    id = "unleased-write"

    def reached(self, facts: SettlementFacts) -> KernelDecision | None:
        # Two roots are not the lease's business. A lease enumerates what came
        # from the host, so both read as uncovered -- and both are uncovered in
        # the direction that makes them safe, holding nothing any capture was
        # meant to protect. The session scratchpad is the harness's own root
        # at every placement, so its spelling carries the exemption. The
        # machine's temporary root is the launch's own only where the launch
        # is a container, gone with it, so that exemption is spelled against
        # `container_private` rather than against the path: uncontained, the
        # same word is the operator's own `/tmp`, shared with every other
        # process on the machine. Any other path the host measured as the
        # container's own is the same case: the lease enumerates what came
        # from the host, and this came from nowhere. All are read here, where
        # every caller's targets meet, because the host half that lists them
        # reaches no kernel to say what either root is.
        reported = [
            target
            for target in facts.unleased
            if not is_session_scratch_target(target)
            and not (
                facts.container_private()
                and (is_temporary_root_target(target) or facts.lands_inside(target))
            )
        ]
        if not reported or facts.decision.effect not in ("allow", "defer"):
            return None
        # The write leads. The verdict this replaces said "every segment is
        # declared safe", which is true and beside the point: what the
        # approver decides on is the path, so the path is the first thing
        # read. A deferral's own reason stays, since "nobody judged this"
        # is a second fact the same approval answers.
        written = (
            f"writes {', '.join(reported)}, which this launch did not mount"
            " writable and nothing captured"
        )
        return facts.decision.revised(
            effect="ask",
            reason=(
                written
                if facts.decision.effect == "allow"
                else f"{facts.decision.reason}; {written}"
            ),
            purpose="unrecovered_local_mutation",
            # Named, because the verdict this replaces was reached by the
            # vocabulary finding nothing to say and carries no id of its own.
            # An ask that names no rule is one nobody can write a case for.
            rule=self.id,
            abstention=None,
        )


class ReadOnlyWrite(SettlementRule):
    """A write landing in a read-only region this launch measured, refused.

    The container binds each repository's shared `config` and `hooks/`
    read-only inside the writable share around them, because their contents
    run on the host at the next git command there: `core.hooksPath`,
    `alias.*`, `core.fsmonitor` and the scripts themselves. Inside it every
    verb meets the bind. The launch records the same holes as its read-only
    roots, and this is them held where no bind stands -- a host posture, or
    a write the classification graded before any mount could answer.

    Refused rather than asked, because that is what the bind does: nobody
    can approve a write past a read-only mount, and git's own commands --
    commit, fetch, worktree -- reach the state they need without writing
    either. Every verb, because the hole is a place and not a spelling: a
    redirection's content already met it through the edit gates, while a
    `cp`, `mv` or `ln` placing the same bytes reached only the loss row,
    which a capture of the session's own checkout discharged for a tree it
    never held.

    Read over ``allow``, ``defer`` and ``ask`` alike, and above
    :class:`RecoveredLoss` for that reason; a refusal already standing needs
    nothing from it.

    Git's own pointers are held the same way without a bind, since `git
    worktree remove` unlinks them and a bind would refuse it: a linked
    worktree's `.git` file, an entry's `commondir` and `gitdir`, its
    `config.worktree`, and the entries themselves. Host git follows each to
    the repository and the config it reads, so rewriting one is choosing that
    config by another route -- and git's own commands write every one of them.
    """

    id = "read-only-write"

    def reached(self, facts: SettlementFacts) -> KernelDecision | None:
        if not (facts.readonly or facts.guarded) or facts.decision.effect == "deny":
            return None
        held = (
            [
                f"writes {', '.join(facts.readonly)}, which this launch holds"
                " read-only: git runs what its config and hooks name on the host"
            ]
            if facts.readonly
            else []
        )
        pointed = (
            [
                f"rewrites {', '.join(facts.guarded)}, a pointer host git follows"
                " to the repository and the config it acts on"
            ]
            if facts.guarded
            else []
        )
        return facts.decision.revised(
            effect="deny",
            reason="; ".join([*held, *pointed]),
            recovery=(
                step(
                    "work in a worktree of that repository",
                    ["git", "worktree", "add", "<path>", "<branch>"],
                ),
                step(
                    "let git's own commands write what they need: `git worktree"
                    " move`, `remove` and `prune`, `git update-ref`"
                ),
                step("ask the operator to change a setting from their own terminal"),
            ),
            cause="deliberate",
            purpose=None,
            rule=self.id,
            abstention=None,
        )


class DisplacedWrite(SettlementRule):
    """A write whose target does not land where its spelling says it does.

    Every grant above reads a path lexically, which is what makes a role
    declarable at all: a root names a tree, and a spelling either sits under
    it or does not. A symlink breaks exactly that step. `/tmp/link/cron.d/job`
    reads as the temporary root — disposable, unreviewed, nothing a capture
    was meant to hold — and lands wherever the link points, which is somebody
    else's file with none of those properties.

    The exposure is what a world-writable root costs. A repository-relative
    root is planted in only by whoever can already write the checkout, and the
    session scratchpad is minted per session by the harness; `/tmp` is neither,
    so any process on an uncontained host can leave a link there and be
    written through by a grant meant for a throwaway file.

    Resolution belongs to the host and the reason belongs here, which is the
    same division `unleased-write` makes: the kernel sees words, so a caller
    that resolved nothing reports nothing and this row is silent — the
    lexical grants then stand exactly as they did.

    Read against ``allow`` and ``defer`` only. A verdict already asking has a
    reviewer, and what they are shown carries the destination.
    """

    id = "displaced-write"

    def reached(self, facts: SettlementFacts) -> KernelDecision | None:
        if not facts.displaced or facts.decision.effect not in ("allow", "defer"):
            return None
        spelled = ", ".join(
            f"{row['path']} → {row['lands']}" for row in facts.displaced
        )
        written = f"writes through a symlink that lands elsewhere: {spelled}"
        return facts.decision.revised(
            effect="ask",
            reason=(
                written
                if facts.decision.effect == "allow"
                else f"{facts.decision.reason}; {written}"
            ),
            purpose="unrecovered_local_mutation",
            rule=self.id,
            abstention=None,
        )


class ProviderNative(SettlementRule):
    """A rule looked and handed the decision to the provider's own mode.

    The one abstention that survives, and the only verdict under which the
    session behaves exactly as it would with Lup absent. It settles rather
    than rewrites so that no row below can turn a deliberate handoff into a
    refusal for want of anybody having looked — somebody looked, and what
    they decided was that this is the provider's to answer.
    """

    id = "provider-native"

    def reached(self, facts: SettlementFacts) -> KernelDecision | None:
        if facts.decision.abstention == "provider_native":
            return facts.decision
        return None


class ContainedJudgement(SettlementRule):
    """A judged question whose harm the measured container holds, settled inside.

    The other half of what :class:`ContainedEffects` answers for work nobody
    judged. A rule asks or refuses on behalf of somebody -- the operator's
    processes, their mounted files, a remote, a lent secret -- and each rule
    says, through the effects it declares, where that harm would land. Inside
    a container this launch measured around the session, harm that lands in
    the container lands on nothing anybody else has: a killed process, an
    exported variable, a package in the image, a file under a directory the
    host never lent. So the question it raises is one nobody needs to answer,
    and it is settled as a permission rather than handed to a boundary.

    Every reason the verdict asks or refuses has to stay inside, read over the
    contributions rather than their join, for the reason
    :class:`RecoveredLoss` reads them that way: one segment killing a process
    beside one pushing to a remote keeps the whole line's question. The
    verdict's own reach has to stay inside as well, because a reader can widen
    it past what its parts name: a word nobody can read carries the reach of
    every command it could be, while its parts are the one it was read as.

    Only the measured container moves it. The native sandbox confines one call
    at a time on the operator's own machine, so the processes, packages and
    directories it leaves reachable are the operator's; a verdict relaxed for
    it would be relaxed on the host.

    Below the rows that cannot be moved -- a hard prohibition, a missing
    channel, a placement with nowhere to go, a read-only hole -- and below the
    escalation rows, whose question the agent asked for and keeps. Above the
    capture, the reviewer and the refusal rows, because each of those would
    otherwise answer a question this row says was never worth asking: a
    capture discharging it, a missing reviewer refusing it, and a judged
    refusal standing on a harm nothing outside the container would meet.
    """

    id = "contained-judgement"

    def reached(self, facts: SettlementFacts) -> KernelDecision | None:
        decision = facts.decision
        if not facts.container_private():
            return None
        if decision.effect not in ("ask", "deny") or decision.hard:
            return None
        if facts.escalation is not None or decision.sandbox == "outside":
            return None
        if decision.cause in ("capability", "unreadable"):
            return None
        judged = [
            part for part in contributions(decision) if part.effect in ("ask", "deny")
        ]
        if not judged or not all(
            facts.stays_inside(part.reach) for part in (decision, *judged)
        ):
            return None
        # The allow says why it is allowed, in place of the question it
        # settles: "requires approval" beside "allowed without asking" told
        # the reader both at once.
        return decision.revised(
            effect="allow",
            reason=CONTAINED_JUDGED,
            sandbox="inside",
            purpose=None,
            cause=None,
            recovery=(),
            abstention=None,
        )


class RecoveredLoss(SettlementRule):
    """A question about a loss a proven capture already put somewhere safe.

    The rules guard *the direction that removes something no second attempt
    restores*. What a second attempt restores is a fact about the session and
    not about the operation, so a rule states the capture its question was
    about and this row asks whether that capture was actually taken.

    Settled as a **permission**, not a deferral. Nothing about the provider's
    own mode is involved: this policy has positively established that the
    loss it was protecting against did not happen, and an operation whose
    every reason to interrupt has been answered is authorized rather than
    handed on. Deferring instead would make the outcome depend on which mode
    the session happened to be in, for a fact that has nothing to do with the
    session's mode.

    It discharges *only* local loss. An operation that also rewrites a
    production file, touches a protected path, reads a credential, or reaches
    a remote keeps its question in full, which is what
    :func:`~lup.policy.kernel.decision.recovery_dischargeable` reads over the
    contributing findings rather than over their join.

    A stated reason keeps its question either way: the escalation rows above
    have already turned it into one, and answering the agent's own request
    with a permission would drop both the question and the reason given for
    it. A capture that was attempted and failed keeps the question too, and
    says which it was, because "nobody captured this" and "the capture did
    not work" are different things to tell a person.
    """

    id = "recovered-loss"

    def reached(self, facts: SettlementFacts) -> KernelDecision | None:
        if facts.decision.effect != "ask" or facts.escalation is not None:
            return None
        if not recovery_dischargeable(facts.decision):
            return None
        match facts.checkpoint:
            case "complete":
                pass
            case "failed":
                return facts.decision.revised(
                    reason=facts.decision.reason + CHECKPOINT_FAILED,
                    visibility="notice",
                )
            case _:
                return None
        return facts.decision.revised(
            effect="allow",
            reason=RECOVERED_LOSS,
            purpose=None,
            recovery=(),
            visibility="notice",
        )


class UnreachableReviewer(SettlementRule):
    """A question in a session no eligible reviewer can be reached from.

    Refused, and the direction matters more than anything else in this order.
    Somebody looked at this operation and decided a person should see it; no
    person can be reached; so it does not happen. Handing it to the boundary
    instead would say the opposite — that a question nobody could answer is a
    question that did not need asking — and a boundary that confines an
    operation does not review it.

    Measured before the refusal existed: in a headless contained session a
    remote ref deletion came back an unprompted allow, and an escalation
    marker, whose entire purpose is to summon the person this path decided
    was unnecessary, granted exactly what the table refused.

    Unjudged work does not reach here and is unaffected. It never becomes an
    ask in a session that can reach nobody, because the row that would make
    one reads the same fact — so the boundary still carries what nobody
    classified, which is the whole of what containment buys the lattice, and
    what it does not buy is a way past a question somebody meant.

    The last resort and not the ordinary path: a detached session reaches a
    person through the durable relay, and a worker reaches its supervisor
    through the same relay. ``reviewable`` is false only where neither exists.
    """

    id = "unreachable-reviewer"

    def reached(self, facts: SettlementFacts) -> KernelDecision | None:
        if facts.decision.effect == "ask" and not facts.reviewable:
            return facts.decision.revised(
                effect="deny",
                reason=facts.decision.reason + NO_REVIEWER,
                cause="deliberate",
            ).advising(facts.hint)
        return None


class ContainedEffects(SettlementRule):
    """Nobody judged it, and everything it can do is confined: run it inside.

    The practical default for odd local work. Reading ``/etc/passwd``,
    listing ``/proc``, searching the session's own home, running a broad
    ``find`` — each is suspicious in the abstract and none of it is worth a
    person's attention when the environment it observes is the contained one
    and no independent rule asks or denies. A host path that is not there
    fails inside with a boundary diagnostic naming the sandbox escalation
    that would reach it, which is a better answer than a question, because
    the agent usually did not need the host and finds that out itself.

    Settles to ``allow inside`` rather than deferring, so the placement is
    Lup's and holds however permissive the session's own mode is. That is the
    whole difference between containing something and hoping the provider
    contains it.

    Not answered by the effect table reading placement, though both are about
    containment. That table decides what a *declaration* earns, and every one
    of its members returns a permission, a question or a refusal -- none of
    them returns ``defer``, because a rule that describes an operation has
    said something about it. This row answers the operations no rule
    described at all, which is the one verdict the effects can never reach.

    A word nobody could read in a command the vocabulary did describe is not
    that. Every command the word could make was judged, and a container
    settles it only where the widest of their harms stays inside:
    `git push $X origin feat` could be a forced push or a deletion, which land
    on the remote whatever holds the process. The native sandbox is not asked
    the same, because it confines the call itself, and a call it leaves alone
    -- a push among them -- is not bounded by it at all.
    """

    id = "contained-effects"

    def reached(self, facts: SettlementFacts) -> KernelDecision | None:
        decision = facts.decision
        if decision.effect != "defer" or not facts.bounded():
            return None
        held = facts.sandbox_confined or facts.stays_inside(decision.reach)
        if decision.unread and not held:
            return None
        return facts.decision.revised(
            effect="allow",
            reason=facts.decision.reason + CONTAINED_READ,
            sandbox="inside",
            abstention=None,
        )


class UnjudgedAmbientPolicy(SettlementRule):
    """Nobody judged it and no boundary confines it: the profile answers.

    Two answers, both declared rather than inferred. ``ask`` keeps unjudged
    work visible, and is the default because what a reviewer is shown is
    exactly what will run — the operation was read and found well-formed, and
    only the vocabulary was silent. ``defer`` is a profile deliberately
    handing the long tail to provider-native judgement.

    A session that can reach nobody creates no question here. The row above
    that rewrites an unanswerable ask is read before this one and so cannot
    see a question this one makes; reading the same fact here is what keeps
    the two from needing to be ordered around each other in both directions.

    Only the legible half reaches either answer. An operation the classifier
    could not *read* — an unresolved expansion, a substitution it cannot see
    into, an operator its parser does not carry — is refused however many
    reviewers are present, because a question about text the policy could not
    parse is one the person cannot answer either: they would approve
    ``cat x ;& rm -rf ~`` on the strength of the ``cat``.
    """

    id = "unjudged-ambient"

    def reached(self, facts: SettlementFacts) -> KernelDecision | None:
        if facts.decision.effect != "defer":
            return None
        if not facts.decision.unlisted:
            return None
        if facts.unjudged_ambient == "defer":
            return facts.decision.revised(abstention="provider_native")
        if not facts.reviewable:
            return None
        return facts.decision.revised(
            effect="ask", purpose="policy_override", abstention=None
        )


class Unreadable(SettlementRule):
    """Nothing judged it, nothing confines it, and nobody can read it.

    The refusal names the recipe rather than only the wall — reshape it into
    the allowed vocabulary, or say why it has to be this shape — because work
    nobody classified is work somebody has to look at, and an agent told only
    "no" looks at nothing.
    """

    id = "unreadable"

    def reached(self, facts: SettlementFacts) -> KernelDecision | None:
        if facts.decision.effect != "defer":
            return None
        return facts.decision.revised(
            effect="deny",
            cause="unreadable",
            abstention=None,
        ).advising(facts.hint)


class JudgedRefusal(SettlementRule):
    """A rule refused this, and no boundary rescues a judged deny.

    The distinction the whole order rests on: unjudged work is refused for
    want of anybody having looked, and a boundary answers that. A judged deny
    is somebody's answer, and running it confined would still be running it.
    """

    id = "judged-refusal"

    def reached(self, facts: SettlementFacts) -> KernelDecision | None:
        if facts.decision.effect != "deny":
            return None
        return facts.decision.revised(
            cause=facts.decision.cause or "deliberate",
        ).advising(facts.hint)


class Standing(SettlementRule):
    """Whatever reached here stands: a permission, or an answerable question."""

    id = "standing"

    def reached(self, facts: SettlementFacts) -> KernelDecision | None:
        return facts.decision


SETTLEMENT_ORDER: list[SettlementRule] = [
    HardProhibition(),
    MissingCapability(),
    DecisionEscalation(),
    SandboxEscalation(),
    TrappedPlacement(),
    UnleasedWrite(),
    ReadOnlyWrite(),
    DisplacedWrite(),
    ProviderNative(),
    ContainedJudgement(),
    RecoveredLoss(),
    UnreachableReviewer(),
    ContainedEffects(),
    UnjudgedAmbientPolicy(),
    Unreadable(),
    JudgedRefusal(),
    Standing(),
]
"""Every row, in the order they are read.

Precedence is position. The two unanswerable rows come first, because nothing
below them could change their answer. Decision escalation precedes sandbox
escalation so that a combined request over an overrideable refusal has
already become a question by the time the placement moves — which is exactly
the difference between ``escalate[decision,sandbox]`` reaching the host and
``escalate[sandbox]`` alone staying refused.

``DisplacedWrite`` sits beside ``UnleasedWrite`` because the two are the same
kind of correction: a measurement the classification above could not make,
against a verdict it reached without one. Neither is the write row spelled
twice — that row decides which tree a spelling names, and these decide what the
filesystem does with the spelling afterwards.

``UnleasedWrite`` sits above ``RecoveredLoss`` for the reason that row exists:
a proven capture settles a loss to a permission, and a capture of *this*
session does not hold what lies outside the boundary it measured. Read the
other way round, an undo reference covering the checkout would discharge a
question about a tree it never held. ``ReadOnlyWrite`` sits beside it, and
above ``RecoveredLoss`` for the same reason taken one step further: a
read-only region is not a loss a capture could put back but a place nothing
writes, so it refuses an ``ask`` as well. ``Standing`` is last because it speaks
for everything.

``ContainedJudgement`` sits after every row whose answer nothing moves and
before the three that answer a question -- a capture discharging it, a missing
reviewer refusing it, a judged refusal standing -- because inside a measured
container a question whose harm stays there is one those rows need never see.
"""


def settle(
    facts: SettlementFacts, order: list[SettlementRule] = SETTLEMENT_ORDER
) -> KernelDecision:
    """Read the order over one classified verdict and return what it settles.

    The same pass serves preliminary and dynamic settlement. What differs is
    the evidence in ``facts`` — a preliminary pass carries ``absent`` capture
    evidence and reaches its question, a dynamic pass carries what was
    actually measured — so no row has to know which of the two it is in, and
    neither pass can apply a rule the other does not.

    A permission is read for what it did not answer: see :func:`left_standing`.
    """
    for rule in order:
        reached = rule.reached(facts)
        if reached is None:
            continue
        if rule.settles:
            return left_standing(facts, reached, order)
        facts = facts.rewritten(reached)
    return facts.decision


def left_standing(
    facts: SettlementFacts, settled: KernelDecision, order: list[SettlementRule]
) -> KernelDecision:
    """A permission for part of a line, with every deferral it did not answer.

    A row that settles a line as a permission answers for the parts it read.
    One settling a question -- a harm the container holds, a loss a capture
    puts back -- reads the questions and refusals, and a part that deferred
    rode beside them unread: `kill 1234 && curl <an origin no scope names>`
    is a kill the container holds and a fetch only the runtime answers, and
    `git rm tmp/x ; frobnicate` a restorable removal and a command nobody
    judged. One settling a deferral answers for work nobody judged, and a
    part that handed its decision to the runtime is not that. Allowing either
    line whole answered the part it never read.

    So what the row left is settled on its own, through the same order, and
    the line takes the stronger answer: a runtime's handoff, a question, or a
    refusal where that is what the leftover earns, and the permission where
    the leftover is allowed too -- placed where every part of it can run.
    """
    if settled.effect != "allow":
        return settled

    def leaves(verdict: KernelDecision) -> Iterator[KernelDecision]:
        if not verdict.findings:
            yield verdict
        for part in verdict.findings:
            yield from leaves(part)

    handoff = facts.decision.effect == "defer"
    unanswered = [
        part
        for part in leaves(facts.decision)
        if part.effect == "defer"
        and (not handoff or part.abstention == "provider_native")
        and part is not facts.decision
    ]
    if not unanswered:
        return settled
    again = settle(facts.rewritten(joined_decision(unanswered)), order)
    placed = joined_placement([settled, again])
    if again.effect == "allow":
        return settled.revised(sandbox=placed)
    return again.revised(sandbox=placed)
