"""Run every session as one of the person's accounts, by the profile's name.

``profile=`` is resolved against the checkout's ``.lup/profiles`` and then
the person's ``~/.config/lup/profiles``, the same name meaning the same
account on both runtimes: Claude Code runs in the profile's config home,
Codex in a home derived from the profile's for this worktree. An unknown
name is refused, listing the known ones.

    uv run -m examples.launch_profile
"""

from lup import Claude, Codex


def main() -> None:
    claude = Claude(profile="work")
    codex = Codex(profile="work")
    print(claude.command())
    print(codex.command())


if __name__ == "__main__":
    main()
