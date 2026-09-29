#!/usr/bin/env python3
# lup: ignore[dict-get]
# A standalone container program: the standard library is all it has. The
# document it amends belongs to the runtime, an open object whose schema lives
# upstream, so the one field it reads is read by name.
"""Trust the checkout a container opens on, in the document every container shares.

Every container started on a config volume runs this before its runtime, and
containers start concurrently -- a launch's probes, a run's workers, a second
terminal -- so the document has writers that cannot see one another. Each
guard below was measured absent:

- An exclusive lock, because two merges that read the same document each
  rename a copy lacking the other's entry, and the checkout that loses opens
  untrusted.
- A temporary file of the writer's own, because a shared name is truncated by
  the next writer while the first is still filling it, and the first's rename
  then publishes a document whose front is NUL bytes.
- No write when the trust is already recorded, which is nearly every start,
  so a running session's own save is exposed only on a checkout's first.
"""

import fcntl
import json
import os
import sys
import tempfile
from pathlib import Path


def record_trust(document: Path, seed: Path, checkouts: list[str]) -> bool:
    """Record each checkout as trusted in *document*, starting it from *seed*.

    Returns whether the document was written. An empty document is started
    from the seed as a missing one is; one that does not parse is refused and
    left for the runtime's own recovery, which keeps a copy before resetting.
    """
    with (document.parent / ".lup-trust.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        present = document.exists() and document.stat().st_size > 0
        source = document if present else seed
        try:
            content = json.loads(source.read_text(encoding="utf-8"))
        except (ValueError, UnicodeError):
            raise ValueError(f"{source} does not parse as JSON") from None
        projects = (
            content.setdefault("projects", {}) if isinstance(content, dict) else None
        )
        if not isinstance(projects, dict):
            raise ValueError(f"{source} holds no projects object")
        entries = {checkout: projects.get(checkout, {}) for checkout in checkouts}
        if not all(isinstance(entry, dict) for entry in entries.values()):
            raise ValueError(f"{source} holds a project entry that is not an object")
        untrusted = [
            checkout
            for checkout, entry in entries.items()
            if entry.get("hasTrustDialogAccepted") is not True
        ]
        if present and not untrusted:
            return False
        for checkout in untrusted:
            projects[checkout] = {**entries[checkout], "hasTrustDialogAccepted": True}
        descriptor, temporary = tempfile.mkstemp(
            prefix=f"{document.name}.lup-", dir=document.parent
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(content, indent=2, ensure_ascii=False) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            Path(temporary).replace(document)
        finally:
            Path(temporary).unlink(missing_ok=True)
        return True


def main() -> None:
    seed, document, *checkouts = sys.argv[1:]
    try:
        record_trust(Path(document), Path(seed), checkouts)
    except (OSError, ValueError) as error:
        # Said and passed over rather than fatal, as the runtime's own
        # recovery for an unreadable document only runs if the session starts.
        print(f"lup: workspace trust was not recorded: {error}", file=sys.stderr)


if __name__ == "__main__":
    main()
