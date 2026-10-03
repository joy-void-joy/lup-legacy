# lup: ignore[constant-declaration]
# The file names below are a handshake across three processes: the generator
# writes them, the guard execs one by the other, and the shipped reader
# answers to both. A caller free to spell them differently is a caller free
# to ship a guard that reaches nothing.
"""What a plugin ships so a paused or budget-held agent's next tool call waits.

The policy hook holds the calls it judges, and judges them once a hold lets
them go; this holds every other call, because the tools a paused agent uses
while it only reads are the ones the policy matcher does not name, and a
pause that let an agent read on would hold nothing it set out to.

It has to cost nothing where nothing is held, which is almost always, since
it runs before every tool call. So a shell guard looks for a hold file in the
store and exits without starting an interpreter where there is none. Only
where one is does it hand over to ``providers/assets/hold_runtime.py``,
shipped verbatim beside the coordination package it reads the holds through.

It fails the other way from delivery. A delivery that cannot answer stands
aside, and the cost is mail one call late; a hold that cannot tell whether
the call is held, while some hold stands, refuses it, since the cost the
other way is an agent the operator paused carrying on. Where no hold file
exists, every failure to look still exits zero: nothing could be held.

Every name the guard needs is interpolated from the definition that owns it
rather than written twice, so a store directory renamed in one place moves
the guard with it.
"""

from importlib import resources
from pathlib import Path

from lup.coordination.bare.store import COORDINATION_DIR, HOLDS_DIR, STORE_DIR
from lup.formats.banner import REGENERATE_COMMAND, VERBATIM_COPY, GeneratedBanner
from lup.harness.models import Artifact
from lup.policy.dispatcher import REFUSAL_STATUS

RUNTIME_MODULE = "coordination_hold.py"
GUARD_SCRIPT = "coordination_hold.sh"
RUNTIME_ORIGIN = "lup.providers.assets.hold_runtime"
"""The two files a plugin carries for holding a call, and where the reader comes from.

A pair of its own beside delivery's and the policy's, because it answers a
different question: not what this call may do, but whether it may happen yet.
"""


def hold_runtime_source() -> str:
    """The reader, read from the module that owns it rather than restated here."""
    return (
        resources.files("lup.providers")
        .joinpath("assets/hold_runtime.py")
        .read_text("utf-8")
    )


def guard_body() -> str:
    """A listing of the store's holds that answers "nothing held" without starting Python.

    Any hold file at all starts the reader, which works out whether this
    call's caller is the one it covers: the guard reads no file and cannot
    tell, and a hold is rare enough that the reader's cost is paid only
    while one stands. The glob is expanded into the positional parameters and
    its first word tested, because an unmatched glob in a POSIX shell stays
    literal and ``[ -e ]`` on it is false.

    The moment the hook started is stamped in whole seconds and handed over,
    because the runtime's timeout counts from it and the reader's limit has
    to be counted from the same place.
    """
    member = "${LUP_COORDINATION_MEMBER}"
    return f"""#!/bin/sh
started=$(date +%s)
[ -n "{member}" ] || exit 0
shared=$(git rev-parse --git-common-dir 2>/dev/null) || exit 0
case "$shared" in
    /*) ;;
    *) shared="$PWD/$shared" ;;
esac
root="$shared/{STORE_DIR}/{COORDINATION_DIR}"
set -- "$root/{HOLDS_DIR}"/*.json
[ -e "$1" ] || exit 0
if ! command -v python3 >/dev/null 2>&1; then
    echo "Lup could not tell whether this call is held: python3 is not on PATH" >&2
    exit {REFUSAL_STATUS}
fi
exec python3 -s "${{0%/*}}/../runtime/{RUNTIME_MODULE}" "$root" "{member}" "$started"
"""


def hold_command(plugin_root_env: str) -> str:
    """The hooks entry, which refuses the call where the guard failed while holds stand."""
    return (
        f'sh "${plugin_root_env}/hooks/scripts/{GUARD_SCRIPT}" || exit {REFUSAL_STATUS}'
    )


def hold_artifacts(plugin_root: Path, semantic_id: str) -> list[Artifact]:
    """The guard and the reader, as one plugin carries them."""
    return [
        Artifact.generated(
            path=plugin_root / "hooks" / "scripts" / GUARD_SCRIPT,
            body=guard_body(),
            semantic_id=semantic_id,
            banner=GeneratedBanner(source=__name__, command=REGENERATE_COMMAND),
            executable=True,
        ),
        Artifact(
            path=plugin_root / "hooks" / "runtime" / RUNTIME_MODULE,
            content=hold_runtime_source(),
            semantic_id=semantic_id,
            banner=VERBATIM_COPY.compiled_from(RUNTIME_ORIGIN),
        ),
    ]
