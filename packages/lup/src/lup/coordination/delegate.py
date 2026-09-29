"""Handing one piece of work to somebody, in the order that cannot lose it.

Five things happen and the order is the whole design. The task is recorded
first, because it is the only durable artefact and everything after it is a
convenience: a delegation whose mail failed is a task somebody can still find,
and a wake that failed costs latency rather than work. Then the holder is set,
the paths are taken, mail is left, and only last does anything try to make the
peer look.

**Nothing is refused for want of a target.** A name nobody answers to parks
the task unheld, which is a real state and often the right one — work is
frequently scoped before there is anybody to do it. What a caller gets back
says which happened, so a skill can report it rather than having to guess.

**A wake is never a substitute for the record.** Mail is written whether or
not anything can nudge the peer, and the two runtimes differ in whether this
library can do the nudging at all — :mod:`lup.coordination.wake` carries that
asymmetry so this does not have to. A wake carries the mail whole, so what one
the peer's runtime accepted carried is handed over with it, and the peer's
hook does not hand it over again.
"""

from pathlib import Path

from lup.coordination.repository import RepositoryPeers
from lup.coordination.tasks import Delegation, Needs, Task
from lup.coordination.watch import roused
from lup.ledger.journal import LedgerStore
from lup.types import JsonValue


def delegate(
    peers: RepositoryPeers,
    store: LedgerStore,
    title: str,
    to: str = "",
    text: str = "",
    needs: Needs = "",
    paths: list[str] | None = None,
    root: Path | None = None,
) -> Delegation:
    """Record one task, hand it to a peer if there is one, and say what happened.

    The task lands whatever else does. Everything after it is reported rather
    than required, so a caller reads one value and knows whether to chase
    anything — which is the difference between a delegation that quietly
    reached nobody and one that says it parked.
    """
    member = peers.address(to) if to else None
    holder = to if member is not None else ""
    # Widened where it crosses into the record, because a node's fields
    # are stored JSON and `list[str]` is not one of those by identity.
    declared: list[JsonValue] = [str(path) for path in paths or []]
    task = store.record(
        Task,
        title,
        text=text,
        holder=holder,
        needs=needs,
        paths=declared,
    )
    if member is None:
        return Delegation(
            task=task,
            note=(
                f"nothing answers to {to!r}, so this is parked for whoever picks it up"
                if to
                else "parked unheld, for whoever picks it up"
            ),
        )
    # Taken on the holder's behalf and after the task exists, so a lock never
    # outlives the record that explains it. They expire with the holder's
    # roster entry, which is why nothing here has a release to forget.
    locked = [str(path) for path in paths or []]
    for path in locked:
        peers.lock(member.id, Path(path))
    message = f"{task.id}: {title}" + (f"\n{text}" if text else "")
    peers.send(to, message, sender=store.author.id)
    reached = next(
        (view for view in peers.listing() if view.member.actor.id == member.id), None
    )
    woken = (
        roused(peers, reached.member, peers.waiting(member.id).messages, root)
        if reached is not None
        else None
    )
    return Delegation(
        task=task,
        holder=to,
        locked=locked,
        delivered=True,
        woken=woken.reached if woken else False,
        note=woken.reason if woken else "",
    )
