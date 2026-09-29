"""Run every session in a configuration home named outright.

``home=`` wins over the home a profile resolves to, the way an explicit
directory outranks a name looked up: Claude Code starts with it as
``CLAUDE_CONFIG_DIR`` and Codex as ``CODEX_HOME``, the launch deriving no
worktree home of its own.

    uv run -m examples.launch_home
"""

from pathlib import Path

from lup import Claude, Codex


def main() -> None:
    claude = Claude(home=Path.home() / ".claude-work")
    codex = Codex(home=Path.home() / ".codex-work")
    print(claude.command())
    print(codex.command())


if __name__ == "__main__":
    main()
