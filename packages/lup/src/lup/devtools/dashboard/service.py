"""The dashboard's own process: what `harness claude|codex` starts once for every session.

Run as ``python -m lup.devtools.dashboard.service <state> <port> <revision>``
by the companion a launch holds (:class:`~lup.devtools.dashboard.companion.Dashboard`),
never by hand. It serves every repository a launch registered, answers its
launcher's health check behind its own capability, expires the reviews no
session waits on, serves each repository's setup page as a pane, and stops
what it started when it is stopped.

Its page is copied beside its state before it serves, so a dashboard started
from a worktree keeps serving after that worktree is removed.
"""

import asyncio
import os
import shutil
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from importlib import resources
from itertools import count
from pathlib import Path

from pydantic import BaseModel

from lup.devtools.dashboard.companion import (
    DashboardHealth,
    DashboardRegistry,
    DashboardToken,
)
from lup.devtools.dashboard.panes import SetupPanes


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


def serve_dashboard(arguments: ServiceArguments) -> None:
    """Serve the dashboard until it is stopped, and stop what it started with it."""
    import uvicorn
    from fastapi import FastAPI

    from lup.devtools.dashboard.reviews import ReviewStore, dashboard_app

    token = DashboardToken(directory=arguments.state).read()
    registry = DashboardRegistry(directory=arguments.state)
    panes = SetupPanes(
        registry.repositories,
        token,
        arguments.state / "logs",
        live=registry.live,
    )
    url = f"http://127.0.0.1:{arguments.port}"
    app = dashboard_app(
        url,
        token,
        (),
        discover=True,
        registry=registry,
        bundles=kept_page(arguments.state, arguments.revision),
        health=DashboardHealth(revision=arguments.revision, pid=os.getpid()),
        panes=panes,
    )
    around = app.router.lifespan_context
    sweeping = ReviewStore(roots=(), discover=True, registry=registry)

    async def retiring() -> None:
        """Every few seconds, whether or not a page is open: expire, then retire panes."""
        for _ in count():
            await asyncio.sleep(10)
            await asyncio.to_thread(sweeping.sweep)
            await asyncio.to_thread(panes.retire)

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        async with around(application):
            watcher = asyncio.create_task(retiring())
            try:
                yield
            finally:
                watcher.cancel()
                await asyncio.gather(watcher, return_exceptions=True)
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
