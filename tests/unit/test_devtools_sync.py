"""Registry resolution and reachability tests for `lup-devtools sync`.

The sync registry contract: sync.json(.local) is the canonical pair, split by
whose fact each key is. The tracked half says which repository a name means,
whether this project can work without it, and what a session may reach of it;
the local half says where this machine keeps it and which transport gets
there. A registration says a project may be *reviewed*; what says it may be
*opened* is a `mount`, which is a separate claim and is never defaulted.
"""

import json
from pathlib import Path

import pytest
import sh
import typer

from lup.devtools import sync
from lup.diagnostics import Refusal
from lup.execution.git import Repository
from tests.unit.repos import commit_file, git_in, initialized_repo


def tracked(registry_root: Path, *projects: dict) -> None:
    """Write these entries as the registrations every machine shares."""
    (registry_root / "sync.json").write_text(
        json.dumps({"projects": list(projects)}) + "\n"
    )


@pytest.fixture
def registry_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(sync, "project_root", lambda: tmp_path)
    return tmp_path


def registered(registry_root: Path, *projects: dict) -> None:
    """Write these entries as this project's personal registrations."""
    (registry_root / "sync.json.local").write_text(
        json.dumps({"projects": list(projects)})
    )


def test_a_registration_is_not_reachable_until_it_says_so(
    registry_root: Path, tmp_path: Path
) -> None:
    """The whole of the opt-in: tracked is not the same claim as open."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    registered(registry_root, {"name": "other", "path": str(elsewhere)})

    assert sync.accessible_roots(lambda said: None) == []


def test_a_declared_mount_reaches_the_lease_at_the_mode_it_names(
    registry_root: Path, tmp_path: Path
) -> None:
    open_wide = tmp_path / "open"
    read_only = tmp_path / "readable"
    open_wide.mkdir()
    read_only.mkdir()
    registered(
        registry_root,
        {"name": "open", "path": str(open_wide), "mount": "rw"},
        {"name": "readable", "path": str(read_only), "mount": "ro"},
    )

    roots = sync.accessible_roots(lambda said: None)

    assert [(root.path, root.writable) for root in roots] == [
        (open_wide, True),
        (read_only, False),
    ]


def test_a_mount_nobody_can_locate_is_reported_rather_than_raised(
    registry_root: Path,
) -> None:
    """A launch does not fail over an unfinished note in a gitignored file."""
    registered(registry_root, {"name": "gone", "mount": "rw"})
    said: list[str] = []

    assert sync.accessible_roots(said.append) == []
    assert "gone" in "\n".join(said)


def test_the_scaffolds_own_shipped_mount_is_not_owed_a_checkout_here(
    registry_root: Path,
) -> None:
    """The entry an adopter mounts names, in the scaffold, this repository.

    `sync.json` ships the lup registration required and mounted read-write,
    so every repository built from the scaffold opens the checkout its
    workflows read. The scaffold is the one repository that is it: a launch
    here mounts nothing for it and says nothing about it, rather than
    reporting the requirement with the command that would clone a copy of
    the tree the session is already standing in.
    """
    (registry_root / "pyproject.toml").write_text(
        "[tool.lup]\ntemplate = true\n", encoding="utf-8"
    )
    tracked(registry_root, {"name": "lup", "required": True, "mount": "rw"})
    said: list[str] = []

    assert sync.accessible_roots(said.append) == []
    assert said == []


def test_a_misspelled_mode_is_refused_by_the_registry_rather_than_ignored(
    registry_root: Path, tmp_path: Path
) -> None:
    """The reason the key is a literal: a typo is an error, not silence."""
    registered(registry_root, {"name": "other", "path": str(tmp_path), "mount": "rwx"})

    with pytest.raises(Exception):
        sync.load_projects()


def test_missing_registries_resolve_to_sync_names(registry_root: Path) -> None:
    assert sync.sync_file() == registry_root / "sync.json"
    assert sync.local_file() == registry_root / "sync.json.local"
    assert sync.load_projects() == []


def test_local_entries_override_tracked_entries_by_name(registry_root: Path) -> None:
    (registry_root / "sync.json").write_text(
        json.dumps({"projects": [{"name": "lup", "url": "https://example.test/lup"}]})
    )
    (registry_root / "sync.json.local").write_text(
        json.dumps({"projects": [{"name": "lup", "ignore": True}]})
    )

    projects = sync.load_projects()

    assert projects == [
        {"name": "lup", "url": "https://example.test/lup", "ignore": True}
    ]


@pytest.fixture
def upstream(tmp_path: Path) -> tuple[str, list[str]]:
    """An upstream checkout with three commits, newest last."""
    work = tmp_path / "upstream"
    git = initialized_repo(work, tmp_path / "hooks")
    for index in range(3):
        commit_file(git, work, "file.txt", f"revision {index}\n", f"commit {index}")
    log = git("log", "--format=%H", "--reverse").strip().splitlines()
    return str(work), [line.strip() for line in log]


def test_a_checkpoint_defaults_to_the_upstream_head(
    upstream: tuple[str, list[str]],
) -> None:
    """What a finished review means: everything up to now was considered."""
    path, commits = upstream

    assert sync.resolved_checkpoint(path, "") == commits[-1]


def test_a_checkpoint_can_record_a_commit_already_consumed(
    upstream: tuple[str, list[str]],
) -> None:
    """A project adopting a library mid-stream knows which commit it took.

    Without this the only reachable checkpoint is the upstream's HEAD, which
    silently claims every commit landed since as reviewed — the opposite of
    what the record is for.
    """
    path, commits = upstream

    assert sync.resolved_checkpoint(path, commits[0]) == commits[0]
    assert sync.resolved_checkpoint(path, commits[0][:8]) == commits[0]


def test_a_checkpoint_that_names_no_commit_is_refused_rather_than_recorded(
    upstream: tuple[str, list[str]],
) -> None:
    """Refused where the caller can still fix it.

    A checkpoint nothing can resolve is one no later range can be computed
    from, and it fails at the next review rather than at the typo.
    """
    path, _commits = upstream

    with pytest.raises(Refusal):
        sync.resolved_checkpoint(path, "no-such-ref")


@pytest.fixture
def cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The per-user clone cache, moved somewhere a test may write."""
    root = tmp_path / "cache"
    monkeypatch.setattr(sync, "cache_dir", lambda: root)
    return root


