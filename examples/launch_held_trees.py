"""Hold the generated trees a contained session runs from, so it cannot change its hooks.

``hold_generated=True`` on ``OuterContainer`` binds the runtime's generated
tree read-only into the container: Claude Code's plugin directory whole, with
the project settings and guidance, or the plugin Codex installs from, with its
project configuration. Regenerating them is then the host's work — `harness
generate all` in the session refuses before writing anything and names the
host command — and so is any git command rewriting them in this checkout.

    uv run -m examples.launch_held_trees
"""

from lup import Claude, Codex, OuterContainer


def main() -> None:
    claude = Claude(sandbox=OuterContainer(hold_generated=True))
    codex = Codex(sandbox=OuterContainer(hold_generated=True))
    print(claude.command())
    print(codex.command())


if __name__ == "__main__":
    main()
