"""The operator's dashboard over the review queues and sessions of every repository it serves.

One loopback page reads every worktree of every repository it was given — the
operator's `--root`s, or every repository a launch held the dashboard for —
so a review parked by any session in any of them is one list away, grouped by
the repository and the session that asked, beside every session and what it
is doing. The page holds a capability carried in the URL fragment, checks
Host and Origin on every request, and answers a review only against the
fingerprint it displayed and the preimages still on disk. It never runs the
operation: an approval releases one exact retry of the call that asked.

Everything live reaches the page on one stream (:mod:`.stream`): every
waiting review and the most recently settled, each as its queue row, never
the documents it binds -- those are read when a review is opened, and the
rest of History a page at a time. Each checkout's relay stays open, so a
look folds only what was appended since the last; a checkout's queue is
re-read only when its relay changed on disk, and a settled review's row is
projected once, since nothing about it can change again — so an idle page
costs a few stats a second, however long the history behind it.

What a review *is* — its projection into files, hunks and captured
evidence — is :mod:`lup.devtools.review.app`'s; this module is what exists
because a browser reads it: the multi-checkout store, the HTTP surface and
the command that serves it.
"""

import asyncio
import hmac
import logging
import sys
import webbrowser
from collections.abc import Callable, Iterator
from datetime import datetime
from functools import cache
from pathlib import Path
from tempfile import mkdtemp
from typing import TYPE_CHECKING

import httpx
import sh
import typer
from pydantic import BaseModel, Field, PrivateAttr
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_fixed

from lup.coordination.repository import PeerDepartedError
from lup.devtools.dashboard.address import AdvertisedDashboard
from lup.devtools.dashboard.companion import (
    Dashboard,
    DashboardHealth,
    DashboardRegistry,
    DashboardToken,
    DeclaredOrigins,
    KnownRepository,
    dashboard_revision,
    dashboard_status,
    launch_urls,
    private_urls,
    restarted,
    refuse_inside_a_session,
)
from lup.devtools.dashboard.panes import SetupPane, SetupPanes
from lup.devtools.dashboard.pulse import PulseFile, answered
from lup.devtools.review.app import (
    ArchivedReview,
    RequesterPresence,
    ReviewArchive,
    ReviewDetail,
    ReviewDocuments,
    ReviewRoot,
    ReviewSummary,
    expire_orphaned,
    newer_code,
    relay,
    retire_settled,
    terminal_answer,
)
from lup.devtools.review.notifications import (
    ReviewNotification,
    ReviewNotifications,
    notify_requester,
)
from lup.devtools.review.preimages import PreimageWatch
from lup.devtools.review.thread import ReviewThread, spoken_on
from lup.launch.companions import lent_directory
from lup.policy.relay import (
    LineComment,
    PersistentQuestion,
    QuestionRecord,
    QuestionRelay,
    RecordedQuestion,
    RecordedRemark,
    RelaySignature,
    Reply,
)
from lup.providers.user_config import UserConfigFile
from lup.sandbox.rail import repository_layout, sibling_worktrees
from lup.types import StringMap

if TYPE_CHECKING:
    from fastapi import FastAPI

    from lup.devtools.dashboard.stream import LiveFeed

logger = logging.getLogger(__name__)


class ReviewError(BaseModel, frozen=True):
    """A checkout that could not be read, without hiding its absence."""

    root: str
    message: str


class ReviewSnapshot(BaseModel, frozen=True):
    """The configured review queues as the page is handed them.

    ``reviews`` holds every review still waiting and History's first page,
    the most recently settled (:attr:`ReviewStore.recent`), each as its queue
    row; the documents a review binds are read only when it is opened.
    ``history`` is how many reviews have left the queue, archived ones
    included.
    """

    roots: list[ReviewRoot]
    reviews: list[ReviewSummary] = []
    errors: list[ReviewError] = []
    history: int = 0


class ReviewHistory(BaseModel, frozen=True):
    """One page of History: reviews that left the queue, most recently settled first, and how many there are."""

    reviews: list[ReviewSummary]
    total: int


class ReviewAnswer(BaseModel, frozen=True, extra="forbid"):
    """A decision bound to the fingerprint the browser displayed, with what the operator wrote."""

    approved: bool
    note: str = ""
    comments: list[LineComment] = []
    fingerprint: str


class ReviewRemarkRequest(BaseModel, frozen=True, extra="forbid"):
    """The operator's note and line comments on a review, sent without deciding it."""

    note: str = ""
    comments: list[LineComment] = []
    fingerprint: str


class ReviewDecision(BaseModel, frozen=True):
    """What the operator's action recorded, and how the requester hears of it."""

    review: ReviewDetail
    notification: ReviewNotification


class ReviewHeaders(BaseModel, frozen=True):
    """The request headers relevant to dashboard authentication."""

    authorization: str = ""
    origin: str = ""
    content_type: str = Field(default="", alias="content-type")
    last_event_id: str = Field(default="", alias="last-event-id")
    """The cursor of the last frame a reconnecting tab saw on the stream."""


