"""This repository's launch workflow, and the declarations `harness claude|codex` make.

`harness claude|codex` is a caller of the library's launch. Each flag becomes
a field of a :class:`~lup.providers.claude.Claude` or
:class:`~lup.providers.codex.Codex` declaration, and the declaration's own
``launch()`` does the rest — preparing its home, checking the host, compiling
and running the CLI in the foreground, and cleaning up after it — with this
repository's workflow around it as lifecycle steps: a checkpoint, the worktree
pointers verified, the base brought level, and every tree this repository
generates regenerated. What it reads only here, the registrations in
`sync.json.local`, becomes the declaration's mounts and devices.
"""

import os
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager, nullcontext
from pathlib import Path
from typing import Protocol, runtime_checkable

import typer
from pydantic import BaseModel, Field, TypeAdapter, ValidationError

from lup.devtools.dashboard.companion import Dashboard
from lup.devtools.dev.branches import settle_base_freshness
from lup.devtools.dev.worktree import RelocationHint, refuse_redirected_pointers
from lup.devtools.harness.composition import NativeTargets
from lup.devtools.harness.drift import (
    RepositoryWriter,
    generate_targets,
    generate_with_report,
)
from lup.devtools.sync import accessible_roots, granted_devices
from lup.harness.devices import Device
from lup.harness.generate import NativeHarnessComposition
from lup.harness.image import Image, MemoryLimit
from lup.harness.models import Harness, NativeName, Plugin, Resumption
from lup.harness.notice import Notice
from lup.harness.process import LocalProcessLauncher
from lup.launch.companions import CompanionLaunch, Contribution, HostCompanion
from lup.launch.declaration import (
    InnerSandbox,
    LaunchSandbox,
    LaunchStep,
    Latest,
    Member,
    Mount,
    NoSandbox,
    OuterContainer,
    Pick,
    Recording,
    Reopen,
    Resume,
    SessionSandbox,
    settled_sandbox,
)
from lup.launch.refusal import LaunchRefused
from lup.launch.session import StandingGrants, personal_config
from lup.observability.audit import TraceJournal
from lup.observability.sessions import SessionRecorder
from lup.providers.claude import Claude, ClaudeTools
from lup.providers.claude.harness import ClaudeSpellings
from lup.providers.claude.launch import companion_plugin_directories
from lup.providers.claude.model_choice import ClaudeModelChoice, claude_effort_named
from lup.providers.codex import Codex, CodexTools
from lup.providers.codex.harness import CodexSpellings
from lup.providers.codex.model_choice import CodexModelChoice, codex_effort_named
from lup.providers.codex.session import prepare_codex_plugin
from lup.providers.profiles import DefaultHomeProfile, ProfileDirectory
from lup.providers.user_config import UserConfigFile
from lup.sandbox.models import NetworkMode
from lup.sessions.events import SessionId
from lup.types import CustomModel, EnvVars
from lup.workspace.paths import project_root


@contextmanager
def usage_refusals() -> Iterator[None]:
    """Say a launch the library refused as this command line's own usage error.

    The library refuses in its own exception, which names what to fix and
    nothing about a command line; here the refusal reaches the operator the
    way every other bad invocation does, as a usage error and its exit code.
    Held once at each entry point -- the launchers and the commands that ask
    the library for a boundary -- rather than at every call into it.
    """
    try:
        yield
    except LaunchRefused as refusal:
        raise typer.BadParameter(str(refusal)) from refusal


def standing_grants() -> StandingGrants:
    """This repository's registrations, as the requirements roster asks for them.

    Read by :mod:`lup.devtools.sync` from the registry and `sync.json.local`,
    which are this repository's bookkeeping rather than the launch's. Named
    at the call rather than captured at import, so the registry asked is the
    one in force when the roster asks.
    """
    return StandingGrants(roots=accessible_roots, devices=granted_devices)


