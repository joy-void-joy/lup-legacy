"""Host companions: what a launch keeps running on the host for as long as its session runs.

Some sessions want something beside them that is no part of the session: a
service answering the operator's questions, a preview server showing what the
session makes, a watcher rebuilding it. Declared on the agent, a companion is
held around every session the declaration opens — launched from a terminal,
printed as a command, or opened in this process — and hands that session what
it needs to reach it: environment variables, folders to mount, the ports it
listens on. :class:`HostCompanion` is that seam, and the one thing a
compilation asks of a companion is :meth:`HostCompanion.held`.

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
import signal
import socket
import uuid
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Callable, Iterator, Sequence
from contextlib import (
    AbstractContextManager,
    ExitStack,
    asynccontextmanager,
    contextmanager,
)
from enum import StrEnum
from pathlib import Path
from typing import Annotated

import sh
from pydantic import BaseModel, Field, StringConstraints, ValidationError

from lup.channels.wait import wait_until
from lup.harness.notice import Notice
from lup.launch.declaration import Mount
from lup.launch.refusal import LaunchRefused
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

    @classmethod
    def of(cls, contributions: dict[str, Contribution]) -> "Joined":
        """Gather contributions by the companion making each, refusing two claiming one variable.

        Two companions exporting one name would leave the session reaching
        whichever was held last, which no declaration said, so the pair is
        refused naming both.
        """
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


def zombie(pid: int) -> bool:
    """Whether ``pid`` has exited and waits on a parent that has not collected it."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        return False
    # lup: ignore[string-split] — /proc stat's fields after the command's name
    fields = stat.rpartition(")")[2].split()
    return bool(fields) and fields[0] == "Z"


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


class CompanionState(BaseModel, frozen=True):
    """What is kept of one shared companion between the launches holding it."""

    checkout: Path | None = None
    """The checkout it serves, for a companion shared per checkout."""

    ports: list[GivenPort] = []
    running: Running | None = None
    leases: list[Lease] = []

    def given(self) -> dict[PortName, PortNumber]:
        """The ports it was given, by its names for them."""
        return {port.name: port.port for port in self.ports}


class CompanionSlot(BaseModel, frozen=True):
    """One shared companion's directory: its state, its lock, its log."""

    directory: Path

    def log(self) -> Path:
        """Where the companion's output goes, outside the terminal the session takes over."""
        return self.directory / "output.log"

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
    """

    scope: CompanionScope = CompanionScope.CHECKOUT
    """Which sessions share one running process."""

    ports: dict[PortName, PortNumber] = {}
    """The ports it listens on, by name, each the one it prefers."""

    ready_within: float = Field(default=10.0, gt=0)
    """How long a started process has to answer before the launch is refused."""

    grace: float = Field(default=5.0, gt=0)
    """How long a stopped process has to exit before it is killed."""

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
        slot = self.slot(launch.root)
        lease = Lease(id=uuid.uuid4().hex, holder=LiveProcess.of(os.getpid()))
        with slot.locked():
            place = self.joined(slot, launch, lease)
        try:
            yield self.contribution(place, launch.root)
        finally:
            with slot.locked():
                self.released(slot, lease)

    def joined(
        self, slot: CompanionSlot, launch: CompanionLaunch, lease: Lease
    ) -> CompanionPlace:
        """Join what runs where it still serves this declaration, else start it; lease it."""
        state = slot.read()
        declared = self.model_dump(mode="json")
        place = CompanionPlace(state=slot.directory, ports=state.given())
        running = state.running
        serving = (
            running is not None
            and running.declared == declared
            and running.process.running()
            and self.answers(place)
        )
        if not serving:
            if running is not None and running.process.running():
                running.process.stop(self.grace)
            home = companions_home()
            with choosing_ports(home):
                given = given_ports(
                    dict(self.ports), state.ports, kept_elsewhere(home, slot)
                )
                place = CompanionPlace(
                    state=slot.directory, ports={port.name: port.port for port in given}
                )
                running = Running(
                    process=self.started(place, launch, slot), declared=declared
                )
                state = state.model_copy(update={"ports": given})
        slot.write(
            state.model_copy(
                update={
                    "checkout": (
                        launch.root.resolve()
                        if self.scope is CompanionScope.CHECKOUT
                        else None
                    ),
                    "running": running,
                    "leases": [
                        *(held for held in state.leases if held.holder.running()),
                        lease,
                    ],
                }
            )
        )
        return place

    def started(
        self, place: CompanionPlace, launch: CompanionLaunch, slot: CompanionSlot
    ) -> LiveProcess:
        """Start it in a session of its own, and wait until it serves.

        A session of its own, so the signal that stops it reaches whatever it
        started too, with nothing on its input, since the launch starting it
        may end long before it does, and its output in a log beside its state.
        """
        command = self.process(place, launch.root)
        program, *arguments = command.argv
        try:
            with (
                slot.log().open("ab") as output,
                Path(os.devnull).open("rb") as nothing,
            ):
                running = sh.Command(program)(
                    *arguments,
                    _cwd=str(command.cwd),
                    _env=command.started_from(launch.environment),
                    _bg=True,
                    _bg_exc=False,
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
        process = LiveProcess.of(running.pid)
        serving = asyncio.run(
            wait_until(
                lambda: True if self.answers(place) else None,
                wait_seconds=self.ready_within,
                poll_interval_seconds=0.05,
            )
        )
        if serving is None:
            process.stop(self.grace)
            raise LaunchRefused(
                f"host companion {self.name!r} did not answer within "
                f"{self.ready_within:g}s; its output is in {slot.log()}"
            )
        return process

    def released(self, slot: CompanionSlot, lease: Lease) -> None:
        """Let go of one lease, stopping what runs once no live one is left."""
        state = slot.read()
        leases = [
            held
            for held in state.leases
            if held.id != lease.id and held.holder.running()
        ]
        running = state.running
        if not leases and running is not None:
            running.process.stop(self.grace)
            running = None
        slot.write(state.model_copy(update={"leases": leases, "running": running}))

    def standing(self, root: Path) -> "CompanionStanding":
        """What serves for a session in ``root`` now, and how many launches hold it.

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
        )

    def stopped(self, root: Path) -> bool:
        """Stop what runs for ``root`` now, whether or not it is held.

        The leases stay: the sessions holding it still hold it, and the next
        launch finds nothing running and starts it again.
        """
        slot = self.slot(root)
        with slot.locked():
            state = slot.read()
            running = state.running
            if running is None:
                return False
            running.process.stop(self.grace)
            slot.write(state.model_copy(update={"running": None}))
        return True


class CompanionStanding(BaseModel, frozen=True):
    """One shared companion as an operator reads it: where it is, whether it serves."""

    place: CompanionPlace
    serving: LiveProcess | None = None
    """The process serving, where one runs and every port it was given answers."""

    leases: int = 0
    """How many launches still running hold it."""
