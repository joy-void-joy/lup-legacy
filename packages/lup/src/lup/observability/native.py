"""Live ingestion of observable native CLI transcripts.

Claude Code and Codex own their interactive terminal UI, so Lup cannot wrap
their provider transport the way it wraps an SDK session. Both persist JSONL
session events, which is the seam: this watcher follows only bytes written
after launch and mirrors them into the same durable journal an SDK run writes,
so one transcript covers both ways of running an agent.

What a runtime writes, and where, is the runtime's own business. This module
holds the reading — polling, cursors, partial lines, scope — and asks a
:class:`NativeTranscripts` for everything vendor-shaped: where its sessions
live, how one of its records names the directory it belongs to, and what its
own words for a reasoning or tool block are. A merged table here would read
one vendor's records with the other's vocabulary, and would have to be edited
rather than extended when a third runtime arrives.

One watcher follows one runtime. A run that drives both starts two, which is
why nothing here has to decide whose record it is holding.
"""

import logging
import threading
from abc import ABC, abstractmethod
from collections.abc import Iterator, Mapping
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, TypeAdapter, ValidationError

from lup.observability.audit import ObservableEventKind, TraceJournal
from lup.sessions.events import TurnMessage
from lup.types import JsonObject, JsonValue

logger = logging.getLogger(__name__)

JSON_OBJECT = TypeAdapter(JsonObject)

type SessionScope = Literal["claimed", "foreign"]
"""Which launch a session was decided to belong to, once it has been decided.

One mapping rather than a set per verdict, because the verdicts are exclusive:
two sets can hold the same session at once and nothing in the type says which
reading wins, while a session that is absent here has simply not identified
itself yet."""


class NativeTurn(BaseModel, frozen=True):
    """One transcript record read as the portable message it carries, and when."""

    message: TurnMessage
    at: datetime | None = None


class NativeSemanticBlock(BaseModel, frozen=True):
    """One semantic index projected from an exact vendor event."""

    kind: ObservableEventKind
    block: JsonObject


class NativeRecordOrigin(BaseModel, frozen=True):
    """What one record says about the run it belongs to.

    The directory and the session are one question with two facets, asked and
    answered together: a record's directory describes that record, a session
    outlives both the file holding it and the directory it started in, and
    deciding scope needs them at the same moment. Either may be absent, which
    keeps a transcript out of a scoped run until some record identifies it --
    the safe direction, since guessing is how another project's session lands
    in this project's record.
    """

    directory: Path | None = None
    session: str | None = None


def blocks_by_type(
    record: JsonValue, spellings: Mapping[str, ObservableEventKind]
) -> list[NativeSemanticBlock]:
    """Every block in one record whose own ``type`` a runtime declares.

    Searched by descent rather than at a known path, because a runtime nests
    the same block under different envelopes and moves them between versions.
    The block's own ``type`` is what both keep stable, so an implementation
    supplies the words and this supplies the walk.
    """

    def walk(node: JsonValue) -> Iterator[NativeSemanticBlock]:
        match node:
            case dict() as mapping:
                native_type = mapping.get("type")  # lup: ignore[dict-get]
                if isinstance(native_type, str) and native_type in spellings:
                    yield NativeSemanticBlock(
                        kind=spellings[native_type], block=mapping
                    )
                for child in mapping.values():
                    yield from walk(child)
            case list() as items:
                for child in items:
                    yield from walk(child)
            case _:
                return

    return list(walk(record))


def first_string(record: JsonValue, key: str) -> str | None:
    """The first string found under ``key`` anywhere in one record.

    One runtime stamps the field at the top level and another carries it
    inside an opening metadata payload; a descent reaches both without either
    having to describe its envelope.
    """
    match record:
        case dict() as mapping:
            candidate = mapping.get(key)
            if isinstance(candidate, str):
                return candidate
            for child in mapping.values():
                if (found := first_string(child, key)) is not None:
                    return found
        case list() as items:
            for child in items:
                if (found := first_string(child, key)) is not None:
                    return found
        case _:
            return None
    return None