def declared_devices(names: list[str]) -> list[Device]:
    """The devices one command line asked this session to be granted.

    The same shape an image declares its standing ones in, so a flag costs no
    second path downstream. A name the specification's grammar refuses is
    refused here in the launcher's own words, naming the flag's value, rather
    than by the engine refusing the whole container over it.
    """

    def declared(name: str) -> Device:
        try:
            return Device(name=name)
        except ValidationError as error:
            raise typer.BadParameter(
                f"--device {name!r}: a device is named the way CDI names it, "
                "vendor/class=device, such as nvidia.com/gpu=all"
            ) from error

    return [declared(name) for name in names]


def memory_limit(spelled: str | None) -> MemoryLimit | None:
    """A memory limit named on a command line, refused in the flag's words where it is none."""
    if spelled is None:
        return None
    try:
        return MemoryLimit.model_validate(spelled)
    except ValidationError as error:
        raise typer.BadParameter(
            f"--memory {spelled!r}: a limit is an amount such as 12GiB or "
            "512MiB, or a share such as 75%"
        ) from error


@runtime_checkable
class LaunchCheckpoint(Protocol):
    """Application-owned data persistence at native launch boundaries."""

    def __call__(self, *, provider: str) -> None: ...


def relocation_hint(worktree_path: Path) -> RelocationHint:
    """Name the follow-through in the vocabulary of the running harness.

    Each launcher exports its own configuration home, so a session that
    reached here through one of them is told the move it actually supports.
    Anything else gets the portable shell form alone rather than the name of
    a tool that runtime may not have.

    The wording is asked of the same spelling the guidance is rendered from
    rather than written again here. Restating it is how the two come to
    disagree: the guidance naming the move a runtime supports, this naming a
    tool, and a workflow change having to find both to land. One of them
    being an adapter method makes that impossible.
    """
    environ = os.environ  # lup: ignore[os-environ]
    move = f"cd /; cd {worktree_path}"
    here = "the path above"
    if "CLAUDE_CONFIG_DIR" in environ:
        return RelocationHint(
            agent=ClaudeSpellings().relocate_session(here),
            shell=f"{move}; claude",
        )
    if "CODEX_HOME" in environ:
        return RelocationHint(
            agent=CodexSpellings().relocate_session(here),
            shell=f"{move}; codex",
        )
    return RelocationHint(agent="", shell=move)


@runtime_checkable
class LaunchSession(Protocol):
    """How a launch mode opens whatever its session needs, around one run.

    Runtime-checkable because :class:`LaunchMode` is a pydantic model and an
    arbitrary-typed field is validated by ``isinstance``; the protocol carries
    only ``__call__``, which is exactly what that check can answer.

    Returns a context manager yielding the environment the native CLI is
    launched under, so a mode that has state to hold — a directory to make, a
    pointer to publish, a record to close — holds it for exactly the span the
    CLI is running and gets its exit path for free.

    The journal rather than a directory, because a mode's session usually has
    to be *findable* by a subprocess the CLI spawns, and what identifies one
    run is the trace context the journal already carries.
    """

    def __call__(
        self, *, provider: str, journal: TraceJournal, transcribe: bool
    ) -> AbstractContextManager[EnvVars]: ...


