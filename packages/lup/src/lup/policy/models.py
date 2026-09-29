"""Shared policy vocabulary: semantic events in, allow/ask/deny decisions out.

The native decoders in ``lup.providers.<provider>.native`` translate wire
payloads into these events (shell command, fetch URL, edit batch, unknown
tool); the policies in :mod:`lup.policy.rules` and :mod:`lup.policy.chain`
consume them and return a :class:`Decision`.
"""

from abc import ABC, abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Literal, Self

from pydantic import (
    AnyHttpUrl,
    BaseModel,
    Field,
    StringConstraints,
    ValidationInfo,
    field_validator,
)

from lup.policy.kernel.rows import EditOperation
from lup.policy.kernel.decision import (
    CheckpointRequirement,
    DecisionEffect,
    FileReviewRow,
    KernelDecision,
    SandboxPlacement,
)
from lup.policy.kernel.semantics import (
    AbstentionPurpose,
    Capability,
    RefusalCause,
    ReviewPurpose,
    ReviewerRequirement,
    Visibility,
)
from lup.types import JsonObject, JsonValue

if TYPE_CHECKING:
    from lup.policy.contracts import DeclaredPolicies

type PolicyId = Literal["fetch", "shell", "edit", "unknown-tool"]
"""One semantic decision family a generated hook set enforces.

Each id names the policy for one semantic tool: ``fetch`` for
:class:`FetchUrl`, ``shell`` for :class:`ShellCommand`, ``edit`` for
:class:`EditBatch`, and ``unknown-tool`` for the conservative
:class:`UnknownTool` fallback."""

type UrlPathPrefix = Annotated[str, StringConstraints(pattern=r"^/")]
"""An absolute URL path prefix scoping a fetch rule beneath an origin."""


class ToolIdentity(BaseModel, frozen=True):
    """Opaque source identity retained only for diagnostics."""

    original_name: str
    source_evidence: JsonValue = None


class EditChange(BaseModel, frozen=True):
    """One named file change within an edit operation.

    ``operation`` is what the native call was, not what the documents look
    like: once a preimage is resolved, a whole-file write and an edit that
    happens to replace every line are the same pair of documents. The adapter
    knows which it decoded, so it says, and an adapter that has not been
    taught leaves the default the gates read as an ordinary modification.
    """

    path: Path
    before: str | None = None
    after: str | None = None
    operation: EditOperation = "modify"

    def as_documents(self) -> "EditChange":
        """The whole before and after documents this change would produce.

        A change carrying a preimage fragment is spliced into the file it
        names, the way the edit tool itself would apply it, because the
        kernel's source-aware reading — comment positions, string literals,
        docstrings — only holds for a document that parses as one. A
        creation, a deletion, a file this process cannot read, or a preimage
        the file does not hold exactly once stays as declared and is judged
        conservatively on its own evidence.

        A whole-file overwrite is the other shape that needs resolving, and
        for the opposite reason: it carries no preimage at all, so the file it
        is about to replace *is* the preimage. Reading it here is what lets
        the marker gate see an overwrite — handed ``""`` instead, it compares
        a file's notes against nothing, finds nothing lost, and admits a write
        that erases every one of them.
        """
        if self.after is None:
            return self
        if self.before is None:
            if self.operation != "overwrite":
                return self
            try:
                return self.model_copy(
                    update={"before": self.path.read_text(encoding="utf-8")}
                )
            except (OSError, UnicodeDecodeError):
                return self
        try:
            current = self.path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return self
        if current.count(self.before) != 1:
            return self
        position = current.find(self.before)
        updated = (
            current[:position] + self.after + current[position + len(self.before) :]
        )
        return self.model_copy(update={"before": current, "after": updated})


def undeclared(family: str) -> "Decision":
    """The verdict for a family this composition never declared a policy for."""
    return Decision(
        effect="ask",
        reason=f"no {family} policy is declared, so this call needs approval",
    )


class SemanticToolBase(BaseModel, ABC, frozen=True):
    """One native call as policy understands it, judging itself.

    Each kind knows which declared family judges it, so routing is the tool
    answering rather than a walk naming the kinds. Pydantic's metaclass is an
    ``ABCMeta``, so a kind that does not answer :meth:`decide_under` cannot be
    built at all — a new tool cannot slip past the router by omission.
    """

    @abstractmethod
    def decide_under(self, policies: "DeclaredPolicies") -> "Decision":
        """The verdict this call's own family policy reaches."""

    def as_documents(self) -> Self:
        """This call with fragment evidence resolved into whole documents.

        Most tools already carry everything a policy reads. An edit stated
        as a preimage and its replacement is the exception, and answers for
        itself — a judge fed the fragment loses the source context the
        kernel's reading depends on.
        """
        return self


