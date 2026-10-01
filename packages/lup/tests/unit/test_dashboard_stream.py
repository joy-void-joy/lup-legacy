"""One stream carries everything live to the page, and a tab that reconnects misses nothing.

A fresh tab is handed the whole state once and then only what changes; a tab
that reconnects names the last frame it saw and is handed exactly what came
after it, or the whole state again where the dashboard that numbered that
frame is gone. Reviews, sessions and messages ride the one stream, and the
operator's reply goes out through the page's own capability.
"""

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from starlette.types import Message, Scope

from lup.channels.models import Door
from lup.coordination import watch as watching
from lup.coordination.bare import mail as bare_mail
from lup.coordination.bare.changes import changes
from lup.coordination.bare.store import MAIL_RECORD, session_actor
from lup.coordination.identity import mint_member_id
from lup.coordination.mail import MAIL_PAGE
from lup.coordination.peers import USER_ADDRESS
from lup.coordination.repository import RepositoryPeers
from lup.coordination.wake import WakePath, Woken
from lup.devtools.dashboard.companion import KnownRepository
from lup.devtools.dashboard.live import MessagePage, ReplyOutcome
from lup.devtools.dashboard.reviews import ReviewStore, dashboard_app
from lup.devtools.dashboard.stream import (
    FollowedFrom,
    LiveFeed,
    MessageEvent,
    Observation,
    ReviewEvent,
    SessionEvent,
    SnapshotEvent,
    StreamFrame,
    UserEvent,
)
from lup.devtools.review.app import relay
from lup.policy.operations import Operation
from lup.policy.relay import PersistentQuestion

BASE_URL = "http://127.0.0.1:8766"
TOKEN = "operator-capability"
AUTHORIZATION = {"Authorization": f"Bearer {TOKEN}"}
POSTING = {**AUTHORIZATION, "Origin": BASE_URL}


def parked(root: Path, question_id: str) -> PersistentQuestion:
    operation = Operation(
        id=f"operation-{question_id}",
        session="native-session",
        requester="asking-session",
        tool="Bash",
        payload={"command": "touch must-not-run"},
        cwd=root,
        worktree=root,
    )
    return relay(root).record(
        PersistentQuestion(
            id=question_id,
            operation=operation,
            fingerprint=operation.fingerprint(),
            reason="The operator reviews this command.",
            eligible=["operator"],
            resumption="native_retry",
        )
    )


def known(root: Path) -> KnownRepository:
    return KnownRepository(repository=root, checkout=root)


def session(root: Path, name: str) -> str:
    member = mint_member_id()
    RepositoryPeers(root).join(member, root / name, cli_name=name)
    return member


def feed(root: Path, kept: int = 4096) -> LiveFeed:
    return LiveFeed(
        lambda: [known(root)],
        ReviewStore(roots=(root,)),
        interval=0.02,
        kept=kept,
    )


def framed(chunk: str) -> StreamFrame | None:
    """The frame one server-sent event carries, or nothing for a comment or retry."""
    data = [
        line.removeprefix("data: ")
        for line in chunk.splitlines()
        if line.startswith("data: ")
    ]
    return StreamFrame.model_validate_json(data[0]) if data else None


async def frames(
    source: LiveFeed,
    resume: str,
    until: Callable[[list[StreamFrame]], bool],
    meanwhile: Callable[[list[StreamFrame]], None] = lambda _: None,
) -> list[StreamFrame]:
    """Follow the feed as a tab would, until *until* holds of what arrived."""
    received: list[StreamFrame] = []
    done = asyncio.Event()

    async def disconnected() -> bool:
        return done.is_set()

    async def follow() -> None:
        stream: AsyncIterator[str] = source.follow(resume, disconnected)
        async for chunk in stream:
            frame = framed(chunk)
            if frame is None:
                continue
            received.append(frame)
            meanwhile(received)
            if until(received):
                done.set()
                return

    await asyncio.wait_for(follow(), timeout=10)
    return received


