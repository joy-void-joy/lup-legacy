"""Sessions and outputs as ledger nodes: pointers at what a run wrote, never the bytes.

A run leaves hundreds of megabytes under ``notes/`` — a session directory
per agent version, a trace journal, the result documents it produced, and
one ``observable.jsonl`` per interactive CLI launch — gitignored, per
checkout, and indexed by nothing. So a later session asking "what ran here,
and did it finish" has a tree to walk and no answer to read. This module
puts the answer in the ledger as two kinds that **point**: a ``Session`` is
the directory and journal a session wrote, pinned to the journal's digest
when it closed; an ``Output`` is one product of a run, pinned the way a
``File`` is. Neither carries a byte of what it points at — both refuse an
attachment outright — so the ledger stays a small log of typed records and
the session data stays where it was.

**Why the library's.** The writers this records at are the library's:
:func:`~lup.workspace.notes.setup_notes` opens the session directory,
:func:`~lup.workspace.history.save_session` writes the result, and the
harness launcher opens the observable journal. Every project built on this
library runs sessions through those writers and keeps the same ``notes/``
tree, and none of them would answer differently what a session is or where
its journal sits. What a project decides is whether to index them at all,
which it does by listing these kinds among its declared ones, the way it
lists :class:`~lup.ledger.files.File`: a writer handed no recorder records
nothing and works no differently. The kinds are declared here beside the
observability package rather than in ``lup.ledger`` because they are about
what happened in a run, which is this package's subject, and the ledger's
charter is to declare mechanism and files and nothing about what a run is.

**One checkout's record, read from another.** The ledger is per clone —
shared under the git directory — while ``notes/`` is per worktree, so a
record made in one checkout is read from every other. Each node therefore
carries ``checkout``, the absolute path of the tree it was recorded in, and
its paths relative to that: standing is read against ``checkout/path`` and
not against the reader's own tree, so a session recorded on branch A reads
``fresh`` from branch B while A's journal still holds the pinned bytes,
``stale`` once it grew, and ``missing`` once A's worktree is gone.

Recording starts when a recorder is wired; a tree written before that is
walked on demand by :mod:`lup.observability.sweep`, which records the same
two kinds through the same writers.
"""

import asyncio
import logging
from collections.abc import AsyncGenerator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Literal, Self

from pydantic import Field, field_validator, model_validator

from lup.channels.models import utc_now
from lup.coordination.refs import ActorRef
from lup.formats import digest
from lup.ledger.files import pinned
from lup.ledger.journal import LedgerStore
from lup.ledger.kinds import kind_of
from lup.ledger.models import LedgerEdge, LedgerNode, Standing, Surroundings
from lup.ledger.store import LedgerLayout
from lup.sessions.capabilities import SessionEngine, SessionWrapper
from lup.sessions.events import SessionId

logger = logging.getLogger(__name__)

type Outcome = Literal["", "completed", "failed", "interrupted"]
"""How a session ended, or nothing while it is open.

Closed, because a reader narrows a listing by it: ``completed`` is the
writer's own exit, ``failed`` an exception it did not expect, and
``interrupted`` a cancellation or a keyboard interrupt — the one a person
caused, which is worth telling apart from the one the code did.
"""


def pointers_only(what: str) -> str:
    """The refusal a session or output gives an attachment, in the writer's words."""
    return (
        f"a {what} points at its data and never carries it: pointers only, "
        "no bytes are attached to the ledger"
    )


def spelled_under(path: Path, checkout: Path) -> str:
    """*path* relative to *checkout* where it is under it, and as it is otherwise.

    A notes override may put a session's tree outside the checkout entirely,
    and a record of that session is still worth having: joined back onto the
    checkout, an absolute spelling reads as itself.
    """
    try:
        return path.relative_to(checkout).as_posix()
    except ValueError:
        return path.as_posix()


