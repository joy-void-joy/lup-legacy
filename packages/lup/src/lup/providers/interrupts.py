"""Stopping a session's running turn, by the runtime its row names.

A Claude turn is stopped by the wake itself, whose frame carries the
runtime's own priority, so nothing here reaches Claude. A Codex turn is
stopped apart from the message that interrupts it, through the app-server the
session's configuration home runs; this asks the adapter that speaks to it,
so a reader holding a row stops a turn without knowing which runtime it holds.
"""

from pathlib import Path

from pydantic import BaseModel


class Interrupted(BaseModel, frozen=True):
    """What came of asking a session's running turn to stop."""

    interrupted: bool
    """Whether a turn was running and its runtime stopped it."""

    turn: str = ""
    reason: str = ""
    """Why nothing was stopped, empty where a turn was."""


async def interrupted_turn(
    runtime: str, home: Path, thread: str, timeout: float = 10.0
) -> Interrupted:
    """Stop the turn *thread* is running in *runtime*, reached through *home*.

    Never raises: a runtime with no turn interrupt to drive, an app-server
    not listening, or nothing running each come back as the reason.
    """
    match runtime:
        case "codex":
            from lup.providers.codex.interrupt import codex_interrupted_turn

            return await codex_interrupted_turn(home, thread, timeout)
        case _:
            return Interrupted(
                interrupted=False,
                reason=f"{runtime or 'its runtime'} has no turn interrupt apart from its wake",
            )
