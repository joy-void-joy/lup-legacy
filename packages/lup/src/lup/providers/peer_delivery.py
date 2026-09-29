# lup: ignore[constant-declaration]
# The file names below are a handshake across three processes: the generator
# writes them, the guard execs one by the other, and the shipped reader
# answers to both. A caller free to spell them differently is a caller free
# to ship a guard that reaches nothing.
"""What a plugin ships so a peer's mail reaches a session nobody opened in-process.

The in-process hook closes over a live mailbox, which is why it reaches only the
sessions lup itself opened. A person's own session has the plugin and no live
object, so delivery has to arrive as files a script can read — and the script
has to be cheap enough to run before *every* tool call, because the tools a
session uses while it is only reading are the ones the policy matcher does not
name.

Two artifacts answer that. A shell guard looks for a file in this member's
mailbox and exits without starting an interpreter where there is none, which is
almost always. Only where something is waiting does it hand over to
``providers/assets/peer_delivery_runtime.py``, shipped verbatim beside the
coordination package it reads the mailbox through. Named as a path rather than
a module because an asset is carried to its destination rather than imported
from here, and `assets` is deliberately not a package.

Every name the guard needs is interpolated from the definition that owns it
rather than written twice: a store directory renamed in one place moves the
guard with it, instead of leaving a script that reads a path nobody writes.
"""

from importlib import resources
from pathlib import Path

from lup.coordination.bare.store import (
    COORDINATION_DIR,
    MAILBOX_DIR,
    MEMBER_KIND,
    STORE_DIR,
    Actor,
    conversation_of,
    subagent_actor,
)
from lup.formats.banner import (
    REGENERATE_COMMAND,
    VERBATIM_COPY,
    GeneratedBanner,
)
from lup.harness.models import Artifact

RUNTIME_MODULE = "coordination_delivery.py"
GUARD_SCRIPT = "coordination_delivery.sh"
RUNTIME_ORIGIN = "lup.providers.assets.peer_delivery_runtime"
"""The two files a plugin carries for delivery, and where the reader comes from.

Beside the policy dispatcher's own pair rather than inside it, because they
answer different questions and fail in opposite directions: a policy that
cannot decide refuses, and a delivery that cannot answer stands aside.
"""


def delivery_runtime_source() -> str:
    """The reader, read from the module that owns it rather than restated here."""
    return (
        resources.files("lup.providers")
        .joinpath("assets/peer_delivery_runtime.py")
        .read_text("utf-8")
    )


def guard_body() -> str:
    """A mailbox listing that answers "nothing waiting" without starting Python.

    A maildir is its own position: a message is one file, put there by the
    sender and deleted by this member once it has been handed over, so what is
    waiting is exactly what is in the directory. There is nothing to compare
    against a cursor and no way for two writes inside one filesystem tick to
    hide each other, which is what a length check had to be careful about.

    The directory is named for the conversation this session is on the roster
    as, which is the member kind and the id together — an id alone is unique
    only within a kind, and the guard has to look where the sender wrote.

    The session's own subagents each have a mailbox of their own, keyed under
    the session, so the guard looks in those too: which conversation the call
    belongs to is in the payload, which only the reader parses, and one
    subagent's waiting mail starts the reader for any call of the session
    until that subagent's next call takes it.

    The globs are expanded into the positional parameters and each word
    tested, because an unmatched glob in a POSIX shell stays literal: `[ -e ]`
    on that word is false, which is the answer wanted, and no `ls` is started
    to find it out.

    Every failure exits zero. This runs before every tool call, so a guard
    that cannot tell must let the call through: the cost of being wrong that
    way is mail arriving one call later, and the cost of the other way is a
    session that cannot work.
    """
    member = "$LUP_COORDINATION_MEMBER"
    session = conversation_of(Actor(kind=MEMBER_KIND, id=member))
    subagents = conversation_of(subagent_actor(member, ""))
    return f"""#!/bin/sh
[ -n "{member}" ] || exit 0
command -v python3 >/dev/null 2>&1 || exit 0
shared=$(git rev-parse --git-common-dir 2>/dev/null) || exit 0
case "$shared" in
    /*) ;;
    *) shared="$PWD/$shared" ;;
esac
root="$shared/{STORE_DIR}/{COORDINATION_DIR}"
set -- "$root/{MAILBOX_DIR}/{session}"/*.json "$root/{MAILBOX_DIR}/{subagents}"*/*.json
for waiting do
    [ -e "$waiting" ] || continue
    exec python3 -s "${{0%/*}}/../runtime/{RUNTIME_MODULE}" "$root" "{member}"
done
exit 0
"""


def delivery_command(plugin_root_env: str) -> str:
    """The hooks entry, which never refuses however badly it goes.

    No `|| exit 2` beside it, unlike the policy guard's: a delivery that fails
    is a session that did not hear something, and turning that into a refused
    tool call would make an unreachable mailbox stop the work it was trying to
    inform.
    """
    return f'sh "${plugin_root_env}/hooks/scripts/{GUARD_SCRIPT}" || exit 0'


def delivery_artifacts(plugin_root: Path, semantic_id: str) -> list[Artifact]:
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
            content=delivery_runtime_source(),
            semantic_id=semantic_id,
            banner=VERBATIM_COPY.compiled_from(RUNTIME_ORIGIN),
        ),
    ]
