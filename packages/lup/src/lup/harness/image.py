"""The image an agent session runs in, rendered from what the project declares.

An image assembled by hand beside a manifest can disagree with it, and the
disagreement surfaces late: the session opens, and the toolchain the manifest
promised is absent, and the absence reads as whatever the missing program says
about itself. So the package list is not written here. It comes off
:meth:`Manifest.packages`, the same roster the preflight exercises, which is
what makes "declared" and "installed" one fact instead of two that agree until
somebody edits one.

What this module adds to that roster is everything a package list cannot say:
which layer each part belongs to, so a ``uv add`` costs a sync rather than a
rebuild; which paths stay container-private, so the host's tooling and the
container's do not fight over one ``.venv``; which directories outlive the
container, so a rebuild does not re-download the world; and which identity the
process runs under, so bind-mounted files do not land root-owned on the host.

Three fields answer things that were measured rather than reasoned, against
Claude Code 2.1.237 on rootless podman 6.1.0, and each records its measurement
where the field is declared: ``inner_sandbox``, ``trusted_projects``, and the
same-path mounting that :func:`run_arguments` refuses to spell any other way.
"""

import json
from collections.abc import Callable, Mapping, Sequence
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Literal, Self

import sh
from pydantic import (
    BaseModel,
    ByteSize,
    Field,
    TypeAdapter,
    ValidationError,
    model_validator,
)

from lup.harness.browser import BrowserBridge
from lup.harness.clipboard import ClipboardBridge, shim_program
from lup.harness.devices import Device
from lup.harness.credential import (
    ForgeCredential,
    GitAccess,
    GitIdentity,
    NoCredential,
    RemoteRewrite,
)
from lup.harness.egress import SessionEgress
from lup.harness.environment import NON_INTERACTIVE_SHELL_ENV
from lup.harness.messaging import WakeSockets
from lup.harness.requirements import Manifest, Package, PackageManager
from lup.harness.terminal import TerminalHandoff
from lup.types import EnvVars, JsonObject


class CacheVolume(BaseModel, frozen=True):
    """A directory whose contents outlive the container that filled it.

    Named volumes rather than bind mounts, because these hold artifacts built
    against the *image's* libc and interpreter. A host cache bound into a
    container mixes wheels compiled for two platforms into one directory, and
    the resulting failure names a package rather than the mount that broke it.
    """

    name: str = Field(description="Volume name, unique to this project")
    path: str = Field(description="Absolute path the volume is mounted at")
    variable: str = Field(
        default="",
        description=(
            "Environment variable that points its tool at this path. Empty "
            "when the tool already defaults here and naming it would only "
            "add a second place the path is written"
        ),
    )
    because: str = Field(
        default="",
        description=(
            "What made this necessary. A boundary answered by widening its "
            "own declaration teaches an agent that every wall is answered by "
            "widening it, and volumes then accrete with nothing removing "
            "one -- each perfectly defensible when written and nobody "
            "afterwards able to say whether it is still earning its place. "
            "Empty is accepted and reported, because refusing it would make "
            "the honest answer -- 'I do not remember' -- unwritable"
        ),
    )

    def environment(self) -> EnvVars:
        """The variable pointing this volume's tool at it, where there is one."""
        return {self.variable: self.path} if self.variable else {}

    def mount_arguments(self) -> list[str]:
        """The run arguments that attach this volume."""
        return ["-v", f"{self.name}:{self.path}"]


class Capability(StrEnum):
    """One capability a session's root may be given back, as both engines name it.

    What administering the container's own files takes, and nothing else:
    owning and moving files it does not own (``CHOWN``, ``DAC_OVERRIDE``,
    ``FOWNER``, ``FSETID``), switching user (``SETUID``, ``SETGID``, which
    sudo and pacman's download user both do), signalling its own processes
    (``KILL``), running install scriptlets in a chroot, as pacman does even
    for the root it runs in (``SYS_CHROOT``), restoring the file capabilities
    a package ships (``SETFCAP``), and writing the audit record sudo keeps
    (``AUDIT_WRITE``). Each is in both engines' own default set.

    A capability reaching past the container -- mounts and namespaces, the
    network stack, other processes' memory, the clock, kernel modules, files
    by handle -- has no member, so no declaration can spell one.
    """

    AUDIT_WRITE = "AUDIT_WRITE"
    CHOWN = "CHOWN"
    DAC_OVERRIDE = "DAC_OVERRIDE"
    FOWNER = "FOWNER"
    FSETID = "FSETID"
    KILL = "KILL"
    SETFCAP = "SETFCAP"
    SETGID = "SETGID"
    SETUID = "SETUID"
    SYS_CHROOT = "SYS_CHROOT"


class SessionPrivileges(BaseModel, frozen=True, extra="forbid"):
    """What a session's processes may hold inside its container, and may come to hold.

    Every capability is dropped, and none is given back unless the session
    may administer its container. The session runs as the operator's own uid
    with an empty effective set, measured, so dropping the bounding set costs
    it nothing it used: what the drop closes is the way back up, since a
    setuid-root binary is granted from that set and the image carries
    thirteen (``su``, ``mount`` and ``passwd`` among them). ``no-new-privileges``
    stops the exec that would do the granting.

    ``sudo`` is the one widening, and it is two changes at once: the
    administering capabilities come back into the bounding set for root to
    hold, and ``no-new-privileges`` is lifted, since sudo is itself a setuid
    binary. What the agent holds stays empty either way, because the
    entrypoint clears the inheritable and ambient sets an engine hands a
    non-root user before it starts the agent.
    """

    sudo: bool = Field(
        default=False,
        description=(
            "Whether the session may become the container's root through "
            "``sudo``, without a password"
        ),
    )

    def capabilities(self) -> list[Capability]:
        """The capabilities given back after every one is dropped."""
        return list(Capability) if self.sudo else []

    def arguments(self) -> list[str]:
        """The run arguments that set these privileges, in either engine's words."""
        return [
            "--cap-drop",
            "ALL",
            *[word for held in self.capabilities() for word in ("--cap-add", held)],
            *([] if self.sudo else ["--security-opt", "no-new-privileges:true"]),
        ]


class MemoryLimit(BaseModel, frozen=True, extra="forbid"):
    """How much memory one session's container may hold: an amount, or a share.

    A share is what a committed declaration can state, because it means the
    same on every machine: half of what the engine can hand a container is a
    sentence two hosts with different memory both honour, where an amount is
    one machine's fact and belongs in that machine's person's config or on one
    launch's command line. Spelled as a byte size (``12GiB``, ``512MiB``, a
    plain number of bytes) or a percentage (``75%``).
    """

    amount: ByteSize | None = Field(default=None, gt=0)
    """Bytes, whatever the machine holds."""

    percent: int | None = Field(default=None, ge=1, le=100)
    """A share of the memory the container engine can hand out."""

    @model_validator(mode="before")
    @classmethod
    # lup: ignore[bare-object] — a before-hook receives whatever the caller
    # wrote, which is the untyped boundary the rule says to narrow at
    def spelled(cls, value: object) -> object:
        """Read the one-word spelling a flag or a config file writes."""
        if not isinstance(value, str):
            return value
        if value.strip().endswith("%"):
            return {"percent": value.strip().removesuffix("%")}
        return {"amount": value}

    @model_validator(mode="after")
    def one_measure(self) -> Self:
        """Refuse a limit stating both measures, or neither."""
        if (self.amount is None) == (self.percent is None):
            raise ValueError("a memory limit is an amount or a percentage, not both")
        return self

    def resolved(self, total: int) -> int:
        """The limit in bytes, given how much the engine can hand out."""
        if self.amount is not None:
            return int(self.amount)
        return total * (self.percent or 0) // 100

    def described(self, total: int) -> str:
        """The limit as a launch says it, with the share it stands for."""
        held = ByteSize(self.resolved(total)).human_readable(decimal=False)
        if self.percent is None:
            return held
        whole = ByteSize(total).human_readable(decimal=False)
        return f"{held}, {self.percent}% of the engine's {whole}"


class Registry(BaseModel, frozen=True):
    """A manager that installs by registry name, and the command that drives it.

    Declared rather than hardcoded in the renderer so a project may swap the
    tool without forking it -- and so the roster of managers that install
    something *verifiable* is one list, which is what the rendering iterates.
    A manager absent from this list installs nothing by registry, which is the
    correct treatment of ``script``.
    """

    manager: PackageManager = Field(description="Which manager this drives")
    command: str = Field(description="The install command, taking names as arguments")
    release_url: str = Field(
        default="",
        description=(
            "Where this manager's registry answers for a package's current "
            "release, as a URL template taking ``{name}``, answering JSON "
            "with a top-level ``version``. Empty means the registry is "
            "never asked, and an unpinned package is left to the build -- "
            "the right declaration for a registry whose answer has another "
            "shape, like PyPI's nested ``info.version``, until something "
            "needs it"
        ),
    )


