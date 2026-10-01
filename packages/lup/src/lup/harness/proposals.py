"""Backpropagation persistence: source patches parked on disk for review.

When reconciliation finds local edits worth carrying back into canonical
sources, the devtools reconcile flow renders them as a git patch and this
module persists it immutably under ``.lup/reconcile/<id>`` with preimage
digests. Nothing here applies or imports a patch — a human reviews the
persisted proposal and applies it explicitly.
"""

import hashlib
from pathlib import Path

from pydantic import BaseModel

from lup.channels.models import write_atomic
from lup.workspace.checkout_state import CheckoutState
from lup.harness.reconciliation import source_patch_base_digest


class ReconciliationMetadata(BaseModel, frozen=True):
    proposal_id: str
    base_digest: str
    source_patch_sha256: str


class ReconciliationProposalWriter:
    """Persist one immutable source patch without applying or importing it."""

    def write(self, root: Path, source_patch: str) -> ReconciliationMetadata:
        if not source_patch.endswith("\n"):
            source_patch += "\n"
        patch_sha256 = hashlib.sha256(source_patch.encode("utf-8")).hexdigest()
        proposal_id = patch_sha256[:16]
        record = ReconciliationMetadata(
            proposal_id=proposal_id,
            base_digest=source_patch_base_digest(root, source_patch),
            source_patch_sha256=patch_sha256,
        )
        directory = CheckoutState(root=root).reconcile() / proposal_id
        directory.mkdir(parents=True, exist_ok=True)
        if not directory.resolve().is_relative_to(root.resolve()):
            raise ValueError("reconciliation proposal directory escapes its root")
        patch_path = directory / "source.patch"
        metadata_path = directory / "metadata.json"
        expected_patch = source_patch.encode("utf-8")
        expected_metadata = (record.model_dump_json(indent=2) + "\n").encode("utf-8")
        for path, expected in [
            (patch_path, expected_patch),
            (metadata_path, expected_metadata),
        ]:
            if path.exists() and path.read_bytes() != expected:
                raise FileExistsError(f"reconciliation proposal collision at {path}")
        self.write_once(patch_path, expected_patch)
        self.write_once(metadata_path, expected_metadata)
        return record

    def write_once(self, path: Path, content: bytes) -> None:
        """Atomically create one proposal member when it is not already exact."""
        if path.exists():
            return
        write_atomic(path, content)
