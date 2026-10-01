# lup: ignore[dict-get]
"""Offline behavior tests for the Docker sandbox lifecycle and REPL transport.

Typed in-process fakes stand in for the docker SDK client at the seam the
production code already injects (``sandbox.docker_client``), and a real
``socket.socketpair`` carries Docker's exec-stream multiplex framing. The
container create/adopt/sweep/destroy legs and the REPL's frame reassembly,
crash, and restart legs all run without a daemon, so a lifecycle regression
fails the unit lane instead of waiting for the nightly integration run.
"""

import io
import json
import os
import shutil
import socket
import tarfile
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path
from typing import cast

import docker
import pytest
from docker.errors import APIError, DockerException, NotFound
from docker.models.containers import Container, ExecResult
from docker.types import Mount as DockerMount

from lup.sandbox.container import Sandbox, connected_docker_client, sandbox_cleanup
from lup.sandbox.models import (
    CodeExecutionTimeoutError,
    DockerUnreachableError,
    ReplCrashedError,
    RootfulDaemonError,
    SandboxNameInUseError,
    SandboxNotInitializedError,
)
from lup.types import JsonObject
from lup.sandbox.process import process_is_alive, process_start_token
from lup.sandbox.repl import REPL_SERVER_SCRIPT, ReplSession


def frame(stream: int, payload: bytes) -> bytes:
    """One Docker exec-stream multiplex frame: 8-byte header plus payload."""
    return bytes([stream, 0, 0, 0]) + len(payload).to_bytes(4, "big") + payload


def repl_reply(exit_code: int = 0, stdout: str = "", stderr: str = "") -> bytes:
    line = json.dumps(
        {"exit_code": exit_code, "stdout": stdout, "stderr": stderr, "duration_ms": 1}
    )
    return frame(1, f"{line}\n".encode("utf-8"))


class FakeVolume:
    def __init__(self, name: str) -> None:
        self.name = name
        self.removed = False

    def remove(self) -> None:
        self.removed = True


class FakeContainer:
    def __init__(
        self,
        name: str,
        labels: dict[str, str],  # lup: ignore[dict-str-payload]
    ) -> None:
        self.name = name
        self.labels = labels
        self.id = f"id-{name}"
        self.removed = False
        self.stopped = False
        self.archive_paths: list[str] = []
        self.archives: list[bytes] = []
        self.exec_commands: list[list[str]] = []

    def remove(self, force: bool = False) -> None:
        self.removed = True

    def stop(self, timeout: int = 10) -> None:
        self.stopped = True

    def put_archive(self, path: str, data: io.BytesIO) -> bool:
        self.archive_paths.append(path)
        self.archives.append(data.read())
        return True

    def exec_run(self, cmd: list[str], demux: bool = False) -> ExecResult:
        self.exec_commands.append(cmd)
        return ExecResult(exit_code=0, output=b"ok")


class FakeContainers:
    def __init__(self) -> None:
        self.existing: dict[str, FakeContainer] = {}
        self.listed: list[FakeContainer] = []
        self.list_error: APIError | None = None
        self.run_error: DockerException | None = None

    def get(self, name: str) -> FakeContainer:
        found = self.existing.get(name)
        if found is None:
            raise NotFound(f"no such container: {name}")
        return found

    def list(
        self,
        all: bool = False,
        filters: dict[str, str] | None = None,  # lup: ignore[dict-str-payload]
    ) -> list[FakeContainer]:
        if self.list_error is not None:
            raise self.list_error
        return list(self.listed)

    def run(
        self,
        image: str,
        name: str,
        detach: bool,
        labels: dict[str, str],  # lup: ignore[dict-str-payload]
        network_mode: str,
        command: str | None = None,
        mounts: list[DockerMount] | None = None,
        working_dir: str | None = None,
        mem_limit: int | str | None = None,
        environment: dict[str, str] | None = None,  # lup: ignore[dict-str-payload]
        # The egress proxy is run with its own hardening arguments, which this
        # accepts without asserting on: what they are is the container's
        # concern, and pinning them here would only restate the call.
        **hardening: object,
    ) -> FakeContainer:
        if self.run_error is not None:
            raise self.run_error
        created = FakeContainer(name, labels)
        self.existing[name] = created
        self.last_run = {
            "image": image,
            "mounts": json.dumps(mounts or []),
            "network_mode": network_mode,
            "mem_limit": str(mem_limit),
        }
        return created


