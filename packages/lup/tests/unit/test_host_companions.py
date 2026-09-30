"""Host companions: held around every session a declaration opens, and shared by lease.

A companion is held around a session however the declaration opens it and
hands the session what reaches it. One shared by several sessions is one
process on the host: started by the first session to hold it, joined by the
rest, replaced where it stopped serving or its declaration changed, and
stopped once the last lease goes — a lease whose launcher died counting as
gone.
"""

import itertools
import json
import os
import signal
import socket
import sys
import time
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path
from unittest.mock import Mock

import pytest

import lup.launch.session as launch_session
import lup.providers.claude.launch as claude_launch
import lup.providers.codex.launch as codex_launch
from lup.coordination.identity import LaunchedMember
from lup.harness.notice import Notice
from lup.launch.companions import (
    CompanionLaunch,
    CompanionPlace,
    CompanionProcess,
    CompanionScope,
    CompanionSlot,
    Contribution,
    GivenPort,
    HostCompanion,
    Joined,
    Lease,
    LiveProcess,
    ReachedPort,
    Running,
    SharedProcess,
    given_ports,
    held_companions,
)
from lup.launch.declaration import InnerSandbox, Member, Mount, NoSandbox
from lup.launch.refusal import LaunchRefused
from lup.providers.claude import Claude
from lup.providers.codex import Codex
from lup.providers.codex.home import CodexHomeSelection
from lup.sessions.recursion import MAX_RECURSIVE_AGENT_ENV

MEMBER = LaunchedMember(member_id="member-1", cli_name="work")


class Seen:
    """Where a companion writes down each hold, kept as the caller's own object."""

    def __init__(self) -> None:
        self.events: list[str] = []


class Fixed(HostCompanion, frozen=True, arbitrary_types_allowed=True):
    """A companion handing every session one fixed contribution, recording each hold."""

    given: Contribution = Contribution()
    seen: Seen = Seen()

    @contextmanager
    def held(self, launch: CompanionLaunch) -> Iterator[Contribution]:
        self.seen.events.append(f"{launch.runtime}:held")
        try:
            yield self.given
        finally:
            self.seen.events.append(f"{launch.runtime}:released")


def free_port() -> int:
    """A port nothing on this host's loopback listens on right now."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class Served(SharedProcess, frozen=True):
    """A companion that is Python's own HTTP server, serving a directory of the checkout."""

    def process(self, place: CompanionPlace, root: Path) -> CompanionProcess:
        return CompanionProcess(
            argv=[
                sys.executable,
                "-m",
                "http.server",
                str(place.ports["web"]),
                "--bind",
                "127.0.0.1",
            ],
            cwd=root,
        )

    def contribution(self, place: CompanionPlace, root: Path) -> Contribution:
        del root
        return Contribution(
            environment={"SERVED_URL": f"http://127.0.0.1:{place.ports['web']}"},
            ports=dict(place.ports),
        )


@pytest.fixture
def state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """lup's own state, this test's alone."""
    home = tmp_path / "state"
    monkeypatch.setenv("XDG_STATE_HOME", str(home))
    return home


def launch_at(root: Path) -> CompanionLaunch:
    return CompanionLaunch(root=root, runtime="claude", environment={})


def test_two_companions_exporting_one_variable_are_refused_naming_both() -> None:
    with pytest.raises(LaunchRefused, match="first, second all export SHARED"):
        Joined.of(
            {
                "first": Contribution(environment={"SHARED": "1"}),
                "second": Contribution(environment={"SHARED": "2"}),
            }
        )


def test_joined_contributions_name_each_port_by_its_companion() -> None:
    joined = Joined.of(
        {
            "inbox": Contribution(ports={"web": 8400}, mounts=[Mount(path=Path("/a"))]),
            "preview": Contribution(ports={"web": 5173}),
        }
    )

    assert joined.ports == [
        ReachedPort(companion="inbox", name="web", port=8400),
        ReachedPort(companion="preview", name="web", port=5173),
    ]
    assert joined.mounts == [Mount(path=Path("/a"))]


def test_every_companion_is_held_in_order_and_let_go_in_reverse() -> None:
    seen: list[str] = []

    @contextmanager
    def noted(name: str) -> Iterator[Contribution]:
        seen.append(f"{name}:held")
        try:
            yield Contribution()
        finally:
            seen.append(f"{name}:released")

    class Noted(HostCompanion, frozen=True):
        def held(self, launch: CompanionLaunch) -> AbstractContextManager[Contribution]:
            del launch
            return noted(self.name)

    with held_companions([Noted(name="a"), Noted(name="b")], launch_at(Path("/"))):
        seen.append("session")

    assert seen == ["a:held", "b:held", "session", "b:released", "a:released"]


