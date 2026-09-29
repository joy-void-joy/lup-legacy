"""One authority for every final ask, and the ways an approval must not travel.

What is pinned here is mostly refusals, because that is what an approval
authority *is*: the value of recording a question is not that somebody can say
yes, it is that nobody else can, that the yes covers exactly what was shown,
and that it is spent once.
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from multiprocessing import get_context
from pathlib import Path
from threading import Barrier
from time import sleep

import pytest

from lup.policy.assets.host import review_records
from lup.policy.kernel.semantics import ReviewerRequirement
from lup.policy.operations import MutationFootprint, Operation
from lup.policy.relay import (
    PersistentQuestion,
    Principal,
    QuestionRelay,
    ReceiptKind,
    SupervisorChain,
)


def operation(command: str = "rm build", requester: str = "worker") -> Operation:
    """One normalized call, with a target so the fingerprint has something to cover."""
    return Operation(
        id="op-1",
        session="session-1",
        requester=requester,
        tool="Bash",
        payload={"command": command},
        cwd=Path("/repo"),
        worktree=Path("/repo"),
        mutations=MutationFootprint(deletions=[Path("/repo/build")]),
    )


def chain() -> SupervisorChain:
    """A worker under an agent supervisor under the person who launched the run."""
    return SupervisorChain(
        principals=[
            Principal(id="worker", kind="agent", supervisor="lead"),
            Principal(id="lead", kind="agent", supervisor="person"),
            Principal(id="person", kind="human"),
        ]
    )


def parked(
    tmp_path: Path,
    requirement: ReviewerRequirement = "supervisor_allowed",
    requester: str = "worker",
    expires: datetime | None = None,
) -> tuple[QuestionRelay, PersistentQuestion]:
    """One question recorded exactly as the coordinator would record it."""
    relay = QuestionRelay(tmp_path / "questions.jsonl")
    call = operation(requester=requester)
    question = PersistentQuestion(
        id="q-1",
        operation=call,
        fingerprint=call.fingerprint(),
        reason="deleting files requires approval",
        requirement=requirement,
        eligible=chain().eligible(requester, requirement),
        expires=expires,
    )
    return relay, relay.record(question)


def test_the_requester_can_never_answer_its_own_question(tmp_path: Path) -> None:
    """The one refusal an approval authority is for.

    Checked on the question rather than at each caller, because a caller that
    remembered only the eligibility list is one that lets a requester answer
    itself by appearing somewhere in its own chain.
    """
    relay, _ = parked(tmp_path)

    with pytest.raises(ValueError, match="may not answer"):
        relay.answer("q-1", "worker", approved=True)


def test_a_human_only_question_skips_every_agent_supervisor(tmp_path: Path) -> None:
    """A chain that can pass a question along can pass it to somebody who answers.

    So a human-only requirement removes the agents from eligibility outright
    rather than trusting them to decline.
    """
    relay, question = parked(tmp_path, requirement="human_only")

    assert question.eligible == ["person"]
    with pytest.raises(ValueError, match="may not answer"):
        relay.answer("q-1", "lead", approved=True)
    assert relay.answer("q-1", "person", approved=True).state == "approved"


def test_a_supervisor_allowed_question_reaches_the_whole_chain(tmp_path: Path) -> None:
    """A quality checkpoint is about how code reads, which a supervisor reads."""
    _, question = parked(tmp_path)

    assert question.eligible == ["lead", "person"]


def test_an_answer_is_single_use(tmp_path: Path) -> None:
    """A second answer is a second authorization for one thing being shown once."""
    relay, _ = parked(tmp_path)
    relay.answer("q-1", "lead", approved=True)

    with pytest.raises(ValueError, match="approved and cannot be answered again"):
        relay.answer("q-1", "person", approved=False)


def delay_question_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    """Expose stale-read races by holding every read before its caller appends."""
    original = QuestionRelay.find

    def delayed_find(self: QuestionRelay, question: str) -> PersistentQuestion | None:
        found = original(self, question)
        sleep(0.05)
        return found

    monkeypatch.setattr(QuestionRelay, "find", delayed_find)


@pytest.mark.parametrize("separate_relays", [False, True])
def test_concurrent_threads_record_exactly_one_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, separate_relays: bool
) -> None:
    """Opposing browser and CLI decisions cannot overwrite each other's receipt."""
    relay, _ = parked(tmp_path)
    delay_question_reads(monkeypatch)
    ready = Barrier(8)

    def answer(index: int) -> str:
        store = QuestionRelay(relay.path) if separate_relays else relay
        ready.wait(timeout=5)
        try:
            return store.answer("q-1", "person", index % 2 == 0).state
        except ValueError as refusal:
            assert "cannot be answered again" in str(refusal)
            return "refused"

    with ThreadPoolExecutor(max_workers=8) as workers:
        results = list(workers.map(answer, range(8)))

    assert results.count("refused") == 7
    assert results.count("approved") + results.count("rejected") == 1
    assert len(review_records(relay.path)) == 2
    settled = relay.find("q-1")
    assert settled is not None and settled.answer is not None
    assert settled.state in results
    assert settled.answer.approved == (settled.state == "approved")


