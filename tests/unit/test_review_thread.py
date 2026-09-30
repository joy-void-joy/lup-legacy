"""Everything the operator writes on a review reaches the session that asked, by one channel.

The note and line comments ride with an approval as with a decline; a remark
-- words without a decision -- wakes the requester's waiter and leaves the
review waiting, and the requester answers in the review's thread. Where a
`review wait` holds the review it is the one channel: nothing is mailed
beside it. Where none does, the words go to the requester's mailbox and its
wake route. Where a subagent asked and the operator said something, the
session it runs in gets a copy; a bare approval pings nobody else. And a
waiter stopped with its review still pending says so, with the command that
waits again.
"""

import os
import signal
import threading
from pathlib import Path
from typing import Final

import pytest
from typer.testing import CliRunner

from lup.coordination.bare import store
from lup.coordination.identity import MEMBER_ENV
from lup.coordination.repository import RepositoryPeers
from lup.coordination.roster import RosterMember
from lup.coordination.wake import WakePath, Woken
from lup.devtools.review import notifications
from lup.devtools.review.app import ReviewDetail, create_review_app, relay
from lup.devtools.review.notifications import notify_requester
from lup.devtools.review.thread import ReviewThread
from lup.devtools.review.wait import ReviewWaiters, wait_on
from lup.policy.operations import Operation
from lup.policy.relay import LineComment, PersistentQuestion
from lup.providers.claude.identity import CLAUDE_SESSION_ENV
from tests.unit.native import bound

SESSION: Final = "thread-session"
RUNNER: Final = CliRunner()


@pytest.fixture
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    checkout = tmp_path / "checkout"
    (checkout / ".git").mkdir(parents=True)
    (checkout / ".git/HEAD").write_text("ref: refs/heads/feature\n")
    monkeypatch.setenv(CLAUDE_SESSION_ENV, SESSION)
    monkeypatch.delenv(MEMBER_ENV, raising=False)
    return checkout


def parked(
    root: Path,
    command: str = "echo carried > marker.txt",
    member: str = "",
    agent: str = "",
) -> PersistentQuestion:
    operation = Operation(
        id="operation-thread",
        session=SESSION,
        requester=SESSION,
        tool="Bash",
        payload={"command": command},
        cwd=root,
        worktree=root,
    )
    return relay(root).record(
        bound(
            PersistentQuestion(
                id="thread-review",
                operation=operation,
                fingerprint="",
                reason="The operator reads this command first.",
                chain_resolved=False,
                resumption="native_retry",
                member=member,
                agent=agent,
            )
        )
    )


def comment(root: Path, note: str, start: int = 3, end: int = 3) -> LineComment:
    return LineComment(path=root / "module.py", start=start, end=end, note=note)


def test_an_approval_s_note_and_line_comments_are_printed_with_what_ran(
    root: Path,
) -> None:
    question = parked(root)
    relay(root).answer(
        question.id,
        "operator",
        True,
        "keep the marker",
        comments=[comment(root, "name it after the review", 3, 5)],
    )

    waited = RUNNER.invoke(create_review_app(root), ["wait", question.id])

    assert waited.exit_code == 0, waited.output
    assert f"review {question.id} — ran:" in waited.output
    assert "operator note: keep the marker" in waited.output
    assert "module.py:3-5: name it after the review" in waited.output
    assert (root / "marker.txt").read_text() == "carried\n"


def test_a_decline_s_line_comments_are_printed_beneath_it(root: Path) -> None:
    question = parked(root)
    relay(root).answer(
        question.id,
        "operator",
        False,
        comments=[
            LineComment(
                path=root / "module.py", start=2, end=2, side="before", note="keep"
            )
        ],
    )

    waited = RUNNER.invoke(create_review_app(root), ["wait", question.id])

    assert waited.exit_code == 1
    assert f"review {question.id} — declined:" in waited.output
    assert "module.py:2 (before the change): keep" in waited.output


def test_a_remark_ends_the_wait_with_the_review_still_pending(root: Path) -> None:
    question = parked(root)
    thread = ReviewThread.of(relay(root))

    def remarked() -> None:
        thread.remark(question, "operator", "why not a flag?", [comment(root, "here")])

    threading.Timer(0.3, remarked).start()
    status = wait_on(root, [question.id], first=False, poll=0.05)

    assert status == 3
    still = relay(root).find(question.id)
    assert still is not None and still.state == "pending"
    assert not (root / "marker.txt").exists()


