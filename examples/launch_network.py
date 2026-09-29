"""Choose the network a contained session joins.

``network=`` on ``OuterContainer`` overrides the image's egress: ``filtered``
behind the egress proxy (the default), ``bridge``, ``host`` — the host's own
network namespace, where a callback the session listens for reaches the
operator's browser — or ``none``. `harness claude|codex --network` states the
same field, over a mode's, the person's ``[container]`` config and the
project's.

    uv run -m examples.launch_network
"""

from lup import Claude, Codex, OuterContainer


def main() -> None:
    claude = Claude(sandbox=OuterContainer(network="host"))
    codex = Codex(sandbox=OuterContainer(network="bridge"))
    print(claude.command())
    print(codex.command())


if __name__ == "__main__":
    main()
