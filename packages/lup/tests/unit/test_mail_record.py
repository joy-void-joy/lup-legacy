"""Mail says who sent it, and every message stays on the record after it is read.

A mailbox is its own position: a message leaves it when its reader takes it,
so the mailbox alone cannot answer what was said to a session an hour ago, or
by whom. The record beside it can — one line per message posted, appended by
whoever posted it — and a reader follows it from where it last stopped. The
record is kept whole, every message for the life of the clone, so a reader
never reads it whole: it reads the latest page from the end, and pages back
from where a page starts.
"""

import json
from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path
from typing import BinaryIO

import pytest

from lup.channels.models import Door, utc_now
from lup.coordination import mail as typed_mail
from lup.coordination.bare import mail as bare_mail
from lup.coordination.bare.store import MAIL_RECORD, session_actor, stamped
from lup.coordination.identity import mint_member_id
from lup.coordination.mail import ActorMail, MailCursor, MailPage, RecordLine
from lup.coordination.peers import USER_ADDRESS
from lup.coordination.refs import ActorRef
from lup.coordination.repository import RepositoryPeers
from lup.coordination.watch import nudge_text


def joined(peers: RepositoryPeers, name: str, root: Path) -> str:
    member = mint_member_id()
    peers.join(member, root / name, cli_name=name)
    return member


def test_a_posted_message_is_on_the_record_after_its_reader_took_it(
    tmp_path: Path,
) -> None:
    reader = ActorRef(kind="session", id="reader")
    mail = ActorMail(tmp_path)
    mail.send(reader, "the base moved", door=Door.AGENT, sender="writer")

    mail.delivered(reader, mail.waiting(reader))

    page = mail.posted(MailCursor())
    assert mail.waiting(reader).messages == []
    assert [
        (each.recipient, each.message.sender, each.message.text)
        for each in page.messages
    ] == [(reader, "writer", "the base moved")]
    assert [each.at for each in page.messages] == [0]
    assert page.start == 0


def test_the_record_is_followed_from_where_a_reader_stopped(tmp_path: Path) -> None:
    reader = ActorRef(kind="session", id="reader")
    mail = ActorMail(tmp_path)
    mail.send(reader, "first")
    mail.send(reader, "second")
    first = mail.posted(MailCursor())

    mail.send(reader, "third")
    with (tmp_path / MAIL_RECORD).open("a", encoding="utf-8") as record:
        record.write(
            '{"recipient": {"kind": "session", "id": "reader"}, "message": {"id'
        )
    then = mail.posted(first.cursor)

    assert [each.message.text for each in first.messages] == ["first", "second"]
    assert [each.message.text for each in then.messages] == ["third"]
    assert then.start == first.cursor.offset == then.messages[0].at
    assert mail.posted(then.cursor).messages == []

    with (tmp_path / MAIL_RECORD).open("a", encoding="utf-8") as record:
        record.write('": "m4", "text": "fourth"}}\n')
    assert [each.message.text for each in mail.posted(then.cursor).messages] == [
        "fourth"
    ]


def test_a_record_begun_again_is_read_from_its_latest_page(tmp_path: Path) -> None:
    reader = ActorRef(kind="session", id="reader")
    mail = ActorMail(tmp_path)
    mail.send(reader, "before the record was replaced")
    read = mail.posted(MailCursor())
    (tmp_path / MAIL_RECORD).write_text("", encoding="utf-8")
    mail.send(reader, "after")

    again = mail.posted(read.cursor)

    assert [(each.at, each.message.text) for each in again.messages] == [(0, "after")]


def test_a_peer_message_names_the_session_that_sent_it(tmp_path: Path) -> None:
    peers = RepositoryPeers(tmp_path)
    writer = joined(peers, "writer", tmp_path)
    reader = joined(peers, "reader", tmp_path)

    peers.send(reader, "rebase before you commit", sender=writer)

    waiting = peers.waiting(reader).messages
    assert [message.sender for message in waiting] == [writer]
    handed = bare_mail.spoken(bare_mail.waiting(peers.root, f"session-{reader}"))
    assert handed == f"[message from {writer} by agent] rebase before you commit"


def test_mail_nobody_signed_reads_as_it_did(tmp_path: Path) -> None:
    peers = RepositoryPeers(tmp_path)
    reader = joined(peers, "reader", tmp_path)

    peers.send(reader, "a door said this")

    handed = bare_mail.spoken(bare_mail.waiting(peers.root, f"session-{reader}"))
    assert handed == "[message by agent] a door said this"


def posted_at(root: Path, reader: str, body: str, when: datetime) -> None:
    """Post one message to *reader* as though it had been sent at *when*."""
    message = bare_mail.new_message(sender="writer", to=reader, body=body, door="agent")
    message["sent_at"] = stamped(when)
    bare_mail.post(root, session_actor(reader), message)


