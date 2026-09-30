"""The dashboard's own process: what `harness claude|codex` starts once for every session.

Run as ``python -m lup.devtools.dashboard.service <state> <port> <revision>``
by the companion a launch holds (:class:`~lup.devtools.dashboard.companion.Dashboard`),
never by hand. It serves every repository a launch registered, answers its
launcher's health check behind its own capability, expires the reviews no
session waits on, serves each repository's setup page as a pane, tells the
operator when a review parks (:class:`Herald`), restarts itself in place onto
its checkout's code when that moves (:mod:`lup.devtools.dashboard.refresh`),
and stops what it started when it is stopped. ``--probe`` imports what
serving imports and exits, which is how a restart learns the new code starts.

`dashboard serve` serves a terminal's own dashboard through the same
:func:`serve_dashboard`, over a private state of its own; restarting in
place, it runs as ``python -m lup.devtools.dashboard.service --terminal
<state> <host> <port> <revision>``, keeping its port and its capability.

Its page and every asset the page names are read whole as it starts and
served from memory, so a rebuild of the bundle on disk never leaves an open
tab a page naming scripts that are gone, and a dashboard started from a
worktree keeps serving after that worktree is removed. A rebuilt bundle is
code like any other: it moves the dashboard onto it in place.

Where the sessions holding it started it again after it stopped
(:class:`~lup.launch.companions.SharedProcess`), it says why it stopped —
on the page, in every status line — for a few minutes (:class:`Recovery`).
"""

import asyncio
import importlib
import logging
import os
import shutil
import socket
import sys
import threading
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
    dashboard_revision,
    read_model,
    written,
)
from lup.devtools.dashboard.panes import SetupPanes
from lup.devtools.dashboard.pulse import DashboardPulse, PulseFile, RunningCode
from lup.devtools.dashboard.refresh import ImportedSource, Refresh, WriteGate
from lup.devtools.dashboard.reviews import ReviewScan, ReviewStore
from lup.devtools.review.app import RequesterPresence, ReviewSummary
from lup.launch.companions import CompanionSlot, lent_directory
from lup.policy.relay import RecordedQuestion
from lup.providers.user_config import UserConfigFile

logger = logging.getLogger(__name__)


class ServiceArguments(BaseModel, frozen=True):
    """What the process is handed: its state, where it listens, what it was built from, whose it is."""

    state: Path
    port: int
    revision: str
    host: str = "127.0.0.1"
    shared: bool = True
    """The dashboard every launch holds, which also tells the operator of
    parked reviews, publishes its pulse and retires the panes no session
    holds. A terminal's own (`dashboard serve`) serves the page alone, and
    removes its state when it stops."""

    @classmethod
    def parsed(cls, words: list[str]) -> "ServiceArguments":
        """Read back what :meth:`words` spelled."""
        match words:
            case ["--terminal", state, host, port, revision]:
                return cls(
                    state=Path(state),
                    port=int(port),
                    revision=revision,
                    host=host,
                    shared=False,
                )
            case [state, port, revision]:
                return cls(state=Path(state), port=int(port), revision=revision)
            case _:
                raise ValueError(
                    "expected <state> <port> <revision>, or --terminal <state> "
                    f"<host> <port> <revision>; got {words}"
                )

    def words(self) -> list[str]:
        """The arguments that serve this dashboard again, as a restart in place passes them."""
        if self.shared:
            return [str(self.state), str(self.port), self.revision]
        return ["--terminal", str(self.state), self.host, str(self.port), self.revision]

    def url(self) -> str:
        """Where it serves, credential-free."""
        authority = f"[{self.host}]" if ":" in self.host else self.host
        if self.port == 80:
            return f"http://{authority}"
        return f"http://{authority}:{self.port}"


def running_source() -> ImportedSource:
    """The lup package this process imports, and every file of the page it serves from it."""
    bundle = resources.files("lup.web").joinpath("bundles", "dashboard")
    with resources.as_file(bundle) as built:
        return ImportedSource(
            Path(__file__).parents[2],
            tuple(path for path in built.rglob("*") if path.is_file()),
        )


class Recovery:
    """Whether the sessions holding this dashboard started it again after it stopped.

    The launches holding it record each exit in the state beside this
    dashboard's own, and when they started it again; for ``window`` after
    that start, why it stopped is said on the page and in every status line,
    and how many times it was started again is published throughout.
    """

    def __init__(
        self, slot: CompanionSlot, window: timedelta = timedelta(minutes=5)
    ) -> None:
        self.slot = slot
        self.window = window

    def said(self, now: datetime | None = None) -> str:
        """Why the dashboard before this one stopped, within ``window`` of this one's start."""
        exited = self.slot.read().exited
        if exited is None or exited.restarted is None:
            return ""
        if (now or datetime.now(UTC)) - exited.restarted > self.window:
            return ""
        return exited.reason()

    def restarts(self) -> int:
        """How many times the sessions holding it started it again."""
        return self.slot.read().restarts


