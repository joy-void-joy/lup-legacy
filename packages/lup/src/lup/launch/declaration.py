"""What a launch adds to an agent's declaration, and the command one compiles to.

Every type here is a field value on :class:`~lup.providers.claude.Claude` and
:class:`~lup.providers.codex.Codex`, read by both of a declaration's
compilations: the SDK options :meth:`open` builds and the command
:meth:`command` prints. A field one of them cannot honour is refused where the
declaration is compiled, in the words of the field, rather than dropped.
"""

import shlex
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from enum import StrEnum
from pathlib import Path
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, Field

from lup.harness.devices import Device
from lup.harness.image import detected_client
from lup.harness.messaging import WakeSockets
from lup.harness.models import Harness, HookSet, Resumption
from lup.harness.notice import Notice
from lup.observability.sessions import SessionRecorder
from lup.policy.enforcement import SandboxPosture
from lup.sandbox.rail import AccessibleRoot
from lup.sessions.events import SessionId, SessionSummary
from lup.types import EnvVars


class Mount(BaseModel, frozen=True, extra="forbid"):
    """A folder outside the working tree that this session is meant to reach.

    The same shape a machine's standing registration resolves to, because the
    two say one thing at different lifetimes: a registration is standing and
    reviewed, a mount lasts one declaration. Both reach the container's mount
    table and each runtime's own sandbox widening alike, so where a session
    runs never decides what it can write.
    """

    path: Path
    writable: bool = False
    """Whether the session may write here; unset, it reads and does not write."""

    def root(self) -> AccessibleRoot:
        """This mount as the roots a lease and a boundary are compiled from."""
        return AccessibleRoot(path=self.path, writable=self.writable)


class LaunchSandbox(StrEnum):
    """Which sandbox a launch opens the session under.

    One axis with three points rather than two booleans, because the two
    walls were never independent: the container stands the runtime's own
    sandbox down, and skipping the container is what makes that sandbox
    worth establishing. Named from the session's point of view -- which
    wall is load-bearing -- so what selects one reads as the posture it buys
    rather than as the machinery it toggles.

    Distinct from :class:`~lup.policy.enforcement.SandboxPosture`, which is
    what a session's *configuration* means to the policy kernel once it is
    open; this is the launcher's choice of what to configure.
    """

    OUTER = "outer"
    """The verified container is the boundary; the native sandbox stands down
    inside it, because a wall that has to be weakened to start nested is worth
    less than saying plainly which wall is load-bearing."""

    INNER = "inner"
    """The session opens on the host and the launcher establishes the
    runtime's own workspace-write sandbox, vouching for it only after
    exercising the tools it stands on."""

    NONE = "none"
    """The session opens on the host under the semantic policy alone. Nothing
    is established and nothing is vouched for, so the deny lattice stays
    standing -- the posture a broken inner sandbox would degrade into
    silently, stated as a choice."""

    def contained(self) -> bool:
        """Whether this launch opens inside the verified container."""
        return self is LaunchSandbox.OUTER


class Sandbox(BaseModel, ABC, frozen=True, extra="forbid"):
    """Which wall a session opens behind, answered the same way by every compilation.

    Each wall answers what a compilation asks of it — which posture it is,
    what the policy judging it should believe, what it mounts and grants, and
    what of the runtime's own sandbox it establishes — so a runtime compiles
    a wall by asking, never by recognising one.
    """

    @abstractmethod
    def posture(self) -> LaunchSandbox:
        """Which wall this makes load-bearing."""

    @abstractmethod
    def enforcement(self) -> SandboxPosture:
        """What this wall means to the policy judging the session it confines.

        The same declaration reaches the runtime and the policy, so a session
        cannot be permitted more or less than whatever judges it believes.
        Read off a runtime constant instead, the two drifted the only way
        they can: the policy granted an escape the settings forbade, and the
        runtime dropped it without a word.
        """

    def roots(self) -> list[AccessibleRoot]:
        """The folders this wall lets the session reach beside its working tree."""
        return []

    def granted(self) -> list[Device]:
        """The devices this wall grants the session."""
        return []

    def confinement(self) -> "InnerSandbox | None":
        """The runtime's own sandbox this wall establishes, or ``None`` where it stands down."""
        return None


class OuterContainer(Sandbox, frozen=True):
    """The verified container is the boundary, and the runtime's own sandbox stands down.

    A wall that has to be weakened to start nested is worth less than saying
    plainly which wall is load-bearing: in an unprivileged container the
    runtime's own confinement cannot start, so inside one the container and
    its egress proxy are the whole boundary.
    """

    mounts: list[Mount] = []
    """Folders mounted beside the working tree, ahead of the machine's standing ones."""

    devices: list[Device] = []
    """Devices the container is granted, ahead of the machine's standing grants."""

    def posture(self) -> LaunchSandbox:
        return LaunchSandbox.OUTER

    def enforcement(self) -> SandboxPosture:
        return SandboxPosture(contained=True)

    def roots(self) -> list[AccessibleRoot]:
        return [mount.root() for mount in self.mounts]

    def granted(self) -> list[Device]:
        return list(self.devices)


