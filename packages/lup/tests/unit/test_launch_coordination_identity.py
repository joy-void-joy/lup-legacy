"""Who a launched session is on its repository's roster, and what it is called.

Two facts with different standing. The **id** is the launcher's claim: it is
minted where both runtimes pass through and exported into the environment the
CLI is started with, so the session's tool server and its hooks — separate
processes with no channel between them — answer to one address rather than to
one each. The **name** is a display detail, and only one runtime has anywhere
to put it.

That asymmetry is the point of the split rather than a gap in it. What a peer
is addressed by lives in lup's own `names.jsonl`, so a runtime with no
launch-time name flag loses nothing: the roster answers to the same name on
both, and renaming goes through the same command.
"""

import socket
from pathlib import Path
from unittest.mock import Mock

import pytest

import lup.devtools.harness.launch as launch
import lup.launch.session as launch_session
from lup.launch.declaration import LaunchSandbox
from lup.coordination.identity import (
    MEMBER_ENV,
    NAME_ENV,
    LaunchedMember,
    mint_member_id,
)
from lup.coordination.repository import RepositoryPeers
from lup.coordination.wake import WakePath
from lup.harness.messaging import WakeSockets
from lup.workspace.edition import shared_git_directory
from tests.unit.harness_launch import composition, harness, profiles, stub_host


