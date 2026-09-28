"""What a break owes a project built on this one, and what the gate refuses.

The split these pin is the one the whole design rests on: a capability that
moved is derived from two trees and needs nobody to write it down, while one
that *went* needs a sentence somebody wrote — so the gate fails on the second
and says nothing about the first.
"""

from pathlib import Path

import pytest

from lup.devtools.dev.migrations import (
    Migration,
    MigrationRecord,
    MigrationStep,
    UnreadableMigration,
    rendered,
    unapplied,
    undeclared_breaks,
    unnamed,
)
from lup.devtools.dev.preservation import Capability
from lup.devtools.project import DevProject
from lup.execution.shell import git
from tests.unit.test_ledger_placement import committed, repository

GONE = Capability(identity="create_ledger_tools", location="lup.ledger.tools")

TAKEN = Migration(
    subjects=["create_ledger_tools"],
    reason="the verbs bind to a project's own kinds rather than to lup's",
    steps=[
        MigrationStep(
            instruction="Pass the kinds this project records, from its own declaration."
        )
    ],
)


def test_a_capability_no_migration_names_is_what_fails() -> None:
    """The gate's whole question, and the only thing it asks."""
    assert unnamed([GONE], []) == [GONE]
    assert unnamed([GONE], [TAKEN]) == []


def test_a_migration_speaks_for_every_name_one_decision_took() -> None:
    """One break takes several names, and reads as one record."""
    retired = Migration(
        subjects=["LibraryMode.LINKED", "link_library"],
        reason="the library moved with no command and nothing recorded",
        steps=[MigrationStep(instruction="Pin the branch carrying the change.")],
    )

    assert not unnamed(
        [
            Capability(identity="LibraryMode.LINKED", location="lup.x"),
            Capability(identity="link_library", location="lup.x"),
        ],
        [retired],
    )


def test_a_declaration_naming_no_commit_is_pending_everywhere(tmp_path: Path) -> None:
    """A commit being written cannot name itself, and its break is still owed."""
    root = repository(tmp_path / "upstream")
    (root / "one.py").write_text("x = 1\n", encoding="utf-8")
    committed(root, "one")
    standing = git.out("-C", str(root), "rev-parse", "HEAD")

    assert unapplied([TAKEN], standing, root) == [TAKEN]


def test_a_project_past_the_break_is_told_nothing(tmp_path: Path) -> None:
    """Ancestry decides it, so a branch that never carried the commit still owes it."""
    root = repository(tmp_path / "upstream")
    (root / "one.py").write_text("x = 1\n", encoding="utf-8")
    committed(root, "one")
    broke = git.out("-C", str(root), "rev-parse", "HEAD")
    (root / "two.py").write_text("y = 2\n", encoding="utf-8")
    committed(root, "two")
    after = git.out("-C", str(root), "rev-parse", "HEAD")
    landed = TAKEN.model_copy(update={"commit": broke})

    assert unapplied([landed], after, root) == []
    assert unapplied([landed], broke, root) == []
    assert landed.applied_at(after, root)


def test_what_a_release_carries_is_the_prose_and_the_steps() -> None:
    """The derived half is left out: a map re-derives, a sentence does not."""
    lines = rendered([TAKEN])

    assert lines[0].startswith("create_ledger_tools — the verbs bind")
    assert "Pass the kinds this project records" in lines[1]


def test_a_step_carries_the_command_that_does_it_where_one_does() -> None:
    step = MigrationStep(
        instruction="Pin the branch carrying the change.",
        command=["uv", "run", "lup-devtools", "dev", "library", "git"],
    )

    assert step.spelled().endswith("uv run lup-devtools dev library git")


def test_every_declaration_this_library_holds_says_what_a_caller_does_about_it() -> (
    None
):
    """The property that holds of the record whatever is in it.

    Asserting *which* files are pending cannot survive a release, because
    moving them is what a release does. What is worth pinning is true of every
    file, pending or released: it parses, and the break it declares carries
    steps a caller can act on.
    """
    record = MigrationRecord()

    assert record.releases(), "a release's migrations stay in the record"
    for migration in record.declared():
        assert migration.subjects
        assert migration.steps, f"{migration.subjects} declares no step to take"
        assert migration.reason
        assert all(step.instruction for step in migration.steps)


