"""A candidate cut, landed and promoted in a real repository.

The promise a promotion makes is about commits, not about files: the final
version is the candidate's own commit under a second tag, so what was tested
is what ships and nothing is rebuilt in between. These drive the whole cycle
— cut on the integration branch, land on the release branch, promote — and
read the tags back from git rather than from anything the release reported.
"""

import datetime as dt
from pathlib import Path

import pytest

from lup.devtools.changelog import Changelog, release_heading
from lup.devtools.dev.migrations import MigrationRecord, rendered
from lup.devtools.dev.release import (
    PendingBreaks,
    ReleasePlan,
    ReleaseRefused,
    ReleaseRequest,
    ReleaseSpec,
    carry_out,
    read_state,
)
from lup.execution.shell import git
from tests.unit.test_ledger_placement import committed, repository
from tests.unit.test_migrations import declare

DAY = dt.date(2026, 9, 28)

SPEC = ReleaseSpec()

CHANGELOG = """# Changelog

## Unreleased

- a feature

## 0.4.0 — 2026-09-22

- older
"""


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """0.4.0 released on `main`, `dev` checked out with one pending break."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", "/dev/null")
    root = repository(tmp_path / "project")
    (root / "pyproject.toml").write_text(
        '[project]\nname = "thing"\nversion = "0.4.0"\n', encoding="utf-8"
    )
    (root / "CHANGELOG.md").write_text(CHANGELOG, encoding="utf-8")
    committed(root, "chore: 0.4.0")
    git("-C", str(root), "tag", "-a", "v0.4.0", "-m", "0.4.0")
    git("-C", str(root), "checkout", "-q", "-b", "dev")
    declare(record_of(root).pending_directory(), "taken", "gone_before_rc1")
    committed(root, "feat: a break, declared")
    monkeypatch.chdir(root)
    return root


def record_of(root: Path) -> MigrationRecord:
    return MigrationRecord(root=root / "migrations")


def cut(root: Path, request: ReleaseRequest) -> ReleasePlan:
    """What `dev release` does, with regeneration standing still."""
    record = record_of(root)
    pending = record.pending()
    log = Changelog.read(root / SPEC.changelog)
    plan = read_state(SPEC, root, record.pending_directory()).planned(
        request,
        DAY,
        log,
        PendingBreaks(lines=rendered(pending), count=len(pending)),
        SPEC,
    )
    carry_out(plan, SPEC, root, log, record, regenerate=lambda: None)
    return plan


def land(root: Path) -> None:
    """Merge the integration branch into `main`, the way a release lands."""
    git("-C", str(root), "checkout", "-q", "main")
    git("-C", str(root), "merge", "-q", "--no-ff", "--no-edit", "dev")
    git("-C", str(root), "checkout", "-q", "dev")


def commit_of(root: Path, ref: str) -> str:
    return git.out("-C", str(root), "rev-parse", f"{ref}^{{commit}}")


def subject_of(root: Path, ref: str) -> str:
    return git.out("-C", str(root), "log", "-1", "--format=%s", ref)


def test_a_candidate_is_tagged_as_one_and_moves_no_pending_break(project: Path) -> None:
    plan = cut(project, ReleaseRequest(level="minor", pre=True))

    assert plan.tag == "v0.5.0rc1"
    assert commit_of(project, "v0.5.0rc1") == commit_of(project, "HEAD")
    assert subject_of(project, "HEAD") == "release: 0.4.0 → 0.5.0rc1"
    # The manifest names where the series is heading; the tag names the
    # candidate, and publishing takes the version from the tag.
    assert 'version = "0.5.0"' in (project / "pyproject.toml").read_text()
    # A candidate's breaks belong to the version it is heading for.
    assert [m.subjects for m in record_of(project).pending()] == [["gone_before_rc1"]]
    assert record_of(project).releases() == []


def test_promotion_tags_the_candidate_commit_itself(project: Path) -> None:
    """Same commit, second tag — and only what the candidate held is released."""
    cut(project, ReleaseRequest(level="minor", pre=True))
    land(project)
    changelog = project / "CHANGELOG.md"
    changelog.write_text(
        changelog.read_text().replace(
            "# Changelog\n\n", "# Changelog\n\n## Unreleased\n\n- after rc1\n\n", 1
        )
    )
    declare(record_of(project).pending_directory(), "later", "gone_after_rc1")
    committed(project, "feat: work after the candidate")

    plan = cut(project, ReleaseRequest())

    assert plan.kind == "promotion"
    assert commit_of(project, "v0.5.0") == commit_of(project, "v0.5.0rc1")
    assert not subject_of(project, "HEAD").startswith("release: ")
    closed = Changelog.read(changelog)
    assert closed.candidate is None
    assert closed.sections[0].text.startswith(release_heading("0.5.0", DAY))
    assert "- a feature" in closed.sections[0].text
    assert "- after rc1" in closed.unreleased
    assert [m.subjects for m in record_of(project).released("0.5.0")] == [
        ["gone_before_rc1"]
    ]
    assert [m.subjects for m in record_of(project).pending()] == [["gone_after_rc1"]]


def test_the_next_candidate_is_cut_from_what_landed_since(project: Path) -> None:
    cut(project, ReleaseRequest(level="minor", pre=True))
    land(project)
    committed_fix = project / "fix.txt"
    committed_fix.write_text("fixed\n", encoding="utf-8")
    committed(project, "fix: what rc1 got wrong")

    plan = cut(project, ReleaseRequest(pre=True))

    assert (plan.previous, plan.version) == ("0.5.0rc1", "0.5.0rc2")
    assert commit_of(project, "v0.5.0rc2") == commit_of(project, "HEAD")
    log = Changelog.read(project / "CHANGELOG.md")
    assert log.candidate is not None
    assert [c.version for c in log.candidate.candidates] == ["0.5.0rc1", "0.5.0rc2"]


def test_a_moved_release_branch_is_read_as_moved(project: Path) -> None:
    cut(project, ReleaseRequest(level="minor", pre=True))
    land(project)
    git("-C", str(project), "checkout", "-q", "main")
    (project / "hotfix.txt").write_text("hot\n", encoding="utf-8")
    committed(project, "fix: straight onto main")
    git("-C", str(project), "checkout", "-q", "dev")

    with pytest.raises(ReleaseRefused, match="moved since 0.5.0rc1"):
        cut(project, ReleaseRequest())

    assert "v0.5.0" not in git.lines("-C", str(project), "tag", "--list")


def test_a_candidate_not_yet_landed_is_not_promoted(project: Path) -> None:
    cut(project, ReleaseRequest(level="minor", pre=True))

    with pytest.raises(ReleaseRefused, match="land it"):
        cut(project, ReleaseRequest())


def test_planning_writes_nothing(project: Path) -> None:
    """A dry run is planning alone: no tag, no commit, no file moved."""
    head = commit_of(project, "HEAD")
    record = record_of(project)

    plan = read_state(SPEC, project, record.pending_directory()).planned(
        ReleaseRequest(level="minor", pre=True),
        DAY,
        Changelog.read(project / "CHANGELOG.md"),
        PendingBreaks(),
        SPEC,
    )

    assert plan.version == "0.5.0rc1"
    assert commit_of(project, "HEAD") == head
    assert git.lines("-C", str(project), "tag", "--list") == ["v0.4.0"]
    assert git.out("-C", str(project), "status", "--porcelain") == ""
