"""What one operation did, reconstructable without re-running anything.

The distinction that shapes this: an audit record is **evidence, never replay
authority**. Nothing here authorizes an operation, and nothing reads a record
back to decide one — an approval lives in the relay and is spent once there,
so a record that could be replayed would be a second approval channel with no
single-use guarantee behind it.

What a record has to answer is narrower and harder: for any interruption a
person remembers, which rule asked, what it was asking about, who could have
answered, what was measured before the answer, and how it ended. Before
provenance, that question had no answer that did not involve reading the
classifier by hand — 860 asks in the measured corpus carried no reason at
all, and a native tool name says `Bash` for every one of them.

Each field is composed from the component that owns it rather than restated,
so a record cannot disagree with the thing it describes.
"""

from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from lup.policy.boundary import CapabilityEvidence
from lup.policy.checkpoints import Checkpoint
from lup.policy.hostexec import HostDispatch
from lup.policy.kernel.diagnostic import stated
from lup.policy.models import Decision
from lup.policy.operations import Operation
from lup.policy.relay import PersistentQuestion

type Outcome = Literal["executed", "refused", "in_doubt", "abandoned"]
"""How the operation ended, from the record's point of view.

``abandoned`` is the honest name for a question that expired or was cancelled
— nobody refused it and nothing ran — and it is separate from ``refused``
because the two mean different things to somebody counting interruptions:
one is a decision, the other is a queue that outlived its reason.
"""


class AuditRecord(BaseModel, frozen=True):
    """One operation's whole history, as the components that own it reported it.

    Composed rather than restated. The decision carries its own rule id and
    reviewer class, the question carries the answer receipt, the checkpoint
    carries what was measured, and the dispatch carries where it ran — so a
    record that disagreed with one of them would have to have been assembled
    from something other than that component's own value.
    """

    operation: Operation
    fingerprint: str
    decision: Decision
    findings: list[Decision] = []
    """Each rule's own verdict, before the join reported the strongest.

    Kept because the join says what happened and not how many reasons reached
    it — which is the difference between "this asked" and "this asked for four
    separate reasons, three of which a capture answered".
    """
    question: PersistentQuestion | None = None
    checkpoint: Checkpoint | None = None
    dispatch: HostDispatch | None = None
    capabilities: list[CapabilityEvidence] = []
    provider: str = ""
    rendering: str = ""
    """What the provider was actually told, in its own spelling.

    Recorded because a semantic verdict and its rendering can differ — a
    placement a runtime cannot honour is rendered as the plain effect — and
    the question "why did this run unconfined" is answered by the second, not
    the first.
    """
    outcome: Outcome = "refused"
    at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    def interrupted(self) -> bool:
        """Whether a person's attention was actually spent on this."""
        return self.question is not None

    def attributable(self) -> bool:
        """Whether this record can say which rule reached its verdict.

        The property the whole model exists to make checkable. A record that
        answers no here is one nobody can tune from: the interruption
        happened, and the only way to find the gate that caused it is to read
        the classifier.
        """
        return bool(self.decision.rule)

    def narrative(self) -> list[str]:
        """The record as a person reads it, in the order they need it.

        The verdict and its rule first, because that is what somebody looking
        up an interruption came for. Everything else is what they ask next.
        """
        lines = [
            f"{self.operation.summary()}"
            f" — {self.decision.effect}"
            f" ({self.decision.rule or 'unattributed'})",
            f"  reason      {stated(self.decision.subject, self.decision.reason)}",
        ]
        if self.decision.cause:
            lines.append(f"  refused as  {self.decision.cause}")
        if self.decision.capability:
            lines.append(f"  missing     {self.decision.capability}")
        if len(self.findings) > 1:
            lines.append(
                "  reasons     "
                + "; ".join(
                    f"{finding.rule or 'unattributed'}: {finding.effect}"
                    for finding in self.findings
                )
            )
        if self.question is not None:
            answer = self.question.answer
            lines.append(
                f"  reviewed    {self.question.requirement},"
                f" eligible {', '.join(self.question.eligible) or 'nobody'}"
            )
            if answer is not None:
                lines.append(
                    f"  answered    {'yes' if answer.approved else 'no'}"
                    f" by {answer.principal} ({answer.receipt})"
                )
        if self.checkpoint is not None:
            lines.append(
                f"  captured    {self.checkpoint.evidence()}"
                f" ({self.checkpoint.requirement})"
                + (f" — {self.checkpoint.failure}" if self.checkpoint.failure else "")
            )
        if self.dispatch is not None:
            lines.append(
                f"  dispatched  {self.dispatch.executor}"
                f" → {self.dispatch.outcome or 'no outcome recorded'}"
            )
        lines.append(f"  ended       {self.outcome}")
        return lines


class AuditLog:
    """Append-only evidence, read back for reporting and never for authority.

    Append-only for the reason the relay is: the failure worth surviving is a
    crash partway through, and a file rewritten in place has a window where it
    holds neither the old record nor the new one. Reading tolerates a torn
    final line, because that is exactly what a crash mid-write looks like and
    losing every record before it would be the worse answer.
    """

    path: Path

    def __init__(self, path: Path) -> None:
        self.path = path

    def record(self, entry: AuditRecord) -> AuditRecord:
        """Write one record down, returning it so a caller cannot skip the write."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(entry.model_dump_json() + "\n")
        return entry

    def records(self) -> list[AuditRecord]:
        """Every record, in the order they were written."""
        if not self.path.exists():
            return []
        entries: list[AuditRecord] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                entries.append(AuditRecord.model_validate_json(line))
            except ValueError:
                continue
        return entries

    # lup: ignore[dict-str-payload] — keyed by rule id, which is open by
    # construction: a rule declares its own, an adopting project adds rules of
    # its own, and a closed vocabulary here would be a second list of ids to
    # keep in step with the rules themselves
    def taxonomy(self) -> dict[str, int]:
        """How many interruptions each rule produced, most first.

        The measurement the whole model exists for. Counted by rule id rather
        than by reason, because a reason is prose and a taxonomy of prose is a
        taxonomy of phrasings — the same gate reworded twice becomes two rows,
        and the same wording reached by two gates becomes one.
        """
        counted = Counter(
            entry.decision.rule or "unattributed"
            for entry in self.records()
            if entry.interrupted()
        )
        return dict(counted.most_common())
