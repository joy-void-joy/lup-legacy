"""What the dashboard reads of every session: who is here, what each is doing, what was said.

Read off what already exists — the roster's rows, the transcript each row
names, the mail record — as it changes, and nothing written for the
dashboard's sake. A reply from the operator goes the way a session's own
message to a peer goes, signed `user`.
"""

import json
from pathlib import Path

import pytest

from lup.channels.models import Door
from lup.coordination import watch as watching
from lup.coordination.bare import store
from lup.coordination.bare.changes import changes
from lup.coordination.identity import mint_member_id
from lup.coordination.mail import ActorMail, MailCursor
from lup.coordination.repository import PeerDepartedError, RepositoryPeers
from lup.coordination.wake import WakePath, Woken
from lup.devtools.dashboard.companion import KnownRepository
from lup.devtools.dashboard.live import (
    RepositoryWatch,
    TranscriptFollower,
    reply,
)
from lup.types import JsonObject


def known(root: Path) -> KnownRepository:
    return KnownRepository(repository=root, checkout=root)


def session(peers: RepositoryPeers, root: Path, name: str) -> str:
    member = mint_member_id()
    peers.join(member, root / name, cli_name=name)
    return member


def transcript_of(peers: RepositoryPeers, member: str, root: Path, path: Path) -> None:
    """Record *path* as the transcript *member*'s last prompt came from, as the prompt fold does."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()
    changes(peers.root, member, root, str(path))


def said(text: str) -> JsonObject:
    return {
        "type": "assistant",
        "uuid": text,
        "timestamp": "2026-09-29T10:00:00Z",
        "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
    }


def called(call: str, tool: str, arguments: JsonObject) -> JsonObject:
    return {
        "type": "assistant",
        "uuid": call,
        "timestamp": "2026-09-29T10:00:05Z",
        "message": {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": call, "name": tool, "input": arguments}
            ],
        },
    }


def answered(call: str) -> JsonObject:
    return {
        "type": "user",
        "uuid": f"{call}-result",
        "timestamp": "2026-09-29T10:00:09Z",
        "message": {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": call, "content": "ok"}],
        },
    }


def appended(path: Path, *records: JsonObject) -> None:
    with path.open("a", encoding="utf-8") as transcript:
        for record in records:
            transcript.write(json.dumps(record) + "\n")


def test_sessions_carry_their_subagents_what_each_does_and_holds(
    tmp_path: Path,
) -> None:
    peers = RepositoryPeers(tmp_path)
    lead = session(peers, tmp_path, "lead")
    other = session(peers, tmp_path, "other")
    peers.describe(lead, "rebuilding the dashboard")
    held = tmp_path / "held.py"
    held.write_text("x = 1\n", encoding="utf-8")
    peers.touched(lead, held)
    store.joined_subagent(
        peers.root,
        lead,
        store.Caller(
            agent_id="a1", agent_type="Explore", cwd=str(tmp_path), name="scout"
        ),
    )

    rows = {row.id: row for row in RepositoryWatch(known(tmp_path)).sessions(0.0)}

    child = store.subagent_id(lead, "a1")
    assert set(rows) == {lead, other, child}
    assert rows[lead].name == "lead"
    assert rows[lead].doing == "rebuilding the dashboard"
    assert rows[lead].holding == [f"at {held}"]
    assert rows[lead].running
    assert rows[child].parent == lead
    assert rows[child].name == "scout"
    assert rows[child].kind == "subagent"
    assert rows[lead].key == f"{known(tmp_path).key()}/{lead}"
    assert rows[lead].repository == known(tmp_path).key()


def test_activity_is_the_last_thing_said_and_the_call_still_outstanding(
    tmp_path: Path,
) -> None:
    peers = RepositoryPeers(tmp_path)
    lead = session(peers, tmp_path, "lead")
    transcript = tmp_path / "home" / "lead.jsonl"
    transcript_of(peers, lead, tmp_path, transcript)
    appended(
        transcript,
        said("Reading the roster."),
        called("t1", "Read", {"file_path": "a.py"}),
    )
    watch = RepositoryWatch(known(tmp_path))

    first = {row.id: row for row in watch.sessions(0.0)}[lead].activity
    appended(transcript, answered("t1"), said("The roster is fine."))
    then = {row.id: row for row in watch.sessions(1.0)}[lead].activity

    assert first.said == "Reading the roster."
    assert first.calling == "Read"
    assert first.arguments == {"file_path": "a.py"}
    assert first.transcript == str(transcript)
    assert then.said == "The roster is fine."
    assert then.calling == ""
    assert then.arguments == {}


def test_a_subagent_is_read_from_its_own_transcript(tmp_path: Path) -> None:
    peers = RepositoryPeers(tmp_path)
    lead = session(peers, tmp_path, "lead")
    transcript = tmp_path / "home" / "lead.jsonl"
    transcript_of(peers, lead, tmp_path, transcript)
    store.joined_subagent(
        peers.root,
        lead,
        store.Caller(
            agent_id="a1", agent_type="Explore", cwd=str(tmp_path), name="scout"
        ),
    )
    sidechain = transcript.with_suffix("") / "subagents" / "agent-a1.jsonl"
    sidechain.parent.mkdir(parents=True)
    appended(sidechain, said("Searching the store."))

    rows = {row.id: row for row in RepositoryWatch(known(tmp_path)).sessions(0.0)}

    child = rows[store.subagent_id(lead, "a1")]
    assert child.activity.said == "Searching the store."
    assert child.activity.transcript == str(sidechain)
    assert rows[lead].activity.said == ""


def test_a_long_transcript_is_read_from_its_recent_end(tmp_path: Path) -> None:
    transcript = tmp_path / "long.jsonl"
    appended(transcript, *[said(f"early {index} " + "x" * 200) for index in range(200)])
    appended(transcript, called("t9", "Bash", {"command": "ls"}))
    follower = TranscriptFollower(transcript, tail=4096)

    activity = follower.advance()

    assert activity.said.startswith("early 199 ")
    assert activity.calling == "Bash"
    assert follower.offset == transcript.stat().st_size


def test_a_line_being_written_waits_for_the_next_read(tmp_path: Path) -> None:
    transcript = tmp_path / "t.jsonl"
    appended(transcript, said("whole"))
    with transcript.open("a", encoding="utf-8") as partial:
        partial.write(json.dumps(said("half"))[:20])
    follower = TranscriptFollower(transcript)

    assert follower.advance().said == "whole"
    with transcript.open("a", encoding="utf-8") as rest:
        rest.write(json.dumps(said("half"))[20:] + "\n")
    assert follower.advance().said == "half"


def test_messages_are_read_off_the_record_with_whether_each_still_waits(
    tmp_path: Path,
) -> None:
    peers = RepositoryPeers(tmp_path)
    lead = session(peers, tmp_path, "lead")
    other = session(peers, tmp_path, "other")
    peers.send(other, "your turn", sender=lead)
    watch = RepositoryWatch(known(tmp_path))
    watch.sessions(0.0)

    first = watch.fresh_messages()
    peers.take(other)
    then = watch.fresh_messages()
    quiet = watch.fresh_messages()

    assert [
        (each.sender, each.recipient, each.text, each.waiting) for each in first
    ] == [(lead, other, "your turn", True)]
    assert [(each.id, each.waiting) for each in then] == [(first[0].id, False)]
    assert quiet == []
    assert first[0].key == f"{known(tmp_path).key()}/{first[0].id}"
    rows = {row.id: row for row in watch.sessions(0.0)}
    assert rows[other].waiting == 0


def test_a_watch_holds_where_the_latest_page_of_what_it_read_starts(
    tmp_path: Path,
) -> None:
    peers = RepositoryPeers(tmp_path)
    lead = session(peers, tmp_path, "lead")
    for index in range(2):
        peers.send(lead, f"early {index}")
    watch = RepositoryWatch(known(tmp_path), page=3)

    first = watch.fresh_messages()
    whole = watch.extent().earlier
    for index in range(2):
        peers.send(lead, f"later {index}")
    then = watch.fresh_messages()
    past = watch.extent().earlier
    for index in range(5):
        peers.send(lead, f"burst {index}")
    burst = watch.fresh_messages()

    assert [each.text for each in first] == ["early 0", "early 1"]
    assert whole == 0
    assert [each.text for each in then] == ["later 0", "later 1"]
    # Four read is one past a page: the stream's messages start at the second.
    assert past == first[1].at
    # Five posted since the last look is more than a page: the look reads the
    # latest page, and the stream's messages start where it does.
    assert [each.text for each in burst] == [f"burst {index}" for index in (2, 3, 4)]
    assert watch.extent().earlier == burst[0].at


def test_a_session_counts_what_waits_for_it(tmp_path: Path) -> None:
    peers = RepositoryPeers(tmp_path)
    lead = session(peers, tmp_path, "lead")
    peers.send(lead, "one")
    peers.send(lead, "two")

    rows = {row.id: row for row in RepositoryWatch(known(tmp_path)).sessions(0.0)}

    assert rows[lead].waiting == 2


def waking(monkeypatch: pytest.MonkeyPatch, reached: bool) -> list[str]:
    """Every wake a reply makes, answered *reached* rather than written anywhere."""
    woken: list[str] = []

    def nudged(
        path: WakePath,
        message: str,
        cwd: Path | None = None,
        *,
        queue_timeout_seconds: float = 20.0,
    ) -> Woken:
        del path, cwd, queue_timeout_seconds
        woken.append(message)
        return Woken(reached=reached, reason="" if reached else "nobody listening")

    monkeypatch.setattr(watching, "wake", nudged)
    return woken


def test_a_reply_reaches_a_session_as_the_user_and_wakes_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    peers = RepositoryPeers(tmp_path)
    lead = session(peers, tmp_path, "lead")
    woken = waking(monkeypatch, reached=True)

    outcome = reply(known(tmp_path), lead, "stop and rebase onto staging")

    posted = ActorMail(peers.root).posted(MailCursor()).messages
    assert [(m.message.sender, m.message.door, m.message.text) for m in posted] == [
        ("user", Door.PAGE, "stop and rebase onto staging")
    ]
    assert outcome.queued and outcome.woken
    assert outcome.session == f"{known(tmp_path).key()}/{lead}"
    assert len(woken) == 1
    assert "from user by page —\nstop and rebase onto staging" in woken[0]
    # The wake carried it whole, so the session's hook has nothing of it to
    # hand over again at its next tool call.
    assert peers.waiting(lead).messages == []


def test_a_reply_nothing_woke_for_waits_for_the_next_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    peers = RepositoryPeers(tmp_path)
    lead = session(peers, tmp_path, "lead")
    waking(monkeypatch, reached=False)

    outcome = reply(known(tmp_path), lead, "stop and rebase onto staging")

    assert outcome.queued and not outcome.woken
    assert [m.text for m in peers.waiting(lead).messages] == [
        "stop and rebase onto staging"
    ]


def test_a_reply_to_a_subagent_waits_for_its_next_call(tmp_path: Path) -> None:
    peers = RepositoryPeers(tmp_path)
    lead = session(peers, tmp_path, "lead")
    child = store.joined_subagent(
        peers.root,
        lead,
        store.Caller(
            agent_id="a1", agent_type="Explore", cwd=str(tmp_path), name="scout"
        ),
    )
    assert child is not None

    outcome = reply(known(tmp_path), store.subagent_id(lead, "a1"), "look at mail.py")

    assert outcome.queued and not outcome.woken
    assert "next tool call" in outcome.detail
    assert [m.text for m in peers.waiting(store.subagent_id(lead, "a1")).messages] == [
        "look at mail.py"
    ]


def test_a_reply_to_nobody_or_to_a_session_that_left_is_refused(tmp_path: Path) -> None:
    peers = RepositoryPeers(tmp_path)
    lead = session(peers, tmp_path, "lead")
    peers.leave(lead, "done")

    with pytest.raises(LookupError):
        reply(known(tmp_path), "nobody-here", "hello")
    with pytest.raises(PeerDepartedError):
        reply(known(tmp_path), lead, "hello")
