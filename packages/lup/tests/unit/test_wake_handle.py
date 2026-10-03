"""What a session declares it can be woken by, and which half decides it.

The failure this is written against is a wake path that reads as present and
reaches nobody. What a handle even is differs by runtime -- a thread a
command names, or a socket on this filesystem -- so the only half that can
say whether one exists is the adapter for that runtime. For most of this
repository's history nothing said anything, because no writer took a wake at
all and every member carried the empty default.
"""

import json
import socket
from pathlib import Path
from threading import Thread

import pytest

from lup.coordination.identity import mint_member_id
from lup.coordination.repository import RepositoryPeers
from lup.coordination.wake import WakePath
from lup.providers.wake import wake
from lup.providers.identity import native_wake


def frames_taken_at(address: Path, wake_with: WakePath) -> list[dict[str, object]]:
    """Every frame one wake wrote, read back off a socket standing in for a peer.

    A real socket rather than a stub, because what is under test is the bytes
    on the wire: the runtime reads them and this repository cannot, so a test
    asserting against a mock would be asserting against its own idea of the
    frame. Stopped by having taken the frame rather than by closing the
    listener, which `accept` does not reliably see.
    """
    taken: list[bytes] = []
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        listener.bind(str(address))
        listener.listen(1)

        def take_one_frame() -> None:
            connection, _ = listener.accept()
            with connection:
                taken.append(connection.recv(4096))

        waiting = Thread(target=take_one_frame)
        waiting.start()
        roused = wake(wake_with, "look at your mailbox")
        waiting.join(timeout=5)

    assert roused.reached, roused.reason
    return [json.loads(frame) for frame in taken]


