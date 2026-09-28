"""One durable session per actor, opened once and kept while the run moves.

A caller reaching for an agent's one-shot ``ask`` opens a session, takes one
turn and closes it. Nine separate symptoms sit downstream of that one
fact in the resolver: a park discards the whole turn, a reviewer re-reads its
concern cold each round, a merger never sees the parent it joined last, and
the same question is answered four times because each turn re-derives it
under an id no recorded answer matches.

An actor here is addressed rather than constructed per turn. It holds its
session across every turn it takes, drains what it does into a journal as it
happens, and is reattached after a park from its persisted identity. The
multi-turn shape is not unusual — :class:`lup.orchestration.background.BackgroundAgent`
already holds one session open across many turns — the one-shot convenience
is simply the easier reach.

The one-shot ``ask`` stays in the library. It is the legitimate one-shot
convenience and ``examples/one_shot.py`` uses it; what an actor buys over it
is being reachable while it works.
"""

import asyncio
import hashlib
import json
import logging
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack
from typing import Protocol

from pydantic import BaseModel, TypeAdapter

from lup.coordination.mail import (
    ActorDelivery,
    ActorMail,
    ActorMessage,
    MailEvent,
    MessageOutstandingEvent,
    MessagePostedEvent,
    StandingNotice,
)
from lup.coordination.refs import ActorRef
from lup.policy.hooks import (
    LupHookInput,
    LupHookMatcher,
    LupHookOutput,
    LupHooksConfig,
)
from lup.observability.journal import JournalRecord
from lup.sessions.errors import ProviderTurnError
from lup.sessions.events import SessionId, TurnEvent, TurnResult
from lup.sessions.surface import Agent, Conversation, Turn

logger = logging.getLogger(__name__)


type ActorEvent = TurnEvent | MailEvent
"""What this layer puts in a journal: what a turn did, and what mail did to it."""


class ActorJournal(Protocol):
    """Whatever records an actor's events, in the consumer's own vocabulary.

    A protocol rather than a class, because a consumer's journal admits more
    than this layer ever writes — the resolver's also carries phases, joins
    and verifications — and what an actor needs is only the narrow verb.
    Structural, so nothing has to be registered to satisfy it.

    The record comes back rather than nothing because the consumer's journal
    returns its own entry and a protocol promising ``None`` would refuse it.
    Nothing here reads the value; naming the shared base is what lets any
    journal's own entry type satisfy this.
    """

    def append(self, actor: ActorRef, event: ActorEvent) -> JournalRecord[ActorRef]:
        """Record one event against the actor that produced it."""
        ...


class ActorSchemaChangedError(RuntimeError):
    """A resumed actor expects a different submission schema than it left with."""


class ActorRecord(BaseModel, frozen=True):
    """What one actor needs to be reattached after a park.

    The digest is recorded here rather than pushed into the runtime because
    only this side knows both halves of the comparison. A provider that
    restores a resumed thread's tools from its own metadata never says what
    it restored, so the answerable question is whether *we* expect the same
    schema now that we expected before the park.
    """

    actor: ActorRef
    session: SessionId | None = None
    schema_digests: dict[str, str] = {}  # lup: ignore[dict-str-payload]
    """The digest this actor last used for each submission type it was asked for.

    Keyed by type rather than one per actor, because one actor is legitimately
    asked for more than one: a merger drives a whole join and reports a
    `JoinReport`, then adjudicates the finished tree and reports a
    `MergeReport`. A single digest read that second ask as the first schema
    having changed, and refused a conversation whose history is exactly what
    the second ask needs.
    """


RECORD_ADAPTER: TypeAdapter[ActorRecord] = TypeAdapter(ActorRecord)


def schema_digest(output_type: type[BaseModel] | type[None] | None) -> str | None:
    """Digest the submission schema an actor's turns are bound to."""
    if output_type is None or not issubclass(output_type, BaseModel):
        return None
    schema = json.dumps(TypeAdapter(output_type).json_schema(), sort_keys=True)
    return hashlib.sha256(schema.encode("utf-8")).hexdigest()


