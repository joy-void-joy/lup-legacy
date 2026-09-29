# lup: ignore[constant-declaration]
# The file names below are a handshake across two processes: the generator
# writes them and the guard runs one by the other. A caller free to spell
# them differently is a caller free to ship a guard that reaches nothing.
"""What a plugin ships so a subagent's report waits for its background work.

Two things travel, registered only where the hook set declares the cleanup:
a shell guard that exits without starting an interpreter where none is
found, and the runtime's host half of the fold, shipped verbatim beside the
kernel it imports. The guard is registered under the subagent events that
runtime answers to — its start, where the fold adds the one sentence saying
what the subagent arms is its own to stop, and its stop, where the fold
refuses the report once while any of that work is still listed. A runtime
whose leftovers resume nobody names no stop event and takes the sentence
alone.

Rendered once here rather than once per adapter, because what the adapters
own is three things — the events' names, the variable their plugin root is
exported as, and the host half that reads their payload — and everything
else would be the same file twice.
"""

from pathlib import Path

from lup.formats.banner import REGENERATE_COMMAND, VERBATIM_COPY, GeneratedBanner
from lup.harness.models import Artifact, HookSet
from lup.providers.roster_prompt import PromptHook, hook_entry

GUARD_SCRIPT = "subagent_cleanup.sh"
RUNTIME_ENTRY = "subagent_cleanup.py"
"""What the plugin carries: the guard, and the host half the guard runs."""


def guard_body(entry: str) -> str:
    """A guard that hands over to the host half, and exits zero otherwise.

    Every failure exits zero. A subagent's report is not something a broken
    fold may stop: the cost of being wrong that way is one leaked task, which
    is what a session without the fold costs, and the cost of the other way
    is a subagent that cannot finish.
    """
    return f"""#!/bin/sh
command -v python3 >/dev/null 2>&1 || exit 0
exec python3 -s "${{0%/*}}/../runtime/{entry}"
"""


def cleanup_hooks(
    plugin_root: Path,
    plugin_root_env: str,
    source: HookSet,
    host: str,
    host_origin: str,
    start_event: str,
    stop_event: str | None,
) -> PromptHook:
    """The entries under the subagent events and the files behind them, where declared.

    The declaration is the hook set's own ``subagent_cleanup``: a project
    that declined it registers nothing and carries nothing. The start-time
    sentence is the declaration's to switch off on its own; the stop-time
    refusal is what the declaration is.

    ``None`` for the stop event is a runtime measured not to resume a subagent
    that has already reported, where the refusal would buy nothing and cost a
    turn. The sentence still goes, because the work still outlives the report;
    a runtime answering to neither registers nothing at all.
    """
    declared = source.subagent_cleanup
    events = (
        []
        if declared is None
        else [
            *([stop_event] if stop_event is not None else []),
            *([start_event] if declared.notice_at_start else []),
        ]
    )
    if not events:
        return PromptHook(registered={}, artifacts=[])
    return PromptHook(
        registered={
            event: [{"hooks": [hook_entry(plugin_root_env, GUARD_SCRIPT)]}]
            for event in events
        },
        artifacts=[
            Artifact.generated(
                path=plugin_root / "hooks" / "scripts" / GUARD_SCRIPT,
                body=guard_body(RUNTIME_ENTRY),
                semantic_id=source.id,
                banner=GeneratedBanner(source=__name__, command=REGENERATE_COMMAND),
                executable=True,
            ),
            Artifact(
                path=plugin_root / "hooks" / "runtime" / RUNTIME_ENTRY,
                content=host,
                semantic_id=source.id,
                banner=VERBATIM_COPY.compiled_from(host_origin),
            ),
        ],
    )