class FakeVolumes:
    def __init__(self) -> None:
        self.existing: dict[str, FakeVolume] = {}

    def get(self, name: str) -> FakeVolume:
        found = self.existing.get(name)
        if found is None:
            raise NotFound(f"no such volume: {name}")
        return found


class FakeNetwork:
    """One network the filtered mode creates and attaches the proxy to."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.connected: list[str] = []
        self.removed = False

    def connect(self, container: object, aliases: list[str] | None = None) -> None:
        self.connected.extend(aliases or [])

    def remove(self) -> None:
        self.removed = True


class FakeNetworks:
    def __init__(self) -> None:
        self.existing: dict[str, FakeNetwork] = {}
        self.created: list[str] = []

    def get(self, name: str) -> FakeNetwork:
        found = self.existing.get(name)
        if found is None:
            raise NotFound(f"no such network: {name}")
        return found

    def create(
        self, name: str, internal: bool = False, labels: object = None
    ) -> FakeNetwork:
        self.created.append(name)
        network = FakeNetwork(name)
        self.existing[name] = network
        return network


class FakeApi:
    """The exec surface: each start hands out the next prepared socket."""

    def __init__(self, sockets: list[socket.socket]) -> None:
        self.sockets = list(sockets)
        self.exec_commands: list[list[str]] = []

    def exec_create(
        self,
        container_id: str,
        cmd: list[str],
        stdin: bool,
        stdout: bool,
        stderr: bool,
        tty: bool,
        workdir: str,
        environment: dict[str, str] | None,  # lup: ignore[dict-str-payload]
    ) -> dict[str, str]:  # lup: ignore[dict-str-payload] — docker wire shape
        self.exec_commands.append(cmd)
        return {"Id": f"exec-{len(self.exec_commands)}"}

    def exec_start(self, exec_id: str, socket: bool = False) -> socket.socket:
        if not self.sockets:
            raise DockerException("no exec socket available")
        return self.sockets.pop(0)


class FakeDockerClient:
    def __init__(self) -> None:
        self.containers = FakeContainers()
        self.volumes = FakeVolumes()
        self.networks = FakeNetworks()
        self.daemon_info: JsonObject = {}
        self.api = FakeApi([])
        self.peers: list[socket.socket] = []
        self.closed = False

    def info(self) -> JsonObject:
        return self.daemon_info

    def prepare_repl_socket(self, *replies: bytes) -> None:
        """Queue an exec socket whose peer has ``replies`` pre-buffered."""
        session_side, peer = socket.socketpair()
        for reply in replies:
            peer.sendall(reply)
        self.api.sockets.append(session_side)
        self.peers.append(peer)

    def close_peers(self) -> None:
        for peer in self.peers:
            peer.close()

    def close(self) -> None:
        self.closed = True


def as_client(fake: FakeDockerClient) -> docker.DockerClient:
    # The one narrowing point for the fake client: the docker SDK ships no
    # protocol surface to implement, so the boundary is a cast by design.
    return cast(docker.DockerClient, fake)  # lup: ignore[cast]


def as_container(fake: FakeContainer) -> Container:
    return cast(Container, fake)  # lup: ignore[cast]


SHARED_ROOT = Path(tempfile.mkdtemp(prefix="lup-sandbox-lifecycle-"))
"""A writable root for the fakes below, taken from wherever temp files go here.

