"""Minting a launch's boundary, measuring it, and writing it down for the session.

The launcher is the only thing that knows all three parts at once: what the
project declared, what this particular launch built, and what the probes came
back with. The compiled dispatcher inside the session knows none of them -- it
runs as a bare script, long after generation, with no way to ask the container
runtime anything -- so the launch writes the answer down and the session reads
it back. That is the same arrangement the mount table already uses, for the
same reason.

What the ledger stands in place of is a variable. Containment spelled as
``LUP_CONTAINED``, a constant baked into the image, answers yes for any
container built from that image, for a bare ``run`` holding none of the
lease, and -- since a launcher forwards its own environment -- for an
uncontained session started from a shell that happened to export it. Keyed by a
value minted here, the ledger answers only for the launch that wrote it.

The sentinels are deliberately not secrets, and the argv carries one. They
discriminate *launches*, not principals: what they defeat is a constant and an
inherited variable, which is the whole of the accidental case this layer
governs. An agent determined to forge one could read it out of `ps`, and the
threat model says so -- this is a guardrail over a fallible agent, not an
isolation product over a hostile one.
"""

import json
import stat
import sys
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from secrets import token_hex

from pydantic import BaseModel, Field

from lup.execution.shell import git
from lup.harness.requirements import SENTINEL_VARIABLE
from lup.policy.boundary import BoundaryPreflight
from lup.policy.snapshots import (
    DestinationPolicy,
    RepositoryPolicyAuthority,
    snapshot_directory,
)
from lup.types import EnvVars

# lup: ignore[constant-declaration] — an identity this repository defines, and
# the one half of the handshake no probe reads: the launcher writes a ledger
# named for a value and the dispatcher believes the ledger this names
NONCE_VARIABLE = "LUP_BOUNDARY_NONCE"
"""Which ledger this session's dispatcher is entitled to believe."""

# lup: ignore[constant-declaration] — the launcher-owned address of its nonce ledger
ROOT_VARIABLE = "LUP_BOUNDARY_ROOT"
"""The launch checkout, independent of a tool or preflight command's cwd."""


class LaunchSentinels(BaseModel, frozen=True):
    """The values one launch mints to tell its own sides apart.

    Independent rather than derived. The whole question a placement probe asks
    is which side answered, and two values either can be computed from are one
    value wearing two names.
    """

    nonce: str = Field(default_factory=lambda: token_hex(16))
    inside: str = Field(default_factory=lambda: token_hex(16))
    host: str = Field(default_factory=lambda: token_hex(16))

    def within(self) -> EnvVars:
        """What a process inside the boundary is given, for the argv that starts it."""
        return {SENTINEL_VARIABLE: self.inside, NONCE_VARIABLE: self.nonce}

    def outside(self) -> EnvVars:
        """What a process on the host side is given: a host probe, an uncontained session."""
        return {SENTINEL_VARIABLE: self.host, NONCE_VARIABLE: self.nonce}


def ledger_directory(root: Path, ledger: str = ".lup/preflight") -> Path:
    """Where every launch in this checkout writes its measurement, one file each."""
    return root / ledger


def ledger_path(root: Path, nonce: str, ledger: str = ".lup/preflight") -> Path:
    """Where this launch's measurement is written, named for the launch.

    Named rather than shared, and that is not tidiness. One file per checkout
    is a file two concurrent sessions overwrite for each other, and the loser
    reads a boundary belonging to a launch that is not its own -- which is the
    same class of wrong answer as an inherited variable, arrived at from the
    other direction.
    """
    return ledger_directory(root, ledger) / f"{nonce}.json"


def mount_table(root: Path, table: str = ".lup/boundary.json") -> Path:
    """Where a contained launch writes the mount table its gate explains refusals from."""
    return root / table


