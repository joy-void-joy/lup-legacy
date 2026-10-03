"""Pure host-side helpers for the sandbox: output and deadlines.

Output decoding and the host-side request deadline. Both operate on the host
OS and stdlib alone — no Docker client — so they are independently testable
and importable without the ``docker`` extra. Whether a sandbox's owner still
runs is the bare :mod:`lup.coordination.bare.runtime` record's to answer, as
it is for every other process lup keeps.
"""

import time
from collections.abc import Iterator


def decode_output(output: bytes | Iterator[bytes] | None) -> str:
    """Decode bytes output to string, handling None and errors."""
    if output is None:
        return ""
    if isinstance(output, bytes):
        return output.decode("utf-8", errors="replace")
    return b"".join(output).decode("utf-8", errors="replace")


def compute_deadline(timeout_seconds: int, grace_seconds: float = 5.0) -> float | None:
    """Host-side deadline (monotonic clock) for a REPL request.

    Returns ``None`` for non-positive timeouts: "no timeout" means no
    host deadline at all, mirroring the in-sandbox behavior where the
    REPL server skips ``signal.alarm`` for non-positive values. Killing
    the connection after a fixed grace would lose the REPL state for
    deliberately long-running code.

    The grace period covers protocol overhead on top of the in-sandbox
    timeout, so the in-sandbox SIGALRM fires first under normal operation.
    """
    if timeout_seconds <= 0:
        return None
    return time.monotonic() + timeout_seconds + grace_seconds
