"""The mount table as it actually is, read from inside the boundary.

:class:`~lup.sandbox.translation.MountTopology` is ordinarily built from a
:class:`~lup.sandbox.container.Sandbox` -- the launcher's object, answering
from configuration before any container exists, which is what lets a hook
process hold one. A command running *inside* the boundary has no such object
and cannot build one. So every devtools command that meets a refusal there has
had nothing to attribute it with, and has reported the kernel's errno instead:
`Read-only file system`, which reads as a broken disk rather than as the lease
that actually refused.

:mod:`lup.sandbox.rail` opens by arguing that a rail without attribution is
worse than no rail, and names :mod:`lup.sandbox.attribution` as what prevents
that. This is the half that was missing on the inside -- the topology that
module needs, read from the running system rather than from a declaration
nobody in here holds.

Two sources rather than one, because each answers the half it is authoritative
for. `findmnt` enumerates what is mounted where, asked for JSON so this stays a
parse rather than a split of `/proc/self/mountinfo`, whose optional fields and
octal escaping are a format. The *mode* comes from ``statvfs`` instead of from
the option list beside it: the kernel's own answer for a path, rather than a
string that has to be taken apart and that spells the flag we want as a
substring of several options we do not.
"""

import json
import os
from pathlib import Path

import sh
from pydantic import BaseModel, Field

from lup.execution.shell import findmnt
from lup.sandbox.models import Mount, MountMode
from lup.sandbox.translation import MountTopology


def refuses_writes(path: str) -> bool:
    """Whether the filesystem at this path is mounted read-only.

    Asked of the kernel rather than of a mount option string. A path that has
    gone since the table was read answers ``False``: it is not a read-only
    mount, and reporting it as one would attribute a refusal to a boundary
    that is not what removed it.
    """
    try:
        return bool(os.statvfs(path).f_flag & os.ST_RDONLY)
    except OSError:
        return False


class ObservedMount(BaseModel, frozen=True):
    """One row of `findmnt --json`, before it is a :class:`Mount`.

    Named for what it is rather than mapped straight onto :class:`Mount`: the
    field names are `findmnt`'s vocabulary, and keeping them here is what makes
    the translation below a place to look rather than a lambda.
    """

    target: str = ""
    fsroot: str = ""

    def declared(self) -> Mount:
        """This row as the topology names it.

        ``fsroot`` rather than ``source`` is the host side: `findmnt` reports
        the device with the bind's subpath appended in brackets, so the field
        already holding that subpath alone is the one that does not have to be
        taken back apart.
        """
        mode: MountMode = "ro" if refuses_writes(self.target) else "rw"
        return Mount(
            container_path=self.target,
            source=self.fsroot or self.target,
            kind="bind",
            mode=mode,
            purpose="observed on the running system",
        )


class ObservedTable(BaseModel):
    """What `findmnt --json` returns, under the one key it puts it at."""

    filesystems: list[ObservedMount] = Field(default=[])


def parsed(listed: str) -> ObservedTable:
    """The tool's output as the table, or an empty one if it is not JSON.

    Separate from the call below so a caller holding output from somewhere
    else -- a test, a recorded session -- reaches the same translation without
    running anything.
    """
    try:
        return ObservedTable.model_validate(json.loads(listed))
    except (json.JSONDecodeError, ValueError):
        return ObservedTable()


def observed_topology() -> MountTopology:
    """Every mount this process can see, as the topology attribution reads.

    Asked for a list rather than the tree `findmnt` prints by default, which
    nests every mount under the one it sits inside. A tree parses without
    complaint into a table holding one row -- the root -- so the difference
    does not surface as an error anywhere: it surfaces as a boundary that
    never attributes anything, which is indistinguishable from a boundary
    that was not involved.

    An empty topology where `findmnt` is absent or fails, which is the honest
    answer rather than a raise: attribution degrades to
    :class:`~lup.sandbox.attribution.Unattributed` against one, so a caller
    that cannot read the table reports the failure it actually had instead of
    a new one about reading mounts. That is what keeps this callable from a
    command whose subject is something else entirely.
    """
    try:
        listed = findmnt("--json", "--list", "-o", "TARGET,FSROOT")
    except (sh.ErrorReturnCode, sh.CommandNotFound):
        return MountTopology(mounts=[])
    return MountTopology(
        mounts=[row.declared() for row in parsed(str(listed)).filesystems]
    )


def bound_read_only(path: str) -> bool:
    """Whether a read-only mount sits at exactly this path in the running table.

    Asked of `findmnt` by its exit status, which answers both halves at
    once -- that the path is a mount point, and that the mount is read-only
    -- with no option list taken apart and no `\\040` in a path to unescape.
    Exact rather than covering: a file bind the host detached leaves its
    path under whatever share it sat in, and a covering reading would report
    the share's mode for a bind that is gone.
    """
    try:
        findmnt("--mountpoint", path, "--options", "ro")
    except sh.ErrorReturnCode:
        return False
    return True


def unheld(expected: list[str]) -> list[str]:
    """The read-only binds a launch recorded that the running table no longer holds."""
    return [path for path in expected if not bound_read_only(path)]


def is_mount_point(path: Path) -> bool:
    """Whether something is mounted at exactly this path.

    Asked of the mount table rather than of :meth:`pathlib.Path.is_mount`,
    which compares a directory's device number against its parent's and so
    cannot see a bind mount that came from the same filesystem. Every
    checkout a launch binds is one of those: the source is a directory on the
    same disk, the device number is unchanged, and the standard probe reports
    a mount point as an ordinary directory.

    That case is not exotic here -- it is every worktree of a contained
    session -- and the caller it matters to is deciding whether a removal
    that failed at its final `rmdir` left the work done or undone.
    """
    return str(path) in {mount.container_path for mount in observed_topology().mounts}
