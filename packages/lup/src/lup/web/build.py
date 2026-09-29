"""The frontend bundles as a generated tree, built by Vite and owned like one.

Every interactive surface lup serves is a TypeScript app under the bun
workspace, and what a wheel carries is the bundle Vite built from it. A
bundle is a compiled artifact in exactly the sense the native trees are:
derived from typed source, meaningless to hand-edit, stale the moment its
source moves. So it is materialised the way those trees are — as artifacts
under an ownership manifest, reconciled against what is on disk, written
atomically, with orphaned files from an earlier build deleted on proof — and
`dev check` reads it against that proof the way it reads every generated
tree against its source, building only where the proof no longer holds.

**Text only.** The materializer normalises artifacts as LF text, so a bundle
is JavaScript, CSS and HTML; Vite inlines small assets and nothing here ships a
large binary one. A build that emits one fails loudly rather than landing a
corrupted file.

**Bun is a dependency of the build, not of the wheel.** An adopter has the
bundles; only somebody changing frontend code needs the toolchain. Bun has no
verb that restores the workspace and then runs, the way `uv run` syncs before
running, so the build restores it itself: where `node_modules` is missing or
behind `bun.lock`, :func:`restore_dependencies` runs the frozen install first
— every package pinned by integrity hash, nothing resolved anew — and the
gate's `bun test` row does the same before it runs.
"""

import hashlib
import os
from importlib.metadata import version
from pathlib import Path
from tempfile import TemporaryDirectory

import sh
from pydantic import BaseModel

from lup.devtools.dev.library import DISTRIBUTION
from lup.execution.shell import LazyCommand
from lup.formats.banner import REGENERATE_COMMAND, VERBATIM_COPY
from lup.harness.materialization import AtomicMaterializer
from lup.harness.models import Artifact, ArtifactTree
from lup.harness.ownership import (
    OWNERSHIP_FILENAME,
    OwnedArtifact,
    OwnershipManifest,
    content_digest,
    load_manifest,
    save_manifest,
)
from lup.harness.reconciliation import (
    DeterministicReconciler,
    FilesystemCurrentTreeReader,
)
from lup.harness.validation import validated_tree
from lup.workspace.paths import project_root

BUN = LazyCommand("bun", tty_out=False)


class Surface(BaseModel, frozen=True, arbitrary_types_allowed=True):
    """One TypeScript surface: the entry it builds from, and what its page is typed against.

    The name is the workspace entry under ``src/<name>/`` and the bundle under
    ``bundles/<name>/`` alike, so a surface is one word in three places. The
    models are every one the page is handed — the routes' replies and what it
    posts — which is what the schema compiles its types from.
    """

    name: str
    models: list[type[BaseModel]]


def source_files(
    workspace: Path,
    globs: tuple[str, ...] = (
        "package.json",
        "bun.lock",
        "tsconfig.json",
        "vite.config.ts",
        "schema/**/*",
        "src/**/*",
    ),
) -> list[Path]:
    """Every file the bundles are built from, in one stable order.

    The globs are what a bundle is compiled from, and therefore what its proof
    digests; a workspace laid out differently names its own. `src/generated/`
    is left out whatever the globs say: it is compiled from the schema at
    build time, so the schema already stands for it.
    """
    found = {
        path
        for pattern in globs
        for path in workspace.glob(pattern)
        if path.is_file() and "generated" not in path.relative_to(workspace).parts
    }
    return sorted(found)


