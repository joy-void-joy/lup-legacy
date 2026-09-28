"""Launch Claude Code and Codex on a plugin built beside the declaration.

``plugin=`` is a directory some earlier step built — or a ``Harness``
declaration, which ``prepare()`` compiles into this project's tree first.
Claude Code loads it with ``--plugin-dir``; Codex installs the one its
project's marketplace offers into the launch's home. It brings skills,
agents and hooks; the servers a session carries are the declaration's
``tools.mcp``, never the plugin's.

    uv run -m examples.launch_plugin
"""

from pathlib import Path

from lup import Claude, Codex


def main() -> None:
    claude = Claude(plugin=Path(".claude/plugins/lup"))
    codex = Codex(plugin=Path.cwd())
    print(claude.command())
    print(codex.command())


if __name__ == "__main__":
    main()
