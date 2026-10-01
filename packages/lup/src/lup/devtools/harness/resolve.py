"""Resolver command glue between the CLI and the shared persisted resolver.

Owns the console question broker, resolver-scoped Git snapshotting of
review-note files, the per-adapter worker and reviewer session factories,
and the driver that starts a persisted resolver run, resumes it, and widens
it with work discovered while it ran.
"""

import asyncio
from functools import partial
from collections.abc import AsyncGenerator, Iterator
from contextlib import asynccontextmanager
import sys
from pathlib import Path

import sh
import typer
from pydantic import BaseModel

from lup.harness.codescan.markers import find_feedback
from lup.workspace.checkout_state import CheckoutState
from lup.harness.enforcement import semantic_policy_for
from lup.harness.models import HookSet
from lup.policy.hooks import LupHooksConfig
from lup.policy.enforcement import (
    EscalationRelay,
    NativeSemantics,
    SandboxPosture,
    create_policy_hooks,
)
from lup.tools.mcp import (
    RawStdioServerConfig,
    create_mcp_server,
    serve_stdio,
)
from lup.mcp import External
from lup.policy.grants import LeaseGrants, allowance_grants_environment
from lup.policy.identity import agent_identity_environment
from lup.harness.environment import inherited, non_interactive_environment
from lup.harness.ownership import GeneratedArtifacts, generated_artifacts
from lup.harness.process import LaunchRequest, LocalProcessLauncher, ProcessLauncher
from lup.sandbox.checked import PointerCheckedLauncher
from lup.resolver.contracts import (
    ResolverAssemblyDeferred,
    ResolverAwaitingAnswers,
    ResolverDrained,
    ResolverEnvironmentFault,
    ResolverRegression,
    ResolverObserver,
    WorktreePreparer,
)
from lup.resolver.core import ASSEMBLY_QUESTION_ID, ResolverCore
from lup.resolver.record import Journal
from lup.resolver.lifecycle import HostWait, drive_with_host_retries
from lup.resolver.orchestrator import WorktreeOrchestrator
from lup.resolver.rebase import BaseRefresher
from lup.resolver.run import ResolveRun
from lup.resolver.state import ResolverStateRepository, StateTransitionError
from lup.resolver.admissions import AdmissionMailbox
from lup.resolver.status import tally_bar, unfinished_runs
from lup.resolver.models import (
    AdmissionRequest,
    Concern,
    ConcernAdmission,
    ConcernProgress,
    InventoryNote,
    IssueEvidence,
    LeaseRefresh,
    ResolveManifest,
    MaterialQuestion,
    RefreshReport,
    ResolvePhase,
    ResolveRequest,
    ResolverConfig,
    ReviewerContext,
    RunTally,
    SourceSnapshot,
    VerificationCommand,
    WorkerContext,
)
from lup.channels.models import local_stamp, utc_now
from lup.coordination.mailbox import (
    AnswerDoor,
    AnswerOffer,
    MailboxConflictError,
)
from lup.resolver.mailbox import QuestionMailbox
from lup.resolver.tools import (
    RESOLVER_CONCERN_ENV,
    RESOLVER_RUN_DIR_ENV,
    ResolverToolContext,
    create_question_tools,
    read_resolver_tool_context,
)
from lup.resolver.join_tools import create_join_tools
from lup.sessions.surface import Agent
from lup.providers.profiles import SessionAccount
from lup.types import EnvVars
from lup.workspace.paths import project_root
from lup.devtools.dev.branches import probe_base_freshness, require_fresh_base
from lup.devtools.dev.comments import FoundComment, scan_tracked
from lup.devtools.dev.issues import comment_on_issues, fetch_open_issues
from lup.devtools.dev.remote_auth import check_remote_auth
from lup.devtools.dev.worktree import (
    copy_gitignored_extras,
    sync_dependencies,
)
from lup.launch.container import (
    engine_absence,
    worker_cli,
    worker_wrapper_path,
)
from lup.harness.generate import NativeHarnessComposition
from lup.devtools.sync import accessible_roots, granted_devices
from lup.providers.claude.login import CLAUDE_LOGIN
from lup.providers.codex.login import CODEX_LOGIN
from lup.devtools.supervisor.page import SUPERVISOR_PORT
from lup.devtools.dev.worktree import report_a_blocked_registration
from lup.devtools.supervisor.projection import answer_recipe as rerun_recipe
from lup.devtools.supervisor.projection import PendingQuestionView, question_views


class ConfiguredModel(BaseModel, frozen=True):
    """The model an application is configured to run, and where it routes.

    A resolver session runs through one native adapter, and a model reaches
    only the backend its vendor prefix names. Naming both here lets the
    driver say which one it declined rather than silently taking a default.
    """

    name: str
    adapter: str

    def reaching(self, adapter: str) -> str | None:
        """This model when that adapter can run it, nothing when it cannot."""
        return self.name if self.adapter == adapter else None


class LocatedNote(BaseModel, frozen=True):
    """Where one scanned note sits, spelled the way a reader opens it."""

    file: str
    start_line: int
    end_line: int

    def location(self) -> str:
        return f"{self.file}:{self.start_line}-{self.end_line}"


class CarriedNote(LocatedNote, frozen=True):
    """One parked note the resolver leaves alone, with the gate it stated."""

    label: str

    def describe(self) -> str:
        return f"carrying {self.label} {self.location()}"


class GeneratedNote(LocatedNote, frozen=True):
    """One note a generated artifact holds, named by the id that owns it."""

    semantic_id: str

    def describe(self) -> str:
        return f"leaving to its generator: {self.semantic_id} owns {self.location()}"


class ResolverIntake(BaseModel):
    """The scan partitioned at the resolver boundary.

    Deferred notes never enter the resolver inventory — waking one is an
    explicit edit that removes its `defer` head — so an editor can never be
    assigned parked work. ``carried`` reports each parked note, and
    ``generated`` each note a generator answers for rather than this tree.

    Each bucket the resolver only reports on carries the note rather than a
    line about it, so the run's own report and a preview of what a run would
    plan render from one declaration instead of agreeing by hand. What it
    plans from stays the scanned comment, because that is what becomes
    evidence and it carries the read context a planner needs.
    """

    actionable: list[FoundComment]
    carried: list[CarriedNote]
    generated: list[GeneratedNote]

    def describe(self) -> list[str]:
        """Render this scan the way somebody deciding whether to start a run reads it.

        Every note is named at its own site rather than only counted, because
        the question this answers — whether the run about to be started is the
        one worth having — turns on which notes it would plan from and which it
        would leave alone, not on how many there are of each.

        Each is named and not quoted: what the notes say is one `dev comments`
        away, and reprinting fifty of them buries the partition this exists to
        show under the listing that already exists.
        """
        return [
            f"{len(self.actionable)} to plan, {len(self.carried)} carried, "
            f"{len(self.generated)} left to a generator",
            *(
                f"planning from {note.file}:{note.start_line}-{note.end_line}"
                for note in self.actionable
            ),
            *(note.describe() for note in self.carried),
            *(note.describe() for note in self.generated),
        ]


def resolver_intake(
    comments: list[FoundComment], owned: GeneratedArtifacts
) -> ResolverIntake:
    """Partition scanned notes into resolver work, deferrals, and generated ones.

    A note inside a generated artifact was written against the generator's own
    source and copied here when the harness materialized. This tree can neither
    resolve it nor park it: editing the artifact is refused, and the next
    generation restores whatever an edit changed. Assigning one spends a concern
    that can only fail, so it is reported and left to whoever owns the source.
    """
    notes = [comment for comment in comments if comment.kind == "note"]

    def generated() -> Iterator[GeneratedNote]:
        """Each note a generated artifact holds, named by the artifact."""
        for comment in notes:
            artifact = owned.owning(comment.file)
            if artifact is not None:
                yield GeneratedNote(
                    file=comment.file,
                    start_line=comment.start_line,
                    end_line=comment.end_line,
                    semantic_id=artifact.semantic_id,
                )

    return ResolverIntake(
        actionable=[note for note in notes if owned.owning(note.file) is None],
        generated=list(generated()),
        carried=[
            CarriedNote(
                file=comment.file,
                start_line=comment.start_line,
                end_line=comment.end_line,
                label=comment.deferral_label(),
            )
            for comment in comments
            if comment.kind == "defer"
        ],
    )


def scanned_intake(root: Path) -> ResolverIntake:
    """Partition the tree's notes the way a run does, without starting one.

    A preview and a run reach the scan through here, so what a preview shows
    and what a run plans from cannot come apart: there is one partitioning
    and both read it.

    The scan is the tracked files of the working directory, which is what
    ``scan_tracked`` reads; ``root`` names only the tree whose ownership proof
    decides which of those a generator owns. Both readers pass the project
    root, so the two halves agree for either of them.
    """
    return resolver_intake(scan_tracked(find_feedback), generated_artifacts(root))


def preview_intake() -> None:
    """Print what a run started now would plan from, without starting one.

    Every other resolve subcommand operates on a run that already exists, so
    reading an inventory meant committing to one — and a run leases a worktree
    per concern. Reading the tree creates nothing, so the decision to start
    can come after the evidence rather than before it.

    Notes only. A run also takes the project's open issues unless it is
    started with `--no-issues`, and `dev issues` prints exactly those.
    """
    for line in scanned_intake(project_root()).describe():
        typer.echo(line)


def lease_plugin_dir(root: Path, plugin_name: str) -> Path:
    """The plugin a session opened in *root* is judged by: that tree's own.

    An interactive launch names its directory with `--plugin-dir` and is
    immune to this; a session the SDK opens names nothing, so it resolves
    plugins through the settings at its working directory. Those settings
    register a marketplace under a name, and a name is one global namespace
    shared by every checkout declaring it — so the plugin a lease actually
    loaded was whichever tree registered that name last, and a worker was
    refused an edit by a policy kernel generated from another commit.
    """
    return root / ".claude" / "plugins" / plugin_name


