"""Shared process doubles for the tests that drive a native boundary.

Every resolver test that needed a launcher wrote the same class: dispatch on
the two words a git invocation starts with, answer that probe, remember the
call. What differed between them was only *which* probes they answered, so
each copy restated the dispatch to vary the answers — and a change to the
orchestrator's probes had to be found in nine places.

:class:`ScriptedLauncher` takes the answers as data and keeps the dispatch
once. A test declares the probes it cares about and inherits recording; a
probe it does not declare succeeds silently, which is what the hand-rolled
doubles all did with their trailing `return ExitStatus(code=0)`.

A launcher answers probes; the Codex app-server is a conversation, so its
double has to be an actual child process. This module is that child: run as a
script it speaks the app-server's newline-framed JSON-RPC from an
:class:`AppServerTranscript`, and imported it hands a test
:class:`FakeAppServer` to script one with. See :func:`converse`.
"""

import os
import signal
import sys
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import timedelta
from pathlib import Path
from typing import Self, overload

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings

from lup.providers.codex.app_server import (
    CodexAppServer,
    OutgoingRpcMessage,
    RpcError,
    RpcFailure,
    RpcMessage,
    RpcNotification,
    RpcSuccess,
)
from lup.execution.process import ExitStatus, LaunchRequest, ProcessLauncher
from lup.providers.claude import ClaudeSession
from lup.sessions.capabilities import (
    ConversationRecord,
    EventStream,
    ForkSession,
    Interrupt,
    SessionEngine,
    TurnEngine,
)
from lup.sessions.events import (
    LiveTurnEvent,
    SessionId,
    SessionSummary,
    TurnEvent,
    TurnId,
    TurnIdentifiers,
    TurnInput,
    TurnMessage,
    TurnRequest,
    TurnResult,
)
from lup.sessions.layers import SessionLayers
from lup.types import EnvVars, JsonValue, Usage

PROBE_WORDS = 2
"""How many words after the executable name identify a git probe."""


def probe_of(request: LaunchRequest) -> str:
    """The subcommand words a launch request is answering, as a script key."""
    return " ".join(request.arguments[1 : 1 + PROBE_WORDS])


def out(stdout: str = "", code: int = 0, stderr: str = "") -> ExitStatus:
    """One scripted answer, spelled the way a test reads it."""
    return ExitStatus(code=code, stdout=stdout, stderr=stderr)


class ScriptedLauncher(ProcessLauncher):
    """Answer declared git probes in order, recording every launch.

    Keys are the two words following the executable — ``"rev-parse HEAD"``,
    ``"diff --name-only"``. A list of answers is consumed one per call and its
    last entry repeats, which is how a probe that must report differently
    before and after an operation is expressed without a counter.
    """

    def __init__(
        self,
        script: dict[str, ExitStatus | list[ExitStatus]] | None = None,
        default: ExitStatus | None = None,
    ) -> None:
        self.script = dict(script or {})
        self.default = default if default is not None else out()
        self.requests: list[LaunchRequest] = []
        self.answered: dict[str, int] = {}

    @property
    def arguments(self) -> list[list[str]]:
        """Every launch's argument list, in call order."""
        return [request.arguments for request in self.requests]

    def probes(self, probe: str) -> int:
        """How many times one declared probe has been asked."""
        return self.answered[probe] if probe in self.answered else 0

    def launch(self, request: LaunchRequest) -> ExitStatus:
        self.requests.append(request)
        probe = probe_of(request)
        if probe not in self.script:
            return self.default
        asked = self.probes(probe)
        self.answered[probe] = asked + 1
        answers = self.script[probe]
        if not isinstance(answers, list):
            return answers
        return answers[min(asked, len(answers) - 1)]


def identifiers(session: str = "session", turn: str = "turn") -> TurnIdentifiers:
    """The pair of ids a completed turn reports itself under."""
    return TurnIdentifiers(session=SessionId(value=session), turn=TurnId(value=turn))


