"""Codex's composition: a project's content, compiled into the tree it opens."""

from collections.abc import Sequence
from pathlib import Path

from lup.harness.evidence import WireContract
from lup.harness.generate import (
    NativeComposer,
    NativeHarnessComposition,
    ProjectContent,
    codex_generation_recipe,
)
from lup.harness.models import CapabilityEvidence, PromptDocument
from lup.providers.codex.harness import CodexSpellings
from lup.providers.codex.harness_runtime import (
    CodexCliEvidence,
    codex_capability_probes,
)
from lup.providers.codex.home import CodexWorktreeHomeStore
from lup.providers.codex.login import CODEX_LOGIN
from lup.providers.codex.trust import HOOKS_LIST, hook_wire_fields


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
