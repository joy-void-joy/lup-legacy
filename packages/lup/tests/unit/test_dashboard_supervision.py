"""What the operator does to the agents the dashboard shows, and as a peer among them.

Each verb writes where every agent reads — a mailbox, a member's own file, a
standing notice — signed `user` through the page's door, and wakes whom it
wrote to the way a message always has. A verb that cannot be done to what it
names is refused with why: an interrupt nothing can deliver, a stop of a
process this dashboard cannot be sure of, a release of somebody else's hold.
"""

import os
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from lup.channels.models import Door
from lup.coordination import watch as watching
from lup.coordination.bare import store
from lup.coordination.bare.runtime import Runtime, runtime_of
from lup.coordination.identity import mint_member_id
from lup.coordination.mail import ActorMail, MailCursor
from lup.coordination.peers import USER_ADDRESS
from lup.coordination.repository import PeerDepartedError, RepositoryPeers
from lup.coordination.wake import WakePath, WakeRuntime, Woken
from lup.devtools.dashboard import supervision
from lup.devtools.dashboard.companion import KnownRepository
from lup.devtools.dashboard.reviews import ReviewStore, dashboard_app
from lup.devtools.dashboard.stream import LiveFeed
from lup.devtools.dashboard.supervision import (
    BARE_WAKE,
    SUPERVISED,
    MessageRequest,
    PostRequest,
    Refused,
    broadcast,
    claim,
    describe,
    notice,
    post,
    read_inbox,
    release,
    rename,
    reply,
    stop,
    wake_member,
    withdraw,
)

BASE_URL = "http://127.0.0.1:8766"
TOKEN = "operator-capability"
WRITING = {"Authorization": f"Bearer {TOKEN}", "Origin": BASE_URL}


def known(root: Path) -> KnownRepository:
    return KnownRepository(repository=root, checkout=root)


def session(
    peers: RepositoryPeers, root: Path, name: str, runtime: WakeRuntime = "claude"
) -> str:
    member = mint_member_id()
    peers.join(
        member,
        root / name,
        cli_name=name,
        wake=WakePath(runtime=runtime, handle=str(root / f"{name}.sock"))
        if runtime
        else WakePath(),
    )
    return member


class Wakes:
    """Every wake a verb makes, answered *reached* rather than written anywhere."""

    def __init__(self, reached: bool) -> None:
        self.reached = reached
        self.made: list[tuple[str, str]] = []

    def __call__(
        self,
        path: WakePath,
        message: str,
        cwd: Path | None = None,
        *,
        queue_timeout_seconds: float = 20.0,
        priority: str = "next",
    ) -> Woken:
        del path, cwd, queue_timeout_seconds
        self.made.append((priority, message))
        return Woken(
            reached=self.reached, reason="" if self.reached else "nobody listening"
        )


@pytest.fixture
def woken(monkeypatch: pytest.MonkeyPatch) -> Wakes:
    wakes = Wakes(reached=True)
    monkeypatch.setattr(watching, "wake", wakes)
    monkeypatch.setattr(supervision, "wake", wakes)
    return wakes


def test_a_message_reaches_a_session_as_the_user_and_wakes_it(
    tmp_path: Path, woken: Wakes
) -> None:
    peers = RepositoryPeers(tmp_path)
    lead = session(peers, tmp_path, "lead")

    outcome = reply(known(tmp_path), lead, MessageRequest(text="rebase onto staging"))

    [posted] = ActorMail(peers.root).posted(MailCursor()).messages
    assert (posted.message.sender, posted.message.door) == ("user", Door.PAGE)
    assert outcome.queued and outcome.woken and not outcome.interrupted
    assert (outcome.post, outcome.thread) == (posted.message.post, posted.message.post)
    assert outcome.session == f"{known(tmp_path).key()}/{lead}"
    assert [priority for priority, _ in woken.made] == ["next"]
    assert peers.waiting(lead).messages == []


def test_a_reply_goes_into_the_thread_of_the_post_it_answers(
    tmp_path: Path, woken: Wakes
) -> None:
    peers = RepositoryPeers(tmp_path)
    lead = session(peers, tmp_path, "lead")
    peers.send(USER_ADDRESS, "Is the relay done?", sender=lead)
    [asked] = peers.take(USER_ADDRESS).messages

    outcome = reply(
        known(tmp_path), lead, MessageRequest(text="Not yet.", in_reply_to=asked.post)
    )

    assert outcome.thread == asked.thread
    [answer] = [
        each.message
        for each in ActorMail(peers.root).posted(MailCursor()).messages
        if each.message.text == "Not yet."
    ]
    assert answer.in_reply_to == asked.post


