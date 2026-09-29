"""The dashboard's own process: what `harness claude|codex` starts once for every session.

Run as ``python -m lup.devtools.dashboard.service <state> <port> <revision>``
by the companion a launch holds (:class:`~lup.devtools.dashboard.companion.Dashboard`),
never by hand. It serves every repository a launch registered, answers its
launcher's health check behind its own capability, expires the reviews no
session waits on, serves each repository's setup page as a pane, tells the
operator when a review parks (:class:`Herald`), and stops what it started
when it is stopped.

Its page is copied beside its state before it serves, so a dashboard started
from a worktree keeps serving after that worktree is removed.
"""

import asyncio
import logging
import os
import shutil
import sys
import webbrowser
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from importlib import resources
from itertools import count
from pathlib import Path

import sh
from pydantic import BaseModel
from pydantic_settings import BaseSettings

from lup.devtools.dashboard.companion import (
    DashboardHealth,
    DashboardRegistry,
    DashboardToken,
    KnownRepository,
    read_model,
    written,
)
from lup.devtools.dashboard.panes import SetupPanes
from lup.devtools.dashboard.pulse import DashboardPulse, PulseFile
from lup.devtools.dashboard.reviews import ReviewScan, ReviewStore
from lup.devtools.review.app import RequesterPresence, ReviewSummary
from lup.policy.relay import PersistentQuestion
from lup.providers.user_config import UserConfigFile

logger = logging.getLogger(__name__)


class ServiceArguments(BaseModel, frozen=True):
    """What the companion hands the process: its state, its port, what it was built from."""

    state: Path
    port: int
    revision: str

    @classmethod
    def parsed(cls, words: list[str]) -> "ServiceArguments":
        state, port, revision = words
        return cls(state=Path(state), port=int(port), revision=revision)


def kept_page(state: Path, revision: str) -> Path:
    """The dashboard's page, copied beside its state under the revision it belongs to.

    The directory handed to the page's server as its bundles, so the page and
    its assets are read from here rather than from the checkout that started
    it, which may be removed while the dashboard serves.
    """
    bundles = state / "bundles" / revision
    target = bundles / "dashboard"
    if not (target / "index.html").is_file():
        source = resources.files("lup.web").joinpath("bundles", "dashboard")
        with resources.as_file(source) as built:
            shutil.copytree(built, target, dirs_exist_ok=True)
    return bundles


class DesktopNotice(BaseModel, frozen=True):
    """One notice on the operator's desktop: its summary line, and the body beneath."""

    summary: str
    body: str


def desktop_notified(notice: DesktopNotice) -> bool:
    """Show one notice through the freedesktop notification service, answering whether it was.

    `notify-send` is that service's own client, speaking D-Bus to whichever
    notification daemon the desktop runs. Where none is installed — a
    desktop without libnotify, a headless host, macOS — nothing is shown:
    the page, its reopening and every session's status line carry on
    without it.
    """
    program = shutil.which("notify-send")
    if program is None:
        return False
    try:
        sh.Command(program)(
            "--app-name=lup", "--", notice.summary, notice.body, _timeout=10
        )
    except (sh.ErrorReturnCode, sh.TimeoutException, OSError) as error:
        logger.warning("notify-send could not show a notice: %s", error)
        return False
    return True


class GraphicalSession(BaseSettings):
    """The display this process could open a browser window on, where it has one."""

    display: str = ""
    wayland_display: str = ""

    def present(self) -> bool:
        """Whether a browser opened from here would be one the operator sees.

        On Linux only under X or Wayland: with neither, the standard library
        falls back to a text browser, which would run inside this service,
        where no terminal draws it.
        """
        return sys.platform != "linux" or bool(self.display or self.wayland_display)


def page_reopened(url: str) -> bool:
    """Open the page in the operator's browser, answering whether one opened."""
    return GraphicalSession().present() and webbrowser.open(url)


class HeraldRecord(BaseModel, frozen=True):
    """What the dashboard has told the operator, kept across its restarts."""

    told: list[str] = []
    """The reviews a notice named, for as long as each still waits."""

    reopened: datetime | None = None
    """When it last opened the page on a review's account."""


