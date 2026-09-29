#!/bin/sh
# Generated from lup.providers.roster_prompt by `uv run lup-devtools harness generate all` — edit the source, not this file.
# See docs/harness.md.

command -v python3 >/dev/null 2>&1 || exit 0
shared=$(git rev-parse --git-common-dir 2>/dev/null) || exit 0
case "$shared" in
    /*) ;;
    *) shared="$PWD/$shared" ;;
esac
root="$shared/lup/coordination"
[ -d "$root/members" ] || exit 0
exec python3 -s "${0%/*}/../runtime/coordination_changes.py" "$root" "$LUP_COORDINATION_MEMBER" "UserPromptSubmit"
