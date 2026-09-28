"""Reopen an earlier session: the newest here, the runtime's picker, or one by id.

``resume=`` is ``Latest()`` (``--continue`` on Claude Code, ``resume
--last`` on Codex), ``Pick()`` (the runtime's own picker, which only a
terminal has) or ``Reopen(session=...)``. A session opened here takes
``Latest()`` or an id; ``open(resume=...)`` names one for a single session.

    uv run -m examples.launch_resume
"""

from lup import Claude, Codex, Latest, Reopen, SessionId


def main() -> None:
    claude = Claude(resume=Latest())
    codex = Codex(resume=Reopen(session=SessionId(value="0199a7c4-demo")))
    print(claude.command())
    print(codex.command())


if __name__ == "__main__":
    main()
