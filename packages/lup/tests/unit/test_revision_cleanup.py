"""`harness clean` lists the Codex revisions contained sessions ran their hooks from.

A contained Codex session's hooks run from a revision written on the host and
held read-only in its home, one directory per revision a session ran. A
revision no running container binds is finished: the next launch that needs it
writes it again from source.
"""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import sh

import lup.devtools.harness.clean as clean
from lup.harness.image import Podman
from lup.launch.environments import revisions_home
from lup.launch.superseded import SupersededFile


class Engine:
    """An engine whose one running container binds the sources it is given."""

    def __init__(self, bound: list[Path]) -> None:
        self.bound = bound
        self.done: list[list[str]] = []

    def __call__(self, *arguments: str) -> str:
        match list(arguments):
            case ["ps", "-q"]:
                return "c0ffee\n"
            case ["inspect", "--format", _, "c0ffee"]:
                return json.dumps([{"Source": str(path)} for path in self.bound])
            case words:
                self.done.append(words)
                return ""


def snapshots(home: Path, *names: str) -> list[Path]:
    made = [home / name for name in names]
    for directory in made:
        (directory / "0.1.0+codex.abc").mkdir(parents=True)
        (directory / "0.1.0+codex.abc" / "hooks.json").write_text("{}")
    return made


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    return revisions_home()


def test_a_revision_no_running_container_binds_is_finished(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    held, loose = snapshots(home, "held0123456789ab", "loose0123456789a")
    (home / ".staging-x").mkdir()
    monkeypatch.setattr(sh, "Command", lambda binary: Engine([held]))

    listed = clean.revisions(Podman())

    assert {item.name: item.finished for item in listed} == {
        str(held): False,
        str(loose): True,
    }
    assert "a running container holds it" in listed[0].why


def test_without_an_engine_nothing_is_called_finished(home: Path) -> None:
    (only,) = snapshots(home, "only0123456789ab")

    (listed,) = clean.revisions(None)

    assert listed.name == str(only) and not listed.finished


def test_yes_removes_the_finished_revisions_and_keeps_the_held(
    tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    held, loose = snapshots(home, "held0123456789ab", "loose0123456789a")
    monkeypatch.setattr(sh, "Command", lambda binary: Engine([held]))
    listed = clean.revisions(Podman())

    said = clean.cleaned(tmp_path, listed, None, [], None, clean_kept(tmp_path))

    assert held.is_dir() and not loose.exists()
    assert str(loose) in said[-1].text


def clean_kept(tmp_path: Path) -> clean.Kept:
    return clean.Kept(
        SupersededFile(tmp_path / "state"), timedelta(days=14), datetime.now(UTC)
    )


def test_the_listing_has_a_heading_for_revisions(home: Path) -> None:
    snapshots(home, "only0123456789ab")

    lines = clean.listing(clean.revisions(None), None)

    assert "revisions:" in lines
