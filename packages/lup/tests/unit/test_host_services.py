"""A service on the host's loopback, reached from a container only through its declared name.

A filtered container's loopback is its own, and the egress proxy refuses the
host's, so a service the operator runs there — a model server, a database, a
preview — is out of reach. A ``HostService`` is held around the session as
every host companion is: on the host it hands the session the service's own
address; for a container whose loopback is its own, the launcher listens on a
socket of the service's, mounted into the container, and forwards what arrives
to that one port and nothing else. Inside, the entrypoint binds the address the
session was told of to that socket.
"""

import os
import shutil
import signal
import socket
import subprocess
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest
from pydantic import ValidationError

from lup.harness.image import Image
from lup.launch.companions import CompanionLaunch, HostService, held_companions
from lup.launch.declaration import (
    InnerSandbox,
    Mount,
    NoSandbox,
    OuterContainer,
    loopback_relayed,
)


@pytest.fixture
def echo() -> Iterator[int]:
    """A service on the host's loopback that answers every line with itself."""
    listening = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listening.bind(("127.0.0.1", 0))
    listening.listen()

    def serve() -> None:
        while True:
            try:
                connection, _ = listening.accept()
            except OSError:
                return
            with connection:
                while data := connection.recv(1024):
                    connection.sendall(data)

    threading.Thread(target=serve, daemon=True).start()
    yield listening.getsockname()[1]
    listening.close()


def launch(tmp_path: Path, relayed: bool) -> CompanionLaunch:
    return CompanionLaunch(
        root=tmp_path, runtime="claude", environment={}, relayed=relayed
    )


def test_on_the_host_the_session_is_handed_the_service_s_own_address(
    tmp_path: Path,
) -> None:
    model = HostService(name="model-server", port=11434, variable="MODEL_URL")

    with held_companions([model], launch(tmp_path, relayed=False)) as joined:
        assert joined.environment == {"MODEL_URL": "http://127.0.0.1:11434"}
        assert joined.mounts == []


def test_a_contained_session_reaches_the_service_through_its_socket_alone(
    tmp_path: Path, echo: int
) -> None:
    model = HostService(name="model-server", port=echo, variable="MODEL_URL")

    with held_companions([model], launch(tmp_path, relayed=True)) as joined:
        ((directory,),) = [[mount.path for mount in joined.mounts]]
        relay = joined.environment["LUP_HOST_SERVICE_MODEL_SERVER"]
        assert joined.environment["MODEL_URL"] == f"http://127.0.0.1:{echo}"
        assert relay == f"{directory / 'model-server.sock'}@{echo}"
        assert joined.mounts == [Mount(path=directory, writable=True)]
        assert not directory.is_relative_to(tmp_path)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.connect(str(directory / "model-server.sock"))
            client.sendall(b"hello\n")
            assert client.recv(1024) == b"hello\n"

    assert not directory.exists()


def test_each_service_is_relayed_under_a_variable_of_its_own(
    tmp_path: Path, echo: int
) -> None:
    services = [
        HostService(name="model", port=echo, variable="MODEL_URL"),
        HostService(name="db", port=echo, variable="DATABASE_URL", scheme="postgres"),
    ]

    with held_companions(services, launch(tmp_path, relayed=True)) as joined:
        assert joined.environment["DATABASE_URL"] == f"postgres://127.0.0.1:{echo}"
        assert {"LUP_HOST_SERVICE_MODEL", "LUP_HOST_SERVICE_DB"} <= set(
            joined.environment
        )
        assert len(joined.mounts) == 2


def test_a_variable_no_shell_could_export_is_refused() -> None:
    with pytest.raises(ValidationError):
        HostService(name="model", port=8080, variable="model-url")


def test_the_entrypoint_binds_each_relayed_service_to_the_port_it_was_told_of() -> None:
    entrypoint = Image().entrypoint()

    assert "LUP_HOST_SERVICE_" in entrypoint
    assert "socat" in entrypoint
    assert entrypoint.index("LUP_HOST_SERVICE_") < entrypoint.index("exec setpriv")


@pytest.mark.skipif(shutil.which("socat") is None, reason="socat is not on PATH")
def test_the_entrypoint_s_relay_carries_a_connection_to_the_host_service(
    tmp_path: Path, echo: int
) -> None:
    """The relay the image's entrypoint starts, run as rendered, with this host as its inside."""
    model = HostService(name="model", port=echo, variable="MODEL_URL")
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        inside = probe.getsockname()[1]
    script = tmp_path / "entrypoint"
    script.write_text(Image(config_home=str(tmp_path / "cfg")).entrypoint())

    with held_companions([model], launch(tmp_path, relayed=True)) as joined:
        (directory,) = [mount.path for mount in joined.mounts]
        started = subprocess.Popen(
            ["sh", str(script), "sleep", "5"],
            env={
                "PATH": "/usr/sbin:/usr/bin:/bin",
                "LUP_HOST_SERVICE_MODEL": f"{directory / 'model.sock'}@{inside}",
            },
            start_new_session=True,
        )
        try:
            answered = b""
            for _ in range(50):
                try:
                    with socket.create_connection(("127.0.0.1", inside), 0.2) as client:
                        client.sendall(b"through\n")
                        answered = client.recv(1024)
                    break
                except OSError:
                    threading.Event().wait(0.1)
            assert answered == b"through\n"
        finally:
            os.killpg(started.pid, signal.SIGKILL)
            started.wait()


def test_only_a_container_with_a_loopback_of_its_own_is_relayed() -> None:
    assert loopback_relayed(None, OuterContainer())
    assert loopback_relayed(None, OuterContainer(network="none"))
    assert not loopback_relayed(None, OuterContainer(network="host"))
    assert not loopback_relayed(None, InnerSandbox())
    assert not loopback_relayed(None, NoSandbox())