class LocatedReview(BaseModel, frozen=True):
    """A review paired with the checkout that owns its relay: in its log, or in its archive."""

    root: Path
    question: RecordedQuestion | None = None
    archived: ArchivedReview | None = None


class ReviewAddress(BaseModel, frozen=True):
    """Where a review's key points: the checkout whose relay holds it, and its id there."""

    root: Path
    id: str


class SettledReview(BaseModel, frozen=True):
    """One review that left the queue, where History finds it: its relay's log, or its archive."""

    root: Path
    settled: datetime
    question: RecordedQuestion | None = None
    archived: ArchivedReview | None = None


class HistoryReading(BaseModel):
    """Every review that left the queue, most recently settled first, as of the queues it was read from.

    Moved whole each time those queues move, by the store that keeps it.
    """

    signatures: list[str] = []
    entries: list[SettledReview] = []


class ReviewScan(BaseModel, frozen=True):
    """Discovered checkouts, the repository each belongs to, and roots that failed."""

    roots: tuple[Path, ...] = ()
    repositories: dict[Path, Path] = {}
    """The repository each discovered checkout belongs to, by its shared git directory."""

    errors: list[ReviewError] = []

    @classmethod
    def of(cls, anchor: Path) -> "ReviewScan":
        """Every worktree of the repository ``anchor`` names."""
        try:
            checkouts = review_roots(anchor, [])
        except (OSError, ValueError, sh.ErrorReturnCode) as error:
            return cls(errors=[ReviewError(root=str(anchor), message=str(error))])
        return cls(
            roots=checkouts, repositories={checkout: anchor for checkout in checkouts}
        )


class ReviewQueue(BaseModel, frozen=True):
    """A relay read whose failure does not hide other checkouts' reviews."""

    root: Path
    signature: RelaySignature = RelaySignature()
    questions: list[RecordedQuestion] = []
    remarks: dict[str, list[RecordedRemark]] = {}
    """The operator's remarks on each review, by review id."""

    replies: dict[str, list[Reply]] = {}
    """The requester's replies on each review, by review id."""

    errors: list[ReviewError] = []

    @classmethod
    def read(
        cls, root: Path, store: QuestionRelay, attempts: int = 3, pause: float = 0.05
    ) -> "ReviewQueue":
        """One checkout's queue through its relay, read again a moment later before a failure is reported.

        The relay stays open between reads, so a read folds only what was
        appended since the last. A writer appending to the relay or its
        answers while the page reads them is gone a moment later, so a read
        that fails is tried *attempts* times, *pause* seconds apart, before
        the queue is reported unavailable.
        """
        signature = store.signature()

        @retry(
            stop=stop_after_attempt(attempts),
            wait=wait_fixed(pause),
            retry=retry_if_exception_type((OSError, ValueError)),
            reraise=True,
        )
        def read_once() -> "ReviewQueue":
            return cls(
                root=root,
                signature=signature,
                questions=store.questions(),
                remarks=store.remarks(),
                replies=store.replies(),
            )

        try:
            return read_once()
        except (OSError, ValueError) as error:
            return cls(
                root=root,
                signature=signature,
                errors=[ReviewError(root=str(root), message=str(error))],
            )

    def said(self, question: QuestionRecord) -> int:
        """How many remarks and replies one review's thread holds."""
        return sum(
            entry.kind != "answer"
            for entry in spoken_on(question, self.remarks, self.replies)
        )


class QueueRow(BaseModel, frozen=True):
    """One review's row beside the checkout it was parked in and its record, for naming who asked."""

    root: Path
    summary: ReviewSummary
    question: QuestionRecord


def settled_key(root: Path, question: QuestionRecord, said: int) -> str | None:
    """What a review's row is kept under, or nothing where time alone can change it.

    A pending review with an expiry turns into an expired one without a
    record being written, so its row is projected afresh each time. Whether
    a waiting review's files moved is read afresh on every look, beside the
    row rather than into its key.
    """
    if question.state == "pending" and question.expires is not None:
        return None
    answered = question.answer.model_dump_json() if question.answer else ""
    return f"{root}#{question.id}#{question.state}#{question.fingerprint}#{answered}#{said}"


