"""Ownership-safe generation engine beneath the harness CLI.

Each frozen ``GenerationRecipe`` compiles the canonical catalog through an
adapter, validates the rendered tree, reconciles it against recorded
ownership, materializes a conflict-free proposal, and saves the manifest.
Inspection exposes the same pipeline without writes. Console-facing command
bodies live in ``drift`` and ``reconcile``; ``composition`` maps target names
to concrete recipes.
"""

import json
from abc import ABC, abstractmethod
from collections.abc import Callable, Sequence
from pathlib import Path

from pydantic import BaseModel, ValidationError
from pydantic_core import ErrorDetails

from lup.providers.harness import (
    claude_prompt_renderer,
    codex_prompt_renderer,
    compile_claude,
    compile_codex,
)
from lup.formats.banner import (
    COMMENT_FREE,
    REGENERATE_COMMAND,
    VERBATIM_COPY,
    GeneratedBanner,
)
from lup.harness.evidence import WireContract
from lup.harness.materialization import (
    AtomicMaterializer,
    MaterializationRefusedError,
    held_read_only,
    mounted_read_only,
    refused_write,
)
from lup.harness.models import (
    Artifact,
    ArtifactTree,
    CapabilityReport,
    Document,
    Harness,
    PromptDocument,
)
from lup.mcp import ServeLaunch, ToolServer
from lup.types import JsonObject
from lup.workspace.paths import declared_project_root
from lup.harness.ownership import (
    OwnershipManifest,
    build_manifest,
    load_manifest,
    save_manifest,
)
from lup.harness.reconciliation import (
    DeterministicReconciler,
    FilesystemCurrentTreeReader,
    ReconciliationConflict,
    ReconciliationProposal,
)
from lup.harness.contracts import (
    CurrentTreeReader,
    PromptRenderer,
    Reconciler,
    SkillInvocationRenderer,
)
from lup.harness.validation import validated_tree
from lup.harness.clipboard import ClipboardTransport
from lup.providers.login import ProviderLogin


class ProjectContent(BaseModel, frozen=True):
    """What a project publishes on top of the tree its harness compiles.

    The harness says what the plugin *is*; this says what else the repository
    ships beside it — the documents rendered into ``docs/``, the downstream
    guidance each runtime publishes, the verbatim assets, and the native
    settings. All of it is one project's, which is why generation takes it
    rather than importing a catalog it would have to name.
    """

    harness: Harness

    documents: list[Document] = []
    """Repository documents, rendered once into the tree that publishes them."""

    assets: list[Path] = []
    """Files copied verbatim into the plugin tree rather than rendered.

    Named one by one rather than swept from a directory: what sits beside
    them is the package machinery of the module that holds them, and a sweep
    would ship that too.
    """

    settings: JsonObject = {}
    """Native settings for the runtime that reads a settings file."""

    servers: list[ToolServer] = []
    """The tool servers every session this project launches carries.

    Declared per session by the launch rather than carried in the tree it
    compiles: a runtime reading a strict roster drops a plugin's own, so the
    session's command line is the one place a server reaches it from.
    """

    serve: ServeLaunch = ServeLaunch()
    """How a launched session starts the servers lup hosts, for this project."""

    settings_source: str = ""
    """The module declaring those settings, and where a reader edits them.

    Carried beside them because the settings themselves are a ``JsonObject``
    and carry nothing: the artifact they become is JSON too, so its provenance
    has no comment to sit in and would otherwise be nowhere at all.
    """


class GenerationRecipe(BaseModel, frozen=True, arbitrary_types_allowed=True):
    """Injected data and capabilities needed by neutral generation orchestration."""

    label: str
    root: Path
    source: Harness
    desired: ArtifactTree
    manifest_path: Path
    prior: OwnershipManifest | None
    reader: CurrentTreeReader
    reconciler: Reconciler = DeterministicReconciler()
    target_requirements: list[str]


class MachineOverlay(BaseModel, frozen=True, arbitrary_types_allowed=True):
    """What one runtime renders per machine beside its committed tree, and where.

    Its directory is the overlay's alone: rewritten whole each time it is
    rendered, ignored by git, and in no ownership manifest, because what it
    holds names this machine's own facts, which no committed file may.
    """

    directory: Path
    """Where it is written, relative to the project root."""

    render: Callable[[Sequence[str]], ArtifactTree]
    """The overlay for a machine keeping these profiles."""

    loaded: bool = False
    """Whether a launch names the directory to its runtime, which otherwise
    reads it where it stands."""


type RuntimeReadiness = Callable[[], Sequence[CapabilityReport]]
"""How a composition asks its runtime whether it is actually installed."""