def test_concurrent_processes_record_exactly_one_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A lock held in Python memory cannot serialize separate operator commands."""
    relay, _ = parked(tmp_path)
    delay_question_reads(monkeypatch)
    context = get_context("fork")
    ready = context.Barrier(2)

    def answer(approved: bool) -> None:
        ready.wait(timeout=5)
        try:
            QuestionRelay(relay.path).answer("q-1", "person", approved)
        except ValueError as refusal:
            assert "cannot be answered again" in str(refusal)
            raise SystemExit(2) from refusal

    processes = [
        context.Process(target=answer, args=(choice,)) for choice in (True, False)
    ]
    try:
        for process in processes:
            process.start()
        for process in processes:
            process.join(timeout=10)
        assert not any(process.is_alive() for process in processes)
        assert {process.exitcode for process in processes} == {0, 2}
    finally:
        for process in processes:
            if process.is_alive():
                process.kill()
            process.join(timeout=5)

    assert len(review_records(relay.path)) == 2
    settled = relay.find("q-1")
    assert settled is not None and settled.answer is not None
    assert settled.answer.approved == (settled.state == "approved")


def test_an_expired_question_is_not_answered_late(tmp_path: Path) -> None:
    """A question nobody answered in time is not one somebody answered.

    Recorded as expired rather than refused with an exception, because the
    coordinator that finds it needs a state to act on and not a failure to
    handle.
    """
    relay, _ = parked(tmp_path, expires=datetime.now(UTC) - timedelta(minutes=1))

    assert relay.answer("q-1", "person", approved=True).state == "expired"


def test_an_answer_carries_a_note_to_the_agent(tmp_path: Path) -> None:
    """Yes-plus-instructions and no-plus-instructions without a second message.

    The note reaches the agent with the resumption or the refusal, which is
    the moment it is worth reading — and the moment a person is most able to
    write it, since they are looking at the operation.
    """
    relay, _ = parked(tmp_path)

    answered = relay.answer(
        "q-1", "lead", approved=True, note="drop the -r while you are there"
    )

    assert answered.answer is not None
    assert answered.answer.note == "drop the -r while you are there"


def test_an_approval_binds_to_what_was_shown(tmp_path: Path) -> None:
    """A payload that changed after somebody answered is a fresh question.

    The fingerprint covers the tool, the arguments, the directory, the
    resolved targets and the placement — everything the person read — so a
    substitution afterwards cannot inherit their answer.
    """
    _, question = parked(tmp_path)
    changed = operation(command="rm -rf /")

    assert question.fingerprint == operation().fingerprint()
    assert question.fingerprint != changed.fingerprint()


def test_a_safe_outer_call_carrying_a_different_inner_one_is_a_different_operation(
    tmp_path: Path,
) -> None:
    """The substitution an approval must not survive.

    A nested operation is part of the fingerprint, so approving `ls && rm a`
    does not authorize `ls && rm b` — which reads identically at the outer
    level and is the whole shape of the attack the binding is for.
    """
    outer = operation(command="ls && rm a")
    nested = outer.model_copy(update={"nested": [operation(command="rm a")]})
    swapped = outer.model_copy(update={"nested": [operation(command="rm b")]})

    assert nested.fingerprint() != swapped.fingerprint()


def test_a_historical_rejection_retains_its_inferred_provenance(tmp_path: Path) -> None:
    """Historical observations remain distinguishable from explicit answers."""
    relay, _ = parked(tmp_path)

    refused = relay.answer("q-1", "person", approved=False, receipt="inferred")

    assert refused.answer is not None
    assert (refused.answer.approved, refused.answer.receipt) == (False, "inferred")


@pytest.mark.parametrize("receipt", ["observed", "inferred"])
def test_unrecorded_answers_are_never_dispatchable(
    tmp_path: Path, receipt: ReceiptKind
) -> None:
    relay, _ = parked(tmp_path)
    relay.answer("q-1", "person", approved=True, receipt=receipt)
    with pytest.raises(ValueError, match="no recorded independent approval"):
        relay.dispatchable("q-1")


def test_only_an_approved_question_reaches_the_executor(tmp_path: Path) -> None:
    """The single-use gate sits on the route rather than at each caller.

    A check each caller performs is one each caller can forget, and the one
    that forgets is the one that dispatches an approval twice.
    """
    relay, _ = parked(tmp_path)

    with pytest.raises(ValueError, match="pending, so nothing may be dispatched"):
        relay.dispatchable("q-1")

    relay.answer("q-1", "lead", approved=True)
    assert relay.dispatchable("q-1").state == "approved"

    relay.advance("q-1", "dispatched")
    with pytest.raises(ValueError, match="dispatched, so nothing may be dispatched"):
        relay.dispatchable("q-1")


def test_an_uncertain_dispatch_is_named_rather_than_retried(tmp_path: Path) -> None:
    """A retry is a second external effect, and this promises at-most-once dispatch.

    `in_doubt` is the honest name for a crash between sending an operation and
    recording its outcome. Nothing guesses; a typed broker may reconcile one
    where the remote system offers an authoritative status query.
    """
    relay, _ = parked(tmp_path)
    relay.answer("q-1", "lead", approved=True)
    relay.advance("q-1", "dispatched")

    doubted = relay.advance("q-1", "in_doubt", "the executor died mid-call")

    assert doubted.state == "in_doubt"
    assert doubted.completed is not None
    with pytest.raises(ValueError, match="in_doubt, so nothing may be dispatched"):
        relay.dispatchable("q-1")


def test_the_queue_survives_a_torn_write_at_the_end_of_the_log(
    tmp_path: Path,
) -> None:
    """The expected shape of a crash must not lose every question before it.

    An append-only log torn mid-record is what a process dying while writing
    looks like, so the reader skips what will not parse rather than refusing
    the whole queue over it.
    """
    relay, _ = parked(tmp_path)
    with relay.path.open("a", encoding="utf-8") as handle:
        handle.write('{"id": "q-2", "operat')

    assert [entry.id for entry in relay.questions()] == ["q-1"]
    damaged = relay.path.read_bytes()
    relay.answer("q-1", "lead", True)
    assert relay.path.read_bytes().startswith(damaged)
    assert relay.dispatchable("q-1").state == "approved"


def test_concurrent_appenders_preserve_every_large_record(tmp_path: Path) -> None:
    relay, question = parked(tmp_path)
    questions = [
        question.model_copy(update={"id": f"q-{index}", "reason": "evidence" * 10000})
        for index in range(2, 18)
    ]
    with ThreadPoolExecutor(max_workers=8) as workers:
        list(workers.map(relay.record, questions))
    assert {item.id: item for item in relay.questions()} == {
        item.id: item for item in [question, *questions]
    }


def test_a_cycle_in_the_chain_climbs_to_the_human_rather_than_hanging(
    tmp_path: Path,
) -> None:
    """A missing supervisor, a cycle, and a self-supervisor resolve alike.

    Upward rather than closed, because a question that reaches nobody in a
    session where somebody is right there is the worse failure — and upward
    rather than sideways, because the human is the one principal whose
    authority is not derived from another.
    """
    looped = SupervisorChain(
        principals=[
            Principal(id="a", kind="agent", supervisor="b"),
            Principal(id="b", kind="agent", supervisor="a"),
        ]
    )
    alone = SupervisorChain(principals=[Principal(id="a", kind="agent")])

    assert looped.eligible("a", "supervisor_allowed") == ["b"]
    assert looped.eligible("a", "human_only") == []
    assert alone.eligible("a", "supervisor_allowed") == []


def test_a_chain_nobody_resolved_is_not_a_chain_that_found_nobody(
    tmp_path: Path,
) -> None:
    """Two different facts were sharing one empty list.

    A chain resolved to nobody is a question that climbed past its supervisors
    and found no human, and it must not be answerable by whoever asks. A chain
    nobody resolved is eligibility this boundary could not compute — the
    compiled dispatcher is hermetic and reaches a verdict without the session's
    principals — and reading that as "nobody" made every question the live path
    parked unanswerable, so the queue could only grow.
    """
    relay = QuestionRelay(tmp_path / "questions.jsonl")
    call = operation()
    unresolved = relay.record(
        PersistentQuestion(
            id="q-1",
            operation=call,
            fingerprint=call.fingerprint(),
            reason="removing a remote ref requires approval",
            eligible=[],
            chain_resolved=False,
        )
    )
    resolved = PersistentQuestion(
        id="q-2",
        operation=call,
        fingerprint=call.fingerprint(),
        reason="removing a remote ref requires approval",
        eligible=[],
    )

    assert unresolved.answerable_by("person")
    assert not resolved.answerable_by("person")


def test_an_unresolved_chain_still_excludes_the_requester(tmp_path: Path) -> None:
    """The invariant that carries the weight holds either way.

    What an unresolved chain gives up is the narrowing — who among others may
    answer — and never the one rule an approval authority exists for.
    """
    relay = QuestionRelay(tmp_path / "questions.jsonl")
    call = operation(requester="worker")
    relay.record(
        PersistentQuestion(
            id="q-1",
            operation=call,
            fingerprint=call.fingerprint(),
            reason="risky",
            chain_resolved=False,
        )
    )

    with pytest.raises(ValueError, match="may not answer"):
        relay.answer("q-1", "worker", approved=True)


def test_an_answer_given_without_a_chain_records_that_it_was(tmp_path: Path) -> None:
    """A weaker receipt is recorded as weaker rather than as approved.

    An audit that could not tell an approval given against a chain from one
    given while nothing could resolve who was entitled to give it would report
    both the same, which is the distinction worth keeping at the moment it is
    known.
    """
    relay = QuestionRelay(tmp_path / "questions.jsonl")
    call = operation()
    relay.record(
        PersistentQuestion(
            id="q-1",
            operation=call,
            fingerprint=call.fingerprint(),
            reason="risky",
            chain_resolved=False,
        )
    )

    answered = relay.answer("q-1", "person", approved=True)

    assert answered.answer is not None
    assert answered.answer.unresolved_chain
