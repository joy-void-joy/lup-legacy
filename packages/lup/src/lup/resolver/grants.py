"""What each lease in a run currently holds, and who may change it.

A run's grants are decided in two places that cannot be collapsed into one.
A plan names the gates a concern's work needs, which is why the human
approving the plan is the one who approves them; and a worker discovers
mid-flight that it needs a gate nobody could have foreseen, asks through
its own question tools, and a human answers while that session is still
running. The first is known before the lease exists, the second only after.

Both write the same document — :mod:`lup.policy.grants` holds its format and
both readers — so "what does this lease hold" has one answer wherever it is
asked: the canonical policy judging in this process, and the dispatcher the
lease's own plugin runs. The run writes when what it derives changes, and
never on a timer, so a human editing a document is not racing a publisher
that would overwrite them.
"""

from collections.abc import Callable
from pathlib import Path

from lup.policy.grants import (
    LeaseGrants,
    read_allowance_grants,
    write_allowance_grants,
)
from lup.coordination.questions import QuestionAnswer
from lup.policy.identity import ConcernAllowance
from lup.resolver.models import (
    AllowanceRuling,
    ConcernShape,
    allowance_question_id,
)

# lup: ignore[constant-declaration] — one leaf of the run directory layout,
# which is the record's own shape rather than a value a caller supplies
GRANT_DIR = "grants"


def lease_grants(
    lease_id: str, planned: list[ConcernAllowance], answers: list[QuestionAnswer]
) -> list[ConcernAllowance]:
    """Every gate one lease may pass: what it was opened with, plus its own.

    Keyed by the lease rather than by the concern behind it, because a
    `request_allowance` question is recorded under the id of whoever asked
    and not every lease has a concern: the integration lease is reserved, so
    a derivation reaching for a concern list would drop the grants made to
    the one actor this tool exists for. Dropping them is worse than never
    delivering them — the lease's own reader sees a gate it was holding
    disappear, which is what a human's withdrawal looks like.

    One derivation rather than one per reader: the publisher and the session
    launcher disagreeing about what a lease held is the same defect as an
    environment disagreeing with a document, arrived at from the other side.
    """
    approved = {
        answer.question_id
        for answer in answers
        if answer.value == AllowanceRuling.GRANT
    }
    return list(
        dict.fromkeys(
            [
                *planned,
                *[
                    allowance
                    for allowance in ConcernAllowance
                    if allowance_question_id(lease_id, allowance) in approved
                ],
            ]
        )
    )


def concern_grants(
    concern: ConcernShape, answers: list[QuestionAnswer]
) -> list[ConcernAllowance]:
    """Every gate this concern may pass, over the lease that carries its id."""
    return lease_grants(concern.id, list(concern.allowances), answers)


class ParkingLeaseGrants(LeaseGrants):
    """A lease's grants, where losing one stops the lease rather than the work.

    Reading at judgment already makes the current state govern in both
    directions, so a gate taken back stops releasing it immediately — that
    much falls out. What does not fall out is what the worker then
    experiences: denials it cannot explain, for work it was told it could do,
    with nothing saying a human changed their mind. So a document that has
    lost a gate this lease was holding parks the run, which is what this
    project already does for every other decision that belongs to a human.

    The comparison is against what this lease held when it was last judged
    rather than against what it was launched with, so a gate granted
    mid-lease and then taken back is a withdrawal too, and one withdrawal is
    reported once rather than on every judgment after it.
    """

    def __init__(
        self,
        document: Path,
        concern_id: str,
        held: list[str],
        park: Callable[[str], None],
    ) -> None:
        super().__init__(document)
        self.concern_id = concern_id
        self.held = held
        self.park = park

    def granted(self) -> list[str]:
        live = super().granted()
        withdrawn = [name for name in self.held if name not in live]
        self.held = live
        if withdrawn:
            self.park(
                f"{', '.join(withdrawn)} was withdrawn from {self.concern_id} "
                "while its lease was running, so the work it was granted for "
                "is no longer approved"
            )
        return live


class GrantLedger:
    """One run's grant documents, one per lease.

    Keyed by concern because that is the unit a human grants to: a gate
    approved for one concern's work must not release the same gate in a
    sibling's session, and one document per lease is what makes that
    structural rather than a rule someone has to keep applying.
    """

    def __init__(self, root: Path) -> None:
        self.root = root / GRANT_DIR

    def document(self, concern_id: str) -> Path:
        """Where one lease's grants are written and read."""
        return self.root / f"{concern_id}.json"

    def publish(self, concern_id: str, allowances: list[ConcernAllowance]) -> None:
        """Set what one lease holds, as the run derives it for a new session."""
        write_allowance_grants(self.document(concern_id), allowances)

    def extend(self, concern_id: str, allowances: list[ConcernAllowance]) -> None:
        """Add to what one lease holds without taking anything away.

        A session's authority is set when it opens and only ever widened
        while it runs. Rewriting the derivation instead would make a grant
        answered mid-lease narrow a session that legitimately holds more —
        a merger carries every gate the branches it joins were approved for
        — and the reader would take that for a human's withdrawal and park.
        It is also what leaves a human free to edit a document without a
        publisher putting back what they took out.
        """
        held = read_allowance_grants(self.document(concern_id))
        self.publish(
            concern_id,
            [
                *[member for member in ConcernAllowance if member.value in held],
                *[member for member in allowances if member.value not in held],
            ],
        )

    def lease(
        self,
        concern_id: str,
        allowances: list[ConcernAllowance],
        park: Callable[[str], None],
    ) -> ParkingLeaseGrants:
        """Publish what one lease holds, and hand back the reader for it.

        Publishing and reading are one call because the reader has to know
        what it was given to notice what it later loses, and a caller left to
        write the document itself could hand over a reader that never held
        anything.
        """
        self.publish(concern_id, allowances)
        return ParkingLeaseGrants(
            self.document(concern_id),
            concern_id,
            [allowance.value for allowance in allowances],
            park,
        )