class TurnSeen:
    """Whether a turn got as far as its first event, which is being accepted."""

    def __init__(self) -> None:
        self.accepted = False


async def record_turn(
    journal: ActorJournal,
    actor: ActorRef,
    events: AsyncIterator[TurnEvent],
    seen: TurnSeen,
) -> None:
    """Drain one turn's durable events into the journal as they arrive.

    Taking the durable view rather than the live one keeps the journal a
    record of what happened rather than of what was being typed. A turn that
    fails ends its events with the failure, which awaiting the turn raises to
    its caller; recording stops there rather than raising it twice.
    """
    try:
        async for event in events:
            seen.accepted = True
            journal.append(actor, event)
    except Exception:
        logger.debug("%s stopped recording a failed turn", actor.label(), exc_info=True)


class ActorMailbox:
    """One conversation's mail, delivered once by whichever path reaches it.

    Two paths put a message in front of an actor — the hook that interrupts
    a live turn, and the collection that heads the next one — and they each
    held their own in-memory position over the same stream. Two positions
    over one stream can only agree by luck: both started at whatever the
    head was when they were constructed, so a message posted while a turn
    was in flight was already behind both of them, and the run reported it
    sent.

    One mailbox per conversation, holding the round it is on, is what lets the
    hook record a delivery against the actor that actually received it while
    the position it commits is the one the next turn resumes from.
    """

    def __init__(self, mail: ActorMail, journal: ActorJournal, actor: ActorRef) -> None:
        self.mail = mail
        self.journal = journal
        self.actor = actor

    def waiting(self) -> ActorDelivery:
        """What this conversation has queued, without consuming any of it."""
        return self.mail.waiting(self.actor)

    def standing(self) -> list[StandingNotice]:
        """Every fact standing over this population, which nothing consumes.

        Read at the head of every turn rather than delivered once, because a
        notice is state: it is true for a member that arrived after it was
        posted, and it goes on being true for one that has already read it.
        Nothing is committed here, and nothing has to remember having read —
        which is what makes a resumed turn read exactly what a first one did.
        """
        return self.mail.standing()

    def commit(self, delivery: ActorDelivery) -> None:
        """Record one delivery as handed over, and resume after it next time.

        Journaled here rather than at either call site, so a mid-turn
        delivery and a between-turns one leave the same record. The record
        being written on only one path is what lets a redirect vanish twice
        over: nothing delivered, and nothing saying so.

        Separate from reading because the two are separated by however long
        it takes to open a session, and the run this exists for was
        interrupted by a spend limit. Committing on the read would have
        consumed a message the interrupted turn never carried.
        """
        for message in delivery.messages:
            self.journal.append(
                self.actor,
                MessagePostedEvent(
                    text=message.text,
                    door=message.door,
                    in_reply_to=message.in_reply_to,
                    redirect=message.redirect,
                ),
            )
        self.mail.delivered(self.actor, delivery)

    def take(self) -> list[ActorMessage]:
        """Take everything queued, for a caller delivering it here and now."""
        delivery = self.waiting()
        self.commit(delivery)
        return delivery.messages

    def record_outstanding(self) -> None:
        """Record whatever is still queued as this conversation closes."""
        for message in self.waiting().messages:
            self.journal.append(
                self.actor,
                MessageOutstandingEvent(
                    text=message.text, door=message.door, redirect=message.redirect
                ),
            )


