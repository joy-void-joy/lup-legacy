"""The verdict vocabulary every kernel module returns."""

from collections.abc import Sequence
from typing import Literal, TypedDict, Unpack

from .diagnostic import (
    Diagnostic,
    Step,
    Verdict,
    devtools,
    diagnostic,
    distinct,
    headline,
    rendered,
    stated,
    step,
    way,
)
from .semantics import (
    AbstentionPurpose,
    Capability,
    REACHES,
    Reach,
    RefusalCause,
    ReviewPurpose,
    ReviewerRequirement,
    Visibility,
)


type DecisionEffect = Literal["allow", "ask", "deny", "defer"]
"""What the provider is told, and the whole of what it may be told.

Four values, and the boundary between them is where native autonomy begins:

* ``allow`` is positive authority. Lup authorizes this exact operation, so no
  further permission decision is needed and the provider's auto-mode has
  nothing to add. It is not a request for the provider to approve.
* ``ask`` is a Lup-owned review requirement. Provider auto-mode cannot
  satisfy it, because the point of the question is that a person sees this
  moment while it is still happening.
* ``deny`` is a prohibition.
* ``defer`` is the only value that hands the decision over, and the only one
  under which the session behaves exactly as it would with Lup absent.

Nothing else joins them. Everything Lup knows *about* one of these four —
who may review, where it runs, what would restore it, why it was refused —
is a separate fact in :mod:`lup.policy.kernel.semantics`, because folding
another value in here is how a runtime ends up being told something it has no
way to act on.
"""

type CheckpointRequirement = Literal["targeted", "boundary_wide", "unrecoverable"]
"""What capture would put back what an operation destroys locally.

An approval question over local loss exists because the loss is permanent.
Naming the capture that makes it impermanent is naming the condition under
which the question stops being worth a person's attention — and, proven,
settles the operation to ``allow`` rather than merely to the provider's own
gate. Proof is the whole of it: a snapshot reference is not evidence, and a
rule's requirement is discharged against a capture somebody recorded.

* ``targeted`` — every path this operation can affect resolves statically, so
  a capture of exactly those paths covers the whole loss. ``rm build/out``,
  ``git restore``, a redirection into a named file.
* ``boundary_wide`` — variables, globs, substitutions, or a directory walk
  prevent an exact footprint, so only a capture of every precious writable
  root covers it. The wider capture is what the opacity costs, not a reason
  to refuse the operation.
* ``unrecoverable`` — no capture reaches it. A remote ref, a published
  artifact, an issue somebody read, a command whose argument is another
  command. Recovery has nothing to say and the question stands.

``unrecoverable`` is the default, and the default is the safety of the axis:
a rule nobody annotated keeps asking, and an annotation is what relaxes it.
The other direction would make every rule anybody forgot into a grant.

A row whose guarded forms do not agree takes the weakest of them. ``sort``
guards ``-o``, which writes a file, beside ``--compress-program``, which runs
one; the row says ``unrecoverable``, because a reader of the verdict cannot
tell which flag brought it.
"""

type SandboxPlacement = Literal["inside", "ambient", "outside"]
"""Where an operation runs, which is a different question from who decides it.

The boundary these name is the *outer containment boundary* — the profile's
own, whatever delivers it — and never merely a provider's per-call sandbox.
That native sandbox is one adapter mechanism for spelling ``inside``; a
generated artifact may also spell it through a provider-native permission
field. Neither is a second semantic placement, and ``outside`` never means
"out of the native sandbox but still in the container".

* ``inside`` — execute inside the containment boundary, whatever mode the
  session is in. Provider auto-mode does not move it.
* ``ambient`` — execute wherever the session already lives. The default,
  because saying nothing about placement is what almost every verdict means.
* ``outside`` — execute on the launcher's host, through the trusted host
  executor. Unprompted under ``allow``; under ``ask``, only after exact
  approval.

Only ``allow`` and ``ask`` carry authoritative placement. ``deny`` never
executes, so placement is moot, and ``defer`` supplies no Lup placement at
all — supplying one would be Lup deciding half of a decision it just handed
over.
"""

# lup: ignore[constant-declaration] — the words this gate says, in a kernel
# compiled hermetically into a bare dispatcher that takes no arguments
SANDBOX_ESCAPE_NOTICE = " — this will run on the host, outside the boundary"
"""What an approval question adds when the call it approves also leaves.

The crossing as an uncontained session makes it: the runtime's per-call
sandbox is the only boundary there is, and lifting it for one call puts that
call on the launcher's host.
"""

# lup: ignore[constant-declaration] — the same gate's words for the other
# boundary, declared beside the first so the two are read together
CONTAINED_ESCAPE_NOTICE = (
    " — this runs with the per-call sandbox off, still inside the container's"
    " mounts; a path mounted read-only stays read-only"
)
"""What the same question adds where a container is the boundary.

A contained launch never arms the per-call sandbox, so lifting it lifts
nothing, and the approved command runs in the same mount namespace as every
other: a write to a human-owned file, bind-mounted read-only over the
checkout, fails approved exactly as it fails unmarked. The question names
where the call lands rather than where the marker asked for it to go, because
a sentence promising the host over a call that stays in the container is the
approver being told the wrong thing at the one moment they decide.
"""

