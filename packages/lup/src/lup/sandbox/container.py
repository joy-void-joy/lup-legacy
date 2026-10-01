"""Docker container lifecycle for the Python sandbox.

The ``Sandbox`` context manager owns one session's container and volume:
create, mount, orphan-sweep, run code/installs through the REPL, and destroy.
``sandbox_cleanup`` guarantees teardown for sandboxes served from a tool
subprocess that may die without a graceful exit.

Network modes:
- "bridge": Full network access (default)
- "filtered": Public HTTP(S) only, through a proxy on a private network — the
  container itself is given no route out, so a private address it might reach
  on a shared bridge (a metadata endpoint, a service on the host's LAN) is
  unreachable rather than merely discouraged
- "none": No network access at all

Examples:
    Run code in an isolated sandbox::

        >>> with Sandbox(session_id="demo", shared_dir="/tmp/shared") as sb:
        ...     result = sb.run_code("import math; print(math.pi)")
        ...     result.stdout
        '3.141592653589793\\n'
        ...     result.exit_code
        0

    A cell ending in an expression echoes it, the way a notebook cell does::

        >>> with Sandbox(session_id="demo", shared_dir="/tmp/shared") as sb:
        ...     sb.run_code("import math; math.pi").result
        '3.141592653589793'

    State persists across calls within the same session::

        >>> with Sandbox(session_id="demo", shared_dir="/tmp/shared") as sb:
        ...     sb.run_code("x = 42")
        ...     result = sb.run_code("print(x * 2)")
        ...     result.stdout
        '84\\n'

    Install packages and create MCP tools for an agent::

        >>> with Sandbox(session_id="demo", shared_dir="/tmp/shared") as sb:
        ...     sb.run_install(["scipy"])
        ...     server = sb.create_mcp_server(name="sandbox")
"""

import io
import logging
import os
import tarfile
import time
from collections.abc import Generator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from types import TracebackType
from typing import Self

try:
    import docker
except ImportError as exc:
    raise ImportError(
        "lup.sandbox requires the 'docker' extra. Install with: uv sync --extra docker"
    ) from exc
from docker.errors import APIError, DockerException, NotFound
from docker.models.containers import Container, ExecResult
from docker.models.networks import Network
from docker.types import Mount as DockerMount
from docker.utils.socket import SocketError
from pydantic import TypeAdapter

from lup.tools.mcp import (
    LupMcpServerConfig,
    LupMcpTool,
    ToolError,
    create_mcp_server,
    lup_tool,
)
from lup.observability.replay import (
    CellOutcome,
    JournalCell,
    JournalStore,
    ReplayReport,
    UnreadableJournalError,
)
from lup.sandbox.models import (
    MountMode,
    DEFAULT_PRE_INSTALL,
    CodeExecutionTimeoutError,
    PathNotMountedError,
    DockerUnreachableError,
    ExecuteCodeInput,
    ExecuteCodeResult,
    InstallPackageInput,
    InstallPackageResult,
    Mount,
    NetworkMode,
    ReplCrashedError,
    SandboxNameInUseError,
    SandboxNotInitializedError,
    SandboxReplayInput,
)
from lup.sandbox.attribution import (
    Attribution,
    attribute_egress,
    attribute_filesystem,
)
from lup.sandbox.egress import EgressPolicy
from lup.sandbox.models import DockerDaemonInfo, RootfulDaemonError
from lup.types import EnvVars
from lup.sandbox.process import decode_output, process_is_alive, process_start_token
from lup.sandbox.repl import REPL_SERVER_SCRIPT, ReplSession
from lup.sandbox.translation import MountTopology

logger = logging.getLogger(__name__)

PACKAGE_LIST = TypeAdapter(list[str])
"""How an install cell's package list crosses the journal and comes back.

A journal cell carries one string, and the packages an install names are a
list — so they cross as JSON with a parser on the far side, rather than
joined on spaces and split back apart on the assumption no name holds one.
"""


def connected_docker_client() -> docker.DockerClient:
    """Connect to the Docker daemon, naming what to check when it is absent.

    A stopped daemon, a socket this user cannot open, and a sandbox that
    denies that socket all arrive as one opaque connection error. The sandbox
    is reached through documented tooling, so the caller is usually reading a
    traceback for a machine they did not know was part of the story.
    """
    try:
        return docker.from_env()
    except DockerException as exc:
        raise DockerUnreachableError(
            "Cannot reach the Docker daemon. Check that it is running, that "
            "this user may open its socket, and that no sandbox is denying "
            f"that socket: {exc}"
        ) from exc


def file_cell(container_path: str) -> str:
    """A cell that runs a file already inside the container.

    The path is embedded as a Python literal rather than pasted, so a name
    carrying a quote stays one argument instead of becoming syntax. The
    compiled program keeps the file's own name, so a traceback points at the
    file the caller named instead of at ``<cell>``.

    ``globals()`` is the session namespace — the REPL executes each cell with
    it — so a file defines its names into the same session an inline cell
    would, and a later cell can use them.
    """
    return (
        f"exec(compile(open({container_path!r}, encoding='utf-8').read(), "
        f"{container_path!r}, 'exec'), globals())"
    )


