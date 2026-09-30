"""`review wait` carries out what the operator approved, in the session that asked.

A parked call is refused while it waits, so the agent does not retry it: the
waiter, started in the session's own shell, reports each review as it
settles and carries out an approved one there -- an edit as the after-document
the operator saw, where the file still stands as it did; a command in the
directory recorded with it. It carries out nothing another session asked,
nothing altered since the operator answered, and nothing twice.
"""

import json
import threading
import time
import os
from pathlib import Path

import pytest
import sh
from typer.testing import CliRunner

from lup.coordination.identity import MEMBER_ENV
from lup.devtools.review.app import create_review_app
from lup.coordination.repository import RepositoryPeers
from lup.coordination.wake import WakePath
from lup.devtools.review import wait as waiter
from lup.devtools.review.wait import ReviewWaiters
from lup.policy.assets.host import review_records
from lup.policy.identity import DASHBOARD_URL_ENV
from lup.policy.relay import QuestionRelay, RecordedQuestion
from lup.providers.claude.identity import CLAUDE_SESSION_ENV
from lup.types import JsonObject
from tests.unit.native import claude_effect

RUNNER = CliRunner()
SESSION = "waiting-session"
DISPATCHER = Path(".claude/plugins/lup/hooks/scripts/policy.py")


def asked(root: Path, tool: str, arguments: JsonObject, session: str = SESSION) -> str:
    """Put one call to the generated Claude hook, as this session makes it.

    Under a launch holding a dashboard, where every question the hook asks
    is parked for the operator.
    """
    payload: JsonObject = {
        "session_id": session,
        "cwd": str(root),
        "hook_event_name": "PreToolUse",
        "tool_name": tool,
        "tool_input": arguments,
    }
    answered = json.loads(
        str(
            sh.Command(str(DISPATCHER.resolve()))(
                _in=json.dumps(payload),
                _env={
                    **os.environ,
                    "CLAUDE_PLUGIN_DATA": str(root / "plugin-data"),
                    DASHBOARD_URL_ENV: "http://127.0.0.1:8766",
                },
            )
        )
    )
    return claude_effect(answered)


@pytest.fixture
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    checkout = tmp_path / "checkout"
    (checkout / ".git").mkdir(parents=True)
    (checkout / ".git/HEAD").write_text("ref: refs/heads/feature\n")
    monkeypatch.setenv(CLAUDE_SESSION_ENV, SESSION)
    monkeypatch.delenv(MEMBER_ENV, raising=False)
    return checkout


def relay_of(root: Path) -> QuestionRelay:
    return QuestionRelay(root / ".lup/questions.jsonl")


def only(root: Path) -> RecordedQuestion:
    (question,) = relay_of(root).pending()
    return question


ESCALATED = "# lup: escalate[decision]: the operator reviews this\n"


def test_an_approved_command_runs_where_it_was_asked_and_reports_it(
    root: Path,
) -> None:
    command = f"{ESCALATED}echo carried > marker.txt"
    assert asked(root, "Bash", {"command": command}) == "ask"
    question = only(root)
    relay_of(root).answer(question.id, "operator", True, "go ahead")

    waited = RUNNER.invoke(create_review_app(root), ["wait", question.id])

    assert waited.exit_code == 0, waited.output
    assert f"review {question.id} — ran:" in waited.output
    assert (root / "marker.txt").read_text() == "carried\n"
    carried = relay_of(root).find(question.id)
    assert carried is not None and carried.state == "completed"
    assert asked(root, "Bash", {"command": command}) == "ask"
    assert (root / "marker.txt").read_text() == "carried\n"


def test_an_approved_edit_writes_the_document_the_operator_saw(root: Path) -> None:
    target = root / "README.md"
    target.write_text("# Before\n")
    assert (
        asked(root, "Write", {"file_path": str(target), "content": "# After\n"})
        == "ask"
    )
    question = only(root)
    relay_of(root).answer(question.id, "operator", True)

    waited = RUNNER.invoke(create_review_app(root), ["wait", question.id])

    assert waited.exit_code == 0, waited.output
    assert f"review {question.id} — applied:" in waited.output
    assert target.read_text() == "# After\n"


def test_an_edit_whose_file_moved_since_is_reported_and_not_written(
    root: Path,
) -> None:
    target = root / "README.md"
    target.write_text("# Before\n")
    asked(root, "Write", {"file_path": str(target), "content": "# After\n"})
    question = only(root)
    relay_of(root).answer(question.id, "operator", True)
    target.write_text("# Somebody else's\n")

    waited = RUNNER.invoke(create_review_app(root), ["wait", question.id])

    assert waited.exit_code == 1
    assert (
        f"review {question.id} — stale: {target} changed since this was recorded"
        in waited.output
    )
    assert target.read_text() == "# Somebody else's\n"
    retired = relay_of(root).find(question.id)
    assert retired is not None and retired.state == "stale"
    assert retired.moved == [target]