# lup: ignore[constant-declaration] — the recipe a diagnostic hands the agent;
# its whole value is being the same words every time
SANDBOX_ESCALATION_RECIPE = (
    " — this ran inside the containment boundary; if it genuinely needs the"
    " launcher's host, resubmit with a leading"
    " '# lup: escalate[sandbox]: <why>' line"
)
"""How an operation that found the boundary insufficient asks to leave it.

Carried on the diagnostic rather than appended to every permitted call. A
context line per allowed operation is how a channel meant for what matters
stops being read, and the moment the agent needs this is the moment something
inside actually failed for want of the host.
"""

# lup: ignore[constant-declaration] — the refusal a call with nowhere to run is
# stopped with, naming the missing capability rather than the wall
SANDBOX_TRAPPED_REASON = (
    "this has to run on the launcher's host, and this profile has no way to"
    " run anything there"
)
"""Why an operation needing the host is refused where no channel reaches it.

A capability-blocked refusal, not a question. Offering the question would
spend a person's attention on a decision that changes nothing: approving it
would still leave the operation with nowhere to go, and running it inside
would run something the placement said must not run there — failing on
whatever it touched first, and misreporting the boundary as a broken
repository.
"""

# lup: ignore[library-default] — the stdlib the kernel actually imports; the hermetic guarantee it exists to hold
KERNEL_IMPORT_ALLOWLIST = (
    "ast",
    "collections",
    "collections.abc",
    "difflib",
    "fnmatch",
    "functools",
    "io",
    "ipaddress",
    "pathlib",
    "posixpath",
    "re",
    "tokenize",
    "typing",
    "urllib.parse",
)
# The ones below are reasons, ways through and one sentinel the kernel's own
# decisions carry: each is declared beside the verdict that returns it, so a
# caller passing different words would be returning a different verdict.
# lup: ignore[library-default] — refusal wording, declared with its verdict
ESCALATE_HINT = (
    step("change the command to one the policy allows"),
    step(
        "or resubmit it with a first line `# lup: escalate[decision]: <why>`,"
        " which puts it to a reviewer"
    ),
)
# lup: ignore[constant-declaration] — refusal wording
RESHAPE_HINT = (step("change the command to one the policy allows"),)
# lup: ignore[library-default] — refusal wording, declared with its verdict
RELAY_HINT = (
    step("change the command to one the policy allows"),
    step(
        "or ask for the gate with `request_allowance`, which reaches whoever is"
        " watching this run"
    ),
)
"""What a reviewed worker is told, which is not what a headless run is told.

Both are non-interactive and only one of them is alone. A resolver worker
holds a question mailbox: it can put the ask to the human supervising the
run and carry on from where it stopped when the answer lands. Telling it to
reshape the command is telling it the route it has does not exist, and
measured, it does what anybody would — it queues a *material question*
instead, which parks the whole run on a decision nobody needed to make.

A genuinely headless run has no such channel and still gets
:data:`RESHAPE_HINT`, because naming a route that is not there is the same
failure pointed the other way.
"""
# lup: ignore[library-default] — refusal wording, declared with its verdict
UNJUDGED_RECOVERY = (
    step(
        "see what the policy says about the same call",
        devtools("dev", "policy", "<the call>"),
    ),
    step("and report the failure", devtools("dev", "report-friction")),
)
"""What an agent can do about a call the hook failed to judge.

The hook refuses it unjudged, which says nothing about whether the call was
fine: reproducing it shows whether the failure is the call's or the hook's,
and a report is how the hook's gets fixed.
"""
# lup: ignore[constant-declaration] — refusal wording, declared with its verdict
SCRIPT_RECOVERY = (
    step(
        "write the code to a script file, which can be reviewed and run again,"
        " and run that",
        ["uv", "run", "python", "<script>"],
    ),
)
# lup: ignore[constant-declaration] — refusal wording, declared with its verdict
SUBSTITUTION_REASON = "command substitution hides a command inside another"
# lup: ignore[constant-declaration] — refusal wording, declared with its verdict
SUBSTITUTION_RECOVERY = (
    step(
        "run the inner command in its own call and use its output as written,"
        " or read it through `<(...)` or a pipe"
    ),
)
# lup: ignore[constant-declaration] — refusal wording, declared with its verdict
BACKTICK_REASON = "backtick substitution hides a command inside another"
# lup: ignore[constant-declaration] — refusal wording, declared with its verdict
BACKTICK_RECOVERY = (step("write it as `$(...)`, so the inner command can be read"),)
# lup: ignore[constant-declaration] — a spelling chosen to sit outside identifier
# space, which is the property the substitution proof below rests on
SUBSTITUTION_SENTINEL = "$~sub~"
"""Spliced into a word where a real ``$(...)`` stood.

The spelling sits outside identifier space, so no variable binding can
instantiate it, and a quoted literal that happens to match only makes the
word read as opaque — the conservative direction.
"""