def test_a_remark_is_printed_with_how_to_answer_and_wait_again(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    question = parked(root)
    thread = ReviewThread.of(relay(root))
    threading.Timer(
        0.3, lambda: thread.remark(question, "operator", "why not a flag?")
    ).start()

    wait_on(root, [question.id], first=False, poll=0.05)

    printed = capsys.readouterr().out
    assert f"review {question.id} — commented:" in printed
    assert "operator note: why not a flag?" in printed
    assert f"review reply {question.id} <text>" in printed
    assert f"lup-devtools review wait {question.id}" in printed


def test_a_remark_made_before_the_wait_began_is_not_reported_again(
    root: Path,
) -> None:
    question = parked(root)
    ReviewThread.of(relay(root)).remark(question, "operator", "heard already")
    relay(root).answer(question.id, "operator", False, "no")

    waited = RUNNER.invoke(create_review_app(root), ["wait", question.id])

    assert "commented" not in waited.output
    assert "declined" in waited.output


def test_the_requester_replies_in_the_thread_and_nobody_else_may(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    question = parked(root)
    ReviewThread.of(relay(root)).remark(question, "operator", "why?")

    replied = RUNNER.invoke(
        create_review_app(root), ["reply", question.id, "because the flag is gone"]
    )
    monkeypatch.setenv(CLAUDE_SESSION_ENV, "another-session")
    foreign = RUNNER.invoke(create_review_app(root), ["reply", question.id, "me too"])

    assert replied.exit_code == 0, replied.output
    assert foreign.exit_code == 2
    thread = ReviewThread.of(relay(root)).said(question)
    assert [(entry.kind, entry.text) for entry in thread] == [
        ("remark", "why?"),
        ("reply", "because the flag is gone"),
    ]
    detail = ReviewDetail.of(root, question, "operator")
    assert detail.summary.said == 2
    assert [entry.kind for entry in detail.thread] == ["remark", "reply"]


class Wakes:
    """Every wake a notification tried, answered as the test says."""

    def __init__(self, reached: bool = True) -> None:
        self.reached = reached
        self.woken: list[str] = []

    def __call__(
        self,
        peers: RepositoryPeers,
        member: RosterMember,
        fresh: list[object],
        cwd: Path | None = None,
    ) -> Woken:
        del peers, fresh, cwd
        self.woken.append(member.actor.id)
        return Woken(reached=self.reached, reason="" if self.reached else "asleep")


@pytest.fixture
def wakes(monkeypatch: pytest.MonkeyPatch) -> Wakes:
    fake = Wakes()
    monkeypatch.setattr(notifications, "roused", fake)
    return fake


def joined(root: Path) -> str:
    peers = RepositoryPeers(root)
    member = "thread-member"
    peers.join(
        member, root, cli_name="lead", wake=WakePath(runtime="claude", session=SESSION)
    )
    return member


def test_a_waiter_holding_the_review_is_the_one_channel(
    root: Path, wakes: Wakes
) -> None:
    member = joined(root)
    question = parked(root, member=member)
    settled = relay(root).answer(question.id, "operator", True, "go ahead")

    with ReviewWaiters(root=root).holding([question.id]):
        told = notify_requester((root,), root, settled)

    assert told.waited and not told.queued and not told.woken
    assert RepositoryPeers(root).waiting(member).messages == []
    assert wakes.woken == []


def test_news_a_waiter_already_reported_is_not_mailed_again(
    root: Path, wakes: Wakes
) -> None:
    member = joined(root)
    question = parked(root, member=member)
    settled = relay(root).answer(question.id, "operator", False, "no")
    assert settled.answer is not None
    ReviewWaiters(root=root).report(question.id, settled.answer.at)

    told = notify_requester((root,), root, settled)

    assert told.waited
    assert RepositoryPeers(root).waiting(member).messages == []


def test_without_a_waiter_the_words_are_mailed_and_the_session_woken(
    root: Path, wakes: Wakes
) -> None:
    member = joined(root)
    question = parked(root, member=member)
    settled = relay(root).answer(
        question.id, "operator", True, "go ahead", comments=[comment(root, "tidy")]
    )

    told = notify_requester((root,), root, settled)

    assert told.queued and told.woken and not told.waited
    assert wakes.woken == [member]
    assert "sent to lead, which was woken" in told.detail


def test_a_subagent_s_session_gets_a_copy_of_the_operator_s_words(
    root: Path, wakes: Wakes
) -> None:
    member = joined(root)
    agent = "a7c1"
    store.joined_subagent(
        RepositoryPeers(root).root,
        member,
        store.Caller(
            agent_id=agent, agent_type="general-purpose", cwd=str(root), name="builder"
        ),
    )
    question = parked(root, member=member, agent=agent)
    remark = ReviewThread.of(relay(root)).remark(question, "operator", "why?").remark

    told = notify_requester((root,), root, question, remark)

    subagent = store.subagent_id(member, agent)
    (to_subagent,) = RepositoryPeers(root).waiting(subagent).messages
    (to_session,) = RepositoryPeers(root).waiting(member).messages
    assert "why?" in to_subagent.text and "still pending" in to_subagent.text
    assert to_session.text.startswith("[copy]")
    assert "builder" in to_session.text and "why?" in to_session.text
    assert told.copied
    assert "handed over before its next tool call" in told.detail


def test_a_bare_approval_pings_nobody_but_the_requester(
    root: Path, wakes: Wakes
) -> None:
    member = joined(root)
    agent = "a7c1"
    store.joined_subagent(
        RepositoryPeers(root).root,
        member,
        store.Caller(
            agent_id=agent, agent_type="general-purpose", cwd=str(root), name="builder"
        ),
    )
    question = parked(root, member=member, agent=agent)
    settled = relay(root).answer(question.id, "operator", True)

    with ReviewWaiters(root=root).holding([question.id]):
        told = notify_requester((root,), root, settled)

    assert told.waited and not told.copied
    assert RepositoryPeers(root).waiting(member).messages == []
    assert (
        RepositoryPeers(root).waiting(store.subagent_id(member, agent)).messages == []
    )


def test_a_waiter_stopped_by_its_runtime_says_how_to_wait_again(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    question = parked(root)
    threading.Timer(0.3, lambda: os.kill(os.getpid(), signal.SIGTERM)).start()

    status = wait_on(root, [question.id], first=False, poll=0.05)

    assert status == 3
    printed = capsys.readouterr().out
    assert "stopped by SIGTERM" in printed
    assert f"lup-devtools review wait {question.id}` again" in printed
    still = relay(root).find(question.id)
    assert still is not None and still.state == "pending"