def test_a_redirect_is_left_for_the_next_tool_call_it_refuses(
    tmp_path: Path, woken: Wakes
) -> None:
    peers = RepositoryPeers(tmp_path)
    lead = session(peers, tmp_path, "lead")

    outcome = reply(
        known(tmp_path), lead, MessageRequest(text="stop: wrong branch", redirect=True)
    )

    assert outcome.woken
    assert "refuses" in outcome.detail
    [left] = peers.waiting(lead).messages
    assert left.redirect and left.carried


def test_now_asks_a_claude_turn_to_stop_for_the_message(
    tmp_path: Path, woken: Wakes
) -> None:
    peers = RepositoryPeers(tmp_path)
    lead = session(peers, tmp_path, "lead")

    outcome = reply(
        known(tmp_path), lead, MessageRequest(text="stop and look", priority="now")
    )

    assert outcome.interrupted
    assert [priority for priority, _ in woken.made] == ["now"]
    assert "finishes first" in outcome.detail


def test_now_is_refused_where_nothing_can_interrupt_and_nothing_is_sent(
    tmp_path: Path, woken: Wakes
) -> None:
    peers = RepositoryPeers(tmp_path)
    codex = session(peers, tmp_path, "codex", runtime="codex")
    silent = session(peers, tmp_path, "silent", runtime="")

    with pytest.raises(Refused, match="turn/interrupt"):
        reply(known(tmp_path), codex, MessageRequest(text="stop", priority="now"))
    with pytest.raises(Refused, match="no Claude wake socket"):
        reply(known(tmp_path), silent, MessageRequest(text="stop", priority="now"))

    assert ActorMail(peers.root).posted(MailCursor()).messages == []
    assert woken.made == []


def test_now_to_a_subagent_queues_it_and_wakes_its_session_with_a_copy(
    tmp_path: Path, woken: Wakes
) -> None:
    peers = RepositoryPeers(tmp_path)
    lead = session(peers, tmp_path, "lead")
    store.joined_subagent(
        peers.root,
        lead,
        store.Caller(
            agent_id="a1", agent_type="Explore", cwd=str(tmp_path), name="scout"
        ),
    )
    child = store.subagent_id(lead, "a1")

    outcome = reply(
        known(tmp_path), child, MessageRequest(text="look at mail.py", priority="now")
    )

    assert [message.text for message in peers.waiting(child).messages] == [
        "look at mail.py"
    ]
    [(priority, carried)] = woken.made
    assert priority == "now"
    assert f"For your subagent scout ({child})" in carried
    assert outcome.interrupted and not outcome.woken
    assert "copy" in outcome.detail


def test_a_message_to_nobody_or_to_a_session_that_left_is_refused(
    tmp_path: Path,
) -> None:
    peers = RepositoryPeers(tmp_path)
    lead = session(peers, tmp_path, "lead")
    peers.leave(lead, "done")

    with pytest.raises(LookupError):
        reply(known(tmp_path), "nobody-here", MessageRequest(text="hello"))
    with pytest.raises(PeerDepartedError):
        reply(known(tmp_path), lead, MessageRequest(text="hello"))


def test_a_bare_wake_carries_what_waits_or_says_nothing_does(
    tmp_path: Path, woken: Wakes
) -> None:
    peers = RepositoryPeers(tmp_path)
    lead = session(peers, tmp_path, "lead")

    idle = wake_member(known(tmp_path), lead)
    peers.send(lead, "the base moved", sender="other")
    carried = wake_member(known(tmp_path), lead)

    assert idle.woken and not idle.queued
    assert woken.made[0] == ("next", BARE_WAKE)
    assert carried.woken and "the base moved" in woken.made[1][1]
    assert peers.waiting(lead).messages == []


def test_a_subagent_has_no_wake_of_its_own(tmp_path: Path) -> None:
    peers = RepositoryPeers(tmp_path)
    lead = session(peers, tmp_path, "lead")
    store.joined_subagent(
        peers.root,
        lead,
        store.Caller(
            agent_id="a1", agent_type="Explore", cwd=str(tmp_path), name="scout"
        ),
    )

    with pytest.raises(Refused, match="no wake of its own"):
        wake_member(known(tmp_path), store.subagent_id(lead, "a1"))


