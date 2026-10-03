"""What an actor does when the provider has lost its conversation."""

from datetime import timedelta
from pathlib import Path

from pydantic import BaseModel

from lup.coordination.mail import ActorMail
from lup.coordination.mailbox import AnswerDoor
from lup.coordination.refs import ActorRef
from lup.coordination.sessions import ActorMailbox, ActorRecord, ActorSession
from lup.resolver.record import Journal
from lup.sessions.capabilities import SessionEngine
from lup.sessions.errors import ProviderTurnError, TurnFailure
from lup.sessions.events import (
    SessionId,
    StartedTurn,
    TurnRequest,
    TurnResult,
)
from lup.types import Usage

from tests.unit.doubles import (
    IgnoredInterrupt,
    SilentStream,
    StaticTurn,
    identifiers,
    EngineAgent,
    agent_over,
)

FRESH = "fresh-session"


class Delivered(BaseModel):
    """What these turns submit: nothing but that they finished."""


def submitted[T: BaseModel | None](request: TurnRequest[T]) -> BaseModel | None:
    """The output a finished turn reports: the requested model, or nothing."""
    return request.output_type() if request.output_type is not None else None


class ResumeRefusingSession(SessionEngine):
    """Refuse every turn opened against a resumed conversation."""

    def __init__(self, resumed: bool) -> None:
        self.resumed = resumed

    async def start[T: BaseModel | None](
        self, request: TurnRequest[T]
    ) -> StartedTurn[T]:
        if self.resumed:
            raise ProviderTurnError(
                TurnFailure(message="No conversation found with session ID")
            )
        result = TurnResult[T].model_validate(
            {
                "output": submitted(request),
                "messages": [],
                "blocks": [],
                "usage": Usage(),
                "duration": timedelta(),
                "identifiers": identifiers(session=FRESH),
            }
        )
        return StartedTurn[T](
            turn=StaticTurn(result),
            events=SilentStream(),
            interrupt=IgnoredInterrupt(),
        )


def refusing_factory() -> tuple[EngineAgent, list[SessionId | None]]:
    """An agent that refuses a resume, recording what each open asked for."""
    opened: list[SessionId | None] = []

    def engine(resume: SessionId | None) -> ResumeRefusingSession:
        opened.append(resume)
        return ResumeRefusingSession(resume is not None)

    return EngineAgent(engine), opened


def worker_session(
    tmp_path: Path, record: ActorRecord | None = None
) -> tuple[ActorSession, list[SessionId | None]]:
    """One worker actor over a factory that refuses whatever it resumes."""
    factory, opened = refusing_factory()
    actor = ActorRef(kind="worker", id="a-concern")
    return ActorSession(actor, factory, Journal(tmp_path), record), opened


class RecordingSession(SessionEngine):
    """Accept every turn, keeping the input each one was actually given."""

    def __init__(self) -> None:
        self.delivered: list[str] = []

    async def start[T: BaseModel | None](
        self, request: TurnRequest[T]
    ) -> StartedTurn[T]:
        self.delivered.append(request.input.text)
        result = TurnResult[T].model_validate(
            {
                "output": submitted(request),
                "messages": [],
                "blocks": [],
                "usage": Usage(),
                "duration": timedelta(),
                "identifiers": identifiers(),
            }
        )
        return StartedTurn[T](
            turn=StaticTurn(result),
            events=SilentStream(),
            interrupt=IgnoredInterrupt(),
        )


def mailed_session(
    tmp_path: Path,
) -> tuple[ActorSession, ActorMailbox, RecordingSession]:
    """One worker actor holding the mailbox its run would hand it."""
    recording = RecordingSession()
    actor = ActorRef(kind="worker", id="a-concern")
    journal = Journal(tmp_path)
    mailbox = ActorMailbox(ActorMail(tmp_path), journal, actor)
    session = ActorSession(actor, agent_over(recording), journal, None, mailbox)
    return session, mailbox, recording


def post(tmp_path: Path, text: str) -> None:
    ActorMail(tmp_path).send(
        ActorRef(kind="worker", id="a-concern"),
        text,
        door=AnswerDoor.AGENT,
        sender="run-1",
    )


async def test_mail_heads_the_next_turn_and_is_carried_once(tmp_path: Path) -> None:
    """What a door said between turns rides in front of the prompt, once."""
    session, mailbox, recording = mailed_session(tmp_path)
    post(tmp_path, "the sibling already renamed that")

    await session.turn("go", Delivered)
    await session.turn("go on", Delivered)

    assert recording.delivered == [
        "[agent] the sibling already renamed that\n\ngo",
        "go on",
    ]
    assert mailbox.waiting().messages == []


async def test_a_turn_that_never_happened_does_not_consume_the_message(
    tmp_path: Path,
) -> None:
    """A run can die of a spend limit between reading a message and its turn.

    Collecting is not delivering: the position moves when the message joins
    a turn, so an interrupt after the read leaves it queued for the turn
    that does happen rather than swallowing it on behalf of one that did
    not.
    """
    session, mailbox, _ = mailed_session(tmp_path)
    post(tmp_path, "stop, that design was rejected")

    session.collect_mailbox()

    assert [message.text for message in mailbox.waiting().messages] == [
        "stop, that design was rejected"
    ]


async def test_a_lost_conversation_continues_on_a_fresh_session(
    tmp_path: Path,
) -> None:
    """A resume the provider cannot honour costs the context, not the run."""
    actor = ActorRef(kind="worker", id="a-concern")
    session, opened = worker_session(
        tmp_path, ActorRecord(actor=actor, session=SessionId(value="gone"))
    )

    result = await session.turn("go", Delivered)

    assert opened == [SessionId(value="gone"), None]
    assert result.identifiers.session == SessionId(value=FRESH)
    assert session.record.session == SessionId(value=FRESH)


async def test_an_actor_with_nothing_to_forget_still_raises(tmp_path: Path) -> None:
    """The fallback answers a lost conversation, and hides no other failure."""
    session, opened = worker_session(tmp_path)
    session.record = session.record.model_copy(
        update={"session": SessionId(value="gone")}
    )
    await session.close()

    result = await session.turn("go", Delivered)

    assert opened == [SessionId(value="gone"), None]
    assert result.identifiers.session == SessionId(value=FRESH)