class Session(LedgerNode, frozen=True):
    """One agent session, pointing at the directory and journal it wrote under notes/.

    A pointer and nothing more: the directory, the journal, the checkout they
    are under, and when and how it ended. Recorded when the directory is
    opened and amended when the session closes, with the journal's digest
    pinned at that moment, so the record says whether the trace a reader is
    about to open is the one that was closed over.

    **No bytes.** A trace runs to megabytes and the ledger is a log of typed
    records, so this kind refuses an attachment: recording one with bytes is
    refused before anything reaches the blob store, and the refusal says
    pointers only.
    """

    kind: Literal["observability:session"] = "observability:session"

    title: str = ""
    """What it is, which for a session is its directory's name: left empty, that fills it."""

    runtime: str = Field(min_length=1)
    """The CLI or client that opened it — ``claude``, ``codex``, an engine name."""

    agent_version: str = ""
    started: datetime
    ended: datetime | None = None
    """When it closed, or nothing while it is open."""

    directory: str = Field(min_length=1)
    """The session's directory, relative to ``checkout``."""

    journal: str = ""
    """The trace journal the digest pins, relative to ``checkout``."""

    checkout: str = Field(min_length=1)
    """The absolute path of the worktree it was recorded in.

    Carried because ``notes/`` is per checkout while the ledger is per clone:
    a reader in another worktree resolves the paths through this, not
    through their own tree.
    """

    journal_digest: str = ""
    """The journal's content digest as it was when the session closed."""

    outcome: Outcome = ""

    @field_validator("attachments")
    @classmethod
    def unattached(cls, attachments: list[str]) -> list[str]:
        """A session carries no bytes; the refusal is the type's, not a gate's."""
        if attachments:
            raise ValueError(pointers_only("session"))
        return attachments

    @model_validator(mode="after")
    def titled(self) -> Self:
        """A session's directory names it, unless somebody said more."""
        if self.title:
            return self
        return self.model_copy(update={"title": Path(self.directory).name})

    def journal_path(self) -> Path:
        """Where the journal is, resolved through the checkout it was written in."""
        return Path(self.checkout) / self.journal

    def finished(self) -> bool:
        return self.ended is not None

    def closed(self, outcome: Outcome, at: datetime) -> Self:
        """This session ended, with the journal pinned as it is at this moment."""
        return self.model_copy(
            update={
                "ended": at,
                "outcome": outcome,
                "journal_digest": digest.file(self.journal_path()) or "",
            }
        )

    def standing(self, around: Surroundings) -> Standing:
        """Open until it ended; then fresh, stale or missing against the pinned journal.

        Unchecked where the reader has no tree at all, for the reason a file
        is: with nothing to read there are no grounds to say the journal moved.
        """
        if self.ended is None:
            return Standing(
                label="open", reason=f"{self.runtime} session opened {self.started}"
            )
        if not self.journal or not self.journal_digest:
            # Amending `ended` by hand closes the record without pinning
            # anything, and asking the tree about an empty path would answer
            # "missing" with nothing named — a diagnostic about the amendment
            # dressed as one about the journal.
            return Standing(
                label="unpinned",
                reason=(
                    f"ended {self.ended} with no journal digest pinned; "
                    "`closed()` pins the journal as it is, an amendment of "
                    "`ended` does not"
                ),
            )
        if around.root is None:
            return Standing(label="unchecked", reason="no working tree to read")
        return pinned(self.journal_path(), self.journal, self.journal_digest)


class Output(LedgerNode, frozen=True):
    """One product of a run — a result document — pointing at the file, pinned by digest.

    A :class:`~lup.ledger.files.File` composed with the run it came from:
    the same path and digest, read through the checkout it was written in
    rather than the reader's tree, plus the session that produced it and
    when. Composed rather than extended because a kind is a literal a
    record spells for itself, and one type cannot answer to two.

    **No bytes**, for the reason a session carries none: an attachment is
    refused before it reaches the blob store, pointers only.
    """

    kind: Literal["observability:output"] = "observability:output"

    title: str = ""
    """What it is, which for an output is its path: left empty, the path fills it."""

    path: str = Field(min_length=1)
    """Relative to ``checkout``, so the record reads from any worktree of the clone."""

    digest: str = ""
    """The content digest as recorded, pinned by `prepared` where a caller left it empty."""

    checkout: str = Field(min_length=1)
    """The absolute path of the worktree it was written in."""

    session: str = ""
    """The id of the session that produced it, empty where nothing knows."""

    produced: datetime

    @field_validator("attachments")
    @classmethod
    def unattached(cls, attachments: list[str]) -> list[str]:
        """An output carries no bytes; the refusal is the type's, not a gate's."""
        if attachments:
            raise ValueError(pointers_only("output"))
        return attachments

    @model_validator(mode="after")
    def titled(self) -> Self:
        """An output's path names it, unless somebody said more."""
        return self if self.title else self.model_copy(update={"title": self.path})

    def held_at(self) -> Path:
        """Where the file is, resolved through the checkout it was written in."""
        return Path(self.checkout) / self.path

    def prepared(self, root: Path) -> Self:
        """The digest pinned through the checkout, where none was recorded."""
        del root
        if self.digest:
            return self
        return self.model_copy(update={"digest": digest.file(self.held_at()) or ""})

    def standing(self, around: Surroundings) -> Standing:
        if around.root is None:
            return Standing(label="unchecked", reason="no working tree to read")
        return pinned(self.held_at(), self.path, self.digest)


class OutputOf(LedgerEdge, frozen=True):
    """An output pointing at the session that produced it.

    Drawn by the recorder wherever the session is known, so ``ledger show``
    on a session counts what it wrote and the explorer draws the line from
    each result to the run it came from. Declared here beside the two kinds
    it joins, because which run wrote a document is a fact about the run and
    not a project's epistemics; a project lists it among its edge kinds the
    way it lists the two node kinds.
    """

    kind: Literal["observability:output_of"] = "observability:output_of"


