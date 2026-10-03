"""One hash, and one framing for each thing that gets hashed.

Every digest this library keeps is SHA-256 in hex; what differs is what is
fed to it, and that is where two copies drift. A digest of a text is of its
UTF-8 bytes. A digest of a file is of its bytes, and a file that is not there
has none — one answer for absence, so no caller compares an empty string
against a hash of nothing. A digest of a tree frames each file's path, and
its executable bit where that is part of what is deployed, and its bytes, each
ended by NUL so no two trees digest alike by sliding a byte from one field
into the next. A digest of several strings length-delimits each, so a part
holding the separator cannot forge its neighbour.

What each digest is *of* is the caller's: which files a tree holds and in
what order, which bytes a file is normalised to first. A changed order is a
changed digest, so a caller that persists one keeps its order stable.

The bare hook script keeps its own copies, since it runs with no ``lup`` to
import.
"""

import hashlib
from collections.abc import Callable, Iterable
from pathlib import Path


def text(content: str) -> str:
    """The digest of ``content``'s UTF-8 bytes."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def file(path: Path) -> str | None:
    """The digest of the bytes at ``path``, or ``None`` where no file is there.

    A directory standing at the path, or a file where one of its parents
    should be, is no file there either.
    """
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except (FileNotFoundError, IsADirectoryError, NotADirectoryError):
        return None


def tree(
    files: Iterable[Path],
    root: Path,
    modes: bool = False,
    read: Callable[[Path], bytes] = Path.read_bytes,
) -> str:
    """One digest over ``files``, in the order given, each framed by its path under ``root``.

    ``modes`` adds whether each file is executable, for a tree whose mode is
    part of what it deploys; ``read`` is what each file's bytes are taken
    as, for a caller that normalises them first.
    """
    digest = hashlib.sha256()
    for path in files:
        digest.update(path.relative_to(root).as_posix().encode("utf-8") + b"\0")
        if modes:
            digest.update(b"x\0" if path.stat().st_mode & 0o111 else b"-\0")
        digest.update(read(path) + b"\0")
    return digest.hexdigest()


def parts(values: Iterable[str]) -> str:
    """One digest over an ordered list of strings, each length-delimited."""
    digest = hashlib.sha256()
    for value in values:
        digest.update(f"{len(value)}\0".encode())
        digest.update(value.encode("utf-8"))
    return digest.hexdigest()
