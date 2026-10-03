"""Feedback state: session loading, outcome matching, and analysis marks.

This is a TEMPLATE script. Run ``/lup:init`` to customize it for your domain.
The data shapes live in ``models``, aggregation in ``metrics``, git commits
in ``commits``, and the CLI command bodies in ``reports``.
"""

import json
import logging
from datetime import datetime
from pathlib import Path

import typer
from pydantic import ValidationError

from lup.channels.models import aware
from lup.types import JsonValue
from lup.workspace.history import iter_session_dirs, list_all_session_ids
from lup.workspace.paths import feedback_path
from lup.devtools.feedback.models import LoadedSession, SessionResult

logger = logging.getLogger(__name__)


def load_sessions(
    since: datetime | None = None, version: str | None = None
) -> list[LoadedSession]:
    """Load session data, optionally filtered by version."""
    sessions: list[LoadedSession] = []

    for session_dir in iter_session_dirs(version=version):
        session_files = sorted(session_dir.glob("*.json"), reverse=True)
        if not session_files:
            continue

        try:
            raw = json.loads(session_files[0].read_text())
            data = LoadedSession.model_validate(raw)
            data.source_session_id = session_dir.name
            data.source_file = str(session_files[0])

            if since and data.timestamp:
                if aware(datetime.fromisoformat(data.timestamp)) < aware(since):
                    continue

            sessions.append(data)
        except (json.JSONDecodeError, OSError, ValidationError) as e:
            logger.warning("Failed to load session %s: %s", session_dir.name, e)

    return sessions


# lup: template: implement your domain's outcome loading (customization step 9).
def load_outcomes() -> dict[str, JsonValue]:
    """Load outcome data for sessions.

    This stub raises so callers can tell "not implemented" from
    "implemented, no outcomes yet" instead of silently aggregating
    nothing.
    """
    raise NotImplementedError(
        "load_outcomes() is a template stub — implement it for your domain "
        "(CLAUDE.md customization step 9)"
    )


def match_outcomes(
    sessions: list[LoadedSession],
) -> list[SessionResult]:
    """Match sessions to their outcomes/feedback.

    A stub ``load_outcomes`` (NotImplementedError) degrades to no
    outcomes with a visible warning rather than failing collection.
    """
    try:
        outcomes = load_outcomes()
    except NotImplementedError as e:
        typer.echo(f"note: collecting without outcomes — {e}", err=True)
        outcomes = {}

    return [
        SessionResult(
            session_id=session.source_session_id,
            timestamp=session.timestamp,
            agent_sdk=session.agent_sdk,
            # Keyed by whatever ids the domain's outcome source uses.
            outcome=outcomes.get(session.source_session_id),
            metrics=session.tool_metrics,
        )
        for session in sessions
    ]


def load_sessions_for_versions(
    versions: list[str] | None,
) -> list[LoadedSession]:
    """Load sessions for a resolved version list (None = all)."""
    if versions is None:
        return load_sessions()
    return [s for v in versions for s in load_sessions(version=v)]


def collect_session_ids(
    effective: list[str] | None,
) -> list[str]:
    """Collect all session IDs for the given version list (None = all), deduplicated."""
    if not effective:
        return list_all_session_ids()
    return list(
        dict.fromkeys(
            session_id
            for v in effective
            for session_id in list_all_session_ids(version=v)
        )
    )


# =============================================================================
# ANALYSIS STATE TRACKING
# =============================================================================


def analyzed_file() -> Path:
    """Return path to the analyzed sessions tracking file."""
    return feedback_path() / "analyzed.json"


def load_analyzed() -> list[str]:
    """Load the already-analyzed session IDs (sorted on save, deduplicated)."""
    path = analyzed_file()
    if not path.exists():
        return []
    match json.loads(path.read_text()):
        case {"analyzed": list(ids)}:
            return list(dict.fromkeys(ids))
    return []


def save_analyzed(session_ids: list[str]) -> None:
    """Save the analyzed session IDs, sorted and deduplicated."""
    feedback_path().mkdir(parents=True, exist_ok=True)
    analyzed_file().write_text(
        json.dumps({"analyzed": sorted(dict.fromkeys(session_ids))}, indent=2) + "\n"
    )
