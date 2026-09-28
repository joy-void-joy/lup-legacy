"""A candidate cut, landed and promoted in a real repository.

The promise a promotion makes is that what ships is what was tested. The
candidate's commit is tagged with its own version, and the release is one
commit on a branch cut from that tag, changing nothing but the version, the
changelog section it closes, and the record of the breaks it carried —
checked against the candidate's tag before anything is tagged. The
integration branch goes on moving meanwhile, and gets the release back by a
merge. These drive the whole cycle — cut on the integration branch, land on
the release branch, promote — and read the result back from git rather than
from anything the release reported.
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
    (root / "thing.py").write_text("answer = 41\n", encoding="utf-8")
    committed(root, "chore: 0.4.0")
    git("-C", str(root), "tag", "-a", "v0.4.0", "-m", "0.4.0")
    git("-C", str(root), "checkout", "-q", "-b", "dev")
    declare(record_of(root).pending_directory(), "taken", "gone_before_rc1")
    committed(root, "feat: a break, declared")
    monkeypatch.chdir(root)
    return root


def record_of(root: Path) -> MigrationRecord:
    return MigrationRecord(root=root / "migrations")


def cut(
    root: Path, request: ReleaseRequest, regenerate: tuple[str, str] | None = None
) -> ReleasePlan:
    """What `dev release` does, regeneration writing ``(path, text)`` if given."""
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

    def regenerated() -> None:
        if regenerate is not None:
            (root / regenerate[0]).write_text(regenerate[1], encoding="utf-8")

    carry_out(plan, SPEC, root, log, record, regenerate=regenerated)
    return plan


def land(root: Path) -> None:
    """Merge the integration branch into `main`, the way a release lands."""
    git("-C", str(root), "checkout", "-q", "main")
    git("-C", str(root), "merge", "-q", "--no-ff", "--no-edit", "dev")
    git("-C", str(root), "checkout", "-q", "dev")


def open_an_entry(root: Path, entry: str) -> None:
    """Land a changelog entry above the candidates' section, as a fix would."""
    changelog = root / "CHANGELOG.md"
    changelog.write_text(
        changelog.read_text().replace(
            "# Changelog\n\n", f"# Changelog\n\n## Unreleased\n\n- {entry}\n\n", 1
        )
    )
    committed(root, f"docs(changelog): {entry}")


def commit_of(root: Path, ref: str) -> str:
    return git.out("-C", str(root), "rev-parse", f"{ref}^{{commit}}")


def subject_of(root: Path, ref: str) -> str:
    return git.out("-C", str(root), "log", "-1", "--format=%s", ref)


def manifest_at(root: Path, ref: str) -> str:
    return git.out("-C", str(root), "show", f"{ref}:pyproject.toml")


def test_a_candidate_carries_its_own_version_and_moves_no_pending_break(
    project: Path,
) -> None:
    """A project pinned to the candidate's tag is told it holds the candidate."""
    plan = cut(project, ReleaseRequest(level="minor", pre=True))

    assert plan.tag == "v0.5.0rc1"
    assert commit_of(project, "v0.5.0rc1") == commit_of(project, "HEAD")
    assert subject_of(project, "HEAD") == "release: 0.4.0 → 0.5.0rc1"
    assert 'version = "0.5.0rc1"' in manifest_at(project, "v0.5.0rc1")
    assert [m.subjects for m in record_of(project).pending()] == [["gone_before_rc1"]]
    assert record_of(project).releases() == []


def keep_working(root: Path) -> None:
    """Land what the integration branch goes on doing while a candidate soaks.

    Code, a break declared with it, and a changelog entry standing directly
    above the candidates' section — the line a promotion rewrites.
    """
    (root / "thing.py").write_text("answer = 42\n", encoding="utf-8")
    declare(record_of(root).pending_directory(), "later", "gone_after_rc1")
    committed(root, "feat: work after the candidate")
    open_an_entry(root, "after rc1")


