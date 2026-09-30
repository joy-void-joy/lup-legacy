"""Host companions: what a launch keeps running on the host for as long as its session runs.

Some sessions want something beside them that is no part of the session: a
service answering the operator's questions, a preview server showing what the
session makes, a watcher rebuilding it. Declared on the agent, a companion is
held around every session the declaration opens — launched from a terminal,
printed as a command, or opened in this process — and hands that session what
it needs to reach it: environment variables, folders to mount, the ports it
listens on. :class:`HostCompanion` is that seam, and the one thing a
compilation asks of a companion is :meth:`HostCompanion.held`.

A service already running on the host's loopback — a model server, a
database — is :class:`HostService`: a container whose loopback is its own
reaches it only through a socket the launch relays for that one port, under
the name the service is declared by.

Most companions are one process shared by many sessions, which is what
:class:`SharedProcess` is: started by the first session that needs it, joined
by every later one, and stopped when the last lets go. Shared per checkout,
each checkout's sessions reach a companion of their own, serving that
checkout's files on ports of its own; shared per person, every session they
launch anywhere reaches one. Each holder keeps a lease, taken as its
launcher's process, so a launcher that died without letting go is swept by
the next one that looks, and a companion outlives nobody's session.

A companion runs on the host, as the operator, outside every boundary the
session has, which is the point of it and the reason it is declared where a
review of the declaration reads it.
"""

import asyncio
import fcntl
import hashlib
import logging
import os
import shutil
import signal
import socket
import tempfile
import threading
import traceback
import uuid
from abc import ABC, abstractmethod
from collections import deque
from collections.abc import AsyncIterator, Callable, Iterator, Sequence
from contextlib import (
    AbstractContextManager,
    ExitStack,
    asynccontextmanager,
    contextmanager,
)
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Annotated

import sh
from pydantic import BaseModel, Field, StringConstraints, ValidationError

from lup.channels.wait import wait_until
from lup.harness.notice import Notice
from lup.launch.declaration import Loopback, Mount
from lup.launch.refusal import LaunchRefused
from lup.launch.secrets import HostSecrets
from lup.observability.audit import TraceJournal
from lup.sandbox.known import store_directory
from lup.sandbox.process import process_is_alive, process_start_token
from lup.types import EnvVars, JsonObject

logger = logging.getLogger(__name__)

type CompanionName = Annotated[
    str, StringConstraints(pattern=r"^[a-z][a-z0-9_-]*$", max_length=64)
]
"""What a companion is called: in what a launch says of it, and in its state's path."""

type PortName = Annotated[
    str, StringConstraints(pattern=r"^[a-z][a-z0-9_-]*$", max_length=64)
]
"""What a companion calls one of the ports it listens on."""

type PortNumber = Annotated[int, Field(ge=1, le=65535)]
"""One TCP port on the host's loopback."""

type VariableName = Annotated[str, StringConstraints(pattern=r"^[A-Z_][A-Z0-9_]*$")]
"""An environment variable's name, as every shell exports one."""

# lup: ignore[constant-declaration] — the prefix the image's entrypoint reads
# a relayed service under, which the launch writing it has to agree with
RELAY_PREFIX = "LUP_HOST_SERVICE_"
"""What each relayed host service's variable is named after, in the session's environment."""


class CompanionLaunch(BaseModel, frozen=True, arbitrary_types_allowed=True):
    """The session a companion is held for, as the compilation opening it knows it."""

    root: Path
    """The checkout the session works in."""

    runtime: str
    """The runtime opening the session, by its launch spelling."""

    environment: EnvVars
    """The environment the session starts from, for a companion started from it."""

    journal: TraceJournal | None = None
    """The run's journal, where the session is recorded; ``None`` where nothing
    is, such as a command printed rather than run."""

    loopback: Loopback = Loopback.HOST
    """Whose loopback the session reaches, which decides whether a service on
    the host's is reached itself, relayed, or refused."""


class StatusLine(BaseModel, frozen=True):
    """A command whose output a session's own status line shows, where its runtime draws one.

    Run by the runtime rather than the agent, at every render and again every
    ``refresh`` seconds while nothing else moves, so it must answer in a
    fraction of a second.
    """

    argv: list[str]
    refresh: int = Field(default=15, ge=1)
    """Seconds between runs while the session is otherwise idle."""


class Contribution(BaseModel, frozen=True):
    """What one held companion hands the session beside it."""

    environment: EnvVars = {}
    """Variables the session runs with, a contained one's carried into its container."""

    mounts: list[Mount] = []
    """Folders the session reaches, beside those its sandbox declares."""

    ports: dict[PortName, PortNumber] = {}
    """The ports the companion listens on, by its own names for them."""

    notices: list[Notice] = []
    """What the launch says of the companion as the session opens."""

    status_line: StatusLine | None = None
    """What the session's status line shows, where its runtime draws one."""


class ReachedPort(BaseModel, frozen=True):
    """One port a held companion listens on, named by the companion and its own name for it."""

    companion: CompanionName
    name: PortName
    port: PortNumber


