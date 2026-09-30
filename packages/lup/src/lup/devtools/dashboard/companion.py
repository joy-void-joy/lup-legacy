"""The dashboard as a host companion: one process per person, held by every launch.

Every `harness claude|codex` session holds it, whichever repository it runs
in and whoever opened it — a session launching a child session included — so
there is one page for all of them rather than one per repository, and it
stops with the last session holding it. What each repository may do stays
that repository's: a review is answered in the relay of the checkout that
parked it, against the fingerprint it recorded.

Its address is stable. The port it was given is kept while it stays free and
the capability that opens it is minted once and kept in the host's private
state, so a tab left open across a restart reconnects on its own instead of
being told its access expired. A session is handed the address and never the
capability.
"""

import hashlib
import os
import secrets
import sys
import uuid
import webbrowser
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from importlib import resources
from importlib.util import find_spec
from pathlib import Path
from typing import Literal

import httpx
import sh
from pydantic import BaseModel, Field, ValidationError
from pydantic_settings import BaseSettings

from lup.coordination.identity import MEMBER_ENV, NAME_ENV
from lup.devtools.dashboard.address import AdvertisedDashboard
from lup.devtools.dashboard.pulse import (
    DASHBOARD_PULSE_ENV,
    DashboardPulse,
    PulseFile,
    RunningCode,
)
from lup.harness.environment import inside_a_container
from lup.harness.notice import Notice
from lup.harness.requirements import SENTINEL_VARIABLE
from lup.launch.companions import (
    CompanionExit,
    CompanionLaunch,
    CompanionName,
    CompanionPlace,
    CompanionProcess,
    CompanionScope,
    CompanionStanding,
    CompanionStop,
    Contribution,
    LiveProcess,
    PortName,
    PortNumber,
    SharedProcess,
    StatusLine,
    lent_directory,
)
from lup.launch.compilation import inherited_environment
from lup.launch.declaration import Mount
from lup.launch.preflight import NONCE_VARIABLE
from lup.launch.refusal import LaunchRefused
from lup.policy.identity import AGENT_IDENTITY_ENV, DASHBOARD_URL_ENV
from lup.sandbox.rail import repository_layout, sibling_worktrees
from lup.types import EnvVars
from lup.workspace.context import SESSION_DIR_ENV, SESSION_ID_ENV


def session_markers() -> list[str]:
    """What names one launched session in an environment, and so no companion's.

    The dashboard is started by whichever session first holds it and then
    serves every other, so it runs as the operator's rather than as that
    session: its identity, its boundary and its sandbox are left behind.
    """
    return [
        NONCE_VARIABLE,
        SENTINEL_VARIABLE,
        MEMBER_ENV,
        NAME_ENV,
        AGENT_IDENTITY_ENV,
        SESSION_DIR_ENV,
        SESSION_ID_ENV,
        DASHBOARD_URL_ENV,
        "LUP_SANDBOX_ACTIVE",
        "LUP_CONTAINED",
        "LUP_BOUNDARY_ROOT",
    ]


class SessionMarkers(BaseSettings):
    """The markers a launched session carries, read where only the operator may act."""

    boundary: str = Field(default="", validation_alias=NONCE_VARIABLE)
    member: str = Field(default="", validation_alias=MEMBER_ENV)
    agent: str = Field(default="", validation_alias=AGENT_IDENTITY_ENV)
    session_dir: str = Field(default="", validation_alias=SESSION_DIR_ENV)
    session_id: str = Field(default="", validation_alias=SESSION_ID_ENV)

    def inside_a_session(self) -> bool:
        """Whether this process runs within a launched agent session."""
        return any(
            (self.boundary, self.member, self.agent, self.session_dir, self.session_id)
        )


def launched_by_an_operator(environment: EnvVars) -> bool:
    """Whether a launch was opened from a terminal of the operator's, not from a session."""
    return not any(
        name in environment and environment[name]
        for name in (
            NONCE_VARIABLE,
            MEMBER_ENV,
            AGENT_IDENTITY_ENV,
            SESSION_DIR_ENV,
            SESSION_ID_ENV,
        )
    )


