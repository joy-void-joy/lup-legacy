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
from lup.launch.refusal import LaunchRefused
from lup.launch.declaration import LaunchSandbox
from lup.coordination.identity import (
    MEMBER_ENV,
    NAME_ENV,
    LaunchedMember,
    mint_member_id,
)
from lup.coordination.repository import RepositoryPeers
from lup.coordination.wake import WakePath
from lup.harness.messaging import SessionInboxes
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
    make these one member, and one of them would read the other's inbox.
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


@pytest.mark.usefixtures("unix_socket")
def test_a_live_inbox_is_refused_by_name_rather_than_by_the_runtime(
    tmp_path: Path,
) -> None:
    """The runtime's own refusal tells the reader to remove a live session's socket.

    Measured, from a session launched while another held the path it was
    given. lup asks first, and answers with the session the roster says is
    woken there -- which is who an operator ends, rather than the file that
    would cut it off if they removed it.
    """
    inboxes = SessionInboxes(directory=str(tmp_path / "in"))
    inboxes.serve()
    minted = LaunchedMember(member_id=mint_member_id(), cli_name="main")
    inbox = inboxes.socket(tmp_path, "main")
    RepositoryPeers(tmp_path).join(
        mint_member_id(),
        tmp_path,
        cli_name="main",
        wake=WakePath(runtime="claude", handle=inbox),
    )

    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as holder:
        holder.bind(inbox)
        holder.listen(1)
        with pytest.raises(LaunchRefused) as refused:
            launch_session.placed_inbox(inboxes, tmp_path, minted)

    assert str(refused.value).startswith(f"main is listening at {inbox}")


@pytest.mark.usefixtures("unix_socket")
def test_a_stale_inbox_is_cleared_and_placed(tmp_path: Path) -> None:
    """A crashed session's socket file is nobody's, so the launch takes the path."""
    inboxes = SessionInboxes(directory=str(tmp_path / "in"))
    inboxes.serve()
    inbox = inboxes.socket(tmp_path, "main")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as crashed:
        crashed.bind(inbox)

    placed = launch_session.placed_inbox(
        inboxes, tmp_path, LaunchedMember(member_id=mint_member_id(), cli_name="main")
    )

    assert placed == inbox
    assert not Path(inbox).exists()


@pytest.mark.usefixtures("socket_refused")
def test_a_launch_refused_a_socket_names_that_rather_than_a_listener(
    tmp_path: Path,
) -> None:
    """A shell that refuses ``socket(AF_UNIX)`` cannot ask who holds the inbox.

    So the launch says that, with whom the roster names there, rather than a
    bare ``PermissionError`` or a listener nobody asked about.
    """
    inboxes = SessionInboxes(directory=str(tmp_path / "in"))
    inboxes.serve()
    inbox = inboxes.socket(tmp_path, "main")
    Path(inbox).touch()
    RepositoryPeers(tmp_path).join(
        mint_member_id(),
        tmp_path,
        cli_name="main",
        wake=WakePath(runtime="claude", handle=inbox),
    )

    with pytest.raises(LaunchRefused) as refusal:
        launch_session.placed_inbox(
            inboxes,
            tmp_path,
            LaunchedMember(member_id=mint_member_id(), cli_name="main"),
        )

    said = str(refusal.value)
    assert said.startswith("this process may not open a Unix socket")
    assert f"listens at {inbox}" in said
    assert "the roster names main" in said
    assert Path(inbox).exists()
