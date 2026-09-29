"""The operator's dashboard over the review queues of every repository it serves.

One loopback page reads every worktree of every repository it was given — the
operator's `--root`s, or every repository a launch held the dashboard for —
so a review parked by any session in any of them is one list away, grouped by
the repository and the session that asked. The page holds a capability
carried in the URL fragment, checks Host and Origin on every request, and
answers a review only against the fingerprint it displayed and the preimages
still on disk. It never runs the operation: an approval releases one exact
retry of the call that asked.

One producer serves every open tab. A checkout's queue is re-read only when
its relay changed on disk, and a settled review's row is projected once,
since nothing about it can change again — so an idle page costs a few stats
a second, however long the history behind it.

What a review *is* — its projection into files, hunks and captured
evidence — is :mod:`lup.devtools.review.app`'s; this module is what exists
because a browser reads it: the multi-checkout store, the HTTP surface and
the command that serves it.
"""

import asyncio
import hmac
import logging
import secrets
import time
import webbrowser
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from pathlib import Path
from tempfile import mkdtemp
from typing import TYPE_CHECKING

import httpx
import sh
import typer
from pydantic import BaseModel, Field

from lup.devtools.dashboard.companion import (
    Dashboard,
    DashboardHealth,
    DashboardRegistry,
    KnownRepository,
    dashboard_status,
    private_url,
    refuse_inside_a_session,
)
from lup.devtools.dashboard.panes import SetupPane, SetupPanes
from lup.devtools.review.app import (
    RequesterPresence,
    ReviewDetail,
    ReviewRoot,
    ReviewSummary,
    expire_orphaned,
    relay,
    stale_preimages,
)
from lup.devtools.review.notifications import (
    ReviewNotification,
    ReviewNotifications,
    notify_requester,
)
from lup.policy.relay import PersistentQuestion
from lup.sandbox.rail import repository_layout, sibling_worktrees
from lup.types import StringMap

if TYPE_CHECKING:
    from fastapi import BackgroundTasks, FastAPI

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
    """A decision bound to the fingerprint the browser displayed."""

    approved: bool
    note: str = ""
    fingerprint: str


class ReviewDecision(BaseModel, frozen=True):
    """The recorded answer and the result of notifying its requester."""

    review: ReviewDetail
    notification: ReviewNotification


class ReviewHeaders(BaseModel, frozen=True):
    """The request headers relevant to dashboard authentication."""

    authorization: str = ""
    origin: str = ""
    content_type: str = Field(default="", alias="content-type")


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


class RelaySignature(BaseModel, frozen=True):
    """What a relay file is on disk now; an appended record changes it."""

    size: int = -1
    modified: int = -1
    inode: int = -1

    @classmethod
    def of(cls, path: Path) -> "RelaySignature":
        try:
            status = path.stat()
        except FileNotFoundError:
            return cls()
        return cls(
            size=status.st_size, modified=status.st_mtime_ns, inode=status.st_ino
        )


class ReviewQueue(BaseModel, frozen=True):
    """A relay read whose failure does not hide other checkouts' reviews."""

    root: Path
    signature: RelaySignature = RelaySignature()
    questions: list[PersistentQuestion] = []
    errors: list[ReviewError] = []

    @classmethod
    def read(cls, root: Path) -> "ReviewQueue":
        store = relay(root)
        signature = RelaySignature.of(store.path)
        try:
            return cls(root=root, signature=signature, questions=store.questions())
        except (OSError, ValueError) as error:
            return cls(
                root=root,
                signature=signature,
                errors=[ReviewError(root=str(root), message=str(error))],
            )


