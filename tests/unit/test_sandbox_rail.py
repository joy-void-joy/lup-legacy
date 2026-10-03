"""Behavior tests for the worktree lease expressed as mounts.

The two that carry the module are the ones about siblings: that they are
mounted at all, and that they are mounted read-only. Omitting them looks like
the tighter choice and is the one that lets `git gc` delete their
administrative state.
"""

from pathlib import Path

import pytest

from lup.execution.shell import git
from lup.harness.image import Image
from lup.sandbox.rail import (
    AccessibleRoot,
    Lease,
    demoted,
    fleet_lease,
    hold_pruning_across,
    hold_worktree_pruning,
    in_repository,
    lease_for,
    prepared_across,
    prepared_shared_directory,
    repository_layout,
    same_path,
    sibling_worktrees,
    worker_lease,
)


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    """A repository with two linked worktrees, since that is the whole subject."""
    root = tmp_path / "main"
    root.mkdir()
    git("-C", str(root), "init", "-q", "-b", "main")
    git("-C", str(root), "config", "user.email", "test@example.invalid")
    git("-C", str(root), "config", "user.name", "Test")
    (root / "README.md").write_text("readme\n", encoding="utf-8")
    git("-C", str(root), "add", "-A")
    git("-C", str(root), "commit", "-qm", "first")
    git("-C", str(root), "worktree", "add", "-q", str(tmp_path / "mine"), "-b", "mine")
    git(
        "-C", str(root), "worktree", "add", "-q", str(tmp_path / "other"), "-b", "other"
    )
    return tmp_path


@pytest.fixture
def bare_repository(tmp_path: Path) -> Path:
    """A bare repository with its worktrees beside it, not under a checkout.

    The layout this rail actually runs in, and the one that puts the shared
    directory in `git worktree list`: a bare repository is reported as the
    main worktree, so it comes back from `sibling_worktrees` as well as from
    `repository_layout`. A normal checkout never produces that, which is why
    the collision it causes needs its own fixture to reach.
    """
    source = tmp_path / "source"
    source.mkdir()
    git("-C", str(source), "init", "-q", "-b", "main")
    git("-C", str(source), "config", "user.email", "test@example.invalid")
    git("-C", str(source), "config", "user.name", "Test")
    (source / "README.md").write_text("readme\n", encoding="utf-8")
    git("-C", str(source), "add", "-A")
    git("-C", str(source), "commit", "-qm", "first")
    bare = tmp_path / "repo.git"
    git("clone", "-q", "--bare", str(source), str(bare))
    git("-C", str(bare), "worktree", "add", "-q", str(tmp_path / "mine"), "-b", "mine")
    return tmp_path


def read_only_here(leased: Lease, found: Path) -> bool:
    """Whether the deepest mount covering this path is a read-only one.

    The lease nests, so "is this under something writable" answers the wrong
    question: everything in the shared directory is under a writable mount,
    and a sibling's administrative entry is read-only inside it regardless.
    A mount engine settles that by applying parents first and letting the
    deepest entry win, which is what :meth:`Lease.answers_from` reads and the
    boundary reads after it -- so the test asks the lease rather than keeping
    a copy that could drift away from the thing under test.
    """
    return leased.covers(found) and not leased.writable_at(found)


def holes(withheld: list[Path]) -> list[Path]:
    """The paths a lease names read-only, without what merely sits inside them.

    A hole over a directory withholds everything beneath it, so a walk
    reports the directory and every sample hook git's own template left in
    it. Those are the hole rather than a second contract, and their names
    are git's version to change — so what a contract is asserted against is
    the paths somebody wrote down, sorted, and the walk stays the thing that
    finds a hole nobody meant to punch.
    """
    return sorted(
        path
        for path in withheld
        if not any(other in path.parents for other in withheld)
    )


def test_no_path_is_leased_writable_and_read_only_at_once(
    bare_repository: Path,
) -> None:
    """One path, one mode: two mounts at one target have no tie-break to trust.

    The shared directory arrives twice here -- once as the directory this
    lease holds read-only, once as its own sibling, because a bare repository
    is the main worktree git reports. Taken as a sibling it would be named
    writable, and a path named both ways settles toward writable, which is
    `config` writable with it -- so the lease's own mode is the one kept.
    """
    layout = repository_layout(bare_repository / "mine")
    leased = lease_for(bare_repository / "mine")

    # The collision this pins is only real while git reports it, so the
    # premise is asserted rather than assumed: without this line the test
    # would keep passing against a git that stopped listing the bare main
    # worktree, having quietly stopped exercising anything.
    assert layout.common in sibling_worktrees(bare_repository / "mine")
    assert not set(leased.writable) & set(leased.read_only)
    assert not leased.writable_at(layout.common / "config")
    assert leased.writable_at(layout.common / "refs")