async def test_a_fresh_tab_is_handed_the_whole_state_then_each_change(
    tmp_path: Path,
) -> None:
    lead = session(tmp_path, "lead")
    parked(tmp_path, "q-1")
    source = feed(tmp_path)

    def post_once(received: list[StreamFrame]) -> None:
        if len(received) == 1:
            RepositoryPeers(tmp_path).send(lead, "the base moved", sender="other")

    received = await frames(
        source,
        "",
        lambda received: any(
            isinstance(frame.event, MessageEvent) for frame in received
        ),
        post_once,
    )

    first = received[0].event
    assert isinstance(first, SnapshotEvent)
    assert [each.id for each in first.sessions] == [lead]
    assert [each.id for each in first.reviews.reviews] == ["q-1"]
    assert [each.name for each in first.repositories] == [known(tmp_path).name()]
    message = received[-1].event
    assert isinstance(message, MessageEvent)
    assert (message.message.text, message.message.sender, message.message.waiting) == (
        "the base moved",
        "other",
        True,
    )


async def test_a_tab_that_reconnects_is_handed_only_what_it_missed(
    tmp_path: Path,
) -> None:
    lead = session(tmp_path, "lead")
    source = feed(tmp_path)
    seen = await frames(source, "", lambda received: len(received) >= 1)
    RepositoryPeers(tmp_path).describe(lead, "reviewing the stream")
    parked(tmp_path, "q-2")

    resumed = await frames(
        source,
        seen[-1].cursor,
        lambda received: (
            any(isinstance(frame.event, ReviewEvent) for frame in received)
            and any(isinstance(frame.event, SessionEvent) for frame in received)
        ),
    )

    assert not any(isinstance(frame.event, SnapshotEvent) for frame in resumed)
    described = [
        frame.event for frame in resumed if isinstance(frame.event, SessionEvent)
    ]
    assert described[-1].session.doing == "reviewing the stream"
    reviews = [frame.event for frame in resumed if isinstance(frame.event, ReviewEvent)]
    assert [each.review.id for each in reviews] == ["q-2"]


@pytest.mark.parametrize(
    "resume", ['{"epoch": "another-dashboard", "seq": 3}', "garbage", "0"]
)
async def test_a_cursor_this_dashboard_never_numbered_gets_the_whole_state(
    tmp_path: Path, resume: str
) -> None:
    session(tmp_path, "lead")
    source = feed(tmp_path)

    received = await frames(source, resume, lambda received: len(received) >= 1)

    assert isinstance(received[0].event, SnapshotEvent)


async def test_a_tab_further_behind_than_the_feed_keeps_gets_the_whole_state(
    tmp_path: Path,
) -> None:
    lead = session(tmp_path, "lead")
    source = feed(tmp_path, kept=2)
    seen = await frames(source, "", lambda received: len(received) >= 1)
    peers = RepositoryPeers(tmp_path)
    for index in range(5):
        peers.send(lead, f"message {index}", sender="other")

    received = await frames(
        source, seen[-1].cursor, lambda received: len(received) >= 1
    )

    first = received[0].event
    assert isinstance(first, SnapshotEvent)
    assert len(first.messages) == 5


def long_record(root: Path, reader: str, count: int) -> None:
    """A mail record of *count* messages to *reader*, written whole rather than posted."""
    record = RepositoryPeers(root).root / MAIL_RECORD
    record.write_text(
        "".join(
            json.dumps(
                bare_mail.Posted(
                    recipient=session_actor(reader),
                    message=bare_mail.new_message(
                        sender="other", to=reader, body=f"message {index}", door="agent"
                    ),
                )
            )
            + "\n"
            for index in range(count)
        ),
        encoding="utf-8",
    )


