"""What is waiting for one member, and what is true for all of them.

Two things reach a member and only one of them is mail, which is the whole of
what this module separates.

**A message is addressed and consumed.** It is one file in one member's mailbox,
written by the sender and deleted by the recipient, so "what is waiting for
me" is a directory listing and there is no position for anybody to keep. No
token in the store means *everyone*: a sender resolves it against the roster
and writes one file per live member, which is the only reading of "everyone" a
message can honestly have — you cannot stop, or interrupt, a member that does
not exist yet.

**A notice is neither.** "The base moved under all of you" is a fact about the
population rather than about its recipients: it stays true after it is said,
and a member that arrives afterwards needs it as much as one already here. So
it is not delivered at all. It sits in one file per notice, is read at the
head of every turn, and is retracted by deleting it. Nothing consumes it,
which is why nothing has to remember having read it — and why a replayed or
resumed turn reads exactly what a first one did.

A statement worth making is also worth hearing before the turn ends, so
posting a notice fans a message copy out to whoever is live. The member that
arrives later never sees that copy and does not need to: the notice is still
there at its first turn head.

Shipped into each plugin beside the fold it stands on, so everything resolves
on a bare interpreter: the standard library, and :mod:`.store` as a sibling.
Nothing here raises, for the reason nothing there does — this runs before a
tool call, and mail that cannot be read must not stop the work it was meant to
inform.
"""

import fcntl
import json
import os
from datetime import datetime
from io import TextIOWrapper
from pathlib import Path
from typing import TypedDict
from uuid import uuid4

from .store import (
    MAIL_RECORD,
    MAILBOX_DIR,
    NOTICES_DIR,
    Actor,
    conversation_of,
    discarded,
    listed,
    loaded,
    published,
    spoken_at,
    stamped,
    text,
)


class Message(TypedDict, total=False):
    """One thing said to one member, as a file in that member's mailbox.

    ``sender`` rather than ``from``, which is not a name a field can have in
    this language. Everything else is spelled as the typed writer spells it.
    """

    id: str
    sender: str
    to: str
    text: str
    door: str
    redirect: bool
    in_reply_to: str
    sent_at: str


class Notice(TypedDict, total=False):
    """One standing fact about this population, true until it is retracted.

    Never addressed and never consumed, which is the whole difference from a
    message: a member that did not exist when this was posted reads it at the
    head of its first turn, and a member that has read it reads it again,
    because it has not stopped being true.
    """

    id: str
    text: str
    door: str
    by: str
    posted_at: str


class Posted(TypedDict, total=False):
    """One line of the mail record: a message, and the member whose mailbox it was put in."""

    recipient: Actor
    message: Message


class Cut(TypedDict):
    """The first line of a record whose head a sweep cut: how many lines went, all told.

    On the record rather than beside it, so the count and the lines it counts
    are replaced in one rename, and a reader numbering lines from it numbers
    every line kept as it was numbered before the cut.
    """

    cut: int


def mailbox_path(root: Path, mailbox: str) -> Path:
    """Where one member's mail waits, under the conversation that reads it.

    The conversation rather than the bare id, because an id is unique only
    within a kind: a worker and a reviewer taken on over one concern share an
    id and are two members, and one directory between them would hand each
    the other's mail. It is also what outlives a round — a member taken
    through a second round is that member further on, and reads what was said
    to the first.
    """
    return root / MAILBOX_DIR / mailbox


def message_path(root: Path, mailbox: str, message_id: str) -> Path:
    """Where one message to one member sits."""
    return mailbox_path(root, mailbox) / f"{message_id}.json"


def new_message(
    sender: str,
    to: str,
    body: str,
    door: str,
    in_reply_to: str = "",
    redirect: bool = False,
) -> Message:
    """One message, stamped and identified, for a sender about to post it.

    The id is minted here rather than derived from the content, because two
    identical messages are two messages: a door repeating itself means it.
    """
    return Message(
        id=uuid4().hex,
        sender=sender,
        to=to,
        text=body,
        door=door,
        redirect=redirect,
        in_reply_to=in_reply_to,
        sent_at=stamped(),
    )