def test_a_rename_is_refused_where_a_live_session_has_the_name(tmp_path: Path) -> None:
    peers = RepositoryPeers(tmp_path)
    lead = session(peers, tmp_path, "lead")
    session(peers, tmp_path, "other")

    renamed = rename(known(tmp_path), lead, "relay-lead")

    assert renamed.name == "relay-lead" and peers.called(lead) == "relay-lead"
    with pytest.raises(Refused, match="other"):
        rename(known(tmp_path), lead, "other")


def test_a_stop_signals_only_the_process_its_row_recorded_here(tmp_path: Path) -> None:
    peers = RepositoryPeers(tmp_path)
    lead = session(peers, tmp_path, "lead")
    store.adopt(peers.root, store.session_actor(lead), runtime_of(os.getpid()))
    contained = session(peers, tmp_path, "contained")
    store.adopt(
        peers.root,
        store.session_actor(contained),
        Runtime(pid=7, started="1234", scope="a-container-of-its-own"),
    )
    signalled: list[tuple[int, int]] = []

    stopped = stop(
        known(tmp_path), lead, kill=lambda pid, sent: signalled.append((pid, sent))
    )

    assert stopped.pid == os.getpid()
    assert [pid for pid, _ in signalled] == [os.getpid()]
    with pytest.raises(Refused, match="pid namespace"):
        stop(
            known(tmp_path),
            contained,
            kill=lambda pid, sent: signalled.append((pid, sent)),
        )
    assert len(signalled) == 1


def test_a_broadcast_is_one_post_to_every_working_member(
    tmp_path: Path, woken: Wakes
) -> None:
    peers = RepositoryPeers(tmp_path)
    lead = session(peers, tmp_path, "lead")
    other = session(peers, tmp_path, "other")
    gone = session(peers, tmp_path, "gone")
    peers.leave(gone, "done")

    said = broadcast(known(tmp_path), "freeze merges for ten minutes")

    assert {outcome.session for outcome in said.outcomes} == {
        f"{known(tmp_path).key()}/{lead}",
        f"{known(tmp_path).key()}/{other}",
    }
    posted = ActorMail(peers.root).posted(MailCursor()).messages
    assert {each.message.post for each in posted} == {said.post}
    assert {each.recipient.id for each in posted} == {lead, other}


def test_a_notice_stands_until_it_is_withdrawn(tmp_path: Path, woken: Wakes) -> None:
    peers = RepositoryPeers(tmp_path)
    session(peers, tmp_path, "lead")

    stated = notice(known(tmp_path), "dev is frozen")

    assert [(each.id, each.by) for each in peers.cohort.mail.standing()] == [
        (stated.id, "user")
    ]
    assert withdraw(known(tmp_path), stated.id).withdrawn
    assert peers.cohort.mail.standing() == []
    with pytest.raises(LookupError):
        withdraw(known(tmp_path), stated.id)


def test_the_person_s_description_is_said_in_every_repository_served(
    tmp_path: Path,
) -> None:
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()

    described = describe([known(first), known(second)], "landing the relay")

    assert described.repositories == [known(first).key(), known(second).key()]
    assert RepositoryPeers(first).person().doing == "landing the relay"
    assert RepositoryPeers(second).person().doing == "landing the relay"


def test_the_person_holds_and_gives_back_a_path_and_only_their_own(
    tmp_path: Path,
) -> None:
    peers = RepositoryPeers(tmp_path)
    lead = session(peers, tmp_path, "lead")
    mine, theirs = tmp_path / "mine", tmp_path / "theirs"
    mine.mkdir()
    theirs.mkdir()
    peers.lock(lead, theirs)

    held = claim(known(tmp_path), str(mine))

    assert held.holders == ["user"]
    assert [claim.path for claim in peers.holding(mine / "a.py")] == [str(mine)]
    with pytest.raises(Refused, match="held by lead"):
        release(known(tmp_path), str(theirs))
    assert release(known(tmp_path), str(mine)).holders == []
    with pytest.raises(Refused, match="does not exist"):
        claim(known(tmp_path), str(tmp_path / "nowhere"))
    with pytest.raises(Refused, match="not absolute"):
        claim(known(tmp_path), "relative/path")


def test_reading_the_inbox_takes_exactly_the_messages_named(tmp_path: Path) -> None:
    peers = RepositoryPeers(tmp_path)
    lead = session(peers, tmp_path, "lead")
    peers.send(USER_ADDRESS, "one", sender=lead)
    peers.send(USER_ADDRESS, "two", sender=lead)
    first, second = peers.waiting(USER_ADDRESS).messages

    read = read_inbox(known(tmp_path), [first.id, "not-a-message"])

    assert read.read == [first.id]
    assert [message.id for message in peers.waiting(USER_ADDRESS).messages] == [
        second.id
    ]


