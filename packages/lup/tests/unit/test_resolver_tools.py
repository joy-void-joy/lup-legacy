# lup: ignore[own-model-dispatch]
# Each question tool answers with one of several typed outputs, and these
# tests pin which one a call produced before reading its fields — the returned
# type is the contract under test, observed from the caller's side.
"""The material-question tools a resolver worker asks through."""

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest

from lup.execution.process import LaunchRequest, LocalProcessLauncher
from lup.tools.mcp import LupMcpTool, ToolError
from lup.policy.identity import ConcernAllowance
from lup.coordination.mailbox import AnswerDoor, RecordedAnswer
from lup.coordination.questions import QuestionAnswer
from lup.resolver.mailbox import QuestionMailbox
from lup.coordination.tools import (
    AskedQuestion,
    AwaitAnswersInput,
    AwaitAnswersOutput,
    QueueQuestionsInput,
    QueueQuestionsOutput,
)
from lup.resolver.tools import (
    CheckDeclarationInput,
    CheckDeclarationOutput,
    RequestAllowanceInput,
    ResolverToolContext,
    create_question_tools,
    read_resolver_tool_context,
)

EPOCH = datetime(2026, 7, 27, 12, 0, tzinfo=UTC)


def tools_for(
    mailbox: QuestionMailbox,
    *,
    wake: asyncio.Event | None = None,
    lease_root: Path | None = None,
) -> dict[str, LupMcpTool]:
    built = create_question_tools(
        mailbox,
        "alpha",
        run_id="run-1",
        lease_root=lease_root if lease_root is not None else mailbox.root,
        wait_seconds=0.05,
        poll_interval_seconds=0.01,
        wake=wake,
    )
    return {tool.name: tool for tool in built}


def asked(identifier: str, choices: list[str] | None = None) -> AskedQuestion:
    return AskedQuestion(
        id=identifier, prompt=f"Decide {identifier}?", choices=choices or []
    )


def promote(mailbox: QuestionMailbox, identifier: str, value: str) -> None:
    mailbox.record(
        RecordedAnswer(
            run_id="run-1",
            answer=QuestionAnswer(question_id=identifier, value=value),
            door=AnswerDoor.PAGE,
            answered_at=EPOCH,
        )
    )


async def test_queueing_returns_composed_ids_without_waiting(tmp_path: Path) -> None:
    mailbox = QuestionMailbox(tmp_path)
    tools = tools_for(mailbox)

    result = await tools["queue_questions"](
        QueueQuestionsInput(questions=[asked("shape"), asked("naming")])
    )

    assert isinstance(result, QueueQuestionsOutput)
    assert result.question_ids == ["alpha-shape", "alpha-naming"]
    assert result.pending == ["alpha-shape", "alpha-naming"]
    assert [item.question.concern_id for item in mailbox.questions()] == [
        "alpha",
        "alpha",
    ]


async def test_an_edit_gate_closes_its_domain_where_a_design_question_does_not(
    tmp_path: Path,
) -> None:
    """A gate is read by machinery that recognizes one spelling.

    Answered "yes" instead of "grant", an open gate settles as a refusal
    silently, and the worker waiting on it is handed an answer with no gate
    behind it. Closed, the same reply comes back as a correctable problem.
    A design question's choices stay suggestions the human may answer past.
    """
    mailbox = QuestionMailbox(tmp_path)
    tools = tools_for(mailbox)

    await tools["request_allowance"](
        RequestAllowanceInput(
            allowance=ConcernAllowance.NEW_DEVTOOLS_MODULE,
            reason="the module this concern adds has nowhere else to go",
        )
    )
    await tools["queue_questions"](QueueQuestionsInput(questions=[asked("shape")]))

    closed = {
        item.question.id: item.question.closed_choices for item in mailbox.questions()
    }
    assert closed["alpha-allow-new-devtools-module"]
    assert not closed["alpha-shape"]


async def test_a_worker_cannot_post_against_a_sibling_concern(tmp_path: Path) -> None:
    """The concern is bound by the factory, so ids are composed, not trusted."""
    mailbox = QuestionMailbox(tmp_path)
    tools = tools_for(mailbox)

    await tools["queue_questions"](QueueQuestionsInput(questions=[asked("beta-x")]))

    assert [item.question.id for item in mailbox.questions()] == ["alpha-beta-x"]