def test_promotion_is_a_commit_on_the_candidate_that_changes_only_the_version(
    project: Path,
) -> None:
    """Version, changelog, record: nothing the candidate did not ship.

    The integration branch kept moving while the candidate soaked, and none of
    it is in the release: the promotion is cut from the candidate's tag.
    """
    cut(project, ReleaseRequest(level="minor", pre=True))
    land(project)
    keep_working(project)

    plan = cut(project, ReleaseRequest())

    assert plan.kind == "promotion"
    assert plan.branch == "release-0.5.0"
    assert commit_of(project, "v0.5.0") == commit_of(project, "release-0.5.0")
    assert commit_of(project, "v0.5.0^") == commit_of(project, "v0.5.0rc1")
    assert subject_of(project, "v0.5.0") == "release: 0.5.0rc1 → 0.5.0"
    assert 'version = "0.5.0"' in manifest_at(project, "v0.5.0")
    assert set(
        git.lines(
            "-C",
            str(project),
            "diff",
            "--name-only",
            "--no-renames",
            "v0.5.0rc1",
            "v0.5.0",
        )
    ) == {
        "CHANGELOG.md",
        "pyproject.toml",
        "migrations/pending/taken.toml",
        "migrations/0.5.0/taken.toml",
    }
    released = Changelog.parse(
        git.out("-C", str(project), "show", "v0.5.0:CHANGELOG.md")
    )
    assert released.candidate is None
    assert released.sections[0].text.startswith(release_heading("0.5.0", DAY))
    assert "after rc1" not in released.render()


def test_the_release_merged_back_keeps_what_the_branch_did_since(
    project: Path,
) -> None:
    """The closed section and the record come back; the newer work stays."""
    cut(project, ReleaseRequest(level="minor", pre=True))
    land(project)
    keep_working(project)
    before = commit_of(project, "HEAD")

    cut(project, ReleaseRequest())

    assert git.out("-C", str(project), "branch", "--show-current") == "dev"
    assert git.lines("-C", str(project), "rev-list", "--parents", "-n", "1", "HEAD")[
        0
    ].endswith(f"{before} {commit_of(project, 'v0.5.0')}")
    assert (project / "thing.py").read_text() == "answer = 42\n"
    assert 'version = "0.5.0"' in (project / "pyproject.toml").read_text()
    closed = Changelog.read(project / "CHANGELOG.md")
    assert closed.candidate is None
    assert closed.sections[0].text.startswith(release_heading("0.5.0", DAY))
    assert "- after rc1" in closed.unreleased
    assert [m.subjects for m in record_of(project).released("0.5.0")] == [
        ["gone_before_rc1"]
    ]
    assert [m.subjects for m in record_of(project).pending()] == [["gone_after_rc1"]]
    assert git.out("-C", str(project), "status", "--porcelain") == ""


def test_the_promotion_commit_is_checked_before_it_is_made(project: Path) -> None:
    """Whatever writes the tree, a change beyond the version is refused and undone."""
    cut(project, ReleaseRequest(level="minor", pre=True))
    land(project)
    head = commit_of(project, "HEAD")

    with pytest.raises(ReleaseRefused, match="thing.py"):
        cut(project, ReleaseRequest(), regenerate=("thing.py", "answer = 42\n"))

    assert git.out("-C", str(project), "branch", "--show-current") == "dev"
    assert commit_of(project, "HEAD") == head
    assert git.out("-C", str(project), "status", "--porcelain") == ""
    assert "release-0.5.0" not in git.out("-C", str(project), "branch", "--list")
    assert "v0.5.0" not in git.lines("-C", str(project), "tag", "--list")


def test_a_promotion_branch_already_standing_is_not_overwritten(project: Path) -> None:
    """A promotion started before is finished or deleted, never cut over."""
    cut(project, ReleaseRequest(level="minor", pre=True))
    land(project)
    git("-C", str(project), "branch", "release-0.5.0", "v0.5.0rc1")

    with pytest.raises(ReleaseRefused, match="release-0.5.0 already exists"):
        cut(project, ReleaseRequest())

    assert git.out("-C", str(project), "branch", "--show-current") == "dev"
    assert "v0.5.0" not in git.lines("-C", str(project), "tag", "--list")


def test_the_next_candidate_is_cut_from_what_landed_since(project: Path) -> None:
    cut(project, ReleaseRequest(level="minor", pre=True))
    land(project)
    (project / "thing.py").write_text("answer = 42\n", encoding="utf-8")
    committed(project, "fix: what rc1 got wrong")

    plan = cut(project, ReleaseRequest(pre=True))

    assert (plan.previous, plan.version) == ("0.5.0rc1", "0.5.0rc2")
    assert commit_of(project, "v0.5.0rc2") == commit_of(project, "HEAD")
    assert 'version = "0.5.0rc2"' in manifest_at(project, "v0.5.0rc2")
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
