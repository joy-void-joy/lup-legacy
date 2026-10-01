# lup: ignore[constant-declaration]
# The file names below are a handshake across three processes: the generator
# writes them, the guard runs one by the other, and the shipped package
# answers to both. A caller free to spell them differently is a caller free
# to ship a guard that reaches nothing.
"""What a plugin ships so the coordination store reaches a session at all.

Three things travel, and only the first is unconditional. The **store
package** is :mod:`lup.coordination.bare` copied whole into the plugin's
``hooks/runtime/coordination/`` — the fold every reader of the store shares,
the prompt-time hook and the departure writer that stand on it, all of it
standard library so it resolves on the bare interpreter a runtime spawns. It
ships whether or not this project declared a roster, because the compiled
permission dispatcher imports it unconditionally and a dispatcher that cannot
import refuses every call in the session.

The other two are **guards**, one per event, registered only where a roster
is declared: a shell script that exits without starting an interpreter where
this repository's store holds nothing, and hands over to a module of the
package where it does. A session has no reason to ask who else is here at the
moment a prompt arrives, which is the moment it most needs to know; and a
session that exits cleanly writes nothing, so its row would read as running
forever. Both runtimes fire an event for each, and both read the same
envelope back.

Rendered once here rather than once per adapter, because what the adapters
own is two words — the event's name and the variable their plugin root is
exported as — and everything else would be the same file twice.

Every name the guards need is interpolated from the definition that owns it:
a store directory renamed in one place moves the guard with it, instead of
leaving a script that reads a path nobody writes.
"""

from importlib import resources
from pathlib import Path
import shlex

from lup.coordination import bare
from lup.coordination.bare.store import COORDINATION_DIR, MEMBERS_DIR, STORE_DIR
from lup.coordination.identity import MEMBER_ENV
from lup.formats.banner import (
    REGENERATE_COMMAND,
    VERBATIM_COPY,
    GeneratedBanner,
)
from lup.harness.models import Artifact, HookSet
from lup.policy.dispatcher import STORE_PACKAGE
from lup.types import JsonObject
from pydantic import BaseModel

CHANGES_MODULE = "changes"
CHANGES_ENTRY = "coordination_changes.py"
GUARD_SCRIPT = "coordination_changes.sh"
"""What the plugin carries for the roster at prompt time: a guard, the entry
the guard runs, and the module of the shipped package that entry calls."""

DEPARTURE_MODULE = "departure"
DEPARTURE_ENTRY = "coordination_departure.py"
DEPARTURE_SCRIPT = "coordination_departure.sh"
"""The same three under the runtime's ending event, reaching the one writer
that finishes this session's row on a clean exit."""

ARRIVAL_MODULE = "arrival"
ARRIVAL_ENTRY = "coordination_arrival.py"
ARRIVAL_SCRIPT = "coordination_arrival.sh"
"""The native session identity binder and its two entry artifacts."""

STORE_ORIGIN = bare.__name__
"""Where the shipped package is copied from, for the banner each file carries."""


def store_modules() -> list[Artifact]:
    """Every module of the shipped package, read from the package that owns it.

    Read rather than restated, and whole rather than module by module: the
    package's relative imports resolve the same beneath ``runtime/`` as they
    do in lup, which is what lets every file travel byte for byte and is the
    same arrangement the policy kernel ships under.
    """
    directory = resources.files(bare)
    return [
        Artifact(
            path=Path(f"{STORE_PACKAGE}/{found.name}"),
            content=found.read_text("utf-8"),
            semantic_id=STORE_PACKAGE,
            banner=VERBATIM_COPY.compiled_from(f"{STORE_ORIGIN}.{found.name[:-3]}"),
        )
        for found in sorted(directory.iterdir(), key=lambda entry: entry.name)
        if found.is_file() and found.name.endswith(".py")
    ]


def store_artifacts(plugin_root: Path, semantic_id: str) -> list[Artifact]:
    """The shipped package, placed under one plugin's runtime directory.

    Unconditional, unlike the guards: the compiled dispatcher imports this
    package at its top level whatever a project declared about rosters, and an
    import a bare script cannot resolve is a permission hook that raises
    before it decides anything.
    """
    return [
        module.model_copy(
            update={
                "path": plugin_root / "hooks" / "runtime" / module.path,
                "semantic_id": semantic_id,
            }
        )
        for module in store_modules()
    ]