@pytest.fixture
def remote(tmp_path: Path) -> Path:
    """A repository to clone from, carrying a history and a second branch."""
    work = tmp_path / "remote"
    git = initialized_repo(work, tmp_path / "hooks")
    for index in range(3):
        commit_file(git, work, "file.txt", f"revision {index}\n", f"commit {index}")
    git("branch", "sidecar")
    return work


def materialize(name: str = "up") -> sync.Upstream:
    """Locate one registration, silencing the progress a test does not read."""
    return sync.ensure_local(sync.find_project(name), lambda said: None)


def test_a_url_registration_materializes_as_a_bare_repository_with_a_worktree(
    registry_root: Path, cache: Path, remote: Path
) -> None:
    """The layout a registration naming a local path already points at.

    Parity is the whole subject: a session opens either kind on the same
    terms, so a URL cannot resolve to something a session cannot work in.
    """
    registered(registry_root, {"name": "up", "url": str(remote)})

    found = materialize()

    assert sync.bare_repository(cache / "up.git")
    assert found.checkout == cache / "up.git" / "tree" / "main"
    assert (found.checkout / "file.txt").read_text() == "revision 2\n"


def test_a_materialized_clone_carries_the_whole_history_and_every_branch(
    registry_root: Path, cache: Path, remote: Path
) -> None:
    """What ``--depth=200`` could not give a session to work in.

    A shallow single-branch clone is a review window that quietly ends and a
    checkout that can be cut no other branch, which is two of the three
    reasons a URL registration would be worth less than a path one.
    """
    registered(registry_root, {"name": "up", "url": str(remote)})
    materialize()
    bare = str(cache / "up.git")

    assert Repository(Path(bare)).answer("rev-list", "--count", "main") == "3"
    assert (
        Repository(Path(bare)).answer("rev-parse", "--is-shallow-repository") == "false"
    )
    assert Repository(Path(bare)).answer(
        "branch", "--format=%(refname:short)"
    ).split() == [
        "main",
        "sidecar",
    ]


