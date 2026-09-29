"""A body of work crossing from one session to another, and what must go with it.

A task says what somebody should do. A handoff says what a *receiver* needs in
order to pick up work already under way, which is a larger thing and fails
differently: the tasks arrive, the locks arrive, and the receiver still redoes
a week because nobody wrote down which three approaches were already tried.

**What a receiver needs is a field, not a gate.** ``open_questions`` refuses to
be empty at construction, and every established result carries the source it
came from and a grade saying how well it is supported. A sender who has
nothing open is not handing over — they are finishing, and closing the task is
the verb for that. Nothing here has a checklist somebody has to remember to
run, because a requirement stated in the type is one that cannot be skipped.

**The grade is a string this library does not interpret.** What grades exist,
and what each is worth, is a project's question — the same question
:mod:`lup.ledger` refuses to answer about node types. What is enforced here is
only that a sender wrote *something* down, because an ungraded result is one
the receiver has to re-derive to trust, which is the cost the handoff exists
to avoid.

**Locks move only where the sender held them.** A session cannot release a
prefix it does not hold, which is an invariant of
:mod:`lup.coordination.touches` and not an accident. So a scope naming a path
somebody else is holding does not take it away from them and does not refuse
the handoff either: it records the contest, with both names on it, the way an
unattributable edit is already recorded. The receiver is told, and the two
sessions settle it the way they would have anyway.
"""

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from lup.coordination.repository import RepositoryPeers
from lup.coordination.tasks import Task
from lup.coordination.wake import wake
from lup.ledger.journal import LedgerStore
from lup.ledger.models import LedgerEdge, LedgerNode, Standing, Surroundings
from lup.types import JsonValue


class Established(BaseModel, frozen=True):
    """One thing the sender takes as settled, and what makes it so.

    Three fields rather than a sentence, because a receiver reads them for
    different reasons: the statement to know what is claimed, the source to
    check it without asking, and the grade to decide whether checking is worth
    the time. Collapsed into prose, the grade is the part that goes missing.
    """

    statement: str = Field(min_length=1)
    source: str = Field(min_length=1)
    """Where it came from — a path, a node id, a command, a URL.

    Required, because "we established X" without a source is the sentence that
    makes a receiver redo the work: they cannot check it and cannot cite it,
    so the only safe thing left is to derive it again.
    """

    grade: str = Field(min_length=1)
    """How well supported it is, in whatever vocabulary the project uses.

    Uninterpreted here. A library that fixed the words would be one every
    adopter argued with, and the vocabularies seen in the wild differ by
    domain — measured against argued against kernel-checked is a mathematics
    project's distinction and not everyone's. What is enforced is that the
    field was filled in.
    """


class Handoff(LedgerNode, frozen=True):
    """One body of work crossing to somebody else, with what it takes to resume.

    A node rather than a message because the receiver is often not listening
    yet: a handoff written to a session that has since stopped is still the
    record the *next* session reads, and a message would have expired with the
    process that received it.
    """

    kind: Literal["coordination:handoff"] = "coordination:handoff"

    to: str = ""
    """Who it went to, empty where it was left for whoever picks the work up.

    Empty is a real state for the same reason a task's holder is: work is
    handed off at the end of a session as often as it is handed to somebody in
    particular, and refusing that would mean the record is not written at
    exactly the moment it is most needed.
    """

    open_questions: list[str] = Field(min_length=1)
    """What is still undecided, and it may not be empty.

    The one requirement this type makes, because a handoff with nothing open
    is not a handoff. Somebody who has settled everything is finishing, and
    `ledger done` is the verb for that — while a receiver handed "it is all
    fine" inherits every unasked question as a surprise.
    """

    established: list[Established] = []
    """What the sender takes as settled, each with its source and its grade.

    Allowed to be empty, unlike the questions: work handed over early has
    genuinely established nothing, and a type that demanded a result would be
    asking the sender to invent one. What it does not allow is a result
    without provenance, which the entry's own fields refuse.
    """

    not_again: list[str] = []
    """Approaches already tried that did not work, so nobody repeats them.

    The cheapest field here and the one that pays most: a dead end costs the
    receiver the same time it cost the sender, and it is invisible in the
    tasks, the locks and the diff. Nothing enforces it, because a sender who
    tried nothing has nothing to say.
    """

    watch: list[str] = []
    """The nodes this work rests on, so the receiver opens on what moved.

    Ids or slugs. The shape a research repository wrote by hand as
    ``watch = [...]`` in every direction file and nothing ever read; here the
    brief reads it, and says which of these have a record newer than the
    handoff — a premise refuted, a question answered, a task closed — before
    the receiver carries a stale one forward.
    """

    done: bool = False
    """Whether the receiver has finished what this carried, as they say so.

    Recorded rather than inferred, for the reason a task's is: nothing else
    in the log knows. A handoff that transferred tasks can be read from them,
    but one carrying work too small or too shapeless to cut into tasks
    transfers none, and reading that silence would leave it held forever --
    by a session that has since stopped, against a receiver with nothing left
    to answer.
    """

    def finished(self) -> bool:
        """A handoff is done with when its receiver says so."""
        return self.done

    def completed(self) -> LedgerNode:
        """This handoff, closed. Work crossing over is work, and it can end."""
        return self.model_copy(update={"done": True})

    def standing(self, around: Surroundings) -> Standing:
        """Whether the work this carried is done, still moving, or unclaimed.

        The receiver's own word first, then the tasks it transferred: the
        handoff is a moment and the work is what continues, so a record
        saying "transferred" would go on saying it long after they finished.
        Either answer is somebody stating an outcome rather than the log
        inferring one from silence.
        """
        if self.done:
            return Standing(label="done", reason=f"{self.to or 'the receiver'} says so")
        moved = [
            node
            for edge in around.outgoing
            if edge.kind == "coordination:transfers"
            and (node := around.at(edge.target)) is not None
        ]
        if moved and all(node.finished() for node in moved):
            return Standing(label="done", reason=f"all {len(moved)} task(s) finished")
        if not self.to:
            return Standing(label="open", reason="left for whoever picks it up")
        return Standing(label="held", reason=f"{self.to} has it")