def sandbox_escaped(sandbox: SandboxPlacement) -> bool:
    """Whether a placed operation runs on the launcher's host.

    One function rather than a comparison spelled at each of the boundaries
    that render the crossing — both hook factories, the in-process renderer,
    and each compiled dispatcher — because a condition spelled out at four
    sites is one that can be spelled differently at four sites, which leaves
    a placement honoured on one path and stripped on the other.
    """
    return sandbox == "outside"


class FileReviewRow(TypedDict):
    """A caller-bound record of the actual file verdict, never authority itself.

    ``after`` is the document the verdict judged, ``None`` where the call
    removes the file: what a reviewer is shown, so what they read is what was
    judged rather than a second reading of the call made where they read it.
    The document it replaces is bound by ``before_sha256`` alone, since the
    caller keeps it as the call's preimage already.
    """

    path: str
    effect: DecisionEffect
    reason: str
    rule: str
    rules: list[str]
    before_sha256: str | None
    after_sha256: str | None
    after: str | None


type UnpreviewedCause = Literal["run", "unread"]
"""Why a step of a command shows no document: ``run`` where only running it
makes one, ``unread`` where the file it leaves does not read as text."""


class UnpreviewedRow(TypedDict):
    """One step of a command whose effect no document states, and the files it leaves so.

    The other half of a command's per-file record: the files a reviewer is
    shown as documents are the ones the policy worked out, and this names
    what it did not, so a command with no diff is never read as a command
    that changes nothing. ``command`` is the step as it reads, ``paths`` the
    files it leaves holding what only it knows, resolved; a step naming none
    is a program that may write where no word says.
    """

    command: str
    paths: list[str]
    cause: UnpreviewedCause


class SegmentRow(TypedDict):
    """One command of a line, and the verdict it reached on its own.

    What a reviewer reads a command line by: each command with its own
    effect and why, so an approval of a line that asks twice is given
    knowing both questions, and the commands allowed on their own are told
    apart from the ones that ask. ``command`` is the command as it reads,
    blank for a verdict about the line rather than one of its commands -- a
    construct the vocabulary does not walk, a remainder nobody could read.
    """

    command: str
    effect: DecisionEffect
    reason: str
    rule: str


class Revision(TypedDict, total=False):
    """What one settlement row may rewrite, absent where it changes nothing.

    A TypedDict rather than a model because this is the hermetic kernel, which
    has no pydantic; partial because a row names one or two fields and the
    point of the shape is that it says nothing about the rest.
    """

    effect: DecisionEffect
    reason: str
    sandbox: SandboxPlacement
    escalated: str
    checkpoint: CheckpointRequirement
    unlisted: bool
    reviewer: ReviewerRequirement
    purpose: ReviewPurpose | None
    visibility: Visibility
    cause: RefusalCause | None
    capability: Capability | None
    abstention: AbstentionPurpose | None
    hard: bool
    findings: tuple["KernelDecision", ...]
    rule: str
    evaluator: str
    recovery: tuple[Step, ...]
    subject: str
    see: str
    queued: str
    reach: Reach | None
    unread: bool
    file_reviews: tuple[FileReviewRow, ...]
    unpreviewed: tuple[UnpreviewedRow, ...]
    segments: tuple[SegmentRow, ...]