def turn_result[T: BaseModel | None](
    output: T, marks: TurnIdentifiers | None = None
) -> TurnResult[T]:
    """A finished turn's result, with the fields no test inspects filled in.

    `TurnResult` carries six fields and a stub cares about one of them. Spelling
    the other five at each double put the transcript, usage, and duration in
    front of the output, which is the part under test.
    """
    return TurnResult[T].model_validate(
        {
            "output": output,
            "messages": [],
            "blocks": [],
            "usage": Usage(),
            "duration": timedelta(),
            "identifiers": marks if marks is not None else identifiers(),
        }
    )


class SilentStream(EventStream):
    """A turn's events for a double with none to report: the stream just ends.

    Every started turn carries a stream, so a double that has nothing to say
    still hands one back, and a reader of it sees a turn that was quiet.
    """

    async def ended(self) -> AsyncIterator[LiveTurnEvent]:
        for event in ():
            yield event

    async def ended_durable(self) -> AsyncIterator[TurnEvent]:
        for event in ():
            yield event

    def events(self) -> AsyncIterator[TurnEvent]:
        return self.ended_durable()

    def live(self) -> AsyncIterator[LiveTurnEvent]:
        return self.ended()


class IgnoredInterrupt(Interrupt):
    """An interrupt a double accepts and does nothing about, counting each ask."""

    def __init__(self) -> None:
        self.asked = 0

    async def interrupt(self) -> None:
        self.asked += 1


class StaticTurn[T: BaseModel | None](TurnEngine[T]):
    """A turn that has already finished, holding the result it will report."""

    def __init__(self, result: TurnResult[T]) -> None:
        self.value = result

    async def result(self) -> TurnResult[T]:
        return self.value


class HeldRecord(ConversationRecord):
    """A conversation's record that holds its identity and no messages."""

    def __init__(self, session: SessionId) -> None:
        self.session = session

    def identity(self) -> SessionId:
        return self.session

    async def messages(self) -> list[TurnMessage]:
        return []


class Unforked(ForkSession[ClaudeSession]):
    """A double that does not fork, saying so if a test asks it to."""

    def fork(
        self, at: TurnId | None = None
    ) -> AbstractAsyncContextManager[ClaudeSession]:
        raise RuntimeError(f"this double does not fork (asked at {at})")


class EngineAgent:
    """An agent whose every session runs its turns on an engine a test supplies.

    What it opens is a Claude session over that engine — the session class
    with a record and no forks — laid in the layers a real agent lays, so a
    test drives the engine it wrote through exactly the path a program takes.
    """

    def __init__(
        self,
        engine: Callable[[SessionId | None], SessionEngine],
        layers: SessionLayers = SessionLayers(),
    ) -> None:
        self.engine = engine
        self.layers = layers

    def open(
        self, resume: SessionId | None = None
    ) -> AbstractAsyncContextManager[ClaudeSession]:
        return self.opening(resume)

    @asynccontextmanager
    async def opening(self, resume: SessionId | None) -> AsyncGenerator[ClaudeSession]:
        @asynccontextmanager
        async def native() -> AsyncGenerator[SessionEngine]:
            yield self.engine(resume)

        async with self.layers.around(native(), resume) as engine:
            yield ClaudeSession(
                engine,
                HeldRecord(resume or SessionId(value="session")),
                Unforked(),
                deltas=True,
            )

    @overload
    async def ask(self, prompt: str | TurnInput) -> TurnResult[None]: ...

    @overload
    async def ask[T: BaseModel](
        self, prompt: str | TurnInput, output: type[T]
    ) -> TurnResult[T]: ...

    async def ask[T: BaseModel](
        self, prompt: str | TurnInput, output: type[T] | None = None
    ) -> TurnResult[T] | TurnResult[None]:
        async with self.open() as session:
            if output is None:
                return await session.ask(prompt)
            return await session.ask(prompt, output)

    async def sessions(self) -> list[SessionSummary]:
        return []

    def layered(self, layers: SessionLayers) -> Self:
        return type(self)(self.engine, layers.over(self.layers))


