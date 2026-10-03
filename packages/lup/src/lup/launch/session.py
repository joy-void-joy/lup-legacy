"""Composing a native session: what a launch clears, opens, and records.

What every launch of a declared agent does between its declaration and the
native CLI taking the terminal, whichever repository asked for it: the
runtime and host rosters exercised before anything opens, the boundary
compiled and measured and refused where it fell short, the argv that opens
the session inside the declared container or on the host, and the
transcript a hand-driven session leaves behind.

What a repository adds around that -- which trees it generates first, which
registrations it keeps, how its command line spells a refusal -- is the
caller's, handed in rather than read from here. A launch that cannot open
raises :class:`~lup.launch.refusal.LaunchRefused`.
"""

import logging
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel

from lup.harness.devices import Device
from lup.providers.login import ProviderLogin
from lup.providers.user_config import UserConfig, UserConfigFile
from lup.launch.config_volume import HomeSeedPlaces
from lup.launch.container import contained_argv, held_lease, state_volume_name
from lup.coordination.identity import MEMBER_ENV, NAME_ENV, LaunchedMember
from lup.coordination.repository import RepositoryPeers, launched_member
from lup.harness.wake_sockets import WakeSockets
from lup.workspace.edition import shared_git_directory
from lup.harness.models import HookSet
from lup.policy.boundary import BoundaryPreflight
from lup.policy.identity import POLICY_ROOT_ENV
from lup.policy.profiles import compile_boundary, depended_on, measured
from lup.policy.snapshots import accept_destination_policies, destination_authorities
from lup.launch.pointer_trust import judged_roots, launcher_state_exposure
from lup.launch.refusal import LaunchRefused
from lup.sandbox.rail import (
    AccessibleRoot,
    NestedRepository,
    fleet_lease,
)
from lup.launch.declaration import LaunchSandbox
from lup.harness.notice import Banner, Notice
from lup.harness.requirements import (
    Finding,
    HostFacts,
    Manifest,
    refused,
)
from lup.harness.toolchain import (
    container_client,
    for_host,
    granted_device_requirement,
)
from lup.observability.audit import (
    ArgvRedaction,
    KeyRedaction,
    PathRedaction,
    PortableRoot,
    Redactions,
    TraceActor,
    TraceContext,
    TraceJournal,
    chain_break,
    read_observable_events,
)
from lup.observability.native import NativeTranscripts, NativeTranscriptWatcher
from lup.observability.sessions import Session, SessionRecorder
from lup.sessions.recursion import MAX_RECURSIVE_AGENT_ENV
from lup.types import EnvVars, JsonObject, JsonValue
from lup.workspace.history import run_transcript
from lup.workspace.paths import agent_version, harness_runs_path
from lup.harness.clipboard import ClipboardTransport
from lup.harness.generate import RuntimeReadiness
from lup.harness.image import Image, MemoryLimit, SessionPrivileges
from lup.launch.preflight import (
    LaunchSentinels,
    ROOT_VARIABLE,
    exclude_sandbox_placeholders,
    record_preflight,
    retire_mount_table,
    sweep_ledgers,
)


type Reporter = Callable[[str], None]
"""Where a registry says what it did on the way past, one line at a time."""


class StandingGrants(BaseModel, frozen=True):
    """What a machine keeps granting every session it launches, asked at the launch.

    The roots a repository's registrations make reachable and the devices a
    machine grants are that repository's own bookkeeping rather than the
    launch's, so a launch asks for them instead of reading a registry it does
    not know. Asked rather than handed over resolved, because resolving a
    registration can clone it and says so as it goes: the launch asks once,
    where it knows which posture it took, and routes what the registry says
    into its own opening. The default grants nothing, which is what a caller
    keeping no registry should get.
    """

    roots: Callable[[Reporter], list[AccessibleRoot]] = lambda _report: []
    """Every registered root a session may reach, reporting as it resolves them."""

    devices: Callable[[Reporter], list[Device]] = lambda _report: []
    """Every host device this machine grants its sessions."""


class LaunchOpening(BaseModel):
    """What clearing the gates before a session left for the opening to say.

    The banner travels with the findings because the two are halves of one
    answer and are produced at opposite ends of the launch: the runtime is
    established here, the boundary is measured after the container starts,
    and the reader wants them in one block ordered by what they might have to
    do about it. A component that printed as it went would put the version
    above the roster above the boundary, which is the order the launcher
    happens to work in and no order at all to anybody reading it.
    """

    findings: list[Finding] = []
    """The host roster, carried to the boundary preflight that needs it."""

    banner: Banner = Banner()
    """Every line held so far, said once the last measurement is in."""

    runtime: str = ""
    """The runtime this opens, and the version of it that answered."""

    sandbox: LaunchSandbox = LaunchSandbox.OUTER
    """The sandbox this session opens under, settled once for everything after.

    Carried rather than recomputed, because the default is settled by asking
    the host and says so when it falls back: a launcher reading the flag
    again would see the question and not its answer."""