class Joined(BaseModel, frozen=True):
    """Every held companion's contribution, gathered for one session."""

    environment: EnvVars = {}
    mounts: list[Mount] = []
    ports: list[ReachedPort] = []
    notices: list[Notice] = []
    status_line: StatusLine | None = None

    @classmethod
    def of(cls, contributions: dict[str, Contribution]) -> "Joined":
        """Gather contributions by the companion making each, refusing two claiming one variable.

        Two companions exporting one name would leave the session reaching
        whichever was held last, which no declaration said, so the pair is
        refused naming both; so are two contributing the one status line a
        session shows.
        """
        showing = [
            companion
            for companion, each in contributions.items()
            if each.status_line is not None
        ]
        if len(showing) > 1:
            raise LaunchRefused(
                f"host companions {', '.join(showing)} all contribute a status "
                "line, and a session shows one: drop it from all but one"
            )
        for variable in sorted(
            {name for each in contributions.values() for name in each.environment}
        ):
            exporters = [
                companion
                for companion, each in contributions.items()
                if variable in each.environment
            ]
            if len(exporters) > 1:
                raise LaunchRefused(
                    f"host companions {', '.join(exporters)} all export {variable}; "
                    "a session reaches only one of them through it, so rename "
                    "the variable in all but one"
                )
        return cls(
            environment={
                variable: value
                for each in contributions.values()
                for variable, value in each.environment.items()
            },
            mounts=[mount for each in contributions.values() for mount in each.mounts],
            ports=[
                ReachedPort(companion=companion, name=name, port=port)
                for companion, each in contributions.items()
                for name, port in each.ports.items()
            ],
            notices=[
                notice for each in contributions.values() for notice in each.notices
            ],
            status_line=next(
                (contributions[companion].status_line for companion in showing), None
            ),
        )


class HostCompanion(BaseModel, ABC, frozen=True, extra="forbid"):
    """Something held on the host for as long as a session runs, handing it what reaches it."""

    name: CompanionName

    @abstractmethod
    def held(self, launch: CompanionLaunch) -> AbstractContextManager[Contribution]:
        """Hold this companion for one session: start or join it, and hand back what it gives.

        Entered before the session's command is compiled, since what it
        contributes is part of that command, and left once the session ends,
        however it ended.
        """


def named_apart(companions: Sequence[HostCompanion]) -> None:
    """Refuse two companions under one name, whose state and contributions would collide."""
    names = [companion.name for companion in companions]
    if len(names) != len(dict.fromkeys(names)):
        raise ValueError(f"host companions must be named apart, got {names}")


@contextmanager
def held_companions(
    companions: Sequence[HostCompanion], launch: CompanionLaunch
) -> Iterator[Joined]:
    """Hold every declared companion around one session, in the order declared.

    Each is let go in the reverse order, the way nested ``with`` blocks
    unwind, and every one already held is let go when a later one refuses.
    """
    with ExitStack() as stack:
        yield Joined.of(
            {
                companion.name: stack.enter_context(companion.held(launch))
                for companion in companions
            }
        )


@asynccontextmanager
async def held_around(
    companions: Sequence[HostCompanion], launch: CompanionLaunch
) -> AsyncIterator[Joined]:
    """:func:`held_companions` for a session opened in this process's event loop.

    Starting a process and waiting for it to answer blocks, so each half runs
    on a worker thread rather than stalling the loop the session runs on.
    """
    stack = ExitStack()
    joined = await asyncio.to_thread(
        stack.enter_context, held_companions(companions, launch)
    )
    try:
        yield joined
    finally:
        await asyncio.to_thread(stack.close)


def piped(source: socket.socket, sink: socket.socket) -> None:
    """Carry one direction of a relayed connection until its source ends, then end the sink's."""
    try:
        while data := source.recv(65536):
            sink.sendall(data)
    except OSError as closed:
        logger.debug("a relayed connection closed: %s", closed)
    try:
        sink.shutdown(socket.SHUT_WR)
    except OSError as closed:
        logger.debug("a relayed connection was already gone: %s", closed)


@contextmanager
def relaying(listening_at: Path, port: int) -> Iterator[None]:
    """Forward every connection to a Unix socket at ``listening_at`` to ``port`` on the host's loopback.

    For as long as it is held, on threads of this process: the launcher's,
    which outlives the session it relays for. Each connection is its own pair
    of pipes to one fresh connection to the port, and nothing else on the
    host's loopback is reachable through it. Let go, it stops listening and
    closes every connection still open.
    """
    listening = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listening.bind(str(listening_at))
    listening.listen()
    open_connections: list[socket.socket] = []

    def carried(client: socket.socket) -> None:
        try:
            upstream = socket.create_connection(("127.0.0.1", port))
        except OSError as refused:
            logger.info(
                "host service on port %s refused a relayed connection: %s",
                port,
                refused,
            )
            client.close()
            return
        open_connections.extend([client, upstream])
        back = threading.Thread(target=piped, args=(upstream, client), daemon=True)
        back.start()
        piped(client, upstream)
        back.join()
        client.close()
        upstream.close()

    def accepted() -> None:
        while True:
            try:
                client, _ = listening.accept()
            except OSError:
                return
            threading.Thread(target=carried, args=(client,), daemon=True).start()

    threading.Thread(target=accepted, daemon=True).start()
    try:
        yield
    finally:
        listening.close()
        for connection in open_connections:
            connection.close()