class KernelDecision:
    """One settled verdict, and every orthogonal fact settled alongside it.

    The first four fields are what a provider is told. The rest are what Lup
    knows about that answer, and each is a separate axis because each has a
    different answerer: a checkpoint does not consent to a release, an
    approval does not build a host channel, and a rule id is not a review
    purpose. Composing them into one enum is what made a verdict unreadable
    at exactly the moment somebody needed to know why it happened.
    """

    effect: DecisionEffect
    reason: str
    """Why the call was stopped, as one clause a person deciding about it reads.

    An approval prompt shows this, after ``subject``, and nothing else. What
    the agent should do instead is not the approver's business and lives in
    ``recovery``.
    """
    subject: str
    """The words of the call that decided this verdict, or ``""``.

    A flag, a path, a package, the command and subcommand a row names:
    ``--force``, ``docs/commands.md``, ``uv add``. Shown in backticks before
    the reason, so a reader sees which part of a long command is at stake
    without the whole command echoed back at them.
    """
    sandbox: SandboxPlacement
    escalated: str
    """Why the agent said this operation was worth putting to a reviewer.

    Carried as its own field rather than left readable in the reason, because
    a host with no reviewer has somewhere else to send it and needs to know
    that this particular refusal is one somebody asked for. Sniffing the
    reason text for a prefix would make every caller re-derive what the
    marker already stated.

    It survives the collapse to ``deny``: the whole point is that the agent's
    stated intent outlives the refusal, so whoever reads the relay sees why
    the agent thought the operation was worth running.
    """
    checkpoint: CheckpointRequirement
    """What capture would put back what this operation destroys locally.

    The one axis that says nothing about this verdict: it says what a session
    carrying that capture could settle differently. It survives every effect,
    because it is a property of the operation rather than of the verdict
    reached about it.
    """
    unlisted: bool
    """Whether this deferral is only the vocabulary having no row for the call.

    Two very different things reach ``defer``, and a session with no boundary
    beneath it has to tell them apart. This one is the vocabulary staying
    silent: the kernel read the command, found it well-formed, and nothing
    named it. Every other deferral is the kernel declining to read — an
    unresolved expansion, a substitution it cannot see into, an operator its
    parser does not carry — or a form some rule deliberately left out.

    Only the silent one is a question a human can answer, because it is the
    only one where what the human is shown is what will run. Asking about
    text the policy itself could not parse would show them `cat x` and run
    `rm -rf ~`, so those keep refusing however many reviewers are present.
    """
    reviewer: ReviewerRequirement
    """Who may answer, on the one effect that asks anybody. ``human_only``
    unless a rule deliberately opted its question into the supervisor chain."""
    purpose: ReviewPurpose | None
    """Which kind of decision a question is, or ``None`` where it asks nobody."""
    visibility: Visibility
    """Whether an operation that did not interrupt should still be surfaced."""
    cause: RefusalCause | None
    """Why a refusal was reached, or ``None`` where nothing was refused."""
    capability: Capability | None
    """Which runtime guarantee was missing, on a capability-blocked refusal.

    Set only alongside ``cause="capability"``, and the pair is what says the
    refusal is unanswerable: a reviewer cannot approve a channel into being,
    so the operation is refused rather than parked on a question whose answer
    changes nothing.
    """
    abstention: AbstentionPurpose | None
    """Why a ``defer`` declined to finalize, or ``None`` on any other effect.

    ``provider_native`` is the deliberate handoff and the only abstention that
    survives to the provider. ``boundary_settle`` is the classifier lacking a
    judgement the boundary still has facts about, and settlement resolves it
    rather than passing it on — which is the distinction that stopped a parser
    gap from silently inheriting provider auto-mode.
    """
    rule: str
    """The stable id of the rule that reached this verdict, or ``""``.

    Stable across rewordings, because it is what an audit counts by and what a
    person answering a repeated question is pointed at. A reason is prose and
    a rule id is an identifier; a taxonomy built on the first is a taxonomy of
    how sentences were phrased.
    """
    hard: bool
    """Whether this prohibition is one no reviewer may override.

    Almost none are. An ordinary refusal is a rule's judgement, and a rule's
    judgement is exactly what a person with more context may overrule — which
    is what decision escalation exists for. A hard prohibition is a policy
    invariant instead: escalating one produces the same refusal, because the
    thing being asked for is not the kind of thing an approval creates.
    """
    findings: tuple["KernelDecision", ...]
    """The rule verdicts that composed into this one, empty where this is one.

    A composed verdict has to answer questions its own fields cannot: whether
    *every* surviving reason to ask is one a proven capture retires, which is
    the difference between recovery settling an operation to allow and
    recovery quietly discharging a code review that happened to travel beside
    it. Keeping the parts is how that stays answerable, and it is what an
    audit reconstructs a decision from.
    """
    evaluator: str
    """The stable id of the evaluator that produced it, or ``""``.

    Which classifier looked, as against which rule it applied — the shell
    vocabulary, the edit gate, the fetch scopes, the effect grammar. Two rules
    may share an evaluator and one rule never spans two.
    """
    recovery: tuple[Step, ...]
    """The ways through: what the agent can do instead, empty where nothing needs saying.

    Addressed to the agent and never to the approver: a refusal carries it to
    the agent with the reason, and a question carries it beside the prompt
    rather than inside it, so the person deciding reads what is at stake and
    the agent learns its way round if the answer is no. A command a way
    through names is held as the words that run it, never in its prose.
    """
    see: str
    """The page that explains the rest, or ``""``: ``docs/rules.md`` for a rule.

    Named rather than quoted, so a refusal stays short and the rationale, its
    examples and its alternatives are said once, where they are looked up.
    """
    queued: str
    """The review a refused call waits on, or ``""`` where it waits on none.

    A call parked for the operator is refused to the runtime, which has no
    word for "held", and is not refused to the agent: it must not be
    reshaped, and the answer carries it out. Its diagnostic opens on
    ``queued`` rather than ``refused`` for that reason.
    """
    reach: Reach | None
    """Where the harm this verdict guards against lands, or ``None`` if unstated.

    Read by one settlement row: inside a container measured around the
    session, a question whose harm stays inside it asks nobody anything they
    need to answer. Derived from the effects the rule declared, so a verdict
    reached by code that declared none carries ``None`` and keeps its question
    wherever the session runs -- the reading that fails closed.

    On a deferral it is the widest of the readings an unread word could take,
    carried where ``unread`` says there were any.
    """
    unread: bool
    """Whether this deferral stands for a word nobody could read in a judged command.

    Work nobody judged is confined by whatever holds the process, so a
    container settles it. A command the vocabulary judges, with a word that
    could still become a guarded flag or operand, is different: every command
    the word could make was judged, and ``reach`` is where the widest of their
    harms would land -- a remote a push reaches, a merged pull request. A
    container settles it only where that stays inside, and a reading that
    stated no reach leaves ``None``, which never does.

    On a question it marks the strictest reading kept for such a word, which
    a capture never retires: what was captured is what that one reading
    named, and the word could have made any other command.
    """

    file_reviews: tuple[FileReviewRow, ...]
    """Original per-file findings attached by the caller after owner routing."""

    unpreviewed: tuple[UnpreviewedRow, ...]
    """The steps of a command whose effect no document states, in the order they run.

    Beside ``file_reviews`` because the two are one record of what a command
    changes: the files worked out, and the steps nobody could work out
    without running them.
    """

    segments: tuple[SegmentRow, ...]
    """Each command this verdict was joined from, with the verdict it reached alone.

    A line's commands are judged one at a time and joined into one verdict,
    whose reason speaks for all of them; this keeps each command's own, in
    the order they run, and survives the rendering that folds the joined
    reasons into one sentence. A command's own verdict names itself here.
    """

    def __init__(
        self,
        effect: DecisionEffect,
        reason: str = "",
        sandbox: SandboxPlacement = "ambient",
        escalated: str = "",
        checkpoint: CheckpointRequirement = "unrecoverable",
        unlisted: bool = False,
        reviewer: ReviewerRequirement = "human_only",
        purpose: ReviewPurpose | None = None,
        visibility: Visibility = "quiet",
        cause: RefusalCause | None = None,
        capability: Capability | None = None,
        abstention: AbstentionPurpose | None = None,
        hard: bool = False,
        findings: tuple["KernelDecision", ...] = (),
        rule: str = "",
        evaluator: str = "",
        recovery: Sequence[Step] = (),
        reach: Reach | None = None,
        unread: bool = False,
        file_reviews: tuple[FileReviewRow, ...] = (),
        unpreviewed: tuple[UnpreviewedRow, ...] = (),
        segments: tuple[SegmentRow, ...] = (),
        subject: str = "",
        see: str = "",
        queued: str = "",
    ) -> None:
        if effect not in ("allow", "ask", "deny", "defer"):
            raise ValueError(f"invalid kernel decision effect {effect!r}")
        if sandbox not in ("inside", "ambient", "outside"):
            raise ValueError(f"invalid kernel decision placement {sandbox!r}")
        if checkpoint not in ("targeted", "boundary_wide", "unrecoverable"):
            raise ValueError(f"invalid kernel decision checkpoint {checkpoint!r}")
        self.effect = effect
        self.reason = reason
        self.escalated = escalated
        self.checkpoint = checkpoint
        self.unlisted = unlisted
        self.reviewer = reviewer
        self.purpose = purpose
        self.visibility = visibility
        self.cause = cause
        self.capability = capability
        self.abstention = abstention
        self.hard = hard
        self.findings = findings
        self.rule = rule
        self.evaluator = evaluator
        self.recovery = tuple(recovery)
        self.reach = reach
        self.unread = unread
        self.file_reviews = file_reviews
        self.unpreviewed = unpreviewed
        self.segments = segments
        self.subject = subject
        self.see = see
        self.queued = queued
        # Only a verdict this policy actually reached is placed: a refusal is
        # not softened by where the operation would have run, and a deferral
        # hands the whole question over, placement included.
        self.sandbox = sandbox if effect in ("allow", "ask") else "ambient"

    def revised(self, **changes: Unpack[Revision]) -> "KernelDecision":
        """This verdict with named fields replaced and the rest carried over.

        Every settlement row rewrites one or two fields and must not drop the
        fourteen it is not about — which is exactly what a constructor call
        listing the fields a row happens to remember does. Spelled once here
        so a row says what it changes and nothing says what it preserves.

        Each field is read individually rather than merged from a mapping,
        because ``None`` is a value three of them can take and a merge cannot
        tell "clear this" from "leave it alone". Verbose, and the verbosity is
        the whole of what makes a row that clears a purpose distinguishable
        from one that never mentioned it.
        """
        return KernelDecision(
            changes["effect"] if "effect" in changes else self.effect,
            changes["reason"] if "reason" in changes else self.reason,
            changes["sandbox"] if "sandbox" in changes else self.sandbox,
            changes["escalated"] if "escalated" in changes else self.escalated,
            changes["checkpoint"] if "checkpoint" in changes else self.checkpoint,
            changes["unlisted"] if "unlisted" in changes else self.unlisted,
            changes["reviewer"] if "reviewer" in changes else self.reviewer,
            changes["purpose"] if "purpose" in changes else self.purpose,
            changes["visibility"] if "visibility" in changes else self.visibility,
            changes["cause"] if "cause" in changes else self.cause,
            changes["capability"] if "capability" in changes else self.capability,
            changes["abstention"] if "abstention" in changes else self.abstention,
            changes["hard"] if "hard" in changes else self.hard,
            changes["findings"] if "findings" in changes else self.findings,
            changes["rule"] if "rule" in changes else self.rule,
            changes["evaluator"] if "evaluator" in changes else self.evaluator,
            changes["recovery"] if "recovery" in changes else self.recovery,
            changes["reach"] if "reach" in changes else self.reach,
            changes["unread"] if "unread" in changes else self.unread,
            changes["file_reviews"] if "file_reviews" in changes else self.file_reviews,
            changes["unpreviewed"] if "unpreviewed" in changes else self.unpreviewed,
            changes["segments"] if "segments" in changes else self.segments,
            changes["subject"] if "subject" in changes else self.subject,
            changes["see"] if "see" in changes else self.see,
            changes["queued"] if "queued" in changes else self.queued,
        )

    def placed(self, escapable: bool, contained: bool = False) -> "KernelDecision":
        """This verdict as the runtime about to render it will carry it out.

        ``escapable`` is whether this runtime can put an operation outside the
        containment boundary at all. Where it cannot, a placement it will not
        honour must not be spelled, or the verdict reads as escaped while the
        operation runs contained — so the plain effect is rendered instead and
        :class:`~lup.policy.kernel.settlement.TrappedPlacement` is what refuses
        the one case where that substitution would be wrong.

        ``contained`` is where the session sits, as its launch measured it,
        and it decides what the question says about the crossing. A runtime's
        per-call escape reaches the host only where no container is between;
        inside one it lifts the per-call sandbox alone and the call lands in
        the same mount namespace, so the sentence names that rather than a
        host the approval does not buy. An input rather than a reading,
        because the kernel measures nothing: the host hands it in beside
        ``escapable``, and a caller that says nothing is read as a session
        that measured no container — the answer the launch ledger gives when
        it is absent.

        The reason is composed with the contributing verdicts here, because
        this is the last seam every renderer funnels through and the reason is
        the only text a reviewer reads. A composed verdict keeps the one
        reason its join picked; the others were computed, stamped into
        ``findings``, and rendered by nothing — so a compound command asking
        for three things put one question to the person answering all three.

        An approval question over an operation that also leaves asks the
        person two things, so the reason says both. Neither reaches a refusal
        or a deferral: those arrive with the placement already collapsed.
        """
        # The findings go when their reasons do: rendered into the reason,
        # keeping them would compose the same sentences again on a second
        # rendering pass.
        composed = self.revised(
            reason=self.stated_whole(),
            recovery=self.recovered_whole(),
            findings=(),
        )
        if not escapable:
            return composed.revised(sandbox="ambient")
        if self.effect == "ask" and self.sandbox == "outside":
            notice = CONTAINED_ESCAPE_NOTICE if contained else SANDBOX_ESCAPE_NOTICE
            return composed.revised(reason=composed.reason + notice)
        return composed

    def stated_whole(self) -> str:
        """The reason, joined with every contributing reason it does not say.

        Only the parts that reached this verdict's own effect are listed: an
        ask beside a surviving ask is a second question the same approval
        answers, while an allow travelling in a refused command decides
        nothing a reviewer needs to weigh. Every survivor is listed rather
        than the first, since answering is one decision over the whole
        operation.
        """
        peers = [
            stated(part.subject, part.reason)
            for part in self.findings
            if part.effect == self.effect
            and part.reason
            and part.reason not in self.reason
        ]
        if not peers:
            return self.reason
        distinct = dict.fromkeys(peers)
        return "\n".join([self.reason, *(f"also: {said}" for said in distinct)])

    def recovered_whole(self) -> tuple[Step, ...]:
        """The ways through, joined with every contributing one they do not hold.

        The same parts :meth:`stated_whole` lists, for the same reason: a
        refusal over three segments leaves the agent three things to change.
        A way through two parts share is said once.
        """
        return distinct(
            through
            for part in (self, *self.findings)
            if part is self or part.effect == self.effect
            for through in part.recovery
        )

    def advising(self, recovery: tuple[Step, ...]) -> "KernelDecision":
        """This verdict with more ways through after its own.

        A settlement row knows a route the rule that reached the verdict did
        not — a relay this run holds, an escalation marker this runtime reads —
        and adds it without overwriting what the rule already said.
        """
        return self.revised(recovery=distinct((*self.recovery, *recovery)))

    def diagnostic(self) -> Diagnostic:
        """This verdict in the one message shape every reader meets."""
        match self.effect:
            case "allow":
                verdict: Verdict = "allowed"
            case "ask":
                verdict = "asks"
            case "deny" if self.queued:
                verdict = "queued"
            case "deny":
                verdict = "refused"
            case "defer":
                verdict = "deferred"
        return diagnostic(
            verdict,
            self.reason,
            what=self.subject,
            steps=self.recovery,
            rule=self.rule,
            see=self.see,
        )

    def headline(self) -> str:
        """The first line: the verdict, its subject and why.

        The whole of what an approver reads, since what the agent should do
        instead is not theirs to weigh. Empty where the verdict says nothing,
        as a permission with no reason does.
        """
        if not (self.reason or self.subject):
            return ""
        return headline(self.diagnostic())

    def beside(self) -> str:
        """The ways through as the agent reads them beside a question, one a line."""
        return "\n".join(way(through) for through in self.recovery)

    def addressed(self) -> str:
        """The verdict line with each way through after it: the whole of what an agent reads.

        For a channel that reaches the agent alone — a refusal, or a question
        a runtime cannot put to anybody and so turns back — where the approver's
        line and the agent's ways through arrive as one text.
        """
        return rendered(self.diagnostic())