def test_refreshing_a_clone_leaves_work_in_it_exactly_where_it_stands(
    registry_root: Path, cache: Path, remote: Path, tmp_path: Path
) -> None:
    """The third reason, and the one that lost work rather than opportunity.

    A refresh made of a fetch and a hard reset onto the upstream takes a
    branch cut in the clone, a commit made on it and every uncommitted file
    beside it with the next review — silently, because a reset says nothing
    about what it wrote over.
    """
    registered(registry_root, {"name": "up", "url": str(remote)})
    checkout = materialize().checkout
    worker = git_in(checkout, tmp_path / "hooks")
    worker("checkout", "-b", "feature")
    commit_file(worker, checkout, "mine.txt", "kept\n", "work in progress")
    standing = worker("rev-parse", "HEAD").strip()
    (checkout / "dirty.txt").write_text("uncommitted\n")

    commit_file(
        git_in(remote, tmp_path / "hooks"), remote, "file.txt", "later\n", "commit 3"
    )
    again = materialize()

    assert worker("rev-parse", "HEAD").strip() == standing
    assert worker("branch", "--show-current").strip() == "feature"
    assert (checkout / "mine.txt").read_text() == "kept\n"
    assert (checkout / "dirty.txt").read_text() == "uncommitted\n"
    assert sync.commit_count(str(again.checkout), "", again.tip) == 4


def test_a_bare_path_registration_is_read_at_the_branch_it_registered(
    registry_root: Path, cache: Path, remote: Path, tmp_path: Path
) -> None:
    """A bare repository's HEAD is nobody's checkout, so it is not the tip.

    Read off HEAD, a registration naming a bare clone with a worktree attached
    reports 0 behind a branch 335 commits on, because HEAD points at the branch
    the clone was made with and nothing that happens in an attached worktree
    moves it.
    """
    registered(registry_root, {"name": "up", "url": str(remote)})
    materialize()
    bare = cache / "up.git"
    sidecar = bare / "tree" / "sidecar"
    Repository(Path(str(bare))).answer("worktree", "add", str(sidecar), "sidecar")
    commit_file(
        git_in(sidecar, tmp_path / "hooks"), sidecar, "side.txt", "on\n", "sidecar work"
    )
    main = Repository(Path(str(bare))).answer("rev-parse", "main")
    registered(
        registry_root,
        {
            "name": "own",
            "path": str(bare),
            "branch": "sidecar",
            "review_from": "local",
        },
    )

    found = sync.existing_upstream(sync.find_project("own"))

    assert found is not None
    assert found.checkout == sidecar
    assert found.tip == "refs/heads/sidecar"
    assert sync.commit_count(str(found.checkout), main, found.tip) == 1


def test_work_done_in_a_clone_is_not_read_back_as_the_upstream_s_own(
    registry_root: Path, cache: Path, remote: Path, tmp_path: Path
) -> None:
    """The other half of leaving the branch alone.

    A review that read ``HEAD`` in a checkout sessions work in would hand
    ``/lup:update`` this project's own commits to consider porting from
    itself. Reading the remote-tracking ref is what makes both halves true at
    once.
    """
    registered(registry_root, {"name": "up", "url": str(remote)})
    checkout = materialize().checkout
    worker = git_in(checkout, tmp_path / "hooks")
    commit_file(worker, checkout, "mine.txt", "mine\n", "not the upstream's")

    found = materialize()

    assert sync.commit_count(str(found.checkout), "", found.tip) == 3