@pytest.fixture
def uncontained(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Everything an uncontained `session_argv` reaches that is not its subject."""
    monkeypatch.setattr(launch_session, "settle_boundary", lambda *a, **k: None)
    monkeypatch.setattr(launch_session, "say_opening", lambda *a, **k: None)


def opened(environment: dict[str, str], tmp_path: Path) -> list[str]:
    """Build the argv for one uncontained session against this environment."""
    built = harness()
    return launch_session.session_argv(
        "claude",
        ["--model", "opus"],
        tmp_path,
        built.image,
        built.requirements,
        Mock(),
        tmp_path,
        Mock(),
        LaunchSandbox.INNER,
        environment,
    )


@pytest.mark.usefixtures("uncontained")
def test_a_launched_session_is_given_a_member_id(tmp_path: Path) -> None:
    """The launcher's claim, in the environment the CLI is started with.

    `sh` is handed this same dictionary as the child's environment, so a value
    here is what the session's tool server and hooks read — which is what lets
    them agree on one address without talking to each other.
    """
    environment: dict[str, str] = {}

    argv = opened(environment, tmp_path)

    assert argv == ["claude", "--model", "opus"]
    assert environment[MEMBER_ENV]
    assert environment[NAME_ENV] == tmp_path.name


@pytest.mark.usefixtures("uncontained")
def test_a_second_session_in_a_worktree_is_named_apart_from_the_first(
    tmp_path: Path,
) -> None:
    """The exported name is the one the roster will answer to, so it is minted
    against the sessions already there rather than derived twice.
    """
    RepositoryPeers(tmp_path).join(mint_member_id(), tmp_path)
    environment: dict[str, str] = {}

    opened(environment, tmp_path)

    assert environment[NAME_ENV] == f"{tmp_path.name}-2"


@pytest.mark.usefixtures("uncontained")
def test_two_launches_are_two_members(tmp_path: Path) -> None:
    """Two sessions in one worktree are two peers, which is the case the id holds.

    The worktree names them and does not identify them: a derived id would
    make these one member, and one of them would read the other's mailbox.
    """
    first: dict[str, str] = {}
    second: dict[str, str] = {}

    opened(first, tmp_path)
    opened(second, tmp_path)

    assert first[MEMBER_ENV] != second[MEMBER_ENV]


@pytest.mark.usefixtures("uncontained")
def test_an_operator_s_own_address_is_not_handed_to_what_they_launch(
    tmp_path: Path,
) -> None:
    """An inherited value is replaced rather than respected.

    The variable means "a launcher minted this and can prove it". An operator
    who happened to have it exported would otherwise hand their own roster
    address to every session they start, and two peers would answer to one id
    — the one thing a durable id exists to rule out.
    """
    environment = {MEMBER_ENV: "the-operators-own-address"}

    opened(environment, tmp_path)

    assert environment[MEMBER_ENV] != "the-operators-own-address"


def test_the_runtime_display_name_follows_the_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What the runtime shows agrees with what the roster answers to.

    A person watching several sessions is distinguishing between checkouts, so
    the worktree is the name — the same one `derived_cli_name` puts on the
    roster for a session nobody renamed.
    """
    worktree = tmp_path / "feat-coordination"
    worktree.mkdir()
    captured: list[list[str]] = []  # lup: ignore[empty-collection] — argv record

    launched(worktree, monkeypatch, captured, extra=[])

    assert "--name" in captured[0]
    assert captured[0][captured[0].index("--name") + 1] == "feat-coordination"


def test_the_runtime_display_name_is_numbered_apart_from_a_live_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What the chrome shows is what the roster answers to, so it is minted once
    against the sessions already there.
    """
    worktree = tmp_path / "feat-coordination"
    worktree.mkdir()
    RepositoryPeers(worktree).join(mint_member_id(), worktree)
    captured: list[list[str]] = []  # lup: ignore[empty-collection] — argv record

    launched(worktree, monkeypatch, captured, extra=[])

    assert captured[0][captured[0].index("--name") + 1] == "feat-coordination-2"


def test_a_caller_who_named_their_own_session_still_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The derived name is a default, and defaults come before what was asked for.

    Both spellings reach the runtime, and the later one is the caller's.
    """
    worktree = tmp_path / "feat-coordination"
    worktree.mkdir()
    captured: list[list[str]] = []  # lup: ignore[empty-collection] — argv record

    launched(worktree, monkeypatch, captured, extra=["--name", "the-one-i-meant"])

    argv = captured[0]
    assert argv.index("the-one-i-meant") > argv.index("feat-coordination")


def launched(
    worktree: Path,
    monkeypatch: pytest.MonkeyPatch,
    captured: list[list[str]],
    extra: list[str],
) -> None:
    """`harness claude` in ``worktree``, the host stubbed and the identity minted for real."""
    import lup.coordination.repository as repository
    import lup.providers.claude.launch as claude_launch

    caught = stub_host(monkeypatch, worktree)
    for module in (launch_session, claude_launch):
        monkeypatch.setattr(module, "launched_member", repository.launched_member)
    monkeypatch.setattr(claude_launch, "settle_claude_theme", lambda *_a, **_k: None)

    launch.launch_claude(
        composition(worktree, "claude"),
        launch.LaunchRequest(words=extra, sandbox=LaunchSandbox.INNER),
        profiles(),
        False,
    )
    captured.append(caught.argv)


def placed(sockets: WakeSockets, root: Path, member_id: str, cli_name: str) -> str:
    """Where a launch in *root* places one member's wake socket, which is somewhere."""
    found = launch_session.placed_wake_socket(
        sockets, root, LaunchedMember(member_id=member_id, cli_name=cli_name)
    )
    assert found is not None
    return found


def joined(root: Path, sockets: WakeSockets, cli_name: str) -> tuple[str, str]:
    """A member on *root*'s roster that declared the wake socket placed for it."""
    member = mint_member_id()
    address = sockets.socket(shared_git_directory(root), member)
    RepositoryPeers(root).join(
        member,
        root,
        cli_name=cli_name,
        wake=WakePath(runtime="claude", handle=address),
    )
    return member, address


def test_the_wake_socket_is_keyed_by_the_member_id_and_not_its_name(
    tmp_path: Path, wake_sockets: WakeSockets
) -> None:
    """The id addresses; the name is for a person to read, and repeats."""
    member = mint_member_id()

    address = placed(wake_sockets, tmp_path, member, "display-only")

    assert address == wake_sockets.socket(shared_git_directory(tmp_path), member)
    assert Path(address).name.endswith(f"--{member}.sock")
    assert "display-only" not in address


def test_two_sessions_with_one_display_name_bind_two_wake_sockets(
    tmp_path: Path, wake_sockets: WakeSockets
) -> None:
    """Display names repeat by design, and each member still binds its own."""
    first = placed(wake_sockets, tmp_path, mint_member_id(), "dev")
    second = placed(wake_sockets, tmp_path, mint_member_id(), "dev")

    assert first != second


def test_a_rename_keeps_the_wake_socket(
    tmp_path: Path, wake_sockets: WakeSockets
) -> None:
    """A session renames itself at will, and its peers go on reaching it.

    The roster's handle is what a peer writes to, and it is the path the
    session bound; a path keyed by the name would be a handle the next rename
    left pointing at nothing.
    """
    member, address = joined(tmp_path, wake_sockets, "dev")
    peers = RepositoryPeers(tmp_path)

    peers.rename(member, "reviewing-the-socket")
    row = peers.row(member)

    assert row is not None
    assert row.wake.handle == address
    assert placed(wake_sockets, tmp_path, member, "reviewing-the-socket") == address


@pytest.mark.usefixtures("unix_socket")
def test_a_stale_socket_of_a_departed_member_is_cleared(
    tmp_path: Path, wake_sockets: WakeSockets
) -> None:
    """The roster says its owner left, and nothing answers where it bound.

    A crashed session leaves its socket file behind. Its path is never placed
    again, so it blocks nobody; it is removed so the directory holds the
    sessions there are rather than every session there was.
    """
    wake_sockets.serve()
    gone, address = joined(tmp_path, wake_sockets, "dev")
    RepositoryPeers(tmp_path).leave(gone)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as crashed:
        crashed.bind(address)

    newcomer = placed(wake_sockets, tmp_path, mint_member_id(), "dev")

    assert not Path(address).exists()
    assert newcomer != address


@pytest.mark.usefixtures("unix_socket")
def test_a_live_member_s_socket_is_left_whatever_answers_on_it(
    tmp_path: Path, wake_sockets: WakeSockets
) -> None:
    """Only the roster says a member is gone; a quiet socket does not."""
    wake_sockets.serve()
    _, address = joined(tmp_path, wake_sockets, "dev")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as quiet:
        quiet.bind(address)

    placed(wake_sockets, tmp_path, mint_member_id(), "dev")

    assert Path(address).is_socket()


@pytest.mark.usefixtures("unix_socket")
def test_a_departed_member_still_listening_keeps_its_socket(
    tmp_path: Path, wake_sockets: WakeSockets
) -> None:
    """The roster reads a lapsed pulse as a departure, and a process may outlive it.

    A machine back from sleep is one: every beat is stale until the next, and
    each session still listens. Its socket stays, so a peer can still wake it
    once it beats again.
    """
    wake_sockets.serve()
    gone, address = joined(tmp_path, wake_sockets, "dev")
    RepositoryPeers(tmp_path).leave(gone)

    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listening:
        listening.bind(address)
        listening.listen(1)
        placed(wake_sockets, tmp_path, mint_member_id(), "dev")

        assert Path(address).is_socket()


@pytest.mark.usefixtures("socket_refused")
def test_a_file_at_the_member_s_own_path_is_its_own_and_is_replaced(
    tmp_path: Path, wake_sockets: WakeSockets
) -> None:
    """Nothing but this member was ever keyed there, so nothing is asked.

    Not even from a shell refused ``socket(AF_UNIX)``: the file is an earlier
    run of this same member, and the launch replaces it rather than refusing.
    """
    wake_sockets.serve()
    member = mint_member_id()
    own = Path(wake_sockets.socket(shared_git_directory(tmp_path), member))
    own.touch()

    assert placed(wake_sockets, tmp_path, member, "dev") == str(own)
    assert not own.exists()
