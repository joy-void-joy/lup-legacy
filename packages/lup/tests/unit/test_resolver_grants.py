"""A gate granted while a lease is running, and one taken back the same way.

The judge here is the canonical policy, built once the way a worker session
builds it and then asked repeatedly, so "the session was not restarted" is
what these assert rather than what they arrange: the same policy object,
the same reader, a different answer.
"""

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import BaseModel

from lup.harness.contracts import SkillInvocationRenderer
from lup.harness.models import ResolveSpec, SkillInvocation
from lup.policy.grants import LeaseGrants, read_allowance_grants
from lup.policy.identity import ConcernAllowance
from lup.policy.models import EditBatch, EditChange
from lup.policy.rules import EditPolicy, PathRule
from lup.resolver.grants import GrantLedger, concern_grants
from lup.resolver.record import Journal
from lup.coordination.mailbox import AnswerDoor, AnswerOffer, ParkRequest
from lup.coordination.questions import QuestionAnswer
from lup.resolver.mailbox import PendingQuestion, QuestionMailbox
from lup.resolver.models import (
    INTEGRATION_CONCERN_ID,
    AcceptanceCriterion,
    Concern,
    ConcernProgress,
    ConcernStatus,
    MaterialQuestion,
    ResolvePhase,
    ResolveState,
    ResolverConfig,
    SourceSnapshot,
    VerificationCommand,
    WorkerContext,
    WritableRootLease,
    allowance_question_id,
)
from lup.coordination.cohort import ActorCohort
from lup.coordination.refs import ActorRef
from lup.resolver.questions import QuestionBroker
from lup.resolver.run import ResolveRun
from lup.resolver.state import ResolverStateRepository
from lup.resolver.turns import TurnRunner
from lup.sessions.capabilities import SessionEngine
from lup.sessions.surface import Agent
from lup.sessions.events import StartedTurn, TurnRequest
from tests.unit.doubles import agent_over

NEW_DEVTOOLS = PathRule(
    kind="new_devtools",
    value="src",
    reason="new devtools module requires approval",
)
CREATION = EditBatch(
    changes=[
        EditChange(
            path=Path("src/app/devtools/harness/newborn.py"),
            after='"""Newborn."""\n',
        )
    ]
)


def judge(grants: LeaseGrants) -> EditPolicy:
    """The edit judge a worker session is given, over one lease's grants."""
    return EditPolicy(protected=[NEW_DEVTOOLS], autonomous=True, grants=grants)


def gated_concern(identifier: str) -> Concern:
    return Concern(
        id=identifier,
        title=identifier.title(),
        spec=f"Resolve {identifier}",
        criteria=[AcceptanceCriterion(id=f"{identifier}-done", description="done")],
    )


def broker(root: Path, concerns: list[Concern]) -> QuestionBroker:
    """One run's question desk over a state held in memory."""
    config = ResolverConfig(
        state_root=root / "state",
        workspace=root,
        worktree_root=root / "worktrees",
        run_id="grant-run",
        integration_branch="resolve/grant-run/review",
        verification_commands=[
            VerificationCommand(name="none", arguments=["git", "status"])
        ],
    )
    repository = ResolverStateRepository(config.state_root, config.run_id)
    run = ResolveRun(repository, Journal(repository.root))
    run.state = ResolveState(
        config_digest="digest",
        run_id=config.run_id,
        phase=ResolvePhase.WORKERS,
        source=SourceSnapshot(branch="dev", commit="0" * 40),
        spec=ResolveSpec(
            id="resolve",
            worker_identity="resolver-worker",
            worker_skill=SkillInvocation(plugin="lup", skill="worker"),
            review_skill=SkillInvocation(plugin="lup", skill="review"),
            merge_skill=SkillInvocation(plugin="lup", skill="merge"),
        ),
        concerns=concerns,
        progress=[
            ConcernProgress(concern_id=item.id, status=ConcernStatus.LEASED)
            for item in concerns
        ],
    )
    return QuestionBroker(
        config,
        run,
        QuestionMailbox(repository.root),
        Journal(repository.root),
        GrantLedger(repository.root),
    )