def post(root: Path, recipient: Actor, message: Message) -> bool:
    """Put one message in one member's mailbox, by rename.

    One recipient, always. Where a sender meant everyone, it resolved that
    against the roster and calls this once per member — so no reader of this
    store has to know what a broadcast is, and a message in a mailbox is a
    message for whoever owns that mailbox.

    Once it has landed it goes on the record too, naming the member it went
    to, which outlives the mailbox copy its reader takes; a record that could
    not be written costs a reader of the history one line, never the
    recipient its message.
    """
    mailbox = conversation_of(recipient)
    landed = published(
        message_path(root, mailbox, text(message.get("id")) or uuid4().hex), message
    )
    if landed is None:
        return False
    recorded(root, Posted(recipient=recipient, message=message))
    return True


def held_record(path: Path) -> TextIOWrapper:
    """The mail record, open to append to and locked against every other writer.

    Checked, once locked, to still be the file at the record's name: a sweep
    cutting the record's head replaces it whole, and a sender that opened the
    one it replaced before taking the lock would otherwise append to a file
    nobody reads again. Closing the handle lets the lock go.
    """
    record = path.open("a", encoding="utf-8")
    try:
        fcntl.flock(record.fileno(), fcntl.LOCK_EX)
        current = path.stat().st_ino == os.fstat(record.fileno()).st_ino
    except FileNotFoundError:
        current = False
    except OSError:
        record.close()
        raise
    if current:
        return record
    record.close()
    return held_record(path)


def recorded(root: Path, posted: Posted) -> bool:
    """Append one line to the mail record, saying whether it was written.

    Appended rather than rewritten, so a reader following the file never meets
    a line it has read change under it; under a lock on the record itself, so
    two senders' lines never interleave however long either message is.
    """
    try:
        root.mkdir(parents=True, exist_ok=True)
        with held_record(root / MAIL_RECORD) as record:
            record.write(json.dumps(posted) + "\n")
            record.flush()
    except OSError:
        return False
    return True


def cut_of(line: bytes) -> int | None:
    """How many lines a sweep cut ahead of the record this is the first line of.

    ``None`` where the line is not that — a message, or a line still being
    written — which is the first line of every record no sweep has cut.
    """
    if not line.endswith(b"\n"):
        return None
    try:
        found: Cut = json.loads(line)
    except ValueError:
        return None
    cut = found.get("cut") if isinstance(found, dict) else None
    return cut if isinstance(cut, int) and not isinstance(cut, bool) else None


def kept(line: bytes, since: datetime) -> bool:
    """Whether one line of the record stays: a message sent since *since*.

    A line still being written stays whatever it says, since its sender is
    only part way through it; a whole line that does not read as a message
    sent at a time goes with the stale ones around it.
    """
    if not line.endswith(b"\n"):
        return True
    try:
        posted: Posted = json.loads(line)
    except ValueError:
        return False
    message = posted.get("message") if isinstance(posted, dict) else None
    sent = (
        spoken_at(text(message.get("sent_at"))) if isinstance(message, dict) else None
    )
    return sent is not None and sent >= since


def stale_head(path: Path, since: datetime) -> bool:
    """Whether the record's first message was sent before *since*, read without the lock.

    What a sweep asks on every tick before it takes the lock and reads the
    whole record: nearly always the head is inside the window and the answer
    costs two lines.
    """
    try:
        with path.open("rb") as record:
            head = record.readline()
            first = record.readline() if cut_of(head) is not None else head
    except OSError:
        return False
    return bool(first) and not kept(first, since)


