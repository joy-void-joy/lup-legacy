"""What this suite's conftest promises every test in it.

Measured from inside a test, where the autouse fixtures no test asks for are
already applied.
"""

import threading
import warnings

import pytest
import sh


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
