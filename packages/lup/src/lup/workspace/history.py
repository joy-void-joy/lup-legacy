"""Session history storage, retrieval, and cross-version data discovery.

This module handles:
1. Saving session results to notes/traces/<version>/sessions/
2. Loading past sessions for context or analysis (across versions)
3. Tracking session metadata (submitted, outcome, etc.)
4. Cross-version iteration over sessions and trace logs
5. Version scope resolution with progressive semver fallback

The feedback loop scripts read from this storage.

All functions accept :class:`pydantic.BaseModel` instances and work
with raw JSON dicts — no dependency on domain-specific models.

Examples:
    Save and load session results::

        >>> from pydantic import BaseModel
        >>> class MyResult(BaseModel):
        ...     summary: str
        ...     confidence: float
        >>> path = save_session(MyResult(summary="done", confidence=0.9), session_id="s1")
        >>> path.exists()
        True

    Load past sessions for analysis::

        >>> sessions = load_session_records("s1")
        >>> len(sessions)
        1
        >>> sessions[0].output["summary"]
        'done'

    Iterate sessions across versions::

        >>> for session_dir in iter_session_dirs(session_id="my-session"):
        ...     print(session_dir)
        PosixPath('.../notes/traces/0.1.0/sessions/my-session')

    Resolve version scope with progressive fallback::

        >>> resolve_version("0.1.0").versions
        ['0.1.0']
"""

import json
import logging
from collections.abc import Callable, Iterator, Sequence
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, Field, SerializeAsAny, ValidationError
from semver import Version as Semver

from lup.types import JsonObject, JsonValue, Usage
from lup.observability.metrics import MetricsSummary
from lup.observability.sessions import Session, SessionRecorder
from lup.workspace.paths import (
    TIMESTAMP_FMT,
    agent_version,
    harness_runs_path,
    parse_timestamp,
    sessions_dir,
    traces_path,
)

logger = logging.getLogger(__name__)


class SessionResult[OutputT: BaseModel](BaseModel):
    """Complete result of an agent session.

    Generic over the output type so domain-specific agent output
    models can be used without modifying this module.

    This captures everything needed for the feedback loop:
    - The structured output
    - Metadata (timing, cost, token usage)
    - Tool metrics for analysis
    """

    session_id: str
    task_id: str | None = Field(default=None, description="Domain-specific task ID")
    agent_version: str = Field(
        default="", description="Agent version that produced this result"
    )
    agent_sdk: str | None = Field(
        default=None,
        description="Provider adapter that ran the session; "
        "None on results predating the stamp",
    )
    sdk_session_id: str | None = Field(
        default=None,
        description="Provider-native session id — pass it as SessionId to "
        "an agent's open(resume=...); None when the provider reported none",
    )
    timestamp: str
    output: OutputT
    reasoning: str = Field(default="", description="Raw reasoning text")
    sources_consulted: list[str] = []
    duration_seconds: float | None = None
    cost_usd: float | None = None
    token_usage: SerializeAsAny[Usage] | None = None
    tool_metrics: MetricsSummary | None = None
    outcome: str | None = Field(default=None, description="Outcome after resolution")


class SessionRecord(BaseModel, extra="allow"):
    """A session result read back from disk, tolerant of domain variation.

    The read-side counterpart of :class:`SessionResult`: every core field
    is defaulted (a record on disk may lack it), ``output`` stays raw JSON
    because the domain's output model is not known at read time, and
    fields a domain adds to its result model survive via ``extra="allow"``.
    """

    session_id: str = ""
    task_id: str | None = None
    agent_version: str = ""
    agent_sdk: str | None = None
    sdk_session_id: str | None = None
    timestamp: str = ""
    output: JsonObject = {}
    reasoning: str = ""
    sources_consulted: list[str] = []
    duration_seconds: float | None = None
    cost_usd: float | None = None
    token_usage: Usage | None = None
    tool_metrics: MetricsSummary | None = None
    outcome: JsonValue = None


def save_session(
    result: BaseModel,  # lup: ignore[bare-basemodel] — any domain's result model
    *,
    session_id: str,
    recorder: SessionRecorder | None = None,
    session: Session | None = None,
) -> Path:
    """Save a session result to disk.

    This is the writer that produces a run's result document, so it is
    where the output is recorded: handed a ``recorder``, it records one
    :class:`~lup.observability.sessions.Output` pointing at the file, an
    ``OutputOf`` the ``session`` node where the caller has one. Handed none it records
    nothing and saves the result all the same. The scaffold's ``run_agent``
    wires both from the build that opened the session.

    Args:
        result: Any Pydantic model representing a session result.
        session_id: Unique session identifier.
        recorder: Where to record the output as a pointer, or nothing.
        session: The recorded session that produced it, where known.

    Returns:
        Path to the saved file.
    """
    session_dir = sessions_dir() / session_id
    session_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime(TIMESTAMP_FMT)
    filepath = session_dir / f"{timestamp}.json"

    filepath.write_text(result.model_dump_json(indent=2), encoding="utf-8")
    logger.info("Saved session %s to %s", session_id, filepath)
    if recorder is not None:
        recorder.produced(filepath, session)

    return filepath


