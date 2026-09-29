"""A dispatcher run where its launch record is held, as a contained launch holds it.

The deployed dispatcher believes a ledger's claim to a container only through
a read-only mount of the ledger's directory, which it reads off its own mount
table. So a test of a contained session's answers runs the dispatcher in a
user and mount namespace of its own, with that one directory bound read-only
over itself -- the hold a contained launch makes -- and starts it with the
namespace's capabilities dropped, as the container's entrypoint does. A host
refusing an unprivileged namespace cannot measure this, and says so.
"""

import shutil
from functools import cache
from pathlib import Path

import pytest
import sh


def held_argv(directory: Path, argv: list[str]) -> list[str]:
    """``argv`` run where ``directory`` is a read-only mount of itself."""
    return [
        "unshare",
        "--user",
        "--map-current-user",
        "--keep-caps",
        "--mount",
        "--",
        "sh",
        "-c",
        'mount --bind "$0" "$0" && mount -o remount,bind,ro "$0" && '
        'exec setpriv --inh-caps=-all --ambient-caps=-all -- "$@"',
        str(directory),
        *argv,
    ]


@cache
def holding_refused() -> str:
    """Why this host cannot hold a directory for a test, or nothing where it can."""
    if shutil.which("unshare") is None or shutil.which("setpriv") is None:
        return "unshare or setpriv is not on PATH"
    try:
        sh.Command("unshare")(
            "--user", "--map-current-user", "--keep-caps", "--mount", "--", "true"
        )
    except sh.ErrorReturnCode as refused:
        return f"this host refuses an unprivileged mount namespace: {refused.stderr!r}"
    return ""


def holding() -> None:
    """Skip, saying why, where a launch record cannot be held for a test."""
    if refused := holding_refused():
        pytest.skip(refused)