def test_a_claude_session_declares_the_wake_socket_its_runtime_named(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The runtime tells a session's own processes where that session listens.

    So the handle is read rather than derived: the session bound the socket
    and the launcher only asked where, which makes a path computed here a
    second opinion about a file exactly one process created.
    """
    monkeypatch.setenv("CLAUDE_CODE_MESSAGING_SOCKET", "/tmp/cc-socks/91.sock")

    declared = native_wake("claude", "dev-6")

    assert declared == WakePath(runtime="claude", handle="/tmp/cc-socks/91.sock")


def test_a_claude_session_nobody_launched_declares_nothing() -> None:
    """Its addressable name is one only the session itself can read.

    Blank rather than a guess: a handle that resolves to nobody costs a
    sender the belief that a peer was nudged, which is worse than being told
    plainly that none will be.
    """
    assert native_wake("claude", "") == WakePath()


def test_codex_declares_nothing_even_though_its_verb_exists() -> None:
    """The half Codex has is the transport, and the half it lacks is the handle.

    `codex queue` reaches a session from any process, which is the half Claude
    Code lacks -- but it takes a thread id or session name, and the launch has
    no flag that names a session, so the roster's name would name a thread
    that does not exist.
    """
    assert native_wake("codex", "dev-6") == WakePath()


def test_a_runtime_nobody_declared_is_not_guessed_at() -> None:
    assert native_wake("something-else", "dev-6") == WakePath()


def test_a_declared_handle_reaches_the_roster_and_survives_the_fold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The property the wake handle exists for.

    A `join` taking no such argument would hand `roster.joined` the model's
    empty default, and every row in the store's history would read
    `{runtime: '', handle: ''}` no matter what the session could actually be
    reached by.
    """
    peers = RepositoryPeers(tmp_path)
    member = mint_member_id()

    monkeypatch.setenv("CLAUDE_CODE_MESSAGING_SOCKET", str(tmp_path / "in.sock"))
    peers.join(
        member,
        tmp_path / "tree",
        cli_name="dev-6",
        wake=native_wake("claude", "dev-6"),
    )
    standing = [row for row in peers.present() if row.actor.id == member]

    assert [row.wake for row in standing] == [
        WakePath(runtime="claude", handle=str(tmp_path / "in.sock"))
    ]


@pytest.mark.usefixtures("unix_socket")
def test_the_roster_row_is_what_actually_wakes_the_member(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End to end, because the two halves were each correct and never met.

    `wake()` has always known how to answer for a Claude member and never had
    one to answer for. This reads the path back off the roster the way a
    sender does and checks a frame reaches the socket, rather than checking
    the two in isolation and assuming the join carried it.
    """
    address = tmp_path / "in.sock"
    monkeypatch.setenv("CLAUDE_CODE_MESSAGING_SOCKET", str(address))
    peers = RepositoryPeers(tmp_path)
    member = mint_member_id()
    peers.join(
        member,
        tmp_path / "tree",
        cli_name="dev-6",
        wake=native_wake("claude", "dev-6"),
    )

    delivered: list[bytes] = []
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        listener.bind(str(address))
        listener.listen(1)

        def take_one_frame() -> None:
            connection, _ = listener.accept()
            with connection:
                delivered.append(connection.recv(4096))

        waiting = Thread(target=take_one_frame)
        waiting.start()
        roused = wake(next(row.wake for row in peers.present()), "look at your mailbox")
        waiting.join(timeout=5)

    assert roused.reached
    assert not roused.reason
    assert json.loads(delivered[0])["message"]["content"] == "look at your mailbox"


def test_a_session_declares_the_id_its_wake_socket_checks_beside_the_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A path reaches whoever bound it; a path and an id reach a member.

    Both come from the runtime rather than being derived, and they are read
    together because a handle without its id is the case this exists to stop:
    every contained session's default socket is named after a pid its own
    namespace assigns, so two sessions in sibling containers publish one path.
    """
    monkeypatch.setenv("CLAUDE_CODE_MESSAGING_SOCKET", "/tmp/cc-socks/7.sock")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "9b1f0689-78cb")

    declared = native_wake("claude", "dev-6")

    assert declared == WakePath(
        runtime="claude", handle="/tmp/cc-socks/7.sock", session="9b1f0689-78cb"
    )


@pytest.mark.usefixtures("unix_socket")
def test_a_nudge_names_the_session_it_is_for(tmp_path: Path) -> None:
    """The receiving wake socket drops a frame whose id disagrees with its own.

    Measured against a live session: of two frames written to one socket, only
    the one carrying that session's id arrives. So naming the session is what
    turns a nudge that reached the wrong process from a message delivered to
    the wrong reader into one refused by them -- which is the ordering the
    whole design rests on, the record having been written already either way.
    """
    address = tmp_path / "in.sock"
    frames = frames_taken_at(
        address,
        wake_with=WakePath(
            runtime="claude", handle=str(address), session="9b1f0689-78cb"
        ),
    )

    assert frames[0]["session_id"] == "9b1f0689-78cb"


@pytest.mark.usefixtures("unix_socket")
def test_a_member_that_named_no_session_asks_for_no_check(tmp_path: Path) -> None:
    """Omitted rather than sent empty, because the runtime reads them apart.

    An absent `session_id` asks the socket to accept the frame, and an empty
    one is a session id that matches nobody. A member whose runtime told it
    nothing is in the first case: it should still be nudged by whoever holds
    that path, which is the behaviour every member had before ids travelled.
    """
    address = tmp_path / "in.sock"
    frames = frames_taken_at(
        address, wake_with=WakePath(runtime="claude", handle=str(address))
    )

    assert "session_id" not in frames[0]


@pytest.mark.usefixtures("socket_refused")
def test_a_process_refused_a_socket_says_so_rather_than_blaming_the_peer(
    tmp_path: Path,
) -> None:
    """The boundary the call ran inside is named, not a wake socket nobody tried.

    Measured in a Claude Code Bash sandbox: `socket(AF_UNIX)` itself fails
    with EPERM there, before any path is connected to, and reporting that as
    nobody listening sends the reader looking for a dead peer.
    """
    roused = wake(WakePath(runtime="claude", handle=str(tmp_path / "in.sock")), "look")

    assert not roused.reached
    assert roused.error_type == "UnixSocketRefused"
    assert "may not open a Unix socket" in roused.reason
    assert "nothing is listening" not in roused.reason
