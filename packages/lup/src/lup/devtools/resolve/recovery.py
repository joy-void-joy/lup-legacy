"""Operator command for a stopped resolver's integration recovery."""

from typing import Annotated

import typer

from lup.devtools.supervisor.doors import resolve_state_root
from lup.devtools.utils import refuse
from lup.resolver.recovery import IntegrationRecoveryDesk, IntegrationRecoveryMode
from lup.resolver.state import ResolverStateRepository


def recover_integration(
    mode: Annotated[
        IntegrationRecoveryMode,
        typer.Argument(
            help="Restore the recorded join or adopt a committed descendant repair"
        ),
    ],
    run_id: Annotated[str, typer.Option("--run-id", help="Stopped run to recover")],
) -> None:
    """Reconcile integration explicitly, retaining answers and completed concern work."""
    repository = ResolverStateRepository(resolve_state_root(), run_id)
    try:
        report = IntegrationRecoveryDesk(repository).recover(mode)
    except (OSError, RuntimeError, ValueError) as error:
        refuse(str(error), what=run_id, code=2)
    typer.echo(report.model_dump_json(indent=2))
    typer.echo("Recovery recorded. Resume the run to continue its verification.")
