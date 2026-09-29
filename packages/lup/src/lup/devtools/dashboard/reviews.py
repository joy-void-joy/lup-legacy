"""The operator's dashboard over the review queues of the repositories it was given.

One loopback page reads every worktree of every repository the operator
named, so a review parked by any session in any of them is one list away.
The page holds a per-launch capability carried in the URL fragment, checks
Host and Origin on every request, and answers a review only against the
fingerprint it displayed and the preimages still on disk. It never runs the
operation: an approval releases one exact retry of the call that asked.

What a review *is* — its projection into files, hunks and captured
evidence — is :mod:`lup.devtools.review.app`'s; this module is what exists
because a browser reads it: the multi-checkout store, the HTTP surface and
the command that serves it.
"""

import asyncio
import hmac
import secrets
import webbrowser
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from itertools import count
from pathlib import Path
from typing import TYPE_CHECKING

import sh
import typer
from pydantic import BaseModel, Field

from lup.devtools.review.app import (
    ReviewDetail,
    ReviewRoot,
    ReviewSummary,
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

if TYPE_CHECKING:
    from fastapi import BackgroundTasks, FastAPI


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
    """Discovered checkouts and any operator-selected roots that failed."""

    roots: tuple[Path, ...] = ()
    errors: list[ReviewError] = []

    @classmethod
    def of(cls, root: Path) -> "ReviewScan":
        try:
            return cls(roots=review_roots(root, []))
        except (OSError, ValueError, sh.ErrorReturnCode) as error:
            return cls(errors=[ReviewError(root=str(root), message=str(error))])


class ReviewQueue(BaseModel, frozen=True):
    """A relay read whose failure does not hide other checkouts' reviews."""

    root: Path
    questions: list[PersistentQuestion] = []
    errors: list[ReviewError] = []

    @classmethod
    def read(cls, root: Path) -> "ReviewQueue":
        try:
            return cls(root=root, questions=relay(root).questions())
        except (OSError, ValueError) as error:
            return cls(
                root=root, errors=[ReviewError(root=str(root), message=str(error))]
            )


class ReviewStore(BaseModel, frozen=True):
    """Read only operator-selected queues and answer their exact records."""

    roots: tuple[Path, ...]
    principal: str = "operator"
    discover: bool = False

    def scan_roots(self) -> ReviewScan:
        if not self.roots or not self.discover:
            return ReviewScan(roots=self.roots)
        scans = [ReviewScan.of(root) for root in self.roots]
        return ReviewScan(
            roots=tuple(dict.fromkeys(root for scan in scans for root in scan.roots)),
            errors=[error for scan in scans for error in scan.errors],
        )

    def checkout_roots(self) -> tuple[Path, ...]:
        return self.scan_roots().roots

    def read_root(self, root: Path) -> ReviewSnapshot:
        queue = ReviewQueue.read(root)
        return ReviewSnapshot(
            roots=[ReviewRoot.of(root)],
            reviews=[
                ReviewSummary.of(root, entry, self.principal)
                for entry in queue.questions
            ],
            errors=queue.errors,
        )

    def snapshot(self) -> ReviewSnapshot:
        scan = self.scan_roots()
        queues = [self.read_root(root) for root in scan.roots]
        return ReviewSnapshot(
            roots=[root for queue in queues for root in queue.roots],
            reviews=sorted(
                (row for queue in queues for row in queue.reviews),
                key=lambda row: row.created,
                reverse=True,
            ),
            errors=scan.errors + [error for queue in queues for error in queue.errors],
        )

    def locate(self, key: str) -> LocatedReview:
        from fastapi import HTTPException

        scan = self.scan_roots()
        queues = [ReviewQueue.read(root) for root in scan.roots]
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


def dashboard_app(
    url: str, token: str, roots: tuple[Path, ...], *, discover: bool = False
) -> "FastAPI":
    """Build the dashboard: an authenticated browser surface over the review queues."""
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
    app = bundle_app("Dashboard", url, "dashboard")
    store = ReviewStore(roots=roots, discover=discover)

    @app.middleware("http")
    async def authorize(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if request.url.path.startswith("/api/"):
            headers = ReviewHeaders.model_validate(request.headers)
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
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = "frame-ancestors 'none'"
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
        async def snapshots() -> AsyncIterator[str]:
            previous = ""
            for tick in count():
                if await request.is_disconnected():
                    return
                snapshot = await asyncio.to_thread(store.snapshot)
                encoded = snapshot.model_dump_json()
                if encoded != previous or tick % 15 == 0:
                    yield encoded + "\n"
                    previous = encoded
                await asyncio.sleep(1)

        return StreamingResponse(snapshots(), media_type="application/x-ndjson")

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


def create_operator_dashboard_app(root: Path) -> typer.Typer:
    """Wire the `dashboard` group: the operator's page over the review queues."""
    app = typer.Typer(no_args_is_help=True)

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
        """Serve the dashboard over every worktree of the selected repositories."""
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
        page = dashboard_app(url, token, roots, discover=True)
        browser_url = f"{url}/#token={token}"
        typer.echo(f"Dashboard: {browser_url}")
        if open_page:
            webbrowser.open(browser_url)
        uvicorn.run(page, host=host, port=port, access_log=False)

    return app