def launch_record(root: Path) -> list[Path]:
    """What a launch writes under ``.lup/`` for its session's gates to believe, made to exist.

    The measurement ledger, the destination policies a launch accepted, and
    the mount table. Each is written on the host -- by the launcher, or by
    ``harness policy-refresh`` from an operator's terminal -- and only read
    inside, by the hooks: which boundary this session stands behind, which
    policy judges a destination repository, why a write was refused. A
    session that could write them could copy in a ledger naming a boundary
    of its own, so a container holds them read-only; nothing a session
    legitimately does writes them.

    Beside them and deliberately not held, each with a writer inside the
    session: the question relay and the review claims the hooks post and
    spend, the approvals record they note what ran in -- observations that
    grant nothing -- and the corpora and counters the hooks keep.

    Made to exist here, because a bind whose source is missing refuses the
    whole container: both directories, and the table's file, left as it
    stands where there is one.
    """
    directories = [ledger_directory(root), snapshot_directory(root)]
    for directory in directories:
        directory.mkdir(parents=True, exist_ok=True)
    table = mount_table(root)
    table.touch(exist_ok=True)
    return [*directories, table]


def record_preflight(
    preflight: BoundaryPreflight,
    sentinels: LaunchSentinels,
    root: Path,
    launch: Sequence[str] | None = None,
    destination_policies: Sequence[DestinationPolicy] = (),
    read_only_roots: Sequence[Path] = (),
    destination_authorities: Sequence[RepositoryPolicyAuthority] = (),
) -> Path:
    """Write what this launch measured, in the shape a bare script can read.

    Every value is a list of strings, which is not the shape a pydantic dump
    would take and is deliberately the shape the dispatcher's existing reader
    already validates. The half that reads this may reach nothing but a pinned
    standard library, so what it is handed has to be checkable by hand -- and a
    reader that has to understand two shapes is a reader with a branch nobody
    exercised.

    Written on every launch, contained or not. A launch that writes nothing
    leaves whatever a contained launch wrote last standing as this session's
    answer.

    ``launch`` is the launcher's own invocation, recorded so a session can
    spell its own reopening. A mount registered mid-session takes effect only
    at the next launch, and the session proposing it is the one that knows why
    -- but without this record it has no way to say *which* launch to repeat,
    because nothing else remembers the profile, sandbox, and flags that opened
    it. Defaulted from the process argv, which is the invocation, rather than
    a reconstruction that would drift from it.
    """
    boundary = preflight.boundary
    written = ledger_path(root, sentinels.nonce)
    written.parent.mkdir(parents=True, exist_ok=True)
    written.write_text(
        json.dumps(
            {
                "profile": [boundary.name],
                "contained": ["yes"] if boundary.contained else [],
                "unjudged_ambient": [boundary.unjudged_ambient],
                "delivered": [
                    entry.capability for entry in preflight.evidence if entry.delivered
                ],
                "blocked": preflight.blocked(),
                "writable_roots": [str(item) for item in boundary.writable_roots],
                "managed_roots": [str(item) for item in boundary.managed_roots],
                "read_only_roots": [str(item.resolve()) for item in read_only_roots],
                "destination_policies": [
                    row.model_dump_json() for row in destination_policies
                ],
                "destination_authorities": [
                    row.model_dump_json() for row in destination_authorities
                ],
                "launch": list(launch if launch is not None else sys.argv[1:]),
            },
            indent=2,
        )
    )
    return written


def reopened(
    launch: Sequence[str],
    resume: str = "--continue",
    resuming: tuple[str, ...] = ("--continue", "--resume", "--session"),
) -> list[str]:
    """The recorded launch, spelled to reach the same conversation again.

    A launch that was already a resumption is repeated as it stands; any other
    gains ``resume``, because repeating it verbatim would open a fresh session
    and orphan the conversation that asked to be reopened.
    """
    argv = list(launch)
    if any(flag in argv for flag in resuming):
        return argv
    return [*argv, resume]