class InnerSandbox(Sandbox, frozen=True):
    """The session opens on the host, inside the runtime's own workspace-write sandbox."""

    mounts: list[Mount] = []
    """Folders the sandbox's write root widens to, beside the working tree."""

    escapable: bool = Field(
        default=False,
        description=(
            "Whether a command may ask to run outside the sandbox, for the "
            "policy to judge. Claude Code spells it allowUnsandboxedCommands, "
            "which a launched CLI already answers yes to on its own, so a "
            "launch says it only to refuse; Codex has no per-command way out "
            "of its envelope, and refuses a declaration asking for one"
        ),
    )

    excluded_commands: list[str] = Field(
        default=[],
        description=(
            "Command prefixes run outside the sandbox, beside the ones the "
            "declared policy excludes. For a session whose policy is composed "
            "by its caller rather than declared: a spawned session inherits "
            "none of the launching shell's settings files, so an exclusion "
            "stated there reaches it only by being named here. Codex has no "
            "way to take a command out of its envelope, and refuses one"
        ),
    )

    def posture(self) -> LaunchSandbox:
        return LaunchSandbox.INNER

    def enforcement(self) -> SandboxPosture:
        """Confined, and escapable exactly where the sandbox lets a command out.

        The two halves are not equally firm, and only one of them settles its
        own question. ``escapable`` decides the escape outright — off, the
        per-call argument is ignored. The sandbox only asks for confinement:
        where one cannot start, Claude Code warns and runs the session
        unconfined, and this reports a boundary that is not there. The CLI
        settles that with a fail-if-unavailable setting the SDK's sandbox
        settings do not carry; until they do, the placement is settled here
        and the confinement is asserted.
        """
        return SandboxPosture(active=True, escapable=self.escapable)

    def roots(self) -> list[AccessibleRoot]:
        return [mount.root() for mount in self.mounts]

    def confinement(self) -> "InnerSandbox | None":
        return self


class NoSandbox(Sandbox, frozen=True):
    """No wall: the session opens on the host under the semantic policy alone."""

    def posture(self) -> LaunchSandbox:
        return LaunchSandbox.NONE

    def enforcement(self) -> SandboxPosture:
        return SandboxPosture()


type SessionSandbox = OuterContainer | InnerSandbox | NoSandbox
"""Which wall a session opens behind."""


def declared_policy(
    plugin: Harness | Path | None, policy: HookSet | None
) -> HookSet | None:
    """The policy a declaration enforces: the one it names, or its plugin harness's.

    Derived rather than restated, because a harness compiles its policy into
    the plugin it builds: a second one named beside it would judge a session
    opened in process by one policy and a launched session by another, under
    one declaration. So a different one is refused, and the same one is
    merely redundant.
    """
    carried = (
        next((each.hooks for each in plugin.plugins if each.hooks is not None), None)
        if isinstance(plugin, Harness)
        else None
    )
    if policy is not None and carried is not None and policy != carried:
        raise ValueError(
            f"policy {policy.id!r} is not the policy {carried.id!r} the plugin "
            "harness declares, which its plugin already enforces; drop policy, "
            "or declare it in the harness"
        )
    return policy if policy is not None else carried


def settled_sandbox(asked: LaunchSandbox | None, stated: str) -> LaunchSandbox:
    """The sandbox a launch opens under: the one asked for, or the default the host holds.

    ``None`` is a launch that named no sandbox, the one answer with anything
    left to settle. The default is the verified container, which a host with
    no container client cannot start, and refusing there would leave the
    plain launch unopenable on every machine without Docker or Podman. So the
    default opens under the runtime's own sandbox instead, and says so at
    warning level: what would restore the container, and ``stated`` — how
    whoever launched this chooses the inner sandbox without the warning.

    Only the default degrades. A container asked for by name reaches the
    container's own argv and is refused there, because a launch that asked
    for a boundary and opened without one is the failure the boundary exists
    to rule out. A client that answers and cannot drive the engine behind it
    is not an absence either: the operator has an engine to repair rather
    than one to install, and the refusal there says which.

    Asked of :func:`~lup.harness.image.detected_client`, the probe the
    container's argv is built from, so the two cannot disagree about whether
    this host has an engine.
    """
    if asked is not None:
        return asked
    if detected_client() is not None:
        return LaunchSandbox.OUTER
    Notice(
        text=(
            "No working Docker or Podman client was found, so this session "
            "runs under the inner sandbox on the host. Install Docker or "
            "Podman to launch in the container (outer), or "
            f"{stated} to choose this without the warning."
        ),
        urgency="warning",
    ).say()
    return LaunchSandbox.INNER