def park_door(desk: QuestionBroker) -> Callable[[str], None]:
    """What a lease's judge calls when a human takes a gate back."""

    def park(reason: str) -> None:
        desk.mailbox.park(ParkRequest(run_id=desk.config.run_id, reason=reason))

    return park


def human_grants(desk: QuestionBroker, concern_id: str, value: str) -> None:
    """Ask for a gate the way `request_allowance` does, and answer it."""
    question = allowance_question_id(concern_id, ConcernAllowance.NEW_DEVTOOLS_MODULE)
    when = datetime(2026, 1, 1, tzinfo=UTC)
    desk.mailbox.queue(
        PendingQuestion(
            run_id=desk.config.run_id,
            question=MaterialQuestion(
                id=question,
                concern_id=concern_id,
                prompt=f"Grant `new-devtools-module` to {concern_id}?",
                choices=["grant", "refuse"],
                closed_choices=True,
            ),
            asked_by=concern_id,
            asked_at=when,
        )
    )
    desk.mailbox.offer(
        AnswerOffer(
            run_id=desk.config.run_id,
            question_id=question,
            value=value,
            door=AnswerDoor.FLAG,
            offered_at=when,
        )
    )
    desk.promote_offers()


class IdleSession(SessionEngine):
    """A session for tests that open one and never take a turn on it."""

    async def start[T: BaseModel | None](
        self, request: TurnRequest[T]
    ) -> StartedTurn[T]:
        raise AssertionError("this test opens sessions but takes no turn")


class LiteralRenderer(SkillInvocationRenderer):
    def render(self, invocation: SkillInvocation) -> str:
        return f"{invocation.plugin}:{invocation.skill}"


class RecordingRecipe:
    """A worker-factory recipe that keeps every context it was handed."""

    def __init__(self) -> None:
        self.opened: list[WorkerContext] = []

    def __call__(self, context: WorkerContext) -> Agent:
        self.opened.append(context)
        return agent_over(IdleSession())


def turn_runner(desk: QuestionBroker, recipe: RecordingRecipe) -> TurnRunner:
    """The real runner over this desk's run, opening watched sessions."""
    return TurnRunner(
        desk.run.require().spec,
        desk.run,
        ActorCohort(
            desk.mailbox.root,
            journal=desk.journal,
            mail=desk.mailbox.mail,
        ),
        desk.mailbox,
        recipe,
        lambda _context: agent_over(IdleSession()),
        LiteralRenderer(),
        desk.grants,
    )


def parked(desk: QuestionBroker) -> None:
    """Nothing has parked this run yet."""
    assert desk.mailbox.parked() is None


def test_a_grant_answered_mid_lease_reaches_the_judge_that_is_already_running(
    tmp_path: Path,
) -> None:
    """The session is not rebuilt between the two verdicts — only the answer is.

    Rendered into the environment the grant had nowhere to land: the only
    channel for it belonged to a process that had already started. Here the
    judge, the reader, and the lease are the ones the worker was launched
    with, and the human's answer alone changes the verdict.
    """
    desk = broker(tmp_path, [gated_concern("a")])
    grants = desk.grants.lease("a", [], park_door(desk))
    policy = judge(grants)
    assert policy.decide(CREATION).effect == "ask"

    human_grants(desk, "a", "grant")

    assert policy.decide(CREATION).effect == "allow"


def test_a_grant_made_before_the_lease_opened_is_honoured_from_its_first_call(
    tmp_path: Path,
) -> None:
    """What a plan approved reaches the lease through the same document."""
    desk = broker(tmp_path, [gated_concern("a")])
    grants = desk.grants.lease(
        "a", [ConcernAllowance.NEW_DEVTOOLS_MODULE], park_door(desk)
    )

    assert judge(grants).decide(CREATION).effect == "allow"