def declare(directory: Path, name: str, subject: str) -> Path:
    """One migration file, written the way an author writes one."""
    path = directory / f"{name}.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f'subjects = ["{subject}"]\n'
        f'reason = """\n{subject} went, and a caller has to know why."""\n'
        "\n[[steps]]\n"
        'instruction = "Call what replaced it."\n',
        encoding="utf-8",
    )
    return path


def test_two_branches_declaring_a_break_each_merge_without_a_conflict(
    tmp_path: Path,
) -> None:
    """A file per break is what lets any number of branches declare one at once.

    One list every branch appended to was one position every pair of them
    conflicted at, so the record is a directory and a break is a file in it.
    """
    root = repository(tmp_path / "upstream")
    record = MigrationRecord(root=root / "migrations")
    declare(record.pending_directory(), "first", "first_gone")
    committed(root, "base")
    git("-C", str(root), "checkout", "-q", "-b", "topic")
    declare(record.pending_directory(), "topic", "topic_gone")
    committed(root, "topic declares a break")
    git("-C", str(root), "checkout", "-q", "main")
    declare(record.pending_directory(), "mainline", "main_gone")
    committed(root, "main declares a break")

    git("-C", str(root), "merge", "-q", "--no-edit", "topic")

    assert sorted(
        subject for migration in record.pending() for subject in migration.subjects
    ) == ["first_gone", "main_gone", "topic_gone"]


def test_a_release_moves_what_is_pending_into_its_version(tmp_path: Path) -> None:
    """Released entries stay structured, stamped with the commit that declared them.

    The stamp is what tells a project already past the break that it has it:
    a pending file cannot name the commit that adds it, and the release reads
    that commit off the history instead.
    """
    root = repository(tmp_path / "upstream")
    record = MigrationRecord(root=root / "migrations")
    (root / "one.py").write_text("x = 1\n", encoding="utf-8")
    committed(root, "before the break")
    declare(record.pending_directory(), "taken", "create_ledger_tools")
    committed(root, "the break")
    broke = git.out("-C", str(root), "rev-parse", "HEAD").strip()

    moved = record.release("0.2.0", root)

    assert record.pending() == []
    assert record.releases() == ["0.2.0"]
    assert moved == record.released("0.2.0")
    assert [migration.subjects for migration in moved] == [["create_ledger_tools"]]
    assert moved[0].commit == broke
    assert unapplied(record.declared(), broke, root) == []


def test_releases_are_read_in_version_order_and_pending_last(tmp_path: Path) -> None:
    """Oldest first, as an update crossing several of them applies them."""
    record = MigrationRecord(root=tmp_path)
    for version, subject in [("0.10.0", "later"), ("0.9.0", "earlier")]:
        declare(tmp_path / version, subject, subject)
    declare(record.pending_directory(), "unreleased", "unreleased")

    assert record.releases() == ["0.9.0", "0.10.0"]
    assert [migration.subjects[0] for migration in record.declared()] == [
        "earlier",
        "later",
        "unreleased",
    ]


def test_a_file_that_does_not_validate_is_refused_by_its_path(tmp_path: Path) -> None:
    """A misspelled key is refused rather than dropped, naming the file."""
    record = MigrationRecord(root=tmp_path)
    record.pending_directory().mkdir()
    (record.pending_directory() / "typo.toml").write_text(
        'subject = ["gone"]\nreason = "r"\nsteps = []\n', encoding="utf-8"
    )

    with pytest.raises(UnreadableMigration, match="typo.toml"):
        record.pending()


def test_a_directory_that_is_neither_pending_nor_a_release_is_refused(
    tmp_path: Path,
) -> None:
    record = MigrationRecord(root=tmp_path)
    (tmp_path / "next").mkdir()

    with pytest.raises(UnreadableMigration, match="next"):
        record.releases()


def test_a_record_with_nothing_declared_reads_as_empty(tmp_path: Path) -> None:
    """Between a release and the next break the pending directory is not there."""
    record = MigrationRecord(root=tmp_path / "migrations")

    assert record.pending() == []
    assert record.declared() == []