def test_a_checkout_inside_the_shared_directory_is_not_bound_a_second_time(
    tmp_path: Path,
) -> None:
    """A mount that would outlive the worktree it holds, and the reason it would.

    Where a bare repository keeps its checkouts beneath itself, the shared
    directory's own mount already reaches every one of them at the same path
    and in the same mode. Binding each again changes nothing a session can
    observe and costs what no session can undo: a mount point is not
    removable from inside its own namespace, so `git worktree remove` empties
    the checkout, `rmdir` fails with a busy device, and the directory outlives
    the branch -- one per landed branch, until nobody can say which are live.
    """
    bare = tmp_path / "repo.git"
    source = tmp_path / "source"
    source.mkdir()
    git("-C", str(source), "init", "-q", "-b", "main")
    git("-C", str(source), "config", "user.email", "test@example.invalid")
    git("-C", str(source), "config", "user.name", "Test")
    (source / "README.md").write_text("readme\n", encoding="utf-8")
    git("-C", str(source), "add", "-A")
    git("-C", str(source), "commit", "-qm", "first")
    git("clone", "-q", "--bare", str(source), str(bare))
    for name in ("mine", "other"):
        git(
            "-C",
            str(bare),
            "worktree",
            "add",
            "-q",
            str(bare / "tree" / name),
            "-b",
            name,
        )

    leased = lease_for(bare / "tree" / "mine")

    assert bare / "tree" / "mine" not in leased.writable
    assert bare / "tree" / "other" not in leased.writable
    assert leased.writable_at(bare / "tree" / "mine")
    assert leased.writable_at(bare / "tree" / "other")


def test_a_lease_makes_its_own_worktree_writable(repository: Path) -> None:
    leased = lease_for(repository / "mine")
    assert repository / "mine" in leased.writable


def test_a_lease_mounts_siblings_rather_than_leaving_them_out(
    repository: Path,
) -> None:
    """Omitting them is the trap: `git gc` prunes worktrees whose directory is gone.

    A session that could not see its siblings would find every one of their
    directories absent and delete their administrative state from the shared
    repository as ordinary housekeeping, with no error anywhere. So they are
    present -- which is the guard, and mounting them at all is what satisfies
    it, rather than the mode they are mounted in.
    """
    leased = lease_for(repository / "mine")
    assert repository / "other" in leased.writable
    assert repository / "other" not in leased.read_only


def test_a_lease_does_not_mount_the_worktree_it_is_for_as_a_sibling(
    repository: Path,
) -> None:
    leased = lease_for(repository / "mine")
    assert repository / "mine" not in leased.read_only


def test_the_shared_directory_is_read_only_with_its_directories_writable(
    repository: Path,
) -> None:
    """Every administrative entry writable to remove a worktree, and `config` not.

    Punching a sibling's administrative entry read-only back over the shared
    directory keeps it present and unwritable -- and makes `git worktree
    remove` impossible from inside, since removing a worktree unlinks exactly
    that entry. `config` and `hooks/` stay under the read-only directory, and
    a directory mount is what a host-side rewrite of `config` cannot detach.
    """
    layout = repository_layout(repository / "mine")
    leased = lease_for(repository / "mine")
    assert not leased.writable_at(layout.common)
    assert not leased.writable_at(layout.common / "config")
    assert not leased.writable_at(layout.common / "hooks" / "pre-commit")
    assert not leased.writable_at(layout.common / "modules")
    for data in ("objects", "refs", "logs", "worktrees"):
        assert leased.writable_at(layout.common / data)
    assert leased.writable_at(layout.common / "worktrees" / "other")
    assert leased.writable_at(layout.private)


