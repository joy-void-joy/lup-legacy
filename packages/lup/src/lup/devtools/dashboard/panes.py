"""Each repository's setup page, as a pane of the dashboard.

A repository's setup steps are its application's own code — the integrations
and steps its CLI declares — so a dashboard started from another checkout
cannot draw them. It runs that repository's own CLI instead: `setup serve`,
in a checkout of the repository, the first time its pane is opened, and serves
the page that draws under its own origin, beneath a path carrying a
capability derived from its own. The page asks for everything relative to
where it was served, so it reaches its own routes through the same path and
needs to know nothing of being a pane.

The page is the operator's, like the dashboard: started from the dashboard's
environment, and stopped with it, or once no running session of that
repository holds the dashboard any more.
"""

import asyncio
import hashlib
import hmac
import os
import socket
import threading
from collections.abc import Callable
from pathlib import Path

import sh
from pydantic import BaseModel

from lup.channels.wait import wait_until
from lup.devtools.dashboard.companion import KnownRepository
from lup.devtools.launcher import console_script
from lup.launch.companions import LiveProcess, port_answers


class SetupPane(BaseModel, frozen=True):
    """One repository's setup page as the dashboard lists it."""

    key: str
    repository: str
    """The repository, by its shared git directory."""

    name: str
    path: str
    """Where the page is served, its capability included."""


class SetupChild(BaseModel, frozen=True):
    """One repository's setup page, running: its process and the port it serves on."""

    process: LiveProcess
    port: int


def free_port() -> int:
    """A port nothing listens on at this moment, as the kernel picks one."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def setup_command(checkout: Path, port: int) -> list[str]:
    """How a repository serves its own setup page: its installed CLI, else through uv."""
    words = ["setup", "serve", "--no-open", "--port", str(port)]
    installed = console_script(checkout)
    if installed is not None:
        return [str(installed), *words]
    return ["uv", "run", "--directory", str(checkout), "lup-devtools", *words]


class SetupPanes:
    """Every repository's setup page the dashboard serves, each started when first opened.

    Mutable and shared by the requests of one server, which reach it from
    worker threads; one lock keeps two opening one pane from starting it twice.
    """

    def __init__(
        self,
        repositories: Callable[[], list[KnownRepository]],
        token: str,
        logs: Path,
        live: Callable[[Path], bool] | None = None,
        ready_within: float = 90.0,
    ) -> None:
        self.repositories = repositories
        self.token = token
        self.logs = logs
        self.live = live
        self.ready_within = ready_within
        self.children: dict[str, SetupChild] = {}
        self.lock = threading.Lock()

    def capability(self, key: str) -> str:
        """The part of a pane's path that admits it, derived from the dashboard's own."""
        return hmac.new(self.token.encode(), key.encode(), hashlib.sha256).hexdigest()[
            :32
        ]

    def admits(self, key: str, capability: str) -> bool:
        return hmac.compare_digest(capability.encode(), self.capability(key).encode())

    def listed(self) -> list[SetupPane]:
        """Every repository's pane, by the path that opens it."""
        return [
            SetupPane(
                key=known.key(),
                repository=str(known.repository),
                name=known.name(),
                path=f"/setup/{known.key()}/{self.capability(known.key())}/",
            )
            for known in self.repositories()
        ]

    def reached(self, key: str) -> int:
        """The port this repository's page serves on, starting it where nothing does.

        Refused with its log where the repository's CLI serves no setup page —
        a project that declined the setup module — or did not answer in time.
        """
        with self.lock:
            if key in self.children:
                child = self.children[key]
                if child.process.running() and port_answers(child.port):
                    return child.port
                child.process.stop(5.0)
                del self.children[key]
            known = next(
                (known for known in self.repositories() if known.key() == key), None
            )
            if known is None:
                raise LookupError(f"no repository is known as {key!r}")
            port = free_port()
            self.logs.mkdir(parents=True, exist_ok=True)
            log = self.logs / f"setup-{key}.log"
            program, *arguments = setup_command(known.checkout, port)
            with log.open("ab") as output, Path(os.devnull).open("rb") as nothing:
                running = sh.Command(program)(
                    *arguments,
                    _cwd=str(known.checkout),
                    _bg=True,
                    _bg_exc=False,
                    _new_session=True,
                    _in=nothing,
                    _out=output,
                    _err_to_out=True,
                )
            process = LiveProcess.of(running.pid)
            serving = asyncio.run(
                wait_until(
                    lambda: True if port_answers(port) else None,
                    wait_seconds=self.ready_within,
                    poll_interval_seconds=0.1,
                )
            )
            if serving is None:
                process.stop(5.0)
                raise RuntimeError(
                    f"{known.name()} served no setup page within "
                    f"{self.ready_within:g}s; its output is in {log}"
                )
            self.children[key] = SetupChild(process=process, port=port)
            return port

    def retire(self) -> None:
        """Stop each page whose repository no running session holds the dashboard for."""
        if self.live is None:
            return
        known = {each.key(): each for each in self.repositories()}
        with self.lock:
            for key in list(self.children):
                if key in known and self.live(known[key].repository):
                    continue
                self.children.pop(key).process.stop(5.0)

    def close(self) -> None:
        """Stop every page, as the dashboard serving them stops."""
        with self.lock:
            for child in self.children.values():
                child.process.stop(5.0)
            self.children.clear()
