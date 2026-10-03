"""Local native-executable launching and its request/status vocabulary.

Defines the ``ProcessLauncher`` seam and implements it for everything that
runs a native executable and reads back what it printed: the resolver's git
operations and verification commands, and the base-freshness probe a launch
settles. An interactive session is not one of them: a launch hands the
terminal to the CLI instead, through :mod:`lup.launch.foreground`. Its
request and status models live here because launchers are their only
producers.
"""

import os
import sys
from abc import ABC, abstractmethod
from collections.abc import Callable
from pathlib import Path
from typing import TextIO

import sh
from pydantic import BaseModel

from lup.types import EnvVars


class LaunchRequest(BaseModel, frozen=True):
    arguments: list[str]
    cwd: Path
    environment: EnvVars = {}

    stream: bool = False
    """Whether the child's output also reaches this process's own terminal.

    Off by default, because most launches here are probes run for an answer
    the caller reads out of :class:`ExitStatus`, and printing that answer on
    the way past would only duplicate it.

    Worth turning on for the few that a person waits through. A command with
    a network or a git hook behind it can take minutes, and a captured one
    says nothing at all while it does — indistinguishable, from the outside,
    from a process that has stopped. Captured either way, so a caller reading
    ``stderr`` for a diagnostic does not have to choose between reading it
    and letting whoever is watching see progress.
    """


class ExitStatus(BaseModel, frozen=True):
    code: int
    stdout: str = ""
    stderr: str = ""


class ProcessLauncher(ABC):
    """Launch one concrete process boundary."""

    @abstractmethod
    def launch(self, request: LaunchRequest) -> ExitStatus:
        """Launch with typed arguments and environment."""


def decoded_output(value: bytes | str | None) -> str:
    """Decode output captured by ``sh`` without discarding framing."""
    if value is None:
        return ""
    return (
        value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value
    )


class LocalProcessLauncher(ProcessLauncher):
    """Run an explicitly named executable with captured output and no shell.

    Output is captured through pipes, never a pseudo-terminal, so children
    that branch on ``isatty`` (pagers, colorizers) emit plain machine output.

    A working directory that is not there is an answer rather than a crash:
    the child could not be run where it was asked to, which is the 126 a
    shell gives such a command, with the reason on stderr — so a probe asked
    of a path that no longer exists reads as one whose program said no.
    """

    def launch(self, request: LaunchRequest) -> ExitStatus:
        if not request.arguments:
            raise ValueError("a launch request must name an executable")
        executable, *arguments = request.arguments
        if not request.cwd.is_dir():
            return ExitStatus(
                code=126,
                stderr=f"cannot run {executable} in {request.cwd}: no such directory\n",
            )
        command = sh.Command(executable)
        environment = {
            **os.environ,  # lup: ignore[os-environ] — inherit the process boundary
            **request.environment,
        }
        shown: list[str] = []
        complained: list[str] = []

        def tee(buffer: list[str], sink: TextIO) -> Callable[[str], None]:
            """Keep each chunk the child writes, and show it as it arrives.

            Flushed per chunk because the point of showing it is that somebody
            is waiting: output held in a buffer until the command exits tells
            them exactly what capturing it told them, one exit later.
            """

            def written(chunk: str) -> None:
                buffer.append(chunk)
                sink.write(chunk)
                sink.flush()

            return written

        # ``None`` is what sh takes for its own buffering, so a launch that
        # asked for nothing gets exactly the collection it always did.
        result = command(
            *arguments,
            _cwd=str(request.cwd),
            _env=environment,
            _ok_code=list(range(256)),
            _return_cmd=True,
            _tty_out=False,
            _out=tee(shown, sys.stdout) if request.stream else None,
            _err=tee(complained, sys.stderr) if request.stream else None,
        )
        if not isinstance(result, sh.RunningCommand):
            raise RuntimeError(f"process {executable!r} did not return command state")
        if result.exit_code is None:
            raise RuntimeError(f"process {executable!r} has no terminal exit status")
        if request.stream:
            return ExitStatus(
                code=result.exit_code,
                stdout="".join(shown),
                stderr="".join(complained),
            )
        return ExitStatus(
            code=result.exit_code,
            stdout=decoded_output(result.stdout),
            stderr=decoded_output(result.stderr),
        )