def session_backend(session_dir: Path) -> str | None:
    """Read the ``agent_sdk`` stamp from a session dir's newest result JSON.

    Returns None when no result JSON exists or none carries the stamp
    (sessions predating it) — display code renders that as unknown
    rather than guessing a backend.
    """
    for filepath in sorted(session_dir.glob("[0-9]*.json"), reverse=True):
        try:
            data = json.loads(filepath.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        try:
            record = SessionRecord.model_validate(data)
        except ValidationError:
            continue
        if record.agent_sdk:
            return record.agent_sdk
    return None


def load_session_records(session_id: str) -> list[SessionRecord]:
    """Load all session records for a given ID across all versions.

    Core fields are typed via :class:`SessionRecord`; domain-added
    fields ride along as extras, so this function still has no
    dependency on domain-specific model classes.

    Args:
        session_id: The session identifier.

    Returns:
        List of session records, sorted by timestamp (oldest first).
    """
    sessions: list[SessionRecord] = []

    for session_dir in iter_session_dirs(session_id=session_id):
        # Only timestamp-named files are session records (save_session's
        # format); sibling artifacts like review.json share the directory.
        for filepath in sorted(session_dir.glob("[0-9]*.json")):
            try:
                data = json.loads(filepath.read_text(encoding="utf-8"))
                sessions.append(SessionRecord.model_validate(data))
            except (json.JSONDecodeError, OSError, ValidationError) as e:
                logger.warning("Failed to load session from %s: %s", filepath, e)

    sessions.sort(key=lambda s: s.timestamp)
    return sessions


def latest_session_record(session_id: str) -> SessionRecord | None:
    """Get the most recent session record for an ID.

    Args:
        session_id: The session identifier.

    Returns:
        The most recent session record, or None if no sessions exist.
    """
    sessions = load_session_records(session_id)
    return sessions[-1] if sessions else None


def update_session_metadata(
    session_id: str,
    *,
    outcome: str | None = None,
    submitted_at: str | None = None,
) -> bool:
    """Update metadata for the latest session.

    Args:
        session_id: The session identifier.
        outcome: Outcome value to set (e.g., "success", "failure").
        submitted_at: ISO timestamp when submitted.

    Returns:
        True if a session was updated, False if not found.
    """
    all_files = [
        filepath
        for session_dir in iter_session_dirs(session_id=session_id)
        for filepath in session_dir.glob("*.json")
    ]

    if not all_files:
        return False

    def file_timestamp(path: Path) -> datetime:
        """Sort key: parsed filename timestamp (non-timestamped names sort first)."""
        try:
            return parse_timestamp(path.name)
        except ValueError:
            return datetime.min

    # Latest by parsed timestamp — a lexicographic full-path sort would
    # order version directories wrong (0.10.0 < 0.9.0).
    latest_file = max(all_files, key=file_timestamp)

    try:
        data: JsonObject = json.loads(latest_file.read_text(encoding="utf-8"))

        if outcome is not None:
            data["outcome"] = outcome
        if submitted_at is not None:
            data["submitted_at"] = submitted_at

        latest_file.write_text(json.dumps(data, indent=2), encoding="utf-8")
        logger.info("Updated metadata for session %s", session_id)
        return True

    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Failed to update session %s: %s", session_id, e)
        return False


# -- Cross-version data discovery ---------------------------------------------


def trace_roots(roots: Sequence[Path] | None) -> Sequence[Path]:
    """The trace trees to walk: the ones named, or the one this process resolved."""
    return (traces_path(),) if roots is None else roots


def version_dirs(roots: Sequence[Path] | None = None) -> list[Path]:
    """Return all version directories under the trace roots given, sorted.

    Omitting ``roots`` takes the one this process resolved, which is the only
    answer available to a caller that names none, and stays the default.
    Naming them replaces that set rather than extending it, for the reason
    :func:`iter_run_dirs` spells out: a caller able to add a root but not to
    decline one has had a choice made for it out of a value only this package
    can see.
    """
    return sorted(
        directory
        for root in trace_roots(roots)
        for directory in (root.iterdir() if root.is_dir() else ())
        if directory.is_dir() and not directory.name.startswith(".")
    )


def iter_session_dirs(
    session_id: str | None = None,
    version: str | None = None,
    roots: Sequence[Path] | None = None,
) -> Iterator[Path]:
    """Iterate over session directories across all (or filtered) versions.

    Yields paths like: notes/traces/0.1.0/sessions/my-session/

    ``roots`` names the trace trees to walk, defaulting to the one this
    process resolved. It exists because that one moves: an adopter may
    relocate the notes root so part of a session writes into a directory of
    its own, and a lookup for a session another process recorded then searches
    inside this one and comes back empty — not because the record is gone but
    because one tree was searched for something kept in another. ``roots`` is
    how a caller that knows of a second tree says so.

    A directory reached through two roots is yielded once, under the spelling
    the earliest root produces. Where a caller counts what comes back to decide
    whether one id names one session, repetition would read as ambiguity and
    refuse the session it did find.
    """

    def found() -> Iterator[Path]:
        for ver_dir in (
            [root / version for root in trace_roots(roots)]
            if version
            else version_dirs(roots)
        ):
            sessions_base = ver_dir / "sessions"
            if not sessions_base.is_dir():
                continue
            if session_id is not None:
                candidate = sessions_base / session_id
                if candidate.is_dir():
                    yield candidate
            else:
                yield from (d for d in sessions_base.iterdir() if d.is_dir())

    unique = {directory.resolve(): directory for directory in reversed(list(found()))}
    yield from reversed(unique.values())


def run_transcript(run_directory: Path) -> Path:
    """The hash-chained observable journal a launch writes in its run directory.

    Named here beside the layout it sits in, so the launcher writing it and
    every reader looking for it spell it once.
    """
    return run_directory / "observable.jsonl"


def iter_run_dirs(
    run_id: str | None = None,
    roots: Sequence[Path] | None = None,
) -> Iterator[Path]:
    """Iterate over harness run directories, in the roots given or the default.

    Yields paths like: notes/harness/claude/20260825_023457_898418_claude_15f67aad/

    A launch writes ``<root>/<provider>/<run_id>/`` and a mode may name the
    root, so a run id alone does not say which root holds it. The provider
    level is searched rather than assumed, because the same run id reaches a
    reader from whichever runtime opened it.

    Which roots hold runs is the adopter's, not this package's: ``roots``
    replaces the set rather than extending it, and omitting it takes the
    default. Prepending the default to whatever was passed would have read as
    a courtesy and behaved as a rule — an adopter could add a root but never
    decline one, which is a choice made for every adopter out of a value only
    this package can see.

    This exists because the tree above is written here and would otherwise be
    readable nowhere: every caller wanting a run back from its id re-deriving
    the ``<provider>/<run_id>`` shape makes a layout this package owns into
    something it cannot change without breaking readers it cannot see.

    A directory reached twice is still one directory. The root set is the
    adopter's, so two of its entries can nest, name the same tree by different
    spellings, or arrive through a symlink — and a caller counting what comes
    back to decide whether one id names one run reads that repetition as
    ambiguity, and refuses the run it did find. Identity is the resolved path,
    which is what collapses those three cases into the one they always were.
    The spelling a caller gets back is the one the roots it passed produce,
    so the earliest is kept: mapping the reverse and reversing the result is
    last-wins standing in for the first-wins a dict cannot express directly.
    """

    def found() -> Iterator[Path]:
        for root in (harness_runs_path(),) if roots is None else roots:
            if not root.is_dir():
                continue
            for provider in sorted(root.iterdir()):
                if not provider.is_dir():
                    continue
                runs = (
                    sorted(run for run in provider.iterdir() if run.is_dir())
                    if run_id is None
                    else [provider / run_id]
                )
                yield from (run for run in runs if run.is_dir())

    unique = {run.resolve(): run for run in reversed(list(found()))}
    yield from reversed(unique.values())


def iter_trace_log_files(
    session_id: str | None = None,
    version: str | None = None,
) -> Iterator[Path]:
    """Iterate reasoning log files across all (or filtered) versions."""
    ver_dirs = [traces_path() / version] if version else version_dirs()

    for ver_dir in ver_dirs:
        logs_base = ver_dir / "logs"
        if not logs_base.exists():
            continue
        if session_id is not None:
            session_logs = logs_base / session_id
            if session_logs.exists():
                yield from session_logs.glob("*.md")
        else:
            yield from logs_base.rglob("*.md")

    if version is not None or not traces_path().exists():
        return
    for session_logs in traces_path().iterdir():
        if not session_logs.is_dir():
            continue
        if session_id is not None and session_logs.name != session_id:
            continue
        yield from session_logs.glob("*.md")


def iter_trace_event_files(
    session_id: str | None = None,
    version: str | None = None,
) -> Iterator[Path]:
    """Iterate browser event logs across all (or filtered) versions."""
    ver_dirs = [traces_path() / version] if version else version_dirs()
    for ver_dir in ver_dirs:
        logs_base = ver_dir / "logs"
        match session_id:
            case str() as session:
                candidate = logs_base / session / "events.json"
                if candidate.is_file():
                    yield candidate
            case _:
                if logs_base.exists():
                    yield from logs_base.glob("*/events.json")


def list_all_session_ids(version: str | None = None) -> list[str]:
    """Return all session IDs across versions, deduplicated."""
    return sorted({d.name for d in iter_session_dirs(version=version)})


# -- Version scope resolution ------------------------------------------------

MIN_VERSION_DATAPOINTS = 10


def parse_semver(version: str) -> Semver | None:
    """Parse an X.Y.Z version, or None where the label is not one.

    Version directory names are semver, so semver's own parser reads them:
    it orders two versions the way the specification says to, and it reads
    the ``+build`` suffix an experiment arm appends to its trace directory
    while still ordering that arm with the release it came from.
    """
    try:
        return Semver.parse(version)
    except ValueError:
        return None


class VersionScope(BaseModel):
    """A resolved version scope: the versions to include (None = all), and the
    widening warning to show, if the requested scope was too sparse."""

    versions: list[str] | None
    warning: str | None


def count_sessions_for_versions(versions: list[str]) -> int:
    """Count total session directories across a set of version directories."""
    return sum(sum(1 for _ in iter_session_dirs(version=v)) for v in versions)


def resolve_version(
    version: str | None,
    all_versions: bool = False,
    min_datapoints: int = MIN_VERSION_DATAPOINTS,
    count: Callable[[list[str]], int] = count_sessions_for_versions,
    datapoint: str = "sessions",
) -> VersionScope:
    """Resolve effective version scope with progressive semver fallback.

    Fallback chain: exact version → X.Y.* → X.* → all versions.
    Widens when the narrower scope has fewer than ``min_datapoints`` of
    whatever ``count`` counts — sessions here, but a project that keeps a
    denser record than its sessions passes its own counter and the word for
    it, rather than restating the whole fallback around a different tally.

    Widening pools releases only. A directory carrying build metadata is an
    experiment arm of its release rather than the release, so pooling it in
    would put a variant under test into the released version's numbers —
    naming that arm exactly is the way to ask for it.

    Returns ``(version_list, warning_message)``.
    ``version_list`` is ``None`` when all versions should be included.
    """
    if all_versions:
        return VersionScope(versions=None, warning=None)

    effective = version if version is not None else agent_version()
    semver = parse_semver(effective)
    available = [d.name for d in version_dirs()]

    # Level 1: exact version
    exact = [effective] if effective in available else []
    exact_count = count(exact)
    if exact_count >= min_datapoints:
        return VersionScope(versions=exact, warning=None)

    if semver is None:
        all_count = count(available)
        if all_count == 0:
            return VersionScope(versions=None, warning=None)
        return VersionScope(
            versions=None,
            warning=f"v{effective} has only {exact_count} {datapoint} "
            f"(need {min_datapoints}) — including all versions",
        )

    major, minor = semver.major, semver.minor

    # Level 2: same minor (X.Y.*)
    minor_matches = [
        v
        for v in available
        if (sv := parse_semver(v)) is not None
        and sv.build is None
        and sv.major == major
        and sv.minor == minor
    ]
    minor_count = count(minor_matches)
    if minor_count >= min_datapoints:
        return VersionScope(
            versions=minor_matches,
            warning=f"v{effective} has only {exact_count} {datapoint} "
            f"— widening to v{major}.{minor}.* ({minor_count} {datapoint})",
        )

    # Level 3: same major (X.*)
    major_matches = [
        v
        for v in available
        if (sv := parse_semver(v)) is not None
        and sv.build is None
        and sv.major == major
    ]
    major_count = count(major_matches)
    if major_count >= min_datapoints:
        return VersionScope(
            versions=major_matches,
            warning=f"v{major}.{minor}.* has only {minor_count} {datapoint} "
            f"— widening to v{major}.* ({major_count} {datapoint})",
        )

    # Level 4: all versions
    all_count = count(available)
    if all_count == 0:
        return VersionScope(versions=None, warning=None)
    return VersionScope(
        versions=None,
        warning=f"v{major}.* has only {major_count} {datapoint} "
        f"(need {min_datapoints}) — including all versions",
    )
