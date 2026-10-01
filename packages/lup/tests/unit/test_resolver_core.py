"""Deterministic resolver DAG, lease, state, and commit-authority tests."""

import asyncio
from collections import Counter
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
import pytest
from pydantic import BaseModel

from lup.harness.contracts import SkillInvocationRenderer
from lup.harness.models import ResolveSpec, SkillInvocation
from lup.harness.ownership import GeneratedArtifacts, OwnedArtifact
from lup.harness.process import (
    LaunchRequest,
    LocalProcessLauncher,
    ProcessLauncher,
)
from lup.execution.dag import DependencyGraphError
from lup.resolver.dag import ConcernGraph
from lup.resolver.join_desk import JoinDesk, JoinPlan, JoinTip
from lup.resolver.join_tools import (
    JoinReport,
    JoinStatusInput,
    LandParentInput,
    StartParentInput,
    create_join_tools,
)
from tests.unit.doubles import (
    IgnoredInterrupt,
    SilentStream,
    FailingLauncher,
    ScriptedLauncher,
    StaticTurn,
    identifiers,
    out,
    EngineAgent,
    agent_over,
    turn_result,
)
from lup.coordination.mailbox import (
    AnswerDoor,
    AnswerOffer,
    ParkRequest,
    RecordedAnswer,
)
from lup.coordination.questions import QuestionAnswer
from lup.resolver.mailbox import PendingQuestion, QuestionMailbox
from lup.policy.identity import ConcernAllowance
from lup.resolver.contracts import (
    ResolverAwaitingAnswers,
    ResolverDrained,
    ResolverEnvironmentFault,
    ResolverObserver,
    WorktreePreparer,
    settles_the_actor,
)
from lup.sessions.errors import ProviderTurnError, TurnFailure
from lup.resolver.core import (
    ASSEMBLY_QUESTION_ID,
    ResolverCore,
    approval_decisions,
    approval_question,
    resolver_config_digest,
)
from lup.channels.models import utc_now
from lup.resolver.run import ResolveRun, ResolverInvariantError
from lup.resolver.record import Journal, LeaseDriftEvent
from lup.resolver.models import (
    AdmissionRequest,
    AnswerBatch,
    AcceptanceCriterion,
    CarriedParent,
    Concern,
    ConcernInventory,
    ConcernOrigin,
    ConcernOutcome,
    ConcernEligibility,
    ConcernProgress,
    ConcernStatus,
    DependencyBase,
    DiffValidation,
    IntegrationRecord,
    JoinProgress,
    InventoryNote,
    MaterialQuestion,
    MergeReport,
    QuestionBatch,
    ResolveInventory,
    ResolveRequest,
    ResolverConfig,
    ResidualRuling,
    ResolvePhase,
    ResolveState,
    ReviewNote,
    ReviewReport,
    RunTally,
    SourceSnapshot,
    VerificationAcceptance,
    VerificationCommand,
    HunkDisposition,
    WorkerContext,
    WorkerReport,
    WritableRootLease,
    AllowanceRuling,
    ConcernApproval,
    allowance_question_id,
    asks_for_an_allowance,
)
from lup.resolver.orchestrator import (
    DependencyBaseBuilder,
    LeaseViolationError,
    WorktreeOrchestrator,
    WritableRootLeases,
)
from lup.resolver.state import (
    ResolverStateRepository,
    StateCorruptionError,
    StateTransitionError,
)
from lup.sessions.capabilities import SessionEngine
from lup.sessions.surface import Agent
from lup.sessions.composition import is_output_model
from lup.sessions.events import (
    SessionId,
    StartedTurn,
    TurnRequest,
)
from lup.types import JsonObject, JsonValue


def concern(
    identifier: str,
    dependencies: list[str] | None = None,
    *,
    approved: bool = True,
) -> Concern:
    return Concern(
        id=identifier,
        title=identifier.title(),
        spec=f"Resolve {identifier}",
        criteria=[AcceptanceCriterion(id=f"{identifier}-done", description="done")],
        dependencies=dependencies or [],
        integration_approved=approved,
    )