class FeatureWorktreePreparer(WorktreePreparer):
    """Prepare leased resolver worktrees exactly like feature worktrees.

    Copies the same gitignored extras and runs the same dependency sync as
    ``git worktree create``, so verification and tests inside a lease bind
    to the leased checkout instead of the source tree's environment.
    """

    def __init__(self, source_root: Path) -> None:
        self.source_root = source_root

    def prepare(self, root: Path) -> None:
        copy_gitignored_extras(self.source_root, root)
        sync_dependencies(root)


class ConsoleResolverObserver(ResolverObserver):
    """Print one line per durably recorded resolver transition.

    Long worker phases are otherwise silent; these lines are the liveness
    signal that lets an operator stop polling state files and worktrees.
    """

    def phase_changed(self, phase: ResolvePhase) -> None:
        typer.echo(f"[resolve] phase: {phase}")

    def concern_changed(self, progress: ConcernProgress) -> None:
        line = f"[resolve] {progress.concern_id}: {progress.status}"
        if progress.reason:
            line = f"{line} ({progress.reason})"
        typer.echo(line)

    def tally_changed(self, tally: RunTally) -> None:
        bar = tally_bar(tally)
        reported = [
            *([f"{bar.label} {bar.render()}"] if bar is not None else []),
            tally.concerns_line(include_joins=bar is None),
        ]
        typer.echo(f"[resolve] progress: {' · '.join(reported)}")


def parse_answer_flags(
    flags: list[str],
) -> dict[str, str]:  # lup: ignore[dict-str-payload] — open question-id map
    """Split repeatable ``--answer`` flags into a question-id to value map."""
    pairs = [
        flag.split("=", 1)  # lup: ignore[string-split] — this CLI's flag grammar
        for flag in flags
    ]
    malformed = [
        flag
        for flag, pair in zip(flags, pairs, strict=True)
        if len(pair) != 2 or not pair[0]
    ]
    if malformed:
        raise typer.BadParameter(
            "--answer takes <question-id>=<value>; got: " + ", ".join(malformed)
        )
    identifiers = [pair[0] for pair in pairs]
    if len(identifiers) != len(dict.fromkeys(identifiers)):
        raise typer.BadParameter("--answer question ids must be unique")
    return {pair[0]: pair[1] for pair in pairs}


class NoteTargetRef(BaseModel, frozen=True):
    """One `file:line` target naming a note already written in the tree."""

    file: Path
    line: int


def parse_note_targets(targets: list[str]) -> list[NoteTargetRef]:
    """Split repeatable ``--admit-note`` flags into located note targets."""
    parsed = [
        target.rpartition(":")  # lup: ignore[string-split] — this CLI's flag grammar
        for target in targets
    ]
    malformed = [
        target
        for target, pair in zip(targets, parsed, strict=True)
        if not pair[0] or not pair[2].isdigit()
    ]
    if malformed:
        raise typer.BadParameter(
            "--admit-note takes <file>:<line>; got: " + ", ".join(malformed)
        )
    return [NoteTargetRef(file=Path(pair[0]), line=int(pair[2])) for pair in parsed]


def admission_notes(
    targets: list[NoteTargetRef], actionable: list[FoundComment]
) -> list[InventoryNote]:
    """Locate each named note among the notes a scan found actionable.

    The note's own text and surrounding context are carried rather than
    retyped, so a concern admitted from a note is grounded in exactly what
    intake would have planned from. Deferred notes never reach ``actionable``,
    so a target landing on parked work is refused with the rest.
    """
    located = {
        f"{comment.file}:{comment.start_line}": comment for comment in actionable
    }
    missing = [
        f"{target.file}:{target.line}"
        for target in targets
        if f"{target.file}:{target.line}" not in located
    ]
    if missing:
        raise typer.BadParameter(
            "no actionable `# lup:` note at: " + ", ".join(missing)
        )
    return [
        InventoryNote(
            file=target.file,
            line=located[f"{target.file}:{target.line}"].start_line,
            text=located[f"{target.file}:{target.line}"].marker_text(),
            context=located[f"{target.file}:{target.line}"].context,
        )
        for target in targets
    ]


def offer_flag_answers(
    mailbox: QuestionMailbox,
    run_id: str,
    provided: dict[str, str],  # lup: ignore[dict-str-payload] — open id map
) -> None:
    """Offer every ``--answer`` value through the mailbox.

    Offers may precede their questions, so a flag answers a question this
    run has not asked yet — which is why a fresh run need not park once
    before its answers can count.

    Every flag is put to the mailbox before any refusal is raised, so one
    rerun is told about all of its stale corrections rather than finding the
    next one only after it has dropped the last.
    """
    refused: list[str] = []
    for identifier, value in provided.items():
        try:
            mailbox.offer(
                AnswerOffer(
                    run_id=run_id,
                    question_id=identifier,
                    value=value,
                    door=AnswerDoor.FLAG,
                    offered_at=utc_now(),
                )
            )
        except MailboxConflictError as error:
            refused.append(str(error))
    if refused:
        raise typer.BadParameter(
            "\n  ".join(["", *refused])
            + "\nDrop those --answer flags to resume on the settled values, or "
            "end this run with --abort and start one answerable afresh."
        )


def run_owned(workspace: Path, root: Path, worktree_root: Path) -> bool:
    """Whether one run's own invocation already covers a workspace.

    Pointing a run at a repository is an explicit act of trust by whoever
    ran it, and a more deliberate one than accepting a dialog — so trust
    reaches that repository, where the planner reads, and the checkouts the
    run makes of it, where every worker and reviewer works. Those all land
    under ``worktree_root``, which is what keeps "a checkout this run
    created" a structural test rather than a judgement, and what stops trust
    from reaching anywhere a session merely happens to be opened.
    """
    return workspace.resolve() == root.resolve() or workspace.is_relative_to(
        worktree_root
    )


def inert_offers(mailbox: QuestionMailbox) -> list[str]:
    """Every offer left on disk that a promoted answer has already outrun.

    Nothing under ``.lup/resolve`` is ever unlinked, so an offer written
    before a promoter took a different value stays there reading like a
    pending correction while being none. A run that says what it found
    cannot be mistaken for one holding the newer value.
    """
    settled = {
        record.answer.question_id: record.answer.value for record in mailbox.answers()
    }
    return [
        f"offer {offer.question_id}={offer.value!r} through the {offer.door} door "
        f"never took: the question settled as {settled[offer.question_id]!r}"
        for offer in mailbox.offers()
        if offer.question_id in settled and settled[offer.question_id] != offer.value
    ]


def run_resolver_tool_server() -> None:
    """Serve the question tools to a worker whose tools run out of process.

    The Codex runtime spawns MCP servers as subprocesses, so a handler there
    cannot see any in-process object. It rebuilds the same mailbox from the
    relayed run directory instead, which is why the mailbox is files.
    """
    context = read_resolver_tool_context()
    if context is None:
        raise typer.BadParameter(
            f"{RESOLVER_RUN_DIR_ENV} and {RESOLVER_CONCERN_ENV} must both be set"
        )
    serve_stdio(
        create_mcp_server(
            "resolver",
            tools=[
                *create_question_tools(
                    QuestionMailbox(context.run_dir),
                    context.concern_id,
                    run_id=context.run_dir.name,
                    lease_root=context.lease_root,
                ),
                *(
                    create_join_tools(
                        context.run_dir, context.lease_root, context.concern_id
                    )
                    if context.actor_kind == "merger"
                    else []
                ),
            ],
        )
    )


SUPERVISED_WAIT_SECONDS = 3600.0
"""The shipped floor ``SupervisorSpawn`` takes as its field default below,
which is where a caller replaces it."""


class SupervisorSpawn(BaseModel, frozen=True):
    """Whether a run opens a page beside itself, and on which port."""

    enabled: bool = False
    port: int = SUPERVISOR_PORT
    linger: bool = False

    wait_floor: float = SUPERVISED_WAIT_SECONDS
    """The shortest wait a supervised run takes, whatever it was asked for: a
    page nobody is watching yet is the case the wait exists for."""

    def arguments(self) -> list[str]:
        """These settings again, for a relaunch that must open the same page."""
        if not self.enabled:
            return []
        return [
            "--supervise",
            "--supervise-port",
            str(self.port),
            *(["--supervise-linger"] if self.linger else []),
        ]

    def waiting(self, asked: float) -> float:
        """How long this run waits for an answer, given what was asked."""
        return max(asked, self.wait_floor) if self.enabled else asked


@asynccontextmanager
async def spawned_supervisor(
    spawn: SupervisorSpawn, run_id: str, adapter: str
) -> AsyncGenerator[None]:
    """Run the supervisor page beside this run, as a separate process.

    The page is an ordinary reader of the run directory, so it does not
    have to share this process — which is what removes the whole thread
    split the in-process host needed. The run's own loop hosts nothing.
    """
    if not spawn.enabled:
        yield
        return
    process = await asyncio.create_subprocess_exec(
        "uv",
        "run",
        "lup-devtools",
        "resolve",
        "supervise",
        "--run-id",
        run_id,
        "--adapter",
        adapter,
        "--port",
        str(spawn.port),
    )
    typer.echo(f"Resolver supervisor: http://127.0.0.1:{spawn.port}")
    try:
        yield
    finally:
        if spawn.linger:
            typer.echo("Supervisor left running; stop it with Ctrl-C in its terminal.")
        else:
            process.terminate()
            await process.wait()


def report_concern_evidence(concern: Concern) -> None:
    """Print what a concern was planned from, so its questions can be judged.

    A question prompt alone reads as a decision with no stakes: whoever
    answers it needs the `# lup:` notes that raised it and the spec the
    planner wrote from them, or they are guessing on the asker's behalf.
    """
    typer.echo(f"concern {concern.id}: {concern.title}")
    for note in concern.notes:
        typer.echo(f"  note {note.file}:{note.line}: {note.text}")
    for criterion in concern.criteria:
        typer.echo(f"  criterion {criterion.id}: {criterion.description}")
    for path in concern.files:
        typer.echo(f"  starting file: {path}")
    typer.echo(f"  spec: {concern.spec}")