class ContainerEngine(BaseModel, frozen=True):
    """How one container runtime spells the identity a session runs under.

    The base carries the portable spelling and each engine adds only what is
    its own, because the difference is not cosmetic: a bind mount carries uid
    numbers rather than names, so an engine that remaps them writes files the
    host user cannot read back. Measured -- ``--userns=keep-id`` is podman's
    word for "do not remap", and Docker 29.7.2 refuses it outright with
    ``--userns: invalid USER mode``, so a launcher that spelled one engine's
    requirement unconditionally could not start under the other.

    The spelling is the *client's*, because the client is what accepts or
    rejects a flag: a Docker CLI refuses ``--userns=keep-id`` before the
    daemon ever sees it, whatever the daemon happens to be. Which engine is
    *behind* that client is a second question, and :class:`ContainerClient`
    is where the two are held together -- a Docker CLI pointed at a podman
    socket by ``DOCKER_HOST`` is a real configuration, and it is one no
    spelling can rescue, because the flag podman needs is the flag this
    client refuses to send.
    """

    binary: str = Field(description="The executable that starts a container")
    memory_report: str = Field(
        default="{{.MemTotal}}",
        description="The `info` template this engine answers its memory in",
    )

    def identity_arguments(self, uid: int, gid: int) -> list[str]:
        """Run the session as this uid and gid, in the words this engine takes."""
        return ["--user", f"{uid}:{gid}"]

    def memory_total(self) -> int | None:
        """How much memory this engine can hand a container, or nothing it said.

        Asked of the engine rather than read off the host, because the two
        differ exactly where a share would go wrong: an engine inside a
        virtual machine hands out the machine's memory, not the laptop's.
        """
        try:
            answered = str(
                sh.Command(self.binary)("info", "--format", self.memory_report)
            ).strip()
        except (sh.CommandNotFound, sh.ErrorReturnCode):
            return None
        return int(answered) if answered.isdigit() else None

    def rootless(self) -> bool:
        """Whether this engine runs as an unprivileged host user.

        Where it does, the container's root is that user on the host and
        holds nothing there; where it does not, it is the host's root, held
        back only by what the container drops. Unanswered is not rootless,
        since the answer is what a widening rests on.
        """
        return False


class Docker(ContainerEngine, frozen=True):
    """Docker, which maps container uids to host uids without being told.

    A rootless Docker install maps the host user onto container root through
    its own userns, which this does not attempt to correct: the remedy there
    is ``--userns=host`` or a daemon-side mapping, both of which are postures
    an operator chooses rather than facts a launcher can read off the client.
    """

    binary: str = "docker"

    def rootless(self) -> bool:
        """Whether the daemon lists ``rootless`` among its security options."""
        try:
            answered = str(
                sh.Command(self.binary)("info", "--format", "{{json .SecurityOptions}}")
            )
            options = TypeAdapter(list[str]).validate_json(answered)
        except (sh.CommandNotFound, sh.ErrorReturnCode, ValidationError):
            return False
        return any(
            option == "name=rootless" or option.startswith("name=rootless,")
            for option in options
        )


class Podman(ContainerEngine, frozen=True):
    """Podman, which remaps into the subuid range unless told to keep the id."""

    binary: str = "podman"
    memory_report: str = "{{.Host.MemTotal}}"

    def identity_arguments(self, uid: int, gid: int) -> list[str]:
        """The portable spelling, plus podman's word for leaving the id alone."""
        return [*super().identity_arguments(uid, gid), "--userns=keep-id"]

    def rootless(self) -> bool:
        """Whether podman reports itself rootless, as it does per invoking user."""
        try:
            answered = str(
                sh.Command(self.binary)(
                    "info", "--format", "{{.Host.Security.Rootless}}"
                )
            )
        except (sh.CommandNotFound, sh.ErrorReturnCode):
            return False
        return answered.strip() == "true"


def reported_version(name: str) -> str:
    """What one container client says when asked who it is.

    Separated from the detection so a caller can answer for a client that is
    not installed here -- which is what makes the ``podman-docker`` case
    testable on a machine that does not have it.
    """
    return str(sh.Command(name)("--version"))


def reported_server(name: str) -> str:
    """What the engine *behind* one client says it is, in its own components.

    A separate question from :func:`reported_version`, and asked separately
    because the two disagree on a real host: ``DOCKER_HOST`` pointed at a
    podman socket leaves a Docker CLI reporting Docker while every container
    it starts is podman's. The components list is what carries the name --
    podman answers with a ``Podman Engine`` entry where Docker answers with
    ``Engine`` and ``containerd`` -- and the version number alone does not,
    which is why this asks for the list rather than the shorter field.

    Unlike the client probe this one reaches a daemon, so it fails whenever
    nothing is listening. That failure is not an error here: a client with no
    running daemon is undrivable for reasons this question cannot improve on,
    and the caller reads the absence as an unknown server.
    """
    return str(sh.Command(name)("version", "--format", "{{json .Server.Components}}"))


type EngineFlavor = Literal["docker", "podman", "unknown"]
"""Which of the two engines something is, or that it did not say."""


type SessionStreams = Literal["terminal", "piped", "captured"]
"""How one container's standard streams are wired to whatever opened it.

Three states because a bool covered two and the third is a real caller. An
operator's session owns a terminal and wants ``-it``. A probe's output is
captured and wants neither, since ``-it`` against a pipe fails on the terminal
it was promised. A worker sits between them: it speaks a protocol over its
stdin, so it needs ``-i`` to be given one and must not have ``-t``, which would
put a terminal discipline in front of a stream carrying framed JSON.

Spelled as three names rather than two flags because the flags are the
engine's vocabulary and these are the situations -- and the situation is what a
caller knows. A caller reaching for ``-i`` directly is deciding a container
argument from a place that should only know it is talking over pipes.
"""


def stream_arguments(streams: SessionStreams) -> list[str]:
    """The engine flags that wire one container's standard streams."""
    match streams:
        case "terminal":
            return ["-it"]
        case "piped":
            return ["-i"]
        case "captured":
            return []


def flavor_of(reported: str) -> EngineFlavor:
    """Read an engine's own words for which of the two it is."""
    return "podman" if "podman" in reported.lower() else "docker"


class ContainerClient(BaseModel, frozen=True):
    """One container client on this host, and the engine actually behind it.

    Both halves, because either alone gets a real host wrong. The client
    decides which flags are *sendable*: the ``podman-docker`` package installs
    a ``docker`` that is podman and takes podman's arguments despite the name,
    which is why nothing here trusts the spelling of a path. The server
    decides what those flags will *mean*: ``DOCKER_HOST`` pointing a genuine
    Docker CLI at a podman socket is an ordinary developer setup, and the
    containers it starts are podman's however the client answers.

    Measured on such a host, and the reason this class exists rather than a
    bare engine: podman remaps uids unless told ``--userns=keep-id``, the
    Docker CLI rejects that flag outright, and the session's own bind-mounted
    checkout is therefore read-only to it -- ``touch: Permission denied`` on
    the tree it was opened to work on. No spelling fixes that, because the
    one word that would is the word this client will not carry.
    """

    binary: str = Field(description="The executable that starts a container")
    client: EngineFlavor = Field(description="What the CLI says it is")
    server: EngineFlavor = Field(
        default="unknown",
        description=(
            "What answers on that CLI's socket. ``unknown`` when nothing "
            "answered, which is not treated as a mismatch: a daemon that is "
            "down is a different problem with its own message, and guessing "
            "at a mismatch would name the wrong one"
        ),
    )

    def engine(self) -> ContainerEngine:
        """The identity spelling this *client* accepts, whatever runs behind it."""
        return (
            Podman(binary=self.binary)
            if self.client == "podman"
            else Docker(binary=self.binary)
        )

    def drives_its_server(self) -> bool:
        """Whether this client can express what the engine behind it requires."""
        return not (self.client == "docker" and self.server == "podman")

    def consequence(self) -> str:
        """What a session started through this client would lose, for a refusal."""
        return (
            f"`{self.binary}` is a Docker client connected to a Podman server. "
            "It cannot pass `--userns=keep-id`, which Lup needs for writable "
            "checkout mounts. Use the Podman CLI, or unset DOCKER_HOST to "
            "connect the Docker client to a Docker server."
        )


