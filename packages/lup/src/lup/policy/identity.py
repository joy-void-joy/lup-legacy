"""The identity a launcher declares for the session it is about to start.

Edit autonomy is a property of *how a session was launched*, not of a name
one runtime happens to put in a hook payload. Claude Code fills `agent_type`
only for native subagent dispatch, so a resolver worker — an ordinary
top-level session — could never be recognized by it, and Codex has no such
field at all. Declaring the identity in the session environment lets the
actor that knows the claim to be true be the actor that makes it, on every
runtime.

This mirrors `LUP_SANDBOX_ACTIVE`: set exactly when the launcher verified
what it announces. A hook script is spawned by the runtime CLI with the
CLI's own environment, so an agent exporting this inside a shell tool call
cannot reach the dispatcher that judges it.
"""

from enum import StrEnum

from lup.types import EnvVars

# lup: ignore[constant-declaration] — the launcher that sets it and the hook that
# reads it are different processes, so the name is an identity, not a preference
AGENT_IDENTITY_ENV = "LUP_AGENT_IDENTITY"
"""Environment variable naming the declared identity of a launched session."""

# lup: ignore[constant-declaration] — a cross-process context key, not a policy preference
POLICY_ROOT_ENV = "LUP_POLICY_ROOT"
"""Application environment for operator review commands, separate from native cwd."""

# lup: ignore[constant-declaration] — the launch that exports it and the hook and
# session that read it are different processes, so the name is an identity
DASHBOARD_URL_ENV = "LUP_DASHBOARD_URL"
"""Environment variable carrying the dashboard's address to every launched session.

The address alone: the capability that opens the page stays in the host's
private state, so a session knows where the operator reviews and cannot
review for them. Its presence is also what tells a session's hook that a
reviewer reads what it parks, so a question is parked there rather than put
to a prompt.
"""

# lup: ignore[constant-declaration] — the launch that lends the directory and the
# hook and waiter that read it are different processes, so the name is an identity
REVIEW_ANSWERS_ENV = "LUP_REVIEW_ANSWERS"
"""Environment variable naming where the host keeps the operator's answers.

Set by every launch to the host's own path, which a contained session reaches
through a read-only mount of its repository's answers at that same path;
unset, the path is derived from `$XDG_STATE_HOME` as the host derives it.
"""


class ConcernAllowance(StrEnum):
    """One edit gate a concern needs, which only a human can grant it.

    These gates exist because the decision is a human's. Naming what a plan
    needs at plan time moves that decision to where the human is already
    deciding, instead of parking the run to ask again for work they just
    approved — and a need nobody could have foreseen is asked for mid-lease
    and granted to the session that asked.

    This enum is the vocabulary's single source of truth: whoever grants a
    gate names it from here, the compiled dispatchers honour exactly its
    members, and a name outside it is dropped rather than trusted.

    Where a lease's current grants live, and why they cannot live here, is
    :mod:`lup.policy.grants`: a gate is granted while the session it is
    granted to is already running, so unlike an identity it is not something
    a launcher can settle in advance.
    """

    NEW_DEVTOOLS_MODULE = "new-devtools-module"
    ANTIPATTERN_SUPPRESSION = "antipattern-suppression"


def agent_identity_environment(identity: str) -> EnvVars:
    """Declare one session's identity, or clear it with an empty name.

    Non-autonomous sessions must set the empty value rather than omit the
    variable: runtimes merge a session's environment over the launching
    process's, so an operator with this exported would otherwise hand their
    own autonomy to a session that was never granted it.
    """
    return {AGENT_IDENTITY_ENV: identity}