def test_a_commit_survives_everything_the_lease_leaves_unwritable(
    repository: Path,
) -> None:
    """The claim above, run rather than asserted about a list of names.

    A lease that names the paths a commit needs can always miss one, and
    `logs` is the one easiest to miss: a ref update appends to
    `logs/refs/heads/<branch>` wherever that file already exists, git fails
    the whole update when it cannot, and a contained session cannot commit
    at all -- with the reflog this module's docstring rests its case on never
    written either. Comparing names cannot catch that, because the name
    nobody thought of is the one missing from both sides.

    Withholding write permission from exactly what the lease calls read-only
    is the cheapest faithful model of the mount table, and it is the only
    shape of test that catches a path nobody thought to name.

    For a withheld *file* the model is only half of one, which is why the
    contents are compared as well. A read-only bind refuses the rename git
    ends every config write with; a cleared write bit does not, because the
    directory holding `config.lock` is writable and the rename replaces the
    file rather than opening it. Permission catches a path git needs, and
    the byte comparison catches a write git managed to land anyway.
    """
    worktree = repository / "mine"
    leased = lease_for(worktree)
    layout = repository_layout(worktree)

    # Every file too, not only the directories holding them. A directory with
    # its write bit off still lets an existing file inside it be rewritten,
    # which a read-only mount does not -- and modelling only the directories
    # would let this test pass against the very bug it is for.
    withheld = [
        found
        for found in [layout.common, *layout.common.rglob("*")]
        if read_only_here(leased, found)
    ]
    # The shared directory, and asserting the whole list rather than a
    # membership is the point: this is the contract the lease states, so a
    # path appearing here is a change somebody has to mean rather than one
    # that slips in. Inside it, `config` and `hooks/` are what stay withheld.
    assert holes(withheld) == [layout.common]
    assert {layout.common / "config", layout.common / "hooks"} <= set(withheld)
    restored = {entry: entry.stat().st_mode for entry in withheld}
    before = (layout.common / "config").read_bytes()
    try:
        for entry in withheld:
            entry.chmod(restored[entry] & ~0o222)
        (worktree / "second.txt").write_text("second\n", encoding="utf-8")
        git("-C", str(worktree), "add", "-A")
        git("-C", str(worktree), "commit", "-qm", "second")
    finally:
        for entry, mode in restored.items():
            entry.chmod(mode)
    assert git.out("-C", str(worktree), "log", "-1", "--format=%s").strip() == "second"
    assert (layout.common / "config").read_bytes() == before


def test_a_new_worktree_can_be_cut_under_everything_the_lease_withholds(
    repository: Path,
) -> None:
    """Cutting a worktree is the workflow this repository mandates, run rather than named.

    `git worktree add` writes a whole administrative entry of its own beside
    the siblings', so the directory holding them has to admit a new child.

    Modelled the way the commit test models it -- withhold write permission
    from exactly what the lease calls read-only, then run the real command --
    because a lease asserted about by name passes whether or not git can do
    anything under it.
    """
    worktree = repository / "mine"
    leased = lease_for(worktree)
    layout = repository_layout(worktree)
    withheld = [
        found
        for found in [layout.common, *layout.common.rglob("*")]
        if read_only_here(leased, found)
    ]

    restored = {entry: entry.stat().st_mode for entry in withheld}
    before = (layout.common / "config").read_bytes()
    try:
        for entry in withheld:
            entry.chmod(restored[entry] & ~0o222)
        git(
            "-C",
            str(worktree),
            "worktree",
            "add",
            "-q",
            str(repository / "cut"),
            "-b",
            "cut",
        )
    finally:
        for entry, mode in restored.items():
            entry.chmod(mode)
    assert (repository / "cut").is_dir()
    assert (layout.common / "worktrees" / "cut").is_dir()
    assert (layout.common / "config").read_bytes() == before


def test_a_sibling_worktree_can_be_removed_under_the_lease(
    repository: Path,
) -> None:
    """The other half of the workflow, and the one a nested lease refuses.

    `git worktree remove` deletes the checkout and unlinks its administrative
    entry, so a lease holding either read-only refuses it -- and refuses it
    with an errno about a filesystem, which reads as a broken disk rather
    than as confinement. A sweep that lands every branch then cannot clear
    any of them.

    Run rather than asserted about names, for the reason the two above are.
    """
    worktree = repository / "mine"
    leased = lease_for(worktree)
    layout = repository_layout(worktree)
    withheld = [
        found
        for found in [layout.common, *layout.common.rglob("*"), repository / "other"]
        if read_only_here(leased, found)
    ]

    restored = {entry: entry.stat().st_mode for entry in withheld}
    before = (layout.common / "config").read_bytes()
    try:
        for entry in withheld:
            entry.chmod(restored[entry] & ~0o222)
        git("-C", str(worktree), "worktree", "remove", str(repository / "other"))
    finally:
        for entry, mode in restored.items():
            entry.chmod(mode)
    assert not (repository / "other").exists()
    assert not (layout.common / "worktrees" / "other").exists()
    assert (layout.common / "config").read_bytes() == before