class LaunchMode(BaseModel, frozen=True, arbitrary_types_allowed=True):
    """One named way of opening a session that the default launch is not.

    A mode is the application's, never the library's: it names a flag, the
    tree generation compiles while it is in force, the model that kind of
    session runs on, where its record is kept, and what has to be open around
    it. Each becomes a field of the declaration the launch opens, and nothing
    here knows what any particular mode is *for*, which is what keeps a
    downstream project's vocabulary out of the framework.
    """

    name: NativeName
    """Spells the flag: a mode named ``syra`` is selected by ``--syra``."""

    help: str = Field(min_length=1)

    targets: NativeTargets
    """What generation compiles while this mode is in force.

    A mode changes the tree rather than only the command line, because the
    thing a mode usually adds — a skill, a hook, a tool server — has to reach
    the session through what the runtime reads at startup."""

    model: Callable[[str], str | None] | None = None
    """What this kind of session runs on, given the runtime that will run it.

    Per runtime for the reason :attr:`arguments` is. A model name is one
    provider's vocabulary, so a mode declaring "the best available" means a
    different word on each, and one name shared between them reaches the
    other as a model that does not exist — a failure that arrives from the
    provider's API mid-session, naming neither the mode nor the launch that
    chose it. Absent an explicit ``--model``, which still wins."""

    record_root: Callable[[], Path] | None = None
    """Where this mode's transcripts are rooted, resolved at launch.

    A callable because the answer is relative to a project root that moves
    between worktrees, and a path captured at import time names the checkout
    the process started in rather than the one it is running against."""

    arguments: Callable[[str], list[str]] | None = None
    """Words this mode adds to the native command line, given the runtime.

    Per runtime because the same intent is spelled differently or not at all:
    a mode that wants its brief in the system prompt has a flag for that on
    one CLI and a generated document on the other, and a seam that could not
    tell them apart would put an unknown option on the second."""

    session: LaunchSession | None = None
    """What must be open while a session of this kind runs."""

    max_recursive_agent: int = Field(default=-1, ge=-1)
    """Mode default when the launcher receives no explicit allowance."""

    recursive_targets: Callable[[int], NativeTargets] | None = None
    """Targets selected from the effective recursive-agent allowance."""

    transcribe_session: Callable[[str], bool] | None = None
    """Whether one runtime's native transcript is mirrored for this mode."""

    def command_words(self, provider: str) -> list[str]:
        """Words this mode contributes to one runtime's command line, if any."""
        return self.arguments(provider) if self.arguments is not None else []

    def native_model(self, provider: str) -> str | None:
        """What this mode runs one runtime on, when it names anything at all."""
        return self.model(provider) if self.model is not None else None

    def transcript_root(self) -> Path | None:
        """Where this launch keeps its record, resolved now, not at import."""
        return self.record_root() if self.record_root is not None else None

    def transcribes(self, provider: str) -> bool:
        """Whether this mode mirrors one provider's native session record."""
        return (
            self.transcribe_session(provider)
            if self.transcribe_session is not None
            else True
        )

    def recursive_agent_limit(self, explicit: int | None) -> int:
        """Resolve an explicit allowance over this mode's default."""
        return self.max_recursive_agent if explicit is None else explicit

    def targets_at(self, allowance: int) -> NativeTargets:
        """The native trees compiled for this allowance."""
        return (
            self.recursive_targets(allowance)
            if self.recursive_targets is not None
            else self.targets
        )

    def opened(
        self, provider: str, journal: TraceJournal, transcribe: bool
    ) -> AbstractContextManager[EnvVars]:
        """Whatever this mode needs open around the run, or nothing to open.

        An empty environment from :func:`contextlib.nullcontext` rather than a
        branch at the call site, so a caller holds one shape and a mode
        declaring no session costs it no conditional.
        """
        if self.session is None:
            return nullcontext({})
        return self.session(
            provider=provider,
            journal=journal,
            transcribe=transcribe,
        )


class LaunchSelection(BaseModel, frozen=True, arbitrary_types_allowed=True):
    """Which mode a caller's words selected, and what is left for the CLI."""

    mode: LaunchMode | None
    arguments: list[str]


def extract_launch_mode(
    modes: list[LaunchMode], arguments: list[str]
) -> LaunchSelection:
    """Take a declared mode flag out of the words meant for the native CLI.

    Read out of the passthrough vector rather than declared as a command
    option, because the flag's name belongs to a declaration the library
    reads at runtime and a Typer option's name is fixed when its function is
    defined. The launch commands already own unknown options — that is how
    they forward a caller's own arguments — so recognizing a few of them
    first is the same surface, not a new one.
    """
    selected = {f"--{mode.name}": mode for mode in modes}
    chosen = [selected[word] for word in arguments if word in selected]
    if len(chosen) > 1:
        named = ", ".join(f"--{mode.name}" for mode in chosen)
        raise typer.BadParameter(f"launch modes are exclusive; got {named}")
    return LaunchSelection(
        mode=chosen[0] if chosen else None,
        arguments=[word for word in arguments if word not in selected],
    )


