"""Codex's composition: a project's content, compiled into the tree it opens."""

from collections.abc import Sequence
from functools import partial
from pathlib import Path

from lup.providers.harness import (
    codex_machine_overlay,
    codex_prompt_renderer,
    compile_codex,
)
from lup.providers.codex.harness import CODEX_OVERLAY

from lup.harness.evidence import WireContract
from lup.harness.generate import (
    GenerationRecipe,
    MachineOverlay,
    NativeComposer,
    NativeHarnessComposition,
    ProjectContent,
    current_reader,
    installer_guidance,
)
from lup.harness.models import CapabilityEvidence, PromptDocument
from lup.harness.ownership import load_manifest
from lup.harness.reconciliation import DeterministicReconciler
from lup.harness.validation import validated_tree
from lup.providers.codex.harness import CodexSpellings
from lup.providers.codex.harness_runtime import (
    CodexCliEvidence,
    codex_capability_probes,
)
from lup.providers.codex.home import CodexWorktreeHomeStore
from lup.providers.codex.login import CODEX_LOGIN
from lup.providers.codex.trust import HOOKS_LIST, hook_wire_fields


CODEX = CodexSpellings()
"""Where Codex keeps each part of its tree, which every path below is read from."""


def codex_generation_recipe(
    root: Path, content: ProjectContent, guidance: PromptDocument | None = None
) -> GenerationRecipe:
    """Compose the Codex renderers, reader, and ownership location."""
    source = content.harness
    prompts = codex_prompt_renderer()
    support_artifacts = installer_guidance(
        path=Path(CODEX.plugin(source.plugins[0].name, "guidance_template", None)),
        document=guidance,
        prompts=prompts,
    )
    compiled = compile_codex(source)
    desired = validated_tree([*compiled.artifacts, *support_artifacts])
    manifest_path = root / CODEX.tree("ownership_manifest")
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
            sensitive_local_only=[Path(CODEX.tree("personal_settings"))],
        ),
        reconciler=DeterministicReconciler(),
        target_requirements=["codex-cli>=0.144"],
    )


class CodexComposer(NativeComposer):
    """Construct the Codex capabilities directly."""

    def compose(
        self,
        root: Path,
        content: ProjectContent,
        guidance: PromptDocument | None = None,
    ) -> NativeHarnessComposition:
        def readiness() -> Sequence[CapabilityEvidence[CodexCliEvidence]]:
            return [probe.probe() for probe in codex_capability_probes()]

        return NativeHarnessComposition(
            recipe=codex_generation_recipe(root, content, guidance),
            servers=content.servers,
            serve=content.serve,
            overlay=MachineOverlay(
                directory=CODEX_OVERLAY,
                render=partial(codex_machine_overlay, content.harness),
                loaded=False,
            ),
            readiness=readiness,
            invocation_renderer=CodexSpellings(),
            login=CODEX_LOGIN,
            default_config_home=CodexWorktreeHomeStore().home_for(root),
            clipboard_transport="x11",
            # The one reply whose field names Lup depends on outside a typed
            # schema: hook trust is seeded from what `hooks/list` reports, and
            # a rename there fails open rather than loudly.
            wire_contracts=[WireContract(method=HOOKS_LIST, fields=hook_wire_fields())],
        )