def detected_client(
    candidates: tuple[str, ...] = ("docker", "podman"),
    ask: Callable[[str], str] = reported_version,
    ask_server: Callable[[str], str] = reported_server,
) -> ContainerClient | None:
    """Which client this host should drive its containers through.

    Every candidate is asked rather than the first one that answers being
    taken, because "answers" and "can do the job" came apart on a real host:
    a Docker CLI pointed at podman answers first and cannot start a usable
    session, while the podman CLI sitting beside it can. Preferring a client
    that matches its own server picks the second without anyone having to
    know the first was there.

    A mismatched client is still returned when it is the only one, so the
    caller can refuse in that client's own terms rather than reporting a
    host with no container runtime -- which would be false, and would send
    an operator to install what they already have.

    ``None`` when no candidate answers at all, which is a real answer rather
    than an error: a host without a container client can still open an
    unconfined session, and refusing here would take that away from everyone
    who never asked for the boundary.
    """

    def probed(name: str) -> ContainerClient | None:
        try:
            reported = ask(name)
        except (sh.CommandNotFound, sh.ErrorReturnCode):
            return None
        try:
            behind = flavor_of(ask_server(name))
        except (sh.CommandNotFound, sh.ErrorReturnCode):
            behind = "unknown"
        return ContainerClient(binary=name, client=flavor_of(reported), server=behind)

    answered = [found for name in candidates if (found := probed(name)) is not None]
    return next(
        (found for found in answered if found.drives_its_server()),
        answered[0] if answered else None,
    )


