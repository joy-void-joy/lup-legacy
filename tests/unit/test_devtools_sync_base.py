"""Behavior tests for `lup-devtools dev pr sync-base`.

A base only topology could name is reported and not merged. Guessing wrong picks
a branch the feature never diverged from, and every later rebase step reads that
answer as settled, so the merge waits for a base the caller passed or worktree
creation recorded.
"""

import json
from pathlib import Path

import pytest
import sh
import typer

from lup.devtools.dev import pr, records
from tests.unit.repos import commit_file, initialized_repo


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    work = tmp_path / "repo"
    git = initialized_repo(work, tmp_path / "no-hooks")
    commit_file(git, work, "file.txt", "base\n", "chore: base")

    git("switch", "-c", "dev")
    (work / "dev.txt").write_text("dev\n", encoding="utf-8")
    git("add", "dev.txt")
    git("commit", "-m", "feat: dev only")

    git("switch", "main")
    git("switch", "-c", "feature")
    (work / "feature.txt").write_text("feature\n", encoding="utf-8")
    git("add", "feature.txt")
    git("commit", "-m", "feat: feature only")
    return work


def emitted(capsys: pytest.CaptureFixture[str]) -> pr.SyncBaseResult:
    return pr.SyncBaseResult.model_validate(json.loads(capsys.readouterr().out))


def test_a_guessed_base_is_reported_and_not_merged(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(repo)
    with pytest.raises(typer.Exit):
        pr.sync_base(None, as_json=True)

    result = emitted(capsys)
    assert result.base_source == "guessed"
    assert result.merged is False
    assert result.conflicts == []


def test_a_guessed_base_leaves_the_branch_untouched(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(repo)
    git = sh.Command("git").bake("-C", str(repo), _tty_out=False)
    before = str(git("rev-parse", "HEAD")).strip()

    with pytest.raises(typer.Exit):
        pr.sync_base(None, as_json=True)

    assert str(git("rev-parse", "HEAD")).strip() == before
    assert not (repo / "dev.txt").exists()


def test_an_explicit_base_merges(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(repo)
    pr.sync_base("dev", as_json=True)

    result = emitted(capsys)
    assert result.base_source == "explicit"
    assert result.merged is True
    assert (repo / "dev.txt").exists()


def test_a_recorded_base_merges(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(repo)
    records.remember("feature", records.BranchRecord(base="dev"), repo)
    pr.sync_base(None, as_json=True)

    result = emitted(capsys)
    assert result.base_source == "recorded"
    assert result.merged is True
    assert (repo / "dev.txt").exists()
