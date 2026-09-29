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
from typing import Protocol, Self, runtime_checkable

from pydantic import BaseModel, Field, field_validator

from lup.harness.devices import Device
from lup.harness.image import Image, MemoryLimit, SessionPrivileges, detected_client
from lup.harness.messaging import WakeSockets
from lup.harness.models import Harness, HookSet, PromptDocument, Resumption
from lup.harness.notice import Notice
from lup.harness.requirements import Manifest
from lup.observability.sessions import SessionRecorder
from lup.policy.enforcement import SandboxPosture
from lup.sandbox.models import NetworkMode
from lup.sandbox.rail import AccessibleRoot, NestedRepository
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

    mounts: list[Mount] = []
    """Folders outside the working tree this session is meant to reach.

    Each wall reaches them its own way — the container mounts them, the inner
    sandbox widens its write root to them — and every wall hands them to the
    policy judging the session, which reads them as reachable and accepts
    their repositories' policies, so a folder is the session's the same way
    whichever wall it opens behind."""

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
        return [mount.root() for mount in self.mounts]

    def widened(self, mounts: list[Mount]) -> Self:
        """This wall reaching ``mounts`` too, after the ones it declares."""
        return self.model_copy(update={"mounts": [*self.mounts, *mounts]})

    def granted(self) -> list[Device]:
        """The devices this wall grants the session."""
        return []

    def named_image(self) -> Image | None:
        """The image this wall's container runs, where it names one."""
        return None

    def networked(self, image: Image) -> Image:
        """``image`` on the network this wall's container joins; a host wall joins none."""
        return image

    def privileges(self) -> SessionPrivileges:
        """What this wall lets a session's processes come to hold: nothing, unless it grants sudo."""
        return SessionPrivileges()

    def memory_limit(self) -> MemoryLimit | None:
        """How much memory this wall's container may hold, where it bounds it."""
        return None

    def holds_generated(self) -> bool:
        """Whether this wall holds the generated trees the runtime runs from read-only."""
        return False

    def held_guidance(self) -> PromptDocument | None:
        """The guidance this wall puts over the committed one, where it swaps it."""
        return None

    def nested(self) -> list[NestedRepository]:
        """The repositories inside the checkout this wall holds as it holds the checkout's own."""
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

    What the container grants its session — its network, memory, sudo,
    devices, folders and held trees — may be stated by several hands at
    once, each an ``OuterContainer`` saying only what it sets, laid one over
    the next by :meth:`over`: a launch's command line over its mode, over the
    person's config, over the project.
    """

    devices: list[Device] = []
    """Host devices the container is granted."""

    image: Image | None = None
    """The image the container runs and how it is started; unset, the plugin
    harness's own, or lup's default image where the plugin is no harness."""

    sudo: bool = False
    """Whether the session may become the container's root through ``sudo``,
    without a password, to administer the container: install a system
    package, say. Unset, every capability is dropped and no process may gain
    one. Granted only on a rootless engine, where that root is an
    unprivileged user on the host; a rootful engine refuses the launch. What sudo
    installs vanishes with the container, so a package the session keeps
    needing belongs in the image's ``tooling``."""

    nested_repositories: list[NestedRepository] = []
    """Repositories kept inside the checkout whose ``config`` and ``hooks/``
    the container holds read-only, as it holds the checkout's own, since the
    host's git runs what they name; their pointers are verified on the host
    at every launch. One marked ``create`` is initialized on the host when
    absent, so no session writes its configuration first."""

    network: NetworkMode | None = None
    """The network the container joins: ``filtered`` behind the egress proxy,
    ``bridge``, ``host`` or ``none``, as :class:`~lup.harness.egress.SessionEgress`
    describes each. Unset, the image's own."""

    memory: MemoryLimit | None = None
    """How much memory the container may hold, an amount or a share of what
    the engine can hand out; unset, the engine's default, which is no limit."""

    hold_generated: bool = False
    """Whether the generated trees the runtime runs from are held read-only:
    the plugin whose hooks judge the session, and the project settings and
    guidance the runtime reads, so a session cannot change what judges it.
    Regenerating them is then the host's work — a session asking is told so
    before anything is written — as is any git command rewriting them in
    this checkout: a merge, a switch or a reset that touches them. Only this
    checkout's trees; a sibling worktree's stay the session's to regenerate."""

    guidance: PromptDocument | None = None
    """The always-loaded document a session in this container reads instead
    of the committed one: rendered for the runtime the way generation renders
    the project's, held to the same budget, and mounted read-only over the
    committed file's path inside the container, so the tree on the host never
    changes. It names the module declaring it, as a rendered file does. Only
    a container can put one file over another; a host session refuses one."""

    @field_validator("guidance")
    @classmethod
    def guidance_names_its_source(
        cls, value: PromptDocument | None
    ) -> PromptDocument | None:
        """Refuse guidance naming no module, which the file it renders to must name."""
        if value is not None and value.source is None:
            raise ValueError(
                "guidance a container holds renders to a file of its own, so it "
                "names the module declaring it: PromptDocument(parts=..., source=...)"
            )
        return value

    def posture(self) -> LaunchSandbox:
        return LaunchSandbox.OUTER

    def enforcement(self) -> SandboxPosture:
        return SandboxPosture(contained=True)

    def granted(self) -> list[Device]:
        return list(self.devices)

    def named_image(self) -> Image | None:
        return self.image

    def networked(self, image: Image) -> Image:
        if self.network is None:
            return image
        egress = image.egress.model_copy(update={"mode": self.network})
        return image.model_copy(update={"egress": egress})

    def privileges(self) -> SessionPrivileges:
        return SessionPrivileges(sudo=self.sudo)

    def memory_limit(self) -> MemoryLimit | None:
        return self.memory

    def holds_generated(self) -> bool:
        return self.hold_generated

    def held_guidance(self) -> PromptDocument | None:
        return self.guidance

    def nested(self) -> list[NestedRepository]:
        return list(self.nested_repositories)

    def over(self, lower: "OuterContainer") -> "OuterContainer":
        """These settings laid over ``lower``'s, as a higher hand's over a lower one's.

        A setting this one states wins, even said as its default, so a
        ``--no-sudo`` takes back a mode's sudo; one it leaves is ``lower``'s.
        The folders, devices and nested repositories either names are all
        granted, this one's first, since a grant is not something a higher
        hand overrules by naming another. The result remembers what either
        stated, so it lays over the next one down the same way.
        """
        granted = {
            "mounts": list(dict.fromkeys([*self.mounts, *lower.mounts])),
            "devices": list(dict.fromkeys([*self.devices, *lower.devices])),
            "nested_repositories": list(
                dict.fromkeys([*self.nested_repositories, *lower.nested_repositories])
            ),
        }
        stated = {
            name: getattr(self, name)
            for name in self.model_fields_set
            if name not in granted
        }
        joined = {name: value for name, value in granted.items() if value}
        return lower.model_copy(update={**stated, **joined})