def test_a_post_reaches_everyone_in_the_discussion_each_woken(
    tmp_path: Path, woken: Wakes
) -> None:
    peers = RepositoryPeers(tmp_path)
    research = session(peers, tmp_path, "research")
    summary = session(peers, tmp_path, "summary")
    peers.send(summary, "Which sources are in?", sender=research)
    [asked] = peers.take(summary).messages

    posted = post(
        known(tmp_path), asked.thread, PostRequest(text="Cite the open ones.")
    )

    assert posted.thread == asked.thread
    assert {each.session for each in posted.deliveries} == {
        f"{known(tmp_path).key()}/{research}",
        f"{known(tmp_path).key()}/{summary}",
    }
    assert all(each.woken and each.post == posted.post for each in posted.deliveries)
    assert all(
        "discussion «Which sources are in?»" in carried for _, carried in woken.made
    )


def client(root: Path) -> AsyncClient:
    app = dashboard_app(BASE_URL, TOKEN, (root,))
    return AsyncClient(transport=ASGITransport(app=app), base_url=BASE_URL)


async def test_every_supervision_write_is_held_to_the_page_s_origin(
    tmp_path: Path,
) -> None:
    peers = RepositoryPeers(tmp_path)
    session(peers, tmp_path, "lead")
    key = known(tmp_path).key()
    held = tmp_path / "held"
    held.mkdir()

    async with client(tmp_path) as http:
        stranger = await http.post(
            f"/api/repositories/{key}/claims",
            headers={
                "Authorization": f"Bearer {TOKEN}",
                "Origin": "http://evil.example",
            },
            json={"path": str(held)},
        )
        unsigned = await http.request(
            "DELETE",
            f"/api/repositories/{key}/claims",
            headers={"Authorization": f"Bearer {TOKEN}"},
            json={"path": str(held)},
        )
        taken = await http.post(
            f"/api/repositories/{key}/claims", headers=WRITING, json={"path": str(held)}
        )
        given = await http.request(
            "DELETE",
            f"/api/repositories/{key}/claims",
            headers=WRITING,
            json={"path": str(held)},
        )

    assert (stranger.status_code, unsigned.status_code) == (403, 403)
    assert taken.status_code == 200 and taken.json()["holders"] == ["user"]
    assert given.status_code == 200


async def test_the_routes_answer_a_refusal_with_its_reason(tmp_path: Path) -> None:
    peers = RepositoryPeers(tmp_path)
    codex = session(peers, tmp_path, "codex", runtime="codex")
    key = known(tmp_path).key()

    async with client(tmp_path) as http:
        refused = await http.post(
            f"/api/repositories/{key}/sessions/{codex}/messages",
            headers=WRITING,
            json={"text": "stop", "priority": "now"},
        )
        missing = await http.post(
            f"/api/repositories/{key}/sessions/nobody/wake", headers=WRITING, json={}
        )
        page = await http.get(
            f"/api/repositories/{key}/sessions/{codex}/transcript",
            headers={"Authorization": f"Bearer {TOKEN}"},
        )

    assert refused.status_code == 409 and "turn/interrupt" in refused.json()["detail"]
    assert missing.status_code == 404
    assert page.status_code == 404 and "no transcript" in page.json()["detail"]


def test_the_stream_says_every_piece_of_supervision_these_routes_serve(
    tmp_path: Path,
) -> None:
    feed = LiveFeed(lambda: [known(tmp_path)], ReviewStore(roots=(tmp_path,)))
    app = dashboard_app(BASE_URL, TOKEN, (tmp_path,), feed=feed)
    paths = {getattr(route, "path", "") for route in app.routes}

    assert feed.state.snapshot().served == list(SUPERVISED)
    assert {
        "/api/repositories/{repository}/sessions/{member}/messages",
        "/api/repositories/{repository}/sessions/{member}/wake",
        "/api/repositories/{repository}/sessions/{member}/name",
        "/api/repositories/{repository}/sessions/{member}/stop",
        "/api/repositories/{repository}/sessions/{member}/transcript",
        "/api/transcripts/follow",
        "/api/repositories/{repository}/broadcast",
        "/api/repositories/{repository}/notices",
        "/api/repositories/{repository}/notices/{notice_id}",
        "/api/user/description",
        "/api/repositories/{repository}/claims",
        "/api/repositories/{repository}/inbox/read",
        "/api/repositories/{repository}/threads/{thread}/posts",
    } <= paths
