"""Where a session's wake socket is placed, and the ways placement silently fails.

Each looks like success from the launcher's side. A bind mount whose source
does not exist takes the whole container down with an engine error naming
neither the directory nor the session. A mount whose target renames its source
produces a session that binds cleanly and publishes a path no peer can open. A
directory the runtime refuses -- because it is a symlink, is not ours, or is
not private -- produces a session with no wake socket at all, which reads
exactly like a peer that is merely busy. And a path keyed by anything that is
not the member's id collides with an earlier session's socket, or moves when
the session renames itself.
"""

import json
import re
import socket
import stat
from pathlib import Path

import pytest

from lup.coordination.identity import mint_member_id
from lup.coordination.wake import WakePath, wake
from lup.harness.image import Image
from lup.harness.messaging import WakeSockets

# The four directories Claude Code will scan for peers, as its own binary
# spells them. Written out rather than imported because they are the runtime's
# and this repository's interest in them is the opposite of the usual one: not
# to match, but to stay out.
RUNTIME_SCANNED = [
    r"^/tmp/cc-socks(?:-(0|[1-9]\d*))?$",
    r"^/private/tmp/cc-socks(?:-(0|[1-9]\d*))?$",
    r"^/run/user/(0|[1-9]\d*)/cc-socks$",
    r"^/data/data/com\.termux/files/usr/tmp/cc-socks(?:-(0|[1-9]\d*))?$",
]

# What the runtime will bind, as a Unix socket address: it refuses a longer
# path outright and says so. Its own message rounds it, and so does this.
ADDRESS_LIMIT = 104

# A repository's shared git directory, as a bare clone with its worktrees
# beside it spells one: what the launcher hands the placement.
LUP = Path("/home/me/lup.git")

# A member id as the launcher mints one.
MEMBER = "3f2a9c1d0e4b"


def test_the_directory_is_made_rather_than_left_to_the_engine(tmp_path: Path) -> None:
    """A bind mount whose source is absent is one the engine refuses entirely.

    Ordinary rather than exotic: a machine where no session has ever run
    outside a container has never had anything create this directory, so the
    first contained launch is the one that would fail.
    """
    directory = tmp_path / "lup-wake"
    served = WakeSockets(directory=str(directory)).serve()

    assert served == directory
    assert directory.is_dir()


def test_the_directory_is_made_private_because_the_runtime_checks(
    tmp_path: Path,
) -> None:
    """The runtime refuses a socket directory that is not mode 0700 and says why.

    Refused there rather than here, which is the reason this is pinned: a
    directory left at the umask's mode produces a session that starts, reports
    nothing unusual, and has no wake socket for anyone to nudge.
    """
    directory = tmp_path / "lup-wake"
    directory.mkdir(mode=0o755)

    served = WakeSockets(directory=str(directory)).serve()

    assert served == directory
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700


def test_serving_twice_is_the_second_session_arriving_rather_than_an_error(
    tmp_path: Path,
) -> None:
    """Every launch serves, and all but the first find it already there.

    That is the whole point of one directory -- several sessions arrive in it
    -- so the second arrival has to be unremarkable. Both postures serve for
    their own reasons, too: the mount needs the source to exist on the host,
    and an uncontained session needs the same directory with no mount at all.
    """
    directory = tmp_path / "lup-wake"
    sockets = WakeSockets(directory=str(directory))

    assert sockets.serve() == directory
    assert sockets.serve() == directory


def test_a_directory_declared_empty_serves_nothing(tmp_path: Path) -> None:
    """Emptying it is how an adopter declines the nudge.

    The posture every launch had before placement existed, kept reachable
    rather than removed: a session nobody can nudge still reads its mail, and
    nothing about the launch fails.
    """
    assert WakeSockets(directory="").serve() is None


def test_a_member_s_socket_is_keyed_by_its_id() -> None:
    """The id is the one name that never moves and never repeats.

    A pid does not survive the container boundary this crosses -- two sessions
    in sibling containers are each pid 7 in their own namespace -- and a
    display name repeats by design and changes at will. The id is minted once
    per member, so a path keyed by it has one owner for as long as it exists.
    """
    placed = WakeSockets(directory="/tmp/lup-wake").socket(LUP, MEMBER)

    assert Path(placed).parent == Path("/tmp/lup-wake")
    assert Path(placed).name.startswith("lup-")
    assert Path(placed).name.endswith(f"--{MEMBER}.sock")


def test_the_default_directory_is_named_for_the_wake() -> None:
    """The socket is what wakes a session, and the directory says so.

    "Inbox" is the review inbox's word: a session's mail is read off the
    coordination store, and nothing in this directory holds any of it.
    """
    assert WakeSockets().directory == "/tmp/lup-wake"