def trimmed(root: Path, since: datetime) -> int:
    """Cut every line off the record's head sent before *since*, saying how many went.

    The head only, so what stays is every line from the first one kept, in
    the order it was posted. What is left is written whole beneath a first
    line saying how many lines have been cut ahead of it, all told, and put
    in the record's place by rename under the record's lock: a sender waiting
    on the lock appends to what replaced it, and a reader following the file
    is carried to the same line in the replacement by that count.
    """
    path = root / MAIL_RECORD
    if not stale_head(path, since):
        return 0
    try:
        with held_record(path):
            lines = path.read_bytes().splitlines(keepends=True)
            before = cut_of(lines[0]) if lines else None
            body = lines if before is None else lines[1:]
            gone = next(
                (index for index, line in enumerate(body) if kept(line, since)),
                len(body),
            )
            if gone == 0:
                return 0
            replacement = path.with_name(f"{path.name}.{uuid4().hex[:8]}.writing")
            heading = json.dumps(Cut(cut=(before or 0) + gone)).encode() + b"\n"
            replacement.write_bytes(heading + b"".join(body[gone:]))
            replacement.replace(path)
    except OSError:
        return 0
    return gone


def waiting(root: Path, mailbox: str) -> list[Message]:
    """Everything queued for this member, oldest first, consuming none of it.

    Reading is separated from consuming so that asking what a member has
    waiting — which is how a sender learns whether anything was read — cannot
    itself be what makes it disappear.
    """
    found = [
        message
        for path in listed(mailbox_path(root, mailbox))
        for message in [loaded(path, Message)]
        if message is not None and text(message.get("id"))
    ]
    return sorted(found, key=lambda message: text(message.get("sent_at")))


def consume(root: Path, mailbox: str, messages: list[Message]) -> None:
    """Take these messages out of this member's mailbox, having handed them over.

    By deletion, which is what makes the mailbox its own position: there is no
    offset to commit, nothing to re-read after a crash but what was never
    handed over, and a second reader cannot be behind a first.
    """
    for message in messages:
        discarded(message_path(root, mailbox, text(message.get("id"))))


def notice_path(root: Path, notice_id: str) -> Path:
    """Where one standing notice sits."""
    return root / NOTICES_DIR / f"{notice_id}.json"


def new_notice(body: str, door: str, by: str = "") -> Notice:
    """One standing fact, stamped and identified, for a door about to post it."""
    return Notice(id=uuid4().hex[:8], text=body, door=door, by=by, posted_at=stamped())


def notices(root: Path) -> list[Notice]:
    """Every standing fact about this population, oldest first.

    Read rather than delivered, so this is what a member reads at the head of
    every turn — including its first, which is what a message can never reach.
    """
    found = [
        notice
        for path in listed(root / NOTICES_DIR)
        for notice in [loaded(path, Notice)]
        if notice is not None and text(notice.get("text"))
    ]
    return sorted(found, key=lambda notice: text(notice.get("posted_at")))


def post_notice(root: Path, notice: Notice) -> bool:
    """Put one standing fact where every member reads it."""
    landed = published(
        notice_path(root, text(notice.get("id")) or uuid4().hex[:8]), notice
    )
    return landed is not None


def retract(root: Path, notice_id: str) -> bool:
    """Take one standing fact down, saying whether it was there to take down.

    Deleting it rather than marking it retracted, because a notice is read as
    state: what is there is true, and what is true is what is there. A
    tombstone would be a record of something no longer the case, which is not
    a shape this store keeps.
    """
    return discarded(notice_path(root, notice_id))


def spoken(messages: list[Message]) -> str:
    """What a member reads when its mail is put in front of it.

    One line per message, naming what carried each, because a redirect and an
    ordinary message ask different things of the reader and a rendering that
    hid the difference hid it from the one party that needed it. Who sent it
    is named where the sender signed it — a peer's id, or `user` for the
    person — which is the address a reply goes to.
    """

    def heading(message: Message) -> str:
        kind = "redirected" if message.get("redirect") else "message"
        sender = text(message.get("sender"))
        signed = f" from {sender}" if sender else ""
        return f"[{kind}{signed} by {text(message.get('door')) or 'peer'}]"

    return "\n".join(
        f"{heading(message)} {text(message.get('text'))}" for message in messages
    )