class EditBatch(SemanticToolBase, frozen=True):
    """The complete set of file changes in one native edit operation."""

    changes: list[EditChange] = Field(min_length=1)
    cwd: Path | None = None

    def decide_under(self, policies: "DeclaredPolicies") -> "Decision":
        if policies.edit is None:
            return undeclared("edit")
        return policies.edit.decide(self)

    def as_documents(self) -> Self:
        return self.model_copy(
            update={"changes": [change.as_documents() for change in self.changes]}
        )


class ShellCommand(SemanticToolBase, frozen=True):
    """A semantic command execution request."""

    command: str
    cwd: Path | None = None
    unsandboxed: bool = False

    def decide_under(self, policies: "DeclaredPolicies") -> "Decision":
        if policies.shell is None:
            return undeclared("shell")
        return policies.shell.decide(self)


class FetchUrl(SemanticToolBase, frozen=True):
    """Retrieval of one known URL."""

    url: AnyHttpUrl

    def decide_under(self, policies: "DeclaredPolicies") -> "Decision":
        if policies.fetch is None:
            return undeclared("fetch")
        return policies.fetch.decide(self)


class SearchWeb(SemanticToolBase, frozen=True):
    """A web search query, distinct from fetching a known URL."""

    query: str

    def decide_under(self, policies: "DeclaredPolicies") -> "Decision":
        """Search has no rule surface at all, so it always asks."""
        return Decision(
            effect="ask",
            reason=f"web search {self.query!r} is not covered by policy",
        )


class UnknownTool(SemanticToolBase, frozen=True):
    """An unclassified native tool invocation retained for audit."""

    identity: ToolIdentity
    input: JsonObject = {}

    def decide_under(self, policies: "DeclaredPolicies") -> "Decision":
        return policies.unknown.decide(self)


type SemanticTool = EditBatch | ShellCommand | FetchUrl | SearchWeb | UnknownTool


class SessionStarted(BaseModel, frozen=True):
    session_id: str


class InputSubmitted(BaseModel, frozen=True):
    text: str


class BeforeTool(BaseModel, frozen=True):
    tool: SemanticTool
    identity: ToolIdentity


class ApprovalRequested(BaseModel, frozen=True):
    tool: SemanticTool
    reason: str


class AfterTool(BaseModel, frozen=True):
    identity: ToolIdentity
    succeeded: bool
    output: str = ""


class SubagentStarted(BaseModel, frozen=True):
    name: str


class SubagentStopped(BaseModel, frozen=True):
    name: str
    succeeded: bool


class CompletionRequested(BaseModel, frozen=True):
    reason: str = ""


class BeforeCompaction(BaseModel, frozen=True):
    reason: str = ""


class AfterCompaction(BaseModel, frozen=True):
    summary: str = ""


type SemanticEvent = (
    SessionStarted
    | InputSubmitted
    | BeforeTool
    | ApprovalRequested
    | AfterTool
    | SubagentStarted
    | SubagentStopped
    | CompletionRequested
    | BeforeCompaction
    | AfterCompaction
)