class ModeSession(HostCompanion, frozen=True, arbitrary_types_allowed=True):
    """What a launch mode needs open around its session, held the way a companion is.

    Held around the run with the run's journal, which is what makes the
    mode's session findable by what the CLI spawns. A command printed rather
    than run has no run to be found by, so it is handed nothing there.
    """

    mode: LaunchMode
    runtime: str
    transcribe: bool

    @contextmanager
    def held(self, launch: CompanionLaunch) -> Iterator[Contribution]:
        if launch.journal is None:
            yield Contribution()
            return
        with self.mode.opened(
            self.runtime, launch.journal, self.transcribe
        ) as environment:
            yield Contribution(environment=environment)


def announce_relaxed_rules(relaxed: bool, plugin: Plugin) -> None:
    """Say what a relaxed launch retired, and what it did not.

    The launch is the only moment this is legible. The tree it compiles
    carries no rules, so nothing downstream can report their absence — a
    session opened under it simply meets no rule and has no way to tell that
    from a repository with none. So the count is read off the plugin actually
    being opened rather than off the declaration it came from.

    Two consequences ride along because both bite later and neither announces
    itself. The repository is unchanged, so the sweep still holds it to every
    rule and a session that edited freely under this will fail `dev check`.
    And the committed tree has just been rewritten, so a commit made from here
    would carry a plugin nobody declared.
    """
    if not relaxed:
        return
    retired = len(plugin.hooks.rules.retired if plugin.hooks is not None else [])
    Notice(
        text=f"anti-patterns retired for this session: {retired} rules",
        urgency="warning",
    ).say()
    typer.echo(
        "`dev check --antipatterns` still holds this repository to them; run "
        "`lup-devtools harness generate all` before committing, or the "
        "compiled tree carries a policy nothing declares. To retire them for "
        "good instead, `dev seams --retire-all` writes it where a review sees "
        "it."
    )


def install_codex_plugin_home(codex_home: Path, force: bool, trusted: bool) -> None:
    """Install the declared plugin into a home the operator names, as a host launch would.

    The whole of `harness codex-plugin install`, held here because installing
    it is the Codex adapter's and the command tree names no adapter.
    """
    prepare_codex_plugin([], codex_home, project_root(), {}, force, trusted)


