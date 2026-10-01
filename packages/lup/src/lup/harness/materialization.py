"""The pipeline's final stage: apply one conflict-free proposal atomically.

Re-verifies every proposed write and deletion against its recorded preimage,
then replaces owned files atomically. Composed by the devtools generation
flow through the ``Materializer`` seam; its ``MaterializationResult`` is
defined here because materializers are the only producers.
"""

import hashlib
import os
from collections.abc import Callable, Iterable
from pathlib import Path

from pydantic import BaseModel

from lup.channels.models import write_atomic
from lup.harness.contracts import Materializer
from lup.harness.models import Artifact
from lup.harness.reconciliation import ReconciliationProposal
from lup.harness.validation import validated_tree


class MaterializationResult(BaseModel, frozen=True):
    changed: list[Path]
    removed: list[Path]


class MaterializationConflictError(RuntimeError):
    """A proposal is unresolved or stale at materialization time."""


class MaterializationRefusedError(RuntimeError):
    """The environment refused a write the proposal was entitled to make.

    Separate from a conflict because nothing about the proposal is wrong:
    the artifact is correct, the base is current, and the boundary the
    session runs behind is what said no.
    """


def mounted_read_only(path: Path) -> bool:
    """Whether ``path`` sits on a read-only mount, asked of the nearest place that exists.

    Asked of the mount rather than of the file's mode: a container holds a
    tree read-only by binding it, which leaves every permission bit as it
    was and refuses the write with a busy device or a read-only filesystem.
    """
    existing = next(
        (candidate for candidate in [path, *path.parents] if candidate.exists()), None
    )
    if existing is None:
        return False
    try:
        return bool(os.statvfs(existing).f_flag & os.ST_RDONLY)
    except OSError:
        return False


def held_read_only(
    paths: Iterable[Path],
    mounted_read_only: Callable[[Path], bool] = mounted_read_only,
) -> list[Path]:
    """Every one of ``paths`` a mount holds read-only here, in the order given."""
    return [path for path in paths if mounted_read_only(path)]


def refused_write(error: OSError) -> MaterializationRefusedError:
    """Name the boundary behind a refused artifact write.

    A runtime protects its own configuration paths by mounting them rather
    than by permissioning them, so a sandboxed session replacing one is
    refused with a busy device or a read-only filesystem — an errno about
    hardware, standing in for a boundary decision. Saying which boundary is
    what turns it back into something a reader can act on.
    """
    target = error.filename2 or error.filename or "a generated artifact"
    return MaterializationRefusedError(
        f"{target}: {error.strerror}. The artifact is correct and its base is "
        "current, so the environment is what refused the write: an OS sandbox "
        "denies the paths its own runtime protects, and no grant lifts those. "
        "Regenerate from a session outside the sandbox."
    )


def file_digest(path: Path) -> str | None:
    """Hash a file if it currently exists."""
    if not path.exists():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def safe_target(root: Path, relative: Path) -> Path:
    """Resolve a managed path while rejecting a symlink escape from its root."""
    resolved_root = root.resolve()
    candidate = (root / relative).resolve()
    if not candidate.is_relative_to(resolved_root):
        raise MaterializationConflictError(
            f"managed path escapes its root through a symlink: {relative}"
        )
    return candidate


def prune_empty_parents(root: Path, path: Path) -> None:
    """Remove directories a deletion left empty, never ascending past the root."""
    resolved_root = root.resolve()
    for parent in path.parents:
        if parent == resolved_root or not parent.is_relative_to(resolved_root):
            return
        if any(parent.iterdir()):
            return
        parent.rmdir()


def write_generated_file(
    artifact: Artifact, root: Path, command: str, *, check: bool
) -> Path:
    """Write or verify one artifact belonging to no runtime's tree.

    Most generated files are reconciled against the ownership manifest of the
    tree they belong to. A repository-wide one belongs to none of them, so the
    whole of its proof is that what is on disk is what the declaration renders,
    and the recovery is the command that renders it.
    """
    destination = safe_target(root, artifact.path)
    expected = validated_tree([artifact]).artifacts[0].content
    if check:
        if (
            not destination.is_file()
            or destination.read_text(encoding="utf-8") != expected
        ):
            raise RuntimeError(f"{destination} is stale; run `{command}`")
        return destination
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(expected, encoding="utf-8", newline="\n")
    return destination


class AtomicMaterializer(Materializer):
    """Replace changed owned files atomically and delete only with prior proof."""

    def apply(self, proposal: ReconciliationProposal) -> MaterializationResult:
        if proposal.conflicts:
            paths = ", ".join(
                conflict.path.as_posix() for conflict in proposal.conflicts
            )
            raise MaterializationConflictError(
                f"proposal {proposal.id} has unresolved conflicts: {paths}"
            )
        for write in proposal.writes:
            path = safe_target(proposal.root, write.artifact.path)
            actual = file_digest(path)
            if actual != write.previous_sha256:
                raise MaterializationConflictError(
                    f"stale base for {write.artifact.path}: expected "
                    f"{write.previous_sha256!r}, found {actual!r}"
                )
            if path.exists() and write.previous_executable is not None:
                executable = bool(path.stat().st_mode & 0o111)
                if executable != write.previous_executable:
                    raise MaterializationConflictError(
                        f"stale executable mode for {write.artifact.path}"
                    )
        for deletion in proposal.deletes:
            path = safe_target(proposal.root, deletion.path)
            actual = file_digest(path)
            if actual != deletion.prior_ownership_sha256:
                raise MaterializationConflictError(
                    f"ownership proof changed for {deletion.path}"
                )

        changed: list[Path] = []  # lup: ignore[empty-collection]
        for write in proposal.writes:
            artifact = write.artifact
            path = safe_target(proposal.root, artifact.path)
            write_atomic(
                path,
                artifact.content.encode("utf-8"),
                mode=0o755 if artifact.executable else 0o644,
            )
            changed.append(artifact.path)

        removed: list[Path] = []  # lup: ignore[empty-collection]
        for deletion in proposal.deletes:
            path = safe_target(proposal.root, deletion.path)
            path.unlink()
            prune_empty_parents(proposal.root, path)
            removed.append(deletion.path)
        return MaterializationResult(changed=changed, removed=removed)
