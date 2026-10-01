"""An append-only log with per-item commit offsets.

A stream is what a slot is not: it never settles, it never blocks anything,
and reading it twice from the same offset yields the same items. That
distinction is load-bearing wherever a run parks on outstanding decisions —
with messages in a stream and decisions in slots, "a message parked the run"
stops being expressible rather than merely being avoided.

Reads return complete lines only, so a reader never sees a half-written
record, and each item carries the offset that consumes exactly it. Applying
an item and then committing its offset means a crash between two items
replays the unapplied one instead of dropping it.

A malformed line is skipped rather than fatal: one bad record must not
poison a log that a live process is still appending to.
"""

import logging
import os
from itertools import count
from pathlib import Path

from pydantic import TypeAdapter, ValidationError

from lup.channels.models import ChannelOverflowError, Offset

logger = logging.getLogger(__name__)


TAIL_WINDOW_BYTES = 256 * 1024
"""How far back to look for a log's last complete record.

Large enough to contain one, small enough that reading it is free. Our
judgement about record sizes, so a caller with bigger records raises it.
"""


class Stream[T]:
    """One ordered log of records, readable from any offset.

    The record type is a ``TypeAdapter`` rather than a model class, because
    a log's items are often a closed union — one stream of several event
    shapes — and a union is exactly what a bare ``type[T]`` cannot name.
    """

    def __init__(
        self,
        path: Path,
        adapter: TypeAdapter[T],
        max_bytes: int | None = None,
        durable: bool = False,
    ) -> None:
        self.path = path
        self.adapter = adapter
        self.max_bytes = max_bytes
        self.durable = durable

    def append(self, record: T) -> int:
        """Append one record and return the offset that consumes it.

        The optional ceiling is backpressure against a looping writer, not a
        retention policy. A journal must never refuse to record, so it
        declares no ceiling at all.
        """
        if (
            self.max_bytes is not None
            and self.path.exists()
            and self.path.stat().st_size >= self.max_bytes
        ):
            raise ChannelOverflowError(
                f"{self.path.name} reached {self.max_bytes} bytes; nobody is "
                "consuming it. Stop appending and end the turn."
            )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = self.adapter.dump_json(record).decode("utf-8") + "\n"
        with self.path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(line)
            if self.durable:
                # Only a record whose absence would be read as evidence pays
                # for the round trip. A transcript that claims to be complete
                # must not lose its tail to a crash; an ordinary channel is
                # replayed from its writer's own state and stays cheap.
                handle.flush()
                os.fsync(handle.fileno())
        return self.path.stat().st_size

    def read_from(self, offset: int) -> list[Offset[T]]:
        """Every complete record after ``offset``, each with its commit offset."""
        if not self.path.exists():
            return []
        with self.path.open("rb") as handle:
            handle.seek(offset)
            data = handle.read()
        end = data.rfind(b"\n")
        if end < 0:
            return []
        region = data[: end + 1]
        region_end = offset + len(region)

        found: list[Offset[T]] = []
        consumed = offset
        for raw in region.splitlines(keepends=True):
            consumed += len(raw)
            line = raw.strip()
            if not line:
                continue
            try:
                found.append(
                    Offset(
                        item=self.adapter.validate_json(line),
                        commit_offset=consumed,
                    )
                )
            except ValidationError:
                logger.exception("Skipping malformed record in %s: %r", self.path, line)
        if found:
            # The final item's offset covers the whole complete region, so
            # committing it also consumes any malformed or blank lines that
            # followed the last record this reader could parse.
            found[-1] = found[-1].model_copy(update={"commit_offset": region_end})
        return found

    def read_all(self) -> list[T]:
        return [pair.item for pair in self.read_from(0)]

    def last(self, window: int = TAIL_WINDOW_BYTES) -> T | None:
        """The most recent complete record, without reading the whole log.

        "What happened most recently?" is the cheapest question a log is
        asked and the one a status view asks every time, so it must not
        scale with the log. A resolver journal reaches tens of megabytes in
        a single run, and parsing all of it to read its last line is the
        difference between a status command somebody runs and one they
        avoid.

        Reading backwards from a bounded window means the earliest line in
        it may be a fragment, which is why a record that will not decode is
        passed over rather than reported: the frontier is expected to be
        cut. A caller wanting every record still uses `read_from`, which
        reports what it could not decode.

        The window is where the search starts, not where it stops. A record
        longer than the window leaves nothing in it that decodes, and
        answering None there tells a chained writer the log is empty — so it
        restarts the sequence at zero mid-file, and the chain breaks with
        nothing having been tampered with. The window doubles until a record
        decodes or it covers the whole file, which only a log holding no
        complete record at all answers with None.
        """
        if not self.path.exists():
            return None
        size = self.path.stat().st_size
        with self.path.open("rb") as handle:
            for doubling in count():
                start = max(0, size - (max(window, 1) << doubling))
                handle.seek(start)
                for raw in reversed(handle.read(size - start).splitlines()):
                    line = raw.strip()
                    if not line:
                        continue
                    try:
                        return self.adapter.validate_json(line)
                    except ValidationError:
                        logger.debug("Incomplete record at the tail of %s", self.path)
                if start == 0:
                    return None
        return None