class ReviewStore(BaseModel, frozen=True):
    """Read only the queues the operator's selection reaches, and answer their exact records.

    ``roots`` are the checkouts or repositories named; ``registry``, where
    given, adds every repository a launch held the dashboard for. Each
    checkout's relay stays open, so a look folds only what was appended since
    the last one, and its queue is kept until the relay changes; the
    checkouts themselves are looked for each time, so a worktree made a
    moment ago is already here. A review's documents are read when it is
    shown, and what they show is worked out once for its fingerprint.
    """

    roots: tuple[Path, ...]
    principal: str = "operator"
    discover: bool = False
    registry: DashboardRegistry | None = None
    recent: int = 50
    """How many of the reviews that left the queue ride on the stream beside the ones waiting.

    History's first page: what the operator just answered, and what went
    stale or expired under them, without asking for a page; the rest is read
    a page at a time (:meth:`history`), this many to a page unless asked
    otherwise.
    """

    restarting: Callable[[], bool] = lambda: False
    """Whether the dashboard is about to restart onto its checkout's newer code.

    Said beside a review this code cannot read, which that code may.
    """

    _queues: dict[Path, ReviewQueue] = {}
    _rows: dict[str, ReviewSummary] = {}
    _preimages: PreimageWatch = PrivateAttr(default_factory=PreimageWatch)
    _relays: dict[Path, QuestionRelay] = {}
    _archives: dict[Path, ReviewArchive] = {}
    _documents: dict[str, ReviewDocuments] = {}
    _addresses: dict[str, ReviewAddress] = {}
    _history: HistoryReading = PrivateAttr(default_factory=HistoryReading)

    def anchors(self) -> tuple[Path, ...]:
        """The named roots, then every repository the registry knows."""
        known = (
            [each.repository for each in self.registry.repositories()]
            if self.registry is not None
            else []
        )
        return tuple(dict.fromkeys([*self.roots, *known]))

    def scan_roots(self) -> ReviewScan:
        if not self.discover:
            return ReviewScan(roots=self.anchors())
        scans = [ReviewScan.of(anchor) for anchor in self.anchors()]
        return ReviewScan(
            roots=tuple(dict.fromkeys(root for each in scans for root in each.roots)),
            repositories={
                root: anchor
                for each in scans
                for root, anchor in each.repositories.items()
            },
            errors=[error for each in scans for error in each.errors],
        )

    def checkout_roots(self) -> tuple[Path, ...]:
        return self.scan_roots().roots

    def relay(self, root: Path) -> QuestionRelay:
        """One checkout's relay, kept open so each read folds only what was appended since."""
        if root not in self._relays:
            self._relays[root] = relay(root)
        return self._relays[root]

    def archive(self, root: Path) -> ReviewArchive:
        """One checkout's archive of retired reviews, kept open the same way."""
        if root not in self._archives:
            self._archives[root] = ReviewArchive(self.relay(root))
        return self._archives[root]

    def queue(self, root: Path) -> ReviewQueue:
        """One checkout's queue, re-read only where its relay or its answers changed on disk."""
        store = self.relay(root)
        signature = store.signature()
        if root in self._queues and self._queues[root].signature == signature:
            return self._queues[root]
        queue = ReviewQueue.read(root, store)
        self._queues[root] = queue
        self._addresses.update(
            {
                ReviewSummary.key_for(root, question.id): ReviewAddress(
                    root=root, id=question.id
                )
                for question in queue.questions
            }
        )
        return queue

    def retired(self, root: Path, question: RecordedQuestion) -> RecordedQuestion:
        """The question as it stands once staleness is settled: retired where a recorded file moved.

        Asked of every waiting review on every look, and again when one is
        opened or answered, since a file moves without the queue changing --
        and a review asked only when the queue changes turns unapprovable in
        front of the operator.
        A stale review leaves the queue at once and its requester is told to
        ask again; what moved is read through a stat-keyed watch against the
        digest each preimage is recorded by, so a waiting review whose files
        did not move costs a few stats.
        """
        if question.state != "pending":
            return question
        drifted = self._preimages.moved(question)
        if not drifted:
            return question
        retired = self.relay(root).retire_stale(
            question.id, [each.path for each in drifted], "the dashboard"
        )
        try:
            notify_requester(self.checkout_roots(), root, retired)
        except Exception:
            logger.exception("%s went stale and its requester was not told", retired.id)
        return retired

    def summary(self, root: Path, question: RecordedQuestion) -> ReviewSummary:
        """One review's row, as its documents read; one whose documents cannot be read back says so."""
        said = self.queue(root).said(question)
        restarting = self.restarting()
        try:
            return ReviewSummary.of(
                root,
                self.relay(root).resolve(question),
                self.principal,
                said,
                restarting,
            )
        except ValueError as unread:
            summary = ReviewSummary.from_files(
                root,
                PersistentQuestion.model_validate(
                    question.model_dump(exclude={"preconditions", "file_reviews"})
                ),
                self.principal,
                [],
                said,
            )
            return summary.model_copy(
                update={
                    "answerable": False,
                    "unanswerable": (
                        f"Its documents cannot be read back whole: {unread}. "
                        "Where newer code parked it, that code can answer it: "
                        f"{terminal_answer(root, question.id, self.principal)}."
                        + newer_code(restarting)
                        if question.state == "pending"
                        else ""
                    ),
                }
            )

    def row(self, queue: ReviewQueue, question: RecordedQuestion) -> ReviewSummary:
        """One review's row, projected once where nothing but a new record changes it.

        Not kept while the dashboard is about to restart, when a row it
        cannot answer says so and the restart ends the saying.
        """
        entry = self.retired(queue.root, question)
        key = settled_key(queue.root, entry, queue.said(entry))
        restarting = self.restarting()
        if key is not None and not restarting and key in self._rows:
            return self._rows[key]
        summary = self.summary(queue.root, entry)
        if key is not None and not restarting:
            self._rows[key] = summary
        return summary

    def settled(self, scan: ReviewScan) -> list[SettledReview]:
        """Every review that left the queue, in every checkout served, most recently settled first.

        Sorted again only where a checkout's relay or archive moved since the
        last reading.
        """
        queues = [self.queue(root) for root in scan.roots]
        archived = {root: self.archive(root).reviews() for root in scan.roots}
        signatures = [
            f"{queue.root}#{queue.signature.model_dump_json()}#{len(archived[queue.root])}"
            for queue in queues
        ]
        if signatures == self._history.signatures:
            return self._history.entries
        entries = sorted(
            [
                *(
                    SettledReview(
                        root=queue.root, settled=question.since(), question=question
                    )
                    for queue in queues
                    for question in queue.questions
                    if question.state != "pending"
                ),
                *(
                    SettledReview(
                        root=root, settled=kept.question.since(), archived=kept
                    )
                    for root, reviews in archived.items()
                    for kept in reviews.values()
                ),
            ],
            key=lambda entry: entry.settled,
            reverse=True,
        )
        self._addresses.update(
            {
                ReviewSummary.key_for(entry.root, entry.archived.question.id): (
                    ReviewAddress(root=entry.root, id=entry.archived.question.id)
                )
                for entry in entries
                if entry.archived is not None
            }
        )
        self._history.signatures, self._history.entries = signatures, entries
        return entries

    def settled_row(self, entry: SettledReview) -> QueueRow:
        """The row of one review that left the queue, from its relay or its archive."""
        if entry.archived is not None:
            return QueueRow(
                root=entry.root,
                summary=ReviewSummary.retired(entry.root, entry.archived),
                question=entry.archived.question,
            )
        assert entry.question is not None
        return QueueRow(
            root=entry.root,
            summary=self.row(self.queue(entry.root), entry.question),
            question=entry.question,
        )

    def presence(self, scan: ReviewScan) -> Callable[[Path], RequesterPresence]:
        """The roster of the repository each checkout belongs to, read once however many reviews ask.

        Read only where a review asks after it, through the first checkout
        of that repository the scan found.
        """

        def repository(root: Path) -> Path:
            return scan.repositories[root] if root in scan.repositories else root

        first = {repository(root): root for root in reversed(scan.roots)}

        @cache
        def roster(held: Path) -> RequesterPresence:
            return RequesterPresence.of(first[held] if held in first else held)

        return lambda root: roster(repository(root))

    def named(
        self, rows: list[QueueRow], presence: Callable[[Path], RequesterPresence]
    ) -> list[ReviewSummary]:
        """Each row named by the session that asked, as its repository's roster calls it."""
        return [
            row.summary.model_copy(
                update={"session": presence(row.root).called(row.question)}
            )
            for row in rows
        ]

    def page(
        self, settled: list[SettledReview], offset: int, limit: int
    ) -> list[QueueRow]:
        """One page of History's rows: *limit* of them from *offset*, most recently settled first."""
        return [self.settled_row(entry) for entry in settled[offset : offset + limit]]

    def snapshot(self) -> ReviewSnapshot:
        """Every waiting review and History's first page, each as its row, with how many settled.

        What the stream hands the page: rows, never documents, and History
        past its first page read a page at a time (:meth:`history`).
        """
        scan = self.scan_roots()
        queues = [self.queue(root) for root in scan.roots]
        waiting = [
            QueueRow(
                root=queue.root,
                summary=self.row(queue, question),
                question=self.retired(queue.root, question),
            )
            for queue in queues
            for question in queue.questions
            if question.state == "pending"
        ]
        settled = self.settled(scan)
        rows = self.named(
            [
                *(row for row in waiting if row.question.state == "pending"),
                *self.page(settled, 0, self.recent),
            ],
            self.presence(scan),
        )
        return ReviewSnapshot(
            roots=[
                ReviewRoot.of(root).within(scan.repositories[root])
                if root in scan.repositories
                else ReviewRoot.of(root)
                for root in scan.roots
            ],
            reviews=sorted(
                {row.key: row for row in rows}.values(),
                key=lambda row: row.created,
                reverse=True,
            ),
            errors=scan.errors + [error for queue in queues for error in queue.errors],
            history=len(settled),
        )

    def history(
        self, offset: int, limit: int, review: str = "", root: str = ""
    ) -> ReviewHistory:
        """One page of History, most recently settled first, archived reviews included.

        Naming a *review* -- and the checkout it was parked in, by its id --
        reads the entries a link to it names instead, wherever they stand.
        """
        scan = self.scan_roots()
        settled = self.settled(scan)
        chosen = (
            [
                entry
                for entry in settled
                if (
                    entry.archived.question.id
                    if entry.archived is not None
                    else entry.question.id
                    if entry.question is not None
                    else ""
                )
                == review
                and (not root or ReviewRoot.of(entry.root).id == root)
            ]
            if review
            else settled
        )
        return ReviewHistory(
            reviews=self.named(self.page(chosen, offset, limit), self.presence(scan)),
            total=len(settled),
        )

    def sweep(self) -> None:
        """Expire every review whose requester is gone, and archive every one settled past its window."""
        for root in self.checkout_roots():
            try:
                expire_orphaned(root, store=self.relay(root))
                retire_settled(root, self.relay(root), self.archive(root))
            except (OSError, ValueError, sh.ErrorReturnCode) as error:
                logger.warning("could not sweep the reviews in %s: %s", root, error)

    def locate(self, key: str) -> LocatedReview:
        """The review a key names, through the addresses the looks so far learned, else every queue read."""
        from fastapi import HTTPException

        if key not in self._addresses:
            scan = self.scan_roots()
            queues = [self.queue(root) for root in scan.roots]
            self.settled(scan)
            errors = scan.errors + [error for queue in queues for error in queue.errors]
            if key not in self._addresses and errors:
                raise HTTPException(
                    status_code=503,
                    detail="\n".join(error.message for error in errors),
                )
        if key not in self._addresses:
            raise HTTPException(status_code=404, detail="No review has that key")
        address = self._addresses[key]
        found = self.relay(address.root).find(address.id)
        if found is not None:
            return LocatedReview(root=address.root, question=found)
        kept = self.archive(address.root).reviews()
        if address.id in kept:
            return LocatedReview(root=address.root, archived=kept[address.id])
        raise HTTPException(status_code=404, detail="No review has that key")

    def shown(self, root: Path, question: PersistentQuestion) -> ReviewDetail:
        """One review whole, what its documents show worked out once for its fingerprint."""
        cached = f"{root}#{question.id}#{question.fingerprint}"
        if cached not in self._documents:
            self._documents[cached] = ReviewDocuments.of(question)
        return ReviewDetail.of(
            root, question, self.principal, self.relay(root), self._documents[cached]
        )

    def resolved(self, root: Path, question: RecordedQuestion) -> PersistentQuestion:
        """One question with its documents read back, or a refusal saying why it cannot be."""
        from fastapi import HTTPException

        try:
            return self.relay(root).resolve(question)
        except ValueError as unread:
            raise HTTPException(
                status_code=409,
                detail=f"The review cannot be read back whole: {unread}",
            ) from unread

    def detail(self, key: str) -> ReviewDetail:
        located = self.locate(key)
        if located.archived is not None:
            return ReviewDetail.retired(located.root, located.archived)
        assert located.question is not None
        entry = self.retired(located.root, located.question)
        return self.shown(located.root, self.resolved(located.root, entry))

    def bound(self, key: str, fingerprint: str) -> LocatedReview:
        """The waiting review under *key*, where it still carries the fingerprint the page displayed."""
        from fastapi import HTTPException

        located = self.locate(key)
        if located.question is None:
            raise HTTPException(
                status_code=409,
                detail="The review was archived: nothing may answer it any more.",
            )
        if not hmac.compare_digest(
            fingerprint.encode("utf-8"), located.question.fingerprint.encode("utf-8")
        ):
            raise HTTPException(
                status_code=409,
                detail="The review changed since the page showed it; read it again.",
            )
        return located

    def answer(self, key: str, decision: ReviewAnswer) -> ReviewDecision:
        """Record the operator's decision, and tell the requester by its one channel.

        Reads nothing but this review: the relay the looks keep open, folded
        forward by what was appended since, and the review's own documents.
        A review a recorded file moved under is retired as stale rather than
        answered, since what its waiter would carry out is no longer what the
        operator read; the refusal names what moved, and the page rolls the
        answer back where the operator is looking.
        """
        from fastapi import HTTPException

        located = self.bound(key, decision.fingerprint)
        assert located.question is not None
        entry = self.retired(located.root, located.question)
        if entry.state == "stale":
            raise HTTPException(
                status_code=409,
                detail="It went stale before it was answered: "
                + "; ".join(str(path) for path in entry.moved)
                + " changed since it was recorded. It left the queue, and its "
                "session is told to ask again.",
            )
        if entry.overdue():
            raise HTTPException(
                status_code=409, detail="The review expired unanswered."
            )
        try:
            settled = self.relay(located.root).answer(
                entry.id,
                self.principal,
                decision.approved,
                decision.note,
                comments=decision.comments,
            )
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        if settled.answer is None:
            raise HTTPException(
                status_code=409, detail=f"The review is {settled.state}."
            )
        notifications = ReviewNotifications(root=located.root)
        notification = notifications.complete(
            settled,
            notifications.prepare(settled),
            lambda: notify_requester(self.checkout_roots(), located.root, settled),
        )
        return ReviewDecision(
            review=self.shown(located.root, settled), notification=notification
        )

    def remark(self, key: str, said: ReviewRemarkRequest) -> ReviewDecision:
        """Send the operator's note and line comments on a review without deciding it.

        The review stays as it was; the requester hears of the remark by the
        same one channel an answer takes, and its waiter says the review is
        still pending and how to wait on it again.
        """
        from fastapi import HTTPException

        located = self.bound(key, said.fingerprint)
        assert located.question is not None
        entry = self.resolved(located.root, located.question)
        try:
            recorded = ReviewThread.of(self.relay(located.root)).remark(
                entry, self.principal, said.note, said.comments
            )
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        try:
            notification = notify_requester(
                self.checkout_roots(), located.root, entry, recorded.remark
            )
        except Exception as error:
            logger.exception("a remark on %s was recorded and not delivered", entry.id)
            notification = ReviewNotification(
                queued=False,
                woken=False,
                detail=f"Recorded; telling the session failed: {error}",
            )
        return ReviewDecision(
            review=self.shown(located.root, entry), notification=notification
        )


