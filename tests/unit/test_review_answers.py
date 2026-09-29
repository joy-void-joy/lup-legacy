"""An operator's answer lives on the host, where no session can write one.

The question a hook parks stays in the checkout, since the session that asked
is the one writing it. The answer does not: it is kept in lup's own state on
the host, which a contained session reaches only through the read-only mount
its launch lends it. So a record appended to the checkout's relay — by any
spelling a session has — answers nothing, and a question altered after it was
parked is one nobody can approve.
"""

import json
import os
from pathlib import Path

import pytest
import sh
from typer.testing import CliRunner

from lup.coordination.identity import MEMBER_ENV
from lup.devtools.review.answers import ReviewAnswers
from lup.devtools.review.app import create_review_app
from lup.launch.companions import CompanionLaunch, held_companions
from lup.launch.pointer_trust import launcher_state_exposure
from lup.policy.assets.host import review_answers, review_answers_home
from lup.policy.identity import REVIEW_ANSWERS_ENV
from lup.policy.relay import QuestionRelay
from lup.sandbox.rail import Lease, same_path
from lup.types import JsonObject
from tests.unit.native import codex_effect

RUNNER = CliRunner()
PUSH = "git push origin --delete reviewed-topic"


def hook(root: Path, command: str = PUSH, **environment: str) -> str:
    """What the generated Codex hook answers one shell call with."""
    payload: JsonObject = {
        "session_id": "requester",
        "tool_use_id": "call-one",
        "cwd": str(root),
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": command},
    }
    script = Path(".codex/plugins/lup/hooks/scripts/policy.py").resolve()
    result = sh.Command(str(script))(
        _in=json.dumps(payload),
        _ok_code=[0, 2],
        _return_cmd=True,
        _env={
            **os.environ,
            "PLUGIN_DATA": str(root / "plugin-data"),
            **environment,
        },
    )
    assert isinstance(result, sh.RunningCommand)
    return codex_effect(result)


@pytest.fixture
def root(tmp_path: Path) -> Path:
    checkout = tmp_path / "checkout"
    (checkout / ".git").mkdir(parents=True)
    (checkout / ".git/HEAD").write_text("ref: refs/heads/feature\n")
    return checkout


def relay_of(root: Path) -> QuestionRelay:
    return QuestionRelay(root / ".lup/questions.jsonl")


def test_an_answer_is_kept_on_the_host_and_releases_the_exact_retry(
    root: Path,
) -> None:
    assert hook(root) == "deny"
    store = relay_of(root)
    (question,) = store.pending()

    store.answer(question.id, "operator", True, "go ahead")

    kept = review_answers(store.path, review_answers_home(REVIEW_ANSWERS_ENV))
    assert kept.is_relative_to(Path(os.environ["XDG_STATE_HOME"]) / "lup" / "reviews")
    assert question.id in kept.read_text()
    assert "approved" not in (root / ".lup/questions.jsonl").read_text()
    answered = store.find(question.id)
    assert answered is not None and answered.state == "approved"
    assert answered.answer is not None and answered.answer.note == "go ahead"
    assert hook(root) == "allow"


def test_an_approval_written_into_the_checkout_releases_nothing(root: Path) -> None:
    assert hook(root) == "deny"
    log = root / ".lup/questions.jsonl"
    (pending,) = [json.loads(line) for line in log.read_text().splitlines()]
    forged = {
        **pending,
        "state": "approved",
        "answer": {
            "approved": True,
            "principal": "operator",
            "receipt": "recorded",
            "note": "",
            "unresolved_chain": True,
            "at": "2026-09-29T00:00:00+00:00",
        },
    }
    with log.open("a", encoding="utf-8") as appended:
        appended.write(json.dumps(forged) + "\n")

    (question,) = relay_of(root).questions()
    assert question.state == "pending"
    assert hook(root) == "deny"
    assert relay_of(root).find(question.id) is not None


def test_a_session_reads_answers_where_its_launch_lent_them(
    root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A contained session is handed the host's store at the path it has on the host."""
    lent = tmp_path / "lent-answers"
    monkeypatch.setenv(REVIEW_ANSWERS_ENV, str(lent))
    assert hook(root, **{REVIEW_ANSWERS_ENV: str(lent)}) == "deny"
    store = relay_of(root)
    (question,) = store.pending()

    store.answer(question.id, "operator", True)

    assert review_answers(store.path, lent).is_file()
    assert hook(root, **{REVIEW_ANSWERS_ENV: str(lent)}) == "allow"


def test_an_answer_recorded_for_another_fingerprint_is_not_this_reviews(
    root: Path,
) -> None:
    assert hook(root) == "deny"
    store = relay_of(root)
    (question,) = store.pending()
    kept = review_answers(store.path, review_answers_home(REVIEW_ANSWERS_ENV))
    kept.parent.mkdir(parents=True)
    kept.write_text(
        json.dumps(
            {
                "question": question.id,
                "fingerprint": "0" * 64,
                "answer": {
                    "approved": True,
                    "principal": "operator",
                    "receipt": "recorded",
                },
            }
        )
        + "\n"
    )

    (unanswered,) = store.questions()
    assert unanswered.state == "pending"
    assert hook(root) == "deny"


def test_a_review_altered_after_it_was_parked_cannot_be_answered_or_spent(
    root: Path,
) -> None:
    """What the operator reads is what the fingerprint covers, or nothing is approved.

    A record rewritten to show one command under another's fingerprint is
    refused an answer; one rewritten after an answer is never spent by the
    call whose fingerprint it carries.
    """
    assert hook(root) == "deny"
    log = root / ".lup/questions.jsonl"
    (pending,) = [json.loads(line) for line in log.read_text().splitlines()]
    shown = {
        **pending,
        "operation": {**pending["operation"], "payload": {"command": "git status"}},
    }
    with log.open("a", encoding="utf-8") as appended:
        appended.write(json.dumps(shown) + "\n")
    store = relay_of(root)

    with pytest.raises(ValueError, match="changed after it was parked"):
        store.answer(pending["id"], "operator", True)
    assert hook(root) == "deny"


def test_every_launch_lends_its_repository_answers_read_only(
    root: Path, tmp_path: Path
) -> None:
    launch = CompanionLaunch(root=root, runtime="claude", environment={})

    with held_companions([ReviewAnswers()], launch) as joined:
        (mount,) = joined.mounts

    home = review_answers_home(REVIEW_ANSWERS_ENV)
    assert joined.environment == {REVIEW_ANSWERS_ENV: str(home)}
    assert not mount.writable
    assert mount.path == review_answers(root / ".lup/questions.jsonl", home).parent
    assert mount.path.is_dir()
    assert mount.path.stat().st_mode & 0o077 == 0
    assert not launcher_state_exposure(Lease(read_only=same_path([mount.path])))
    assert launcher_state_exposure(Lease(writable=same_path([mount.path])))
    assert launcher_state_exposure(Lease(read_only=same_path([home.parent])))
    del tmp_path


def test_the_operator_answers_from_outside_every_session(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert hook(root) == "deny"
    (question,) = relay_of(root).pending()
    monkeypatch.setenv(MEMBER_ENV, "cebe7dd5892f")

    answered = RUNNER.invoke(
        create_review_app(root), ["approve", question.id, "--as", "operator"]
    )

    assert answered.exit_code == 2
    assert "outside the agent session" in answered.stderr
    still = relay_of(root).find(question.id)
    assert still is not None and still.state == "pending"
