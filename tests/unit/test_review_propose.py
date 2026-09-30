"""`review propose` parks one review for a batch of edits written under scratch.

Each file meets the edit gates a direct write of it would meet, and the review
carries each verdict; a file the gates refuse refuses the proposal. Approved,
the requester's waiter writes every file or none -- a new one created, a
declared deletion removed -- and a file moved since the proposal was recorded
stales the whole of it.
"""

import json
from pathlib import Path
from typing import Final

import pytest
import typer
from typer.testing import CliRunner

from lup.coordination.identity import MEMBER_ENV
from lup.devtools.review.app import ReviewDetail, create_review_app, relay
from lup.policy.relay import PersistentQuestion
from lup.providers.claude.identity import CLAUDE_SESSION_ENV
from lup_template.harness.catalog import declared_hook_set

SESSION: Final = "proposing-session"
RUNNER: Final = CliRunner()
WHY: Final = "Retire the amending flags, which one row read and nothing sets."


@pytest.fixture
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    checkout = tmp_path / "checkout"
    (checkout / ".git").mkdir(parents=True)
    (checkout / ".git/HEAD").write_text("ref: refs/heads/feature\n")
    monkeypatch.setenv(CLAUDE_SESSION_ENV, SESSION)
    monkeypatch.delenv(MEMBER_ENV, raising=False)
    return checkout


def app(root: Path) -> typer.Typer:
    """The `review` group as the roster composes it, over the declared hook set."""
    return create_review_app(root, declared_hook_set)


def staged(root: Path, files: dict[str, str], manifest: object = None) -> Path:
    """Write *files* under the checkout's scratch, at their paths in the checkout."""
    directory = root / "tmp/cdx"
    for relative, content in files.items():
        target = directory / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    if manifest is not None:
        (directory / ".proposal.json").write_text(json.dumps(manifest))
    return directory


def proposed(root: Path) -> PersistentQuestion:
    """The one proposal waiting, read back whole from the relay's store."""
    store = relay(root)
    (question,) = store.pending()
    return store.resolve(question)


def test_a_batch_parks_as_one_review_carrying_each_file_s_verdict(root: Path) -> None:
    (root / "notes.md").write_text("# Notes\n")
    (root / ".pre-commit-config.yaml").write_text("repos: []\n")
    directory = staged(
        root,
        {"notes.md": "# Notes\n\nMore.\n", ".pre-commit-config.yaml": "repos: [x]\n"},
        {"about": {"notes.md": "says more"}},
    )

    parked = RUNNER.invoke(app(root), ["propose", str(directory), "--why", WHY])

    assert parked.exit_code == 0, parked.output
    question = proposed(root)
    assert question.operation.tool == "Propose"
    assert question.bound()
    assert [account.text for account in question.account] == [WHY]
    detail = ReviewDetail.of(root, question, "operator")
    effects = {file.path: file.review_effect for file in detail.files}
    assert effects[str(root / ".pre-commit-config.yaml")] == "ask"
    assert effects[str(root / "notes.md")] == "allow"
    assert {file.path: file.about for file in detail.files}[
        str(root / "notes.md")
    ] == "says more"
    assert detail.summary.paths == [".pre-commit-config.yaml"]
    assert question.id in parked.output


def test_approved_it_writes_every_file_creates_one_and_deletes_one(root: Path) -> None:
    (root / "kept.md").write_text("old\n")
    (root / "gone.md").write_text("to delete\n")
    directory = staged(
        root,
        {"kept.md": "new\n", "docs/fresh.md": "created\n"},
        {"delete": ["gone.md"]},
    )
    RUNNER.invoke(app(root), ["propose", str(directory), "--why", WHY])
    question = proposed(root)
    relay(root).answer(question.id, "operator", True, "all three")

    waited = RUNNER.invoke(app(root), ["wait", question.id])

    assert waited.exit_code == 0, waited.output
    assert f"review {question.id} — applied:" in waited.output
    assert "operator note: all three" in waited.output
    assert (root / "kept.md").read_text() == "new\n"
    assert (root / "docs/fresh.md").read_text() == "created\n"
    assert not (root / "gone.md").exists()


def test_a_file_moved_since_it_was_proposed_stales_the_whole_proposal(
    root: Path,
) -> None:
    (root / "first.md").write_text("one\n")
    (root / "second.md").write_text("two\n")
    directory = staged(root, {"first.md": "one!\n", "second.md": "two!\n"})
    RUNNER.invoke(app(root), ["propose", str(directory), "--why", WHY])
    question = proposed(root)
    (root / "second.md").write_text("two, edited meanwhile\n")

    waited = RUNNER.invoke(app(root), ["wait", question.id])

    assert f"review {question.id} — stale:" in waited.output
    assert (root / "first.md").read_text() == "one\n"
    stored = relay(root).find(question.id)
    assert stored is not None and stored.state == "stale"
    assert stored.moved == [root / "second.md"]


def test_a_file_the_gates_refuse_refuses_the_proposal_naming_it(root: Path) -> None:
    directory = staged(root, {"pkg/module.py": "value = []  # type: ignore\n"})

    refused = RUNNER.invoke(app(root), ["propose", str(directory), "--why", WHY])

    assert refused.exit_code == 2
    assert "module.py" in refused.output
    assert relay(root).pending() == []


def test_a_manifest_noting_a_file_it_does_not_hold_is_refused(root: Path) -> None:
    directory = staged(root, {"a.md": "a\n"}, {"about": {"b.md": "missing"}})

    refused = RUNNER.invoke(app(root), ["propose", str(directory), "--why", WHY])

    assert refused.exit_code == 2
    assert "b.md" in refused.output
    assert relay(root).pending() == []
