# lup: ignore[constant-declaration]
# The file names below are a handshake across three processes: the generator
# writes them, the guard runs one by the other, and the host half reads its
# settings under its own name. A caller free to spell them differently is a
# caller free to ship a guard that reaches nothing.
"""What a plugin ships so a session is named for its work at its first prompt.

Three things travel, registered only where the hook set declares both a
roster and the naming: the guard every prompt-time fold of the roster is
started by, which exits without an interpreter where the repository's store
holds nothing; the runtime's host half, shipped verbatim, which imports
:mod:`lup.coordination.bare.naming` from the package already beside it; and
the declaration compiled into that runtime's words, under the host half's
name beside the hooks manifest — where
:func:`lup.coordination.bare.naming.compiled_for` looks, and outside the
runtime directory a policy evaluator's snapshot admits only source into.

Rendered once here rather than once per adapter, because what the adapters
own is the event's name, the variable their plugin root is exported as, the
host half, and a :class:`NamingSpelling` — the words their CLI takes for the
declared tier and effort and for an ask with no tools, and where it hears a
resume — and everything else would be the same files twice.

Nothing here holds a prompt: the entry keeps the short budget every
prompt-time fold has, because the host half starts its ask in a process of
its own and returns.
"""

import json
from collections.abc import Callable
from pathlib import Path

from pydantic import BaseModel

from lup.coordination.bare.naming import Naming
from lup.formats.banner import (
    COMMENT_FREE,
    REGENERATE_COMMAND,
    VERBATIM_COPY,
    GeneratedBanner,
)
from lup.harness.models import Artifact, HookSet, SessionNaming
from lup.providers.roster_prompt import PromptHook, guard_body, hook_entry
from lup.types import JsonObject, ModelTier, SessionEffort

GUARD_SCRIPT = "session_naming.sh"
RUNTIME_ENTRY = "session_naming.py"
"""What the plugin carries: the guard, and the host half the guard runs."""


class ResumeEvent(BaseModel, frozen=True):
    """The event a runtime fires as a session starts, and the matcher selecting a resume."""

    event: str
    matcher: str


class NamingSpelling(BaseModel, frozen=True):
    """What one runtime says for a naming ask, which the declaration leaves to it."""

    chosen: Callable[[ModelTier, SessionEffort], list[str] | None]
    """Its words for the declared tier's model at the declared effort.

    Compiled the way its sessions compile them, and refusing — by raising —
    an effort the model's catalog row lacks, so a declaration the CLI would
    reject at every ask is refused where it is generated. Nothing where the
    runtime has no model for the tier."""

    arguments: list[str]
    """The words that open its ask with no native tool, from its own list of them."""

    resume: ResumeEvent | None = None
    """Where the hook also hears that a session was reopened.

    Needed by a runtime that reports a session's title to no hook: the resume
    is recorded as it starts, and the name the reopened session already has
    is read at the next prompt. None for one whose prompt hook is handed the
    title itself, which is how a resumed title reaches it there."""


def compiled(
    declared: SessionNaming, chosen: list[str], spelling: NamingSpelling
) -> Naming:
    """The declaration in one runtime's words, as its host half reads it."""
    return Naming(
        instruction=declared.instruction,
        attempts=declared.attempts,
        deadline_seconds=declared.deadline_seconds,
        longest=declared.longest,
        arguments=[*chosen, *spelling.arguments],
    )


def naming_hook(
    plugin_root: Path,
    plugin_root_env: str,
    source: HookSet,
    event: str,
    host: str,
    host_origin: str,
    spelling: NamingSpelling,
) -> PromptHook:
    """The entry under *event* and the files behind it, where naming is declared.

    A tier the runtime cannot spell registers nothing, rather than an ask that
    could only fail; an effort its model does not take is refused here.
    """
    declared = source.session_naming
    if declared is None or source.peer_policy is None:
        return PromptHook(registered={}, artifacts=[])
    chosen = spelling.chosen(declared.tier, declared.effort)
    if chosen is None:
        return PromptHook(registered={}, artifacts=[])
    entry = hook_entry(plugin_root_env, GUARD_SCRIPT)
    hooks = plugin_root / "hooks"
    reopened: JsonObject = (
        {
            spelling.resume.event: [
                {"matcher": spelling.resume.matcher, "hooks": [entry]}
            ]
        }
        if spelling.resume is not None
        else {}
    )
    return PromptHook(
        registered={event: [{"hooks": [entry]}], **reopened},
        artifacts=[
            Artifact.generated(
                path=hooks / "scripts" / GUARD_SCRIPT,
                body=guard_body(event, RUNTIME_ENTRY),
                semantic_id=source.id,
                banner=GeneratedBanner(source=__name__, command=REGENERATE_COMMAND),
                executable=True,
            ),
            Artifact(
                path=hooks / "runtime" / RUNTIME_ENTRY,
                content=host,
                semantic_id=source.id,
                banner=VERBATIM_COPY.compiled_from(host_origin),
            ),
            Artifact(
                path=hooks / Path(RUNTIME_ENTRY).with_suffix(".json"),
                content=json.dumps(
                    compiled(declared, chosen, spelling),
                    indent=2,
                    sort_keys=True,
                ),
                semantic_id=source.id,
                banner=COMMENT_FREE.compiled_from(source.id),
            ),
        ],
    )
