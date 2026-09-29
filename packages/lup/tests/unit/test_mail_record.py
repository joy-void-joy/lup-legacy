"""Mail says who sent it, and every message stays on the record after it is read.

A mailbox is its own position: a message leaves it when its reader takes it,
so the mailbox alone cannot answer what was said to a session an hour ago, or
by whom. The record beside it can — one line per message posted, appended by
whoever posted it — and a reader follows it from where it last stopped.
"""

from pathlib import Path

from lup.channels.models import Door
from lup.coordination.bare import mail as bare_mail
from lup.coordination.bare.store import MAIL_RECORD
from lup.coordination.identity import mint_member_id
from lup.coordination.mail import ActorMail, MailCursor
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
        (each.mailbox, each.message.sender, each.message.text) for each in page.messages
    ] == [(reader.conversation(), "writer", "the base moved")]
    assert [each.seq for each in page.messages] == [0]


def test_the_record_is_followed_from_where_a_reader_stopped(tmp_path: Path) -> None:
    reader = ActorRef(kind="session", id="reader")
    mail = ActorMail(tmp_path)
    mail.send(reader, "first")
    mail.send(reader, "second")
    first = mail.posted(MailCursor())

    mail.send(reader, "third")
    with (tmp_path / MAIL_RECORD).open("a", encoding="utf-8") as record:
        record.write('{"mailbox": "session-reader", "message": {"id": "half')
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


def test_a_nudge_names_who_sent_each_message(tmp_path: Path) -> None:
    peers = RepositoryPeers(tmp_path)
    reader = joined(peers, "reader", tmp_path)
    peers.send(reader, "look at the dashboard", door=Door.PAGE, sender=USER_ADDRESS)

    carried = nudge_text(peers.waiting(reader).messages)

    assert "from user by page —\nlook at the dashboard" in carried
