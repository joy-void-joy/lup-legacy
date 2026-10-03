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

import json
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
    locked,
    published,
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
    post: str
    """Shared by every copy one send left, one per recipient."""

    thread: str
    """The post its thread began with: its own where it began one."""

    title: str
    """The discussion it was posted into, empty where it is a message to one."""

    participants: list[str]
    """Everyone else in that discussion, as its reader addresses each."""

    carried: bool
    """A wake has put this redirect in front of its reader already; it waits
    only for the hook to refuse the reader's next tool call with it."""


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


def new_post_id() -> str:
    """One post's id, short enough for a reader to copy into a reply."""
    return uuid4().hex[:12]


def new_message(
    sender: str,
    to: str,
    body: str,
    door: str,
    in_reply_to: str = "",
    redirect: bool = False,
    post: str = "",
    thread: str = "",
    title: str = "",
    participants: tuple[str, ...] = (),
) -> Message:
    """One message, stamped and identified, for a sender about to post it.

    The id is minted here rather than derived from the content, because two
    identical messages are two messages: a door repeating itself means it.
    A *post* is minted too where the sender names none, and a message in no
    *thread* begins its own; a sender leaving copies in several mailboxes
    passes one post to all of them.
    """
    posted = post or new_post_id()
    return Message(
        post=posted,
        thread=thread or posted,
        title=title,
        participants=list(participants),
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


def recorded(root: Path, posted: Posted) -> bool:
    """Append one line to the mail record, saying whether it was written.

    Appended and never rewritten, so a line keeps the byte it starts at for
    as long as the record stands, and a reader paging back from that byte
    finds exactly what was older; under a lock on the record itself, so two
    senders' lines never interleave however long either message is.
    """
    try:
        with locked(root / MAIL_RECORD) as record:
            record.write(json.dumps(posted) + "\n")
            record.flush()
    except OSError:
        return False
    return True


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


def mark_carried(root: Path, mailbox: str, messages: list[Message]) -> None:
    """Record that a wake carried these messages, leaving each in the mailbox.

    What a redirect a wake carried needs: its reader has read it, and its
    next tool call has still to be refused with it, which only the hook
    handing it over can do. Each file is rewritten whole by rename, so the
    hook reading it at that moment reads one or the other.
    """
    for message in messages:
        path = message_path(root, mailbox, text(message.get("id")))
        found = loaded(path, Message)
        if found is not None:
            carried = found.copy()
            carried["carried"] = True
            published(path, carried)


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


def heading(message: Message) -> str:
    """What a reader is told before one message's text: what it is, who sent it, through what.

    Who sent it is named where the sender signed it — a peer's id, or `user`
    for the person — which is the address a reply goes to, and its post is
    what a reply names as the one it answers. A message posted into a
    discussion is headed by the discussion first: its title, everyone else
    in it, and the thread a reply posts into, so its reader can answer all of
    them rather than whoever happened to write last.
    """
    kind = "redirected" if message.get("redirect") else "message"
    sender = text(message.get("sender"))
    signed = f" from {sender}" if sender else ""
    post = text(message.get("post"))
    marked = f" · post {post}" if post else ""
    said = f"[{kind}{signed} by {text(message.get('door')) or 'peer'}{marked}]"
    title = text(message.get("title"))
    if not title:
        return said
    found = message.get("participants")
    others = (
        ", ".join(each for each in found if isinstance(each, str))
        if isinstance(found, list)
        else ""
    )
    return f"[discussion «{title}» · with {others} · thread {text(message.get('thread'))}] {said}"


def spoken(messages: list[Message]) -> str:
    """What a member reads when its mail is put in front of it.

    One line per message, naming what carried each, because a redirect and an
    ordinary message ask different things of the reader and a rendering that
    hid the difference hid it from the one party that needed it.
    """
    return "\n".join(
        f"{heading(message)} {text(message.get('text'))}" for message in messages
    )