def report_questions(
    questions: list[MaterialQuestion], concerns: list[Concern]
) -> None:
    """Print each open question under the concern evidence that raised it."""
    evidence = {concern.id: concern for concern in concerns}
    for concern_id in dict.fromkeys(question.concern_id for question in questions):
        if concern_id in evidence:
            report_concern_evidence(evidence[concern_id])
        for question in [item for item in questions if item.concern_id == concern_id]:
            typer.echo(f"question {question.id} (concern {concern_id}):")
            typer.echo(f"  {question.prompt}")
            if question.choices:
                typer.echo("  choices: " + " | ".join(question.choices))
            if question.recommendation is not None:
                typer.echo(f"  recommendation: {question.recommendation}")
            if not question.closed_choices:
                typer.echo("  (choices are suggestions; any answer is accepted)")


def report_awaiting(
    parked: ResolverAwaitingAnswers,
    adapter: str,
    run_id: str,
    concerns: list[Concern],
    views: list[PendingQuestionView],
) -> None:
    """Print the parked questions still open, their evidence, and the recipe.

    ``parked.pending`` is the list one concern held when it raised, and a run
    goes on working after that — for 37 minutes in the case reported, during
    which an answer arrived and was promoted. Printed unfiltered, the report
    named a settled question and told the human to answer it again, which
    costs a whole round trip to discover.

    An offered answer settles a question here alongside a promoted one: it
    is waiting on the run to take it, not on somebody to decide it, and this
    report's whole job is naming what a human still owes.
    """
    typer.echo(f"{local_stamp()} — resolver run parked awaiting material answers.")
    for problem in parked.problems:
        typer.echo(f"  problem: {problem}")
    settled = {
        view.question.id
        for view in views
        if view.answered is not None or view.offer is not None
    }
    outstanding = [
        question for question in parked.pending if question.id not in settled
    ]
    report_questions(outstanding, concerns)
    already = len(parked.pending) - len(outstanding)
    if already:
        typer.echo(
            f"{already} question(s) raised with this park were answered while "
            "it ran, and are not repeated here."
        )
    if not outstanding:
        typer.echo("Every question this park raised is answered; rerun to continue:")
    else:
        typer.echo("Relay the questions to the human, then rerun:")
    typer.echo(f"  {rerun_recipe(adapter, run_id, outstanding)}")


HOST_RETRIES: int = 20
"""How many times to come back to a host that refused, before giving up."""

HOST_BACKOFF_SECONDS: float = 60.0
"""The first wait after a host refuses; each later one doubles into the ceiling."""

HOST_RETRY_CEILING_SECONDS: float = 1800.0
"""The longest gap between probes, once doubling has grown past it."""

AUTH_PROBE_SECONDS: float = 30.0
"""How long to let a rotated credential settle before opening one fresh session.

Long enough that the session which rotated the token has written the file,
short enough that being wrong about a genuinely dead credential delays the
hand-back by less than a minute.
"""


def host_retry_delay(
    attempt: int,
    retries: int = HOST_RETRIES,
    backoff: float = HOST_BACKOFF_SECONDS,
    ceiling: float = HOST_RETRY_CEILING_SECONDS,
) -> float | None:
    """How long to wait before trying the host again, or None to stop trying.

    Doubling rather than reading the reset time the message quotes: the
    time is prose in a provider's sentence, and a probe that is early costs
    one failed session while a parse that is wrong costs the whole wait.
    """
    if attempt >= retries:
        return None
    return min(backoff * 2.0**attempt, ceiling)


def report_environment_fault(
    fault: ResolverEnvironmentFault, adapter: str, run_id: str
) -> None:
    """Print what stopped the host, and the command that continues once it works.

    Says plainly that nothing failed, because a record saying the opposite --
    every concern in flight written down as having failed with a provider's
    error as its reason -- leaves a reader deciding what to re-admit unable
    to tell those from work that did not hold up.
    """
    typer.echo(
        f"{local_stamp()} — resolver run stopped on an environmental fault, "
        "not on its work."
    )
    typer.echo(f"  cause: {fault.cause}")
    if fault.concerns:
        typer.echo(f"  interrupted: {', '.join(fault.concerns)}")
    typer.echo("No concern was failed and no outcome was recorded.")
    typer.echo(
        "For authentication or account allowance failures, re-login or switch "
        "account; a reported reset time is not a required wait. For a transient "
        "host failure, restore service before resuming."
    )
    typer.echo("Once the selected account and host are ready, continue with:")
    typer.echo(
        f"  uv run lup-devtools resolve --adapter {adapter} "
        f"--run-id {run_id} --adopt-config"
    )
    # Naming what resuming already does, because the fix that unblocks a run
    # lands while it is stopped, and a reader who does not know the base comes
    # forward on its own reaches for `refresh` or resumes onto a stale tree.
    typer.echo(
        "That takes whatever landed on the branch meanwhile. Leases already "
        "holding work stay put; `resolve refresh --apply` moves those."
    )


def report_drained(drained: ResolverDrained, adapter: str, run_id: str) -> None:
    """Print what an operator's stop cost, which is nothing, and how to go on."""
    typer.echo(
        f"{local_stamp()} — resolver run drained at a safe boundary, on request."
    )
    typer.echo(f"  reason: {drained.reason}")
    if drained.concerns:
        typer.echo(f"  stopped before a turn: {', '.join(drained.concerns)}")
    typer.echo("No concern failed and every committed round stands.")
    typer.echo("Continue with:")
    typer.echo(f"  uv run lup-devtools resolve --adapter {adapter} --run-id {run_id}")


def report_deferred_assembly(
    deferred: ResolverAssemblyDeferred, adapter: str, run_id: str
) -> None:
    """Print what is waiting to be merged, and how to come back to it.

    Deferring is not failing, and the wording matters: every branch this
    names is committed, verified and untouched. The run stopped at the one
    junction where stopping otherwise means killing the process.
    """
    typer.echo(f"{local_stamp()} — assembly deferred. The review branch was not built.")
    typer.echo(f"  ready to merge: {', '.join(deferred.verified)}")
    if deferred.excluded:
        typer.echo(f"  would be excluded: {', '.join(deferred.excluded)}")
    typer.echo("Every lease, branch and outcome is intact. Assemble later with:")
    typer.echo(
        f"  uv run lup-devtools resolve --adapter {adapter} "
        f"--run-id {run_id} --adopt-config "
        f"--answer {ASSEMBLY_QUESTION_ID}=approve"
    )


def report_regression(
    regression: ResolverRegression, adapter: str, run_id: str
) -> None:
    """Print which concerns the merged tree broke, and what the ruling means.

    The re-check's other answer, "superseded", settles a lost criterion and
    lets the run finish. This one does not, and saying so is the point: the
    review branch is deliberately left unfinished so the repair happens
    before anything is landed, rather than after.
    """
    typer.echo(
        f"{local_stamp()} — integration regressed criteria that held before the merge."
    )
    for ruling in regression.regressed:
        typer.echo(f"  {ruling.concern_id}: {', '.join(ruling.criteria)}")
    typer.echo("The review branch was not completed. Every lease and branch is intact.")
    typer.echo("Repair the merged tree, then continue with:")
    typer.echo(
        f"  uv run lup-devtools resolve --adapter {adapter} "
        f"--run-id {run_id} --adopt-config"
    )


def report_admission(admission: ConcernAdmission, adapter: str, run_id: str) -> None:
    """Print what joined the run and the gates it still has to pass."""
    typer.echo(
        f"Admitted {len(admission.concerns)} concern(s) into {run_id} "
        f"at phase {admission.phase}."
    )
    report_questions(admission.questions, admission.concerns)
    for problem in admission.rejected:
        typer.echo(f"  rejected: {problem}")
    answered = len(admission.questions) - len(admission.outstanding)
    if answered:
        typer.echo(f"Applied {answered} answer(s) supplied with this admission.")
    if not admission.outstanding:
        typer.echo("Every admitted question is answered; rerun to drive the run on:")
        typer.echo(f"  {rerun_recipe(adapter, run_id, [])}")
        return
    typer.echo("Relay the new questions to the human, then rerun:")
    typer.echo(f"  {rerun_recipe(adapter, run_id, admission.outstanding)}")


def resolver_git(
    launcher: ProcessLauncher,
    root: Path,
    arguments: list[str],
    *,
    environment: EnvVars | None = None,
) -> str:
    """Run one resolver-owned Git inspection or snapshot operation."""
    status = launcher.launch(
        LaunchRequest(
            arguments=["git", *arguments],
            cwd=root,
            environment=environment or {},
        )
    )
    if status.code != 0:
        raise RuntimeError(
            f"resolver Git operation failed ({' '.join(arguments)}): {status.stderr}"
        )
    lines = status.stdout.splitlines()
    return lines[0] if len(lines) == 1 else "\n".join(lines)


def resolver_source_snapshot(
    launcher: ProcessLauncher,
    root: Path,
    run_root: Path,
    note_paths: list[Path],
) -> SourceSnapshot:
    """Create an unattached source commit containing current review-note files."""
    branch = resolver_git(launcher, root, ["branch", "--show-current"]) or "HEAD"
    head = resolver_git(launcher, root, ["rev-parse", "HEAD"])
    # A run seeded from statements alone has no note file to preserve, and a
    # pathless diff would compare the whole tree and snapshot HEAD's own tree
    # under a second commit.
    if not note_paths:
        return SourceSnapshot(branch=branch, commit=head)
    status = launcher.launch(
        LaunchRequest(
            arguments=["git", "diff", "--quiet", "HEAD", "--", *map(str, note_paths)],
            cwd=root,
        )
    )
    if status.code == 0:
        return SourceSnapshot(branch=branch, commit=head)
    if status.code != 1:
        raise RuntimeError(f"resolver source inspection failed: {status.stderr}")
    run_root.mkdir(parents=True, exist_ok=True)
    index = (run_root / ".source.index").resolve()
    environment = {"GIT_INDEX_FILE": str(index)}
    try:
        resolver_git(launcher, root, ["read-tree", "HEAD"], environment=environment)
        resolver_git(
            launcher,
            root,
            ["add", "--", *map(str, note_paths)],
            environment=environment,
        )
        tree = resolver_git(launcher, root, ["write-tree"], environment=environment)
        commit = resolver_git(
            launcher,
            root,
            [
                "commit-tree",
                tree,
                "-p",
                head,
                "-m",
                "chore(review): resolver source snapshot",
            ],
            environment=environment,
        )
    finally:
        if index.exists():
            index.unlink()
    return SourceSnapshot(branch=branch, commit=commit)


