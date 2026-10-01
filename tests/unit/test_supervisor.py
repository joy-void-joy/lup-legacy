# lup: ignore[own-model-dispatch]
# Which event a projection difference produced is the claim these tests make:
# asserting `ConcernEvent` is the assertion, not a branch taken on the way to
# one. Letting the event name itself would hand the code under test the answer
# the test exists to check.
"""Web supervision of persisted resolver runs, through the mailbox alone.

There is no session, no hub, and no thread here because the supervisor no
longer needs any: it reads a run's files and writes offers into that run's
mailbox. Every test therefore builds a run on disk and drives the app over
it, exactly as a page drives a run nothing in this process is attached to.
"""

import os
import re  # lup: ignore[import-re] — extracting identifiers from packaged JS
import time
from pathlib import Path
from typing import get_args

import pytest
from httpx import ASGITransport, AsyncClient

from lup.providers.harness import AdapterName
from lup.harness.models import ResolveSpec, SkillInvocation
from lup.channels.models import utc_now
from lup.resolver.record import Journal, JournalEntry, PhaseChangedEvent, RunEvent
from lup.sessions.events import TurnEvent
from lup.coordination.mailbox import AnswerDoor, RecordedAnswer
from lup.coordination.questions import QuestionAnswer
from lup.coordination.refs import ActorRef
from lup.resolver.mailbox import run_cohort
from lup.coordination.roster import Roster
from lup.resolver.mailbox import PendingQuestion, QuestionMailbox
from lup.resolver.models import (
    AcceptanceCriterion,
    AnswerBatch,
    Concern,
    ConcernProgress,
    ConcernStatus,
    IntegrationRecord,
    MaterialQuestion,
    QuestionBatch,
    ResolvePhase,
    ResolveState,
    SourceSnapshot,
)
from lup.resolver.state import ResolverStateRepository
from lup.devtools.supervisor import doors
from lup.devtools.supervisor.app import create_supervisor
from lup.devtools.supervisor.events import stream
from lup.devtools.utils import Refusal
from lup.devtools.supervisor.projection import (
    LIVENESS_WINDOW_SECONDS,
    RunIndex,
    RunStatus,
    SupervisorState,
    concern_views,
    run_is_live,
    supervisor_state,
)

BASE_URL = "http://127.0.0.1:8766"


def question(
    identifier: str, choices: list[str] | None = None, closed: bool = False
) -> MaterialQuestion:
    return MaterialQuestion(
        id=identifier,
        concern_id="alpha",
        prompt=f"Decide {identifier}?",
        choices=choices or [],
        recommendation=(choices or [None])[0],
        closed_choices=closed,
    )


def concern_of(identifier: str) -> Concern:
    return Concern(
        id=identifier,
        title=identifier.title(),
        spec=f"Resolve {identifier}",
        criteria=[AcceptanceCriterion(id=f"{identifier}-done", description="done")],
    )


def persisted_state(
    phase: ResolvePhase = ResolvePhase.WORKERS,
    questions: QuestionBatch | None = None,
    answers: AnswerBatch | None = None,
    integration: IntegrationRecord | None = None,
) -> ResolveState:
    return ResolveState(
        config_digest="config-sha",
        run_id="run-1",
        phase=phase,
        source=SourceSnapshot(branch="dev", commit="source-sha"),
        spec=ResolveSpec(
            id="resolve",
            worker_identity="resolver-worker",
            worker_skill=SkillInvocation(plugin="lup", skill="worker"),
            review_skill=SkillInvocation(plugin="lup", skill="review"),
            merge_skill=SkillInvocation(plugin="lup", skill="merge"),
        ),
        concerns=[concern_of("alpha")],
        progress=[ConcernProgress(concern_id="alpha")],
        questions=questions,
        answers=answers,
        integration=integration,
    )