def unjudged(reason: str) -> KernelDecision:
    """One machinery bail-out: the kernel could not read this, so it abstains.

    A ``boundary_settle`` abstention, never a handoff. The classifier has no
    final judgement and the boundary still has facts about the operation, so
    settlement resolves it from those facts rather than passing it to the
    provider's own mode — which is what a gap in the parser must never buy.
    """
    return KernelDecision("defer", reason, abstention="boundary_settle")


def unlisted(reason: str) -> KernelDecision:
    """The same abstention, from the vocabulary simply naming nothing here.

    Separate from :func:`unjudged` because only this one is answerable. The
    command was read and is well-formed; no row speaks about it. A session
    with a reviewer puts that to them, exactly as a decision escalation
    already does, rather than refusing work whose only fault is being new.
    """
    return KernelDecision("defer", reason, unlisted=True, abstention="boundary_settle")


def handed_over(reason: str, rule: str = "", evaluator: str = "") -> KernelDecision:
    """A rule looked, and decided the provider's own mode should answer.

    The one abstention that survives to the provider. Edit size is the shape:
    a large ordinary edit is exactly what a native auto-accept mode exists
    for, and interposing here would replace a decision an operator already
    made. Distinct from :func:`unjudged` in the field rather than the wording,
    because settlement reads the field and a reader of two similar sentences
    reads neither.
    """
    return KernelDecision(
        "defer", reason, abstention="provider_native", rule=rule, evaluator=evaluator
    )