def test_a_declined_review_reports_the_operator_s_note(root: Path) -> None:
    command = f"{ESCALATED}echo nothing > marker.txt"
    asked(root, "Bash", {"command": command})
    question = only(root)
    relay_of(root).answer(question.id, "operator", False, "use the other branch")

    waited = RUNNER.invoke(create_review_app(root), ["wait", question.id])

    assert waited.exit_code == 1
    assert f"review {question.id} — declined:" in waited.output
    assert "operator note: use the other branch" in waited.output
    assert not (root / "marker.txt").exists()


def test_a_review_another_session_asked_is_carried_out_by_none_other(
    root: Path,
) -> None:
    command = f"{ESCALATED}echo foreign > marker.txt"
    asked(root, "Bash", {"command": command}, session="another-session")
    question = only(root)
    relay_of(root).answer(question.id, "operator", True)

    waited = RUNNER.invoke(create_review_app(root), ["wait", question.id])

    assert waited.exit_code == 2
    assert "another session" in waited.output
    assert not (root / "marker.txt").exists()
    untouched = relay_of(root).find(question.id)
    assert untouched is not None and untouched.state == "approved"


def test_a_review_altered_after_its_answer_is_not_carried_out(root: Path) -> None:
    command = f"{ESCALATED}echo approved > marker.txt"
    asked(root, "Bash", {"command": command})
    question = only(root)
    relay_of(root).answer(question.id, "operator", True)
    log = root / ".lup/questions.jsonl"
    (_, record) = review_records(log)
    parked = record["parked"]
    altered = {
        **parked,
        "operation": {
            **parked["operation"],
            "payload": {"command": f"{ESCALATED}echo swapped > marker.txt"},
        },
    }
    with log.open("a", encoding="utf-8") as appended:
        appended.write(json.dumps({"parked": altered}) + "\n")

    waited = RUNNER.invoke(create_review_app(root), ["wait", question.id])

    assert waited.exit_code == 1
    assert "changed after" in waited.output
    assert not (root / "marker.txt").exists()


def test_an_approval_is_spent_once(root: Path) -> None:
    command = f"{ESCALATED}echo once >> marker.txt"
    asked(root, "Bash", {"command": command})
    question = only(root)
    relay_of(root).answer(question.id, "operator", True)
    app = create_review_app(root)

    first = RUNNER.invoke(app, ["wait", question.id])
    second = RUNNER.invoke(app, ["wait", question.id])

    assert first.exit_code == 0, first.output
    assert "already carried out" in second.output
    assert (root / "marker.txt").read_text() == "once\n"


def test_with_no_review_named_it_waits_on_every_one_this_session_asked(
    root: Path,
) -> None:
    for name in ("first", "second"):
        asked(root, "Bash", {"command": f"{ESCALATED}echo {name} > {name}.txt"})
    asked(
        root,
        "Bash",
        {"command": f"{ESCALATED}echo theirs > theirs.txt"},
        session="another-session",
    )
    store = relay_of(root)
    pending = store.pending()
    ours = [question for question in pending if question.operation.session == SESSION]
    (theirs,) = [question for question in pending if question not in ours]
    store.answer(ours[0].id, "operator", True)

    waited = RUNNER.invoke(create_review_app(root), ["wait", "--any"])

    (waiting, *_) = waited.output.splitlines()
    assert waited.exit_code == 0, waited.output
    assert ours[0].id in waiting and ours[1].id in waiting
    assert theirs.id not in waiting
    assert f"review {ours[0].id} — ran:" in waited.output
    assert f"review {ours[1].id} —" not in waited.output
    assert (root / "first.txt").read_text() == "first\n"
    assert not (root / "theirs.txt").exists()


def test_a_waiter_waits_until_its_review_settles_unless_given_a_timeout(
    root: Path,
) -> None:
    """No limit of its own: only a timeout it was handed ends it early, carrying out nothing."""
    asked(root, "Bash", {"command": f"{ESCALATED}echo later > marker.txt"})
    question = only(root)

    waited = RUNNER.invoke(
        create_review_app(root), ["wait", question.id, "--timeout", "0.2"]
    )

    assert waited.exit_code == 3, waited.output
    assert f"still waiting on {question.id}" in waited.output
    assert f"lup-devtools review wait {question.id}` again" in waited.output
    assert not (root / "marker.txt").exists()
    assert only(root).state == "pending"


