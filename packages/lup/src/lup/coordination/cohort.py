# lup: ignore[constant-declaration]
# The constants here name the cohort's own on-disk layout, which a spawning
# process and an outside door must spell alike to meet at all — an identity
# of this format rather than a choice a caller can make.
"""A population of agents that stay in contact with the outside while they work.

One session per agent, held open across every turn it takes; mail that lands in
front of an agent's next tool call whether or not it thinks to look; and an
address for whoever spawned them, so the contact goes both ways. That is the
whole shape, and it is the same shape whether the agents were decided before
anything started or minted a moment ago.

Two ways to run one, because they are different work — and the difference is
what makes the rest of this reachable at all. A short check is *asked* and
awaited, and the answer is what the caller wanted. A long one is *started*, and
the caller keeps its turn. Only the second leaves anyone able to say anything:
a caller blocked inside a call is a caller that cannot make another, so a
population of awaited spawns has steering tools that can never fire.

What the cohort does not leave to its callers is the wiring. An agent is
addressable only if the mailbox hook is in the options its session was opened
with, so a caller assembling that itself has a way to produce an agent nobody
can reach by forgetting one step. Callers pass a recipe and the cohort hands it
the hooks.

Nothing here knows what the agents are for. A resolver names one worker per
concern and derives every id from durable state; a research session mints ids
for spawns nobody declared. Both are this type, with an id supplied or not.

The root is used as given rather than nested under a directory of this layer's
choosing. Where a cohort's files sit beside a consumer's own — a resolver's run
directory holds both — the consumer is the one that knows whether they belong
together, and a layer that nested unconditionally would put the sessions
somewhere no resumed run would look for them.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager, nullcontext
from datetime import datetime
from pathlib import Path
from typing import Protocol, runtime_checkable
from uuid import uuid4

from pydantic import BaseModel, TypeAdapter

from lup.coordination.mail import ActorDelivery, ActorMail
from lup.coordination.manifest import CohortManifest, publish_manifest
from lup.coordination.peers import USER_KIND, join_user
from lup.coordination.refs import ActorRef
from lup.coordination.roster import Delivery, Roster, RosterMember
from lup.coordination.sessions import (
    RECORD_ADAPTER,
    ActorEvent,
    ActorMailbox,
    ActorJournal,
    ActorRecord,
    ActorSession,
    create_mailbox_hooks,
)
from lup.channels.models import Door, publish_atomic, utc_now
from lup.policy.hooks import LupHooksConfig
from lup.observability.journal import ChainedWriter, Journal, JournalRecord
from lup.sessions.events import TurnResult
from lup.sessions.surface import Agent

logger = logging.getLogger(__name__)

SESSION_DIR = "sessions"
JOURNAL_FILE = "journal.jsonl"
JOURNAL_LOCK = "journal.lock"
"""What writers take before appending, beside the file they are ordering.

Its own path rather than the journal itself, because a lock taken on the file
being appended to is a lock every reader of that file also has to know about.
"""


type ActorRecipe = Callable[[ActorRef, LupHooksConfig], Agent]
"""How one actor's session is configured, given the hooks that reach it.

The hooks are a parameter rather than something a recipe fetches, because
delivery depends on them being in the options the session opens with. A recipe
that must remember to go and get them is a recipe that can be written once
without them, producing an agent that looks spawned and answers to nothing.

The ref rides along because a recipe usually needs it: a worker's tools are
bound to the concern its id names, and its permissions to the lease that id
holds.
"""


type WorkSettles = Callable[[BaseException], bool]
"""Whether a raise out of an agent's work means that agent is done.

A raise usually settles the agent it came out of, because an agent whose turn
died is not going to take another. Suspension is the case where that stops
holding: a piece of work can stop because it was *parked* on a question,
*drained* at a boundary, or stopped by a *host* that failed — and every one of
those expects the same agent to carry on once the reason is gone. An agent
recorded finished while its work is merely suspended is the report that sends
somebody looking for a failure that did not happen, and what resumes opens a
fresh conversation instead of reattaching to the one that was already holding
the context.

