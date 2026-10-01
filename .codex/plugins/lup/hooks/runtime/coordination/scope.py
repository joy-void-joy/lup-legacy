"""Execution identity for native queues whose home and daemon must be reachable.

Not :func:`~lup.coordination.bare.runtime.process_scope`, which answers a
narrower question and must stay narrower. That one asks whether a pid read
here names the process a row recorded, so it hashes only what numbers pids —
the boot and the pid namespace — and a reader in any mount, network or user
namespace that shares them asks the process rightly. This one asks whether a
sender reaches the same Codex home and the daemon serving it: a home is a
path, so the filesystem root and the mount namespace decide what it names; a
daemon is reached over a socket, so the network namespace does; and the
effective user decides whose files and whose daemon those are. Merged, a
liveness check would refuse a session whose tool server merely runs under
another user, and a queue route would accept a sender whose path names
another home.
"""

import hashlib
import json
import os
from pathlib import Path


def execution_scope() -> str:
    """Prove one Linux host, root, user and namespace scope; unknown is empty."""
    try:
        root = Path("/proc/self/root").stat()
        identity = {
            "root": [root.st_dev, root.st_ino],
            "user": os.geteuid(),
            "boot": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
            "namespaces": {
                name: [stat.st_dev, stat.st_ino]
                for name in ("mnt", "net", "user")
                for stat in [Path("/proc/self/ns", name).stat()]
            },
        }
    except OSError:
        # Missing kernel evidence is an unsupported route, not an inferred match.
        return ""
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