def contributions(decision: KernelDecision) -> tuple[KernelDecision, ...]:
    """The rule verdicts behind one settled verdict, which may be itself.

    A composed verdict keeps its parts and a single rule's verdict is its own
    only part. Spelled once so a settlement row asking "what are all the
    reasons this asks" cannot accidentally ask it of a composed verdict's
    summary fields, which carry the join rather than the reasons.
    """
    return decision.findings or (decision,)


def objecting_reach(
    parts: tuple["KernelDecision", ...], order: list[Reach] = REACHES
) -> Reach | None:
    """Where the harm of every part that asks or refuses lands: the widest of them.

    A composed verdict's own reach, which is what makes it safe to compose
    again. A line is joined from its segments and a segment from what rode in
    it -- an assignment and the command behind it -- so a reader of one level
    of parts meets verdicts that are themselves joins, and each has to carry
    all of what its parts objected to rather than the first part's word.
    ``None`` where any objecting part stated none, or none objects at all.

    A deferral for a word nobody could read objects as well, with the reach
    of what the word could make: `make x && git push $X origin feat` asks
    about a recipe the container holds, and could push anything anywhere.
    """
    stated = [part.reach for part in parts if objecting(part)]
    if not stated or any(word is None for word in stated):
        return None
    return max((word for word in order if word in stated), key=order.index)