def forwarded_headers(port: int, content_type: str) -> StringMap:
    """What a pane's request carries to the page serving it: that page's own Host."""
    return {
        "host": f"127.0.0.1:{port}",
        **({"content-type": content_type} if content_type else {}),
    }


def dashboard_app(
    url: str,
    token: str,
    roots: tuple[Path, ...],
    *,
    discover: bool = False,
    registry: DashboardRegistry | None = None,
    bundles: Path | None = None,
    health: DashboardHealth | None = None,
    panes: SetupPanes | None = None,
    feed: "LiveFeed | None" = None,
    config: UserConfigFile | None = None,
) -> "FastAPI":
    """Build the dashboard: an authenticated browser surface over reviews and sessions.

    ``health`` is what a running dashboard answers its launcher with, and
    ``panes`` each repository's setup page; neither is served where not given.
    The repositories whose sessions it shows are the ``roots`` named and
    every one the ``registry`` knows. ``feed`` is the stream's producer where
    the caller follows it too — the service, asking whether any tab is open —
    and its store is the one every route reads, so the relays it keeps open
    serve the stream, a review opened, and an answer alike. Beside ``url``,
    it answers at each origin the person's lup ``config`` declares, as that
    file says at each request.
    """
    from fastapi import HTTPException, Query, Request
    from fastapi.responses import JSONResponse, Response, StreamingResponse
    from starlette.datastructures import MutableHeaders
    from starlette.types import ASGIApp, Message, Receive, Scope, Send

    from lup.devtools.dashboard.live import (
        MessagePage,
        ReplyOutcome,
        ReplyRequest,
        earlier_messages,
        reply,
    )
    from lup.devtools.dashboard.stream import LiveFeed
    from lup.web.serve import bundle_app

    named = named_repositories(roots)

    def anchor(root: Path) -> Path:
        try:
            return repository_layout(root).common.resolve()
        except (OSError, ValueError, sh.ErrorReturnCode):
            # Keep unavailable selections so scans report them and can recover.
            return root

    if discover:
        roots = tuple(dict.fromkeys(anchor(root) for root in roots))
    declared = DeclaredOrigins(config)
    refusal = (
        "unexpected Host header: the dashboard answers at its loopback address, "
        "and at each origin `[dashboard] origins` declares in the person's lup config"
    )
    app = (
        bundle_app("Dashboard", url, "dashboard", origins=declared, refusal=refusal)
        if bundles is None
        else bundle_app(
            "Dashboard", url, "dashboard", bundles, origins=declared, refusal=refusal
        )
    )

    def watched() -> list[KnownRepository]:
        return [*named, *(registry.repositories() if registry is not None else [])]

    feed = (
        feed
        if feed is not None
        else LiveFeed(
            watched, ReviewStore(roots=roots, discover=discover, registry=registry)
        )
    )
    store = feed.reviews

    def refused(request: Request) -> JSONResponse | None:
        """Why a request is turned away before it is served; nothing where it is not."""
        headers = ReviewHeaders.model_validate(request.headers)
        if request.url.path.startswith("/api/"):
            provided = headers.authorization.encode("utf-8")
            expected = f"Bearer {token}".encode("utf-8")
            if not hmac.compare_digest(provided, expected):
                return JSONResponse(
                    {"detail": "Authentication required"}, status_code=401
                )
        if request.method == "POST":
            if headers.origin != url and headers.origin not in declared():
                return JSONResponse({"detail": "Origin refused"}, status_code=403)
            if headers.content_type != "application/json":
                return JSONResponse({"detail": "JSON required"}, status_code=415)
        return None

    class Authorized:
        """Every request's gate: the capability on the API, the origin and JSON
        on a write, and the headers every answer carries.

        Plain ASGI rather than an ``http`` middleware function, which runs each
        response body in a task group of its own: a stream the server ends as it
        stops then ends, rather than being cancelled there and logged as an error.
        """

        def __init__(self, inner: ASGIApp) -> None:
            self.inner = inner

        async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
            if scope["type"] != "http":
                await self.inner(scope, receive, send)
                return
            request = Request(scope)
            refusal = refused(request)
            if refusal is not None:
                await refusal(scope, receive, send)
                return
            pane = request.url.path.startswith("/setup/")

            async def headed(message: Message) -> None:
                if message["type"] == "http.response.start":
                    answered = MutableHeaders(scope=message)
                    answered["Cache-Control"] = "no-store"
                    answered["Referrer-Policy"] = "no-referrer"
                    answered["X-Content-Type-Options"] = "nosniff"
                    answered["X-Frame-Options"] = "SAMEORIGIN" if pane else "DENY"
                    answered["Content-Security-Policy"] = (
                        "frame-ancestors 'self'" if pane else "frame-ancestors 'none'"
                    )
                await send(message)

            await self.inner(scope, receive, headed)

    app.add_middleware(Authorized)

    @app.get("/api/reviews")
    def reviews() -> ReviewSnapshot:
        return store.snapshot()

    @app.get("/api/reviews/history")
    def history(
        offset: int = Query(0, ge=0),
        limit: int | None = Query(None, ge=1),
        review: str = "",
        root: str = "",
    ) -> ReviewHistory:
        """One page of History, most recently settled first, or where a link names one review."""
        return store.history(
            offset, limit if limit is not None else store.recent, review, root
        )

    @app.get("/api/reviews/{key}")
    def detail(key: str) -> ReviewDetail:
        return store.detail(key)

    @app.post("/api/reviews/{key}/answer")
    def decide(key: str, decision: ReviewAnswer) -> ReviewDecision:
        return store.answer(key, decision)

    @app.post("/api/reviews/{key}/remark")
    def remark(key: str, said: ReviewRemarkRequest) -> ReviewDecision:
        return store.remark(key, said)

    @app.get("/api/stream")
    async def stream(request: Request) -> StreamingResponse:
        """Everything live, as server-sent events resuming after ``Last-Event-ID``."""
        resume = ReviewHeaders.model_validate(request.headers).last_event_id
        return StreamingResponse(
            feed.follow(resume, request.is_disconnected),
            media_type="text/event-stream",
        )

    @app.post("/api/repositories/{repository}/sessions/{member}/messages")
    def message(repository: str, member: str, request: ReplyRequest) -> ReplyOutcome:
        """The operator's message to one session or subagent, by its member id."""
        known = next((each for each in feed.served() if each.key() == repository), None)
        if known is None:
            raise HTTPException(status_code=404, detail="No repository has that key")
        try:
            return reply(known, member, request.text)
        except PeerDepartedError as departed:
            raise HTTPException(status_code=409, detail=str(departed)) from departed
        except LookupError as missing:
            raise HTTPException(status_code=404, detail=str(missing)) from missing

    @app.get("/api/repositories/{repository}/messages")
    def messages(repository: str, before: int = Query(ge=0)) -> MessagePage:
        """One page of a repository's mail record, whose lines end by byte ``before``."""
        known = next((each for each in feed.served() if each.key() == repository), None)
        if known is None:
            raise HTTPException(status_code=404, detail="No repository has that key")
        return earlier_messages(known, before)

    @app.get("/api/setup")
    def setup_panes() -> list[SetupPane]:
        return panes.listed() if panes is not None else []

    if health is not None:
        answered = health

        @app.get("/api/service")
        def service() -> DashboardHealth:
            return answered

    if panes is not None:
        served = panes

        @app.api_route("/setup/{key}/{capability}/{rest:path}", methods=["GET", "POST"])
        async def setup_pane(
            key: str, capability: str, rest: str, request: Request
        ) -> Response:
            if not served.admits(key, capability):
                return Response(status_code=404)
            try:
                port = await asyncio.to_thread(served.reached, key)
            except LookupError:
                return Response(status_code=404)
            except (RuntimeError, OSError, sh.ErrorReturnCode) as error:
                return Response(content=str(error), status_code=502)
            headers = ReviewHeaders.model_validate(request.headers)
            async with httpx.AsyncClient(trust_env=False, timeout=60) as client:
                answered_by = await client.request(
                    request.method,
                    f"http://127.0.0.1:{port}/{rest}",
                    params=request.query_params,
                    content=await request.body(),
                    headers=forwarded_headers(port, headers.content_type),
                )
            kind = (
                answered_by.headers["content-type"]
                if "content-type" in answered_by.headers
                else None
            )
            return Response(
                content=answered_by.content,
                status_code=answered_by.status_code,
                media_type=kind,
            )

    return app


