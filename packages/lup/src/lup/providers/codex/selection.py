"""Codex as one selectable runtime.

Codex decides autonomy with a sandbox, and native authority is compiled
independently of it: ``tools`` names the built-in facilities a session
may reach, and an unsupported exact grant fails before launch rather than
being approximated. Declared application tools keep their Python handlers
through the thread's dynamic tools, while explicitly external servers keep
their subprocess transport. Session-level ``allowed_tools`` and
``disallowed_tools`` have no app-server equivalent and are refused rather
than dropped.

Typed output rides ``outputSchema`` on each ``turn/start``, so a schema may
change or disappear between turns without disturbing the thread. The
dynamic-tool channel is thread-scoped and therefore carries application tools
alone; changing those still requires a fresh session.

Portable PostToolUse and Stop hooks run on native lifecycle events. Tagged
mailbox observers also deliver on native activity without changing approvals.
Other PreToolUse hooks must explicitly name one of the native approval
methods, or the exact joined methods in
:data:`lup.providers.codex.hooks.APPROVAL_METHODS`; only those
registrations enable approval requests. The app-server does not ask before
every tool call, so broader pre-execution hooks are refused. The generated
policy dispatcher enforces policy at the native PreToolUse boundary.

``disallowed_tools`` is refused despite the dispatcher being able to deny a
tool it can match, because that dispatcher is installed once per harness tree
and this field is asked per session: honouring it there would give every
session in the project a refusal one of them asked for. A block list is also
the field where silence costs most — a roster that came out too wide fails
visibly, where a refusal that was dropped leaves the tool callable and
nothing saying so.

``effort`` passes through under its own name: every portable rung is one
Codex's catalog lists. Which rungs a given model takes is narrower, and a
rung the model lacks is refused where the session is declared rather than
narrowed to one it has. A model only Claude's catalog lists is refused too.
"""

from pathlib import Path

from lup.providers.codex.hooks import codex_hook_approval_policy
from lup.providers.codex.home import select_codex_home
from lup.providers.codex.login import CODEX_LOGIN
from lup.providers.codex.model_choice import codex_model_choice
from lup.providers.codex.models import CodexEffort
from lup.providers.codex.builtins import CodexBuiltins
from lup.providers.codex import CODEX_PROGRAM, Codex, CodexSandbox, CodexTools
from lup.sessions.errors import UnsupportedCapability
from lup.providers.selection import (
    Runtime,
    SessionAutonomy,
    SessionEffort,
    SessionRequest,
)
from lup.types import EnvVars
from lup.workspace.paths import project_root

# lup: ignore[constant-declaration] — each value is Codex's own sandbox name for
# the autonomy beside it, over a vocabulary this library closes
CODEX_AUTONOMY: dict[SessionAutonomy, CodexSandbox] = {
    "ask": "read-only",
    "plan": "read-only",
    "accept_edits": "workspace-write",
    "unattended": "danger-full-access",
}
"""What a session may reach, standing in for an approval it cannot raise."""

# lup: ignore[constant-declaration] — each value is Codex's own effort for the
# degree beside it, over a vocabulary this library closes
CODEX_EFFORT: dict[SessionEffort, CodexEffort] = {
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "xhigh",
    "max": "max",
    "ultra": "ultra",
}
"""What Codex calls each degree of effort a caller can ask for.

Every rung under its own name, each one Codex's catalog lists; a model whose
own row lacks one refuses it where the session is declared."""


def codex_config(request: SessionRequest) -> Codex:
    """Render a portable request into Codex's own session configuration.

    Rendering is separate from building so an application can stack a
    :class:`~lup.providers.config.ConfigTransform` — a compatible endpoint, a
    profile — onto what a request asked for, before any session exists.

    ``cwd`` is required rather than defaulted: Codex sandboxes a session
    against its working directory, so inferring one would decide what the
    session may write from wherever the process happened to start.

    ``sandbox`` and ``autonomy`` both land on Codex's sandbox mode, which is
    the only field Codex has for either;
    :func:`~lup.providers.codex.launch.codex_sandbox_mode` states how the
    two are reconciled. An ``outer`` request is also started as the program
    that enters its container, the same seam Claude spells ``cli_path``.
    """
    refused = [
        name
        for name, asked in (
            ("allowed_tools", bool(request.allowed_tools)),
            ("disallowed_tools", bool(request.disallowed_tools)),
        )
        if asked
    ]
    if refused:
        raise UnsupportedCapability(
            f"Codex has no session-level {', '.join(refused)}; govern this "
            "session through the policy dispatcher in its harness tree"
        )
    limits = [
        name
        for name, value in (
            ("max_turns", request.max_turns),
            ("max_thinking_tokens", request.max_thinking_tokens),
        )
        if value is not None
    ]
    if limits:
        raise UnsupportedCapability(
            f"Codex cannot enforce {', '.join(limits)}; omit these limits and use "
            "effort for reasoning, or client timeout/budget middleware for a whole turn"
        )
    if request.cwd is None:
        raise ValueError("Codex sandboxes a session against a cwd; none was given")
    builtins = CodexBuiltins.compile(request.tools.builtin)
    return Codex(
        model=None if request.model is None else codex_model_choice(request.model),
        system_prompt=request.instructions,
        cwd=request.cwd,
        policy_root=project_root(),
        sandbox=request.sandbox,
        sandbox_mode=(
            None
            if request.autonomy is None or request.sandbox.posture().contained()
            else CODEX_AUTONOMY[request.autonomy]
        ),
        executable=request.contained_program or CODEX_PROGRAM,
        approval_policy=codex_hook_approval_policy(request.hooks),
        hooks=request.hooks,
        effort=(None if request.effort is None else CODEX_EFFORT[request.effort]),
        environment=request.environment,
        tools=CodexTools(builtin=request.tools.builtin, mcp=request.tools.mcp),
        writable_roots=[request.cwd] if builtins.write or builtins.shell else [],
        submission_gate_resolver=request.submission_gate,
    )


def codex_workspace_home(environment: EnvVars, workspace: Path) -> EnvVars:
    """Give one workspace's Codex sessions a home of their own.

    A home the environment already names is honoured as it stands: Codex
    seeds a scoped home by copying credentials into it, so deriving a second
    one underneath a home somebody selected deliberately would run the
    session against a copy of an account rather than the account.

    Naming a home is all this does. The project's own plugin is installed
    into it when a session opens, because installing is a package manager
    away and naming is asked for wherever a request is merely described —
    including where a request states something Codex refuses, which has to
    reach its refusal rather than dying on an install first.
    """
    return CODEX_LOGIN.environment(select_codex_home(None, environment, workspace).path)


CODEX_RUNTIME = Runtime(
    name="Codex",
    login=CODEX_LOGIN,
    open=codex_config,
    workspace_home=codex_workspace_home,
)
"""Codex, as the single value an application assigns to select it."""
