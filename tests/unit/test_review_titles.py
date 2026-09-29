"""Queue titles describe captured actions without executing their commands."""

from pathlib import Path

import pytest

from lup.devtools.dev.questions import ReviewSummary
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
