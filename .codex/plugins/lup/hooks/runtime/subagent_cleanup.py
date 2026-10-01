# Generated from lup.providers.subagent_cleanup by `uv run lup-devtools harness generate all` — edit the source, not this file.
# See docs/harness.md.

"""Entry point for cleanup_payload, run as a bare script."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from cleanup_payload import decided


def main() -> None:
    """Answer the event on stdin, or say nothing and let it through as it came."""
    try:
        answer = decided(json.load(sys.stdin))
    except Exception:
        return
    if answer is not None:
        print(json.dumps(answer))


main()
