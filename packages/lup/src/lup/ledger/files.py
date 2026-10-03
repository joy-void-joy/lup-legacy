"""Files as nodes: the substrate standing already rots against, made citable.

Evidence in the scaffold's corpus pins the digests of the files it was
checked against and reads as stale the moment one moves — the ledger's one
mechanism for rot, which every project adopting it rebuilds for anything
else that names a file. So the file itself is a node, the one kind this
library declares: recorded with the digest of its content as it was, and
standing ``fresh`` while the tree still holds those bytes, ``stale`` once it
does not, ``missing`` where the path is gone, ``unchecked`` without a tree to
read. An edge to a file is then an ordinary edge — a claim ``about``
``src/parser.py`` — the explorer draws it, and staleness comes free.

Declared here against the charter of declaring none, because a file is not
an epistemics. It says nothing about what counts as verified or what a claim
owes; it says what bytes a path held, which every project's files have in
common and no project would answer differently.
"""

from pathlib import Path
from typing import Literal, Self

from pydantic import Field, model_validator

from lup.formats import digest
from lup.ledger.models import LedgerNode, Standing, Surroundings


def pinned(held_at: Path, spelled: str, recorded: str) -> Standing:
    """Where a path pinned to a digest stands now: fresh, stale, or missing.

    The one reading every pinned kind shares — a file, a session's journal,
    an output — over the path the kind resolved and the spelling it shows a
    reader, so the words for stale and missing are the same wherever a
    digest rots.
    """
    held = digest.file(held_at)
    if held is None:
        return Standing(
            label="missing", reason=f"{spelled} is not in the tree", sound=False
        )
    if held != recorded:
        return Standing(
            label="stale",
            reason=f"{spelled} changed since it was recorded",
            sound=False,
        )
    return Standing(label="fresh")


class File(LedgerNode, frozen=True):
    """One file in the working tree, pinned to the bytes it held when recorded."""

    kind: Literal["ledger:file"] = "ledger:file"

    title: str = ""
    """What it is, which for a file is its path: left empty, the path fills it."""

    path: str = Field(min_length=1)
    """Relative to the working tree, so the same record reads in every checkout."""

    digest: str = ""
    """The content digest as recorded, pinned by `prepared` where a caller left it empty."""

    @model_validator(mode="after")
    def titled(self) -> Self:
        """A file's name is its title, unless somebody said more."""
        return self if self.title else self.model_copy(update={"title": self.path})

    def prepared(self, root: Path) -> Self:
        """The digest pinned to the tree as it is now, where none was recorded.

        A digest already carried is kept, so a record replayed from another
        machine keeps what it was pinned to there.
        """
        if self.digest:
            return self
        return self.model_copy(update={"digest": digest.file(root / self.path) or ""})

    def standing(self, around: Surroundings) -> Standing:
        """Fresh while the tree holds the recorded bytes; stale or missing once it does not.

        Unchecked where there is no tree to read, and reported as sound: a
        reader that cannot check has no grounds to say the file moved.
        """
        if around.root is None:
            return Standing(label="unchecked", reason="no working tree to read")
        return pinned(around.root / self.path, self.path, self.digest)
