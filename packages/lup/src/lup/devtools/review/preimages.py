"""Whether the files a review recorded still stand as it recorded them.

A native review binds its approval to the files it would change as they
stood when it was parked, and three readers ask whether they still do: the
requester's `review wait`, which carries an approval out only where they do
and retires a review that no approval could release any more; the dashboard,
which says so on the review before the operator reaches for Approve; and the
answer endpoint, which refuses the approval. One reading serves all three, so
they never disagree about which file moved.

A path moved where its text differs, where a file came or went, and where a
directory now stands at it: the hook records a directory as nothing -- only a
file has a document to bind -- so a retry finding one there records the call
afresh, and the review recorded before it can release nothing.

Only what this process can see is judged. A session records paths the
dashboard's process may not reach -- one inside the session's container, one
it may not read -- and what cannot be read here cannot be said to have
moved: such a path is never stale, whoever asks.
"""

from collections.abc import Sequence
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from lup.policy.relay import FileSignature, PersistentQuestion

type MoveCause = Literal["changed", "created", "deleted", "directory"]
"""How one recorded file differs from what the review recorded."""


class MovedPreimage(BaseModel, frozen=True):
    """One file a review recorded that no longer stands as it recorded it."""

    path: Path
    cause: MoveCause
    read: bool = False
    """Whether the call only reads it -- a copy's source, a patch -- rather than writing it."""

    def sentence(self) -> str:
        """What moved, as the operator and the requester read it."""
        since = (
            "since the operator saw this copy"
            if self.read
            else "since this was recorded"
        )
        match self.cause:
            case "changed":
                return f"{self.path} changed {since}"
            case "created":
                return f"{self.path} was created {since}"
            case "deleted":
                return f"{self.path} was deleted {since}"
            case "directory":
                return f"a directory now stands at {self.path}, {since}"


def seen_here(path: Path, roots: Sequence[Path]) -> bool:
    """Whether this process sees where *path* lies, so its absence would mean something.

    Inside the review's own checkout it does: the queue is read there. Outside
    it, only beneath a directory that stands here -- a path whose directory is
    nowhere to be found is in a filesystem this process does not share, such
    as the session's container.
    """
    return any(path.is_relative_to(root) for root in roots) or path.parent.is_dir()


def moved_preimage(
    path: Path, recorded: str | None, roots: Sequence[Path] = ()
) -> MovedPreimage | None:
    """How one recorded file moved since the review recorded it, or ``None`` where it did not.

    ``None`` too where this process cannot tell: a path it cannot see, or a
    file it may not read.
    """
    if not seen_here(path, roots):
        return None
    if path.is_dir():
        return MovedPreimage(path=path, cause="directory")
    try:
        standing = path.read_bytes() if path.is_file() else None
    except OSError:
        return None
    if standing is None and recorded is None:
        return None
    if recorded is None:
        return MovedPreimage(path=path, cause="created")
    if standing is None:
        return MovedPreimage(path=path, cause="deleted")
    if standing == recorded.encode():
        return None
    return MovedPreimage(path=path, cause="changed")


def written(question: PersistentQuestion) -> dict[Path, str | None]:
    """The recorded preimages of the files the call writes.

    A command's record says which they are: each file its verdict worked out
    a document for, and each file a step no document shows leaves. Every
    other file it recorded is one a step reads from -- a copy's source, a
    patch -- whose text the record already holds. Any other call writes every
    file it recorded.
    """
    if question.unpreviewed is None:
        return dict(question.preconditions)
    writes = {row.path for row in question.file_reviews or []} | {
        path for step in question.unpreviewed for path in step.paths
    }
    return {
        path: recorded
        for path, recorded in question.preconditions.items()
        if path in writes
    }


def moved(question: PersistentQuestion, sources: bool = False) -> list[MovedPreimage]:
    """Every file the call writes that no longer stands as recorded, as far as this process sees.

    With *sources*, the files it reads from too: what runs the call has to
    know a copy would land something other than what the operator saw, where
    whoever only decides whether a review can still be answered does not.
    """
    roots = [question.operation.worktree, question.operation.cwd]
    writes = written(question)
    judged = question.preconditions if sources else writes
    return [
        found.model_copy(update={"read": path not in writes})
        for path, recorded in judged.items()
        if (found := moved_preimage(path, recorded, roots)) is not None
    ]


class WatchedReading(BaseModel, frozen=True):
    """What one review's recorded files were when last read, and what that reading found."""

    signatures: tuple[FileSignature, ...]
    moved: list[MovedPreimage]


class PreimageWatch:
    """Which recorded files moved, per review, read again only where one's status changed.

    The dashboard asks of every waiting review on every look, a second apart,
    and a review can record a large file; so a file is read only once its size,
    modification time or inode says it may have changed, and a review whose
    files all stand as they were last read is answered from that reading.
    *sources* is :func:`moved`'s: whether the files a call reads from count.
    """

    def __init__(self, sources: bool = False) -> None:
        self.sources = sources
        self.seen: dict[str, WatchedReading] = {}

    def moved(self, question: PersistentQuestion) -> list[MovedPreimage]:
        def signature(path: Path) -> FileSignature:
            """What one recorded path is on disk now; one that cannot be stated reads as absent."""
            try:
                return FileSignature.of(path)
            except OSError:
                return FileSignature()

        signatures = tuple(signature(path) for path in question.preconditions)
        if (
            question.fingerprint in self.seen
            and self.seen[question.fingerprint].signatures == signatures
        ):
            return self.seen[question.fingerprint].moved
        found = moved(question, self.sources)
        self.seen[question.fingerprint] = WatchedReading(
            signatures=signatures, moved=found
        )
        return found
