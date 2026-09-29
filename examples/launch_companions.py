"""Keep a service running on the host for as long as the sessions using it run.

A ``HostCompanion`` is held around every session a declaration opens and
hands the session what reaches it. A ``SharedProcess`` is one process
shared by the sessions its scope names — per checkout here — started by the
first on its preferred port or the next free one, joined by the rest, and
stopped once the last lets go.

    uv run -m examples.launch_companions
"""

import sys
from pathlib import Path

from lup import (
    Claude,
    CompanionPlace,
    CompanionProcess,
    Contribution,
    SharedProcess,
)


class Preview(SharedProcess, frozen=True):
    """A static preview of the checkout, served on the host's loopback."""

    def process(self, place: CompanionPlace, root: Path) -> CompanionProcess:
        port = str(place.ports["web"])
        return CompanionProcess(
            argv=[sys.executable, "-m", "http.server", port, "--bind", "127.0.0.1"],
            cwd=root,
        )

    def contribution(self, place: CompanionPlace, root: Path) -> Contribution:
        del root
        return Contribution(
            environment={"PREVIEW_URL": f"http://127.0.0.1:{place.ports['web']}"},
            ports=dict(place.ports),
        )


def main() -> None:
    agent = Claude(companions=[Preview(name="preview", ports={"web": 8765})])
    print(agent.command().env["PREVIEW_URL"])


if __name__ == "__main__":
    main()
