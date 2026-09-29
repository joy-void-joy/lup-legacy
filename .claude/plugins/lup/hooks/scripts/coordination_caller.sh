#!/bin/sh
# Generated from lup.providers.coordination_caller by `uv run lup-devtools harness generate all` — edit the source, not this file.
# See docs/harness.md.

command -v python3 >/dev/null 2>&1 || exit 0
exec python3 -s "${0%/*}/../runtime/coordination_caller.py"