def probe_imports(
    modules: tuple[str, ...] = (
        "uvicorn",
        "fastapi",
        "lup.devtools.dashboard.reviews",
        "lup.devtools.dashboard.stream",
    ),
) -> None:
    """Import what serving imports beyond this module, and nothing more."""
    for module in modules:
        importlib.import_module(module)


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
    """Ask the operator's browser to open the page, answering whether one could be asked.

    On a thread of its own: a browser the standard library starts in the
    foreground — one `$BROWSER` names, say — is waited on until it exits,
    which would stall every look after it and let the pulse go stale.
    """
    if not GraphicalSession().present():
        return False
    threading.Thread(
        target=webbrowser.open, args=(url,), name="dashboard-reopen", daemon=True
    ).start()
    return True


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
    current; ``code`` is which code the dashboard runs, which the pulse says,
    and ``restarts`` how many times the sessions holding it started it again.
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
        code: Callable[[], RunningCode] = RunningCode,
        restarts: Callable[[], int] = lambda: 0,
        store: ReviewStore | None = None,
    ) -> None:
        self.record_path = directory / "herald.json"
        self.pulse = PulseFile.of(lent_directory(directory))
        self.registry = registry
        self.store = (
            store
            if store is not None
            else ReviewStore(roots=(), discover=True, registry=registry)
        )
        self.url = url
        self.capability = capability
        self.tabs = tabs
        self.code = code
        self.restarts = restarts
        self.config = config if config is not None else UserConfigFile()
        self.notify = notify
        self.reopen = reopen
        self.quiet = quiet
        self.crowd = crowd
        self.heartbeat = heartbeat
        self.published: DashboardPulse | None = None
        self.unheard = False
        self.writing = threading.Lock()
        self.stopped = False

    def look(self, now: datetime | None = None) -> None:
        """Read every queue once: tell of what parked since, reopen where due, publish."""
        moment = now or datetime.now(UTC)
        scan = self.store.scan_roots()
        queues = [self.store.queue(root) for root in scan.roots]
        pending = {
            ReviewSummary.key_for(queue.root, question.id): (queue.root, question)
            for queue in queues
            for question in queue.questions
            if question.state == "pending" and not question.overdue()
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
        self, root: Path, question: RecordedQuestion, scan: ReviewScan
    ) -> Waiting:
        """One review as a notice names it: what it asks, who asked, and where.

        Where is the checkout its files lie in, which its title's paths are
        relative to, rather than the one keeping the queue.
        """
        anchor = scan.repositories[root] if root in scan.repositories else root
        summary = self.store.summary(root, question)
        return Waiting(
            key=ReviewSummary.key_for(root, question.id),
            title=summary.title,
            session=RequesterPresence.of(root).called(question),
            repository=KnownRepository(repository=anchor, checkout=root).name(),
            checkout=Path(summary.target).name
            if summary.target
            else question.operation.worktree.name,
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
            code=self.code(),
            restarts=self.restarts(),
        )
        last = self.published
        if (
            last is not None
            and pulse.model_copy(update={"beat": last.beat}) == last
            and moment - last.beat < self.heartbeat
        ):
            return
        with self.writing:
            if self.stopped:
                return
            written(self.pulse.path, pulse.model_dump_json(indent=2))
        self.published = pulse

    def retired(self) -> None:
        """Take the pulse down for good, so no session reads a stopped dashboard as serving.

        Under the lock a look publishes under, so a look still running on its
        thread as the dashboard stops cannot put the pulse back.
        """
        with self.writing:
            self.stopped = True
            self.pulse.path.unlink(missing_ok=True)


