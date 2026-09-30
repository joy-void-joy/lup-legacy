"""Where editing is currently happening, published for whoever resolves code.

A language server is started once, against the directory a session opened in,
and keeps that root for its whole life. Work happens somewhere else — this
project asks that every change be made in a worktree — and nothing about that
move reaches the server. It goes on resolving the same module names against
the launch checkout: not an error, an answer about the wrong tree.

The edited file is the one thing that knows where editing is happening, and
the permission hook already sees it on every edit. So the hook publishes the
checkout it belongs to, and the servers that would otherwise guess read it.

The reader and the writer never share a process, and the writer is the
hermetic hook runtime, which may not import this module. Nothing is passed
between them: each resolves the same location out of the repository they are
both in, and the record at the end of it is a JSON object this module owns
the shape of. Tests pin both halves against each other, since no type checker
spans the boundary.
"""

from pathlib import Path

from pydantic import BaseModel


def shared_git_directory(root: Path) -> Path:
    """The git directory every worktree of *root*'s repository shares.

    A linked worktree's ``.git`` is a file naming its own directory beneath
    the common one; a main checkout's ``.git`` is that common directory. The
    common directory rather than a main checkout, because a repository may
    keep its git directory beside its worktrees rather than inside one, and
    then there is no checkout above them to write into.

    A path in no repository answers for itself, so a caller outside one gets
    somewhere to look rather than an exception it has nothing to do with.
    """
    marker = root / ".git"
    if marker.is_dir():
        return marker
    if not marker.is_file():
        return root
    gitdir = marker.read_text("utf-8").removeprefix("gitdir:").strip()
    linked = Path(gitdir)
    return linked.parents[1] if gitdir and len(linked.parents) >= 2 else root


class Edition(BaseModel, frozen=True):
    """The checkout an edit landed in, and the file that says so.

    Carries the file as well as the workspace because the workspace alone
    cannot be checked: a reader that disagrees needs to see which edit the
    claim came from, and a stale record is worth recognising by the file it
    names rather than by a timestamp alone.
    """

    workspace: Path
    file: Path


def edition_path(root: Path) -> Path:
    """Where the hook publishes, named from any worktree of the repository.

    The reader is rooted wherever its server was launched and the writer
    sits in whichever checkout was edited, so the location has to be one
    neither has to be told. ``git`` already keeps one: every worktree names
    the directory they all share, so both ends resolve to the same file.
    """
    return shared_git_directory(root) / "lup" / "edition.json"


def read_edition(path: Path) -> Edition | None:
    """The last published edition, or None when nobody has published one.

    Every failure is the same answer. A missing file is the ordinary case,
    and a corrupt one is a hint rather than a fact — this only ever refines
    a root the caller already has, so refusing to answer costs the caller
    nothing and raising would cost it a working tool.

    A workspace that no longer exists is one more of them. The record
    outlives the checkout it names — deleting a worktree does not clear it,
    and this repository's own workflow deletes one whenever a branch lands —
    and a reader that trusts a dead path resolves every question against it
    and answers none of them. That is worse than the guess it replaced: the
    guess was rooted somewhere real.
    """
    if not path.is_file():
        return None
    try:
        published = Edition.model_validate_json(path.read_text("utf-8"))
    except ValueError:
        return None
    return published if published.workspace.is_dir() else None
