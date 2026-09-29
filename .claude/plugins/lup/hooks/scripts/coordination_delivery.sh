#!/bin/sh
# Generated from lup.providers.peer_delivery by `uv run lup-devtools harness generate all` — edit the source, not this file.
# See docs/harness.md.

[ -n "$LUP_COORDINATION_MEMBER" ] || exit 0
command -v python3 >/dev/null 2>&1 || exit 0
shared=$(git rev-parse --git-common-dir 2>/dev/null) || exit 0
case "$shared" in
    /*) ;;
    *) shared="$PWD/$shared" ;;
esac
root="$shared/lup/coordination"
set -- "$root/mailbox/session-$LUP_COORDINATION_MEMBER"/*.json "$root/mailbox/subagent-$LUP_COORDINATION_MEMBER-"*/*.json
for waiting do
    [ -e "$waiting" ] || continue
    exec python3 -s "${0%/*}/../runtime/coordination_delivery.py" "$root" "$LUP_COORDINATION_MEMBER"
done
exit 0
