"""A program started apart from its starter: its own session, its log, its readiness."""

import os
import sys
from pathlib import Path

from lup.launch.companions import DetachedProcess, answering


def test_one_that_never_answers_is_left_running_for_its_caller_to_stop(
    tmp_path: Path,
) -> None:
    started = DetachedProcess.start(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        tmp_path,
        None,
        tmp_path / "logs" / "output.log",
        lambda: False,
        0.2,
    )

    assert not started.answered
    assert started.process.running()
    assert os.getpgid(started.process.pid) == started.process.pid
    started.process.stop(5.0)
    assert not started.process.running()


def test_its_output_is_appended_where_this_run_begins(tmp_path: Path) -> None:
    log = tmp_path / "output.log"
    log.write_text("an earlier run\n")

    started = DetachedProcess.start(
        [sys.executable, "-c", "print('this run', flush=True)"],
        tmp_path,
        {"PATH": os.defpath},
        log,
        lambda: True,
        5.0,
    )

    assert started.answered
    assert started.output == len("an earlier run\n")
    assert answering(lambda: "this run" in log.read_text(), 5.0)
    assert log.read_text().startswith("an earlier run\n")
