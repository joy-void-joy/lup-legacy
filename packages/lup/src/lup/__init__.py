"""Lup's deliberately small, provider-neutral runtime front door.

Three kinds of name are exported here, and what each costs to import decides
how it is reached.

The **session vocabulary** -- sessions, turns, results, and the protocols
code naming no provider holds -- is provider-neutral and free to import:
naming it costs `lup.sessions`, with neither SDK among it, so it is bound as
the package loads.

The **agents**, `Claude` and `Codex`, are not free, because each is declared
in terms of the tools its provider speaks, and those reach an MCP server and
the rest of that ecosystem. Nor is the **launch vocabulary**: the fields a
launch adds to an agent's declaration -- the wall its session opens behind
(`OuterContainer`, `InnerSandbox`, `NoSandbox`), a folder outside the working
tree it reaches (`Mount`), its coordination identity (`Member`), what is kept
of it (`Recording`), the session it reopens (`Latest`, `Pick`, `Reopen`),
and what it keeps running on the host beside it (`HostCompanion`, most often
a `SharedProcess`, with the values one is written in) -- defined beside the
harness, policy and sandbox machinery a launch composes. Bound here, either would have `import lup` pull several hundred
modules on behalf of a caller who may have wanted a type annotation.

So both resolve on first access, through one table naming the module each is
defined in: the module hook is a lookup rather than a branch per name, and a
name joining them is one row. A reader still writes
`from lup import Claude, InnerSandbox` and a checker still sees the real
classes -- the `TYPE_CHECKING` block below is what it reads -- while a module
that only annotates against `Agent` pays for none of it.

This module is a re-export and nothing else: every name here is defined in
the module a reader could import it from, and nothing inside the library
imports from here.

That laziness is also what keeps the harder promise: neither provider SDK is
imported by `import lup`, nor by naming an agent or a launch field, but only
by opening a session with one.
"""

from importlib import import_module
from typing import TYPE_CHECKING

from lup.sessions.events import (
    SessionId,
    SessionSummary,
    TurnId,
    TurnInput,
    TurnMessage,
    TurnResult,
)
from lup.sessions.surface import Agent, Conversation, Turn
from lup.types import CustomModel

if TYPE_CHECKING:
    from lup.launch.companions import (
        CompanionLaunch,
        CompanionPlace,
        CompanionProcess,
        CompanionScope,
        Contribution,
        HostCompanion,
        SharedProcess,
    )
    from lup.launch.declaration import (
        InnerSandbox,
        Latest,
        Member,
        Mount,
        NoSandbox,
        OuterContainer,
        Pick,
        Recording,
        Reopen,
    )
    from lup.providers.claude import Claude
    from lup.providers.codex import Codex

# Where each lazy export is defined, so the resolution below is a lookup
# rather than a branch per name -- a third adapter, or another launch field,
# is one row.
# lup: ignore[library-default] — the names this library defines and the
# modules it defines them in, so the table is what lup ships rather than a
# choice made for an adopter: a provider arrives here as an adapter, and a row
# an adopter replaced would point `from lup import Claude` at something lup
# never wrote.
LAZY_EXPORTS = {
    "Claude": "lup.providers.claude",
    "Codex": "lup.providers.codex",
    "CompanionLaunch": "lup.launch.companions",
    "CompanionPlace": "lup.launch.companions",
    "CompanionProcess": "lup.launch.companions",
    "CompanionScope": "lup.launch.companions",
    "Contribution": "lup.launch.companions",
    "HostCompanion": "lup.launch.companions",
    "InnerSandbox": "lup.launch.declaration",
    "Latest": "lup.launch.declaration",
    "Member": "lup.launch.declaration",
    "Mount": "lup.launch.declaration",
    "NoSandbox": "lup.launch.declaration",
    "OuterContainer": "lup.launch.declaration",
    "Pick": "lup.launch.declaration",
    "Recording": "lup.launch.declaration",
    "Reopen": "lup.launch.declaration",
    "SharedProcess": "lup.launch.companions",
}


def __getattr__(
    name: str,
) -> type[
    Claude
    | Codex
    | CompanionLaunch
    | CompanionPlace
    | CompanionProcess
    | CompanionScope
    | Contribution
    | HostCompanion
    | InnerSandbox
    | Latest
    | Member
    | Mount
    | NoSandbox
    | OuterContainer
    | Pick
    | Recording
    | Reopen
    | SharedProcess
]:
    """Resolve a lazy export on first access, and nothing else.

    PEP 562's module hook, used for exactly the names above. Anything else
    raises the ``AttributeError`` Python would have raised anyway, in the same
    words, so a typo at the front door reads as a typo rather than as an import
    failure somewhere inside an adapter or the launch machinery.
    """
    if name not in LAZY_EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(import_module(LAZY_EXPORTS[name]), name)


__all__ = [  # lup: ignore[all-export] -- the package-root public API
    "Agent",
    "Claude",
    "Codex",
    "CompanionLaunch",
    "CompanionPlace",
    "CompanionProcess",
    "CompanionScope",
    "Contribution",
    "Conversation",
    "CustomModel",
    "HostCompanion",
    "InnerSandbox",
    "Latest",
    "Member",
    "Mount",
    "NoSandbox",
    "OuterContainer",
    "Pick",
    "Recording",
    "Reopen",
    "SessionId",
    "SessionSummary",
    "SharedProcess",
    "Turn",
    "TurnId",
    "TurnInput",
    "TurnMessage",
    "TurnResult",
]