def settled_key(root: Path, question: PersistentQuestion) -> str | None:
    """What a review's row is kept under, or nothing where time alone can change it.

    A pending review with an expiry turns into an expired one without a
    record being written, so its row is projected afresh each time.
    """
    if question.state == "pending" and question.expires is not None:
        return None
    answered = question.answer.model_dump_json() if question.answer else ""
    return f"{root}#{question.id}#{question.state}#{question.fingerprint}#{answered}"


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
        """One checkout's queue, re-read only where its relay changed on disk."""
        signature = RelaySignature.of(relay(root).path)
        if root in self._queues and self._queues[root].signature == signature:
            return self._queues[root]
        queue = ReviewQueue.read(root)
        self._queues[root] = queue
        return queue

    def row(self, root: Path, question: PersistentQuestion) -> ReviewSummary:
        """One review's row, projected once where nothing but a new record changes it."""
        key = settled_key(root, question)
        if key is not None and key in self._rows:
            return self._rows[key]
        summary = ReviewSummary.of(root, question, self.principal)
        if key is not None:
            self._rows[key] = summary
        return summary

    def read_root(self, root: Path, repository: Path | None = None) -> ReviewSnapshot:
        queue = self.queue(root)
        presence = RequesterPresence.of(root)
        located = ReviewRoot.of(root)
        return ReviewSnapshot(
            roots=[located.within(repository) if repository else located],
            reviews=[
                self.row(root, entry).model_copy(
                    update={"session": presence.called(entry)}
                )
                for entry in queue.questions
            ],
            errors=queue.errors,
        )

    def snapshot(self) -> ReviewSnapshot:
        scan = self.scan_roots()
        queues = [
            self.read_root(
                root, scan.repositories[root] if root in scan.repositories else None
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
        return ReviewDetail.of(located.root, located.question, self.principal)

    def answer(
        self,
        key: str,
        decision: ReviewAnswer,
        background: "BackgroundTasks | None" = None,
    ) -> ReviewDecision:
        from fastapi import HTTPException

        located = self.locate(key)
        entry = located.question
        if entry.stale():
            raise HTTPException(status_code=409, detail="Review has expired")
        if not hmac.compare_digest(
            decision.fingerprint.encode("utf-8"), entry.fingerprint.encode("utf-8")
        ):
            raise HTTPException(status_code=409, detail="Review fingerprint changed")
        if decision.approved and (reason := stale_preimages(entry)):
            raise HTTPException(status_code=409, detail=reason)
        try:
            settled = relay(located.root).answer(
                entry.id, self.principal, decision.approved, decision.note
            )
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        if settled.answer is None:
            raise HTTPException(status_code=409, detail=f"Review is {settled.state}")
        notifications = ReviewNotifications(root=located.root)
        attempt = notifications.prepare(settled)

        def deliver() -> ReviewNotification:
            return notify_requester(self.checkout_roots(), settled)

        if background is None:
            notification = notifications.complete(settled, attempt, deliver)
        else:
            background.add_task(notifications.complete, settled, attempt, deliver)
            notification = attempt.notification
        return ReviewDecision(
            review=ReviewDetail.of(located.root, settled, self.principal),
            notification=notification,
        )


class SnapshotFeed:
    """One producer per server, however many tabs follow it.

    A tab following the stream is handed the snapshot the producer last took,
    whenever it changes and at least every ``resend_after`` seconds; the
    producer runs while anybody follows, and every ``sweep_every`` ticks
    expires what no session waits on any more.
    """

    def __init__(
        self,
        store: ReviewStore,
        interval: float = 1.0,
        resend_after: float = 15.0,
        sweep_every: int = 10,
    ) -> None:
        self.store = store
        self.interval = interval
        self.resend_after = resend_after
        self.sweep_every = sweep_every
        self.encoded = ""
        self.version = 0
        self.followers = 0
        self.published = asyncio.Event()
        self.producer: asyncio.Task[None] | None = None

    async def produce(self) -> None:
        """Take a snapshot a tick while anybody follows, publishing what changed."""
        tick = 0
        while self.followers:
            if tick % self.sweep_every == 0:
                await asyncio.to_thread(self.store.sweep)
            snapshot = await asyncio.to_thread(self.store.snapshot)
            encoded = snapshot.model_dump_json()
            if encoded != self.encoded:
                self.encoded = encoded
                self.version += 1
                published, self.published = self.published, asyncio.Event()
                published.set()
            tick += 1
            await asyncio.sleep(self.interval)
        self.producer = None

    async def published_within(self, seconds: float) -> None:
        """Wait for the next snapshot published, or for ``seconds``, whichever comes first."""
        waiter = asyncio.ensure_future(self.published.wait())
        finished, _ = await asyncio.wait({waiter}, timeout=seconds)
        if not finished:
            waiter.cancel()

    async def follow(
        self, disconnected: Callable[[], Awaitable[bool]]
    ) -> AsyncIterator[str]:
        """One tab's stream: every snapshot published while it stays connected."""
        self.followers += 1
        if self.producer is None:
            self.producer = asyncio.create_task(self.produce())
        seen = 0
        sent = time.monotonic()
        try:
            while not await disconnected():
                if self.version != seen or (
                    self.version and time.monotonic() - sent >= self.resend_after
                ):
                    seen = self.version
                    sent = time.monotonic()
                    yield self.encoded + "\n"
                await self.published_within(self.interval)
        finally:
            self.followers -= 1


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
) -> "FastAPI":
    """Build the dashboard: an authenticated browser surface over the review queues.

    ``health`` is what a running dashboard answers its launcher with, and
    ``panes`` each repository's setup page; neither is served where not given.
    """
    from fastapi import BackgroundTasks, Request
    from fastapi.responses import JSONResponse, Response, StreamingResponse

    from lup.web.serve import bundle_app

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
    feed = SnapshotFeed(store)

    @app.middleware("http")
    async def authorize(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        headers = ReviewHeaders.model_validate(request.headers)
        pane = request.url.path.startswith("/setup/")
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
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "SAMEORIGIN" if pane else "DENY"
        response.headers["Content-Security-Policy"] = (
            "frame-ancestors 'self'" if pane else "frame-ancestors 'none'"
        )
        return response

    @app.get("/api/reviews")
    def reviews() -> ReviewSnapshot:
        return store.snapshot()

    @app.get("/api/reviews/{key}")
    def detail(key: str) -> ReviewDetail:
        return store.detail(key)

    @app.post("/api/reviews/{key}/answer")
    def decide(
        key: str, decision: ReviewAnswer, background_tasks: BackgroundTasks
    ) -> ReviewDecision:
        return store.answer(key, decision, background_tasks)

    @app.get("/api/events")
    async def events(request: Request) -> StreamingResponse:
        return StreamingResponse(
            feed.follow(request.is_disconnected), media_type="application/x-ndjson"
        )

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
            refuse_inside_a_session(verb)
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
        """Say whether the dashboard runs, where, and for how many sessions."""
        typer.echo(dashboard_status(companion, root).model_dump_json(indent=2))

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
