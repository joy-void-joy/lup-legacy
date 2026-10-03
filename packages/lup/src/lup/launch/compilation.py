"""The launch fields every runtime compiles alike, for a session opened here or launched.

Each function answers one field the same way whichever output reads it, so
the SDK options a program opens a session with and the command a person
launches carry one meaning for it.
"""

from pathlib import Path
from collections.abc import Iterator
from contextlib import contextmanager

from lup.harness.enforcement import semantic_policy_for
from lup.harness.environment import inherited, non_interactive_environment
from lup.harness.models import HookSet
from lup.launch.session import start_harness_transcript
from lup.observability.native import NativeTranscripts
from lup.launch.declaration import Recording, Sandbox
from lup.policy.enforcement import NativeSemantics, create_policy_hooks
from lup.policy.hooks import LupHooksConfig
from lup.sessions.recursion import (
    MAX_RECURSIVE_AGENT_ENV,
    child_recursive_agent_allowance,
)
from lup.types import EnvVars


def allowance_environment(declared: int | None, inherited: EnvVars) -> EnvVars:
    """The recursive-agent allowance a session opens with, as the variable carrying it.

    A session is one level below whatever opened it, so the allowance
    ``inherited`` holds is spent by one first — from a person's terminal that
    is no limit at all, and inside a session running out of levels it is the
    refusal that says so. A declared allowance narrows that and never widens
    it: a declaration cannot hand a session more levels than the process
    opening it has left to give. ``-1`` is no limit on either side.
    """
    remaining = child_recursive_agent_allowance(inherited).remaining
    if declared is not None:
        remaining = min(
            (bound for bound in (declared, remaining) if bound != -1), default=-1
        )
    return {MAX_RECURSIVE_AGENT_ENV: str(remaining)}


def semantic_hooks(
    policy: HookSet, sandbox: Sandbox, semantics: NativeSemantics
) -> LupHooksConfig:
    """The declared policy as in-process hooks, judging a session opened here.

    The same declaration a launched session's plugin compiles into its
    dispatcher, read against the wall this session actually opens behind, so
    an escape is judged escapable exactly where the sandbox lets one through.
    Nobody answers a question a session opened in process asks, so the
    policy is composed for a session with no human at it.
    """
    posture = sandbox.enforcement()
    return create_policy_hooks(
        semantic_policy_for(
            policy,
            sandbox_active=posture.active,
            escapable=posture.escapable,
            contained=posture.contained,
            interactive=False,
        ),
        semantics.also_refusing(policy.refused_tools),
        sandbox=posture,
    )


def inherited_environment() -> EnvVars:
    """The environment a launched CLI starts from: this process's, made non-interactive.

    Read here, once, because a launch is a child of this process and inherits
    what it runs under: the PATH that finds the CLI, the terminal it draws on.
    What the declaration sets is laid over it by each runtime's compilation.
    """
    return non_interactive_environment(inherited())


@contextmanager
def kept_record(
    provider: str,
    transcripts: NativeTranscripts,
    root: Path,
    record: Recording | None,
    model: str | None,
    profile: str | None,
) -> Iterator[None]:
    """Keep what a declaration records of one session opened here, for as long as it is open.

    The same record a launched session keeps — the run's journal, the
    runtime's transcript rendered as it is written, the ledger's account of
    the session opening and closing — so a session is recorded the same
    whichever way it was opened. Nothing is kept where nothing was declared.
    """
    if record is None:
        yield
        return
    transcript = start_harness_transcript(
        provider,
        transcripts,
        root,
        model=model,
        profile=profile,
        arguments=[],
        record_root=record.root,
        transcribe=record.transcript,
        mode=record.mode,
        recorder=record.ledger,
    )
    succeeded = False
    try:
        yield
        succeeded = True
    finally:
        transcript.close(succeeded=succeeded)
