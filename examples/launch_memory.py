"""Bound how much memory a contained session may hold.

``memory=`` on ``OuterContainer`` takes an amount (``MemoryLimit(amount=...)``,
or ``12GiB`` spelled on `harness claude|codex --memory`) or a share of what the
engine can hand a container, which is what a committed declaration can state
and mean the same on every machine. A share nothing can resolve refuses the
launch rather than dropping the bound.

    uv run -m examples.launch_memory
"""

from lup import Claude, OuterContainer
from lup.harness.image import MemoryLimit


def main() -> None:
    agent = Claude(sandbox=OuterContainer(memory=MemoryLimit(percent=50)))
    print(agent.command())


if __name__ == "__main__":
    main()
