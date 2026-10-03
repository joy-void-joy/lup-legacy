#!/bin/sh
# Generated from lup.providers.hold_guard by `uv run lup-devtools harness generate all` — edit the source, not this file.
# See docs/harness.md.

started=$(date +%s)
[ -n "${LUP_COORDINATION_MEMBER}" ] || exit 0
shared=$(git rev-parse --git-common-dir 2>/dev/null) || exit 0
case "$shared" in
    /*) ;;
    *) shared="$PWD/$shared" ;;
esac
root="$shared/lup/coordination"
set -- "$root/holds"/*.json
[ -e "$1" ] || exit 0
if ! command -v python3 >/dev/null 2>&1; then
    echo "Lup could not tell whether this call is held: python3 is not on PATH" >&2
    exit 2
fi
exec python3 -s "${0%/*}/../runtime/coordination_hold.py" "$root" "${LUP_COORDINATION_MEMBER}" "$started"