def test_two_companions_under_one_name_are_refused_at_declaration() -> None:
    with pytest.raises(ValueError, match="named apart"):
        Claude(companions=[Fixed(name="same"), Fixed(name="same")])
    with pytest.raises(ValueError, match="named apart"):
        Codex(companions=[Fixed(name="same"), Fixed(name="same")])


def test_a_port_is_the_preferred_one_where_it_is_free() -> None:
    assert given_ports({"web": 8400}, [], [], free=lambda _port: True) == [
        GivenPort(name="web", preferred=8400, port=8400)
    ]


def test_a_port_taken_or_kept_elsewhere_moves_to_the_next_free_one() -> None:
    given = given_ports(
        {"web": 8400, "api": 8400},
        [],
        [8401],
        free=lambda port: port != 8400,
    )

    assert [port.port for port in given] == [8402, 8403]


def test_a_port_given_before_stands_while_it_is_still_free() -> None:
    kept = [GivenPort(name="web", preferred=8400, port=8411)]

    assert given_ports({"web": 8400}, kept, [], free=lambda _port: True) == kept


def test_a_shared_process_is_started_once_and_stopped_after_its_last_lease(
    state: Path, tmp_path: Path
) -> None:
    served = Served(name="served", ports={"web": free_port()})
    launch = launch_at(tmp_path)

    with served.held(launch) as first:
        running = served.slot(tmp_path).read().running
        assert running is not None
        with served.held(launch) as second:
            assert second == first
            assert served.slot(tmp_path).read().running == running
        assert running.process.running()
        assert len(served.slot(tmp_path).read().leases) == 1

    assert not running.process.running()
    assert served.slot(tmp_path).read().running is None
    assert first.environment["SERVED_URL"].startswith("http://127.0.0.1:")


def test_a_changed_declaration_replaces_what_runs(state: Path, tmp_path: Path) -> None:
    port = free_port()
    before = Served(name="served", ports={"web": port})
    after = Served(name="served", ports={"web": port}, grace=1.0)

    with before.held(launch_at(tmp_path)):
        replaced = before.slot(tmp_path).read().running
        assert replaced is not None
        with after.held(launch_at(tmp_path)):
            running = after.slot(tmp_path).read().running
            assert running is not None
            assert running.process != replaced.process
            assert not replaced.process.running()


def test_a_companion_leaves_what_it_unsets_and_is_stopped_from_outside(
    state: Path, tmp_path: Path
) -> None:
    class Unmarked(Served, frozen=True):
        def process(self, place: CompanionPlace, root: Path) -> CompanionProcess:
            return super().process(place, root).model_copy(update={"unset": ["MARKED"]})

    served = Unmarked(name="served", ports={"web": free_port()})
    marked = CompanionLaunch(
        root=tmp_path, runtime="claude", environment={"MARKED": "the session's"}
    )

    with served.held(marked):
        standing = served.standing(tmp_path)
        assert standing.serving is not None and standing.leases == 1
        environ = Path(f"/proc/{standing.serving.pid}/environ").read_bytes()
        assert b"MARKED=" not in environ
        assert served.stopped(tmp_path)
        assert served.standing(tmp_path).serving is None
        assert not served.stopped(tmp_path)


def test_a_lease_whose_launcher_died_is_swept(state: Path, tmp_path: Path) -> None:
    served = Served(name="served", ports={"web": free_port()})
    slot = served.slot(tmp_path)

    with served.held(launch_at(tmp_path)):
        kept = slot.read()
        dead = Lease(id="gone", holder=LiveProcess(pid=2**22 + 7, started="0"))
        slot.write(kept.model_copy(update={"leases": [*kept.leases, dead]}))
        running = kept.running
        assert running is not None

    assert not running.process.running()
    assert slot.read().leases == []


def started_after(
    slot: CompanionSlot, before: Running, within: float = 30.0
) -> Running:
    """What runs once a process other than ``before``'s is recorded running."""
    for _ in range(int(within / 0.05)):
        running = slot.read().running
        if (
            running is not None
            and running.process != before.process
            and running.process.running()
        ):
            return running
        time.sleep(0.05)
    raise AssertionError(f"nothing replaced pid {before.process.pid} in {within}s")


