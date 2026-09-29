"""One stream of everything live on the dashboard, numbered so a tab that reconnects resumes.

The page subscribes once and is handed everything that moves — sessions,
their subagents and what each is doing, the messages between them, the
reviews waiting on the operator — as server-sent events. One producer serves
every tab: while any tab follows, it looks at what each source says now,
reading each only where it changed, and numbers every difference it finds.
A fresh tab is handed the whole state once and then each numbered change; a
tab that reconnects sends the last cursor it saw (`Last-Event-ID`) and is
handed exactly what came after it. A cursor this dashboard did not hand out —
one from before it restarted, or older than the changes it keeps — is
answered with the whole state again, so a tab can fall behind but never go
wrong.

The numbering follows :mod:`lup.devtools.supervisor.events`, which streams a
run's journal and replays by sequence; here the sequence is the dashboard's
own, over the differences it observed, since its sources are many files
rather than one journal.
"""

import asyncio
import logging
import time
from abc import ABC, abstractmethod
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Annotated, Literal
from uuid import uuid4

from pydantic import BaseModel, Field, ValidationError

from lup.devtools.dashboard.companion import KnownRepository
from lup.devtools.dashboard.live import (
    LiveMessage,
    LiveRepository,
    LiveSession,
    RepositoryWatch,
)
from lup.devtools.dashboard.reviews import ReviewError, ReviewSnapshot, ReviewStore
from lup.devtools.review.app import ReviewRoot, ReviewSummary

logger = logging.getLogger(__name__)

HEARTBEAT_SECONDS = 15.0
"""How long a quiet stream goes before it says it is still there."""

# lup: ignore[constant-declaration] — what the page is told to wait before reconnecting
RETRY_MILLISECONDS = 3000
"""How long a tab whose stream dropped waits before it reconnects."""

KEPT_FRAMES = 4096
"""How many numbered changes are kept for a tab that reconnects.

A tab further behind than this is handed the whole state again, which is
always correct and only costs one larger frame.
"""


class StreamEvent(BaseModel, ABC, frozen=True):
    """One thing the stream carries, answering for how it moves the state it describes.

    Asked rather than tested, so the state that every tab converges on is
    moved the same way on the server as a tab moves its own copy.
    """

    @abstractmethod
    def moves(self, state: "LiveState") -> None:
        """Move *state* by this difference."""


class SnapshotEvent(StreamEvent, frozen=True):
    """Everything the dashboard knows now, handed to a tab with nothing to resume from."""

    type: Literal["snapshot"] = "snapshot"
    repositories: list[LiveRepository]
    sessions: list[LiveSession]
    messages: list[LiveMessage]
    reviews: ReviewSnapshot

    def moves(self, state: "LiveState") -> None:
        """Nothing: a snapshot is read off the state, never applied to it."""
        del state


class RepositoryEvent(StreamEvent, frozen=True):
    """A repository the dashboard serves, first seen or changed."""

    type: Literal["repository"] = "repository"
    repository: LiveRepository

    def moves(self, state: "LiveState") -> None:
        state.repositories[self.repository.key] = self.repository


class RepositoryGoneEvent(StreamEvent, frozen=True):
    """A repository the dashboard no longer serves."""

    type: Literal["repository_gone"] = "repository_gone"
    key: str

    def moves(self, state: "LiveState") -> None:
        state.repositories.pop(self.key, None)


class SessionEvent(StreamEvent, frozen=True):
    """A session or subagent that arrived, or whose row or activity changed."""

    type: Literal["session"] = "session"
    session: LiveSession

    def moves(self, state: "LiveState") -> None:
        state.sessions[self.session.key] = self.session


class SessionGoneEvent(StreamEvent, frozen=True):
    """A session or subagent no roster lists any more."""

    type: Literal["session_gone"] = "session_gone"
    key: str

    def moves(self, state: "LiveState") -> None:
        state.sessions.pop(self.key, None)


class MessageEvent(StreamEvent, frozen=True):
    """A message posted, or one its recipient has now taken."""

    type: Literal["message"] = "message"
    message: LiveMessage

    def moves(self, state: "LiveState") -> None:
        state.messages[self.message.key] = self.message


class ReviewEvent(StreamEvent, frozen=True):
    """A review that was parked, or whose row changed."""

    type: Literal["review"] = "review"
    review: ReviewSummary

    def moves(self, state: "LiveState") -> None:
        state.reviews[self.review.key] = self.review


class ReviewGoneEvent(StreamEvent, frozen=True):
    """A review no queue holds any more."""

    type: Literal["review_gone"] = "review_gone"
    key: str

    def moves(self, state: "LiveState") -> None:
        state.reviews.pop(self.key, None)


class ReviewScopeEvent(StreamEvent, frozen=True):
    """Which checkouts' queues are read, and which could not be."""

    type: Literal["review_scope"] = "review_scope"
    roots: list[ReviewRoot]
    errors: list[ReviewError]

    def moves(self, state: "LiveState") -> None:
        state.roots, state.errors = self.roots, self.errors