Only the consumer knows which of its own failures suspend, so the judgement
reaches it as a default rather than a fixed rule: everything settles unless a
caller says otherwise, which is what a caller with no suspension of its own
already meant.

It is the population's, not each wave's. A suspension is raised in both places
a raise can happen — a drain checked between rounds comes out of the work, and
a host fault comes out of the turn itself — so a judgement held by the wave
answers for one and not the other, and the agent is finished by the turn's own
failure path before the wave is ever consulted. One per cohort is also the
truth of it: which failures suspend is a fact about the consumer's vocabulary,
and a consumer has one.
"""


class CohortEntry(JournalRecord[ActorRef], frozen=True):
    """One thing an actor did, or one thing said to it."""

    at: datetime
    event: ActorEvent


class CohortJournal(Journal[ActorRef, CohortEntry]):
    """The default record, for a cohort whose consumer keeps none of its own.

    Its own file rather than the spawning session's transcript, because the two
    answer different questions: a transcript is what that session did, and this
    is what was said to whom and whether it landed. A redirect that reached
    nobody is only visible here.

    Written under a lock, because this file has several writers and the default
    does not. :class:`~lup.observability.journal.AppendWriter` holds its
    sequence in memory and says so — "for a log with one writer" — while every
    process that reaches a cohort appends here: the session that spawned it,
    each spawned agent's own tools, and a console door in a third terminal.
    Each would start counting from what the file held when *it* first appended,
    so two of them write the same sequence number and the record stops being an
    order at all. :class:`~lup.observability.journal.ChainedWriter` re-reads the
    head inside the lock it writes under, which is the property this needs.
    """

    def __init__(self, root: Path) -> None:
        super().__init__(
            root / JOURNAL_FILE,
            TypeAdapter(CohortEntry),
            ChainedWriter(root / JOURNAL_LOCK),
        )

    def append(self, actor: ActorRef, event: ActorEvent) -> CohortEntry:
        """Record one event against the actor that produced it."""
        return self.write(
            lambda seq, _previous: CohortEntry(
                seq=seq, at=utc_now(), actor=actor, event=event
            )
        )

    def conversation(self, actor: ActorRef, after_seq: int = -1) -> list[CohortEntry]:
        """One conversation's own slice of the record, from a cursor forward.

        By conversation rather than by ref, which is what
        :meth:`~lup.observability.journal.Journal.for_actor` matches. A round
        is an attempt
        and not a new agent, so a reader following a worker into its second
        round would otherwise watch that agent's record stop at the moment it
        got another go.

        This is the half that makes a *working* agent legible. Its turn events
        are drained here as they happen, so what it has found so far is on
        disk long before it returns; what a caller needs is the read.
        """
        return [
            entry
            for entry in self.read(after_seq)
            if entry.actor.conversation() == actor.conversation()
        ]


@runtime_checkable
class CarriesSummary(Protocol):
    """A submission that names what it found, in a field called ``summary``."""

    summary: str


def submitted_summary(output: BaseModel | None) -> str:
    """What a finished agent found, read off the result it has already given.

    A summary that costs another model call is one nobody takes on a path that
    has already produced its answer, so this reads a ``summary`` field where
    the submission carries one and reports nothing where it does not. A caller
    whose result names it differently passes its own.
    """
    if not isinstance(output, CarriesSummary):
        return ""
    # The protocol declares the field's type; matching one at runtime proves
    # only that the field is there, so what it holds is still asked about.
    return output.summary if isinstance(output.summary, str) else ""


class ActorCohort:
    """Every agent one session holds, and how to reach any of them.

    Held by the process that runs the work, because that is where the sessions
    live. Everything an outside door needs — who exists, what each was asked,
    what is queued for whom — is on disk under the cohort's root, so steering
    from another process is the same operation as steering from this one.
    """

    def __init__(
        self,
        root: Path,
        journal: ActorJournal | None = None,
        mail: ActorMail | None = None,
        run_id: str | None = None,
        description: str = "",
        parallel: int | None = None,
        settles: WorkSettles = lambda _: True,
    ) -> None:
        self.root = root
        self.settles = settles
        self.run_id = run_id or root.name
        # What this cohort reads back, which is not always what it writes to.
        # A consumer keeping a journal of its own keeps one in its own
        # vocabulary — the resolver's carries what the *run* did as well as
        # what each actor did — and folding an agent's own stream out of that
        # is not this layer's to do. The file is the one the root names either
        # way, so the read is over the cohort's record wherever the writes go.
        self.record = CohortJournal(self.root)
        self.journal = journal or self.record
        # Taken rather than always built, because a consumer that already has
        # one must not end up with two. A question mailbox holds mail of its
        # own over the same directory, and a cohort that opened a second
        # stream beside it would give the two halves of one conversation
        # different files to disagree in.
        self.mail = mail or ActorMail(self.root)
        self.roster = Roster(self.root)
        # What makes this directory a cohort to a reader that did not open it,
        # and the address a member reaches a person on — written and joined
        # here rather than by each consumer, because three call sites
        # reconstructing the same convention is what a manifest prevents.
        self.manifest: CohortManifest = publish_manifest(
            self.root, self.run_id, description
        )
        self.user = join_user(self.roster)
        # Keyed by conversation rather than by label, because a round is an
        # attempt and not a new agent: a worker's second round is the session
        # that took its first, and keying by the label held two of them.
        self.sessions: dict[str, ActorSession] = {}
        self.mailboxes: dict[str, ActorMailbox] = {}
        self.running: dict[str, asyncio.Task[None]] = {}
        # The cap belongs to the population rather than to each caller that
        # fans out over it. An agent still waiting on it has been recorded
        # nowhere and has opened no session, so an interruption leaves it
        # exactly as it was before the fan-out — which is what makes a cut
        # wave resumable rather than half-spent. A caller holding its own
        # semaphore around `start` gets the opposite: the roster carries a
        # spawn for an agent that never ran.
        self.admitted: AbstractAsyncContextManager[object] = (
            asyncio.Semaphore(parallel) if parallel else nullcontext()
        )

    def actor(self, kind: str, id: str | None = None, round: int = 1) -> ActorRef:
        """An address in this cohort, derived from what a caller has or minted.

        A caller with durable state to name an agent by passes it, and the
        address is then stable across a restart — which is what lets a resumed
        run reattach to the conversation it left rather than open a fresh one.
        A caller with nothing to derive from omits it and gets a mint.

        That is the only difference between the two cases, and it is why they
        are one method: everything downstream — the held session, the mail, the
        record — is identical either way.
        """
        return ActorRef(kind=kind, id=id or uuid4().hex[:8], round=round)

    def path(self, actor: ActorRef) -> Path:
        return self.root / SESSION_DIR / f"{actor.conversation()}.json"

    def mailbox(self, actor: ActorRef) -> ActorMailbox:
        """This conversation's mail, kept current with the round it is on.

        One object per conversation rather than one per caller, because the
        hook that interrupts a live turn and the collection that heads the next
        one are two views of one stream. Handing each its own left them with
        two positions over it, and a message could sit behind both.
        """
        held = self.mailboxes.get(actor.conversation())
        if held is None:
            held = ActorMailbox(self.mail, self.journal, actor)
            self.mailboxes[actor.conversation()] = held
        held.actor = actor
        return held

    def persisted(self, actor: ActorRef) -> ActorRecord | None:
        """The identity this actor left behind, if it has run before."""
        path = self.path(actor)
        if not path.exists():
            return None
        try:
            return RECORD_ADAPTER.validate_json(path.read_text("utf-8"))
        except ValueError:
            logger.exception("Discarding unreadable actor record at %s", path)
            return None

    def session(self, actor: ActorRef, recipe: ActorRecipe) -> ActorSession:
        """This actor's session, resumed from its record the first time.

        The recipe is handed this actor's mailbox hooks, so what it opens is
        reachable mid-turn without the caller having arranged anything.
        """
        held = self.sessions.get(actor.conversation())
        if held is not None:
            held.actor = actor
            return held
        mailbox = self.mailbox(actor)
        opened = ActorSession(
            actor,
            recipe(actor, create_mailbox_hooks(mailbox)),
            self.journal,
            self.persisted(actor),
            mailbox,
        )
        self.sessions[actor.conversation()] = opened
        return opened

    def spawn(self, actor: ActorRef, task: str) -> None:
        """Record that this address is live, before anything is said to it."""
        self.roster.spawned(actor, task)

    async def finish(self, actor: ActorRef, summary: str = "", error: str = "") -> None:
        """Record that this agent's work concluded, and let go of its session.

        Closing here rather than leaving it to the caller, because finishing is
        exactly the moment the session stops being worth holding — and the
        record is saved first, so an agent asked for again reattaches to the
        conversation instead of starting a fresh one.
        """
        self.roster.finished(actor, summary=summary, error=error)
        await self.retire(actor)

    async def retire(self, actor: ActorRef) -> None:
        """Save and close one actor's session without saying its work ended."""
        held = self.sessions.pop(actor.conversation(), None)
        if held is None:
            return
        self.save(actor, held)
        self.mailbox(actor).record_outstanding()
        await held.close()

    def save(self, actor: ActorRef, held: ActorSession | None = None) -> None:
        """Persist one actor's identity so a resumed run reattaches to it."""
        found = held or self.sessions.get(actor.conversation())
        if found is not None:
            publish_atomic(self.path(actor), found.record)

    def live(self) -> list[RosterMember]:
        """Every agent this cohort holds, the ones still working first.

        The agents, which is one member short of the roster: the person this
        cohort answers to is on it and is not one of them. A listing that
        included them would answer "what did I start?" with something nobody
        started, and every reader counting agents would carry the same
        exception — while a person's address needs no discovering, being the
        same word in every cohort.
        """
        return [member for member in self.roster.live() if member.kind != USER_KIND]

    def members(self) -> list[ActorRef]:
        """Every address on this roster, the person's included.

        Wider than :meth:`live` on purpose, because the two answer different
        questions. This one is asked by whatever routes a message, and routing
        that skipped the one member a report is for would be the delivery bug
        the person being on the roster exists to close.
        """
        return self.roster.members()

    def reaching(self, address: str) -> ActorRef | None:
        """The agent an operator's spelling of an address reaches, if any.

        Folded from the record rather than from what this process spawned, so
        a console in another terminal resolves the same address the cohort's
        own tools do — and so a run resumed after a park can still be steered.

        A broadcast token resolves to nobody on purpose: it is not one agent,
        and a door that answered it with the first member would deliver to one
        recipient what was meant for all of them.

        One fold and no cases. A person resolved ahead of the roster by a
        branch of their own makes every other reader of a roster half-right:
        a listing does not show them, a peer cannot find them, and whether an
        address reaches anybody depends on which of the two paths a caller
        happens to be on.
        """
        return self.roster.reaching(address)

    def say(
        self,
        actor: ActorRef,
        text: str,
        redirect: bool = False,
        door: Door = Door.AGENT,
        in_reply_to: str = "",
    ) -> None:
        """Put something in front of one agent's next tool call.

        A redirect refuses that call and hands back the text as its reason, so
        the agent cannot take another step down what it was doing without
        reading why it was stopped. An ordinary message rides alongside and it
        keeps going.
        """
        self.post(
            actor.label(), text, redirect=redirect, door=door, in_reply_to=in_reply_to
        )

    def notify(self, text: str, door: Door = Door.AGENT, by: str = "") -> None:
        """State something that is true for this whole population.

        Two effects, because a statement has two audiences. It is posted as a
        **notice**, which is state: every member reads it at the head of every
        turn, including a member spawned an hour from now, and nothing
        consumes it because it has not stopped being true. And it is sent as a
        **message** to whoever is live, because a fact worth stating is worth
        hearing before the turn they are in ends.

        A member that arrives later gets only the notice, which is the whole
        of what it needs: the message was the interruption, and there was
        nothing to interrupt.
        """
        self.mail.notify(text, door=door, by=by)
        for member in self.live():
            if member.running:
                self.say(member.actor, text, door=door)

    def redirect_all(self, text: str, door: Door = Door.AGENT) -> None:
        """Stop every agent that is working, and say what to do instead.

        Whoever is live, and nobody else. A redirect denies a tool call, so a
        member that has not started has no call to deny and no reason to be
        stopped — one spawned after this was spawned *knowing* about it, and
        stopping it would refuse its first call with somebody else's reason.

        Where the point is a standing fact rather than a stop, that is
        :meth:`notify`, which does reach the ones that arrive next.
        """
        for member in self.live():
            if member.running:
                self.say(member.actor, text, redirect=True, door=door)

    def tell_user(
        self, text: str, door: Door = Door.AGENT, in_reply_to: str = ""
    ) -> None:
        """Say something to the person this cohort answers to.

        A roster member with a mailbox and no session, which is what makes
        contact symmetric: an agent volunteering something uses the verb that
        steers it, and what it says lands somewhere a person can read rather
        than nowhere.
        """
        self.say(self.user, text, door=door, in_reply_to=in_reply_to)

    def post(
        self,
        to_actor: str,
        text: str,
        redirect: bool = False,
        door: Door = Door.AGENT,
        in_reply_to: str = "",
    ) -> bool:
        """Write one message to whatever address a caller already holds.

        The one place a message is built, so a door with a raw address string
        and a caller with a ref reach one member's mailbox the same way — and
        the one place the address is resolved, because a mailbox belongs to a
        member and a spelling that reaches nobody has no mailbox to go in.

        Says whether it landed. A door that was told "sent" for an address the
        population never heard of is a door that goes on believing somebody
        was told something.
        """
        member = self.reaching(to_actor)
        if member is None:
            return False
        self.mail.send(
            member,
            text,
            door=door,
            sender=self.run_id,
            in_reply_to=in_reply_to,
            redirect=redirect,
        )
        return True

    def heard(self) -> ActorDelivery:
        """What agents have told the user, consuming none of it."""
        return self.mail.waiting(self.user)

    def hear(self) -> ActorDelivery:
        """Take what agents have told the user, for a door displaying it."""
        delivery = self.heard()
        self.mail.delivered(self.user, delivery)
        return delivery

    def reaches(self, actor: ActorRef) -> bool:
        """Whether this address is still working, and so will read what it is sent.

        The honest half of "delivered". Posting cannot know that anybody read a
        message — that is what :meth:`outstanding` answers afterwards — but it
        can know whether there is still a session that ever will. Mail to an
        agent whose rounds have ended sits in the file permanently, and a sender
        told otherwise goes on steering something that stopped listening.
        """
        conversation = actor.conversation()
        return any(
            member.actor.conversation() == conversation and member.running
            for member in self.roster.standing()
        )

    def delivery(self, actor: ActorRef) -> Delivery:
        """How a message to this member is carried, off its own roster entry.

        Read rather than assumed, because it differs by what the member is: an
        agent this process spawned holds a hook that puts mail in front of its
        next tool call, and a peer that walked in may have nothing but the
        file. A sender told only that the mail accepted a message cannot tell
        those apart, and the difference is whether anything will wake.

        A member nothing recorded is reached the way a member that declared
        nothing is: the mode that needs no process beside it. Guessing the
        other way would tell a sender something is about to be woken when the
        record does not say so.
        """
        conversation = actor.conversation()
        return next(
            (
                member.delivery
                for member in self.roster.standing()
                if member.actor.conversation() == conversation
            ),
            Delivery.MAILBOX,
        )

    def outstanding(self, actor: ActorRef) -> int:
        """How much this agent has been sent and not yet been handed.

        What makes "sent" answerable. The mail accepting a message is not the
        same as anyone reading it, and a sender told only the first has no way
        to find out about the second.
        """
        return len(self.mail.waiting(actor).messages)

    async def round[T: BaseModel](
        self,
        actor: ActorRef,
        prompt: str,
        output: type[T],
        recipe: ActorRecipe,
        task: str = "",
    ) -> TurnResult[T]:
        """Run one turn of an agent that is not finished by having taken it.

        A turn and a lifetime coincide only for an agent asked once. An agent
        that revises over several rounds is the same agent between them, and
        recording it finished after each round says two false things at once:
        every door reads a working agent as stopped, and the roster carries a
        finish for work that is still moving.

        So the round is recorded and the agent left live. A turn that raises
        is usually the exception, because an agent whose turn died is not
        going to take another — that finishes, with the error against its
        address.

        Usually, because a suspension is raised here too. A host fault comes
        out of the turn rather than out of the work around it, and finishing
        on it unconditionally settled the agent before the population's
        judgement was ever asked — costing the retry the conversation it
        meant to reattach to, which is the one thing that judgement exists to
        protect.
        """
        self.spawn(actor, task or prompt)
        try:
            return await self.session(actor, recipe).turn(prompt, output)
        except Exception as error:
            if self.settles(error):
                await self.finish(actor, error=str(error))
            raise

    async def ask[T: BaseModel](
        self,
        actor: ActorRef,
        prompt: str,
        output: type[T],
        recipe: ActorRecipe,
        task: str = "",
    ) -> TurnResult[T]:
        """Run one agent to an answer, recording what it was asked and gave.

        The caller waits, so this is for work whose result is what the caller
        wanted. It is still addressable while it runs — by a door in another
        process, or by a sibling — because the mail is on disk and the hook is
        in the session either way. What it is not is steerable by *this*
        caller, who is inside the call.

        One round and then done, which is the one-shot case of :meth:`round`
        rather than the general one.
        """
        result = await self.round(actor, prompt, output, recipe, task)
        await self.finish(actor, summary=submitted_summary(result.output))
        return result

    def start[T: BaseModel](
        self,
        actor: ActorRef,
        prompt: str,
        output: type[T],
        recipe: ActorRecipe,
        task: str = "",
        then: Callable[[TurnResult[T]], Awaitable[None]] | None = None,
    ) -> ActorRef:
        """Set one agent working and keep the caller's turn.

        This is what makes a cohort worth having. The caller returns
        immediately, so it can spawn more, answer a question, or say something
        to what it just started — none of which is reachable from inside an
        awaited call.

        The failure is recorded against the agent rather than swallowed: the
        roster carries it, the log carries the traceback, and the task keeps
        the exception for whoever gathers it.
        """
        return self.start_work(
            actor,
            lambda opened: self.ask(opened, prompt, output, recipe, task),
            task=task or prompt,
            then=then,
        )

    def start_work[T](
        self,
        actor: ActorRef,
        work: Callable[[ActorRef], Awaitable[T]],
        task: str = "",
        then: Callable[[T], Awaitable[None]] | None = None,
    ) -> ActorRef:
        """Set a whole piece of work going under one address, keeping the turn.

        Work rather than a turn, because the unit a caller runs concurrently
        is rarely a single turn. A concern carried through a worker turn, a
        commit, a review and a revision round is one agent's work from the
        roster's side, and a cohort that could only detach single turns left
        every such caller to fan out for itself — with its own cap, its own
        task set, and its own answer to who is running. Three copies of what
        the population already is.

        The work itself says whether it finishes the agent: what it runs is
        rounds, and only it knows which round was the last. What is recorded
        here is the failure — but only where the raise settles the agent,
        which the population's :data:`WorkSettles` decides. Work that stopped
        because it was suspended expects the same agent to carry on, so
        recording it finished would cost the resume the conversation it meant
        to reattach to.

        The address is recorded before the cap rather than behind it, so an
        agent queued behind a full cap is one the caller can already list and
        say something to. Mail is held by address and read at a session's
        first tool call, so nothing about that needs the work to have
        started — and a caller that had to wait for a slot before it could
        steer what it started would leave a caller that cannot steer.
        The round this opens announces the same round again and the roster
        keeps one record of it.
        """
        self.spawn(actor, task)

        async def admitted_work() -> None:
            async with self.admitted:
                try:
                    result = await work(actor)
                except Exception as error:
                    logger.exception("%s failed", actor.label())
                    if self.settles(error):
                        await self.finish(actor, error=str(error))
                    raise
                if then is not None:
                    await then(result)

        self.running[actor.conversation()] = asyncio.create_task(admitted_work())
        return actor

    async def work_all[T](
        self,
        work: Callable[[ActorRef], Awaitable[T]],
        over: list[ActorRef],
        task: str = "",
    ) -> list[T | BaseException]:
        """Run one piece of work per address at once, and hand back each answer.

        The wave shape, which is what a caller fanning out over a population
        actually has: the same work, one agent per address, all of it under
        the population's cap and all of it recorded against the population's
        own task set — so a close reaches it and a door listing who is running
        sees it. A caller assembling this from :meth:`start_work` and a gather
        of its own gets the cap right and the other two wrong.

        Results come back positionally against ``over``, each either what that
        agent's work returned or what it raised. Faithfully, and including a
        ``BaseException`` that is not an ``Exception``: a caller that
        classifies failures — this one was a park, this one was the host —
        cannot do it from a flattened list, and one that cannot see a
        cancellation reads an interrupted wave as a wave that decided
        something.
        """
        caught: dict[str, T | BaseException] = {}

        def remembering(run: Callable[[ActorRef], Awaitable[T]]):
            """The same work, keeping what it answered under its own address."""

            async def kept(opened: ActorRef) -> None:
                try:
                    caught[opened.conversation()] = await run(opened)
                # lup: ignore[except-baseexception] — taken and re-raised
                # unconditionally, so nothing is swallowed; a cancellation the
                # caller cannot see reads as a wave that decided something
                except BaseException as error:
                    caught[opened.conversation()] = error
                    raise

            return kept

        for actor in over:
            self.start_work(actor, remembering(work), task=task or actor.id)
        await self.wait_all()
        return [caught[actor.conversation()] for actor in over]

    async def wait_all(self) -> None:
        """Wait for everything started, whatever each of them does.

        Failures are gathered rather than raised, because one agent going down
        must not cost the others their results — and every one of them has
        already been recorded against its own address.
        """
        await asyncio.gather(*self.running.values(), return_exceptions=True)
        self.running.clear()

    async def wait_any(self) -> None:
        """Wait until one started agent finishes, leaving the rest working."""
        if not self.running:
            return
        await asyncio.wait(self.running.values(), return_when=asyncio.FIRST_COMPLETED)
        for conversation in [
            conversation for conversation, task in self.running.items() if task.done()
        ]:
            del self.running[conversation]

    async def close(self) -> None:
        """Stop whatever is still running, recording what none of it read.

        A sender is told a message was sent on the strength of the mailbox
        accepting it, which is not the same as anyone reading it. What is still
        queued as the sessions close is therefore recorded against the agent it
        was for — outstanding across a park, and never read at all on a run
        that ended.
        """
        for task in self.running.values():
            task.cancel()
        await asyncio.gather(*self.running.values(), return_exceptions=True)
        self.running.clear()
        for held in list(self.sessions.values()):
            await self.retire(held.actor)