def refuse_inside_a_session(command: str) -> None:
    """Refuse a command that is the operator's alone when run from inside a session.

    The policy refuses the command before it runs; this is the same answer
    given by the command itself, for a caller the policy never saw.
    """
    if SessionMarkers().inside_a_session():
        raise PermissionError(
            f"`{command}` is the operator's: run it from a terminal "
            "outside the agent session"
        )


class Capability(BaseModel, frozen=True):
    """The capability that opens the dashboard, and whether it was minted just now."""

    value: str
    fresh: bool = False


class DashboardToken(BaseModel, frozen=True):
    """The capability that opens the dashboard, kept for as long as its state is."""

    directory: Path

    def path(self) -> Path:
        return self.directory / "token"

    def minted(self) -> Capability:
        """This dashboard's capability, minting it the first time it is asked for.

        Written whole under a name of its own and linked into place, so two
        launches minting at once agree on one, and a reader never meets an
        empty file.
        """
        self.directory.mkdir(parents=True, exist_ok=True)
        self.directory.chmod(0o700)
        if self.path().exists():
            return Capability(value=self.read())
        staged = self.directory / f"token.{uuid.uuid4().hex}"
        staged.touch(mode=0o600, exist_ok=False)
        staged.write_text(secrets.token_urlsafe(32), encoding="utf-8")
        try:
            self.path().hardlink_to(staged)
        except FileExistsError:
            return Capability(value=self.read())
        finally:
            staged.unlink(missing_ok=True)
        return Capability(value=self.read(), fresh=True)

    def read(self) -> str:
        """The capability, refused where anybody but the operator could have written it."""
        target = self.path()
        status = target.lstat()
        if (
            target.is_symlink()
            or status.st_uid != os.getuid()
            or status.st_mode & 0o077
        ):
            raise PermissionError(
                f"{target} must be the operator's own file, readable by nobody else"
            )
        return target.read_text(encoding="utf-8").strip()


class KnownRepository(BaseModel, frozen=True):
    """One repository a launch held the dashboard for, and a checkout of it to run in."""

    repository: Path
    """Its shared git directory, which every worktree of it answers to."""

    checkout: Path

    def key(self) -> str:
        """A name for it that stays the same across checkouts and restarts."""
        return hashlib.sha256(str(self.repository).encode()).hexdigest()[:16]

    def name(self) -> str:
        """What a reader calls it: the directory it is checked out as."""
        if self.repository.name == ".git":
            return self.repository.parent.name
        return self.repository.name.removesuffix(".git")


class LaunchRecord(BaseModel, frozen=True):
    """One launch holding the dashboard: the repository, and the process that launched."""

    repository: Path
    checkout: Path
    holder: LiveProcess


class DashboardRegistry(BaseModel, frozen=True):
    """The repositories the dashboard serves, and the launches holding it for each.

    A repository stays known once a launch named it, until its directory is
    gone, so its history is still on the page after its sessions end; a
    launch is known only while it runs.
    """

    directory: Path

    def repositories_directory(self) -> Path:
        return self.directory / "repositories"

    def launches_directory(self) -> Path:
        return self.directory / "launches"

    def recorded(self, known: KnownRepository) -> None:
        """Keep one repository known, with a checkout of it, until its directory is gone."""
        written(
            self.repositories_directory() / f"{known.key()}.json",
            known.model_dump_json(indent=2),
        )

    @contextmanager
    def registered(self, checkout: Path) -> Iterator[None]:
        """Record one launch in ``checkout`` for as long as it holds the dashboard."""
        try:
            repository = repository_layout(checkout).common.resolve()
        except (sh.ErrorReturnCode, OSError):
            repository = checkout.resolve()
        known = KnownRepository(repository=repository, checkout=checkout.resolve())
        record = LaunchRecord(
            repository=repository,
            checkout=known.checkout,
            holder=LiveProcess.of(os.getpid()),
        )
        self.recorded(known)
        launch = self.launches_directory() / f"{uuid.uuid4().hex}.json"
        written(launch, record.model_dump_json(indent=2))
        try:
            yield
        finally:
            launch.unlink(missing_ok=True)

    def repositories(self) -> list[KnownRepository]:
        """Every known repository still on disk, each with a checkout that still is."""

        def standing(known: KnownRepository) -> KnownRepository | None:
            if not known.repository.exists():
                return None
            if (known.checkout / ".git").exists():
                return known
            try:
                siblings = sibling_worktrees(known.repository)
            except (sh.ErrorReturnCode, OSError, ValueError):
                return None
            present = [path for path in siblings if (path / ".git").exists()]
            return (
                known.model_copy(update={"checkout": present[0]}) if present else None
            )

        found = [
            standing(known)
            for path in sorted(self.repositories_directory().glob("*.json"))
            if (known := read_model(path, KnownRepository)) is not None
        ]
        return [known for known in found if known is not None]

    def launches(self) -> list[LaunchRecord]:
        """Every launch still running that holds the dashboard, sweeping the rest."""

        def running(path: Path) -> LaunchRecord | None:
            record = read_model(path, LaunchRecord)
            if record is None or not record.holder.running():
                path.unlink(missing_ok=True)
                return None
            return record

        found = [
            running(path) for path in sorted(self.launches_directory().glob("*.json"))
        ]
        return [record for record in found if record is not None]

    def live(self, repository: Path) -> bool:
        """Whether any running launch holds the dashboard for this repository."""
        return any(record.repository == repository for record in self.launches())


