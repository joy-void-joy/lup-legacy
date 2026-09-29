"""Behavior tests for judging a root before host git reads through its pointer.

A worktree is discovered from the repository that vouches for it -- found by
path, or remembered from the host -- rather than trusted for where it sits, so
every layout is exercised the same way: a plain clone with `git worktree add
../x` beside it, a bare clone holding `tree/`, a plain clone with worktrees
inside it, and a resolver worker two levels down; each clean, then with its
pointer redirected. Then the store: remembered only from the host, never under
a mount a launch grants. Then each place the judgement is wired, asserting the
refusal lands before any git runs.
"""

from pathlib import Path
from types import SimpleNamespace

import pytest
import typer

from lup.launch.pointer_trust import judged_roots, launcher_state_exposure
from lup.launch.refusal import LaunchRefused
from lup.execution.shell import git
from lup.harness.process import ExitStatus, LaunchRequest, ProcessLauncher
from lup.sandbox.checked import PointerCheckedLauncher, RedirectedPointer
from lup.sandbox.known import known_repositories, remember, store_directory
from lup.sandbox.pointers import pointer_drift, tree_checkouts, verdict
from lup.sandbox.rail import Lease, fleet_lease


class Reached(Exception):
    """Raised by a stand-in for the step a refusal must come before."""


def unreachable(*_args: object, **_kwargs: object) -> None:
    raise Reached