async def test_an_answer_that_landed_first_is_reported_not_awaited(
    tmp_path: Path,
) -> None:
    mailbox = QuestionMailbox(tmp_path)
    tools = tools_for(mailbox)
    promote(mailbox, "alpha-shape", "tuple")

    result = await tools["queue_questions"](
        QueueQuestionsInput(questions=[asked("shape")])
    )

    assert result.already_answered == ["alpha-shape"]
    assert result.pending == []


async def test_waiting_returns_the_promoted_answers(tmp_path: Path) -> None:
    mailbox = QuestionMailbox(tmp_path)
    wake = asyncio.Event()
    tools = tools_for(mailbox, wake=wake)
    await tools["queue_questions"](QueueQuestionsInput(questions=[asked("shape")]))
    promote(mailbox, "alpha-shape", "a BaseModel")
    wake.set()

    result = await tools["await_answers"](AwaitAnswersInput())

    assert isinstance(result, AwaitAnswersOutput)
    assert result.status == "answered"
    assert [item.value for item in result.answers] == ["a BaseModel"]
    assert result.unanswered == []


async def test_waiting_on_an_unqueued_id_is_refused_rather_than_blocked(
    tmp_path: Path,
) -> None:
    mailbox = QuestionMailbox(tmp_path)
    tools = tools_for(mailbox)

    with pytest.raises(ToolError, match="no question is queued"):
        await tools["await_answers"](AwaitAnswersInput(question_ids=["alpha-ghost"]))


async def test_waiting_before_queuing_anything_is_refused(tmp_path: Path) -> None:
    mailbox = QuestionMailbox(tmp_path)
    tools = tools_for(mailbox)

    with pytest.raises(ToolError, match="queue a question"):
        await tools["await_answers"](AwaitAnswersInput())


async def test_a_partial_answer_keeps_waiting_and_then_parks(tmp_path: Path) -> None:
    mailbox = QuestionMailbox(tmp_path)
    tools = tools_for(mailbox)
    await tools["queue_questions"](
        QueueQuestionsInput(questions=[asked("shape"), asked("naming")])
    )
    promote(mailbox, "alpha-shape", "tuple")

    result = await tools["await_answers"](AwaitAnswersInput())

    assert result.status == "parked"
    assert [item.id for item in result.answers] == ["alpha-shape"]
    assert result.unanswered == ["alpha-naming"]
    assert "submit your report" in result.instruction


async def test_reasking_a_question_differently_is_refused(tmp_path: Path) -> None:
    mailbox = QuestionMailbox(tmp_path)
    tools = tools_for(mailbox)
    await tools["queue_questions"](QueueQuestionsInput(questions=[asked("shape")]))

    with pytest.raises(ToolError, match="already asked differently"):
        await tools["queue_questions"](
            QueueQuestionsInput(questions=[asked("shape", ["a", "b"])])
        )


async def test_a_recommendation_outside_the_choices_is_a_readable_refusal(
    tmp_path: Path,
) -> None:
    mailbox = QuestionMailbox(tmp_path)
    tools = tools_for(mailbox)

    with pytest.raises(ToolError, match="not well formed"):
        await tools["queue_questions"](
            QueueQuestionsInput(
                questions=[
                    AskedQuestion(
                        id="shape",
                        prompt="Decide?",
                        choices=["a", "b"],
                        recommendation="c",
                    )
                ]
            )
        )


async def test_ask_questions_queues_and_waits_in_one_call(tmp_path: Path) -> None:
    mailbox = QuestionMailbox(tmp_path)
    wake = asyncio.Event()
    tools = tools_for(mailbox, wake=wake)

    async def answer_once() -> None:
        await asyncio.sleep(0)
        promote(mailbox, "alpha-shape", "yes")
        wake.set()

    answering = asyncio.create_task(answer_once())
    result = await tools["ask_questions"](
        QueueQuestionsInput(questions=[asked("shape")])
    )
    await answering

    assert result.status == "answered"
    assert [item.value for item in result.answers] == ["yes"]


