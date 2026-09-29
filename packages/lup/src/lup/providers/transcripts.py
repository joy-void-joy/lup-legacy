"""A runtime's transcript, read by whoever does not know which runtime wrote it.

A roster row names the file its session's runtime writes and never the
runtime's vocabulary, so a reader folding what a session is doing hands each
line to every runtime's own reader and takes the one answer that comes back:
the two write records neither mistakes for its own. Asked of the adapters
rather than known here, as :mod:`lup.providers.identity` asks them who a
session is.
"""

from pathlib import Path

from lup.observability.native import NativeTurn, NativeTurns
from lup.providers.claude.transcripts import ClaudeTurns
from lup.providers.codex.transcripts import CodexTurns
from lup.types import JsonObject


def native_readers() -> list[NativeTurns]:
    """Every runtime's reader of one transcript line."""
    return [ClaudeTurns(), CodexTurns()]


def native_turn(record: JsonObject) -> NativeTurn | None:
    """One transcript line as the message it carries, whichever runtime wrote it."""
    return next(
        (
            turn
            for reader in native_readers()
            if (turn := reader.turn(record)) is not None
        ),
        None,
    )


def subagent_transcript(transcript: Path, agent: str) -> Path | None:
    """Where the subagent *agent* of the session *transcript* records is kept, if anywhere."""
    return next(
        (
            kept
            for reader in native_readers()
            if (kept := reader.subagent(transcript, agent)) is not None
        ),
        None,
    )
