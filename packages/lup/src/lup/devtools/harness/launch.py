"""This repository's launch workflow, and the Claude and Codex launchers over it.

Each launcher regenerates its target's artifacts, clears the gates this
repository keeps before a session -- its companion trees, its base, its
worktree pointers -- and maps its command line onto the session the
library composes (:mod:`lup.launch.session`), handing the terminal to the
native CLI with the non-interactive environment applied. What it reads
only here, the registrations in `sync.json.local`, it hands the library
as the grants a session stands on.
"""

import os
import shutil
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager, nullcontext
from pathlib import Path
from tempfile import mkdtemp
from typing import Protocol, runtime_checkable

import sh
import typer
from pydantic import BaseModel, Field, ValidationError

from lup.harness.devices import Device
from lup.providers.profile_tree import profile_directory
from lup.providers.profiles import DefaultHomeProfile, ProfileDirectory
from lup.providers.user_config import UserConfigFile
from lup.launch.config_volume import HomeSeedPlaces
from lup.providers.claude.model_choice import (
    claude_default_effort,
    claude_model_id,
    claude_effort,
    claude_effort_named,
    listed_claude_model,
    refuse_unsupported_effort as refuse_claude_effort,
)
from lup.providers.codex.model_choice import (
    codex_default_effort,
    codex_effort_arguments,
    codex_effort_named,
    codex_model_id,
    listed_codex_model,
    refuse_unsupported_effort as refuse_codex_effort,
)
from lup.providers.codex.subagents import CodexModelTiers
from lup.providers.claude.config_home import (
    ClaudeConfigUnreadable,
    selected_config_home,
)
from lup.providers.claude.home_seed import (
    ClaudeHomeSeed,
)
from lup.providers.claude.theme import settle_claude_theme
from lup.providers.claude.harness import ClaudeSpellings
from lup.providers.claude.transcripts import ClaudeTranscripts
from lup.providers.codex.harness import CodexSpellings
from lup.providers.codex.login import CODEX_LOGIN
from lup.providers.codex.marketplace import CodexMarketplace
from lup.providers.codex.profile import CodexProfileSettings
from lup.providers.codex.transcripts import CodexTranscripts
from lup.coordination.repository import launched_member
from lup.harness.environment import non_interactive_environment
from lup.harness.models import NativeName, Plugin, Resumption
from lup.launch.refusal import LaunchRefused
from lup.sandbox.rail import (
    AccessibleRoot,
)
from lup.devtools.sync import accessible_roots, granted_devices
from lup.launch.session import (
    LaunchOpening,
    StandingGrants,
    ambient_config_home,
    personal_config,
    placed_wake_socket,
    runtime_preflight,
    session_argv,
    start_harness_transcript,
)
from lup.providers.claude.session import carry_claude_home
from lup.providers.codex.session import (
    carry_codex_home,
    codex_login_preflight,
    prepare_codex_plugin,
    settled_codex_seed,
)
from lup.launch.boundary import apply_sandbox_environment
from lup.launch.declaration import LaunchSandbox, settled_sandbox
from lup.providers.claude.launch import (
    claude_resume_arguments,
    claude_sandbox_arguments,
    companion_plugin_directories,
)
from lup.providers.codex.launch import codex_resume_arguments, codex_sandbox_arguments
from lup.harness.notice import Notice
from lup.harness.process import LocalProcessLauncher
from lup.harness.toolchain import (
    bubblewrap_requirement,
    socat_requirement,
)
from lup.observability.audit import (
    TraceJournal,
)
from lup.observability.sessions import SessionRecorder
from lup.sessions.recursion import MAX_RECURSIVE_AGENT_ENV
from lup.types import EnvVars, JsonObject
from lup.workspace.paths import project_root
from lup.providers.codex.home import (
    CodexWorktreeHomeStore,
    select_codex_home,
)
from lup.devtools.harness.composition import NativeTargets
from lup.devtools.dev.branches import settle_base_freshness
from lup.devtools.harness.drift import (
    RepositoryWriter,
    generate_targets,
    generate_with_report,
)
from lup.harness.generate import NativeHarnessComposition
from lup.launch.preflight import (
    LaunchSentinels,
    exclude_sandbox_placeholders,
    release_ledger,
    sweep_ledgers,
)
from lup.devtools.dev.worktree import RelocationHint, refuse_redirected_pointers
from lup.devtools.layout import find_tree_dir


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
    """This repository's registrations, as the library asks a launch for them.

    Read by :mod:`lup.devtools.sync` from the registry and `sync.json.local`,
    which are this repository's bookkeeping rather than the launch's. Named
    at the call rather than captured at import, so the registry asked is the
    one in force when the launch asks.
    """
    return StandingGrants(roots=accessible_roots, devices=granted_devices)