def test_a_plain_checkout_leases_its_own_git_directory(tmp_path: Path) -> None:
    """A repository with no linked worktrees is degenerate here, not broken."""
    git("-C", str(tmp_path), "init", "-q", "-b", "main")
    layout = repository_layout(tmp_path)
    assert not layout.linked()
    assert lease_for(tmp_path).writable_at(layout.common)


def test_a_plain_checkout_holds_its_config_back_too(tmp_path: Path) -> None:
    """The keys naming host programs are the same file in either layout.

    Degenerate here means the shared directory *is* `.git`, not that there
    is no `config` under it -- so a lease that held the file back only where
    a worktree happened to be linked would leave the same door open on every
    plain clone.
    """
    git("-C", str(tmp_path), "init", "-q", "-b", "main")
    layout = repository_layout(tmp_path)
    assert layout.common / "config" in lease_for(tmp_path).read_only
    assert layout.common / "config" in worker_lease(tmp_path).read_only


def started_with(leased: Lease, checkout: Path) -> list[str]:
    """The argv a session opens with under this lease."""
    return Image().session_arguments(
        tag="lup-agent:x",
        checkout=checkout,
        uid=1000,
        gid=1000,
        writable=leased.writable,
        read_only=leased.read_only,
        state_volume="lup-cfg-x",
        config_home_env="CLAUDE_CONFIG_DIR",
    )


def test_the_shared_directory_reaches_a_session_read_only_under_its_writable_data(
    repository: Path,
) -> None:
    """The nesting has to survive the argv, not only the lease that declared it.

    The shared directory is bound read-only as a directory and each of its
    data directories writable after it. Measured on podman 6.1.0 the engine
    sorts by depth and would land them either way, and that is exactly why
    the order is pinned: an engine applying the list as written would shadow
    every writable child with the read-only parent. `config` and `hooks/`
    get no bind of their own -- a file bind is what a host rewrite detaches.
    """
    layout = repository_layout(repository / "mine")
    started = started_with(lease_for(repository / "mine"), repository / "mine")
    shared = f"{layout.common}:{layout.common}:ro"
    for data in ("objects", "refs", "logs", "worktrees"):
        child = layout.common / data
        assert started.index(shared) < started.index(f"{child}:{child}:rw")
    assert not [word for word in started if word.startswith(f"{layout.common}/config")]
    assert not [word for word in started if word.startswith(f"{layout.common}/hooks")]


def test_a_plain_checkout_binds_config_read_only_inside_its_writable_share(
    tmp_path: Path,
) -> None:
    """The fallback layout, with the hole emitted after the base it sits in."""
    git("-C", str(tmp_path), "init", "-q", "-b", "main")
    layout = repository_layout(tmp_path)
    leased = lease_for(tmp_path)
    started = started_with(leased, tmp_path)
    carrier = leased.answers_from(layout.common)
    base = f"{carrier}:{carrier}:rw"
    for held in (layout.common / "config", layout.common / "hooks"):
        assert started.index(base) < started.index(f"{held}:{held}:ro")


def test_paths_are_mounted_at_the_names_the_host_calls_them(
    repository: Path,
) -> None:
    """Forced, not chosen: a linked worktree's `.git` holds an absolute pointer."""
    mapped = same_path([repository / "mine"])
    assert mapped == {repository / "mine": (repository / "mine").as_posix()}


def test_a_human_owned_path_stays_writable_and_the_policy_asks(
    repository: Path,
) -> None:
    """A mount holding `README.md` would refuse every fast-forward touching it.

    Git replaces a file by unlinking it, which a mount point refuses, so the
    merge landing a branch would be the user's from a host terminal each
    time. What a mount would protect, the policy protects by asking, which a
    mount cannot do.
    """
    readme = repository / "mine" / "README.md"
    readme.write_text("x", encoding="utf-8")
    leased = lease_for(repository / "mine")
    assert readme.resolve() not in leased.read_only
    assert leased.covers(readme)
    assert set(leased.read_only) == {repository_layout(repository / "mine").common}


def test_siblings_are_asked_of_git_rather_than_scanned(repository: Path) -> None:
    """Where sibling checkouts live is the repository's arrangement, not a layout."""
    (repository / "unrelated").mkdir()
    found = sibling_worktrees(repository / "mine")
    assert repository / "other" in found
    assert repository / "unrelated" not in found