def review_roots(root: Path, additional: list[Path]) -> tuple[Path, ...]:
    """Discover sibling worktrees only for repositories named by the operator."""

    def candidates() -> Iterator[Path]:
        for source in [root, *additional]:
            resolved = source.resolve(strict=True)
            for candidate in [resolved, *sibling_worktrees(resolved)]:
                if (candidate / ".git").exists():
                    yield candidate.resolve()

    selected = tuple(dict.fromkeys(candidates()))
    if not selected:
        raise ValueError("No Git worktrees were found for the selected roots")
    return selected


def named_repositories(roots: tuple[Path, ...]) -> list[KnownRepository]:
    """The repositories an operator named on a command line, each with its checkout."""

    def known(root: Path) -> KnownRepository:
        try:
            repository = repository_layout(root).common.resolve()
        except (OSError, ValueError, sh.ErrorReturnCode):
            repository = root
        return KnownRepository(repository=repository, checkout=root)

    return [known(root) for root in roots]


def create_operator_dashboard_app(root: Path) -> typer.Typer:
    """Wire the `dashboard` group: the operator's page, and the service every launch holds."""
    app = typer.Typer(no_args_is_help=True)
    companion = Dashboard()

    def refused(verb: str, action: Callable[[], None]) -> None:
        try:
            refuse_inside_a_session(f"dashboard {verb}")
            action()
        except (PermissionError, LookupError) as refusal:
            typer.echo(str(refusal), err=True)
            raise typer.Exit(2) from refusal

    def announced(addresses: list[str], declared: DeclaredOrigins) -> None:
        """Print each launch address, then what to hear of the declared origins."""
        for address in addresses:
            typer.echo(f"Operator launch URL: {address}")
        for warning in declared.warnings():
            typer.echo(warning, err=True)

    @app.command("serve")
    def serve_cmd(
        selected_roots: list[Path] | None = typer.Option(
            None,
            "--root",
            help="Watch this repository's worktrees instead of the current repository; repeatable",
        ),
        host: str = typer.Option("127.0.0.1", help="Loopback address to bind"),
        port: int = typer.Option(8766, min=1, max=65535, help="Dashboard port"),
        open_page: bool = typer.Option(
            True, "--open/--no-open", help="Open the browser"
        ),
    ) -> None:
        """Serve the dashboard in this terminal, over the selected repositories, until Ctrl+C."""

        def serve() -> None:
            from lup.devtools.dashboard.service import (
                ServiceArguments,
                said_in_output,
                serve_dashboard,
            )
            from lup.web.loopback import refuse_non_loopback

            refuse_non_loopback(host, "Dashboard")
            roots = tuple(
                dict.fromkeys(
                    path.resolve(strict=True) for path in (selected_roots or [root])
                )
            )
            # This terminal's own state, private to it: the capability a
            # restart in place keeps, and the repositories it serves.
            state = Path(mkdtemp(prefix="lup-dashboard-serve-"))
            token = DashboardToken(directory=state).minted().value
            registry = DashboardRegistry(directory=state)
            for known in named_repositories(roots):
                registry.recorded(known)
            served = ServiceArguments(
                state=state,
                port=port,
                revision=dashboard_revision(),
                host=host,
                shared=False,
            )
            declared = DeclaredOrigins()
            addresses = launch_urls(served.url(), token, declared())
            typer.echo(f"Dashboard: {served.url()} — Ctrl+C stops this server.")
            announced(addresses, declared)
            if open_page:
                webbrowser.open(addresses[0])
            said_in_output()
            serve_dashboard(served)

        refused("serve", serve)

    @app.command("open")
    def open_cmd() -> None:
        """Open the dashboard the running sessions hold, in this machine's browser."""

        def opened() -> None:
            declared = DeclaredOrigins()
            addresses = private_urls(companion, root, declared())
            if not webbrowser.open(addresses[0]):
                typer.echo("The browser did not open.")
            announced(addresses, declared)

        refused("open", opened)

    @app.command("status")
    def status_cmd() -> None:
        """Say whether the dashboard runs, where, for how many sessions, and what waits."""
        typer.echo(dashboard_status(companion, root).model_dump_json(indent=2))

    @app.command("line")
    def line_cmd(
        pulse: Path | None = typer.Argument(
            None,
            help="The pulse file to read; unset, the one this session's launch "
            "named, else the running dashboard's",
        ),
    ) -> None:
        """Print what a session's status line shows: which session, what waits on you, and the dashboard.

        The session is the one the runtime names on stdin, as it runs its
        status line; what waits and what other agents need show only while
        something does. Named with its pulse, as a status line runs it, it is
        answered before the project's application loads.
        """
        named = pulse or Path(
            AdvertisedDashboard().pulse
            or PulseFile.of(lent_directory(companion.slot(root).directory)).path
        )
        typer.echo(answered(named, sys.stdin), color=True)

    @app.command("reopen")
    def reopen_cmd(
        turned: bool | None = typer.Option(
            None,
            "--on/--off",
            help="Turn reopening on or off in your lup config; neither says which it is",
        ),
    ) -> None:
        """Whether a review parking while no tab is open reopens the page in the browser."""

        def settled() -> None:
            config = UserConfigFile()
            if turned is not None:
                config.record({("dashboard", "reopen"): turned})
            state = "on" if config.load().dashboard.reopen else "off"
            typer.echo(
                f"Reopening the page when a review parks with no tab open: {state} "
                f"(`[dashboard] reopen` in {config.path()}); the desktop notice "
                "is sent either way."
            )

        refused("reopen", settled)

    @app.command("restart")
    def restart_cmd() -> None:
        """Restart the running dashboard onto its checkout's code, keeping its address.

        It does so by itself once its checkout's code moves; this asks now.
        """
        refused("restart", lambda: typer.echo(restarted(companion, root)))

    @app.command("stop")
    def stop_cmd() -> None:
        """Stop the running dashboard; it stays stopped until a restart or a launch."""

        def stopped() -> None:
            typer.echo(
                "Dashboard stopped; it stays stopped until "
                "`uv run lup-devtools dashboard restart` or the next launch."
                if companion.stopped(root)
                else "No dashboard was running."
            )

        refused("stop", stopped)

    return app
