"""A fetched review target must not depend on an attached worker's HEAD."""

import json
from pathlib import Path

import pytest
import sh
import typer

from lup.devtools import sync
from lup.execution.git import Repository
from tests.unit.repos import commit_file, git_in, initialized_repo


@pytest.fixture
def registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "consumer"
    initialized_repo(root, tmp_path / "hooks")
    monkeypatch.setattr(sync, "project_root", lambda: root)
    return root


@pytest.mark.parametrize("bare", [False, True])
def test_fetch_reads_remote_tip_without_moving_registered_checkout(
    registry: Path, tmp_path: Path, bare: bool, capsys: pytest.CaptureFixture[str]
) -> None:
    upstream = tmp_path / "upstream"
    remote = initialized_repo(upstream, tmp_path / "hooks")
    commit_file(remote, upstream, "file", "base", "base")
    before = remote("rev-parse", "HEAD").strip()
    remote("branch", "dev")
    clone = tmp_path / "registered"
    sh.git("clone", *(["--bare"] if bare else []), str(upstream), str(clone))
    work = clone / "tree" / "dev" if bare else clone
    if bare:
        sh.git("-C", str(clone), "worktree", "add", str(work), "dev")
    else:
        sh.git("-C", str(work), "checkout", "dev")
    (work / "dirty").write_text("keep me")
    remote("checkout", "dev")
    commit_file(remote, upstream, "file", "upstream", "upstream moved")
    after = remote("rev-parse", "HEAD").strip()
    (registry / "sync.json.local").write_text(
        json.dumps(
            {
                "projects": [
                    {
                        "name": "lib",
                        "path": str(clone),
                        "branch": "dev",
                        "last_synced_commit": before,
                    }
                ]
            }
        )
    )

    sync.fetch_cmd("lib")
    found = sync.existing_upstream(sync.find_project("lib"))

    assert found is not None
    assert Repository(Path(str(found.checkout))).answer("rev-parse", found.tip) == after
    assert Repository(Path(str(work))).answer("rev-parse", "HEAD") == before
    assert (work / "dirty").read_text() == "keep me"
    sync.status_cmd()
    assert "refs/remotes/origin/dev" in capsys.readouterr().out
    sync.mark_synced("lib", at="")
    assert sync.checkpoint(sync.find_project("lib"), found) == after


def test_unbranched_library_registration_follows_consumed_branch(
    registry: Path, tmp_path: Path
) -> None:
    upstream = tmp_path / "upstream"
    remote = initialized_repo(upstream, tmp_path / "hooks")
    commit_file(remote, upstream, "file", "base", "base")
    remote("branch", "dev")
    clone = tmp_path / "clone"
    sh.git("clone", str(upstream), str(clone))
    (registry / "pyproject.toml").write_text(
        f'[tool.uv.sources]\nlup-agents = {{git = "{upstream}", branch = "dev"}}\n'
    )

    found = sync.registered_upstream({"name": "lup", "path": str(clone)}, clone)

    assert found.tip == "refs/remotes/origin/dev"


