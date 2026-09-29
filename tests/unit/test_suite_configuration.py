"""What this suite's conftest promises every test in it.

Measured from inside a test, where the autouse fixtures no test asks for are
already applied.
"""

import os
import threading
import warnings

import pytest
import sh

from lup.harness.environment import launcher_decided_names
from lup.providers.identity import runtime_decided_names


@pytest.mark.parametrize("foreground", [False, True])
def test_a_fork_beside_a_running_thread_warns_nothing(foreground: bool) -> None:
    """`sh` forks, and Python warns about every fork a threaded process makes.

    The warning names a hazard `sh` does not have — it execs straight after
    forking — and a suite that runs a pool beside it printed hundreds of them
    per run, burying the warnings that were news. A foreground call forks
    through `os.spawnve`, so its warning names `os` rather than `sh`.
    """
    release = threading.Event()
    held = threading.Thread(target=release.wait)
    held.start()
    try:
        with warnings.catch_warnings(record=True) as caught:
            sh.Command("true")(_fg=foreground)
    finally:
        release.set()
        held.join()

    assert [str(warning.message) for warning in caught] == []


def test_no_session_variable_survives_into_the_suite() -> None:
    """Whatever session runs this suite, none of its variables reach a test.

    Written against an incident: a test here read the configuration
    directory its runtime exported for the live session, and wrote that
    session's own settings through it.
    """
    survivors = [
        name
        for name in os.environ
        if name in launcher_decided_names({name: ""})
        or name in runtime_decided_names({name: ""})
    ]

    assert survivors == []
