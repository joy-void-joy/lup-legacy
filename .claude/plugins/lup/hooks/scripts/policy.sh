#!/bin/sh
# Generated from lup.policy.dispatcher by `uv run lup-devtools harness generate all` — edit the source, not this file.
# See docs/harness.md.

script="${0%/*}/policy.py"
if [ ! -r "$script" ]; then
    printf 'Lup hook unavailable: %s\n' "$script" >&2
    printf 'Run outside this session: uv run lup-devtools harness generate all\n' >&2
    exit 2
fi
if ! command -v python3 >/dev/null 2>&1; then
    printf 'Lup hook cannot start: python3 is missing. Install Python 3 or fix PATH.\n' >&2
    exit 2
fi
python3 -s "$script"
lup_hook_status=$?
case "$lup_hook_status" in
    0|2) exit "$lup_hook_status" ;;
    *) printf 'Lup hook failed (exit %s).\n' "$lup_hook_status" >&2
       printf 'Run outside this session: uv run lup-devtools harness generate all\n' >&2
       exit 2 ;;
esac