# lup: ignore[constant-declaration] — the run's own branch naming, which a
# resumed run must spell exactly as the run that created the branch
REVIEW_BRANCH_SUFFIX = "/review"


def integration_branch(launcher: ProcessLauncher, root: Path, run_id: str) -> str:
    """Where this run integrates: onto a standing review branch, or a fresh one.

    A resolve run started while HEAD is already a review branch is resolving
    that branch's own feedback, so minting a second one strands the work on a
    branch nobody asked for and leaves the human to reconcile two. Advancing
    the branch it was launched from is what makes a nested run compose.
    """
    current = resolver_git(launcher, root, ["branch", "--show-current"])
    if current.startswith("resolve/") and current.endswith(REVIEW_BRANCH_SUFFIX):
        return current
    return f"resolve/{run_id}{REVIEW_BRANCH_SUFFIX}"


def refresh_run(
    run_id: str = typer.Option(..., "--run-id", help="Run whose base to refresh"),
    apply: bool = typer.Option(
        False,
        "--apply",
        help="Take the refresh, instead of only reporting what it would do",
    ),
    base: str = typer.Option(
        "",
        "--base",
        help="Adopt a base you resolved by hand, where the combine conflicts. "
        "It must contain both the run's base and the branch, so a resolution "
        "that dropped one side is refused rather than taken",
    ),
) -> None:
    """Bring a run's base, and the leases holding work, up to its branch.

    A run's leases are cut from the commit it was created at, so a fix made
    on the integration branch to unblock that very run is the one thing it
    cannot see. A lease made after this reads the branch as it stands; a
    lease already holding work is merged only with `--apply`, and only where
    this has already reported that it would not conflict.

    Where combining the two bases conflicts, this reports the paths and
    stops: the fix that unblocks a run touches what that run's notes are
    about, so conflicting is ordinary. Resolve it once in a worktree and
    hand the commit back with `--base`, rather than meeting the same
    conflict again in every lease at land.

    This takes the run's own lock, so the run's process must have exited —
    which is exactly when a parked run is waiting for the fix to land.
    """
    root = project_root()
    state_root = CheckoutState(root=root).resolve()
    repository = ResolverStateRepository(state_root, run_id)
    if not repository.exists():
        raise typer.BadParameter(f"no resolver run {run_id!r} under {state_root}")
    launcher = PointerCheckedLauncher(LocalProcessLauncher(), root)
    journal = Journal(repository.root)
    run = ResolveRun(repository, journal)
    run.state = repository.load()
    report = BaseRefresher(run, WorktreeOrchestrator(launcher, root), journal).report(
        run.require(), apply, base
    )
    for line in describe_refresh(report):
        typer.echo(line)


def describe_refresh(report: RefreshReport) -> list[str]:
    """Render a refresh the way somebody deciding whether to take it reads it."""
    base = report.base

    def opening() -> str:
        if base.reason:
            return f"base unchanged: {base.reason}"
        if not base.moved():
            return f"base is current with {base.branch}"
        return (
            f"base {'moved' if report.applied else 'would move'} onto {base.branch}: "
            f"{base.was[:12]} → {base.commit[:12]}"
        )

    def outcome(lease: LeaseRefresh) -> str:
        match lease:
            case LeaseRefresh(conflicts=[_, *_] as conflicts):
                return "conflicts on " + ", ".join(
                    path.as_posix() for path in conflicts
                )
            case LeaseRefresh(applied=True):
                return "merged"
            case LeaseRefresh(reason=str() as reason) if reason:
                return reason
            case _:
                return "merges cleanly"

    lines = [opening()]
    lines.extend(f"  {path.as_posix()}" for path in base.conflicts)
    if base.conflicts:
        lines.append(
            f"Merge {base.was[:12]} with {base.branch} in a worktree, resolve it "
            "there, then adopt the result: --base <commit> --apply."
        )
    if not base.moved():
        return lines
    lines.extend(f"  {lease.concern_id}: {outcome(lease)}" for lease in report.leases)
    if not report.applied and any(not lease.conflicts for lease in report.leases):
        lines.append("Re-run with --apply to take it.")
    return lines


class AdmissionFlags(BaseModel, frozen=True):
    """The evidence one invocation named, in the flags that carried it.

    Kept as flags rather than resolved evidence because a detached run is
    launched by rebuilding this command line: what a human named has to be
    sayable again, and whatever a relaunch cannot say is dropped without a
    word.
    """

    statements: list[str]
    notes: list[str]
    issues: list[int]

    def named_anything(self) -> bool:
        return bool(self.statements or self.notes or self.issues)

    def arguments(self) -> list[str]:
        """These flags again, so a relaunch carries everything this one named."""
        return [
            *(part for item in self.statements for part in ("--admit", item)),
            *(part for item in self.notes for part in ("--admit-note", item)),
            *(part for item in self.issues for part in ("--admit-issue", str(item))),
        ]


class DetachedRun(BaseModel, frozen=True):
    """One invocation, spelled as the relaunch that has to carry it on.

    A detached launch re-issues its own command in a child, so every option
    deciding what the run does has to survive the fork. Carrying a subset is
    what this shape exists to prevent, and each omission fails silently
    behind a parent that has already reported a run started: a missing
    `--admit` loses the words the run was asked to plan from, and a missing
    `--no-issues` detaches a larger run than anyone asked for — every open
    issue as evidence, a worktree leased per concern.

    ``run_id`` is forwarded only where a human named one. A launch derives
    the same id from the same commit the child derives it from, so passing it
    back adds nothing except a claim that the run already exists — and that
    claim refuses an admission instead of seeding from it, leaving the child
    to reject its own command line where nobody is listening.
    """

    adapter: str
    run_id: str | None
    answers: list[str]
    admitted: AdmissionFlags
    issues: bool
    wait: float
    host_retries: int
    host_backoff: float
    supervisor: SupervisorSpawn
    adopt_config: bool
    auth_probe_delay: float
    max_parallel_workers: int
    recheck_standing_per_join: bool
    profile: str | None
    """The account the child opens every session under.

    Carried like everything else here, and for a sharper reason than most: a
    child that dropped it would not fail, it would run on whichever account
    the detaching shell happened to export — the operator's own, silently,
    for every planner, worker and reviewer the run opens."""

    def arguments(self) -> list[str]:
        """The command a child is started with, carrying this whole invocation."""
        return [
            "uv",
            "run",
            "lup-devtools",
            "resolve",
            "--adapter",
            self.adapter,
            *(["--profile", self.profile] if self.profile is not None else []),
            *(["--run-id", self.run_id] if self.run_id is not None else []),
            *(part for answer in self.answers for part in ("--answer", answer)),
            *self.admitted.arguments(),
            *([] if self.issues else ["--no-issues"]),
            *(["--wait", str(self.wait)] if self.wait else []),
            # Always spelled: zero retries parks on the first refusal, so
            # rendering these only when truthy would drop the one setting a
            # human is most likely to have named deliberately.
            "--host-retries",
            str(self.host_retries),
            "--host-backoff",
            str(self.host_backoff),
            *self.supervisor.arguments(),
            *(["--adopt-config"] if self.adopt_config else []),
            # Spelled for the same reason the host settings above are: each
            # decides how wide the run goes and how long it waits, so falling
            # back to a default in the child is a run nobody asked for.
            "--auth-probe-delay",
            str(self.auth_probe_delay),
            "--max-parallel-workers",
            str(self.max_parallel_workers),
            *(
                ["--recheck-standing-per-join"]
                if self.recheck_standing_per_join
                else []
            ),
        ]


def detach_resolve(detached: DetachedRun) -> None:
    """Start a run that outlives this command, and say where to reach it.

    A blocking run holds the launching agent's only turn, so nothing could
    write to a run while it moved — which made every delivery route in the
    design unreachable, however well the channels underneath worked. Once
    launching returns, the run directory is the whole contract: the page and
    an orchestrating agent are peers on it, exactly as two pages would be.

    Statements ride along with everything else the invocation named: seeding
    a run from what a human said and returning is the shape this flag exists
    for.

    The log publishes the run directory before the child forks, so the watcher
    can attach while inventory is still being planned. It reports success after
    the fork and cannot retract it. What can be judged first is judged here;
    what cannot goes to a named file, so a child refusal leaves
    a record instead of a launcher claiming a run that does not exist.
    """
    root = project_root()
    resolved = detached.run_id or (
        "resolve-"
        + resolver_git(
            LocalProcessLauncher(), root, ["rev-parse", "--short=12", "HEAD"]
        )
    )
    # Resolve the evidence here and discard what it returns. The child
    # resolves it again, but only the child would meet a `--admit-note`
    # naming nothing actionable or an issue number naming nothing open, and
    # it meets it after this command has already reported a run started.
    admission_request(detached.admitted)
    repository = ResolverStateRepository(CheckoutState(root=root).resolve(), resolved)
    if repository.held():
        raise typer.BadParameter(f"resolver run {resolved!r} is already active")
    log = detached_log(root, resolved)
    arguments = detached.arguments()
    # One stream, not one path opened twice: sh opens `_out` and `_err`
    # separately, so naming the same file for both leaves two handles
    # truncating at offset zero and overwriting each other — losing exactly
    # the refusal this file exists to keep.
    with log.open("ab") as output:
        sh.Command(arguments[0])(
            *arguments[1:],
            _cwd=str(root),
            _bg=True,
            _bg_exc=False,
            _new_session=True,
            _out=output,
            _err_to_out=True,
        )
    typer.echo(f"Run {resolved} started detached.")
    typer.echo(f"Its output: {log}")
    typer.echo(
        f"Follow it: uv run lup-devtools resolve status --run-id {resolved} --watch"
    )