def objecting(part: "KernelDecision") -> bool:
    """Whether a verdict puts something a boundary has to answer for.

    A question or a refusal does, and so does a deferral for a word nobody
    could read, which stands for whatever the word could make.
    """
    return part.effect in ("ask", "deny") or (part.effect == "defer" and part.unread)


def carrying_readings(
    deferral: KernelDecision, readings: tuple[KernelDecision, ...]
) -> KernelDecision:
    """A deferral for a word nobody could read, carrying where its readings land.

    Marked only where one of them objects. A word whose every reading is
    allowed could only become that, or a program the vocabulary does not
    know, which a container confines as it confines anything unjudged: so
    `$CMD push origin feat` is settled there, since the `git push` it could
    be asks nothing. Where one objects, the deferral carries the widest reach
    of all of them, the deferral's own among them, and a container settles it
    only where that stays inside.
    """
    parts = (deferral, *readings)
    if not any(objecting(part) for part in parts):
        return deferral
    return deferral.revised(unread=True, reach=objecting_reach(parts))


def joined_checkpoint(
    decisions: list[KernelDecision],
) -> CheckpointRequirement:
    """What puts back what a whole command line destroys.

    The weakest answer any of its segments gave. One line is one act as far
    as a person deciding about it is concerned, so a segment nothing restores
    makes the line one nothing restores -- ``ls && git push --delete`` is not
    made recoverable by the half that only read.
    """
    if any(item.checkpoint == "unrecoverable" for item in decisions):
        return "unrecoverable"
    if any(item.checkpoint == "boundary_wide" for item in decisions):
        return "boundary_wide"
    return "targeted"


