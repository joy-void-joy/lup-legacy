"""Let a contained session administer its own container through sudo.

Every container session drops every capability and gains none, so the agent
holds nothing. ``sudo=True`` on ``OuterContainer`` builds the image with
passwordless sudo and gives the container's root back what administering its
own files takes — only on a rootless engine, where that root is an
unprivileged user on the host; a rootful one refuses the launch. What sudo
installs vanishes with the container: a package the session keeps needing
belongs in the image's ``tooling``.

    uv run -m examples.launch_sudo
"""

from lup import Claude, OuterContainer


def main() -> None:
    agent = Claude(sandbox=OuterContainer(sudo=True))
    print(agent.command())


if __name__ == "__main__":
    main()