class NativeHarnessComposition(BaseModel, frozen=True, arbitrary_types_allowed=True):
    """Concrete capabilities supplied to one CLI composition root.

    The one shape every harness command works in: a generation recipe, a
    readiness probe set, and a renderer for skill invocations. Which classes
    fill those is the composition root's business, never the command's.
    """

    recipe: GenerationRecipe
    readiness: RuntimeReadiness
    invocation_renderer: SkillInvocationRenderer
    login: ProviderLogin
    default_config_home: Path
    clipboard_transport: ClipboardTransport = "commands"
    servers: list[ToolServer] = []
    """The tool servers a session launched on this composition carries."""

    serve: ServeLaunch = ServeLaunch()
    """How that session starts the servers lup hosts."""

    overlay: MachineOverlay | None = None
    """What this runtime renders per machine beside the tree, where it renders any."""

    wire_contracts: list[WireContract] = []
    """Reply shapes this runtime's adapter reads fields off by name.

    Carried by the composition because the field names are a provider's words
    and this is where a provider is named. A doctor reaching across the seam
    for them would be importing an adapter to obtain a list of strings, and
    the strings are all the check needs: the generated schema either declares
    them or it does not, whoever's they are.
    """


class NativeComposer(ABC):
    """How one runtime assembles a project's content into what a CLI opens.

    One declared seam rather than a free function per runtime, and the
    difference is not style. A function is reached by name, so adding a
    runtime means finding every caller that names one and remembering the new
    one — and a caller that forgets leaves that runtime silently absent
    rather than failing. A seam is reached by the object a project declared,
    so what ``NativeTargets`` holds is the whole of what exists.

    Deliberately one method. What a runtime answers here is a composition,
    and every part of it — the recipe, the readiness probes, the invocation
    renderer — is that same runtime's answer, so splitting them into three
    seams would hand a caller three objects that never vary independently.
    The composition is the unit that varies.
    """

    @abstractmethod
    def compose(
        self,
        root: Path,
        content: ProjectContent,
        guidance: PromptDocument | None = None,
    ) -> NativeHarnessComposition:
        """This runtime's composition over one project's content."""


class HarnessGenerationConflict(RuntimeError):
    """Generated output collides with local or unproven content."""

    def __init__(self, conflicts: list[ReconciliationConflict]) -> None:
        detail = ", ".join(conflict.path.as_posix() for conflict in conflicts)
        super().__init__(f"harness generation has unresolved conflicts: {detail}")
        self.conflicts = conflicts


class GeneratedTreesHeld(MaterializationRefusedError):
    """A tree this generation would write is held read-only in this session.

    A container may hold the generated trees its runtime runs from, so a
    session cannot change the hooks judging it; regenerating them is then
    the host's work. Raised before anything is written, since a rename
    refused halfway leaves a tree matching neither its source nor its proof.
    """

    def __init__(self, held: list[Path]) -> None:
        super().__init__(
            "these trees are read-only in this session: "
            + ", ".join(str(path) for path in held)
            + f"; run `{REGENERATE_COMMAND}` on the host"
        )
        self.held = held


class DeclarationObstruction(BaseModel, frozen=True):
    """Why something generated would not compile, in its declaration's terms.

    A generated artifact is compiled from a typed declaration, so a compile
    that refuses is answered in that declaration and nowhere else — and the
    two facts that takes are what refused and where it was declared. Both are
    buried by what raises them: a pydantic ``ValidationError`` holds one line
    per refused field under a repr a hundred lines long, and a missing file
    arrives as an interpreter traceback through the frames of whichever reader
    happened to open it. Neither is about the reader's next decision, and both
    are the same decision.
    """

    target: str
    """What was being compiled when the declaration refused."""

    obstruction: list[str]
    """What refused, in its own words, one line per refusal."""

    declaration: str = ""
    """Where it was declared: the model that validated it, or the file read.

    Empty where what raised names neither, which is the honest answer — the
    refusal's own words then carry everything there is to act on, and a line
    naming the exception class in that slot would read as a declaration and
    be none.
    """

    def described(self) -> list[str]:
        """This obstruction as a refusal prints it, the declaration last."""
        return [
            f"{self.target}: nothing generated, the declaration was refused",
            *(f"  {line}" for line in self.obstruction),
            *([f"  declared in {self.declaration}"] if self.declaration else []),
        ]


def obstruction_at(target: str, refusal: Exception) -> DeclarationObstruction:
    """Read whatever refused a compile into the facts a reader acts on.

    Three shapes arrive here. A ``ValidationError`` already knows every field
    it refused and which model refused them, and needs only to be asked. An
    ``OSError`` knows a path and an errno and has no sentence at all. Anything
    else has said what it means in its own message and names no declaration,
    which is reported as naming none rather than as naming its own class.
    """

    def spelled(item: ErrorDetails) -> str:
        """One refused field, its location in front of it where it has one."""
        where = ".".join(str(part) for part in item["loc"])
        return f"{where}: {item['msg']}" if where else item["msg"]

    match refusal:
        case ValidationError():
            return DeclarationObstruction(
                target=target,
                obstruction=[spelled(item) for item in refusal.errors()],
                declaration=refusal.title,
            )
        case OSError():
            return DeclarationObstruction(
                target=target,
                obstruction=[refusal.strerror or str(refusal)],
                declaration=str(refusal.filename or ""),
            )
        case _:
            return DeclarationObstruction(target=target, obstruction=[str(refusal)])