def joined_placement(decisions: list[KernelDecision]) -> SandboxPlacement:
    """Where a whole command runs, given what each of its segments needs.

    One command line is one process, so its segments cannot be placed
    apart. Confinement outranks escape: a segment that has to stay inside
    keeps the whole line inside, and only a line where something needs the
    outside and nothing needs the inside leaves.
    """
    if any(item.sandbox == "inside" for item in decisions):
        return "inside"
    if any(item.sandbox == "outside" for item in decisions):
        return "outside"
    return "ambient"


def as_segment(decision: KernelDecision, command: str = "") -> SegmentRow:
    """One verdict as the row a line's record keeps of it: *command*, and what it decided."""
    return SegmentRow(
        command=command,
        effect=decision.effect,
        reason=decision.stated_whole(),
        rule=decision.rule,
    )


def judged_command(decision: KernelDecision, command: str) -> KernelDecision:
    """A verdict one command of a line reached, naming the command as its only row."""
    return decision.revised(segments=(as_segment(decision, command),))


def joined_decision(decisions: list[KernelDecision]) -> KernelDecision:
    """One verdict for a whole line, from what each of its commands decided.

    Each part's commands are kept as the line's rows, in order; a part
    naming none is a row about the line.
    """
    placement = joined_placement(decisions)
    restoration = joined_checkpoint(decisions)
    parts = tuple(decisions)
    segments = tuple(
        row for item in decisions for row in item.segments or (as_segment(item),)
    )
    denied = next((item for item in decisions if item.effect == "deny"), None)
    if denied is not None:
        return denied.revised(
            findings=parts, reach=objecting_reach(parts), segments=segments
        )
    asked = next((item for item in decisions if item.effect == "ask"), None)
    if asked is not None:
        return asked.revised(
            sandbox=placement,
            checkpoint=restoration,
            findings=parts,
            reach=objecting_reach(parts),
            segments=segments,
        )
    deferred = [item for item in decisions if item.effect == "defer"]
    if deferred:
        # The abstention leaving the most to settle speaks for the line: a
        # handoff to the runtime answers for its own segment, never for an
        # unread one beside it, whichever of the two was written first.
        return min(
            deferred,
            key=lambda item: (item.abstention == "provider_native", item.unlisted),
        ).revised(
            findings=parts,
            reach=objecting_reach(parts),
            unread=any(item.unread for item in deferred),
            segments=segments,
        )
    reached = dict.fromkeys(item.rule for item in decisions if item.rule)
    return KernelDecision(
        "allow",
        "every shell segment is declared safe",
        placement,
        checkpoint=restoration,
        rule=next(iter(reached)) if len(reached) == 1 else "",
        evaluator="shell-vocabulary",
        findings=parts,
        segments=segments,
    )


def recovery_dischargeable(decision: KernelDecision) -> bool:
    """Whether a proven capture retires every surviving reason this asks.

    The question recovery is allowed to answer, and the whole of it. Local
    loss a capture puts back is not worth a person's attention; a code
    review, a protected path, an external effect, or a credential read
    travelling in the same operation is worth exactly as much attention as it
    was before, and an operation carrying both keeps its question.

    Read over the contributions rather than the join, because the join
    reports the strongest effect and says nothing about how many reasons
    reached it — which is how a recoverable deletion beside a full-file
    rewrite would have discharged the rewrite. A question kept for a word
    nobody could read is never retired, whatever loss it names.
    """
    asking = [part for part in contributions(decision) if part.effect == "ask"]
    if not asking:
        return False
    return all(
        part.purpose == "unrecovered_local_mutation"
        and part.checkpoint != "unrecoverable"
        and not part.unread
        for part in asking
    )


def file_review_row(
    decision: KernelDecision,
    path: str,
    before_sha256: str | None,
    after_sha256: str | None,
    after: str | None,
) -> FileReviewRow:
    """One file's evidence: *decision* stated whole, bound to the images judged.

    Stated whole because the row is what a reviewer reads for that file, and
    every surviving reason at the verdict's own effect is part of what they
    are approving.
    """
    return FileReviewRow(
        path=path,
        effect=decision.effect,
        reason=decision.stated_whole(),
        rule=decision.rule,
        rules=[part.rule for part in contributions(decision) if part.effect != "allow"],
        before_sha256=before_sha256,
        after_sha256=after_sha256,
        after=after,
    )


def captured_edit_decision(
    decision: KernelDecision,
    path: str,
    *,
    before_sha256: str | None,
    after_sha256: str | None,
    after: str | None,
) -> KernelDecision:
    """Bind the already-routed verdict to the exact images its owner judged.

    The digests are the host's to take, which the kernel reads no hash of;
    ``after`` is the document ``after_sha256`` names, carried whole.
    """
    return decision.revised(
        file_reviews=(
            file_review_row(decision, path, before_sha256, after_sha256, after),
        )
    )
