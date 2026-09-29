"""Hold a repository kept inside the checkout as the checkout's own is held.

``nested_repositories=`` on ``OuterContainer`` names repositories inside the
working tree whose ``config`` and ``hooks/`` the container binds read-only,
since the host's git runs what they name; their pointers are verified on the
host at every launch. ``create=True`` initializes one on the host when it is
absent, so no session writes its configuration first.

    uv run -m examples.launch_nested_repositories
"""

from pathlib import PurePosixPath

from lup import Claude, OuterContainer
from lup.sandbox.rail import NestedRepository


def main() -> None:
    works = NestedRepository(path=PurePosixPath("works"), create=True)
    agent = Claude(sandbox=OuterContainer(nested_repositories=[works]))
    print(agent.command())


if __name__ == "__main__":
    main()