def launched_sandbox() -> SessionSandbox:
    """The wall a launch declaring none opens behind: the container where an engine answers."""
    return (
        OuterContainer()
        if settled_sandbox(None, "declare sandbox=InnerSandbox()").contained()
        else InnerSandbox()
    )


class Member(BaseModel, frozen=True, extra="forbid"):
    """Who this session is on the repository's coordination roster, and where it is woken.

    A launched session is minted an identity before it starts, because its
    tool server and its hooks are separate processes with no channel between
    them, and an id each worked out for itself would put one session on the
    roster twice.
    """

    name: str | None = None
    """What the roster calls this session; unset, the worktree's name, numbered
    past any live session already answering to it."""

    wake_sockets: WakeSockets | None = WakeSockets()
    """Where this session binds the wake socket a peer nudges it through, keyed
    by its id; ``None`` binds none, and the session reads its mail at its next
    tool call instead."""


class Recording(BaseModel, frozen=True, extra="forbid", arbitrary_types_allowed=True):
    """What is kept of a session besides the runtime's own record of it."""

    transcript: bool = False
    """Render the runtime's native transcript into the run's own, as it is written."""

    ledger: SessionRecorder | None = None
    """Where the session is recorded as opened and closed, or nowhere."""

    root: Path | None = None
    """Where the run's journal and transcript are written; unset, the project's runs."""


type RecordedSessions = Callable[[], Awaitable[list[SessionSummary]]]
"""The sessions a runtime has on record for a workspace, newest first."""


class Reopening(BaseModel, ABC, frozen=True, extra="forbid"):
    """Which earlier session the next one reopens, in words every runtime spells."""

    @abstractmethod
    def resumption(self) -> Resumption:
        """This reopening as each runtime's own launch spelling reads it."""

    @abstractmethod
    async def reopened(self, recorded: RecordedSessions) -> SessionId:
        """The session a session opened in process resumes, from what is on record."""


class Latest(Reopening, frozen=True):
    """Reopen the most recent session in this workspace, without choosing one."""

    def resumption(self) -> Resumption:
        return Resumption(latest=True)

    async def reopened(self, recorded: RecordedSessions) -> SessionId:
        newest = next(iter(await recorded()), None)
        if newest is None:
            raise ValueError(
                "there is no earlier session in this workspace to continue"
            )
        return newest.id


class Pick(Reopening, frozen=True):
    """Offer the runtime's own picker over this workspace's sessions.

    A picker is a terminal's, so only a launch can take it; a session opened
    in process refuses one and asks for :class:`Latest` or a session id.
    """

    def resumption(self) -> Resumption:
        return Resumption(pick=True)

    async def reopened(self, recorded: RecordedSessions) -> SessionId:
        del recorded
        raise ValueError(
            "Pick() offers the runtime's own picker, which only a launched "
            "terminal has; name the session, or declare Latest()"
        )


class Reopen(Reopening, frozen=True):
    """Reopen one session by the id its runtime knows it as."""

    session: SessionId

    def resumption(self) -> Resumption:
        return Resumption(session=self.session.value)

    async def reopened(self, recorded: RecordedSessions) -> SessionId:
        del recorded
        return self.session


type Resume = Latest | Pick | Reopen
"""Which earlier session a launch reopens: ``Latest()``, ``Pick()``, or ``Reopen(session=...)``."""


def resumption(resume: Reopening | None) -> Resumption:
    """A declared reopening in the shape each runtime's own spelling reads."""
    return Resumption() if resume is None else resume.resumption()


class LaunchCommand(BaseModel, frozen=True):
    """The exact process an interactive launch runs: its argv, environment and directory.

    Public so a launch can be printed, compared, or run by something other
    than :meth:`launch`; ``str()`` of one is its argv as a shell would read it.
    """

    argv: list[str]
    env: EnvVars
    """The whole environment the process runs with, not only what the launch set."""

    cwd: Path

    def exported(self, inherited: EnvVars) -> EnvVars:
        """The variables this launch sets or changes over ``inherited``."""
        return {
            name: value
            for name, value in self.env.items()
            if inherited.get(name) != value
        }

    def __str__(self) -> str:
        return shlex.join(self.argv)


@runtime_checkable
class LaunchStep(Protocol):
    """Repository workflow a launch runs around a session without owning it.

    A checkpoint, a base-freshness sync, a companion tree regenerated: work a
    repository wants done at a launch's edges that is no capability of the
    session. ``before`` runs ahead of everything the launch does, in the order
    the steps were given; ``after`` runs once the session has ended, in the
    reverse order, the way nested ``with`` blocks unwind, for every step whose
    ``before`` ran — a session that failed or was interrupted included, which
    ``succeeded`` says.
    """

    def before(self) -> None: ...

    def after(self, succeeded: bool) -> None: ...