def test_shared_checkpoint_overrides_stale_sibling_local_value(
    registry: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    consumer = git_in(registry, tmp_path / "hooks")
    commit_file(consumer, registry, "tracked", "base", "base")
    sibling = tmp_path / "sibling"
    consumer("worktree", "add", "-b", "sibling", str(sibling))
    upstream = tmp_path / "upstream"
    remote = initialized_repo(upstream, tmp_path / "hooks")
    commit_file(remote, upstream, "file", "base", "base")
    before = remote("rev-parse", "HEAD").strip()
    commit_file(remote, upstream, "file", "next", "next")
    after = remote("rev-parse", "HEAD").strip()
    declaration = json.dumps(
        {
            "projects": [
                {
                    "name": "lib",
                    "path": str(upstream),
                    "last_synced_commit": before,
                }
            ]
        }
    )
    for root in (registry, sibling):
        (root / "sync.json.local").write_text(declaration)

    sync.mark_synced("lib", at=after)
    monkeypatch.setattr(sync, "project_root", lambda: sibling)
    project = sync.find_project("lib")
    found = sync.existing_upstream(project)

    assert found is not None
    assert sync.checkpoint(project, found) == after
    assert (sibling / "sync.json.local").read_text() == declaration


def test_fetch_failure_is_not_reported_as_ready(
    registry: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    clone = tmp_path / "clone"
    git = initialized_repo(clone, tmp_path / "hooks")
    commit_file(git, clone, "file", "base", "base")
    git("remote", "add", "origin", str(tmp_path / "unreachable"))
    (registry / "sync.json.local").write_text(
        json.dumps(
            {
                "projects": [
                    {
                        "name": "lib",
                        "path": str(clone),
                        "branch": "main",
                    }
                ]
            }
        )
    )

    with pytest.raises(typer.Exit) as caught:
        sync.fetch_cmd("lib")

    assert caught.value.exit_code == 1
    assert "ready at" not in capsys.readouterr().out


def test_explicit_branch_mismatch_is_reported(
    registry: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    repo = tmp_path / "upstream"
    git = initialized_repo(repo, tmp_path / "hooks")
    commit_file(git, repo, "file", "base", "base")
    (registry / "pyproject.toml").write_text(
        f'[tool.uv.sources]\nlup-agents = {{git = "{repo}", branch = "dev"}}\n'
    )

    branch = sync.review_branch(
        {"name": "framework", "url": str(repo), "branch": "main"}, repo
    )

    assert branch == "main"
    assert "library consumes dev" in caplog.text


def test_setup_accepts_a_bare_repository(registry: Path, tmp_path: Path) -> None:
    bare = tmp_path / "source.git"
    sh.git("init", "--bare", str(bare))

    sync.setup_project("source", str(bare), branch="main")

    assert sync.find_project("source").get("path") == str(bare)


def test_local_reviews_of_distinct_clones_have_distinct_checkpoint_sources(
    registry: Path, tmp_path: Path
) -> None:
    upstream = tmp_path / "upstream"
    git = initialized_repo(upstream, tmp_path / "hooks")
    commit_file(git, upstream, "file", "base", "base")
    identities = []
    for name in ("first", "second"):
        clone = tmp_path / name
        sh.git("clone", str(upstream), str(clone))
        found = sync.registered_upstream(
            {"name": "source", "review_from": "local"}, clone
        )
        identities.append(sync.checkpoint_identity(found))

    assert identities[0] != identities[1]


def test_a_registration_cannot_fetch_a_checkout_for_another_remote(
    registry: Path, tmp_path: Path
) -> None:
    """And the refusal names the entry's own key and the command that repoints it.

    The checkout is the one the registration named, so what is wrong is the
    path rather than a clone in the cache — and the reader is told to point
    that key somewhere else rather than to correct "the registration",
    which is two files.
    """
    clone = tmp_path / "clone"
    git = initialized_repo(clone, tmp_path / "hooks")
    commit_file(git, clone, "file", "base", "base")
    git("remote", "add", "origin", str(tmp_path / "different"))
    messages: list[str] = []

    with pytest.raises(typer.Exit):
        sync.ensure_local(
            {"name": "source", "path": str(clone), "url": str(tmp_path / "wanted")},
            messages.append,
        )

    said = "\n".join(messages)
    assert "two names" in said
    assert 'point that entry\'s "path" at a checkout of it' in said
    assert "uv run lup-devtools sync setup source /path/to/repo" in said


def test_setup_checkpoint_honors_the_committed_branch(
    registry: Path, tmp_path: Path
) -> None:
    upstream = tmp_path / "upstream"
    git = initialized_repo(upstream, tmp_path / "hooks")
    commit_file(git, upstream, "file", "base", "base")
    git("checkout", "-b", "dev")
    commit_file(git, upstream, "file", "dev", "dev")
    expected = git("rev-parse", "HEAD").strip()
    git("checkout", "main")
    clone = tmp_path / "clone"
    sh.git("clone", str(upstream), str(clone))
    (registry / "sync.json").write_text(
        json.dumps(
            {
                "projects": [
                    {
                        "name": "source",
                        "branch": "dev",
                    }
                ]
            }
        )
    )

    sync.setup_project("source", str(clone), synced=True)

    project = sync.find_project("source")
    found = sync.existing_upstream(project)
    assert found is not None
    assert sync.checkpoint(project, found) == expected


def test_an_existing_cache_for_another_repository_cannot_be_read_or_mounted(
    registry: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clone = tmp_path / "cached"
    git = initialized_repo(clone, tmp_path / "hooks")
    commit_file(git, clone, "file", "base", "base")
    git("remote", "add", "origin", str(tmp_path / "different"))
    monkeypatch.setattr(sync, "cached_clone", lambda _name: clone)
    (registry / "sync.json.local").write_text(
        json.dumps(
            {
                "projects": [
                    {
                        "name": "source",
                        "url": str(tmp_path / "wanted"),
                        "mount": "ro",
                    }
                ]
            }
        )
    )

    with pytest.raises(typer.Exit):
        sync.status_cmd()
    assert sync.accessible_roots(lambda _line: None) == []