class GenerationReport(BaseModel, frozen=True):
    target: str
    changed: list[Path]
    removed: list[Path]
    source_digest: str


class DriftReport(BaseModel, frozen=True):
    """Read-only desired-tree comparison for pre-commit and CI."""

    target: str
    ownership_present: bool
    manifest_current: bool
    """Whether the manifest on disk is the one this source would write.

    The artifacts and the proof that covers them go stale independently. A
    merge is where they part: the driver resolving a manifest keeps one
    side, which is a digest of a tree neither branch has now, while the
    artifacts themselves merged cleanly and match their source. Reading only
    the artifacts calls that settled, and the regeneration the conflict was
    supposed to force is then the step nothing asks for.
    """

    proposal: ReconciliationProposal

    held: list[Path] = []
    """What a regeneration would write that is read-only in this session.

    A contained session may hold its runtime's generated trees, and then a
    tree behind its source is regenerated on the host rather than here."""

    @property
    def clean(self) -> bool:
        return (
            self.ownership_present
            and self.manifest_current
            and not self.proposal.writes
            and not self.proposal.deletes
            and not self.proposal.conflicts
        )


def rendered_document(
    *,
    path: Path,
    document: PromptDocument,
    prompts: PromptRenderer,
    semantic_id: str,
) -> Artifact:
    """Render one canonical document below the banner naming its module."""
    return Artifact.generated(
        path=path,
        body=prompts.render(document),
        semantic_id=semantic_id,
        banner=GeneratedBanner(
            source=document.declared_source(), command=REGENERATE_COMMAND
        ),
    )


def installer_guidance(
    *, path: Path, document: PromptDocument | None, prompts: PromptRenderer
) -> list[Artifact]:
    """Render the guidance an installer merges into a target, if there is any.

    Only a template has one: a project that is nobody's starting point has no
    downstream to hand guidance to, so it publishes no such file rather than
    advertising its own project guidance as something to install elsewhere.
    """
    if document is None:
        return []
    return [
        rendered_document(
            path=path,
            document=document,
            prompts=prompts,
            semantic_id="harness.template-guidance",
        )
    ]


def published_documents(
    prompts: PromptRenderer, documents: list[Document]
) -> list[Artifact]:
    """Render every document the roster declares.

    The roster is the whole of what ``docs/`` contains: a document not
    declared there is not published, and a file found there that this did not
    produce is deleted as unowned. Both trees render the same set, so the two
    cannot disagree about what the repository documents.
    """
    return [
        rendered_document(
            path=document.path,
            document=document.document,
            prompts=prompts,
            semantic_id=document.semantic_id,
        )
        for document in documents
    ]


def managed_paths(desired: ArtifactTree, prior: OwnershipManifest | None) -> list[Path]:
    """Combine desired and formerly owned paths for deletion detection."""
    paths = [artifact.path for artifact in desired.artifacts]
    if prior is not None:
        paths.extend(item.path for item in prior.files)
    return list(dict.fromkeys(paths))


def current_reader(
    prior: OwnershipManifest | None,
    desired: ArtifactTree,
    *,
    sensitive_local_only: list[Path],
) -> FilesystemCurrentTreeReader:
    """Build the ordinary ownership reader used by concrete native recipes."""
    return FilesystemCurrentTreeReader(
        prior,
        sensitive_local_only=sensitive_local_only,
        managed_paths=managed_paths(desired, prior),
    )