def test_a_lease_reports_whether_it_covers_a_path(repository: Path) -> None:
    leased = lease_for(repository / "mine")
    assert leased.covers(repository / "mine" / "src" / "x.py")
    assert not leased.covers(Path("/etc/passwd"))


def test_an_empty_lease_covers_nothing() -> None:
    assert not Lease().covers(Path("/anywhere"))


@pytest.fixture
def other_repository(tmp_path: Path) -> Path:
    """A second repository entirely, which is what a declared root is.

    Beside the first rather than under it, because the whole subject is a
    checkout `git worktree list` in this repository will never mention. Its
    own worktree is linked, so the arrangement a declared root gets is the
    same nested one the checkout's own lease has rather than the degenerate
    plain-checkout case.
    """
    root = tmp_path / "away" / "main"
    root.mkdir(parents=True)
    git("-C", str(root), "init", "-q", "-b", "main")
    git("-C", str(root), "config", "user.email", "test@example.invalid")
    git("-C", str(root), "config", "user.name", "Test")
    (root / "README.md").write_text("readme\n", encoding="utf-8")
    git("-C", str(root), "add", "-A")
    git("-C", str(root), "commit", "-qm", "first")
    git(
        "-C",
        str(root),
        "worktree",
        "add",
        "-q",
        str(root.parent / "side"),
        "-b",
        "side",
    )
    return root


def test_a_declared_root_is_leased_with_the_repository_behind_it(
    repository: Path, other_repository: Path
) -> None:
    """A bind of the working copy alone is a checkout pointing at nothing.

    Its `.git` is a file holding an absolute `gitdir:` pointer, so the shared
    directory has to come too -- and the siblings with it, or `git gc` inside
    the boundary prunes the administrative state of worktrees it cannot see,
    in a repository nobody in this session owns.
    """
    side = other_repository.parent / "side"
    leased = fleet_lease(repository / "mine", accessible=[AccessibleRoot(path=side)])
    layout = repository_layout(side)

    assert leased.writable_at(side)
    assert not leased.writable_at(layout.common / "config")
    assert leased.writable_at(layout.common / "refs")
    assert leased.writable_at(layout.private)
    assert leased.writable_at(other_repository)
    assert leased.writable_at(repository / "mine")


def test_a_commit_lands_in_a_declared_root_under_the_lease_it_gets(
    repository: Path, other_repository: Path
) -> None:
    """Run rather than asserted about names, for the reason the local one is.

    Committing is the whole point of declaring a root read-write, and a lease
    that names the paths a commit needs can always miss one. Modelled the way
    the checkout's own commit test models it: withhold write permission from
    exactly what the lease calls read-only, then run the real command.

    A declared root gets the same read-only shared directory the checkout's
    own lease gets, and asserting the whole list says so: a repository
    reached across the boundary is one whose config keys and hook scripts run
    on the same host.
    """
    side = other_repository.parent / "side"
    leased = fleet_lease(repository / "mine", accessible=[AccessibleRoot(path=side)])
    layout = repository_layout(side)
    withheld = [
        found
        for found in [layout.common, *layout.common.rglob("*"), other_repository]
        if read_only_here(leased, found)
    ]
    assert holes(withheld) == [layout.common]

    restored = {entry: entry.stat().st_mode for entry in withheld}
    before = (layout.common / "config").read_bytes()
    try:
        for entry in withheld:
            entry.chmod(restored[entry] & ~0o222)
        (side / "second.txt").write_text("second\n", encoding="utf-8")
        git("-C", str(side), "add", "-A")
        git("-C", str(side), "commit", "-qm", "second")
    finally:
        for entry, mode in restored.items():
            entry.chmod(mode)
    assert git.out("-C", str(side), "log", "-1", "--format=%s").strip() == "second"
    assert (layout.common / "config").read_bytes() == before


def test_a_root_declared_read_only_has_nothing_writable_under_it(
    repository: Path, other_repository: Path
) -> None:
    """Including its shared directory, which is what read-only has to mean."""
    leased = fleet_lease(
        repository / "mine",
        accessible=[AccessibleRoot(path=other_repository, writable=False)],
    )

    assert not leased.writable_at(other_repository)
    assert not leased.writable_at(repository_layout(other_repository).common)
    assert leased.covers(other_repository)
    assert not any(
        other_repository == root or other_repository in root.parents
        for root in leased.writable
    )


