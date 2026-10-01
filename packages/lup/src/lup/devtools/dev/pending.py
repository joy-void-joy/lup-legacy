"""Working-tree pending changes, free of entries that are not repository content.

A sandboxed shell sees masked paths where the harness has bind-mounted device
nodes over sensitive files (`.bashrc`, `.gitconfig`, `.mcp.json`), and git
reports every one of them as untracked. Any workflow that reads `git status`
directly therefore inherits a working-tree picture that is wrong in exactly the
place it matters: the set of things about to be committed. This module is the
one reader that returns the real set, so callers never have to know whether the
shell they ran in was masked.
"""

import logging
import stat
from pathlib import Path

from pydantic import BaseModel, computed_field

from lup.execution.git import Repository
from lup.devtools.utils import format_table, output_json
from lup.execution.shell import git

logger = logging.getLogger(__name__)


class PendingEntry(BaseModel):
    """One pending path with its porcelain index and worktree status codes."""

    path: str
    index_status: str
    worktree_status: str

    @computed_field
    @property
    def staged(self) -> bool:
        return self.index_status not in " ?"


class PendingResult(BaseModel):
    """Real and sandbox-masked changes reported separately."""

    branch: str
    entries: list[PendingEntry]
    masked: list[str]


def parse_porcelain(
    payload: str,
    rename_codes: str = "RC",
    status_field_width: int = 3,
) -> list[PendingEntry]:
    """Entries from ``git status --porcelain=v1 -z`` output.

    NUL framing is the machine interface: it disables the path quoting that
    would otherwise mangle non-ASCII and whitespace names. Rename and copy
    records carry a second path — the origin — which is consumed alongside its
    destination so it is never counted as a change of its own.
    """
    # lup: ignore[string-split] — Git porcelain's NUL framing
    records = [record for record in payload.split("\0") if record]
    entries: tuple[PendingEntry, ...] = ()
    skip_origin = False

    for record in records:
        if skip_origin:
            skip_origin = False
            continue
        if len(record) < status_field_width:
            logger.warning("Skipping malformed status record: %r", record)
            continue
        index_status, worktree_status = record[0], record[1]
        skip_origin = index_status in rename_codes or worktree_status in rename_codes
        entries += (
            PendingEntry(
                path=record[status_field_width:],
                index_status=index_status,
                worktree_status=worktree_status,
            ),
        )

    return list(entries)


def is_masked(path: Path) -> bool:
    """True for device, fifo, and socket entries — never repository content.

    A path that cannot be stat'd is reported as real: a deletion is a genuine
    pending change, and its target is gone by definition.
    """
    try:
        mode = path.lstat().st_mode
    except OSError:
        return False
    return not (stat.S_ISREG(mode) or stat.S_ISDIR(mode) or stat.S_ISLNK(mode))


def exclude_masked(
    entries: list[PendingEntry], root: Path, branch: str
) -> PendingResult:
    """Separate real pending changes from masked paths."""
    masked = [entry.path for entry in entries if is_masked(root / entry.path)]

    return PendingResult(
        branch=branch,
        entries=[entry for entry in entries if entry.path not in masked],
        masked=masked,
    )


def collect() -> PendingResult:
    """The true pending change set for the current worktree."""
    payload = str(git("status", "--porcelain=v1", "-z"))

    repository = Repository(Path.cwd())
    return exclude_masked(
        parse_porcelain(payload), repository.top(), repository.branch()
    )


def report(as_json: bool) -> None:
    """Print the pending change set, or a clean-tree confirmation."""
    result = collect()

    if as_json:
        output_json(result)
        return

    if result.masked:
        logger.info("Ignored %d masked path(s)", len(result.masked))

    if not result.entries:
        print(f"Working tree clean ({result.branch})")
        return

    print(
        format_table(
            headers=["STATUS", "STAGED", "PATH"],
            rows=[
                [
                    f"{entry.index_status}{entry.worktree_status}",
                    "yes" if entry.staged else "no",
                    entry.path,
                ]
                for entry in result.entries
            ],
        )
    )
