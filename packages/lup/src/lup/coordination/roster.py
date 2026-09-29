"""Who a population holds: one file per member, and nothing folded.

A registry kept in memory answers only for the process that filled it. That is
the wrong shape for a population whose whole purpose is being reachable: the
door that wants to steer a spawn is often not the process that made it, and a
run resumed after a park has forgotten every address it ever minted. Both cases
read as "no such actor" from a store that was simply never rebuilt.

So the population is on disk, a file per member rather than a stream of
arrivals and departures every reader folds. A directory listing answers the
same questions, and two a fold cannot: whether a member that wrote no
departure is still there, and what it is holding. Both are the file itself —
its modification time, and what it says.

What that costs is history. A stream could say who was here last Tuesday, and
nothing asks: every reader wants who is here, and pays for the replay.
What stops is a store that grows without bound, and rows for sessions that
ended weeks ago crowding out the two that are working.

**The reading is not here.** :mod:`lup.coordination.bare.store` does it, where
the prompt hook and the compiled permission dispatcher reach it too, and what
stays here is the vocabulary a typed caller holds: the model a member reads
back as, and the verbs that revise one.
"""

from collections.abc import Iterator
from datetime import datetime
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, computed_field

from lup.coordination.bare import store
from lup.coordination.refs import ActorRef
from lup.coordination.wake import WakePath, declared_wake


class Delivery(StrEnum):
    """How a message reaches one member, which differs by what that member is.

    A property of the member rather than of the message: what carries a line to
    a peer is decided by what that peer is and who is holding it, and a sender
    that had to choose would be choosing on facts it does not have.
    """

    HOOK = "hook"
    """Handed over in front of the member's next tool call by its own hook.

    What a spawned agent gets, and the only mode that needs nothing running
    beside it: the hook fires because the member takes a turn, so a busy member
    cannot fail to receive and an idle one receives the moment it moves.
    """

    WAITING = "waiting"
    """Left waiting in its mailbox for the member to read when it next looks.

    The mode with no wake at all, and the honest answer for a peer nothing can
    reach: a headless session between invocations, a member on a machine this
    one does not share. The mailbox is the durable record either way — every other
    mode is a wake *on top of* this one, not an alternative to it.
    """


def carried(spelled: str, fallback: Delivery) -> Delivery:
    """The delivery a member file spells, or the fallback where it spells none.

    A spelling no mode answers to falls back rather than raising, because this
    is read on every listing and a store written by a newer library must not
    stop an older one reading who is here.
    """
    return next((mode for mode in Delivery if mode.value == spelled), fallback)


class RosterMember(BaseModel, frozen=True):
    """One member the population holds: who it is, and what it is doing.

    It carries the ref rather than a copy of the parts, so what a reader is
    shown and what the send path recognizes cannot drift apart. The address
    is computed for exactly that reason and still serializes, because a tool
    result that named a member without giving a handle on it would leave an
    operator to reassemble one — which is where the spellings disagreed.
    """

    actor: ActorRef
    task: str
    running: bool
    summary: str = ""
    error: str = ""

    heard: datetime | None = None
    """When this member was last heard from: its own file's modification time.

    One source rather than two. A record saying when a member last *said*
    something beside a pulse for when it was last *seen* would leave a reader
    taking the later of them; a member that touches its own file while it
    lives collapses that into the stat every read already does.
    """

    arrived: datetime | None = None
    """When this member's present standing began.

    Kept apart from ``heard`` because the two move differently — every write
    advances ``heard``, and only a join sets this — and a reader has a
    question only this answers: which departures happened while this member
    was here to have written to the departed.
    """

    worktree: str = ""
    """Where this member is working, absolute, empty where it is nowhere."""

    description: str = ""
    """What this member is doing now, empty until it has said.

    Beside the task rather than replacing it, because a reader wants both: the
    task is what this member is answerable for, and the description is where
    it has got to. Empty is honest for a member that has not spoken, and reads
    as such — the task is still there to fall back on.
    """

    liveness: str = ""
    """Who answers for this member still being there, empty where it answers itself."""

    wake: WakePath = WakePath()
    """What would make this member look, where anything can.

    Beside the delivery mode rather than folded into it, because they answer
    different questions: delivery is how a message is carried and is always
    the file, while this is what nudges the member into reading it.
    """

    delivery: Delivery = Delivery.HOOK
    """How a message reaches this member.

    The spawned default, because a spawned member is opened with the hook that
    makes it true — mail lands in front of its next tool call whether or not it
    thinks to look. A peer says what it can actually do instead.
    """

    parent: str = ""
    """The session this member is a native subagent of, empty for every other member.

    The one relation between rows the roster keeps: a subagent is somebody
    else to the harness, with work and holdings of its own, and still part of
    the session it runs in — listed beneath it, and gone when it is.
    """

    cli_name: str = ""
    """What this member is called now, empty until something named it.

    On the member rather than in a record beside it, because the two would
    always be read together and a rename is the member changing rather than an
    event about it. What it *was* called stays in its file, so a reference
    somebody wrote down still resolves.
    """

    transcript: str = ""
    """The runtime's own transcript of this member's conversation, empty where none is known.

    What its prompt fold last recorded, so a reader following what the member
    is doing reads the file the runtime writes rather than guessing where a
    runtime keeps one.
    """

    @computed_field
    @property
    def address(self) -> str:
        """The spelling that currently reaches this member."""
        return self.actor.label()

    @computed_field
    @property
    def kind(self) -> str:
        """What this agent was spawned as, so a label reads before it is used."""
        return self.actor.kind


