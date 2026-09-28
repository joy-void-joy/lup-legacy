"""Let a session reach folders outside its working tree, whichever wall it opens behind.

A ``Mount`` on the sandbox is mounted into the container, widens the inner
sandbox's write root where it is writable, and is read as the session's own
by the policy under any wall — so where a session runs never decides what it
can write.

    uv run -m examples.launch_mounts
"""

from pathlib import Path

from lup import Claude, InnerSandbox, Mount, OuterContainer


def main() -> None:
    mounts = [
        Mount(path=Path("../notes"), writable=True),
        Mount(path=Path("../reference")),
    ]
    inner = Claude(sandbox=InnerSandbox(mounts=mounts))
    outer = Claude(sandbox=OuterContainer(mounts=mounts))
    print(inner.command())
    print(outer.command())


if __name__ == "__main__":
    main()
