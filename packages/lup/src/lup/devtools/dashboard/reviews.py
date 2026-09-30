"""The operator's dashboard over the review queues and sessions of every repository it serves.

One loopback page reads every worktree of every repository it was given — the
operator's `--root`s, or every repository a launch held the dashboard for —
so a review parked by any session in any of them is one list away, grouped by
the repository and the session that asked, beside every session and what it
is doing. The page holds a capability carried in the URL fragment, checks
Host and Origin on every request, and answers a review only against the
fingerprint it displayed and the preimages still on disk. It never runs the
operation: an approval releases one exact retry of the call that asked.

Everything live reaches the page on one stream (:mod:`.stream`). A
checkout's queue is re-read only when its relay changed on disk, and a
settled review's row is projected once, since nothing about it can change
again — so an idle page costs a few stats a second, however long the history
behind it.

What a review *is* — its projection into files, hunks and captured
evidence — is :mod:`lup.devtools.review.app`'s; this module is what exists
because a browser reads it: the multi-checkout store, the HTTP surface and
the command that serves it.
"""

import asyncio
import hmac
import logging
import secrets
import webbrowser
from collections.abc import Callable, Iterator
from functools import partial
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
    KnownRepository,
    dashboard_status,
    private_url,
    restarted,
    refuse_inside_a_session,
)
from lup.devtools.dashboard.panes import SetupPane, SetupPanes
from lup.devtools.dashboard.pulse import PulseFile, status_line
from lup.devtools.review.app import (
    RequesterPresence,
    ReviewDetail,
    ReviewRoot,
    ReviewSummary,
    expire_orphaned,
    relay,
)
from lup.devtools.review.notifications import (
    ReviewNotification,
    ReviewNotifications,
    notify_requester,
)
from lup.devtools.review.preimages import PreimageWatch
from lup.devtools.review.thread import (
    RecordedRemark,
    Reply,
    ReviewThread,
    spoken_on,
)
from lup.policy.relay import LineComment, PersistentQuestion, RelaySignature
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
    """A complete snapshot of the configured review queues."""

    roots: list[ReviewRoot]
    reviews: list[ReviewSummary] = []
    errors: list[ReviewError] = []


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
    """A stored question paired with the checkout that owns its relay."""

    root: Path
    question: PersistentQuestion


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
    questions: list[PersistentQuestion] = []
    remarks: dict[str, list[RecordedRemark]] = {}
    """The operator's remarks on each review, by review id."""

    replies: dict[str, list[Reply]] = {}
    """The requester's replies on each review, by review id."""

    errors: list[ReviewError] = []

    @classmethod
    def read(cls, root: Path, attempts: int = 3, pause: float = 0.05) -> "ReviewQueue":
        """One checkout's queue, read again a moment later before a failure is reported.

        A writer appending to the relay or its answers while the page reads
        them is gone a moment later, so a read that fails is tried *attempts*
        times, *pause* seconds apart, before the queue is reported unavailable.
        """
        store = relay(root)
        signature = store.signature()
        thread = ReviewThread.of(store)

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
                remarks=thread.remarks(),
                replies=thread.replies(),
            )

        try:
            return read_once()
        except (OSError, ValueError) as error:
            return cls(
                root=root,
                signature=signature,
                errors=[ReviewError(root=str(root), message=str(error))],
            )

    def said(self, question: PersistentQuestion) -> int:
        """How many remarks and replies one review's thread holds."""
        return sum(
            entry.kind != "answer"
            for entry in spoken_on(question, self.remarks, self.replies)
        )