def test_a_long_wait_says_now_and_then_that_it_is_still_waiting(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    asked(root, "Bash", {"command": f"{ESCALATED}echo later > marker.txt"})
    question = only(root)

    status = waiter.wait_on(
        root, [question.id], False, timeout=0.5, announce=0.1, poll=0.05
    )

    said = capsys.readouterr().out
    assert status == 3
    assert said.count(f"still waiting on {question.id} (") >= 2


def test_a_waiter_is_seen_holding_what_it_waits_on(root: Path) -> None:
    waiters = ReviewWaiters(root=root)

    with waiters.holding(["review-one"]):
        assert waiters.held("review-one")
        assert not waiters.held("review-two")
    assert not waiters.held("review-one")


def codex_asked(root: Path, monkeypatch: pytest.MonkeyPatch, agent: str = "") -> str:
    """A Codex session's call parked by its generated hook, its subagent's where *agent* names one.

    The session joins the roster with Codex's wake route.
    """
    monkeypatch.delenv(CLAUDE_SESSION_ENV, raising=False)
    monkeypatch.setenv(MEMBER_ENV, "codex-member")
    RepositoryPeers(root).join(
        "codex-member",
        root,
        wake=WakePath(runtime="codex", session="codex-thread", handle="codex-thread"),
    )
    payload: JsonObject = {
        "session_id": "codex-thread",
        "cwd": str(root),
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": f"{ESCALATED}echo queued > marker.txt"},
        **({"agent_id": agent} if agent else {}),
    }
    sh.Command(str(Path(".codex/plugins/lup/hooks/scripts/policy.py").resolve()))(
        _in=json.dumps(payload),
        _ok_code=[0, 2],
        _env={**os.environ, "PLUGIN_DATA": str(root / "plugin-data")},
    )
    question = only(root)
    assert question.member == "codex-member"
    assert question.agent == agent
    return question.id


def answered_while_waiting(root: Path, review: str) -> int:
    """Run a waiter on *review*, and approve it once the waiter holds it."""
    exits: list[int] = []
    running = threading.Thread(
        target=lambda: exits.append(waiter.wait_on(root, [review], False, poll=0.02))
    )
    running.start()
    for _ in range(500):
        if ReviewWaiters(root=root).held(review):
            break
        time.sleep(0.01)
    relay_of(root).answer(review, "operator", True)
    running.join(timeout=30)
    (exit_code,) = exits
    return exit_code


def test_a_codex_session_is_woken_with_what_its_waiter_reported(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Codex starts no turn when a command its shell tool left running ends.

    So a waiter that waited queues its report into the session's thread, the
    way a peer's mail wakes it: through the member's own wake route on the
    roster, here recorded rather than sent.
    """
    review = codex_asked(root, monkeypatch)
    queued: list[str] = []
    monkeypatch.setattr(
        waiter, "wake", lambda path, message, cwd: queued.append(message)
    )

    assert answered_while_waiting(root, review) == 0

    (message,) = queued
    assert f"review {review} — ran:" in message
    assert (root / "marker.txt").read_text() == "queued\n"


def test_a_waiter_run_once_the_answer_is_in_queues_nothing(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The session's own thread runs it when the answer wakes it, and reads it there.

    Queuing the same report into the thread would start a turn for nothing.
    """
    review = codex_asked(root, monkeypatch)
    relay_of(root).answer(review, "operator", True)
    queued: list[str] = []
    monkeypatch.setattr(
        waiter, "wake", lambda path, message, cwd: queued.append(message)
    )

    waited = RUNNER.invoke(create_review_app(root), ["wait", review])

    assert waited.exit_code == 0, waited.output
    assert f"review {review} — ran:" in waited.output
    assert queued == []


def test_a_codex_subagent_s_waiter_wakes_nobody_else(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The session's thread is not the subagent, which reads its waiter itself."""
    review = codex_asked(root, monkeypatch, agent="codex-subagent")
    queued: list[str] = []
    monkeypatch.setattr(
        waiter, "wake", lambda path, message, cwd: queued.append(message)
    )

    assert answered_while_waiting(root, review) == 0

    assert queued == []
    assert (root / "marker.txt").read_text() == "queued\n"


def test_a_waiter_that_times_out_says_to_start_it_again_quietly(root: Path) -> None:
    """A waiter ending is no news: a subagent restarts it and tells nobody."""
    review = asked(root, "Bash", {"command": f"{ESCALATED}echo later > marker.txt"})

    waited = RUNNER.invoke(create_review_app(root), ["wait", review, "--timeout", "0.2"])

    assert waited.exit_code == 3, waited.output
    assert (
        f"start `uv run --directory {root} lup-devtools review wait {review}` "
        "again to keep waiting, quietly: a waiter ending is news to nobody"
    ) in waited.output
