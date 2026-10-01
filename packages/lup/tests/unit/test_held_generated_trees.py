"""A contained session may be denied the generated trees its runtime runs from.

``OuterContainer(hold_generated=True)`` binds each runtime's generated tree
read-only into the container — the plugin whose hooks judge the session, the
project settings and guidance the runtime reads — so the session cannot change
what judges it. What it costs is regenerating from inside, which is said
before anything is written, naming the host command, rather than dying on a
busy device halfway through a rename.
"""

import os
from pathlib import Path
from typing import Any

import pytest
import typer

import lup.devtools.harness.drift as drift
import lup.harness.generate as generation
import lup.harness.materialization as materialization
import lup.launch.session as launch_session
import lup.devtools.harness.launch as launch
import lup.providers.codex.launch as codex_launch
from lup.formats.banner import REGENERATE_COMMAND
from lup.harness.generate import GeneratedTreesHeld, ProjectContent
from lup.harness.materialization import held_read_only
from lup.harness.reconciliation import ReconciliationProposal
from lup.launch.container import held_lease
from lup.launch.declaration import LaunchSandbox, OuterContainer
from lup.providers.claude.launch import claude_held_trees
from lup.providers.codex.launch import codex_held_trees
from lup.providers.codex.marketplace import MARKETPLACE_MANIFEST, CodexMarketplace
from lup.sandbox.rail import Lease, same_path
from tests.unit.harness_launch import (
    CONTAINER,
    checkout,
    composition,
    profiles,
    stub_host,
)


def made(root: Path, *relative: str) -> list[Path]:
    """Each path under ``root``, a directory where it ends in ``/``, a file otherwise."""
    paths: list[Path] = []
    for spelled in relative:
        path = root / spelled
        if spelled.endswith("/"):
            path.mkdir(parents=True, exist_ok=True)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("generated\n", encoding="utf-8")
        paths.append(path)
    return paths


def test_claude_holds_its_plugin_whole_and_its_settings_and_guidance_alone(
    tmp_path: Path,
) -> None:
    plugin, marketplace, settings, guidance = made(
        tmp_path,
        ".claude/plugins/lup/",
        ".claude/plugins/.claude-plugin/marketplace.json",
        ".claude/settings.json",
        ".claude/CLAUDE.md",
    )
    made(tmp_path, ".claude/plugins/overlay/", ".claude/settings.local.json")

    assert claude_held_trees(tmp_path, plugin) == [
        plugin,
        marketplace,
        settings,
        guidance,
    ]


def test_a_plugin_built_outside_the_checkout_is_not_the_checkout_s_to_hold(
    tmp_path: Path,
) -> None:
    (settings,) = made(tmp_path, ".claude/settings.json")

    assert claude_held_trees(tmp_path, tmp_path.parent / "built") == [settings]
    assert claude_held_trees(tmp_path, None) == [settings]


def test_codex_holds_the_plugin_it_installs_from_and_its_project_config(
    tmp_path: Path,
) -> None:
    held = made(
        tmp_path,
        ".codex/plugins/lup/",
        ".codex/rules/lup.rules",
        ".codex/agents/",
        ".codex/config.toml",
        ".agents/plugins/marketplace.json",
        "AGENTS.md",
    )
    made(
        tmp_path, ".codex/config.local.toml", ".codex/skills/", ".codex/rules/own.rules"
    )
    offered = CodexMarketplace(
        name="project", plugin="lup", source=tmp_path / ".codex" / "plugins" / "lup"
    )

    assert codex_held_trees(tmp_path, offered) == held
    assert codex_held_trees(tmp_path, None) == held[2:]


def test_a_held_tree_is_bound_read_only_and_its_parents_pinned(tmp_path: Path) -> None:
    plugin = tmp_path / ".claude" / "plugins" / "lup"
    lease = Lease(writable=same_path([tmp_path]))

    held = held_lease(tmp_path, lease, trees=[plugin])

    assert plugin in held.read_only
    assert tmp_path / ".claude" / "plugins" in held.writable