@pytest.fixture(autouse=True)
def own_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A store per test, so one test's first sighting is not another's memory."""
    state = tmp_path / "state"
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    monkeypatch.delenv("LUP_BOUNDARY_NONCE", raising=False)
    monkeypatch.delenv("LUP_SANDBOX_ACTIVE", raising=False)
    return state / "lup"


@pytest.fixture
def plain(tmp_path: Path) -> Path:
    """A plain clone with one commit, whose `.git` is a directory of its own."""
    checkout = tmp_path / "plain"
    checkout.mkdir()
    git("-C", str(checkout), "init", "-q", "-b", "main")
    (checkout / "README.md").write_text("readme\n", encoding="utf-8")
    git("-C", str(checkout), "add", "-A")
    git("-C", str(checkout), "commit", "-qm", "first")
    return checkout


@pytest.fixture
def clone(tmp_path: Path, plain: Path) -> Path:
    """A bare clone holding its worktrees in `tree/`."""
    bare = tmp_path / "proj.git"
    git("clone", "-q", "--bare", str(plain), str(bare))
    git("-C", str(bare), "worktree", "add", "-q", str(bare / "tree" / "main"), "main")
    git(
        "-C",
        str(bare),
        "worktree",
        "add",
        "-q",
        str(bare / "tree" / "side"),
        "-b",
        "side",
    )
    return bare


def beside(plain: Path, name: str) -> Path:
    """A worktree cut with `git worktree add ../<name>`, beside its repository."""
    worktree = plain.parent / name
    git("-C", str(plain), "worktree", "add", "-q", str(worktree), "-b", name)
    return worktree


def evil_gitdir(at: Path) -> Path:
    """A gitdir a container could build anywhere it may write, config and all."""
    (at / "objects").mkdir(parents=True)
    (at / "refs").mkdir()
    (at / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (at / "config").write_text("[core]\n\thooksPath = /evil\n", encoding="utf-8")
    return at


def redirect(worktree: Path, to: Path) -> None:
    (worktree / ".git").write_text(f"gitdir: {to}\n", encoding="utf-8")


# -- every layout, clean and redirected --


def test_a_worktree_beside_a_plain_clone_is_remembered_then_verified(
    plain: Path, tmp_path: Path
) -> None:
    """`git worktree add ../x` has no path back, so its repository is remembered.

    The first host-side sighting records it; afterwards the worktree is found
    through it, and a redirected pointer is a mismatch rather than a new
    repository to meet.
    """
    worktree = beside(plain, "x")
    first = judged_roots([worktree], operator=worktree)
    assert first.refusal == ""
    assert any("First sighting" in notice for notice in first.notices)
    assert (plain / ".git").resolve() in known_repositories()
    assert judged_roots([worktree], operator=worktree).notices == []

    redirect(worktree, evil_gitdir(tmp_path / "built"))
    assert str(worktree) in judged_roots([worktree], operator=worktree).refusal


def test_a_worktree_beside_a_plain_clone_is_vouched_for_from_the_clone(
    plain: Path,
) -> None:
    """Launched from the clone, its repository is found by path and vouches."""
    worktree = beside(plain, "x")
    judged = verdict(worktree, [plain / ".git"])
    assert (judged.repository, judged.candidate, judged.drifts) == (
        plain / ".git",
        None,
        [],
    )


def test_a_bare_clone_and_its_tree_worktrees_are_found_by_path(
    clone: Path, tmp_path: Path
) -> None:
    assert verdict(clone, []).repository == clone
    side = clone / "tree" / "side"
    assert (verdict(side, []).repository, verdict(side, []).drifts) == (clone, [])

    redirect(side, evil_gitdir(tmp_path / "built"))
    assert verdict(side, []).drifts != []


def test_a_worktree_inside_its_plain_clone_is_found_by_path(
    plain: Path, tmp_path: Path
) -> None:
    inside = plain / "wt"
    git("-C", str(plain), "worktree", "add", "-q", str(inside), "-b", "wt")
    judged = verdict(inside, [])
    assert (judged.repository, judged.drifts) == (plain / ".git", [])

    redirect(inside, evil_gitdir(tmp_path / "built"))
    assert verdict(inside, []).drifts != []


def test_a_resolver_worker_is_found_by_path_and_checked_at_every_git(
    clone: Path, tmp_path: Path
) -> None:
    """Workers sit in `tree/<name>-resolve-<id>/`, and the resolver runs git there."""

    class Recording(ProcessLauncher):
        def launch(self, request: LaunchRequest) -> ExitStatus:
            return ExitStatus(code=0)

    worker = clone / "tree" / "main-resolve-1" / "c1"
    git("-C", str(clone), "worktree", "add", "-q", "--detach", str(worker), "main")
    assert verdict(worker, []).repository == clone
    launcher = PointerCheckedLauncher(Recording(), clone / "tree" / "main")
    request = LaunchRequest(arguments=["git", "add", "-A"], cwd=worker)
    assert launcher.launch(request).code == 0

    redirect(worker, evil_gitdir(tmp_path / "built"))
    with pytest.raises(RedirectedPointer):
        launcher.launch(request)
    other = LaunchRequest(arguments=["uv", "--version"], cwd=worker)
    assert launcher.launch(other).code == 0


# -- what a container could plant, and the seven readings that catch it --


def test_a_pointer_swapped_for_a_directory_is_a_mismatch(plain: Path) -> None:
    """The swap that makes a worktree look like a plain checkout of its own."""
    worktree = beside(plain, "x")
    (worktree / ".git").unlink()
    evil_gitdir(worktree / ".git")
    assert verdict(worktree, [plain / ".git"]).drifts != []


def test_a_relative_worktree_pointer_is_not_drift(clone: Path) -> None:
    """`worktree.useRelativePaths` pointers resolve where git resolves them."""
    relative = clone / "tree" / "rel"
    relative_paths = ["-c", "worktree.useRelativePaths=true"]
    git(
        "-C",
        str(clone),
        *relative_paths,
        "worktree",
        "add",
        "-q",
        str(relative),
        "-b",
        "rel",
    )
    assert not (relative / ".git").read_text().split()[1].startswith("/")
    assert verdict(relative, []).drifts == []


def test_a_commondir_planted_in_the_repository_is_refused(
    clone: Path, tmp_path: Path
) -> None:
    (clone / "commondir").write_text(
        f"{evil_gitdir(tmp_path / 'b')}\n", encoding="utf-8"
    )
    assert verdict(clone, []).drifts != []
    assert verdict(clone / "tree" / "side", []).drifts != []


def test_a_hidden_entry_is_caught_from_the_checkout(
    clone: Path, tmp_path: Path
) -> None:
    """The entry's back-pointer rewritten too, blinding a scan that starts there."""
    side = clone / "tree" / "side"
    redirect(side, evil_gitdir(tmp_path / "built"))
    (clone / "worktrees" / "side" / "gitdir").write_text(
        f"{tmp_path / 'gone' / '.git'}\n", encoding="utf-8"
    )
    assert pointer_drift(clone) == []
    assert pointer_drift(clone, tree_checkouts(clone / "tree")) != []