class NativeTranscripts(ABC):
    """What one runtime writes about a session, and how to read it.

    An engine the watcher is handed, never a surface a consumer holds: a
    caller wants a watcher, and the runtime it follows is a construction
    detail. Every method here is a place a vendor's own vocabulary would
    otherwise have leaked into provider-neutral code.
    """

    @abstractmethod
    def roots(self) -> list[Path]:
        """Directories under which this runtime persists session transcripts."""

    @abstractmethod
    def belongs_to(self, record: JsonObject) -> NativeRecordOrigin:
        """Which run this record says it came from, in whatever it stamps.

        One question rather than two, because both runtimes stamp a directory
        and a session identifier, each spells them its own way, and the scope
        decision needs whichever of them a given record happens to carry.
        """

    @abstractmethod
    def semantic_blocks(self, record: JsonObject) -> list[NativeSemanticBlock]:
        """The reasoning and tool blocks this record exposes, in its own words."""


class NativeTurns(ABC):
    """What a session is saying and doing, read line by line out of its runtime's transcript.

    Apart from :class:`NativeTranscripts`, which finds and mirrors a runtime's
    files wholesale: this starts from a file somebody already named — a
    roster row's — and answers for one line, or for where a subagent of it
    writes. Every method is a place a vendor's own vocabulary would
    otherwise have leaked into provider-neutral code.
    """

    @abstractmethod
    def turn(self, record: JsonObject) -> NativeTurn | None:
        """This record as the message it carries, or ``None`` for anything else.

        ``None`` for bookkeeping, for what the runtime injected rather than
        anybody said, and for a record another runtime wrote — which is what
        lets a reader that knows no runtime ask each in turn.
        """

    @abstractmethod
    def subagent(self, transcript: Path, agent: str) -> Path | None:
        """Where the subagent *agent* of the session *transcript* records is kept.

        ``None`` where this runtime kept none there, or it is not this
        runtime's session.
        """