def test_a_shared_process_killed_while_held_is_started_again_where_it_was(
    state: Path, tmp_path: Path
) -> None:
    """Its holder finds it gone, records how it ended, and starts it again on its port."""
    served = Served(
        name="served", ports={"web": free_port()}, backoff=(0.2,), watched_every=0.1
    )
    slot = served.slot(tmp_path)

    with served.held(launch_at(tmp_path)) as given:
        killed = slot.read().running
        assert killed is not None
        os.kill(killed.process.pid, signal.SIGKILL)
        again = started_after(slot, killed)
        kept = slot.read()

    exited = kept.exited
    assert kept.given() == given.ports
    assert exited is not None and exited.process == killed.process
    assert exited.status == -signal.SIGKILL and exited.restarted is not None
    assert exited.reason() == "it was ended by SIGKILL"
    assert kept.restarts == 1 and kept.retry is None and kept.failing == 1
    assert not again.process.running()
    assert "is gone while held (live leases: 1)" in slot.log().read_text()


CRASHING = """
import http.server, os, sys, threading, time
from pathlib import Path

with Path(sys.argv[2]).open("a", encoding="utf-8") as starts:
    starts.write(f"{time.time()}\\n")
threading.Timer(float(sys.argv[3]), os._exit, [3]).start()
http.server.ThreadingHTTPServer(
    ("127.0.0.1", int(sys.argv[1])), http.server.SimpleHTTPRequestHandler
).serve_forever()
"""
"""A server that writes down when it started, serves, and exits with status 3 a moment later."""


class Crashing(Served, frozen=True):
    """A companion that serves for ``lasts`` seconds each time it is started, then exits."""

    script: Path
    starts: Path
    lasts: float = 0.5

    def process(self, place: CompanionPlace, root: Path) -> CompanionProcess:
        return CompanionProcess(
            argv=[
                sys.executable,
                str(self.script),
                str(place.ports["web"]),
                str(self.starts),
                str(self.lasts),
            ],
            cwd=root,
        )


def test_a_companion_that_keeps_exiting_is_started_again_ever_more_slowly(
    state: Path, tmp_path: Path
) -> None:
    script = tmp_path / "crashing.py"
    script.write_text(CRASHING, encoding="utf-8")
    starts = tmp_path / "starts"
    crashing = Crashing(
        name="crashing",
        ports={"web": free_port()},
        script=script,
        starts=starts,
        backoff=(0.2, 0.8, 1.6),
        watched_every=0.05,
    )
    slot = crashing.slot(tmp_path)

    with crashing.held(launch_at(tmp_path)):
        for _ in range(300):
            if starts.exists() and len(starts.read_text().splitlines()) >= 4:
                break
            time.sleep(0.05)
        kept = slot.read()

    begun = [float(line) for line in starts.read_text().splitlines()]
    waited = [later - earlier for earlier, later in itertools.pairwise(begun)]
    assert len(begun) >= 4, begun
    assert waited[0] >= 0.5 + 0.2 and waited[1] >= 0.5 + 0.8, waited
    assert waited[2] >= 0.5 + 1.6, waited
    # The fourth start writes its line before it answers, and a start is
    # counted once it answers.
    assert kept.failing >= 3 and kept.restarts >= 2
    assert kept.exited is not None and kept.exited.status == 3


def test_a_lease_dropped_while_its_launcher_runs_is_taken_back_and_started_again(
    state: Path, tmp_path: Path
) -> None:
    """A release that judged a live holder gone stopped what ran; that holder brings it back."""
    served = Served(
        name="served", ports={"web": free_port()}, backoff=(0.2,), watched_every=0.1
    )
    slot = served.slot(tmp_path)

    with served.held(launch_at(tmp_path)):
        with slot.locked():
            dropped = slot.read()
            assert dropped.running is not None
            dropped.running.process.stop(served.grace)
            slot.write(dropped.model_copy(update={"leases": [], "running": None}))
        again = started_after(slot, dropped.running)
        leases = slot.read().leases

    assert [lease.id for lease in leases] == [lease.id for lease in dropped.leases]
    assert not again.process.running()
    assert "while that launch still holds it; taken back" in slot.log().read_text()


def test_every_stop_says_why_first_in_the_log_and_the_state(
    state: Path, tmp_path: Path
) -> None:
    served = Served(name="served", ports={"web": free_port()})
    slot = served.slot(tmp_path)

    with served.held(launch_at(tmp_path)):
        running = slot.read().running
        assert running is not None
    stopped = slot.read().stopped

    assert stopped is not None and stopped.process == running.process
    assert stopped.why.startswith("the last lease was let go")
    assert stopped.leases == 0 and stopped.by == os.getpid()
    assert f"stopping pid {running.process.pid}: the last lease" in (
        slot.log().read_text()
    )