def seed_offer(core: ResolverCore, question_id: str, value: str) -> None:
    """Answer through the same door a `--answer` flag uses.

    Offers may precede their questions, so a whole run's decisions can be
    supplied before it starts — which is what stands in for a test broker.
    """
    core.mailbox.offer(
        AnswerOffer(
            run_id=core.config.run_id,
            question_id=question_id,
            value=value,
            door=AnswerDoor.FLAG,
            offered_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
    )


def seed_approvals(core: ResolverCore, concerns: list[Concern]) -> None:
    """Approve every concern, and approve assembling what they produce.

    Two decisions, not one: a concern gate authorizes the work, and the
    assembly gate authorizes merging the results into a review branch. A
    run seeded with only the first parks before integration, which is the
    point of that gate — so a test that means to reach COMPLETE says so by
    answering both.
    """
    for item in concerns:
        seed_offer(core, approval_question(item).id, ConcernApproval.APPROVE)
    seed_offer(core, ASSEMBLY_QUESTION_ID, ConcernApproval.APPROVE)


def worker_asks(mailbox: QuestionMailbox, run_id: str, asked: MaterialQuestion) -> None:
    """Queue a question exactly as a worker's ``queue_questions`` tool does.

    The fake worker cannot call an MCP tool, so it writes the record the
    tool would write. Everything downstream — promotion, folding, and the
    park — then runs against the real mailbox rather than a stub.
    """
    mailbox.queue(
        PendingQuestion(
            run_id=run_id,
            question=asked,
            asked_by=asked.concern_id,
            asked_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
    )


def dynamic_question(concern_id: str) -> MaterialQuestion:
    return MaterialQuestion(
        id=f"{concern_id}-dynamic",
        concern_id=concern_id,
        prompt="Choose the durable implementation",
        choices=["durable"],
        recommendation="durable",
    )


def resolve_spec() -> ResolveSpec:
    return ResolveSpec(
        id="resolve",
        worker_identity="resolver-worker",
        worker_skill=SkillInvocation(plugin="lup", skill="worker"),
        review_skill=SkillInvocation(plugin="lup", skill="review"),
        merge_skill=SkillInvocation(plugin="lup", skill="merge"),
    )


def test_dag_batches_roots_single_parent_and_multi_parent() -> None:
    graph = ConcernGraph(
        [
            concern("a"),
            concern("b"),
            concern("c", ["a"]),
            concern("d", ["a", "b"]),
            concern("e", ["c", "d"]),
        ]
    )

    assert [[item.id for item in batch] for batch in graph.topological_batches()] == [
        ["a", "b"],
        ["c", "d"],
        ["e"],
    ]
    assert [item.id for item in graph.ancestors("e")] == ["a", "b", "c", "d"]


def test_dag_rejects_missing_nodes_and_cycles_and_filters_unapproved_ancestry() -> None:
    with pytest.raises(DependencyGraphError, match="missing nodes"):
        ConcernGraph([concern("child", ["missing"])])
    with pytest.raises(DependencyGraphError, match="contains a cycle"):
        ConcernGraph([concern("a", ["b"]), concern("b", ["a"])])

    graph = ConcernGraph(
        [concern("parent", approved=False), concern("child", ["parent"])]
    )
    assert graph.approved() == []


def test_a_question_id_must_name_one_mailbox_file() -> None:
    with pytest.raises(ValueError, match="not a path-safe name"):
        MaterialQuestion(id="nested/id", concern_id="a", prompt="Decide?")


def test_a_question_cannot_offer_a_gate_its_concern_was_not_granted() -> None:
    """The unapprovable option is unplannable, not merely disclaimed.

    A choice whose text admits it needs a gate still gets picked, and the
    run still spends a lease discovering the worker is denied on arrival.
    """
    gated = MaterialQuestion(
        id="roster-home",
        concern_id="a",
        prompt="Where should the roster live?",
        choices=["Existing page", "A new published page"],
        allowances=[ConcernAllowance.NEW_DEVTOOLS_MODULE],
    )

    with pytest.raises(ValueError, match="does not request"):
        Concern(
            id="a",
            title="A",
            spec="Resolve a",
            criteria=[AcceptanceCriterion(id="a-done", description="done")],
            questions=[gated],
        )

    granted = Concern(
        id="a",
        title="A",
        spec="Resolve a",
        criteria=[AcceptanceCriterion(id="a-done", description="done")],
        questions=[gated],
        allowances=[ConcernAllowance.NEW_DEVTOOLS_MODULE],
    )
    assert granted.questions[0].allowances == [ConcernAllowance.NEW_DEVTOOLS_MODULE]


def test_a_missing_approval_answer_is_a_named_invariant() -> None:
    """Answers arrive per question, so absence must read as itself."""
    with pytest.raises(ResolverInvariantError, match="no persisted approval answer"):
        approval_decisions([concern("a")], AnswerBatch(run_id="run-1", answers=[]))


def test_persisted_approval_answers_filter_deferred_ancestry() -> None:
    concerns = [concern("parent"), concern("child", ["parent"])]
    answers = AnswerBatch(
        run_id="approval-decisions",
        answers=[
            QuestionAnswer(
                question_id=approval_question(concerns[0]).id,
                value=ConcernApproval.DEFER,
            ),
            QuestionAnswer(
                question_id=approval_question(concerns[1]).id,
                value=ConcernApproval.APPROVE,
            ),
        ],
    )

    decisions = approval_decisions(concerns, answers)

    assert decisions.directly_approved == ["child"]
    assert decisions.eligible == []


def test_leases_are_unique_and_bounded(tmp_path: Path) -> None:
    leases = WritableRootLeases(tmp_path / "agents")
    first = leases.acquire("a", "resolve/a")
    second = leases.acquire("b", "resolve/b")

    assert first.root != second.root
    leases.assert_path("a", first.root / "src" / "module.py")
    with pytest.raises(LeaseViolationError, match="outside"):
        leases.assert_path("a", second.root / "module.py")
    with pytest.raises(LeaseViolationError, match="already has"):
        leases.acquire("a", "resolve/a-2")


def test_dependency_bases_cover_root_single_and_semantic_join() -> None:
    source = SourceSnapshot(branch="feature", commit="source-sha")
    builder = DependencyBaseBuilder(source)
    root = builder.build(concern("root"), {})
    single = builder.build(concern("single", ["root"]), {"root": "root-sha"})

    assert root.commit == "source-sha"
    assert single.commit == "root-sha"
    with pytest.raises(ValueError, match="semantic multi-parent join"):
        builder.build(
            concern("join", ["left", "right"]),
            {"left": "left-sha", "right": "right-sha"},
        )
    joined = builder.build(
        concern("join", ["left", "right"]),
        {"left": "left-sha", "right": "right-sha"},
        joined_commit="join-sha",
    )
    assert joined == DependencyBase(
        concern_id="join",
        parent_concerns=["left", "right"],
        parent_commits=["left-sha", "right-sha"],
        commit="join-sha",
        semantic_join=True,
    )


def test_state_repository_records_and_replaces_an_acceptance(tmp_path: Path) -> None:
    """Accepting is recorded, and re-accepting a pair corrects rather than adds."""
    state = ResolveState(
        config_digest="config-sha",
        run_id="run-1",
        phase=ResolvePhase.INVENTORY,
        source=SourceSnapshot(branch="feature", commit="source-sha"),
        spec=resolve_spec(),
        concerns=[concern("a")],
        progress=[ConcernProgress(concern_id="a")],
    )
    repository = ResolverStateRepository(tmp_path, "run-1")
    repository.save(state)

    repository.accept(
        VerificationAcceptance(concern_id="a", verification="dev check", reason="first")
    )
    repository.accept(
        VerificationAcceptance(
            concern_id="a", verification="dev check", reason="second"
        )
    )
    repository.accept(
        VerificationAcceptance(concern_id="b", verification="dev check", reason="other")
    )
    recorded = repository.load().acceptances

    # One row per concern-and-verification pair, carrying the latest reason.
    assert [(row.concern_id, row.reason) for row in recorded] == [
        ("a", "second"),
        ("b", "other"),
    ]


def running_on(statuses: dict[str, ConcernStatus]) -> ResolveState:
    """A worker phase holding one concern per named status."""
    return ResolveState(
        config_digest="config-sha",
        run_id="run-1",
        phase=ResolvePhase.WORKERS,
        source=SourceSnapshot(branch="dev", commit="source-sha"),
        spec=resolve_spec(),
        concerns=[concern(name) for name in statuses],
        progress=[
            ConcernProgress(concern_id=name, status=status)
            for name, status in statuses.items()
        ],
    )


def test_a_host_fault_suspends_its_agent_in_the_spelling_the_turn_raises() -> None:
    """The fault has to be recognised before the executor renames it.

    A revoked credential leaves the provider as a `TurnError` and only becomes
    a `ResolverEnvironmentFault` one frame above the turn, in the executor that
    classifies it. The population sees the first spelling, so a judgement
    testing only for the second finishes the worker on exactly the failure that
    says nothing about its work — and the retry the fault exists for reattaches
    to nothing, because the conversation it wanted was recorded finished.

    Both halves of the classification, too: the adapter's flag and the message
    the executor's own classifier reads, since the flag is set where the
    exception is first caught and several layers re-wrap it on the way up.
    """
    flagged = ProviderTurnError(TurnFailure(message="401", environmental=True))
    by_message = ProviderTurnError(TurnFailure(message="429 quota exhausted"))
    refused = ProviderTurnError(TurnFailure(message="the tests do not pass"))

    assert settles_the_actor(flagged) is False
    assert settles_the_actor(by_message, lambda text: "429" in text) is False
    assert settles_the_actor(by_message) is True, "unclassified is the work's"
    assert settles_the_actor(refused, lambda text: "429" in text) is True

    # The three the executor raises in its own vocabulary still suspend, and
    # an ordinary failure still settles.
    assert settles_the_actor(ResolverAwaitingAnswers([], [])) is False
    assert settles_the_actor(ResolverDrained("operator asked", [])) is False
    assert settles_the_actor(ResolverEnvironmentFault("revoked", [])) is False
    assert settles_the_actor(RuntimeError("died")) is True


def test_a_concern_is_stamped_with_the_moment_it_settles(tmp_path: Path) -> None:
    """The sample every worker-phase rate is taken from.

    Written where the transition is applied rather than where the state is
    saved, because the in-memory copy is what the observer reads to draw its
    bar; stamped at the write boundary it would reach the file and never the
    surface watching it move.
    """
    run = ResolveRun(ResolverStateRepository(tmp_path, "run-1"), Journal(tmp_path))
    working = running_on({"a": ConcernStatus.RUNNING})

    landed = run.progress_state(working, ["a"], ConcernStatus.VERIFIED)

    assert landed.progress[0].settled_at is not None


def test_a_concern_moving_between_working_statuses_is_not_stamped(
    tmp_path: Path,
) -> None:
    """Only settling is a landing; the rest is a concern still in flight."""
    run = ResolveRun(ResolverStateRepository(tmp_path, "run-1"), Journal(tmp_path))
    working = running_on({"a": ConcernStatus.LEASED})

    moved = run.progress_state(working, ["a"], ConcernStatus.RUNNING)

    assert moved.progress[0].settled_at is None


def test_a_concern_that_settles_twice_keeps_the_moment_it_first_landed(
    tmp_path: Path,
) -> None:
    """Integration takes a verified concern back out and returns it settled.

    Re-stamping on the way back would move the sample to the integration
    phase's pace, which is a different rate measured over the same concern —
    and would drag every earlier interval along with it.
    """
    run = ResolveRun(ResolverStateRepository(tmp_path, "run-1"), Journal(tmp_path))
    working = running_on({"a": ConcernStatus.RUNNING})

    verified = run.progress_state(working, ["a"], ConcernStatus.VERIFIED)
    integrating = run.progress_state(verified, ["a"], ConcernStatus.INTEGRATING)
    integrated = run.progress_state(integrating, ["a"], ConcernStatus.INTEGRATED)

    assert integrated.progress[0].settled_at == verified.progress[0].settled_at


def test_a_refused_save_leaves_behind_no_belief_in_what_it_refused(
    tmp_path: Path,
) -> None:
    """What a run believes is what is durable, so a rejected write leaves none.

    The failure handler reads this back to record the phase to resume from. A
    candidate kept in memory after its save was refused therefore gets written
    a second time, fails the same validation, and the run reports the raise
    from inside its own handler rather than the one that stopped it — two
    identical tracebacks, and the real cause established by hand.
    """
    settled = ResolveState(
        config_digest="config-sha",
        run_id="run-1",
        phase=ResolvePhase.INVENTORY,
        source=SourceSnapshot(branch="feature", commit="source-sha"),
        spec=resolve_spec(),
        concerns=[concern("a")],
        progress=[ConcernProgress(concern_id="a", status=ConcernStatus.RETIRED)],
    )
    run = ResolveRun(ResolverStateRepository(tmp_path, "run-1"), Journal(tmp_path))
    run.persist(settled)

    with pytest.raises(StateTransitionError):
        run.persist(
            settled.model_copy(
                update={
                    "progress": [
                        ConcernProgress(concern_id="a", status=ConcernStatus.CLEANED)
                    ]
                }
            )
        )

    assert run.require().progress[0].status == ConcernStatus.RETIRED


def test_state_repository_adopts_a_moved_composition(tmp_path: Path) -> None:
    """Adoption writes the one field every other save path holds immutable."""
    state = ResolveState(
        config_digest="config-sha",
        run_id="run-1",
        phase=ResolvePhase.INVENTORY,
        source=SourceSnapshot(branch="feature", commit="source-sha"),
        spec=resolve_spec(),
        concerns=[concern("a")],
        progress=[ConcernProgress(concern_id="a")],
    )
    repository = ResolverStateRepository(tmp_path, "run-1")
    repository.save(state)
    moved = ResolverConfig(
        state_root=tmp_path / "state",
        workspace=tmp_path,
        worktree_root=tmp_path / "worktrees",
        run_id="run-1",
        integration_branch="resolve/run-1/review",
        verification_commands=[
            VerificationCommand(name="verify", arguments=["git", "diff"])
        ],
    )

    # save refuses the same change, which is what adoption exists to get past.
    with pytest.raises(StateTransitionError):
        repository.save(state.model_copy(update={"config_digest": "moved-sha"}))

    adopted = repository.adopt(moved, "moved-sha")

    assert adopted.config_digest == "moved-sha"
    assert adopted.config == moved
    assert repository.load().config_digest == "moved-sha"
    # Adoption re-stamps the composition and nothing else about the run.
    assert repository.load().concerns == state.concerns
    assert repository.load().source == state.source


def test_state_repository_writes_atomic_typed_projection_tree(tmp_path: Path) -> None:
    state = ResolveState(
        config_digest="config-sha",
        run_id="run-1",
        phase=ResolvePhase.INVENTORY,
        source=SourceSnapshot(branch="feature", commit="source-sha"),
        spec=resolve_spec(),
        concerns=[concern("a")],
        progress=[ConcernProgress(concern_id="a")],
    )
    repository = ResolverStateRepository(tmp_path, "run-1")
    repository.save(state)

    assert repository.load() == state
    assert sorted(path.name for path in repository.root.iterdir()) == [
        ".state.lock",
        "agents",
        "answers.json",
        "bases.json",
        "concerns.json",
        "integration",
        "leases.json",
        "questions.json",
        "reviews",
        "state.json",
    ]
    assert not list(repository.root.glob("*.tmp"))

    with pytest.raises(StateTransitionError, match="cannot move"):
        repository.save(state.model_copy(update={"phase": ResolvePhase.DAG}))
    questions = state.model_copy(update={"phase": ResolvePhase.QUESTIONS})
    repository.save(questions)
    with pytest.raises(StateTransitionError, match="cannot move"):
        repository.save(state)


def test_undecodable_persisted_state_raises_a_typed_recovery_error(
    tmp_path: Path,
) -> None:
    state = ResolveState(
        config_digest="config-sha",
        run_id="run-1",
        phase=ResolvePhase.INVENTORY,
        source=SourceSnapshot(branch="feature", commit="source-sha"),
        spec=resolve_spec(),
        concerns=[concern("a")],
        progress=[ConcernProgress(concern_id="a")],
    )
    repository = ResolverStateRepository(tmp_path, "run-1")
    repository.save(state)
    path = repository.root / "state.json"
    complete = path.read_text(encoding="utf-8")

    path.write_text(complete[: len(complete) // 2], encoding="utf-8")
    with pytest.raises(StateCorruptionError, match="restore the file"):
        repository.load()

    path.write_text('{"run_id": "run-1"}', encoding="utf-8")
    with pytest.raises(StateCorruptionError, match="remove the run directory"):
        repository.load()

    path.unlink()
    with pytest.raises(FileNotFoundError, match="does not exist"):
        repository.load()


def recording_launcher() -> ScriptedLauncher:
    """A worktree reporting one HEAD before its branch exists and one after."""
    return ScriptedLauncher(
        {
            "rev-parse HEAD": [out("base-sha\n"), out("created-sha\n")],
            "diff --name-only": out("src/module.py\n"),
            "diff --cached": out(code=1),
            "branch --show-current": out("resolve/a\n"),
        }
    )


def head_launcher(head: str, branch: str) -> ScriptedLauncher:
    """A worktree that exists, sits on one branch, and reports one HEAD."""
    return ScriptedLauncher(
        {
            "rev-parse HEAD": out(f"{head}\n"),
            "branch --show-current": out(f"{branch}\n"),
        }
    )


def successful_launcher() -> ScriptedLauncher:
    """Every probe succeeds."""
    return ScriptedLauncher()


def missing_branch_launcher() -> ScriptedLauncher:
    """Every probe fails, the way git answers about a branch that is not there."""
    return ScriptedLauncher(default=out(code=1))


def unused_session_factory() -> Agent:
    def refuse(resume: SessionId | None) -> SessionEngine:
        raise AssertionError(f"session factory should not be opened: {resume}")

    return EngineAgent(refuse)


class UnusedInvocationRenderer(SkillInvocationRenderer):
    def render(self, invocation: SkillInvocation) -> str:
        raise AssertionError(f"invocation should not be rendered: {invocation}")


type ResolverResponse = Callable[[Path, str], JsonObject]
type JoinDriver = Callable[[], Awaitable[None]]


def merger_that_keeps_everything(
    run_dir: Path, lease_root: Path, concern_id: str, launcher: ProcessLauncher
) -> JoinDriver:
    """A merger that lands every parent and accounts for whatever it is asked.

    Written as a driver rather than a canned report because the merger owns
    its own sequence: a double that only answers with a report would
    land nothing, and the checkpoint the run reads is written by the verbs,
    not by the answer. Dispositioning every candidate as kept is the
    simplest complete account — the point under test is that the gate is
    reached and satisfied, not what a real merger would decide.
    """
    tools = {
        tool.name: tool
        for tool in create_join_tools(
            run_dir, lease_root, concern_id, launcher=launcher
        )
    }

    async def drive() -> None:
        status = await tools["join_status"](JoinStatusInput())
        if status.drain_requested:
            return
        for tip in status.remaining:
            await tools["start_parent"](StartParentInput(commit=tip.commit))
            landing = await tools["land_parent"](
                LandParentInput(commit=tip.commit, summary=f"joined {tip.commit[:12]}")
            )
            if not landing.landed:
                landing = await tools["land_parent"](
                    LandParentInput(
                        commit=tip.commit,
                        summary=f"joined {tip.commit[:12]}",
                        dispositions=[
                            HunkDisposition(
                                path=candidate.path,
                                parent=candidate.parent,
                                fate="kept",
                                rationale="carried through the join",
                            )
                            for candidate in landing.unaccounted
                        ],
                    )
                )
            if landing.drain_requested:
                return

    return drive


def recording_worker_recipe(
    state_root: Path,
    launcher: ProcessLauncher,
    response: ResolverResponse,
    log: list[str],
) -> Callable[[WorkerContext], Agent]:
    """``worker_recipe``, for the tests that also read back every prompt."""

    def run_dir() -> Path:
        runs = sorted(path for path in state_root.iterdir() if path.is_dir())
        if len(runs) != 1:
            raise AssertionError(f"expected one run under {state_root}, found {runs}")
        return runs[0]

    def recipe(context: WorkerContext) -> Agent:
        return agent_over(
            PromptRecordingSession(
                context.root,
                response,
                log,
                merger_that_keeps_everything(
                    run_dir(), context.root, context.concern_id, launcher
                )
                if context.actor.kind == "merger"
                else None,
            )
        )

    return recipe


def merger_draining_after_one_parent(
    run_dir: Path, launcher: ProcessLauncher, response: ResolverResponse
) -> Callable[[WorkerContext], Agent]:
    """A merger that lands one parent, then finds the run asked to stop.

    The drain arrives mid-sequence rather than before the join, because
    that is the boundary worth pinning: one already waiting stops the join
    before a session is opened, which exercises a different path.
    """

    def recipe(context: WorkerContext) -> Agent:
        if context.actor.kind != "merger":
            return resolver_test_factory(context.root, response)
        tools = {
            tool.name: tool
            for tool in create_join_tools(
                run_dir, context.root, context.concern_id, launcher=launcher
            )
        }

        async def drive() -> None:
            status = await tools["join_status"](JoinStatusInput())
            first = status.remaining[0]
            await tools["start_parent"](StartParentInput(commit=first.commit))
            QuestionMailbox(run_dir).drain(
                ParkRequest(run_id=run_dir.name, reason="operator stopped it")
            )
            landed = await tools["land_parent"](
                LandParentInput(commit=first.commit, summary="joined the first")
            )
            assert landed.drain_requested, "the landing verb reports a waiting drain"

        return resolver_test_factory(context.root, response, drive)

    return recipe


class ResolverTestSession(SessionEngine):
    def __init__(
        self, root: Path, response: ResolverResponse, joining: JoinDriver | None = None
    ) -> None:
        self.root = root
        self.response = response
        self.joining = joining
        self.sequence = 0

    async def start[T: BaseModel | None](
        self, request: TurnRequest[T]
    ) -> StartedTurn[T]:
        output_type = request.output_type
        if not is_output_model(output_type):
            raise AssertionError("resolver turns must request typed output")
        self.sequence += 1
        if output_type is JoinReport and self.joining is not None:
            await self.joining()
            output = output_type.model_validate(
                {"plan": "in the order given", "summary": "joined every parent"}
            )
        else:
            output = output_type.model_validate(
                self.response(self.root, output_type.__name__)
            )
        result = turn_result(
            output,
            identifiers(f"resolver-{self.root.name}", f"turn-{self.sequence}"),
        )
        return StartedTurn[T](
            turn=StaticTurn(result),
            events=SilentStream(),
            interrupt=IgnoredInterrupt(),
        )


def resolver_test_factory(
    root: Path, response: ResolverResponse, joining: JoinDriver | None = None
) -> Agent:
    return agent_over(ResolverTestSession(root, response, joining))


def worker_recipe(
    state_root: Path,
    launcher: ProcessLauncher,
    response: ResolverResponse,
) -> Callable[[WorkerContext], Agent]:
    """The worker factory a test core gets, with a merger that drives its join.

    One recipe opens both a concern's worker and the merger that joins into
    it, and only the second is handed the verbs that land a parent — the
    same split the real factory makes, for the same reason.

    The run directory is found rather than named, because a test builds its
    core with the run id inline and there is exactly one run under a test's
    state root. Resolved at session time, when the directory exists.
    """

    def run_dir() -> Path:
        runs = sorted(path for path in state_root.iterdir() if path.is_dir())
        if len(runs) != 1:
            raise AssertionError(f"expected one run under {state_root}, found {runs}")
        return runs[0]

    def recipe(context: WorkerContext) -> Agent:
        return resolver_test_factory(
            context.root,
            response,
            merger_that_keeps_everything(
                run_dir(), context.root, context.concern_id, launcher
            )
            if context.actor.kind == "merger"
            else None,
        )

    return recipe


class LiteralInvocationRenderer(SkillInvocationRenderer):
    def render(self, invocation: SkillInvocation) -> str:
        return f"{invocation.plugin}:{invocation.skill}"


def two_note_request() -> ResolveRequest:
    """Evidence wide enough that a plan can quietly leave one note behind."""
    return ResolveRequest(
        source=SourceSnapshot(branch="feature", commit="source-sha"),
        notes=[
            InventoryNote(
                file=Path("src/module.py"),
                line=7,
                text="close the union",
                context="def render(self):  # lup: close the union",
            ),
            InventoryNote(
                file=Path("docs/guide.md"),
                line=1,
                text="this guide repeats itself",
                context="# Guide  <!-- lup: this guide repeats itself -->",
            ),
        ],
    )


def planning_core(tmp_path: Path, response: ResolverResponse) -> ResolverCore:
    """A core wired for planning turns only."""
    return ResolverCore(
        ResolverConfig(
            state_root=tmp_path / "state",
            workspace=tmp_path,
            worktree_root=tmp_path / "worktrees",
            run_id="coverage",
            integration_branch="resolve/coverage/review",
            verification_commands=[
                VerificationCommand(name="verify", arguments=["git", "diff"])
            ],
        ),
        resolve_spec(),
        lambda context: resolver_test_factory(context.root, response),
        lambda context: resolver_test_factory(context.root, response),
        LiteralInvocationRenderer(),
        recording_launcher(),
    )


def concern_referencing(indexes: list[int]) -> JsonObject:
    return {
        "id": f"concern-{'-'.join(str(index) for index in indexes)}",
        "title": "A concern",
        "spec": "Do the thing",
        "criteria": [{"id": "done", "description": "It is done"}],
        "evidence_indexes": [index for index in indexes],
    }


def plan_of(*concerns: JsonObject) -> JsonObject:
    return {"concerns": [concern for concern in concerns]}


SAID = "the relay must investigate before it answers"


@pytest.mark.asyncio
async def test_a_run_is_planned_from_words_with_no_note_written_first(
    tmp_path: Path,
) -> None:
    """How a human arrives: the work in their own words, nothing in the tree.

    A run seedable only from notes made them invent a note site for the
    planner to read back, which is a file edit standing in for a sentence.
    """
    core = planning_core(tmp_path, lambda *_: plan_of(concern_referencing([0])))

    inventory = await core.plan_inventory(
        ResolveRequest(
            source=SourceSnapshot(branch="feature", commit="source-sha"),
            statements=[SAID],
        )
    )

    assert [concern.id for concern in inventory.concerns] == ["concern-0"]
    assert inventory.concerns[0].notes == []
    assert inventory.concerns[0].evidence == SAID


@pytest.mark.asyncio
async def test_a_statement_and_a_note_reach_one_inventory(tmp_path: Path) -> None:
    """Positions run end to end, so one turn plans both kinds together."""
    request = two_note_request().model_copy(update={"statements": [SAID]})
    core = planning_core(
        tmp_path,
        lambda *_: plan_of(
            concern_referencing([0, 1]),
            concern_referencing([2]),
        ),
    )

    inventory = await core.plan_inventory(request)

    assert [
        ([note.line for note in concern.notes], concern.evidence)
        for concern in inventory.concerns
    ] == [([7, 1], ""), ([], SAID)]
    assert all(concern.eligible for concern in inventory.concerns)


@pytest.mark.asyncio
async def test_a_note_no_concern_claims_names_itself_and_stops_the_run(
    tmp_path: Path,
) -> None:
    """Coverage is the surviving invariant: an ignored note goes unresolved."""
    core = planning_core(tmp_path, lambda *_: plan_of(concern_referencing([0])))

    with pytest.raises(ResolverInvariantError, match=r"no concern references: \[1\]"):
        await core.plan_inventory(two_note_request())


@pytest.mark.asyncio
async def test_a_plan_that_ignored_a_note_is_corrected_rather_than_discarded(
    tmp_path: Path,
) -> None:
    """The complaint names the gap, so the next attempt can close it."""
    replies = iter(
        [
            plan_of(concern_referencing([0])),
            plan_of(concern_referencing([0]), concern_referencing([1])),
        ]
    )
    core = planning_core(tmp_path, lambda *_: next(replies))

    inventory = await core.plan_inventory(two_note_request())

    assert [note.line for concern in inventory.concerns for note in concern.notes] == [
        7,
        1,
    ]


@pytest.mark.asyncio
async def test_one_note_raising_two_issues_reaches_both_concerns(
    tmp_path: Path,
) -> None:
    """A note is not a unit of work — several concerns may answer one note."""
    request = ResolveRequest(
        source=SourceSnapshot(branch="feature", commit="source-sha"),
        notes=[
            InventoryNote(
                file=Path("src/module.py"),
                line=7,
                text="close the union, and write the principle into guidance",
                context="def render(self):  # lup: close the union",
            )
        ],
    )

    def reviewer_response(_root: Path, output_name: str) -> JsonObject:
        assert output_name == ConcernInventory.__name__
        return {
            "concerns": [
                {
                    "id": "close-the-union",
                    "title": "Close the union",
                    "spec": "Let each variant answer for itself",
                    "criteria": [{"id": "closed", "description": "No dispatch"}],
                    "evidence_indexes": [0],
                },
                {
                    "id": "write-the-principle",
                    "title": "Write the principle into guidance",
                    "spec": "State the convention where conventions live",
                    "criteria": [{"id": "stated", "description": "Guidance says it"}],
                    "evidence_indexes": [0],
                },
            ]
        }

    core = ResolverCore(
        ResolverConfig(
            state_root=tmp_path / "state",
            workspace=tmp_path,
            worktree_root=tmp_path / "worktrees",
            run_id="shared",
            integration_branch="resolve/shared/review",
            verification_commands=[
                VerificationCommand(name="verify", arguments=["git", "diff"])
            ],
        ),
        resolve_spec(),
        worker_recipe(tmp_path / "state", recording_launcher(), reviewer_response),
        lambda context: resolver_test_factory(context.root, reviewer_response),
        LiteralInvocationRenderer(),
        recording_launcher(),
    )

    inventory = await core.plan_inventory(request)

    shared = ReviewNote(
        file=Path("src/module.py"),
        line=7,
        text="close the union, and write the principle into guidance",
    )
    assert [concern.notes for concern in inventory.concerns] == [[shared], [shared]]


@pytest.mark.asyncio
async def test_inventory_planner_clusters_every_contextual_note_once(
    tmp_path: Path,
) -> None:
    request = ResolveRequest(
        source=SourceSnapshot(branch="feature", commit="source-sha"),
        notes=[
            InventoryNote(
                file=Path("src/module.py"),
                line=7,
                text="use the domain type",
                context="value: int  # lup: use the domain type",
            )
        ],
    )

    def reviewer_response(_root: Path, output_name: str) -> JsonObject:
        assert output_name == ConcernInventory.__name__
        return {
            "concerns": [
                {
                    "id": "domain-type",
                    "title": "Use the domain type",
                    "spec": "Represent the domain concept explicitly",
                    "criteria": [
                        {"id": "typed", "description": "Domain type is explicit"}
                    ],
                    "evidence_indexes": [0],
                }
            ]
        }

    core = ResolverCore(
        ResolverConfig(
            state_root=tmp_path / "state",
            workspace=tmp_path,
            worktree_root=tmp_path / "worktrees",
            run_id="inventory",
            integration_branch="resolve/inventory/review",
            verification_commands=[
                VerificationCommand(name="verify", arguments=["git", "diff"])
            ],
        ),
        resolve_spec(),
        worker_recipe(tmp_path / "state", recording_launcher(), reviewer_response),
        lambda context: resolver_test_factory(context.root, reviewer_response),
        LiteralInvocationRenderer(),
        recording_launcher(),
    )

    inventory = await core.plan_inventory(request)

    assert inventory.source == request.source
    assert [item.id for item in inventory.concerns] == ["domain-type"]
    assert inventory.concerns[0].eligible
    assert inventory.concerns[0].integration_approved
    # Notes are materialized from the request by position — context stripped,
    # content authoritative, never echoed by the planner.
    assert inventory.concerns[0].notes == [
        ReviewNote(file=Path("src/module.py"), line=7, text="use the domain type")
    ]


def join_launcher(marker_check_code: int) -> ScriptedLauncher:
    """Answer the join probes, reporting one unmerged path with chosen markers."""
    return ScriptedLauncher(
        {
            "diff --check": out("leftover marker", code=marker_check_code),
            "diff --name-only": out("src/module.py\n"),
            "status --porcelain": out("UU src/module.py\n"),
            "rev-parse HEAD": out("joined-sha\n"),
        }
    )


def ancestry_launcher(is_ancestor: bool) -> ScriptedLauncher:
    """Answer merge-base ancestry with a fixed verdict, recording every call."""
    return ScriptedLauncher(
        {"merge-base --is-ancestor": out(code=0 if is_ancestor else 1)}
    )


def merging_launcher(merge_head: str) -> ScriptedLauncher:
    """Report an open merge against a chosen parent, recording every call."""
    return ScriptedLauncher(
        {"rev-parse -q": out(f"{merge_head}\n") if merge_head else out(code=1)}
    )


def joined_lease(tmp_path: Path) -> WritableRootLease:
    """The integration lease an orchestrator driven directly operates on."""
    return WritableRootLeases(tmp_path / "agents").acquire("integration", "resolve/i")


def test_a_conflict_only_in_rendered_artifacts_is_settled_by_rendering(
    tmp_path: Path,
) -> None:
    """The generator decides these, so a merger choosing between them cannot.

    Every lease touching a catalog re-renders both plugin trees, so nearly
    every join disagrees about them — a join can carry hundreds of changed
    lines of `policy_data.py`, twice over. Rendering again takes a second and
    settles it exactly; putting it to a merger takes minutes and asks for a
    judgement about content that is nobody's to make.
    """
    launcher = ScriptedLauncher(
        {"diff --name-only": out(".claude/plugins/lup/policy_data.py\n")}
    )
    orchestrator = WorktreeOrchestrator(
        launcher, tmp_path, generated=rendered(".claude/plugins/lup/policy_data.py")
    )

    assert orchestrator.settle_generated(joined_lease(tmp_path), ["render", "all"])

    ran = [" ".join(call) for call in launcher.arguments]
    assert any("checkout --ours" in command for command in ran)
    assert "render all" in ran


def test_a_conflict_outside_the_rendered_set_is_left_to_the_merger(
    tmp_path: Path,
) -> None:
    """Rendering settles what a generator owns, and decides nothing else."""
    launcher = ScriptedLauncher({"diff --name-only": out("src/module.py\n")})
    orchestrator = WorktreeOrchestrator(
        launcher, tmp_path, generated=rendered(".claude/plugins/lup/policy_data.py")
    )

    assert not orchestrator.settle_generated(joined_lease(tmp_path), ["render", "all"])
    assert not any("render" in " ".join(call) for call in launcher.arguments)


def test_preparing_the_same_join_twice_leaves_the_open_merge_alone(
    tmp_path: Path,
) -> None:
    launcher = merging_launcher("parent-sha")
    orchestrator = WorktreeOrchestrator(launcher, tmp_path)
    lease = joined_lease(tmp_path)

    # The open merge is the one a turn was already resolving, so preparing it
    # again reports the conflict that turn was invoked over.
    assert orchestrator.prepare_join(lease, ["head-sha", "parent-sha"])
    assert ["git", "merge", "--no-commit", "--no-ff", "parent-sha"] not in (
        launcher.arguments
    )


def test_preparing_a_different_join_still_opens_the_merge(tmp_path: Path) -> None:
    launcher = merging_launcher("")
    orchestrator = WorktreeOrchestrator(launcher, tmp_path)
    lease = joined_lease(tmp_path)

    orchestrator.prepare_join(lease, ["head-sha", "parent-sha"])

    assert [
        "git",
        "merge",
        "--no-commit",
        "--no-ff",
        "parent-sha",
    ] in launcher.arguments


def test_containment_is_reported_from_merge_base(tmp_path: Path) -> None:
    lease = joined_lease(tmp_path)

    contained = ancestry_launcher(is_ancestor=True)
    assert WorktreeOrchestrator(contained, tmp_path).already_joined(lease, "sha")
    assert contained.arguments == [
        ["git", "merge-base", "--is-ancestor", "sha", "HEAD"]
    ]

    absent = ancestry_launcher(is_ancestor=False)
    assert not WorktreeOrchestrator(absent, tmp_path).already_joined(lease, "sha")


def test_the_join_tally_counts_the_parents_that_will_be_merged() -> None:
    """A bar has to be able to reach its own end.

    Counted from the outcomes, the total would include every concern holding
    a commit — each one that failed or retired still holding work, and each
    that rides inside a sibling and is therefore never merged on its own — so
    it could read 24 where 13 parents will be joined, standing at 3/24 having
    done 3 of 13.
    """
    state = integration_state(
        "tallied",
        Path("/tmp/tallied"),
        JoinProgress(
            joined=["one", "two", "three"],
            commit="j3",
            planned=[f"parent-{index}" for index in range(10)]
            + ["one", "two", "three"],
        ),
    )

    tally = state.tally()

    assert (tally.joined, tally.join_total) == (3, 13)
    assert "joins 3/13" in tally.concerns_line()


def test_a_parent_inside_another_is_carried_rather_than_merged(
    tmp_path: Path,
) -> None:
    """Concerns cut from their dependencies' commits stack, so parents nest.

    A run can hold many parents inside a sibling — one contained in five
    different siblings — and each join spent on such a parent costs a
    verification and can cost a merger turn to conclude that git has nothing
    to do.
    """
    launcher = LocalProcessLauncher()
    workspace = failure_leg_workspace(tmp_path, launcher)

    def git(*arguments: str) -> str:
        # Identity per invocation, never `git config`: a misbound command then
        # writes nothing, where a persisted setting lands in the shared config
        # every worktree of a real repository inherits (see `lup.devtools.gitguard`).
        status = launcher.launch(
            LaunchRequest(
                arguments=[
                    "git",
                    "-c",
                    "user.email=resolver@example.test",
                    "-c",
                    "user.name=Resolver Test",
                    *arguments,
                ],
                cwd=workspace,
            )
        )
        assert status.code == 0, status.stderr
        return status.stdout.strip()

    def commit_file(filename: str) -> str:
        (workspace / filename).write_text(f"{filename}\n", encoding="utf-8")
        git("add", filename)
        git("commit", "-m", f"add {filename}")
        return git("rev-parse", "HEAD")

    base = git("rev-parse", "HEAD")
    git("checkout", "-b", "stacked", base)
    dependency = commit_file("dependency.txt")
    dependent = commit_file("dependent.txt")
    git("checkout", "-b", "apart", base)
    unrelated = commit_file("unrelated.txt")

    def unused_actor(_root: Path, _output_name: str) -> JsonObject:
        raise AssertionError("planning a join spends no turn")

    core = failure_leg_core(
        tmp_path, workspace, launcher, "carried", unused_actor, unused_actor
    )
    lease = core.leases.acquire("integration", "resolve/carried/review")
    core.worktrees.create(lease, base)

    carried = core.joiner.carried_parents(lease, [dependency, dependent, unrelated])

    assert carried == [CarriedParent(commit=dependency, inside=dependent)]


def test_a_parent_is_credited_only_with_the_paths_it_wrote(tmp_path: Path) -> None:
    """A base that moved ahead was charged to every parent that forked before it.

    The inflation is identical for every parent, so it does not cancel out:
    it makes each one look like it touched everything, every pair of them
    look like they overlap, and the two filters built on this — the join
    ordering and the standing re-check — stop discriminating at all.
    """
    launcher = LocalProcessLauncher()
    workspace = failure_leg_workspace(tmp_path, launcher)

    def git(*arguments: str) -> str:
        # Identity per invocation, never `git config`: a misbound command then
        # writes nothing, where a persisted setting lands in the shared config
        # every worktree of a real repository inherits (see `lup.devtools.gitguard`).
        status = launcher.launch(
            LaunchRequest(
                arguments=[
                    "git",
                    "-c",
                    "user.email=resolver@example.test",
                    "-c",
                    "user.name=Resolver Test",
                    *arguments,
                ],
                cwd=workspace,
            )
        )
        assert status.code == 0, status.stderr
        return status.stdout.strip()

    def commit_file(filename: str) -> str:
        (workspace / filename).write_text(f"{filename}\n", encoding="utf-8")
        git("add", filename)
        git("commit", "-m", f"add {filename}")
        return git("rev-parse", "HEAD")

    fork = git("rev-parse", "HEAD")
    git("checkout", "-b", "lease", fork)
    parent = commit_file("lease.txt")
    git("checkout", "source")
    base = commit_file("upstream.txt")

    def unused_actor(_root: Path, _output_name: str) -> JsonObject:
        raise AssertionError("measuring what a parent wrote spends no turn")

    core = failure_leg_core(
        tmp_path, workspace, launcher, "authored", unused_actor, unused_actor
    )
    lease = core.leases.acquire("integration", "resolve/authored/review")
    core.worktrees.create(lease, base)

    assert [
        path.as_posix() for path in core.joiner.authored_by(lease, base, parent)
    ] == ["lease.txt"]
    # Measured from the base, the parent answers for the upstream file too.
    assert {
        path.as_posix() for path in core.worktrees.authored_between(lease, base, parent)
    } == {"lease.txt", "upstream.txt"}


def test_join_accepts_a_resolved_path_the_merger_left_unstaged(
    tmp_path: Path,
) -> None:
    orchestrator = WorktreeOrchestrator(join_launcher(marker_check_code=0), tmp_path)
    lease = joined_lease(tmp_path)

    assert orchestrator.commit_join(lease, "resolve: integrate") == "joined-sha"


def test_join_still_refuses_a_path_whose_content_carries_markers(
    tmp_path: Path,
) -> None:
    orchestrator = WorktreeOrchestrator(join_launcher(marker_check_code=2), tmp_path)
    lease = joined_lease(tmp_path)

    with pytest.raises(RuntimeError, match="invalid changes: leftover marker"):
        orchestrator.commit_join(lease, "resolve: integrate")


def test_only_orchestrator_creates_commits_and_reads_their_identity(
    tmp_path: Path,
) -> None:
    launcher = recording_launcher()
    orchestrator = WorktreeOrchestrator(launcher, tmp_path)
    leases = WritableRootLeases(tmp_path / "agents")
    lease = leases.acquire("a", "resolve/a")
    item = concern("a")

    orchestrator.create(lease, "base-sha")
    diff = orchestrator.validate_and_commit(
        item,
        WorkerReport(
            concern_id="a",
            changed=True,
            summary="updated module",
            files_changed=[Path("src/module.py")],
        ),
        lease,
        "base-sha",
        leases,
    )

    assert diff.valid
    assert diff.commit == "created-sha"
    assert [request.arguments for request in launcher.requests] == [
        ["git", "worktree", "add", "-b", "resolve/a", str(lease.root), "base-sha"],
        ["git", "branch", "--show-current"],
        ["git", "rev-parse", "HEAD"],
        ["git", "add", "-N", "."],
        ["git", "diff", "--name-only", "base-sha"],
        ["git", "diff", "--check", "base-sha"],
        ["git", "add", "-A"],
        ["git", "diff", "--cached", "--quiet", "HEAD"],
        ["git", "commit", "-m", "resolve: A"],
        ["git", "rev-parse", "HEAD"],
    ]


def moved_head_launcher(contains_base: bool, clean_tree: bool) -> ScriptedLauncher:
    """A lease whose HEAD has moved off the base this check was handed."""
    return ScriptedLauncher(
        {
            "rev-parse HEAD": [out("moved-sha\n"), out("committed-sha\n")],
            "branch --show-current": out("resolve/a\n"),
            "merge-base --is-ancestor": out(code=0 if contains_base else 1),
            "diff --name-only": out("src/module.py\n"),
            "diff --cached": out(code=0 if clean_tree else 1),
        }
    )


def validate_moved(launcher: ScriptedLauncher, tmp_path: Path) -> DiffValidation:
    """Validate one worker's report against a lease whose HEAD has moved."""
    leases = WritableRootLeases(tmp_path / "agents")
    lease = leases.acquire("a", "resolve/a")
    return WorktreeOrchestrator(launcher, tmp_path).validate_and_commit(
        concern("a"),
        WorkerReport(
            concern_id="a",
            changed=True,
            summary="updated module",
            files_changed=[Path("src/module.py")],
        ),
        lease,
        "base-sha",
        leases,
    )


def test_a_lease_that_advanced_past_its_base_is_still_measured_from_it(
    tmp_path: Path,
) -> None:
    """A base the check carried while the branch moved on is not a rewrite.

    The base is still in the lease's history, so everything the diff
    measures from it is intact — failing here spent a concern on the run's
    own bookkeeping.
    """
    diff = validate_moved(
        moved_head_launcher(contains_base=True, clean_tree=False), tmp_path
    )

    assert diff.valid
    assert diff.commit == "committed-sha"


def test_a_lease_that_lost_its_base_names_the_authority_that_changed(
    tmp_path: Path,
) -> None:
    """A stale base and a rewritten history each get a verdict of their own."""
    diff = validate_moved(
        moved_head_launcher(contains_base=False, clean_tree=True), tmp_path
    )

    assert not diff.valid
    assert diff.reason == (
        "worker changed commit authority: base-sha is no longer in the lease's history"
    )


def test_work_already_committed_in_the_lease_is_accepted_where_it_sits(
    tmp_path: Path,
) -> None:
    """`git commit` refuses an empty commit, and the work is present anyway."""
    launcher = moved_head_launcher(contains_base=True, clean_tree=True)

    diff = validate_moved(launcher, tmp_path)

    assert diff.valid
    assert diff.commit == "moved-sha"
    assert ["git", "commit", "-m", "resolve: A"] not in launcher.arguments


@pytest.mark.asyncio
async def test_failed_integration_verification_is_not_marked_successful(
    tmp_path: Path,
) -> None:
    run_id = "verification-failure"
    integration_root = tmp_path / "integration"
    integration = IntegrationRecord(
        branch="resolve/review",
        worktree=integration_root,
        concerns=["a"],
        commit="integration-sha",
        completed=False,
    )
    state = ResolveState(
        config_digest="config-sha",
        run_id=run_id,
        phase=ResolvePhase.INTEGRATION,
        source=SourceSnapshot(branch="feature", commit="source-sha"),
        spec=resolve_spec(),
        concerns=[concern("a")],
        progress=[ConcernProgress(concern_id="a", status=ConcernStatus.INTEGRATING)],
        leases=[
            WritableRootLease(
                concern_id="integration",
                root=integration_root,
                branch="resolve/review",
            )
        ],
        outcomes=[
            ConcernOutcome(
                concern_id="a",
                branch="resolve/a",
                commit="a-sha",
                verified=True,
            )
        ],
        integration=integration,
    )
    config = ResolverConfig(
        state_root=tmp_path / "state",
        workspace=tmp_path,
        worktree_root=tmp_path / "worktrees",
        run_id=run_id,
        integration_branch="resolve/review",
        verification_commands=[VerificationCommand(name="tests", arguments=["pytest"])],
    )
    core = ResolverCore(
        config,
        resolve_spec(),
        lambda _cwd: unused_session_factory(),
        lambda _cwd: unused_session_factory(),
        UnusedInvocationRenderer(),
        FailingLauncher(),
    )
    core.persist(state)

    with pytest.raises(ResolverInvariantError, match="verification failed"):
        await core.integrate(state, state.outcomes)

    persisted = core.repository.load()
    assert persisted.phase == ResolvePhase.VERIFICATION
    assert persisted.integration is not None
    assert not persisted.integration.completed
    assert [record.passed for record in persisted.verification] == [False]
    assert [outcome.integrated for outcome in persisted.outcomes] == [False]


@pytest.mark.asyncio
async def test_a_refused_resume_names_what_moved_and_how_to_recover(
    tmp_path: Path,
) -> None:
    """The run holding the most answers is the one that hits this.

    Parking exposes a defect and fixing it moves the gate under the parked
    run, so the message has to say which input moved and that aborting is
    the way out — neither was recoverable from "does not match".
    """
    run_id = "digest-drift"

    def build_core(command: str) -> ResolverCore:
        return ResolverCore(
            ResolverConfig(
                state_root=tmp_path / "state",
                workspace=tmp_path,
                worktree_root=tmp_path / "worktrees",
                run_id=run_id,
                integration_branch="resolve/review",
                verification_commands=[
                    VerificationCommand(name="gate", arguments=[command])
                ],
            ),
            resolve_spec(),
            lambda _cwd: unused_session_factory(),
            lambda _cwd: unused_session_factory(),
            UnusedInvocationRenderer(),
            FailingLauncher(),
        )

    parked = build_core("check-at-park")
    parked.persist(
        ResolveState(
            config_digest=resolver_config_digest(parked.config),
            run_id=run_id,
            phase=ResolvePhase.INVENTORY,
            source=SourceSnapshot(branch="feature", commit="source-sha"),
            spec=resolve_spec(),
            concerns=[concern("a")],
            progress=[ConcernProgress(concern_id="a")],
        )
    )

    with pytest.raises(ResolverInvariantError) as refused:
        await build_core("check-after-the-fix").resume()

    message = str(refused.value)
    assert "configuration" in message
    assert "--abort" in message
    # The inputs that did not move are not blamed for the one that did.
    assert "run id" not in message
    assert "specification" not in message


def test_illegal_per_concern_transition_is_rejected(tmp_path: Path) -> None:
    state = ResolveState(
        config_digest="config-sha",
        run_id="illegal-progress",
        phase=ResolvePhase.INVENTORY,
        source=SourceSnapshot(branch="feature", commit="source-sha"),
        spec=resolve_spec(),
        concerns=[concern("a")],
        progress=[ConcernProgress(concern_id="a")],
    )
    repository = ResolverStateRepository(tmp_path, state.run_id)
    repository.save(state)

    with pytest.raises(StateTransitionError, match="cannot move"):
        repository.save(
            state.model_copy(
                update={
                    "progress": [
                        ConcernProgress(concern_id="a", status=ConcernStatus.INTEGRATED)
                    ]
                }
            )
        )


def test_interrupted_concern_returns_to_persisted_lease_boundary(
    tmp_path: Path,
) -> None:
    run_id = "interrupted"
    lease = WritableRootLease(
        concern_id="a",
        root=tmp_path / "worktrees" / "a",
        branch="resolve/interrupted/a",
    )
    state = ResolveState(
        config_digest="config-sha",
        run_id=run_id,
        phase=ResolvePhase.WORKERS,
        source=SourceSnapshot(branch="feature", commit="source-sha"),
        spec=resolve_spec(),
        concerns=[concern("a")],
        progress=[ConcernProgress(concern_id="a", status=ConcernStatus.REVIEWING)],
        leases=[lease],
        bases=[
            DependencyBase(
                concern_id="a",
                parent_concerns=[],
                parent_commits=[],
                commit="source-sha",
            )
        ],
    )
    core = ResolverCore(
        ResolverConfig(
            state_root=tmp_path / "state",
            workspace=tmp_path,
            worktree_root=tmp_path / "worktrees",
            run_id=run_id,
            integration_branch="resolve/interrupted/review",
            verification_commands=[
                VerificationCommand(name="tests", arguments=["pytest"])
            ],
        ),
        resolve_spec(),
        lambda _cwd: unused_session_factory(),
        lambda _cwd: unused_session_factory(),
        UnusedInvocationRenderer(),
        missing_branch_launcher(),
    )
    core.persist(state)

    core.restore_leases(state)

    persisted = core.repository.load()
    assert persisted.progress == [
        ConcernProgress(
            concern_id="a",
            status=ConcernStatus.LEASED,
            reason="retry lease restored",
        )
    ]


def integration_state(
    run_id: str, tmp_path: Path, progress: JoinProgress
) -> ResolveState:
    """A run parked with an integration lease and nothing else in flight."""
    lease = WritableRootLease(
        concern_id="integration",
        root=tmp_path / "worktrees" / "integration",
        branch=f"resolve/{run_id}/review",
    )
    return ResolveState(
        config_digest="config-sha",
        run_id=run_id,
        phase=ResolvePhase.INTEGRATION,
        source=SourceSnapshot(branch="feature", commit="source-sha"),
        spec=resolve_spec(),
        concerns=[concern("a")],
        progress=[ConcernProgress(concern_id="a", status=ConcernStatus.INTEGRATING)],
        leases=[lease],
        join_progress=progress,
    )


def test_a_resume_partway_through_integration_keeps_the_joins_it_built(
    tmp_path: Path,
) -> None:
    """The joins already committed are progress, not a failed attempt.

    Before join progress had a field, an integration that parked mid-sequence
    had no `IntegrationRecord` — that is written only once every join lands —
    so the expected commit fell back to the run's source and the restore
    discarded every join already built. Six were thrown away in one observed
    run, and the same semantic question came back under a fresh id because
    the whole integration restarted beneath it.
    """
    run_id = "partway"
    state = integration_state(
        run_id, tmp_path, JoinProgress(joined=["p1", "p2"], commit="j2")
    )
    core = ResolverCore(
        ResolverConfig(
            state_root=tmp_path / "state",
            workspace=tmp_path,
            worktree_root=tmp_path / "worktrees",
            run_id=run_id,
            integration_branch=f"resolve/{run_id}/review",
            verification_commands=[
                VerificationCommand(name="tests", arguments=["pytest"])
            ],
        ),
        resolve_spec(),
        lambda _cwd: unused_session_factory(),
        lambda _cwd: unused_session_factory(),
        UnusedInvocationRenderer(),
        head_launcher("j2", f"resolve/{run_id}/review"),
    )
    core.persist(state)

    core.restore_leases(state)

    assert core.repository.load().join_progress == JoinProgress(
        joined=["p1", "p2"], commit="j2"
    )


def test_a_base_moves_onto_the_commit_that_cleared_its_notes(
    tmp_path: Path,
) -> None:
    """The resolver must not raise an invariant it violated itself.

    The orchestrator strips a concern's notes as a commit of its own, so a
    concern that never verified has HEAD at that clearance while its recorded
    base sits behind it. Restoring then raised `persisted commit changed`,
    with no CLI operation able to repair it, and the worktree had to be reset
    by hand.
    """
    run_id = "cleared"
    base = DependencyBase(
        concern_id="a", parent_concerns=[], parent_commits=[], commit="base-sha"
    )
    core = ResolverCore(
        ResolverConfig(
            state_root=tmp_path / "state",
            workspace=tmp_path,
            worktree_root=tmp_path / "worktrees",
            run_id=run_id,
            integration_branch=f"resolve/{run_id}/review",
            verification_commands=[
                VerificationCommand(name="tests", arguments=["pytest"])
            ],
        ),
        resolve_spec(),
        lambda _cwd: unused_session_factory(),
        lambda _cwd: unused_session_factory(),
        UnusedInvocationRenderer(),
        missing_branch_launcher(),
    )
    core.persist(
        ResolveState(
            config_digest="config-sha",
            run_id=run_id,
            phase=ResolvePhase.WORKERS,
            source=SourceSnapshot(branch="feature", commit="source-sha"),
            spec=resolve_spec(),
            concerns=[concern("a")],
            progress=[ConcernProgress(concern_id="a")],
            bases=[base],
        )
    )

    moved = asyncio.run(core.run_state.record_note_clearance(base, "clearance-sha"))

    assert moved.commit == "clearance-sha"
    assert core.repository.load().bases == [moved]


def test_a_retried_concern_adopts_the_base_its_own_clearance_advanced(
    tmp_path: Path,
) -> None:
    """A resumed concern re-derives the base its clearance already moved past.

    `record_note_clearance` advances a recorded base by design, so a concern
    retried after an interruption offers the pre-clearance commit again.
    Reading that as the base changing would fail every concern with a note to
    clear — which is every concern an inventory finds — so only admitted
    concerns, whose clearance commits nothing, would survive a resume.
    """
    run_id = "retried"
    derived = DependencyBase(
        concern_id="a", parent_concerns=[], parent_commits=[], commit="base-sha"
    )
    cleared = derived.model_copy(update={"commit": "clearance-sha"})
    core = ResolverCore(
        ResolverConfig(
            state_root=tmp_path / "state",
            workspace=tmp_path,
            worktree_root=tmp_path / "worktrees",
            run_id=run_id,
            integration_branch=f"resolve/{run_id}/review",
            verification_commands=[
                VerificationCommand(name="tests", arguments=["pytest"])
            ],
        ),
        resolve_spec(),
        lambda _cwd: unused_session_factory(),
        lambda _cwd: unused_session_factory(),
        UnusedInvocationRenderer(),
        missing_branch_launcher(),
    )
    core.persist(
        ResolveState(
            config_digest="config-sha",
            run_id=run_id,
            phase=ResolvePhase.WORKERS,
            source=SourceSnapshot(branch="feature", commit="source-sha"),
            spec=resolve_spec(),
            concerns=[concern("a")],
            progress=[ConcernProgress(concern_id="a")],
            bases=[cleared],
        )
    )

    adopted = asyncio.run(core.run_state.record_dependency_base(derived))

    assert adopted == cleared
    assert core.repository.load().bases == [cleared]

    # A base whose dependency shape moved is still the invariant it was
    # written to catch, and the commit is what clearance is allowed to move.
    with pytest.raises(ResolverInvariantError, match="dependency base changed"):
        asyncio.run(
            core.run_state.record_dependency_base(
                derived.model_copy(update={"parent_commits": ["other-sha"]})
            )
        )


def test_releasing_a_run_cleans_concern_branches_and_keeps_the_review_one(
    tmp_path: Path,
) -> None:
    """Cleanup is unconditional; there is no decision left to spend a park on."""
    run_id = "acceptance"
    concern_lease = WritableRootLease(
        concern_id="a",
        root=tmp_path / "worktrees" / "a",
        branch="resolve/acceptance/a",
    )
    integration_lease = WritableRootLease(
        concern_id="integration",
        root=tmp_path / "worktrees" / "integration",
        branch="resolve/acceptance/review",
    )
    state = ResolveState(
        config_digest="config-sha",
        run_id=run_id,
        phase=ResolvePhase.VERIFICATION,
        source=SourceSnapshot(branch="feature", commit="source-sha"),
        spec=resolve_spec(),
        concerns=[concern("a")],
        progress=[ConcernProgress(concern_id="a", status=ConcernStatus.INTEGRATED)],
        leases=[concern_lease, integration_lease],
        outcomes=[
            ConcernOutcome(
                concern_id="a",
                branch=concern_lease.branch,
                commit="a-sha",
                verified=True,
                integrated=True,
            )
        ],
        integration=IntegrationRecord(
            branch=integration_lease.branch,
            worktree=integration_lease.root,
            concerns=["a"],
            commit="integration-sha",
            completed=True,
        ),
    )
    core = ResolverCore(
        ResolverConfig(
            state_root=tmp_path / "state",
            workspace=tmp_path,
            worktree_root=tmp_path / "worktrees",
            run_id=run_id,
            integration_branch=integration_lease.branch,
            verification_commands=[
                VerificationCommand(name="tests", arguments=["pytest"])
            ],
        ),
        resolve_spec(),
        lambda _cwd: unused_session_factory(),
        lambda _cwd: unused_session_factory(),
        UnusedInvocationRenderer(),
        successful_launcher(),
    )
    core.persist(state)

    core.release(state)

    persisted = core.repository.load()
    assert persisted.phase == ResolvePhase.COMPLETE
    assert all(not lease.active for lease in persisted.leases)
    assert persisted.progress[0].status == ConcernStatus.CLEANED
    assert [record.action for record in persisted.cleanup] == ["removed", "retained"]


def test_releasing_a_run_keeps_the_decision_a_retired_concern_carries(
    tmp_path: Path,
) -> None:
    """Retiring settles the decision; it does not hand back the worktree.

    So a retired concern still holds its lease when cleanup arrives, and
    cleanup moving it on would land it in a status the transition table
    declares unreachable, because retiring is a human's word and nothing
    overwrites it. That would crash the run at its last step, with every
    concern integrated and re-checked and most worktrees already removed.
    """
    run_id = "acceptance"
    retired_lease = WritableRootLease(
        concern_id="r",
        root=tmp_path / "worktrees" / "r",
        branch="resolve/acceptance/r",
    )
    integration_lease = WritableRootLease(
        concern_id="integration",
        root=tmp_path / "worktrees" / "integration",
        branch="resolve/acceptance/review",
    )
    state = ResolveState(
        config_digest="config-sha",
        run_id=run_id,
        phase=ResolvePhase.VERIFICATION,
        source=SourceSnapshot(branch="feature", commit="source-sha"),
        spec=resolve_spec(),
        concerns=[concern("r")],
        progress=[ConcernProgress(concern_id="r", status=ConcernStatus.RETIRED)],
        leases=[retired_lease, integration_lease],
        integration=IntegrationRecord(
            branch=integration_lease.branch,
            worktree=integration_lease.root,
            concerns=[],
            commit="integration-sha",
            completed=True,
        ),
    )
    core = ResolverCore(
        ResolverConfig(
            state_root=tmp_path / "state",
            workspace=tmp_path,
            worktree_root=tmp_path / "worktrees",
            run_id=run_id,
            integration_branch=integration_lease.branch,
            verification_commands=[
                VerificationCommand(name="tests", arguments=["pytest"])
            ],
        ),
        resolve_spec(),
        lambda _cwd: unused_session_factory(),
        lambda _cwd: unused_session_factory(),
        UnusedInvocationRenderer(),
        successful_launcher(),
    )
    core.persist(state)

    core.release(state)

    persisted = core.repository.load()
    assert persisted.phase == ResolvePhase.COMPLETE
    # The worktree still went, and the record of that is where it belongs.
    assert persisted.progress[0].status == ConcernStatus.RETIRED
    assert [record.action for record in persisted.cleanup] == ["removed", "retained"]


@pytest.mark.asyncio
async def test_complete_resolver_lifecycle_uses_real_isolated_git_worktrees(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "source"
    workspace.mkdir()
    launcher = LocalProcessLauncher()

    def git(*arguments: str, cwd: Path = workspace) -> str:
        # Identity per invocation, never `git config` — see `lup.devtools.gitguard`.
        status = launcher.launch(
            LaunchRequest(
                arguments=[
                    "git",
                    "-c",
                    "user.email=resolver@example.test",
                    "-c",
                    "user.name=Resolver Test",
                    *arguments,
                ],
                cwd=cwd,
            )
        )
        if status.code != 0:
            raise AssertionError(status.stderr)
        return status.stdout.strip()

    git("init", "-b", "source")
    (workspace / "README.md").write_text("base\n", encoding="utf-8")
    git("add", "README.md")
    git("commit", "-m", "base")
    source_commit = git("rev-parse", "HEAD")
    source_branch = git("branch", "--show-current")

    worker_calls: Counter[str] = Counter()
    review_calls: Counter[str] = Counter()

    def worker_response(root: Path, output_name: str) -> JsonObject:
        identifier = root.name
        if output_name == MergeReport.__name__:
            return {"completed": True, "summary": "semantic join reviewed"}
        if output_name != WorkerReport.__name__:
            raise AssertionError(output_name)
        call = worker_calls[identifier] + 1
        worker_calls[identifier] = call
        if identifier == "b" and call == 1:
            worker_asks(
                QuestionMailbox(tmp_path / "state" / run_id),
                run_id,
                dynamic_question("b"),
            )
        relative = Path(f"{identifier}.txt")
        (root / relative).write_text(f"{identifier} round {call}\n", encoding="utf-8")
        return {
            "concern_id": identifier,
            "changed": True,
            "summary": f"implemented {identifier}",
            "files_changed": [relative.as_posix()],
        }

    def reviewer_response(root: Path, output_name: str) -> JsonObject:
        if output_name != ReviewReport.__name__:
            raise AssertionError(output_name)
        identifier = root.name
        call = review_calls[identifier] + 1
        review_calls[identifier] = call
        if identifier == "a" and call == 1:
            return {
                "concern_id": "a",
                "accepted": False,
                "generalized": False,
                "reason": "one revision required",
                "residual": ["revise the implementation"],
            }
        return {
            "concern_id": identifier,
            "accepted": True,
            "generalized": True,
            "reason": "criteria met",
            "criteria_met": [f"{identifier}-done"],
        }

    run_id = "complete-lifecycle"
    core = ResolverCore(
        ResolverConfig(
            state_root=tmp_path / "state",
            workspace=workspace,
            worktree_root=tmp_path / "resolver-worktrees",
            run_id=run_id,
            integration_branch=f"resolve/{run_id}/review",
            verification_commands=[
                VerificationCommand(
                    name="combined-diff", arguments=["git", "diff", "--check", "HEAD"]
                )
            ],
        ),
        resolve_spec(),
        worker_recipe(tmp_path / "state", launcher, worker_response),
        lambda context: resolver_test_factory(context.root, reviewer_response),
        LiteralInvocationRenderer(),
        launcher,
    )
    initial_question = MaterialQuestion(
        id="a-initial",
        concern_id="a",
        prompt="Confirm integration",
        choices=["yes"],
        recommendation="yes",
    )
    inventory = ResolveInventory(
        source=SourceSnapshot(branch=source_branch, commit=source_commit),
        concerns=[
            concern("a").model_copy(update={"questions": [initial_question]}),
            concern("b"),
            concern("c", ["a", "b"]),
        ],
    )

    seed_approvals(core, inventory.concerns)
    seed_offer(core, "a-initial", "yes")
    seed_offer(core, "b-dynamic", "durable")

    manifest = await core.run(inventory)

    assert all(record.passed for record in manifest.verification)
    assert all(outcome.verified for outcome in manifest.outcomes)
    assert {outcome.concern_id for outcome in manifest.outcomes} == {"a", "b", "c"}
    assert worker_calls == {"a": 2, "b": 1, "c": 1}
    assert {item.question.id for item in core.mailbox.questions()} >= {
        "a-initial",
        "b-dynamic",
    }
    assert git("branch", "--show-current") == source_branch
    assert git("rev-parse", "HEAD") == source_commit

    # The population record, which is what an outside door reads. It was
    # empty for every resolver run: the run drove its own sessions, so
    # nothing ever announced an agent, and `resolve actors` answered from a
    # full scan of a journal that reaches tens of megabytes instead.
    members = {member.address: member for member in core.actors.live()}
    assert {"worker:a#2", "worker:b#1", "worker:c#1"} <= set(members)
    # One member per worker, not one per round: a's second round is the
    # agent that took its first.
    assert members["worker:a#2"].actor.round == 2
    assert members["worker:a#2"].running is False
    assert members["worker:a#2"].summary == "verified in 2 rounds"
    # Every kind the run opens, not only the writing ones.
    assert {"reviewer-a", "reviewer-b", "reviewer-c", "merger-c"} <= {
        member.actor.conversation() for member in core.actors.live()
    }
    # And it resolves the addresses it prints, from the record alone.
    assert core.actors.reaching("worker:a#1") == members["worker:a#2"].actor
    # A bare concern id is answered to by every member working that concern —
    # its worker and its reviewer are two members of one id — so it reaches one
    # of them and the roster prefers whoever is still live. Naming the exact
    # member is what the qualified address is for.
    reached = core.actors.reaching("b")
    assert reached is not None and reached.id == "b"
    assert core.actors.reaching("worker:b#1") == members["worker:b#1"].actor

    assert [record.action for record in manifest.cleanup] == [
        "removed",
        "removed",
        "removed",
        "retained",
    ]
    assert (tmp_path / "resolver-worktrees" / "integration").exists()
    for identifier in ("a", "b", "c"):
        branch = f"refs/heads/resolve/{run_id}/{identifier}"
        status = launcher.launch(
            LaunchRequest(
                arguments=["git", "show-ref", "--verify", "--quiet", branch],
                cwd=workspace,
            )
        )
        assert status.code != 0


def test_persist_clamps_phase_to_the_recorded_high_water_mark(tmp_path: Path) -> None:
    run_id = "phase-clamp"
    state = ResolveState(
        config_digest="config-sha",
        run_id=run_id,
        phase=ResolvePhase.REVIEW,
        source=SourceSnapshot(branch="feature", commit="source-sha"),
        spec=resolve_spec(),
        concerns=[concern("a")],
        progress=[ConcernProgress(concern_id="a", status=ConcernStatus.VERIFIED)],
        outcomes=[
            ConcernOutcome(
                concern_id="a",
                branch="resolve/a",
                commit="a-sha",
                verified=True,
            )
        ],
    )
    core = ResolverCore(
        ResolverConfig(
            state_root=tmp_path / "state",
            workspace=tmp_path,
            worktree_root=tmp_path / "worktrees",
            run_id=run_id,
            integration_branch="resolve/review",
            verification_commands=[
                VerificationCommand(name="tests", arguments=["pytest"])
            ],
        ),
        resolve_spec(),
        lambda _cwd: unused_session_factory(),
        lambda _cwd: unused_session_factory(),
        UnusedInvocationRenderer(),
        recording_launcher(),
    )
    core.persist(state)

    core.persist(state.model_copy(update={"phase": ResolvePhase.WORKERS}))

    assert core.repository.load().phase == ResolvePhase.REVIEW


@pytest.mark.asyncio
async def test_resume_after_a_kill_past_workers_completes_without_backward_phase(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "source"
    workspace.mkdir()
    launcher = LocalProcessLauncher()

    def git(*arguments: str) -> str:
        # Identity per invocation, never `git config`: a misbound command then
        # writes nothing, where a persisted setting lands in the shared config
        # every worktree of a real repository inherits (see `lup.devtools.gitguard`).
        status = launcher.launch(
            LaunchRequest(
                arguments=[
                    "git",
                    "-c",
                    "user.email=resolver@example.test",
                    "-c",
                    "user.name=Resolver Test",
                    *arguments,
                ],
                cwd=workspace,
            )
        )
        assert status.code == 0, status.stderr
        return status.stdout.strip()

    git("init", "-b", "source")
    (workspace / "README.md").write_text("base\n", encoding="utf-8")
    git("add", "README.md")
    git("commit", "-m", "base")
    source = SourceSnapshot(
        branch=git("branch", "--show-current"), commit=git("rev-parse", "HEAD")
    )

    def worker_response(root: Path, output_name: str) -> JsonObject:
        if output_name == MergeReport.__name__:
            return {"completed": True, "summary": "semantic join reviewed"}
        assert output_name == WorkerReport.__name__
        relative = Path("a.txt")
        (root / relative).write_text("a\n", encoding="utf-8")
        return {
            "concern_id": "a",
            "changed": True,
            "summary": "implemented a",
            "files_changed": [relative.as_posix()],
        }

    def reviewer_response(root: Path, output_name: str) -> JsonObject:
        assert output_name == ReviewReport.__name__
        return {
            "concern_id": "a",
            "accepted": True,
            "generalized": True,
            "reason": "criteria met",
            "criteria_met": ["a-done"],
        }

    def build_core() -> ResolverCore:
        return ResolverCore(
            ResolverConfig(
                state_root=tmp_path / "state",
                workspace=workspace,
                worktree_root=tmp_path / "resolver-worktrees",
                run_id="kill-resume",
                integration_branch="resolve/kill-resume/review",
                verification_commands=[
                    VerificationCommand(
                        name="combined-diff",
                        arguments=["git", "diff", "--check", "HEAD"],
                    )
                ],
            ),
            resolve_spec(),
            worker_recipe(tmp_path / "state", launcher, worker_response),
            lambda context: resolver_test_factory(context.root, reviewer_response),
            LiteralInvocationRenderer(),
            launcher,
        )

    killed = build_core()
    monkeypatch.setattr(killed, "persist_failure", lambda error: None)
    acquire = killed.leases.acquire

    def kill_before_integration(concern_id: str, branch: str) -> WritableRootLease:
        if concern_id == "integration":
            raise RuntimeError("killed before the integration lease")
        return acquire(concern_id, branch)

    monkeypatch.setattr(killed.leases, "acquire", kill_before_integration)
    with pytest.raises(RuntimeError, match="killed before"):
        seed_approvals(killed, [concern("a")])
        await killed.run(ResolveInventory(source=source, concerns=[concern("a")]))
    assert killed.repository.load().phase == ResolvePhase.REVIEW

    resumed = build_core()
    manifest = await resumed.resume()

    assert all(record.passed for record in manifest.verification)
    assert [outcome.verified for outcome in manifest.outcomes] == [True]
    assert resumed.repository.load().phase == ResolvePhase.COMPLETE


@pytest.mark.asyncio
async def test_a_resume_with_nothing_left_to_lease_still_takes_the_landed_fix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The base refresh does not wait on needing a new lease, which is unrelated.

    Gated on one, a run whose concerns are all leased would never refresh,
    reading its original commit for the rest of its life — and every worker
    would resume into a tree predating the fix the run parked for, asking the
    human about a blocker the branch has already settled.
    """
    workspace = failure_leg_workspace(tmp_path, LocalProcessLauncher())
    launcher = LocalProcessLauncher()

    def git(*arguments: str) -> str:
        # Identity per invocation, never `git config`: a misbound command then
        # writes nothing, where a persisted setting lands in the shared config
        # every worktree of a real repository inherits (see `lup.devtools.gitguard`).
        status = launcher.launch(
            LaunchRequest(
                arguments=[
                    "git",
                    "-c",
                    "user.email=resolver@example.test",
                    "-c",
                    "user.name=Resolver Test",
                    *arguments,
                ],
                cwd=workspace,
            )
        )
        assert status.code == 0, status.stderr
        return status.stdout.strip()

    source = SourceSnapshot(
        branch=git("branch", "--show-current"), commit=git("rev-parse", "HEAD")
    )

    def worker_response(root: Path, output_name: str) -> JsonObject:
        if output_name == MergeReport.__name__:
            return {"completed": True, "summary": "semantic join reviewed"}
        (root / "a.txt").write_text("a\n", encoding="utf-8")
        return {
            "concern_id": "a",
            "changed": True,
            "summary": "implemented a",
            "files_changed": ["a.txt"],
        }

    def reviewer_response(_root: Path, output_name: str) -> JsonObject:
        return {
            "concern_id": "a",
            "accepted": True,
            "generalized": True,
            "reason": "criteria met",
            "criteria_met": ["a-done"],
        }

    def build_core() -> ResolverCore:
        return failure_leg_core(
            tmp_path,
            workspace,
            launcher,
            "landed-fix",
            worker_response,
            reviewer_response,
        )

    killed = build_core()
    monkeypatch.setattr(killed, "persist_failure", lambda error: None)
    acquire = killed.leases.acquire

    def kill_before_integration(concern_id: str, branch: str) -> WritableRootLease:
        if concern_id == "integration":
            raise RuntimeError("killed before the integration lease")
        return acquire(concern_id, branch)

    monkeypatch.setattr(killed.leases, "acquire", kill_before_integration)
    with pytest.raises(RuntimeError, match="killed before"):
        seed_approvals(killed, [concern("a")])
        await killed.run(ResolveInventory(source=source, concerns=[concern("a")]))

    # The whole point of parking: the fix lands on the branch meanwhile.
    (workspace / "README.md").write_text("base, fixed\n", encoding="utf-8")
    git("add", "README.md")
    git("commit", "-m", "the fix the run parked for")
    landed = git("rev-parse", "HEAD")

    await build_core().resume()

    assert build_core().repository.load().root_base().commit == landed


def failure_leg_workspace(tmp_path: Path, launcher: LocalProcessLauncher) -> Path:
    workspace = tmp_path / "source"
    workspace.mkdir()

    def git(*arguments: str) -> None:
        status = launcher.launch(
            LaunchRequest(arguments=["git", *arguments], cwd=workspace)
        )
        if status.code != 0:
            raise AssertionError(status.stderr)

    git("init", "-b", "source")
    (workspace / "README.md").write_text("base\n", encoding="utf-8")
    git("add", "README.md")
    git("commit", "-m", "base")
    return workspace


def failure_leg_core(
    tmp_path: Path,
    workspace: Path,
    launcher: LocalProcessLauncher,
    run_id: str,
    worker_response: Callable[[Path, str], JsonObject],
    reviewer_response: Callable[[Path, str], JsonObject],
    max_revision_rounds: int = 2,
    max_declaration_attempts: int = 2,
    environmental_fault: Callable[[str], bool] = lambda _: False,
    recheck_standing_per_join: bool = False,
) -> ResolverCore:
    return ResolverCore(
        ResolverConfig(
            state_root=tmp_path / "state",
            workspace=workspace,
            worktree_root=tmp_path / "resolver-worktrees",
            run_id=run_id,
            integration_branch=f"resolve/{run_id}/review",
            verification_commands=[
                VerificationCommand(
                    name="combined-diff", arguments=["git", "diff", "--check", "HEAD"]
                )
            ],
            max_revision_rounds=max_revision_rounds,
            max_declaration_attempts=max_declaration_attempts,
            recheck_standing_per_join=recheck_standing_per_join,
        ),
        resolve_spec(),
        worker_recipe(tmp_path / "state", launcher, worker_response),
        lambda context: resolver_test_factory(context.root, reviewer_response),
        LiteralInvocationRenderer(),
        launcher,
        environmental_fault=environmental_fault,
    )


def snapshot(workspace: Path, launcher: LocalProcessLauncher) -> SourceSnapshot:
    def git(*arguments: str) -> str:
        # Identity per invocation, never `git config`: a misbound command then
        # writes nothing, where a persisted setting lands in the shared config
        # every worktree of a real repository inherits (see `lup.devtools.gitguard`).
        status = launcher.launch(
            LaunchRequest(
                arguments=[
                    "git",
                    "-c",
                    "user.email=resolver@example.test",
                    "-c",
                    "user.name=Resolver Test",
                    *arguments,
                ],
                cwd=workspace,
            )
        )
        assert status.code == 0, status.stderr
        return status.stdout.strip()

    return SourceSnapshot(
        branch=git("branch", "--show-current"), commit=git("rev-parse", "HEAD")
    )


@pytest.mark.asyncio
async def test_worker_crash_persists_the_failure_and_raises_a_group(
    tmp_path: Path,
) -> None:
    launcher = LocalProcessLauncher()
    workspace = failure_leg_workspace(tmp_path, launcher)

    def worker_response(_root: Path, _output_name: str) -> JsonObject:
        raise RuntimeError("worker exploded")

    def reviewer_response(_root: Path, _output_name: str) -> JsonObject:
        raise AssertionError("the reviewer must not run after a worker crash")

    core = failure_leg_core(
        tmp_path,
        workspace,
        launcher,
        "worker-crash",
        worker_response,
        reviewer_response,
    )

    with pytest.raises(ExceptionGroup) as raised:
        seed_approvals(core, [concern("a")])
        await core.run(
            ResolveInventory(
                source=snapshot(workspace, launcher), concerns=[concern("a")]
            )
        )

    assert [str(error) for error in raised.value.exceptions] == ["worker exploded"]
    persisted = core.repository.load()
    progress = {item.concern_id: item for item in persisted.progress}
    assert progress["a"].status == ConcernStatus.FAILED
    assert progress["a"].reason == "worker exploded"
    assert any("worker exploded" in failure for failure in persisted.failures)


@pytest.mark.asyncio
async def test_a_host_fault_parks_the_run_without_failing_any_concern(
    tmp_path: Path,
) -> None:
    """A dead credential says nothing about the work, so it records nothing.

    The whole recovery rests on no outcome being written: a resume reads
    progress, finds the concern non-terminal, and returns it to the eligible
    set. Recording a failure here to explain the interruption would convert
    every expired login into permanent concern loss.
    """
    launcher = LocalProcessLauncher()
    workspace = failure_leg_workspace(tmp_path, launcher)

    def worker_response(_root: Path, _output_name: str) -> JsonObject:
        raise ProviderTurnError(
            TurnFailure(
                message="Failed to authenticate. API Error: 401 OAuth "
                "access token has been revoked.",
                environmental=True,
            )
        )

    def reviewer_response(_root: Path, _output_name: str) -> JsonObject:
        raise AssertionError("the reviewer must not run after a host fault")

    core = failure_leg_core(
        tmp_path,
        workspace,
        launcher,
        "host-fault",
        worker_response,
        reviewer_response,
    )

    with pytest.raises(ResolverEnvironmentFault) as raised:
        seed_approvals(core, [concern("a")])
        await core.run(
            ResolveInventory(
                source=snapshot(workspace, launcher), concerns=[concern("a")]
            )
        )

    assert "401 OAuth access token has been revoked" in raised.value.cause
    assert raised.value.concerns == ["a"]
    persisted = core.repository.load()
    progress = {item.concern_id: item for item in persisted.progress}
    assert progress["a"].status is not ConcernStatus.FAILED
    assert not [outcome for outcome in persisted.outcomes if outcome.concern_id == "a"]
    assert not persisted.failures


@pytest.mark.asyncio
async def test_a_host_fault_is_recognised_from_its_words_when_the_flag_is_lost(
    tmp_path: Path,
) -> None:
    """The flag is set where an exception is caught; layers above re-wrap it.

    `composition.py` and `wrappers.py` both turn a raw exception into a fresh
    `TurnFailure(message=str(error))`, so the words survive and the flag does
    not. A session limit reached six concurrent concerns and every one was
    recorded as having failed, with the classifier working correctly and its
    answer discarded two frames above where it was made.
    """
    launcher = LocalProcessLauncher()
    workspace = failure_leg_workspace(tmp_path, launcher)
    limit = "You've hit your session limit · resets 11:50pm (Europe/Paris)"

    def worker_response(_root: Path, _output_name: str) -> JsonObject:
        # environmental deliberately unset, as a re-wrapping layer leaves it.
        raise ProviderTurnError(TurnFailure(message=limit))

    def reviewer_response(_root: Path, _output_name: str) -> JsonObject:
        raise AssertionError("the reviewer must not run after a host fault")

    core = failure_leg_core(
        tmp_path,
        workspace,
        launcher,
        "rewrapped-host-fault",
        worker_response,
        reviewer_response,
        environmental_fault=lambda message: "session limit" in message.casefold(),
    )

    with pytest.raises(ResolverEnvironmentFault):
        seed_approvals(core, [concern("a")])
        await core.run(
            ResolveInventory(
                source=snapshot(workspace, launcher), concerns=[concern("a")]
            )
        )

    persisted = core.repository.load()
    progress = {item.concern_id: item for item in persisted.progress}
    assert progress["a"].status is not ConcernStatus.FAILED
    assert not [outcome for outcome in persisted.outcomes if outcome.concern_id == "a"]


@pytest.mark.asyncio
async def test_a_turn_failure_that_is_not_environmental_still_fails_its_concern(
    tmp_path: Path,
) -> None:
    """The classification must not become a blanket amnesty for provider errors.

    `environmental` defaults false precisely so an unclassified fault is
    attributed to the turn: treating a real failure as the host's would
    retry it on every resume forever.
    """
    launcher = LocalProcessLauncher()
    workspace = failure_leg_workspace(tmp_path, launcher)

    def worker_response(_root: Path, _output_name: str) -> JsonObject:
        raise ProviderTurnError(TurnFailure(message="the model refused the tool"))

    def reviewer_response(_root: Path, _output_name: str) -> JsonObject:
        raise AssertionError("the reviewer must not run after a worker crash")

    core = failure_leg_core(
        tmp_path,
        workspace,
        launcher,
        "ordinary-provider-failure",
        worker_response,
        reviewer_response,
    )

    with pytest.raises(ExceptionGroup):
        seed_approvals(core, [concern("a")])
        await core.run(
            ResolveInventory(
                source=snapshot(workspace, launcher), concerns=[concern("a")]
            )
        )

    progress = {item.concern_id: item for item in core.repository.load().progress}
    assert progress["a"].status == ConcernStatus.FAILED


def test_a_failed_concern_does_not_strand_the_leases_beside_it(
    tmp_path: Path,
) -> None:
    """One stale pointer must not cost a resume every healthy concern.

    A concern can only exhaust its rounds by committing work across several,
    so its tree legitimately sits ahead of the base while no commit was ever
    accepted. A restore reading that as the branch having moved under the run
    and raising before any other lease is reached would leave every verified
    and newly eligible concern beside it unreachable through every resume.
    """
    launcher = LocalProcessLauncher()
    workspace = failure_leg_workspace(tmp_path, launcher)
    source = snapshot(workspace, launcher)
    core = failure_leg_core(
        tmp_path,
        workspace,
        launcher,
        "stranded",
        lambda _root, _name: {},
        lambda _root, _name: {},
    )
    failed = WritableRootLease(
        concern_id="a",
        root=tmp_path / "resolver-worktrees" / "a",
        branch="resolve/stranded/a",
    )
    healthy = WritableRootLease(
        concern_id="b",
        root=tmp_path / "resolver-worktrees" / "b",
        branch="resolve/stranded/b",
    )
    core.worktrees.create(failed, source.commit)
    core.worktrees.create(healthy, source.commit)
    (failed.root / "work.txt").write_text("salvageable\n", encoding="utf-8")
    for arguments in (["add", "-A"], ["commit", "-m", "work the worker committed"]):
        status = launcher.launch(
            LaunchRequest(arguments=["git", *arguments], cwd=failed.root)
        )
        assert status.code == 0, status.stderr
    moved = core.worktrees.head(failed)

    def base(identifier: str) -> DependencyBase:
        return DependencyBase(
            concern_id=identifier,
            parent_concerns=[],
            parent_commits=[],
            commit=source.commit,
        )

    state = ResolveState(
        config_digest="config-sha",
        run_id="stranded",
        phase=ResolvePhase.WORKERS,
        source=source,
        spec=resolve_spec(),
        concerns=[concern("a"), concern("b")],
        progress=[
            ConcernProgress(concern_id="a", status=ConcernStatus.FAILED),
            ConcernProgress(concern_id="b", status=ConcernStatus.RUNNING),
        ],
        leases=[failed, healthy],
        bases=[base("a"), base("b")],
        outcomes=[
            # The shape a run persisted before an outcome carried its head:
            # no accepted commit, and nothing recording where the tree ended.
            ConcernOutcome(
                concern_id="a",
                branch=failed.branch,
                verified=False,
                failure="revision limit exhausted",
            )
        ],
    )
    core.persist(state)

    core.restore_leases(state)

    assert moved != source.commit
    assert LeaseDriftEvent(concern_id="a", expected=source.commit, found=moved) in [
        entry.event for entry in core.journal.read()
    ]
    persisted = core.repository.load()
    progress = {item.concern_id: item.status for item in persisted.progress}
    assert progress["b"] == ConcernStatus.LEASED
    assert core.worktrees.head(failed) == moved


@pytest.mark.asyncio
async def test_a_declaration_mismatch_does_not_spend_a_revision_round(
    tmp_path: Path,
) -> None:
    """The contract is bookkeeping, and it is not what the budget is for.

    A worker learns where the declaration boundary is by crossing it, and
    the two directions are reported one at a time: correcting an
    under-declaration by declaring the whole expected set is what produces
    an over-declaration. Charging both to the revision budget let a concern
    fail on the third crossing with its six criteria never once evaluated.
    """
    launcher = LocalProcessLauncher()
    workspace = failure_leg_workspace(tmp_path, launcher)
    worker_calls: Counter[str] = Counter()

    def worker_response(root: Path, output_name: str) -> JsonObject:
        if output_name == MergeReport.__name__:
            return {"completed": True, "summary": "integration join reviewed"}
        assert output_name == WorkerReport.__name__
        identifier = root.name
        worker_calls[identifier] += 1
        attempt = worker_calls[identifier]
        (root / "a.txt").write_text(f"round {attempt}\n", encoding="utf-8")
        report = {
            "concern_id": identifier,
            "changed": True,
            "summary": f"implemented {identifier}",
            "files_changed": ["a.txt"],
        }
        if attempt == 1:
            # Changed a file it did not declare.
            (root / "stray.txt").write_text("undeclared\n", encoding="utf-8")
            return report
        if attempt == 2:
            # Corrected by declaring more, which crosses the other way.
            (root / "stray.txt").unlink()
            return {**report, "swept_beyond_scope": ["ghost.txt"]}
        return report

    def reviewer_response(root: Path, output_name: str) -> JsonObject:
        assert output_name == ReviewReport.__name__
        return {
            "concern_id": root.name,
            "accepted": True,
            "generalized": True,
            "reason": "criteria met",
            "criteria_met": [f"{root.name}-done"],
        }

    core = failure_leg_core(
        tmp_path,
        workspace,
        launcher,
        "declaration-budget",
        worker_response,
        reviewer_response,
        max_revision_rounds=0,
    )

    seed_approvals(core, [concern("a")])
    manifest = await core.run(
        ResolveInventory(
            source=snapshot(workspace, launcher),
            concerns=[concern("a")],
        )
    )

    outcomes = {outcome.concern_id: outcome for outcome in manifest.outcomes}
    assert outcomes["a"].verified is True
    assert worker_calls == {"a": 3}


@pytest.mark.asyncio
async def test_a_concern_that_never_reached_its_criteria_says_so(
    tmp_path: Path,
) -> None:
    """`revision limit exhausted` reads as "the work was not good enough".

    A concern that spent every attempt on the declaration contract never had
    its work judged at all, and reporting that as the same failure hides a
    harness problem inside a work-quality verdict.
    """
    launcher = LocalProcessLauncher()
    workspace = failure_leg_workspace(tmp_path, launcher)
    worker_calls: Counter[str] = Counter()

    def worker_response(root: Path, output_name: str) -> JsonObject:
        if output_name == MergeReport.__name__:
            return {"completed": True, "summary": "integration join reviewed"}
        assert output_name == WorkerReport.__name__
        worker_calls[root.name] += 1
        (root / "a.txt").write_text(
            f"round {worker_calls[root.name]}\n", encoding="utf-8"
        )
        return {
            "concern_id": root.name,
            "changed": True,
            "summary": "implemented a",
            "files_changed": ["a.txt"],
            "swept_beyond_scope": ["ghost.txt"],
        }

    def reviewer_response(_root: Path, _output_name: str) -> JsonObject:
        raise AssertionError("no round should have reached the reviewer")

    core = failure_leg_core(
        tmp_path,
        workspace,
        launcher,
        "never-judged",
        worker_response,
        reviewer_response,
        max_revision_rounds=1,
        max_declaration_attempts=1,
    )

    seed_approvals(core, [concern("a")])
    manifest = await core.run(
        ResolveInventory(
            source=snapshot(workspace, launcher),
            concerns=[concern("a")],
        )
    )

    outcomes = {outcome.concern_id: outcome for outcome in manifest.outcomes}
    assert outcomes["a"].verified is False
    assert outcomes["a"].failure == (
        "declaration contract unmet: no round reached the criteria"
    )
    assert worker_calls == {"a": 3}


@pytest.mark.asyncio
async def test_revision_exhaustion_soft_fails_and_blocks_dependents(
    tmp_path: Path,
) -> None:
    launcher = LocalProcessLauncher()
    workspace = failure_leg_workspace(tmp_path, launcher)
    worker_calls: Counter[str] = Counter()

    def worker_response(root: Path, output_name: str) -> JsonObject:
        if output_name == MergeReport.__name__:
            return {"completed": True, "summary": "integration join reviewed"}
        assert output_name == WorkerReport.__name__
        identifier = root.name
        worker_calls[identifier] += 1
        relative = Path(f"{identifier}.txt")
        (root / relative).write_text(
            f"{identifier} round {worker_calls[identifier]}\n", encoding="utf-8"
        )
        return {
            "concern_id": identifier,
            "changed": True,
            "summary": f"implemented {identifier}",
            "files_changed": [relative.as_posix()],
        }

    def reviewer_response(root: Path, output_name: str) -> JsonObject:
        assert output_name == ReviewReport.__name__
        identifier = root.name
        if identifier == "a":
            return {
                "concern_id": "a",
                "accepted": False,
                "generalized": False,
                "reason": "never good enough",
                "residual": ["still wrong"],
            }
        return {
            "concern_id": identifier,
            "accepted": True,
            "generalized": True,
            "reason": "criteria met",
            "criteria_met": [f"{identifier}-done"],
        }

    core = failure_leg_core(
        tmp_path,
        workspace,
        launcher,
        "revision-exhaustion",
        worker_response,
        reviewer_response,
        max_revision_rounds=1,
    )

    seed_approvals(core, [concern("a"), concern("b"), concern("c", ["a"])])
    manifest = await core.run(
        ResolveInventory(
            source=snapshot(workspace, launcher),
            concerns=[concern("a"), concern("b"), concern("c", ["a"])],
        )
    )

    outcomes = {outcome.concern_id: outcome for outcome in manifest.outcomes}
    assert outcomes["a"].verified is False
    assert outcomes["a"].failure == "revision limit exhausted"
    assert outcomes["b"].verified is True
    assert outcomes["c"].verified is False
    assert outcomes["c"].failure == "a dependency did not produce a verified commit"
    assert worker_calls == {"a": 2, "b": 1}
    persisted = core.repository.load()
    progress = {item.concern_id: item for item in persisted.progress}
    assert progress["a"].status == ConcernStatus.FAILED
    assert progress["c"].status == ConcernStatus.FAILED


@pytest.mark.asyncio
async def test_unresolved_semantic_join_fails_the_dependent_concern(
    tmp_path: Path,
) -> None:
    launcher = LocalProcessLauncher()
    workspace = failure_leg_workspace(tmp_path, launcher)

    def worker_response(root: Path, output_name: str) -> JsonObject:
        if output_name == MergeReport.__name__:
            return {
                "completed": False,
                "summary": "conflicting intents in shared.txt",
                "unresolved_paths": ["shared.txt"],
            }
        assert output_name == WorkerReport.__name__
        identifier = root.name
        (root / "shared.txt").write_text(f"{identifier} version\n", encoding="utf-8")
        return {
            "concern_id": identifier,
            "changed": True,
            "summary": f"implemented {identifier}",
            "files_changed": ["shared.txt"],
        }

    def reviewer_response(root: Path, output_name: str) -> JsonObject:
        assert output_name == ReviewReport.__name__
        identifier = root.name
        return {
            "concern_id": identifier,
            "accepted": True,
            "generalized": True,
            "reason": "criteria met",
            "criteria_met": [f"{identifier}-done"],
        }

    core = failure_leg_core(
        tmp_path,
        workspace,
        launcher,
        "join-conflict",
        worker_response,
        reviewer_response,
    )

    with pytest.raises(ExceptionGroup) as raised:
        seed_approvals(core, [concern("a"), concern("b"), concern("c", ["a", "b"])])
        await core.run(
            ResolveInventory(
                source=snapshot(workspace, launcher),
                concerns=[concern("a"), concern("b"), concern("c", ["a", "b"])],
            )
        )

    assert len(raised.value.exceptions) == 1
    joined_failure = raised.value.exceptions[0]
    assert isinstance(joined_failure, ResolverInvariantError)
    assert "semantic join failed for c" in str(joined_failure)
    persisted = core.repository.load()
    progress = {item.concern_id: item for item in persisted.progress}
    assert progress["a"].status == ConcernStatus.VERIFIED
    assert progress["b"].status == ConcernStatus.VERIFIED
    assert progress["c"].status == ConcernStatus.FAILED
    assert "semantic join failed for c" in (progress["c"].reason or "")


@pytest.mark.asyncio
async def test_aborting_a_parked_run_frees_its_leases_and_refuses_resumption(
    tmp_path: Path,
) -> None:
    """Cleanup reachable only at acceptance strands every lease short of it."""
    launcher = LocalProcessLauncher()
    workspace = failure_leg_workspace(tmp_path, launcher)

    def worker_response(_root: Path, output_name: str) -> JsonObject:
        if output_name == MergeReport.__name__:
            return {"completed": True, "summary": "joined"}
        mailbox = QuestionMailbox(tmp_path / "state" / "abandoned")
        worker_asks(mailbox, "abandoned", dynamic_question("a"))
        return {
            "concern_id": "a",
            "changed": False,
            "summary": "parked awaiting a material choice",
        }

    def build_core() -> ResolverCore:
        return ResolverCore(
            ResolverConfig(
                state_root=tmp_path / "state",
                workspace=workspace,
                worktree_root=tmp_path / "resolver-worktrees",
                run_id="abandoned",
                integration_branch="resolve/abandoned/review",
                verification_commands=[
                    VerificationCommand(name="v", arguments=["git", "diff"])
                ],
            ),
            resolve_spec(),
            worker_recipe(tmp_path / "state", launcher, worker_response),
            lambda context: resolver_test_factory(context.root, lambda *_: {}),
            LiteralInvocationRenderer(),
            launcher,
        )

    parked = build_core()
    seed_approvals(parked, [concern("a")])
    with pytest.raises(ResolverAwaitingAnswers):
        await parked.run(
            ResolveInventory(
                source=snapshot(workspace, launcher), concerns=[concern("a")]
            )
        )
    leased = [lease.root for lease in parked.repository.load().leases]
    assert leased and all(root.exists() for root in leased)

    manifest = build_core().abort("superseded by a re-plan")

    assert [record.action for record in manifest.cleanup] == ["removed"]
    assert not any(root.exists() for root in leased)
    persisted = build_core().repository.load()
    assert persisted.phase == ResolvePhase.ABORTED
    assert persisted.abort_reason == "superseded by a re-plan"
    with pytest.raises(ResolverInvariantError, match="was aborted"):
        await build_core().resume()


@pytest.mark.asyncio
async def test_midrun_question_parks_the_concern_and_resumes_after_answers(
    tmp_path: Path,
) -> None:
    launcher = LocalProcessLauncher()
    workspace = failure_leg_workspace(tmp_path, launcher)
    worker_calls: Counter[str] = Counter()

    def worker_response(root: Path, output_name: str) -> JsonObject:
        if output_name == MergeReport.__name__:
            return {"completed": True, "summary": "semantic join reviewed"}
        if output_name != WorkerReport.__name__:
            raise AssertionError(output_name)
        worker_calls["a"] += 1
        mailbox = QuestionMailbox(tmp_path / "state" / "midrun-park")
        worker_asks(mailbox, "midrun-park", dynamic_question("a"))
        if "a-dynamic" not in mailbox.answered_ids():
            return {
                "concern_id": "a",
                "changed": False,
                "summary": "parked awaiting a material choice",
            }
        (root / "a.txt").write_text("durable\n", encoding="utf-8")
        return {
            "concern_id": "a",
            "changed": True,
            "summary": "implemented a",
            "files_changed": ["a.txt"],
        }

    def reviewer_response(_root: Path, output_name: str) -> JsonObject:
        return {
            "concern_id": "a",
            "accepted": True,
            "generalized": True,
            "reason": "criteria met",
            "criteria_met": ["a-done"],
        }

    def build_core() -> ResolverCore:
        return ResolverCore(
            ResolverConfig(
                state_root=tmp_path / "state",
                workspace=workspace,
                worktree_root=tmp_path / "resolver-worktrees",
                run_id="midrun-park",
                integration_branch="resolve/midrun-park/review",
                verification_commands=[
                    VerificationCommand(
                        name="combined-diff",
                        arguments=["git", "diff", "--check", "HEAD"],
                    )
                ],
            ),
            resolve_spec(),
            worker_recipe(tmp_path / "state", launcher, worker_response),
            lambda context: resolver_test_factory(context.root, reviewer_response),
            LiteralInvocationRenderer(),
            launcher,
        )

    parked_core = build_core()
    seed_approvals(parked_core, [concern("a")])
    with pytest.raises(ResolverAwaitingAnswers) as raised:
        await parked_core.run(
            ResolveInventory(
                source=snapshot(workspace, launcher), concerns=[concern("a")]
            )
        )

    assert [question.id for question in raised.value.pending] == ["a-dynamic"]
    persisted = parked_core.repository.load()
    progress = {item.concern_id: item for item in persisted.progress}
    assert progress["a"].status == ConcernStatus.WAITING_FOR_ANSWERS
    assert progress["a"].reason == "parked on material questions"
    assert persisted.phase != ResolvePhase.FAILED
    assert persisted.questions is not None
    assert "a-dynamic" in {q.id for q in persisted.questions.questions}

    resumed = build_core()
    seed_offer(resumed, "a-dynamic", "durable")
    manifest = await resumed.resume()

    assert worker_calls["a"] == 2
    assert [outcome.verified for outcome in manifest.outcomes] == [True]
    assert all(record.passed for record in manifest.verification)


@pytest.mark.asyncio
async def test_a_concern_resumed_past_a_committed_round_keeps_that_round_as_its_base(
    tmp_path: Path,
) -> None:
    """A second process measures a turn from the lease, not from loop entry.

    The commit a turn is measured from is only the clearance for a lease
    nothing has committed to yet. A concern resumed after a rejected round
    re-enters with its lease already holding that round, so a base taken at
    loop entry names a commit HEAD has moved past — and the very next
    validation reads the orchestrator's own commit as the worker having
    seized commit authority, failing the concern for work it did itself.
    """
    launcher = LocalProcessLauncher()
    workspace = failure_leg_workspace(tmp_path, launcher)
    worker_calls: Counter[str] = Counter()
    review_calls: Counter[str] = Counter()

    def worker_response(root: Path, output_name: str) -> JsonObject:
        if output_name == MergeReport.__name__:
            return {"completed": True, "summary": "semantic join reviewed"}
        if output_name != WorkerReport.__name__:
            raise AssertionError(output_name)
        worker_calls["a"] += 1
        if worker_calls["a"] == 2:
            worker_asks(
                QuestionMailbox(tmp_path / "state" / "resumed-base"),
                "resumed-base",
                dynamic_question("a"),
            )
            return {
                "concern_id": "a",
                "changed": False,
                "summary": "parked awaiting a material choice",
            }
        (root / "a.txt").write_text(f"round {worker_calls['a']}\n", encoding="utf-8")
        return {
            "concern_id": "a",
            "changed": True,
            "summary": "implemented a",
            "files_changed": ["a.txt"],
        }

    def reviewer_response(_root: Path, output_name: str) -> JsonObject:
        assert output_name == ReviewReport.__name__
        review_calls["a"] += 1
        if review_calls["a"] == 1:
            return {
                "concern_id": "a",
                "accepted": False,
                "generalized": False,
                "reason": "wants one more pass",
                "residual": ["revise"],
            }
        return {
            "concern_id": "a",
            "accepted": True,
            "generalized": True,
            "reason": "criteria met",
            "criteria_met": ["a-done"],
        }

    def build_core() -> ResolverCore:
        return failure_leg_core(
            tmp_path,
            workspace,
            launcher,
            "resumed-base",
            worker_response,
            reviewer_response,
        )

    parked_core = build_core()
    seed_approvals(parked_core, [concern("a")])
    with pytest.raises(ResolverAwaitingAnswers):
        await parked_core.run(
            ResolveInventory(
                source=snapshot(workspace, launcher), concerns=[concern("a")]
            )
        )

    # The first process committed its rejected round, so the lease head has
    # moved past both the recorded base and the clearance it came from.
    parked = parked_core.repository.load()
    committed = launcher.launch(
        LaunchRequest(
            arguments=["git", "rev-parse", "HEAD"],
            cwd=parked_core.leases.leases["a"].root,
        )
    ).stdout.strip()
    assert committed not in [base.commit for base in parked.bases]

    resumed = build_core()
    seed_offer(resumed, "a-dynamic", "durable")
    manifest = await resumed.resume()

    outcomes = {outcome.concern_id: outcome for outcome in manifest.outcomes}
    # Both rounds, not only the one this process took. A resume re-enters
    # from what is persisted, so the outcome records the whole history
    # rather than starting the count again at the interruption.
    assert [round.round for round in outcomes["a"].rounds] == [1, 2]
    assert [round.diff.reason for round in outcomes["a"].rounds] == ["", ""]
    assert outcomes["a"].verified is True
    assert all(record.passed for record in manifest.verification)


@pytest.mark.asyncio
async def test_a_resumed_concern_still_knows_why_it_was_sent_back(
    tmp_path: Path,
) -> None:
    """The review that produced the feedback was spent; losing it spends it twice.

    An interrupted concern re-entering at round one with `feedback = ""` while
    its branch still carries the rounds it committed would have the worker
    meet its own work with no record of what the reviewer asked for.
    """
    launcher = LocalProcessLauncher()
    workspace = failure_leg_workspace(tmp_path, launcher)
    written: list[str] = []  # lup: ignore[empty-collection]
    running: list[ResolverCore] = []  # lup: ignore[empty-collection]

    def worker_response(root: Path, output_name: str) -> JsonObject:
        if output_name == MergeReport.__name__:
            return {"completed": True, "summary": "semantic join reviewed"}
        written.append("turn")
        (root / "a.txt").write_text(f"round {len(written)}\n", encoding="utf-8")
        return {
            "concern_id": "a",
            "changed": True,
            "summary": "implemented a",
            "files_changed": ["a.txt"],
        }

    def reviewer_response(_root: Path, _output_name: str) -> JsonObject:
        if len(running) == 1:
            running[0].mailbox.drain(
                ParkRequest(run_id="feedback-survives", reason="stop here")
            )
            return {
                "concern_id": "a",
                "accepted": False,
                "generalized": False,
                "reason": "rename the helper before this can pass",
                "criteria_met": [],
            }
        return {
            "concern_id": "a",
            "accepted": True,
            "generalized": True,
            "reason": "criteria met",
            "criteria_met": ["a-done"],
        }

    def build_core() -> ResolverCore:
        return failure_leg_core(
            tmp_path,
            workspace,
            launcher,
            "feedback-survives",
            worker_response,
            reviewer_response,
        )

    stopped = build_core()
    running.append(stopped)
    seed_approvals(stopped, [concern("a")])
    with pytest.raises(ResolverDrained):
        await stopped.run(
            ResolveInventory(
                source=snapshot(workspace, launcher), concerns=[concern("a")]
            )
        )

    # The round the interruption left behind, whole rather than split across
    # its halves — the diff is what joins them and what a re-entry needs.
    persisted = stopped.repository.rounds_for("a")
    assert [record.round for record in persisted] == [1]
    assert "rename the helper" in persisted[0].review.reason

    resumed = build_core()
    running.append(resumed)
    manifest = await resumed.resume()

    outcomes = {outcome.concern_id: outcome for outcome in manifest.outcomes}
    # Two worker turns in total, not three: the resume continued at round two
    # rather than repeating the round already committed on the branch.
    assert len(written) == 2
    assert [record.round for record in outcomes["a"].rounds] == [1, 2]
    assert outcomes["a"].verified is True


@pytest.mark.asyncio
async def test_a_drained_run_stops_without_failing_anything(tmp_path: Path) -> None:
    """The only way to end a busy run was to kill it, and killing loses work.

    Park reaches a run sitting on an answer and no other, so a worker inside
    a model turn was unaffected by it. A drain stops at the top of a round,
    where the previous round is committed and nothing is in flight.
    """
    launcher = LocalProcessLauncher()
    workspace = failure_leg_workspace(tmp_path, launcher)

    def worker_response(root: Path, output_name: str) -> JsonObject:
        if output_name == MergeReport.__name__:
            return {"completed": True, "summary": "semantic join reviewed"}
        (root / "a.txt").write_text("durable\n", encoding="utf-8")
        return {
            "concern_id": "a",
            "changed": True,
            "summary": "implemented a",
            "files_changed": ["a.txt"],
        }

    asked: list[ResolverCore] = []  # lup: ignore[empty-collection]

    def asking_reviewer(_root: Path, _output_name: str) -> JsonObject:
        """Reject once, and ask for the drain while round one is being judged.

        The request has to arrive while the run is working, which is the
        whole case: a marker set before it starts is cleared as stale, the
        way a park is, so a resume is not stopped by the drain it answered.
        """
        asked[0].mailbox.drain(
            ParkRequest(run_id="drained-run", reason="operator stopped it")
        )
        return {
            "concern_id": "a",
            "accepted": False,
            "generalized": False,
            "reason": "another round, please",
            "criteria_met": [],
        }

    core = failure_leg_core(
        tmp_path,
        workspace,
        launcher,
        "drained-run",
        worker_response,
        asking_reviewer,
    )
    asked.append(core)
    seed_approvals(core, [concern("a")])

    with pytest.raises(ResolverDrained) as drained:
        await core.run(
            ResolveInventory(
                source=snapshot(workspace, launcher), concerns=[concern("a")]
            )
        )

    assert drained.value.reason == "operator stopped it"
    state = core.repository.load()
    # Not failed, not written off: a drain says nothing about the work.
    assert state.outcomes == []
    assert state.phase is not ResolvePhase.FAILED
    assert [item.status for item in state.progress] == [ConcernStatus.ELIGIBLE]


@pytest.mark.asyncio
async def test_a_resume_clears_the_drain_that_stopped_it(tmp_path: Path) -> None:
    """Left standing, the run stops again at the first boundary of the resume."""
    launcher = LocalProcessLauncher()
    workspace = failure_leg_workspace(tmp_path, launcher)

    def worker_response(root: Path, output_name: str) -> JsonObject:
        if output_name == MergeReport.__name__:
            return {"completed": True, "summary": "semantic join reviewed"}
        (root / "a.txt").write_text("durable\n", encoding="utf-8")
        return {
            "concern_id": "a",
            "changed": True,
            "summary": "implemented a",
            "files_changed": ["a.txt"],
        }

    running: list[ResolverCore] = []  # lup: ignore[empty-collection]

    def reviewer_response(_root: Path, _output_name: str) -> JsonObject:
        """Reject and drain on the first run; accept once resumed."""
        if len(running) == 1:
            running[0].mailbox.drain(
                ParkRequest(run_id="drained-then-resumed", reason="operator stopped it")
            )
            return {
                "concern_id": "a",
                "accepted": False,
                "generalized": False,
                "reason": "another round, please",
                "criteria_met": [],
            }
        return {
            "concern_id": "a",
            "accepted": True,
            "generalized": True,
            "reason": "criteria met",
            "criteria_met": ["a-done"],
        }

    def build_core() -> ResolverCore:
        return failure_leg_core(
            tmp_path,
            workspace,
            launcher,
            "drained-then-resumed",
            worker_response,
            reviewer_response,
        )

    stopped = build_core()
    running.append(stopped)
    seed_approvals(stopped, [concern("a")])
    with pytest.raises(ResolverDrained):
        await stopped.run(
            ResolveInventory(
                source=snapshot(workspace, launcher), concerns=[concern("a")]
            )
        )

    resumed = build_core()
    running.append(resumed)
    manifest = await resumed.resume()

    outcomes = {outcome.concern_id: outcome for outcome in manifest.outcomes}
    assert outcomes["a"].verified is True
    assert resumed.mailbox.draining() is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("per_join", "expected"), [(False, 2), (True, 3)], ids=["gated-off", "asked-for"]
)
async def test_a_standing_recheck_costs_a_turn_only_when_it_is_asked_for(
    tmp_path: Path, per_join: bool, expected: int
) -> None:
    """One reviewer turn per overlapping pair, which grows quadratically.

    21 parents is up to 210 of them, at about fourteen minutes each in a
    measured run — against 21 for the final pass, which examines the same
    concerns against the finished tree. So what the per-join pass adds is
    the name of the join responsible, and that is worth asking for rather
    than spending by default.
    """
    launcher = LocalProcessLauncher()
    workspace = tmp_path / "source"
    workspace.mkdir()

    def git(*arguments: str) -> str:
        # Identity per invocation, never `git config`: a misbound command then
        # writes nothing, where a persisted setting lands in the shared config
        # every worktree of a real repository inherits (see `lup.devtools.gitguard`).
        status = launcher.launch(
            LaunchRequest(
                arguments=[
                    "git",
                    "-c",
                    "user.email=resolver@example.test",
                    "-c",
                    "user.name=Resolver Test",
                    *arguments,
                ],
                cwd=workspace,
            )
        )
        assert status.code == 0, status.stderr
        return status.stdout.strip()

    def commit_shared(name: str, first: str, last: str) -> str:
        """Edit opposite ends of one file, so the parents overlap but merge."""
        git("checkout", "-b", name, "source")
        lines = [first, *["middle"] * 12, last]
        (workspace / "shared.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
        git("add", "shared.txt")
        git("commit", "-m", f"{name} edits shared.txt")
        return git("rev-parse", "HEAD")

    git("init", "-b", "source")
    (workspace / "shared.txt").write_text(
        "\n".join(["top", *["middle"] * 12, "bottom"]) + "\n", encoding="utf-8"
    )
    git("add", "shared.txt")
    git("commit", "-m", "base")

    first = commit_shared("one", "one rewrote the top", "bottom")
    second = commit_shared("two", "top", "two rewrote the bottom")
    git("checkout", "source")

    reviews: list[str] = []  # lup: ignore[empty-collection] — turn counter

    def reviewer_response(_root: Path, _output_name: str) -> JsonObject:
        reviews.append("recheck")
        return {
            "concern_id": "a",
            "accepted": True,
            "generalized": True,
            "reason": "still holds",
            "criteria_met": ["held"],
        }

    def unused_worker(_root: Path, _output_name: str) -> JsonObject:
        raise AssertionError("these parents merge without adjudication")

    core = failure_leg_core(
        tmp_path,
        workspace,
        launcher,
        f"standing-{per_join}",
        unused_worker,
        reviewer_response,
        recheck_standing_per_join=per_join,
    )
    # One criterion id both declare, so a re-check answers for either without
    # the unknown-label correction turn muddying what is being counted.
    concerns = [
        concern(identifier).model_copy(
            update={"criteria": [AcceptanceCriterion(id="held", description="done")]}
        )
        for identifier in ("a", "b")
    ]
    state = ResolveState(
        config_digest="config-sha",
        run_id=f"standing-{per_join}",
        phase=ResolvePhase.REVIEW,
        source=snapshot(workspace, launcher),
        spec=resolve_spec(),
        concerns=concerns,
        progress=[
            ConcernProgress(concern_id=item.id, status=ConcernStatus.VERIFIED)
            for item in concerns
        ],
        outcomes=[
            ConcernOutcome(
                concern_id="a", branch="one", commit=first, head=first, verified=True
            ),
            ConcernOutcome(
                concern_id="b", branch="two", commit=second, head=second, verified=True
            ),
        ],
    )
    core.persist(state)

    await core.integrate(state, state.outcomes)

    # Two either way for the final pass, one per integrated concern. The third
    # is the standing re-check of the parent already in the tree.
    assert len(reviews) == expected


@pytest.mark.asyncio
async def test_a_drain_stops_integration_between_two_parents(tmp_path: Path) -> None:
    """The longest phase of a run holds a boundary a drain is seen at.

    Integration begins after the last worker round and dependency batch, so
    a ``draining()`` consulted only there would never be asked again. A drain
    issued during integration would wait out the whole merge, leaving
    ``kill`` the only lever, which costs the in-flight parent's merger work.
    """
    launcher = LocalProcessLauncher()
    workspace = failure_leg_workspace(tmp_path, launcher)

    def git(*arguments: str) -> str:
        # Identity per invocation, never `git config`: a misbound command then
        # writes nothing, where a persisted setting lands in the shared config
        # every worktree of a real repository inherits (see `lup.devtools.gitguard`).
        status = launcher.launch(
            LaunchRequest(
                arguments=[
                    "git",
                    "-c",
                    "user.email=resolver@example.test",
                    "-c",
                    "user.name=Resolver Test",
                    *arguments,
                ],
                cwd=workspace,
            )
        )
        assert status.code == 0, status.stderr
        return status.stdout.strip()

    def branch_adding(name: str, filename: str) -> str:
        git("checkout", "-b", name, "source")
        (workspace / filename).write_text(f"{name}\n", encoding="utf-8")
        git("add", filename)
        git("commit", "-m", f"add {filename}")
        return git("rev-parse", "HEAD")

    first = branch_adding("one", "one.txt")
    second = branch_adding("two", "two.txt")
    git("checkout", "source")

    def unused_actor(_root: Path, _output_name: str) -> JsonObject:
        raise AssertionError("a drained join spends no turn past the merger's")

    core = failure_leg_core(
        tmp_path, workspace, launcher, "drained-join", unused_actor, unused_actor
    )
    # Asked for while the merger is mid-sequence, which is the boundary under
    # test. A drain already waiting when integration begins stops the join
    # before a session is opened at all, so it never reaches this boundary.
    core.turns.worker_factory = merger_draining_after_one_parent(
        tmp_path / "state" / "drained-join", launcher, unused_actor
    )
    concerns = [concern("a"), concern("b")]
    state = ResolveState(
        config_digest="config-sha",
        run_id="drained-join",
        phase=ResolvePhase.REVIEW,
        source=snapshot(workspace, launcher),
        spec=resolve_spec(),
        concerns=concerns,
        progress=[
            ConcernProgress(concern_id=item.id, status=ConcernStatus.VERIFIED)
            for item in concerns
        ],
        outcomes=[
            ConcernOutcome(
                concern_id="a", branch="one", commit=first, head=first, verified=True
            ),
            ConcernOutcome(
                concern_id="b", branch="two", commit=second, head=second, verified=True
            ),
        ],
    )
    core.persist(state)
    with pytest.raises(ResolverDrained) as drained:
        await core.integrate(state, state.outcomes)

    assert drained.value.reason == "operator stopped it"
    # Stopped after the first parent was committed and its progress written,
    # so the resume re-enters at the second rather than rebuilding the first.
    persisted = core.repository.load()
    assert persisted.join_progress is not None
    # Exactly one, whichever the ordering picked: the boundary is between two
    # parents, so the first is committed and recorded and the second is not.
    assert len(persisted.join_progress.joined) == 1
    assert persisted.join_progress.joined[0] in {first, second}
    assert persisted.integration is None


@pytest.mark.asyncio
async def test_integration_opens_on_a_join_record_of_its_own(tmp_path: Path) -> None:
    """What the worker phase left behind describes a different sequence.

    ``join_progress`` is written by every join a run performs, dependency
    joins included, and those name a concern lease's tree. Carried into
    integration it is wrong twice over: a resume restores the integration
    lease to another lease's commit, and the completions have the dependency
    joins timing the integration ones at several times what each one takes.
    """
    launcher = LocalProcessLauncher()
    workspace = failure_leg_workspace(tmp_path, launcher)

    def git(*arguments: str) -> str:
        status = launcher.launch(
            LaunchRequest(
                arguments=[
                    "git",
                    "-c",
                    "user.email=resolver@example.test",
                    "-c",
                    "user.name=Resolver Test",
                    *arguments,
                ],
                cwd=workspace,
            )
        )
        assert status.code == 0, status.stderr
        return status.stdout.strip()

    def branch_adding(name: str, filename: str) -> str:
        git("checkout", "-b", name, "source")
        (workspace / filename).write_text(f"{name}\n", encoding="utf-8")
        git("add", filename)
        git("commit", "-m", f"add {filename}")
        return git("rev-parse", "HEAD")

    first = branch_adding("one", "one.txt")
    second = branch_adding("two", "two.txt")
    git("checkout", "source")

    def unused_actor(_root: Path, _output_name: str) -> JsonObject:
        raise AssertionError("a drained join spends no turn past the merger's")

    core = failure_leg_core(
        tmp_path, workspace, launcher, "fresh-join", unused_actor, unused_actor
    )
    core.turns.worker_factory = merger_draining_after_one_parent(
        tmp_path / "state" / "fresh-join", launcher, unused_actor
    )
    concerns = [concern("a"), concern("b")]
    stale = utc_now() - timedelta(hours=2)
    state = ResolveState(
        config_digest="config-sha",
        run_id="fresh-join",
        phase=ResolvePhase.REVIEW,
        source=snapshot(workspace, launcher),
        spec=resolve_spec(),
        concerns=concerns,
        progress=[
            ConcernProgress(concern_id=item.id, status=ConcernStatus.VERIFIED)
            for item in concerns
        ],
        outcomes=[
            ConcernOutcome(
                concern_id="a", branch="one", commit=first, head=first, verified=True
            ),
            ConcernOutcome(
                concern_id="b", branch="two", commit=second, head=second, verified=True
            ),
        ],
        # What a dependency join left behind: another lease's tree, and two
        # landings two hours older than anything this phase will merge.
        join_progress=JoinProgress(
            joined=["dependency-parent"],
            commit="a" * 40,
            planned=["dependency-parent"],
            completions=[stale, stale + timedelta(minutes=1)],
        ),
    )
    core.persist(state)
    with pytest.raises(ResolverDrained):
        await core.integrate(state, state.outcomes)

    persisted = core.repository.load()
    assert persisted.join_progress is not None
    # One landing, this phase's own: the samples it would have inherited are
    # what made the rate an order of magnitude wrong.
    assert len(persisted.join_progress.completions) == 1
    assert persisted.join_progress.completions[0] > stale + timedelta(hours=1)
    assert "dependency-parent" not in persisted.join_progress.joined


@pytest.mark.asyncio
async def test_a_finished_run_releases_itself_without_a_human_gate(
    tmp_path: Path,
) -> None:
    """The gate decided nothing: both answers cleaned the same leases.

    So a run reaches COMPLETE on its own, having freed every concern
    worktree and kept the review branch for whoever lands it.
    """
    launcher = LocalProcessLauncher()
    workspace = failure_leg_workspace(tmp_path, launcher)

    def worker_response(root: Path, output_name: str) -> JsonObject:
        if output_name == MergeReport.__name__:
            return {"completed": True, "summary": "semantic join reviewed"}
        (root / "a.txt").write_text("durable\n", encoding="utf-8")
        return {
            "concern_id": "a",
            "changed": True,
            "summary": "implemented a",
            "files_changed": ["a.txt"],
        }

    def reviewer_response(_root: Path, output_name: str) -> JsonObject:
        return {
            "concern_id": "a",
            "accepted": True,
            "generalized": True,
            "reason": "criteria met",
            "criteria_met": ["a-done"],
        }

    def build_core() -> ResolverCore:
        return ResolverCore(
            ResolverConfig(
                state_root=tmp_path / "state",
                workspace=workspace,
                worktree_root=tmp_path / "resolver-worktrees",
                run_id="acceptance-door",
                integration_branch="resolve/acceptance-door/review",
                verification_commands=[
                    VerificationCommand(
                        name="combined-diff",
                        arguments=["git", "diff", "--check", "HEAD"],
                    )
                ],
            ),
            resolve_spec(),
            worker_recipe(tmp_path / "state", launcher, worker_response),
            lambda context: resolver_test_factory(context.root, reviewer_response),
            LiteralInvocationRenderer(),
            launcher,
        )

    core = build_core()
    seed_approvals(core, [concern("a")])
    manifest = await core.run(
        ResolveInventory(source=snapshot(workspace, launcher), concerns=[concern("a")])
    )

    assert all(record.passed for record in manifest.verification)
    assert core.repository.load().phase == ResolvePhase.COMPLETE
    assert [record.action for record in manifest.cleanup] == ["removed", "retained"]


@pytest.mark.asyncio
async def test_an_offer_outside_a_closed_gate_never_decides(
    tmp_path: Path,
) -> None:
    """A door is a form, not a trusted caller."""
    launcher = LocalProcessLauncher()
    workspace = failure_leg_workspace(tmp_path, launcher)
    core = ResolverCore(
        ResolverConfig(
            state_root=tmp_path / "state",
            workspace=workspace,
            worktree_root=tmp_path / "resolver-worktrees",
            run_id="bad-acceptance",
            integration_branch="resolve/bad-acceptance/review",
            verification_commands=[VerificationCommand(name="v", arguments=["git"])],
        ),
        resolve_spec(),
        worker_recipe(tmp_path / "state", launcher, lambda *_: {}),
        lambda context: resolver_test_factory(context.root, lambda *_: {}),
        LiteralInvocationRenderer(),
        launcher,
    )
    core.questions.queue_questions(
        [
            MaterialQuestion(
                id="a-superseded",
                concern_id="a",
                prompt="superseded or a regression?",
                choices=["superseded", "regression"],
                closed_choices=True,
            )
        ],
        "a",
    )
    seed_offer(core, "a-superseded", "maybe")

    problems = core.questions.promote_offers()

    assert core.mailbox.answers() == []
    assert problems == [
        "a-superseded was answered 'maybe', but that gate "
        "accepts only: superseded, regression"
    ]


@pytest.mark.asyncio
async def test_a_design_question_records_an_answer_in_the_humans_own_words(
    tmp_path: Path,
) -> None:
    """A planner's choices are suggestions, so an answer outside them counts."""
    launcher = LocalProcessLauncher()
    workspace = failure_leg_workspace(tmp_path, launcher)
    core = ResolverCore(
        ResolverConfig(
            state_root=tmp_path / "state",
            workspace=workspace,
            worktree_root=tmp_path / "resolver-worktrees",
            run_id="own-words",
            integration_branch="resolve/own-words/review",
            verification_commands=[VerificationCommand(name="v", arguments=["git"])],
        ),
        resolve_spec(),
        worker_recipe(tmp_path / "state", launcher, lambda *_: {}),
        lambda context: resolver_test_factory(context.root, lambda *_: {}),
        LiteralInvocationRenderer(),
        launcher,
    )
    core.questions.queue_questions(
        [
            MaterialQuestion(
                id="shape",
                concern_id="design",
                prompt="Which shape?",
                choices=["a method", "a registry"],
            )
        ],
        "design",
    )
    seed_offer(core, "shape", "neither — close the union at its base")

    problems = core.questions.promote_offers()

    assert problems == []
    assert [record.answer.value for record in core.mailbox.answers()] == [
        "neither — close the union at its base"
    ]


def test_an_allowance_answered_in_prose_is_refused_rather_than_read_as_no(
    tmp_path: Path,
) -> None:
    """A grant that cannot be read must not promote into a silent refusal.

    The reader tests for the literal token, so anything else means refused —
    and a promoted answer is never revisable, which made the mistake
    terminal for the concern. Closing the domain turns it into a correctable
    problem the human is told about at the moment they answer.
    """
    launcher = LocalProcessLauncher()
    workspace = failure_leg_workspace(tmp_path, launcher)
    core = ResolverCore(
        ResolverConfig(
            state_root=tmp_path / "state",
            workspace=workspace,
            worktree_root=tmp_path / "resolver-worktrees",
            run_id="prose-grant",
            integration_branch="resolve/prose-grant/review",
            verification_commands=[VerificationCommand(name="v", arguments=["git"])],
        ),
        resolve_spec(),
        worker_recipe(tmp_path / "state", launcher, lambda *_: {}),
        lambda context: resolver_test_factory(context.root, lambda *_: {}),
        LiteralInvocationRenderer(),
        launcher,
    )
    gate = allowance_question_id("spu", ConcernAllowance.ANTIPATTERN_SUPPRESSION)
    core.questions.queue_questions(
        [
            MaterialQuestion(
                id=gate,
                concern_id="spu",
                prompt="Grant antipattern-suppression to spu?",
                choices=AllowanceRuling.choices(),
                closed_choices=asks_for_an_allowance("spu", gate),
            )
        ],
        "spu",
    )
    seed_offer(
        core, gate, "Granted. Your reading is accepted: the violations do not change."
    )

    problems = core.questions.promote_offers()

    assert problems and "accepts only: grant, refuse" in problems[0]
    assert core.mailbox.answers() == []


def admitted_plan(*concerns: JsonObject) -> JsonObject:
    """One planner reply admitting concerns against a run's new evidence."""
    return {"concerns": [item for item in concerns]}


def admitted_concern(
    identifier: str,
    dependencies: list[str] | None = None,
    questions: list[JsonObject] | None = None,
) -> JsonObject:
    return {
        "id": identifier,
        "title": identifier.title(),
        "spec": f"Resolve {identifier}",
        "criteria": [{"id": f"{identifier}-done", "description": "done"}],
        "dependencies": [parent for parent in dependencies or []],
        "questions": [question for question in questions or []],
        "evidence_indexes": [0],
    }


def admitting_core(
    tmp_path: Path,
    workspace: Path,
    launcher: LocalProcessLauncher,
    run_id: str,
    worker_response: ResolverResponse,
    reviewer_response: ResolverResponse,
) -> ResolverCore:
    return ResolverCore(
        ResolverConfig(
            state_root=tmp_path / "state",
            workspace=workspace,
            worktree_root=tmp_path / "resolver-worktrees",
            run_id=run_id,
            integration_branch=f"resolve/{run_id}/review",
            verification_commands=[
                VerificationCommand(
                    name="combined-diff", arguments=["git", "diff", "--check", "HEAD"]
                )
            ],
        ),
        resolve_spec(),
        worker_recipe(tmp_path / "state", launcher, worker_response),
        lambda context: resolver_test_factory(context.root, reviewer_response),
        LiteralInvocationRenderer(),
        launcher,
    )


def admitted(identifier: str, evidence: str = "a human said so") -> Concern:
    """One concern as an admission records it: origin and evidence carried."""
    return concern(identifier).model_copy(
        update={"origin": ConcernOrigin.ADMITTED, "evidence": evidence}
    )


def implementing_worker(
    parks: dict[str, str],  # lup: ignore[dict-str-payload] — concern-id index
    state_root: Path,
    run_id: str,
) -> ResolverResponse:
    """A worker that parks each named concern once, then implements it.

    ``parks`` maps a concern id to the question it asks before doing any
    work, which is what puts a run at the boundary a human discovers more
    work from.
    """

    def respond(root: Path, output_name: str) -> JsonObject:
        if output_name == MergeReport.__name__:
            return {"completed": True, "summary": "semantic join reviewed"}
        if output_name != WorkerReport.__name__:
            raise AssertionError(output_name)
        identifier = root.name
        if identifier in parks:
            mailbox = QuestionMailbox(state_root / run_id)
            worker_asks(mailbox, run_id, dynamic_question(identifier))
            if parks[identifier] not in mailbox.answered_ids():
                return {
                    "concern_id": identifier,
                    "changed": False,
                    "summary": "parked awaiting a material choice",
                }
        (root / f"{identifier}.txt").write_text("durable\n", encoding="utf-8")
        return {
            "concern_id": identifier,
            "changed": True,
            "summary": f"implemented {identifier}",
            "files_changed": [f"{identifier}.txt"],
        }

    return respond


def planning_reviewer(plan: JsonObject, *concerns: str) -> ResolverResponse:
    """A reviewer that plans admitted evidence and accepts every concern.

    A per-concern review runs in that concern's own lease, so the worktree
    name is the concern id and echoing it reports the criterion met. The
    post-integration re-check does not: it runs in the integration lease and
    asks about a concern named only in the prompt, so the same echo reports
    a foreign label and every criterion lost. Naming this run's concerns
    tells the two worktrees apart, which is what makes the fixture mean
    "accepts every concern" at both call sites instead of only the first.
    """

    def respond(root: Path, output_name: str) -> JsonObject:
        if output_name == ConcernInventory.__name__:
            return plan
        rechecking = bool(concerns) and root.name not in concerns
        met: list[JsonValue] = (
            [f"{name}-done" for name in concerns]
            if rechecking
            else [f"{root.name}-done"]
        )
        return {
            "concern_id": root.name,
            "accepted": True,
            "generalized": True,
            "reason": "criteria met",
            "criteria_met": met,
        }

    return respond


@pytest.mark.asyncio
async def test_a_concern_admitted_into_a_parked_run_finishes_beside_the_originals(
    tmp_path: Path,
) -> None:
    """Discovery mid-run cost a restart, which threw away every answer.

    The run keeps its id, its recorded answers, and its completed work; the
    admitted concern still passes the approval and material-question gates
    its siblings passed.
    """
    launcher = LocalProcessLauncher()
    workspace = failure_leg_workspace(tmp_path, launcher)

    def build_core() -> ResolverCore:
        return admitting_core(
            tmp_path,
            workspace,
            launcher,
            "admit-parked",
            implementing_worker({"a": "a-dynamic"}, tmp_path / "state", "admit-parked"),
            planning_reviewer(
                admitted_plan(
                    admitted_concern(
                        "b",
                        questions=[
                            {
                                "id": "b-shape",
                                "concern_id": "b",
                                "prompt": "Which shape?",
                                "choices": ["a method"],
                            }
                        ],
                    )
                )
            ),
        )

    parked = build_core()
    seed_approvals(parked, [concern("a")])
    with pytest.raises(ResolverAwaitingAnswers):
        await parked.run(
            ResolveInventory(
                source=snapshot(workspace, launcher), concerns=[concern("a")]
            )
        )
    before = parked.repository.load()

    admission = await build_core().admit(
        AdmissionRequest(statements=["the relay has to investigate before it asks"])
    )

    assert admission.run_id == "admit-parked"
    assert [item.id for item in admission.concerns] == ["b"]
    assert admission.concerns[0].origin is ConcernOrigin.ADMITTED
    assert admission.concerns[0].evidence == (
        "the relay has to investigate before it asks"
    )
    assert [question.id for question in admission.questions] == [
        "b-shape",
        "integration-approval-b",
    ]
    widened = build_core().repository.load()
    assert [item.id for item in widened.concerns] == ["a", "b"]
    assert widened.answers == before.answers
    assert widened.phase == before.phase
    # The questions join the run's batch, not just the admission's reply: a
    # concern admitted with its gates unwritten is one nothing can answer.
    assert widened.questions is not None
    assert {"b-shape", "integration-approval-b"} <= {
        question.id for question in widened.questions.questions
    }

    ungated = build_core()
    seed_offer(ungated, "a-dynamic", "durable")
    seed_offer(ungated, "b-shape", "a method")
    with pytest.raises(ResolverAwaitingAnswers) as gated:
        await ungated.resume()
    assert [question.id for question in gated.value.pending] == [
        "integration-approval-b"
    ]

    resumed = build_core()
    seed_offer(resumed, "integration-approval-b", ConcernApproval.APPROVE)
    manifest = await resumed.resume()

    assert sorted(
        outcome.concern_id for outcome in manifest.outcomes if outcome.verified
    ) == ["a", "b"]
    assert all(record.passed for record in manifest.verification)
    persisted = resumed.repository.load()
    assert persisted.answers is not None
    assert {answer.question_id for answer in persisted.answers.answers} == {
        "integration-approval-a",
        "a-dynamic",
        "b-shape",
        "integration-approval-b",
        ASSEMBLY_QUESTION_ID,
    }
    assert [item.status for item in persisted.progress if item.concern_id == "b"] == [
        ConcernStatus.CLEANED
    ]


@pytest.mark.asyncio
async def test_an_answer_offered_with_an_admission_settles_its_new_question(
    tmp_path: Path,
) -> None:
    """The run's own rerun recipe hands out the flags this combines.

    An answer offered beside an admission is offered before the question it
    names exists, so only the admission that creates the question can settle
    it. Leaving it unpromoted admitted the concern and discarded the answer.
    """
    launcher = LocalProcessLauncher()
    workspace = failure_leg_workspace(tmp_path, launcher)

    def build_core() -> ResolverCore:
        return admitting_core(
            tmp_path,
            workspace,
            launcher,
            "admit-answered",
            implementing_worker(
                {"a": "a-dynamic"}, tmp_path / "state", "admit-answered"
            ),
            planning_reviewer(
                admitted_plan(
                    admitted_concern(
                        "b",
                        questions=[
                            {
                                "id": "b-shape",
                                "concern_id": "b",
                                "prompt": "Which shape?",
                                "choices": ["a method"],
                            }
                        ],
                    )
                )
            ),
        )

    parked = build_core()
    seed_approvals(parked, [concern("a")])
    with pytest.raises(ResolverAwaitingAnswers):
        await parked.run(
            ResolveInventory(
                source=snapshot(workspace, launcher), concerns=[concern("a")]
            )
        )

    admitting = build_core()
    seed_offer(admitting, "b-shape", "a method")
    admission = await admitting.admit(
        AdmissionRequest(statements=["the relay answered while admitting"])
    )

    assert admission.rejected == []
    assert [question.id for question in admission.questions] == [
        "b-shape",
        "integration-approval-b",
    ]
    # Only the gate the flag did not name is still owed to the human.
    assert [question.id for question in admission.outstanding] == [
        "integration-approval-b"
    ]
    persisted = build_core().repository.load()
    assert persisted.answers is not None
    assert {
        answer.question_id: answer.value for answer in persisted.answers.answers
    } | {"b-shape": "a method"} == {
        answer.question_id: answer.value for answer in persisted.answers.answers
    }


@pytest.mark.asyncio
async def test_an_admitted_concern_bases_on_a_completed_concerns_recorded_commit(
    tmp_path: Path,
) -> None:
    """Work discovered *because* an earlier concern landed may depend on it.

    One admission carries as many concerns as its evidence needs, so `d`
    rides in beside `c` rather than costing a second pass.
    """
    launcher = LocalProcessLauncher()
    workspace = failure_leg_workspace(tmp_path, launcher)

    def build_core() -> ResolverCore:
        return admitting_core(
            tmp_path,
            workspace,
            launcher,
            "admit-dependent",
            implementing_worker(
                {"b": "b-dynamic"}, tmp_path / "state", "admit-dependent"
            ),
            planning_reviewer(
                admitted_plan(admitted_concern("c", ["a"]), admitted_concern("d")),
                "a",
                "b",
                "c",
                "d",
            ),
        )

    parked = build_core()
    seed_approvals(parked, [concern("a"), concern("b")])
    with pytest.raises(ResolverAwaitingAnswers):
        await parked.run(
            ResolveInventory(
                source=snapshot(workspace, launcher),
                concerns=[concern("a"), concern("b")],
            )
        )
    completed = next(
        outcome
        for outcome in parked.repository.load().outcomes
        if outcome.concern_id == "a"
    )
    assert completed.verified and completed.commit is not None

    admission = await build_core().admit(
        AdmissionRequest(statements=["the landed change needs a follow-up"])
    )
    assert [item.id for item in admission.concerns] == ["c", "d"]

    resumed = build_core()
    seed_offer(resumed, "b-dynamic", "durable")
    seed_offer(resumed, "integration-approval-c", ConcernApproval.APPROVE)
    seed_offer(resumed, "integration-approval-d", ConcernApproval.APPROVE)
    manifest = await resumed.resume()

    assert sorted(
        outcome.concern_id for outcome in manifest.outcomes if outcome.verified
    ) == ["a", "b", "c", "d"]
    base = next(
        item for item in resumed.repository.load().bases if item.concern_id == "c"
    )
    assert base.parent_concerns == ["a"]
    assert base.parent_commits == [completed.commit]
    assert base.commit == completed.commit


@pytest.mark.asyncio
async def test_admission_refuses_a_reused_lease_a_cycle_and_a_missing_parent(
    tmp_path: Path,
) -> None:
    """Nothing is persisted unless the widened graph and leases both hold."""
    launcher = LocalProcessLauncher()
    workspace = failure_leg_workspace(tmp_path, launcher)

    def build_core(plan: JsonObject) -> ResolverCore:
        return admitting_core(
            tmp_path,
            workspace,
            launcher,
            "admit-refused",
            implementing_worker(
                {"a": "a-dynamic"}, tmp_path / "state", "admit-refused"
            ),
            planning_reviewer(plan),
        )

    parked = build_core(admitted_plan(admitted_concern("b")))
    seed_approvals(parked, [concern("a")])
    with pytest.raises(ResolverAwaitingAnswers):
        await parked.run(
            ResolveInventory(
                source=snapshot(workspace, launcher), concerns=[concern("a")]
            )
        )
    evidence = AdmissionRequest(statements=["something else is broken"])

    with pytest.raises(LeaseViolationError, match="already has a lease"):
        await build_core(admitted_plan(admitted_concern("a"))).admit(evidence)
    with pytest.raises(DependencyGraphError, match="contains a cycle"):
        await build_core(
            admitted_plan(admitted_concern("c", ["d"]), admitted_concern("d", ["c"]))
        ).admit(evidence)
    with pytest.raises(DependencyGraphError, match="missing nodes"):
        await build_core(admitted_plan(admitted_concern("e", ["absent"]))).admit(
            evidence
        )

    survivor = build_core(admitted_plan(admitted_concern("b")))
    assert [item.id for item in survivor.repository.load().concerns] == ["a"]


@pytest.mark.asyncio
async def test_admission_is_refused_once_the_review_branch_is_assembled(
    tmp_path: Path,
) -> None:
    """Past integration a joining concern would have to reopen the branch."""
    run_id = "admit-late"
    core = ResolverCore(
        ResolverConfig(
            state_root=tmp_path / "state",
            workspace=tmp_path,
            worktree_root=tmp_path / "worktrees",
            run_id=run_id,
            integration_branch="resolve/review",
            verification_commands=[
                VerificationCommand(name="tests", arguments=["pytest"])
            ],
        ),
        resolve_spec(),
        lambda _cwd: unused_session_factory(),
        lambda _cwd: unused_session_factory(),
        UnusedInvocationRenderer(),
        LocalProcessLauncher(),
    )
    core.persist(
        ResolveState(
            config_digest="config-sha",
            run_id=run_id,
            phase=ResolvePhase.INTEGRATION,
            source=SourceSnapshot(branch="feature", commit="source-sha"),
            spec=resolve_spec(),
            concerns=[concern("a")],
            progress=[
                ConcernProgress(concern_id="a", status=ConcernStatus.INTEGRATING)
            ],
        )
    )

    with pytest.raises(ResolverInvariantError, match="review branch is assembled"):
        await core.admit(AdmissionRequest(statements=["too late"]))


def test_a_recorded_concern_is_immutable_while_a_later_one_may_join(
    tmp_path: Path,
) -> None:
    """Widening is the only edit the concern set accepts."""
    state = ResolveState(
        config_digest="config-sha",
        run_id="run-1",
        phase=ResolvePhase.INVENTORY,
        source=SourceSnapshot(branch="feature", commit="source-sha"),
        spec=resolve_spec(),
        concerns=[concern("a")],
        progress=[ConcernProgress(concern_id="a")],
    )
    repository = ResolverStateRepository(tmp_path, "run-1")
    repository.save(state)
    joined = admitted("b")

    repository.save(
        state.model_copy(
            update={
                "concerns": [concern("a"), joined],
                "progress": [
                    ConcernProgress(concern_id="a"),
                    ConcernProgress(concern_id="b"),
                ],
            }
        )
    )

    assert [item.id for item in repository.load().concerns] == ["a", "b"]
    with pytest.raises(StateTransitionError, match="recorded resolver concern"):
        repository.save(
            state.model_copy(
                update={
                    "concerns": [joined],
                    "progress": [ConcernProgress(concern_id="b")],
                }
            )
        )
    with pytest.raises(StateTransitionError, match="cover every concern"):
        repository.save(
            state.model_copy(
                update={
                    "concerns": [concern("a"), joined],
                    "progress": [ConcernProgress(concern_id="a")],
                }
            )
        )


def test_an_admitted_concern_must_cite_what_raised_it() -> None:
    """A concern from intake is grounded in notes; an admitted one says so."""
    with pytest.raises(ValueError, match="cites no evidence"):
        Concern.model_validate(
            concern("a").model_dump() | {"origin": ConcernOrigin.ADMITTED}
        )


class RecordingObserver(ResolverObserver):
    def __init__(self) -> None:
        self.phases: list[ResolvePhase] = []
        self.transitions: list[ConcernProgress] = []
        self.tallies: list[RunTally] = []

    def phase_changed(self, phase: ResolvePhase) -> None:
        self.phases.append(phase)

    def concern_changed(self, progress: ConcernProgress) -> None:
        self.transitions.append(progress)

    def tally_changed(self, tally: RunTally) -> None:
        self.tallies.append(tally)


@pytest.mark.asyncio
async def test_observer_receives_every_persisted_transition_in_order(
    tmp_path: Path,
) -> None:
    launcher = LocalProcessLauncher()
    workspace = failure_leg_workspace(tmp_path, launcher)

    def worker_response(root: Path, output_name: str) -> JsonObject:
        if output_name == MergeReport.__name__:
            return {"completed": True, "summary": "semantic join reviewed"}
        (root / "a.txt").write_text("done\n", encoding="utf-8")
        return {
            "concern_id": "a",
            "changed": True,
            "summary": "implemented a",
            "files_changed": ["a.txt"],
        }

    def reviewer_response(_root: Path, output_name: str) -> JsonObject:
        return {
            "concern_id": "a",
            "accepted": True,
            "generalized": True,
            "reason": "criteria met",
            "criteria_met": ["a-done"],
        }

    observer = RecordingObserver()
    core = ResolverCore(
        ResolverConfig(
            state_root=tmp_path / "state",
            workspace=workspace,
            worktree_root=tmp_path / "resolver-worktrees",
            run_id="observed",
            integration_branch="resolve/observed/review",
            verification_commands=[
                VerificationCommand(
                    name="combined-diff",
                    arguments=["git", "diff", "--check", "HEAD"],
                )
            ],
        ),
        resolve_spec(),
        worker_recipe(tmp_path / "state", launcher, worker_response),
        lambda context: resolver_test_factory(context.root, reviewer_response),
        LiteralInvocationRenderer(),
        launcher,
        observer=observer,
    )

    seed_approvals(core, [concern("a")])
    await core.run(
        ResolveInventory(source=snapshot(workspace, launcher), concerns=[concern("a")])
    )

    assert observer.phases == [
        ResolvePhase.INVENTORY,
        ResolvePhase.QUESTIONS,
        ResolvePhase.ELIGIBILITY,
        ResolvePhase.DAG,
        ResolvePhase.LEASES,
        ResolvePhase.WORKERS,
        ResolvePhase.DEPENDENCY_BASES,
        ResolvePhase.REVIEW,
        ResolvePhase.INTEGRATION,
        ResolvePhase.VERIFICATION,
        ResolvePhase.CLEANUP,
        ResolvePhase.COMPLETE,
    ]
    assert [item.status for item in observer.transitions] == [
        ConcernStatus.DISCOVERED,
        ConcernStatus.WAITING_FOR_ANSWERS,
        ConcernStatus.ELIGIBLE,
        ConcernStatus.LEASED,
        ConcernStatus.RUNNING,
        ConcernStatus.VALIDATING,
        ConcernStatus.REVIEWING,
        ConcernStatus.VERIFIED,
        ConcernStatus.INTEGRATING,
        ConcernStatus.INTEGRATED,
        ConcernStatus.CLEANED,
    ]
    assert {item.concern_id for item in observer.transitions} == {"a"}
    # Every status move above changed the aggregate, so the tally followed
    # each one; the last word is the whole run accounted for, joins included.
    assert observer.tallies, "aggregate progress never reached the observer"
    final = observer.tallies[-1]
    assert final.phase == ResolvePhase.COMPLETE
    assert final.by_status == {ConcernStatus.CLEANED: 1}
    # One concern joins without a pairwise sequence, so no join figures.
    assert (final.joined, final.join_total) == (0, 0)
    assert final.concerns_line() == "cleaned 1 of 1"


def noted_workspace(tmp_path: Path, launcher: LocalProcessLauncher) -> Path:
    """A source tree whose module carries two concerns' notes at once."""
    workspace = failure_leg_workspace(tmp_path, launcher)
    (workspace / "module.py").write_text(
        "# lup: rework the dispatch\nvalue = 1\n# lup: belongs to another concern\n",
        encoding="utf-8",
    )
    for arguments in (["add", "module.py"], ["commit", "-m", "noted"]):
        status = launcher.launch(
            LaunchRequest(arguments=["git", *arguments], cwd=workspace)
        )
        assert status.code == 0, status.stderr
    return workspace


def noted_concern() -> Concern:
    return Concern(
        id="a",
        title="A",
        spec="Resolve a",
        criteria=[AcceptanceCriterion(id="a-done", description="done")],
        notes=[ReviewNote(file=Path("module.py"), line=1, text="rework the dispatch")],
        integration_approved=True,
    )


def accepting_reviewer(_root: Path, output_name: str) -> JsonObject:
    return {
        "concern_id": "a",
        "accepted": True,
        "generalized": True,
        "reason": "criteria met",
        "criteria_met": ["a-done"],
    }


@pytest.mark.asyncio
async def test_a_concerns_notes_are_cleared_before_its_worker_runs(
    tmp_path: Path,
) -> None:
    """Who removes the marker, and how much of the file they may touch.

    A worker told to remove its own marker meets the edit policy, which asks
    on that unconditionally, and every concern parks. The orchestrator
    removes it first — and removes only what this concern owns, leaving the
    sibling's note in the same file untouched.
    """
    launcher = LocalProcessLauncher()
    workspace = noted_workspace(tmp_path, launcher)
    seen: list[str] = []

    def worker_response(root: Path, output_name: str) -> JsonObject:
        if output_name == MergeReport.__name__:
            return {"completed": True, "summary": "joined"}
        seen.append((root / "module.py").read_text(encoding="utf-8"))
        (root / "a.txt").write_text("implemented\n", encoding="utf-8")
        return {
            "concern_id": "a",
            "changed": True,
            "summary": "implemented a",
            "files_changed": ["a.txt"],
        }

    core = failure_leg_core(
        tmp_path,
        workspace,
        launcher,
        "note-clearance",
        worker_response,
        accepting_reviewer,
    )
    seed_approvals(core, [noted_concern()])
    manifest = await core.run(
        ResolveInventory(
            source=snapshot(workspace, launcher), concerns=[noted_concern()]
        )
    )

    outcome = next(item for item in manifest.outcomes if item.concern_id == "a")
    assert outcome.verified, outcome.failure
    assert [note.text for note in outcome.notes_cleared] == ["rework the dispatch"]
    assert seen and "rework the dispatch" not in seen[0]
    assert "belongs to another concern" in seen[0]


@pytest.mark.asyncio
async def test_clearance_commits_separately_from_the_workers_change(
    tmp_path: Path,
) -> None:
    """Its own commit is what keeps the diff-equality invariant strict."""
    launcher = LocalProcessLauncher()
    workspace = noted_workspace(tmp_path, launcher)

    def worker_response(root: Path, output_name: str) -> JsonObject:
        if output_name == MergeReport.__name__:
            return {"completed": True, "summary": "joined"}
        (root / "a.txt").write_text("implemented\n", encoding="utf-8")
        return {
            "concern_id": "a",
            "changed": True,
            "summary": "implemented a",
            "files_changed": ["a.txt"],
        }

    core = failure_leg_core(
        tmp_path,
        workspace,
        launcher,
        "note-commit",
        worker_response,
        accepting_reviewer,
    )
    seed_approvals(core, [noted_concern()])
    manifest = await core.run(
        ResolveInventory(
            source=snapshot(workspace, launcher), concerns=[noted_concern()]
        )
    )

    outcome = next(item for item in manifest.outcomes if item.concern_id == "a")
    assert outcome.commit is not None
    subjects = launcher.launch(
        LaunchRequest(
            arguments=["git", "log", "--format=%s", "-3", outcome.commit],
            cwd=workspace,
        )
    )
    assert "resolve: clear review notes for a" in subjects.stdout


@pytest.mark.asyncio
async def test_a_concern_with_no_notes_clears_nothing(tmp_path: Path) -> None:
    """No note, no commit — the worker's base stays the dependency base."""
    launcher = LocalProcessLauncher()
    workspace = noted_workspace(tmp_path, launcher)

    def worker_response(root: Path, output_name: str) -> JsonObject:
        if output_name == MergeReport.__name__:
            return {"completed": True, "summary": "joined"}
        (root / "a.txt").write_text("implemented\n", encoding="utf-8")
        return {
            "concern_id": "a",
            "changed": True,
            "summary": "implemented a",
            "files_changed": ["a.txt"],
        }

    core = failure_leg_core(
        tmp_path, workspace, launcher, "note-free", worker_response, accepting_reviewer
    )
    seed_approvals(core, [concern("a")])
    manifest = await core.run(
        ResolveInventory(source=snapshot(workspace, launcher), concerns=[concern("a")])
    )

    outcome = next(item for item in manifest.outcomes if item.concern_id == "a")
    assert outcome.verified, outcome.failure
    assert outcome.notes_cleared == []


@pytest.mark.asyncio
async def test_a_granted_allowance_reaches_the_sessions_launched_next(
    tmp_path: Path,
) -> None:
    """A "grant" answer to `request_allowance` is machinery, not a wish.

    The concern declared no allowance at plan time; the grant arrives
    through the mailbox, so every session launched after its promotion —
    here the first worker — carries the gate in its context, where before
    the answer reached nobody.
    """
    launcher = LocalProcessLauncher()
    workspace = failure_leg_workspace(tmp_path, launcher)
    carried: list[list[str]] = []

    def worker_response(root: Path, output_name: str) -> JsonObject:
        if output_name == MergeReport.__name__:
            return {"completed": True, "summary": "joined"}
        (root / "a.txt").write_text("implemented\n", encoding="utf-8")
        return {
            "concern_id": "a",
            "changed": True,
            "summary": "implemented a",
            "files_changed": ["a.txt"],
        }

    granting = worker_recipe(tmp_path / "state", launcher, worker_response)

    def recording_worker_factory(context: WorkerContext) -> Agent:
        carried.append(context.grants.granted())
        return granting(context)

    core = ResolverCore(
        ResolverConfig(
            state_root=tmp_path / "state",
            workspace=workspace,
            worktree_root=tmp_path / "resolver-worktrees",
            run_id="mid-run-grant",
            integration_branch="resolve/mid-run-grant/review",
            verification_commands=[
                VerificationCommand(
                    name="combined-diff",
                    arguments=["git", "diff", "--check", "HEAD"],
                )
            ],
        ),
        resolve_spec(),
        recording_worker_factory,
        lambda context: resolver_test_factory(context.root, accepting_reviewer),
        LiteralInvocationRenderer(),
        launcher,
    )
    gate = allowance_question_id("a", ConcernAllowance.ANTIPATTERN_SUPPRESSION)
    worker_asks(
        core.mailbox,
        "mid-run-grant",
        MaterialQuestion(
            id=gate,
            concern_id="a",
            prompt="Grant `antipattern-suppression` to a?",
            choices=["grant", "refuse"],
            closed_choices=True,
        ),
    )
    seed_offer(core, gate, "grant")
    seed_approvals(core, [concern("a")])
    manifest = await core.run(
        ResolveInventory(source=snapshot(workspace, launcher), concerns=[concern("a")])
    )

    outcome = next(item for item in manifest.outcomes if item.concern_id == "a")
    assert outcome.verified, outcome.failure
    assert concern("a").allowances == []
    assert carried and all(
        ConcernAllowance.ANTIPATTERN_SUPPRESSION.value in launch for launch in carried
    )


class PromptRecordingSession(ResolverTestSession):
    """A scripted session that also records every prompt it was handed."""

    def __init__(
        self,
        root: Path,
        response: ResolverResponse,
        log: list[str],
        joining: JoinDriver | None = None,
    ) -> None:
        super().__init__(root, response, joining)
        self.log = log

    async def start[T: BaseModel | None](
        self, request: TurnRequest[T]
    ) -> StartedTurn[T]:
        self.log.append(request.input.text)
        return await super().start(request)


def journal_events(core: ResolverCore, kind: str) -> list[JsonObject]:
    dumps = [entry.event.model_dump(mode="json") for entry in core.journal.read()]
    return [dump for dump in dumps if "type" in dump and dump["type"] == kind]


def recheck_core(
    tmp_path: Path, run_id: str, reviewer_response: ResolverResponse, log: list[str]
) -> ResolverCore:
    return ResolverCore(
        ResolverConfig(
            state_root=tmp_path / "state",
            workspace=tmp_path,
            worktree_root=tmp_path / "worktrees",
            run_id=run_id,
            integration_branch=f"resolve/{run_id}/review",
            verification_commands=[
                VerificationCommand(name="verify", arguments=["git", "diff"])
            ],
        ),
        resolve_spec(),
        worker_recipe(tmp_path / "state", recording_launcher(), reviewer_response),
        lambda context: agent_over(
            PromptRecordingSession(context.root, reviewer_response, log)
        ),
        LiteralInvocationRenderer(),
        recording_launcher(),
    )


@pytest.mark.asyncio
async def test_recheck_prompt_carries_the_record_and_corrects_foreign_labels(
    tmp_path: Path,
) -> None:
    """The re-reviewer reads the declared criteria instead of reconstructing
    them by archaeology, and a report labelled outside them is corrected once
    on the same session rather than counted as every criterion lost."""
    reports: list[JsonObject] = [
        {
            "concern_id": "a",
            "accepted": True,
            "generalized": True,
            "reason": "all criteria hold",
            "criteria_met": ["guidance-roster-done"],
        },
        {
            "concern_id": "a",
            "accepted": True,
            "generalized": True,
            "reason": "all criteria hold",
            "criteria_met": ["a-done"],
        },
    ]
    calls: list[str] = []
    log: list[str] = []

    def reviewer_response(_root: Path, _output_name: str) -> JsonObject:
        calls.append("turn")
        return reports[min(len(calls) - 1, 1)]

    core = recheck_core(tmp_path, "recheck-record", reviewer_response, log)
    await core.joiner.recheck_concern(
        concern("a"),
        tmp_path,
        situation="Re-check after a sibling landed.",
        occasion="join-abc123",
        lost_because="once the sibling joined",
    )

    assert '"a-done"' in log[0]
    assert "declared criterion ids" in log[0]
    assert "never declared: guidance-roster-done" in log[1]
    assert [item.question.id for item in core.mailbox.questions()] == []


@pytest.mark.parametrize("published", [False, True])
async def test_recheck_resume_replays_verdict_across_question_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, published: bool
) -> None:
    calls: list[str] = []

    def response(_root: Path, _output_name: str) -> JsonObject:
        calls.append("review")
        return {
            "concern_id": "a",
            "accepted": False,
            "generalized": True,
            "reason": "criterion lost",
            "criteria_met": [],
        }

    core = recheck_core(tmp_path, "recheck-interrupted", response, [])
    queue = core.questions.queue_questions

    def interrupted(questions: list[MaterialQuestion], asked_by: str) -> None:
        if published:
            queue(questions, asked_by)
        raise RuntimeError("interrupted publication")

    monkeypatch.setattr(core.questions, "queue_questions", interrupted)
    with pytest.raises(RuntimeError, match="interrupted publication"):
        await core.joiner.recheck_concern(
            concern("a"),
            tmp_path,
            situation="integrated",
            occasion="integrated",
            lost_because="after integration",
            commit="first-tree",
        )
    await core.joiner.runner.actors.close()
    resumed = recheck_core(tmp_path, "recheck-interrupted", response, [])
    question = await resumed.joiner.recheck_concern(
        concern("a"),
        tmp_path,
        situation="resumed",
        occasion="integrated",
        lost_because="after integration",
        commit="first-tree",
    )
    assert calls == ["review"]
    assert question is not None and question.recheck_commit == "first-tree"
    assert [pending.question for pending in resumed.mailbox.questions()] == [question]
    resumed.mailbox.record(
        RecordedAnswer(
            run_id="recheck-interrupted",
            answer=QuestionAnswer(question_id=question.id, value="superseded"),
            door=AnswerDoor.CONSOLE,
            answered_at=utc_now(),
        )
    )
    again = await resumed.joiner.recheck_concern(
        concern("a"),
        tmp_path,
        situation="resumed again",
        occasion="integrated",
        lost_because="after integration",
        commit="first-tree",
    )
    assert again == question
    assert resumed.mailbox.answered_ids() == [question.id]
    assert calls == ["review"]
    await resumed.joiner.runner.actors.close()


async def test_recheck_changed_tree_preserves_old_question_and_answer(
    tmp_path: Path,
) -> None:
    calls: list[str] = []

    def response(_root: Path, _output_name: str) -> JsonObject:
        calls.append("review")
        return {
            "concern_id": "a",
            "accepted": False,
            "generalized": True,
            "reason": "criterion finding",
            "criteria_met": ["a-done"] if len(calls) == 1 else [],
        }

    core = recheck_core(tmp_path, "recheck-repaired", response, [])
    item = concern("a").model_copy(
        update={
            "criteria": [
                *concern("a").criteria,
                AcceptanceCriterion(id="a-more", description="more"),
            ]
        }
    )
    first = await core.joiner.recheck_concern(
        item,
        tmp_path,
        situation="integrated",
        occasion="integrated",
        lost_because="after integration",
        commit="first-tree",
    )
    assert first is not None
    core.mailbox.record(
        RecordedAnswer(
            run_id="recheck-repaired",
            answer=QuestionAnswer(question_id=first.id, value="superseded"),
            door=AnswerDoor.CONSOLE,
            answered_at=utc_now(),
        )
    )
    second = await core.joiner.recheck_concern(
        item,
        tmp_path,
        situation="repaired",
        occasion="integrated",
        lost_because="after repair",
        commit="repaired-tree",
    )
    assert second is not None and second.id != first.id
    assert first.criteria == ["a-more"]
    assert second.criteria == ["a-done", "a-more"]
    assert core.mailbox.answered_ids() == [first.id]
    assert {pending.question.id for pending in core.mailbox.questions()} == {
        first.id,
        second.id,
    }
    changed = journal_events(core, "recheck_changed")
    assert len(changed) == 1 and changed[0]["commit"] == "repaired-tree"
    await core.joiner.runner.actors.close()


def standing_state(run_id: str, ruling: str | None) -> ResolveState:
    asked = MaterialQuestion(
        id="a-superseded-join-one",
        concern_id="a",
        prompt="a no longer meets a-done. Superseded or regression?",
        choices=["superseded", "regression"],
        closed_choices=True,
        criteria=["a-done"],
    )
    return ResolveState(
        config_digest="digest",
        run_id=run_id,
        phase=ResolvePhase.INTEGRATION,
        source=SourceSnapshot(branch="feature", commit="source-sha"),
        spec=resolve_spec(),
        concerns=[concern("a")],
        progress=[ConcernProgress(concern_id="a", status=ConcernStatus.VERIFIED)],
        questions=QuestionBatch(run_id=run_id, questions=[asked]),
        answers=AnswerBatch(
            run_id=run_id,
            answers=(
                [QuestionAnswer(question_id=asked.id, value=ruling)]
                if ruling is not None
                else []
            ),
        ),
    )


def test_a_standing_ruling_settles_the_same_lost_set(tmp_path: Path) -> None:
    """One open or superseded question holds the set; a regression ruling —
    whose remediation makes a later identical loss a new fact — does not."""
    core = planning_core(tmp_path, lambda *_: plan_of())

    core.state = standing_state("rulings", "superseded")
    assert core.joiner.standing_ruling_exists("a", ["a-done"])
    assert not core.joiner.standing_ruling_exists("a", ["a-done", "a-extra"])
    assert not core.joiner.standing_ruling_exists("b", ["a-done"])

    core.state = standing_state("rulings", None)
    assert core.joiner.standing_ruling_exists("a", ["a-done"])

    core.state = standing_state("rulings", "regression")
    assert not core.joiner.standing_ruling_exists("a", ["a-done"])


@pytest.mark.asyncio
async def test_an_identical_standing_finding_is_recorded_not_reasked(
    tmp_path: Path,
) -> None:
    """A lost-set already settled is not asked again in the same run; it
    lands in the journal, not the mailbox."""
    log: list[str] = []

    def reviewer_response(_root: Path, _output_name: str) -> JsonObject:
        return {
            "concern_id": "a",
            "accepted": False,
            "generalized": True,
            "reason": "the criterion no longer holds",
            "criteria_met": [],
        }

    core = recheck_core(tmp_path, "recheck-dedup", reviewer_response, log)
    core.state = standing_state("recheck-dedup", "superseded")
    await core.joiner.recheck_concern(
        concern("a"),
        tmp_path,
        situation="Re-check after another join.",
        occasion="join-def456",
        lost_because="once the later join landed",
    )

    assert [item.question.id for item in core.mailbox.questions()] == []
    repeated = journal_events(core, "recheck_repeated")
    assert repeated and repeated[0]["criteria"] == ["a-done"]
    assert repeated[0]["occasion"] == "join-def456"


@pytest.mark.asyncio
async def test_completeness_guard_appends_and_names_the_gap(tmp_path: Path) -> None:
    """The human sends it back, and the exact unmatched ids ride with it.

    The guard does not decide this alone — the criteria are the human's bar,
    so whether missing one still passes is theirs to say. Refusing to carry
    the gap reaches the worker exactly as an automatic rejection would, which
    is what this pins.
    """
    launcher = LocalProcessLauncher()
    workspace = failure_leg_workspace(tmp_path, launcher)
    worker_prompts: list[str] = []
    reviews: list[JsonObject] = [
        {
            "concern_id": "a",
            "accepted": True,
            "generalized": True,
            "reason": "solid work overall",
            "criteria_met": [],
        },
        {
            "concern_id": "a",
            "accepted": True,
            "generalized": True,
            "reason": "criteria met",
            "criteria_met": ["a-done"],
        },
    ]
    handed: list[str] = []

    def reviewer_response(_root: Path, _output_name: str) -> JsonObject:
        handed.append("turn")
        return reviews[min(len(handed) - 1, 1)]

    def worker_response(root: Path, output_name: str) -> JsonObject:
        if output_name == MergeReport.__name__:
            return {"completed": True, "summary": "joined"}
        with (root / "a.txt").open("a", encoding="utf-8") as handle:
            handle.write("implemented\n")
        return {
            "concern_id": "a",
            "changed": True,
            "summary": "implemented a",
            "files_changed": ["a.txt"],
        }

    core = ResolverCore(
        ResolverConfig(
            state_root=tmp_path / "state",
            workspace=workspace,
            worktree_root=tmp_path / "resolver-worktrees",
            run_id="guard-append",
            integration_branch="resolve/guard-append/review",
            verification_commands=[
                VerificationCommand(
                    name="combined-diff",
                    arguments=["git", "diff", "--check", "HEAD"],
                )
            ],
        ),
        resolve_spec(),
        recording_worker_recipe(
            tmp_path / "state", launcher, worker_response, worker_prompts
        ),
        lambda context: resolver_test_factory(context.root, reviewer_response),
        LiteralInvocationRenderer(),
        launcher,
    )
    seed_approvals(core, [concern("a")])
    seed_offer(core, "a-residual-round-1", ResidualRuling.SEND_BACK)
    manifest = await core.run(
        ResolveInventory(source=snapshot(workspace, launcher), concerns=[concern("a")])
    )

    outcome = next(item for item in manifest.outcomes if item.concern_id == "a")
    assert outcome.verified, outcome.failure
    revision = next(prompt for prompt in worker_prompts if "Review feedback" in prompt)
    assert "solid work overall" in revision
    assert "unaccounted: a-done" in revision


@pytest.mark.asyncio
async def test_a_carried_residual_takes_the_acceptance_the_reviewer_wrote(
    tmp_path: Path,
) -> None:
    """An accept the guard would turn back on the worker stays an accept.

    A reviewer can write an accept while honestly declining to claim one
    criterion whose text asks for verification on a real session, which no
    round inside the lease can supply. A guard flipping that to a rejection
    spends the revision budget sending the worker back for a gap the
    reviewer has already said no round will close.

    So the human rules, and carrying the gap keeps the verdict its author
    wrote. One round, and no worker turn spent on it.
    """
    launcher = LocalProcessLauncher()
    workspace = failure_leg_workspace(tmp_path, launcher)
    worker_prompts: list[str] = []

    def reviewer_response(_root: Path, _output_name: str) -> JsonObject:
        return {
            "concern_id": "a",
            "accepted": True,
            "generalized": True,
            "reason": "every criterion but a-done, which no round here can reach",
            "criteria_met": [],
        }

    def worker_response(root: Path, output_name: str) -> JsonObject:
        if output_name == MergeReport.__name__:
            return {"completed": True, "summary": "joined"}
        with (root / "a.txt").open("a", encoding="utf-8") as handle:
            handle.write("implemented\n")
        return {
            "concern_id": "a",
            "changed": True,
            "summary": "implemented a",
            "files_changed": ["a.txt"],
        }

    core = ResolverCore(
        ResolverConfig(
            state_root=tmp_path / "state",
            workspace=workspace,
            worktree_root=tmp_path / "resolver-worktrees",
            run_id="residual-carried",
            integration_branch="resolve/residual-carried/review",
            verification_commands=[
                VerificationCommand(
                    name="combined-diff",
                    arguments=["git", "diff", "--check", "HEAD"],
                )
            ],
        ),
        resolve_spec(),
        recording_worker_recipe(
            tmp_path / "state", launcher, worker_response, worker_prompts
        ),
        lambda context: resolver_test_factory(context.root, reviewer_response),
        LiteralInvocationRenderer(),
        launcher,
    )
    seed_approvals(core, [concern("a")])
    seed_offer(core, "a-residual-round-1", ResidualRuling.CARRY)
    manifest = await core.run(
        ResolveInventory(source=snapshot(workspace, launcher), concerns=[concern("a")])
    )

    outcome = next(item for item in manifest.outcomes if item.concern_id == "a")
    assert outcome.verified, outcome.failure
    assert not [prompt for prompt in worker_prompts if "Review feedback" in prompt]
    carried = journal_events(core, "criteria_carried")
    assert [event["criteria"] for event in carried] == [["a-done"]]


@pytest.mark.asyncio
async def test_accepted_review_residuals_reach_the_journal(tmp_path: Path) -> None:
    """Observations beside an accepting verdict reach the journal, not nobody."""
    launcher = LocalProcessLauncher()
    workspace = failure_leg_workspace(tmp_path, launcher)

    def worker_response(root: Path, output_name: str) -> JsonObject:
        if output_name == MergeReport.__name__:
            return {"completed": True, "summary": "joined"}
        (root / "a.txt").write_text("implemented\n", encoding="utf-8")
        return {
            "concern_id": "a",
            "changed": True,
            "summary": "implemented a",
            "files_changed": ["a.txt"],
        }

    def reviewer_response(_root: Path, _output_name: str) -> JsonObject:
        return {
            "concern_id": "a",
            "accepted": True,
            "generalized": True,
            "reason": "criteria met",
            "criteria_met": ["a-done"],
            "residual": ["print_block styling is unpinned"],
        }

    core = failure_leg_core(
        tmp_path,
        workspace,
        launcher,
        "residual-surface",
        worker_response,
        reviewer_response,
    )
    seed_approvals(core, [concern("a")])
    manifest = await core.run(
        ResolveInventory(source=snapshot(workspace, launcher), concerns=[concern("a")])
    )

    outcome = next(item for item in manifest.outcomes if item.concern_id == "a")
    assert outcome.verified, outcome.failure
    surfaced = journal_events(core, "review_residual")
    assert surfaced and surfaced[0]["residual"] == ["print_block styling is unpinned"]
    assert surfaced[0]["concern_id"] == "a"


@pytest.mark.asyncio
async def test_a_revision_carries_its_assignment_and_names_its_round(
    tmp_path: Path,
) -> None:
    """A revising worker is not guaranteed to be the session that was reviewed.

    A resumed run opens a fresh one, and a short prompt handing it only "did
    not pass" — no concern, no criteria, no skill invocation — leaves a
    worker spending a whole turn working out whether it was rejected or
    merely re-leased.
    """
    launcher = LocalProcessLauncher()
    workspace = failure_leg_workspace(tmp_path, launcher)
    worker_prompts: list[str] = []
    handed: list[str] = []

    def reviewer_response(_root: Path, _output_name: str) -> JsonObject:
        handed.append("turn")
        met: list[JsonValue] = ["a-done"] if len(handed) > 1 else []
        return {
            "concern_id": "a",
            "accepted": len(handed) > 1,
            "generalized": True,
            "reason": "wants another pass",
            "criteria_met": met,
        }

    def worker_response(root: Path, output_name: str) -> JsonObject:
        if output_name == MergeReport.__name__:
            return {"completed": True, "summary": "joined"}
        with (root / "a.txt").open("a", encoding="utf-8") as handle:
            handle.write("implemented\n")
        return {
            "concern_id": "a",
            "changed": True,
            "summary": "implemented a",
            "files_changed": ["a.txt"],
        }

    core = ResolverCore(
        ResolverConfig(
            state_root=tmp_path / "state",
            workspace=workspace,
            worktree_root=tmp_path / "resolver-worktrees",
            run_id="revision-carries",
            integration_branch="resolve/revision-carries/review",
            verification_commands=[
                VerificationCommand(
                    name="combined-diff",
                    arguments=["git", "diff", "--check", "HEAD"],
                )
            ],
        ),
        resolve_spec(),
        recording_worker_recipe(
            tmp_path / "state", launcher, worker_response, worker_prompts
        ),
        lambda context: resolver_test_factory(context.root, reviewer_response),
        LiteralInvocationRenderer(),
        launcher,
    )
    seed_approvals(core, [concern("a")])
    manifest = await core.run(
        ResolveInventory(source=snapshot(workspace, launcher), concerns=[concern("a")])
    )

    outcome = next(item for item in manifest.outcomes if item.concern_id == "a")
    assert outcome.verified, outcome.failure
    revision = next(prompt for prompt in worker_prompts if "Review feedback" in prompt)
    assert "Round 2" in revision
    assert "wants another pass" in revision
    # The record rides along rather than being assumed remembered.
    assert "Assignment:" in revision
    assert "a-done" in revision


@pytest.mark.asyncio
async def test_an_answered_question_credited_as_met_is_corrected_not_charged(
    tmp_path: Path,
) -> None:
    """An acceptance naming an id outside the declared list keeps its verdict.

    The reviewer reads its criteria beside the answered questions, so
    crediting a question id is the slip that shape invites. Charging a
    revision round for it would spend a run's whole budget re-deriving an
    acceptance every reviewer has already given.
    """
    launcher = LocalProcessLauncher()
    workspace = failure_leg_workspace(tmp_path, launcher)
    worker_prompts: list[str] = []
    handed: list[str] = []

    def reviewer_response(_root: Path, _output_name: str) -> JsonObject:
        handed.append("turn")
        # Every declared criterion accounted for, plus a question id.
        met: list[JsonValue] = ["a-done", "a-q1"] if len(handed) == 1 else ["a-done"]
        return {
            "concern_id": "a",
            "accepted": True,
            "generalized": True,
            "reason": "every criterion holds",
            "criteria_met": met,
        }

    def worker_response(root: Path, output_name: str) -> JsonObject:
        if output_name == MergeReport.__name__:
            return {"completed": True, "summary": "joined"}
        (root / "a.txt").write_text("implemented\n", encoding="utf-8")
        return {
            "concern_id": "a",
            "changed": True,
            "summary": "implemented a",
            "files_changed": ["a.txt"],
        }

    core = ResolverCore(
        ResolverConfig(
            state_root=tmp_path / "state",
            workspace=workspace,
            worktree_root=tmp_path / "resolver-worktrees",
            run_id="foreign-label",
            integration_branch="resolve/foreign-label/review",
            verification_commands=[
                VerificationCommand(
                    name="combined-diff",
                    arguments=["git", "diff", "--check", "HEAD"],
                )
            ],
        ),
        resolve_spec(),
        recording_worker_recipe(
            tmp_path / "state", launcher, worker_response, worker_prompts
        ),
        lambda context: resolver_test_factory(context.root, reviewer_response),
        LiteralInvocationRenderer(),
        launcher,
    )
    seed_approvals(core, [concern("a")])
    manifest = await core.run(
        ResolveInventory(source=snapshot(workspace, launcher), concerns=[concern("a")])
    )

    outcome = next(item for item in manifest.outcomes if item.concern_id == "a")
    assert outcome.verified, outcome.failure
    # Corrected on the reviewer's own session, so no worker round was spent.
    assert len(outcome.rounds) == 1
    assert not any("Review feedback" in prompt for prompt in worker_prompts)


@pytest.mark.asyncio
async def test_a_round_that_commits_nothing_neither_charges_nor_reviews_an_empty_range(
    tmp_path: Path,
) -> None:
    """A worker re-entering finished work leaves the branch where it was.

    `round_base..round_base` is empty, and a reviewer handed it can only
    accept vacuously or reject for having no content — one run's reviewer
    reconstructed the real delivery by hand and said so in its report.
    """
    launcher = LocalProcessLauncher()
    workspace = failure_leg_workspace(tmp_path, launcher)
    reviewer_prompts: list[str] = []
    turns: list[str] = []

    def worker_response(root: Path, output_name: str) -> JsonObject:
        if output_name == MergeReport.__name__:
            return {"completed": True, "summary": "joined"}
        turns.append("worker")
        # Only the first turn writes; every later one finds its work done.
        first = len(turns) == 1
        if first:
            (root / "a.txt").write_text("implemented\n", encoding="utf-8")
        touched: list[JsonValue] = ["a.txt"] if first else []
        return {
            "concern_id": "a",
            "changed": first,
            "summary": "implemented a",
            "files_changed": touched,
        }

    def reviewer_response(_root: Path, _output_name: str) -> JsonObject:
        # Rejects the first submission, so the second round commits nothing.
        return {
            "concern_id": "a",
            "accepted": len(reviewer_prompts) > 1,
            "generalized": True,
            "reason": "needs another look",
            "criteria_met": ["a-done"] if len(reviewer_prompts) > 1 else [],
        }

    core = ResolverCore(
        ResolverConfig(
            state_root=tmp_path / "state",
            workspace=workspace,
            worktree_root=tmp_path / "resolver-worktrees",
            run_id="empty-round",
            integration_branch="resolve/empty-round/review",
            max_revision_rounds=1,
            verification_commands=[
                VerificationCommand(
                    name="combined-diff",
                    arguments=["git", "diff", "--check", "HEAD"],
                )
            ],
        ),
        resolve_spec(),
        worker_recipe(tmp_path / "state", launcher, worker_response),
        lambda context: agent_over(
            PromptRecordingSession(context.root, reviewer_response, reviewer_prompts)
        ),
        LiteralInvocationRenderer(),
        launcher,
    )
    seed_approvals(core, [concern("a")])
    manifest = await core.run(
        ResolveInventory(source=snapshot(workspace, launcher), concerns=[concern("a")])
    )

    outcome = next(item for item in manifest.outcomes if item.concern_id == "a")
    # With max_revision_rounds=1 a rule charging every round fails here: the
    # empty second round gives the reviewer nothing new and costs the budget.
    assert outcome.verified, outcome.failure
    empty = [prompt for prompt in reviewer_prompts if "Commits under review" in prompt]
    assert empty and not any(
        # The range handed over is never a commit against itself.
        span.split("..")[0].split()[-1] == span.split("..")[1].split()[0]
        for prompt in empty
        for span in [prompt.split("Commits under review: ")[1]]
    )


@pytest.mark.asyncio
async def test_review_prompt_names_the_range_and_the_rulings(tmp_path: Path) -> None:
    """A reviewer handed one commit spent its round discovering the others,
    and three reviews could not verify rulings the workers cited."""
    launcher = LocalProcessLauncher()
    workspace = failure_leg_workspace(tmp_path, launcher)
    reviewer_prompts: list[str] = []

    def worker_response(root: Path, output_name: str) -> JsonObject:
        if output_name == MergeReport.__name__:
            return {"completed": True, "summary": "joined"}
        (root / "a.txt").write_text("implemented\n", encoding="utf-8")
        return {
            "concern_id": "a",
            "changed": True,
            "summary": "implemented a",
            "files_changed": ["a.txt"],
        }

    def reviewer_response(_root: Path, _output_name: str) -> JsonObject:
        return {
            "concern_id": "a",
            "accepted": True,
            "generalized": True,
            "reason": "criteria met",
            "criteria_met": ["a-done"],
        }

    core = ResolverCore(
        ResolverConfig(
            state_root=tmp_path / "state",
            workspace=workspace,
            worktree_root=tmp_path / "resolver-worktrees",
            run_id="review-range",
            integration_branch="resolve/review-range/review",
            verification_commands=[
                VerificationCommand(
                    name="combined-diff",
                    arguments=["git", "diff", "--check", "HEAD"],
                )
            ],
        ),
        resolve_spec(),
        worker_recipe(tmp_path / "state", launcher, worker_response),
        lambda context: agent_over(
            PromptRecordingSession(context.root, reviewer_response, reviewer_prompts)
        ),
        LiteralInvocationRenderer(),
        launcher,
    )
    worker_asks(
        core.mailbox,
        "review-range",
        MaterialQuestion(
            id="a-shape",
            concern_id="a",
            prompt="Which dispatch shape should the decoder keep?",
        ),
    )
    seed_offer(core, "a-shape", "the flat one")
    seed_approvals(core, [concern("a")])
    manifest = await core.run(
        ResolveInventory(source=snapshot(workspace, launcher), concerns=[concern("a")])
    )

    outcome = next(item for item in manifest.outcomes if item.concern_id == "a")
    assert outcome.verified, outcome.failure
    first = next(
        prompt for prompt in reviewer_prompts if "Independently review" in prompt
    )
    assert "Commits under review: " in first
    assert "Which dispatch shape should the decoder keep?" in first
    assert "answered: the flat one" in first


@pytest.mark.asyncio
async def test_plan_prompt_states_the_marker_stripping_rule(tmp_path: Path) -> None:
    """The planner is told notes leave the lease first, so it cannot mint
    note-resolved criteria the pipeline makes unsatisfiable-as-read."""
    log: list[str] = []

    def planner_response(_root: Path, _output_name: str) -> JsonObject:
        return plan_of(concern_referencing([0, 1]))

    core = ResolverCore(
        ResolverConfig(
            state_root=tmp_path / "state",
            workspace=tmp_path,
            worktree_root=tmp_path / "worktrees",
            run_id="plan-prompt",
            integration_branch="resolve/plan-prompt/review",
            verification_commands=[
                VerificationCommand(name="verify", arguments=["git", "diff"])
            ],
        ),
        resolve_spec(),
        worker_recipe(tmp_path / "state", recording_launcher(), planner_response),
        lambda context: agent_over(
            PromptRecordingSession(context.root, planner_response, log)
        ),
        LiteralInvocationRenderer(),
        recording_launcher(),
    )
    await core.plan_inventory(two_note_request())

    assert "in-place marker" in log[0]
    assert "# lup: solved:" in log[0]


class RecordingPreparer(WorktreePreparer):
    def __init__(self) -> None:
        self.prepared: list[Path] = []

    def prepare(self, root: Path) -> None:
        self.prepared.append(root)


def test_every_created_and_restored_worktree_is_prepared(tmp_path: Path) -> None:
    launcher = LocalProcessLauncher()
    workspace = failure_leg_workspace(tmp_path, launcher)
    source = snapshot(workspace, launcher)
    preparer = RecordingPreparer()
    orchestrator = WorktreeOrchestrator(launcher, workspace, preparer)
    leases = WritableRootLeases(tmp_path / "resolver-worktrees")
    lease = leases.acquire("a", "resolve/prepared/a")

    orchestrator.create(lease, source.commit)
    assert preparer.prepared == [lease.root]

    orchestrator.remove(lease)
    orchestrator.create(lease, source.commit)
    launcher.launch(
        LaunchRequest(
            arguments=["git", "worktree", "remove", "--force", str(lease.root)],
            cwd=workspace,
        )
    )
    orchestrator.restore(lease)
    assert preparer.prepared == [lease.root, lease.root, lease.root]


def test_a_worktree_already_gone_is_freed_rather_than_reported_as_dirty(
    tmp_path: Path,
) -> None:
    """`git worktree remove` refuses a dirty tree and a missing one alike.

    Reading that one refusal as uncommitted work would tell a human that
    worktrees hold work they have to remove by hand, naming a directory that
    is not there. What is on disk decides, and git's own refusal is
    what a genuinely retained worktree reports.
    """
    launcher = LocalProcessLauncher()
    workspace = failure_leg_workspace(tmp_path, launcher)
    source = snapshot(workspace, launcher)
    orchestrator = WorktreeOrchestrator(launcher, workspace)
    leases = WritableRootLeases(tmp_path / "resolver-worktrees")

    vanished = leases.acquire("gone", "resolve/removal/gone")
    orchestrator.create(vanished, source.commit)
    launcher.launch(
        LaunchRequest(
            arguments=["git", "worktree", "remove", str(vanished.root)],
            cwd=workspace,
        )
    )
    assert not vanished.root.exists()

    freed = orchestrator.remove(vanished)
    assert freed.freed
    assert freed.detail == "worktree was already gone"

    dirty = leases.acquire("dirty", "resolve/removal/dirty")
    orchestrator.create(dirty, source.commit)
    (dirty.root / "unstaged.txt").write_text("held\n", encoding="utf-8")

    retained = orchestrator.remove(dirty)
    assert not retained.freed
    assert "untracked" in retained.detail


def test_a_restore_git_refused_prepares_no_root(tmp_path: Path) -> None:
    """A root git never created has nothing to prepare and no lease to hand out."""
    lease = joined_lease(tmp_path)
    preparer = RecordingPreparer()
    launcher = ScriptedLauncher(
        {"worktree add": out(code=128, stderr="fatal: 'resolve/i' is already used")}
    )

    with pytest.raises(RuntimeError) as raised:
        WorktreeOrchestrator(launcher, tmp_path, preparer).restore(lease)

    assert "failed to restore worktree for integration" in str(raised.value)
    assert "is already used" in str(raised.value)
    assert preparer.prepared == []


def shadowed_admin(tmp_path: Path, shadow: bool) -> ScriptedLauncher:
    """A launcher whose `rev-parse` names an admin directory, optionally shadowed.

    Bind-mounting `/dev/null` over `config.lock` is what the sandbox does and
    what a test may not do, so a symlink stands in for it: `stat` reports the
    same device node either way. `rev-parse` names the same directory twice
    because that is what a checkout whose worktree is its own reports.
    """
    admin = tmp_path / ("shadowed" if shadow else "healthy")
    admin.mkdir()
    (admin / "config").write_text("[core]\n", encoding="utf-8")
    if shadow:
        (admin / "config.lock").symlink_to(Path("/dev/null"))
    return ScriptedLauncher(
        {
            "worktree add": out(code=128, stderr="fatal: config.lock: File exists"),
            "rev-parse --git-dir": out(stdout=f"{admin}\n{admin}\n"),
        }
    )


def test_a_lease_git_refused_names_the_sandbox_holding_the_lock(
    tmp_path: Path,
) -> None:
    """A run leases a worktree per concern and dies at the first one.

    `File exists` is also what a stale lock reports, so the bare refusal sends
    a reader after a file that is not there. What the admin directory is says
    which of the two this is, and git never had that to say.
    """
    with pytest.raises(RuntimeError) as raised:
        WorktreeOrchestrator(shadowed_admin(tmp_path, True), tmp_path).create(
            joined_lease(tmp_path), "9e060ad"
        )

    assert "git config writes are blocked by the sandbox" in str(raised.value)
    assert "Rerun outside the sandbox" in str(raised.value)


def test_a_prune_the_sandbox_blocked_retains_the_lease_rather_than_cleaning_it(
    tmp_path: Path,
) -> None:
    """A step that could not run must not be recorded as one that did.

    `remove` reaches prune only where the checkout is already gone, and a
    prune the lock refuses leaves the registration standing — so reporting
    the lease freed would tell a reviewer it was cleaned by the very command
    that failed, which is this concern's mislabel one layer up.
    """
    launcher = shadowed_admin(tmp_path, True)
    launcher.script["worktree remove"] = out(code=128, stderr="fatal: not found")
    launcher.script["worktree prune"] = out(code=128, stderr="fatal: config.lock")

    removal = WorktreeOrchestrator(launcher, tmp_path).remove(joined_lease(tmp_path))

    assert not removal.freed
    assert "prune failed" in removal.detail
    assert "blocked by the sandbox" in removal.detail


def test_a_lease_git_refused_on_a_normal_tree_stays_gits_own_words(
    tmp_path: Path,
) -> None:
    with pytest.raises(RuntimeError) as raised:
        WorktreeOrchestrator(shadowed_admin(tmp_path, False), tmp_path).create(
            joined_lease(tmp_path), "9e060ad"
        )

    assert "File exists" in str(raised.value)
    assert "sandbox" not in str(raised.value)


def test_a_restored_root_is_the_one_the_preparer_receives(tmp_path: Path) -> None:
    lease = joined_lease(tmp_path)
    preparer = RecordingPreparer()
    launcher = ScriptedLauncher()

    WorktreeOrchestrator(launcher, tmp_path, preparer).restore(lease)

    assert launcher.arguments == [
        ["git", "worktree", "add", str(lease.root), lease.branch]
    ]
    assert preparer.prepared == [lease.root]


def preparing_launcher(code: int = 0, stderr: str = "") -> ScriptedLauncher:
    """No merge left open, and the `git merge` that follows exiting as chosen."""
    return ScriptedLauncher(
        {
            "rev-parse -q": out(code=1),
            "merge --no-commit": out(code=code, stderr=stderr),
        }
    )


def test_preparing_a_join_reports_a_conflict_only_when_git_left_one(
    tmp_path: Path,
) -> None:
    """The answer is what decides whether a merger turn is spent at all."""
    lease = joined_lease(tmp_path)

    assert not WorktreeOrchestrator(preparing_launcher(), tmp_path).prepare_join(
        lease, ["head-sha", "parent-sha"]
    )
    assert WorktreeOrchestrator(preparing_launcher(code=1), tmp_path).prepare_join(
        lease, ["head-sha", "parent-sha"]
    )


def test_a_merge_git_could_not_begin_refuses_in_gits_own_words(tmp_path: Path) -> None:
    launcher = preparing_launcher(code=128, stderr="fatal: not something we can merge")
    lease = joined_lease(tmp_path)

    with pytest.raises(RuntimeError) as raised:
        WorktreeOrchestrator(launcher, tmp_path).prepare_join(lease, ["head", "parent"])

    assert "failed to prepare semantic join for integration" in str(raised.value)
    assert "`git merge` exited 128" in str(raised.value)
    assert "not something we can merge" in str(raised.value)


def test_one_join_step_joins_exactly_two_commits(tmp_path: Path) -> None:
    orchestrator = WorktreeOrchestrator(ScriptedLauncher(), tmp_path)
    lease = joined_lease(tmp_path)

    with pytest.raises(ValueError, match="exactly two commits"):
        orchestrator.prepare_join(lease, ["only-one"])


def test_a_settled_join_commits_nothing_and_reports_the_head_it_found(
    tmp_path: Path,
) -> None:
    """A clean tree with no open MERGE_HEAD is a join an earlier turn committed."""
    launcher = ScriptedLauncher(
        {
            "status --porcelain": out(),
            "rev-parse -q": out(code=1),
            "rev-parse HEAD": out("settled-sha\n"),
        }
    )
    lease = joined_lease(tmp_path)

    joined = WorktreeOrchestrator(launcher, tmp_path).commit_join(lease, "resolve: j")

    assert joined == "settled-sha"
    assert ["git", "commit", "-m", "resolve: j"] not in launcher.arguments


def test_a_join_whose_unmerged_paths_git_could_not_list_refuses(
    tmp_path: Path,
) -> None:
    """The guard reports git's own complaint when git is what failed, not a marker."""
    broken = "fatal: index file smaller than expected"
    launcher = ScriptedLauncher({"diff --name-only": out(code=128, stderr=broken)})
    lease = joined_lease(tmp_path)

    with pytest.raises(RuntimeError) as raised:
        WorktreeOrchestrator(launcher, tmp_path).commit_join(lease, "resolve: j")

    assert "semantic join for integration still has invalid changes" in str(
        raised.value
    )
    assert broken in str(raised.value)


def test_a_join_whose_status_cannot_be_read_refuses_before_it_stages(
    tmp_path: Path,
) -> None:
    launcher = ScriptedLauncher(
        {"status --porcelain": out(code=128, stderr="fatal: index file corrupt")}
    )
    lease = joined_lease(tmp_path)

    with pytest.raises(RuntimeError) as raised:
        WorktreeOrchestrator(launcher, tmp_path).commit_join(lease, "resolve: j")

    assert "failed to inspect semantic join for integration" in str(raised.value)
    assert "index file corrupt" in str(raised.value)
    assert ["git", "add", "-A"] not in launcher.arguments


def test_a_join_that_could_not_be_staged_is_not_committed_anyway(
    tmp_path: Path,
) -> None:
    """Committing over a failed `git add -A` records a tree without the resolution."""
    launcher = ScriptedLauncher(
        {
            "status --porcelain": out("UU src/module.py\n"),
            "add -A": out(code=128, stderr="fatal: unable to write new index file"),
        }
    )
    lease = joined_lease(tmp_path)

    with pytest.raises(RuntimeError) as raised:
        WorktreeOrchestrator(launcher, tmp_path).commit_join(lease, "resolve: j")

    assert "failed to stage semantic join for integration" in str(raised.value)
    assert "unable to write new index file" in str(raised.value)
    assert ["git", "commit", "-m", "resolve: j"] not in launcher.arguments


def test_a_join_commit_git_refused_names_what_it_was_refused_for(
    tmp_path: Path,
) -> None:
    unmerged = "error: Committing is not possible because you have unmerged files."
    launcher = ScriptedLauncher(
        {
            "status --porcelain": out("UU src/module.py\n"),
            "commit -m": out(code=1, stderr=unmerged),
        }
    )
    lease = joined_lease(tmp_path)

    with pytest.raises(RuntimeError) as raised:
        WorktreeOrchestrator(launcher, tmp_path).commit_join(lease, "resolve: j")

    assert "failed to commit semantic join for integration" in str(raised.value)
    assert "you have unmerged files" in str(raised.value)


def test_a_join_commit_without_one_identity_is_not_reported_as_one(
    tmp_path: Path,
) -> None:
    """A commit that cannot be named cannot become anyone's dependency base."""
    launcher = ScriptedLauncher(
        {
            "status --porcelain": out("UU src/module.py\n"),
            "rev-parse HEAD": out("first-sha\nsecond-sha\n"),
        }
    )
    lease = joined_lease(tmp_path)

    with pytest.raises(RuntimeError) as raised:
        WorktreeOrchestrator(launcher, tmp_path).commit_join(lease, "resolve: j")

    assert "failed to identify worktree integration" in str(raised.value)
    assert "named 2 commits: 'first-sha\\nsecond-sha'" in str(raised.value)


async def test_a_failing_promotion_round_is_retried_not_fatal(tmp_path: Path) -> None:
    """The only writer of ``answers/`` must not be lost to one bad round.

    A promoter that dies takes every door with it: later waits park on
    questions a human already answered, and nothing says why.
    """
    core = planning_core(tmp_path, lambda *_: plan_of())
    core.questions.poll_interval_seconds = 0.01
    rounds = Counter()

    async def failing_once() -> list[str]:
        rounds["called"] += 1
        if rounds["called"] == 1:
            raise OSError("offers/ is unreadable")
        return []

    core.questions.apply_mailbox = failing_once
    async with core.promoting():
        await asyncio.sleep(0.05)

    assert rounds["called"] > 1, "the promoter stopped after the failing round"
    assert any("retried" in problem for problem in core.promoter_problems)


async def test_a_settled_answer_stops_a_concern_reporting_that_it_waits(
    tmp_path: Path,
) -> None:
    """Parked is the one span in which nothing refreshes the status.

    ``waiting_for_answers`` is written where a concern raises to park and
    overwritten only where that concern executes again, so a run holding a
    settled answer for every question it named would still report itself
    blocked on them — and the status view is what a human reads to decide
    whether the run is unblocked.
    """
    core = planning_core(tmp_path, lambda *_: plan_of())
    core.persist(
        ResolveState(
            config_digest="config-sha",
            run_id=core.config.run_id,
            phase=ResolvePhase.WORKERS,
            source=SourceSnapshot(branch="feature", commit="source-sha"),
            spec=resolve_spec(),
            concerns=[concern("a"), concern("b"), concern("c")],
            progress=[
                ConcernProgress(
                    concern_id="a", status=ConcernStatus.WAITING_FOR_ANSWERS
                ),
                ConcernProgress(
                    concern_id="b", status=ConcernStatus.WAITING_FOR_ANSWERS
                ),
                ConcernProgress(
                    concern_id="c", status=ConcernStatus.WAITING_FOR_ANSWERS
                ),
            ],
            eligibility=[
                ConcernEligibility(
                    concern_id="a", eligible=True, integration_approved=True
                ),
                ConcernEligibility(
                    concern_id="b", eligible=True, integration_approved=True
                ),
            ],
        )
    )
    core.questions.queue_questions(
        [
            MaterialQuestion(id="a-q1", concern_id="a", prompt="settle a?"),
            MaterialQuestion(id="b-q1", concern_id="b", prompt="settle b?"),
            MaterialQuestion(id="c-q1", concern_id="c", prompt="approve c?"),
        ],
        "planning",
    )
    seed_offer(core, "a-q1", "yes")
    seed_offer(core, "c-q1", "defer")

    await core.questions.apply_mailbox()

    statuses = {
        item.concern_id: item.status for item in core.repository.load().progress
    }
    assert statuses["a"] == ConcernStatus.ELIGIBLE, (
        "a concern whose every question settled still reported waiting"
    )
    assert statuses["b"] == ConcernStatus.WAITING_FOR_ANSWERS, (
        "a concern with an outstanding question was unparked by a sibling's answer"
    )
    assert statuses["c"] == ConcernStatus.WAITING_FOR_ANSWERS, (
        "a concern whose own eligibility is undecided was moved to eligible, "
        "which has no transition to ineligible if the answer defers it"
    )


async def test_a_promoter_failure_does_not_replace_the_bodys_exception(
    tmp_path: Path,
) -> None:
    """The finally runs while the real failure is propagating through it."""
    core = planning_core(tmp_path, lambda *_: plan_of())
    core.questions.poll_interval_seconds = 0.01

    async def always_failing() -> list[str]:
        raise OSError("offers/ is unreadable")

    core.questions.apply_mailbox = always_failing

    with pytest.raises(ResolverInvariantError) as raised:
        async with core.promoting():
            raise ResolverInvariantError("what actually went wrong")

    assert "what actually went wrong" in str(raised.value)


async def test_a_park_report_names_a_broken_promoter(tmp_path: Path) -> None:
    """ "Nobody answered" and "it never counted" look identical from outside."""
    core = planning_core(tmp_path, lambda *_: plan_of())
    core.state = standing_state("coverage", None)
    core.promoter_problems.append("the answer promoter stopped early")

    with pytest.raises(ResolverAwaitingAnswers) as raised:
        await core.questions.await_questions(
            [
                MaterialQuestion(
                    id="q1",
                    concern_id="concern-1",
                    prompt="Which way?",
                    choices=["left", "right"],
                )
            ]
        )

    assert any("promoter stopped early" in item for item in raised.value.problems)


def test_a_revision_that_only_changed_its_report_keeps_the_work_it_describes(
    tmp_path: Path,
) -> None:
    # A rejection naming a finding outside the lease is honestly answered by a
    # revision that leaves the tree alone. Read as an empty diff, that would
    # spend a round to say the worker did nothing — and fail the concern with
    # its criteria never evaluated.
    orchestrator = WorktreeOrchestrator(recording_launcher(), tmp_path)

    diff = orchestrator.settled_round(
        concern("a"),
        WorkerReport(concern_id="a", changed=True, summary="answered the rejection"),
        "round-one-sha",
        "origin-sha",
    )

    assert diff.valid
    assert diff.commit == "round-one-sha"
    assert "the work it describes stands" in diff.reason


def test_a_claim_of_changes_over_an_untouched_branch_is_still_a_fault(
    tmp_path: Path,
) -> None:
    orchestrator = WorktreeOrchestrator(recording_launcher(), tmp_path)

    diff = orchestrator.settled_round(
        concern("a"),
        WorkerReport(concern_id="a", changed=True, summary="claimed a change"),
        "origin-sha",
        "origin-sha",
    )

    assert not diff.valid
    assert diff.reason == "worker reported changes but diff is empty"


def test_a_worker_that_reported_no_change_and_made_none_is_settled(
    tmp_path: Path,
) -> None:
    orchestrator = WorktreeOrchestrator(recording_launcher(), tmp_path)

    diff = orchestrator.settled_round(
        concern("a"),
        WorkerReport(concern_id="a", changed=False, summary="nothing to do here"),
        "origin-sha",
        "origin-sha",
    )

    assert diff.valid
    assert diff.commit == "origin-sha"


def rendered(*paths: str) -> GeneratedArtifacts:
    """An ownership answer that names exactly these paths as the generator's."""
    return GeneratedArtifacts(
        by_path={
            path: OwnedArtifact(
                path=Path(path), category="generated", sha256="", semantic_id="plugin"
            )
            for path in paths
        }
    )


def changed_paths_launcher(*paths: str) -> ScriptedLauncher:
    """Report one changed-path listing for every diff this asks for."""
    listing = "".join(f"{path}\n" for path in paths)
    return ScriptedLauncher({"diff --name-only": out(listing)})


def test_a_generated_artifact_is_not_a_path_this_repository_authored(
    tmp_path: Path,
) -> None:
    launcher = changed_paths_launcher(
        "packages/lup/src/lup/policy/vocabulary.py",
        ".claude/.lup-ownership.json",
        ".codex/plugins/lup/hooks/runtime/policy_data.py",
    )
    orchestrator = WorktreeOrchestrator(
        launcher,
        tmp_path,
        generated=rendered(
            ".claude/.lup-ownership.json",
            ".codex/plugins/lup/hooks/runtime/policy_data.py",
        ),
    )
    lease = joined_lease(tmp_path)

    # The join adjudicates what a person wrote. Every lease that touches the
    # catalog re-renders both plugin trees, so counting those renderings made
    # every parent overlap every other and asked the merger to justify content
    # whose only correct value is whatever the generator emits next.
    assert orchestrator.authored_between(lease, "base", "parent") == [
        Path("packages/lup/src/lup/policy/vocabulary.py")
    ]
    assert orchestrator.changed_between(lease, "base", "parent") == [
        Path("packages/lup/src/lup/policy/vocabulary.py"),
        Path(".claude/.lup-ownership.json"),
        Path(".codex/plugins/lup/hooks/runtime/policy_data.py"),
    ]


def test_a_tree_that_renders_nothing_authors_everything_it_changed(
    tmp_path: Path,
) -> None:
    launcher = changed_paths_launcher("docs/rules.md")
    orchestrator = WorktreeOrchestrator(launcher, tmp_path, generated=rendered())
    lease = joined_lease(tmp_path)

    assert orchestrator.authored_between(lease, "base", "parent") == [
        Path("docs/rules.md")
    ]


@pytest.mark.asyncio
async def test_a_parent_an_earlier_run_landed_is_recorded_without_a_second_account(
    tmp_path: Path,
) -> None:
    """A resume re-enters a join the previous run got part way through.

    Its progress file is the run's, so a fresh one starts empty and the
    merger is offered every parent again — including the ones already in the
    tree. Those were accounted for when they landed, and asking again would
    have this merger adjudicate somebody else's decisions against a tree
    that has since moved past them.
    """
    launcher = LocalProcessLauncher()
    workspace = failure_leg_workspace(tmp_path, launcher)

    def git(*arguments: str, cwd: Path | None = None) -> str:
        status = launcher.launch(
            LaunchRequest(arguments=["git", *arguments], cwd=cwd or workspace)
        )
        assert status.code == 0, status.stderr
        return status.stdout.strip()

    git("checkout", "-b", "one", "source")
    (workspace / "one.txt").write_text("one\n", encoding="utf-8")
    git("add", "one.txt")
    git("commit", "-m", "add one")
    first = git("rev-parse", "HEAD")
    git("checkout", "source")

    tree = tmp_path / "integration"
    git("worktree", "add", "--detach", str(tree), "source")
    # The previous run merged this parent and committed it; this run has no
    # record of having done so.
    git("merge", "--no-ff", "-m", "resolve: join", first, cwd=tree)

    run_dir = tmp_path / "state" / "resumed-join"
    run_dir.mkdir(parents=True)
    desk = JoinDesk(run_dir, "integration")
    desk.write_plan(
        JoinPlan(
            concern_id="integration",
            worktree=tree,
            base=git("rev-parse", "source"),
            title="resolve: join",
            purpose="integration",
            tips=[JoinTip(commit=first, concern_id="a")],
        )
    )
    tools = {
        tool.name: tool
        for tool in create_join_tools(run_dir, tree, "integration", launcher=launcher)
    }

    prepared = await tools["start_parent"](StartParentInput(commit=first))
    assert prepared.state == "already-in-tree"

    landed = await tools["land_parent"](
        LandParentInput(commit=first, summary="already in")
    )

    assert landed.landed is True
    assert landed.problems == []
    assert landed.unaccounted == []
    assert desk.progress().joined == [first]


@pytest.mark.asyncio
async def test_the_final_recheck_reads_the_tree_from_a_checkout_of_its_own(
    tmp_path: Path,
) -> None:
    """The last phase is a reviewer turn per concern, with no order between them.

    Serially, one measured run's 21 integrated concerns at 16.3 minutes a
    turn is most of a working day spent on a phase where nothing waits for
    anything. They run together instead — but not in one directory: a
    reviewer is denied Write and Edit, and still runs the project's gate,
    which writes caches and re-renders artifacts. Concurrent readers sharing
    the integration worktree would regenerate into each other, and into the
    tree the audit and the review branch are read from.
    """
    launcher = LocalProcessLauncher()
    workspace = failure_leg_workspace(tmp_path, launcher)

    def git(*arguments: str) -> str:
        # Identity per invocation, never `git config`: a misbound command then
        # writes nothing, where a persisted setting lands in the shared config
        # every worktree of a real repository inherits (see `lup.devtools.gitguard`).
        status = launcher.launch(
            LaunchRequest(
                arguments=[
                    "git",
                    "-c",
                    "user.email=resolver@example.test",
                    "-c",
                    "user.name=Resolver Test",
                    *arguments,
                ],
                cwd=workspace,
            )
        )
        assert status.code == 0, status.stderr
        return status.stdout.strip()

    read_from: list[Path] = []

    def reviewer_response(root: Path, _output_name: str) -> JsonObject:
        read_from.append(root)
        return {
            "concern_id": "a",
            "accepted": True,
            "generalized": True,
            "reason": "criteria hold",
            "criteria_met": ["held"],
        }

    run_id = "recheck-pool"
    core = ResolverCore(
        ResolverConfig(
            state_root=tmp_path / "state",
            workspace=workspace,
            worktree_root=tmp_path / "resolver-worktrees",
            run_id=run_id,
            integration_branch=f"resolve/{run_id}/review",
            max_parallel_workers=3,
            verification_commands=[
                VerificationCommand(name="verify", arguments=["git", "diff", "--check"])
            ],
        ),
        resolve_spec(),
        worker_recipe(tmp_path / "state", launcher, reviewer_response),
        lambda context: resolver_test_factory(context.root, reviewer_response),
        LiteralInvocationRenderer(),
        launcher,
    )
    # One criterion id across the three, so a single scripted reviewer answers
    # for whichever concern reaches it — the pool decides that, not the test.
    concerns = [
        Concern(
            id=identifier,
            title=identifier.title(),
            spec=f"Resolve {identifier}",
            criteria=[AcceptanceCriterion(id="held", description="done")],
            integration_approved=True,
        )
        for identifier in ("a", "b", "c")
    ]
    state = ResolveState(
        config_digest="config-sha",
        run_id=run_id,
        phase=ResolvePhase.INTEGRATION,
        source=snapshot(workspace, launcher),
        spec=resolve_spec(),
        concerns=concerns,
        progress=[
            ConcernProgress(concern_id=item.id, status=ConcernStatus.VERIFIED)
            for item in concerns
        ],
    )
    core.persist(state)
    integration = IntegrationRecord(
        branch=f"resolve/{run_id}/review",
        worktree=tmp_path / "resolver-worktrees" / "integration",
        concerns=[item.id for item in concerns],
        commit=git("rev-parse", "HEAD"),
    )

    asked = await core.joiner.recheck_criteria(state, integration)

    assert asked == [], "every concern's criteria still hold"
    assert len(read_from) == 3, "one reviewer turn per integrated concern"
    # A checkout each, and never the integration worktree itself.
    assert len(set(read_from)) == 3
    assert integration.worktree not in read_from
    # Nothing left behind: the pool discards what it made.
    assert not [path for path in read_from if path.exists()]

    # Resumed against the same tree, the re-check spends nothing: like every
    # other phase it skips work it has already done. Re-examining every
    # concern on each interruption spends reviewer turns several times over —
    # and re-running a reviewer can return a different verdict for an
    # unchanged tree, which then wedges the run on a question already asked
    # another way.
    read_from.clear()

    again = await core.joiner.recheck_criteria(state, integration)

    assert again == [], "the recorded re-checks still hold"
    assert read_from == [], "no reviewer turn is spent twice on one tree"

    # A tree reassembled from different parents is a different question.
    git("commit", "--allow-empty", "-m", "reassembled")
    reassembled = integration.model_copy(update={"commit": git("rev-parse", "HEAD")})
    read_from.clear()

    await core.joiner.recheck_criteria(state, reassembled)

    assert len(read_from) == 3, "a different commit is examined afresh"


@pytest.mark.asyncio
async def test_a_capped_wave_holds_the_cap_and_resumes_what_it_never_started(
    tmp_path: Path,
) -> None:
    """The cap is what a cut wave costs, so what it queues has to survive one.

    Uncapped, a batch opens a session per runnable concern — eleven within
    the same second, measured — which spends the host's allowance at the
    width of the batch and races the credential file every session shares.
    Capping is the instrument, and it introduces a state of its own: a
    concern leased but never started, because the cap was full when the run
    died.

    Such a concern has recorded nothing, so the next batch selects it again
    exactly as the lease phase left it. That is the property here — the run
    dies mid-wave with two concerns never begun, and the resume finishes all
    three rather than shipping one.
    """
    launcher = LocalProcessLauncher()
    workspace = failure_leg_workspace(tmp_path, launcher)

    def git(*arguments: str) -> str:
        # Identity per invocation, never `git config`: a misbound command then
        # writes nothing, where a persisted setting lands in the shared config
        # every worktree of a real repository inherits (see `lup.devtools.gitguard`).
        status = launcher.launch(
            LaunchRequest(
                arguments=[
                    "git",
                    "-c",
                    "user.email=resolver@example.test",
                    "-c",
                    "user.name=Resolver Test",
                    *arguments,
                ],
                cwd=workspace,
            )
        )
        assert status.code == 0, status.stderr
        return status.stdout.strip()

    run_id = "capped-wave"
    live: list[str] = []
    widest = 0
    started: list[str] = []
    stopping = True

    def worker_response(root: Path, output_name: str) -> JsonObject:
        nonlocal widest, stopping
        identifier = root.name
        if output_name != WorkerReport.__name__:
            raise AssertionError(output_name)
        started.append(identifier)
        live.append(identifier)
        widest = max(widest, len(live))
        live.remove(identifier)
        if stopping:
            # Stopped while the wave is one concern deep. The other two are
            # leased and queued behind the cap, having run nothing at all.
            stopping = False
            QuestionMailbox(tmp_path / "state" / run_id).drain(
                ParkRequest(run_id=run_id, reason="stopped mid-wave")
            )
        (root / f"{identifier}.txt").write_text("done\n", encoding="utf-8")
        return {
            "concern_id": identifier,
            "changed": True,
            "summary": f"implemented {identifier}",
            "files_changed": [f"{identifier}.txt"],
        }

    def reviewer_response(root: Path, output_name: str) -> JsonObject:
        if output_name != ReviewReport.__name__:
            raise AssertionError(output_name)
        return {
            "concern_id": root.name,
            "accepted": True,
            "generalized": True,
            "reason": "criteria met",
            "criteria_met": [f"{root.name}-done"],
        }

    def build() -> ResolverCore:
        return ResolverCore(
            ResolverConfig(
                state_root=tmp_path / "state",
                workspace=workspace,
                worktree_root=tmp_path / "resolver-worktrees",
                run_id=run_id,
                integration_branch=f"resolve/{run_id}/review",
                max_parallel_workers=1,
                verification_commands=[
                    VerificationCommand(
                        name="combined-diff",
                        arguments=["git", "diff", "--check", "HEAD"],
                    )
                ],
            ),
            resolve_spec(),
            worker_recipe(tmp_path / "state", launcher, worker_response),
            lambda context: resolver_test_factory(context.root, reviewer_response),
            LiteralInvocationRenderer(),
            launcher,
        )

    inventory = ResolveInventory(
        source=SourceSnapshot(
            branch=git("branch", "--show-current"), commit=git("rev-parse", "HEAD")
        ),
        concerns=[concern("a"), concern("b"), concern("c")],
    )

    core = build()
    seed_approvals(core, inventory.concerns)
    with pytest.raises(ResolverDrained):
        await core.run(inventory)

    # One ran; the other two were still behind the cap and recorded nothing.
    assert started == ["a"]
    assert widest == 1, "never more than the cap in flight"

    resumed = build()
    seed_approvals(resumed, inventory.concerns)
    # Parks at the final re-check: it asks each concern's reviewer about the
    # integrated tree, and one shared double cannot answer as three different
    # concerns. Beside the point here, which is what the worker phase did.
    with pytest.raises(ResolverAwaitingAnswers):
        await resumed.resume()

    outcomes = resumed.repository.load().outcomes
    assert {outcome.concern_id for outcome in outcomes if outcome.verified} == {
        "a",
        "b",
        "c",
    }
    # Both concerns the stopped wave never began were started by the resume.
    assert {"b", "c"} <= set(started)
    assert widest == 1
