"""Classifying one command exactly as a live session would, outside a session.

Two callers ask the same question and must not answer it differently: the
`hooks` tree, where somebody asks what a command earns before spending a turn
on it, and the everyday sweep in `dev check`, which asks the same of a whole
declared corpus. A sweep reaching its verdict by a second route would be
checking a policy nobody runs.
"""

from collections.abc import Sequence

from lup.harness.enforcement import semantic_policy_for
from lup.harness.models import HookSet
from lup.policy.everyday import SESSION_SHAPES, SessionShape
from lup.policy.kernel.diagnostic import stated
from lup.policy.models import Decision, ShellCommand
from lup.workspace.paths import project_root

from pydantic import BaseModel


def shell_decision(
    hooks: HookSet,
    command: str,
    autonomous: bool = False,
    interactive: bool = True,
    trapped: bool = False,
    contained: bool = False,
) -> Decision:
    """Classify one shell command exactly as a live session would.

    The host facts a decision needs — which redirect targets already exist,
    which operands Git could restore, which are directories — are resolved by
    the policy itself against ``cwd``, so this answer is the answer a session
    standing here would get rather than one reached without them.

    ``trapped`` asks the other question a placement raises: what a session an
    OS sandbox confines, on a runtime that puts no single call outside it, is
    told about a command that has to run outside. Left off, the answer is the
    declared verdict — the placement itself, which is what a reader wants to
    see. Turned on, it is the refusal that boundary reaches.

    The undo layer is reported as present either way, because it is: a session
    snapshots the tree before every command, and a reader asking what they will
    be asked about should be told what a session is told.

    ``contained`` is the other wall: the container `outer` measures around the
    whole session, with the runtime's sandbox off inside it. It comes from the
    posture rather than the ledger this process runs behind, because the
    shapes swept here are postures rather than readings of this session.
    Taken from the measurement, a shape named for no container inherited the
    one around the sweep and reported the bounded answer under the unbounded
    name — so the corpus could only be swept honestly from a checkout no
    contained session had ever launched in, which is the one place nobody
    runs it.
    """
    policy = semantic_policy_for(
        hooks,
        autonomous=autonomous,
        interactive=interactive,
        sandbox_active=trapped,
        recovered=True,
        contained=contained,
        inside_placement=contained,
    )
    return policy.decide(ShellCommand(command=command, cwd=project_root()))


class StoppedCommand(BaseModel, frozen=True):
    """One everyday command the declared table does not let through.

    The family travels with it because the family is the claim: a reader
    shown `git diff --stat` has to reconstruct why it mattered, and one shown
    it under "asking git what happened" is told what broke. The posture
    travels for the other half of the same reason: a command stopped only for
    a worker session is a different defect from one stopped for everybody.
    """

    what: str
    shape: str
    command: str
    effect: str
    reason: str


def stopped_everyday(
    hooks: HookSet, shapes: Sequence[SessionShape] = SESSION_SHAPES
) -> list[StoppedCommand]:
    """Every declared everyday command this policy would not simply allow.

    Empty is the passing answer, and the only one. A corpus of commands that
    must keep allowing has no partial credit: an ask is a session interrupted
    for something nobody meant to interrupt, and a deny is one refused.

    Swept once per posture, because a verdict is only ever reached for
    somebody. A worker session and a contained one take rows an interactive
    session never reaches, so reading one posture measures a claim narrower
    than the one the corpus makes.
    """
    return [
        StoppedCommand(
            what=family.what,
            shape=shape.what,
            command=command,
            effect=decision.effect,
            reason=stated(decision.subject, decision.reason),
        )
        for shape in shapes
        for family in hooks.everyday_commands
        for command in family.commands
        for decision in [
            shell_decision(
                hooks,
                command,
                autonomous=shape.autonomous,
                interactive=shape.interactive,
                trapped=shape.trapped,
                contained=shape.contained,
            )
        ]
        if decision.effect != "allow"
    ]