def test_a_lease_holding_no_grant_sees_the_unchanged_lattice(tmp_path: Path) -> None:
    """A gate nobody granted still asks, document or no document."""
    desk = broker(tmp_path, [gated_concern("a")])
    unheld = desk.grants.lease("a", [], park_door(desk))

    assert judge(LeaseGrants()).decide(CREATION).effect == "ask"
    assert judge(unheld).decide(CREATION).effect == "ask"


def test_one_concerns_grant_does_not_release_a_siblings_gate(tmp_path: Path) -> None:
    """A grant is scoped to the lease it was made for by having its own document."""
    desk = broker(tmp_path, [gated_concern("a"), gated_concern("b")])
    sibling = desk.grants.lease("b", [], park_door(desk))

    human_grants(desk, "a", "grant")

    assert read_allowance_grants(desk.grants.document("a")) == [
        ConcernAllowance.NEW_DEVTOOLS_MODULE.value
    ]
    assert judge(sibling).decide(CREATION).effect == "ask"


def test_a_refused_gate_is_recorded_without_becoming_a_grant(tmp_path: Path) -> None:
    """Only "grant" grants; every other answer leaves the gate where it was."""
    desk = broker(tmp_path, [gated_concern("a")])
    grants = desk.grants.lease("a", [], park_door(desk))

    human_grants(desk, "a", "refuse")

    assert judge(grants).decide(CREATION).effect == "ask"


def test_a_grant_answered_mid_lease_widens_a_lease_without_narrowing_it(
    tmp_path: Path,
) -> None:
    """A merger holds every gate the branches it joins were approved for.

    Rewriting the concern's own derivation when a later grant settles would
    take those back, and the lease's reader would read that as a human's
    withdrawal and park a join nobody interfered with.
    """
    desk = broker(tmp_path, [gated_concern("a")])
    joining = desk.grants.lease(
        "a", [ConcernAllowance.ANTIPATTERN_SUPPRESSION], park_door(desk)
    )

    human_grants(desk, "a", "grant")

    assert sorted(joining.granted()) == [
        ConcernAllowance.ANTIPATTERN_SUPPRESSION.value,
        ConcernAllowance.NEW_DEVTOOLS_MODULE.value,
    ]
    parked(desk)


def test_a_grant_reaches_the_integration_lease_that_no_concern_stands_behind(
    tmp_path: Path,
) -> None:
    """The lease with no concern is the one this tool exists for.

    A rule that first meets its exception once two branches are joined is
    `request_allowance`'s own reason to exist, and the actor that meets it
    holds the reserved integration lease — which is not a concern and never
    can be, so a publisher reading the concern list writes nothing for it.
    """
    desk = broker(tmp_path, [gated_concern("a")])
    joining = desk.grants.lease(INTEGRATION_CONCERN_ID, [], park_door(desk))
    policy = judge(joining)
    assert policy.decide(CREATION).effect == "ask"

    human_grants(desk, INTEGRATION_CONCERN_ID, "grant")

    assert policy.decide(CREATION).effect == "allow"


def test_a_later_turn_on_the_same_lease_does_not_take_a_mid_lease_grant_back(
    tmp_path: Path,
) -> None:
    """A lease republishes before every turn, and the reader outlives them.

    A join adjudicates up to four times, and each turn republishes what the
    run believes the lease holds — while the session holding the reader is
    the one opened for the first turn. A set that left out what the lease
    had since been granted would take it away, and the reader would report
    that as the human withdrawal it cannot be told apart from.
    """
    desk = broker(tmp_path, [gated_concern("a")])
    recipe = RecordingRecipe()
    turns = turn_runner(desk, recipe)
    lease = WritableRootLease(
        concern_id=INTEGRATION_CONCERN_ID,
        root=tmp_path / "integration",
        branch="resolve/grant-run/review",
    )
    turns.actors.session(
        ActorRef(kind="merger", id=lease.concern_id), turns.merger_recipe(lease)
    )
    policy = judge(recipe.opened[0].grants)
    human_grants(desk, INTEGRATION_CONCERN_ID, "grant")
    assert policy.decide(CREATION).effect == "allow"

    turns.actors.session(
        ActorRef(kind="merger", id=lease.concern_id), turns.merger_recipe(lease)
    )

    assert policy.decide(CREATION).effect == "allow"
    parked(desk)