def settled_key(root: Path, question: PersistentQuestion, said: int) -> str | None:
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
    checkout's queue is kept until its relay changes; the checkouts themselves
    are looked for each time, so a worktree made a moment ago is already here.
    """

    roots: tuple[Path, ...]
    principal: str = "operator"
    discover: bool = False
    registry: DashboardRegistry | None = None

    _queues: dict[Path, ReviewQueue] = {}
    _rows: dict[str, ReviewSummary] = {}
    _preimages: PreimageWatch = PrivateAttr(default_factory=PreimageWatch)

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

    def queue(self, root: Path) -> ReviewQueue:
        """One checkout's queue, re-read only where its relay or its answers changed on disk."""
        signature = relay(root).signature()
        if root in self._queues and self._queues[root].signature == signature:
            return self._queues[root]
        queue = ReviewQueue.read(root)
        self._queues[root] = queue
        return queue

    def retired(self, root: Path, question: PersistentQuestion) -> PersistentQuestion:
        """The question as it stands once staleness is settled: retired where a recorded file moved.

        Asked of every waiting review on every look, and again when one is
        opened or answered, since a file moves without the queue changing --
        which is how a review turned unapprovable in front of the operator.
        A stale review leaves the queue at once and its requester is told to
        ask again; what moved is read through a stat-keyed watch, so a waiting
        review whose files did not move costs a few stats.
        """
        if question.state != "pending":
            return question
        drifted = self._preimages.moved(question)
        if not drifted:
            return question
        retired = relay(root).retire_stale(
            question.id, [each.path for each in drifted], "the dashboard"
        )
        try:
            notify_requester(self.checkout_roots(), root, retired)
        except Exception:
            logger.exception("%s went stale and its requester was not told", retired.id)
        return retired

    def row(self, queue: ReviewQueue, question: PersistentQuestion) -> ReviewSummary:
        """One review's row, projected once where nothing but a new record changes it."""
        entry = self.retired(queue.root, question)
        said = queue.said(entry)
        key = settled_key(queue.root, entry, said)
        if key is not None and key in self._rows:
            return self._rows[key]
        summary = ReviewSummary.of(queue.root, entry, self.principal, said)
        if key is not None:
            self._rows[key] = summary
        return summary

    def read_root(
        self,
        root: Path,
        repository: Path | None = None,
        presence: Callable[[], RequesterPresence] | None = None,
    ) -> ReviewSnapshot:
        """One checkout's queue, each review named by the session that asked.

        ``presence`` reads the repository's roster, where one reading is shared
        by every worktree of it; read only where the queue holds a review.
        """
        queue = self.queue(root)
        located = ReviewRoot.of(root)
        seen = (
            (presence() if presence is not None else RequesterPresence.of(root))
            if queue.questions
            else RequesterPresence()
        )
        return ReviewSnapshot(
            roots=[located.within(repository) if repository else located],
            reviews=[
                self.row(queue, entry).model_copy(
                    update={"session": seen.called(entry)}
                )
                for entry in queue.questions
            ],
            errors=queue.errors,
        )

    def snapshot(self) -> ReviewSnapshot:
        scan = self.scan_roots()
        presences: dict[Path, RequesterPresence] = {}

        def presence(root: Path) -> RequesterPresence:
            """The roster of the repository ``root`` belongs to, read once a snapshot."""
            repository = scan.repositories[root] if root in scan.repositories else root
            if repository not in presences:
                presences[repository] = RequesterPresence.of(root)
            return presences[repository]

        queues = [
            self.read_root(
                root,
                scan.repositories[root] if root in scan.repositories else None,
                partial(presence, root),
            )
            for root in scan.roots
        ]
        return ReviewSnapshot(
            roots=[root for queue in queues for root in queue.roots],
            reviews=sorted(
                (row for queue in queues for row in queue.reviews),
                key=lambda row: row.created,
                reverse=True,
            ),
            errors=scan.errors + [error for queue in queues for error in queue.errors],
        )

    def sweep(self) -> None:
        """Expire every review whose requester is gone, in every checkout served."""
        for root in self.checkout_roots():
            try:
                expire_orphaned(root)
            except (OSError, ValueError, sh.ErrorReturnCode) as error:
                logger.warning("could not sweep the reviews in %s: %s", root, error)

    def locate(self, key: str) -> LocatedReview:
        from fastapi import HTTPException

        scan = self.scan_roots()
        queues = [self.queue(root) for root in scan.roots]
        for queue in queues:
            for entry in queue.questions:
                if ReviewSummary.key_for(queue.root, entry.id) == key:
                    return LocatedReview(root=queue.root, question=entry)
        errors = scan.errors + [error for queue in queues for error in queue.errors]
        if errors:
            raise HTTPException(
                status_code=503, detail="\n".join(error.message for error in errors)
            )
        raise HTTPException(status_code=404, detail="No review has that key")

    def detail(self, key: str) -> ReviewDetail:
        located = self.locate(key)
        return ReviewDetail.of(
            located.root, self.retired(located.root, located.question), self.principal
        )

    def bound(self, key: str, fingerprint: str) -> LocatedReview:
        """The review under *key*, where it still carries the fingerprint the page displayed."""
        from fastapi import HTTPException

        located = self.locate(key)
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

        A review a recorded file moved under is retired as stale rather than
        answered, since what its waiter would carry out is no longer what the
        operator read; the refusal names what moved, and the page rolls the
        answer back where the operator is looking.
        """
        from fastapi import HTTPException

        located = self.bound(key, decision.fingerprint)
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
            settled = relay(located.root).answer(
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
            review=ReviewDetail.of(located.root, settled, self.principal),
            notification=notification,
        )

    def remark(self, key: str, said: ReviewRemarkRequest) -> ReviewDecision:
        """Send the operator's note and line comments on a review without deciding it.

        The review stays as it was; the requester hears of the remark by the
        same one channel an answer takes, and its waiter says the review is
        still pending and how to wait on it again.
        """
        from fastapi import HTTPException

        located = self.bound(key, said.fingerprint)
        entry = located.question
        try:
            recorded = ReviewThread.of(relay(located.root)).remark(
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
            review=ReviewDetail.of(located.root, entry, self.principal),
            notification=notification,
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
) -> "FastAPI":
    """Build the dashboard: an authenticated browser surface over reviews and sessions.

    ``health`` is what a running dashboard answers its launcher with, and
    ``panes`` each repository's setup page; neither is served where not given.
    The repositories whose sessions it shows are the ``roots`` named and
    every one the ``registry`` knows. ``feed`` is the stream's producer where
    the caller follows it too — the service, asking whether any tab is open.
    """
    from fastapi import HTTPException, Request
    from fastapi.responses import JSONResponse, Response, StreamingResponse
    from starlette.datastructures import MutableHeaders
    from starlette.types import ASGIApp, Message, Receive, Scope, Send

    from lup.devtools.dashboard.live import ReplyOutcome, ReplyRequest, reply
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
    app = (
        bundle_app("Dashboard", url, "dashboard")
        if bundles is None
        else bundle_app("Dashboard", url, "dashboard", bundles)
    )
    store = ReviewStore(roots=roots, discover=discover, registry=registry)

    def watched() -> list[KnownRepository]:
        return [*named, *(registry.repositories() if registry is not None else [])]

    feed = feed if feed is not None else LiveFeed(watched, store)

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
            if headers.origin != url:
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
            import uvicorn

            from lup.web.loopback import refuse_non_loopback

            refuse_non_loopback(host, "Dashboard")
            roots = tuple(
                dict.fromkeys(
                    path.resolve(strict=True) for path in (selected_roots or [root])
                )
            )
            authority = f"[{host}]" if ":" in host else host
            url = f"http://{authority}" if port == 80 else f"http://{authority}:{port}"
            token = secrets.token_urlsafe(32)
            panes = SetupPanes(
                lambda: named_repositories(roots),
                token,
                Path(mkdtemp(prefix="lup-setup-panes-")),
            )
            page = dashboard_app(url, token, roots, discover=True, panes=panes)
            browser_url = f"{url}/#token={token}"
            typer.echo(f"Dashboard: {url} — Ctrl+C stops this server.")
            typer.echo(f"Operator launch URL: {browser_url}")
            if open_page:
                webbrowser.open(browser_url)
            try:
                uvicorn.run(
                    page,
                    host=host,
                    port=port,
                    access_log=False,
                    timeout_graceful_shutdown=2,
                )
            finally:
                panes.close()

        refused("serve", serve)

    @app.command("open")
    def open_cmd() -> None:
        """Open the dashboard the running sessions hold, in this machine's browser."""

        def opened() -> None:
            address = private_url(companion, root)
            if webbrowser.open(address):
                typer.echo(f"Dashboard: {dashboard_status(companion, root).url}")
                return
            typer.echo(f"The browser did not open. Operator launch URL: {address}")

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
        """Print what a session's status line shows: the reviews waiting, and where.

        Nothing where no dashboard answers; the address alone where nothing
        waits. Named with its pulse, as a status line runs it, it is answered
        before the project's application loads.
        """
        named = pulse or Path(
            AdvertisedDashboard().pulse
            or PulseFile.of(companion.slot(root).directory).path
        )
        shown = status_line(named)
        if shown:
            typer.echo(shown)

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
        """Stop the running dashboard now; the next session's launch starts it again."""

        def stopped() -> None:
            typer.echo(
                "Dashboard stopped."
                if companion.stopped(root)
                else "No dashboard was running."
            )

        refused("stop", stopped)

    return app