def build_run(tmp_path: Path, state: ResolveState | None = None) -> QuestionMailbox:
    """Persist one run and hand back the mailbox every door writes into."""
    ResolverStateRepository(tmp_path, "run-1").save(state or persisted_state())
    return QuestionMailbox(tmp_path / "run-1")


def ask(
    mailbox: QuestionMailbox,
    identifier: str,
    choices: list[str] | None,
    closed: bool = False,
) -> None:
    mailbox.queue(
        PendingQuestion(
            run_id="run-1",
            question=question(identifier, choices, closed),
            asked_by="alpha",
            asked_at=utc_now(),
        )
    )


def client_for(state_root: Path) -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=create_supervisor(state_root, BASE_URL, "run-1")),
        base_url=BASE_URL,
    )


async def test_supervisor_serves_its_built_page(tmp_path: Path) -> None:
    """The page is the `supervisor` bundle under `lup.web`'s package data,
    served by name with its script reachable under the bundle's own assets."""
    build_run(tmp_path)
    async with client_for(tmp_path) as client:
        response = await client.get("/")
        script = next(
            name
            for name in response.text.split('"')
            if name.startswith("./assets/") and name.endswith(".js")
        )
        asset = await client.get(script.removeprefix("./"))

    assert response.status_code == 200
    assert asset.status_code == 200
    assert "Resolver supervision" in asset.text
    assert "Pending questions" in asset.text


async def test_a_run_reads_back_from_its_mailbox(tmp_path: Path) -> None:
    mailbox = build_run(tmp_path)
    ask(mailbox, "q1", ["yes", "no"])

    async with client_for(tmp_path) as client:
        listing = await client.get("/api/runs")
        read = await client.get("/api/state")
    index = RunIndex.model_validate(listing.json())
    state = SupervisorState.model_validate(read.json())

    assert [run.run_id for run in index.runs] == ["run-1"]
    assert index.runs[0].pending_questions == 1
    assert [view.question.id for view in state.pending] == ["q1"]
    assert state.pending[0].answered is None
    assert state.concerns[0].status is ConcernStatus.DISCOVERED
    assert "--answer q1=<value>" in state.rerun_recipe
    assert state.progress_line == "discovered 1 of 1"


async def test_browser_answers_become_offers_a_promoter_can_take(
    tmp_path: Path,
) -> None:
    mailbox = build_run(tmp_path)
    ask(mailbox, "q1", ["yes", "no"])

    async with client_for(tmp_path) as client:
        response = await client.post(
            "/api/runs/run-1/answers",
            json={"answers": [{"question_id": "q1", "value": "no"}]},
        )

    assert response.status_code == 200
    offers = mailbox.offers()
    assert [(offer.question_id, offer.value) for offer in offers] == [("q1", "no")]
    assert offers[0].door is AnswerDoor.PAGE
    assert mailbox.answers() == []


async def test_a_partial_answer_set_is_accepted(tmp_path: Path) -> None:
    """Answering one of two open questions is the point, not an error.

    The single-open-batch invariant is gone: concerns ask concurrently, and
    whoever knows one decision answers it without waiting to know the rest.
    """
    mailbox = build_run(tmp_path)
    ask(mailbox, "q1", ["yes", "no"])
    ask(mailbox, "q2", None)

    async with client_for(tmp_path) as client:
        response = await client.post(
            "/api/runs/run-1/answers",
            json={"answers": [{"question_id": "q1", "value": "yes"}]},
        )

    assert response.status_code == 200
    assert [offer.question_id for offer in mailbox.offers()] == ["q1"]


