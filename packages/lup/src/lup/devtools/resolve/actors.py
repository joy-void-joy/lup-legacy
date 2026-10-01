"""Operator recovery for actor conversations parked across schema changes."""

from typing import Annotated

import typer

from lup.devtools.supervisor.doors import resolve_state_root
from lup.devtools.utils import refuse
from lup.resolver.actor_recovery import retire_actor_binding
from lup.resolver.state import ResolverStateRepository, StateTransitionError


def rebind_actor(
    actor: Annotated[
        str, typer.Argument(help="Exact actor address from resolve actors")
    ],
    run_id: Annotated[str, typer.Option("--run-id", help="Stopped run to recover")],
    reason: Annotated[
        str, typer.Option("--reason", help="Why its conversation is retired")
    ],
) -> None:
    """Retire one binding so resume opens a fresh conversation with current schemas.

    The prior binding and reason remain in the journal. Conversation memory is
    lost; the run's questions, answers, join checkpoints and worktrees are kept.
    A live run refuses this operation: park or stop it before rebinding.
    """
    try:
        event = retire_actor_binding(
            ResolverStateRepository(resolve_state_root(), run_id), actor, reason
        )
    except (StateTransitionError, ValueError) as error:
        refuse(str(error), what=actor, code=2)
    typer.echo(
        f"Retired {event.previous.actor.label()}; resume {run_id} to open a fresh "
        "conversation. The prior binding is preserved in journal.jsonl."
    )
