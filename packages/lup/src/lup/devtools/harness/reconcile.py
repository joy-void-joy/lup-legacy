"""Reconciliation of local drift against canonical Python source.

Classification reports how the working trees differ from the desired
generated trees without rewriting anything; the propose/apply pair persists
a reviewed source patch and applies it only while its recorded digests
still match the patch and the canonical source base.
"""

import hashlib
from pathlib import Path

import typer

from lup.execution.git import GitError, Repository
from lup.harness.proposals import ReconciliationMetadata, ReconciliationProposalWriter
from lup.harness.reconciliation import source_patch_base_digest
from lup.workspace.paths import project_root
from lup.devtools.harness.drift import generate_with_report, report_drift
from lup.harness.generate import NativeHarnessComposition, inspect_generation


def classify_targets(compositions: list[NativeHarnessComposition]) -> None:
    """Classify local differences without rewriting canonical Python source."""
    reports = [inspect_generation(composition.recipe) for composition in compositions]
    unresolved = False
    for report in reports:
        report_drift(report)
        unresolved = unresolved or bool(report.proposal.conflicts)
    if unresolved:
        typer.echo(
            "Unrecognized changes were preserved as conflicts; no arbitrary prompt "
            "or script content was reverse-engineered.",
            err=True,
        )
        raise typer.Exit(1)


def apply_proposal(
    proposal_id: str, compositions: list[NativeHarnessComposition]
) -> None:
    """Apply a stale-base-checked source patch, then regenerate both targets."""
    directory = project_root() / ".lup" / "reconcile" / proposal_id
    metadata = directory / "metadata.json"
    patch = directory / "source.patch"
    if not metadata.is_file() or not patch.is_file():
        raise typer.BadParameter(f"unknown reconciliation proposal {proposal_id!r}")
    try:
        record = ReconciliationMetadata.model_validate_json(
            metadata.read_text(encoding="utf-8")
        )
    except ValueError as error:
        raise typer.BadParameter("reconciliation metadata is malformed") from error
    if record.proposal_id != proposal_id:
        raise typer.BadParameter("reconciliation proposal identity does not match")
    actual = hashlib.sha256(patch.read_bytes()).hexdigest()
    if record.source_patch_sha256 != actual:
        raise typer.BadParameter("reconciliation patch digest is stale or malformed")
    content = patch.read_text(encoding="utf-8")
    try:
        base_digest = source_patch_base_digest(project_root(), content)
    except (OSError, ValueError) as error:
        raise typer.BadParameter("reconciliation source patch is malformed") from error
    if record.base_digest != base_digest:
        raise typer.BadParameter("reconciliation source base is stale")
    typer.echo(content)
    if not typer.confirm("Apply this canonical source patch and regenerate?"):
        raise typer.Abort()
    repository = Repository(project_root())
    try:
        repository.answer("apply", "--check", str(patch))
        repository.answer("apply", str(patch))
    except GitError as error:
        raise typer.BadParameter("reconciliation patch no longer applies") from error
    for composition in compositions:
        generate_with_report(composition)
    metadata.unlink()
    patch.unlink()
    directory.rmdir()


def propose_patch(patch: Path) -> None:
    """Persist a source patch for separate review and stale-base-checked apply."""
    if not patch.is_file():
        raise typer.BadParameter(f"source patch does not exist: {patch}")
    try:
        record = ReconciliationProposalWriter().write(
            project_root(), patch.read_text(encoding="utf-8")
        )
    except (OSError, UnicodeDecodeError, ValueError) as error:
        raise typer.BadParameter("reconciliation source patch is invalid") from error
    typer.echo(
        f"Reconciliation proposal {record.proposal_id} persisted; review it, then run "
        f"`uv run lup-devtools harness apply-reconciliation {record.proposal_id}`"
    )