def create_mailbox_hooks(mailbox: ActorMailbox) -> LupHooksConfig:
    """Put anything said to this actor in front of it, mid-turn.

    Non-cooperative by construction. The actor calls any tool at all and the
    message is in its context — it never chooses to check, so it cannot fail
    to. Waiting for the next turn would mean a directive sits unread for as
    long as the current one runs, which on a long turn is most of the run.

    Telling and stopping are different acts and get different verdicts. A
    message rides alongside the call and the actor keeps going. A redirect
    denies the call and hands back the text as the reason, so an actor going
    the wrong way cannot take one more step down it — which is the whole
    difference between being informed and being redirected. Nothing here
    spends an interrupt: a turn that ends mid-report is a turn whose typed
    submission never arrives, and the actor is answering a refused tool call
    either way.

    The mailbox is the actor's own rather than one opened here, so what this
    delivers the next turn does not deliver again, and what it delivers is
    recorded. Built from a target of its own, it matched the bare id while
    the console printed and accepted ``worker:some-concern#1`` — so a
    redirect sent to the address the console gave reached neither path.
    """

    async def deliver(_input: LupHookInput) -> LupHookOutput:
        delivery = mailbox.waiting()
        arrived = delivery.messages
        if not arrived:
            return LupHookOutput()

        def received() -> None:
            mailbox.commit(delivery)

        # Only the adapter can acknowledge that the context reached its
        # native transport. Until then the next turn must still see this mail.
        delivered = "\n".join(
            f"[{'redirected' if message.redirect else 'message'} by {message.door}] "
            f"{message.text}"
            for message in arrived
        )
        if any(message.redirect for message in arrived):
            return LupHookOutput(
                decision="deny",
                reason=delivered
                + "\n\nStop what this call was part of and act on the above.",
                delivery_receipt=received,
            )
        return LupHookOutput(additional_context=delivered, delivery_receipt=received)

    return LupHooksConfig(pre_tool_use=[LupHookMatcher(hook=deliver, tag="mailbox")])