class Transfers(LedgerEdge, frozen=True):
    """This handoff moved that task, which is what makes the record navigable.

    An edge rather than a list of ids on the handoff, because the question
    asked of it runs the other way as often: a task's reader wants to know
    which handoff brought it here and what came with it, and a field on the
    handoff answers only the direction it was written in.
    """

    kind: Literal["coordination:transfers"] = "coordination:transfers"


class Handover(BaseModel, frozen=True):
    """What one handoff did, in the words its caller reports.

    Carried as a value because three surfaces render it — a skill telling an
    agent, a console telling a person, and the handoff document itself — and
    none of them should have to reassemble the facts from the store.
    """

    handoff: Handoff
    to: str = ""
    transferred: list[str] = []
    """Ids of the tasks whose holder is now the receiver."""

    locked: list[str] = []
    """Paths the sender held and the receiver now holds."""

    contested: list[str] = []
    """Paths in scope that somebody else holds, recorded rather than taken.

    Not an error and not a refusal. Two sessions in one place is sometimes
    right; what it must not be is silent, which is the whole reason this is
    reported back rather than skipped.
    """

    delivered: bool = False
    woken: bool = False
    note: str = ""


def hand_off(
    peers: RepositoryPeers,
    store: LedgerStore,
    title: str,
    open_questions: list[str],
    to: str = "",
    text: str = "",
    established: list[Established] | None = None,
    not_again: list[str] | None = None,
    tasks: list[str] | None = None,
    paths: list[str] | None = None,
    watch: list[str] | None = None,
    root: Path | None = None,
) -> Handover:
    """Move a body of work to a peer, and say exactly what crossed.

    The record lands first and everything after it is reported rather than
    required, which is the order :func:`lup.coordination.delegate.delegate`
    establishes and for the same reason: a handoff whose mail failed is still
    a handoff the next session can read.
    """
    member = peers.address(to) if to else None
    holder = to if member is not None else ""
    # Widened where they cross into the record, because a node's fields are
    # stored JSON and neither `list[str]` nor a model is one by identity.
    questions: list[JsonValue] = list(open_questions)
    results: list[JsonValue] = [entry.model_dump() for entry in established or []]
    dead_ends: list[JsonValue] = list(not_again or [])
    watched: list[JsonValue] = list(watch or [])
    handoff = store.record(
        Handoff,
        title,
        text=text,
        to=holder,
        open_questions=questions,
        established=results,
        not_again=dead_ends,
        watch=watched,
    )

    # The typed read rather than resolving each id, so what comes back is a
    # `Task` by construction: an id naming something that is not one is not a
    # task this can move, and drops out here rather than being narrowed later.
    known = {task.id: task for task in store.read(Task)}
    moved = [known[node_id] for node_id in tasks or [] if node_id in known]
    for task in moved:
        store.relate(Transfers, handoff, task)
        if member is not None:
            store.amend(task.model_copy(update={"holder": to}))

    if member is None:
        return Handover(
            handoff=handoff,
            transferred=[task.id for task in moved],
            note=(
                f"nothing answers to {to!r}, so this is left for whoever picks it up"
                if to
                else "left unheld, for whoever picks the work up"
            ),
        )

    # Whoever is recording this, which the store stamps rather than takes —
    # so a sender cannot hand over a lock by naming somebody else as itself.
    sender = store.author
    taken, disputed = [], []
    for path in paths or []:
        target = Path(path).resolve()
        others = [
            holder_ref
            for claim in peers.holding(target)
            for holder_ref in claim.holders
            if holder_ref.id not in (sender.id, member.id)
        ]
        # A prefix the sender does not hold cannot be released by the sender,
        # so a claim somebody else has is left standing rather than taken
        # away — the receiver is told it is disputed, and the two settle it as
        # usual. Nothing records the dispute: both holders' own files claim
        # the path, and that is what a reader derives it from.
        if others:
            disputed.append(str(target))
            continue
        peers.release(sender.id, target)
        peers.lock(member.id, target)
        taken.append(str(target))

    message = f"{handoff.id}: {title}" + (f"\n{text}" if text else "")
    peers.send(to, message, sender=sender.id)
    reached = next(
        (view for view in peers.listing() if view.member.actor.id == member.id), None
    )
    roused = wake(reached.member.wake, message, root) if reached is not None else None
    return Handover(
        handoff=handoff,
        to=to,
        transferred=[task.id for task in moved],
        locked=taken,
        contested=disputed,
        delivered=True,
        woken=roused.reached if roused else False,
        note=roused.reason if roused else "",
    )