def test_two_repositories_key_one_id_apart() -> None:
    """The directory is the machine's, and a roster is one repository's.

    The repository leads the name -- its shared git directory's name and a
    digest of that directory's path -- so a file says which roster to ask about
    its owner. Its readable part is the same for two checkouts of one project,
    so the digest is what tells them apart.
    """
    sockets = WakeSockets()

    placed = {
        sockets.socket(Path("/home/me/nori/.git"), MEMBER),
        sockets.socket(Path("/home/me/lup.git"), MEMBER),
        sockets.socket(Path("/srv/elsewhere/nori/.git"), MEMBER),
    }

    assert len(placed) == 3
    assert sorted(Path(path).name.startswith("nori-") for path in placed) == [
        False,
        True,
        True,
    ]


def test_two_members_of_one_repository_bind_one_socket_each() -> None:
    """Two ids are two sockets, and one id is always the same one."""
    sockets = WakeSockets()
    other = mint_member_id()

    assert sockets.socket(LUP, MEMBER) != sockets.socket(LUP, other)
    assert sockets.socket(LUP, MEMBER) == sockets.socket(LUP, MEMBER)


def test_a_placed_address_stays_inside_what_a_unix_socket_holds() -> None:
    """Past about 104 bytes the runtime refuses the address and binds nothing.

    Which is why this directory is short and shallow rather than living beside
    the checkout it serves. A repository name that would run past it is cut,
    and the id and the digest never are: the id is what addresses the member,
    so a cut through it would hand two members one path.
    """
    deep = Path("/home/someone/" + "a-very-long-project-name-" * 4 + ".git")
    ids = [mint_member_id() for _ in range(2)]

    placed = [WakeSockets().socket(deep, member_id) for member_id in ids]

    assert len(WakeSockets().socket(LUP, MEMBER).encode()) < ADDRESS_LIMIT
    assert all(len(path.encode()) < ADDRESS_LIMIT for path in placed)
    assert all(
        Path(path).name.endswith(f"--{member_id}.sock")
        for path, member_id in zip(placed, ids, strict=True)
    )
    assert Path(placed[0]).name.startswith("a-very-long-project-name-")


def test_a_directory_too_long_to_hold_an_id_is_refused() -> None:
    """A path that cannot carry the whole id is one no member can own alone."""
    sockets = WakeSockets(directory="/tmp/" + "d" * 90)

    with pytest.raises(ValueError, match="wake socket"):
        sockets.socket(LUP, MEMBER)


def test_the_default_is_not_a_directory_the_runtime_scans_for_peers() -> None:
    """Staying out of those four is what keeps this a nudge and not a channel.

    A directory the runtime scans makes every session in it natively
    reachable by every other, through files that `lup.policy.kernel.peers`
    cannot see because it guards tool calls. Measured the other way round too:
    a session launched into this directory left the runtime's own holding only
    the launcher's socket.
    """
    directory = WakeSockets().directory

    assert not [scanned for scanned in RUNTIME_SCANNED if re.match(scanned, directory)]


def test_the_mount_keeps_the_path_it_had_outside() -> None:
    """The path is a datum, so source and target renaming it apart breaks it.

    A member publishes the path it bound and a peer in another container opens
    that same text. A target that differed would leave every handle correct
    inside the session that wrote it and wrong everywhere it was read -- and
    the launch would report success either way.
    """
    started = Image().session_arguments(
        tag="lup-agent:x",
        checkout=Path("/home/u/repo"),
        uid=1000,
        gid=1000,
        writable={Path("/home/u/repo"): "/home/u/repo"},
        read_only={},
        state_volume="lup-cfg-x",
        config_home_env="CLAUDE_CONFIG_DIR",
        wake_directory=Path("/tmp/lup-wake"),
    )

    assert "/tmp/lup-wake:/tmp/lup-wake:rw" in started


def test_a_launch_that_placed_nothing_mounts_nothing() -> None:
    """The launch where serving failed on a machine that wanted it.

    Separate from the declined case on purpose: the argv this produces has to
    be the argv of a session without placement rather than one carrying a
    mount to nowhere.
    """
    started = Image().session_arguments(
        tag="lup-agent:x",
        checkout=Path("/home/u/repo"),
        uid=1000,
        gid=1000,
        writable={Path("/home/u/repo"): "/home/u/repo"},
        read_only={},
        state_volume="lup-cfg-x",
        config_home_env="CLAUDE_CONFIG_DIR",
    )

    assert not [argument for argument in started if "lup-wake" in argument]


