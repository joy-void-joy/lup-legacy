"""The resolver's mailbox: the shared one, bound to its own question type.

The mechanism lives in :mod:`lup.coordination.mailbox`. Declaring a question
once, letting any door correct an offer, promoting exactly one answer, and
carrying messages on a stream nothing parks on is not about concerns or
leases.

What is here is the binding: which question rides in the slots. Only that, so
this is a specialization rather than a barrel — a caller wanting
``AnswerOffer`` or ``ActorMessage`` imports it from the layer that defines it,
and there is one place each name comes from.
"""

from pathlib import Path

from lup.coordination.cohort import ActorCohort
from lup.coordination.mailbox import PendingQuestion as SharedPendingQuestion
from lup.coordination.mailbox import QuestionMailbox as SharedMailbox
from lup.coordination.mailbox import AnswerOffer, MailboxConflictError
from lup.resolver.record import Journal
from lup.resolver.models import MaterialQuestion, ResolveState


class PendingQuestion(SharedPendingQuestion[MaterialQuestion], frozen=True):
    """One resolver question a run is waiting on.

    A subclass rather than an alias because callers construct it, and an
    alias to a parameterized generic is a type rather than something
    callable.
    """


class QuestionMailbox(SharedMailbox[MaterialQuestion]):
    """This run's mailbox, carrying the resolver's own question.

    A subclass rather than an alias so the question type is supplied once,
    here, and every construction site keeps saying ``QuestionMailbox(root)``.
    """

    def __init__(self, root: Path) -> None:
        super().__init__(root, MaterialQuestion)

    def retired_ids(self) -> list[str]:
        """Read explicit question retirement from the run's atomic authority."""
        path = self.root / "state.json"
        if not path.exists():
            return []
        return ResolveState.model_validate_json(path.read_text()).retired_questions

    def questions(
        self, include_retired: bool = False
    ) -> list[SharedPendingQuestion[MaterialQuestion]]:
        retired = [] if include_retired else self.retired_ids()
        return [item for item in super().questions() if item.question.id not in retired]

    def offer(self, offer: AnswerOffer) -> None:
        if offer.question_id in self.retired_ids():
            raise MailboxConflictError(
                f"question {offer.question_id!r} was retired by integration recovery; "
                "its recorded evidence and answers are retained"
            )
        super().offer(offer)


def run_cohort(mailbox: QuestionMailbox, run_id: str) -> ActorCohort:
    """This run's population, over the mail and the journal the run itself writes.

    Opened for reading and for steering: it lists who the run holds, resolves
    the address a door typed, and posts what that door said. It spawns
    nobody — the run's own process owns every session — so a console in
    another terminal and the orchestrator reach one record rather than two.

    Built here rather than at each door, because a cohort left to build its
    own would open a second ``ActorMail`` beside the one every door writes, and a
    journal on the path the run's own already holds.
    """
    return ActorCohort(
        mailbox.root,
        journal=Journal(mailbox.root),
        mail=mailbox.mail,
        run_id=run_id,
    )
