"""This repository's workflow around a harness launch, as steps of the library's launch.

`harness claude|codex` hands its checkpoint, the worktree pointers, the base
and the regeneration of every tree to the declaration's ``launch()`` as
lifecycle steps, which nest around the session: each ``before`` in order,
the session, then each ``after`` in reverse, however the session ended. A
generation that launches nothing runs the regeneration and readies the home,
and checkpoints nothing.
"""

from pathlib import Path
from unittest.mock import Mock

import pytest

import lup.devtools.harness.launch as launch
import lup.providers.claude.launch as claude_launch
from lup.harness.generate import NativeHarnessComposition
from lup.launch.declaration import LaunchSandbox
from tests.unit.harness_launch import Caught, checkout, composition, profiles, stub_host


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return checkout(tmp_path)


@pytest.fixture
def caught(root: Path, monkeypatch: pytest.MonkeyPatch) -> Caught:
    monkeypatch.setattr(claude_launch, "settle_claude_theme", lambda *_a, **_k: None)
    return stub_host(monkeypatch, root)


def checkpoint(caught: Caught) -> launch.LaunchCheckpoint:
    """Record the provider a project checkpoint receives."""

    def record(*, provider: str) -> None:
        caught.events.append(f"checkpoint:{provider}")

    return record


def harnessed(
    runtime: str,
    root: Path,
    request: launch.LaunchArguments,
    generate_only: bool,
    caught: Caught | None = None,
) -> None:
    """One `harness <runtime>` over this repository's composition."""
    saved = checkpoint(caught) if caught is not None else None
    if runtime == "claude":
        launch.launch_claude(
            composition(root, runtime),
            request,
            profiles(),
            generate_only,
            checkpoint=saved,
        )
    else:
        launch.launch_codex(
            composition(root, runtime),
            request,
            None,
            generate_only,
            False,
            checkpoint=saved,
        )


@pytest.mark.parametrize("runtime", ["claude", "codex"])
@pytest.mark.parametrize("sandbox", list(LaunchSandbox))
def test_the_workflow_wraps_the_session_its_checkpoint_outermost(
    root: Path, caught: Caught, runtime: str, sandbox: LaunchSandbox
) -> None:
    """Checkpointed first and last; the pointers, the base and the trees before the host."""
    harnessed(runtime, root, launch.LaunchArguments(sandbox=sandbox), False, caught)

    assert [event for event in caught.events if event != "installed"] == [
        f"checkpoint:{runtime}",
        "pointers",
        "base",
        "generated:passing",
        "siblings",
        "ready",
        "cli",
        "close:True",
        f"checkpoint:{runtime}",
    ]


@pytest.mark.parametrize("runtime", ["claude", "codex"])
def test_generate_only_regenerates_and_readies_without_a_checkpoint(
    root: Path, caught: Caught, runtime: str
) -> None:
    harnessed(runtime, root, launch.LaunchArguments(), True, caught)

    assert caught.events == [
        "generated:reported",
        "siblings",
        *(["installed"] if runtime == "codex" else []),
    ]


def test_opening_one_runtime_generates_every_declared_tree(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """What makes launching a runtime mean what `harness generate all` means.

    A shared source moves both trees, so a launcher generating only its own
    would leave the other stale until somebody ran the selector by hand --
    surfacing as `dev check` failing on drift the session did not introduce.
    """
    generated: list[object] = []
    monkeypatch.setattr(
        launch,
        "generate_with_report",
        lambda composition, in_passing=False: generated.append(composition),
    )
    monkeypatch.setattr(
        launch,
        "generate_targets",
        lambda compositions, writers, in_passing=False: generated.extend(
            [*compositions, *writers]
        ),
    )
    opened, sibling = (
        Mock(spec=NativeHarnessComposition),
        Mock(spec=NativeHarnessComposition),
    )
    writer = Mock()

    launch.TreesGenerated(
        composition=opened, companions=[sibling], writers=[writer]
    ).before()

    assert generated == [opened, sibling, writer]


@pytest.mark.parametrize("runtime", ["claude", "codex"])
def test_a_launch_names_the_waits_it_spends_silent(
    root: Path,
    caught: Caught,
    runtime: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """One line before each quiet stretch, and none when only generation was asked."""
    request = launch.LaunchArguments(sandbox=LaunchSandbox.INNER)
    harnessed(runtime, root, request, True)
    assert "checking the host" not in capsys.readouterr().out

    harnessed(runtime, root, request, False)
    said = [
        line
        for line in capsys.readouterr().out.splitlines()
        if line.endswith(("opens against", "checking the host"))
    ]

    assert said[0] == "regenerating what this session opens against"
    assert said[1].endswith("checking the host")
