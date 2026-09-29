# lup: ignore[constant-declaration]
# The file names below are a handshake across three processes: the generator
# writes them, the guard execs one by the other, and the shipped reader
# answers to both. A caller free to spell them differently is a caller free
# to ship a guard that reaches nothing.
"""What a plugin ships so a session is told its carriers have parted.

A project built on a scaffold can be holding a library from one upstream
commit and a copied half from another, and nothing in the checkout looks
wrong: the imports resolve, the tests that were written against the old
signature pass, and the first sign is a session spending an afternoon on a
call that cannot work. Both runtimes fire an event when a prompt is submitted
and read context back from it, so both plugins carry the same two artifacts: a
shell guard that exits without starting an interpreter where this project has
no scaffold branch at all, and the fold from
:mod:`lup.devtools.dev.drift_fold`, shipped verbatim beside it.

Rendered once here rather than once per adapter, for the reason the roster's
pair is: what the adapters own is two words — the event's name and the
variable their plugin root is exported as — and everything else would be the
same file twice.

The branch and the distribution are interpolated from the declaration that
owns them, because a copy shipped into a plugin can import neither: a project
that renamed its scaffold branch gets a guard that names the branch it has.
"""

from importlib import resources
from pathlib import Path

from lup.formats.banner import VERBATIM_COPY, GeneratedBanner
from lup.formats.banner import REGENERATE_COMMAND
from lup.harness.models import Artifact, HookSet
from lup.providers.roster_prompt import PromptHook, hook_entry

RUNTIME_MODULE = "carrier_drift.py"
GUARD_SCRIPT = "carrier_drift.sh"
RUNTIME_ORIGIN = "lup.devtools.dev.drift_fold"
RUNTIME_SOURCE = "drift_fold.py"
"""The two files a plugin carries for the carriers, and the fold's own home."""


def runtime_source(module: str) -> str:
    """A shipped runtime, read from the module that owns it rather than restated."""
    return resources.files("lup.devtools.dev").joinpath(module).read_text("utf-8")


def guard_body(event: str, runtime_module: str, branch: str, distribution: str) -> str:
    """A branch-existence check that answers "nothing was merged here" without Python.

    The scaffold branch is the whole test: a project that never adopted one
    has no such ref, and a session there is told nothing rather than told its
    carriers agree — which would cost a look at the lock on every prompt of
    every project that has no upstream.

    Every failure exits zero. A prompt is not something an unreadable lock may
    stop, so a guard that cannot tell must let it through: the cost of being
    wrong that way is one prompt without the line, and the cost of the other
    way is a session that cannot be prompted.
    """
    return f"""#!/bin/sh
command -v python3 >/dev/null 2>&1 || exit 0
git rev-parse --verify --quiet "refs/heads/{branch}" >/dev/null 2>&1 || exit 0
exec python3 -s "${{0%/*}}/../runtime/{runtime_module}" \
"{branch}" "{distribution}" "{event}"
"""


def drift_hook(
    plugin_root: Path, plugin_root_env: str, source: HookSet, event: str
) -> PromptHook:
    """The hooks entry under *event* and the files behind it, where carriers can part.

    The declaration is the hook set's own ``carriers``: a project that took a
    copied half from somewhere is one whose sessions are told when the halves
    stop agreeing, and a project that took none is told nothing here either.
    """
    if source.carriers is None:
        return PromptHook(registered={}, artifacts=[])
    return PromptHook(
        registered={event: [{"hooks": [hook_entry(plugin_root_env, GUARD_SCRIPT)]}]},
        artifacts=[
            Artifact.generated(
                path=plugin_root / "hooks" / "scripts" / GUARD_SCRIPT,
                body=guard_body(
                    event,
                    RUNTIME_MODULE,
                    source.carriers.branch,
                    source.carriers.distribution,
                ),
                semantic_id=source.id,
                banner=GeneratedBanner(source=__name__, command=REGENERATE_COMMAND),
                executable=True,
            ),
            Artifact(
                path=plugin_root / "hooks" / "runtime" / RUNTIME_MODULE,
                content=runtime_source(RUNTIME_SOURCE),
                semantic_id=source.id,
                banner=VERBATIM_COPY.compiled_from(RUNTIME_ORIGIN),
            ),
        ],
    )
