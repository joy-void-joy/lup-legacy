"""A session is its runtime's process, which a reader that can see it asks directly.

A beat alone would read a stopped session as running, because a beat comes
from whichever process carries the session's id — a tool server under the
runtime, or a runtime started from the session's own shell, which inherited
it. What is asserted is the process
itself: one recorded by its id and start time, alive exactly while that same
process runs, and unknown rather than guessed where the reader is in another
process namespace; and that a stdio server finds its runtime as the process
feeding its standard input, through whatever wrapper started it.
"""

import json
import os
import socket
import subprocess
import sys
from pathlib import Path

from lup.coordination.bare.runtime import (
    Runtime,
    ancestry,
    process_scope,
    runtime_alive,
    runtime_of,
)

PROBE = (
    "import json; from lup.coordination.bare.runtime import stdin_runtime; "
    "print(json.dumps(stdin_runtime()))"
)
"""A server's own reading of its runtime, printed where the test can take it."""


def exited() -> Runtime:
    """A runtime that ran, recorded while it did, and has since stopped."""
    sleeper = subprocess.Popen(["sleep", "60"])
    recorded = runtime_of(sleeper.pid)
    sleeper.kill()
    sleeper.wait()
    return recorded


def served(stdin: int | None, shim: bool = False) -> Runtime:
    """What a server started with *stdin* reads its runtime as, through a shell where *shim*."""
    probe = [sys.executable, "-c", PROBE]
    command = ["sh", "-c", '"$@"; :', "shim", *probe] if shim else probe
    done = subprocess.run(
        command, stdin=stdin, capture_output=True, text=True, check=True
    )
    [line] = done.stdout.splitlines()
    return Runtime(**json.loads(line))


def test_a_runtime_recorded_now_is_alive_to_a_reader_beside_it() -> None:
    mine = runtime_of(os.getpid())

    assert mine.get("pid") == os.getpid()
    assert mine.get("started")
    assert mine.get("scope") == process_scope()
    assert runtime_alive(mine, process_scope()) is True


def test_a_runtime_that_stopped_is_not_alive() -> None:
    assert runtime_alive(exited(), process_scope()) is False


def test_a_reused_pid_is_not_the_runtime_that_was_recorded() -> None:
    """The start time is what tells the process from the next one given its id."""
    mine = runtime_of(os.getpid())
    reused = Runtime(pid=os.getpid(), started="1", scope=mine.get("scope", ""))

    assert runtime_alive(reused, process_scope()) is False


def test_a_runtime_in_another_namespace_is_unknown_rather_than_guessed() -> None:
    """A pid means nothing outside the namespace that gave it."""
    mine = runtime_of(os.getpid())
    elsewhere = Runtime(
        pid=os.getpid(), started=mine.get("started", ""), scope="another-namespace"
    )

    assert runtime_alive(elsewhere, process_scope()) is None
    assert runtime_alive(mine, "") is None
    assert runtime_alive(Runtime(), process_scope()) is None


def test_ancestry_climbs_from_a_process_to_those_that_started_it() -> None:
    chain = list(ancestry(os.getpid()))

    assert chain[0] == os.getpid()
    assert chain[1] == os.getppid()
    assert 1 not in chain


def test_a_server_reads_its_runtime_as_the_process_feeding_its_socket() -> None:
    """What Claude Code gives a stdio server: one end of a socket pair it holds."""
    ours, theirs = socket.socketpair()
    with ours, theirs:
        found = served(theirs.fileno())

    assert found.get("pid") == os.getpid()
    assert found.get("started") == runtime_of(os.getpid()).get("started")


def test_a_server_reads_its_runtime_through_a_wrapper_sharing_its_input() -> None:
    """A shell or `uv run` between them inherits the wire rather than feeding it."""
    ours, theirs = socket.socketpair()
    with ours, theirs:
        found = served(theirs.fileno(), shim=True)

    assert found.get("pid") == os.getpid()


def test_a_server_reads_its_runtime_as_the_process_feeding_its_pipe() -> None:
    """What Codex gives a stdio server: a pipe whose writing end it holds."""
    reading, writing = os.pipe()
    try:
        found = served(reading, shim=True)
    finally:
        os.close(reading)
        os.close(writing)

    assert found.get("pid") == os.getpid()


def test_a_process_with_no_wire_reads_its_parent_as_its_runtime(
    tmp_path: Path,
) -> None:
    """Started by hand from a terminal or a file, the parent is all there is to answer."""
    source = tmp_path / "empty"
    source.write_text("", encoding="utf-8")
    with source.open("rb") as handle:
        found = served(handle.fileno())

    assert found.get("pid") == os.getpid()