VENDORING_MANIFEST = """\
[project]
name = "app"
version = "0.1.0"
dependencies = ["lup-agents"]

[tool.uv.workspace]
members = ["packages/*"]

[tool.uv.sources]
lup-agents = { workspace = true }
"""
"""A project resolving lup from the copy under ``packages/lup``."""

GIT_MANIFEST = """\
[project]
name = "app"
version = "0.1.0"
dependencies = ["lup-agents"]

[tool.uv.sources]
lup-agents = { git = "https://github.com/joy-void-joy/lup", branch = "main", subdirectory = "packages/lup" }
"""
"""The same project after `dev library git`: lup is a dependency, not a tree."""


def vendoring_checkout(root: Path) -> str:
    """A project vendoring lup beside a package of its own, committed.

    Returns the commit, which is the base the gate judges the range from.
    """
    repository(root)
    (root / "pyproject.toml").write_text(VENDORING_MANIFEST, encoding="utf-8")
    library = root / "packages/lup/src/lup"
    library.mkdir(parents=True)
    (library / "client.py").write_text("class Client: ...\n", encoding="utf-8")
    (library / "session.py").write_text(
        "def open_session() -> None: ...\n", encoding="utf-8"
    )
    (root / "src/app").mkdir(parents=True)
    (root / "src/app/core.py").write_text(
        "def run() -> None: ...\n\n\ndef stop() -> None: ...\n", encoding="utf-8"
    )
    committed(root, "vendored")
    return git.out("-C", str(root), "rev-parse", "HEAD")


def test_resolving_the_library_from_a_dependency_takes_nothing_from_anybody(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`dev library git` removes the vendored copy, and every name still imports.

    What an adopter met right after switching: the base's vendored `lup`
    read against a working tree holding none of it, and the gate failing on
    every name the library declares. The project's own package is judged
    across the same range all the same, so a name it really dropped is the
    one thing reported.
    """
    root = tmp_path / "project"
    base = vendoring_checkout(root)
    monkeypatch.chdir(root)
    (root / "pyproject.toml").write_text(GIT_MANIFEST, encoding="utf-8")
    git("rm", "-r", "--quiet", "packages/lup")
    (root / "src/app/core.py").write_text("def run() -> None: ...\n", encoding="utf-8")

    owed = undeclared_breaks(
        DevProject(package="app"), base, MigrationRecord(root=tmp_path / "none")
    )

    assert [capability.identity for capability in owed] == ["stop"]


def test_a_library_this_checkout_vendors_is_held_to_what_it_offered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Where the library's source is the checkout's own, a name it drops is a break.

    lup's own repository is this case, and the one the gate exists for.
    """
    root = tmp_path / "project"
    base = vendoring_checkout(root)
    monkeypatch.chdir(root)
    (root / "packages/lup/src/lup/session.py").write_text("\n", encoding="utf-8")

    owed = undeclared_breaks(
        DevProject(package="app"), base, MigrationRecord(root=tmp_path / "none")
    )

    assert [capability.identity for capability in owed] == ["open_session"]


def test_a_break_a_release_declared_is_read_across_a_range_spanning_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The release moved the declaration, and the range still finds it.

    Judged from a base older than the release, the name is gone and the
    release's record speaks for it. Judged from a base the release already
    reached, the same record is in force there, so it speaks for nothing that
    base still had: a name that came back and went again is a break of its own.
    """
    root = tmp_path / "project"
    base = vendoring_checkout(root)
    monkeypatch.chdir(root)
    record = MigrationRecord(root=root / "migrations")
    (root / "src/app/core.py").write_text("def run() -> None: ...\n", encoding="utf-8")
    declare(record.pending_directory(), "stop", "stop")
    committed(root, "stop goes, and says so")
    record.release("0.2.0", root)
    committed(root, "release: 0.1.0 → 0.2.0")

    assert undeclared_breaks(DevProject(package="app"), base, record) == []

    (root / "src/app/core.py").write_text(
        "def run() -> None: ...\n\n\ndef stop() -> None: ...\n", encoding="utf-8"
    )
    committed(root, "stop comes back")
    returned = git.out("-C", str(root), "rev-parse", "HEAD").strip()
    (root / "src/app/core.py").write_text("def run() -> None: ...\n", encoding="utf-8")

    owed = undeclared_breaks(DevProject(package="app"), returned, record)

    assert [capability.identity for capability in owed] == ["stop"]
