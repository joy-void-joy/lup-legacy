"""The gate's generated Pyright configuration outlives a run that was killed.

It has to live at the project root: Pyright resolves a base's relative
``include`` and each ``executionEnvironments`` root against the file that
declares them, so a configuration written elsewhere analyses a different tree:
extending this repository's own base from a temporary directory analyses
files outside it and reports imports missing that are not missing.

Living at the root means a run that is killed rather than returned from leaves
its file behind, because the `finally` that unlinks it never executes. They
accumulate as untracked junk every later `git status` and drift check reports.

Sweeping by age rather than wholesale is the part worth pinning. Several
sessions check this repository at once, and a sweep of every such file would
delete a configuration another session's Pyright is still reading.
"""

import os
import time
from datetime import timedelta
from pathlib import Path

from lup.devtools.dev.check import PYRIGHT_SCRATCH, sweep_pyright_scratch

HOUR = timedelta(hours=1)


def scratch(root: Path, name: str, age: timedelta = timedelta()) -> Path:
    """One generated configuration at the root, as old as a case needs it."""
    path = root / f"{PYRIGHT_SCRATCH}{name}.json"
    path.write_text("{}", encoding="utf-8")
    stamp = time.time() - age.total_seconds()
    os.utime(path, (stamp, stamp))
    return path


def test_a_killed_runs_leavings_are_removed(tmp_path: Path) -> None:
    """The failure itself: nothing unlinked it, so the next run does."""
    abandoned = scratch(tmp_path, "abandoned", age=timedelta(days=11))

    swept = sweep_pyright_scratch(tmp_path, HOUR)

    assert swept == [abandoned]
    assert not abandoned.exists()


def test_a_configuration_a_live_run_is_reading_is_left_alone(tmp_path: Path) -> None:
    """The reason this is by age and not wholesale.

    Several sessions check this repository at once. A sweep that took every
    such file would hand one session's Pyright a configuration that vanished
    mid-analysis, which is a worse failure than the junk it cleans.
    """
    live = scratch(tmp_path, "live")

    assert sweep_pyright_scratch(tmp_path, HOUR) == []
    assert live.exists()


def test_a_run_still_inside_the_window_is_somebody_else_s(tmp_path: Path) -> None:
    """The sweep never reaches back into the window a run could still be in.

    Just inside rather than exactly on it: the mtime is stamped and the sweep
    reads the clock afterwards, so a file written *at* the threshold is
    microseconds past it by the time anything asks. Asserting the exact
    boundary would be asserting the clock, and the property worth holding is
    that a run Pyright could still be executing is left alone.
    """
    running = scratch(tmp_path, "running", age=HOUR - timedelta(minutes=1))

    assert sweep_pyright_scratch(tmp_path, HOUR) == []
    assert running.exists()


def test_nothing_else_at_the_root_is_touched(tmp_path: Path) -> None:
    """The glob names this gate's own files and no other untracked thing."""
    stale = scratch(tmp_path, "stale", age=timedelta(days=2))
    for name in ("pyrightconfig.json", "CLAUDE.md", ".lup-pyright-notjson.txt"):
        (tmp_path / name).write_text("keep", encoding="utf-8")

    swept = sweep_pyright_scratch(tmp_path, HOUR)

    assert swept == [stale]
    assert sorted(path.name for path in tmp_path.iterdir()) == [
        ".lup-pyright-notjson.txt",
        "CLAUDE.md",
        "pyrightconfig.json",
    ]


def test_a_root_holding_none_sweeps_nothing(tmp_path: Path) -> None:
    """A clean checkout is the common case and costs one empty glob."""
    assert sweep_pyright_scratch(tmp_path, HOUR) == []