def written(path: Path, text: str) -> None:
    """Replace one file in a single rename, so no reader meets half of it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    staged = path.with_name(f"{path.name}.{uuid.uuid4().hex}")
    staged.write_text(text, encoding="utf-8")
    staged.replace(path)


def read_model[Model: BaseModel](path: Path, model: type[Model]) -> Model | None:
    """One record, or nothing where it vanished or does not parse."""
    try:
        return model.model_validate_json(path.read_bytes())
    except (OSError, ValidationError):
        return None


def dashboard_revision() -> str:
    """What a dashboard is built from — its server's code and its page — as one digest.

    Part of the declaration a running dashboard was started from, so a launch
    from a checkout whose dashboard differs replaces what runs rather than
    joining a server that answers in another version's words.
    """
    here = Path(__file__).parent
    sources = sorted([*here.glob("*.py"), *here.parent.joinpath("review").glob("*.py")])
    page = resources.files("lup.web").joinpath("bundles", "dashboard", "index.html")
    digest = hashlib.sha256()
    for source in sources:
        digest.update(source.name.encode())
        digest.update(source.read_bytes())
    if page.is_file():
        digest.update(page.read_bytes())
    return digest.hexdigest()[:16]


class DashboardHealth(BaseModel, frozen=True):
    """What a running dashboard says of itself to a caller holding its capability."""

    service: Literal["lup.dashboard"] = "lup.dashboard"
    revision: str
    pid: int


def page_url(place: CompanionPlace) -> str:
    """The dashboard's address on the host's loopback, credential-free."""
    return f"http://127.0.0.1:{place.ports['page']}"


def pulse_status_line(root: Path, pulse: Path) -> StatusLine:
    """What a session's status line runs: its checkout's CLI reading the pulse, and nothing more.

    Unsynced, since a status line is no moment to install anything, and on a
    route the CLI answers before loading the project's application.
    """
    return StatusLine(
        argv=[
            "uv",
            "run",
            "--no-sync",
            "--directory",
            str(root),
            "lup-devtools",
            "dashboard",
            "line",
            str(pulse),
        ]
    )


class Dashboard(SharedProcess, frozen=True):
    """The operator's dashboard, one process per person, held by every session they launch.

    The first session to hold it starts it on its preferred port, or the next
    free one; every later session, in any repository, joins it and registers
    its repository; it stops once the last session holding it ends. The page
    opens in a browser only the first time its capability is minted, and only
    for a launch the operator made — every later start keeps the same address,
    which the tabs already open reconnect to.
    """

    name: CompanionName = "dashboard"
    scope: CompanionScope = CompanionScope.USER
    ports: dict[PortName, PortNumber] = {"page": 8766}
    ready_within: float = Field(default=60.0, gt=0)
    """How long a start has to answer: long enough for a checkout whose
    environment compiles every module it imports the first time it starts,
    since one that does not answer is stopped and refuses the launch."""

    revision: str = Field(default_factory=dashboard_revision)
    """What it was built from; a launch from code that differs replaces what runs."""

    def process(self, place: CompanionPlace, root: Path) -> CompanionProcess:
        del root
        return CompanionProcess(
            argv=[
                sys.executable,
                "-m",
                "lup.devtools.dashboard.service",
                str(place.state),
                str(place.ports["page"]),
                self.revision,
            ],
            cwd=place.state,
            unset=session_markers(),
        )

    def contribution(self, place: CompanionPlace, root: Path) -> Contribution:
        """Its address, and its pulse lent read-only at the host's path for the status line to read."""
        url = page_url(place)
        pulse = PulseFile.of(lent_directory(place.state)).path
        return Contribution(
            environment={DASHBOARD_URL_ENV: url, DASHBOARD_PULSE_ENV: str(pulse)},
            mounts=[Mount(path=pulse.parent)],
            ports=dict(place.ports),
            notices=[
                Notice(
                    text=(
                        f"Dashboard: {url} — every session's reviews, one page. "
                        "`uv run lup-devtools dashboard open` opens it."
                    ),
                    urgency="detail",
                )
            ],
            status_line=pulse_status_line(root, pulse),
        )

    def answers(self, place: CompanionPlace) -> bool:
        """Whether a dashboard serves there: its own health, behind its own capability."""
        if "page" not in place.ports:
            return False
        try:
            token = DashboardToken(directory=place.state).read()
            response = httpx.get(
                f"{page_url(place)}/api/service",
                headers={"Authorization": f"Bearer {token}"},
                timeout=0.5,
                trust_env=False,
            )
        except (OSError, httpx.HTTPError):
            return False
        if response.status_code != 200:
            return False
        try:
            DashboardHealth.model_validate_json(response.content)
        except ValidationError:
            return False
        return True

    @contextmanager
    def held(self, launch: CompanionLaunch) -> Iterator[Contribution]:
        if inside_a_container(launch.environment):
            # A session launched from inside a container launches there too,
            # where a dashboard would serve nobody: the one the host holds
            # already reads this checkout's reviews, at the address handed in,
            # and publishes its pulse where this container already reads it.
            handed = launch.environment
            passed = {
                name: handed[name]
                for name in (DASHBOARD_URL_ENV, DASHBOARD_PULSE_ENV)
                if name in handed
            }
            yield Contribution(
                environment=passed,
                status_line=(
                    pulse_status_line(launch.root, Path(passed[DASHBOARD_PULSE_ENV]))
                    if DASHBOARD_PULSE_ENV in passed
                    else None
                ),
            )
            return
        if find_spec("fastapi") is None or find_spec("uvicorn") is None:
            yield Contribution(
                notices=[
                    Notice(
                        text=(
                            "The dashboard serves through lup-agents[web], which "
                            "is not installed here: install it, or decline the "
                            "dashboard module."
                        ),
                        urgency="warning",
                    )
                ]
            )
            return
        slot = self.slot(launch.root)
        capability = DashboardToken(directory=slot.directory).minted()
        registry = DashboardRegistry(directory=slot.directory)
        # Made before the session's mount table names it, which it must exist for.
        lent_directory(slot.directory).mkdir(mode=0o700, exist_ok=True)
        with (
            registry.registered(launch.root),
            super().held(launch) as contribution,
        ):
            if capability.fresh and launched_by_an_operator(launch.environment):
                webbrowser.open(
                    f"http://127.0.0.1:{contribution.ports['page']}"
                    f"/#token={capability.value}"
                )
            yield contribution

    def stopped(
        self, root: Path, why: str = "the operator stopped it", stays: bool = True
    ) -> bool:
        """Stop it for ``why``; where it stays stopped, every session's status line says so.

        The pulse the stopped dashboard took down is replaced by one saying
        the operator stopped it and what starts it, which holds until a start
        replaces it.
        """
        if not super().stopped(root, why, stays):
            return False
        slot = self.slot(root)
        state = slot.read()
        stop = state.stopped
        if stays and stop is not None and "page" in state.given():
            halted = DashboardPulse(
                url=f"http://127.0.0.1:{state.given()['page']}",
                pid=stop.process.pid,
                beat=stop.at,
                halted="dashboard stopped by the operator; `dashboard restart` starts it",
            )
            written(
                PulseFile.of(lent_directory(slot.directory)).path,
                halted.model_dump_json(indent=2),
            )
        return True