class HostService(HostCompanion, frozen=True):
    """A service on the host's loopback, reached from a session only through its declared name.

    Nothing is started: the service is the operator's, running on the host
    already. The session is handed its address under the variable the
    project names. On the host that is the service's own. A container whose
    loopback is its own cannot reach the host's, and its egress proxy refuses
    it, so the launcher listens on a socket of this service's, mounts it into
    the container and forwards what arrives to this one port; the image's
    entrypoint binds the same address inside to that socket. Nothing else on
    the host's loopback is reachable that way. A container joined to no
    network is refused one: the relay would be its way out.
    """

    port: PortNumber
    """Where the service listens on the host's loopback, and the port the
    session reaches it at inside a container."""

    variable: VariableName
    """The variable the session reads the service's address from."""

    scheme: str = Field(default="http", pattern=r"^[a-z][a-z0-9+.-]*$")
    """How the address is spelled for whatever reads it: ``http``, ``postgres``."""

    def address(self) -> str:
        """Where the session reaches the service, on the host or inside its container."""
        return f"{self.scheme}://127.0.0.1:{self.port}"

    def relay_variable(self) -> str:
        """The variable the entrypoint finds this service's socket and port in."""
        return RELAY_PREFIX + "".join(
            "_" if character == "-" else character.upper() for character in self.name
        )

    @contextmanager
    def held(self, launch: CompanionLaunch) -> Iterator[Contribution]:
        if launch.loopback is Loopback.SEALED:
            raise LaunchRefused(
                f"Host service {self.name} is declared, and this session's "
                'container joins no network (network "none"): relaying '
                f"127.0.0.1:{self.port} into it would be the one way through "
                "that wall. Give the container a network, or drop the service "
                "from this declaration."
            )
        if launch.loopback is Loopback.HOST:
            yield Contribution(environment={self.variable: self.address()})
            return
        directory = Path(tempfile.mkdtemp(prefix=f"lup-service-{self.name}-"))
        listening_at = directory / f"{self.name}.sock"
        try:
            with relaying(listening_at, self.port):
                yield Contribution(
                    environment={
                        self.variable: self.address(),
                        self.relay_variable(): f"{listening_at}@{self.port}",
                    },
                    mounts=[Mount(path=directory, writable=True)],
                    notices=[
                        Notice(
                            text=(
                                f"Host service {self.name}: 127.0.0.1:{self.port} "
                                f"relayed into the container as {self.variable}"
                            ),
                            urgency="detail",
                        )
                    ],
                )
        finally:
            shutil.rmtree(directory)


class CompanionScope(StrEnum):
    """Which sessions share one running companion."""

    CHECKOUT = "checkout"
    """Every session in one checkout; another checkout gets a companion of its own."""

    USER = "user"
    """Every session the person launches, in whichever checkout."""


class CompanionPlace(BaseModel, frozen=True):
    """Where one running companion is: its private directory, and the ports it was given."""

    state: Path
    """A directory of its own, outside every checkout, kept while it runs and after."""

    ports: dict[PortName, PortNumber] = {}
    """The port each of its names was given, which it listens on."""


class CompanionProcess(BaseModel, frozen=True):
    """How a shared companion is started: its program, where, and what it adds."""

    argv: list[str] = Field(min_length=1)
    """The program and its arguments, run without a shell."""

    cwd: Path
    environment: EnvVars = {}
    """Variables set over the environment of the session that starts it."""

    unset: list[str] = []
    """Variables of that session's environment it must not inherit.

    A companion outlives the session that happened to start it and serves
    every other, so what names that one session — its identity, its
    boundary — is no part of the companion's own environment.
    """

    def started_from(self, environment: EnvVars) -> EnvVars:
        """The environment it runs with, started from a session's."""
        merged = {**environment, **self.environment}
        return {name: value for name, value in merged.items() if name not in self.unset}


def stat_fields(pid: int) -> list[str]:
    """The fields of ``pid``'s /proc stat from its state on; none where unreadable."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        return []
    # lup: ignore[string-split] — /proc stat's fields after the command's name
    fields = stat.rpartition(")")[2].split()
    return fields


def zombie(pid: int) -> bool:
    """Whether ``pid`` has exited and waits on a parent that has not collected it."""
    fields = stat_fields(pid)
    return bool(fields) and fields[0] == "Z"


def exit_status(pid: int, started: str | None) -> int | None:
    """How the process that started at ``started`` ended, while it waits to be collected.

    Its exit code, or the negated signal that ended it, as
    ``os.waitstatus_to_exitcode`` spells it: a zombie's own stat carries its
    wait status (field 52) until its parent collects it. Nothing where it
    runs, was collected already, or the pid names another process now.
    """
    fields = stat_fields(pid)
    if len(fields) < 50 or fields[0] != "Z":
        return None
    if started is not None and fields[19] != started:
        return None
    return os.waitstatus_to_exitcode(int(fields[49]))


class LiveProcess(BaseModel, frozen=True):
    """A process by its id and when it started, so a reused id is not taken for it."""

    pid: int
    started: str | None = None

    @classmethod
    def of(cls, pid: int) -> "LiveProcess":
        """The process running under ``pid`` now."""
        return cls(pid=pid, started=process_start_token(pid))

    def running(self) -> bool:
        """Whether this same process still runs, rather than waiting to be collected."""
        return process_is_alive(self.pid, self.started) and not zombie(self.pid)

    def missing(self) -> str:
        """Why this process is judged gone, as a log line says it; nothing while it runs."""
        if self.running():
            return ""
        current = process_start_token(self.pid)
        if current is None:
            return f"no process has pid {self.pid}"
        if self.started is not None and current != self.started:
            return (
                f"pid {self.pid} names another process now, started at tick "
                f"{current} rather than {self.started}"
            )
        return f"pid {self.pid} exited and waits to be collected"

    def collected(self) -> int | None:
        """How it ended, where anything can still say: collected where it is ours, else read off its zombie.

        Its exit code, or the negated signal that ended it. A process whose
        parent already collected it — a launcher gone, which left it to init —
        leaves nothing to say it.
        """
        waiting = exit_status(self.pid, self.started)
        try:
            collected, status = os.waitpid(self.pid, os.WNOHANG)
        except ChildProcessError:
            return waiting
        return os.waitstatus_to_exitcode(status) if collected == self.pid else waiting

    def stop(self, grace: float) -> None:
        """Stop it and everything it started: a terminate to its group, then a kill.

        It leads a process group of its own, so the group is signalled even
        where its leader has already gone. An id now belonging to another
        process says the group it led has gone too.
        """
        current = process_start_token(self.pid)
        if current is not None and self.started is not None and current != self.started:
            return
        for sent in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(self.pid, sent)
            except (ProcessLookupError, PermissionError):
                return
            gone = asyncio.run(
                wait_until(
                    lambda: True if self.gone() else None,
                    wait_seconds=grace,
                    poll_interval_seconds=0.05,
                )
            )
            if gone:
                return

    def gone(self) -> bool:
        """Whether nothing of its group is left, collecting its leader where it is ours."""
        try:
            os.waitpid(self.pid, os.WNOHANG)
        except ChildProcessError:
            logger.debug("host companion %s is not this launcher's child", self.pid)
        try:
            os.killpg(self.pid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            return False
        return False


class Lease(BaseModel, frozen=True):
    """One session's hold on a shared companion, kept while its launcher runs."""

    id: str
    holder: LiveProcess