class InnerSandbox(Sandbox, frozen=True):
    """The session opens on the host, inside the runtime's own workspace-write sandbox."""

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

    def confinement(self) -> "InnerSandbox | None":
        return self


class NoSandbox(Sandbox, frozen=True):
    """No wall: the session opens on the host under the semantic policy alone.

    Its mounts widen no wall, since there is none: they are what the policy
    reads as the session's own beside its working tree.
    """

    def posture(self) -> LaunchSandbox:
        return LaunchSandbox.NONE

    def enforcement(self) -> SandboxPosture:
        return SandboxPosture()


type SessionSandbox = OuterContainer | InnerSandbox | NoSandbox
"""Which wall a session opens behind."""


def laid_over[T: BaseModel](preset: T, base: T) -> T:
    """``base`` with every field ``preset`` states taken from it: a preset over a declaration.

    A preset states only what it changes — a ``Claude(permission_mode="auto")``
    names one field and leaves the rest — so what it left unstated is
    ``base``'s, and one it states is its own even said as the default. A field
    holding a declaration of the same kind on both sides is laid the same
    way, field by field, so a preset moving its record's ``root`` keeps the
    ledger the base records to; anything else it states, a list included,
    replaces the base's whole. The result is validated again as a whole, so a
    combination neither side refused alone is refused where they meet.
    """

    def stated(name: str) -> object:
        value = getattr(preset, name)
        under = getattr(base, name)
        if isinstance(value, BaseModel) and type(value) is type(under):
            return laid_over(value, under)
        return value

    laid = base.model_copy(
        update={name: stated(name) for name in preset.model_fields_set}
    )
    return type(base).model_validate(laid)


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


def declared_requirements(
    plugin: Harness | Path | None, requirements: Manifest | None
) -> Manifest:
    """What a launch checks the host and the container for: the roster named, or the harness's.

    A harness declares the requirements its plugin's sessions stand on beside
    the plugin itself, so a declaration compiling one checks those; a plugin
    built elsewhere says nothing of what it needs, so its declaration names
    them, and one naming none checks nothing beyond the runtime itself.
    """
    if requirements is not None:
        return requirements
    return plugin.requirements if isinstance(plugin, Harness) else Manifest()


def declared_image(plugin: Harness | Path | None, sandbox: "SessionSandbox") -> Image:
    """The image a contained session runs: the container's own, the harness's, or lup's.

    On the network the container names, where it names one, since which
    network a session joins and the proxy its environment points at are one
    fact the image's egress holds.
    """
    named = sandbox.named_image()
    if named is not None:
        return sandbox.networked(named)
    return sandbox.networked(plugin.image if isinstance(plugin, Harness) else Image())


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
    """Where the run's journal and transcript are written; unset, the project's
    runs. A relative one is in the checkout the session works in, wherever the
    launching process stands."""

    mode: str | None = None
    """The named kind of session this is, written into its record, so a run
    copied out of the directory that sorts it still says what it was."""


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