# lup: defer: the hand-written host halves still name their own search path —
# both `subagent_cleanup` halves, `peer_delivery_runtime`, `policy_evaluator`
# and both policy dispatchers — where the caller hook's is named here, by the
# generated entry alone. Moving them is its own branch: the dispatchers are
# compiled from their halves into one script beside no module to import.
def entry_body(module: str) -> str:
    """The script a guard runs, which is one import and one call.

    It exists to name its own directory as a search path before importing,
    which is what the compiled dispatcher beside it does and for the same
    reason: a hook is launched as a bare script and promised no working
    directory, no ``PYTHONPATH`` and no interpreter environment. Python puts a
    script's own directory on the path by itself, and stops under any of
    ``-I``, ``-P`` or ``PYTHONSAFEPATH`` — the same three that break running
    the module directly with ``-m`` — and every hook here fails open, so what
    that would cost is a roster that silently stops answering.

    *module* is dotted from ``hooks/runtime/``: a module of the shipped store
    package, or a runtime's own host half shipped beside it. Generated rather
    than written into each, so the search path is named here once and no
    hand-written module carries it.
    """
    return f'''"""Entry point for {module}, run as a bare script."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from {module} import main

main()
'''


def answering_entry_body(module: str) -> str:
    """The script a guard runs for a host half that only decides one event.

    A hook of that kind reads one JSON event on stdin and answers with one
    JSON envelope on stdout or with nothing, and every one of them fails
    open: an event it cannot read or decide is let through as it came. So
    the read, the answer and the silence are written here once, and the host
    half exposes ``decided(payload)`` and nothing else — the caller hook's
    and the subagent cleanup fold's halves, on both runtimes. The search path
    is named as :func:`entry_body` names it, for the same reason.
    """
    return f'''"""Entry point for {module}, run as a bare script."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from {module} import decided


def main() -> None:
    """Answer the event on stdin, or say nothing and let it through as it came."""
    try:
        answer = decided(json.load(sys.stdin))
    except Exception:
        return
    if answer is not None:
        print(json.dumps(answer))


main()
'''


def guard_body(
    event: str, entry: str, home_env: str = "", events: tuple[str, ...] = ()
) -> str:
    """A store-existence check that answers "nobody coordinates here" without Python.

    The store's members directory is the whole test: a repository whose
    sessions have never joined has no directory, and a session there is told
    nothing rather than told the roster is empty — which would cost a line on
    every prompt of every project that never coordinates.

    What it hands over to is the entry beside the shipped package rather than
    a module of it, so the interpreter is given nothing to resolve from its
    environment: the entry names its own directory as the search path, the
    way the compiled dispatcher does.

    Every failure exits zero. A prompt is not something a broken store may
    stop, and neither is an exit, so a guard that cannot tell must let it
    through: the cost of being wrong that way is one prompt without the
    roster or one departure unwritten, and the cost of the other way is a
    session that cannot be prompted or cannot end.

    The member's launcher-proven id is handed over as an argument even where
    it is blank, so the reader can fall back to the id the runtime itself
    hands the hook; the environment variable's name stays the identity
    module's and the event's name stays the adapter's.
    """
    home_argument = f' "${{{home_env}:-}}"' if home_env else (' ""' if events else "")
    event_arguments = f" {shlex.join(events)}" if events else ""
    return f"""#!/bin/sh
command -v python3 >/dev/null 2>&1 || exit 0
shared=$(git rev-parse --git-common-dir 2>/dev/null) || exit 0
case "$shared" in
    /*) ;;
    *) shared="$PWD/$shared" ;;
esac
root="$shared/{STORE_DIR}/{COORDINATION_DIR}"
[ -d "$root/{MEMBERS_DIR}" ] || exit 0
exec python3 -s "${{0%/*}}/../runtime/{entry}" "$root" "${MEMBER_ENV}" "{event}"{home_argument}{event_arguments}
"""


def guard_command(plugin_root_env: str, guard_script: str) -> str:
    """The hooks entry, which never refuses however badly it goes.

    No `|| exit 2` beside it, unlike the policy guard's: a fold that fails is
    a session that was not told who arrived, and turning that into a refused
    prompt would make an unreadable roster stop the work it was trying to
    inform.
    """
    return f'sh "${plugin_root_env}/hooks/scripts/{guard_script}" || exit 0'


def hook_entry(plugin_root_env: str, guard_script: str) -> JsonObject:
    """The one command hook a runtime registers under one of its events.

    A short timeout, because the fold is three small files and a prompt is
    waiting on it: a store that takes longer than this to read is one the
    session is better off without for this prompt. An ending runs under a
    budget of its own that the same figure raises to fit.
    """
    # lup: defer: Codex 0.158.0 clamps a SessionEnd hook's timeout to 3s and
    # says so in hooks/list ("clamping SessionEnd hook timeout to 3s"), so on
    # Codex the ending does not get the 10s this raises its budget to; seen as
    # the warning the hook discovery of a Codex prepare() reports
    return {
        "type": "command",
        "command": guard_command(plugin_root_env, guard_script),
        "timeout": 10,
    }


