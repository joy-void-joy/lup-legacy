"""Exercise the Python sandbox through its container and persistent REPL."""

from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from lup.harness.requirements import ExerciseOutcome


def check_sandbox(image: str | None = None) -> ExerciseOutcome:
    """Start an isolated sandbox, evaluate arithmetic, and remove what it created.

    The sandbox has no network and installs no packages. Its normal startup
    still creates the container, copies the REPL, and opens the execution
    transport: a reachable daemon alone cannot establish any of those facts.
    """
    try:
        from lup.sandbox.container import Sandbox
    except ImportError as error:
        return ExerciseOutcome(
            proved=False,
            exercised=False,
            detail=f"Sandbox support could not load: {error}. Install lup-agents[docker].",
        )
    with TemporaryDirectory(prefix="lup-sandbox-check-") as directory:
        sandbox = Sandbox(
            session_id=f"check-{uuid4().hex}",
            shared_dir=Path(directory),
            docker_image=image or Sandbox.DEFAULT_DOCKER_IMAGE,
            network_mode="none",
            pre_install=None,
        )
        outcome = ExerciseOutcome(
            proved=False, detail="Sandbox startup did not finish."
        )
        stage = "startup before evaluating the expression"
        try:
            sandbox.start()
            stage = "expression evaluation"
            result = sandbox.run_code("1 + 1", timeout_seconds=10)
            outcome = ExerciseOutcome(
                proved=result.exit_code == 0 and result.result == "2",
                detail=(
                    "Sandbox evaluated 1 + 1 = 2 through its Python REPL."
                    if result.exit_code == 0 and result.result == "2"
                    else f"Sandbox expression did not produce 2: {result.model_dump_json()}"
                ),
            )
        except Exception as error:
            outcome = ExerciseOutcome(
                proved=False,
                detail=f"Sandbox {stage} failed: {type(error).__name__}: {error}",
            )
        finally:
            try:
                sandbox.stop()
            except Exception as error:
                outcome = ExerciseOutcome(
                    proved=False,
                    detail=f"{outcome.detail}\nSandbox cleanup failed: {error}",
                )
        return outcome