class DashboardStatus(BaseModel, frozen=True):
    """Whether the dashboard serves, where, and for whom; never its capability."""

    serving: bool
    url: str = ""
    pid: int | None = None
    sessions: int = 0
    """How many running launches hold it."""

    repositories: list[str] = []
    pending: int = 0
    """Reviews waiting on the operator, across every repository it serves."""

    tabs: int = 0
    """Pages following it now."""

    code: RunningCode = RunningCode()
    """Which code it runs, and whether its checkout has moved past it."""

    restarts: int = 0
    """How many times the sessions holding it started it again after it stopped."""

    exited: CompanionExit | None = None
    """The last time it was found gone while sessions held it: when, how it
    ended and the last lines it wrote. Read from outside a session only."""

    stopped: CompanionStop | None = None
    """The last time lup stopped it, why, and how many sessions held it then.
    Read from outside a session only."""

    retry: datetime | None = None
    """When the sessions holding it start it again, where it is gone."""

    detail: str


def published_status(advertised: AdvertisedDashboard, now: datetime) -> DashboardStatus:
    """The dashboard as its pulse says, which is all a session may read of it.

    The private state is the operator's, and a session reading it would be a
    session holding the capability; the pulse is what the service publishes
    for sessions, lent to each read-only.
    """
    if not advertised.pulse:
        return DashboardStatus(
            serving=bool(advertised.url),
            url=advertised.url,
            detail=(
                "The address this session's launch handed it; the launch lent "
                "no pulse, so what the dashboard counts is not readable here."
                if advertised.url
                else "This session's launch held no dashboard."
            ),
        )
    pulse = PulseFile(path=Path(advertised.pulse)).read()
    if pulse is None:
        return DashboardStatus(
            serving=False,
            url=advertised.url,
            detail="The dashboard stopped: it took its pulse down.",
        )
    if pulse.halted:
        return DashboardStatus(
            serving=False,
            url=pulse.url,
            detail=pulse.halted[:1].upper() + pulse.halted[1:] + ".",
        )
    if not pulse.current(now):
        return DashboardStatus(
            serving=False,
            url=pulse.url,
            detail=(
                f"The dashboard stopped: it last published at {pulse.beat:%H:%M:%S} "
                "UTC without taking its pulse down. The sessions holding it "
                "start it again, and the operator's `dashboard restart` does now."
            ),
        )
    return counted(
        pulse,
        "As the dashboard published it moments ago; the operator opens the "
        "page with `uv run lup-devtools dashboard open`.",
    )