def serve_dashboard(arguments: ServiceArguments) -> None:
    """Serve the dashboard until it is stopped, and stop what it started with it.

    Or until its checkout's code has moved past what it runs, or the operator
    asked, and no write is in flight: then it stops serving as it would for a
    stop, and replaces itself with the same command in the same process, so
    it keeps its port, its capability and its herald's record, and every tab
    reconnects. A signal that stops it is raised again once it has stopped
    serving, so a stop is never taken for a restart. Either way, every open
    stream ends as serving stops rather than being cut off after the grace.

    The dashboard every launch holds and a terminal's own (`dashboard serve`)
    are served here alike; ``arguments.shared`` adds what only the first does.
    """
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
        live=registry.live if arguments.shared else None,
    )
    url = arguments.url()
    refresh = Refresh(running_source())
    recovery = Recovery(CompanionSlot(directory=arguments.state))

    def code() -> RunningCode:
        return refresh.code().model_copy(update={"restarted": recovery.said()})

    # One store for the stream, every route, the herald and the sweep, so the
    # relays it keeps open are read once however many of them ask.
    store = ReviewStore(
        roots=(),
        discover=True,
        registry=registry,
        restarting=lambda: bool(refresh.refusal()),
    )
    feed = LiveFeed(registry.repositories, store, code=code)
    # Taken before the page is read, so a bundle rebuilt while it is read
    # moves the dashboard onto the new one rather than past it unseen.
    refresh.source.taken()
    app = dashboard_app(
        url,
        token,
        (),
        discover=True,
        registry=registry,
        health=DashboardHealth(revision=arguments.revision, pid=os.getpid()),
        panes=panes,
        feed=feed,
    )
    around = app.router.lifespan_context
    herald = (
        Herald(
            arguments.state,
            registry,
            url,
            token,
            tabs=lambda: feed.followers,
            code=code,
            restarts=recovery.restarts,
            store=store,
        )
        if arguments.shared
        else None
    )
    refresh.source.taken()
    gate = WriteGate(app, refresh.refusal)

    class Serving(uvicorn.Server):
        """uvicorn's server, ending every open stream as it begins to stop.

        A stream only ends when its tab leaves or the feed closes, so without
        this the server waits out its grace for each, then cancels them.
        """

        async def shutdown(self, sockets: list[socket.socket] | None = None) -> None:
            feed.close()
            await super().shutdown(sockets)

    server = Serving(
        uvicorn.Config(
            gate,
            host=arguments.host,
            port=arguments.port,
            access_log=False,
            timeout_graceful_shutdown=2,
        )
    )

    @app.post("/api/service/restart", status_code=202)
    def restart() -> RunningCode:
        """Restart onto the checkout's code once no write is in flight: the operator's ask."""
        logger.info("the operator asked for a restart onto its checkout's code")
        refresh.asked = True
        return refresh.code()

    async def refreshing() -> None:
        """Every two seconds: compare the code with its checkout; stop serving once due and quiet."""
        for _ in count():
            await asyncio.sleep(2)
            try:
                await asyncio.to_thread(refresh.look)
            except Exception:
                logger.exception("the dashboard could not compare its code this time")
                continue
            if refresh.due and not gate.writing:
                logger.info("restarting onto the code in %s", refresh.source.package)
                server.should_exit = True
                return

    async def retiring() -> None:
        """Every ten seconds, whether or not a page is open: expire, then retire panes."""
        for _ in count():
            await asyncio.sleep(10)
            try:
                await asyncio.to_thread(store.sweep)
                await asyncio.to_thread(panes.retire)
            except Exception:
                logger.exception(
                    "the dashboard could not sweep or retire panes this time"
                )

    async def heralding(herald: Herald) -> None:
        """Every two seconds, whether or not a page is open: tell what parked, publish the pulse."""
        for _ in count():
            try:
                await asyncio.to_thread(herald.look)
            except Exception:
                logger.exception("the dashboard could not look at its queues this time")
            await asyncio.sleep(2)

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        """The app's own, then what the service leaves behind: its pulse, unless it restarts, and its panes.

        Run as serving stops, before a signal that stopped it is raised again.
        """
        async with around(application):
            try:
                yield
            finally:
                if herald is not None and not refresh.due:
                    await asyncio.to_thread(herald.retired)
                await asyncio.to_thread(panes.close)

    async def served() -> None:
        """Serve beside the watchers, every one owned by one task group.

        The group holds each task until it ends, so none is collected while it
        runs, and each is cancelled once serving stops. A failure its own loop
        does not catch cancels serving and ends the service with its
        traceback in the log, where the next launch finds it gone and starts
        it again.
        """
        async with asyncio.TaskGroup() as group:
            watchers = [
                group.create_task(watch)
                for watch in (
                    retiring(),
                    refreshing(),
                    *([heralding(herald)] if herald is not None else []),
                )
            ]
            await server.serve()
            for watcher in watchers:
                watcher.cancel()

    app.router.lifespan_context = lifespan
    try:
        asyncio.run(served(), loop_factory=server.config.get_loop_factory())
    finally:
        if herald is not None and not refresh.due:
            herald.retired()
        panes.close()
        if not arguments.shared and not refresh.due:
            shutil.rmtree(arguments.state)
    if refresh.due:
        sys.stdout.flush()
        sys.stderr.flush()
        # lup: ignore[os-shell] — replacing this process in place is the point:
        # the same pid is what the launches' record names, which a child would not
        os.execv(
            sys.executable,
            [
                sys.executable,
                "-m",
                "lup.devtools.dashboard.service",
                *arguments.model_copy(
                    update={"revision": dashboard_revision()}
                ).words(),
            ],
        )


def said_in_output() -> None:
    """Send lup's own lines to this process's output: the shared service's log, or a terminal.

    What it says of its own restarts belongs there beside what lup says of
    stopping it. Called by whatever starts serving, never by a library.
    """
    logging.basicConfig(format="%(levelname)s:     %(name)s: %(message)s")
    logging.getLogger("lup").setLevel(logging.INFO)


if __name__ == "__main__":
    match sys.argv[1:]:
        case ["--probe"]:
            probe_imports()
        case words:
            said_in_output()
            try:
                serve_dashboard(ServiceArguments.parsed(words))
            except KeyboardInterrupt:
                # A terminal's Ctrl+C, once `dashboard serve` restarted in place
                # and this module is what runs: stopped, as asked.
                sys.exit(130)