class ActorSession:
    """One actor's conversation, held open across every turn it takes."""

    def __init__(
        self,
        actor: ActorRef,
        agent: Agent,
        journal: ActorJournal,
        record: ActorRecord | None = None,
        mailbox: ActorMailbox | None = None,
    ) -> None:
        self.actor = actor
        self.agent = agent
        self.journal = journal
        self.mailbox = mailbox
        self.record = record or ActorRecord(actor=actor)
        self.stack = AsyncExitStack()
        self.conversation: Conversation | None = None
        self.pending: list[str] = []
        self.collected: ActorDelivery | None = None

    async def opened(self) -> Conversation:
        """Open this actor's session once, resuming where one was persisted."""
        if self.conversation is None:
            self.conversation = await self.stack.enter_async_context(
                self.agent.open(resume=self.record.session)
            )
        return self.conversation

    async def taken[T: BaseModel](
        self, conversation: Conversation, prompt: str, output: type[T], seen: TurnSeen
    ) -> TurnResult[T]:
        """One turn on ``conversation``, its events drained as they happen.

        The events are drained concurrently with awaiting the result rather
        than afterwards: a watcher that only saw a turn once it finished would
        be a log rather than a trace. The drain is awaited before this
        returns or raises, so ``seen`` has settled by then.
        """
        turn: Turn[T] = conversation.ask(prompt, output)
        drain = asyncio.create_task(
            record_turn(self.journal, self.actor, turn.events(), seen)
        )
        try:
            return await turn
        finally:
            await drain

    async def turn[T: BaseModel](self, prompt: str, output: type[T]) -> TurnResult[T]:
        """Take one turn on this actor's session, recording it as it happens.

        A recorded session is a claim that the provider still holds that
        conversation, and neither runtime guarantees it: a transcript can be
        pruned, and a runtime that does not persist one never wrote it. Ending
        a run over lost context rather than over the work is the worse
        failure, so a turn the provider refused before accepting it — one
        that reached no event — is taken again on a fresh conversation, the
        loss recorded rather than passed off as continuity.
        """
        self.check_schema(output)
        self.collect_mailbox()
        delivered = self.with_pending(prompt)
        seen = TurnSeen()
        try:
            result = await self.taken(await self.opened(), delivered, output, seen)
        except ProviderTurnError as error:
            # A host fault is not lost context. Reopening would meet the same
            # dead credential, and the attempt costs the resume point: the
            # record is cleared before the retry, so a run interrupted here
            # would resume every actor on a fresh conversation having
            # forgotten the one it was holding. A turn that was accepted
            # failed on its own account, and is not retried here either.
            if (
                seen.accepted
                or self.record.session is None
                or error.failure.environmental
            ):
                raise
            logger.exception(
                "%s could not resume session %s; continuing on a fresh one",
                self.actor.label(),
                self.record.session.value,
            )
            self.record = self.record.model_copy(update={"session": None})
            await self.close()
            result = await self.taken(
                await self.opened(), delivered, output, TurnSeen()
            )
        self.record = self.record.model_copy(
            update={"session": result.identifiers.session}
        )
        return result

    def collect_mailbox(self) -> None:
        """Take anything a door said to this actor since its last turn.

        Between turns there is nothing to append to, so a message waits here
        and lands at the head of the next one. Mid-turn delivery is the
        hook's job instead: the actor calls any tool and the message is in
        its context, which is what makes it impossible to forget — the actor
        was never involved in receiving it.

        A redirect says so even here. Where a hook surface exists it refuses
        the call outright, and this path is what a runtime without one gets
        instead — later, and unable to stop anything, but an actor told it
        was redirected can still abandon what it was doing. Delivering it in
        the same words as an ordinary message hid the difference from the one
        party that needed it, while the journal recorded the distinction
        faithfully for everyone who did not.
        """
        if self.mailbox is None:
            return
        collected = self.mailbox.waiting()
        if not collected.messages:
            return
        self.collected = collected
        self.pending.extend(
            f"[redirected by {message.door}] {message.text}\n"
            "Stop what you were doing and act on this."
            if message.redirect
            else f"[{message.door}] {message.text}"
            for message in collected.messages
        )

    def standing_context(self) -> list[str]:
        """What is true for this whole population, restated at every turn head.

        Ahead of the mail and ahead of the prompt, because it is the frame the
        other two are read inside: a redirect that revises an instruction has
        to be read after the standing facts it is revising against, and the
        instruction after both.

        Restated rather than delivered once. A notice is state, so a turn that
        did not restate it would be a turn working from a fact it had been
        told to forget — and the alternative, remembering which member has
        read which notice, is per-member bookkeeping that a replay, a resume
        or a second reader each get wrong differently.
        """
        if self.mailbox is None:
            return []
        found = self.mailbox.standing()
        if not found:
            return []
        lines = "\n".join(
            f"- {notice.text}" + (f" ({notice.by})" if notice.by else "")
            for notice in found
        )
        return [f"Standing for everyone working here:\n{lines}"]

    def with_pending(self, prompt: str) -> str:
        """Put whatever was volunteered between turns at the head of this one.

        Ahead of the prompt rather than after it, because a message that
        retargets an actor has to be read before the instruction it revises.

        This is where the mail counts as delivered, because this is where it
        joins a turn. Anything that ends the run between collecting it and
        here leaves the position untouched, so the message heads the next
        turn instead of being consumed by one that never happened.
        """
        standing = self.standing_context()
        if not self.pending and not standing:
            return prompt
        delivered = "\n\n".join([*standing, *self.pending, prompt])
        self.pending.clear()
        if self.mailbox is not None and self.collected is not None:
            self.mailbox.commit(self.collected)
            self.collected = None
        return delivered

    def check_schema(self, output: type[BaseModel]) -> None:
        """Refuse a resumed actor whose submission schema no longer matches.

        Per submission type, because being asked for a second one is ordinary
        rather than suspect: the same merger reports a join and then, once the
        tree is whole, adjudicates it. What is worth refusing is a type this
        actor has answered before whose shape has since moved, which is the
        park-across-a-code-change this guard was built for.
        """
        digest = schema_digest(output)
        if digest is None:
            return
        named = output.__name__
        seen = self.record.schema_digests
        if named in seen and seen[named] != digest:
            raise ActorSchemaChangedError(
                f"{self.actor.label()} resumed expecting a different {named} "
                "than the one it was bound to. Stop the owning run and explicitly "
                "retire this actor's persisted conversation before rebinding. "
                "For a resolver run, use `uv run lup-devtools resolve rebind-actor "
                f"'{self.actor.label()}' --run-id <run-id> --reason '<schema change>'`; "
                "this loses conversation memory but preserves run checkpoints and answers."
            )
        self.record = self.record.model_copy(
            update={"schema_digests": {**seen, named: digest}}
        )

    async def close(self) -> None:
        await self.stack.aclose()
        self.conversation = None