def counted(pulse: DashboardPulse, detail: str) -> DashboardStatus:
    """A serving dashboard's status, in the counts its pulse carries."""
    said = pulse.code.said()
    return DashboardStatus(
        serving=True,
        url=pulse.url,
        pid=pulse.pid,
        sessions=pulse.sessions,
        repositories=pulse.repositories,
        pending=pulse.pending,
        tabs=pulse.tabs,
        code=pulse.code,
        restarts=pulse.restarts,
        detail=said[:1].upper() + said[1:] + "." if said else detail,
    )


def unserved_detail(standing: CompanionStanding) -> str:
    """What an operator reads of a dashboard that does not serve: who holds it, and what starts it."""
    if not standing.leases:
        return (
            "Not running: the next `harness claude|codex` session starts it, "
            "or `uv run lup-devtools dashboard serve` serves one in this terminal."
        )
    if standing.stays_stopped:
        return (
            "Stopped by the operator; `uv run lup-devtools dashboard restart` "
            "starts it."
        )
    held = (
        "1 session holds"
        if standing.leases == 1
        else f"{standing.leases} sessions hold"
    )
    if standing.retry is None or standing.exited is None:
        return (
            f"Not running while {held} it: "
            "`uv run lup-devtools dashboard restart` starts it for them."
        )
    return (
        f"Not running: it stopped ({standing.exited.reason()}) while {held} "
        f"it, and they start it again at {standing.retry:%H:%M:%S} UTC; "
        "`uv run lup-devtools dashboard restart` starts it now."
    )