class SessionRecorder:
    """Records sessions and outputs into one store, and never lets a refusal reach the writer.

    A writer records as it goes — the directory opened, the session closed,
    the result written — and none of those must fail because the ledger
    did: a store nothing can write to, a placement whose tree is missing, a
    kind the type refuses. Every call here answers with the node or with
    nothing, and says why in the log, so the session goes on.

    An output is related to its session by :class:`OutputOf` wherever the
    session is known, and names it by id besides.
    """

    def __init__(self, store: LedgerStore) -> None:
        self.store = store

    @property
    def checkout(self) -> Path:
        """The tree whose ``notes/`` the records point into."""
        return self.store.project

    def opened(
        self,
        runtime: str,
        agent_version: str,
        directory: Path,
        journal: Path | None,
        started: datetime | None = None,
    ) -> Session | None:
        """One session's directory was opened; record it as open.

        ``journal`` is the trace the close will pin, or nothing where the run
        wrote none, and ``started`` is when it opened where the caller knows
        better than now — a sweep over directories already on disk does.
        """
        now = utc_now()
        try:
            return self.store.record(
                Session,
                "",
                at=now,
                runtime=runtime,
                agent_version=agent_version,
                started=(started or now).isoformat(),
                directory=spelled_under(directory, self.checkout),
                journal=""
                if journal is None
                else spelled_under(journal, self.checkout),
                checkout=str(self.checkout),
            )
        except Exception:
            logger.exception(
                "the ledger refused to record the session at %s; it goes on unrecorded",
                directory,
            )
            return None

    def closed(
        self, session: Session, outcome: Outcome, at: datetime | None = None
    ) -> Session | None:
        """The session ended; amend its record with the journal pinned now.

        ``at`` is when it ended, where the caller knows better than now.
        """
        try:
            return self.store.amend(session.closed(outcome, at or utc_now()))
        except Exception:
            logger.exception(
                "the ledger refused to close session %s; its record stays open",
                session.id,
            )
            return None

    def produced(
        self, path: Path, session: Session | None, at: datetime | None = None
    ) -> Output | None:
        """One product was written; record it, about its session where one is known.

        ``at`` is when it was produced, where the caller knows better than now.
        """
        try:
            output = self.store.record(
                Output,
                "",
                path=spelled_under(path, self.checkout),
                checkout=str(self.checkout),
                session=session.id if session is not None else "",
                produced=(at or utc_now()).isoformat(),
            )
            if session is not None:
                self.store.relate(OutputOf, output, session)
            return output
        except Exception:
            logger.exception(
                "the ledger refused to record the output at %s; the file stands",
                path,
            )
            return None


def session_recorder(
    root: Path,
    author: ActorRef,
    classes: list[type[LedgerNode]],
    layout: LedgerLayout = LedgerLayout(),
) -> SessionRecorder | None:
    """A recorder over this project's declared kinds, or nothing where they are not declared.

    Nothing rather than a recorder that would be refused on every call: a
    project that did not list ``Session`` and ``Output`` among its kinds has
    decided not to index its runs, and the answer is logged once here rather
    than at every writer.
    """
    declared = {kind_of(each) for each in classes}
    undeclared = [
        kind_of(each) for each in (Session, Output) if kind_of(each) not in declared
    ]
    if undeclared:
        logger.info(
            "sessions are not indexed: %s not among the declared ledger kinds",
            ", ".join(undeclared),
        )
        return None
    return SessionRecorder(LedgerStore(root, author, layout))


class CloseRecordingWrapper(SessionWrapper):
    """Amend the session's record when the session closes, however it closes.

    Wired by the composition that opened the session directory with a
    recorder — the scaffold's ``build_session_factory`` — outermost, so the
    close is recorded after every other wrapper has run. The outcome is read
    off how the session left: its own exit is ``completed``, a cancellation
    or keyboard interrupt is ``interrupted``, and anything else raised is
    ``failed``; the exception goes on to the caller either way.
    """

    def __init__(self, recorder: SessionRecorder, session: Session) -> None:
        self.recorder = recorder
        self.session = session

    def around(
        self,
        opened: AbstractAsyncContextManager[SessionEngine],
        resume: SessionId | None,
    ) -> AbstractAsyncContextManager[SessionEngine]:
        return self.recorded(opened)

    @asynccontextmanager
    async def recorded(
        self, opened: AbstractAsyncContextManager[SessionEngine]
    ) -> AsyncGenerator[SessionEngine]:
        outcome: Outcome = "failed"
        try:
            async with opened as inner:
                yield inner
            outcome = "completed"
        except (asyncio.CancelledError, KeyboardInterrupt):
            outcome = "interrupted"
            raise
        finally:
            self.recorder.closed(self.session, outcome)
