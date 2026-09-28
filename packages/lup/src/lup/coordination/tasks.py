"""Work somebody still has to do, as a node in the repository's one log.

A task is the cheapest thing this vocabulary has, and deliberately: delegating
is the common case, reached for far more often than anything else here, so it
has to be one line. Everything it requires it requires because a reader could
not act without it, and nothing else is asked.

**Why a ledger node rather than a record of coordination's own.** A touch says
what a live session is holding and expires with that session; a task outlives
whoever wrote it, and is meant to be picked up by somebody who was not there.
That is the same distinction the log exists for. It also puts a task in reach
of an edge from anywhere — a handoff transferring it, a claim it verifies —
which a private table could not offer.

**`needs` and `blocks` are different facts and must not be conflated.**
``needs`` says what *class of input* a task is waiting for, which is what lets
a rendering be ordered without anybody writing "most urgent first" at the top,
and what tells a reader whether a row is a decision to make or a command to
paste. Dependency between tasks is the ``blocks`` edge, node to node.
"""

from typing import Literal

from pydantic import BaseModel

from lup.ledger.models import LedgerEdge, LedgerNode, Standing, Surroundings

type Needs = Literal[
    "", "judgement", "identity", "account", "payment", "command", "review"
]
"""What class of input a task is waiting for, or nothing where it waits on none.

Closed, because the whole value is that a reader can sort by it and know what
each row will cost them. An open string would be a field every writer spells
differently and no rendering can order.

The members are the boundaries collectors deliberately stop at rather than
act through: a judgement only a person can make, an identity or an account
only they hold, a payment, a command this session's sandbox cannot run, and a
review somebody other than the author owes.
"""


# lup: ignore[library-default] — the members of `Needs`, which a CLI validates
# against and which nothing outside this vocabulary may add to
NEEDS_NAMES: list[Needs] = [
    "",
    "judgement",
    "identity",
    "account",
    "payment",
    "command",
    "review",
]
"""Every spelling `Needs` admits, for a surface that has to check one.

Beside the type rather than derived from it, because a caller validating a
word needs the members at run time and a `Literal` does not hand them over
without reaching into typing internals.
"""


class Task(LedgerNode, frozen=True):
    """One piece of work, and whoever is answerable for it.

    Almost nothing is required. A title is the whole of what a task must have,
    because a task nobody has claimed and nobody has scoped is still a real
    thing to have written down — and a gate that refused it would be a gate
    that made delegating expensive enough to skip.
    """

    kind: Literal["coordination:task"] = "coordination:task"

    holder: str = ""
    """Who is answerable for this, empty where nobody has taken it.

    A spelling the roster resolves rather than a member id, so a task written
    against a name somebody typed goes on meaning that peer after a rename.
    Empty is a real state and the honest one for work parked before there was
    anybody to hold it.
    """

    needs: Needs = ""
    paths: list[str] = []
    """What this work touches, taken as locks when somebody claims it.

    Declared rather than observed, unlike a touch, because the point is the
    moment *before* the first write: an agent about to rewrite a package has
    touched none of it, and that is exactly when a second session needs to
    know.
    """

    done: bool = False
    """Whether it is finished, which is the one thing only the holder can say.

    A field rather than an edge because it is a property of the task and not a
    relation to anything, and recorded rather than inferred because nothing
    else in the log knows: no artifact lands when a task is done, and reading
    it from silence would call every parked task finished.

    Set by amending the task, which appends it again rather than changing what
    is written — so what it looked like before, and who closed it, stay
    readable.
    """

    def finished(self) -> bool:
        """A task is done with when its holder says so."""
        return self.done

    def completed(self) -> LedgerNode:
        """This task, closed. Work is the thing "done" means something about."""
        return self.model_copy(update={"done": True})

    def standing(self, around: Surroundings) -> Standing:
        """Where this task is: done, blocked, waiting, held, or open.

        Read in that order because that is the order a reader acts on. Blocked
        outranks held: a task somebody holds and cannot start is not progress,
        and a rendering that showed it as held would send its reader to ask
        the wrong person.

        A blocker that has finished stops blocking without anybody amending
        the edge, because the question is asked of the blocker rather than
        remembered on the relation — the same reason nothing here stores a
        status.
        """
        if self.done:
            return Standing(label="done")
        blocking = around.held_by("coordination:blocks")
        if blocking:
            return Standing(
                label="blocked", reason=f"{len(blocking)} task(s) come first"
            )
        if self.needs:
            return Standing(label="waiting", reason=f"needs {self.needs}")
        if self.holder:
            return Standing(label="held", reason=f"{self.holder} has it")
        return Standing(label="open", reason="nobody has taken it")


class Blocks(LedgerEdge, frozen=True):
    """The source must finish before the target can start.

    A relation rather than a field on either end, because it is a fact about
    the pair: written on the blocked task it would go stale when the blocker
    landed, and nothing would be watching.
    """

    kind: Literal["coordination:blocks"] = "coordination:blocks"


class Delegation(BaseModel, frozen=True):
    """What one delegation did, in the words its caller reports.

    Carried as a value rather than printed here because two surfaces render
    it: a skill tells an agent what happened, and a console tells a person.
    Both need the same facts and neither should have to reassemble them.
    """

    task: Task
    holder: str = ""
    """Who it went to, empty where it was parked for whoever picks it up."""

    locked: list[str] = []
    """Paths taken on the holder's behalf, empty where it was parked."""

    delivered: bool = False
    """Whether mail reached the holder's mailbox."""

    woken: bool = False
    """Whether anything has actually made the holder look."""

    note: str = ""
    """Why nobody was woken, for a target that is parked or unreachable."""
