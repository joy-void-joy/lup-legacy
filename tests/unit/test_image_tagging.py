"""Naming an image after the declaration it was built from, and sweeping up.

Content-addressing is what lets two checkouts share one image without anybody
asserting their toolchains match, and the cost that comes with it is that a
declaration edit leaves the old image standing. These pin both halves: what
two checkouts get, and what the sweep is willing to delete.
"""

from pathlib import Path

import pytest

from lup.launch.container import (
    checkout_tag,
    finished_tags,
    image_tag,
    state_volume_name,
)
from lup.execution.shell import git
from lup.providers.claude.login import CLAUDE_LOGIN
from lup.providers.codex.login import CODEX_LOGIN

BASE = "FROM archlinux:base\nRUN pacman -Syu --noconfirm\n"


def test_one_declaration_is_one_image_whatever_renders_it() -> None:
    """The property the whole choice was made for.

    Two worktrees declaring the same toolchain build it once and share it,
    and nothing had to assume they would -- the name is a function of what
    would be built.
    """
    assert image_tag(BASE) == image_tag(BASE)


def test_a_changed_declaration_is_a_different_image() -> None:
    """And the other half: they diverge exactly when the declaration does.

    Which is what stops two checkouts rebuilding over each other on every
    switch -- the failure a repository-wide name has, because
    `image_matches` compares the declaration digest and each finds the
    other's.
    """
    assert image_tag(BASE) != image_tag(BASE + "RUN pacman -S --noconfirm jq\n")


def test_a_checkout_name_says_nothing_about_the_declaration(tmp_path: Path) -> None:
    """The readable tag is stateable inside the manifest; the digest is not.

    The preflight has to name the image it probes, and the digest is computed
    from the manifest that preflight is part of. A name that does not depend
    on the declaration is the only one that can be written down there.
    """
    assert checkout_tag(tmp_path / "feat-thing") == "lup-agent:feat-thing"


def test_a_digest_tag_a_checkout_points_at_is_kept() -> None:
    """The whole test the sweep applies: is anybody still naming this image."""
    listing = (
        "abc123 localhost/lup-agent:77d332d7914c\nabc123 localhost/lup-agent:dev\n"
    )

    assert finished_tags(listing, keep="lup-agent:000000000000") == []


def test_a_digest_tag_nothing_points_at_is_finished() -> None:
    """A declaration edit leaves this behind, and nothing else will name it."""
    listing = "abc123 localhost/lup-agent:77d332d7914c\n"

    assert finished_tags(listing, keep="lup-agent:000000000000") == [
        "lup-agent:77d332d7914c"
    ]


def test_the_image_this_declaration_would_build_is_never_swept() -> None:
    """A sweep between a declaration edit and the next launch must not take it.

    Nothing is tagged onto it yet -- the launch that will reuse it has not
    happened -- so the only thing holding it is that it is what this
    declaration renders to.
    """
    listing = "abc123 localhost/lup-agent:77d332d7914c\n"

    assert finished_tags(listing, keep="lup-agent:77d332d7914c") == []


def test_another_project_s_images_are_left_alone() -> None:
    """The sweep names one prefix, and everything else on the host is somebody's."""
    listing = "abc123 docker.io/library/archlinux:base\nabc123 localhost/other:latest\n"

    assert finished_tags(listing, keep="lup-agent:000000000000") == []


def test_a_line_the_engine_shaped_differently_is_skipped() -> None:
    """An engine free to add a column must not turn a sweep into a crash."""
    listing = "abc123\nabc123 localhost/lup-agent:77d332d7914c extra\n\n"

    assert finished_tags(listing, keep="lup-agent:000000000000") == []


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    """A bare repository with two linked worktrees, which is the arrangement."""
    bare = tmp_path / "project.git"
    git("init", "--bare", "-q", "-b", "main", str(bare))
    git("-C", str(bare), "config", "user.email", "test@example.invalid")
    git("-C", str(bare), "config", "user.name", "Test")
    seed = tmp_path / "seed"
    git("clone", "-q", str(bare), str(seed))
    git("-C", str(seed), "config", "user.email", "test@example.invalid")
    git("-C", str(seed), "config", "user.name", "Test")
    (seed / "README.md").write_text("readme\n", encoding="utf-8")
    git("-C", str(seed), "add", "-A")
    git("-C", str(seed), "commit", "-qm", "first")
    git("-C", str(seed), "push", "-q", "origin", "HEAD:main")
    for branch in ("feat-one", "feat-two"):
        git(
            "-C",
            str(bare),
            "worktree",
            "add",
            "-q",
            str(bare / "tree" / branch),
            "-b",
            branch,
        )
    return bare


def test_every_worktree_of_one_repository_shares_a_config_home(
    repository: Path,
) -> None:
    """A state volume is named for the project, never for the branch.

    Keyed on the worktree directory, the name would be the *branch* -- so the
    documented `dev worktree create` workflow would hand every feature a
    config home created empty: default theme, trust re-seeded, every
    preference set by hand again, and a sign-in per branch once one can be
    made inside.
    """
    one = state_volume_name(repository / "tree" / "feat-one", CLAUDE_LOGIN)
    two = state_volume_name(repository / "tree" / "feat-two", CLAUDE_LOGIN)
    assert one == two == "lup-claude-project"
    codex = state_volume_name(repository / "tree" / "feat-one", CODEX_LOGIN)
    assert codex == "lup-codex-project"


def test_a_plain_checkout_and_its_worktrees_answer_the_same_name(
    tmp_path: Path,
) -> None:
    """A non-bare repository's shared directory is `.git`, which names nothing.

    Both spellings have to collapse onto the project, or a repository cloned
    the ordinary way would key its config home on the string `.git` and every
    project on the machine would share one.
    """
    root = tmp_path / "checkout"
    root.mkdir()
    git("-C", str(root), "init", "-q", "-b", "main")
    git("-C", str(root), "config", "user.email", "test@example.invalid")
    git("-C", str(root), "config", "user.name", "Test")
    (root / "README.md").write_text("readme\n", encoding="utf-8")
    git("-C", str(root), "add", "-A")
    git("-C", str(root), "commit", "-qm", "first")
    git("-C", str(root), "worktree", "add", "-q", str(tmp_path / "feat"), "-b", "feat")
    assert state_volume_name(root, CLAUDE_LOGIN) == "lup-claude-checkout"
    assert state_volume_name(tmp_path / "feat", CODEX_LOGIN) == "lup-codex-checkout"
