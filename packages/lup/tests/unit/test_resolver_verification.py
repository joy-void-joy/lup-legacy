"""What a failing verification records, and what its rejection tells the next round."""

from pathlib import Path

from lup.execution.process import LocalProcessLauncher
from lup.resolver.models import VerificationCommand, VerificationRecord
from lup.resolver.verification import (
    Verifier,
    rejection_reason,
    verification_output,
)


def test_a_failing_check_keeps_what_it_said(tmp_path: Path) -> None:
    """The verdict and the evidence for it are recorded together.

    Reproducing a check to learn why it failed needs the lease worktree it
    ran in, and a run holds that worktree until it finishes — so a verdict
    without its output is one a later session cannot investigate at all.
    """
    verifier = Verifier(
        [
            VerificationCommand(
                name="dev check",
                arguments=["sh", "-c", "echo 'abc-capability profiles.py:23'; exit 1"],
            )
        ],
        LocalProcessLauncher(),
    )

    records = verifier.verify(tmp_path)

    assert not records[0].passed
    assert "abc-capability profiles.py:23" in records[0].output


def test_a_passing_check_keeps_nothing(tmp_path: Path) -> None:
    """Only a failure is worth the bytes; a green gate has nothing to explain."""
    verifier = Verifier(
        [VerificationCommand(name="dev check", arguments=["sh", "-c", "echo fine"])],
        LocalProcessLauncher(),
    )

    records = verifier.verify(tmp_path)

    assert records[0].passed
    assert records[0].output == ""


def test_a_rejection_names_the_finding_rather_than_the_gate() -> None:
    """Three concerns once re-derived one finding because the reason omitted it.

    This string is handed to the next worker round as the feedback to revise
    against, so a gate restating its own name asks for a revision without
    saying what to revise.
    """
    reason = rejection_reason(
        [
            VerificationRecord(
                name="dev check",
                arguments=["uv", "run", "lup-devtools", "dev", "check"],
                passed=False,
                exit_code=1,
                output="abc-capability  runtime/profiles.py:23",
            )
        ]
    )

    assert reason.startswith("verification failed: dev check")
    assert "runtime/profiles.py:23" in reason


def test_a_long_check_is_kept_by_its_tail() -> None:
    """A gate running several tools reports the one that failed last."""
    kept = verification_output("\n".join(str(row) for row in range(100)), "", lines=3)

    assert kept.splitlines() == ["97", "98", "99"]