@pytest.mark.parametrize(
    "answers",
    [
        [{"question_id": "q1", "value": "yes"}, {"question_id": "ghost", "value": "y"}],
        [{"question_id": "gate", "value": "maybe"}],
        [{"question_id": "q1", "value": "yes"}, {"question_id": "q1", "value": "no"}],
    ],
    ids=["unknown", "outside-a-closed-gate", "duplicate"],
)
async def test_a_bad_answer_set_is_correctable_instead_of_fatal(
    tmp_path: Path,
    answers: list[dict[str, str]],  # lup: ignore[dict-str-payload] — raw JSON body
) -> None:
    mailbox = build_run(tmp_path)
    ask(mailbox, "q1", ["yes", "no"])
    ask(mailbox, "gate", ["accept", "reject"], closed=True)

    async with client_for(tmp_path) as client:
        response = await client.post(
            "/api/runs/run-1/answers", json={"answers": answers}
        )

    assert response.status_code == 400
    assert mailbox.offers() == []


async def test_an_offer_stays_correctable_until_it_is_promoted(tmp_path: Path) -> None:
    mailbox = build_run(tmp_path)
    ask(mailbox, "q1", ["yes", "no"])

    async with client_for(tmp_path) as client:
        await client.post(
            "/api/runs/run-1/answers",
            json={"answers": [{"question_id": "q1", "value": "yes"}]},
        )
        await client.post(
            "/api/runs/run-1/answers",
            json={"answers": [{"question_id": "q1", "value": "no"}]},
        )

    assert [(offer.question_id, offer.value) for offer in mailbox.offers()] == [
        ("q1", "no")
    ]


async def test_an_answered_question_reports_its_promoted_value(tmp_path: Path) -> None:
    mailbox = build_run(tmp_path)
    ask(mailbox, "q1", ["yes", "no"])
    mailbox.record(
        RecordedAnswer(
            run_id="run-1",
            answer=QuestionAnswer(question_id="q1", value="yes"),
            door=AnswerDoor.FLAG,
            answered_at=utc_now(),
        )
    )

    async with client_for(tmp_path) as client:
        read = await client.get("/api/state")
    state = SupervisorState.model_validate(read.json())

    assert state.pending[0].answered == "yes"
    assert state.status is RunStatus.RUNNING
    assert "--answer" not in state.rerun_recipe


async def test_parking_from_the_page_writes_the_park_request(tmp_path: Path) -> None:
    mailbox = build_run(tmp_path)
    ask(mailbox, "q1", None)

    async with client_for(tmp_path) as client:
        response = await client.post(
            "/api/runs/run-1/park", json={"reason": "answering tomorrow"}
        )

    assert response.status_code == 200
    request = mailbox.parked()
    assert request is not None
    assert request.reason == "answering tomorrow"


async def test_a_message_reaches_an_actor_without_parking_the_run(
    tmp_path: Path,
) -> None:
    """A message settles nothing, so no amount of messaging can park a run.

    That is the whole reason messages ride a member's mailbox and decisions
    ride slots. The recipient is spawned first because a mailbox belongs to a
    member: the address is resolved against the population, and a spelling
    this run never recorded is reported rather than written into a directory
    nobody reads.
    """
    mailbox = build_run(tmp_path)
    cohort = run_cohort(mailbox, "run-1")
    worker = ActorRef(kind="worker", id="a")
    cohort.roster.spawned(worker, "resolve a")

    async with client_for(tmp_path) as client:
        sent = await client.post(
            "/api/runs/run-1/messages",
            json={"text": "the sibling already renamed that", "to_actor": "worker:a#1"},
        )
        missing = await client.post(
            "/api/runs/run-1/messages",
            json={"text": "anyone there", "to_actor": "worker:nobody"},
        )

    assert sent.status_code == 200
    assert [message.text for message in cohort.mail.waiting(worker).messages] == [
        "the sibling already renamed that"
    ]
    assert sent.json()["status"] != "awaiting_answers"
    assert missing.status_code == 400
    assert "no actor this run has recorded" in missing.json()["detail"]