def test_two_declared_worktrees_of_one_repository_settle_toward_writable(
    repository: Path, other_repository: Path
) -> None:
    """The collision only a global settlement catches.

    Each worktree's own lease calls the other a sibling and holds it
    read-only, so settling the leases one at a time leaves both of them
    unwritable -- with each having been named read-write deliberately.
    """
    side = other_repository.parent / "side"
    leased = fleet_lease(
        repository / "mine",
        accessible=[AccessibleRoot(path=other_repository), AccessibleRoot(path=side)],
    )

    assert other_repository in leased.writable
    assert side in leased.writable
    assert other_repository not in leased.read_only
    assert side not in leased.read_only


def test_a_declared_root_that_is_gone_is_skipped_rather_than_mounted(
    repository: Path, tmp_path: Path
) -> None:
    """A bind whose source is absent is one the engine refuses the container for.

    So a registration somebody moved costs its own reachability and not the
    session.
    """
    leased = fleet_lease(
        repository / "mine",
        accessible=[AccessibleRoot(path=tmp_path / "never-existed")],
    )

    assert leased.writable == lease_for(repository / "mine").writable


def test_a_declared_root_the_checkout_already_leases_is_not_mounted_twice(
    repository: Path,
) -> None:
    leased = fleet_lease(
        repository / "mine",
        accessible=[AccessibleRoot(path=repository / "mine" / "src")],
    )

    assert leased.writable == lease_for(repository / "mine").writable


def test_a_declared_root_that_is_not_a_repository_is_one_plain_mount(
    repository: Path, tmp_path: Path
) -> None:
    """Not everything worth reaching is a checkout, and git is not asked about one."""
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    leased = fleet_lease(repository / "mine", accessible=[AccessibleRoot(path=corpus)])

    assert corpus in leased.writable
    assert not in_repository(corpus)


def test_the_prune_guard_is_armed_per_repository_and_says_where_it_was_not(
    repository: Path, other_repository: Path, tmp_path: Path
) -> None:
    """A directory git does not answer for is neither armed nor reported."""
    corpus = tmp_path / "corpus"
    corpus.mkdir()

    assert hold_pruning_across([repository / "mine", other_repository, corpus]) == []
    assert (
        git.out("-C", str(other_repository), "config", "gc.worktreePruneExpire").strip()
        == "never"
    )


def test_a_worker_lease_makes_only_its_own_worktree_writable(
    repository: Path,
) -> None:
    """The half the session table deliberately stopped answering.

    Where `lease_for` hands a session every checkout of its repository, this
    confines one actor to the tree it was given -- which is the population the
    launch-time table missed, since every worktree a run leases is cut after
    its operator's container started.
    """
    leased = worker_lease(repository / "mine")
    assert repository / "mine" in leased.writable
    assert repository / "other" in leased.read_only
    assert repository / "other" not in leased.writable


def test_a_worker_lease_still_mounts_the_siblings_it_withholds(
    repository: Path,
) -> None:
    """Read-only, not absent. `git gc` prunes worktrees whose directory is gone."""
    leased = worker_lease(repository / "mine")
    assert leased.covers(repository / "other")


def test_a_worker_lease_holds_each_sibling_entry_read_only_inside_a_writable_share(
    repository: Path,
) -> None:
    """The nesting the arrangement rests on, and the reason it is per entry.

    The shared directory stays writable so a worktree can be cut at all; each
    sibling's own administrative entry is punched read-only back over it so
    nothing in here can remove one. The worktree's own entry is left writable,
    since that is the one it is entitled to move.
    """
    layout = repository_layout(repository / "mine")
    leased = worker_lease(repository / "mine")
    assert leased.writable_at(layout.common / "worktrees")
    assert not leased.writable_at(layout.common / "worktrees" / "other")
    assert leased.writable_at(layout.private)


def test_a_worker_lease_holds_the_shared_config_read_only(
    repository: Path,
) -> None:
    """The hole that guards the host rather than a sibling's bookkeeping.

    `core.hooksPath`, `alias.*`, `credential.helper` and `merge.*.driver` each
    hand git a command line the operator's next git command runs, from a write
    that lands in no diff. A worker pays nothing to be held out of it: no step
    of the mandated workflow writes the file.
    """
    layout = repository_layout(repository / "mine")
    leased = worker_lease(repository / "mine")
    assert read_only_here(leased, layout.common / "config")