async def test_a_fresh_tab_is_handed_one_page_of_a_long_mail_record(
    tmp_path: Path,
) -> None:
    """Ten thousand messages on the record, and the first frame carries the latest page."""
    lead = session(tmp_path, "lead")
    long_record(tmp_path, lead, 10_000)

    received = await frames(feed(tmp_path), "", lambda received: len(received) >= 1)

    snapshot = received[0].event
    assert isinstance(snapshot, SnapshotEvent)
    assert [each.text for each in snapshot.messages] == [
        f"message {index}" for index in range(10_000 - MAIL_PAGE, 10_000)
    ]
    [extent] = snapshot.extents
    assert extent.earlier == min(each.at for each in snapshot.messages) > 0

    async with client(tmp_path) as http:
        response = await http.get(
            f"/api/repositories/{known(tmp_path).key()}/messages",
            params={"before": extent.earlier},
            headers=AUTHORIZATION,
        )

    older = MessagePage.model_validate_json(response.content)
    assert [each.text for each in older.messages] == [
        f"message {index}"
        for index in range(10_000 - 2 * MAIL_PAGE, 10_000 - MAIL_PAGE)
    ]
    assert 0 < older.earlier < extent.earlier


async def test_the_stream_holds_each_repository_s_latest_page_as_mail_arrives(
    tmp_path: Path,
) -> None:
    lead = session(tmp_path, "lead")
    long_record(tmp_path, lead, MAIL_PAGE)
    source = feed(tmp_path)
    seen = await frames(source, "", lambda received: len(received) >= 1)
    peers = RepositoryPeers(tmp_path)
    for index in range(3):
        peers.send(lead, f"later {index}", sender="other")

    await frames(
        source,
        seen[-1].cursor,
        lambda received: (
            sum(isinstance(each.event, MessageEvent) for each in received) >= 3
        ),
    )

    snapshot = source.state.snapshot()
    texts = [each.text for each in snapshot.messages]
    assert len(texts) == MAIL_PAGE
    assert texts[-3:] == [f"later {index}" for index in range(3)]
    assert texts[0] == "message 3"
    assert snapshot.extents[0].earlier == snapshot.messages[0].at


def client(root: Path) -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=dashboard_app(BASE_URL, TOKEN, (root,))),
        base_url=BASE_URL,
    )


