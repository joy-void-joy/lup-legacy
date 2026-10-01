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
from contextlib import contextmanager
from pathlib import Path
from typing import Protocol, Self, TypedDict, runtime_checkable

import typer
from pydantic import BaseModel, Field, TypeAdapter, ValidationError, model_validator

from lup.devtools.dashboard.companion import Dashboard
from lup.devtools.review.answers import ReviewAnswers
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
from lup.execution.process import LocalProcessLauncher
from lup.launch.companions import HostCompanion
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
    laid_over,
    settled_sandbox,
)
from lup.launch.refusal import LaunchRefused
from lup.launch.session import StandingGrants, personal_config
from lup.observability.sessions import SessionRecorder
from lup.providers.runtime_homes import selected_runtime
from lup.providers.claude import Claude, ClaudeTools
from lup.providers.claude.harness import ClaudeSpellings
from lup.providers.claude.launch import companion_plugin_directories
from lup.providers.claude.model_choice import ClaudeModelChoice, claude_effort_named
from lup.providers.codex import Codex, CodexTools
from lup.providers.codex.harness import CodexSpellings
from lup.providers.codex.home import CodexWorktreeHomeStore, move_codex_homes
from lup.providers.codex.model_choice import CodexModelChoice, codex_effort_named
from lup.providers.codex.session import prepare_codex_plugin
from lup.providers.profiles import DefaultHomeProfile, ProfileDirectory
from lup.providers.user_config import UserConfigFile
from lup.sandbox.models import NetworkMode
from lup.sessions.events import SessionId
from lup.types import CustomModel
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
    match selected_runtime(dict(environ)):
        case "claude":
            return RelocationHint(
                agent=ClaudeSpellings().relocate_session(here),
                shell=f"{move}; claude",
            )
        case "codex":
            return RelocationHint(
                agent=CodexSpellings().relocate_session(here),
                shell=f"{move}; codex",
            )
        case None:
            return RelocationHint(agent="", shell=move)


class LaunchMode(BaseModel, frozen=True, arbitrary_types_allowed=True):
    """One named kind of session: a preset over the project's declaration, selected by ``--mode``.

    A mode is the application's, never the library's, and nothing here knows
    what any mode is *for*. What it changes is a declaration's own fields —
    the model, how much the runtime asks, the system prompt, the record, the
    companions held beside it — stated as a ``Claude(...)`` and its
    ``Codex(...)`` variant naming only those, each laid over the declaration
    the project's composition builds by
    :func:`~lup.launch.declaration.laid_over`, and the command line laid over
    both. What it grants its container is an ``OuterContainer``, between the
    command line and the person's config. The wall itself is the command
    line's, so a preset naming a sandbox is refused.
    """

    name: NativeName
    """Selects it: ``harness claude --mode <name>``."""

    help: str = Field(min_length=1)
    """What this kind of session is for, where the launchers list the modes."""

    claude: Claude = Claude()
    """What this mode changes of the project's Claude Code declaration: every
    field it states, a nested declaration field by field."""

    codex: Codex = Codex()
    """Its Codex variant, laid over the project's Codex declaration the same way."""

    container: OuterContainer = OuterContainer()
    """What this mode grants its container — network, memory, sudo, devices,
    folders, held trees, guidance of its own — over the person's config and
    the project's, under the command line. A mode meant to run unattended
    holds the generated trees here, ``hold_generated=True``: the launch has no
    other way to know the session will go unwatched."""

    targets: NativeTargets | None = None
    """What generation compiles while this mode is in force; unset, the project's own.

    A mode changes the tree rather than only the declaration where what it
    adds — a skill, a hook, a tool server — reaches the session through what
    the runtime reads at startup."""

    recursive_targets: Callable[[int], NativeTargets] | None = None
    """The tree compiled for the effective recursive-agent allowance, where it differs by one."""

    @model_validator(mode="after")
    def the_wall_is_the_command_line_s(self) -> Self:
        """Refuse a preset naming a sandbox: a mode's container is its ``container``."""
        named = [
            runtime
            for runtime, preset in (("claude", self.claude), ("codex", self.codex))
            if "sandbox" in preset.model_fields_set
        ]
        if named:
            raise ValueError(
                f"mode {self.name!r} names a sandbox in its {' and '.join(named)} "
                "preset; state what its container grants in container=, and "
                "leave the wall to --sandbox"
            )
        return self

    def targets_at(self, allowance: int) -> NativeTargets | None:
        """The trees compiled for this allowance, or ``None`` for the project's."""
        if self.recursive_targets is not None:
            return self.recursive_targets(allowance)
        return self.targets

    def allowance(self, runtime: str) -> int | None:
        """The recursive-agent allowance this mode states for one runtime, if it states one."""
        match runtime:
            case "claude" if "max_recursive_agent" in self.claude.model_fields_set:
                return self.claude.max_recursive_agent
            case "codex" if "max_recursive_agent" in self.codex.model_fields_set:
                return self.codex.max_recursive_agent
            case _:
                return None

    def contained_only(self, runtime: str) -> list[str]:
        """What this mode asks of one runtime that only a container stands in for.

        A runtime told not to ask — Claude Code's ``auto`` or
        ``bypassPermissions``, Codex never asking or its reviewer answering in
        the person's place — or with its own sandbox stood down, and guidance
        of the mode's own, which only a container can put over the committed
        file. On the host nothing would be left where these took something
        away, so a mode naming any of them is refused there.
        """
        match runtime:
            case "claude":
                asked = (
                    [f"Claude Code's {self.claude.permission_mode} permission mode"]
                    if "permission_mode" in self.claude.model_fields_set
                    and self.claude.permission_mode in ("auto", "bypassPermissions")
                    else []
                )
            case "codex":
                asked = [
                    *(
                        ["Codex never asking"]
                        if "approval_policy" in self.codex.model_fields_set
                        and self.codex.approval_policy == "never"
                        else []
                    ),
                    *(
                        ["Codex's auto_review reviewer"]
                        if self.codex.approvals_reviewer == "auto_review"
                        else []
                    ),
                    *(
                        ["Codex's own sandbox stood down"]
                        if self.codex.sandbox_mode == "danger-full-access"
                        else []
                    ),
                ]
            case _:
                asked = []
        guidance = ["its own guidance"] if self.container.guidance is not None else []
        return [*asked, *guidance]