class LaunchRequest(BaseModel, frozen=True, arbitrary_types_allowed=True):
    """What one `harness claude|codex` command line asked for, before it is a declaration."""

    words: list[str] = []
    """What reaches the CLI after everything the declaration compiles to."""

    model: str | None = None
    effort: str | None = None
    profile: str | None = None
    resume: Resumption = Resumption()
    sandbox: LaunchSandbox | None = None
    """The sandbox named on the command line, or ``None`` for the default."""

    mounts: list[Path] = []
    read_only: list[Path] = []
    devices: list[str] = []
    network: NetworkMode | None = None
    memory: MemoryLimit | None = None
    sudo: bool | None = None
    """``--sudo`` or ``--no-sudo``, or ``None`` where the command line said neither."""

    container: OuterContainer = OuterContainer()
    """The project's own container, under the person's config, the mode and
    what this command line states."""

    max_recursive_agent: int | None = None
    transcribe_session: bool = False
    relaxed: bool = False
    mode: LaunchMode | None = None
    recorder: SessionRecorder | None = None

    def allowance(self) -> int:
        """The recursive-agent allowance: the flag's, else the mode's, else no limit."""
        if self.mode is not None:
            return self.mode.recursive_agent_limit(self.max_recursive_agent)
        return -1 if self.max_recursive_agent is None else self.max_recursive_agent

    def reopening(self) -> Resume | None:
        """The session to reopen, refusing two named at once before anything runs."""
        contradiction = self.resume.contradicted()
        if contradiction is not None:
            raise typer.BadParameter(contradiction)
        if self.resume.session is not None:
            return Reopen(session=SessionId(value=self.resume.session))
        if self.resume.pick:
            return Pick()
        return Latest() if self.resume.latest else None

    def named_model(self, runtime: str) -> str | None:
        """The model named: the flag's, else the mode's for this runtime."""
        if self.model is not None:
            return self.model
        return self.mode.native_model(runtime) if self.mode is not None else None

    def launch_words(self, runtime: str) -> list[str]:
        """The mode's words for this runtime, then the caller's own."""
        mode = self.mode.command_words(runtime) if self.mode is not None else []
        return [*mode, *self.words]

    def transcribes(self, runtime: str) -> bool:
        """Whether the native transcript is mirrored: asked for, or no mode declines it."""
        return (
            self.transcribe_session
            or self.mode is None
            or self.mode.transcribes(runtime)
        )

    def recording(self, runtime: str) -> Recording:
        """What is kept of the session: its transcript, its ledger entry, the mode's root."""
        return Recording(
            transcript=self.transcribes(runtime),
            ledger=self.recorder,
            root=self.mode.transcript_root() if self.mode is not None else None,
            mode=self.mode.name if self.mode is not None else None,
        )

    def companions(self, runtime: str) -> list[HostCompanion]:
        """What the mode needs open around its session, held as a companion."""
        if self.mode is None or self.mode.session is None:
            return []
        return [
            ModeSession(
                name=self.mode.name,
                mode=self.mode,
                runtime=runtime,
                transcribe=self.transcribes(runtime),
            )
        ]

    def posture(self, settle: bool) -> LaunchSandbox:
        """The sandbox a session opens under: the one named, else the host's default.

        ``settle`` asks the host for its default, which a launch does. A
        generation that opens nothing does not: it readies the home a host
        session opens against, and asks nobody whether an engine answers.
        """
        if self.sandbox is not None:
            return self.sandbox
        if not settle:
            return LaunchSandbox.INNER
        return settled_sandbox(None, "pass `--sandbox inner`")

    def stated(self) -> OuterContainer:
        """What this command line states of the container: its folders, devices and settings named.

        Only what was typed, so a setting left off the command line stays the
        mode's, the person's or the project's.
        """
        named = {
            name: value
            for name, value in (
                ("network", self.network),
                ("memory", self.memory),
                ("sudo", self.sudo),
            )
            if value is not None
        }
        return OuterContainer.model_validate(
            {
                "mounts": [
                    *[Mount(path=path, writable=True) for path in self.mounts],
                    *[Mount(path=path) for path in self.read_only],
                ],
                "devices": declared_devices(self.devices),
                **named,
            }
        )

    def settled_container(self, image: Image) -> OuterContainer:
        """The container this launch opens: the command line over the person, over the project.

        The project's layer is its declared container over what this machine
        registers for the repository — its standing folders and devices — on
        the harness's ``image``; the person's is their lup config's
        ``[container]``.
        """
        standing = OuterContainer(
            image=image,
            mounts=[
                Mount(path=root.path, writable=root.writable)
                for root in accessible_roots()
            ],
            devices=granted_devices(),
        )
        person = personal_config(UserConfigFile()).container
        return self.stated().over(person.over(self.container.over(standing)))

    def wall(
        self, posture: LaunchSandbox, image: Image, escapable: bool
    ) -> SessionSandbox:
        """The sandbox declared, with every folder the container's layers name.

        The container is :meth:`settled_container`. The inner sandbox lets a
        command ask to run outside it, for the policy to judge, where the
        runtime has such a way out: Claude Code has, Codex's envelope has
        none. What only a container grants — devices, sudo, a network, a
        memory limit — the host already holds or has no wall to hold, so a
        flag naming one there is said rather than granted.
        """
        settled = self.settled_container(image)
        match posture:
            case LaunchSandbox.OUTER:
                return settled
            case LaunchSandbox.INNER:
                self.say_hosted_settings()
                return InnerSandbox(mounts=settled.mounts, escapable=escapable)
            case LaunchSandbox.NONE:
                self.say_hosted_settings()
                return NoSandbox(mounts=settled.mounts)

    def say_hosted_settings(self) -> None:
        """Say what the command line asked of a container, where the session opens on the host."""
        asked = [
            *(
                [
                    "--device "
                    + ", ".join(
                        device.name for device in declared_devices(self.devices)
                    )
                ]
                if self.devices
                else []
            ),
            *([f"--network {self.network}"] if self.network is not None else []),
            *(["--memory"] if self.memory is not None else []),
            *(
                ["--sudo" if self.sudo else "--no-sudo"]
                if self.sudo is not None
                else []
            ),
        ]
        if not asked:
            return
        Notice(
            text=(
                f"{'; '.join(asked)} asked for; the session runs on the host, "
                "which holds its own devices, network and memory, and grants "
                "each of these only to the container."
            ),
            urgency="detail",
        ).say()


