"""Environment CLI for running agent sessions.

This is a TEMPLATE. Customize for your domain.

The CLI is the domain-specific harness that:
1. Handles user interaction or game logic
2. Runs agent sessions with inputs
3. Manages application flow and lifecycle

**The commit loop is optional** (see CLAUDE.md § Scaffolding Is a Menu, Not a
Mandate): ``loop`` can auto-commit each session's outputs so a batch run leaves
a per-session audit trail. Keep it when every run yields a data artifact worth
versioning (forecasts, game records, generated files); for interactive or
no-artifact domains it is just noise — pass ``--no-commit`` or remove the
auto-commit wiring entirely.

The feedback loop focuses on improving lup_template.agent.
This code evolves with application requirements.

Usage:
    uv run lup run "your task here"
    uv run lup run --session-id my-session "task"
    uv run lup loop "task1" "task2" "task3"
"""

import asyncio
import logging
from typing import Annotated

import sh
import typer

import lup.workspace.paths
from lup.execution.shell import git
from lup.observability.display import format_duration
from lup.sessions.events import SessionId

from lup_template.agent.config import settings
from lup_template.agent.core import run_agent
from lup_template.agent.models import AgentSessionResult

logger = logging.getLogger(__name__)

app = typer.Typer(
    name="lup",
    help="Self-improving agent CLI",
    no_args_is_help=True,
    add_completion=False,
)


@app.callback(invoke_without_command=True)
def callback(ctx: typer.Context) -> None:
    """Self-improving agent CLI.

    Wires AGENT_NOTES_PATH / AGENT_LOGS_PATH (``settings.notes_path`` /
    ``settings.logs_path``) into ``lup.workspace.paths`` so all session data —
    sessions, outputs, and trace logs — lands under the configured
    directories. The overrides are always applied: relative values
    (including the "./notes" and "./logs" defaults) are anchored at the
    project root, so default behavior is unchanged regardless of cwd,
    while absolute env values move the data wholesale.
    """
    root = lup.workspace.paths.project_root()
    lup.workspace.paths.configure(
        notes_dir=(root / settings.notes_path).resolve(),
        logs_dir=(root / settings.logs_path).resolve(),
    )
    if ctx.invoked_subcommand is None:
        raise typer.Exit()


async def run_session(
    task: str,
    *,
    session_id: str | None = None,
    resume: SessionId | None = None,
) -> AgentSessionResult:
    """Run an agent session with the given task.

    This is the main entry point for the environment harness.
    Customize this for your domain's needs.

    Args:
        task: The task/prompt for the agent.
        session_id: Optional session identifier.
        resume: Opaque provider session identity to continue.

    Returns:
        AgentSessionResult with the agent's output and metadata.
    """
    logger.info("Starting session with model: %s", settings.model)

    typer.echo(f"\n{'─' * 60}")
    typer.echo(task)
    typer.echo(f"{'─' * 60}\n")

    result = await run_agent(
        task,
        session_id=session_id,
        resume=resume,
    )

    logger.info(
        "Session %s completed (cost: $%.4f, duration: %.1fs)",
        result.session_id,
        result.cost_usd or 0,
        result.duration_seconds or 0,
    )

    return result


# lup: template: customize the commit message/scope, or remove auto-commit.
def commit_results() -> None:
    """Commit any uncommitted session results.

    This commits session outputs (notes/traces/) after each run. For
    domains like forecasting, game playing, or batch processing, it keeps
    data commits atomic and automatic; interactive domains (e.g. coaching)
    usually drop it.
    """
    status = str(git.status("--porcelain", "--", "notes/", _ok_code=[0]))
    if not status.strip():
        return

    try:
        git.add("notes/")
        # --quiet exits 1 exactly when something is staged — git's own probe.
        staged = git.diff("--cached", "--quiet", _ok_code=[0, 1], _return_cmd=True)
        if staged.exit_code == 1:
            git.commit("-m", "data(sessions): auto-commit session results")
            typer.echo("Committed session results.")
    except sh.ErrorReturnCode as e:
        logger.warning("Auto-commit failed: %s", e)