def test_the_context_round_trips_through_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = ResolverToolContext(
        run_dir=tmp_path, concern_id="alpha", lease_root=tmp_path / "lease"
    )
    for name, value in context.to_env().items():
        monkeypatch.setenv(name, value)

    assert read_resolver_tool_context() == context


def test_no_context_outside_a_tool_subprocess(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LUP_RESOLVER_RUN_DIR", raising=False)
    monkeypatch.delenv("LUP_RESOLVER_CONCERN", raising=False)
    monkeypatch.delenv("LUP_RESOLVER_LEASE_ROOT", raising=False)

    assert read_resolver_tool_context() is None


def lease_with(tmp_path: Path, *, tracked: str, edits: dict[str, str]) -> Path:
    """One committed worktree, then edited the way a worker's turn edits it."""
    launcher = LocalProcessLauncher()
    root = tmp_path / "lease"
    root.mkdir()
    (root / tracked).write_text("committed\n", encoding="utf-8")
    for arguments in (
        ["init"],
        ["add", "-A"],
        ["commit", "-m", "base"],
    ):
        # Identity per invocation, never `git config` — a misbound command then
        # writes nothing, where a persisted setting lands in the shared config
        # every worktree of a real repository inherits (see `lup.devtools.gitguard`).
        status = launcher.launch(
            LaunchRequest(
                arguments=[
                    "git",
                    "-c",
                    "user.email=lease@example.invalid",
                    "-c",
                    "user.name=lease",
                    *arguments,
                ],
                cwd=root,
            )
        )
        assert status.code == 0, status.stderr
    for name, content in edits.items():
        (root / name).write_text(content, encoding="utf-8")
    return root


async def declaration_for(
    tmp_path: Path, root: Path, params: CheckDeclarationInput
) -> CheckDeclarationOutput:
    tools = tools_for(QuestionMailbox(tmp_path / "run"), lease_root=root)
    answer = await tools["check_declaration"](params)
    assert isinstance(answer, CheckDeclarationOutput)
    return answer


async def test_an_account_naming_what_changed_settles(tmp_path: Path) -> None:
    """The gate accepts what this settles, so a worker can stop guessing."""
    root = lease_with(tmp_path, tracked="kept.py", edits={"kept.py": "edited\n"})

    answer = await declaration_for(
        tmp_path, root, CheckDeclarationInput(files_changed=[Path("kept.py")])
    )

    assert answer.settled
    assert answer.changed == ["kept.py"]


async def test_a_path_no_commit_holds_yet_is_still_seen(tmp_path: Path) -> None:
    """A worker's new files are most of what it must account for."""
    root = lease_with(tmp_path, tracked="kept.py", edits={"added.py": "new\n"})

    answer = await declaration_for(tmp_path, root, CheckDeclarationInput())

    assert answer.undeclared == ["added.py"]


async def test_both_directions_arrive_in_one_answer(tmp_path: Path) -> None:
    """Reporting one at a time makes the contract oscillate.

    A worker told only that it under-declared corrects by declaring the set
    it expected to touch, and hears about the over-declaration a round
    later — each verdict correct, the pair of them a trap. A concern can
    spend every round it has crossing back and forth and be marked failed
    with its acceptance criteria never once evaluated.
    """
    root = lease_with(tmp_path, tracked="kept.py", edits={"added.py": "new\n"})

    answer = await declaration_for(
        tmp_path,
        root,
        CheckDeclarationInput(swept_beyond_scope=[Path("untouched.py")]),
    )

    assert answer.undeclared == ["added.py"]
    assert answer.unswept == ["untouched.py"]


async def test_a_worker_reaches_a_settled_account_inside_one_turn(
    tmp_path: Path,
) -> None:
    """The whole point: converging costs calls, not sessions.

    Charging each correction a session costs a rejected report, a terminated
    session, and a fresh worker respawned with none of its own reasoning —
    for bookkeeping a subprocess can check.
    """
    root = lease_with(tmp_path, tracked="kept.py", edits={"added.py": "new\n"})

    first = await declaration_for(
        tmp_path,
        root,
        CheckDeclarationInput(swept_beyond_scope=[Path("untouched.py")]),
    )
    corrected = await declaration_for(
        tmp_path,
        root,
        CheckDeclarationInput(files_changed=[Path(path) for path in first.undeclared]),
    )

    assert not first.settled
    assert corrected.settled
