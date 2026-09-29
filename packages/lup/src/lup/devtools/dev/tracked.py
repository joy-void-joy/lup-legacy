"""Every file this checkout holds, listed once each.

`git ls-files` reads the index, and the index holds a path mid-merge at every
stage the merge left it — base, ours, theirs — so a walk over its cached
entries meets one conflicted file three times: three anti-pattern findings
for one line, three `# lup:` notes for one comment, a surface capture that
records one module at three locations. Every sweep over the tree reads the
index through here, where the one `--deduplicate` lives, so a file on disk is
one entry whatever the merge did to its index rows.

:mod:`lup.devtools.dev.pending` is the neighbouring reader, for what the tree
changed rather than what it holds.
"""

from collections.abc import Sequence
from pathlib import Path, PurePosixPath

from lup.execution.shell import git


def tracked_files(
    *, others: bool = False, suffixes: Sequence[str] = (), root: Path = Path()
) -> list[str]:
    """Every path the index holds, once each, relative to ``root``.

    ``others`` adds the untracked files the ignore rules do not exclude, for a
    sweep answerable for a module written five minutes ago as much as for a
    committed one. ``suffixes`` keeps only the paths carrying one of them, and
    none keeps every path. Nothing here reads the disk: a path the index names
    and the tree deleted is the caller's to drop or to refuse.

    ``root`` is the working directory unless a caller holding a checkout by
    path names it, and git answers only for what lies beneath it — which is
    the whole tree from the top of a checkout, and one subtree from inside it.

    Read as NUL-separated records, because a line-per-path listing quotes a
    path holding a non-ASCII byte — `"docs/caf\\303\\251.md"`, quotes and all —
    and a sweep matching suffixes or opening files then skips it as a name no
    file answers to.
    """
    listed = str(
        git(
            "ls-files",
            "--cached",
            "--deduplicate",
            "-z",
            *(("--others", "--exclude-standard") if others else ()),
            _cwd=str(root),
        )
    )
    return [
        rel
        # lup: ignore[string-split] — `-z` records, separated by NUL
        for rel in listed.split("\0")
        if rel and (not suffixes or PurePosixPath(rel).suffix in suffixes)
    ]