def model_choice[T](
    spelled: str | None, choices: TypeAdapter[T]
) -> T | CustomModel | None:
    """A model named on a command line, as a runtime's catalog reads it.

    A name the catalog does not list is the CLI's to judge rather than this
    launcher's to refuse, so it goes through as a custom id.
    """
    if spelled is None:
        return None
    try:
        return choices.validate_python(spelled)
    except ValidationError:
        return CustomModel(id=spelled)


def effort_named[T](spelled: str | None, named: Callable[[str], T]) -> T | None:
    """An effort named on a command line, refused in the flag's words where it is none."""
    if spelled is None:
        return None
    try:
        return named(spelled)
    except ValueError as refusal:
        raise typer.BadParameter(str(refusal)) from refusal


def declared[T](build: Callable[[], T]) -> T:
    """Build a declaration, refusing what it refuses as a usage error.

    A declaration validates as it is built — an effort the model's catalog
    row lacks is refused there — and that is the command line's mistake,
    said before anything is generated or checkpointed.
    """
    try:
        return build()
    except ValidationError as refusal:
        raise typer.BadParameter(
            "; ".join(str(error["msg"]) for error in refusal.errors())
        ) from refusal


def machine_overlay(composition: NativeHarnessComposition) -> list[Path]:
    """This machine's overlay, where the runtime is told of it and it holds any skill.

    Named whether or not it is on disk yet: the regeneration a launch runs
    before its session renders it.
    """
    overlay = composition.overlay
    plugin = composition.recipe.source.plugins[0]
    if overlay is None or not overlay.loaded or not plugin.machine_skills():
        return []
    return [composition.recipe.root / overlay.directory]


def held_services(harness: Harness) -> list[HostCompanion]:
    """What this repository's harness holds on the host around every session it opens.

    The dashboard, where the module is taken: every launch holds it, whichever
    runtime, sandbox or caller — a session opening a child session included —
    so there is one page for all of them.
    """
    return [Dashboard()] if harness.dashboard else []