class NativeTranscriptWatcher:
    """Follow new JSONL bytes under one runtime's session roots until stopped."""

    def __init__(
        self,
        transcripts: NativeTranscripts,
        journal: TraceJournal,
        *,
        scope: Path | None = None,
        poll_seconds: float = 0.25,
    ) -> None:
        self.transcripts = transcripts
        self.journal = journal
        self.scope = scope
        self.poll_seconds = poll_seconds
        self.cursors: dict[Path, int] = {}
        self.origins: dict[Path, Path] = {}
        self.sessions: dict[str, SessionScope] = {}
        self.claimed_directories: set[Path] = set()  # lup: ignore[set-shape]
        self.stop_signal = threading.Event()
        self.thread: threading.Thread | None = None

    def discover(self) -> list[Path]:
        """Find session JSONL files under every root this runtime declares."""
        return sorted(
            {
                path
                for root in self.transcripts.roots()
                if root.exists()
                for path in root.rglob("*.jsonl")
                if path.is_file()
            }
        )

    def snapshot(self) -> None:
        """Mark all pre-launch transcript bytes as historical.

        Taken before the first poll so a long-lived config root does not
        replay months of unrelated sessions into this run's journal.
        """
        self.cursors = {path: path.stat().st_size for path in self.discover()}

    def start(self) -> None:
        """Snapshot history and begin live background ingestion."""
        self.snapshot()
        self.thread = threading.Thread(
            target=self.watch,
            name="lup-native-transcript",
            daemon=True,
        )
        self.thread.start()

    def watch(self) -> None:
        """Poll native roots and ingest complete newly appended lines."""
        while not self.stop_signal.wait(self.poll_seconds):
            try:
                self.scan()
            except Exception as error:
                logger.exception("Native transcript ingestion failed")
                self.journal.emit("error", {"message": str(error)})

    def scan(self, *, final: bool = False) -> None:
        """Ingest all currently available complete records.

        One transcript's failure stays that transcript's. These roots carry
        every project's sessions and a CLI rotates its own files freely, so a
        transcript routinely disappears between being listed and being read;
        letting that end the pass would let an unrelated project's churn stop
        this one's recording.
        """
        for path in self.discover():
            try:
                self.follow(path, final=final)
            except OSError:
                logger.debug("Unreadable native transcript: %s", path, exc_info=True)
                self.cursors.pop(path, None)

    def follow(self, path: Path, *, final: bool = False) -> None:
        """Ingest the complete records one transcript appended since last seen.

        A partial trailing line is left for the next pass, since the CLI is
        still writing it — except on the final scan, where nothing more is
        coming and a truncated record is better evidence than none.
        """
        cursor = self.cursors.get(path, 0)
        size = path.stat().st_size
        if size < cursor:
            cursor = 0
        if size == cursor:
            return
        with path.open("rb") as stream:
            stream.seek(cursor)
            appended = stream.read()
        offset = 0
        for line in appended.splitlines(keepends=True):
            if not line.endswith((b"\n", b"\r")) and not final:
                break
            offset += len(line)
            self.record_line(path, line.removesuffix(b"\n").removesuffix(b"\r"))
        self.cursors[path] = cursor + offset

    def directory_in_scope(self, directory: Path) -> bool:
        """Report whether work done in one directory belongs to this launch.

        Containment covers the project tree. The claimed set additionally covers
        directories this launch has already been seen working in — sibling
        worktrees among them, which no containment test reaches because they are
        siblings of the project root rather than children of it.
        """
        if self.scope is None:
            return True
        return (
            directory.is_relative_to(self.scope)
            or directory in self.claimed_directories
        )

    def path_in_scope(self, path: Path) -> bool:
        """Report whether a transcript naming no directory yet is this launch's.

        Such a record inherits the decision already made for its file; a
        transcript that has never identified itself stays out until it does.
        """
        if self.scope is None:
            return True
        origin = self.origins.get(path)
        return origin is not None and self.directory_in_scope(origin)

    def admit(self, path: Path, origin: NativeRecordOrigin) -> bool:
        """Decide whether one record belongs to this launch, and remember why.

        Scope is decided per session, not per record. A native CLI keys a
        transcript to the directory a session started in while stamping the
        directory on every record, so the two diverge the moment the session
        changes directory: entering a worktree opens a fresh transcript under a
        different project root, and every record in it fails a containment test
        against the launching project. Recording stopped there, silently.

        A session claimed while working in scope therefore stays claimed
        wherever it goes next, and the directories a claim has been seen in are
        themselves claimed, so a session opened in a worktree this launch had
        already moved into is recognised too.

        The rule runs the other way with equal force: a session first seen in a
        directory this launch never visited is another launch's, and is never
        adopted however close it later comes. Without that, one concurrent
        session stepping into this project would hand over its whole remaining
        transcript -- which is the failure scoping exists to prevent, arriving
        by the door that following a session opens.
        """
        if self.scope is None:
            return True
        directory, session = origin.directory, origin.session
        if directory is not None:
            self.origins[path] = directory
        if session is not None and session in self.sessions:
            match self.sessions[session]:
                case "foreign":
                    return False
                case "claimed":
                    if directory is not None:
                        self.claimed_directories.add(directory)
                    return True
        if directory is None:
            return self.path_in_scope(path)
        decided: SessionScope = (
            "claimed" if self.directory_in_scope(directory) else "foreign"
        )
        if session is not None:
            self.sessions[session] = decided
        if decided == "foreign":
            return False
        self.claimed_directories.add(directory)
        return True

    def record_line(self, path: Path, encoded: bytes) -> None:
        """Persist one vendor event and semantic indexes for exposed blocks."""
        if not encoded:
            return
        source = str(path)
        try:
            record = JSON_OBJECT.validate_json(encoded)
        except ValidationError:
            if self.path_in_scope(path):
                self.journal.emit(
                    "message",
                    {
                        "native_source": source,
                        "raw": encoded.decode("utf-8", errors="replace"),
                    },
                )
            return
        origin = self.transcripts.belongs_to(record)
        if not self.admit(path, origin):
            return
        self.journal.emit(
            "message",
            {"native_source": source, "native_event": record},
            session_id=origin.session,
        )
        for semantic in self.transcripts.semantic_blocks(record):
            self.journal.emit(
                semantic.kind,
                {"native_source": source, "block": semantic.block},
                session_id=origin.session,
            )

    def stop(self) -> None:
        """Stop the watcher and synchronously ingest the final partial record."""
        self.stop_signal.set()
        if self.thread is not None:
            self.thread.join()
        self.scan(final=True)