class Waiting(BaseModel, frozen=True):
    """One review waiting on the operator, as a notice names it."""

    key: str
    title: str
    session: str
    repository: str
    checkout: str

    def said(self) -> str:
        return f"{self.title}\nasked by {self.session} in {self.checkout}"


class Herald:
    """Makes a parked review visible where no page shows it, and publishes what it counts.

    Every look reads each queue the dashboard serves — whether or not a tab
    follows the page, which the stream alone would not — and, for each
    review parked since, sends a desktop notice naming what waits and where:
    one per review, or one for the lot where more than ``crowd`` park at
    once. Where no tab follows the page, it opens the page too, at most once
    per ``quiet`` period and never where the person's lup config says
    ``[dashboard] reopen = false``. What it told is kept beside the
    dashboard's state, so a restart tells nothing twice.

    It writes the dashboard's pulse on every look where anything changed,
    and at least every ``heartbeat``, so a session reading it knows it is
    current.
    """

    def __init__(
        self,
        directory: Path,
        registry: DashboardRegistry,
        url: str,
        capability: str,
        tabs: Callable[[], int] = lambda: 0,
        config: UserConfigFile | None = None,
        notify: Callable[[DesktopNotice], bool] = desktop_notified,
        reopen: Callable[[str], bool] = page_reopened,
        quiet: timedelta = timedelta(minutes=10),
        crowd: int = 3,
        heartbeat: timedelta = timedelta(seconds=10),
    ) -> None:
        self.record_path = directory / "herald.json"
        self.pulse = PulseFile.of(directory)
        self.registry = registry
        self.store = ReviewStore(roots=(), discover=True, registry=registry)
        self.url = url
        self.capability = capability
        self.tabs = tabs
        self.config = config if config is not None else UserConfigFile()
        self.notify = notify
        self.reopen = reopen
        self.quiet = quiet
        self.crowd = crowd
        self.heartbeat = heartbeat
        self.published: DashboardPulse | None = None
        self.unheard = False

    def look(self, now: datetime | None = None) -> None:
        """Read every queue once: tell of what parked since, reopen where due, publish."""
        moment = now or datetime.now(UTC)
        scan = self.store.scan_roots()
        queues = [self.store.queue(root) for root in scan.roots]
        pending = {
            ReviewSummary.key_for(queue.root, question.id): (queue.root, question)
            for queue in queues
            for question in queue.questions
            if question.state == "pending" and not question.stale()
        }
        complete = not scan.errors and not any(queue.errors for queue in queues)
        record = read_model(self.record_path, HeraldRecord) or HeraldRecord()
        fresh = [
            self.waiting(root, question, scan)
            for key, (root, question) in pending.items()
            if key not in record.told
        ]
        reopened = record.reopened
        if fresh:
            self.announce(fresh)
            if self.reopening(moment, reopened):
                reopened = moment
        kept = [key for key in record.told if key in pending or not complete]
        updated = HeraldRecord(
            told=[*kept, *(each.key for each in fresh)], reopened=reopened
        )
        if updated != record:
            written(self.record_path, updated.model_dump_json(indent=2))
        self.publish(len(pending), moment)

    def waiting(
        self, root: Path, question: PersistentQuestion, scan: ReviewScan
    ) -> Waiting:
        """One review as a notice names it: what it asks, who asked, and where."""
        anchor = scan.repositories[root] if root in scan.repositories else root
        return Waiting(
            key=ReviewSummary.key_for(root, question.id),
            title=ReviewSummary.of(root, question, "operator").title,
            session=RequesterPresence.of(root).called(question),
            repository=KnownRepository(repository=anchor, checkout=root).name(),
            checkout=question.operation.worktree.name,
        )

    def announce(self, fresh: list[Waiting]) -> None:
        """Send the notices for what parked since the last look."""
        places = ", ".join(dict.fromkeys(each.repository for each in fresh))
        notices = (
            [
                DesktopNotice(
                    summary=f"A review waits for you in {each.repository}",
                    body=f"{each.said()}\n{self.url}",
                )
                for each in fresh
            ]
            if len(fresh) <= self.crowd
            else [
                DesktopNotice(
                    summary=f"{len(fresh)} reviews wait for you in {places}",
                    body="\n".join([*(each.title for each in fresh), self.url]),
                )
            ]
        )
        for notice in notices:
            if not self.notify(notice) and not self.unheard:
                self.unheard = True
                logger.warning(
                    "no desktop notification service took a notice (is "
                    "`notify-send` installed?); reviews still reach the page "
                    "and each session's status line"
                )

    def reopening(self, moment: datetime, reopened: datetime | None) -> bool:
        """Open the page where no tab follows it and the person has not said otherwise."""
        if self.tabs() or (reopened is not None and moment - reopened < self.quiet):
            return False
        try:
            wanted = self.config.load().dashboard.reopen
        except ValueError as unreadable:
            logger.warning("the page is not reopened: %s", unreadable)
            return False
        return wanted and self.reopen(f"{self.url}/#token={self.capability}")

    def publish(self, pending: int, moment: datetime) -> None:
        """Write the pulse where anything in it changed, or its last beat is old."""
        pulse = DashboardPulse(
            url=self.url,
            pid=os.getpid(),
            pending=pending,
            sessions=len(self.registry.launches()),
            repositories=[
                str(each.repository) for each in self.registry.repositories()
            ],
            tabs=self.tabs(),
            beat=moment,
        )
        last = self.published
        if (
            last is not None
            and pulse.model_copy(update={"beat": last.beat}) == last
            and moment - last.beat < self.heartbeat
        ):
            return
        written(self.pulse.path, pulse.model_dump_json(indent=2))
        self.published = pulse

    def retired(self) -> None:
        """Take the pulse down, so no session reads a stopped dashboard as serving."""
        self.pulse.path.unlink(missing_ok=True)