class Sandbox:
    """Docker-based Python sandbox for isolated code execution.

    Each session gets a unique container and volume, so concurrent sessions
    cannot interfere with each other.

    Two paths are mounted (see :meth:`mount_topology`): ``/workspace`` is
    the persistent working directory and process cwd, backed by a private
    Docker volume that is *not* visible on the host; ``/shared`` is a bind
    of ``shared_dir`` for exchanging files with the host. Relative paths in
    executed code resolve under ``/workspace``, so host exchange must use
    the absolute ``/shared`` path.

    Args:
        session_id: Unique identifier for this session (used in container/volume names).
        shared_dir: Host directory bound for host-sandbox file exchange.
        shared_path: Container path to bind it at, defaulting to
            :attr:`DEFAULT_SHARED_PATH`. Passing the host directory's own path
            makes one spelling true on both sides.
        docker_image: Docker image to use for the sandbox.
        network_mode: Network access level ("bridge" or "none").
        memory_limit_bytes: Container memory ceiling, defaulting to
            :attr:`DEFAULT_MEMORY_LIMIT_BYTES`. Crossing it has the kernel
            kill the container outright, which reaches a caller as a dropped
            connection rather than as a ``MemoryError``, so size it for the
            workload rather than leaving it to be discovered.
        timeout_seconds: Default timeout for code execution.
        pre_install: Packages to pre-install on start. Pass ``None`` to skip.
        source_roots: Host import roots to bind read-only and put on
            ``PYTHONPATH``. Name the source directories themselves, never a
            repository root: a checkout carries its `.env` files too, and the
            default network mode would carry them back out.
    """

    DEFAULT_DOCKER_IMAGE = "ghcr.io/astral-sh/uv:python3.14-bookworm-slim"

    DEFAULT_EGRESS_PROXY_IMAGE = "ubuntu/squid:6.6-24.04_edge"
    """The proxy image `filtered` mode bridges the sandbox's network through."""

    DEFAULT_MEMORY_LIMIT_BYTES = 1024**3
    """What a sandbox gets when its caller names no ceiling, in bytes.

    Sized for the default workload — a scratch REPL running glue an agent
    wrote a moment ago — and deliberately small, because the ceiling's job is
    to keep a runaway cell from taking the host down with it. A caller whose
    container does real computational work is the one that knows how much it
    needs, which is why this is a default rather than the only value: a
    workload sized against the wrong number pays for it in shrunken runs
    rather than in an error that says what went wrong.
    """

    SOURCE_ROOT = "/sources"
    """Where read-only host source trees are mounted, one directory each."""

    REPLAY_DETAIL_CHARS = 200
    """How much of a replayed cell's output is kept to explain a divergence.

    Enough to carry the exception line that says why a cell went the other
    way, and bounded because a report covers every cell in the journal at
    once — a full stderr each would push the finding off the end of it.
    """

    DEFAULT_SHARED_PATH = "/shared"
    """Where the host exchange directory is mounted, absent a caller's choice.

    Pass ``shared_path=str(shared_dir)`` to mount it at the path the host
    already calls it. The two sides then agree on one spelling, and an agent
    holding tools on both cannot pick the wrong one — there is only one.
    """

    def __init__(
        self,
        *,
        session_id: str,
        shared_dir: str | Path,
        shared_path: str | None = None,
        docker_image: str = DEFAULT_DOCKER_IMAGE,
        network_mode: NetworkMode = "bridge",
        memory_limit_bytes: int = DEFAULT_MEMORY_LIMIT_BYTES,
        require_rootless: bool = False,
        durable: bool = False,
        egress: EgressPolicy | None = None,
        egress_proxy_image: str = DEFAULT_EGRESS_PROXY_IMAGE,
        timeout_seconds: int = 30,
        pre_install: Sequence[str] | None = DEFAULT_PRE_INSTALL,
        source_roots: Mapping[str, Path] | None = None,
        read_only_mounts: Mapping[Path, str] | None = None,
        rw_mounts: Mapping[Path, str] | None = None,
    ) -> None:
        suffix = session_id.replace("/", "-")  # lup: ignore[string-replace] — slug
        self.container_name = f"lup-sandbox-{suffix}"
        self.docker_image = docker_image
        self.volume_name = f"lup-sandbox-ws-{suffix}"
        self.shared_dir = Path(shared_dir).resolve()
        self.shared_path = shared_path or self.DEFAULT_SHARED_PATH
        self.network_mode = network_mode
        self.memory_limit_bytes = memory_limit_bytes
        self.require_rootless = require_rootless
        self.durable = durable
        self.egress = egress or EgressPolicy()
        self.egress_proxy_image = egress_proxy_image
        self.network_name = f"lup-sandbox-net-{suffix}"
        self.proxy_name = f"lup-sandbox-egress-{suffix}"
        self.timeout_seconds = timeout_seconds
        self.pre_install = list(pre_install) if pre_install is not None else None
        self.source_roots = {
            name: Path(root).resolve() for name, root in (source_roots or {}).items()
        }
        self.read_only_mounts = {
            Path(host).resolve(): path
            for host, path in (read_only_mounts or {}).items()
        }
        self.rw_mounts = {
            Path(host).resolve(): path for host, path in (rw_mounts or {}).items()
        }
        self.active_container: Container | None = None
        self.docker_client: docker.DockerClient | None = None
        self.repl: ReplSession | None = None
        self.filtered_network: Network | None = None
        self.egress_proxy: Container | None = None
        self.proxy_config_path = self.shared_dir.parent / f".{self.proxy_name}.conf"
        self.journal = JournalStore(
            self.shared_dir / "sandbox-journal.json",
            session_id,
            determinism_claimed=False,
        )
        """This sandbox's execution record, claiming nothing about reproducing it.

        The container has network access and installs packages, so a replay
        may legitimately differ — which is a reason to state the absent claim
        rather than a reason to keep no account of what was run.
        """

    @property
    def container(self) -> Container:
        """Get the active container (raises if not initialized)."""
        if self.active_container is None:
            raise SandboxNotInitializedError(
                "Sandbox not initialized. Use 'with Sandbox() as sandbox:' first."
            )
        return self.active_container

    def mount_topology(self) -> list[Mount]:
        """The container's filesystem layout: source, mode, and purpose.

        One source of truth for the Docker ``volumes`` mapping
        (:meth:`start_container`) and the code-execution tool description
        (:meth:`create_tools`), so the paths the agent is told about are
        exactly the paths that exist. Derived from names set in
        ``__init__``, so it is valid before the container starts.
        """
        return [
            Mount(
                container_path="/workspace",
                source=self.volume_name,
                kind="volume",
                mode="rw",
                purpose=(
                    "Persistent working directory and process cwd; survives "
                    "across calls but is not visible on the host."
                ),
            ),
            *self.shared_mounts(),
            *[
                Mount(
                    container_path=f"{self.SOURCE_ROOT}/{name}",
                    source=str(root),
                    kind="bind",
                    mode="ro",
                    purpose=(
                        f"Read-only import root from the host directory {root}; "
                        "on PYTHONPATH, so this project's code imports here."
                    ),
                )
                for name, root in sorted(self.source_roots.items())
            ],
            *self.declared_mounts(),
        ]

    def shared_mounts(self) -> list[Mount]:
        """The exchange directory, at every path that names it.

        A caller who moved the exchange onto its host path keeps the default
        path alongside it. Paths are written in the code the sandbox runs as
        well as in tool arguments, and code carrying the default spelling
        reaches no boundary where anything could correct it — so the cheaper
        answer is for both spellings to be true.
        """
        primary = Mount(
            container_path=self.shared_path,
            source=str(self.shared_dir),
            kind="bind",
            mode="rw",
            purpose=(
                f"File exchange with the host directory {self.shared_dir}; "
                "read host inputs and write host outputs here."
            ),
        )
        if self.shared_path == self.DEFAULT_SHARED_PATH:
            return [primary]
        return [
            primary,
            Mount(
                container_path=self.DEFAULT_SHARED_PATH,
                source=str(self.shared_dir),
                kind="bind",
                mode="rw",
                purpose=f"The same directory as {self.shared_path}.",
            ),
        ]

    def docker_mounts(self) -> list[DockerMount]:
        """The topology as Docker's own mount specification.

        A list rather than the ``volumes`` mapping Docker also accepts: that
        mapping is keyed by host path, so one directory mounted at two
        container paths loses one of them, and silently — which is exactly
        what a caller asks for when they want a path to mean the same thing
        on both sides.
        """
        return [
            DockerMount(
                target=mount.container_path,
                source=mount.source,
                type=mount.kind,
                read_only=mount.mode == "ro",
            )
            for mount in self.mount_topology()
        ]

    def topology(self) -> MountTopology:
        """The mount table, asked what a path is called on the other side."""
        return MountTopology(mounts=self.mount_topology())

    def proxy_log(self) -> list[str]:
        """What the egress proxy wrote, or nothing when no proxy is running.

        Absent in every mode but ``filtered``, where there is no proxy to have
        refused anything -- so nothing is the correct answer rather than a
        failure, and a caller attributing a network error gets "not the
        boundary" instead of an exception about a container that was never
        supposed to exist.
        """
        if self.egress_proxy is None:
            return []
        try:
            written = self.egress_proxy.logs(stdout=True, stderr=True)
        except (APIError, DockerException, NotFound):
            logger.warning("Egress proxy log unavailable for attribution")
            return []
        return decode_output(written).splitlines()

    def attribute(self, failure: str) -> Attribution:
        """Say whether this boundary caused a failure, or decline to say.

        The mount table and the proxy log are asked in that order because the
        filesystem answer is the more specific: a refused write names a path
        this topology made unwritable, where a proxy denial is about the
        session rather than about this command. Neither is guessed at -- a
        failure matching nothing comes back unattributed, which is a real
        answer and the one that stops an agent hunting a wall that was not
        involved.
        """
        on_disk = attribute_filesystem(
            failure, self.topology(), directory=self.shared_path
        )
        if on_disk.explains():
            return on_disk
        refused = attribute_egress(self.proxy_log())
        return refused[-1] if refused else on_disk

    def explained(self, ran: ExecuteCodeResult) -> ExecuteCodeResult:
        """One cell's outcome, with the boundary's own account appended to it.

        Only ever adds, and only to a failure the boundary can be shown to
        have caused. A cell that failed on its own merits comes back exactly
        as it was, because a note saying "this was not confinement" on every
        ordinary traceback is noise that would make the real one invisible.

        Appended to stderr rather than replacing it: the original message is
        what the agent will search for, and the attribution is what tells it
        not to. Both are wanted, in that order.
        """
        if ran.exit_code == 0:
            return ran
        found = self.attribute(f"{ran.stderr}\n{ran.stdout}")
        if not found.explains():
            return ran
        return ran.model_copy(
            update={"stderr": f"{ran.stderr}\n\n[boundary] {found.sentence()}"}
        )

    def declared_mounts(self) -> list[Mount]:
        """Host directories the caller placed at container paths of its choosing.

        Distinct from ``source_roots``, which are import roots this class puts
        under one directory it names. These go where the caller says, because
        what asks for them is a prompt that already names the path — mounting
        the same tree somewhere else would leave every such instruction wrong.

        Ordered parent before child by container path, across both modes
        together rather than read-only group then read-write group. A mount
        engine applies these in sequence and a later entry covers an earlier
        one it sits over, so grouping by mode can express only one nesting:
        a read-write hole punched in a read-only base. The inverse — a
        read-only hole inside a read-write base, which is how a shared git
        directory keeps one subdirectory unwritable — comes out of it with the
        read-write parent applied last, silently shadowing the hole into
        writability. Sorting on ``Path.parts`` puts every parent ahead of
        everything beneath it, so each entry keeps its own mode whichever
        way the nesting runs.
        """

        def placed(mounts: Mapping[Path, str], mode: MountMode) -> list[Mount]:
            return [
                Mount(
                    container_path=path,
                    source=str(host),
                    kind="bind",
                    mode=mode,
                    purpose=f"Host directory {host}, mounted {mode} by this session.",
                )
                for host, path in sorted(mounts.items())
            ]

        return sorted(
            [*placed(self.read_only_mounts, "ro"), *placed(self.rw_mounts, "rw")],
            key=lambda mount: Path(mount.container_path).parts,
        )

    def source_path(self) -> str:
        """The container-side ``PYTHONPATH`` for every mounted source root.

        Import roots are mounted rather than installed, and only the roots a
        caller names: a project's checkout also holds its `.env` files, and
        this sandbox reaches the network by default, so the repository root
        is a path out rather than a convenience.
        """
        return ":".join(
            f"{self.SOURCE_ROOT}/{name}" for name in sorted(self.source_roots)
        )

    @property
    def is_active(self) -> bool:
        """Check if the sandbox container is currently running."""
        return self.active_container is not None

    SANDBOX_LABEL = "lup.sandbox"
    CREATED_AT_LABEL = "lup.sandbox.created_at"
    VOLUME_LABEL = "lup.sandbox.volume"
    OWNER_PID_LABEL = "lup.sandbox.owner_pid"
    OWNER_START_LABEL = "lup.sandbox.owner_start"
    DURABLE_LABEL = "lup.sandbox.durable"
    STALE_AGE_HOURS = 24.0

    def verify_rootless_daemon(self) -> None:
        """Refuse a rootful daemon when the caller asked for the boundary.

        A container escape against a rootful daemon lands as root on the host;
        against a rootless one it lands as the unprivileged user the daemon
        runs as. For generated code that difference is the whole containment
        story, so a caller running it can insist on the boundary being there
        rather than assuming it.

        Off by default, because a standard Docker install is rootful and
        turning this on for everyone would refuse to start a sandbox that
        works today.
        """
        if not self.require_rootless:
            return
        if self.docker_client is None:
            raise SandboxNotInitializedError("Docker client not created")
        info = DockerDaemonInfo.model_validate(self.docker_client.info())
        if not any(
            option == "rootless" or option.endswith("=rootless")
            for option in info.security_options
        ):
            raise RootfulDaemonError(
                "This sandbox requires a rootless Docker daemon. Point "
                "DOCKER_HOST at the rootless user socket, or construct the "
                "sandbox with require_rootless=False."
            )

    def proxy_environment(self) -> EnvVars:
        """Proxy variables pointing a filtered container at its only way out.

        Both spellings are set because the tools a sandbox runs disagree about
        which they read, and ``NO_PROXY`` is emptied so nothing arrives from
        the host claiming an exemption.
        """
        proxy = f"http://egress:{self.egress.listen_port}"
        return {
            "HTTP_PROXY": proxy,
            "HTTPS_PROXY": proxy,
            "ALL_PROXY": proxy,
            "http_proxy": proxy,
            "https_proxy": proxy,
            "all_proxy": proxy,
            "NO_PROXY": "",
            "no_proxy": "",
        }

    def infrastructure_labels(self) -> dict[str, str]:  # lup: ignore[dict-str-payload]
        """Labels marking one session's containers and networks as ours.

        An ordinary sandbox names the process that created it, so the orphan
        sweep can reap it the moment that process dies. A durable one names
        no owner, because the work it holds is meant to outlive the process
        that queued it — it is still reaped once it ages past
        ``STALE_AGE_HOURS``, so a forgotten job cannot leak forever.
        """
        owned = {
            self.OWNER_PID_LABEL: str(os.getpid()),
            self.OWNER_START_LABEL: process_start_token(os.getpid()) or "",
        }
        return {
            self.SANDBOX_LABEL: "1",
            self.CREATED_AT_LABEL: str(time.time()),
        } | ({self.DURABLE_LABEL: "1"} if self.durable else owned)

    def container_environment(self, filtered: bool) -> EnvVars:
        """The environment the sandbox container starts with."""
        source = {"PYTHONPATH": self.source_path()} if self.source_roots else {}
        return source | (self.proxy_environment() if filtered else {})

    def start_filtered_egress(self) -> None:
        """Create the internal network and the one proxy bridged out of it.

        The network is ``internal``, which is what the mode rests on: Docker
        gives it no gateway, so the sandbox has no route out at all rather
        than a route it is asked not to take. The proxy is the only member of
        both networks, and is stripped of every capability it does not need
        to bind its port.
        """
        if self.docker_client is None:
            raise SandboxNotInitializedError("Docker client not created")
        self.proxy_config_path.parent.mkdir(parents=True, exist_ok=True)
        self.proxy_config_path.write_text(self.egress.render(), encoding="utf-8")
        labels = self.infrastructure_labels()
        self.filtered_network = self.docker_client.networks.create(
            self.network_name, internal=True, labels=labels
        )
        self.egress_proxy = self.docker_client.containers.run(
            self.egress_proxy_image,
            name=self.proxy_name,
            detach=True,
            volumes={
                str(self.proxy_config_path): {
                    "bind": "/etc/squid/squid.conf",
                    "mode": "ro",
                }
            },
            network_mode="bridge",
            labels=labels,
            mem_limit="256m",
            pids_limit=128,
            cap_drop=["ALL"],
            cap_add=["SETUID", "SETGID", "CHOWN", "DAC_OVERRIDE"],
            security_opt=["no-new-privileges:true"],
        )
        self.filtered_network.connect(self.egress_proxy, aliases=["egress"])

    def stop_filtered_egress(self) -> None:
        """Remove the proxy, its network, and the rendered configuration.

        Every step reports what it could not clean up rather than raising:
        teardown runs when a session is already ending, and a cleanup that
        aborts leaves more behind than the one thing it failed to remove.
        """
        if self.egress_proxy is not None:
            try:
                self.egress_proxy.stop(timeout=5)
                self.egress_proxy.remove()
            except (APIError, DockerException) as error:
                logger.warning("Failed to clean up egress proxy: %s", error)
            finally:
                self.egress_proxy = None
        if self.filtered_network is not None:
            try:
                self.filtered_network.remove()
            except (APIError, DockerException) as error:
                logger.warning("Failed to clean up sandbox network: %s", error)
            finally:
                self.filtered_network = None
        try:
            self.proxy_config_path.unlink(missing_ok=True)
        except OSError as error:
            logger.warning("Failed to remove egress configuration: %s", error)

    def remove_stale_container(self) -> None:
        """Remove a pre-existing container with the same name, if it is stale.

        Stale means its owner is gone. A name that two live sessions both
        derived is not staleness: the container answering to it is somebody's
        working sandbox, and forcing it out takes whatever is running inside
        away from a process still talking to it.

        Owner liveness decides, off the same labels
        :meth:`sweep_orphaned_containers` reads — that sweep skips this name
        because this method owns it, so leaving the judgement out of here left
        the one collision that matters unjudged.
        """
        if self.docker_client is None:
            return
        try:
            old = self.docker_client.containers.get(self.container_name)
            if not self.container_is_orphaned(old.labels or {}):
                raise SandboxNameInUseError(
                    f"Sandbox container {self.container_name!r} belongs to a "
                    "session that is still running. Two sessions naming "
                    "themselves the same thing is what puts them both here; "
                    "give this one its own session id and it gets its own "
                    "container."
                )
            logger.warning("Removing stale container: %s", self.container_name)
            old.remove(force=True)
        except NotFound:
            pass
        # A crashed run leaves its proxy holding the network, so the proxy has
        # to go before the network it is attached to can be recreated.
        try:
            proxy = self.docker_client.containers.get(self.proxy_name)
            logger.warning("Removing stale egress proxy: %s", self.proxy_name)
            proxy.remove(force=True)
        except NotFound:
            pass
        try:
            network = self.docker_client.networks.get(self.network_name)
            logger.warning("Removing stale sandbox network: %s", self.network_name)
            network.remove()
        except NotFound:
            pass

    def container_is_orphaned(
        self,
        labels: dict[str, str],  # lup: ignore[dict-str-payload] — open label map
    ) -> bool:
        """Decide whether a labelled container belongs to a dead owner.

        Liveness is owner-driven: a container whose creating process is
        still running is kept even past ``STALE_AGE_HOURS`` — this
        library's persistent/relay agents are meant to run indefinitely.
        Only when the owner-pid label is missing (older containers, or
        ones created elsewhere) do we fall back to the age heuristic.
        """
        owner_pid_raw = labels.get(self.OWNER_PID_LABEL)
        if owner_pid_raw is not None:
            try:
                owner_pid = int(owner_pid_raw)
            except ValueError:
                return True
            started = labels.get(self.OWNER_START_LABEL)
            return not process_is_alive(owner_pid, started)
        try:
            created_raw = labels.get(self.CREATED_AT_LABEL)
            created_at = float(created_raw or "0")
        except ValueError:
            created_at = 0.0
        return created_at <= time.time() - self.STALE_AGE_HOURS * 3600

    def sweep_orphaned_containers(self) -> None:
        """Remove lup sandbox containers whose creating process is gone.

        A SIGKILLed owner leaves its uniquely-named container running
        forever (``remove_stale_container`` only matches this session's
        name). Every lup sandbox is labelled with its owner's PID and
        start token; a container is swept (with its volume) only when
        that owner process is no longer alive, so a long-lived persistent
        session is never destroyed out from under itself.
        """
        if self.docker_client is None:
            return
        try:
            candidates = self.docker_client.containers.list(
                all=True, filters={"label": self.SANDBOX_LABEL}
            )
        except (APIError, DockerException) as e:
            logger.warning("Orphan sweep failed to list containers: %s", e)
            return
        for container in candidates:
            if container.name == self.container_name:
                continue
            labels = container.labels or {}
            if not self.container_is_orphaned(labels):
                continue
            logger.warning("Removing orphaned sandbox container: %s", container.name)
            try:
                container.remove(force=True)
                volume_name = labels.get(self.VOLUME_LABEL)
                if volume_name:
                    volume = self.docker_client.volumes.get(volume_name)
                    volume.remove()
            except (NotFound, APIError, DockerException) as e:
                logger.warning("Orphan cleanup of %s failed: %s", container.name, e)

    def destroy_container(self) -> None:
        """Stop and remove the current container and its session volume."""
        if self.active_container is None:
            return
        try:
            self.active_container.stop(timeout=5)
            self.active_container.remove()
        except (APIError, DockerException) as e:
            logger.warning("Failed to cleanup container: %s", e)
        finally:
            self.active_container = None

        if self.docker_client is not None:
            try:
                vol = self.docker_client.volumes.get(self.volume_name)
                vol.remove()
            except (NotFound, APIError):
                pass

        self.stop_filtered_egress()

    def write_repl_script(self) -> None:
        """Write the REPL server script into the container via tar archive."""
        script_bytes = REPL_SERVER_SCRIPT.encode("utf-8")
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tar:
            info = tarfile.TarInfo(name=".repl_server.py")
            info.size = len(script_bytes)
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(script_bytes))
        buf.seek(0)
        self.container.put_archive("/workspace", buf)

    def run_pre_install(self) -> None:
        """Pre-install packages for faster agent execution."""
        if not self.pre_install:
            return
        logger.info("Pre-installing packages: %s", self.pre_install)
        cmd = ["uv", "pip", "install", "--system", *self.pre_install]
        result = self.container.exec_run(cmd, demux=False)
        if result.exit_code != 0:
            logger.warning(
                "Package pre-install failed (exit %d): %s",
                result.exit_code,
                decode_output(result.output),
            )
        else:
            logger.info("Pre-installed packages successfully")

    def start(self) -> None:
        """Start the sandbox container.

        Creates a new Docker container for code execution. Removes any
        stale container with the same name first.
        """
        self.docker_client = connected_docker_client()
        try:
            self.start_container()
        except (APIError, DockerException, OSError, RuntimeError):
            self.stop()
            raise

    def start_container(self) -> None:
        """Create the container and bring up the REPL (assumes a client)."""
        if self.docker_client is None:
            raise SandboxNotInitializedError("Docker client not created")
        self.verify_rootless_daemon()
        self.remove_stale_container()
        self.sweep_orphaned_containers()

        self.shared_dir.mkdir(parents=True, exist_ok=True)

        filtered = self.network_mode == "filtered"
        if filtered:
            self.start_filtered_egress()

        logger.info(
            "Creating sandbox container: %s (network=%s)",
            self.container_name,
            self.network_mode,
        )
        logger.info(
            "Mounting shared directory: %s -> %s", self.shared_dir, self.shared_path
        )
        self.active_container = self.docker_client.containers.run(
            self.docker_image,
            name=self.container_name,
            command="sleep infinity",
            detach=True,
            mounts=self.docker_mounts(),
            working_dir="/workspace",
            mem_limit=self.memory_limit_bytes,
            network_mode=self.network_name if filtered else self.network_mode,
            environment=self.container_environment(filtered),
            labels=self.infrastructure_labels() | {self.VOLUME_LABEL: self.volume_name},
        )

        if self.network_mode != "none":
            self.run_pre_install()

        self.write_repl_script()
        if self.docker_client is None or self.active_container is None:
            raise SandboxNotInitializedError(
                "Docker client or container not available after start"
            )
        self.repl = ReplSession(self.docker_client, self.active_container, {})
        self.repl.start()

    def stop(self) -> None:
        """Stop and remove the sandbox container."""
        if self.repl is not None:
            self.repl.stop()
            self.repl = None
        logger.info("Destroying sandbox container")
        self.destroy_container()
        if self.docker_client is not None:
            self.docker_client.close()
            self.docker_client = None

    def ensure_started(self) -> None:
        """Start the sandbox if it isn't running (lazy initialization).

        Tool handlers call this so a sandbox served by a tool subprocess
        only pays Docker startup when the agent actually executes code.
        """
        if self.repl is None:
            self.start()

    def restart_repl(self) -> None:
        """Restart the REPL, clearing in-memory state.

        Container, filesystem, and installed packages are preserved.
        Only the Python namespace is reset.
        """
        if self.repl is not None:
            self.repl.stop()
        if self.docker_client is None or self.active_container is None:
            raise SandboxNotInitializedError("Sandbox not initialized")
        self.repl = ReplSession(self.docker_client, self.active_container, {})
        self.repl.start()
        logger.info("REPL restarted (state cleared)")

    def rebuild_container(self) -> None:
        """Replace a container that is gone, keeping the workspace volume.

        Deliberately not :meth:`stop` first: ``stop`` runs
        ``destroy_container``, which removes the session volume too.
        ``start_container`` drops the old container by name and mounts the
        same named volume, so files under ``/workspace`` outlive a rebuild
        even though the Python namespace and installed packages do not.
        """
        self.repl = None
        self.active_container = None
        self.start()

    def __enter__(self) -> Self:
        """Enter context manager, starting the sandbox."""
        started = False
        try:
            self.start()
            started = True
        finally:
            if not started:
                self.stop()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        """Exit context manager, stopping the sandbox."""
        self.stop()

    # --- Code execution methods ---

    def run_code(
        self, code: str, timeout_seconds: int | None = None
    ) -> ExecuteCodeResult:
        """Execute Python code in the sandbox's persistent REPL.

        Variables, imports, and data persist between calls within the same
        session. A crashed REPL is recovered before this returns — by
        re-exec, or by rebuilding the container — and the returned result
        says which state that cost (see :meth:`recover_from_crash`).

        Args:
            code: Python code to execute.
            timeout_seconds: Max execution time in seconds. If None, uses
                the sandbox's default timeout. Zero or negative means no
                timeout at all — no in-sandbox alarm and no host deadline,
                so long-running code completes without losing REPL state.

        Returns:
            Result containing exit code, stdout, stderr, and duration.
        """
        if self.repl is None:
            raise SandboxNotInitializedError("REPL not initialized")
        if timeout_seconds is None:
            timeout_seconds = self.timeout_seconds

        try:
            return self.repl.execute(code, timeout_seconds)
        except ReplCrashedError as crash:
            logger.warning("REPL crashed, recovering")
            return self.recover_from_crash(self.repl, crash)

    def container_spelling(self, path: str) -> str:
        """The container's name for a file the caller named from either side.

        An agent holding tools on both sides has whichever spelling it last
        saw and cannot tell from the path which side that was. Translating
        rather than refusing is what makes the two sides one namespace to the
        caller. A path that crosses in neither direction is the genuine
        error, and carries the topology's own account of where to put it.
        """
        topology = self.topology()
        crossing = topology.to_container(path)
        if crossing.resolved is not None:
            return crossing.resolved
        if topology.contains(path):
            return path
        raise PathNotMountedError(crossing.explanation)

    def run_file(
        self, path: str, timeout_seconds: int | None = None
    ) -> ExecuteCodeResult:
        """Execute a file the container can already see, named from either side.

        The file is read inside the container, so this reaches exactly what is
        mounted and nothing else: a host path that was never mounted is
        refused rather than copied in, and the sandbox's isolation is the same
        whether a cell arrives as text or as a file.

        The cell runs for effect: names it defines persist into the session,
        but a trailing expression is not echoed the way an inline cell's is,
        because the file is executed rather than parsed here.
        """
        return self.run_code(file_cell(self.container_spelling(path)), timeout_seconds)

    def recover_from_crash(
        self, repl: ReplSession, crash: ReplCrashedError
    ) -> ExecuteCodeResult:
        """Bring the sandbox back up, reporting what the crash cost.

        Escalates in two steps: re-exec the REPL inside the existing
        container, and rebuild the container only if that fails. A broken
        exec socket usually means the container itself is gone, and
        re-execing into a container that no longer exists can never
        succeed.

        Recovery finishes inside the call that observed the crash, so its
        caller is always told what was lost. Leaving the sandbox
        uninitialized instead would push the rebuild onto the next call,
        which has no idea a crash happened and would report a wiped
        namespace as an ordinary success.
        """
        repl.stop()
        try:
            repl.start()
            lost = "Variables from previous cells have been lost."
        except (RuntimeError, DockerException, APIError, SocketError):
            logger.warning("REPL re-exec failed, rebuilding the container")
            try:
                self.rebuild_container()
            except (APIError, DockerException, OSError, RuntimeError) as e:
                logger.exception("Sandbox rebuild failed")
                self.repl = None
                raise SandboxNotInitializedError(
                    f"REPL crashed and the sandbox could not be rebuilt: {e}"
                ) from crash
            lost = "Variables and installed packages have been lost."
        return ExecuteCodeResult(
            exit_code=1,
            stderr=(
                f"REPL process crashed and was restarted. {lost} "
                "Please re-run any setup code."
            ),
        )

    def run_shell(self, command: str, workdir: str | None = None) -> ExecuteCodeResult:
        """Run one shell command in the container, outside the Python REPL.

        The REPL is a Python process and holds the session's state; a
        toolchain the image ships — a compiler, a converter — is a process of
        its own, and running it here leaves that state untouched. Through a
        shell, so a caller writes the pipeline and redirection it would type.
        A dead container is rebuilt and the command retried, as installing is.
        """
        argv = ["sh", "-c", command]
        started = time.time()
        try:
            result: ExecResult = self.container.exec_run(
                argv, workdir=workdir, demux=False
            )
        except NotFound:
            logger.warning("Container gone during shell command, rebuilding")
            self.rebuild_container()
            result = self.container.exec_run(argv, workdir=workdir, demux=False)
        return ExecuteCodeResult(
            exit_code=result.exit_code if result.exit_code is not None else -1,
            stdout=decode_output(result.output),
            duration_ms=round((time.time() - started) * 1000),
        )

    def run_install(self, packages: list[str]) -> InstallPackageResult:
        """Install Python packages using uv.

        Args:
            packages: List of package names to install.

        Returns:
            Result containing exit code, output, and package list.
        """
        cmd = ["uv", "pip", "install", "--system", *packages]
        try:
            result: ExecResult = self.container.exec_run(cmd, demux=False)
        except NotFound:
            # Same dead-container case run_code recovers from, reached
            # through the install path instead of the REPL socket.
            logger.warning("Container gone during install, rebuilding")
            self.rebuild_container()
            result = self.container.exec_run(cmd, demux=False)

        output_text = decode_output(result.output)

        return InstallPackageResult(
            exit_code=result.exit_code if result.exit_code is not None else -1,
            output=output_text,
            packages=packages,
        )

    # --- Journal replay ---

    def replay_cell(self, cell: JournalCell) -> CellOutcome:
        """Re-run one journaled cell, through the same call that first ran it.

        The kinds are this environment's own vocabulary, written by the tool
        handlers below and read only here — the journal itself stays generic,
        so an environment with a different set of calls records its own.
        """
        match cell.kind:
            case "install":
                installed = self.run_install(PACKAGE_LIST.validate_json(cell.source))
                return CellOutcome(
                    ok=installed.exit_code == 0,
                    detail=installed.output[: self.REPLAY_DETAIL_CHARS],
                )
            case "file":
                executed = self.run_file(cell.source)
            case _:
                executed = self.run_code(cell.source)
        return CellOutcome(
            ok=executed.exit_code == 0,
            detail=executed.stderr[: self.REPLAY_DETAIL_CHARS],
        )

    def replay_journal(self) -> ReplayReport:
        """Re-run every journaled cell in order and report what differed.

        Into the container as it stands, which is what makes this a replay of
        the session and not a fresh-machine check: the packages earlier cells
        installed are still there. A clean replay says the sequence still
        holds together, not that it would hold together anywhere else.
        """
        journal = self.journal.load()
        self.ensure_started()
        return journal.compare([self.replay_cell(cell) for cell in journal.cells])

    # --- MCP tool creation ---

    def create_tools(self, usage_notes: str = "") -> list[LupMcpTool]:
        """Create MCP tools bound to this sandbox instance.

        ``usage_notes`` is appended to the filesystem section of the
        execute-code description. What a caller mounts is a caller's decision,
        and only the caller knows what the files there are *for* — this class
        can say a path exists and is writable, not that the plan lives in it.

        Returns:
            List of MCP tools for code execution and package installation.
        """
        timeout_seconds = self.timeout_seconds
        filesystem_text = "\n".join(
            [
                *(
                    f"  {mount.container_path} ({mount.mode}) — {mount.purpose}"
                    for mount in self.mount_topology()
                ),
                *([f"\n{usage_notes}"] if usage_notes else []),
            ]
        )
        network_text = (
            "The container has network access. "
            if self.network_mode != "none"
            else "The container has no network access. "
        )

        @lup_tool(
            "Execute Python code in an isolated Docker container with persistent state. "
            "Variables, imports, and data persist between calls — no need to re-define them. "
            "A cell ending in an expression returns that value's repr as 'result' and "
            "binds it to '_', so you only need print() for intermediate output. "
            f"{network_text}Timeout: {timeout_seconds}s.\n\n"
            f"Filesystem (use absolute paths; the cwd is /workspace):\n{filesystem_text}\n\n"
            "Pass code= to run a cell inline, or file= to run a program that is "
            "already on disk — name it by its host path or its container path, "
            "whichever you have, and it is translated on arrival. A file runs "
            "for effect: state persists, but a trailing expression is not echoed.\n\n"
            "Examples:\n"
            "  execute_code(code='import numpy as np; data = [1,2,3]; print(np.mean(data))')\n"
            "  execute_code(code='# Monte Carlo simulation\\nimport numpy as np\\n"
            "returns = np.random.normal(0.0005, 0.015, (10000, 14))\\n"
            "paths = 100 * np.cumprod(1 + returns, axis=1)\\n"
            "print(np.percentile(paths[:,-1], [10,25,50,75,90]))')\n"
            "State persists: define variables in one call, use them in the next.",
            name="execute_code",
        )
        async def execute_code(inp: ExecuteCodeInput) -> ExecuteCodeResult:
            try:
                self.ensure_started()
                if inp.file is not None:
                    ran = self.run_file(inp.file)
                    kind, source = "file", inp.file
                else:
                    if inp.code is None:
                        raise ToolError("pass exactly one of code or file")
                    ran = self.run_code(inp.code)
                    kind, source = "code", inp.code
                self.journal.record(
                    JournalCell(kind=kind, source=source, ok=ran.exit_code == 0)
                )
                return self.explained(ran)
            except UnreadableJournalError as e:
                raise ToolError(str(e)) from e
            except PathNotMountedError as e:
                raise ToolError(str(e)) from e
            except SandboxNotInitializedError as e:
                logger.error("Sandbox not initialized: %s", e)
                raise ToolError(f"Sandbox error: {e}") from e
            except CodeExecutionTimeoutError as e:
                logger.warning("Code execution timed out")
                raise ToolError(str(e)) from e
            except (APIError, DockerException) as e:
                logger.exception("Docker execution failed")
                raise ToolError(f"Docker error: {e}") from e

        @lup_tool(
            "Install one or more Python packages from PyPI using uv. Packages persist "
            "in the container across executions.",
            name="install_package",
        )
        async def install_package(inp: InstallPackageInput) -> InstallPackageResult:
            try:
                self.ensure_started()
                installed = self.run_install(inp.packages)
                self.journal.record(
                    JournalCell(
                        kind="install",
                        source=PACKAGE_LIST.dump_json(inp.packages).decode("utf-8"),
                        ok=installed.exit_code == 0,
                    )
                )
                return installed
            except UnreadableJournalError as e:
                raise ToolError(str(e)) from e
            except SandboxNotInitializedError as e:
                logger.error("Sandbox not initialized: %s", e)
                raise ToolError(f"Sandbox error: {e}") from e
            except (APIError, DockerException) as e:
                logger.exception("Docker execution failed")
                raise ToolError(f"Docker error: {e}") from e

        @lup_tool(
            "Re-run every cell this sandbox has executed, in order, and report "
            "which ones came out differently. Use it before relying on a "
            "sandbox result for anything that matters. This sandbox claims no "
            "determinism — it has network access and installs packages — so a "
            "divergence is not a bug here, it is the finding: it says the "
            "result depended on something outside the recorded code. The "
            "replay runs in this container as it stands, with everything "
            "earlier cells installed still present, so a clean replay says the "
            "sequence still holds together and not that it would hold "
            "anywhere else. Returns the journal id, how many cells were "
            "replayed, each divergence, and a finding that reads the result "
            "against the claim this environment makes.",
            name="sandbox_replay",
        )
        async def sandbox_replay(inp: SandboxReplayInput) -> ReplayReport:
            try:
                return self.replay_journal()
            except UnreadableJournalError as e:
                raise ToolError(str(e)) from e
            except PathNotMountedError as e:
                raise ToolError(str(e)) from e
            except SandboxNotInitializedError as e:
                logger.error("Sandbox not initialized: %s", e)
                raise ToolError(f"Sandbox error: {e}") from e
            except CodeExecutionTimeoutError as e:
                logger.warning("Replayed cell timed out")
                raise ToolError(str(e)) from e
            except (APIError, DockerException) as e:
                logger.exception("Docker replay failed")
                raise ToolError(f"Docker error: {e}") from e

        return [execute_code, install_package, sandbox_replay]

    def create_mcp_server(
        self,
        name: str = "sandbox",
        version: str = "1.0.0",
    ) -> LupMcpServerConfig:
        """Create an MCP server with sandbox tools."""
        return create_mcp_server(
            name=name,
            version=version,
            tools=self.create_tools(),
        )


@contextmanager
def sandbox_cleanup(session_id: str, shared_dir: Path) -> Generator[None]:
    """Guarantee a session's sandbox container is removed on exit.

    For sessions whose sandbox lives in a tool subprocess (the
    subprocess-served-tool paths): the subprocess owner may kill it
    without a graceful exit, skipping its atexit cleanup. The parent
    wraps the session in this context so the container and volume are
    removed regardless of how the subprocess died.
    """
    try:
        yield
    finally:
        sandbox = Sandbox(session_id=session_id, shared_dir=shared_dir)
        # Connecting here can fail on the way out of a failed session, and
        # raising would replace whatever the session was already raising.
        try:
            client = connected_docker_client()
        except DockerUnreachableError as e:
            logger.warning("Skipping post-session sandbox cleanup: %s", e)
        else:
            sandbox.docker_client = client
            try:
                sandbox.remove_stale_container()
                client.volumes.get(sandbox.volume_name).remove()
            except NotFound:
                pass
            except (APIError, DockerException) as e:
                logger.warning("Post-session sandbox cleanup failed: %s", e)
            finally:
                client.close()