class Image(BaseModel, frozen=True):
    """The container image and the run it is started with, declared together.

    One model rather than two, because the pair is only correct jointly. A
    ``UV_PROJECT_ENVIRONMENT`` baked into the image and a volume list supplied
    at run time have to name the same paths, and the way that stays true is
    that one declaration answers both.
    """

    base: str = Field(
        default=(
            "archlinux:base@sha256:"
            "63c7b061c0c001cb7ce4f8d11b63d351c23e7f97121bc5c8bd5d9f431e615d7d"
        ),
        description=(
            "Base image, pinned by digest for the reason the snapshot below "
            "is pinned by date: a floating tag never appears in a diff, and a "
            "base that moved rebuilds every layer above it on either engine. "
            "This is the manifest list `archlinux:base` resolved to on "
            "2026-09-18; bumping it is a decision somebody makes and reviews. "
            "Chosen for what its archive carries rather than for "
            "size. Measured against Debian stable: ``gh``, ``bun`` and ``uv`` "
            "are all absent there, so each would have to be fetched by a "
            "shell line the build executes unverified -- three holes opened "
            "in the boundary this harness exists to close. Every one of them "
            "is a signed package here, checked before it unpacks"
        ),
    )
    snapshot: str = Field(
        default="2026/08/20",
        description=(
            "Archive snapshot the distribution is pinned to, as YYYY/MM/DD. "
            "A rolling distribution is the cost of the archive above, and "
            "this is what pays it: the build resolves against "
            "archive.archlinux.org rather than today's mirrors, so rebuilding "
            "in six months installs the same versions rather than whatever "
            "has landed since. Bumping the date is a decision somebody makes "
            "and reviews, which is the property a floating base tag cannot "
            "offer because it never appears in a diff"
        ),
    )
    baseline: list[str] = Field(
        default=[
            "ca-certificates",
            "curl",
            "git",
            "jq",
            "less",
            "openssh",
            "socat",
            "procps-ng",
            "ripgrep",
            "fd",
            "python",
            "uv",
            "nodejs",
            "bun",
            "util-linux",
        ],
        description=(
            "Packages every image gets regardless of the manifest -- what a "
            "shell session needs to be usable at all, as against what this "
            "particular project's work needs. ``uv``, ``nodejs`` and ``bun`` "
            "are here because the registry managers stand on them: a package "
            "declared for ``uv`` or ``bun`` cannot install if the tool that "
            "installs it was itself left to a shell script. ``bun`` is here "
            "rather than left to the manifest because ``agent_clis`` installs "
            "through it in every image, and a project with no JavaScript of "
            "its own has no reason to declare it: such a build reached the "
            "layer carrying the runtimes the session exists to run and died "
            "at ``bun: command not found``. ``python`` is here for the "
            "permission dispatcher, which a native CLI starts as a bare "
            "``python3`` deliberately outside any virtual environment, so the "
            "interpreter ``uv`` manages is the one interpreter it may not "
            "use: an image carrying only that one has no policy at all. "
            "``socat`` is the relay a session reaches for when something has "
            "to be carried between two things that cannot address each other "
            "-- a port, a socket, a transport an HTTP proxy will not take. "
            "``util-linux`` is here for ``setpriv``, which the entrypoint "
            "starts the agent through so it inherits no capability. "
            "It is *not* here for the runtime's own sandbox, whose packages "
            "``inner_sandbox`` deliberately leaves unlisted; read that field "
            "before adding its companion here"
        ),
    )
    inner_sandbox: list[str] = Field(
        default=[],
        description=(
            "What the runtime's own sandbox needs in order to run inside "
            "this one. Empty, because the launcher turns that sandbox off by "
            "name for a contained session rather than leaving it to fail, "
            "which is the posture this field's own earlier reasoning named "
            "as the alternative to filling it. Filling it was tried and "
            "measured on both halves of the claim. The false half: "
            "bubblewrap cannot mount a fresh ``/proc`` in an unprivileged "
            "container -- ``Can't mount proc on /newroot/proc: Operation not "
            "permitted`` -- so the inner boundary did not stand up. The true "
            "half: their presence silenced the CLI's 'Commands will run "
            "WITHOUT sandboxing' notice, so two packages bought quiet about "
            "a boundary that was not there, which is the cry of wolf they "
            "were installed to prevent, moved rather than stopped. A project "
            "that means to keep the inner sandbox fills this and sets the "
            "runtime's own nested-sandbox option, and should read what that "
            "option costs before it does"
        ),
    )
    tooling: list[Package] = Field(
        default=[],
        description=(
            "What *this project's* work needs inside the image, which is the "
            "other side of the line ``baseline`` draws: that field is a shell "
            "being usable at all and is the library's answer, this one is the "
            "application's and starts empty. A project that reads PDFs or "
            "renders diagrams says so here, in the same call where it "
            "declares its image, rather than restating fourteen baseline "
            "names to add one -- and rather than inventing a capability. "
            "A ``Requirement`` is the other door and a narrower one: it takes "
            "a purpose, an exercise proving a machine has the thing, and a "
            "policy for going without, so a tool whose *absence* deserves a "
            "diagnostic belongs there and one that simply has to be present "
            "belongs here. Nothing exercises these, deliberately: a name the "
            "manager cannot resolve fails the build, naming it, which is a "
            "better answer than a probe. Whole packages rather than bare "
            "names, so a registry package can be pinned and reached through "
            "the manager that obtains it"
        ),
    )
    project_environment: str = Field(
        default=".venv-contained",
        description=(
            "``UV_PROJECT_ENVIRONMENT``, relative so `uv` resolves it against "
            "each project root rather than naming one directory for the whole "
            "machine. An absolute value is a single environment shared by "
            "every project mounted here, and `uv sync` is exact by default -- "
            "measured on uv 0.12.7, syncing a second project uninstalls the "
            "first and its dependencies, so a session's own toolchain does "
            "not survive an agent running `uv sync` in a mounted clone. "
            "Relative, `uv` does that keying itself for every spelling, a "
            "bare `uv run` in a shell included -- which is why this is not "
            "re-keyed from lup's own code, that reaching only the processes "
            "lup starts. What keeps the environment off the host is then the "
            "mount rather than the path: a private directory is bound at "
            "this name inside each project root, so the checkout's own "
            "`.venv` and this one never meet"
        ),
    )
    agent_clis: list[Package] = Field(
        default=[
            Package(name="@anthropic-ai/claude-code", manager="bun"),
            Package(name="@openai/codex", manager="bun"),
        ],
        description=(
            "The agent runtimes this image carries. Every runtime the "
            "harness launches belongs here: a contained launch runs `<cli>` "
            "inside the container, so a runtime missing from this list "
            "builds an image, starts a proxy, and then fails with `not "
            "found` on the one program the session existed to run -- which "
            "is what happened to Codex while the list was one hardcoded "
            "line. An empty version is resolved to the registry's current "
            "release at each contained launch and rendered as a concrete "
            "pin, so the image tag still content-addresses a real version "
            "and a new release rebuilds the one layer that installs them, "
            "which sits below everything else for that reason; declaring a "
            "version freezes it instead. The installs land in a layer the "
            "run mounts read-only, which is what stops a self-update from "
            "silently making the image disagree with what was rendered"
        ),
    )
    terminal: TerminalHandoff = Field(
        default=TerminalHandoff(),
        description=(
            "Which facts about the operator's terminal cross into the "
            "session, and the editors the image carries so that the ones "
            "naming a program name one that is there. Held on the image "
            "rather than beside the launch for the same reason the mounts "
            "are: the editor list is a layer this image builds, and the "
            "variable pointing at it is only correct if the two agree"
        ),
    )
    config_home: str = Field(
        default="/cfg",
        description=(
            "Where the runtime's configuration home sits inside the "
            "container. Container-private rather than the host's, because "
            "that directory holds the credential store and the session state "
            "of every project the operator has open, none of which this "
            "session has any business reading. What has to cross is named "
            "one file at a time. Which variable points a CLI at it is that "
            "runtime's own word and arrives from its login declaration"
        ),
    )
    browser: BrowserBridge = Field(
        default=BrowserBridge(),
        description=(
            "How a sign-in URL reaches a browser on the operator's machine, "
            "which is the one thing a contained session cannot finish alone. "
            "Declared beside the egress because it is the same subject read "
            "the other way round: that says what may leave, and this is the "
            "single narrow thing that may -- a URL, on a known sign-in "
            "address, and nothing back"
        ),
    )
    clipboard: ClipboardBridge = Field(
        default=ClipboardBridge(),
        description=(
            "How the operator's clipboard reaches a contained session without "
            "connecting to the host's display or compositor. "
            "Declared beside the browser bridge because it is the same kind "
            "of thing -- one narrow channel through the boundary, named "
            "rather than buried. The alternative it replaces is mounting the "
            "host's display socket, which on X11 would hand a confined "
            "session the ability to read and type into every other window, "
            "and on Wayland or macOS would not work at all"
        ),
    )
    wake_sockets: WakeSockets = Field(
        default=WakeSockets(),
        description=(
            "Where this session binds the wake socket a peer nudges it "
            "through, and where it finds its peers'. Declared beside the other "
            "two bridges and unlike them in what it crosses: the browser and "
            "the clipboard run between a session and its operator, and this "
            "runs between two sessions. Mounted at the same path it has "
            "outside, because the path is what a member publishes and another "
            "container reads back"
        ),
    )
    credential_seed: str = Field(
        default="/opt/lup/credential-seed",
        description=(
            "Read-only host login offered outside the writable config home. "
            "A login fingerprint applies each host change once, retaining "
            "private container renewals and unrelated authorization records"
        ),
    )
    home_seed: str = Field(
        default="/opt/lup/home-seed",
        description=(
            "Read-only directory of settings a launch hands the config home, "
            "applied at every start: each name listed in its ``managed`` file "
            "is merged three ways with the home's against what the last launch "
            "seeded, as is each key of each JSON object under ``merge/`` in the "
            "home's file of the same name. Offered outside the "
            "home for the reason the credential is: the home is the "
            "session's to write, and the seed is the person's"
        ),
    )
    registry_root: str = Field(
        default="/opt/bun",
        description=(
            "``BUN_INSTALL``: where bun keeps what it installs globally. "
            "Outside any home directory, because the build installs as root "
            "and the session runs as the host's uid -- and root's home is "
            "mode 750, so a global toolchain left there is installed into a "
            "directory the session user cannot enter. The build owns this "
            "path to the session's uid for the same reason"
        ),
    )

    def registry_bin(self) -> str:
        """Where globally installed executables land, for PATH.

        A directory on PATH rather than each tool linked by name: a list of
        names goes stale the moment a package is added, leaving a tool like
        `tsc` installed and unreachable.
        """
        return f"{self.registry_root}/bin"

    registries: list[Registry] = Field(
        default=[
            Registry(
                manager="bun",
                command="bun add -g",
                release_url="https://registry.npmjs.org/{name}/latest",
            ),
            Registry(manager="uv", command="uv tool install"),
        ],
        description=(
            "The managers that install by registry name, in the order their "
            "layers are built. ``script`` is deliberately absent: a manager "
            "listed here installs something whose version was pinned and "
            "whose integrity the lockfile recorded, and a shell line is "
            "neither"
        ),
    )
    forge: GitAccess = Field(
        default=GitAccess(),
        description=(
            "How this session reaches a remote, and what its commits claim. "
            "Part of this declaration for the same reason the egress is: the "
            "credential the container is given and the rewrite that makes it "
            "reachable are one fact, and a launcher holding half of it is "
            "how a session ends up with a token and an ssh remote it cannot "
            "use the token on"
        ),
    )
    egress: SessionEgress = Field(
        default=SessionEgress(),
        description=(
            "How the session reaches the network. Part of this declaration "
            "rather than the launcher's because the network the session "
            "attaches to and the proxy the environment points at are one "
            "fact spelled twice, and a launcher holding half of it is how a "
            "session ends up on an internal network with no way out of it"
        ),
    )
    caches: list[CacheVolume] = Field(
        default=[
            CacheVolume(
                name="lup-uv",
                path="/cache/uv",
                variable="UV_CACHE_DIR",
                because="a `uv sync` at every container start, re-downloading "
                "the whole dependency tree without it",
            ),
            CacheVolume(
                name="lup-bun",
                path="/cache/bun",
                variable="BUN_INSTALL_CACHE_DIR",
                because="`bunx tsc` and any `bun add` at container start, "
                "re-fetching every package without it",
            ),
            CacheVolume(
                name="lup-ruff",
                path="/cache/ruff",
                variable="RUFF_CACHE_DIR",
                because="ruff re-analysing every file on each check otherwise",
            ),
        ],
        description="Directories that outlive the container, to bound rebuild cost",
    )
    published_ports: list[int] = Field(
        default=[],
        description=(
            "Ports the session's container publishes to the host, so a human "
            "can open what the agent is serving. Empty by default, because "
            "publishing is the one hole in a container's surface that faces "
            "the operator's own machine and most projects serve nothing. A "
            "project doing frontend work names its dev ports here and gets "
            "them; the agent's own `curl localhost:<port>` needs nothing "
            "from this list, because that traffic never leaves the container"
        ),
    )
    pids_limit: int = Field(
        default=4096,
        description=(
            "How many processes the session may hold, spelled rather than "
            "left to the engine's default for the reason the egress proxy "
            "spells its own: a bound a reader can see in the argv is a bound "
            "somebody can argue with, where an absent flag is a bound nobody "
            "knows the value of. Measured, and the reason this is not "
            "optional -- a Docker CLI driving a podman engine is given "
            "``pids.max=1`` when the flag is absent, and a container that may "
            "hold one process cannot fork, so the entrypoint dies on its "
            "first `mkdir` with `fork: Resource temporarily unavailable` and "
            "nothing in the message names a limit. 4096 because a session "
            "runs a toolchain rather than one program: a `uv sync`, a test "
            "run and a language server are each many processes, and the "
            "number is a runaway backstop rather than a budget"
        ),
    )
    trusted_projects: list[Path] = Field(
        default=[],
        description=(
            "Checkout paths trusted *in addition to* the one the container is "
            "started against, which the entrypoint adds on its own. Normally "
            "empty, and worth understanding before filling: a fresh config "
            "home starts every workspace untrusted, and an untrusted "
            "workspace has its `permissions.allow` entries *ignored* with a "
            "notice rather than an error -- 'Ignoring 10 permissions.allow "
            "entries ... this workspace has not been trusted'. So a container "
            "that starts clean each time starts with the policy those entries "
            "encode silently switched off. Measured, and the reason the "
            "entrypoint seeds at all. What does *not* belong here is a list "
            "built by enumerating sibling directories: that bakes host paths "
            "into a portable image and rebuilds the layer whenever one "
            "appears"
        ),
    )

    def packages(self, manifest: Manifest) -> list[Package]:
        """Everything the image installs: baseline, editors, tooling, declared.

        Deduplicated with declaration order kept, so a rebuild does not
        invalidate a layer because two lists mentioned one package. The
        baseline is spelled as bare names, which parse as distribution
        packages -- correct for every one of them, and the reason the short
        spelling exists.

        The editors come off the terminal handoff rather than being listed in
        the baseline, so that the list an image installs and the list a launch
        matches an ``EDITOR`` against are one list. Written twice, they come
        apart in the direction that is hardest to see: the launch forwards a
        name it believes is carried, the layer never installed it, and the
        operator's editor fails to open with the runtime blamed for it.

        The project's own tooling sits between the library's answer and the
        manifest's, which is where it belongs in the reading as well as in
        the layer: after what a shell needs to work at all, before what a
        declared capability asked for.
        """
        return list(
            dict.fromkeys(
                [
                    *(Package(name=name) for name in self.baseline),
                    *(Package(name=name) for name in self.inner_sandbox),
                    *self.terminal.packages(),
                    *self.clipboard.packages(),
                    *self.tooling,
                    *manifest.packages(),
                ]
            )
        )

    def obtained_by(self, manifest: Manifest, manager: PackageManager) -> list[Package]:
        """The packages one ecosystem is responsible for, in declaration order."""
        return [item for item in self.packages(manifest) if item.manager == manager]

    def environment(self) -> EnvVars:
        """Every variable the image bakes in, cache pointers included.

        :data:`~lup.harness.environment.NON_INTERACTIVE_SHELL_ENV` first, and
        baked rather than passed, because a container is a spawn point like
        any other and the one easiest to miss: the launch and resolver
        flows merge these at every place they start a command, and a session
        inside the image starting with none of them costs a
        credential prompt with no terminal to answer it -- the failure the
        whole forge design exists to head off, reintroduced at the one spot
        nothing measures. Baked, so anything that starts this image gets
        it: a probe and a one-off ``run`` are as unattended as a session.

        ``LUP_CONTAINED`` says a process is inside an image this harness
        built, for anything in there that wants to know. It is not what the
        policy reads: a constant answers the same for any container built from
        this image, for a bare ``run`` holding none of the lease, and for a
        session whose launcher forwarded the variable from its own shell —
        so the placement is settled against a value minted per launch, and
        this stays a description rather than an authority. :meth:`sealed`
        keeps a run from restating it.

        ``LANG`` is baked at the handoff's fallback and overwritten at run
        time by whatever the operator's terminal answered. Both, because the
        two cover different callers: a launch carries the operator's own
        locale across, and anything that starts this image directly -- a
        probe, a one-off ``run`` -- still gets UTF-8 rather than the ASCII an
        unset ``LANG`` means.
        """
        return {
            **NON_INTERACTIVE_SHELL_ENV,
            **self.browser.environment(),
            **self.clipboard.environment(),
            "LUP_CONTAINED": "1",
            "LANG": self.terminal.fallback_locale,
            "UV_PROJECT_ENVIRONMENT": self.project_environment,
            "UV_LINK_MODE": "copy",
            **{
                name: value
                for cache in self.caches
                for name, value in cache.environment().items()
            },
        }

    def entrypoint(self) -> str:
        """The script every container of this image starts through, as the image carries it.

        Everything a session's home needs at every start, applied as the
        session's own uid before the agent runs: the config home checked
        writable, trust merged into the runtime's document, the host login
        and the person's settings seeded, the forge token handed to ``gh``.
        Its own method so that what it leaves the agent holding can be run
        and read rather than only rendered.
        """
        return f"""\
#!/bin/sh
set -e
config={self.config_home}
mkdir -p "$config"
# Said here because here is where the evidence is. A config volume filled
# under one user-namespace mapping and mounted under another belongs to a uid
# this session is not, and the shell's own report of that is `Permission
# denied` on a path -- which names neither the volume nor the mapping, and
# reads as a broken image. Nothing on the host can ask this reliably: the
# volume's directory is not always stat-able from outside, so a launcher's
# check would be silent exactly when it mattered.
if [ ! -w "$config" ]; then
  echo "lup: $config is not writable by uid $(id -u)." >&2
  echo "lup: the volume mounted there was filled by a different uid, which" >&2
  echo "lup: happens when this project's container engine or its user" >&2
  echo "lup: namespace mapping changed since the volume was created." >&2
  echo "lup: remove that volume and the next launch recreates it." >&2
  exit 1
fi
# Only for the runtime that keeps trust in a document of its own, which
# the launch names; another runtime's home would gain a stray file.
trust="${{LUP_TRUST_DOCUMENT:-}}"
# The checkout this container was started against is the one the operator
# chose when they wrote the mount and the workdir, so it is trusted here
# rather than enumerated at build time. Building the list from a directory
# listing was tried: it baked thirty-one host paths into the image, granted
# trust to directories that were not checkouts, and rebuilt the layer every
# time a worktree appeared or went. The repository the checkout belongs to
# is trusted beside it: a linked worktree's project is its main repository
# to the runtime, which asked for exactly that path and dropped the declared
# permissions with a notice when the worktree alone was trusted. Merged on
# every start rather than written once, because the document outlives the
# image in its volume, and a runtime that moves where it looks would
# otherwise meet a file nothing amends. A program rather than a jq pipeline,
# because every container on this volume runs this line and several start at
# once: trust-seed.py seeds a missing document, holds the lock, and says what
# each of its guards answers.
if [ -n "$trust" ]; then
  repository=$(git -C "$PWD" rev-parse --path-format=absolute --git-common-dir 2>/dev/null || printf '%s' "$PWD")
  case "$repository" in */.git) repository=${{repository%/.git}} ;; esac
  python3 /opt/lup/trust-seed.py /opt/lup/trust-seed.json "$config/$trust" "$PWD" "$repository"
fi
# A selected host login is applied once per change. Native renewal remains
# container-private, and unrelated records in a shared credential file survive.
if [ -n "${{LUP_CREDENTIAL_NAME:-}}" ]; then
  python3 /opt/lup/credential-seed.py {self.credential_seed} "$config/$LUP_CREDENTIAL_NAME" \\
    --keys "${{LUP_CREDENTIAL_KEYS:-[]}}" --renewable "${{LUP_CREDENTIAL_RENEWABLE:-}}"
fi
# The person's settings, handed over at every start rather than once: a file
# seeded once is the person's settings as they stood the day this volume was
# made. Merged three ways against what the last launch seeded rather than
# copied over, because another session may be running in this volume and
# have changed a setting it has yet to carry back.
if [ -f "{self.home_seed}/managed" ]; then
  python3 /opt/lup/home-seed.py {self.home_seed} "$config"
fi

# One credential, two consumers. The token crosses the boundary by name --
# `-e {self.forge.token_variable}` with no value, so it never appears in the
# argv that starts this container and never reaches anything reading `ps` --
# and `gh` reads a variable of its own. Derived in here rather than passed as
# a second `-e`, because the launcher would have to hold the value to pass
# it, which is the thing being avoided.
if [ -n "${{{self.forge.token_variable}:-}}" ]; then
  export GH_TOKEN="${self.forge.token_variable}"
fi
# Started through setpriv with its inheritable and ambient sets cleared: an
# engine hands a non-root `--user` the capabilities a run gives back as
# ambient ones -- measured on podman -- so without this the agent itself
# would hold what only sudo's root is meant to. The bounding set is left
# alone, since it is what sudo's root is granted from.
exec setpriv --inh-caps=-all --ambient-caps=-all -- "$@"
"""

    def dockerfile(
        self, manifest: Manifest, privileges: SessionPrivileges = SessionPrivileges()
    ) -> str:
        """Render the image as a Dockerfile, with sudo where ``privileges`` grant it.

        Layered by how often each part changes: the OS toolchain is baked in
        and rebuilt when the manifest changes, the agent CLIs sit last so a
        release rebuilds one layer, and the project's own dependencies are
        installed at container *start* into a cache volume rather than copied
        in. Metadata that changes together is one instruction -- one ``ENV``,
        one ``VOLUME``, one ``chmod`` -- because both engines commit a layer
        per instruction, and a layer per variable is a layer for nothing.
        That last choice is what
        makes ``uv add`` cost a sync instead of a rebuild, and it is also why
        no ``COPY`` of the project appears here -- the checkout arrives as a
        mount, at its own absolute path, for the reason
        ``same_path_mount_requirement`` explains.

        Sudo is installed only into the image of a session granted it, so
        the rule granting it is absent wherever the grant is: an image any
        other session runs carries no passwordless root however its run is
        spelled. The grant is a different image, and a different tag.
        """
        entrypoint = self.entrypoint()
        administered = (
            "\n# sudo for the uid the session runs as, which the layer above named,\n"
            "# and without a password: this image is built only for a session\n"
            "# granted it. The proxy variables are kept across it, since a package\n"
            "# manager reaches its mirrors the way the session does. What it\n"
            "# installs lasts until the container stops; the image's tooling is\n"
            "# where a package is kept.\n"
            "RUN pacman -S --noconfirm --needed sudo \\\n"
            "    && pacman -Scc --noconfirm \\\n"
            "    && printf '%s\\n' \\\n"
            "        'Defaults env_keep += \"http_proxy https_proxy HTTP_PROXY"
            " HTTPS_PROXY no_proxy NO_PROXY\"' \\\n"
            "        'ALL ALL=(ALL:ALL) NOPASSWD: ALL' \\\n"
            "        > /etc/sudoers.d/lup-session \\\n"
            "    && chmod 0440 /etc/sudoers.d/lup-session\n"
            if privileges.sudo
            else ""
        )
        installed = " \\\n        ".join(
            item.name for item in self.obtained_by(manifest, "pacman")
        )
        generated = self.terminal.generated()
        locales = (
            "\n# The locales the terminal handoff carries, compiled so that an\n"
            "# operator's own `LANG` names one that exists here. A forwarded\n"
            "# locale glibc cannot find is answered by falling back to ASCII and\n"
            "# warning once per program, which costs a session its box drawing\n"
            "# and blames neither the image nor the launch.\n"
            "RUN printf '%s\\n' \\\n        "
            + " \\\n        ".join(f"'{item.line()}'" for item in generated)
            + " >> /etc/locale.gen \\\n    && locale-gen\n"
            if generated
            else ""
        )
        registry_layers = "".join(
            f"\n# The {registry.manager} half of the toolchain, "
            f"pinned and integrity-checked.\n"
            f"RUN {registry.command} "
            f"{' '.join(item.requested() for item in obtained)}\n"
            for registry in self.registries
            if (obtained := self.obtained_by(manifest, registry.manager))
        )
        script_layer = "".join(
            f"\n# {item.name}: no manager packages this, so it installs by a shell\n"
            f"# line that checks what it fetched against a declared digest:\n"
            f"# {item.digest}\n"
            f"RUN {item.command}\n"
            for item in self.obtained_by(manifest, "script")
        )
        agent_clis = " ".join(item.requested() for item in self.agent_clis)
        opening = self.browser.script(self.egress.shares_host_loopback())
        clipping = shim_program()
        seeding = (Path(__file__).parent / "assets" / "credential_seed.py").read_text(
            encoding="utf-8"
        )
        home_seeding = (Path(__file__).parent / "assets" / "home_seed.py").read_text(
            encoding="utf-8"
        )
        trusting = (Path(__file__).parent / "assets" / "trust_seed.py").read_text(
            encoding="utf-8"
        )
        shim_names = " ".join(self.clipboard.shims)
        # Quoted, because `ENV name=value` takes whitespace as separating
        # *more* pairs: an unquoted `GIT_SSH_COMMAND=ssh -o BatchMode=yes`
        # makes `-o` a name with no value and the whole file unparseable.
        # JSON is the quoting, since its escapes are the ones this parser
        # reads and nothing here wants shell expansion -- `PATH`, which does,
        # is written literally a few lines up.
        exported = "ENV " + " \\\n    ".join(
            f"{name}={json.dumps(value)}" for name, value in self.environment().items()
        )
        volumes = "VOLUME " + json.dumps([cache.path for cache in self.caches])
        seed = json.dumps(self.seed_configuration(), indent=2)
        return f"""\
# Generated from the project's Manifest. Edit the declaration, not this file.
FROM {self.base}

# Pin the archive before anything resolves against it, so every layer below
# sees one snapshot rather than whichever mirror answered first.
RUN printf '%s\\n' \\
        'Server=https://archive.archlinux.org/repos/{self.snapshot}/$repo/os/$arch' \\
        > /etc/pacman.d/mirrorlist \\
    && pacman -Syu --noconfirm

# The OS toolchain, from the same roster the host preflight exercises. Signed
# by the distribution and verified before it unpacks.
RUN pacman -S --noconfirm --needed \\
        {installed} \\
    && pacman -Scc --noconfirm
{locales}
# Where the registry manager installs, declared before it installs anything.
# Outside any home directory: this build runs as root and the session runs as
# the host's uid, and root's home is mode 750 -- so a global toolchain left
# there is installed into a directory the session cannot enter, which reads
# as the tool having failed to install rather than as a permission.
#
# On PATH as a directory rather than each tool linked by name, because a list
# of names goes stale the moment a package is added, leaving a tool like
# `tsc` installed and unreachable.
ENV BUN_INSTALL={self.registry_root}
ENV PATH={self.registry_bin()}:$PATH
{registry_layers}{script_layer}
# Trust, seeded where a fresh config home will find it. A workspace this
# image was built for is one the operator already decided to run, but an
# unseeded config home does not know that: it discards the declared
# `permissions.allow` with a notice and continues, so the policy is off and
# nothing failed. Seeded at start rather than baked into the config home,
# because that directory is a mount and a mount hides whatever the image put
# beneath it.
COPY <<'SEED' /opt/lup/trust-seed.json
{seed}
SEED
COPY <<'ENTRY' /usr/local/bin/lup-entrypoint
{entrypoint}ENTRY
ENTRYPOINT ["/usr/local/bin/lup-entrypoint"]

COPY <<'CREDENTIAL' /opt/lup/credential-seed.py
{seeding}
CREDENTIAL
COPY <<'HOMESEED' /opt/lup/home-seed.py
{home_seeding}
HOMESEED
COPY <<'TRUST' /opt/lup/trust-seed.py
{trusting}
TRUST

# What `BROWSER` names, so a sign-in inside can reach a browser outside. The
# pipe it writes to is mounted per launch; with nothing mounted the script
# prints the URL and returns, which is what the flow falls back to.
COPY <<'OPEN' {self.browser.opener}
{opening}
OPEN

# Every name a clipboard is asked for by, pointed at one program that speaks
# to the broker in the launcher. The socket is mounted per launch; with none
# mounted each shim exits non-zero, which is what "this machine has no
# clipboard" already looked like to every caller.
COPY <<'CLIP' /usr/local/bin/clipboard_shim.py
{clipping}
CLIP
COPY <<'X11' /usr/local/bin/lup-clipboard-x11
{self.clipboard.native_program()}
X11
# One layer marks every program above executable and links the clipboard
# names: each is a metadata change, and a layer per file is a layer for
# nothing.
RUN chmod +x /usr/local/bin/lup-entrypoint {self.browser.opener} \\
        /usr/local/bin/clipboard_shim.py /usr/local/bin/lup-clipboard-x11 \\
    && ln -sf /usr/local/bin/clipboard_shim.py /usr/local/bin/lup-clipboard \\
    && for name in {shim_names}; do \\
        ln -sf /usr/local/bin/lup-clipboard "/usr/local/bin/$name"; \\
    done

# The identity the session runs as. Supplied at build time from the host's own
# uid/gid, because a bind mount carries numbers rather than names: a container
# that ran as its own root would leave root-owned files in the host checkout.
#
# Every directory the build wrote to as root and the session has to reach is
# handed over here, the registry root included. Leaving that one out is how a
# pinned toolchain ends up installed and unreachable, reported by whatever
# tried to run it rather than by the layer that misplaced it.
#
# The project environment is not among them and cannot be: it is a name
# relative to each project root, so it has no path here to create. The run
# binds a host directory over it instead, which arrives owned by the host uid
# this build is handing everything else to.
ARG UID=1000
ARG GID=1000
RUN groupadd -g $GID agent 2>/dev/null || true \\
    && useradd -u $UID -g $GID -m -s /bin/bash agent 2>/dev/null || true \\
    && mkdir -p {self.registry_root} \\
        {" ".join(c.path for c in self.caches)} \\
    && chown -R $UID:$GID {self.registry_root} \\
        {" ".join(c.path for c in self.caches)}
{administered}
{exported}

{volumes}

# Every agent runtime the harness launches, from the registry at the version
# the launch resolved (or the declaration pinned) rather than an install
# script, so what lands is what was rendered and the layer the run mounts
# read-only cannot be rewritten by a self-update. The last layer, because it
# is the one that changes most: a release lands most days, and every layer
# below an install is rebuilt with it. Handed to the session's uid in the
# same layer, since a chown in a later one copies every file it touches.
RUN bun add -g {agent_clis} \\
    && chown -R $UID:$GID {self.registry_root}

USER $UID:$GID
"""

    def run_arguments(
        self,
        checkout: Path,
        uid: int,
        gid: int,
        engine: ContainerEngine = Docker(),
        proxy_address: str = "",
        privileges: SessionPrivileges = SessionPrivileges(),
        memory: int | None = None,
    ) -> list[str]:
        """The run arguments a session is started with, mounts excluded.

        The checkout is mounted at its own absolute path and cannot be mounted
        anywhere else: a linked worktree's ``.git`` is a file holding an
        absolute ``gitdir:`` pointer, so a tree mounted elsewhere is a checkout
        pointing at a path that does not exist. The rail's own mount topology
        supplies the list; this supplies everything around it.

        The identity is the ``engine``'s to spell rather than this method's,
        because the two runtimes disagree about it in a way that is fatal
        rather than cosmetic -- see :class:`ContainerEngine`. Docker is the
        default because it is the one an adopter is likeliest to have, and it
        is a default rather than an assumption: a caller that detected podman
        passes it and gets podman's spelling.

        The egress environment is passed here rather than baked into the
        image, unlike everything in :meth:`environment`. Which network a
        session runs on is a posture, and baking it would mean an operator
        who flipped the mode paid a distribution rebuild to change one
        variable -- where the paths and the project environment really are
        facts about what was built.

        ``proxy_address`` is where the proxy sits on that network, which a
        caller reads back after starting it. It cannot be anything this
        declaration holds -- it is assigned when the container joins -- and
        the earlier attempt to avoid needing it, by addressing the proxy
        under a DNS alias, is what put a resolver on the internal network and
        left the proxy unable to resolve anything at all.

        ``--init`` is what makes the bound beside it survivable, and the two
        are one subject. Without it PID 1 is the agent runtime, which does not
        reap: every child a session orphans -- a ``git`` the runtime spawned
        and stopped waiting on, a worker thread's helper -- is reparented to
        it and stays a zombie for the life of the container, so the process
        table fills monotonically and never drains. Measured in a session a
        few hours old: 4,045 zombies against a limit of 4,096, of which 3,933
        were ``[git] <defunct>``.

        What that costs is not a message about processes. It is
        ``RuntimeError: can't start new thread`` and ``fork: Resource
        temporarily unavailable`` scattered through a suite -- 94 failures and
        151 errors in one run -- which reads exactly like the change under
        test having broken something, and cost a whole bisection of a change
        that was fine. Both engines take the flag and put a real reaper at PID
        1, so the class stops existing rather than being watched for.

        ``privileges`` is what the wall granted: every capability dropped and
        none gained, unless the session may administer its container -- see
        :class:`SessionPrivileges`. A device grant is untouched, being the
        runtime's to inject from outside the container's own capability set.

        ``memory`` is the bytes the container may hold, resolved from a
        :class:`MemoryLimit` against what this engine can hand out; unset,
        the engine's own default, which is no limit.
        """
        return [
            *engine.identity_arguments(uid, gid),
            *self.egress.attachment_arguments(checkout.name),
            "--init",
            "--pids-limit",
            str(self.pids_limit),
            *(["--memory", str(memory)] if memory is not None else []),
            *privileges.arguments(),
            *[
                argument
                for port in self.published_ports
                for argument in ("-p", f"{port}:{port}")
            ],
            "-w",
            str(checkout),
            *[
                argument
                for cache in self.caches
                for argument in cache.mount_arguments()
            ],
            *[
                argument
                for name, value in (
                    self.environment() | self.egress.environment(proxy_address)
                ).items()
                if name not in self.sealed()
                for argument in ("-e", f"{name}={value}")
            ],
        ]

    def sealed(self) -> list[str]:
        """Baked variables a run must not restate, whatever value it would give.

        ``environment()`` says ``LUP_CONTAINED`` is baked rather than passed
        "because it is a fact about where the process is, not a posture a
        caller chooses: a session that could switch it off from the outside
        would be telling the policy to relax with nothing underneath." The run
        then re-emitted the whole baked map as ``-e NAME=VALUE`` pairs, this
        one included -- so the property held only against a caller who did not
        also control the argv, which is every caller this defends against.

        Restating a baked value is a no-op at best: the image tag *is* the
        declaration digest, so an image built from a different declaration is
        a different image and gets rebuilt. What the restatement bought was a
        line in `ps` and a claim its own docstring contradicted.
        """
        return ["LUP_CONTAINED"]

    def ide_bridge(self, rendezvous: Path) -> list[str]:
        """Mount the host's editor lockfile directory into the container's config.

        A lockfile there is how an editor extension and a CLI find each other:
        a port and a token, written by one and read by the other. With the CLI
        in a container and the editor on the host the directory is on the wrong
        side and the editor connection simply never happens, so the one
        directory is bridged -- narrower than sharing the config home, which
        holds the credential store, and read-write because the rendezvous is
        answered from both ends.

        Bound at the same name inside, because the container's CLI looks for it
        under *its* configuration home and the name is the runtime's own. Which
        host directory arrives here is not the launch's home and is
        :func:`~lup.launch.container.contained_argv`'s to say.
        """
        return ["-v", f"{rendezvous}:{self.config_home}/{rendezvous.name}:rw"]

    def environment_mounts(self, environments: Mapping[Path, Path]) -> list[str]:
        """A container-private directory at each project root's environment name.

        :attr:`project_environment` is relative, so every mounted project
        resolves it inside its own bind mount -- which is the host's tree. A
        session syncing there would write its environment into the checkout
        the operator is also working in, and the interpreter paths a venv
        bakes are absolute, so the two would overwrite each other's. Binding
        a private directory at that name is what makes the relative value
        safe, and it is per root rather than one shared directory because
        sharing one is the collision this whole arrangement exists to end.

        Host binds rather than named volumes, which is a departure from
        :class:`CacheVolume` and deliberate. That class prefers named volumes
        because a cache *shared with the host* mixes wheels built for two
        platforms, and nothing here is shared: only the container ever reads
        this path, so the objection does not reach it. What decides it
        instead is ownership. A bind arrives owned by whoever owns it on the
        host, and the launcher creates it as the operator; a fresh named
        volume at a path the image does not hold is the engine's to own, and
        the engines disagree about who that is. Measured on rootless podman
        6.1.0 with ``--userns=keep-id``: a named volume nested here came out
        ``1000:1000`` and writable, so on that engine the volume would have
        worked. The bind is chosen for the engine that host could not answer
        for -- a Docker daemon seeds a fresh volume from the image, and the
        image cannot hold a project-relative path to seed it from, which
        leaves the ownership its choice rather than ours. A bind takes that
        question off the table on both. If a session ever does meet an
        unwritable one, the entrypoint's config-home check is the shape of
        the diagnostic it needs: a permission error on a path names neither
        the mount nor the mapping.

        Keyed by project root, and the caller decides which roots are in it:
        a read-only root gets none, because a session cannot sync into one
        and a directory bound inside it would be a mount nothing writes.
        """
        return [
            argument
            for root, held in environments.items()
            for argument in ("-v", f"{held}:{root / self.project_environment}:rw")
        ]

    def session_arguments(
        self,
        *,
        tag: str,
        checkout: Path,
        uid: int,
        gid: int,
        writable: Mapping[Path, str],
        read_only: Mapping[Path, str],
        state_volume: str,
        config_home_env: str,
        credential_file: str = ".credentials.json",
        credential_renewable: str = "",
        credential_fields: list[str] | None = None,
        credential: Path | None = None,
        editor_rendezvous: Path | None = None,
        engine: ContainerEngine = Docker(),
        forge: ForgeCredential | None = None,
        granted: bool = False,
        rewrites: list[RemoteRewrite] | None = None,
        identity: GitIdentity | None = None,
        browser_directory: Path | None = None,
        clipboard_directory: Path | None = None,
        wake_directory: Path | None = None,
        terminal: EnvVars | None = None,
        streams: SessionStreams = "terminal",
        proxy_address: str = "",
        boundary: EnvVars | None = None,
        inherited_environment: list[str] | None = None,
        environments: Mapping[Path, Path] | None = None,
        devices: Sequence[Device] = (),
        home_seed: Path | None = None,
        trust_document: str = "",
        privileges: SessionPrivileges = SessionPrivileges(),
        memory: int | None = None,
        overlays: Mapping[Path, str] | None = None,
    ) -> list[str]:
        """The whole argv that opens one agent session inside a container.

        Assembled here rather than at the launcher because every part of it
        has to agree with a part of the image: the config home the entrypoint
        seeds, the caches the build chowned, the environment the layers
        baked. A launcher that spelled these itself would be a second
        declaration of the same facts, free to drift from this one.

        ``state_volume`` carries the config home across launches. A container
        that started clean each time would re-seed trust every launch, and --
        measured -- an unseeded config home discards the workspace's declared
        ``permissions.allow`` with a notice rather than an error, so the
        policy would be off with nothing having failed.

        ``trust_document`` is the file in the config home the runtime keeps
        workspace trust in, which the entrypoint seeds and merges this
        checkout's trust into; empty for a runtime that keeps it elsewhere.

        ``home_seed`` is a host directory laid out as :attr:`home_seed`
        describes, offered read-only and applied by the entrypoint at every
        start, so the person's settings reach a volume that outlives them.

        ``credential`` is offered read-only at :attr:`credential_seed` and
        applied once per host-login change, including the first adoption of
        fingerprinted handoff. An unchanged seed preserves private renewals;
        a missing or unrenewable saved login may be recovered from the seed.
        ``credential_renewable`` is the provider's own test, never an inferred
        access-token expiry. ``credential_fields`` selects login records in
        a shared file, preserving unrelated container authorizations.

        The agent can read the credential either way, which is not a leak
        this could close: an agent that can open a session can reach whatever
        opens one. The scope is the boundary, not the secrecy. What the copy
        does close is the other direction -- nothing in here writes the
        host's file. A container login remains private until the selected host
        login changes, at which point the explicit host selection takes effect.

        ``terminal`` is what :meth:`TerminalHandoff.for_host` answered on this
        machine, passed rather than resolved here for the reason every host
        fact in this file is passed: the declaration is hashed, and a
        ``TERM`` read inside it would report a generated tree stale for having
        been checked from a different terminal.

        ``streams`` is how this container's standard streams are wired, and
        the same argv has to serve every way of reaching it: an exercise that
        ran through a differently-assembled argv would verify a container no
        session opens. Three states rather than two, because the third is the
        one a bool had no room for -- see :type:`SessionStreams`.

        ``environments`` is what :meth:`environment_mounts` binds, and it is
        emitted after the leased mounts because that is the order it reads
        in, not because the engine needs it: measured on podman 6.1.0, a
        nested mount emitted *before* the bind it sits inside still wins, so
        that engine sorts by depth rather than applying the list in order.
        The same sorting is what ``lease_for`` relies on for its read-only
        holes, and this leans on it in the same direction -- so an engine
        that applied the list in order would already be breaking that, and
        the order here costs nothing to keep right either way.

        ``devices`` is what the device lease granted: the machine's standing
        grants and this launch's flags, resolved against its CDI registry, so
        what reaches the engine is what the host answered for. Passed rather
        than declared here for the reason no machine fact is: this
        declaration is hashed and shared by every machine that builds the
        image, and which GPU one of them holds is that machine's alone.
        Emitted with the mounts because it is the same kind of thing -- the
        boundary, widened by a grant -- and read in the same place by
        whoever reads the argv.

        ``privileges`` is what the wall this session opens behind granted,
        and has to be what the image ``tag`` was rendered with: sudo is a
        layer of the image and a flag of the run, one grant spelled twice;
        ``memory`` the bytes its container may hold, as :meth:`run_arguments`
        spells them.

        ``overlays`` are files held read-only over a path inside the checkout
        -- a kind of session's own guidance over the committed one -- keyed
        by where each is on the host. Sorted with every other mount, so each
        lands over the checkout it sits in rather than under it.
        """
        granted_devices = [
            argument for device in devices for argument in device.arguments()
        ]
        # Parent before child whatever the mode: a lease nests a read-only
        # shared git directory with writable directories bound back over it,
        # and a writable share with read-only files over it, and an engine
        # applying the list in order would shadow the child with the parent
        # in either direction.
        declared = [
            *[(host, inside, "rw") for host, inside in writable.items()],
            *[(host, inside, "ro") for host, inside in read_only.items()],
            *[(host, inside, "ro") for host, inside in (overlays or {}).items()],
        ]
        mounts = [
            argument
            for host, inside, mode in sorted(
                declared, key=lambda mount: len(PurePosixPath(mount[1]).parts)
            )
            for argument in ("-v", f"{host}:{inside}:{mode}")
        ]
        seeded = (
            [
                "-v",
                f"{credential}:{self.credential_seed}:ro",
                "-e",
                f"LUP_CREDENTIAL_NAME={credential_file}",
                "-e",
                f"LUP_CREDENTIAL_RENEWABLE={credential_renewable}",
                "-e",
                f"LUP_CREDENTIAL_KEYS={json.dumps(credential_fields or [])}",
            ]
            if credential is not None
            else []
        )
        bridged = (
            self.ide_bridge(editor_rendezvous) if editor_rendezvous is not None else []
        )
        opening = (
            ["-v", f"{browser_directory}:{self.browser.inside}:rw"]
            if browser_directory is not None
            else []
        )
        clipping = (
            ["-v", f"{clipboard_directory}:{self.clipboard.inside}:rw"]
            if clipboard_directory is not None
            else []
        )
        # Source and target are one string rather than two, which is the only
        # mount here that has to be: a member publishes the path it bound and
        # a peer in another container opens that same text, so a target that
        # renamed it would leave every handle right where it was written and
        # wrong everywhere it was read.
        nudging = (
            ["-v", f"{wake_directory}:{wake_directory}:rw"]
            if wake_directory is not None
            else []
        )
        # The forge configuration is passed rather than baked, and passed
        # here rather than through `run_arguments`, because it is the one
        # part of the run that is neither an image fact nor a posture: it is
        # this host's own credential and a resolution of this host's remotes,
        # and neither belongs in a layer anybody could pull.
        selected = forge or NoCredential(
            variable=self.forge.token_variable, host=self.forge.host
        )
        # The boundary's own values join here rather than being baked, and
        # that is the whole difference between a placement a launch can check
        # and one it can only assert. A baked constant answers for every
        # container this image ever starts; a value minted per launch answers
        # for this one. It reaches argv with its value visible, unlike the
        # credential below, because it is not a secret -- it discriminates
        # launches rather than principals, and what it defeats is a constant
        # and an inherited variable rather than somebody reading `ps`.
        reaching = (
            self.forge.environment(rewrites or [], selected, granted, identity)
            | (terminal or {})
            | (boundary or {})
        )
        # By name, with no value beside it, which is what both engines read as
        # "take this one from my own environment". The value would otherwise
        # be in this argv -- readable out of `ps` by every process on the host
        # for as long as the session runs, and recorded verbatim by anything
        # that logs the launch.
        inherited = [
            argument
            for name in [
                *self.forge.inherited(granted),
                *(inherited_environment or []),
            ]
            for argument in ("-e", name)
        ]
        return [
            engine.binary,
            "run",
            "--rm",
            *stream_arguments(streams),
            "-v",
            f"{state_volume}:{self.config_home}",
            "-e",
            f"{config_home_env}={self.config_home}",
            *(["-e", f"LUP_TRUST_DOCUMENT={trust_document}"] if trust_document else []),
            *self.run_arguments(
                checkout, uid, gid, engine, proxy_address, privileges, memory
            ),
            *[
                argument
                for name, value in reaching.items()
                for argument in ("-e", f"{name}={value}")
            ],
            *inherited,
            *selected.mount_arguments(),
            *mounts,
            *self.environment_mounts(environments or {}),
            *granted_devices,
            *seeded,
            *(
                ["-v", f"{home_seed}:{self.home_seed}:ro"]
                if home_seed is not None
                else []
            ),
            *bridged,
            *opening,
            *clipping,
            *nudging,
            tag,
        ]

    def home_mounts(
        self, opening: list[str], state_volume: str, held: Mapping[Path, str]
    ) -> list[str]:
        """A session argv with read-only mounts nested in its config home.

        What only a preparation run through this argv can name -- the
        revision a runtime installed into its home -- held read-only for the
        session that follows. Placed straight after the volume they nest in,
        parent before child as :meth:`session_arguments` places every other
        mount, rather than relying on each engine to sort them.
        """
        volume = opening.index(f"{state_volume}:{self.config_home}") + 1
        nested = [
            word
            for host, inside in held.items()
            for word in ("-v", f"{host}:{inside}:ro")
        ]
        return [*opening[:volume], *nested, *opening[volume:]]

    def seed_configuration(self) -> JsonObject:
        """The container-side ``.claude.json`` a fresh config home starts from.

        Without this the workspace is untrusted and its declared permissions
        are discarded with a notice. Seeding is the whole fix, and it belongs
        beside the image rather than in a launch script because the paths it
        names are the paths the mount table names.
        """
        return {
            "hasCompletedOnboarding": True,
            "projects": {
                str(project): {"hasTrustDialogAccepted": True}
                for project in self.trusted_projects
            },
        }