def test_an_entry_that_does_not_list_its_checkout_back_is_a_mismatch(
    clone: Path, tmp_path: Path
) -> None:
    (clone / "worktrees" / "side" / "gitdir").write_text(
        f"{tmp_path / 'elsewhere' / '.git'}\n", encoding="utf-8"
    )
    assert verdict(clone / "tree" / "side", []).drifts != []


def test_an_entry_without_commondir_is_drift_only_where_it_stands_alone(
    clone: Path,
) -> None:
    """Git refuses an entry with neither `commondir` nor objects, and reads nothing."""
    entry = clone / "worktrees" / "side"
    (entry / "commondir").unlink()
    assert pointer_drift(clone) == []
    (entry / "objects").mkdir()
    (entry / "refs").mkdir(exist_ok=True)
    assert pointer_drift(clone) != []


# -- first sightings: remembered from the host, reported otherwise --


def test_a_repository_built_where_a_session_writes_is_not_trusted_on_sight(
    plain: Path,
) -> None:
    """Its entry deleted, the worktree aimed at a repository built in the clone."""
    remember([plain / ".git"])
    worktree = beside(plain, "x")
    built = evil_gitdir(plain / "fake")
    (built / "worktrees" / "x").mkdir(parents=True)
    (built / "worktrees" / "x" / "commondir").write_text("../..\n", encoding="utf-8")
    (built / "worktrees" / "x" / "gitdir").write_text(
        f"{worktree / '.git'}\n", encoding="utf-8"
    )
    git("-C", str(plain), "worktree", "remove", "--force", str(worktree))
    worktree.mkdir()
    redirect(worktree, built / "worktrees" / "x")
    trust = judged_roots([worktree], operator=worktree)
    assert any("Unverified" in notice for notice in trust.notices)
    assert built.resolve() not in known_repositories()