def member_identity(actor: ActorRef) -> store.Actor:
    """One member's address, as the store names the file that holds it.

    The whole ref rather than its id, because an id is unique only within a
    kind: two members taken on over one concern — a worker and its reviewer —
    share one, and a store keyed by it would give them one file to answer for
    each other from.
    """
    return store.Actor(kind=actor.kind, id=actor.id, round=actor.round)


def folded_member(member: store.Member) -> RosterMember:
    """One member file, as a typed caller reads it.

    Total rather than validating: every field is coerced to something the model
    accepts, because this is the read path a listing, a hook and a console all
    go through, and a file written by a newer library must leave an older one
    still able to say who is here. What a malformed field costs is that field.
    """
    wake = member.get("wake") or store.Wake()
    conversation = member.get("conversation") or store.Conversation()
    return RosterMember(
        actor=ActorRef(
            kind=store.text(member.get("kind")),
            id=store.text(member.get("id")),
            round=store.whole(member.get("round")),
        ),
        task=store.text(member.get("task")),
        running=bool(member.get("running")),
        summary=store.text(member.get("summary")),
        error=store.text(member.get("error")),
        heard=store.spoken_at(store.text(member.get("heard"))),
        arrived=store.spoken_at(store.text(member.get("arrived"))),
        worktree=store.text(member.get("worktree")),
        description=store.text(member.get("description")),
        liveness=store.text(member.get("liveness")),
        wake=declared_wake(
            store.text(wake.get("runtime")),
            store.text(wake.get("handle")),
            store.text(wake.get("session")),
            store.text(wake.get("home")),
            store.text(wake.get("scope")),
        ),
        delivery=carried(store.text(member.get("delivery")), Delivery.HOOK),
        parent=store.parent_of(member),
        cli_name=store.current_name(member),
        transcript=store.text(conversation.get("transcript")),
    )


class Roster:
    """Every member one population holds, and whether each is still working."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def present(self, actor: ActorRef) -> bool:
        """Whether this exact round already has a file under ``members/``.

        The idempotence test. Two callers legitimately announce one arrival —
        for a spawn, the process that detached the work and the round that
        work then opened; for a peer, one session rejoining after a restart —
        and writing twice would reset what the first had already recorded.
        """
        found = store.read_member(
            store.member_path(self.root, member_identity(actor)), running=True
        )
        return found is not None and store.whole(found.get("round")) >= actor.round

    def announce(
        self,
        actor: ActorRef,
        task: str,
        liveness: str = "",
        delivery: Delivery = Delivery.WAITING,
        worktree: str = "",
        wake: WakePath = WakePath(),
    ) -> None:
        """Put this member's file down, unless one is already standing for it.

        No lock, because a member file is written by that member's own
        processes and nobody else: two of them arriving at once write the same
        thing, and the rename makes the second replace the first whole. What
        does take the store's lock is choosing a *name*, which is the one
        decision made against every other member — and that is the join's,
        not this.

        A member returning after its row was retired writes a fresh file with
        a fresh arrival, which is the honest reading: it was gone, and the
        departures it missed are not its to have been told about.
        """
        if self.present(actor):
            return
        member = store.blank_member(actor.kind, actor.id)
        member["round"] = actor.round
        member["task"] = task
        member["liveness"] = liveness
        member["delivery"] = delivery.value
        member["worktree"] = worktree
        member["wake"] = store.Wake(
            runtime=wake.runtime,
            handle=wake.handle,
            session=wake.session,
            home=wake.home,
            scope=wake.scope,
        )
        store.write_member(self.root, member)

    def spawned(self, actor: ActorRef, task: str) -> None:
        """Record that a member this process started is live."""
        self.announce(actor, task, delivery=Delivery.HOOK)

    def joined(
        self,
        actor: ActorRef,
        task: str = "",
        liveness: str = "",
        delivery: Delivery = Delivery.WAITING,
        worktree: str = "",
        wake: WakePath = WakePath(),
    ) -> None:
        """Record that a peer nobody spawned is present, and how to reach it."""
        self.announce(actor, task, liveness, delivery, worktree, wake)

    def describes(self, actor: ActorRef, description: str) -> None:
        """Record what this member is doing now, under its own lock."""

        def said(member: store.Member) -> store.Member:
            """This member, saying something else about itself."""
            settled = member.copy()
            settled["description"] = description
            return settled

        store.revised(self.root, member_identity(actor), said)

    def finished(self, actor: ActorRef, summary: str = "", error: str = "") -> None:
        """Record that this address has stopped, and how."""
        store.depart(self.root, member_identity(actor), summary=summary, error=error)

    def beat(self, actor: ActorRef) -> None:
        """Record that this member is here now."""
        store.beat(self.root, member_identity(actor))

    def standing(self) -> Iterator[RosterMember]:
        """Every member this population holds, as the read leaves each."""
        for member in store.present(self.root):
            yield folded_member(member)

    def live(self) -> list[RosterMember]:
        """Every member, the ones still working first."""
        return sorted(self.standing(), key=lambda member: not member.running)

    def members(self) -> list[ActorRef]:
        """Every member as the ref that currently reaches it."""
        return [member.actor for member in self.standing()]

    def reaching(self, address: str) -> ActorRef | None:
        """The member an operator's spelling of an address reaches, if any.

        Matched against what each member answers to rather than parsed out of
        the text, so a label this population printed is a label that works and
        a bare id reaches the same member as the full one. Every consumer that
        instead took an address apart disagreed with whatever printed it, and
        a message sent to the spelling shown reached nobody.
        """
        if not address:
            return None
        return next(
            (member for member in self.members() if address in member.addresses()),
            None,
        )