def detached_log(root: Path, run_id: str) -> Path:
    """Where a detached run's console output is kept, beside its own record."""
    directory = CheckoutState(root=root).resolve() / run_id
    directory.mkdir(parents=True, exist_ok=True)
    return directory / "detached.log"


def forwardable_arguments(argv: list[str]) -> list[str]:
    """This invocation's own arguments, minus the flag that detached it.

    Taken from the command line rather than rebuilt from parsed values
    because rebuilding is what loses flags. ``--detach`` is the one thing
    removed: left in, the child detaches again and nothing ever runs.

    Matched as a token, so an option *value* spelled ``--detach`` would go
    with it. Every value this command takes is a reason, an id, an answer or
    a number, and detaching an abort is contradictory anyway — the case is
    named rather than guarded because a guard would have to re-list the
    options, which is the coupling this function exists to remove.
    """
    return [argument for argument in argv[1:] if argument != "--detach"]


def admission_request(flags: AdmissionFlags) -> AdmissionRequest | None:
    """Build the evidence one invocation asked to admit, if it asked at all."""
    if not flags.named_anything():
        return None
    targets = parse_note_targets(flags.notes)
    scanned = scanned_intake(project_root()).actionable if targets else []
    return AdmissionRequest(
        notes=admission_notes(targets, scanned),
        statements=flags.statements,
        issues=admitted_issues(flags.issues),
    )


def queue_existing_admission(
    flags: AdmissionFlags, run_id: str | None, answers: list[str], start_new: bool
) -> bool:
    """Route evidence to an existing run before a detached child can be opened."""
    if not flags.named_anything() or (start_new and run_id is None):
        return False
    root = project_root()
    state_root = CheckoutState(root=root).resolve()
    selected = run_id or chosen_run(
        state_root,
        "resolve-"
        + resolver_git(
            LocalProcessLauncher(), root, ["rev-parse", "--short=12", "HEAD"]
        ),
        start_new=False,
        ending=False,
    )
    repository = ResolverStateRepository(state_root, selected)
    if not repository.exists():
        if (refusal := missing_run_refusal(run_id, selected)) is not None:
            raise typer.BadParameter(refusal)
        return False
    request = admission_request(flags)
    if request is None:
        return False
    offer_flag_answers(
        QuestionMailbox(repository.root), selected, parse_answer_flags(answers)
    )
    try:
        receipt = repository.queue_admission(request)
    except StateTransitionError as error:
        raise typer.BadParameter(str(error)) from error
    typer.echo(f"Admission {receipt.id} queued for run {selected}.")
    typer.echo(
        "Evidence is durable; the run plans it at its next scheduling boundary. Resume the run if it is idle."
    )
    typer.echo(f"Inspect: uv run lup-devtools resolve admissions --run-id {selected}")
    return True


def list_admissions(
    run_id: str = typer.Option(
        ..., "--run-id", help="Run whose admission receipts to read"
    ),
    json_output: bool = typer.Option(
        False, "--json", help="Print each complete receipt as JSON"
    ),
) -> None:
    """Inspect accepted evidence and its pending, applied, or rejected result."""
    repository = ResolverStateRepository(
        CheckoutState(root=project_root()).resolve(), run_id
    )
    if not repository.exists():
        raise typer.BadParameter(f"no resolver run {run_id!r}")
    receipts = AdmissionMailbox(repository.root).receipts()
    if not receipts:
        typer.echo("No admission requests.")
    for receipt in receipts:
        if json_output:
            typer.echo(receipt.model_dump_json())
        else:
            typer.echo(f"{receipt.id}: {receipt.status}")
            if receipt.error:
                typer.echo(receipt.error)
            if receipt.result is not None:
                typer.echo(
                    "Concerns: "
                    + ", ".join(item.id for item in receipt.result.concerns)
                )


def missing_run_refusal(run_id: str | None, resolved_run_id: str) -> str | None:
    """Why admitting into a run that does not exist is refused, where it is.

    Naming a run is a claim that it exists, so a typo seeds a second run under
    the misspelling and leases a worktree per concern before anyone reads the
    line saying so. An id nobody named was never that claim: it defaulted from
    the commit, and seeding is what somebody arriving with statements asked
    for. So the refusal survives for exactly one case, and says that statements
    do not need a run to exist rather than leaving that to be inferred.
    """
    if run_id is None:
        return None
    return (
        f"no resolver run {resolved_run_id!r} to admit into. Statements seed a "
        "run of their own: drop --run-id to start one from them, or name a run "
        "that exists."
    )


def seed_request(
    source: SourceSnapshot,
    comments: list[FoundComment],
    issues: list[IssueEvidence],
    admission: AdmissionRequest | None,
) -> ResolveRequest | None:
    """The evidence a fresh run plans from: what the tree holds and what was said.

    A statement has nowhere else to come from — somebody arriving with the
    work in their own words has written nothing down yet — so a run seedable
    only from notes made them invent note sites before it would start. The
    same evidence a run is handed mid-flight seeds one at the outset, and the
    two kinds mix: the tree's notes and the human's statements are both
    positions in one request, planned by one turn.

    Evidence named explicitly is folded in rather than appended, because a
    note or an issue reaches a fresh run through the scan as well, and the
    same work described twice is planned as two concerns.

    Having nothing to plan from is answered here, as ``None``, rather than by
    whoever calls this: the evidence folded together here is exactly the
    evidence a request refuses to exist without, so a caller deciding it
    separately is a second copy of one rule — and the copy that drifts is the
    one a suite stays green through, because no test can reach it.
    """
    named = admission.notes if admission is not None else []
    statements = admission.statements if admission is not None else []
    admitted = admission.issues if admission is not None else []
    scanned = [
        InventoryNote(
            file=Path(comment.file),
            line=comment.start_line,
            text=comment.marker_text(),
            context=comment.context,
        )
        for comment in comments
    ]
    notes = {(note.file, note.line): note for note in [*scanned, *named]}
    numbered = {issue.number: issue for issue in [*issues, *admitted]}
    if not notes and not statements and not numbered:
        return None
    return ResolveRequest(
        source=source,
        notes=list(notes.values()),
        statements=statements,
        issues=list(numbered.values()),
    )


def worker_policy_hooks(
    declared_hooks: HookSet,
    grants: LeaseGrants,
    semantics: NativeSemantics,
    sandbox: SandboxPosture,
    relay: EscalationRelay | None = None,
) -> LupHooksConfig:
    """Judge a worker's calls by the policy every plugin enforces.

    The directory ACL beside this bounds the worker's filesystem reach and
    says nothing about what it may fetch or run — so egress and shell passed
    unjudged, with the OS sandbox as the only floor. This supplies exactly
    those verdicts, scoped to the tools it has rules for: a headless worker
    has no human to answer an `ask`, so judging a tool this vocabulary cannot
    classify would deny it, and would outrank the ACL's own grant.

    The worker is autonomous because its edits are reviewed by an actor of
    this run, which is the same fact the generated tree derives its
    autonomous list from.

    ``grants`` is asked what the lease holds at each judgment, over the same
    document the session environment names — so this judge and the lease's
    deployed dispatcher release exactly the same gates, including one granted
    after both were built.

    ``semantics`` is how one runtime's calls become the vocabulary this
    policy judges. It is a parameter rather than a constant because both
    runtimes have that decode now, and hardcoding one was what left the
    other's workers judged by nothing.

    ``relay`` is where an escalation goes when there is nobody here to answer
    it. The marker exists to route a judgement to a human who can weigh the
    actual command, and a worker session has none attached — so for a worker
    the three tiers collapsed to two and every escalation was a guaranteed
    refusal, in exactly the context that most needs one. It still refuses,
    because nothing here can approve what no human saw; what it stops doing
    is refusing in silence.

    ``sandbox`` is the session's own confinement, read from the declaration
    the factory opens it with rather than from the runtime. Taking the
    runtime's word granted an escape the session forbade — rendered onto the
    wire, dropped without a word, and the call left confined to fail on
    whatever it wrote first. Asking the session instead is what lets a
    worker's toolchain be placed outside and actually get there.

    Only the placement is taken from it. What the posture says about
    confinement stays out of the verdict, because the kernel spends that fact
    on a substitution this host cannot afford: its arm takes ``defer`` and
    ``ask`` together, so where there is no human to answer, every guarded
    verdict becomes a run rather than a refusal — ``find -delete`` and ``git
    push --delete`` among them, and a ``# lup: escalate:`` marker, which
    resolves to an ask, turned into the way to avoid the human it exists to
    summon. A worker keeps the fail-closed floor until it can answer a
    question through the channel it already holds.

    It composes here, outside the factory that calls it, because the defect
    this shape exists to prevent was never in a verdict: the kernel answered
    correctly every time and the session handed it host facts it did not
    have. A composition only a running resolver can build is one no test
    reaches, and this one shipped its own widening once already.
    """
    return create_policy_hooks(
        semantic_policy_for(
            declared_hooks,
            autonomous=True,
            interactive=False,
            escapable=semantics.escapes_from(sandbox),
            grants=grants,
        ),
        semantics.also_refusing(declared_hooks.refused_tools),
        sandbox=sandbox,
        relay=relay,
    )