async def test_the_stream_is_served_as_events_behind_the_capability(
    tmp_path: Path,
) -> None:
    session(tmp_path, "lead")
    app = dashboard_app(BASE_URL, TOKEN, (tmp_path,))
    chunks: list[bytes] = []
    statuses: list[int] = []
    kinds: list[str] = []
    started = asyncio.Event()
    finished = asyncio.Event()
    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/api/stream",
        "raw_path": b"/api/stream",
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"host", b"127.0.0.1:8766"),
            (b"authorization", f"Bearer {TOKEN}".encode()),
        ],
        "client": ("127.0.0.1", 12345),
        "server": ("127.0.0.1", 8766),
    }

    async def receive() -> Message:
        if not started.is_set():
            started.set()
            return {"type": "http.request", "body": b"", "more_body": False}
        await finished.wait()
        return {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        match message["type"]:
            case "http.response.start":
                statuses.append(message["status"])
                kinds.extend(
                    value.decode()
                    for name, value in message["headers"]
                    if name == b"content-type"
                )
            case "http.response.body" if message.get("body"):
                chunks.append(message["body"])
                if any(framed(chunk.decode()) for chunk in chunks):
                    finished.set()

    await asyncio.wait_for(app(scope, receive, send), timeout=10)

    assert statuses == [200]
    assert kinds[0].startswith("text/event-stream")
    first = next(frame for chunk in chunks if (frame := framed(chunk.decode())))
    assert isinstance(first.event, SnapshotEvent)
    assert chunks[0].decode().startswith("retry: ")


async def test_the_stream_refuses_a_page_without_the_capability(tmp_path: Path) -> None:
    async with client(tmp_path) as http:
        response = await http.get("/api/stream", headers={"Authorization": "Bearer no"})

    assert response.status_code == 401


async def test_a_reply_goes_to_the_session_it_names_as_the_user(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lead = session(tmp_path, "lead")

    def nudged(
        path: WakePath,
        message: str,
        cwd: Path | None = None,
        *,
        queue_timeout_seconds: float = 20.0,
        priority: str = "next",
    ) -> Woken:
        del path, message, cwd, queue_timeout_seconds, priority
        return Woken(reached=False, reason="nothing is listening")

    monkeypatch.setattr(watching, "wake", nudged)
    repository = known(tmp_path).key()

    async with client(tmp_path) as http:
        response = await http.post(
            f"/api/repositories/{repository}/sessions/{lead}/messages",
            headers=POSTING,
            json={"text": "rebase onto staging"},
        )

    assert response.status_code == 200
    outcome = ReplyOutcome.model_validate(response.json())
    assert outcome.queued and not outcome.woken
    assert "nothing is listening" in outcome.detail
    waiting = RepositoryPeers(tmp_path).waiting(lead).messages
    assert [(m.sender, m.door, m.text) for m in waiting] == [
        ("user", Door.PAGE, "rebase onto staging")
    ]


@pytest.mark.parametrize(
    ("headers", "status"),
    [
        (AUTHORIZATION, 403),
        ({"Authorization": "Bearer no", "Origin": BASE_URL}, 401),
        ({**POSTING, "Origin": "http://evil.example"}, 403),
    ],
)
async def test_a_reply_needs_the_capability_and_the_pages_own_origin(
    tmp_path: Path, headers: dict[str, str], status: int
) -> None:
    lead = session(tmp_path, "lead")

    async with client(tmp_path) as http:
        response = await http.post(
            f"/api/repositories/{known(tmp_path).key()}/sessions/{lead}/messages",
            headers=headers,
            json={"text": "anything"},
        )

    assert response.status_code == status
    assert RepositoryPeers(tmp_path).waiting(lead).messages == []


async def test_a_reply_to_nobody_or_to_a_session_that_left_says_so(
    tmp_path: Path,
) -> None:
    lead = session(tmp_path, "lead")
    RepositoryPeers(tmp_path).leave(lead, "done")
    repository = known(tmp_path).key()

    async with client(tmp_path) as http:
        nobody = await http.post(
            f"/api/repositories/{repository}/sessions/nobody/messages",
            headers=POSTING,
            json={"text": "hello"},
        )
        left = await http.post(
            f"/api/repositories/{repository}/sessions/{lead}/messages",
            headers=POSTING,
            json={"text": "hello"},
        )
        elsewhere = await http.post(
            f"/api/repositories/unknown/sessions/{lead}/messages",
            headers=POSTING,
            json={"text": "hello"},
        )
        empty = await http.post(
            f"/api/repositories/{repository}/sessions/{lead}/messages",
            headers=POSTING,
            json={"text": ""},
        )

    assert (nobody.status_code, left.status_code, elsewhere.status_code) == (
        404,
        409,
        404,
    )
    assert "left" in left.json()["detail"]
    assert empty.status_code == 422


async def test_a_tab_is_told_once_it_has_caught_up(tmp_path: Path) -> None:
    """A resumed tab that missed nothing is handed no frame, and still learns it is current."""
    session(tmp_path, "lead")
    source = feed(tmp_path)
    seen = await frames(source, "", lambda received: len(received) >= 1)
    done = asyncio.Event()

    async def disconnected() -> bool:
        return done.is_set()

    async def chunks(resume: str) -> list[str]:
        received: list[str] = []
        async for chunk in source.follow(resume, disconnected):
            received.append(chunk)
            if chunk == ": live\n\n":
                return received
        return received

    fresh = await asyncio.wait_for(chunks(""), timeout=10)
    resumed = await asyncio.wait_for(chunks(seen[-1].cursor), timeout=10)
    done.set()

    assert [framed(chunk) is not None for chunk in fresh] == [False, True, False]
    assert resumed[0].startswith("retry: ")
    assert resumed[1:] == [": live\n\n"]


async def test_a_look_that_fails_is_logged_and_the_feed_keeps_producing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    session(tmp_path, "lead")
    source = feed(tmp_path)
    publish = source.publish
    failed: list[bool] = []

    def once_broken(observation: Observation) -> None:
        if not failed:
            failed.append(True)
            raise ValueError("a look went wrong")
        publish(observation)

    monkeypatch.setattr(source, "publish", once_broken)

    received = await frames(source, "", lambda received: len(received) >= 1)

    assert isinstance(received[0].event, SnapshotEvent)
    assert "could not read its sources" in caplog.text


async def test_a_producer_that_ended_is_let_go_and_the_next_tab_starts_another(
    tmp_path: Path,
) -> None:
    session(tmp_path, "lead")
    source = feed(tmp_path)

    async def broken() -> None:
        raise RuntimeError("the producer broke")

    source.producer = asyncio.create_task(broken())
    with pytest.raises(RuntimeError):
        await source.producer

    received = await frames(source, "", lambda received: len(received) >= 1)

    assert isinstance(received[0].event, SnapshotEvent)


async def test_the_whole_state_carries_the_person_s_row_and_what_is_served(
    tmp_path: Path,
) -> None:
    session(tmp_path, "lead")
    peers = RepositoryPeers(tmp_path)
    peers.describe(USER_ADDRESS, "watching the relay land")
    source = LiveFeed(
        lambda: [known(tmp_path)],
        ReviewStore(roots=(tmp_path,)),
        interval=0.02,
        served=("transcript",),
    )

    def described_again(received: list[StreamFrame]) -> None:
        if len(received) == 1:
            peers.describe(USER_ADDRESS, "landing it")

    received = await frames(
        source,
        "",
        lambda received: any(isinstance(frame.event, UserEvent) for frame in received),
        described_again,
    )

    first = received[0].event
    assert isinstance(first, SnapshotEvent)
    assert [row.description for row in first.users] == ["watching the relay land"]
    assert first.served == ["transcript"]
    changed = received[-1].event
    assert isinstance(changed, UserEvent)
    assert changed.user.description == "landing it"


def transcript_line(text: str) -> str:
    return json.dumps(
        {
            "type": "assistant",
            "uuid": text,
            "timestamp": "2026-09-29T10:00:00Z",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": text}],
            },
        }
    )


def test_a_followed_transcript_is_read_on_while_its_lease_holds(
    tmp_path: Path,
) -> None:
    lead = session(tmp_path, "lead")
    transcript = tmp_path / "home" / "lead.jsonl"
    transcript.parent.mkdir()
    transcript.write_text(transcript_line("already read") + "\n", encoding="utf-8")
    changes(RepositoryPeers(tmp_path).root, lead, tmp_path, str(transcript))
    source = feed(tmp_path)
    key = known(tmp_path).key()

    following = source.follow_transcripts(
        [
            FollowedFrom(repository=key, member=lead, after=transcript.stat().st_size),
            FollowedFrom(repository=key, member="nobody"),
        ],
        now=0.0,
    )
    with transcript.open("a", encoding="utf-8") as written:
        written.write(transcript_line("said since") + "\n")
    read = source.transcribed(1.0)
    lapsed = source.transcribed(1.0 + source.lease)

    assert following.sessions == [f"{key}/{lead}"]
    assert [refusal.session for refusal in following.refused] == [f"{key}/nobody"]
    [event] = read
    assert event.session == f"{key}/{lead}"
    assert [entry.text for entry in event.entries] == ["said since"]
    assert event.end == transcript.stat().st_size
    assert lapsed == []
    assert source.followed == {}