async def test_a_redirect_naming_nobody_stops_whoever_is_working(
    tmp_path: Path,
) -> None:
    """Every live actor, resolved at the send rather than matched at each read.

    A redirect denies a tool call, so "everyone" can only honestly mean
    everyone with a call to deny. An actor spawned afterwards was spawned
    knowing about the stop and is left alone.
    """
    mailbox = build_run(tmp_path)
    cohort = run_cohort(mailbox, "run-1")
    merger = ActorRef(kind="merger", id="integration")
    reviewer = ActorRef(kind="reviewer", id="b", round=2)
    cohort.roster.spawned(merger, "integrate")
    cohort.roster.spawned(reviewer, "review b")

    async with client_for(tmp_path) as client:
        stopped = await client.post(
            "/api/runs/run-1/messages",
            json={"text": "stop rewriting", "redirect": True},
        )

    assert stopped.status_code == 200
    assert len(cohort.mail.waiting(merger).messages) == 1
    assert len(cohort.mail.waiting(reviewer).messages) == 1


async def test_a_message_naming_nobody_is_refused_rather_than_broadcast(
    tmp_path: Path,
) -> None:
    """Mail is addressed and consumed, so there is no honest reading of "all"."""
    build_run(tmp_path)

    async with client_for(tmp_path) as client:
        refused = await client.post(
            "/api/runs/run-1/messages", json={"text": "stop rewriting"}
        )

    assert refused.status_code == 400
    assert "notice" in refused.json()["detail"]


async def test_a_notice_stands_over_the_run_until_it_is_taken_down(
    tmp_path: Path,
) -> None:
    """State rather than mail: read at every turn head, and never consumed."""
    mailbox = build_run(tmp_path)
    cohort = run_cohort(mailbox, "run-1")

    async with client_for(tmp_path) as client:
        posted = await client.post(
            "/api/runs/run-1/notices", json={"text": "the base moved under all of you"}
        )
        [standing] = cohort.mail.standing()
        lifted = await client.delete(f"/api/runs/run-1/notices/{standing.id}")
        absent = await client.delete("/api/runs/run-1/notices/nothing")

    assert posted.status_code == 200
    assert standing.text == "the base moved under all of you"
    assert lifted.status_code == 200
    assert absent.status_code == 404
    assert cohort.mail.standing() == []


async def test_the_review_branch_is_reported_with_no_decision_to_take(
    tmp_path: Path,
) -> None:
    """The gate retired, so the page hands the branch over rather than asking.

    What it reports is mechanical — the branch and what verification did.
    There is no verdict to render because nothing persists one: whether the
    merged concerns are jointly right is read off the trace by whoever lands
    the branch, and that reader is the only actor able to act on the answer.
    """
    build_run(
        tmp_path,
        persisted_state(
            phase=ResolvePhase.VERIFICATION,
            integration=IntegrationRecord(
                branch="resolve/run-1/review",
                worktree=tmp_path / "integration",
                concerns=["alpha"],
                completed=True,
            ),
        ),
    )

    async with client_for(tmp_path) as client:
        response = await client.get("/api/runs/run-1")
        gone = await client.post("/api/runs/run-1/decision", json={"accepted": True})

    assert response.json()["review"]["review_branch"] == "resolve/run-1/review"
    assert gone.status_code == 404


async def test_a_missing_run_is_reported_rather_than_invented(tmp_path: Path) -> None:
    async with client_for(tmp_path) as client:
        response = await client.get("/api/runs/ghost")

    assert response.status_code == 404


async def test_no_door_takes_the_run_lock(tmp_path: Path) -> None:
    """A live run holds ``.run.lock`` for its whole life.

    A door that reached for it — even to ask whether a run is live — could
    only ever serve runs that had already finished, and a shared probe can
    make a concurrently starting run fail to take its exclusive lease.
    """
    mailbox = build_run(tmp_path)
    ask(mailbox, "q1", ["yes", "no"])

    def refuse(_self: ResolverStateRepository) -> None:
        raise AssertionError("a supervisor door took the resolver state lock")

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(ResolverStateRepository, "exclusive", refuse)
        async with client_for(tmp_path) as client:
            await client.get("/api/runs")
            await client.get("/api/state")
            await client.post(
                "/api/runs/run-1/answers",
                json={"answers": [{"question_id": "q1", "value": "yes"}]},
            )
            parked = await client.post("/api/runs/run-1/park", json={})

    assert parked.status_code == 200