def dashboard_status(dashboard: Dashboard, root: Path) -> DashboardStatus:
    """The dashboard as whoever asks may read it: a session, from its pulse alone."""
    now = datetime.now(UTC)
    if SessionMarkers().inside_a_session():
        return published_status(AdvertisedDashboard(), now)
    standing = dashboard.standing(root)
    registry = DashboardRegistry(directory=standing.place.state)
    serving = standing.serving
    pulse = PulseFile.of(lent_directory(standing.place.state)).read()
    held = "Held by the running sessions; it stops once the last one ends."
    fared = {
        "restarts": standing.restarts,
        "exited": standing.exited,
        "stopped": standing.stopped,
        "retry": standing.retry,
    }
    if serving is not None and pulse is not None and pulse.current(now):
        return counted(pulse, held).model_copy(update=fared)
    return DashboardStatus(
        serving=serving is not None,
        url=page_url(standing.place) if "page" in standing.place.ports else "",
        pid=serving.pid if serving is not None else None,
        sessions=standing.leases,
        repositories=[str(known.repository) for known in registry.repositories()],
        detail=held if serving is not None else unserved_detail(standing),
        **fared,
    )


def private_url(dashboard: Dashboard, root: Path) -> str:
    """The address that opens the running dashboard, capability included.

    Refused where nothing serves, rather than starting a dashboard no
    session holds and so nothing would ever stop.
    """
    refuse_inside_a_session("dashboard open")
    standing = dashboard.standing(root)
    if standing.serving is None:
        raise LookupError(f"No dashboard is running. {unserved_detail(standing)}")
    token = DashboardToken(directory=standing.place.state).read()
    return f"{page_url(standing.place)}/#token={token}"


def started_for_holders(dashboard: Dashboard, root: Path) -> str:
    """Start the dashboard from ``root``'s code for the sessions holding it; its address.

    Under a lease of the operator's command, let go at once: the sessions'
    own leases keep it running, and it stops with the last of them. A start
    that fails is the operator's to read, as the refusal it is.
    """
    launch = CompanionLaunch(
        root=root, runtime="operator", environment=inherited_environment()
    )
    try:
        with dashboard.held(launch) as contribution:
            return contribution.environment[DASHBOARD_URL_ENV]
    except LaunchRefused as refused:
        raise LookupError(str(refused)) from refused


def restarted(dashboard: Dashboard, root: Path) -> str:
    """Restart the dashboard onto its checkout's code, keeping its address.

    Asked of the dashboard itself, behind its capability and from its own
    origin, as the page asks: it restarts in place once no write is in
    flight. Where none serves while sessions hold it, one is started for
    them from this checkout's code. One that predates restarting itself
    answers the ask with nothing to take it, and is replaced instead —
    stopped, and started from this checkout's code for the sessions holding
    it; with none holding it, stopping is all there is to do.
    """
    refuse_inside_a_session("dashboard restart")
    standing = dashboard.standing(root)
    if standing.serving is None:
        if not standing.leases:
            raise LookupError(f"No dashboard is running. {unserved_detail(standing)}")
        held = f"{standing.leases} session{'' if standing.leases == 1 else 's'}"
        url = started_for_holders(dashboard, root)
        return (
            f"No dashboard was running while {held} held it: started one from "
            f"{root}'s code at {url}; open tabs reconnect on their own."
        )
    url = page_url(standing.place)
    token = DashboardToken(directory=standing.place.state).read()
    try:
        answered = httpx.post(
            f"{url}/api/service/restart",
            headers={"Authorization": f"Bearer {token}", "Origin": url},
            json={},
            timeout=5,
            trust_env=False,
        )
    except httpx.HTTPError as unanswered:
        raise LookupError(
            f"The dashboard at {url} did not answer: {unanswered}"
        ) from unanswered
    match answered.status_code:
        case 202:
            return (
                "The dashboard restarts onto its checkout's code once no answer is "
                f"in flight, at {url}; open tabs reconnect on their own."
            )
        case 503:
            return (
                "The dashboard is already restarting; open tabs reconnect on their own."
            )
        case 404 | 405:
            dashboard.stopped(
                root,
                why="the operator's restart replaces a dashboard that predates "
                "restarting itself",
                stays=False,
            )
            if not standing.leases:
                return (
                    "The dashboard predated restarting itself and no session held it, "
                    "so it was stopped; the next launch starts it."
                )
            started_for_holders(dashboard, root)
            return (
                "The dashboard predated restarting itself, so it was replaced: "
                f"stopped, and started from {root}'s code for the sessions holding it."
            )
        case status:
            raise LookupError(f"The dashboard refused the restart: HTTP {status}")