class HarnessTranscript(BaseModel, arbitrary_types_allowed=True):
    """Canonical journal and native watcher owned by one CLI launch.

    An interactive CLI owns its terminal, so a launch cannot be wrapped the way
    an SDK session is. Mirroring what the CLI persists into a journal of our own
    is what makes a hand-driven session produce the same observable trace a
    programmatic run does -- and what a launch path that starts nothing quietly
    takes away, since a trace nobody wrote is indistinguishable from a session
    nobody ran.
    """

    journal: TraceJournal
    watcher: NativeTranscriptWatcher | None = None
    diagnostics: logging.Handler | None = None
    record: Session | None = None
    """The ledger node pointing at this launch's directory, where one was recorded."""

    recorder: SessionRecorder | None = None

    def close(self, *, succeeded: bool, interrupted: bool = False) -> None:
        """Stop ingestion, record the outcome, release the diagnostics log, and verify.

        The ledger's record of the launch is amended after the journal holds
        its final record, so the digest pinned is the closed journal's. The
        chain is checked last, over that same closed journal, so nothing it
        finds can keep the launch from tidying up.
        """
        if self.watcher is not None:
            self.watcher.stop()
        self.journal.emit("run_end", {"succeeded": succeeded})
        if self.diagnostics is not None:
            watcher_logger().removeHandler(self.diagnostics)
            self.diagnostics.close()
        if self.recorder is not None and self.record is not None:
            self.recorder.closed(
                self.record,
                "interrupted"
                if interrupted
                else "completed"
                if succeeded
                else "failed",
            )
        self.verified()

    def verified(self) -> None:
        """Say so, at the end of the session, where its transcript's chain does not hold.

        The person who ran the session is still at the terminal, and a
        transcript that does not verify is better learned of now than by
        whoever later reads it as evidence. One that holds says nothing,
        since a line on every launch is read on none.
        """
        run = self.journal.path.parent.name
        try:
            broken = chain_break(read_observable_events(self.journal.path))
        except OSError as error:
            Notice(
                text=f"this session's transcript could not be read to verify it: {error}",
                urgency="warning",
            ).say()
            return
        if broken is None:
            return
        Notice(
            text=(
                f"this session's transcript does not verify: {broken.explained()}; "
                f"`uv run lup-devtools trace events {run}` reads it, the break marked"
            ),
            urgency="warning",
        ).say()


def watcher_logger() -> logging.Logger:
    """The logger the native transcript watcher reports its own failures on."""
    return logging.getLogger(NativeTranscriptWatcher.__module__)


