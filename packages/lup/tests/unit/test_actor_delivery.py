"""Delivering what a door said to an actor, exactly once and never never.

The defect these are written against reported success and delivered
nothing: every reader of the message stream started at whatever its head
was when the reader was constructed, so a message posted while a turn was
in flight was already behind the window the next turn opened. It was not
delivered late. It was delivered to nobody, in any round, while the console
printed `redirected worker:research-corpus-retrieval#1`.
"""

from pathlib import Path

from lup.coordination.mail import ActorMail
from lup.coordination.mailbox import AnswerDoor
from lup.coordination.refs import ActorRef
from lup.coordination.sessions import ActorMailbox, create_mailbox_hooks
from lup.resolver.record import Journal

from lup.policy.hooks import LupHookInput


def worker(round_number: int = 1) -> ActorRef:
    return ActorRef(kind="worker", id="a-concern", round=round_number)


def mailbox_for(tmp_path: Path, actor: ActorRef) -> ActorMailbox:
    return ActorMailbox(ActorMail(tmp_path), Journal(tmp_path), actor)


def post(tmp_path: Path, to: ActorRef, text: str, redirect: bool = False) -> None:
    """One message into one member's mailbox, as a door that resolved an address does.

    A member rather than a spelling, because resolving what an operator typed
    is the roster's and happens before this: a message file sits in exactly
    one mailbox, so there is no token left for a reader to match against itself.
    """
    ActorMail(tmp_path).send(
        to, text, door=AnswerDoor.AGENT, sender="run-1", redirect=redirect
    )


def test_a_message_posted_before_a_reader_exists_is_still_delivered(
    tmp_path: Path,
) -> None:
    """The bug exactly: a reader built after the message must still see it."""
    post(tmp_path, worker(), "stop, that design was rejected")

    taken = mailbox_for(tmp_path, worker()).take()

    assert [message.text for message in taken] == ["stop, that design was rejected"]


def test_a_new_reader_resumes_where_the_last_one_was_delivered_to(
    tmp_path: Path,
) -> None:
    """A resumed run reattaches to the position, not to the stream head."""
    post(tmp_path, worker(), "first")
    mailbox_for(tmp_path, worker()).take()
    post(tmp_path, worker(), "second")

    resumed = mailbox_for(tmp_path, worker(2)).take()

    assert [message.text for message in resumed] == ["second"]


def test_the_reported_run_replayed_end_to_end(tmp_path: Path) -> None:
    """The whole sequence from the report, in the order it happened.

    A redirect issued mid-turn against a live worker, the run interrupted by
    a spend limit before that turn ended, a second redirect issued after the
    resume, and the worker taking its next turn. Both must reach it, and the
    console must have been able to see that neither had yet.
    """
    actor = worker()
    post(tmp_path, worker(), "superseded; stop", redirect=True)
    interrupted = mailbox_for(tmp_path, actor)
    interrupted.waiting()  # the turn that was killed before it started

    post(tmp_path, worker(), "still superseded", redirect=True)
    resumed = mailbox_for(tmp_path, worker(2))
    outstanding = resumed.waiting()
    taken = resumed.take()

    assert [message.text for message in outstanding.messages] == [
        "superseded; stop",
        "still superseded",
    ]
    assert [message.text for message in taken] == [
        "superseded; stop",
        "still superseded",
    ]
    assert all(message.redirect for message in taken)
    assert resumed.waiting().messages == []


def test_reading_what_is_waiting_does_not_consume_it(tmp_path: Path) -> None:
    """Asking whether anything was read cannot be what makes it disappear."""
    post(tmp_path, worker(), "everyone stop")
    mailbox = mailbox_for(tmp_path, worker())

    assert [message.text for message in mailbox.waiting().messages] == ["everyone stop"]
    assert [message.text for message in mailbox.take()] == ["everyone stop"]
    assert mailbox.waiting().messages == []