async def test_resuming_a_moving_run_is_refused(tmp_path: Path) -> None:
    """Two resolvers over one run would race for its exclusive lease."""
    build_run(tmp_path)

    async with client_for(tmp_path) as client:
        response = await client.post("/api/runs/run-1/resume", json={})

    assert response.status_code == 409


def test_console_doors_read_and_answer_without_the_run_lock(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    mailbox = build_run(tmp_path)
    ask(mailbox, "q1", ["yes", "no"])

    def refuse(_self: ResolverStateRepository) -> None:
        raise AssertionError("a console door took the resolver state lock")

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(ResolverStateRepository, "exclusive", refuse)
        patch.setattr(doors, "resolve_state_root", lambda: tmp_path)
        doors.list_questions(run_id="run-1", pending_only=True)
        listed = capsys.readouterr().out
        doors.answer_questions(pairs=["q1=no"], run_id="run-1")
        doors.park_run(run_id="run-1", reason="answering tomorrow")

    assert "q1 (concern alpha)" in listed
    assert "choices: yes | no" in listed
    assert [(offer.question_id, offer.value) for offer in mailbox.offers()] == [
        ("q1", "no")
    ]
    assert mailbox.offers()[0].door is AnswerDoor.CONSOLE
    parked = mailbox.parked()
    assert parked is not None and parked.reason == "answering tomorrow"


def test_an_answered_question_stops_reading_as_waiting_on_the_human(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`--pending` means waiting on you, not waiting on the promoter.

    Answering and then listing reported every question back as pending,
    because an offer is not a promoted answer — so the one command a human
    runs to check their work said nothing had been recorded, and counting
    files on disk was the only way to tell.
    """
    mailbox = build_run(tmp_path)
    ask(mailbox, "q1", ["yes", "no"])
    ask(mailbox, "q2", ["yes", "no"])

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(doors, "resolve_state_root", lambda: tmp_path)
        doors.answer_questions(pairs=["q1=no"], run_id="run-1")
        doors.list_questions(run_id="run-1", pending_only=True)

    listed = capsys.readouterr().out
    assert "q2 (concern alpha)" in listed, "a genuinely unanswered question is hidden"
    assert "q1 (concern alpha)" not in listed, (
        "an answered question still reads pending"
    )
    assert "1 answered and awaiting promotion" in listed


@pytest.mark.parametrize(
    "pairs",
    [["ghost=yes"], ["gate=maybe"], ["q1"]],
    ids=["unknown", "outside-a-closed-gate", "malformed"],
)
def test_a_console_answer_is_refused_before_it_reaches_the_mailbox(
    tmp_path: Path, pairs: list[str]
) -> None:
    mailbox = build_run(tmp_path)
    ask(mailbox, "q1", ["yes", "no"])
    ask(mailbox, "gate", ["accept", "reject"], closed=True)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(doors, "resolve_state_root", lambda: tmp_path)
        with pytest.raises(Refusal):
            doors.answer_questions(pairs=pairs, run_id="run-1")

    assert mailbox.offers() == []


def test_a_console_answer_may_reject_every_choice_a_design_question_offered(
    tmp_path: Path,
) -> None:
    """The planner's choices are suggestions, so the door forwards free text."""
    mailbox = build_run(tmp_path)
    ask(mailbox, "q1", ["yes", "no"])

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(doors, "resolve_state_root", lambda: tmp_path)
        doors.answer_questions(pairs=["q1=neither, split it in two"], run_id="run-1")

    assert [(offer.question_id, offer.value) for offer in mailbox.offers()] == [
        ("q1", "neither, split it in two")
    ]


def test_a_redirect_reports_who_it_is_queued_for_rather_than_that_it_sent(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`redirected <actor>` was printed on the strength of writing a file.

    Nothing between writing the stream and the actor reading it said which
    had happened, so a redirect that reached nobody read exactly like one
    that stopped a worker mid-design.
    """
    build_run(tmp_path)
    # The population record, which is what a door resolves an address
    # against — the run announces each round as it opens one.
    Roster(tmp_path / "run-1").spawned(
        ActorRef(kind="worker", id="alpha", round=2), "resolve alpha"
    )

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(doors, "resolve_state_root", lambda: tmp_path)
        doors.redirect_actor(
            text="superseded, stop", run_id="run-1", to="worker:alpha#1"
        )
        queued = capsys.readouterr().out
        doors.list_actors(run_id="run-1")
        listed = capsys.readouterr().out

    assert "queued for worker:alpha#2" in queued
    assert "undelivered redirect from agent: superseded, stop" in listed


def test_a_message_for_an_actor_nobody_recorded_says_so(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Not a refusal: a concern that has not started reads this at its first turn."""
    build_run(tmp_path)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(doors, "resolve_state_root", lambda: tmp_path)
        doors.say_to_actor(
            text="the file moved", run_id="run-1", to="worker:ghost#1", in_reply_to=""
        )

    assert "names no actor this run has recorded yet" in capsys.readouterr().out


def test_a_console_door_refuses_a_run_that_was_never_recorded(tmp_path: Path) -> None:
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(doors, "resolve_state_root", lambda: tmp_path)
        with pytest.raises(Refusal) as refused:
            doors.list_questions(run_id="ghost", pending_only=False)

    assert "names no resolver run" in refused.value.said["why"]


async def test_an_unexpected_host_header_is_refused(tmp_path: Path) -> None:
    build_run(tmp_path)
    async with AsyncClient(
        transport=ASGITransport(app=create_supervisor(tmp_path, BASE_URL, "run-1")),
        base_url="http://evil.example",
    ) as client:
        response = await client.get("/")

    assert response.status_code == 421


def test_a_finished_run_falls_back_to_the_state_file_fold(tmp_path: Path) -> None:
    """A run recorded before the mailbox existed still renders its questions."""
    state = persisted_state(
        phase=ResolvePhase.COMPLETE,
        questions=QuestionBatch(run_id="run-1", questions=[question("q1", ["yes"])]),
        answers=AnswerBatch(
            run_id="run-1", answers=[QuestionAnswer(question_id="q1", value="yes")]
        ),
    )
    projected = supervisor_state(
        state, QuestionMailbox(tmp_path / "empty"), AdapterName.CLAUDE
    )

    assert [view.question.id for view in projected.pending] == ["q1"]
    assert projected.pending[0].answered == "yes"
    assert projected.status is RunStatus.COMPLETE


def test_liveness_is_derived_from_activity_not_from_a_lock() -> None:
    moving = persisted_state(phase=ResolvePhase.WORKERS)
    finished = persisted_state(phase=ResolvePhase.COMPLETE)

    assert run_is_live(moving, activity=100.0, now=100.0)
    assert not run_is_live(moving, activity=0.0, now=LIVENESS_WINDOW_SECONDS + 1.0)
    assert not run_is_live(finished, activity=100.0, now=100.0)


def test_an_unanswered_question_parks_a_run_that_stopped_moving() -> None:
    state = persisted_state(
        phase=ResolvePhase.WORKERS,
        questions=QuestionBatch(run_id="run-1", questions=[question("q1", None)]),
    )
    quiet = supervisor_state(
        state, QuestionMailbox(Path("/nonexistent")), AdapterName.CLAUDE, now=1e12
    )

    assert not quiet.live
    assert quiet.status is RunStatus.PARKED


def test_an_aborted_run_is_neither_live_nor_running() -> None:
    """Aborted is terminal: a recent write must not dress it as moving."""
    aborted = persisted_state(phase=ResolvePhase.ABORTED)

    assert not run_is_live(aborted, activity=100.0, now=100.0)
    projected = supervisor_state(
        aborted, QuestionMailbox(Path("/nonexistent")), AdapterName.CLAUDE, now=100.0
    )
    assert projected.status is RunStatus.ABORTED


def projection(
    phase: ResolvePhase = ResolvePhase.WORKERS, status: ConcernStatus | None = None
) -> SupervisorState:
    state = persisted_state(phase=phase)
    if status is not None:
        state = state.model_copy(
            update={"progress": [ConcernProgress(concern_id="alpha", status=status)]}
        )
    return supervisor_state(
        state, QuestionMailbox(Path("/nonexistent")), AdapterName.CLAUDE
    )


async def test_the_stream_replays_the_record_then_follows_it(tmp_path: Path) -> None:
    journal = Journal(tmp_path / "run-1")
    journal.record(PhaseChangedEvent(phase=ResolvePhase.REVIEW))

    body = stream(journal, interval=0.0, heartbeat=0.0)
    opening = await anext(body)
    replayed = await anext(body)
    journal.record(PhaseChangedEvent(phase=ResolvePhase.COMPLETE))
    followed = await anext(body)
    await body.aclose()

    assert opening.startswith("retry:")
    assert replayed.startswith("id: 0\n")
    assert '"phase":"review"' in replayed
    assert followed.startswith("id: 1\n")
    assert '"phase":"complete"' in followed


async def test_a_reconnect_replays_only_what_it_missed(tmp_path: Path) -> None:
    """The sequence number a reader last saw is what makes a resume exact."""
    journal = Journal(tmp_path / "run-1")
    journal.record(PhaseChangedEvent(phase=ResolvePhase.REVIEW))
    journal.record(PhaseChangedEvent(phase=ResolvePhase.COMPLETE))

    body = stream(journal, after_seq=0, interval=0.0, heartbeat=0.0)
    frames = [await anext(body) for _ in range(2)]
    await body.aclose()

    assert frames[0].startswith("retry:")
    assert frames[1].startswith("id: 1\n")


async def test_a_fresh_reader_gets_a_bounded_tail_not_the_whole_run(
    tmp_path: Path,
) -> None:
    """Replaying a long run whole froze the page; state comes from the
    projection, so a fresh page needs recent context, not the record."""
    journal = Journal(tmp_path / "run-1")
    for _ in range(6):
        journal.record(PhaseChangedEvent(phase=ResolvePhase.REVIEW))

    body = stream(journal, interval=0.0, heartbeat=0.0, catchup=2)
    opening = await anext(body)
    frames = [await anext(body) for _ in range(2)]
    await body.aclose()

    assert opening.startswith("retry:")
    assert [frame.split("\n")[0] for frame in frames] == ["id: 4", "id: 5"]


async def test_a_resuming_reader_is_never_bounded(tmp_path: Path) -> None:
    """A named sequence is a promise: everything after it, exactly."""
    journal = Journal(tmp_path / "run-1")
    for _ in range(6):
        journal.record(PhaseChangedEvent(phase=ResolvePhase.REVIEW))

    body = stream(journal, after_seq=0, interval=0.0, heartbeat=0.0, catchup=2)
    opening = await anext(body)
    frames = [await anext(body) for _ in range(5)]
    await body.aclose()

    assert opening.startswith("retry:")
    assert [frame.split("\n")[0] for frame in frames] == [
        f"id: {seq}" for seq in range(1, 6)
    ]


async def test_the_stream_keeps_a_quiet_connection_alive(tmp_path: Path) -> None:
    body = stream(Journal(tmp_path / "run-1"), interval=0.0, heartbeat=0.0)
    frames = [await anext(body) for _ in range(2)]
    await body.aclose()

    assert frames[1] == ": keep-alive\n\n"


async def test_record_older_than_the_tail_is_served_page_by_page(
    tmp_path: Path,
) -> None:
    """What the bounded catch-up skipped stays reachable on demand."""
    build_run(tmp_path)
    journal = Journal(tmp_path / "run-1")
    for _ in range(6):
        journal.record(PhaseChangedEvent(phase=ResolvePhase.REVIEW))

    async with client_for(tmp_path) as client:
        page = await client.get(
            "/api/runs/run-1/journal", params={"before": 4, "count": 2}
        )
        start = await client.get(
            "/api/runs/run-1/journal", params={"before": 0, "count": 2}
        )

    served = [JournalEntry.model_validate(item) for item in page.json()]
    assert [entry.seq for entry in served] == [2, 3]
    assert start.json() == []


SURFACE = Path("packages/lup/web/src/supervisor")
"""The supervisor's TypeScript source, which the roster tests read the arms out of."""


def test_the_page_draws_every_event_the_journal_can_record() -> None:
    """A trace that omits what it cannot name is not a record.

    The page switches on `event.type` in one component, and an event union
    it has never heard of falls to the arm that draws it raw. Reading the
    arms back out of the source is what makes adding an event to either
    union fail here — where the new event is named — rather than in a trace
    somebody is trying to read, and it is the one thing the type checker
    cannot hold the page to, since the raw arm accepts anything.
    """
    entries = (SURFACE / "entries.tsx").read_text("utf-8")
    drawn = set(
        re.findall(  # lup: ignore[re-call] — switch arms in the page's source
            r"""case ["'](\w+)["']:""", entries
        )
    )
    recordable = {
        member.model_fields["type"].default
        for union in (RunEvent, TurnEvent)
        for member in get_args(union.__value__)
    }

    assert recordable
    assert sorted(recordable - drawn) == []


def test_the_page_posts_only_routes_the_app_serves() -> None:
    """The routes are spelled once, in the page's API module, and read here.

    The types across the seam are compiled, but a route is a path and not a
    model: a renamed one still type-checks and answers 404.
    """
    posted = re.findall(  # lup: ignore[re-call] — fetch targets in the page's source
        r"""api/runs/\$\{[^}]+\}/(\w+)""", (SURFACE / "api.ts").read_text("utf-8")
    )

    assert sorted(dict.fromkeys(posted)) == [
        "answers",
        "events",
        "journal",
        "messages",
        "park",
        "resume",
    ]


def test_a_concern_without_a_progress_row_still_projects() -> None:
    """A reader can arrive between a concern being written and its progress."""
    state = persisted_state().model_copy(update={"progress": []})

    views = concern_views(state)

    assert [view.id for view in views] == ["alpha"]
    assert views[0].reason == "no progress recorded yet"


async def test_one_malformed_run_costs_its_own_row_and_no_others(
    tmp_path: Path,
) -> None:
    """The rail is what an operator opens when a run went wrong."""
    build_run(tmp_path)
    broken = tmp_path / "run-0"
    broken.mkdir()
    (broken / "state.json").write_text("{not json", encoding="utf-8")

    async with client_for(tmp_path) as client:
        response = await client.get("/api/runs")

    assert response.status_code == 200
    rows = RunIndex.model_validate(response.json()).runs
    assert sorted(row.run_id for row in rows) == ["run-0", "run-1"]
    assert [row.run_id for row in rows if row.unreadable] == ["run-0"]


async def test_the_rail_is_ordered_by_activity_not_by_run_id(tmp_path: Path) -> None:
    """Run ids are commit digests, so name order is arbitrary in time."""
    for name, age in (("run-1", 0.0), ("run-0", 5000.0), ("run-2", 50.0)):
        state = persisted_state().model_copy(update={"run_id": name})
        ResolverStateRepository(tmp_path, name).save(state)
        stamp = time.time() - age
        os.utime(tmp_path / name / "state.json", (stamp, stamp))

    async with client_for(tmp_path) as client:
        response = await client.get("/api/runs")

    rows = RunIndex.model_validate(response.json()).runs
    assert [row.run_id for row in rows] == ["run-1", "run-2", "run-0"]
