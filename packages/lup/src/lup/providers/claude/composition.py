"""Claude Code's composition: a project's content, compiled into the tree it opens."""

import json
from collections.abc import Sequence
from functools import partial
from pathlib import Path

from lup.providers.harness import (
    claude_machine_overlay,
    claude_prompt_renderer,
    compile_claude,
)
from lup.providers.claude.harness import CLAUDE_OVERLAY

from lup.formats.banner import COMMENT_FREE, VERBATIM_COPY
from lup.harness.generate import (
    GenerationRecipe,
    MachineOverlay,
    NativeComposer,
    NativeHarnessComposition,
    ProjectContent,
    current_reader,
    installer_guidance,
    published_documents,
)
from lup.harness.models import Artifact, CapabilityEvidence, PromptDocument
from lup.harness.ownership import load_manifest
from lup.harness.reconciliation import DeterministicReconciler
from lup.harness.validation import validated_tree
from lup.workspace.paths import declared_project_root
from lup.providers.claude.harness import ClaudeSpellings
from lup.providers.claude.harness_runtime import (
    ClaudeCliEvidence,
    claude_capability_probes,
)
from lup.providers.claude.login import CLAUDE_LOGIN


CLAUDE = ClaudeSpellings()
"""Where Claude Code keeps each part of its tree, which every path below is read from."""


def claude_generation_recipe(
    root: Path, content: ProjectContent, guidance: PromptDocument | None = None
) -> GenerationRecipe:
    """Compose the complete Claude tree from canonical typed declarations."""
    source = content.harness
    compiled = compile_claude(source)
    prompts = claude_prompt_renderer()
    plugin = Path(CLAUDE.plugin(source.plugins[0].name, "root", None))

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
            path=Path(CLAUDE.plugin(source.plugins[0].name, "guidance_template", None)),
            document=guidance,
            prompts=prompts,
        ),
        *verbatim,
        Artifact(
            path=Path(CLAUDE.tree("project_settings")),
            content=json.dumps(content.settings, indent=2, sort_keys=True),
            semantic_id="harness.project-settings",
            banner=COMMENT_FREE.compiled_from(content.settings_source),
        ),
    ]
    desired = validated_tree([*compiled.artifacts, *support_artifacts])
    manifest_path = root / CLAUDE.tree("ownership_manifest")
    prior = load_manifest(manifest_path)
    reader = current_reader(
        prior,
        desired,
        sensitive_local_only=[Path(CLAUDE.tree("personal_settings"))],
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


class ClaudeComposer(NativeComposer):
    """Construct the Claude capabilities directly."""

    def compose(
        self,
        root: Path,
        content: ProjectContent,
        guidance: PromptDocument | None = None,
    ) -> NativeHarnessComposition:
        plugin = root / CLAUDE.plugin(content.harness.plugins[0].name, "root", None)

        def readiness() -> Sequence[CapabilityEvidence[ClaudeCliEvidence]]:
            return [probe.probe() for probe in claude_capability_probes(plugin)]

        return NativeHarnessComposition(
            recipe=claude_generation_recipe(root, content, guidance),
            servers=content.servers,
            serve=content.serve,
            overlay=MachineOverlay(
                directory=CLAUDE_OVERLAY,
                render=partial(claude_machine_overlay, content.harness),
                loaded=True,
            ),
            readiness=readiness,
            invocation_renderer=ClaudeSpellings(),
            login=CLAUDE_LOGIN,
            default_config_home=CLAUDE_LOGIN.ambient_home,
            clipboard_transport="commands",
        )
