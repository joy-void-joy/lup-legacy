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
from lup.harness.environment import inside_a_container
from lup.harness.notice import Notice
from lup.harness.requirements import SENTINEL_VARIABLE
from lup.launch.companions import (
    CompanionLaunch,
    CompanionName,
    CompanionPlace,
    CompanionProcess,
    CompanionScope,
    Contribution,
    LiveProcess,
    PortName,
    PortNumber,
    SharedProcess,
)
from lup.launch.preflight import NONCE_VARIABLE
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
        written(
            self.repositories_directory() / f"{known.key()}.json",
            known.model_dump_json(indent=2),
        )
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
        del root
        url = page_url(place)
        return Contribution(
            environment={DASHBOARD_URL_ENV: url},
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
            # already reads this checkout's reviews, at the address handed in.
            handed = launch.environment
            yield Contribution(
                environment=(
                    {DASHBOARD_URL_ENV: handed[DASHBOARD_URL_ENV]}
                    if DASHBOARD_URL_ENV in handed
                    else {}
                )
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


class DashboardStatus(BaseModel, frozen=True):
    """Whether the dashboard serves, where, and for whom; never its capability."""

    serving: bool
    url: str = ""
    pid: int | None = None
    sessions: int = 0
    """How many running launches hold it."""

    repositories: list[str] = []
    detail: str


def dashboard_status(dashboard: Dashboard, root: Path) -> DashboardStatus:
    """The dashboard as whoever asks may read it.

    Inside a session, only the address its launch handed it: the private
    state is the operator's, and a session reading it would be a session
    holding the capability.
    """
    if SessionMarkers().inside_a_session():
        advertised = AdvertisedDashboard().url
        return DashboardStatus(
            serving=bool(advertised),
            url=advertised,
            detail=(
                "The address this session's launch handed it; the operator "
                "opens the page with `uv run lup-devtools dashboard open`."
                if advertised
                else "This session's launch held no dashboard."
            ),
        )
    standing = dashboard.standing(root)
    registry = DashboardRegistry(directory=standing.place.state)
    serving = standing.serving
    return DashboardStatus(
        serving=serving is not None,
        url=page_url(standing.place) if "page" in standing.place.ports else "",
        pid=serving.pid if serving is not None else None,
        sessions=standing.leases,
        repositories=[str(known.repository) for known in registry.repositories()],
        detail=(
            "Held by the running sessions; it stops once the last one ends."
            if serving is not None
            else "Not running: the next `harness claude|codex` session starts it, "
            "or `uv run lup-devtools dashboard serve` serves one in this terminal."
        ),
    )


def private_url(dashboard: Dashboard, root: Path) -> str:
    """The address that opens the running dashboard, capability included.

    Refused where nothing serves, rather than starting a dashboard no
    session holds and so nothing would ever stop.
    """
    refuse_inside_a_session("dashboard open")
    standing = dashboard.standing(root)
    if standing.serving is None:
        raise LookupError(
            "No dashboard is running: the next `harness claude|codex` session "
            "starts it, or `uv run lup-devtools dashboard serve` serves one in "
            "this terminal."
        )
    token = DashboardToken(directory=standing.place.state).read()
    return f"{page_url(standing.place)}/#token={token}"