def test_checkouts_hold_companions_of_their_own_and_a_person_one(
    state: Path, tmp_path: Path
) -> None:
    per_checkout = Served(name="served")
    per_person = Served(name="served", scope=CompanionScope.USER)

    assert per_checkout.slot(tmp_path / "a") != per_checkout.slot(tmp_path / "b")
    assert per_person.slot(tmp_path / "a") == per_person.slot(tmp_path / "b")
    assert per_checkout.slot(tmp_path / "a").directory.is_relative_to(state)


def test_a_companion_that_never_answers_refuses_the_launch(
    state: Path, tmp_path: Path
) -> None:
    class Silent(Served, frozen=True):
        def process(self, place: CompanionPlace, root: Path) -> CompanionProcess:
            return CompanionProcess(
                argv=[sys.executable, "-c", "import time; time.sleep(30)"], cwd=root
            )

    silent = Silent(name="silent", ports={"web": free_port()}, ready_within=0.3)

    with pytest.raises(LaunchRefused, match="did not answer"):
        with silent.held(launch_at(tmp_path)):
            pass


@pytest.fixture
def stubbed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Every host measurement a command settles, stubbed, around a fresh checkout."""
    root = tmp_path / "tree" / "work"
    root.mkdir(parents=True)
    monkeypatch.delenv(MAX_RECURSIVE_AGENT_ENV, raising=False)
    monkeypatch.setattr(launch_session, "settle_boundary", Mock())
    monkeypatch.setattr(launch_session, "say_opening", Mock())
    monkeypatch.setattr(launch_session, "verify_inside", Mock(return_value=[]))
    for module in (launch_session, claude_launch, codex_launch):
        monkeypatch.setattr(module, "launched_member", lambda *_a, **_k: MEMBER)
    for module in (claude_launch, codex_launch):
        monkeypatch.setattr(module, "runtime_preflight", lambda *_a, **_k: [])
        monkeypatch.setattr(
            module, "apply_sandbox_environment", lambda *_a, **_k: False
        )
    home = CodexHomeSelection(path=root / "codex-home", isolated=False)
    monkeypatch.setattr(codex_launch, "select_codex_home", lambda *_a, **_k: home)
    monkeypatch.setattr(codex_launch, "codex_login_preflight", lambda *_a, **_k: None)
    monkeypatch.setattr(codex_launch, "prepare_codex_plugin", lambda *_a, **_k: None)
    return root


def test_a_command_carries_what_its_companions_hand_it_and_lets_them_go(
    stubbed: Path,
) -> None:
    seen = Seen()
    companion = Fixed(
        name="inbox",
        given=Contribution(
            environment={"INBOX_URL": "http://127.0.0.1:8400"},
            mounts=[Mount(path=stubbed.parent / "answers", writable=True)],
            notices=[Notice(text="inbox at 8400")],
        ),
        seen=seen,
    )
    claude = Claude(
        cwd=stubbed,
        sandbox=InnerSandbox(escapable=True),
        identity=Member(wake_sockets=None),
        companions=[companion],
    ).command()
    codex = Codex(
        cwd=stubbed,
        sandbox=InnerSandbox(),
        identity=Member(wake_sockets=None),
        companions=[companion],
    ).command()
    settings = json.loads(claude.argv[claude.argv.index("--settings") + 1])

    assert claude.env["INBOX_URL"] == codex.env["INBOX_URL"] == "http://127.0.0.1:8400"
    assert (
        str(stubbed.parent / "answers")
        in settings["sandbox"]["filesystem"]["allowWrite"]
    )
    assert any(str(stubbed.parent / "answers") in word for word in codex.argv)
    assert seen.events == [
        "claude:held",
        "claude:released",
        "codex:held",
        "codex:released",
    ]


def test_a_contained_session_is_handed_its_companions_variables_by_name(
    stubbed: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    forwarded: list[str] = []

    def contained(*_args: object, inherited_environment: list[str], **_k: object):
        forwarded.extend(inherited_environment)
        return ["engine", "run"]

    monkeypatch.setattr(launch_session, "contained_argv", contained)
    from lup.launch.declaration import OuterContainer

    Claude(
        cwd=stubbed,
        sandbox=OuterContainer(),
        identity=Member(wake_sockets=None),
        companions=[
            Fixed(name="inbox", given=Contribution(environment={"INBOX_URL": "x"}))
        ],
    ).command()

    assert "INBOX_URL" in forwarded


def test_a_mount_a_companion_hands_reaches_the_policy_with_no_wall(
    stubbed: Path,
) -> None:
    answers = Mount(path=stubbed.parent / "answers")
    agent = Claude(
        cwd=stubbed,
        sandbox=NoSandbox(),
        companions=[Fixed(name="inbox", given=Contribution(mounts=[answers]))],
    )

    assert agent.sandbox.widened([answers]).roots() == [answers.root()]
