"""Updating a pin preserves the explicitly configured framework repository."""

from pathlib import Path

import pytest
import sh

from lup.devtools.dev import library, scaffold, update
from lup.diagnostics import Refusal


def test_a_revision_uses_the_selected_scaffold_registration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = tmp_path / "pyproject.toml"
    manifest.write_text(
        '[project]\nname = "consumer"\ndependencies = ["lup-agents"]\n'
        "[tool.uv.sources]\nlup-agents = { workspace = true }\n"
    )
    (tmp_path / "sync.json.local").write_text(
        '{"projects":[{"name":"framework","url":"https://forge.example/team/library"}]}'
    )
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(update, "uv", lambda *args, **_kwargs: calls.append(args))
    monkeypatch.setattr(scaffold, "pinned_commit", lambda *_args: "resolved")

    assert (
        update.resolved_pin(
            tmp_path, "lup-agents", "revision", lambda _line: None, project="framework"
        )
        == "resolved"
    )
    assert library.read_git_source(tmp_path) == library.GitSource(
        url="https://forge.example/team/library", ref_kind="rev", ref="revision"
    )
    assert calls == [("lock", "--upgrade-package", "lup-agents")]


def test_an_unconfigured_revision_refuses_before_mutating_the_pin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = tmp_path / "pyproject.toml"
    original = '[project]\nname = "consumer"\ndependencies = ["lup-agents"]\n'
    manifest.write_text(original)
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(update, "uv", lambda *args, **_kwargs: calls.append(args))

    with pytest.raises(Refusal) as refused:
        update.resolved_pin(tmp_path, "lup-agents", "revision", lambda _line: None)
    assert refused.value.said["why"] == "no repository is configured for it"
    assert manifest.read_text() == original
    assert calls == []


def test_deleted_branch_refuses_before_relocking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    remote = tmp_path / "remote"
    sh.git("init", "--bare", str(remote))
    manifest = tmp_path / "pyproject.toml"
    original = (
        f'[tool.uv.sources]\nlup-agents = {{git = "{remote}", branch = "deleted"}}\n'
    )
    manifest.write_text(original)
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(update, "uv", lambda *args, **_kwargs: calls.append(args))

    with pytest.raises(Refusal) as refused:
        update.resolved_pin(tmp_path, "lup-agents", "", lambda _line: None)

    assert refused.value.said["steps"][0]["run"][-2:] == ["--branch", "<replacement>"]
    assert calls == []
    assert manifest.read_text() == original


def test_unreachable_remote_does_not_claim_branch_absence(tmp_path: Path) -> None:
    source = library.GitSource(url=str(tmp_path / "unreachable"), ref="dev")

    with pytest.raises(Refusal) as refused:
        source.require_available_branch()
    assert "whether it is gone is unknown" in refused.value.said["why"]


def test_existing_remote_branch_is_accepted(tmp_path: Path) -> None:
    git = sh.git.bake(
        "-C", str(tmp_path), "-c", "user.name=Test", "-c", "user.email=t@x"
    )
    git("init", "-b", "dev")
    git("commit", "--allow-empty", "-m", "base")

    library.GitSource(url=str(tmp_path), ref="dev").require_available_branch()


def test_update_diagnoses_deleted_pin_before_materializing_its_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A project that rooted its scaffold branch, so the pin is all that is wrong."""
    sh.git("init", "--quiet", str(tmp_path))
    sh.git("-C", str(tmp_path), "commit", "--quiet", "--allow-empty", "-m", "adopted")
    sh.git("-C", str(tmp_path), "branch", scaffold.ScaffoldSource().branch)
    remote = tmp_path / "remote"
    sh.git("init", "--bare", str(remote))
    (tmp_path / "pyproject.toml").write_text(
        f'[tool.uv.sources]\nlup-agents = {{git = "{remote}", branch = "deleted"}}\n'
    )

    def unexpected_checkout(*_args) -> Path:
        pytest.fail("must diagnose the pin before attempting its missing worktree")

    monkeypatch.setattr(update, "upstream_checkout", unexpected_checkout)

    with pytest.raises(Refusal) as refused:
        update.updated(
            tmp_path,
            scaffold.ScaffoldSource(),
            "consumer",
            "",
            "lup-agents",
            lambda _line: None,
        )
    assert refused.value.said["what"] == "deleted"
    assert "the pinned branch is gone" in refused.value.said["why"]