# Compiler, prompt renderers, ownership reader, and reconciler in one place.
def claude_generation_recipe(
    root: Path, content: ProjectContent, guidance: PromptDocument | None = None
) -> GenerationRecipe:
    """Compose the complete Claude tree from canonical typed declarations."""
    source = content.harness
    compiled = compile_claude(source)
    prompts = claude_prompt_renderer()
    plugin = Path(".claude/plugins") / source.plugins[0].name

    def copied_from(asset: Path) -> str:
        """Where the asset sits, named from the project that holds it.

        The bytes are read from wherever the declaring package was imported,
        which is not always the checkout being written: generating into a
        sibling worktree leaves the two apart. Anchoring on ``root`` there
        names the asset by an absolute path into somebody else's tree, and
        that path is committed — so the map a reader opens points at a
        checkout they may not have, and the same source compiles to different
        bytes depending on where the command ran. The asset's own project
        answers the same in every checkout, which is what the row means.
        """
        anchor = declared_project_root(asset.parent) or root
        inside = asset.relative_to(anchor) if asset.is_relative_to(anchor) else asset
        return inside.as_posix()

    verbatim = [
        Artifact(
            path=plugin / "scripts" / asset.name,
            content=asset.read_text(encoding="utf-8"),
            semantic_id="harness.file-suggestion",
            executable=True,
            banner=VERBATIM_COPY.compiled_from(copied_from(asset)),
        )
        for asset in content.assets
    ]
    support_artifacts = [
        *published_documents(prompts, content.documents),
        *installer_guidance(
            path=plugin / "TEMPLATE_CLAUDE.md", document=guidance, prompts=prompts
        ),
        *verbatim,
        Artifact(
            path=Path(".claude/settings.json"),
            content=json.dumps(content.settings, indent=2, sort_keys=True),
            semantic_id="harness.project-settings",
            banner=COMMENT_FREE.compiled_from(content.settings_source),
        ),
    ]
    desired = validated_tree([*compiled.artifacts, *support_artifacts])
    manifest_path = root / ".claude" / ".lup-ownership.json"
    prior = load_manifest(manifest_path)
    reader = current_reader(
        prior,
        desired,
        sensitive_local_only=[Path(".claude/settings.local.json")],
    )
    return GenerationRecipe(
        label="claude",
        root=root,
        source=source,
        desired=desired,
        manifest_path=manifest_path,
        prior=prior,
        reader=reader,
        reconciler=DeterministicReconciler(),
        target_requirements=["claude-code"],
    )


def codex_generation_recipe(
    root: Path, content: ProjectContent, guidance: PromptDocument | None = None
) -> GenerationRecipe:
    """Compose the Codex renderers, reader, and ownership location."""
    source = content.harness
    prompts = codex_prompt_renderer()
    support_artifacts = installer_guidance(
        path=Path(".codex/plugins") / source.plugins[0].name / "TEMPLATE_AGENTS.md",
        document=guidance,
        prompts=prompts,
    )
    compiled = compile_codex(source)
    desired = validated_tree([*compiled.artifacts, *support_artifacts])
    manifest_path = root / ".codex" / ".lup-ownership.json"
    prior = load_manifest(manifest_path)
    return GenerationRecipe(
        label="codex",
        root=root,
        source=source,
        desired=desired,
        manifest_path=manifest_path,
        prior=prior,
        reader=current_reader(
            prior,
            desired,
            sensitive_local_only=[Path(".codex/config.local.toml")],
        ),
        reconciler=DeterministicReconciler(),
        target_requirements=["codex-cli>=0.144"],
    )


def manifest_of(recipe: GenerationRecipe) -> OwnershipManifest:
    """The proof *recipe* would write, built once for writing and for reading.

    Inspection compares against this and generation saves it, so the manifest
    a check calls current is the one a regeneration would produce — the two
    cannot disagree about what settled means.
    """
    return build_manifest(
        recipe.source,
        recipe.desired,
        generator_version=recipe.source.generator_version,
        target_requirements=recipe.target_requirements,
    )


def inspect_generation(recipe: GenerationRecipe) -> DriftReport:
    """Compute ownership-aware drift without changing the working tree.

    Every path a regeneration would write — each artifact written or
    removed, and the proof where it moves — is asked whether this session
    holds it read-only, so a held tree is said before anything touches it.
    """
    current = recipe.reader.read(recipe.root)
    manifest_current = recipe.prior == manifest_of(recipe)
    proposal = recipe.reconciler.propose(current, recipe.desired)
    touched = [
        *(recipe.root / write.artifact.path for write in proposal.writes),
        *(recipe.root / delete.path for delete in proposal.deletes),
        *([] if manifest_current else [recipe.manifest_path]),
    ]
    return DriftReport(
        target=recipe.label,
        ownership_present=recipe.prior is not None,
        manifest_current=manifest_current,
        proposal=proposal,
        held=held_read_only(touched, mounted_read_only),
    )


def generate(recipe: GenerationRecipe) -> GenerationReport:
    """Compile, reconcile, materialize, then update proof—never source prompts.

    Refused before anything is written where this session holds a tree it
    would write read-only, naming the host command that writes it instead.
    """
    drift = inspect_generation(recipe)
    proposal = drift.proposal
    if proposal.conflicts:
        raise HarnessGenerationConflict(proposal.conflicts)
    if drift.held:
        raise GeneratedTreesHeld(drift.held)
    try:
        result = AtomicMaterializer().apply(proposal)
    except OSError as error:
        raise refused_write(error) from error
    manifest = manifest_of(recipe)
    save_manifest(recipe.manifest_path, manifest)
    return GenerationReport(
        target=recipe.label,
        changed=result.changed,
        removed=result.removed,
        source_digest=manifest.source_digest,
    )
