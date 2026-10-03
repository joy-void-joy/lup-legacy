"""Ownership proof: which on-disk files the generator owns, hashed and stored.

Defines the ``OwnershipCategory`` vocabulary every later stage uses to talk
about a file's provenance, and builds, loads, and atomically saves the
manifest recorded after each successful materialization.
:mod:`lup.harness.reconciliation` classifies the current tree against this
proof, and the devtools generation flow persists it.
"""

from collections.abc import Collection, Iterator
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ValidationError

from lup.channels.models import publish_atomic
from lup.formats import digest
from lup.harness.models import ArtifactTree, Harness

type OwnershipCategory = Literal[
    "generated",
    "backpropagation_candidate",
    "local_only",
    "sensitive_local_only",
    "unknown_conflict",
    "obsolete_generated",
]


class OwnedArtifact(BaseModel, frozen=True):
    path: Path
    category: OwnershipCategory
    sha256: str
    semantic_id: str
    executable: bool = False


class OwnershipManifest(BaseModel, frozen=True):
    schema_version: int
    generator_version: str
    source_digest: str
    target_requirements: list[str]
    files: list[OwnedArtifact]


class OwnershipManifestError(RuntimeError):
    """Persisted ownership proof cannot be decoded as its schema."""


def source_digest(source: Harness) -> str:
    """Hash canonical declarations independently from rendered native output."""
    return digest.text(source.model_dump_json(exclude_none=True))


def build_manifest(
    source: Harness,
    tree: ArtifactTree,
    *,
    generator_version: str,
    target_requirements: list[str],
) -> OwnershipManifest:
    """Build proof for every generated artifact and no local file."""
    return OwnershipManifest(
        schema_version=1,
        generator_version=generator_version,
        source_digest=source_digest(source),
        target_requirements=target_requirements,
        files=[
            OwnedArtifact(
                path=artifact.path,
                category="generated",
                sha256=digest.text(artifact.content),
                semantic_id=artifact.semantic_id,
                executable=artifact.executable,
            )
            for artifact in tree.artifacts
        ],
    )


def load_manifest(path: Path) -> OwnershipManifest | None:
    """Load prior ownership proof if it exists."""
    if not path.exists():
        return None
    try:
        return OwnershipManifest.model_validate_json(path.read_text(encoding="utf-8"))
    except ValidationError as error:
        raise OwnershipManifestError(
            f"ownership manifest at {path} cannot be decoded; repair or remove "
            "it, then regenerate"
        ) from error


# lup: ignore[constant-declaration] — the proof file's own name, which every
# generator and the merge driver that reads it must spell alike
OWNERSHIP_FILENAME = ".lup-ownership.json"
"""What proof is called inside whichever tree a native adapter materializes."""


class GeneratedArtifacts(BaseModel, frozen=True):
    """Which files in a tree the generator owns rather than the repository.

    Keyed the way a repository scan names files — relative to the root the
    proof was read from — so a scanned path can be asked about directly, and
    answered with the artifact rather than a bare yes.
    """

    by_path: dict[str, OwnedArtifact]

    def owning(self, path: str) -> OwnedArtifact | None:
        """The artifact generated at ``path``, where the generator owns one."""
        return self.by_path.get(path)


def proof_artifact(root: Path, proof: Path) -> OwnedArtifact:
    """The manifest, as the generated artifact it is but never lists.

    No manifest records itself, so a consumer asking whether the generator
    owns a path got "no" for the one file materialization always writes.
    That answer reached a resolver join as an obligation to justify keeping
    or dropping ownership proof, which is not a choice anybody makes: the
    proof is whatever the next generation emits.
    """
    return OwnedArtifact(
        path=proof,
        category="generated",
        sha256=digest.text((root / proof).read_text(encoding="utf-8")),
        semantic_id=f"ownership.{proof.parts[0]}",
    )


def generated_artifacts(root: Path, manifests: Collection[str]) -> GeneratedArtifacts:
    """What every manifest under ``root`` records the generator as owning.

    ``manifests`` are where proof is kept, relative to ``root``: each runtime
    states its own (:func:`lup.providers.harness.runtime_generated`), and a
    caller holding other proof names it.
    """

    def owned() -> Iterator[OwnedArtifact]:
        """Each generated artifact, across every tree that kept proof."""
        for kept in manifests:
            proof = Path(kept)
            manifest = load_manifest(root / proof)
            if manifest is None:
                continue
            yield proof_artifact(root, proof)
            for artifact in manifest.files:
                if artifact.category == "generated":
                    yield artifact

    return GeneratedArtifacts(
        by_path={str(artifact.path): artifact for artifact in owned()}
    )


# This is `publish_atomic` with an unchanged-content guard, and the pair to
# `load_manifest`: both are about the file, which is why neither is on the proof.
def save_manifest(path: Path, manifest: OwnershipManifest) -> None:
    """Atomically replace ownership proof after successful materialization."""
    content = manifest.model_dump_json(indent=2) + "\n"
    if path.is_file() and path.read_text(encoding="utf-8") == content:
        return
    publish_atomic(path, manifest)