def print_result(result: AgentSessionResult) -> None:
    """Print a session result summary."""
    typer.echo(f"\nSession: {result.session_id}")
    typer.echo(f"Output: {result.output.summary}")
    typer.echo(f"Confidence: {result.output.confidence:.1%}")
    if result.cost_usd:
        typer.echo(f"Cost: ${result.cost_usd:.4f}")
    if result.duration_seconds:
        typer.echo(f"Duration: {format_duration(result.duration_seconds)}")


@app.command()
def run(
    task: Annotated[str, typer.Argument(help="The task for the agent to perform")],
    session_id: Annotated[
        str | None,
        typer.Option("--session-id", "-s", help="Optional session identifier"),
    ] = None,
    resume: Annotated[
        str | None,
        typer.Option(
            "--resume",
            help="Continue a previous run's conversation: a saved session "
            "name (looked up in history) or a raw engine session id",
        ),
    ] = None,
    persistent: Annotated[
        bool,
        typer.Option(
            "--persistent",
            help="Persistent (sleep/wake) session via the file relay — "
            "AGENT_SDK=codex/openai; replies print to stdout "
            "(see docs/orchestration.md, Persistent Agent)",
        ),
    ] = False,
    verbose: Annotated[
        bool,
        typer.Option("--verbose", "-v", help="Enable verbose logging"),
    ] = False,
) -> None:
    """Run a single agent session."""
    if verbose:
        logging.basicConfig(level=logging.DEBUG)
    else:
        logging.basicConfig(level=logging.INFO)

    if persistent:
        from lup_template.agent.core import run_persistent_agent

        persistent_result = asyncio.run(
            run_persistent_agent(task, session_id=session_id)
        )
        typer.echo(
            f"\nPersistent session ended after {persistent_result.turns} turn(s)."
        )
        return

    resume_token = None
    if resume is not None:
        from lup_template.agent.core import resolve_resume_token

        resume_token = resolve_resume_token(resume)

    result = asyncio.run(run_session(task, session_id=session_id, resume=resume_token))
    print_result(result)


# lup: template: adapt the task format and post-processing for your domain.
@app.command()
def loop(
    tasks: Annotated[list[str], typer.Argument(help="Tasks for the agent to perform")],
    verbose: Annotated[
        bool,
        typer.Option("--verbose", "-v", help="Enable verbose logging"),
    ] = False,
    auto_commit: Annotated[
        bool,
        typer.Option(
            "--commit/--no-commit", help="Auto-commit results after each task"
        ),
    ] = True,
) -> None:
    """Run multiple agent sessions and auto-commit results.

    This is the primary entry point for batch processing: for a
    forecasting bot, tasks might be question IDs; for a game-playing
    agent, game configs.

    Example:
        uv run lup loop "task1" "task2" "task3"
    """
    if verbose:
        logging.basicConfig(level=logging.DEBUG)
    else:
        logging.basicConfig(level=logging.INFO)

    results: list[AgentSessionResult] = []
    total_cost = 0.0

    for i, task in enumerate(tasks, 1):
        typer.echo(f"\n{'=' * 60}")
        typer.echo(f"Task {i}/{len(tasks)}: {task}")
        typer.echo(f"{'=' * 60}")

        try:
            result = asyncio.run(run_session(task))
            results.append(result)
            total_cost += result.cost_usd or 0
            print_result(result)
        except RuntimeError as e:
            typer.echo(f"Error: {e}", err=True)
            continue
        # Batch isolation: one failed task must not abort the remaining
        # tasks; the error is logged with traceback.
        except Exception as e:
            typer.echo(f"Unexpected error: {e}", err=True)
            logger.exception("Unexpected error on task %d/%d", i, len(tasks))
            continue

        if auto_commit:
            commit_results()

    typer.echo(f"\n{'=' * 60}")
    typer.echo(f"Completed {len(results)}/{len(tasks)} sessions")
    typer.echo(f"Total cost: ${total_cost:.4f}")


if __name__ == "__main__":
    app()
