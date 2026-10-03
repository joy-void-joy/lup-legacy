"""Bytes a note points at, kept under the digest of what they are.

A note that quoted its evidence would be a note whose evidence is as editable
as the claim, and a note that named a path would be a note whose evidence
moved when somebody reorganised a tree. Under its own digest, the content is
what the name means: two notes attaching the same bytes attach one blob, and
nothing that arrives later can change what an earlier note pointed at.

**First writer wins**, which needs no lock and no ordering. Two sessions
storing identical bytes are storing the same bytes; the second finding the
path already there has nothing to do, and there is no case where the file is
present and wrong.
"""

from hashlib import sha256
from pathlib import Path

from lup.channels.models import write_atomic
from lup.ledger.models import Placement
from lup.ledger.store import BLOBS_DIR


class Blobs:
    """The content-addressed bytes one ledger's notes attach.

    Holds nothing open and locks nothing: every operation is a path derived
    from a digest, so a hook, a console and a tool server may all reach it at
    once without any of them having to be running for the others to work.
    """

    def __init__(self, root: Path) -> None:
        self.root = root / BLOBS_DIR

    def path(self, digest: str) -> Path:
        """Where the bytes with this digest sit, whether or not they are there."""
        return self.root / digest

    def name(self, content: bytes) -> str:
        """The name these bytes would be stored under, without storing them.

        What a record names before its bytes land, so a type may refuse the
        attachment while nothing is yet on disk.
        """
        return sha256(content).hexdigest()

    def store(self, content: bytes) -> str:
        """Put these bytes in the store and hand back the name they are under.

        Written through a temporary beside the destination and renamed, so a
        reader never opens a half-written blob: the path either does not exist
        or holds every byte the digest promises.
        """
        digest = self.name(content)
        landing = self.path(digest)
        if landing.exists():
            return digest
        write_atomic(landing, content)
        return digest

    def read(self, digest: str) -> bytes | None:
        """The bytes under this digest, or nothing where the store has none.

        ``None`` rather than an exception, because a missing attachment is a
        fact a reader renders — a note whose evidence is not on this machine
        is still a note — and not a failure of the read that found it.
        """
        landing = self.path(digest)
        try:
            return landing.read_bytes()
        except OSError:
            return None

    def holds(self, digest: str) -> bool:
        """Whether these bytes are in the store."""
        return self.path(digest).is_file()


class BlobStores:
    """The blob directories of one log, one beside each journal, read as one.

    Bytes go beside the journal of the node attaching them, so a committed
    node's evidence is committed with it and a local node's never reaches a
    commit. A read looks in every one, because a digest names the same bytes
    wherever they sit, and two stores holding one blob hold the same blob.
    """

    def __init__(self, roots: dict[Placement, Path]) -> None:
        self.halves = {placement: Blobs(root) for placement, root in roots.items()}

    def name(self, content: bytes) -> str:
        """The name these bytes would take in either half, without storing them."""
        return sha256(content).hexdigest()

    def store(self, content: bytes, placement: Placement) -> str:
        """Put these bytes beside one half's journal and hand back their digest."""
        return self.halves[placement].store(content)

    def read(self, digest: str) -> bytes | None:
        """The bytes under this digest from whichever half holds them, or nothing."""
        return next(
            (
                found
                for half in self.halves.values()
                if (found := half.read(digest)) is not None
            ),
            None,
        )

    def holds(self, digest: str) -> bool:
        """Whether either half has these bytes."""
        return any(half.holds(digest) for half in self.halves.values())