def test_the_label_the_console_prints_reaches_the_actor() -> None:
    """Every spelling a door may use, including the one `actors` prints."""
    actor = worker(2)

    assert "worker:a-concern#2" in actor.addresses()
    assert "worker:a-concern#1" in actor.addresses()
    assert "worker:a-concern" in actor.addresses()
    assert "a-concern" in actor.addresses()
    assert "" not in actor.addresses()


def test_a_label_from_an_earlier_round_still_reaches_the_conversation(
    tmp_path: Path,
) -> None:
    """An operator reading `actors` a round ago named this same session."""
    post(tmp_path, worker(), "the concern was superseded")

    taken = mailbox_for(tmp_path, worker(3)).take()

    assert [message.text for message in taken] == ["the concern was superseded"]


def test_a_sibling_actor_never_takes_this_actor_s_mail(tmp_path: Path) -> None:
    post(tmp_path, worker(), "for the worker")
    reviewer = ActorRef(kind="reviewer", id="a-concern")

    assert mailbox_for(tmp_path, reviewer).take() == []
    assert [message.text for message in mailbox_for(tmp_path, worker()).take()] == [
        "for the worker"
    ]


def test_one_message_reaches_one_actor_however_many_share_its_concern(
    tmp_path: Path,
) -> None:
    """A message is addressed and consumed, so there is exactly one recipient.

    A spelling two members answer to is resolved by the sender against the
    roster, which picks one of them — and a caller that meant both reaches for
    the verb that means both. A token every reader matched against itself is
    what forced a position per member, and what stopped a redirect meaning
    anything a stop can sensibly mean.
    """
    reviewer = ActorRef(kind="reviewer", id="a-concern")
    post(tmp_path, worker(), "the file moved")

    assert len(mailbox_for(tmp_path, worker()).take()) == 1
    assert mailbox_for(tmp_path, reviewer).take() == []


def test_a_delivery_is_recorded_against_the_actor_that_took_it(
    tmp_path: Path,
) -> None:
    """Non-delivery was established from the journal's silence; so must delivery."""
    post(tmp_path, worker(), "superseded by another concern", redirect=True)

    mailbox_for(tmp_path, worker(2)).take()

    recorded = Journal(tmp_path).read()
    posted = [
        event
        for entry in recorded
        for event in [entry.event]
        if event.type == "message_posted"
    ]
    assert [event.redirect for event in posted] == [True]
    assert [
        entry.actor.round for entry in recorded if entry.event.type == "message_posted"
    ] == [2]


def test_mail_still_queued_when_a_conversation_closes_is_recorded(
    tmp_path: Path,
) -> None:
    post(tmp_path, worker(), "stop", redirect=True)

    mailbox_for(tmp_path, worker()).record_outstanding()

    outstanding = [
        entry.event
        for entry in Journal(tmp_path).read()
        if entry.event.type == "message_outstanding"
    ]
    assert [event.text for event in outstanding] == ["stop"]


async def test_the_hook_and_the_next_turn_never_deliver_the_same_message(
    tmp_path: Path,
) -> None:
    """One position, so what interrupts a turn does not also head the next."""
    mailbox = mailbox_for(tmp_path, worker())
    hooks = create_mailbox_hooks(mailbox)
    post(tmp_path, worker(), "read this now")

    hook = hooks.pre_tool_use[0].hook
    mid_turn = await hook(LupHookInput(event="PreToolUse", tool_name="Read"))

    assert mid_turn.additional_context == "[message by agent] read this now"
    assert len(mailbox.waiting().messages) == 1
    mid_turn.delivered()
    assert mailbox.waiting().messages == []


async def test_a_redirect_refuses_the_call_it_interrupted(tmp_path: Path) -> None:
    mailbox = mailbox_for(tmp_path, worker())
    hooks = create_mailbox_hooks(mailbox)
    post(tmp_path, worker(), "that design was rejected", redirect=True)

    hook = hooks.pre_tool_use[0].hook
    mid_turn = await hook(LupHookInput(event="PreToolUse", tool_name="Write"))

    assert mid_turn.decision == "deny"
    assert "that design was rejected" in mid_turn.reason