def serve_dashboard(arguments: ServiceArguments) -> None:
    """Serve the dashboard until it is stopped, and stop what it started with it."""
    import uvicorn
    from fastapi import FastAPI

    from lup.devtools.dashboard.reviews import dashboard_app
    from lup.devtools.dashboard.stream import LiveFeed

    token = DashboardToken(directory=arguments.state).read()
    registry = DashboardRegistry(directory=arguments.state)
    panes = SetupPanes(
        registry.repositories,
        token,
        arguments.state / "logs",
        live=registry.live,
    )
    url = f"http://127.0.0.1:{arguments.port}"
    feed = LiveFeed(
        registry.repositories,
        ReviewStore(roots=(), discover=True, registry=registry),
    )
    app = dashboard_app(
        url,
        token,
        (),
        discover=True,
        registry=registry,
        bundles=kept_page(arguments.state, arguments.revision),
        health=DashboardHealth(revision=arguments.revision, pid=os.getpid()),
        panes=panes,
        feed=feed,
    )
    around = app.router.lifespan_context
    sweeping = ReviewStore(roots=(), discover=True, registry=registry)
    herald = Herald(arguments.state, registry, url, token, tabs=lambda: feed.followers)

    async def retiring() -> None:
        """Every few seconds, whether or not a page is open: expire, then retire panes."""
        for _ in count():
            await asyncio.sleep(10)
            await asyncio.to_thread(sweeping.sweep)
            await asyncio.to_thread(panes.retire)

    async def heralding() -> None:
        """Every two seconds, whether or not a page is open: tell what parked, publish the pulse."""
        for _ in count():
            try:
                await asyncio.to_thread(herald.look)
            except Exception:
                logger.exception("the dashboard could not look at its queues this time")
            await asyncio.sleep(2)

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        async with around(application):
            watchers = [
                asyncio.create_task(retiring()),
                asyncio.create_task(heralding()),
            ]
            try:
                yield
            finally:
                for watcher in watchers:
                    watcher.cancel()
                await asyncio.gather(*watchers, return_exceptions=True)
                await asyncio.to_thread(herald.retired)
                await asyncio.to_thread(panes.close)

    app.router.lifespan_context = lifespan
    uvicorn.run(
        app,
        host="127.0.0.1",
        port=arguments.port,
        access_log=False,
        timeout_graceful_shutdown=2,
    )


if __name__ == "__main__":
    serve_dashboard(ServiceArguments.parsed(sys.argv[1:]))
