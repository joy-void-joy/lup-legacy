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

from lup.coordination.bare import store
from lup.coordination.identity import MEMBER_ENV
from lup.coordination.repository import RepositoryPeers
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
        {"kept.md": "new\n", "notes/fresh.md": "created\n"},
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
    assert (root / "notes/fresh.md").read_text() == "created\n"
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


def test_a_file_of_several_with_no_note_parks_with_a_warning_naming_it(
    root: Path,
) -> None:
    directory = staged(
        root,
        {"noted.md": "a\n", "bare.md": "b\n"},
        {"about": {"noted.md": "Adds the setup step."}},
    )

    parked = RUNNER.invoke(app(root), ["propose", str(directory), "--why", WHY])

    assert parked.exit_code == 0, parked.output
    question = proposed(root)
    assert "Warning: the operator sees no note from you on bare.md." in parked.output
    assert f"review reply {question.id}" in parked.output
    assert "noted.md" not in parked.output.split("Warning:")[1]


def test_one_file_needs_no_note_beside_the_why(root: Path) -> None:
    directory = staged(root, {"only.md": "a\n"})

    parked = RUNNER.invoke(app(root), ["propose", str(directory), "--why", WHY])

    assert parked.exit_code == 0, parked.output
    assert "Warning" not in parked.output


def test_an_empty_why_is_refused_pointing_at_the_help(root: Path) -> None:
    directory = staged(root, {"only.md": "a\n"})

    refused = RUNNER.invoke(app(root), ["propose", str(directory), "--why", "  "])

    assert refused.exit_code == 2
    assert "`review propose --help`" in refused.output
    assert relay(root).pending() == []


def test_show_prints_each_file_s_note_whole_under_its_name(root: Path) -> None:
    note = "Adds the [missing] `uv sync` step, " + "so a fresh clone builds. " * 8
    directory = staged(
        root,
        {"a.md": "a\n", "b.md": "b\n"},
        {"about": {"a.md": note, "b.md": "Fixes a typo; no behaviour change."}},
    )
    RUNNER.invoke(app(root), ["propose", str(directory), "--why", WHY])

    shown = RUNNER.invoke(app(root), ["show", proposed(root).id])

    assert shown.exit_code == 0, shown.output
    assert " ".join(note.split()) in " ".join(shown.output.split())
    assert "agent's note: Fixes a typo; no behaviour change." in shown.output


def checkout_at(path: Path) -> Path:
    """A directory Git would call a checkout: a `.git` holding a HEAD."""
    (path / ".git").mkdir(parents=True)
    (path / ".git/HEAD").write_text("ref: refs/heads/feature\n")
    return path


def test_a_proposal_for_another_checkout_is_kept_in_the_session_s_queue(
    root: Path, tmp_path: Path
) -> None:
    """Run with the session's own code, into its queue, landing where the directory lies."""
    feature = checkout_at(tmp_path / "feature")
    (feature / ".pre-commit-config.yaml").write_text("repos: []\n")
    directory = staged(feature, {".pre-commit-config.yaml": "repos: [x]\n"})

    parked = RUNNER.invoke(app(root), ["propose", str(directory), "--why", WHY])

    assert parked.exit_code == 0, parked.output
    assert not (feature / ".lup/questions.jsonl").exists()
    question = proposed(root)
    assert question.operation.worktree == feature
    summary = ReviewDetail.of(root, question, "operator").summary
    assert summary.target == str(feature)
    assert summary.paths == [".pre-commit-config.yaml"]
    assert summary.title.endswith(" .pre-commit-config.yaml")
    assert f"in {feature}" in parked.output
    relay(root).answer(question.id, "operator", True)

    waited = RUNNER.invoke(app(root), ["wait", question.id])

    assert waited.exit_code == 0, waited.output
    assert (feature / ".pre-commit-config.yaml").read_text() == "repos: [x]\n"


def test_a_proposal_names_the_checkout_its_files_land_in(
    root: Path, tmp_path: Path
) -> None:
    feature = checkout_at(tmp_path / "feature")
    directory = staged(root, {"fresh.md": "created\n"})

    parked = RUNNER.invoke(
        app(root),
        ["propose", str(directory), "--why", WHY, "--checkout", str(feature)],
    )

    assert parked.exit_code == 0, parked.output
    detail = ReviewDetail.of(root, proposed(root), "operator")
    assert [file.path for file in detail.files] == [str(feature / "fresh.md")]


def test_a_proposal_from_another_checkout_than_the_session_s_is_refused_with_the_way(
    root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """That checkout's code writes its own queue, which the operator's page may not read."""
    feature = checkout_at(tmp_path / "feature")
    directory = staged(feature, {"notes.md": "# Notes\n"})
    monkeypatch.setenv("LUP_BOUNDARY_ROOT", str(root))
    monkeypatch.chdir(feature)

    refused = RUNNER.invoke(app(feature), ["propose", "tmp/cdx", "--why", WHY])

    assert refused.exit_code == 2
    assert "nothing was parked" in refused.output
    assert (
        f"`uv run --directory {root} lup-devtools review propose {directory} --why"
    ) in refused.output
    assert relay(feature).pending() == []


def test_a_proposal_from_a_subdirectory_is_kept_at_the_top_of_its_checkout(
    root: Path,
) -> None:
    below = root / "docs"
    below.mkdir()
    directory = staged(root, {"notes.md": "# Notes\n"})

    parked = RUNNER.invoke(app(below), ["propose", str(directory), "--why", WHY])

    assert parked.exit_code == 0, parked.output
    assert not (below / ".lup").exists()
    assert proposed(root).operation.worktree == root


@pytest.mark.parametrize(
    ("running", "agent", "rule"),
    [
        ("subagent", "a7c1", "nothing else wakes a subagent"),
        ("session", "", "the operator's answer wakes this session"),
        ("", "", "From a session's own conversation:"),
    ],
    ids=["subagent", "session", "nobody-can-tell"],
)
def test_a_proposal_records_the_conversation_running_it(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    running: str,
    agent: str,
    rule: str,
) -> None:
    """As the hooks record ``agent`` off a call, read here off the roster's open command window."""
    member = "proposing-member"
    monkeypatch.setenv(MEMBER_ENV, member)
    peers = RepositoryPeers(root)
    peers.join(member, root, cli_name="lead")
    store.joined_subagent(
        peers.root,
        member,
        store.Caller(
            agent_id="a7c1", agent_type="general-purpose", cwd=str(root), name="b"
        ),
    )
    window = {"subagent": store.subagent_id(member, "a7c1"), "session": member}
    if running:
        windows = peers.root / store.WINDOWS_DIR
        windows.mkdir(parents=True, exist_ok=True)
        (windows / f"{window[running]}.json").write_text("{}")
    directory = staged(root, {"notes.md": "# Notes\n"})

    parked = RUNNER.invoke(app(root), ["propose", str(directory), "--why", WHY])

    assert parked.exit_code == 0, parked.output
    question = proposed(root)
    assert question.member == member
    assert question.agent == agent
    assert rule in " ".join(parked.output.split())