def answer_issues(concerns: list[Concern], manifest: ResolveManifest) -> list[int]:
    """Say on each answered issue where the work that answers it now sits.

    Only issues whose concern reached the review branch, because a concern
    that failed has nothing to point at and an issue told about work that
    does not exist is worse than an issue told nothing. Commented, never
    closed: a run's reviewer passing is not a human having read the code.
    """
    landed = {outcome.concern_id for outcome in manifest.outcomes if outcome.integrated}
    answered = {
        issue.number: issue
        for concern in concerns
        if concern.id in landed
        for issue in concern.issues
    }
    if not answered:
        return []
    return comment_on_issues(
        sorted(answered.values(), key=lambda issue: issue.number),
        f"Addressed on review branch `{manifest.review_branch}` by resolver run "
        f"`{manifest.run_id}`. Left open: the branch still wants a human review.",
    )


def admitted_issues(numbers: list[int]) -> list[IssueEvidence]:
    """Locate each named issue among the open ones, refusing any it cannot.

    Read from the tracker rather than retyped, so a concern admitted from an
    issue is grounded in exactly what intake would have planned from — and a
    number that names nothing open is a typo worth refusing at the flag
    rather than a concern planned from silence.
    """
    if not numbers:
        return []
    open_issues = {issue.number: issue for issue in fetch_open_issues()}
    missing = [str(number) for number in numbers if number not in open_issues]
    if missing:
        raise typer.BadParameter("no open issue numbered: " + ", ".join(missing))
    return [open_issues[number] for number in numbers]


def chosen_run(state_root: Path, fresh: str, *, start_new: bool, ending: bool) -> str:
    """Which run this invocation means, when it named none.

    A run id defaults to the commit it starts from, so the default moves at
    every commit and two invocations a day apart never mean the same run.
    Left to that, entering the resolver from a skill starts a second run
    beside a parked one holding every answer already collected — the most
    expensive thing this tool can do, and the one the entry made easiest.

    So an unfinished run is put to whoever ran this rather than guessed at
    in either direction. Non-interactively it refuses and names both routes,
    because the caller is then an agent whose job is to relay the choice to
    a human, not to take it.
    """
    if start_new or ending:
        return fresh
    unfinished = unfinished_runs(state_root)
    if not unfinished:
        return fresh
    newest = unfinished[0]
    typer.echo(f"{len(unfinished)} unfinished run(s) under {state_root}:", err=True)
    for summary in unfinished:
        typer.echo(f"  {summary.line()}", err=True)
    if not sys.stdin.isatty():
        raise typer.BadParameter(
            f"this project has an unfinished run. Resume it with --run-id "
            f"{newest.run_id}, or start a fresh one with --new. Resuming keeps "
            "every answer already collected; starting fresh re-derives the "
            "inventory and discards them"
        )
    if typer.confirm(f"Resume {newest.run_id}?", default=True):
        return newest.run_id
    return fresh