def texts(page: MailPage) -> list[str]:
    """What each message a page read says, oldest first."""
    return [each.message.text for each in page.messages]


def numbered(count: int, first: int = 0) -> str:
    """*count* lines of the record, one message each, numbered from *first*."""
    return "".join(
        json.dumps(
            bare_mail.Posted(
                recipient=session_actor("reader"),
                message=bare_mail.new_message(
                    sender="writer",
                    to="session:reader#1",
                    body=f"message {index}",
                    door="agent",
                ),
            )
        )
        + "\n"
        for index in range(first, first + count)
    )


def written(root: Path, text: str) -> Path:
    """The record at *root*, holding exactly *text*."""
    root.mkdir(parents=True, exist_ok=True)
    record = root / MAIL_RECORD
    record.write_text(text, encoding="utf-8")
    return record


def examined(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Where each line a read went through starts, as the reader takes them."""
    seen: list[int] = []
    reading = typed_mail.backward

    def counted(
        record: BinaryIO, end: int, floor: int, block: int
    ) -> Iterator[RecordLine]:
        for line in reading(record, end, floor, block):
            seen.append(line.start)
            yield line

    monkeypatch.setattr(typed_mail, "backward", counted)
    return seen


def test_a_sweep_keeps_every_message_however_old(tmp_path: Path) -> None:
    peers = RepositoryPeers(tmp_path)
    reader = joined(peers, "reader", tmp_path)
    window = timedelta(seconds=peers.retention.departed_seconds)
    posted_at(peers.root, reader, "long ago", utc_now() - window * 3)
    posted_at(peers.root, reader, "a while ago", utc_now() - window * 2)
    posted_at(peers.root, reader, "just now", utc_now())
    before = (peers.root / MAIL_RECORD).read_bytes()

    peers.sweep(now=utc_now() + window * 2)

    assert (peers.root / MAIL_RECORD).read_bytes() == before
    assert texts(ActorMail(peers.root).posted(MailCursor())) == [
        "long ago",
        "a while ago",
        "just now",
    ]


def test_the_latest_page_of_a_long_record_is_read_from_its_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    written(tmp_path, numbered(10_000))
    lines = examined(monkeypatch)

    page = ActorMail(tmp_path).posted(MailCursor())

    assert texts(page) == [f"message {index}" for index in range(9_900, 10_000)]
    assert page.start == page.messages[0].at > 0
    # The page's own lines, and the one older message that says there is more.
    assert len(lines) == typed_mail.MAIL_PAGE + 1


@pytest.mark.parametrize("block", [64, typed_mail.MAIL_BLOCK])
def test_older_pages_are_read_back_to_the_record_s_start(
    tmp_path: Path, block: int
) -> None:
    written(tmp_path, numbered(250))
    mail = ActorMail(tmp_path)

    latest = mail.posted(MailCursor(), limit=100, block=block)
    middle = mail.earlier(latest.start, limit=100, block=block)
    oldest = mail.earlier(middle.start, limit=100, block=block)

    assert texts(oldest) + texts(middle) + texts(latest) == [
        f"message {index}" for index in range(250)
    ]
    assert (len(oldest.messages), oldest.start) == (50, 0)
    assert 0 < middle.start < latest.start


def test_a_record_an_older_sweep_cut_still_reads(tmp_path: Path) -> None:
    head = '{"cut": 121}\n'
    written(tmp_path, head + numbered(3, first=121) + '{"recipient": {"ki')
    mail = ActorMail(tmp_path)

    page = mail.posted(MailCursor())

    assert texts(page) == ["message 121", "message 122", "message 123"]
    assert (page.start, page.messages[0].at) == (0, len(head))
    assert texts(mail.earlier(page.messages[1].at)) == ["message 121"]
    assert page.cursor.offset == (tmp_path / MAIL_RECORD).stat().st_size - len(
        '{"recipient": {"ki'
    )


def test_a_reader_further_behind_than_a_page_is_handed_the_latest(
    tmp_path: Path,
) -> None:
    record = written(tmp_path, numbered(2))
    mail = ActorMail(tmp_path)
    first = mail.posted(MailCursor())
    with record.open("a", encoding="utf-8") as appended:
        appended.write(numbered(5, first=2))

    then = mail.posted(first.cursor, limit=3)

    assert texts(then) == ["message 4", "message 5", "message 6"]
    assert then.start > first.cursor.offset
    assert texts(mail.earlier(then.start)) == [f"message {index}" for index in range(4)]


def test_a_nudge_names_who_sent_each_message(tmp_path: Path) -> None:
    peers = RepositoryPeers(tmp_path)
    reader = joined(peers, "reader", tmp_path)
    peers.send(reader, "look at the dashboard", door=Door.PAGE, sender=USER_ADDRESS)

    carried = nudge_text(peers.waiting(reader).messages)

    assert "from user by page —\nlook at the dashboard" in carried