class GivenPort(BaseModel, frozen=True):
    """One port a companion was given for one of its names."""

    name: PortName
    preferred: PortNumber
    port: PortNumber


class Running(BaseModel, frozen=True):
    """The process a shared companion runs as, and the declaration it was started from."""

    process: LiveProcess
    declared: JsonObject
    since: datetime | None = None
    """When it was started."""

    output: int = 0
    """Where its output begins in the companion's log, which every run appends to."""


class CompanionStop(BaseModel, frozen=True):
    """Lup stopping what ran, recorded in the slot and its log before the signal is sent."""

    at: datetime
    process: LiveProcess
    """What it stopped."""

    why: str
    """The path that stopped it, naming the lease or launch it acted for."""

    by: int
    """The process that stopped it: a launcher, or the operator's command."""

    leases: int = 0
    """The live leases it counted as it stopped it."""

    stays: bool = False
    """Whether it stays stopped: the operator's own stop, which the launches
    holding it leave alone until a launch or a restart starts it again. Every
    other stop they start it again after."""


def ended(status: int | None) -> str:
    """How a process ended, in words, from its exit code or negated signal."""
    if status is None:
        return "it exited, and how is not known: it was no holder's to collect"
    if status >= 0:
        return "it exited cleanly" if status == 0 else f"it exited with status {status}"
    try:
        name = signal.Signals(-status).name
    except ValueError:
        name = f"signal {-status}"
    return f"it was ended by {name}"


class CompanionExit(BaseModel, frozen=True):
    """What ran, found gone while launches still held it, recorded before it is started again."""

    process: LiveProcess
    at: datetime
    """When a holder, or a launch joining, found it gone."""

    ran: float | None = None
    """How many seconds it had run, where its start is known."""

    status: int | None = None
    """Its exit code, or the negated signal that ended it; ``None`` where
    nothing could collect it any more."""

    stopped: CompanionStop | None = None
    """Lup's own stop of it, where lup stopped it."""

    log: Path
    """The companion's log, which holds this run's output whole."""

    offset: int = 0
    """Where this run's output begins in ``log``."""

    tail: list[str] = []
    """The last lines of that output."""

    restarted: datetime | None = None
    """When it was started again after this, once it answered."""

    def reason(self) -> str:
        """Why it stopped, in one line: lup's stop where lup made one, else how it ended.

        With what it said last, where it ended by itself or nobody knows how:
        a process a signal ended said nothing of why.
        """
        if self.stopped is not None:
            return f"lup stopped it: {self.stopped.why}"
        if self.status is not None and self.status < 0:
            return ended(self.status)
        said = next((line for line in reversed(self.tail) if line.strip()), "")
        return ended(self.status) + (f"; it last said: {said}" if said else "")


class CompanionState(BaseModel, frozen=True):
    """What is kept of one shared companion between the launches holding it."""

    checkout: Path | None = None
    """The checkout it serves, for a companion shared per checkout."""

    ports: list[GivenPort] = []
    running: Running | None = None
    leases: list[Lease] = []
    stopped: CompanionStop | None = None
    """The last time lup stopped what ran, and why."""

    exited: CompanionExit | None = None
    """The last time what ran was found gone while launches held it."""

    restarts: int = 0
    """How many times it was started again after that, since launches began holding it."""

    failing: int = 0
    """Exits in a row, each soon after the start before it: how far along its
    backoff the next start waits."""

    retry: datetime | None = None
    """When the launches holding it start it again, where it is gone and not started yet."""

    def given(self) -> dict[PortName, PortNumber]:
        """The ports it was given, by its names for them."""
        return {port.name: port.port for port in self.ports}

    def holds(self, lease: Lease) -> bool:
        """Whether ``lease`` is among those kept."""
        return any(held.id == lease.id for held in self.leases)

    def stays_stopped(self) -> bool:
        """Whether what ran was stopped to stay stopped, and nothing has started since."""
        stop = self.stopped
        running = self.running
        return (
            stop is not None
            and stop.stays
            and running is not None
            and stop.process == running.process
        )


class Started(BaseModel, frozen=True):
    """One start of a shared companion: the state it leaves, and whether what it started answered."""

    state: CompanionState
    answered: bool


class Reaped(BaseModel, frozen=True):
    """How a companion's process ended, as the launcher that started it collected it."""

    pid: int
    status: int
    """Its exit code, or the negated signal that ended it."""

    at: datetime