def declared_mounts(
    writable: list[Path], read_only: list[Path]
) -> list[AccessibleRoot]:
    """The folders one command line asked this session to reach, as roots.

    The same shape a `sync.json.local` registration resolves to, because the
    two say the same thing at different lifetimes: a registration is standing
    and reviewed, a flag lasts one launch. Everything downstream -- the lease,
    the boundary declaration, each runtime's own widening -- already speaks
    this type, so the flag costs no second path.
    """
    return [
        *[AccessibleRoot(path=path) for path in writable],
        *[AccessibleRoot(path=path, writable=False) for path in read_only],
    ]


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


def ready_to_open(
    composition: NativeHarnessComposition,
    generate_only: bool,
    sentinels: LaunchSentinels,
    companions: list[NativeHarnessComposition] = [],
    repository_writers: list[RepositoryWriter] = [],
    sandbox: LaunchSandbox | None = None,
) -> LaunchOpening | None:
    """Generate this target's artifacts and clear every gate standing before a session.

    Both launchers reach a session through here, so a gate added once is a
    gate every entry point makes — including one written later, which cannot
    open a session without first generating the artifacts it opens against.
    Answers whether to go on: a generate-only invocation has already done
    everything it was asked for.

    ``companions`` are the trees this launch does not open and regenerates
    anyway, which is what makes launching a runtime mean what `harness
    generate all` means. A launcher that left the other's tree behind
    whenever a shared source moved would fail the next `dev check` on drift
    nobody had introduced -- reported against a session that had done
    nothing but open. They are generated in passing, so a tree that is
    already current says nothing.

    ``None`` is that answer, and the opening is the other one — including an
    empty roster, which is why this is not a list and a truth test. What
    those findings are *for* is the boundary preflight, which needs both
    halves of one measurement and can only be assembled where the second
    half is taken; carrying them out of here is what saves the launch from
    exercising the same probes twice and reporting each of them twice.

    Settling the base is one of those steps rather than a workflow's own. A
    tree whose base has moved is self-consistent and says nothing about it, so
    a session opened on one plans and edits against code that is no longer
    there — which cost a planning pass over thirteen concerns on a tree ten
    commits behind its remote, where two merged pull requests had already done
    part of the work being planned. Being behind is not itself grounds for
    refusing a session, so what happens here is a sync and a report: a clean
    checkout is brought level with its own remote, and a base that has moved
    is named on the way in.

    Settling the sandbox is another. ``sandbox`` is what the command line
    asked for, ``None`` where it named none, and :func:`settled_sandbox`
    answers it once here, as the first thing asked of the host: the host
    roster depends on which side of the container the session runs, and
    everything after reads the answer off the opening. After generation, so
    a generate-only invocation, which opens nothing, is neither probed nor
    warned.

    The two lines said here are said before their work rather than after
    it, for the reason the fetch names itself below: these are the stretches
    a launch spends silent when everything is current, and a line naming
    the wait is what separates a slow one from a stopped one. A
    generate-only invocation reports each tree anyway, so it is not told.
    """
    if not generate_only:
        typer.echo("regenerating what this session opens against")
    generate_with_report(composition, in_passing=not generate_only)
    generate_targets(companions, repository_writers, in_passing=not generate_only)
    if generate_only:
        return None
    # Before any host git runs on the way in -- base freshness, status, the
    # preflight's own probes -- so a session a previous contained one left with
    # a redirected worktree pointer is refused here rather than opened onto git
    # reading the config that pointer now leads to.
    refuse_redirected_pointers()
    # Before anything writes one, so a launch that was killed last week does
    # not leave its measurement standing for somebody to find. On the way in
    # rather than only on the way out, because the launch that crashed is
    # exactly the one that did not get to tidy up after itself.
    sweep_ledgers(project_root())
    # The runtime sandbox's own leavings, taken out of `git status` before a
    # session reads it: said only when something was added, because a line
    # repeated on every launch is read on none.
    excluded = exclude_sandbox_placeholders(project_root())
    if excluded:
        typer.echo(
            f"excluded {len(excluded)} sandbox placeholder file(s) from git "
            f"status: {', '.join(excluded)}"
        )
    typer.echo("checking the host")
    opening = LaunchOpening(sandbox=settled_sandbox(sandbox, "pass `--sandbox inner`"))
    opening.findings = runtime_preflight(
        composition.recipe.label,
        composition.readiness,
        composition.recipe.source.requirements,
        project_root(),
        sentinels,
        opening,
        opening.sandbox.contained(),
        standing=standing_grants(),
    )
    settle_base_freshness(LocalProcessLauncher(), project_root())
    return opening


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
    it. The launchers take one and read it; nothing here knows what any
    particular mode is *for*, which is what keeps a downstream project's
    vocabulary out of the framework.
    """

    name: NativeName
    """Spells the flag: a mode named ``syra`` is selected by ``--syra``."""

    help: str = Field(min_length=1)

    targets: NativeTargets
    """What generation compiles while this mode is in force.

    A mode changes the tree rather than only the command line, because the
    thing a mode usually adds — a tool server, a hook, a document — has to
    reach the session through an artifact the runtime reads at startup."""

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
        branch at the call site, so a launcher holds one shape and a mode
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


# It takes a composition, an account, a profile, a model and a passthrough
# vector, and a mode is one optional argument among them; moving it onto
# LaunchMode would make the model answerable for starting a runtime it knows
# nothing about, and leave a project declaring no mode with no launcher at all.
@usage_refusals()
def launch_claude(
    composition: NativeHarnessComposition,
    extra_args: list[str],
    profiles: ProfileDirectory,
    profile: str | None,
    model: str | None,
    generate_only: bool,
    mode: LaunchMode | None = None,
    resume: Resumption = Resumption(),
    relaxed: bool = False,
    sandbox: LaunchSandbox | None = None,
    checkpoint: LaunchCheckpoint | None = None,
    max_recursive_agent: int = -1,
    transcribe_session: bool = False,
    companions: list[NativeHarnessComposition] = [],
    repository_writers: list[RepositoryWriter] = [],
    mounts: list[AccessibleRoot] = [],
    devices: list[Device] = [],
    recorder: SessionRecorder | None = None,
    effort: str | None = None,
) -> None:
    """Generate/reconcile Claude artifacts and launch the verified local plugin.

    ``sandbox`` is what the command line asked for, ``None`` where it named
    none; :func:`settled_sandbox` answers the default.
    """
    contradiction = resume.contradicted()
    if contradiction is not None:
        raise typer.BadParameter(contradiction)
    config = UserConfigFile()
    personal = personal_config(config)
    # A mode's model is a default rather than a fixture: it says what this kind
    # of session runs on when nobody said otherwise, and an explicit --model
    # still wins, because overriding the model is why a caller passes one.
    # Where neither names one, the person's tier does, as it does in code.
    selected_model = (
        model
        or (mode.native_model("claude") if mode is not None else None)
        or claude_model_id(personal.tier)
    )
    # Refused before anything is generated or checkpointed: an effort the
    # model's catalog row lacks would be dropped by the CLI without a word.
    # Unnamed, it is the model's default from the person's preferred rung, the
    # one a session declared in code takes, rather than the CLI's own settings.
    listed = None if selected_model is None else listed_claude_model(selected_model)
    try:
        chosen_effort = (
            claude_default_effort(listed, personal.effort or "xhigh")
            if effort is None
            else claude_effort_named(effort)
        )
        refuse_claude_effort(listed, chosen_effort)
    except ValueError as refusal:
        raise typer.BadParameter(str(refusal)) from refusal
    compiled_effort = None if chosen_effort is None else claude_effort(chosen_effort)
    if checkpoint is not None and not generate_only:
        checkpoint(provider="claude")
    plugin = composition.recipe.source.plugins[0]
    announce_relaxed_rules(relaxed, plugin)
    sentinels = LaunchSentinels()
    cleared = ready_to_open(
        composition,
        generate_only,
        sentinels,
        companions,
        repository_writers,
        sandbox=sandbox,
    )
    if cleared is None:
        return
    # What the gate settled on, which is the only posture read from here on.
    sandbox = cleared.sandbox
    arguments: list[str] = claude_resume_arguments(resume)
    if selected_model is not None:
        arguments.extend(["--model", selected_model])
    if compiled_effort is not None:
        arguments.extend(compiled_effort.arguments())
    root = project_root()
    # Minted here rather than where the argv is settled, because this runtime
    # shows the name in its own chrome and the flag carrying it is built now;
    # the same identity is handed on so the exported one agrees with it.
    member = launched_member(root)
    wake_socket = placed_wake_socket(
        composition.recipe.source.image.wake_sockets, root, member
    )
    named = [
        root / ".claude" / "plugins" / plugin.name,
        *companion_plugin_directories(root, plugin.name),
    ]
    arguments.extend(
        [
            *[flag for directory in named for flag in ("--plugin-dir", str(directory))],
            *claude_sandbox_arguments(
                plugin.hooks,
                sandbox=sandbox,
                accessible=(
                    [*mounts, *accessible_roots()]
                    if sandbox is LaunchSandbox.INNER
                    else []
                ),
                settings=(
                    compiled_effort.settings if compiled_effort is not None else None
                ),
                tree=find_tree_dir(),
            ),
            # What this runtime shows in its own chrome, made to agree with
            # the name the roster answers to: the same minted name is
            # exported for the session's tool server to join under, numbered
            # already where a live session in this worktree has the plain
            # one. The roster's name lives on the member's own file in the
            # store and is what addressing resolves through, so this is a
            # display detail rather than the identity — which is why Codex,
            # whose launch takes no such flag, loses nothing by it: a peer
            # there is addressed by exactly the same name, and renames through
            # the same command.
            #
            # Ahead of `extra_args`, so a caller who named their own session
            # still wins.
            "--name",
            member.cli_name,
            # Where this session binds the wake socket a peer nudges it
            # through, keyed by the member id minted above; nowhere leaves the
            # flag off and the session on its own default.
            *(
                ["--messaging-socket-path", wake_socket]
                if wake_socket is not None
                else []
            ),
            *(mode.command_words("claude") if mode is not None else []),
            *extra_args,
        ]
    )
    environment = non_interactive_environment(os.environ)  # lup: ignore[os-environ]
    environment[MAX_RECURSIVE_AGENT_ENV] = str(max_recursive_agent)
    apply_sandbox_environment(
        plugin.hooks,
        environment,
        "claude",
        [bubblewrap_requirement(), socat_requirement()],
        sandbox=sandbox,
    )
    # A name no origin answers to reaches here from an explicit --profile, and
    # from an active selection whose profile has since gone; a profile naming
    # the default home arrives by either route too. Each is the caller's to
    # fix, so none should arrive as a traceback.
    try:
        home = profiles.launch_home(profile)
    except (KeyError, DefaultHomeProfile) as error:
        raise typer.BadParameter(str(error)) from error
    if home is not None:
        environment.update(profiles.login.environment(home))
    # The theme is the account's, handed through its own files rather than as
    # a launch override, which would outrank a session's /theme: filled in
    # where the account keeps none, replaced only where the person's lup
    # config names one. A session on the host runs in the account's own home,
    # so what it changes there is the account's already.
    # lup: solved: a contained session runs in its repository's config volume,
    # so no theme reaches it and none it sets returns to the account; which
    # home a container's theme belongs to is the volume's question.
    # A contained session runs in its repository's volume instead, so the
    # account's settings are seeded into it at every start, the person's lup
    # config winning, and what the session changed of the person's comes
    # back when it closes.
    account = selected_config_home(environment)
    try:
        seed = (
            ClaudeHomeSeed.compose(
                account,
                personal,
                model=claude_model_id(personal.tier),
                effort=(
                    None
                    if personal.effort is None
                    else claude_effort(personal.effort).level
                ),
            )
            if sandbox.contained()
            else None
        )
        if seed is None:
            settle_claude_theme(account, personal.theme.claude)
    except ClaudeConfigUnreadable as error:
        raise typer.BadParameter(str(error)) from error
    seeded_at = Path(mkdtemp(prefix="lup-home-seed-")) if seed is not None else None
    places = (
        HomeSeedPlaces(
            seed=seed.write(seeded_at / "seed"), applied=seeded_at / "applied"
        )
        if seed is not None and seeded_at is not None
        else None
    )
    # Only a volume the seed reached is compared with it: before the
    # container starts, the volume still holds the previous session's
    # settings, and those read against this seed are no session's changes.
    seed_applied = False
    transcribing = transcribe_session or mode is None or mode.transcribes("claude")
    transcript = start_harness_transcript(
        "claude",
        ClaudeTranscripts(home),
        root,
        model=selected_model,
        profile=profile,
        arguments=arguments,
        record_root=mode.transcript_root() if mode is not None else None,
        mode=mode.name if mode is not None else None,
        transcribe=transcribing,
        recorder=recorder,
    )
    succeeded = False
    interrupted = False
    try:
        opening = (
            nullcontext({})
            if mode is None
            else mode.opened("claude", transcript.journal, transcribing)
        )
        with opening as session:
            environment.update(session)
            argv = session_argv(
                "claude",
                arguments,
                root,
                composition.recipe.source.image,
                composition.recipe.source.requirements,
                plugin.hooks,
                home if home is not None else ambient_config_home(profiles.login),
                profiles.login,
                sandbox,
                environment,
                transcript.journal.path,
                sentinels,
                cleared,
                mounts,
                devices,
                member=member,
                home_seed=places,
                standing=standing_grants(),
                clipboard=composition.clipboard_transport,
            )
            seed_applied = places is not None
            sh.Command(argv[0])(*argv[1:], _fg=True, _env=environment)
        succeeded = True
    except KeyboardInterrupt:
        interrupted = True
        raise
    except sh.CommandNotFound as error:
        raise typer.BadParameter(
            f"Cannot launch Claude Code: executable {error} was not found. Check PATH."
        ) from error
    except sh.ErrorReturnCode as error:
        raise typer.Exit(error.exit_code) from error
    finally:
        # Taken away here rather than swept by the next launch, because a
        # launch that swept every ledger but its own would be correct exactly
        # once: with a second session open it removes a measurement that
        # session's dispatcher is still reading.
        release_ledger(project_root(), sentinels.nonce)
        transcript.close(succeeded=succeeded, interrupted=interrupted)
        if places is not None and seed_applied:
            # Measured against what this launch applied, which is the
            # seed settled against whatever a running session had changed.
            carry_claude_home(
                composition.recipe.source.image,
                project_root(),
                profiles.login,
                ClaudeHomeSeed.applied(places.applied),
                account,
                config,
                personal,
            )
        if seeded_at is not None:
            shutil.rmtree(seeded_at)
        if checkpoint is not None:
            checkpoint(provider="claude")


# For the reason spelled at `launch_claude`: the mode is one optional argument
# among the ones that actually decide how a runtime starts.
@usage_refusals()
def launch_codex(
    composition: NativeHarnessComposition,
    extra_args: list[str],
    codex_home: Path | None,
    profile: str | None,
    model: str | None,
    generate_only: bool,
    force_install: bool,
    mode: LaunchMode | None = None,
    resume: Resumption = Resumption(),
    relaxed: bool = False,
    sandbox: LaunchSandbox | None = None,
    checkpoint: LaunchCheckpoint | None = None,
    max_recursive_agent: int = -1,
    transcribe_session: bool = False,
    companions: list[NativeHarnessComposition] = [],
    repository_writers: list[RepositoryWriter] = [],
    mounts: list[AccessibleRoot] = [],
    devices: list[Device] = [],
    recorder: SessionRecorder | None = None,
    effort: str | None = None,
) -> None:
    """Generate/reconcile Codex artifacts and launch without updating the CLI.

    ``sandbox`` is read as :func:`launch_claude` reads it.
    """
    contradiction = resume.contradicted()
    if contradiction is not None:
        raise typer.BadParameter(contradiction)
    config = UserConfigFile()
    personal = personal_config(config)
    named_model = model or (mode.native_model("codex") if mode is not None else None)
    # A named profile with no model named over it chose its model and effort
    # together, so neither the person's tier nor a default effort is sent.
    profiled = profile is not None and named_model is None
    selected_model = (
        named_model
        if named_model is not None or profiled
        else codex_model_id(personal.tier, CodexModelTiers())
    )
    # Refused before anything is generated or checkpointed: the API refuses an
    # effort the model lacks with a 400 that names neither. Unnamed, it is the
    # model's default from the person's preferred rung, the one a session
    # declared in code takes, rather than whatever the home's configuration says.
    listed = None if selected_model is None else listed_codex_model(selected_model)
    try:
        chosen_effort = (
            codex_effort_named(effort)
            if effort is not None
            else None
            if profiled
            else codex_default_effort(
                listed, CodexModelTiers(), personal.effort or "xhigh"
            )
        )
        refuse_codex_effort(listed, chosen_effort, CodexModelTiers())
    except ValueError as refusal:
        raise typer.BadParameter(str(refusal)) from refusal
    if checkpoint is not None and not generate_only:
        checkpoint(provider="codex")
    plugin = composition.recipe.source.plugins[0]
    announce_relaxed_rules(relaxed, plugin)
    sentinels = LaunchSentinels()
    cleared = ready_to_open(
        composition,
        generate_only,
        sentinels,
        companions,
        repository_writers,
        sandbox=sandbox,
    )
    if cleared is None:
        return
    # What the gate settled on, which is the only posture read from here on.
    sandbox = cleared.sandbox
    environment = non_interactive_environment(os.environ)  # lup: ignore[os-environ]
    environment[MAX_RECURSIVE_AGENT_ENV] = str(max_recursive_agent)
    envelope = codex_sandbox_arguments(
        plugin.hooks,
        environment,
        extra_args,
        sandbox=sandbox,
        accessible=(
            [*mounts, *accessible_roots()] if sandbox is LaunchSandbox.INNER else []
        ),
        tree=find_tree_dir(),
    )
    # The account a worktree home is derived from, and returns its login and
    # settings to: the selected profile, this checkout's then the global one,
    # resolved as on Claude, else the operator's own default home.
    try:
        account_home = profile_directory(CODEX_LOGIN, config).launch_home(None)
    except (KeyError, DefaultHomeProfile) as error:
        raise typer.BadParameter(str(error)) from error
    store = CodexWorktreeHomeStore(
        account_home=account_home or CODEX_LOGIN.ambient_home,
        theme=personal.theme.codex,
        editor=personal.editor,
        settings=personal.codex.settings,
    )
    # Only a volume the settings reached is compared with them: before the
    # container's home is prepared, it still holds the previous session's.
    installed: list[Path] = []
    # What a contained session's volume was given, settled three ways
    # against a session that may still be running there.
    applied: list[JsonObject] = []
    home = select_codex_home(codex_home, environment, project_root(), profile, store)
    selected_home = home.path
    selected_profile = (
        CodexProfileSettings.capture(
            selected_home, profile, as_base=sandbox.contained()
        )
        if profile is not None or sandbox.contained()
        else None
    )
    if home.isolated:
        typer.echo(
            f"Using worktree-scoped Codex home: {selected_home}, derived from "
            f"{store.account_home}"
        )
    # The subcommand leads, and everything the envelope carries follows it,
    # because a word placed after a positional session id would be read as
    # another one.
    arguments: list[str] = [*codex_resume_arguments(resume), *envelope]
    if selected_profile is not None and not selected_profile.as_base:
        arguments.extend(
            [
                "--profile",
                selected_profile.installed_name(),
            ]
        )
    if selected_model is not None:
        arguments.extend(["--model", selected_model])
    if chosen_effort is not None:
        arguments.extend(codex_effort_arguments(chosen_effort))
    arguments.extend(mode.command_words("codex") if mode is not None else [])
    arguments.extend(extra_args)
    environment["CODEX_HOME"] = str(selected_home)
    transcribing = transcribe_session or mode is None or mode.transcribes("codex")
    transcript = start_harness_transcript(
        "codex",
        CodexTranscripts(selected_home),
        project_root(),
        model=selected_model,
        profile=profile,
        arguments=arguments,
        record_root=mode.transcript_root() if mode is not None else None,
        mode=mode.name if mode is not None else None,
        transcribe=transcribing,
        recorder=recorder,
    )
    succeeded = False
    interrupted = False
    opening = (
        nullcontext({})
        if mode is None
        else mode.opened("codex", transcript.journal, transcribing)
    )

    def authenticate(command: list[str], native_home: Path, headless: bool) -> None:
        codex_login_preflight(
            native_home,
            environment,
            command,
            headless=headless,
            profile=None if sandbox.contained() else profile,
            consent=lambda question: typer.confirm(question, default=True),
        )
        if home.isolated and not sandbox.contained():
            store.publish(project_root())

    def prepare(prefix: list[str], native_home: Path) -> None:
        if prefix and selected_profile is not None:
            applied.append(
                settled_codex_seed(
                    composition.recipe.source.image,
                    project_root(),
                    selected_profile.personal_settings(
                        CodexMarketplace.declared(project_root()) is not None
                    ),
                )
            )
        prepare_codex_plugin(
            prefix,
            native_home,
            project_root(),
            environment,
            force_install,
            settings=selected_profile,
        )
        installed.append(native_home)

    try:
        with opening as session:
            environment.update(session)
            argv = session_argv(
                "codex",
                arguments,
                project_root(),
                composition.recipe.source.image,
                composition.recipe.source.requirements,
                plugin.hooks,
                selected_home,
                CODEX_LOGIN,
                sandbox,
                environment,
                transcript.journal.path,
                sentinels,
                cleared,
                mounts,
                devices,
                authenticate=authenticate,
                prepare=prepare,
                standing=standing_grants(),
                clipboard=composition.clipboard_transport,
            )
            sh.Command(argv[0])(*argv[1:], _fg=True, _env=environment)
        succeeded = True
    except KeyboardInterrupt:
        interrupted = True
        raise
    except sh.CommandNotFound as error:
        raise typer.BadParameter(
            f"Cannot launch Codex: executable {error} was not found. Check PATH."
        ) from error
    except sh.ErrorReturnCode as error:
        raise typer.Exit(error.exit_code) from error
    finally:
        release_ledger(project_root(), sentinels.nonce)
        transcript.close(succeeded=succeeded, interrupted=interrupted)
        if home.isolated and store.publish(project_root()):
            typer.echo("Returned the refreshed Codex login to the account home")
        # lup: solved: a contained session runs in its repository's config
        # volume, so a setting it changes there — its /theme included — never
        # returns to the account; which home those belong to is the volume's
        # question.
        if home.isolated and (installed or not sandbox.contained()):
            carry_codex_home(
                store,
                composition.recipe.source.image if sandbox.contained() else None,
                project_root(),
                config,
                applied[0] if applied else None,
            )
        if checkpoint is not None:
            checkpoint(provider="codex")
