"""Reach a service on the host's loopback from a contained session, by its name alone.

A ``HostService`` names a service the operator already runs on the host's
loopback, and the variable the session reads its address from. On the host
the session is handed the service's own address; in a container whose
loopback is its own, the launch relays that one port through a socket mounted
into it, and nothing else on the host's loopback is reachable that way.

    uv run -m examples.launch_host_service
"""

from lup import Claude, HostService, OuterContainer


def main() -> None:
    model = HostService(name="model-server", port=11434, variable="MODEL_URL")
    agent = Claude(sandbox=OuterContainer(), companions=[model])
    print(agent.command().env["MODEL_URL"])


if __name__ == "__main__":
    main()