def selected_mode(modes: list[LaunchMode], name: str | None) -> LaunchMode | None:
    """The mode ``--mode`` names, or none; an undeclared one is refused naming the declared ones."""
    if name is None:
        return None
    declared = {mode.name: mode for mode in modes}
    if name not in declared:
        raise typer.BadParameter(
            f"--mode {name!r} is not a mode this project declares"
            + (
                f"; it declares {', '.join(declared)}"
                if declared
                else "; it declares none"
            )
        )
    return declared[name]


def modes_help(modes: list[LaunchMode]) -> str:
    """The modes a launcher's ``--mode`` selects among, one per line, for its help."""
    return "".join(f"  --mode {mode.name}: {mode.help}" for mode in modes)


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


def move_checkout_codex_homes(dry_run: bool) -> list[str]:
    """Move this repository's Codex homes out of its checkouts, into lup's state.

    The whole of `harness codex-home migrate`, held here for the reason
    :func:`install_codex_plugin_home` is: the store is the Codex adapter's.
    """
    return move_codex_homes(project_root(), CodexWorktreeHomeStore(), dry_run=dry_run)


class StatedLaunch(TypedDict, total=False):
    """The launch fields a command line may state, each only where it states it."""

    resume: Resume
    max_recursive_agent: int
    record: Recording


