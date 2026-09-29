"""Server-sent events, read from the run's own journal.

Nothing publishes into this module. A run records everything it does to
``journal.jsonl``, so a connected page is a reader like any other door — it
follows the file. That is what lets a page follow a run no process in this
program is attached to, and why there is no hub, no queue, and no thread to
hand events across.

The stream carries the record rather than a diff of two projections. A diff
answers "what changed since I last looked", which is enough to keep a status
table current and useless for reading what an actor actually did; the record
answers both, and reconnecting replays from the last sequence number seen
rather than from the beginning.
"""

import asyncio
from collections.abc import AsyncGenerator

from lup.resolver.record import Journal, JournalEntry

HEARTBEAT_SECONDS = 15.0
RETRY_MILLISECONDS = 3000
WATCH_INTERVAL_SECONDS = 0.5
FRESH_CATCHUP_ENTRIES = 200
"""How much recent record a reader with no resume point is handed.

A long run's journal passes tens of thousands of entries with individual
lines running to megabytes; replaying it whole into a page that renders per
event froze the browser the stream exists to serve. A fresh page reads the
projection for current state and this much recent record for context; a
reconnecting reader still resumes exactly from what it last saw, and the
per-entry endpoint serves any older sequence whole.
"""


def frame(entry: JournalEntry) -> str:
    """One SSE frame carrying its sequence number as the event id.

    The id is what a reconnect sends back in ``Last-Event-ID``, so numbering
    frames by journal sequence is what makes a resume exact instead of
    approximate.
    """
    return f"id: {entry.seq}\ndata: {entry.model_dump_json()}\n\n"


async def stream(
    journal: Journal,
    after_seq: int = -1,
    interval: float = WATCH_INTERVAL_SECONDS,
    heartbeat: float = HEARTBEAT_SECONDS,
    catchup: int = FRESH_CATCHUP_ENTRIES,
    retry_milliseconds: int = RETRY_MILLISECONDS,
) -> AsyncGenerator[str, None]:
    """Replay what this reader missed, then follow the journal as it grows.

    A reader that names a sequence gets exactly what it missed. One that
    names nothing is a fresh page: it gets the last ``catchup`` entries
    rather than the run from sequence zero, because its current state comes
    from the projection and the whole record is what froze it.

    The tick drives both the events and the keep-alive, so no await is ever
    cancelled to deliver a heartbeat — an async generator interrupted that
    way would be closed rather than resumed, and the stream would go silent
    exactly when the run went quiet.
    """
    yield f"retry: {retry_milliseconds}\n\n"
    caught_up = journal.tail(0)
    missed = (
        caught_up.entries[-catchup:]
        if after_seq < 0
        else [entry for entry in caught_up.entries if entry.seq > after_seq]
    )
    for entry in missed:
        yield frame(entry)
    offset = caught_up.offset
    silent = 0.0
    while True:
        arrived = journal.tail(offset)
        offset = arrived.offset
        for entry in arrived.entries:
            yield frame(entry)
        silent = 0.0 if arrived.entries else silent + interval
        if silent >= heartbeat:
            yield ": keep-alive\n\n"
            silent = 0.0
        await asyncio.sleep(interval)