def claude_declaration(
    composition: NativeHarnessComposition,
    request: LaunchRequest,
    profiles: ProfileDirectory,
    settle: bool = True,
) -> Claude:
    """The Claude Code agent one command line launches, from this repository's composition.

    Its plugin is the tree the composition generates, with every other plugin
    the checkout carries beside it; its policy, requirements, image and wake
    socket are the composition's harness's, and its servers the composition's; its
    account is the one ``profiles`` resolves the named profile, or the
    selected one, to. What the command line got wrong is refused before the
    host is asked for its default sandbox, which ``settle`` asks for.
    """
    source = composition.recipe.source
    root = composition.recipe.root
    plugin = source.plugins[0]
    try:
        home = profiles.launch_home(request.profile)
        selected = request.profile or profiles.active_name()
    except (KeyError, ValueError, DefaultHomeProfile) as error:
        raise typer.BadParameter(str(error)) from error
    # lup: solved: a contained session runs in its repository's config volume,
    # so no theme reaches it and none it sets returns to the account; which
    # home a container's theme belongs to is the volume's question.

    def declaration(sandbox: SessionSandbox) -> Claude:
        return Claude(
            model=model_choice(
                request.named_model("claude"), TypeAdapter(ClaudeModelChoice)
            ),
            effort=effort_named(request.effort, claude_effort_named),
            cwd=root,
            tools=ClaudeTools(
                builtin="stock", mcp=composition.servers, serve=composition.serve
            ),
            plugin=root / ".claude" / "plugins" / plugin.name,
            plugin_dirs=[
                *machine_overlay(composition),
                *companion_plugin_directories(root, plugin.name),
            ],
            policy=plugin.hooks,
            requirements=source.requirements,
            sandbox=sandbox,
            identity=Member(wake_sockets=source.image.wake_sockets),
            record=request.recording("claude"),
            resume=request.reopening(),
            max_recursive_agent=request.allowance(),
            profile=selected,
            home=home,
            companions=[*request.companions("claude"), *held_services(source)],
        )

    declared(lambda: declaration(NoSandbox()))
    wall = request.wall(request.posture(settle), source.image, escapable=True)
    return declared(lambda: declaration(wall))


def codex_declaration(
    composition: NativeHarnessComposition,
    request: LaunchRequest,
    home: Path | None,
    settle: bool = True,
) -> Codex:
    """The Codex agent one command line launches, from this repository's composition.

    Its plugin is the one this checkout's marketplace offers, installed into
    the launch's home: the one ``home`` names, else one derived for this
    worktree from the named profile's account, or the selected one's. What
    the command line got wrong is refused before the host is asked for its
    default sandbox, which ``settle`` asks for.
    """
    source = composition.recipe.source
    root = composition.recipe.root
    plugin = source.plugins[0]
    # lup: solved: a contained session runs in its repository's config
    # volume, so a setting it changes there — its /theme included — never
    # returns to the account; which home those belong to is the volume's
    # question.

    def declaration(sandbox: SessionSandbox) -> Codex:
        return Codex(
            model=model_choice(
                request.named_model("codex"), TypeAdapter(CodexModelChoice)
            ),
            effort=effort_named(request.effort, codex_effort_named),
            cwd=root,
            tools=CodexTools(
                builtin="stock", mcp=composition.servers, serve=composition.serve
            ),
            plugin=root,
            policy=plugin.hooks,
            requirements=source.requirements,
            sandbox=sandbox,
            identity=Member(wake_sockets=source.image.wake_sockets),
            record=request.recording("codex"),
            resume=request.reopening(),
            max_recursive_agent=request.allowance(),
            profile=request.profile,
            home=home,
            companions=[*request.companions("codex"), *held_services(source)],
        )

    declared(lambda: declaration(NoSandbox()))
    wall = request.wall(request.posture(settle), source.image, escapable=False)
    return declared(lambda: declaration(wall))


class Checkpointed(BaseModel, frozen=True, arbitrary_types_allowed=True):
    """The application's data saved before a session and again after it."""

    checkpoint: LaunchCheckpoint
    runtime: str

    def before(self) -> None:
        self.checkpoint(provider=self.runtime)

    def after(self, succeeded: bool) -> None:
        del succeeded
        self.checkpoint(provider=self.runtime)


class PointersVerified(BaseModel, frozen=True):
    """Refuse a worktree whose pointer a contained session moved, before host git reads it.

    Ahead of every step that runs host git, so a session a previous contained
    one left with a redirected pointer is refused rather than opened onto git
    reading the config that pointer now leads to.
    """

    def before(self) -> None:
        refuse_redirected_pointers()

    def after(self, succeeded: bool) -> None:
        del succeeded