class Decision(BaseModel, frozen=True):
    """One conservative policy verdict, and where the call it judges runs.

    The two fields are separate axes: :attr:`effect` answers who decides,
    :attr:`sandbox` answers where it runs. :data:`SandboxPlacement` carries
    what each pair means and :meth:`placed` renders one for a given runtime.
    """

    effect: DecisionEffect
    reason: str = ""
    """What stopped the call, as the approver reads it. See
    :attr:`~lup.policy.kernel.decision.KernelDecision.reason`."""
    recovery: str = ""
    """What the agent can do instead. See
    :attr:`~lup.policy.kernel.decision.KernelDecision.recovery`."""
    sandbox: SandboxPlacement = "ambient"
    checkpoint: CheckpointRequirement = "unrecoverable"
    """What capture would put back what this operation destroys locally."""
    reviewer: ReviewerRequirement = "human_only"
    """Who may answer, on the one effect that asks anybody."""
    purpose: ReviewPurpose | None = None
    """Which kind of decision a question is, or ``None`` where it asks nobody."""
    visibility: Visibility = "quiet"
    """Whether an operation that did not interrupt should still be surfaced."""
    cause: RefusalCause | None = None
    """Why a refusal was reached, or ``None`` where nothing was refused.

    Carried rather than left readable in the reason, because a
    capability-blocked refusal and a rule's judgement need different responses
    from the agent and reached the seam indistinguishable without it.
    """
    capability: Capability | None = None
    """Which runtime guarantee was missing, on a capability-blocked refusal."""
    abstention: AbstentionPurpose | None = None
    """Why a ``defer`` declined to finalize, or ``None`` on any other effect."""
    rule: str = ""
    """The stable id of the rule that reached this verdict, or ``""``."""
    evaluator: str = ""
    """The stable id of the evaluator that produced it, or ``""``."""
    escalated: str = ""
    """Why the agent said this call was worth a human, where it said so.

    Survives a collapse to ``deny``, so a host that cannot ask can still
    relay what was asked for. See
    :attr:`~lup.policy.kernel.decision.KernelDecision.escalated`.
    """
    hard: bool = False
    """Whether this prohibition is one no reviewer may override."""
    findings: tuple["Decision", ...] = ()
    """The rule verdicts that composed into this one, empty where this is one.

    What a reviewer is owed past the sentence: every gate that had something
    to say about this call, including the ones that decided nothing. A
    verdict carrying them can name the evidence a reader would otherwise have
    to go find in the diff being approved. See
    :attr:`~lup.policy.kernel.decision.KernelDecision.findings`.
    """

    file_reviews: tuple[FileReviewRow, ...] = ()
    """Original routed file verdicts, bound to the documents they judged."""

    @field_validator("sandbox")
    @classmethod
    def reached(
        cls, sandbox: SandboxPlacement, info: ValidationInfo
    ) -> SandboxPlacement:
        """Hold the kernel's own invariant rather than restating it here.

        ``effect`` is declared first, so it is already validated and readable
        while this field is: a deny or a defer collapses the placement here
        exactly as it does in the kernel, from the same line of code.
        """
        return KernelDecision(info.data["effect"], sandbox=sandbox).sandbox

    def placed(self, escapable: bool, contained: bool = False) -> "Decision":
        """This verdict as a runtime that can, or cannot, place a call sees it.

        ``contained`` is the session's measured placement, which decides what
        an approved crossing is described as; see
        :meth:`~lup.policy.kernel.decision.KernelDecision.placed`.
        """
        kernel = self.as_kernel().placed(escapable, contained=contained)
        return self.model_copy(
            update={
                "effect": kernel.effect,
                "reason": kernel.reason,
                "recovery": kernel.recovery,
                "sandbox": kernel.sandbox,
            }
        )

    @classmethod
    def of(cls, decision: KernelDecision) -> "Decision":
        """The validated verdict one hermetic kernel verdict corresponds to.

        Every settled fact crosses, not the handful a caller remembered. This
        is the one seam between the two spellings of a verdict, so a field
        named on one side and absent here is a field the in-process path
        silently does not have — which is how a capability-blocked refusal
        arrived indistinguishable from a rule's judgement, and how an ask
        arrived with no attributable rule.
        """
        return cls(
            effect=decision.effect,
            reason=decision.reason,
            recovery=decision.recovery,
            sandbox=decision.sandbox,
            checkpoint=decision.checkpoint,
            reviewer=decision.reviewer,
            purpose=decision.purpose,
            visibility=decision.visibility,
            cause=decision.cause,
            capability=decision.capability,
            abstention=decision.abstention,
            rule=decision.rule,
            evaluator=decision.evaluator,
            escalated=decision.escalated,
            hard=decision.hard,
            findings=tuple(cls.of(finding) for finding in decision.findings),
            file_reviews=decision.file_reviews,
        )

    def as_kernel(self) -> KernelDecision:
        """This verdict as the hermetic kernel spells it.

        One conversion rather than a constructor call per caller, because a
        call site listing the fields it happens to remember is how the two
        spellings drift — and what drifts out is whatever was added last,
        which is always the fact nobody has a test for yet.
        """
        return KernelDecision(
            self.effect,
            self.reason,
            self.sandbox,
            self.escalated,
            checkpoint=self.checkpoint,
            reviewer=self.reviewer,
            purpose=self.purpose,
            visibility=self.visibility,
            cause=self.cause,
            capability=self.capability,
            abstention=self.abstention,
            rule=self.rule,
            evaluator=self.evaluator,
            hard=self.hard,
            findings=tuple(finding.as_kernel() for finding in self.findings),
            recovery=self.recovery,
            file_reviews=self.file_reviews,
        )


class ObservationFailure(BaseModel, frozen=True):
    """One observer failure that cannot change the policy verdict."""

    observer: str
    message: str


class PolicyEvaluation(BaseModel, frozen=True):
    """Computed decision plus separately surfaced observer failures."""

    decision: Decision
    observation_failures: list[ObservationFailure] = []
