"""Guidance a container puts over the committed file, written outside the checkout.

A kind of session may read a document of its own instead of the project's
always-loaded guidance. The committed file is never rewritten for it: the
document is written once, content-addressed, under lup's cache on the host,
and mounted read-only over the committed file's path inside the container,
so the next session on the host reads the tree exactly as it was.
"""

import hashlib
from pathlib import Path

from lup.channels.models import write_atomic
from lup.launch.refusal import LaunchRefused


def guidance_home(cache: Path | None = None) -> Path:
    """Where guidance a container swaps in is kept: beside lup's other caches, outside every checkout."""
    return cache or Path.home() / ".cache" / "lup" / "guidance"


def held_guidance(
    root: Path, committed: Path, content: str, cache: Path | None = None
) -> dict[Path, str]:
    """``content`` written outside ``root``, keyed to the committed file it is mounted over.

    ``committed`` is the runtime's own guidance file, relative to ``root``.
    Refused where the checkout commits none there: a mount onto a file that
    is not there makes it on the host, which is the one thing this must not
    do, and a runtime reading no guidance there would read none of this.
    """
    target = root / committed
    if not target.is_file():
        raise LaunchRefused(
            f"This session's guidance goes over {committed}, which {root} does "
            "not commit. Generate the project's guidance first, or drop the "
            "guidance from the container."
        )
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]
    written = guidance_home(cache) / digest / committed.name
    if not written.is_file():
        write_atomic(written, content.encode("utf-8"))
    return {written: str(target)}
