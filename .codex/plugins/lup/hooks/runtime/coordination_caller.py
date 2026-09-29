# Generated from lup.providers.coordination_caller by `uv run lup-devtools harness generate all` — edit the source, not this file.
# See docs/harness.md.

"""Entry point for caller_payload, run as a bare script."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from caller_payload import main

main()