class CompanionSlot(BaseModel, frozen=True):
    """One shared companion's directory: its state, its lock, its log."""

    directory: Path

    def reaped(self, command: sh.RunningCommand, success: bool, status: int) -> None:
        """Keep how the process a launch started ended, as that launch's ``sh`` collected it.

        ``sh`` calls it in the launcher that started the process, whose own
        thread collects its child within a second of its end — often before
        any holder looks — so what it learned is written where every holder
        reads it. Outside the slot's lock, so under a name of its own first.
        """
        del success
        record = Reaped(pid=command.pid, status=status, at=datetime.now(UTC))
        staged = self.directory / f"reaped.json.{uuid.uuid4().hex}"
        staged.write_text(record.model_dump_json(), encoding="utf-8")
        staged.replace(self.directory / "reaped.json")

    def ended(self, running: Running) -> int | None:
        """How what ran ended: collected now where it waits to be, else as its launcher collected it."""
        status = running.process.collected()
        if status is not None:
            return status
        try:
            reaped = Reaped.model_validate_json(
                (self.directory / "reaped.json").read_bytes()
            )
        except (OSError, ValidationError):
            return None
        if reaped.pid != running.process.pid:
            return None
        if running.since is not None and reaped.at < running.since:
            return None
        return reaped.status

    def log(self) -> Path:
        """Where the companion's output goes, outside the terminal the session takes over."""
        return self.directory / "output.log"

    def logged(self) -> int:
        """How much the log holds: where the next run's output begins."""
        try:
            return self.log().stat().st_size
        except FileNotFoundError:
            return 0

    def noted(self, said: str) -> None:
        """Write one line of lup's own into the log, among what the companion writes there.

        So whoever reads the log for why the companion stopped reads, beside
        its own last words, what lup did to it and on whose account.
        """
        self.directory.mkdir(parents=True, exist_ok=True)
        with self.log().open("a", encoding="utf-8") as log:
            log.write(
                f"lup {datetime.now(UTC):%Y-%m-%dT%H:%M:%SZ} pid {os.getpid()}: {said}\n"
            )

    def tail(self, offset: int, lines: int) -> list[str]:
        """The last ``lines`` lines of the output written from ``offset`` on."""
        try:
            with self.log().open("rb") as log:
                log.seek(offset)
                written = log.read().decode(errors="replace")
        except FileNotFoundError:
            return []
        return list(deque(written.splitlines(), maxlen=lines))

    @contextmanager
    def locked(self) -> Iterator[None]:
        """Hold this companion for one launch's read, decide and write."""
        self.directory.mkdir(parents=True, exist_ok=True)
        with (self.directory / "lock").open("a", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def read(self) -> CompanionState:
        """What is kept of this companion; nothing, where nothing is or it does not parse."""
        path = self.directory / "state.json"
        if not path.is_file():
            return CompanionState()
        try:
            return CompanionState.model_validate_json(path.read_bytes())
        except ValidationError as error:
            logger.warning("host companion state %s starts over: %s", path, error)
            return CompanionState()

    def write(self, state: CompanionState) -> None:
        """Replace the state in one rename, so no reader meets half of it."""
        path = self.directory / "state.json"
        staged = path.with_name("state.json.tmp")
        staged.write_text(state.model_dump_json(indent=2), encoding="utf-8")
        staged.replace(path)


def companions_home() -> Path:
    """Where every shared companion keeps its state: lup's own state, per person."""
    return store_directory() / "companions"


def port_free(port: int) -> bool:
    """Whether something could listen on this port of the host's loopback now."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def port_answers(port: int, within: float = 0.5) -> bool:
    """Whether something accepts a connection on this port of the host's loopback."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=within):
            return True
    except OSError:
        return False


def given_ports(
    wanted: dict[PortName, PortNumber],
    kept: list[GivenPort],
    claimed: list[int],
    free: Callable[[int], bool] = port_free,
) -> list[GivenPort]:
    """The port each wanted name is given: what it had, else the preferred, else the next free.

    What a name was given before stands while it is still preferred as it was
    then and still free; otherwise its preferred port where free, else the
    next free one above it — never one ``claimed`` by another companion, or
    given to an earlier name here.
    """
    before = {port.name: port for port in kept}
    taken = {*claimed}

    def chosen(name: str, preferred: int) -> GivenPort:
        standing = before.get(name)
        port = (
            standing.port
            if standing is not None
            and standing.preferred == preferred
            and standing.port not in taken
            and free(standing.port)
            else next(
                (
                    candidate
                    for candidate in range(preferred, 65536)
                    if candidate not in taken and free(candidate)
                ),
                preferred,
            )
        )
        taken.add(port)
        return GivenPort(name=name, preferred=preferred, port=port)

    return [chosen(name, preferred) for name, preferred in wanted.items()]


@contextmanager
def choosing_ports(home: Path) -> Iterator[None]:
    """Hold port choosing for every companion, so two are never given one port."""
    home.mkdir(parents=True, exist_ok=True)
    with (home / "ports.lock").open("a", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def kept_elsewhere(home: Path, slot: CompanionSlot) -> list[int]:
    """Every port another companion keeps, where the checkout it serves still exists."""
    return [
        given.port
        for path in home.glob("*/*/state.json")
        if path.parent != slot.directory
        for state in [CompanionSlot(directory=path.parent).read()]
        if state.checkout is None or state.checkout.is_dir()
        for given in state.ports
    ]


class SharedProcess(HostCompanion, ABC, frozen=True):
    """A companion that is one process on the host, shared by the sessions its scope names.

    The first session to hold it starts it, on the ports it prefers where
    they are free and the next free ones where not; every later session joins
    the one running. What runs is replaced for the session asking when its
    process has gone, when a port it listens on stops answering, or when the
    declaration it was started from is not this one — the sessions already
    holding it keep their leases on the replacement. It stops once no lease
    is left.

    While any lease is held, the holders keep it running. Each looks every
    ``watched_every`` seconds, and whichever finds it gone first records how
    it ended, then starts it again from the declaration it launched with, on
    the ports it had where they are still free — waiting longer after each
    exit in a row, and never less than the first step of ``backoff``. Only a
    stop the operator asked to stay (``stopped``) is left alone, until a
    launch or a restart starts it. Every stop lup makes is written, with why
    and how many live leases held it, into the companion's log and its state
    before the signal is sent.
    """

    scope: CompanionScope = CompanionScope.CHECKOUT
    """Which sessions share one running process."""

    ports: dict[PortName, PortNumber] = {}
    """The ports it listens on, by name, each the one it prefers."""

    ready_within: float = Field(default=10.0, gt=0)
    """How long a started process has to answer before the launch is refused."""

    grace: float = Field(default=5.0, gt=0)
    """How long a stopped process has to exit before it is killed."""

    secrets: list[VariableName] = []
    """The keys of the project's host store this process is started with.

    Only those: the store's values leave it for the companions naming them,
    and a launched session holds none. A key the store lacks refuses the
    launch, naming where it is set."""

    backoff: tuple[float, ...] = Field(default=(1.0, 5.0, 30.0, 60.0), min_length=1)
    """Seconds the holders wait before starting it again after each exit in a
    row, the last repeating. An exit after a run longer than the last starts
    the row over."""

    watched_every: float = Field(default=1.0, gt=0)
    """Seconds between each holder's looks at whether it still runs."""

    exit_lines: int = Field(default=40, ge=0)
    """How many of its last lines of output an exit's record keeps; its log keeps every one."""

    @abstractmethod
    def process(self, place: CompanionPlace, root: Path) -> CompanionProcess:
        """How to start it at ``place``, for the session in the checkout at ``root``."""

    @abstractmethod
    def contribution(self, place: CompanionPlace, root: Path) -> Contribution:
        """What a session in the checkout at ``root`` is handed of it, running at ``place``."""

    def answers(self, place: CompanionPlace) -> bool:
        """Whether it serves: every port it was given accepts a connection."""
        return all(port_answers(port) for port in place.ports.values())

    def slot(self, root: Path) -> CompanionSlot:
        """This companion's directory, for a session in the checkout at ``root``."""
        match self.scope:
            case CompanionScope.USER:
                shared = "user"
            case CompanionScope.CHECKOUT:
                digest = hashlib.sha256(str(root.resolve()).encode()).hexdigest()
                shared = f"checkout-{digest[:16]}"
        return CompanionSlot(directory=companions_home() / shared / self.name)

    @contextmanager
    def held(self, launch: CompanionLaunch) -> Iterator[Contribution]:
        """Hold it for one session, and keep it running while held: the watch ends before the lease."""
        slot = self.slot(launch.root)
        lease = Lease(id=uuid.uuid4().hex, holder=LiveProcess.of(os.getpid()))
        with slot.locked():
            place = self.joined(slot, launch, lease)
        letting_go = threading.Event()
        watching = threading.Thread(
            target=self.supervised,
            args=(slot, launch, lease, letting_go),
            name=f"supervising {self.name}",
            daemon=True,
        )
        watching.start()
        try:
            yield self.contribution(place, launch.root)
        finally:
            letting_go.set()
            watching.join()
            with slot.locked():
                self.released(slot, lease)

    def joined(
        self, slot: CompanionSlot, launch: CompanionLaunch, lease: Lease
    ) -> CompanionPlace:
        """Join what runs where it still serves this declaration, else start it; lease it.

        What runs and does not serve is stopped first, saying why; one found
        gone while launches held it is recorded as exited. A start that fails
        refuses this launch, with the state written first, so the launches
        already holding it start it again.
        """
        now = datetime.now(UTC)
        state = slot.read()
        leases = self.live(slot, state.leases)
        state = state.model_copy(
            update={"leases": leases}
            if leases
            else {"leases": leases, "restarts": 0, "failing": 0, "retry": None}
        )
        place = CompanionPlace(state=slot.directory, ports=state.given())
        unserved = self.unserved(state.running, place)
        if unserved:
            state = self.vacated(
                slot, state, f"a launch in {launch.root} replaces it: {unserved}", now
            )
            try:
                start = self.started(slot, launch, state)
            except LaunchRefused:
                slot.write(state)
                raise
            if not start.answered:
                slot.write(start.state)
                raise LaunchRefused(
                    f"host companion {self.name!r} did not answer within "
                    f"{self.ready_within:g}s; its output is in {slot.log()}"
                )
            state = self.resumed(start.state, now)
            place = CompanionPlace(state=slot.directory, ports=state.given())
        slot.write(
            state.model_copy(
                update={
                    "checkout": (
                        launch.root.resolve()
                        if self.scope is CompanionScope.CHECKOUT
                        else None
                    ),
                    "leases": [*leases, lease],
                }
            )
        )
        return place

    def unserved(
        self, running: Running | None, place: CompanionPlace, patience: float = 5.0
    ) -> str:
        """Why what runs cannot serve a launch of this declaration; nothing where it serves.

        One of this declaration that does not answer at once is given
        ``patience`` seconds before it is judged not to: stopping one that is
        only busy would cost every session holding it its service.
        """
        if running is None:
            return "nothing runs"
        missing = running.process.missing()
        if missing:
            return f"it is gone: {missing}"
        if running.declared != self.model_dump(mode="json"):
            return "it was started from another declaration"
        if answering(lambda: self.answers(place), patience):
            return ""
        return f"it did not answer within {patience:g}s"

    def started(
        self, slot: CompanionSlot, launch: CompanionLaunch, state: CompanionState
    ) -> Started:
        """Start it from this launch's declaration, and wait until it serves.

        On the ports it had where they are still free, chosen and started
        under the lock every companion chooses under. One that does not
        answer within ``ready_within`` is stopped, saying so, and stays named
        as what ran, so the launches holding it record its exit and try again.
        """
        home = companions_home()
        with choosing_ports(home):
            given = given_ports(
                dict(self.ports), state.ports, kept_elsewhere(home, slot)
            )
            place = CompanionPlace(
                state=slot.directory, ports={port.name: port.port for port in given}
            )
            running = self.spawned(place, launch, slot)
            state = state.model_copy(update={"ports": given, "running": running})
            if answering(lambda: self.answers(place), self.ready_within):
                return Started(state=state, answered=True)
            stopped = self.halted(
                slot,
                state,
                running.process,
                f"it did not answer within {self.ready_within:g}s of starting, "
                f"from the launch in {launch.root}",
            )
            return Started(state=stopped, answered=False)

    def spawned(
        self, place: CompanionPlace, launch: CompanionLaunch, slot: CompanionSlot
    ) -> Running:
        """Start it in a session of its own, without waiting for it: what runs, and where its output begins.

        A session of its own, so the signal that stops it reaches whatever it
        started too, with nothing on its input, since the launch starting it
        may end long before it does, and its output in a log beside its state.
        """
        command = self.process(place, launch.root)
        program, *arguments = command.argv
        stored = HostSecrets.for_checkout(launch.root)
        held = stored.read()
        missing = [key for key in self.secrets if key not in held]
        if missing:
            raise LaunchRefused(
                f"host companion {self.name!r} is started with {', '.join(missing)}, "
                f"which {stored.path()} does not hold; set it there from a "
                "terminal on the host"
            )
        begins = slot.logged()
        try:
            with (
                slot.log().open("ab") as output,
                Path(os.devnull).open("rb") as nothing,
            ):
                running = sh.Command(program)(
                    *arguments,
                    _cwd=str(command.cwd),
                    _env={
                        **command.started_from(launch.environment),
                        **{key: held[key] for key in self.secrets},
                    },
                    _bg=True,
                    _bg_exc=False,
                    _done=slot.reaped,
                    _new_session=True,
                    _in=nothing,
                    _out=output,
                    _err_to_out=True,
                )
        except sh.CommandNotFound as error:
            raise LaunchRefused(
                f"host companion {self.name!r} runs {program!r}, which is not "
                "installed here; install it, or drop the companion"
            ) from error
        return Running(
            process=LiveProcess.of(running.pid),
            declared=self.model_dump(mode="json"),
            since=datetime.now(UTC),
            output=begins,
        )

    def released(self, slot: CompanionSlot, lease: Lease) -> None:
        """Let go of one lease, stopping what runs once no live one is left, saying so first."""
        state = slot.read()
        leases = self.live(slot, [held for held in state.leases if held.id != lease.id])
        state = state.model_copy(update={"leases": leases})
        if leases:
            slot.write(state)
            return
        running = state.running
        if running is not None and running.process.running():
            state = self.halted(
                slot,
                state,
                running.process,
                f"the last lease was let go: {lease.id}, the launch pid "
                f"{lease.holder.pid}'s",
            )
        slot.write(
            state.model_copy(
                update={"running": None, "restarts": 0, "failing": 0, "retry": None}
            )
        )

    def live(self, slot: CompanionSlot, leases: list[Lease]) -> list[Lease]:
        """The leases whose launchers still run, writing into the log each one let go and why."""

        def kept(lease: Lease) -> bool:
            missing = lease.holder.missing()
            if missing:
                slot.noted(
                    f"lease {lease.id} is let go: its launcher is gone, {missing}"
                )
            return not missing

        return [lease for lease in leases if kept(lease)]

    def halted(
        self,
        slot: CompanionSlot,
        state: CompanionState,
        process: LiveProcess,
        why: str,
        stays: bool = False,
    ) -> CompanionState:
        """Stop ``process`` for ``why``, having written why into its log and its state first.

        ``state`` carries the live leases, whose count the record keeps. The
        launches still holding it start it again after the stop, unless it
        ``stays`` stopped.
        """
        stop = CompanionStop(
            at=datetime.now(UTC),
            process=process,
            why=why,
            by=os.getpid(),
            leases=len(state.leases),
            stays=stays,
        )
        slot.noted(
            f"stopping pid {process.pid}: {why} (live leases: {len(state.leases)})"
        )
        stopping = state.model_copy(update={"stopped": stop})
        slot.write(stopping)
        process.stop(self.grace)
        return stopping

    def vacated(
        self, slot: CompanionSlot, state: CompanionState, why: str, now: datetime
    ) -> CompanionState:
        """The state once what ran no longer stands in a start's way.

        Stopped for ``why`` where it still runs; else, where launches hold
        it and nobody stopped it to stay stopped, recorded as exited.
        """
        running = state.running
        if running is not None and running.process.running():
            return self.halted(slot, state, running.process, why)
        if not state.leases or state.stays_stopped():
            return state
        return self.exit_recorded(slot, state, now)

    def exit_recorded(
        self, slot: CompanionSlot, state: CompanionState, now: datetime
    ) -> CompanionState:
        """What ran recorded as gone, once, and when the launches holding it start it again.

        Recorded by whichever holder, or launch joining, finds it first: when,
        how it ended where that can still be collected, lup's stop of it
        where lup made one, and the last lines it wrote. An exit sooner after
        its start than the last step of ``backoff`` is one more in a row and
        waits the next step; any other starts the row over.
        """
        running = state.running
        exited = state.exited
        if running is None or (
            exited is not None and exited.process == running.process
        ):
            return state
        ran = (now - running.since).total_seconds() if running.since else None
        row = state.failing + 1 if ran is not None and ran < self.backoff[-1] else 1
        wait = self.backoff[min(row, len(self.backoff)) - 1]
        stop = state.stopped
        gone = CompanionExit(
            process=running.process,
            at=now,
            ran=ran,
            status=slot.ended(running),
            stopped=stop
            if stop is not None and stop.process == running.process
            else None,
            log=slot.log(),
            offset=running.output,
            tail=slot.tail(running.output, self.exit_lines),
        )
        slot.noted(
            f"pid {running.process.pid} is gone while held (live leases: "
            f"{len(state.leases)}): {gone.reason()}; starting it again in {wait:g}s"
        )
        return state.model_copy(
            update={
                "exited": gone,
                "failing": row,
                "retry": now + timedelta(seconds=wait),
            }
        )

    def resumed(self, state: CompanionState, now: datetime) -> CompanionState:
        """The state once a start answered: the exit it follows marked restarted, and counted."""
        exited = state.exited
        if exited is None or exited.restarted is not None:
            return state.model_copy(update={"retry": None})
        return state.model_copy(
            update={
                "exited": exited.model_copy(update={"restarted": now}),
                "restarts": state.restarts + 1,
                "retry": None,
            }
        )

    def supervised(
        self,
        slot: CompanionSlot,
        launch: CompanionLaunch,
        lease: Lease,
        letting_go: threading.Event,
    ) -> None:
        """Keep it running for as long as this launch holds it, on a thread of the launcher's.

        Each look reads the state and whether its process runs, and locks the
        slot only where something is to be done, so whichever holder locks
        first records the exit and, once its wait is over, starts it; every
        other then finds it recorded, or running again. A look that fails is
        written into the companion's log — the launcher's terminal is the
        session's, drawn over whole by its runtime — and the next waits along
        the backoff while they keep failing.
        """
        failed = 0

        def waited() -> bool:
            steps = self.backoff
            pause = steps[min(failed, len(steps)) - 1] if failed else self.watched_every
            return letting_go.wait(pause)

        for _ in iter(waited, True):
            try:
                if self.untended(slot.read(), lease):
                    with slot.locked():
                        if not letting_go.is_set():
                            self.tended(slot, launch, lease)
            except Exception:
                failed += 1
                slot.noted(
                    f"this launch could not look after it:\n{traceback.format_exc()}"
                )
                continue
            failed = 0

    def untended(self, state: CompanionState, lease: Lease) -> bool:
        """Whether a holder has anything to do: its own lease dropped, or nothing
        running that nobody stopped to stay stopped."""
        running = state.running
        gone = running is None or not running.process.running()
        return not state.holds(lease) or (gone and not state.stays_stopped())

    def tended(
        self, slot: CompanionSlot, launch: CompanionLaunch, lease: Lease
    ) -> None:
        """Under the slot's lock: take back a dropped lease, record an exit, start it again once due.

        A lease dropped while its launcher runs was judged gone by mistake,
        so it is taken back, saying so, and what that mistake stopped is
        started again like any exit. What the operator stopped to stay
        stopped is left so. A start that fails waits the next step of the
        backoff, as an exit would.
        """
        now = datetime.now(UTC)
        state = slot.read()
        if not state.holds(lease):
            slot.noted(
                f"lease {lease.id} of the launch pid {lease.holder.pid} was let go "
                "while that launch still holds it; taken back"
            )
            state = state.model_copy(
                update={"leases": [*self.live(slot, state.leases), lease]}
            )
        running = state.running
        if (running is not None and running.process.running()) or (
            state.stays_stopped()
        ):
            slot.write(state)
            return
        state = self.exit_recorded(slot, state, now)
        if state.retry is not None and now < state.retry:
            slot.write(state)
            return
        try:
            start = self.started(slot, launch, state)
        except LaunchRefused as refused:
            row = state.failing + 1
            wait = self.backoff[min(row, len(self.backoff)) - 1]
            slot.noted(
                f"starting it again failed: {refused}; trying again in {wait:g}s"
            )
            slot.write(
                state.model_copy(
                    update={"failing": row, "retry": now + timedelta(seconds=wait)}
                )
            )
            return
        slot.write(self.resumed(start.state, now) if start.answered else start.state)

    def standing(self, root: Path) -> "CompanionStanding":
        """What serves for a session in ``root`` now, how many launches hold it, and how it has fared.

        Read from outside any launch — an operator asking after it — so it
        joins nothing and starts nothing.
        """
        slot = self.slot(root)
        with slot.locked():
            state = slot.read()
        place = CompanionPlace(state=slot.directory, ports=state.given())
        running = state.running
        serving = (
            running is not None and running.process.running() and self.answers(place)
        )
        return CompanionStanding(
            place=place,
            serving=running.process if serving and running is not None else None,
            leases=sum(1 for lease in state.leases if lease.holder.running()),
            stopped=state.stopped,
            stays_stopped=state.stays_stopped(),
            exited=state.exited,
            restarts=state.restarts,
            retry=state.retry,
        )

    def stopped(
        self, root: Path, why: str = "the operator stopped it", stays: bool = True
    ) -> bool:
        """Stop what runs for ``root`` now, whether or not it is held, saying ``why`` first.

        The leases stay, and so does the record of what ran. Where it
        ``stays`` stopped — the operator's own stop — the launches holding it
        leave it so, until a launch or a restart starts it; otherwise they
        find it gone and start it again. With none holding it, the next
        launch starts it either way.
        """
        slot = self.slot(root)
        with slot.locked():
            state = slot.read()
            running = state.running
            if running is None or not running.process.running():
                return False
            self.halted(
                slot,
                state.model_copy(update={"leases": self.live(slot, state.leases)}),
                running.process,
                why,
                stays=stays,
            )
        return True


def answering(answers: Callable[[], bool], within: float, every: float = 0.05) -> bool:
    """Whether ``answers`` says yes within ``within`` seconds, asked every ``every``."""
    return (
        asyncio.run(
            wait_until(
                lambda: True if answers() else None,
                wait_seconds=within,
                poll_interval_seconds=every,
            )
        )
        is not None
    )


class CompanionStanding(BaseModel, frozen=True):
    """One shared companion as an operator reads it: where it is, whether it serves, how it fared."""

    place: CompanionPlace
    serving: LiveProcess | None = None
    """The process serving, where one runs and every port it was given answers."""

    stopped: CompanionStop | None = None
    """The last time lup stopped it, and why."""

    stays_stopped: bool = False
    """Whether the operator stopped it to stay stopped, and nothing has started it since."""

    exited: CompanionExit | None = None
    """The last time it was found gone while launches held it."""

    restarts: int = 0
    """How many times the launches holding it started it again."""

    retry: datetime | None = None
    """When they start it again, where it is gone and not started yet."""

    leases: int = 0
    """How many launches still running hold it."""
