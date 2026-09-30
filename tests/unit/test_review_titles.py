"""Queue titles describe captured actions without executing their commands."""

from pathlib import Path

import pytest

from lup.devtools.review.app import ReviewSummary
from lup.policy.operations import Operation
from lup.policy.relay import PersistentQuestion
from lup.types import JsonObject


def question(
    root: Path, tool: str, payload: JsonObject, before: dict[Path, str | None]
) -> PersistentQuestion:
    operation = Operation(
        id="operation",
        session="session",
        requester="agent",
        tool=tool,
        payload=payload,
        cwd=root,
        worktree=root,
    )
    return PersistentQuestion(
        id="review-id",
        operation=operation,
        fingerprint=operation.fingerprint(),
        preconditions=before,
        reason="Review this captured action.",
        eligible=["operator"],
    )


@pytest.mark.parametrize("before,verb", [(None, "Create"), ("old\n", "Replace")])
def test_written_file_title_uses_relative_path(
    tmp_path: Path, before: str | None, verb: str
) -> None:
    path = tmp_path / "src/module.py"
    entry = question(
        tmp_path, "Write", {"file_path": str(path), "content": "new\n"}, {path: before}
    )
    summary = ReviewSummary.of(tmp_path, entry, "operator")
    assert summary.title == f"{verb} src/module.py"
    assert summary.paths == ["src/module.py"]
    assert summary.operation == entry.operation.summary()


def test_multi_file_title_keeps_all_paths(tmp_path: Path) -> None:
    paths = [tmp_path / "src" / f"file-{index}.py" for index in range(6)]
    patch = (
        "*** Begin Patch\n"
        + "".join(
            f"*** Add File: {path}\n+value = {index}\n"
            for index, path in enumerate(paths)
        )
        + "*** End Patch\n"
    )
    entry = question(tmp_path, "apply_patch", {"command": patch}, dict.fromkeys(paths))
    summary = ReviewSummary.of(tmp_path, entry, "operator")
    assert summary.title == "Change 6 files in src"
    assert summary.paths == [str(path.relative_to(tmp_path)) for path in paths]


def test_delete_title_is_distinct_from_update(tmp_path: Path) -> None:
    path = tmp_path / "obsolete.py"
    entry = question(
        tmp_path,
        "apply_patch",
        {"command": f"*** Begin Patch\n*** Delete File: {path}\n*** End Patch\n"},
        {path: "old\n"},
    )
    assert ReviewSummary.of(tmp_path, entry, "operator").title == "Delete obsolete.py"


def test_shell_title_preserves_command_without_execution(tmp_path: Path) -> None:
    target = tmp_path / "must-not-execute"
    command = f"touch {target}"
    entry = question(tmp_path, "Bash", {"command": command}, {})
    assert ReviewSummary.of(tmp_path, entry, "operator").title == f"Run: {command}"
    assert not target.exists()


def test_multiline_shell_title_counts_complete_script(tmp_path: Path) -> None:
    command = "echo first\necho second\necho third\n"
    entry = question(tmp_path, "Bash", {"command": command}, {})
    summary = ReviewSummary.of(tmp_path, entry, "operator")
    assert summary.title == "Run shell script (3 lines)"
    assert entry.operation.payload["command"] == command


def checkout_at(path: Path) -> Path:
    """A directory Git would call a checkout: a `.git` holding a HEAD."""
    (path / ".git").mkdir(parents=True)
    (path / ".git/HEAD").write_text("ref: refs/heads/feature\n")
    return path


def test_a_session_editing_another_checkout_is_titled_relative_to_that_checkout(
    tmp_path: Path,
) -> None:
    """The review sits in the session's queue; its title reads in the checkout it changes."""
    session = checkout_at(tmp_path / "dev")
    other = checkout_at(tmp_path / "feat-x")
    path = other / "src/module.py"
    entry = question(
        session, "Write", {"file_path": str(path), "content": "new\n"}, {path: None}
    )

    summary = ReviewSummary.of(session, entry, "operator")

    assert summary.title == "Create src/module.py"
    assert summary.paths == ["src/module.py"]
    assert summary.target == str(other)


def test_files_in_several_checkouts_keep_the_recorded_one(tmp_path: Path) -> None:
    session = checkout_at(tmp_path / "dev")
    first = checkout_at(tmp_path / "one") / "a.py"
    second = checkout_at(tmp_path / "two") / "b.py"
    patch = (
        "*** Begin Patch\n"
        f"*** Add File: {first}\n+a = 1\n*** Add File: {second}\n+b = 2\n"
        "*** End Patch\n"
    )
    entry = question(
        session, "apply_patch", {"command": patch}, dict.fromkeys([first, second])
    )

    summary = ReviewSummary.of(session, entry, "operator")

    assert summary.target == str(session)
    assert summary.paths == [str(first), str(second)]


def test_a_review_only_others_may_answer_names_the_command_they_answer_with(
    tmp_path: Path,
) -> None:
    entry = question(tmp_path, "Bash", {"command": "make"}, {}).model_copy(
        update={"eligible": ["operator-two"]}
    )

    summary = ReviewSummary.of(tmp_path, entry, "operator")

    assert not summary.answerable
    assert summary.unanswerable == (
        "Only operator-two may answer it: "
        f"`uv run --directory {tmp_path} lup-devtools review approve review-id --as "
        f"operator-two` or `uv run --directory {tmp_path} lup-devtools review "
        "decline review-id --as operator-two`, from a terminal outside every "
        "session."
    )


@pytest.mark.parametrize("restarting", [False, True])
def test_a_review_newer_code_parked_names_the_code_that_can_answer_it(
    tmp_path: Path, restarting: bool
) -> None:
    """The checkout keeping the queue parked it with its own code, which reads it."""
    entry = question(tmp_path, "Bash", {"command": "make"}, {}).model_copy(
        update={
            "resumption": "native_retry",
            "scheme": ["file_reviews", "unpreviewed", "segments", "later"],
        }
    )

    summary = ReviewSummary.of(tmp_path, entry, "operator", restarting=restarting)

    assert not summary.answerable
    assert summary.unanswerable.startswith("This dashboard runs older code")
    assert (
        "The code that parked it can answer it: "
        f"`uv run --directory {tmp_path} lup-devtools review approve review-id --as "
        "operator`"
    ) in summary.unanswerable
    assert (
        "The dashboard restarts onto its checkout's newer code shortly"
        in summary.unanswerable
    ) is restarting