def run_resolve(
    composition: NativeHarnessComposition,
    account: SessionAccount,
    run_id: str | None,
    answers: list[str],
    abort_reason: str | None = None,
    wait_seconds: float = 0.0,
    supervisor: SupervisorSpawn | None = None,
    admission: AdmissionRequest | None = None,
    model: ConfiguredModel | None = None,
    adopt_config: bool = False,
    take_issues: bool = True,
    host_retries: int = HOST_RETRIES,
    host_backoff: float = HOST_BACKOFF_SECONDS,
    auth_probe_delay: float = AUTH_PROBE_SECONDS,
    max_parallel_workers: int = 4,
    recheck_standing_per_join: bool = False,
    start_new: bool = False,
) -> None:
    """Drive the shared persisted resolver through one explicit native adapter."""
    provided = parse_answer_flags(answers)
    if abort_reason is not None and admission is not None:
        raise typer.BadParameter("a run cannot be widened and ended in one command")
    # The recipe already names the target it compiles for and carries the
    # declaration it compiles, so the adapter a rerun recipe prints, the
    # plugin a lease deploys, and the hooks a session judges by all come from
    # the composition that was actually resolved.
    adapter = composition.recipe.label
    harness = composition.recipe.source
    if harness.resolver is None:
        raise typer.BadParameter(
            "this project declined the resolver module, so it declares no "
            "resolver to run: take the module back in `DECLINED` and regenerate"
        )
    resolver_spec = harness.resolver
    plugin = harness.plugins[0]
    root = project_root()
    launcher = PointerCheckedLauncher(LocalProcessLauncher(), root)
    state_root = CheckoutState(root=root).resolve()
    resolved_run_id = run_id or chosen_run(
        state_root,
        "resolve-" + resolver_git(launcher, root, ["rev-parse", "--short=12", "HEAD"]),
        start_new=start_new,
        ending=abort_reason is not None,
    )
    # Every worktree this run leases lands under here, which is what makes
    # "a checkout lup created" a structural test rather than a judgement.
    worktree_root = root.parent / f"{root.name}-resolve-{resolved_run_id}"
    # A run captures one base and cuts every lease from it, so a base already
    # behind is planned against code that moved: the pass this refusal exists
    # for planned thirteen concerns on a tree ten commits stale, where merged
    # work had already done part of them. Following the move instead would
    # mean re-basing every lease, re-deriving each diff, and re-running intake
    # mid-flight, which can add or drop concerns while work is leased. Only a
    # starting run is refused; one already recorded keeps the base it
    # recorded, so a pull mid-run never strands it.
    recorded = ResolverStateRepository(state_root, resolved_run_id).exists()
    if abort_reason is not None:
        # Ending a run reads recorded state and frees worktrees. It opens no
        # session and reaches no remote, so a run that was never recorded is
        # refused here rather than after a config home, a plugin install and a
        # remote probe have been built to end something that is not there.
        if not recorded:
            raise typer.BadParameter(f"no resolver run {resolved_run_id!r} to abort")
    else:
        if not check_remote_auth():
            typer.echo(
                "Continuing local-only: agent git commands that need the remote "
                "will fail fast instead of prompting.",
                err=True,
            )
        if not recorded:
            require_fresh_base(probe_base_freshness(launcher, root))
    # A run leases a worktree per concern, and leasing one registers the merge
    # driver where the clone has none — so a confinement that owns
    # `config.lock` would meet that at its first lease, however many concerns
    # it planned, with a bare `File exists` that names nothing about a
    # sandbox. Said once here instead, before anything is planned or leased,
    # and only where that registration is still outstanding: a clone that
    # already resolves the driver leases every worktree without writing config
    # at all, and one that cannot register leases them unregistered, which is
    # the state every worktree of that clone was already in.
    if abort_reason is None:
        report_a_blocked_registration(root)

    async def execute() -> None:
        from lup.launch.declaration import (
            InnerSandbox,
            NoSandbox,
            OuterContainer,
        )
        from lup.providers.claude import Claude, ClaudeTools
        from lup.providers.claude.runtime import (
            environmental_fault,
            may_be_a_rotation,
            needs_a_person,
        )
        from lup.providers.codex import Codex, CodexTools
        from lup.providers.claude.model_choice import claude_model_choice
        from lup.providers.codex.model_choice import codex_model_choice
        from lup.providers.claude.config_home import (
            selected_config_home,
            untrusted_degradation,
            workspace_config_environment,
        )
        from lup.providers.claude.hooks import CLAUDE_SEMANTICS
        from lup.providers.codex.hooks import CODEX_SEMANTICS
        from lup.policy.hooks import (
            create_git_inspection_hook,
            create_permission_hooks,
            merge_hooks,
        )

        from lup.providers.codex.install import install_codex_plugin

        from lup.providers.codex.home import CodexWorktreeHomeStore, select_codex_home
        from lup.providers.profile_tree import profile_directory

        def codex_policy_environment(target: str, environment: EnvVars) -> EnvVars:
            """Point a Codex session at a home carrying this project's policy.

            The other adapter takes the semantic kernel as session hooks. This
            one has no such seam — its app-server handles dynamic tools and MCP
            elicitations and holds approvals at ``never`` — so what judges a
            Codex session is the generated dispatcher its plugin registers, and
            a plugin is installed per configuration home. Preparing the home
            the way an interactive launch does is what puts a session opened
            here under the policy an operator already runs under, instead of
            leaving the OS sandbox as its only floor.

            One home per run rather than per concern: the plugin is the same
            for every worker, and seeding a personal account copy once per
            concern would differ from the operator's in nothing but cost.
            It is derived from the run's account on Codex — the profile the
            run was started as, which names one account on both runtimes.
            """
            if target != "codex":
                return {}
            codex_account = profile_directory(CODEX_LOGIN).launch_home(account.name)
            store = (
                CodexWorktreeHomeStore()
                if codex_account is None
                else CodexWorktreeHomeStore(account_home=codex_account)
            )
            home = select_codex_home(None, environment, root, store=store)
            if not resolver_spec.contain_actors:
                install_codex_plugin(root, home.path, trusted=home.isolated)
            return {"CODEX_HOME": str(home.path)}

        session_environment = account.exported(non_interactive_environment(inherited()))
        # Both identities are written, never omitted: a runtime merges the
        # session environment over the launching process's, so a reviewer
        # that stayed silent would inherit an operator's exported identity.
        worker_environment = {
            **session_environment,
            **agent_identity_environment(resolver_spec.worker_identity),
        }
        reviewer_environment = {
            **session_environment,
            **agent_identity_environment(""),
            **allowance_grants_environment(None),
        }
        for environment in (worker_environment, reviewer_environment):
            environment.update(codex_policy_environment(adapter, session_environment))
        session_model = model.reaching(adapter) if model is not None else None
        if session_model is None and model is not None:
            typer.echo(
                f"Configured model {model.name!r} does not route to adapter "
                f"{adapter!r}; sessions use the adapter's native default model."
            )
        # Read against the catalog of the adapter it routes to, so a name that
        # adapter does not list is refused before any session is spawned.
        claude_model = (
            claude_model_choice(session_model)
            if session_model is not None and adapter == "claude"
            else None
        )
        codex_model = (
            codex_model_choice(session_model)
            if session_model is not None and adapter == "codex"
            else None
        )

        def isolated_claude_environment(
            environment: EnvVars, workspace: Path
        ) -> EnvVars:
            """One workspace's sessions, on a configuration document of their own.

            Every worker starts by reading Claude's configuration document and
            writing it back, so a phase that opens them together has each one
            reading what the others are still writing: a run of eleven lost
            six to a truncated parse and the remaining five never started,
            with no concern having produced code. A document per workspace
            removes the shared file the race needs, which is why neither a
            lock nor a cap on how many run at once appears anywhere here.

            Trust rides along because it is written into that same document.
            An untrusted workspace does not fail a session — Claude drops the
            repository's declared permissions, warns into that session's own
            stderr and carries on — so a run without this establishes nothing
            and reports the loss only as noise between progress lines. A run
            that cannot establish it stops here instead, before the session
            that would have run under a posture the repository never declared.
            """
            derived = workspace_config_environment(
                environment,
                workspace,
                trust=run_owned(workspace, root, worktree_root),
            )
            # The whole session environment rather than the home alone: which
            # document a session reads depends on more of it than the home.
            session = {**environment, **derived}
            degradation = untrusted_degradation(
                workspace, selected_config_home(session).document
            )
            if degradation is not None:
                raise typer.BadParameter(
                    f"{degradation} This run extends trust to the repository it "
                    "was invoked against and to the checkouts it made of that "
                    f"repository under {worktree_root}, and to nothing else."
                )
            return session

        # Once, before anything is leased. Every private home this run derives
        # is seeded from the one document this reads, so a run that cannot
        # read it opens no session anywhere — a fact about the environment
        # rather than about any concern. Discovering it per worker instead
        # turns one environmental fault into an exception group of concern
        # failures and burns every lease the run has taken.
        home = (
            selected_config_home(session_environment) if adapter == "claude" else None
        )
        # Two facts about the same home, established before anything is
        # leased: whether its document can be read, and whether a session
        # opened under it would keep its shell. The second is the one a run
        # otherwise discovers by losing Bash in every worker at once, with each
        # failure naming a read-only filesystem and none of them naming the
        # boundary that made it one.
        fault = next(
            (
                found
                for found in (
                    []
                    if home is None
                    else [home.configuration_fault(), home.shell_fault()]
                )
                if found is not None
            ),
            None,
        )
        if fault is not None:
            raise typer.BadParameter(fault)

        # Once, before anything is leased, and for the same reason the config
        # home is read here: a host that cannot give its actors a boundary is a
        # fact about the machine rather than about any concern, and discovering
        # it per worker would turn one environmental refusal into an exception
        # group of concern failures after the run had already taken its leases.
        contained_actors = resolver_spec.contain_actors
        if contained_actors:
            absence = engine_absence()
            if absence is not None:
                raise typer.BadParameter(absence)

        # The program this run's actors are started as, named here because
        # this is where the adapter is already chosen. A CLI's own name is its
        # provider's vocabulary, so it is spelled at the composition root that
        # picked the provider rather than in the neutral container builder,
        # which would then be a second place to keep agreeing with this one.
        actor_program = "claude" if adapter == "claude" else "codex"

        def actor_cli(
            workspace: Path,
            concern_id: str,
            actor: str,
            environment: EnvVars,
            read_only: bool = False,
        ) -> Path | None:
            """The program one actor is started as, so it runs in its own container.

            ``None`` when the project declared its actors uncontained, which is
            what both runtimes take to mean "find the CLI the way you always
            do" — so the unconfined path stays the one that was there rather
            than becoming a second arrangement this has to keep working.

            The configuration home is read from the actor's own environment
            rather than the run's. Each workspace already gets a derived home so
            that no two actors write one document at once, and a container bound
            to the run's home instead would put every actor back on the shared
            file that race needs.
            """
            if not contained_actors:
                return None
            login = CLAUDE_LOGIN if adapter == "claude" else CODEX_LOGIN
            config_home = login.selected_home(environment)
            credential = login.credentials_path(config_home)
            return worker_cli(
                worker_wrapper_path(state_root / resolved_run_id, concern_id, actor),
                harness.image,
                harness.requirements,
                workspace,
                # No editor bridge: an actor has nobody at a keyboard, so
                # binding the operator's rendezvous directory into its
                # container would widen the boundary for a channel nothing
                # on either side of it reads.
                None,
                credential if credential.exists() else None,
                login,
                program=actor_program,
                read_only=read_only,
                accessible=accessible_roots(),
                devices=granted_devices(),
            )

        def toolchain_writable_paths() -> list[Path]:
            """Absolute paths a sandboxed worker's toolchain must be able to write.

            A worker is contained to its lease, which is where its work belongs
            — but every command it verifies with reaches the toolchain through
            `uv`, whose cache lives outside any worktree. Granting the declared
            paths is what keeps containment from disarming verification.
            """
            declared = plugin.hooks.sandbox if plugin.hooks is not None else None
            return [
                Path(path).expanduser()
                for path in (declared.writable_paths if declared is not None else [])
            ]

        # A worker is confined to its lease, and the toolchain it verifies
        # with is declared to run outside that confinement — so the escape has
        # to be permitted where the session is opened, or the placement is
        # rendered and dropped, which is the failure the placement exists to
        # prevent. Claude's channel is a per-call argument, so one object says
        # this to the session and to the policy at once and the two cannot
        # come apart.
        #
        # A spawned session also inherits none of the settings files a launched
        # one reads, so the exclusions the declaration states are handed over
        # here as well: without them a worker is confined by a boundary its own
        # toolchain does not fit through, and every failure it meets names
        # something other than the boundary.
        claude_worker_sandbox = InnerSandbox(
            escapable=True,
            excluded_commands=plugin.hooks.excluded_commands() if plugin.hooks else [],
        )
        # Codex has no such channel: its sandbox is a mode on the whole
        # session, declared with that session below, and a call placed outside
        # runs confined there whatever anyone says. So this carries the one
        # fact the policy can act on — that nothing escapes — and says nothing
        # about a confinement it could only restate.
        codex_worker_sandbox = SandboxPosture()

        def worker_factory(context: WorkerContext) -> Agent:
            """Open one worker session that can ask its own questions.

            The tools are bound to this concern here rather than taking the
            id as an argument, so a worker structurally cannot post against
            a sibling. ``core`` is read at call time, which is after it is
            built — the wake event only exists once the core does.

            The delivery hooks arrive in the context rather than being
            fetched here. They are the run's own mailbox for this actor, and
            opening a second reader over one message stream gave the two
            positions that each began at whatever the head was when they were
            made — so a message posted while a turn was in flight sat behind
            both of them.
            """
            cwd = context.root
            tool_context = ResolverToolContext(
                run_dir=state_root / resolved_run_id,
                concern_id=context.concern_id,
                lease_root=cwd,
                actor_kind=context.actor.kind,
            )

            def relay(why: str, refusal: str) -> None:
                """Put a refused escalation where a human running this will see it.

                Addressed to the person rather than broadcast, so it lands in
                the one mailbox `resolve actors` prints for a human rather than
                in every sibling worker's context.
                """
                core.actors.tell_user(
                    f"{context.actor.label()} was refused a command it escalated.\n"
                    f"Its reason: {why}\n"
                    f"The refusal: {refusal}"
                )

            # Grants are per-concern, and the environment names where this
            # lease's are written rather than carrying them: a gate granted
            # after this session starts reaches it, and one taken back stops
            # applying, without the restart the environment would have needed.
            concern_environment = {
                **worker_environment,
                **allowance_grants_environment(context.grants.document),
            }
            # A merger sequences its own join, so it carries the verbs that
            # do it. They are added by actor kind rather than granted to
            # every session, because commit authority over the integration
            # tree is exactly what a worker must not have.
            actor_tools = [
                *create_question_tools(
                    QuestionMailbox(tool_context.run_dir),
                    context.concern_id,
                    run_id=resolved_run_id,
                    lease_root=tool_context.lease_root,
                    wake=core.wake,
                ),
                *(
                    create_join_tools(
                        tool_context.run_dir,
                        tool_context.lease_root,
                        context.concern_id,
                        launcher=launcher,
                    )
                    if context.actor.kind == "merger"
                    else []
                ),
            ]
            # Over stdio rather than in process, which is what makes the
            # boundary reachable at all: a CLI running inside a container
            # cannot see an object living in this one. One transport for both
            # runtimes, so they answer the question the same way instead of
            # one of them holding a server only an uncontained worker could
            # reach.
            questions = External(
                name="resolver",
                server=RawStdioServerConfig(
                    command="uv",
                    args=["run", "lup-devtools", "resolve", "serve-tools"],
                    env={**session_environment, **tool_context.to_env()},
                ),
            )
            if adapter == "claude":
                worker_environment_for = isolated_claude_environment(
                    concern_environment, cwd
                )
                return Claude(
                    model=claude_model,
                    system_prompt="Execute the persisted Lup resolver assignment.",
                    tools=ClaudeTools(builtin="stock", mcp=[questions]),
                    cwd=cwd,
                    add_dirs=[cwd, *toolchain_writable_paths()],
                    plugin_dirs=[lease_plugin_dir(cwd, plugin.name)],
                    sandbox=claude_worker_sandbox,
                    environment=worker_environment_for,
                    cli_path=actor_cli(
                        cwd,
                        context.concern_id,
                        context.actor.kind,
                        worker_environment_for,
                    ),
                    # Named from the tools rather than from the server,
                    # which is the half a stdio transport cannot answer:
                    # `server_tool_names` reports nothing for an external
                    # config because introspecting one means connecting to
                    # it, so reading the allowlist from there would empty
                    # it in silence and deny this worker its own questions.
                    allowed_tools=[
                        f"mcp__resolver__{tool.name}" for tool in actor_tools
                    ],
                    hooks=merge_hooks(
                        merge_hooks(
                            merge_hooks(
                                create_permission_hooks([cwd], []),
                                worker_policy_hooks(
                                    harness.declared_hooks,
                                    context.grants,
                                    CLAUDE_SEMANTICS,
                                    claude_worker_sandbox.enforcement(),
                                    relay,
                                ),
                            ),
                            create_git_inspection_hook(),
                        ),
                        context.hooks,
                    ),
                )
            return Codex(
                model=codex_model,
                tools=CodexTools(builtin="stock", mcp=[questions]),
                system_prompt=("Execute the persisted Lup resolver assignment."),
                cwd=cwd,
                sandbox_mode="workspace-write",
                sandbox=OuterContainer() if contained_actors else NoSandbox(),
                # Falls back to the program's own name, which is what this
                # field already defaults to. Claude's seam takes ``None``
                # for the same case instead, because its SDK searches for a
                # CLI that is often not on PATH and a name passed there
                # would skip the search rather than stand in for it.
                executable=actor_cli(
                    cwd,
                    context.concern_id,
                    context.actor.kind,
                    concern_environment,
                )
                or Path(actor_program),
                # An asking policy is what makes the app-server put this
                # worker's commands to the hooks below. Left at "never" a
                # Codex worker ran with the OS sandbox as its only floor,
                # because its generated plugin hook is not reached either.
                approval_policy="on-request",
                hooks=merge_hooks(
                    worker_policy_hooks(
                        harness.declared_hooks,
                        context.grants,
                        CODEX_SEMANTICS,
                        codex_worker_sandbox,
                        relay,
                    ),
                    context.hooks,
                ),
                environment=concern_environment,
                writable_roots=[cwd],
            )

        def reviewer_factory(context: ReviewerContext) -> Agent:
            # A reviewer takes the same mail every other actor does. It used
            # to take none, being the one kind whose recipe was handed a bare
            # path, so the actor best placed to use a late fact — a criterion
            # already settled elsewhere, a base that moved under the tree it
            # is judging — was the one nobody could tell.
            cwd = context.root
            # Named for the tree it opens rather than for a concern, which is
            # the one identifier a reviewer's context carries. The lease
            # directory is already named for what it holds, so this separates
            # two reviewers exactly where their leases differ.
            reviewing = cwd.name
            if adapter == "claude":
                reviewer_environment_for = isolated_claude_environment(
                    reviewer_environment, cwd
                )
                return Claude(
                    model=claude_model,
                    system_prompt=(
                        "Independently review the persisted resolver change."
                    ),
                    tools=ClaudeTools(
                        builtin=[
                            "Read",
                            "Glob",
                            "Grep",
                            "WebFetch",
                            "WebSearch",
                            "Bash",
                            "TaskOutput",
                            "TaskStop",
                        ]
                    ),
                    cwd=cwd,
                    add_dirs=[cwd],
                    environment=reviewer_environment_for,
                    # A reviewer is read-only by design, so its lease is
                    # the worker's with nothing writable rather than a
                    # table of its own: built from one call, the two cannot
                    # come to disagree about which checkouts exist.
                    cli_path=actor_cli(
                        cwd,
                        reviewing,
                        "reviewer",
                        reviewer_environment_for,
                        read_only=True,
                    ),
                    hooks=merge_hooks(
                        create_permission_hooks([], [cwd]), context.hooks
                    ),
                )
            return Codex(
                model=codex_model,
                tools=CodexTools(builtin=["WebSearch", "Bash"]),
                approval_policy="on-request",
                hooks=merge_hooks(create_permission_hooks([], [cwd]), context.hooks),
                system_prompt=("Independently review the persisted resolver change."),
                cwd=cwd,
                sandbox_mode="read-only",
                sandbox=OuterContainer() if contained_actors else NoSandbox(),
                executable=actor_cli(
                    cwd,
                    reviewing,
                    "reviewer",
                    reviewer_environment,
                    read_only=True,
                )
                or Path(actor_program),
                environment=reviewer_environment,
            )

        mailbox = QuestionMailbox(state_root / resolved_run_id)
        offer_flag_answers(mailbox, resolved_run_id, provided)
        for stale in inert_offers(mailbox):
            typer.echo(stale, err=True)
        core = ResolverCore(
            ResolverConfig(
                state_root=state_root,
                workspace=root,
                worktree_root=worktree_root,
                run_id=resolved_run_id,
                integration_branch=integration_branch(launcher, root, resolved_run_id),
                # The whole gate rather than part of it restated. Naming three
                # of its checks let a worker introduce an anti-pattern or
                # leave an unresolved note and still verify green — the two
                # rules most specific to this repository were the ones its own
                # output was not held to. `--since` scopes the note gate to
                # what this tree changed, because a concern's worktree holds
                # every sibling's notes and it has no lease on any of them.
                # The commit is named per verified tree rather than here: a
                # base written into this list is part of the composition every
                # resume is checked against, so a run could not resume itself
                # once its base moved, and leases cut from different commits
                # have no one answer to give it.
                verification_commands=[
                    VerificationCommand(
                        name="dev check",
                        arguments=["uv", "run", "lup-devtools", "dev", "check"],
                        base_option="--since",
                    ),
                ],
                max_parallel_workers=max_parallel_workers,
                recheck_standing_per_join=recheck_standing_per_join,
                # Both plugin trees are rendered from the catalogs a lease
                # edits, so nearly every join disagrees about them. Rendering
                # again is a second and settles it exactly; a merger choosing
                # between two stale renderings is minutes and a judgement it
                # has no basis for.
                regeneration_command=[
                    "uv",
                    "run",
                    "lup-devtools",
                    "harness",
                    "generate",
                    "all",
                ],
            ),
            resolver_spec,
            worker_factory,
            reviewer_factory,
            composition.invocation_renderer,
            launcher,
            observer=ConsoleResolverObserver(),
            worktree_preparer=FeatureWorktreePreparer(root),
            answer_wait_seconds=wait_seconds,
            adopt_config=adopt_config,
            environmental_fault=environmental_fault,
        )

        async def drive() -> None:
            if abort_reason is not None:
                aborted = core.abort(abort_reason)
                for record in aborted.cleanup:
                    typer.echo(
                        f"[abort] {record.action} {record.path}: {record.reason}"
                    )
                typer.echo(f"aborted {resolved_run_id}: {abort_reason}")
                return
            # Evidence offered to a run that does not exist yet seeds one
            # rather than being refused. What a human arrives with is the work
            # in their own words, and a refusal here made them write a note
            # into the tree to name a site the run would only read back.
            #
            # A run named explicitly is the one case that still refuses: the
            # id is a claim that the run exists, so seeding on a typo would
            # start a second run under the misspelling and lease a worktree
            # per concern before anyone read the line saying so.
            if admission is not None:
                if core.repository.exists():
                    report_admission(
                        await core.admit(admission), adapter, resolved_run_id
                    )
                    return
                refusal = missing_run_refusal(run_id, resolved_run_id)
                if refusal is not None:
                    raise typer.BadParameter(refusal)
                typer.echo(
                    f"No resolver run {resolved_run_id!r} yet; seeding one with "
                    "what was admitted, beside whatever notes the tree holds."
                )
            try:
                if core.repository.exists():
                    manifest = await core.resume_exclusive()
                else:
                    intake = scanned_intake(root)
                    for carried in intake.carried:
                        typer.echo(carried.describe())
                    for owned in intake.generated:
                        typer.echo(owned.describe())
                    comments = intake.actionable
                    open_issues = fetch_open_issues() if take_issues else []
                    for issue in open_issues:
                        typer.echo(
                            f"taking as evidence: {issue.reference()} {issue.title}"
                        )
                    note_paths = sorted({Path(comment.file) for comment in comments})
                    source = resolver_source_snapshot(
                        launcher,
                        root,
                        core.repository.root,
                        note_paths,
                    )
                    seeded = seed_request(source, comments, open_issues, admission)
                    if seeded is None:
                        typer.echo(
                            "No unresolved # lup: comments, and no open issues. "
                            "Seed a run with what you want done instead: "
                            '--admit "<the work, in your own words>".'
                        )
                        return
                    manifest = await core.run_exclusive(
                        await seeded.inventory(core.plan_inventory)
                    )
            except ResolverDrained as drained:
                # Exit zero: an operator asked for this and got it, which is
                # the command succeeding rather than the run failing.
                report_drained(drained, adapter, resolved_run_id)
                return
            except ResolverRegression as regression:
                report_regression(regression, adapter, resolved_run_id)
                raise typer.Exit(code=65)
            except ResolverAssemblyDeferred as deferred:
                report_deferred_assembly(deferred, adapter, resolved_run_id)
                return
            except ResolverAwaitingAnswers as parked:
                # Read back rather than trusting what the raise carried: the
                # run kept working after it, and the mailbox is authoritative
                # for anything still pending.
                recorded = core.repository.load() if core.repository.exists() else None
                report_awaiting(
                    parked,
                    adapter,
                    resolved_run_id,
                    [] if recorded is None else recorded.concerns,
                    [] if recorded is None else question_views(recorded, mailbox),
                )
                return
            typer.echo(f"Review branch: {manifest.review_branch}")
            for number in answer_issues(core.repository.load().concerns, manifest):
                typer.echo(f"commented on #{number}")
            typer.echo(manifest.model_dump_json(indent=2))

        def announce_host_wait(wait: HostWait) -> None:
            typer.echo(
                f"[resolve] the host refused ({wait.cause}); {wait.mode} "
                f"at {wait.retry_at.isoformat()}. The run remains active."
            )

        async with spawned_supervisor(
            supervisor or SupervisorSpawn(), resolved_run_id, adapter
        ):
            try:
                if abort_reason is not None or (
                    admission is not None and core.repository.exists()
                ):
                    await drive()
                else:
                    await drive_with_host_retries(
                        drive,
                        core.repository,
                        retries=host_retries,
                        retry_delay=partial(
                            host_retry_delay, retries=host_retries, backoff=host_backoff
                        ),
                        auth_probe_delay=auth_probe_delay,
                        needs_a_person=needs_a_person,
                        may_be_a_rotation=may_be_a_rotation,
                        announce=announce_host_wait,
                    )
            except ResolverEnvironmentFault as fault:
                report_environment_fault(fault, adapter, resolved_run_id)
                raise typer.Exit(code=75) from fault

    asyncio.run(execute())
