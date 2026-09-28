"""Open a session on the host, inside the runtime's own workspace-write sandbox.

``InnerSandbox()`` is Claude Code's sandbox enabled through ``--settings``
and Codex's ``--sandbox workspace-write`` envelope, each widened to the
sibling worktrees and the declared mounts. Claude Code can let one command
ask to run outside it, for the policy to judge (``escapable``), and take
named commands out (``excluded_commands``); Codex's envelope has no such
way out, and refuses a declaration asking for one.

    uv run -m examples.launch_inner_sandbox
"""

from lup import Claude, Codex, InnerSandbox


def main() -> None:
    claude = Claude(sandbox=InnerSandbox(escapable=True, excluded_commands=["git *"]))
    codex = Codex(sandbox=InnerSandbox())
    print(claude.command())
    print(codex.command())


if __name__ == "__main__":
    main()