def retire_mount_table(root: Path, ledger: str = ".lup/boundary.json") -> None:
    """Take away a mount table that describes a boundary this launch is not behind.

    ``record_boundary`` writes the table on every *contained* launch, so
    without this an uncontained launch in the same checkout reads whatever
    the last contained one left. That is the reader's worst case: a refusal
    attributed to a read-only mount that is not there, which teaches an
    agent to reach for the host when the bug is its own. Its docstring names
    staleness as the hazard and per-launch rewriting as the answer, and this
    is that answer for the posture with no table to write.

    Emptied where it stands rather than removed, since a contained session
    in this checkout holds the file read-only by its inode, and a file
    bind is detached when the host replaces or unlinks the file under it.
    An empty table reads as none.
    """
    table = mount_table(root, ledger)
    if table.is_file():
        table.write_text("", encoding="utf-8")


def release_ledger(root: Path, nonce: str) -> None:
    """Take this launch's measurement away when its session ends.

    On the way out rather than on the way in, and the difference is a session
    somebody else is running. A launch that swept every ledger but its own
    would be correct exactly once -- with a second session open, it takes away
    a boundary that session's dispatcher is still reading, and that session
    falls back to no boundary for the rest of its life. Fail-closed, and wrong.

    Missing is not a failure. A ledger already gone is one an operator tidied,
    a container removed with its mount, or a previous exit removed twice.
    """
    ledger_path(root, nonce).unlink(missing_ok=True)


def sweep_ledgers(root: Path, older_than: timedelta = timedelta(days=7)) -> int:
    """Remove measurements left behind by launches that never got to exit.

    The crash path, and only that. A session that ends normally takes its own
    ledger with it, so anything still here after a week belongs to a launch
    that was killed -- and a window measured in days cannot reach a session
    somebody is still using, which is the property that makes sweeping safe to
    do on the way in.
    """
    directory = ledger_directory(root)
    if not directory.is_dir():
        return 0
    cutoff = datetime.now(UTC) - older_than
    stale = [
        item
        for item in directory.glob("*.json")
        if datetime.fromtimestamp(item.stat().st_mtime, UTC) < cutoff
    ]
    for item in stale:
        item.unlink()
    return len(stale)


def exclude_sandbox_placeholders(root: Path) -> list[str]:
    """Keep the runtime sandbox's mount targets out of ``git status``, by name.

    The runtime's own sandbox holds a set of dotfiles read-only over the
    checkout -- shell profiles, ``.gitconfig``, ``.mcp.json``, the ``.claude/``
    entries a command could plant a hook in -- by binding an empty file over
    each. Where the file is absent, bubblewrap creates the mount target
    itself: zero bytes, mode 0444, and on the host, because the checkout is
    bound writable. The mount goes with the sandbox and the file stays,
    untracked and matched by no ignore rule, in every worktree a sandboxed
    session entered -- measured as eleven in one checkout and twenty-one in
    its sibling, one ``git add -A`` away from a commit over harness-owned
    paths.

    Nothing in this repository writes them, so nothing here can stop them.
    What it can do is take away the one harm they do, and ``info/exclude`` is
    where: per clone, unversioned, and read by every worktree of the
    repository. The names are read off the disk rather than kept in a list,
    because the list is the sandbox's and moves with it; the signature --
    untracked, empty, and exactly the mode bubblewrap gives a target it
    created -- is what tells a placeholder from a file somebody meant.

    Returns what it added, so a launch can say so once and a second launch
    says nothing. A launch opened outside any repository has no status to
    keep them out of, and adds nothing.
    """
    untracked = git.lines(
        "-C", str(root), "ls-files", "--others", "--exclude-standard", _ok_code=[0, 128]
    )
    placeholders = [
        name
        for name in untracked
        if (found := root / name).is_file()
        and (held := found.stat()).st_size == 0
        and stat.S_IMODE(held.st_mode) == 0o444
    ]
    if not placeholders:
        return []
    common = git.out(
        "-C", str(root), "rev-parse", "--path-format=absolute", "--git-common-dir"
    ).strip()
    exclude = Path(common) / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    held_lines = (
        exclude.read_text(encoding="utf-8").splitlines() if exclude.exists() else []
    )
    added = [name for name in placeholders if f"/{name}" not in held_lines]
    if added:
        exclude.write_text(
            "\n".join([*held_lines, *(f"/{name}" for name in added), ""]),
            encoding="utf-8",
        )
    return added
