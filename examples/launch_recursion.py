"""Bound how many more levels of lup-created agents a session may open.

``max_recursive_agent=`` narrows the allowance the launching process holds,
one level spent by the session itself, and never widens it: a declaration
cannot hand a session more levels than its opener has left. ``-1`` is no
limit on either side.

    uv run -m examples.launch_recursion
"""

from lup import Claude, Codex


def main() -> None:
    claude = Claude(max_recursive_agent=1)
    codex = Codex(max_recursive_agent=0)
    print(claude.command().env["LUP_MAX_RECURSIVE_AGENT"])
    print(codex.command().env["LUP_MAX_RECURSIVE_AGENT"])


if __name__ == "__main__":
    main()