def test_both_leases_hold_the_shared_hooks_read_only_in_either_layout(
    repository: Path, tmp_path: Path
) -> None:
    """The same door `config` opens, with no key in between.

    A `pre-commit` written into `<common>/hooks/` runs on the host at the
    operator's next commit in any worktree, and no config key is involved in
    arranging it. Held in both leases and in either layout, because the
    shared directory *is* `.git` in a plain checkout rather than absent from
    it, and a hole punched only where a worktree happens to be linked leaves
    the door open on every plain clone.

    Costing nothing is what lets it be held: hooks resolve through that same
    shared directory from every worktree, so a guard armed once on the host
    is inherited by each one cut afterwards rather than rewritten by it.
    """
    plain = tmp_path / "plain"
    plain.mkdir()
    git("-C", str(plain), "init", "-q", "-b", "main")
    for worktree in (repository / "mine", plain):
        hooks = repository_layout(worktree).common / "hooks"
        for leased in (lease_for(worktree), worker_lease(worktree)):
            assert read_only_here(leased, hooks)
            assert read_only_here(leased, hooks / "pre-commit")


def test_a_reviewer_lease_is_the_worker_lease_with_nothing_writable(
    repository: Path,
) -> None:
    """Built from the same call rather than a table of its own.

    Two tables could come to disagree about which checkouts exist; demoting
    one cannot, which is why a read-only actor is spelled this way.
    """
    working = worker_lease(repository / "mine")
    reviewing = demoted(working)
    assert reviewing.writable == {}
    assert repository / "mine" in reviewing.read_only
    assert repository / "other" in reviewing.read_only


def test_a_worker_lease_holds_no_human_owned_path_either(repository: Path) -> None:
    owned = repository / "mine" / "CLAUDE.md"
    owned.write_text("owned\n", encoding="utf-8")
    leased = worker_lease(repository / "mine")
    assert owned not in leased.read_only
    assert leased.covers(owned)


@pytest.fixture
def cloned(tmp_path: Path) -> Path:
    """A bare clone with a worktree inside it and a remote beside it, refs packed.

    The layout the rail is shaped for: `tree/` inside the shared directory,
    every ref packed the way a clone or a `gc` leaves them.
    """
    remote = tmp_path / "remote.git"
    git("init", "-q", "--bare", "-b", "main", str(remote))
    seed = tmp_path / "seed"
    git("clone", "-q", str(remote), str(seed))
    who = ("-c", "user.name=Test", "-c", "user.email=test@example.invalid")
    git("-C", str(seed), *who, "commit", "-q", "--allow-empty", "-m", "first")
    git("-C", str(seed), "push", "-q", "origin", "HEAD:main", "HEAD:gone")
    shared = tmp_path / "proj.git"
    git("clone", "-q", "--bare", str(remote), str(shared))
    fetched = "+refs/heads/*:refs/remotes/origin/*"
    git("-C", str(shared), "config", "remote.origin.fetch", fetched)
    git("-C", str(shared), "fetch", "-q", "origin")
    (shared / "tree").mkdir()
    worktree = shared / "tree" / "dev"
    git("-C", str(shared), "worktree", "add", "-q", str(worktree), "-b", "dev")
    git("-C", str(shared), "pack-refs", "--all")
    return worktree


def test_preparing_the_shared_directory_is_idempotent(cloned: Path) -> None:
    """Every launch runs it, so the second run has to change nothing at all."""
    common = repository_layout(cloned).common
    assert prepared_shared_directory(cloned, owned=("lup",)) == ""
    packed = common / "packed-refs"
    settled = {
        entry.name: (entry.is_symlink(), entry.lstat().st_ino)
        for entry in [*common.iterdir(), *(common / "lup-refs").iterdir()]
    }
    assert prepared_shared_directory(cloned, owned=("lup",)) == ""
    again = {
        entry.name: (entry.is_symlink(), entry.lstat().st_ino)
        for entry in [*common.iterdir(), *(common / "lup-refs").iterdir()]
    }
    assert again == settled
    assert packed.readlink() == Path("lup-refs") / "packed-refs"
    for name in ("logs", "info", "worktrees", "rr-cache", "lup"):
        assert (common / name).is_dir()
    assert not (common / "packed-refs.lock").exists()


def test_every_ref_survives_the_move_behind_the_symlink(cloned: Path) -> None:
    """What `for-each-ref` reads before is what it reads after, byte for byte."""
    listed = ["-C", str(cloned), "for-each-ref", "--format=%(refname) %(objectname)"]
    before = git.out(*listed)
    prepared_shared_directory(cloned)
    assert git.out(*listed) == before


