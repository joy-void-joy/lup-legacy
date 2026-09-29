"""Give a contained session guidance of its own instead of the committed one.

``guidance=`` on ``OuterContainer`` is rendered for the runtime the way
generation renders the project's guidance, held to its budget, written outside
the checkout, and mounted read-only over ``.claude/CLAUDE.md`` or
``AGENTS.md`` inside the container, so the tree on the host never changes. It
names the module declaring it, as every rendered file does.

    uv run -m examples.launch_guidance
"""

from lup import Claude, Codex, OuterContainer
from lup.harness.models import PromptDocument, TextPart

EXPLORING = PromptDocument(
    source=__name__,
    parts=[
        TextPart(text="# Exploring\n\nTry things freely; a later session tidies up.\n")
    ],
)


def main() -> None:
    claude = Claude(sandbox=OuterContainer(guidance=EXPLORING))
    codex = Codex(sandbox=OuterContainer(guidance=EXPLORING))
    print(claude.command())
    print(codex.command())


if __name__ == "__main__":
    main()