type DashboardEvent = Annotated[
    SnapshotEvent
    | RepositoryEvent
    | RepositoryGoneEvent
    | SessionEvent
    | SessionGoneEvent
    | MessageEvent
    | ReviewEvent
    | ReviewGoneEvent
    | ReviewScopeEvent,
    Field(discriminator="type"),
]
"""Everything one frame of the stream can carry, told apart by its ``type``."""


class StreamFrame(BaseModel, frozen=True):
    """One frame: the event, and the cursor a tab resumes after it from."""

    cursor: str
    event: DashboardEvent


class Cursor(BaseModel, frozen=True):
    """Where a tab stands in one dashboard's numbering: which dashboard, and which change.

    Spelled as its own JSON document, which is what the page keeps and sends
    back without reading it.
    """

    epoch: str
    seq: int

    @classmethod
    def read(cls, spelled: str) -> "Cursor | None":
        """The cursor a tab sent back, or nothing where it is not one."""
        try:
            return cls.model_validate_json(spelled)
        except ValidationError:
            return None


class Sent(BaseModel, frozen=True):
    """One numbered frame as it was sent, kept for a tab that reconnects."""

    seq: int
    text: str


def sse(frame: StreamFrame) -> str:
    """One frame as a server-sent event, its cursor as the event id."""
    return f"id: {frame.cursor}\ndata: {frame.model_dump_json()}\n\n"


class Observation(BaseModel, frozen=True):
    """What every source says now; reviews only on the looks that read them."""

    repositories: list[LiveRepository]
    sessions: list[LiveSession]
    messages: list[LiveMessage]
    reviews: ReviewSnapshot | None = None


class LiveState:
    """The state every tab is converging on, and the differences that move it.

    Mutable, and moved only by the producer on the event loop's thread.
    """

    def __init__(self) -> None:
        self.repositories: dict[str, LiveRepository] = {}
        self.sessions: dict[str, LiveSession] = {}
        self.messages: dict[str, LiveMessage] = {}
        self.reviews: dict[str, ReviewSummary] = {}
        self.roots: list[ReviewRoot] = []
        self.errors: list[ReviewError] = []

    def observed(self, seen: Observation) -> list[DashboardEvent]:
        """Every difference between what the sources say and this state, applied to it."""
        repositories = {each.key: each for each in seen.repositories}
        sessions = {each.key: each for each in seen.sessions}
        events: list[DashboardEvent] = [
            *[
                RepositoryEvent(repository=each)
                for key, each in repositories.items()
                if self.repositories.get(key) != each
            ],
            *[
                RepositoryGoneEvent(key=key)
                for key in self.repositories
                if key not in repositories
            ],
            *[
                SessionEvent(session=each)
                for key, each in sessions.items()
                if self.sessions.get(key) != each
            ],
            *[
                SessionGoneEvent(key=key)
                for key in self.sessions
                if key not in sessions
            ],
            *[MessageEvent(message=each) for each in seen.messages],
            *(self.reviewed(seen.reviews) if seen.reviews is not None else []),
        ]
        for event in events:
            event.moves(self)
        return events

    def reviewed(self, snapshot: ReviewSnapshot) -> list[DashboardEvent]:
        """Every difference between the queues as read now and this state."""
        rows = {row.key: row for row in snapshot.reviews}
        scope: list[DashboardEvent] = (
            []
            if (snapshot.roots, snapshot.errors) == (self.roots, self.errors)
            else [ReviewScopeEvent(roots=snapshot.roots, errors=snapshot.errors)]
        )
        return [
            *scope,
            *[
                ReviewEvent(review=row)
                for key, row in rows.items()
                if self.reviews.get(key) != row
            ],
            *[ReviewGoneEvent(key=key) for key in self.reviews if key not in rows],
        ]

    def snapshot(self) -> SnapshotEvent:
        """The whole of this state, as one event."""
        return SnapshotEvent(
            repositories=list(self.repositories.values()),
            sessions=list(self.sessions.values()),
            messages=sorted(self.messages.values(), key=lambda each: each.sent_at),
            reviews=ReviewSnapshot(
                roots=self.roots,
                reviews=sorted(
                    self.reviews.values(), key=lambda row: row.created, reverse=True
                ),
                errors=self.errors,
            ),
        )