def agent_over(session: SessionEngine) -> EngineAgent:
    """An agent whose every opened session runs on the one engine given."""
    return EngineAgent(lambda _resume: session)


class FailingLauncher(ProcessLauncher):
    """Fail every launch, echoing the arguments that were refused."""

    def __init__(self, code: int = 1) -> None:
        self.code = code
        self.requests: list[LaunchRequest] = []

    def launch(self, request: LaunchRequest) -> ExitStatus:
        self.requests.append(request)
        return ExitStatus(code=self.code, stderr=f"failed: {request.arguments}")


class ChildEnvironment(BaseSettings, extra="ignore"):
    """The two halves of the environment, as only the child can report them.

    `CodexAppServer.start` overlays the caller's declared variables onto the
    ambient ones, and a child that sees both is the whole of that claim.
    `PATH` stands for the ambient half because nothing in the transport
    declares it.
    """

    lup_fake_declared: str = ""
    path: str = ""


class ChildExit(BaseModel, frozen=True):
    """How a scripted child ends its own process, instead of answering."""

    status: int = 1
    stderr: str = ""


class ScriptedReply(BaseModel, frozen=True):
    """What the app-server double does when one client method arrives.

    A reply that is neither silent nor an exit answers the request — with
    `error` when the transcript declares one, and with `result` otherwise.
    Notifications follow the answer, so a silent reply is still observable
    from the client side without one being owed.
    """

    result: JsonValue = None
    error: RpcError | None = None
    notifications: list[RpcNotification] = Field(default_factory=list)
    exits: ChildExit | None = None
    silent: bool = False


class AppServerTranscript(BaseModel, frozen=True):
    """One scripted app-server session, keyed by the method each reply answers.

    A child ordinarily ends when its stdin does, which is a clean exit and so
    never the signal exit that terminating a live child produces. `lingers`
    keeps it running past that, leaving `close` a child that is genuinely
    still there to terminate.
    """

    replies: dict[str, ScriptedReply] = Field(default_factory=dict)
    otherwise: ScriptedReply = ScriptedReply()
    lingers: bool = False


class AppServerRecord(BaseModel, frozen=True):
    """What one child saw: how it was launched, and what reached it."""

    pid: int
    arguments: list[str]
    environment: ChildEnvironment
    received: list[RpcMessage] = Field(default_factory=list)

    def report_to(self, record: Path) -> None:
        """Publish this record where the parent reads it, in one step.

        The parent reads while the child is still writing: the assertions
        about a terminated child ask what it saw immediately after killing it.
        Writing in place truncates first and fills after, so a read landing in
        that window gets an empty file and a JSON error naming this record —
        a fault that looks like it belongs to whatever the test was driving.
        Writing beside the target and renaming onto it leaves the reader
        either the previous record or this one, never half of either.
        """
        beside = record.with_name(f"{record.name}.{os.getpid()}.part")
        beside.write_text(self.model_dump_json(), encoding="utf-8")
        os.replace(beside, record)


def emit(message: OutgoingRpcMessage) -> None:
    """Write one JSON-RPC line, flushed so a pipe delivers it now."""
    sys.stdout.write(message.model_dump_json(by_alias=True, exclude_none=True) + "\n")
    sys.stdout.flush()


def answer(reply: ScriptedReply, message: RpcMessage) -> None:
    """Reply to one request, where the transcript grants it a reply at all."""
    if reply.silent or reply.exits is not None or message.id is None:
        return
    if reply.error is not None:
        emit(RpcFailure(id=message.id, error=reply.error))
        return
    emit(RpcSuccess(id=message.id, result=reply.result))


