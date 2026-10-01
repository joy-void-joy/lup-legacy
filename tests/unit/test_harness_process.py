"""Behavioral tests for the local process launcher's capture contract."""

from pathlib import Path

from lup.execution.process import LaunchRequest, LocalProcessLauncher
from lup.devtools.harness.resolve import resolver_source_snapshot


def test_captured_stdout_is_not_a_terminal(tmp_path: Path) -> None:
    status = LocalProcessLauncher().launch(
        LaunchRequest(arguments=["test", "-t", "1"], cwd=tmp_path)
    )
    assert status.code == 1


def test_git_output_stays_plain_under_pager_environments(tmp_path: Path) -> None:
    launcher = LocalProcessLauncher()

    def git(*arguments: str) -> None:
        # Identity per invocation, never `git config` — see `lup.devtools.gitguard`.
        status = launcher.launch(
            LaunchRequest(
                arguments=[
                    "git",
                    "-c",
                    "user.email=launcher@example.test",
                    "-c",
                    "user.name=Launcher Test",
                    *arguments,
                ],
                cwd=tmp_path,
            )
        )
        assert status.code == 0, status.stderr

    git("init", "-b", "main")
    (tmp_path / "tracked.txt").write_text("base\n", encoding="utf-8")
    git("add", "tracked.txt")
    git("commit", "-m", "base")
    (tmp_path / "tracked.txt").write_text("changed\n", encoding="utf-8")
    named = launcher.launch(
        LaunchRequest(
            arguments=["git", "diff", "--name-only", "HEAD"],
            cwd=tmp_path,
            environment={"GIT_PAGER": "less", "LESS": ""},
        )
    )
    assert named.code == 0
    assert "\x1b" not in named.stdout
    assert named.stdout.split() == ["tracked.txt"]


def test_resolver_snapshot_includes_notes_without_mutating_the_checkout(
    tmp_path: Path,
) -> None:
    launcher = LocalProcessLauncher()

    def git(*arguments: str) -> str:
        # Identity per invocation, never `git config` — see `lup.devtools.gitguard`.
        status = launcher.launch(
            LaunchRequest(
                arguments=[
                    "git",
                    "-c",
                    "user.email=snapshot@example.test",
                    "-c",
                    "user.name=Snapshot Test",
                    *arguments,
                ],
                cwd=tmp_path,
            )
        )
        assert status.code == 0, status.stderr
        return status.stdout.strip()

    git("init", "-b", "feature")
    note = tmp_path / "tracked.py"
    note.write_text("value = 1\n", encoding="utf-8")
    git("add", "tracked.py")
    git("commit", "-m", "base")
    head = git("rev-parse", "HEAD")
    note.write_text("value = 1  # lup: use a domain type\n", encoding="utf-8")
    status = git("status", "--short")

    snapshot = resolver_source_snapshot(
        launcher,
        tmp_path,
        tmp_path / ".state",
        [Path("tracked.py")],
    )

    assert snapshot.branch == "feature"
    assert snapshot.commit != head
    assert git("rev-parse", "HEAD") == head
    assert git("status", "--short") == status
    assert "# lup: use a domain type" in git("show", f"{snapshot.commit}:tracked.py")
    assert not (tmp_path / ".state" / ".source.index").exists()