def test_a_url_registration_is_mounted_whole_while_refs_names_its_worktree(
    registry_root: Path, cache: Path, remote: Path
) -> None:
    """The clone this machine materialized, bare half and every worktree.

    The lease is what a worktree mount gets; the edit authority is not: a
    bare mount accepts every worktree's own policy, and one cut after the
    launch once the operator refreshes it. `refs/<name>` stays a working
    tree, because that is what the workflows run a project's tooling in.
    """
    registered(registry_root, {"name": "up", "url": str(remote), "mount": "rw"})

    roots = sync.accessible_roots(lambda said: None)

    assert [(root.path, root.writable) for root in roots] == [(cache / "up.git", True)]
    assert (registry_root / "refs" / "up").resolve() == (
        cache / "up.git" / "tree" / "main"
    )


def test_a_clone_registered_under_one_name_at_two_urls_is_refused(
    registry_root: Path, cache: Path, remote: Path, tmp_path: Path
) -> None:
    """The cache is per user and keyed by the registered name.

    Two projects on this machine naming different repositories ``lup`` would
    otherwise share one clone, and the second would review, mount and commit
    into the first one's history under its own name.
    """
    registered(registry_root, {"name": "up", "url": str(remote)})
    materialize()
    registered(registry_root, {"name": "up", "url": str(tmp_path / "elsewhere")})
    said: list[str] = []

    with pytest.raises(typer.Exit):
        sync.ensure_local(sync.find_project("up"), said.append)

    assert "elsewhere" in "\n".join(said)


