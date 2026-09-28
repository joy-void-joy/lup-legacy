"""Join the repository's coordination roster under a name, woken through an inbox.

``identity=Member(...)`` mints the session a roster id before it starts and
exports it, so its tool servers and hooks answer to one address; ``name``
is what the roster calls it (the worktree's name, unset), and ``inboxes``
where it binds the socket a peer nudges it through — ``None`` binds none,
and the session reads its mail at its next tool call.

    uv run -m examples.launch_identity
"""

from lup import Claude, Codex, Member


def main() -> None:
    claude = Claude(identity=Member(name="reviewer"))
    codex = Codex(identity=Member(name="reviewer", inboxes=None))
    print(claude.command())
    print(codex.command())


if __name__ == "__main__":
    main()