class BaseSettled(BaseModel, frozen=True):
    """Bring a clean checkout level with its remote, and name a base that has moved.

    A tree whose base has moved is self-consistent and says nothing about it,
    so a session opened on one plans and edits against code that is no longer
    there — which cost a planning pass over thirteen concerns on a tree ten
    commits behind its remote, where two merged pull requests had already done
    part of the work being planned. Being behind is not itself grounds for
    refusing a session, so this syncs and reports. Ahead of the regeneration,
    so the trees the session opens against are the synced source's.
    """

    root: Path

    def before(self) -> None:
        settle_base_freshness(LocalProcessLauncher(), self.root)

    def after(self, succeeded: bool) -> None:
        del succeeded


class TreesGenerated(BaseModel, frozen=True, arbitrary_types_allowed=True):
    """Every tree this repository generates, regenerated before a session opens against one.

    The trees the launch does not open too — ``companions`` — and every
    generated file beside them, which is what makes launching a runtime mean
    what `harness generate all` means: a launcher that left the other's tree
    behind whenever a shared source moved would fail the next `dev check` on
    drift nobody had introduced. A tree already current says nothing.
    """

    composition: NativeHarnessComposition
    companions: list[NativeHarnessComposition] = []
    writers: list[RepositoryWriter] = []

    def generated(self, in_passing: bool) -> None:
        """Generate every tree, reporting each unless it is ``in_passing`` and current."""
        generate_with_report(self.composition, in_passing=in_passing)
        generate_targets(self.companions, self.writers, in_passing=in_passing)

    def before(self) -> None:
        typer.echo("regenerating what this session opens against")
        self.generated(in_passing=True)

    def after(self, succeeded: bool) -> None:
        del succeeded


def workflow_steps(
    runtime: str,
    generation: TreesGenerated,
    checkpoint: LaunchCheckpoint | None,
) -> list[LaunchStep]:
    """This repository's workflow around one session, outermost first."""
    return [
        *([Checkpointed(checkpoint=checkpoint, runtime=runtime)] if checkpoint else []),
        PointersVerified(),
        BaseSettled(root=generation.composition.recipe.root),
        generation,
    ]


def exited(status: int) -> None:
    """End this command with the session's exit status, where it failed."""
    if status:
        raise typer.Exit(status)


@usage_refusals()
def launch_claude(
    composition: NativeHarnessComposition,
    request: LaunchRequest,
    profiles: ProfileDirectory,
    generate_only: bool,
    checkpoint: LaunchCheckpoint | None = None,
    companions: list[NativeHarnessComposition] = [],
    repository_writers: list[RepositoryWriter] = [],
) -> None:
    """Launch Claude Code on this repository's plugin, or only generate and prepare it."""
    announce_relaxed_rules(request.relaxed, composition.recipe.source.plugins[0])
    agent = claude_declaration(composition, request, profiles, settle=not generate_only)
    generation = TreesGenerated(
        composition=composition, companions=companions, writers=repository_writers
    )
    if generate_only:
        generation.generated(in_passing=False)
        agent.prepare()
        return
    exited(
        agent.launch(
            *request.launch_words("claude"),
            steps=workflow_steps("claude", generation, checkpoint),
        )
    )


@usage_refusals()
def launch_codex(
    composition: NativeHarnessComposition,
    request: LaunchRequest,
    codex_home: Path | None,
    generate_only: bool,
    force_install: bool,
    checkpoint: LaunchCheckpoint | None = None,
    companions: list[NativeHarnessComposition] = [],
    repository_writers: list[RepositoryWriter] = [],
) -> None:
    """Launch Codex on this repository's plugin, or only generate and install it."""
    announce_relaxed_rules(request.relaxed, composition.recipe.source.plugins[0])
    agent = codex_declaration(
        composition, request, codex_home, settle=not generate_only
    )
    generation = TreesGenerated(
        composition=composition, companions=companions, writers=repository_writers
    )
    if generate_only:
        generation.generated(in_passing=False)
        agent.prepare(force=force_install)
        return
    exited(
        agent.launch(
            *request.launch_words("codex"),
            steps=workflow_steps("codex", generation, checkpoint),
            force=force_install,
        )
    )