class LiveFeed:
    """One producer per dashboard, however many tabs follow it.

    It runs while any tab follows: every ``interval`` it looks at every
    repository — the sessions each roster lists and what each is doing, the
    mail record — and every ``review_every`` looks at the review queues,
    expiring those no session waits on every ``sweep_every``. What differs is
    numbered and kept for replay; the last ``kept`` of them are replayable.
    """

    def __init__(
        self,
        repositories: Callable[[], list[KnownRepository]],
        reviews: ReviewStore,
        interval: float = 0.5,
        review_every: int = 2,
        sweep_every: int = 20,
        kept: int = KEPT_FRAMES,
        heartbeat: float = HEARTBEAT_SECONDS,
    ) -> None:
        self.repositories = repositories
        self.reviews = reviews
        self.interval = interval
        self.review_every = review_every
        self.sweep_every = sweep_every
        self.heartbeat = heartbeat
        self.epoch = uuid4().hex[:12]
        self.seq = 0
        self.sent: deque[Sent] = deque(maxlen=kept)
        self.state = LiveState()
        self.watches: dict[str, RepositoryWatch] = {}
        self.looks = 0
        self.followers = 0
        self.primed = asyncio.Event()
        self.published = asyncio.Event()
        self.producer: asyncio.Task[None] | None = None

    def served(self) -> list[KnownRepository]:
        """Every repository this dashboard serves now, once each."""
        return list({each.key(): each for each in self.repositories()}.values())

    def cursor(self) -> str:
        """Where this dashboard's numbering stands now, spelled for a tab to send back."""
        return Cursor(epoch=self.epoch, seq=self.seq).model_dump_json()

    def observe(self) -> Observation:
        """Look at every source once; run off the event loop, by the producer alone."""
        self.looks += 1
        now = time.monotonic()
        known = {each.key(): each for each in self.served()}
        self.watches = {
            key: self.watches[key] if key in self.watches else RepositoryWatch(each)
            for key, each in known.items()
        }
        reviews = None
        if (self.looks - 1) % self.review_every == 0:
            if (self.looks - 1) % self.sweep_every == 0:
                self.reviews.sweep()
            reviews = self.reviews.snapshot()
        return Observation(
            repositories=[LiveRepository.of(each) for each in known.values()],
            sessions=[
                row for watch in self.watches.values() for row in watch.sessions(now)
            ],
            messages=[
                message
                for watch in self.watches.values()
                for message in watch.fresh_messages()
            ],
            reviews=reviews,
        )

    def publish(self, observation: Observation) -> None:
        """Number every difference the look found, and wake every tab following.

        The first look is the baseline and numbers nothing: no tab can hold a
        cursor into this dashboard before it, and a tab arriving now is handed
        the state it produced whole.
        """
        events = self.state.observed(observation)
        if not self.primed.is_set():
            self.primed.set()
            return
        for event in events:
            self.seq += 1
            frame = StreamFrame(cursor=self.cursor(), event=event)
            self.sent.append(Sent(seq=self.seq, text=sse(frame)))
        if events:
            published, self.published = self.published, asyncio.Event()
            published.set()

    async def produce(self) -> None:
        """Look and publish while anybody follows, then stop."""
        while self.followers:
            try:
                observation = await asyncio.to_thread(self.observe)
            except Exception:
                logger.exception("the dashboard could not read its sources this time")
                self.primed.set()
            else:
                self.publish(observation)
            await asyncio.sleep(self.interval)
        self.producer = None

    async def published_within(self, seconds: float) -> None:
        """Wait for the next change published, or for ``seconds``, whichever comes first."""
        waiter = asyncio.ensure_future(self.published.wait())
        finished, _ = await asyncio.wait({waiter}, timeout=seconds)
        if not finished:
            waiter.cancel()

    def resumed(self, spelled: str) -> int | None:
        """Where a tab's cursor resumes in this dashboard's numbering, if it does."""
        cursor = Cursor.read(spelled)
        if cursor is None or cursor.epoch != self.epoch or cursor.seq > self.seq:
            return None
        return cursor.seq

    def behind(self, position: int) -> bool:
        """Whether a tab at *position* missed changes this dashboard no longer keeps."""
        return bool(self.sent) and position < self.sent[0].seq - 1

    def whole(self) -> str:
        """The whole state as one frame, at the cursor it stands at."""
        return sse(StreamFrame(cursor=self.cursor(), event=self.state.snapshot()))

    async def follow(
        self, resume: str, disconnected: Callable[[], Awaitable[bool]]
    ) -> AsyncIterator[str]:
        """One tab's stream: what it missed since *resume*, or the whole state, then each change."""
        self.followers += 1
        if self.producer is None:
            self.producer = asyncio.create_task(self.produce())
        try:
            yield f"retry: {RETRY_MILLISECONDS}\n\n"
            await self.primed.wait()
            position = self.resumed(resume)
            quiet = time.monotonic()
            while not await disconnected():
                if position is None or self.behind(position):
                    yield self.whole()
                    position = self.seq
                    quiet = time.monotonic()
                fresh = [each for each in self.sent if each.seq > position]
                for each in fresh:
                    yield each.text
                if fresh:
                    position = fresh[-1].seq
                    quiet = time.monotonic()
                if time.monotonic() - quiet >= self.heartbeat:
                    yield ": keep-alive\n\n"
                    quiet = time.monotonic()
                await self.published_within(self.interval)
        finally:
            self.followers -= 1