Asked for rather than spelled. A sandbox derives its egress config path from
the parent of its shared directory and unlinks it on the way down, so naming
a literal ``/tmp`` puts a write into a directory the suite does not own — and
``missing_ok`` covers a file that is not there, not a filesystem that will
not take one. The suite would then pass or fail on whether the machine running
it happens to keep ``/tmp`` writable, failing under a sandbox that mounts it
read-only while passing everywhere else.
"""


@pytest.fixture(scope="session", autouse=True)
def cleared_shared_root() -> Iterator[None]:
    """Take the root back down, since this module made it rather than pytest."""
    yield
    shutil.rmtree(SHARED_ROOT)


def make_sandbox(client: FakeDockerClient, **overrides: str) -> Sandbox:
    sandbox = Sandbox(
        session_id=overrides.get("session_id", "t1"),
        shared_dir=str(SHARED_ROOT / "shared"),
        pre_install=None,
    )
    sandbox.docker_client = as_client(client)
    return sandbox


class TestDurableInfrastructure:
    def test_an_ordinary_sandbox_names_the_process_that_created_it(self) -> None:
        # Which is what lets the sweep reap it the moment that process dies.
        labels = make_sandbox(FakeDockerClient()).infrastructure_labels()

        assert labels[Sandbox.OWNER_PID_LABEL] == str(os.getpid())
        assert Sandbox.DURABLE_LABEL not in labels

    def test_a_durable_sandbox_names_no_owner(self) -> None:
        # The work it holds outlives the process that queued it, so an owner
        # label would have the sweep reap it as soon as that process exits.
        sandbox = make_sandbox(FakeDockerClient())
        sandbox.durable = True

        labels = sandbox.infrastructure_labels()

        assert labels[Sandbox.DURABLE_LABEL] == "1"
        assert Sandbox.OWNER_PID_LABEL not in labels

    def test_a_durable_sandbox_is_still_reaped_once_it_ages_out(self) -> None:
        # Naming no owner must not mean living forever.
        sandbox = make_sandbox(FakeDockerClient())
        sandbox.durable = True
        aged = sandbox.infrastructure_labels() | {
            Sandbox.CREATED_AT_LABEL: str(time.time() - 48 * 3600)
        }

        assert sandbox.container_is_orphaned(aged)

    def test_a_fresh_durable_sandbox_survives_the_sweep(self) -> None:
        sandbox = make_sandbox(FakeDockerClient())
        sandbox.durable = True

        assert not sandbox.container_is_orphaned(sandbox.infrastructure_labels())


class TestRootlessRequirement:
    def rootless_client(self, *options: str) -> FakeDockerClient:
        client = FakeDockerClient()
        client.daemon_info = {"SecurityOptions": list(options)}
        return client

    def test_a_rootful_daemon_is_refused_when_the_boundary_is_required(self) -> None:
        sandbox = make_sandbox(self.rootless_client("name=seccomp,profile=builtin"))
        sandbox.require_rootless = True

        with pytest.raises(RootfulDaemonError):
            sandbox.verify_rootless_daemon()

    def test_a_rootless_daemon_satisfies_the_requirement(self) -> None:
        sandbox = make_sandbox(self.rootless_client("name=rootless"))
        sandbox.require_rootless = True

        sandbox.verify_rootless_daemon()

    def test_a_daemon_reporting_nothing_is_refused(self) -> None:
        # Absent evidence reads as absent boundary, not as a pass.
        sandbox = make_sandbox(self.rootless_client())
        sandbox.require_rootless = True

        with pytest.raises(RootfulDaemonError):
            sandbox.verify_rootless_daemon()

    def test_the_check_is_off_unless_a_caller_asks(self) -> None:
        sandbox = make_sandbox(self.rootless_client("name=seccomp"))

        sandbox.verify_rootless_daemon()


class TestFilteredEgress:
    def test_the_sandbox_network_is_internal_and_the_proxy_bridges_it(
        self, tmp_path: Path
    ) -> None:
        # The mode rests on the network having no gateway: the proxy is the
        # only member of both sides, reached under a fixed alias.
        client = FakeDockerClient()
        sandbox = Sandbox(
            session_id="t1",
            shared_dir=tmp_path / "shared",
            network_mode="filtered",
            pre_install=None,
        )
        sandbox.docker_client = as_client(client)

        sandbox.start_filtered_egress()

        assert client.networks.created == [sandbox.network_name]
        assert client.networks.existing[sandbox.network_name].connected == ["egress"]
        assert sandbox.proxy_config_path.read_text(encoding="utf-8").startswith(
            "http_port"
        )

    def test_a_filtered_container_is_pointed_at_the_proxy(self, tmp_path: Path) -> None:
        sandbox = Sandbox(
            session_id="t1",
            shared_dir=tmp_path / "shared",
            network_mode="filtered",
            pre_install=None,
        )

        environment = sandbox.container_environment(filtered=True)

        assert environment["HTTPS_PROXY"] == "http://egress:3128"
        assert environment["NO_PROXY"] == ""

    def test_a_bridge_container_is_given_no_proxy(self, tmp_path: Path) -> None:
        sandbox = Sandbox(
            session_id="t1", shared_dir=tmp_path / "shared", pre_install=None
        )

        assert sandbox.container_environment(filtered=False) == {}

    def test_teardown_removes_the_proxy_network_and_config(
        self, tmp_path: Path
    ) -> None:
        client = FakeDockerClient()
        sandbox = Sandbox(
            session_id="t1",
            shared_dir=tmp_path / "shared",
            network_mode="filtered",
            pre_install=None,
        )
        sandbox.docker_client = as_client(client)
        sandbox.start_filtered_egress()

        sandbox.stop_filtered_egress()

        assert client.networks.existing[sandbox.network_name].removed
        assert not sandbox.proxy_config_path.exists()


class TestStaleRemoval:
    def test_same_name_container_is_force_removed(self) -> None:
        client = FakeDockerClient()
        sandbox = make_sandbox(client)
        stale = FakeContainer(sandbox.container_name, {})
        client.containers.existing[sandbox.container_name] = stale

        sandbox.remove_stale_container()

        assert stale.removed

    def test_a_live_owners_container_is_refused_not_taken(self) -> None:
        # Two sessions that named themselves alike both reach this name; the
        # one already working inside it keeps it.
        client = FakeDockerClient()
        sandbox = make_sandbox(client)
        working = FakeContainer(sandbox.container_name, sandbox.infrastructure_labels())
        client.containers.existing[sandbox.container_name] = working

        with pytest.raises(SandboxNameInUseError):
            sandbox.remove_stale_container()

        assert not working.removed

    def test_absent_container_is_a_no_op(self) -> None:
        sandbox = make_sandbox(FakeDockerClient())

        sandbox.remove_stale_container()


class TestOrphanSweep:
    def orphan(self, name: str, volume: str) -> FakeContainer:
        return FakeContainer(
            name,
            {
                Sandbox.SANDBOX_LABEL: "1",
                Sandbox.CREATED_AT_LABEL: "0",
                Sandbox.VOLUME_LABEL: volume,
            },
        )

    def test_dead_owner_container_and_volume_are_swept(self) -> None:
        client = FakeDockerClient()
        sandbox = make_sandbox(client)
        orphan = self.orphan("lup-sandbox-dead", "lup-sandbox-ws-dead")
        volume = FakeVolume("lup-sandbox-ws-dead")
        client.containers.listed = [orphan]
        client.volumes.existing[volume.name] = volume

        sandbox.sweep_orphaned_containers()

        assert orphan.removed
        assert volume.removed

    def test_own_container_is_never_swept(self) -> None:
        client = FakeDockerClient()
        sandbox = make_sandbox(client)
        own = self.orphan(sandbox.container_name, sandbox.volume_name)
        client.containers.listed = [own]

        sandbox.sweep_orphaned_containers()

        assert not own.removed

    def test_live_owner_container_is_kept(self) -> None:
        client = FakeDockerClient()
        sandbox = make_sandbox(client)
        live = FakeContainer(
            "lup-sandbox-live",
            {
                Sandbox.SANDBOX_LABEL: "1",
                Sandbox.OWNER_PID_LABEL: str(os.getpid()),
                Sandbox.OWNER_START_LABEL: process_start_token(os.getpid()) or "",
            },
        )
        client.containers.listed = [live]

        sandbox.sweep_orphaned_containers()

        assert not live.removed

    def test_listing_failure_degrades_to_a_warning(self) -> None:
        client = FakeDockerClient()
        client.containers.list_error = APIError("daemon down")
        sandbox = make_sandbox(client)

        sandbox.sweep_orphaned_containers()


class TestDestroy:
    def test_teardown_survives_an_unremovable_egress_config(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`missing_ok` answers absence, which is not the only way unlink fails.

        A read-only or permission-denied location raises rather than reporting
        the file gone, and left to escape a teardown whose every other step
        logs and continues, it would leave a container and its volume behind
        over a configuration file, at the one moment nothing is left to retry.
        """
        client = FakeDockerClient()
        sandbox = make_sandbox(client)
        active = FakeContainer(sandbox.container_name, {})
        volume = FakeVolume(sandbox.volume_name)
        client.volumes.existing[volume.name] = volume
        sandbox.active_container = as_container(active)

        def refuse(*_args: object, **_kwargs: object) -> None:
            raise OSError(30, "Read-only file system")

        monkeypatch.setattr(Path, "unlink", refuse)

        sandbox.destroy_container()

        assert active.removed and volume.removed
        assert sandbox.active_container is None

    def test_container_and_volume_are_removed(self) -> None:
        client = FakeDockerClient()
        sandbox = make_sandbox(client)
        active = FakeContainer(sandbox.container_name, {})
        volume = FakeVolume(sandbox.volume_name)
        client.volumes.existing[volume.name] = volume
        sandbox.active_container = as_container(active)

        sandbox.destroy_container()

        assert active.stopped and active.removed
        assert volume.removed
        assert sandbox.active_container is None