def capture_watcher_diagnostics(run_directory: Path) -> logging.Handler:
    """Send watcher diagnostics to a file for the life of one launch.

    The launcher hands its terminal to an interactive CLI that draws over the
    whole screen. Nothing configures logging on this path, so a watcher failure
    would reach Python's last-resort handler and print a traceback into that UI
    -- so a recovered polling error would read as a crash. The durable
    record is the journal's own error event; this file is for the detail
    that does not belong in it.
    """
    run_directory.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(run_directory / "watcher.log", encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger = watcher_logger()
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    return handler


def portable_roots(root: Path) -> list[PortableRoot]:
    """The roots a durable transcript should name by role, not by location.

    The checkout ``root``, the tree of sibling checkouts around it, and the
    operator's home — between them, everywhere a session's paths come from.
    Ordered widest-last is irrelevant here because the rule sorts by length;
    what matters is that all three are offered, since a payload quoting a
    sibling worktree names none of the other two.
    """
    return [
        PortableRoot(label="<project>", path=root),
        PortableRoot(label="<tree>", path=root.parent),
        PortableRoot(label="<home>", path=Path.home()),
    ]


def start_harness_transcript(
    provider: str,
    transcripts: NativeTranscripts,
    root: Path,
    *,
    model: str | None,
    profile: str | None,
    arguments: list[str],
    record_root: Path | None = None,
    mode: str | None = None,
    transcribe: bool = True,
    recorder: SessionRecorder | None = None,
) -> HarnessTranscript:
    """Start one canonical transcript around a native interactive CLI.

    The runtime arrives as its own transcript reader rather than as a directory
    to scan, because where a runtime keeps its sessions and how one of its
    records names itself are the runtime's business, not this launcher's.

    ``record_root`` is where the transcript tree is rooted, a relative one
    in the checkout the session works in, so a launch mode
    whose records are kept to a different standard keeps them somewhere a
    reader can tell apart without opening one. ``mode`` puts the same fact
    inside the record, because a directory is renameable and a run that has
    been copied out of one should still say what it was.

    This is the writer that opens a launch's directory, so it is where the
    launch is recorded in the ledger: handed a ``recorder``, it records one
    :class:`~lup.observability.sessions.Session` pointing at the directory
    and its observable journal, which :meth:`HarnessTranscript.close` amends
    with the outcome. The harness command tree wires the recorder from the
    project's declared kinds; handed none, nothing is recorded.

    ``root`` is the checkout the session works in: the paths its record
    names by role, and the scope its native transcripts are read from.
    """
    run_id = (
        f"{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}_{provider}_{uuid4().hex[:8]}"
    )
    runs = root / record_root if record_root is not None else harness_runs_path()
    trace_path = run_transcript(runs / provider / run_id)
    journal = TraceJournal(
        trace_path,
        TraceContext.root(
            run_id,
            TraceActor(
                kind="harness",
                name=f"lup-devtools harness {provider}",
                provider=provider,
                model=model,
            ),
        ),
        # A harness transcript is the one journal written to be kept and read
        # later, so it carries the path rule the in-process default does not:
        # what it mirrors is a native CLI's own record, full of this machine's
        # directories in prose no key-name rule can see.
        redaction=Redactions(KeyRedaction(), PathRedaction(portable_roots(root))),
    )
    # An argv vector reaches the journal only redacted: these are the words a
    # caller typed, and a credential passed as an option value is a value the
    # key-name redaction cannot see.
    safe_arguments: list[JsonValue] = list(ArgvRedaction().arguments(arguments))
    payload: JsonObject = {
        "provider": provider,
        "model": model,
        "profile": profile,
        "mode": mode,
        "arguments": safe_arguments,
    }
    journal.emit("run_start", payload)
    record = (
        recorder.opened(provider, agent_version(), trace_path.parent, trace_path)
        if recorder is not None
        else None
    )
    if not transcribe:
        return HarnessTranscript(journal=journal, record=record, recorder=recorder)
    watcher = NativeTranscriptWatcher(
        transcripts,
        journal.child(
            TraceActor(
                kind="native_agent",
                name=provider,
                provider=provider,
                model=model,
            )
        ),
        scope=root,
    )
    diagnostics = capture_watcher_diagnostics(trace_path.parent)
    watcher.start()
    # Said by the banner rather than here, where it was printed a second time
    # under `Artifacts` a few lines later: one path, twice, in two different
    # spellings of the same sentence.
    return HarnessTranscript(
        journal=journal,
        watcher=watcher,
        diagnostics=diagnostics,
        record=record,
        recorder=recorder,
    )


def cleared_on_the_way_in(root: Path) -> None:
    """Clear what earlier launches left in the checkout, before this one reads it.

    Measurements a killed launch never took away are swept here rather than
    only on the way out, because the launch that crashed is exactly the one
    that did not get to tidy up after itself. The runtime sandbox's own
    leavings are taken out of `git status` before a session reads it, said
    only when something was added, because a line repeated on every launch
    is read on none. Then the host is checked, and that stretch is named
    before it starts, since a launch spends it silent when everything is
    current and a line naming the wait is what tells a slow one from a
    stopped one.
    """
    sweep_ledgers(root)
    excluded = exclude_sandbox_placeholders(root)
    if excluded:
        Notice(
            text=(
                f"excluded {len(excluded)} sandbox placeholder file(s) from git "
                f"status: {', '.join(excluded)}"
            ),
            urgency="detail",
        ).say()
    Notice(text="checking the host", urgency="progress").say()


def runtime_preflight(
    label: str,
    readiness: RuntimeReadiness,
    requirements: Manifest,
    root: Path,
    sentinels: LaunchSentinels,
    opening: LaunchOpening,
    contained: bool = True,
) -> list[Finding]:
    """Verify each claimed native requirement immediately before launch.

    Two rosters, asked in the order their failures matter. The native probes
    answer whether this runtime can host a session at all, and a gap there
    stops the launch. The declared requirements answer what the session will
    be able to *do*, and almost every gap there costs a capability rather
    than the session -- so those are named and the launch continues, which is
    the only posture that works on a machine without a display, a container,
    or an editor attached.

    A supported probe is not a line. Three of them answering with one version
    between them is one fact stated three times, and the fact is which
    runtime this opens -- so that is what the opening carries, and each
    capability is heard from only where it is missing, which is the answer
    the launch stops on.

    ``label`` names the runtime in what is said, ``readiness`` probes it, and
    ``requirements`` is the manifest the host roster exercises.
    """
    target = label
    evidence = readiness()
    for item in evidence:
        if item.supported:
            continue
        Notice(
            text=f"{target} {item.version}: required capability unavailable: {item.capability}",
            urgency="refusal",
        ).say()
    if any(not item.supported for item in evidence):
        raise LaunchRefused(
            f"Cannot launch {target}: required runtime checks failed. "
            "Run `uv run lup-devtools harness doctor` for details."
        )
    versions = ", ".join(sorted({item.version for item in evidence}))
    opening.runtime = f"{target} {versions}" if versions else target
    return report_requirements(
        requirements,
        root,
        sentinels=sentinels,
        in_passing=True,
        contained=contained,
    )


def report_requirements(
    manifest: Manifest,
    root: Path,
    setting_up: bool = False,
    sentinels: LaunchSentinels = LaunchSentinels(),
    in_passing: bool = False,
    contained: bool = False,
    standing: StandingGrants = StandingGrants(),
) -> list[Finding]:
    """Exercise the host-side requirements, printing what each one found.

    An absence is exactly the fact a session needs at the top of its
    scrollback, because it is the one the agent inside cannot discover except
    by failing at it.

    *in_passing* is a roster exercised on the way to something else, and
    there a capability that works is counted rather than named: the roster
    grows, the block of `working` grows with it, and the launch reports the
    same block every session. A caller that asked -- `harness requirements`,
    whose whole job is this answer -- hears every entry, because a command
    that prints nothing on a healthy machine has not answered the question.

    *setting_up* widens this to everything checked only at setup, and is off
    for a launch. Two different things live there and both would be wrong to
    repeat: a nicety reported before every session becomes a line people learn
    to skip, along with the line above it that mattered; and an exercise that
    starts a container is a cost no session should pay to be told something
    that was equally true yesterday.
    """
    # The host sentinel reaches the probe observing it in that exercise's own
    # environment, and never through this process's: the launcher is the
    # operator's, and a sentinel left in its environment would make everything
    # it runs afterwards read as a launched session.
    environ: EnvVars = {
        **os.environ,  # lup: ignore[os-environ]
        **sentinels.outside(),
    }
    # Pointed at this host's client here rather than declared as one, because
    # the declaration is hashed into the ownership digest and a container
    # client is a fact about the machine. This is the only place the
    # exercises actually run, so it is the only place that has to know.
    #
    # The machine's device grants join the roster here for the same reason
    # and from the same side: a grant is read off this machine's own file at
    # the moment the roster runs, so a committed manifest never names a
    # vendor's device, and a setup check still re-proves every grant.
    findings = for_host(
        Manifest(
            requirements=[
                *manifest.requirements,
                *(
                    granted_device_requirement(device)
                    for device in standing.devices(print)
                ),
            ]
        ),
        container_client(),
        root,
        inside_sentinel=sentinels.inside,
        host_sentinel=sentinels.host,
    ).check(environ, setting_up=setting_up, contained=contained)
    return reported(findings, in_passing)


def reported(findings: list[Finding], in_passing: bool = False) -> list[Finding]:
    """Say what each finding found, and stop where absence refuses.

    One place for both halves so the two rosters cannot come to differ about
    what a refusal means. Two rosters printed separately have somewhere to
    differ: either is free to treat a refused finding as a line rather than
    a stop.

    The refusal names the capabilities and stops there. Joining their whole
    consequences into the exception is unreadable at the size this roster
    reaches: four refusals make one nine-line paragraph inside an error box,
    restating word for word what has just been printed above it with the
    causes, the recoveries and the blank lines all flattened out. The
    lines above are the report; this is the exit code and what it was about.

    *in_passing* decides which half of a finding is read: everything it
    found, for a caller that asked, and only what a reader has to act on for
    a launch on its way to opening a session.
    """
    for finding in findings:
        for notice in finding.alarms() if in_passing else finding.notices():
            notice.say()
    stopping = refused(findings)
    if stopping:
        raise LaunchRefused(
            f"Cannot launch: {len(stopping)} required checks failed: "
            + ", ".join(item.requirement.capability for item in stopping)
            + ". See the errors above."
        )
    return findings


def verify_inside(
    manifest: Manifest,
    opening: list[str],
    root: Path,
    setting_up: bool = True,
    sentinels: LaunchSentinels = LaunchSentinels(),
    environment: EnvVars | None = None,
    in_passing: bool = False,
    skipped: Sequence[str] = (),
    accessible: Sequence[AccessibleRoot] = (),
    nested: Sequence[NestedRepository] = (),
    trees: Sequence[Path] = (),
) -> list[Finding]:
    """Exercise the image half behind an argv somebody already assembled.

    Split from :func:`report_inside_requirements` so the launch can use it
    without building the argv a second time. Assembling it starts the egress
    proxy and may build the image, so a second call would not merely be slow
    -- it would print the whole boundary notice again, which reads as the
    launch having done it twice.

    ``skipped`` is what another composition over the same image already
    exercised, by :meth:`Manifest.inside_signatures`.

    ``accessible`` is the roots the argv was assembled with, so the read-only
    binds a probe checks are the lease that argv carries -- taken after the
    argv, whose assembly readies each shared git directory the lease reads.
    """
    if environment is None:
        environ: EnvVars = dict(os.environ)  # lup: ignore[os-environ]
    else:
        environ = dict(environment)
    leased = held_lease(root, fleet_lease(root, list(accessible)), nested, trees)
    return reported(
        manifest.check_inside(
            environ,
            opening,
            setting_up,
            HostFacts(
                checkout=root,
                inside_sentinel=sentinels.inside,
                host_sentinel=sentinels.host,
                read_only_binds=list(leased.read_only),
            ),
            skipped,
        ),
        in_passing,
    )


def report_inside_requirements(
    image: Image,
    requirements: Manifest,
    root: Path,
    config_home: Path,
    login: ProviderLogin,
    sentinels: LaunchSentinels = LaunchSentinels(),
    setting_up: bool = True,
    skipped: Sequence[str] = (),
    banner: Banner | None = None,
    standing: StandingGrants = StandingGrants(),
) -> list[Finding]:
    """Exercise the image-side requirements inside the container a session opens.

    ``skipped`` and ``banner`` are for a caller asking two runtimes about one
    image: the checks the first already exercised are not paid for again, and
    the boundary notice the argv assembly says is collected into the banner
    rather than printed a second time. ``standing`` is what this machine
    grants every session, asked once and handed to both halves below.

    The half of the manifest that had nowhere to run. An image requirement is
    excluded from the host roster for a good reason -- a laptop without
    ``bun`` is not a laptop with a problem -- and excluded was as far as it
    went: declared, rendered into a package list, never exercised. What that
    bought was a preflight that reported a healthy machine and a session that
    could not resolve its own proxy, because everything the boundary is made
    of sat on the unexercised side.

    Behind the *same* argv a launch opens with, assembled by the same call.
    That is the whole design, and the alternative has already been measured
    wrong twice: an exercise spelled as its own ``run`` verified a container
    with no network, no mounts and no config home, and an exercise spelled
    with its own client verified an engine no session opens through. A probe
    that assembles its own container answers about that container.

    Non-interactive, which is the one deliberate difference. A probe's output
    is captured rather than shown, and ``-it`` against a pipe fails on the
    terminal it was promised.
    """
    credential = login.credentials_path(config_home)
    accessible = standing.roots(print)
    opening = contained_argv(
        image,
        requirements,
        root,
        editor_rendezvous(login),
        credential if credential.exists() else None,
        login,
        streams="captured",
        banner=banner,
        sentinels=sentinels,
        # The same mounts and devices a session gets, for the reason this
        # probe assembles nothing of its own: a container built without the
        # declared roots is a container no session opens, and a placement
        # verified in one says nothing about the other.
        accessible=accessible,
        devices=standing.devices(print),
    )
    # The same values on both sides of one call, which is the whole of what a
    # placement probe asks. Injected into the argv above and handed to the
    # roster below: aimed with one and opened with the other, the probe would
    # look for a value nothing had set and report the boundary broken on a
    # machine whose boundary was fine.
    return verify_inside(
        requirements,
        opening,
        root,
        setting_up=setting_up,
        sentinels=sentinels,
        skipped=skipped,
        accessible=accessible,
    )


def personal_config(config: UserConfigFile) -> UserConfig:
    """The person's lup config, or a refusal naming the file to fix.

    Refused rather than passed over, because a launch that dropped a setting
    it could not read would open looking exactly like one that never had it.
    """
    try:
        return config.load()
    except ValueError as refusal:
        raise LaunchRefused(str(refusal)) from refusal


def ambient_config_home(login: ProviderLogin, fallback: Path | None = None) -> Path:
    """The configuration home a launch would inherit, made concrete for a mount.

    ``launch_home`` answers ``None`` for "inherit whatever the environment
    selected", which is the right answer everywhere it is read -- except at a
    mount, which names a file rather than a policy. Resolving it here lets
    that ``None`` keep meaning what it means everywhere else instead of every
    caller inventing a default.

    ``fallback`` is what answers where the environment selected nothing, and
    it is a caller's because it is not always the provider's own default: a
    Codex composition falls back to the *worktree-scoped* home its store
    derives, which is a home per checkout rather than the account's. Omitting
    it takes the runtime's declared default, which is the right answer for a
    caller that has no home of its own in mind.
    """
    # lup: ignore[os-environ] — the process environment is the open
    # mapping this reads by definition, and absence is the answer it wants
    selected = login.selected_home(dict(os.environ))
    return selected if fallback is None or selected != login.ambient_home else fallback


def editor_rendezvous(login: ProviderLogin) -> Path | None:
    """Where an editor on this machine would leave a lockfile for this runtime.

    Read off the environment this launcher runs in rather than the home the
    launch selected, because the editor is a *sibling* process: it reads the
    same variable and knows nothing about ``--profile``. ``None`` where the
    runtime declares no rendezvous, which is every runtime but Claude Code.
    """
    # lup: ignore[os-environ] — the same open mapping the editor itself reads
    return login.editor_rendezvous(dict(os.environ))


def settle_boundary(
    root: Path,
    hooks: HookSet | None,
    sandbox: LaunchSandbox,
    findings: list[Finding],
    sentinels: LaunchSentinels,
    environment: EnvVars,
    banner: Banner,
    accessible: list[AccessibleRoot] = [],
    runtime: str = "",
    nested: Sequence[NestedRepository] = (),
    trees: Sequence[Path] = (),
) -> BoundaryPreflight:
    """Compile what this launch promised, measure it, and refuse if it fell short.

    The gate, and the reason every other part of this exists. A profile
    requiring a capability nothing delivered does not open, and the diagnostic
    names the capability and what was tried -- because "the sandbox is broken"
    sends somebody to read configuration, where the exercise's own words send
    them to the thing that failed.

    An optional capability that came back absent is not a refusal and not a
    question. The operations that need it are capability-blocked, which is
    what sends an agent to the missing channel rather than to argue with a
    rule, and the ledger is how the dispatcher learns which those are.

    The lease is read here rather than carried from the argv builder because
    an uncontained launch never builds one and still has a boundary to
    describe: the same worktree, the same siblings, and no container under
    them. One call answers for both postures, which is what stops the two
    from coming to disagree about what this session may write.

    ``accessible`` arrives as an argument rather than being read here, which
    is the one part of the lease that cannot be recomputed freely: resolving
    a registration can clone it, so a launch settles the list once and hands
    the same one to the boundary and to the argv. Passed empty, this answers
    for the checkout alone -- which is what a caller with no registry to read
    should get, rather than a boundary that quietly went looking for one.

    A refusal is said here and everything else is held. The banner is printed
    after the last measurement, and a launch that raises never reaches it --
    so the one report a reader cannot afford to lose is the one that cannot
    wait for it.

    ``root`` is the checkout the boundary is measured for, handed in rather
    than read from wherever this process happens to stand: a declaration
    opened on another checkout measures that one.
    """
    declared = hooks or HookSet(id="hooks.absent", policy_ids=[])
    # Before the lease asks git anything about these roots, on either posture:
    # a root whose pointer its repository does not list back is refused here
    # rather than mounted and read through, and each repository vouching for
    # a root is remembered before any container runs in it.
    trust = judged_roots([root, *(item.path for item in accessible)], operator=root)
    for notice in trust.notices:
        print(notice, file=sys.stderr)
    if trust.refusal:
        raise LaunchRefused(trust.refusal)
    # A container holds its launch record read-only, so the boundary its
    # ledger describes carries the holds the policy then refuses writes to.
    leased = fleet_lease(root, accessible=accessible)
    lease = held_lease(root, leased, nested, trees) if sandbox.contained() else leased
    if exposed := launcher_state_exposure(lease):
        raise LaunchRefused(exposed)
    boundary = compile_boundary(
        declared,
        contained=sandbox.contained(),
        writable=list(lease.writable),
    )
    preflight = measured(boundary, depended_on(declared, sandbox.contained()), findings)
    said = preflight.opening()
    if not preflight.launchable():
        Notice(text=said, urgency="boundary").say()
        raise LaunchRefused(
            f"{boundary.name}: "
            + ", ".join(entry.capability for entry in preflight.missing_required())
            + " could not be verified. Launch stopped. See the failed checks above."
        )
    if said:
        banner.add([Notice(text=said, urgency="boundary")])
    try:
        record_preflight(
            preflight,
            sentinels,
            root,
            destination_policies=accept_destination_policies(
                root, accessible, lease, runtime
            ),
            read_only_roots=list(lease.read_only),
            destination_authorities=destination_authorities(accessible, runtime),
        )
    except OSError as refused:
        # Where a launch is opened from inside a container, whose own
        # launch record that container holds read-only.
        raise LaunchRefused(
            f"This launch cannot write its launch record under {root / '.lup'}: "
            f"{refused.strerror}. A container holds its session's record "
            "read-only, so a session is launched from the host."
        ) from refused
    if not sandbox.contained():
        # No mounts, so no mount table -- and the one a contained launch left
        # behind describes a boundary this session is not behind. Attributing
        # a refusal to it teaches an agent to reach for the host when the bug
        # was its own, which outlives the command it was wrong about.
        retire_mount_table(root)
    environment.update(
        sentinels.within() if sandbox.contained() else sentinels.outside()
    )
    environment[ROOT_VARIABLE] = str(root.resolve())
    return preflight


def session_argv(
    cli: str,
    arguments: list[str],
    root: Path,
    image: Image,
    requirements: Manifest,
    hooks: HookSet | None,
    config_home: Path,
    login: ProviderLogin,
    sandbox: LaunchSandbox,
    environment: EnvVars,
    transcript: Path | None = None,
    sentinels: LaunchSentinels = LaunchSentinels(),
    cleared: LaunchOpening = LaunchOpening(),
    mounts: list[AccessibleRoot] = [],
    devices: list[Device] = [],
    authenticate: Callable[[list[str], Path, bool], None] | None = None,
    member: LaunchedMember | None = None,
    prepare: Callable[[list[str], Path], Mapping[Path, str]] | None = None,
    home_seed: HomeSeedPlaces | None = None,
    clipboard: ClipboardTransport = "commands",
    forwarded: Sequence[str] = (),
    privileges: SessionPrivileges = SessionPrivileges(),
    nested: Sequence[NestedRepository] = (),
    memory: MemoryLimit | None = None,
    trees: Sequence[Path] = (),
    overlays: Mapping[Path, str] | None = None,
) -> list[str]:
    """The argv that opens a session, inside the declared container or on the host.

    One place decides this for both runtimes, because "contained unless the
    operator said otherwise, or the host has no engine to contain it" is a
    property of the launch rather than of the CLI being launched -- and a
    second runtime that decided it separately is how one of them ends up
    quietly uncontained. ``sandbox`` arrives settled, by
    :func:`settled_sandbox`, so the fallback is said before this runs.

    It is also the one place that knows the whole opening: what the container
    had to say about itself, and whether the checks behind it passed. So the
    banner is said here, after the verification rather than before it -- a
    launch cannot report itself ready while the thing that would refute it
    has not run yet, and thirty lines printed ahead of the answer is how the
    refutation ends up below the fold. Both postures say it. A posture that
    says nothing leaves a transcript path in its place, printed on the way
    past by whatever opened the file.

    And it is where the boundary is settled, for the same reason: this is the
    one place that knows which posture the launch took, so it is the only
    place a boundary can be compiled that answers for the session actually
    about to open. Both postures write a ledger. A launch that wrote nothing
    would leave whatever a contained launch wrote last standing as this
    session's answer -- a boundary belonging to a session that has already
    ended.

    ``mounts`` and ``devices`` are every folder and device the session is
    granted, resolved once by the caller: resolving a registration can clone
    it, so a second resolution would be a second trip to the forge, and a
    boundary compiled against one set of roots beside mounts built from
    another.

    ``forwarded`` names what else of ``environment`` a contained session is
    handed -- what the host companions held around it export -- carried by
    name into its container, as the launch's own variables are.

    ``privileges`` is what the wall grants a contained session's processes,
    which a host posture has no container to grant, ``nested`` the
    repositories inside the checkout its container holds, ``memory`` how
    much its container may hold, ``trees`` the generated trees it holds
    read-only, which the boundary it records names as it names every hold,
    and ``overlays`` the files it holds over a path in the checkout -- a
    kind of session's own guidance -- keyed by where each is on the host,
    each path they cover held and recorded as a tree is.

    ``prepare`` readies the runtime's home through the argv the session
    opens with, and answers with what the session should find held
    read-only in it, by host path and the path inside; a host posture's
    home has no container to hold anything in, and its answer is not read.

    What it reads of the declaration arrives piece by piece -- the checkout
    it opens in, the image and its manifest, the policy, the clipboard's way
    in -- rather than as a generated harness, so a declaration whose plugin
    is a built directory, opened on any checkout, opens a session the same
    way as one this library compiled for this one.
    """
    banner = cleared.banner
    # Minted where both runtimes pass through, so a session's coordination
    # identity is a fact about having been launched rather than about which
    # CLI was launched. Exported rather than derived because the session's
    # tool server and its hooks are separate processes with no channel
    # between them, and an id each worked out for itself would put one
    # session on the roster twice. A launcher that already minted one, to
    # show its name in the runtime's own chrome, hands it in so the chrome
    # and the roster agree.
    #
    # Overwritten rather than respected. The variables are a launcher's claim
    # to have minted what is behind them, and an operator who happened to
    # have them exported would otherwise hand their own roster address to
    # every session they start — two peers answering to one id, which is the
    # one thing the durable id exists to rule out.
    environment.update((member or launched_member(root)).environment())
    environment[POLICY_ROOT_ENV] = str(root)

    accessible = list(mounts)
    # A file something is held over is held all the same, and the boundary
    # this launch records names it as it names every hold, so the policy and
    # `harness binds` know it; what is bound there is the overlay.
    held_trees = list(
        dict.fromkeys([*trees, *(Path(inside) for inside in (overlays or {}).values())])
    )
    if not sandbox.contained():
        # A host posture holds the host's devices already, so a flag asking
        # for one describes a container this launch does not open. Said
        # rather than ignored: the operator typed it expecting a grant, and
        # silence would leave them reading a GPU that answers on the host as
        # one the flag delivered.
        if devices:
            banner.add(
                [
                    Notice(
                        text=(
                            "Devices: "
                            + ", ".join(device.name for device in devices)
                            + " asked for; the session runs on the host, which "
                            "holds its own devices, and --device grants one "
                            "inside the container."
                        ),
                        urgency="detail",
                    )
                ]
            )
        if prepare is not None:
            prepare([], config_home)
        if authenticate is not None:
            authenticate([cli], config_home, False)
        settle_boundary(
            root,
            hooks,
            sandbox,
            cleared.findings,
            sentinels,
            environment,
            banner,
            accessible,
            runtime=cli,
        )
        say_opening(cleared, cleared.findings, transcript)
        return [cli, *arguments]
    # A token crosses by name, so its value has to be in the environment of the
    # process that starts the container rather than anywhere in the argv. That
    # makes this the one place it has to be resolved: the argv builder reads
    # the same declaration for whether to pass the name, and a name passed
    # against an environment nobody populated forwards nothing.
    carried = image.forge.sourced(environment)
    if carried:
        environment[image.forge.token_variable] = carried
    credential = login.credentials_path(config_home)
    opening = contained_argv(
        image,
        requirements,
        root,
        editor_rendezvous(login),
        credential if credential.exists() else None,
        login,
        inherited_environment=[
            # By name, so the value crosses out of this process's environment
            # rather than through an argv every process on the host can read.
            # The member id is not a secret, but a session whose id reached it
            # by a second route would be a session two mechanisms could
            # disagree about.
            *(
                [MAX_RECURSIVE_AGENT_ENV]
                if MAX_RECURSIVE_AGENT_ENV in environment
                else []
            ),
            MEMBER_ENV,
            NAME_ENV,
            POLICY_ROOT_ENV,
            *forwarded,
        ],
        banner=banner,
        sentinels=sentinels,
        accessible=accessible,
        devices=devices,
        home_seed=home_seed,
        privileges=privileges,
        nested=nested,
        memory=memory,
        trees=held_trees,
        overlays=overlays,
    )
    # Verified on the way in, rather than asserted. This is §6's whole point
    # and the launch is where it has to happen: the boundary was built two
    # lines ago and nothing had ever asked whether it carries traffic. What
    # that cost, measured on the first contained session anybody opened, was
    # a session that started cleanly, looked entirely healthy, and reported
    # every request as the operator's own internet or DNS being down.
    #
    # Not the whole image roster -- only the entries marked `always`, which
    # is the handful whose absence means the session can do nothing. A model
    # call and a toolchain version belong to `harness requirements --inside`.
    inside = verify_inside(
        requirements,
        probing(opening),
        root,
        setting_up=False,
        sentinels=sentinels,
        environment=environment,
        in_passing=True,
        accessible=accessible,
        nested=nested,
        trees=held_trees,
    )
    if prepare is not None:
        # What preparing the home installed and asks to be held -- a
        # runtime's hooks, in a home the session writes -- is mounted
        # read-only for the session, nested in the home's own volume.
        held = prepare(probing(opening, stdin=True), Path(image.config_home))
        if held:
            volume = state_volume_name(root, login)
            opening = image.home_mounts(opening, volume, held)
    # A contained session sharing host loopback can receive a browser callback.
    # Device login is needed where that callback stays outside its namespace.
    if authenticate is not None:
        authenticate(
            [*probing(opening, stdin=True), cli],
            Path(image.config_home),
            not image.egress.shares_host_loopback(),
        )
    # Both halves of one measurement, joined here because this is where the
    # second is taken. The host roster answered for the relay and the store
    # before the container existed; the inside roster answered for the
    # placement behind the argv this session opens with. A preflight built
    # from either alone would report a capability nothing asked about.
    measured_here = [*cleared.findings, *inside]
    settle_boundary(
        root,
        hooks,
        sandbox,
        measured_here,
        sentinels,
        environment,
        banner,
        accessible,
        runtime=cli,
        nested=nested,
        trees=held_trees,
    )
    say_opening(cleared, measured_here, transcript)
    native = image.clipboard.wrap([cli, *arguments], clipboard)
    return [*opening, *native]


def say_opening(
    cleared: LaunchOpening, findings: list[Finding], transcript: Path | None
) -> None:
    """Say everything this launch held, once, in the order a reader wants it.

    The count is what replaces the roster. A reader who wants to know *which*
    checks passed is asking a question `harness requirements` answers on
    demand and a launch cannot answer usefully anyway -- the list is the same
    list as yesterday, every session, and the one time it differs is the one
    time a line is printed for it.
    """
    passed = sum(1 for finding in findings if finding.working)
    named = f"{cleared.runtime}: " if cleared.runtime else ""
    cleared.banner.add(
        [
            Notice(
                text=f"{named}artifacts current, {passed} checks passed",
                urgency="ready",
            ),
            *(
                [Notice(text=f"Transcript: {transcript}", urgency="artifact")]
                if transcript is not None
                else []
            ),
        ]
    )
    cleared.banner.say()


def probing(opening: list[str], *, stdin: bool = False) -> list[str]:
    """The session's own argv, with the interactive terminal taken back off.

    The same argv rather than a fresh one, because a probe assembled
    separately verifies a container no session opens -- an exercise that
    passes on a host whose sessions cannot start. The one difference is
    deliberate: a probe's output is captured, and ``-it`` against a pipe
    fails on the terminal it was promised.
    """
    return [
        "-i" if word == "-it" else word for word in opening if stdin or word != "-it"
    ]


def placed_wake_socket(
    sockets: WakeSockets, root: Path, member: LaunchedMember
) -> str | None:
    """Where this session binds the wake socket a peer nudges it through, if anywhere.

    Named by the launcher rather than left to the runtime, whose own default
    is a directory a container does not share and a file named after a pid its
    namespace assigns -- so two sessions in sibling containers name one path
    and neither can reach the other. A directory that could not be made
    answers nothing, which is a peer that waits for its mail rather than a
    launch that fails.

    Keyed by the member's id, so a file already at the path is this member's
    own, left by an earlier run of it: it is replaced without asking, since
    nothing else was ever keyed there. The departed members of this
    repository are asked about while the roster is in hand -- a socket the
    roster's departed left behind is removed where nothing answers on it,
    which :meth:`WakeSockets.retire` settles.
    """
    if sockets.serve() is None:
        return None
    repository = shared_git_directory(root)
    for row in RepositoryPeers(root).present():
        if not row.running:
            sockets.retire(repository, row.actor.id, row.wake.handle)
    address = sockets.socket(repository, member.member_id)
    Path(address).unlink(missing_ok=True)
    return address