def test_a_mount_registration_spells_the_reopening_launch(
    registry_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The session that proposed the mount is told which launch to repeat.

    Mounts are built at launch, so a registration's effect waits for one —
    and the launch that opened this session recorded its own invocation
    precisely so this moment names the reopening instead of sending whoever
    reads it to reconstruct profile and flags from memory.
    """
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    ledger = registry_root / ".lup" / "preflight" / "opened.json"
    ledger.parent.mkdir(parents=True)
    ledger.write_text(json.dumps({"launch": ["harness", "claude", "-p", "dev"]}))
    monkeypatch.setenv("LUP_BOUNDARY_NONCE", "opened")

    sync.setup_project("corpus", str(corpus), mount="ro")

    assert (
        "Reopen this conversation to mount it: "
        "uv run lup-devtools harness claude -p dev --continue"
    ) in capsys.readouterr().out


def test_a_session_with_no_recorded_launch_gets_no_reopening(
    registry_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A hint reconstructed from nothing would name a launch nobody took."""
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    monkeypatch.delenv("LUP_BOUNDARY_NONCE", raising=False)

    sync.setup_project("corpus", str(corpus), mount="ro")

    out = capsys.readouterr().out
    assert "takes effect at the next launch" in out
    assert "Reopen this conversation" not in out


def located(entry: sync.ProjectEntry) -> sync.LocatedProject:
    """One registration this machine failed to locate, as the reading of it."""
    return sync.LocatedProject(entry=entry)


def cloned_with_origin(remote: Path, repository: Path, origin: str) -> None:
    """A bare clone of `remote` whose origin is spelled the way `origin` is.

    A forge cannot be reached from a test, and what these exercise is not the
    network but the comparison: a clone carrying the right history under an
    ssh spelling, and one carrying somebody else's under a plausible one, are
    the two cases the refusal has to tell apart.
    """
    repository.parent.mkdir(parents=True, exist_ok=True)
    sh.Command("git")("clone", "--bare", str(remote), str(repository), _tty_out=False)
    Repository(Path(str(repository))).answer("remote", "set-url", "origin", origin)


def test_a_tracked_requirement_nobody_answered_is_reported_with_its_command(
    registry_root: Path, cache: Path, remote: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Refused up front, rather than found by a fresh checkout at the point of use.

    A tracked entry saying the project cannot work without that repository is
    the one thing that makes its absence a result: status names it, says what
    would answer it, and exits nonzero, rather than printing `not cloned` in
    a column and leaving the next command to fail.
    """
    tracked(registry_root, {"name": "up", "required": True, "url": str(remote)})

    with pytest.raises(typer.Exit):
        sync.status_cmd()

    out = capsys.readouterr().out
    assert "'up' is required by this project" in out
    assert "uv run lup-devtools sync fetch up" in out


def test_a_requirement_with_nowhere_to_read_it_from_names_both_halves(
    registry_root: Path, cache: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """What the shipped scaffold looks like on a machine that answered nothing.

    The tracked half declares the need and names no account; the machine owes
    a transport or a path, so the report is what to write and where, not a
    complaint that a URL is missing.
    """
    tracked(registry_root, {"name": "up", "required": True})

    with pytest.raises(typer.Exit):
        sync.status_cmd()

    out = capsys.readouterr().out
    assert "no url in sync.json" in out
    assert "no remote or path in sync.json.local" in out
    assert "uv run lup-devtools sync remote up <url>" in out
    assert "uv run lup-devtools sync setup up /path/to/repo" in out


def test_fetch_materializes_a_requirement_whose_review_is_ignored(
    registry_root: Path, cache: Path, remote: Path
) -> None:
    """`ignore` withholds a review and never withholds the repository.

    Being the upstream of this project is a reason not to read its commits
    back, and no reason at all for a workflow that needs the checkout to find
    nothing there.
    """
    tracked(registry_root, {"name": "up", "required": True, "url": str(remote)})
    registered(registry_root, {"name": "up", "ignore": True})

    sync.fetch_cmd(None)

    assert (cache / "up.git" / "tree" / "main" / "file.txt").read_text() == (
        "revision 2\n"
    )


def test_the_mount_table_carries_a_project_the_tracked_half_declares(
    registry_root: Path, cache: Path, remote: Path
) -> None:
    """A mount is the project's claim, so the committed half may make it.

    Still never defaulted: this entry writes it. What the tracked half buys
    is that the claim is made once for every machine instead of being widened
    on each one by whoever meets the absence first — and it hands out nothing
    until this machine says where the project is, which is the URL here.
    """
    tracked(
        registry_root,
        {"name": "up", "required": True, "mount": "rw", "url": str(remote)},
    )

    roots = sync.accessible_roots(lambda said: None)

    assert [(root.path, root.writable) for root in roots] == [(cache / "up.git", True)]


def test_an_ssh_clone_of_an_https_registration_is_one_repository(
    registry_root: Path, cache: Path, remote: Path
) -> None:
    """The ordinary arrangement, which a URL comparison reads as a conflict.

    The tracked file holds the canonical https URL because that is what every
    machine shares; this machine's keys are ssh, so its clone reaches the
    same history at `git@...`. Nothing is wrong, and the registration needs
    no place to hide the machine's spelling in.
    """
    tracked(registry_root, {"name": "up", "url": "https://example.test/owner/repo"})
    registered(
        registry_root, {"name": "up", "remote": "git@example.test:owner/repo.git"}
    )
    cloned_with_origin(remote, cache / "up.git", "git@example.test:owner/repo.git")

    found = sync.existing_upstream(sync.find_project("up"))

    assert found is not None
    assert found.checkout == cache / "up.git"


def test_two_different_repositories_under_one_name_are_still_refused(
    registry_root: Path, cache: Path, remote: Path
) -> None:
    """And the refusal says which file holds which half of the answer.

    Accepting a transport is not relaxing the check: a clone of somebody
    else's repository under this name would be reviewed as this one, mounted
    as this one and committed into as this one. What changes is that the
    reader is told the file and the edit rather than to correct
    "the registration".
    """
    tracked(registry_root, {"name": "up", "url": "https://example.test/owner/repo"})
    cloned_with_origin(
        remote, cache / "up.git", "git@example.test:someone-else/repo.git"
    )
    said: list[str] = []

    with pytest.raises(typer.Exit):
        sync.existing_upstream(sync.find_project("up"))

    with pytest.raises(typer.Exit):
        sync.require_registered_origin(
            sync.find_project("up"), cache / "up.git", said.append
        )

    message = "\n".join(said)
    assert "someone-else/repo" in message
    assert "set \"url\" on the 'up' entry in sync.json" in message
    assert f"remove {cache / 'up.git'}" in message
    assert "register them under two names" in message


def test_a_transport_naming_another_repository_is_refused_where_it_is_written(
    registry_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Checked as the claim is made, like a device grant is exercised as it is.

    The alternative is a clone of the wrong history made on the strength of a
    typo, and a review of it under a name that means something else.
    """
    tracked(registry_root, {"name": "up", "url": "https://example.test/owner/repo"})

    with pytest.raises(typer.Exit):
        sync.set_remote("up", "git@example.test:someone-else/repo.git")

    said = capsys.readouterr().err
    assert "does not name the repository 'up' is registered as" in said
    assert "register it under its own name" in said
    assert sync.local_file().exists() is False


def test_a_transport_this_machine_reaches_it_over_is_recorded_locally(
    registry_root: Path,
) -> None:
    """The machine's half goes to the machine's file, and nowhere else."""
    tracked(registry_root, {"name": "up", "url": "https://example.test/owner/repo"})

    sync.set_remote("up", "ssh://git@example.test/owner/repo.git")

    assert json.loads(sync.local_file().read_text())["projects"] == [
        {"name": "up", "remote": "ssh://git@example.test/owner/repo.git"}
    ]
    assert json.loads(sync.sync_file().read_text())["projects"] == [
        {"name": "up", "url": "https://example.test/owner/repo"}
    ]
    assert sync.transport_url(sync.find_project("up")) == (
        "ssh://git@example.test/owner/repo.git"
    )
    assert sync.registered_repository(sync.find_project("up")) == (
        "https://example.test/owner/repo"
    )


def test_the_scaffold_itself_does_not_owe_the_requirement_it_ships(
    registry_root: Path,
) -> None:
    """`sync.json` in the template is the file an adopter receives.

    The scaffold is the upstream of every registration it ships, so the
    requirement is the adopter's to answer and the tree that wrote it has
    nothing to clone. A requirement in the machine's own local file is owed
    here like any other.
    """
    tracked(registry_root, {"name": "lup", "required": True})
    (registry_root / "pyproject.toml").write_text("[tool.lup]\ntemplate = true\n")

    assert sync.unmet_requirements([located(sync.find_project("lup"))]) == []

    registered(registry_root, {"name": "mine", "required": True})

    assert [
        found.name
        for found in sync.unmet_requirements([located(p) for p in sync.load_projects()])
    ] == ["mine"]


def test_a_requirement_naming_this_repository_is_met_by_standing_in_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A project registered at its own origin needs no second copy of itself.

    Read off origin rather than off a key somebody has to remember to write,
    and on identity rather than spelling, so the ssh origin of an https
    registration answers it too.
    """
    own = tmp_path / "own"
    git = initialized_repo(own, tmp_path / "hooks")
    git("remote", "add", "origin", "https://example.test/owner/repo")
    monkeypatch.setattr(sync, "project_root", lambda: own)
    (own / "sync.json").write_text(
        json.dumps(
            {
                "projects": [
                    {
                        "name": "itself",
                        "required": True,
                        "url": "git@example.test:owner/repo.git",
                    }
                ]
            }
        )
    )

    assert sync.unmet_requirements([located(sync.find_project("itself"))]) == []