class TestStartAndStop:
    def start_ready_client(self) -> FakeDockerClient:
        client = FakeDockerClient()
        client.prepare_repl_socket(repl_reply())
        return client

    def test_context_round_trip_creates_and_tears_down(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        client = self.start_ready_client()
        monkeypatch.setattr(docker, "from_env", lambda: as_client(client))

        with Sandbox(
            session_id="round", shared_dir=tmp_path / "shared", pre_install=["pkgx"]
        ) as sandbox:
            created = client.containers.existing[sandbox.container_name]
            assert created.labels[Sandbox.SANDBOX_LABEL] == "1"
            assert created.labels[Sandbox.VOLUME_LABEL] == sandbox.volume_name
            assert created.labels[Sandbox.OWNER_PID_LABEL] == str(os.getpid())
            assert sandbox.volume_name in client.containers.last_run["mounts"]
            assert ["uv", "pip", "install", "--system", "pkgx"] in (
                created.exec_commands
            )
            assert created.archive_paths == ["/workspace"]
            with tarfile.open(fileobj=io.BytesIO(created.archives[0])) as tar:
                member = tar.extractfile(".repl_server.py")
                assert member is not None
                assert member.read().decode("utf-8") == REPL_SERVER_SCRIPT
            assert sandbox.repl is not None

        assert created.stopped and created.removed
        assert client.closed
        client.close_peers()

    def test_a_sandbox_naming_no_ceiling_gets_the_default(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        client = self.start_ready_client()
        monkeypatch.setattr(docker, "from_env", lambda: as_client(client))

        with Sandbox(session_id="default-mem", shared_dir=tmp_path / "shared"):
            assert client.containers.last_run["mem_limit"] == str(
                Sandbox.DEFAULT_MEMORY_LIMIT_BYTES
            )

        client.close_peers()

    def test_a_callers_ceiling_reaches_the_container(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A workload that knows what it needs must be able to say so.

        Asserted at the ``containers.run`` call rather than on the attribute,
        because a ceiling held on the sandbox and never passed to Docker is
        exactly the failure this parameter exists to prevent — and it reads
        as configured right up until a cell dies against the default ceiling.
        """
        client = self.start_ready_client()
        monkeypatch.setattr(docker, "from_env", lambda: as_client(client))

        with Sandbox(
            session_id="big-mem",
            shared_dir=tmp_path / "shared",
            memory_limit_bytes=16 * 1024**3,
        ):
            assert client.containers.last_run["mem_limit"] == str(16 * 1024**3)

        client.close_peers()

    def test_network_none_skips_pre_install(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        client = self.start_ready_client()
        monkeypatch.setattr(docker, "from_env", lambda: as_client(client))

        with Sandbox(
            session_id="offline",
            shared_dir=tmp_path / "shared",
            network_mode="none",
            pre_install=["pkgx"],
        ) as sandbox:
            created = client.containers.existing[sandbox.container_name]
            assert created.exec_commands == []

        client.close_peers()

    def test_creation_failure_tears_down_and_raises(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        client = FakeDockerClient()
        client.containers.run_error = DockerException("image missing")
        monkeypatch.setattr(docker, "from_env", lambda: as_client(client))

        with pytest.raises(DockerException, match="image missing"):
            with Sandbox(session_id="broken", shared_dir=tmp_path / "shared"):
                pass

        assert client.closed


class TestReplTransport:
    def session(self, client: FakeDockerClient) -> ReplSession:
        container = FakeContainer("repl-host", {})
        return ReplSession(as_client(client), as_container(container), {})

    def test_response_reassembles_across_frames_and_ignores_stderr(self) -> None:
        line = json.dumps(
            {"exit_code": 0, "stdout": "4\n", "stderr": "", "duration_ms": 2}
        )
        first, second = f"{line}\n".encode("utf-8")[:9], f"{line}\n".encode("utf-8")[9:]
        client = FakeDockerClient()
        client.prepare_repl_socket(
            repl_reply(),
            frame(1, first) + frame(2, b"progress noise") + frame(1, second),
        )
        repl = self.session(client)
        repl.start()

        result = repl.execute("print(2 + 2)", timeout_seconds=10)

        assert result.exit_code == 0
        assert result.stdout == "4\n"
        repl.stop()
        client.close_peers()

    def test_exit_code_124_raises_timeout(self) -> None:
        client = FakeDockerClient()
        client.prepare_repl_socket(repl_reply(), repl_reply(exit_code=124))
        repl = self.session(client)
        repl.start()

        with pytest.raises(CodeExecutionTimeoutError):
            repl.execute("while True: pass", timeout_seconds=1)

        repl.stop()
        client.close_peers()

    def test_non_json_line_is_a_crash(self) -> None:
        client = FakeDockerClient()
        client.prepare_repl_socket(repl_reply(), frame(1, b"Segmentation fault\n"))
        repl = self.session(client)
        repl.start()

        with pytest.raises(ReplCrashedError, match="non-JSON"):
            repl.execute("boom()", timeout_seconds=10)

        repl.stop()
        client.close_peers()

    def test_peer_eof_is_a_crash(self) -> None:
        client = FakeDockerClient()
        client.prepare_repl_socket(repl_reply())
        repl = self.session(client)
        repl.start()
        client.close_peers()

        with pytest.raises(ReplCrashedError):
            repl.execute("print('gone')", timeout_seconds=10)

        repl.stop()

    def test_execute_without_start_is_not_initialized(self) -> None:
        repl = self.session(FakeDockerClient())

        with pytest.raises(SandboxNotInitializedError):
            repl.execute("pass", timeout_seconds=10)


class TestRunCodeCrashRecovery:
    def crashed_sandbox(self, client: FakeDockerClient) -> Sandbox:
        client.prepare_repl_socket(repl_reply())
        sandbox = make_sandbox(client)
        container = FakeContainer(sandbox.container_name, {})
        sandbox.active_container = as_container(container)
        sandbox.repl = ReplSession(as_client(client), as_container(container), {})
        sandbox.repl.start()
        client.peers[0].close()
        return sandbox

    def test_crash_restarts_the_repl_and_reports_lost_state(self) -> None:
        client = FakeDockerClient()
        sandbox = self.crashed_sandbox(client)
        client.prepare_repl_socket(repl_reply())

        result = sandbox.run_code("print('x')")

        assert result.exit_code == 1
        assert "Variables from previous cells have been lost" in result.stderr
        assert sandbox.repl is not None
        client.close_peers()

    def test_failed_reexec_rebuilds_the_container_and_names_the_deeper_loss(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A re-exec cannot succeed when the container itself is gone.

        Recovery has to escalate to a rebuild inside this same call —
        deferring it to the next one hands the caller a wiped namespace
        reported as an ordinary success.
        """
        sandbox = self.crashed_sandbox(FakeDockerClient())
        sandbox.shared_dir = tmp_path / "shared"
        rebuilt = FakeDockerClient()
        rebuilt.prepare_repl_socket(repl_reply())
        monkeypatch.setattr(docker, "from_env", lambda: as_client(rebuilt))

        result = sandbox.run_code("print('x')")

        assert result.exit_code == 1
        assert "Variables and installed packages have been lost" in result.stderr
        assert sandbox.repl is not None
        assert sandbox.container_name in rebuilt.containers.existing
        rebuilt.close_peers()

    def test_failed_rebuild_raises_chained_to_the_original_crash(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sandbox = self.crashed_sandbox(FakeDockerClient())

        def refuse_client() -> docker.DockerClient:
            raise DockerException("daemon gone")

        monkeypatch.setattr(docker, "from_env", refuse_client)

        with pytest.raises(
            SandboxNotInitializedError, match="could not be rebuilt"
        ) as raised:
            sandbox.run_code("print('x')")

        assert isinstance(raised.value.__cause__, ReplCrashedError)
        assert sandbox.repl is None


class TestSandboxCleanup:
    def test_container_and_volume_are_removed_after_the_block(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        client = FakeDockerClient()
        probe = Sandbox(session_id="swept", shared_dir=tmp_path)
        leftover = FakeContainer(probe.container_name, {})
        volume = FakeVolume(probe.volume_name)
        client.containers.existing[leftover.name] = leftover
        client.volumes.existing[volume.name] = volume
        monkeypatch.setattr(docker, "from_env", lambda: as_client(client))

        with sandbox_cleanup("swept", tmp_path):
            pass

        assert leftover.removed
        assert volume.removed
        assert client.closed

    def test_nothing_leftover_is_tolerated(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        client = FakeDockerClient()
        monkeypatch.setattr(docker, "from_env", lambda: as_client(client))

        with sandbox_cleanup("clean", tmp_path):
            pass

        assert client.closed


class TestOrphanAgeFallback:
    def test_unlabelled_recent_container_is_kept(self) -> None:
        sandbox = make_sandbox(FakeDockerClient())
        labels = {Sandbox.CREATED_AT_LABEL: str(time.time())}

        assert sandbox.container_is_orphaned(labels) is False


class TestDockerReachability:
    @staticmethod
    def refuse_connections(monkeypatch: pytest.MonkeyPatch) -> None:
        """Make the daemon unreachable the way a denied socket reads."""

        def refuse_client() -> docker.DockerClient:
            raise DockerException("Operation not permitted")

        monkeypatch.setattr(docker, "from_env", refuse_client)

    def test_an_unreachable_daemon_names_what_to_check(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self.refuse_connections(monkeypatch)

        with pytest.raises(DockerUnreachableError, match="sandbox is denying"):
            connected_docker_client()

    def test_cleanup_leaves_the_session_failure_to_propagate(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        self.refuse_connections(monkeypatch)

        with pytest.raises(ValueError, match="what the session raised"):
            with sandbox_cleanup(session_id="masked", shared_dir=tmp_path):
                raise ValueError("what the session raised")


HAVE_PROC = Path("/proc/self/stat").exists()


def unstarted_sandbox() -> Sandbox:
    """A Sandbox whose __init__ touches no Docker (no start() called)."""
    return Sandbox(session_id="liveness-test", shared_dir="/tmp/lup-test-shared")


NEVER_A_PID = 0x7FFFFFFF  # far above any real Linux PID; os.kill -> ProcessLookupError


class TestProcessLiveness:
    def test_own_process_is_alive(self) -> None:
        token = process_start_token(os.getpid())
        assert process_is_alive(os.getpid(), token) is True

    def test_absent_pid_is_dead(self) -> None:
        assert process_is_alive(NEVER_A_PID, None) is False
        assert process_is_alive(0, None) is False
        assert process_is_alive(-1, None) is False

    @pytest.mark.skipif(not HAVE_PROC, reason="needs /proc start tokens")
    def test_reused_pid_token_mismatch_is_dead(self) -> None:
        """A live PID with a stale token means the original owner is gone."""
        assert process_start_token(os.getpid()) is not None
        assert process_is_alive(os.getpid(), "0") is False

    @pytest.mark.skipif(not HAVE_PROC, reason="needs /proc start tokens")
    def test_start_token_absent_for_dead_pid(self) -> None:
        assert process_start_token(NEVER_A_PID) is None


class TestContainerOrphanDecision:
    def test_live_owner_kept_even_when_ancient(self) -> None:
        """A long-lived owner keeps its container past STALE_AGE_HOURS."""
        sandbox = unstarted_sandbox()
        ancient = str(time.time() - sandbox.STALE_AGE_HOURS * 3600 * 10)
        labels = {
            sandbox.OWNER_PID_LABEL: str(os.getpid()),
            sandbox.OWNER_START_LABEL: process_start_token(os.getpid()) or "",
            sandbox.CREATED_AT_LABEL: ancient,
        }
        assert sandbox.container_is_orphaned(labels) is False

    def test_dead_owner_is_orphaned(self) -> None:
        sandbox = unstarted_sandbox()
        labels = {
            sandbox.OWNER_PID_LABEL: str(NEVER_A_PID),
            sandbox.CREATED_AT_LABEL: str(time.time()),  # young, but owner gone
        }
        assert sandbox.container_is_orphaned(labels) is True

    def test_unparseable_owner_pid_is_orphaned(self) -> None:
        sandbox = unstarted_sandbox()
        assert sandbox.container_is_orphaned({sandbox.OWNER_PID_LABEL: "nope"}) is True

    def test_age_fallback_when_no_owner_label(self) -> None:
        sandbox = unstarted_sandbox()
        fresh = {sandbox.CREATED_AT_LABEL: str(time.time())}
        old = {
            sandbox.CREATED_AT_LABEL: str(
                time.time() - sandbox.STALE_AGE_HOURS * 3600 - 60
            )
        }
        assert sandbox.container_is_orphaned(fresh) is False
        assert sandbox.container_is_orphaned(old) is True
        # No labels at all -> created_at defaults to 0 -> treated as stale.
        assert sandbox.container_is_orphaned({}) is True