class PromptHook(BaseModel, frozen=True):
    """What one plugin registers and carries for the roster at prompt time.

    Empty on both counts where the project declared no roster: a hook that
    fired for a population nobody declared would read an absent store on
    every prompt to say nothing, and a plugin would carry a file for it.
    """

    registered: JsonObject
    artifacts: list[Artifact]


def folded(hooks: list[PromptHook]) -> PromptHook:
    """Several prompt-time hooks as one registration, entries side by side.

    Two folds under one event are two entries under one key, and merging the
    dictionaries would keep whichever was written last — a plugin quietly
    registering one of the two. Every matching hook runs and none of these can
    refuse, so what the runtime is being told is the concatenation.
    """
    registered: JsonObject = {}  # lup: ignore[empty-collection] — event fold
    for hook in hooks:
        for event, entries in hook.registered.items():
            match (registered.get(event), entries):
                case ([*standing], [*arriving]):
                    registered[event] = [*standing, *arriving]
                case _:
                    registered[event] = entries
    return PromptHook(
        registered=registered,
        artifacts=[artifact for hook in hooks for artifact in hook.artifacts],
    )


def prompt_hook(
    plugin_root: Path, plugin_root_env: str, source: HookSet, event: str
) -> PromptHook:
    """The hooks entry under *event* and the guard behind it, where a roster is declared.

    The declaration is the hook set's own ``peer_policy``: a project whose
    sessions find each other on a roster is one whose sessions are told when
    it moves, and one that declined the roster is told nothing here either.
    """
    if source.peer_policy is None:
        return PromptHook(registered={}, artifacts=[])
    return PromptHook(
        registered={event: [{"hooks": [hook_entry(plugin_root_env, GUARD_SCRIPT)]}]},
        artifacts=hook_artifacts(
            plugin_root, source.id, event, GUARD_SCRIPT, CHANGES_ENTRY, CHANGES_MODULE
        ),
    )


def departure_hook(
    plugin_root: Path,
    plugin_root_env: str,
    source: HookSet,
    event: str,
    subagent_event: str,
) -> PromptHook:
    """The hooks entry under the runtime's ending events, and the guard behind it.

    Declared by the same ``peer_policy`` as the prompt-time hook: a session
    on a roster is one whose row has to end when it does, and a project that
    declined the roster has no row to end.

    One guard under two events — the session's ending, and the stop the
    runtime fires for one of its subagents — because the reader tells them
    apart by the subagent's id in the payload rather than by which event
    passed it, and the event the guard names is the session's, read by
    nobody.
    """
    if source.peer_policy is None:
        return PromptHook(registered={}, artifacts=[])
    return PromptHook(
        registered={
            ending: [{"hooks": [hook_entry(plugin_root_env, DEPARTURE_SCRIPT)]}]
            for ending in (event, subagent_event)
        },
        artifacts=hook_artifacts(
            plugin_root,
            source.id,
            event,
            DEPARTURE_SCRIPT,
            DEPARTURE_ENTRY,
            DEPARTURE_MODULE,
        ),
    )


def hook_artifacts(
    plugin_root: Path,
    semantic_id: str,
    event: str,
    guard_script: str,
    entry: str,
    module: str,
    home_env: str = "",
    events: tuple[str, ...] = (),
) -> list[Artifact]:
    """One event's guard and the entry it runs, beside the shipped package."""
    return [
        Artifact.generated(
            path=plugin_root / "hooks" / "scripts" / guard_script,
            body=guard_body(event, entry, home_env, events),
            semantic_id=semantic_id,
            banner=GeneratedBanner(source=__name__, command=REGENERATE_COMMAND),
            executable=True,
        ),
        Artifact.generated(
            path=plugin_root / "hooks" / "runtime" / entry,
            body=entry_body(f"{STORE_PACKAGE}.{module}"),
            semantic_id=semantic_id,
            banner=GeneratedBanner(source=__name__, command=REGENERATE_COMMAND),
        ),
    ]


def wake_hook(
    plugin_root: Path,
    plugin_root_env: str,
    source: HookSet,
    runtime: str,
    events: tuple[str, ...],
    home_env: str,
) -> PromptHook:
    """Bind a root native session after startup or a delayed roster join."""
    if source.peer_policy is None:
        return PromptHook(registered={}, artifacts=[])
    return PromptHook(
        registered={
            event: [{"hooks": [hook_entry(plugin_root_env, ARRIVAL_SCRIPT)]}]
            for event in events
        },
        artifacts=hook_artifacts(
            plugin_root,
            source.id,
            runtime,
            ARRIVAL_SCRIPT,
            ARRIVAL_ENTRY,
            ARRIVAL_MODULE,
            home_env,
            events,
        ),
    )
