"""Grant a contained session a host device, named the way CDI names it.

``devices=`` on ``OuterContainer`` hands the container each device, and the
launch withholds one the engine's registry does not know, saying so. A
session on the host holds the host's devices already.

    uv run -m examples.launch_devices
"""

from lup import Claude, OuterContainer
from lup.harness.devices import Device


def main() -> None:
    agent = Claude(sandbox=OuterContainer(devices=[Device(name="nvidia.com/gpu=all")]))
    print(agent.command())


if __name__ == "__main__":
    main()