def converse(transcript: AppServerTranscript, record: Path) -> int:
    """Answer what arrives until stdin ends or the transcript says to exit.

    This is the child half of :class:`FakeAppServer`, reached by running this
    module as a script. Everything it saw goes to `record` as it arrives, so
    a child that is about to be terminated or to exit has already reported.
    """
    seen = AppServerRecord(
        pid=os.getpid(), arguments=sys.argv, environment=ChildEnvironment()
    )
    seen.report_to(record)
    for line in iter(sys.stdin.readline, ""):
        message = RpcMessage.model_validate_json(line)
        seen = seen.model_copy(update={"received": [*seen.received, message]})
        seen.report_to(record)
        reply = (
            transcript.replies[message.method]
            if message.method in transcript.replies
            else transcript.otherwise
        )
        answer(reply, message)
        for notification in reply.notifications:
            emit(notification)
        if reply.exits is not None:
            sys.stderr.write(reply.exits.stderr)
            sys.stderr.flush()
            return reply.exits.status
    if transcript.lingers:
        signal.pause()
    return 0


PROGRAM = Path(__file__).resolve()
"""This module's own path, handed to the interpreter as the child program."""


def alive(pid: int) -> bool:
    """Whether a recorded child is still a process, rather than a reaped id."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


class FakeAppServer(BaseModel, frozen=True):
    """A `CodexAppServer` bound to a scripted child instead of an installed CLI.

    The child is this interpreter running this module, so it needs no execute
    bit, no shebang, and no `codex` on PATH: the transport already takes an
    `executable` plus leading `arguments`, which is exactly enough to express
    it. Nothing here knows which of the transport's legs a test is after, so
    a suite that wants the turn lifecycle offline scripts `thread/start` and
    `turn/start` the same way this one scripts `initialize`.
    """

    root: Path

    @property
    def transcript_path(self) -> Path:
        """Where the child reads its script from."""
        return self.root / "transcript.json"

    @property
    def record_path(self) -> Path:
        """Where the child writes down how it was launched and what arrived."""
        return self.root / "record.json"

    def attach(
        self,
        transcript: AppServerTranscript,
        *,
        arguments: list[str] | None = None,
        environment: EnvVars | None = None,
    ) -> CodexAppServer:
        """A transport whose child is this transcript, run by this interpreter."""
        self.transcript_path.write_text(transcript.model_dump_json(), encoding="utf-8")
        return CodexAppServer(
            Path(sys.executable),
            arguments=[
                str(PROGRAM),
                str(self.transcript_path),
                str(self.record_path),
                *(arguments or []),
            ],
            environment=environment,
        )

    def observed(self) -> AppServerRecord:
        """What the child recorded, read back after the exchange."""
        return AppServerRecord.model_validate_json(
            self.record_path.read_text(encoding="utf-8")
        )


if __name__ == "__main__":
    sys.exit(
        converse(
            AppServerTranscript.model_validate_json(
                Path(sys.argv[1]).read_text(encoding="utf-8")
            ),
            Path(sys.argv[2]),
        )
    )


@overload
def request_for(prompt: str | TurnInput) -> TurnRequest[None]: ...


@overload
def request_for[T: BaseModel](
    prompt: str | TurnInput, output: type[T]
) -> TurnRequest[T]: ...


def request_for[T: BaseModel](
    prompt: str | TurnInput, output: type[T] | None = None
) -> TurnRequest[T] | TurnRequest[None]:
    """What a session asks its engine to start, for a test driving the engine.

    A program never builds one — it asks a session — but a test of the engine
    beneath the session starts turns on it directly.
    """
    words = TurnInput(text=prompt) if isinstance(prompt, str) else prompt
    if output is None:
        return TurnRequest[None](input=words)
    return TurnRequest[T](input=words, output_type=output)


def conversation_over(session: SessionEngine) -> ClaudeSession:
    """A conversation whose turns run on the one engine given."""
    return ClaudeSession(
        session, HeldRecord(SessionId(value="session")), Unforked(), deltas=True
    )
