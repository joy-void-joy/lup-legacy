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
from lup.workspace.checkout_state import CheckoutState
from lup.harness.reconciliation import source_patch_base_digest
from lup.policy.kernel.diagnostic import devtools, step
from lup.workspace.paths import project_root
from lup.devtools.harness.drift import generate_with_report, report_drift
from lup.diagnostics import refuse
from lup.harness.generate import NativeHarnessComposition, inspect_generation


def classify_targets(compositions: list[NativeHarnessComposition]) -> None:
    """Classify local differences without rewriting canonical Python source."""
    reports = [inspect_generation(composition.recipe) for composition in compositions]
    unresolved = False
    for report in reports:
        report_drift(report)
        unresolved = unresolved or bool(report.proposal.conflicts)
    if unresolved:
        refuse(
            "unrecognized changes were preserved as conflicts, and no prompt or"
            " script content was reverse-engineered from them",
            steps=[
                step(
                    "write the change as a patch to the canonical Python source,"
                    " and put it up for review",
                    devtools("harness", "propose-reconciliation", "<patch>"),
                )
            ],
        )


def apply_proposal(
    proposal_id: str, compositions: list[NativeHarnessComposition]
) -> None:
    """Apply a stale-base-checked source patch, then regenerate both targets."""
    directory = CheckoutState(root=project_root()).reconcile() / proposal_id
    metadata = directory / "metadata.json"
    patch = directory / "source.patch"
    if not metadata.is_file() or not patch.is_file():
        refuse("is no reconciliation proposal on record", what=proposal_id, code=2)
    try:
        record = ReconciliationMetadata.model_validate_json(
            metadata.read_text(encoding="utf-8")
        )
    except ValueError:
        refuse("its recorded metadata is malformed", what=proposal_id, code=2)
    if record.proposal_id != proposal_id:
        refuse("its recorded metadata names another proposal", what=proposal_id, code=2)
    actual = hashlib.sha256(patch.read_bytes()).hexdigest()
    if record.source_patch_sha256 != actual:
        refuse(
            "its patch no longer matches the digest recorded for it",
            what=proposal_id,
            code=2,
        )
    content = patch.read_text(encoding="utf-8")
    try:
        base_digest = source_patch_base_digest(project_root(), content)
    except (OSError, ValueError) as error:
        refuse(f"its source patch is malformed: {error}", what=proposal_id, code=2)
    if record.base_digest != base_digest:
        refuse(
            "was made against a source base that has since moved",
            what=proposal_id,
            steps=[
                step(
                    "propose the patch again over the source as it stands",
                    devtools("harness", "propose-reconciliation", "<patch>"),
                )
            ],
            code=2,
        )
    typer.echo(content)
    if not typer.confirm("Apply this canonical source patch and regenerate?"):
        raise typer.Abort()
    repository = Repository(project_root())
    try:
        repository.answer("apply", "--check", str(patch))
        repository.answer("apply", str(patch))
    except GitError:
        refuse(
            "its patch no longer applies",
            what=proposal_id,
            steps=[
                step(
                    "propose the patch again over the source as it stands",
                    devtools("harness", "propose-reconciliation", "<patch>"),
                )
            ],
            code=2,
        )
    for composition in compositions:
        generate_with_report(composition)
    metadata.unlink()
    patch.unlink()
    directory.rmdir()


def propose_patch(patch: Path) -> None:
    """Persist a source patch for separate review and stale-base-checked apply."""
    if not patch.is_file():
        refuse(
            "does not exist, so there is no patch to propose", what=str(patch), code=2
        )
    try:
        record = ReconciliationProposalWriter().write(
            project_root(), patch.read_text(encoding="utf-8")
        )
    except (OSError, UnicodeDecodeError, ValueError) as error:
        refuse(
            f"is not a patch this repository's canonical source can take: {error}",
            what=str(patch),
            code=2,
        )
    typer.echo(
        f"Reconciliation proposal {record.proposal_id} persisted; review it, then run "
        f"`uv run lup-devtools harness apply-reconciliation {record.proposal_id}`"
    )
