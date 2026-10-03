"""Git checkpoint for retained conversation deliveries."""

import logging
from collections.abc import Sequence
from pathlib import Path

import sh
import typer

from lup.devtools.utils import decode_stderr
from lup.diagnostics import refuse
from lup.execution.shell import git

logger = logging.getLogger(__name__)


def checkpoint_delivery(
    root: Path, destinations: Sequence[Path], *, provider: str
) -> str | None:
    """Commit every retained input Git does not ignore, as one checkpoint.

    One command run reaches the history as one commit however many URLs it was
    given, and a destination Git ignores or holds outside the repository is
    reported and left out rather than force-added.
    """
    inside: tuple[str, ...] = ()
    for destination in destinations:
        try:
            inside += (str(destination.resolve().relative_to(root.resolve())),)
        except ValueError:
            typer.echo(f"{destination} is outside the repository; not committed.")
    try:
        tracked: tuple[str, ...] = ()
        for relative in inside:
            ignored = git.out(
                "check-ignore",
                "--",
                relative,
                _cwd=str(root),
                _ok_code=[0, 1],
            )
            if ignored:
                typer.echo(f"{relative} is gitignored; not committed.")
                continue
            tracked += (relative,)
        if not tracked:
            return None
        git.add("-A", "--", *tracked, _cwd=str(root))
        staged = git.out(
            "diff",
            "--cached",
            "--name-only",
            "--",
            *tracked,
            _cwd=str(root),
            _ok_code=[0, 1],
        )
        if not staged:
            return None
        git.commit(
            "-m",
            f"data(conversations): retain {provider}",
            "--",
            *tracked,
            _cwd=str(root),
        )
        revision = git.out("rev-parse", "--short", "HEAD", _cwd=str(root))
    except sh.ErrorReturnCode as error:
        logger.exception("Could not checkpoint retained conversation data")
        refuse(
            f"could not checkpoint retained conversation data: {decode_stderr(error)}",
            code=2,
        )
    typer.echo(f"Committed {provider} conversation checkpoint: {revision}")
    return revision