def source_digest(workspace: Path) -> str:
    """One digest over the workspace's sources, so proof says what was built."""
    digest = hashlib.sha256()
    for path in source_files(workspace):
        digest.update(path.relative_to(workspace).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def toolchain_output(failed: sh.ErrorReturnCode) -> str:
    """What a failed bun command printed, both streams, kept whole.

    Both, because the toolchain disagrees with itself about where a verdict
    goes: the type checker writes its diagnostics to stdout and the bundler
    to stderr. Neither is always UTF-8, so a decode error here would hide the
    failure it describes.
    """
    return "\n".join(
        stream.decode("utf-8", errors="replace")
        for stream in (failed.stdout, failed.stderr)
    )


def dependencies_behind(workspace: Path, lockfile: str = "bun.lock") -> bool:
    """Whether the workspace's dependencies are missing or older than its lockfile.

    Bun writes nothing under `node_modules` saying which lockfile it
    restored, so the directory's own timestamp stands for that record:
    :func:`restore_dependencies` dates a restore after the lockfile it read,
    and a lockfile that moved since — a checkout, a merge, a `bun add` — is
    newer than the tree it describes. A workspace with no lockfile has
    nothing to be behind, so its dependencies are behind only while absent.
    """
    installed = workspace / "node_modules"
    if not installed.is_dir():
        return True
    locked = workspace / lockfile
    return locked.is_file() and locked.stat().st_mtime > installed.stat().st_mtime


def restore_dependencies(workspace: Path) -> bool:
    """Restore the workspace's dependencies from its lockfile where they are behind it.

    What `uv run` does before running anything, for a toolchain that has no
    verb for it: `bun test` and `bun run build` assume `node_modules`, so
    whatever runs them restores it first — from the lockfile as frozen, every
    package pinned by integrity hash and nothing resolved anew, which is the
    restore the policy allows unasked. A restore that fails raises naming the
    command with bun's own output, so the row or generation reporting it says
    what to run by hand. Returns whether a restore ran, for a caller that
    reports its work.
    """
    if not dependencies_behind(workspace):
        return False
    try:
        BUN("install", "--frozen-lockfile", _cwd=str(workspace))
    except sh.ErrorReturnCode as failed:
        raise RuntimeError(
            f"`bun install --frozen-lockfile` in {workspace} failed:\n"
            f"{toolchain_output(failed)}"
        ) from failed
    # Dated after the lockfile it read, so the next reading finds nothing
    # behind whether or not bun itself touched the directory.
    os.utime(workspace / "node_modules")
    return True


def built_files(workspace: Path, surface: str, out: Path) -> list[Path]:
    """Build one surface into ``out`` and return every file Vite wrote there.

    The surface rides as Vite's own ``--mode`` and the destination as
    ``--outDir``, both arguments to the workspace's build script, so nothing
    about the build is carried in an environment this process would have to
    assemble.
    """
    try:
        BUN(
            "run",
            "build",
            "--",
            "--mode",
            surface,
            "--outDir",
            str(out),
            _cwd=str(workspace),
        )
    except sh.ErrorReturnCode as failed:
        raise RuntimeError(
            f"building the {surface!r} surface failed:\n{toolchain_output(failed)}"
        ) from failed
    return sorted(path for path in out.rglob("*") if path.is_file())


def bundle_artifacts(
    workspace: Path, bundles: Path, surface: str, out: Path
) -> list[Artifact]:
    """One surface's built files as artifacts under the bundles tree."""
    source = (workspace / "src" / surface).as_posix()

    def artifact(built: Path) -> Artifact:
        relative = built.relative_to(out)
        try:
            content = built.read_text(encoding="utf-8")
        except UnicodeDecodeError as binary:
            raise RuntimeError(
                f"the {surface!r} bundle emitted a binary file, {relative}, which"
                " a text-only generated tree cannot carry; inline or drop it"
            ) from binary
        return Artifact(
            path=bundles / surface / relative,
            content=content,
            semantic_id=f"web.{surface}.{relative.as_posix()}",
            banner=VERBATIM_COPY.compiled_from(source),
        )

    return [artifact(built) for built in built_files(workspace, surface, out)]


def bundle_manifest(workspace: Path, desired: ArtifactTree) -> OwnershipManifest:
    """The proof a materialization of these bundles writes and a check reads."""
    return OwnershipManifest(
        schema_version=1,
        generator_version=version(DISTRIBUTION),
        source_digest=source_digest(workspace),
        target_requirements=["bun"],
        files=[
            OwnedArtifact(
                path=artifact.path,
                category="generated",
                sha256=content_digest(artifact.content),
                semantic_id=artifact.semantic_id,
            )
            for artifact in desired.artifacts
        ],
    )


def proof_holds(prior: OwnershipManifest, base: Path, workspace: Path) -> bool:
    """Whether the bundles on disk are still the ones this proof was written for.

    Three facts, each of which a fresh build could only restate: the sources
    hash to what the proof recorded, the generator is the version that wrote
    it, and every owned file still carries the digest it landed with. Vite is
    deterministic over those inputs, so where all three hold a rebuild lands
    the tree that is already there. Building to learn that costs the whole
    toolchain run, and a launch asks twice.
    """
    if prior.source_digest != source_digest(workspace):
        return False
    if prior.generator_version != version(DISTRIBUTION):
        return False
    owned = [item.path for item in prior.files]
    current = FilesystemCurrentTreeReader(prior, managed_paths=owned).read(base)
    standing = {artifact.path: artifact.category for artifact in current.artifacts}
    return all(standing.get(path) == "generated" for path in owned)


def write_web_bundles(
    workspace: Path,
    bundles: Path,
    surfaces: list[Surface],
    root: Path | None = None,
    *,
    check: bool = False,
) -> Path:
    """Build every surface and materialise the bundles tree, or verify it.

    ``workspace`` and ``bundles`` are relative to the repository root, so the
    template names them once. A tree whose proof holds is current in either
    mode without a build, since :func:`proof_holds` reads exactly what a
    rebuild would restate; anything else is behind, which a check says and a
    write settles by building — after restoring the workspace's dependencies
    where they are behind its lockfile, since a build assumes them.
    """
    base = root or project_root()
    home = base / workspace
    manifest_path = base / bundles / OWNERSHIP_FILENAME
    prior = load_manifest(manifest_path)
    if prior is not None and proof_holds(prior, base, home):
        return base / bundles
    if check:
        raise RuntimeError(
            f"{bundles} is behind {workspace}; run `{REGENERATE_COMMAND}`"
        )
    restore_dependencies(home)
    with TemporaryDirectory(prefix="lup-web-") as scratch:
        artifacts = [
            artifact
            for surface in surfaces
            for artifact in bundle_artifacts(
                home, bundles, surface.name, Path(scratch) / surface.name
            )
        ]
    desired = validated_tree(artifacts)
    managed = list(
        dict.fromkeys(
            [
                *(artifact.path for artifact in desired.artifacts),
                *(item.path for item in (prior.files if prior is not None else [])),
            ]
        )
    )
    reader = FilesystemCurrentTreeReader(prior, managed_paths=managed)
    proposal = DeterministicReconciler().propose(reader.read(base), desired)
    AtomicMaterializer().apply(proposal)
    save_manifest(manifest_path, bundle_manifest(home, desired))
    return base / bundles