def test_an_operator_is_told_which_way_it_went() -> None:
    """Both answers change what the reader does next, so both are said.

    One who does not know placement happened routes a nudge through the
    person; one who does not know it failed reads a peer that never looks as
    the coordination store being broken, and goes looking in the wrong half.
    """
    sockets = WakeSockets()
    placed = sockets.notice(True)
    absent = sockets.notice(False)

    assert placed and absent
    assert [said.text for said in placed] != [said.text for said in absent]


@pytest.mark.usefixtures("unix_socket")
def test_a_nudge_reaches_the_member_it_is_keyed_to(wake_sockets: WakeSockets) -> None:
    """Two members listen side by side, and a wake picks the one it names.

    Real sockets bound at the placed paths, because what is under test is that
    the path a member declares is the one its own session binds and no other
    session does: the frame lands with the member it was addressed to, and the
    other has nothing waiting.
    """
    wake_sockets.serve()
    wanted = wake_sockets.socket(LUP, MEMBER)
    beside = wake_sockets.socket(LUP, mint_member_id())

    with (
        socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as wanted_socket,
        socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as beside_socket,
    ):
        for listener, path in [(wanted_socket, wanted), (beside_socket, beside)]:
            listener.bind(path)
            listener.listen(1)
            listener.setblocking(False)
        roused = wake(WakePath(runtime="claude", handle=wanted), "read your mail")
        connection, _ = wanted_socket.accept()
        with connection:
            frame = json.loads(connection.recv(4096))
        with pytest.raises(BlockingIOError):
            beside_socket.accept()

    assert roused.reached, roused.reason
    assert frame["message"]["content"] == "read your mail"


@pytest.mark.usefixtures("unix_socket")
def test_a_departed_member_s_socket_nothing_answers_on_is_retired(
    wake_sockets: WakeSockets,
) -> None:
    """A crashed session leaves its socket file behind, bound by nobody.

    Retired only once the roster has said its owner is gone, which is the
    caller's to ask; what is checked here is the rest -- that the file is that
    member's own, and that nothing still answers on it.
    """
    wake_sockets.serve()
    address = wake_sockets.socket(LUP, MEMBER)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as crashed:
        crashed.bind(address)

    assert Path(address).is_socket()
    assert wake_sockets.retire(LUP, MEMBER, address)
    assert not Path(address).exists()


@pytest.mark.usefixtures("unix_socket")
def test_a_socket_something_still_answers_on_is_left(wake_sockets: WakeSockets) -> None:
    """A roster that reads every pulse as lapsed is a machine back from sleep.

    Every session's beat is minutes stale until its next one, so the roster
    calls them gone while each still listens. Removing the file would leave
    that session answering on nothing a peer can open, for the rest of its
    life, so a socket something answers on is never retired.
    """
    wake_sockets.serve()
    address = wake_sockets.socket(LUP, MEMBER)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as live:
        live.bind(address)
        live.listen(1)

        assert not wake_sockets.retire(LUP, MEMBER, address)
        assert Path(address).is_socket()


@pytest.mark.usefixtures("unix_socket")
def test_a_handle_that_is_not_the_member_s_own_path_is_never_touched(
    tmp_path: Path, wake_sockets: WakeSockets
) -> None:
    """A session nothing placed declares the runtime's pid-named default.

    That path is keyed by a pid, which another session reuses in its own
    namespace, so the file there is not provably the departed member's.
    """
    wake_sockets.serve()
    foreign = tmp_path / "7.sock"
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as crashed:
        crashed.bind(str(foreign))

    assert not wake_sockets.retire(LUP, MEMBER, str(foreign))
    assert foreign.is_socket()


def test_a_member_with_no_socket_left_retires_nothing(
    wake_sockets: WakeSockets,
) -> None:
    """The ordinary departure: the runtime removed its own socket as it left."""
    wake_sockets.serve()

    assert not wake_sockets.retire(LUP, MEMBER, wake_sockets.socket(LUP, MEMBER))


@pytest.mark.usefixtures("unix_socket")
def test_a_process_refused_a_socket_leaves_the_file_it_could_not_ask_about(
    wake_sockets: WakeSockets, request: pytest.FixtureRequest
) -> None:
    """Neither answer is safe unasked, and leaving the file costs nothing.

    An id is never minted twice, so a socket left in place blocks no launch:
    it waits for a launcher that may ask. The socket is bound before the
    refusal starts, as a crashed session's was before this shell existed.
    """
    wake_sockets.serve()
    address = wake_sockets.socket(LUP, MEMBER)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as crashed:
        crashed.bind(address)
    request.getfixturevalue("socket_refused")

    assert not wake_sockets.retire(LUP, MEMBER, address)
    assert Path(address).is_socket()