def test_a_worker_opening_after_a_merger_held_more_does_not_read_a_withdrawal(
    tmp_path: Path,
) -> None:
    """Two actors share one lease's document and hold different sets.

    A merger carries every gate the branches it joins were approved for; the
    worker beside it carries only its concern's own, and republishes that
    when it opens. Each reader starts from what it was itself given, so the
    narrower one finds nothing missing — a withdrawal is a gate this reader
    held and lost, not one it never had.
    """
    desk = broker(tmp_path, [gated_concern("a")])
    desk.grants.lease("a", [ConcernAllowance.ANTIPATTERN_SUPPRESSION], park_door(desk))

    working = desk.grants.lease("a", [], park_door(desk))

    assert working.granted() == []
    parked(desk)


def test_a_grant_withdrawn_mid_lease_stops_applying_and_parks_the_run(
    tmp_path: Path,
) -> None:
    """The state at judgment governs in both directions, and loudly.

    Narrowing alone would leave a worker collecting denials it cannot
    explain for work it was told it could do. The park is what says a human
    changed something, which is what this run already does for every other
    decision that belongs to one.
    """
    desk = broker(tmp_path, [gated_concern("a")])
    grants = desk.grants.lease(
        "a", [ConcernAllowance.NEW_DEVTOOLS_MODULE], park_door(desk)
    )
    policy = judge(grants)
    assert policy.decide(CREATION).effect == "allow"
    parked(desk)

    desk.grants.publish("a", [])

    assert policy.decide(CREATION).effect == "ask"
    request = desk.mailbox.parked()
    assert request is not None
    assert ConcernAllowance.NEW_DEVTOOLS_MODULE.value in request.reason
    assert "a" in request.reason


def test_a_withdrawal_is_reported_once_rather_than_on_every_judgment(
    tmp_path: Path,
) -> None:
    """A park is a directive, so repeating it would overwrite a later one."""
    desk = broker(tmp_path, [gated_concern("a")])
    grants = desk.grants.lease(
        "a", [ConcernAllowance.NEW_DEVTOOLS_MODULE], park_door(desk)
    )
    judge(grants).decide(CREATION)
    desk.grants.publish("a", [])
    judge(grants).decide(CREATION)
    desk.mailbox.clear_park()

    judge(grants).decide(CREATION)

    parked(desk)


@pytest.mark.parametrize(
    ("planned", "answered", "expected"),
    [
        ([], [], []),
        ([ConcernAllowance.ANTIPATTERN_SUPPRESSION], [], ["antipattern-suppression"]),
        ([], [ConcernAllowance.NEW_DEVTOOLS_MODULE], ["new-devtools-module"]),
        (
            [ConcernAllowance.NEW_DEVTOOLS_MODULE],
            [ConcernAllowance.NEW_DEVTOOLS_MODULE],
            ["new-devtools-module"],
        ),
    ],
)
def test_what_a_concern_holds_is_its_plan_plus_what_a_human_since_granted(
    planned: list[ConcernAllowance],
    answered: list[ConcernAllowance],
    expected: list[str],
) -> None:
    """One derivation, so the publisher and the launcher cannot disagree."""
    subject = gated_concern("a").model_copy(update={"allowances": planned})
    answers = [
        QuestionAnswer(question_id=allowance_question_id("a", item), value="grant")
        for item in answered
    ]

    assert [item.value for item in concern_grants(subject, answers)] == expected
