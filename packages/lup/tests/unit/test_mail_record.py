"""Mail says who sent it, and every message stays on the record after it is read.

A mailbox is its own position: a message leaves it when its reader takes it,
so the mailbox alone cannot answer what was said to a session an hour ago, or
by whom. The record beside it can — one line per message posted, appended by
whoever posted it — and a reader follows it from where it last stopped. The
record holds the roster's retention window and no more, so a reader following
it is carried across the sweep that cuts its head.
"""

import fcntl
import threading
from datetime import datetime, timedelta
from pathlib import Path

from lup.channels.models import Door, utc_now
from lup.coordination.bare import mail as bare_mail
from lup.coordination.bare.store import MAIL_RECORD, session_actor, stamped
from lup.coordination.identity import mint_member_id
from lup.coordination.mail import ActorMail, MailCursor, MailPage
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
    assert [each.seq for each in page.messages] == [0]


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
    assert [(each.seq, each.message.text) for each in then.messages] == [(2, "third")]
    assert mail.posted(then.cursor).messages == []


def test_a_record_that_shrank_is_read_again_from_its_start(tmp_path: Path) -> None:
    reader = ActorRef(kind="session", id="reader")
    mail = ActorMail(tmp_path)
    mail.send(reader, "before the record was replaced")
    read = mail.posted(MailCursor())
    (tmp_path / MAIL_RECORD).write_text("", encoding="utf-8")
    mail.send(reader, "after")

    again = mail.posted(read.cursor)

    assert [(each.seq, each.message.text) for each in again.messages] == [(0, "after")]


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


def texts(page: MailPage) -> list[tuple[int, str]]:
    """Each message a page read, at the line it sits on."""
    return [(each.seq, each.message.text) for each in page.messages]


def test_the_sweep_keeps_the_record_to_the_retention_window(tmp_path: Path) -> None:
    peers = RepositoryPeers(tmp_path)
    reader = joined(peers, "reader", tmp_path)
    window = timedelta(seconds=peers.retention.departed_seconds)
    posted_at(peers.root, reader, "long ago", utc_now() - window * 3)
    posted_at(peers.root, reader, "a while ago", utc_now() - window * 2)
    posted_at(peers.root, reader, "just now", utc_now())

    peers.sweep()

    record = (peers.root / MAIL_RECORD).read_text(encoding="utf-8").splitlines()
    assert len(record) == 2
    assert texts(ActorMail(peers.root).posted(MailCursor())) == [(2, "just now")]


def test_a_record_inside_the_window_is_left_as_it_is(tmp_path: Path) -> None:
    peers = RepositoryPeers(tmp_path)
    reader = joined(peers, "reader", tmp_path)
    posted_at(peers.root, reader, "just now", utc_now())
    before = (peers.root / MAIL_RECORD).stat()

    peers.sweep()

    after = (peers.root / MAIL_RECORD).stat()
    assert (after.st_ino, after.st_size) == (before.st_ino, before.st_size)


def test_a_reader_follows_the_record_across_a_trim(tmp_path: Path) -> None:
    peers = RepositoryPeers(tmp_path)
    reader = joined(peers, "reader", tmp_path)
    mail = ActorMail(peers.root)
    window = timedelta(seconds=peers.retention.departed_seconds)
    posted_at(peers.root, reader, "stale", utc_now() - window * 2)
    posted_at(peers.root, reader, "read before the trim", utc_now())
    first = mail.posted(MailCursor())

    peers.sweep()
    for count in range(4):
        posted_at(peers.root, reader, f"after the trim {count}", utc_now())
    then = mail.posted(first.cursor)

    assert texts(first) == [(0, "stale"), (1, "read before the trim")]
    assert texts(then) == [(2 + count, f"after the trim {count}") for count in range(4)]
    assert mail.posted(then.cursor).messages == []


def test_a_record_replaced_under_a_waiting_sender_takes_its_line(
    tmp_path: Path,
) -> None:
    peers = RepositoryPeers(tmp_path)
    reader = joined(peers, "reader", tmp_path)
    posted_at(peers.root, reader, "before", utc_now())
    record = peers.root / MAIL_RECORD
    racing = threading.Thread(
        target=posted_at, args=(peers.root, reader, "racing", utc_now())
    )
    with record.open("a", encoding="utf-8") as held:
        fcntl.flock(held.fileno(), fcntl.LOCK_EX)
        racing.start()
        racing.join(timeout=0.2)
        replacement = record.with_name("replacement")
        replacement.write_text(record.read_text(encoding="utf-8"), encoding="utf-8")
        replacement.replace(record)
        fcntl.flock(held.fileno(), fcntl.LOCK_UN)
    racing.join()

    read = ActorMail(peers.root).posted(MailCursor())
    assert [text for _, text in texts(read)] == ["before", "racing"]


def test_a_nudge_names_who_sent_each_message(tmp_path: Path) -> None:
    peers = RepositoryPeers(tmp_path)
    reader = joined(peers, "reader", tmp_path)
    peers.send(reader, "look at the dashboard", door=Door.PAGE, sender=USER_ADDRESS)

    carried = nudge_text(peers.waiting(reader).messages)

    assert "from user by page —\nlook at the dashboard" in carried