def test_a_tree_the_lease_does_not_write_is_not_held(tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere" / ".claude" / "plugins" / "lup"

    held = held_lease(tmp_path / "work", Lease(), trees=[elsewhere])

    assert elsewhere not in held.read_only


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return checkout(tmp_path)


def opened_with(
    root: Path, monkeypatch: pytest.MonkeyPatch, runtime: str, hold: bool | None
) -> list[Path]:
    """The trees a stubbed launch hands the container to hold."""
    stub_host(monkeypatch, root)
    handed: dict[str, Any] = {}

    def contained(*_arguments: object, **keywords: Any) -> list[str]:
        handed.update(keywords)
        return list(CONTAINER)

    monkeypatch.setattr(launch_session, "contained_argv", contained)
    monkeypatch.setattr(codex_launch, "held_revision", lambda *_a, **_k: {})
    made(root, ".claude/plugins/lup/", ".codex/plugins/lup/", ".codex/config.toml")
    (root / MARKETPLACE_MANIFEST).parent.mkdir(parents=True, exist_ok=True)
    (root / MARKETPLACE_MANIFEST).write_text(
        '{"name": "project", "plugins": [{"name": "lup", '
        '"source": {"path": "./.codex/plugins/lup", "source": "local"}}]}',
        encoding="utf-8",
    )
    request = launch.LaunchRequest(sandbox=LaunchSandbox.OUTER, hold_generated=hold)
    if runtime == "claude":
        launch.launch_claude(composition(root, "claude"), request, profiles(), False)
    else:
        launch.launch_codex(composition(root, "codex"), request, None, False, False)
    return list(handed.get("trees", []))


@pytest.mark.parametrize("runtime", ["claude", "codex"])
def test_a_launch_holding_the_trees_hands_them_to_the_container(
    root: Path, monkeypatch: pytest.MonkeyPatch, runtime: str
) -> None:
    held = opened_with(root, monkeypatch, runtime, hold=True)

    assert root / f".{runtime}" / "plugins" / "lup" in held


@pytest.mark.parametrize("runtime", ["claude", "codex"])
def test_a_launch_that_does_not_hold_them_hands_none(
    root: Path, monkeypatch: pytest.MonkeyPatch, runtime: str
) -> None:
    assert opened_with(root, monkeypatch, runtime, hold=None) == []


def test_only_the_container_holds_trees() -> None:
    assert OuterContainer(hold_generated=True).holds_generated()
    assert not OuterContainer().holds_generated()


def test_only_the_paths_a_mount_holds_read_only_are_held(tmp_path: Path) -> None:
    plugin = tmp_path / ".claude" / "plugins" / "lup"
    free = tmp_path / "docs" / "README.md"

    held = held_read_only(
        [plugin / "hooks" / "hooks.json", free],
        mounted_read_only=lambda path: plugin in path.parents,
    )

    assert held == [plugin / "hooks" / "hooks.json"]


class Statvfs:
    """What ``os.statvfs`` answers for a mount, with its read-only flag as given."""

    def __init__(self, read_only: bool) -> None:
        self.f_flag = os.ST_RDONLY if read_only else 0


def test_a_path_not_there_yet_is_asked_of_the_mount_it_would_land_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (plugin,) = made(tmp_path, ".claude/plugins/lup/")
    asked: list[str] = []

    def statvfs(path: str) -> Statvfs:
        asked.append(str(path))
        return Statvfs(read_only=Path(path) == plugin)

    monkeypatch.setattr(os, "statvfs", statvfs)

    assert materialization.mounted_read_only(plugin / "hooks" / "hooks.json")
    assert asked == [str(plugin)]
    assert not materialization.mounted_read_only(tmp_path / "docs" / "README.md")


def test_generation_refuses_before_writing_anything_where_a_tree_is_held(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lup_template.harness.catalog import portable_harness

    plugin = tmp_path / ".claude" / "plugins"
    monkeypatch.setattr(
        generation,
        "mounted_read_only",
        lambda path: path == plugin or plugin in path.parents,
    )
    recipe = generation.claude_generation_recipe(
        tmp_path, ProjectContent(harness=portable_harness())
    )

    with pytest.raises(GeneratedTreesHeld) as refused:
        generation.generate(recipe)

    assert "read-only in this session" in str(refused.value)
    assert not plugin.exists()
    assert not (tmp_path / ".claude" / ".lup-ownership.json").exists()


def test_the_command_says_to_generate_on_the_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def held(_recipe: object) -> None:
        raise GeneratedTreesHeld([tmp_path / ".claude" / "plugins" / "lup"])

    monkeypatch.setattr(drift, "generate_target", held)
    monkeypatch.setattr(drift, "inspect_generation", lambda _recipe: drift_report())

    with pytest.raises(typer.Exit):
        drift.generate_with_report(composition(tmp_path, "claude"))

    said = capsys.readouterr().err
    assert "read-only in this session" in said
    assert f"`{REGENERATE_COMMAND}` on the host" in said


def drift_report(held: list[Path] | None = None) -> generation.DriftReport:
    """A tree whose proof is behind its source."""
    return generation.DriftReport(
        target="claude",
        ownership_present=True,
        manifest_current=False,
        proposal=ReconciliationProposal(id="p", root=Path("/"), base_digest="d"),
        held=held or [],
    )


def test_the_drift_line_says_a_held_tree_is_regenerated_on_the_host(
    tmp_path: Path,
) -> None:
    verdict = drift.DriftVerdict(
        reports=[drift_report([tmp_path / ".claude" / "plugins" / "lup"])],
        stale_repository=[],
    )

    (line,) = [line for line in verdict.summary if "→" in line]
    assert "read-only in this session" in line
    assert "on the host" in line
    assert "`uv run lup-devtools harness generate all`" in line
