"""Running one tree through the whole verification set a run was given.

Three phases ask this question of three different trees — a concern's own
worktree before it is reviewed, the tree each join produces, and the
integrated result — and none of them is about the others. Naming the check
itself is what lets each hold the answer without holding the phase that
asked it first.
"""

from pathlib import Path

from lup.execution.process import LaunchRequest, ProcessLauncher
from lup.resolver.models import VerificationCommand, VerificationRecord


VERIFICATION_OUTPUT_LINES = 40
"""How much of a failing check to keep beside its verdict.

The tail rather than the head: a gate that runs several tools reports the
one that failed last, and its own summary lines come after everything that
passed. Our judgement about what a reader needs, so a caller replaces it.
"""


def verification_output(
    stdout: str, stderr: str, lines: int = VERIFICATION_OUTPUT_LINES
) -> str:
    """The tail of what a failing check said, both streams in order."""
    combined = "\n".join(part for part in (stdout.strip(), stderr.strip()) if part)
    kept = combined.splitlines()[-lines:]
    return "\n".join(kept)


def rejection_reason(broke: list[VerificationRecord]) -> str:
    """State what each failing check saw, not just what it is called.

    This string is read twice: by whoever debugs the rejection weeks later,
    and by the next worker round, which is handed it as the feedback to
    revise against. A gate restating its own name is feedback neither can
    act on, and re-deriving it costs a worker turn each time.
    """
    named = ", ".join(record.name for record in broke)
    said = [
        f"\n\n{record.name} (exit {record.exit_code}) said:\n{record.output}"
        for record in broke
        if record.output
    ]
    return f"verification failed: {named}" + "".join(said)


class Verifier:
    """The run's verification commands, and one tree at a time to run them on."""

    def __init__(
        self, commands: list[VerificationCommand], launcher: ProcessLauncher
    ) -> None:
        self.commands = commands
        self.launcher = launcher

    def verify(self, root: Path, base: str = "") -> list[VerificationRecord]:
        """Run the whole verification set against one tree, from its own base.

        The full set every time, never a fast subset. Per-join verification
        is the only mechanical detector of a clean merge that is jointly
        wrong — one branch changes a signature, another adds a caller, and
        the type error exists in neither parent alone — and a subset chosen
        for speed is exactly the one that misses it.

        The base is the tree's, not the run's. A gate scoped to what changed
        has to be told where this tree started, and a run whose leases start
        from different commits has no single answer to give it once.
        """
        records: list[VerificationRecord] = []  # lup: ignore[empty-collection]
        for command in self.commands:
            arguments = command.against(base)
            status = self.launcher.launch(LaunchRequest(arguments=arguments, cwd=root))
            records.append(
                VerificationRecord(
                    name=command.name,
                    arguments=arguments,
                    passed=status.code == 0,
                    exit_code=status.code,
                    output=verification_output(status.stdout, status.stderr)
                    if status.code != 0
                    else "",
                )
            )
        return records
