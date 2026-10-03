# lup: ignore[constant-declaration]
# The file names below are a handshake across three processes: the generator
# writes them, the guard runs the entry, and the entry and the permission
# dispatcher import the host half. A caller free to spell them differently is
# a caller free to ship a guard that reaches nothing.
"""What a plugin ships so a coordination call says which conversation made it.

No tool server's environment tells a session's conversations apart — Claude
Code serves them all from one, and Codex starts a subagent's copy under the
session's own environment — and a call arriving there carries no caller; each
runtime's hook payload does. So a hook matched
to the coordination tools alone writes the caller into the call's arguments
before it goes out, and the server acts on that conversation's own roster row.

Three things travel, registered only where a roster is declared: a shell
guard, a generated entry that names the runtime directory as a search path,
reads each event, asks the host half and prints its answer, and the
runtime's host half, shipped verbatim — the one module that reads that
runtime's payload into a caller, which the
compiled permission dispatcher imports as well, so a claim is recorded
against the row the tool server would act on.

The guard does not look for the store first, unlike the roster's other
guards. The first call a subagent makes is the one that would otherwise put
its description on its session's row, and a store with no members yet is
exactly where that first call lands.

Rendered once here rather than once per adapter, because what the adapters
own is the event, the matcher that names their spelling of the coordination
server's tools, the variable their plugin root is exported as, and the host
half — everything else would be the same file twice.
"""

from collections.abc import Callable
from pathlib import Path

from lup.formats.banner import REGENERATE_COMMAND, VERBATIM_COPY, GeneratedBanner
from lup.harness.models import Artifact, HookSet
from lup.providers.roster_prompt import PromptHook, answering_entry_body, hook_entry

GUARD_SCRIPT = "coordination_caller.sh"
RUNTIME_ENTRY = "coordination_caller.py"
HOST_MODULE = "caller_payload"
"""What the plugin carries: the guard, the entry it runs, and the host half."""


def guard_body() -> str:
    """A guard that hands over to the entry, and exits zero otherwise.

    Every failure exits zero. A call left unstamped acts as the session,
    which is the roster's behaviour before there was a caller to stamp; a
    refused coordination call would stop the work it was describing.
    """
    return f"""#!/bin/sh
command -v python3 >/dev/null 2>&1 || exit 0
exec python3 -s "${{0%/*}}/../runtime/{RUNTIME_ENTRY}"
"""


def caller_hooks(
    plugin_root: Path,
    plugin_root_env: str,
    source: HookSet,
    host: str,
    host_origin: str,
    event: str,
    matcher: Callable[[str], str],
) -> PromptHook:
    """The entry under *event* for *matcher*'s tools and the files behind it, where declared.

    The declaration is the hook set's own ``peer_policy``: a project whose
    sessions keep no roster has no row for a subagent to act on, and
    registers no hook. The host half travels whatever was declared, because
    the compiled dispatcher imports it at its top level, and an import a bare
    script cannot resolve is a permission hook that refuses every call.
    """
    carried = Artifact(
        path=plugin_root / "hooks" / "runtime" / f"{HOST_MODULE}.py",
        content=host,
        semantic_id=source.id,
        banner=VERBATIM_COPY.compiled_from(host_origin),
    )
    if source.peer_policy is None:
        return PromptHook(registered={}, artifacts=[carried])
    return PromptHook(
        registered={
            event: [
                {
                    "matcher": matcher(source.peer_policy.server),
                    "hooks": [hook_entry(plugin_root_env, GUARD_SCRIPT)],
                }
            ]
        },
        artifacts=[
            carried,
            Artifact.generated(
                path=plugin_root / "hooks" / "scripts" / GUARD_SCRIPT,
                body=guard_body(),
                semantic_id=source.id,
                banner=GeneratedBanner(source=__name__, command=REGENERATE_COMMAND),
                executable=True,
            ),
            Artifact.generated(
                path=plugin_root / "hooks" / "runtime" / RUNTIME_ENTRY,
                body=answering_entry_body(HOST_MODULE),
                semantic_id=source.id,
                banner=GeneratedBanner(source=__name__, command=REGENERATE_COMMAND),
            ),
        ],
    )
