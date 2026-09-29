"""Say how much a launched session asks before it acts, in each runtime's own words.

Claude Code's ``permission_mode`` and Codex's ``approval_policy`` are the
fields; ``approvals_reviewer="auto_review"`` has a reviewer agent answer what
Codex asks under ``on-request`` in the person's place. Both are meant for the
inside of the container, where it stands in for what they take away; a
launched session asks the person at its terminal, and a session opened in this
process that asks is refused without hooks to answer it.

    uv run -m examples.launch_asking
"""

from lup import Claude, Codex, OuterContainer


def main() -> None:
    claude = Claude(sandbox=OuterContainer(), permission_mode="auto")
    codex = Codex(
        sandbox=OuterContainer(),
        approval_policy="on-request",
        approvals_reviewer="auto_review",
    )
    print(claude.command())
    print(codex.command())


if __name__ == "__main__":
    main()
