"""Open a session in the verified container, with the runtime's own sandbox stood down.

``OuterContainer()`` builds the image — the plugin harness's own, or the one
named in ``image=`` — verifies it, starts the egress proxy, and runs the CLI
inside, as a launch left to its default does wherever Docker or Podman
answers. A session opened here inside the container names the program that
enters it as ``cli_path`` (Claude) or ``executable`` (Codex).

    uv run -m examples.launch_outer_container
"""

from lup import Claude, Codex, OuterContainer


def main() -> None:
    claude = Claude(sandbox=OuterContainer())
    codex = Codex(sandbox=OuterContainer())
    print(claude.command())
    print(codex.command())


if __name__ == "__main__":
    main()