class LaunchArguments(BaseModel, frozen=True, arbitrary_types_allowed=True):
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

    hold_generated: bool | None = None
    """``--hold-generated`` or ``--release-generated``, or ``None`` for neither."""

    container: OuterContainer = OuterContainer()
    """The project's own container, under the person's config, the mode and
    what this command line states."""

    max_recursive_agent: int | None = None
    transcribe_session: bool = False
    relaxed: bool = False
    mode: LaunchMode | None = None
    recorder: SessionRecorder | None = None

    def allowance(self, runtime: str) -> int:
        """The recursive-agent allowance: the flag's, else the mode's, else no limit."""
        if self.max_recursive_agent is not None:
            return self.max_recursive_agent
        stated = self.mode.allowance(runtime) if self.mode is not None else None
        return -1 if stated is None else stated

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

    def recording(self) -> Recording:
        """What the project keeps of a session: its transcript, its ledger entry, the mode's name."""
        return Recording(
            transcript=True,
            ledger=self.recorder,
            mode=self.mode.name if self.mode is not None else None,
        )

    def stated_claude(self) -> Claude:
        """What this command line states of a Claude Code declaration, and nothing else."""
        return Claude.model_validate(
            {
                **(
                    {"model": model_choice(self.model, TypeAdapter(ClaudeModelChoice))}
                    if self.model is not None
                    else {}
                ),
                **(
                    {"effort": effort_named(self.effort, claude_effort_named)}
                    if self.effort is not None
                    else {}
                ),
                **self.stated_launch(),
            }
        )

    def stated_codex(self) -> Codex:
        """What this command line states of a Codex declaration, and nothing else."""
        return Codex.model_validate(
            {
                **(
                    {"model": model_choice(self.model, TypeAdapter(CodexModelChoice))}
                    if self.model is not None
                    else {}
                ),
                **(
                    {"effort": effort_named(self.effort, codex_effort_named)}
                    if self.effort is not None
                    else {}
                ),
                **self.stated_launch(),
            }
        )

    def stated_launch(self) -> StatedLaunch:
        """The launch fields this command line states, spelled alike for either runtime."""
        stated = StatedLaunch()
        reopening = self.reopening()
        if reopening is not None:
            stated["resume"] = reopening
        if self.max_recursive_agent is not None:
            stated["max_recursive_agent"] = self.max_recursive_agent
        if self.transcribe_session:
            stated["record"] = Recording(transcript=True)
        return stated

    def refuse_hosted_mode(self, posture: LaunchSandbox, runtime: str) -> None:
        """Refuse a mode taking away what only a container stands in for, where none opens."""
        if self.mode is None or posture.contained():
            return
        asked = self.mode.contained_only(runtime)
        if asked:
            raise typer.BadParameter(
                f"mode {self.mode.name!r} asks for {', '.join(asked)}, which only "
                "a container stands in for, and this session opens on the host; "
                "launch it with --sandbox outer, where Docker or Podman answers"
            )

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
                ("hold_generated", self.hold_generated),
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
        """The container this launch opens: the command line over the mode, the person, the project.

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
        mode = self.mode.container if self.mode is not None else OuterContainer()
        return self.stated().over(mode.over(person.over(self.container.over(standing))))

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
            *(
                ["--hold-generated" if self.hold_generated else "--release-generated"]
                if self.hold_generated is not None
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

    The operator's answers to parked reviews, lent read-only, since every
    session parks what it may not do alone and must read the answer; and
    the dashboard, where the module is taken: every launch holds it,
    whichever runtime, sandbox or caller — a session opening a child session
    included — so there is one page for all of them.
    """
    return [ReviewAnswers(), *([Dashboard()] if harness.dashboard else [])]


def claude_declaration(
    composition: NativeHarnessComposition,
    request: LaunchArguments,
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
        project = Claude(
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
            record=request.recording(),
            max_recursive_agent=-1,
            profile=selected,
            home=home,
        )
        moded = (
            laid_over(request.mode.claude, project)
            if request.mode is not None
            else project
        )
        stated = laid_over(request.stated_claude(), moded)
        return laid_over(
            Claude(companions=[*stated.companions, *held_services(source)]), stated
        )

    declared(lambda: declaration(NoSandbox()))
    posture = request.posture(settle)
    if settle:
        request.refuse_hosted_mode(posture, "claude")
    wall = request.wall(posture, source.image, escapable=True)
    return declared(lambda: declaration(wall))


def codex_declaration(
    composition: NativeHarnessComposition,
    request: LaunchArguments,
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
        project = Codex(
            cwd=root,
            tools=CodexTools(
                builtin="stock", mcp=composition.servers, serve=composition.serve
            ),
            plugin=root,
            policy=plugin.hooks,
            requirements=source.requirements,
            sandbox=sandbox,
            identity=Member(wake_sockets=source.image.wake_sockets),
            record=request.recording(),
            max_recursive_agent=-1,
            profile=request.profile,
            home=home,
        )
        moded = (
            laid_over(request.mode.codex, project)
            if request.mode is not None
            else project
        )
        stated = laid_over(request.stated_codex(), moded)
        return laid_over(
            Codex(companions=[*stated.companions, *held_services(source)]), stated
        )

    declared(lambda: declaration(NoSandbox()))
    posture = request.posture(settle)
    if settle:
        request.refuse_hosted_mode(posture, "codex")
    wall = request.wall(posture, source.image, escapable=False)
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
    request: LaunchArguments,
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
            *request.words,
            steps=workflow_steps("claude", generation, checkpoint),
        )
    )


@usage_refusals()
def launch_codex(
    composition: NativeHarnessComposition,
    request: LaunchArguments,
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
            *request.words,
            steps=workflow_steps("codex", generation, checkpoint),
            force=force_install,
        )
    )
