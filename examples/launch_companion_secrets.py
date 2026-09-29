"""Start a host companion with a key only it may hold.

``secrets=`` on a ``SharedProcess`` names keys of the project's host store,
``$XDG_CONFIG_HOME/lup/secrets/<project>.env``, which the companion is
started with and no session holds: a launch takes every name the store holds
out of what the session inherits. A key the store lacks refuses the launch.
Set it first, on the host:

    uv run lup-devtools setup secret PREVIEW_TOKEN
    uv run -m examples.launch_companion_secrets
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
    """A preview server that authenticates to its backend with a token of its own."""

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
        )


def main() -> None:
    preview = Preview(name="preview", ports={"web": 8766}, secrets=["PREVIEW_TOKEN"])
    agent = Claude(companions=[preview])
    print(agent.command().env["PREVIEW_URL"])


if __name__ == "__main__":
    main()
