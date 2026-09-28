"""Claude Code's composition: a project's content, compiled into the tree it opens."""

from collections.abc import Sequence
from functools import partial
from pathlib import Path

from lup.providers.harness import claude_machine_overlay
from lup.providers.claude.harness import CLAUDE_OVERLAY

from lup.harness.generate import (
    MachineOverlay,
    NativeComposer,
    NativeHarnessComposition,
    ProjectContent,
    claude_generation_recipe,
)
from lup.harness.models import CapabilityEvidence, PromptDocument
from lup.providers.claude.harness import ClaudeSpellings
from lup.providers.claude.harness_runtime import (
    ClaudeCliEvidence,
    claude_capability_probes,
)
from lup.providers.claude.login import CLAUDE_LOGIN


class ClaudeComposer(NativeComposer):
    """Construct the Claude capabilities directly."""

    def compose(
        self,
        root: Path,
        content: ProjectContent,
        guidance: PromptDocument | None = None,
    ) -> NativeHarnessComposition:
        plugin = root / ".claude" / "plugins" / content.harness.plugins[0].name

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