def test_a_first_sighting_waits_while_a_session_runs_in_it(
    plain: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import lup.launch.pointer_trust as pointer_trust

    monkeypatch.setattr(pointer_trust, "live_session", lambda _repository: True)
    worktree = beside(plain, "x")
    trust = judged_roots([worktree], operator=worktree)
    assert any("session is running" in notice for notice in trust.notices)
    assert known_repositories() == []


def test_a_launched_session_remembers_nothing_and_still_refuses(
    plain: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LUP_BOUNDARY_NONCE", "inside")
    worktree = beside(plain, "x")
    assert judged_roots([worktree], operator=plain) == judged_roots([])
    assert known_repositories() == []
    redirect(worktree, evil_gitdir(tmp_path / "built"))
    assert judged_roots([worktree], operator=plain).refusal != ""


def test_a_pointer_to_a_repository_of_its_own_is_a_candidate_not_a_refusal(
    plain: Path, tmp_path: Path
) -> None:
    """A submodule's `.git` names a repository outright; its shape refuses nothing."""
    separate = tmp_path / "separate.git"
    git("clone", "-q", "--bare", str(plain), str(separate))
    checkout = tmp_path / "sub"
    checkout.mkdir()
    redirect(checkout, separate)
    judged = verdict(checkout, [])
    assert (judged.candidate, judged.drifts) == (separate, [])


def test_a_directory_in_no_repository_passes(tmp_path: Path) -> None:
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    judged = verdict(corpus, [])
    assert (judged.checkout, judged.repository, judged.candidate) == (None, None, None)


# -- the store, which no container may write --


def test_the_store_honours_xdg_state_home_and_ignores_a_relative_one(
    own_store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert store_directory() == own_store
    monkeypatch.setenv("XDG_STATE_HOME", "relative/state")
    assert store_directory() == Path.home() / ".local" / "state" / "lup"


def test_a_lease_mounting_the_store_is_refused(own_store: Path) -> None:
    assert launcher_state_exposure(Lease()) == ""
    for lease in (
        Lease(writable={own_store.parent: str(own_store.parent)}),
        Lease(read_only={own_store: str(own_store)}),
    ):
        assert str(own_store) in launcher_state_exposure(lease)


def test_the_store_under_a_launched_root_is_refused_by_the_real_lease(
    clone: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Measured against the lease a launch builds, not a hand-written one."""
    side = clone / "tree" / "side"
    assert launcher_state_exposure(fleet_lease(side)) == ""
    monkeypatch.setenv("XDG_STATE_HOME", str(side / ".state"))
    assert launcher_state_exposure(fleet_lease(side)) != ""


# -- where it is wired --


def test_sync_refuses_a_redirected_registration_before_running_git(
    clone: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lup.devtools import sync

    side = clone / "tree" / "side"
    redirect(side, evil_gitdir(tmp_path / "built"))
    monkeypatch.setattr(sync, "registered_upstream", unreachable)
    registration: sync.ProjectEntry = {"name": "proj", "path": str(side)}
    with pytest.raises(typer.Exit):
        sync.existing_upstream(registration)
    with pytest.raises(typer.Exit):
        sync.ensure_local(registration, lambda _line: None)


def test_sync_refuses_a_redirected_cached_clone_before_refreshing(
    clone: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lup.devtools import sync

    (clone / "commondir").write_text(
        f"{evil_gitdir(tmp_path / 'b')}\n", encoding="utf-8"
    )
    monkeypatch.setattr(sync, "cached_clone", lambda _name: clone)
    monkeypatch.setattr(sync, "transport_url", lambda _proj: "https://example.invalid")
    monkeypatch.setattr(sync, "require_registered_origin", unreachable)
    monkeypatch.setattr(sync, "refresh", unreachable)
    with pytest.raises(typer.Exit):
        sync.ensure_local({"name": "proj"}, lambda _line: None)


def test_the_boundary_refuses_a_mismatch_and_an_exposed_store(
    clone: Path, tmp_path: Path, own_store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both postures settle their boundary here, so both refuse here."""
    import lup.launch.session as launch_session

    side = clone / "tree" / "side"
    monkeypatch.setattr(launch_session, "compile_boundary", unreachable)
    sandbox = SimpleNamespace(contained=lambda: False)
    arguments = {"sandbox": sandbox, "findings": [], "sentinels": None}
    extra = {"environment": None, "banner": None}
    with pytest.raises(Reached):
        launch_session.settle_boundary(side, None, **arguments, **extra)  # type: ignore[arg-type]
    assert (clone).resolve() in known_repositories()

    monkeypatch.setattr(
        launch_session,
        "fleet_lease",
        lambda *_a, **_k: Lease(writable={own_store: "x"}),
    )
    with pytest.raises(LaunchRefused, match="store of trusted"):
        launch_session.settle_boundary(side, None, **arguments, **extra)  # type: ignore[arg-type]

    redirect(side, evil_gitdir(tmp_path / "built"))
    with pytest.raises(LaunchRefused, match="redirected"):
        launch_session.settle_boundary(side, None, **arguments, **extra)  # type: ignore[arg-type]


def test_a_container_start_refuses_before_any_broker(
    clone: Path, tmp_path: Path, own_store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Worker launches reach the engine here without a boundary preflight."""
    from lup.launch import container

    side = clone / "tree" / "side"
    monkeypatch.setattr(container, "resolved_agent_clis", unreachable)
    arguments = {
        "image": None,
        "manifest": None,
        "root": side,
        "editor_rendezvous": None,
        "credential": None,
        "login": None,
        "engine": object(),
        "lease": Lease(),
    }
    with pytest.raises(Reached):
        container.contained_argv(**arguments)  # type: ignore[arg-type]
    exposed = {**arguments, "lease": Lease(writable={own_store: "x"})}
    with pytest.raises(LaunchRefused, match="store of trusted"):
        container.contained_argv(**exposed)  # type: ignore[arg-type]

    redirect(side, evil_gitdir(tmp_path / "built"))
    with pytest.raises(LaunchRefused, match="redirected"):
        container.contained_argv(**arguments)  # type: ignore[arg-type]