def test_host_git_writes_packed_refs_through_the_symlink(cloned: Path) -> None:
    """The measured half: every ref rewrite lands beside the target, the link stays."""
    prepared_shared_directory(cloned)
    common = repository_layout(cloned).common
    git("-C", str(cloned), "branch", "-q", "topic")
    git("-C", str(cloned), "pack-refs", "--all")
    git("-C", str(cloned), "branch", "-q", "-D", "topic")
    remote = common.parent / "remote.git"
    git("-C", str(remote), "branch", "-q", "-D", "gone")
    git("-C", str(cloned), "fetch", "-q", "--prune", "origin")
    git("-C", str(cloned), "gc", "-q")
    packed = common / "packed-refs"
    assert packed.is_symlink()
    assert packed.readlink() == Path("lup-refs") / "packed-refs"
    names = git.out("-C", str(cloned), "for-each-ref", "--format=%(refname)").split()
    assert "refs/heads/topic" not in names
    assert "refs/remotes/origin/gone" not in names
    assert "refs/remotes/origin/main" in names


def test_fsck_reads_the_symlinked_packed_refs_as_a_warning(cloned: Path) -> None:
    """`git fsck` exits 8 over a symlinked `packed-refs` until this is lowered."""
    prepared_shared_directory(cloned)
    git("-C", str(cloned), "fsck", "--no-progress")


def test_the_config_host_git_writes_is_left_a_plain_file(cloned: Path) -> None:
    """The layout needs nothing of `config`, so host rewrites stay ordinary.

    `remote add`, `push -u` and a plain `config` set each replace the file by
    renaming a lockfile over it; under a directory bind that changes a file
    inside the mount rather than the mount, so nothing here redirects it.
    """
    prepared_shared_directory(cloned)
    common = repository_layout(cloned).common
    remote = common.parent / "remote.git"
    git("-C", str(cloned), "remote", "add", "again", str(remote))
    who = ("-c", "user.name=Test", "-c", "user.email=test@example.invalid")
    git("-C", str(cloned), *who, "commit", "-q", "--allow-empty", "-m", "second")
    git("-C", str(cloned), "push", "-q", "-u", "again", "dev")
    git("-C", str(cloned), "config", "lab.key", "value")
    config = common / "config"
    assert not config.is_symlink()
    assert git.out("-C", str(cloned), "config", "branch.dev.remote").strip() == "again"


def test_a_held_packed_refs_lock_defers_the_move_and_says_why(cloned: Path) -> None:
    """A writer mid-rewrite owns the file; the move waits for the next launch."""
    common = repository_layout(cloned).common
    (common / "packed-refs.lock").touch()
    said = prepared_shared_directory(cloned)
    assert "packed-refs.lock" in said
    assert not (common / "packed-refs").is_symlink()
    assert (common / "packed-refs.lock").exists()


def test_a_plain_checkout_is_left_as_it_is(tmp_path: Path) -> None:
    """Its lease keeps the directory writable, so nothing here is readied."""
    git("-C", str(tmp_path), "init", "-q", "-b", "main")
    before = sorted(entry.name for entry in (tmp_path / ".git").iterdir())
    assert prepared_shared_directory(tmp_path) == ""
    assert sorted(entry.name for entry in (tmp_path / ".git").iterdir()) == before


def test_a_launch_binds_every_directory_the_preparation_made(cloned: Path) -> None:
    """What the migration creates is what the lease hands back writable."""
    prepared_shared_directory(cloned, owned=("lup",))
    common = repository_layout(cloned).common
    leased = lease_for(cloned)
    for name in ("logs", "info", "worktrees", "rr-cache", "lup", "lup-refs", "tree"):
        assert leased.writable_at(common / name)
    assert leased.writable_at(common / "lup-refs" / "packed-refs")
    assert not leased.writable_at(common / "packed-refs")
    assert not leased.writable_at(common / "config")


def test_readying_passes_over_a_directory_git_does_not_answer_for(
    cloned: Path, tmp_path: Path
) -> None:
    """Reference material has no shared directory to ready, and is not a failure."""
    plain = tmp_path / "notes"
    plain.mkdir()
    assert prepared_across([cloned, plain], owned=("lup",)) == []
    assert (repository_layout(cloned).common / "lup").is_dir()
    assert list(plain.iterdir()) == []


def test_the_prune_guard_leaves_config_alone_once_it_stands(repository: Path) -> None:
    """A rewrite of `config` detaches a file bind in every running container."""
    config = repository_layout(repository / "mine").common / "config"
    assert hold_worktree_pruning(repository / "mine")
    placed = config.stat().st_ino
    assert hold_worktree_pruning(repository / "mine")
    assert config.stat().st_ino == placed
