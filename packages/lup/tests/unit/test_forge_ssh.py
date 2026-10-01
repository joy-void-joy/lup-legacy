"""What a host's ssh identity offers a contained session, and what it does not.

Every question here has an easy wrong answer that looks right at launch and
fails at the first push: `SSH_AUTH_SOCK` is set, so there is an agent; a key
file exists, so there is a key; ssh works on this machine, so it works in the
container. Each of these puts the real question to ssh's own tools instead,
against real agents, real keys and a real `known_hosts`, because a double
that answered these would be a double of the assumption being refuted.
"""

import os
import shutil
import socket
import stat
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
import sh

from lup.harness.credential import GitAccess
from lup.harness.ssh import (
    agent_socket,
    configuration,
    ephemeral_home,
    host_ssh_material,
    knows,
)

needs_ssh = pytest.mark.skipif(
    shutil.which("ssh-keygen") is None or shutil.which("ssh-add") is None,
    reason="the questions this asks are put to ssh's own tools",
)


def sockets_bind() -> bool:
    """Whether a unix socket can be made where this process's temporary files go.

    Asked rather than assumed, because the answer is no inside this project's
    own sandbox: writing into the temporary directory is permitted and
    binding in it is not, which arrives as `unix_listener: socket: Operation
    not permitted` from ssh-agent. The agent rungs are exercised against real
    agents on real sockets, so where none can be made they skip with the
    reason instead of failing as though the code under test were wrong.

    The constructor is inside the guard rather than before it, which is the
    whole measurement: this sandbox refuses `AF_UNIX` at `socket()` itself,
    so a probe guarding only the `bind` would raise during collection and take
    the entire module down with it -- every test in here reported as an
    error, including the ones that need no socket at all.
    """
    with tempfile.TemporaryDirectory() as directory:
        try:
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        except OSError:
            return False
        try:
            listener.bind(str(Path(directory) / "probe.sock"))
        except OSError:
            return False
        finally:
            listener.close()
    return True


needs_sockets = pytest.mark.skipif(
    not sockets_bind(),
    reason="this sandbox binds no unix socket where its temporary files go",
)


def made_key(path: Path, passphrase: str = "") -> Path:
    """One real key pair, because a fabricated file answers nothing ssh asks."""
    sh.Command("ssh-keygen")(
        "-t", "ed25519", "-N", passphrase, "-C", "", "-q", "-f", str(path)
    )
    return path


def trusted(directory: Path, host: str) -> Path:
    """A `known_hosts` carrying a real host key for that name.

    Built from a generated key rather than written by hand: `ssh-keygen -F`
    reads the file as ssh reads it, so a line that merely contains the
    hostname would answer nothing.
    """
    key = made_key(directory / "hostkey")
    known = directory / "known_hosts"
    known.write_text(
        f"{host} {(directory / 'hostkey.pub').read_text().strip()}\n", encoding="utf-8"
    )
    key.unlink()
    (directory / "hostkey.pub").unlink()
    return known


@pytest.fixture
def ssh_home(tmp_path: Path) -> Path:
    """An operator's ssh directory, empty and addressed the way a launch does."""
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    return home


def running_agent(socket_path: Path) -> Iterator[sh.RunningCommand]:
    """A real ssh agent on a socket of this test's own, stopped afterwards."""
    # `_bg_exc=False` because stopping it is how this ends: the kill is a
    # nonzero exit sh would otherwise raise on a thread nobody is waiting on,
    # which arrives as an unhandled-thread warning against whichever test was
    # running at the time.
    started = sh.Command("ssh-agent")(
        "-D", "-a", str(socket_path), _bg=True, _bg_exc=False
    )
    for _ in range(100):
        if socket_path.exists():
            break
        time.sleep(0.02)
    try:
        yield started
    finally:
        started.kill()


@pytest.fixture
def empty_agent(tmp_path: Path) -> Iterator[Path]:
    """An agent that answers and holds nothing, which is a fresh login session."""
    where = tmp_path / "empty.sock"
    for _ in running_agent(where):
        yield where


@pytest.fixture
def loaded_agent(tmp_path: Path) -> Iterator[Path]:
    """An agent holding one identity, which is the only kind worth forwarding."""
    where = tmp_path / "loaded.sock"
    key = made_key(tmp_path / "loaded-key")
    for _ in running_agent(where):
        sh.Command("ssh-add")(
            str(key), _env={**os.environ, "SSH_AUTH_SOCK": str(where)}
        )
        yield where


def test_an_unset_variable_is_not_an_agent() -> None:
    """The base case, and the only one a naive check gets right."""
    assert agent_socket({}) == ""


def test_a_socket_path_that_no_longer_exists_is_not_an_agent(tmp_path: Path) -> None:
    """What a reattached terminal multiplexer leaves in a session's environment.

    The agent that owned it exited; the variable pointing at it did not, and
    every shell opened in that session inherits a name for nothing.
    """
    assert agent_socket({"SSH_AUTH_SOCK": str(tmp_path / "gone.sock")}) == ""


def test_a_path_that_is_not_a_socket_is_not_an_agent(tmp_path: Path) -> None:
    """A file where a socket should be, which forwards as a file and signs nothing."""
    ordinary = tmp_path / "not-a-socket"
    ordinary.write_text("", encoding="utf-8")

    assert agent_socket({"SSH_AUTH_SOCK": str(ordinary)}) == ""


@needs_sockets
def test_a_socket_nothing_answers_on_is_not_an_agent(tmp_path: Path) -> None:
    """It is a socket, it is bound, and no agent is behind it.

    Every test that stops at `S_ISSOCK` passes here, and the session it
    admits meets `Error connecting to agent` at the first push.
    """
    where = tmp_path / "dead.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(where))
    try:
        assert stat.S_ISSOCK(where.stat().st_mode)
        assert agent_socket({"SSH_AUTH_SOCK": str(where)}) == ""
    finally:
        listener.close()


@needs_ssh
@needs_sockets
def test_an_agent_holding_no_identity_is_not_a_credential(empty_agent: Path) -> None:
    """Reachable, well-formed, and unable to sign anything.

    This is what a login session looks like before the first `ssh-add`, and
    it is the case that separates "an agent answers" from "an agent can
    authenticate".
    """
    assert agent_socket({"SSH_AUTH_SOCK": str(empty_agent)}) == ""


@needs_ssh
@needs_sockets
def test_an_agent_holding_an_identity_is_the_one_rung_that_leaves_nothing_behind(
    loaded_agent: Path,
) -> None:
    """The credential the ladder prefers, and the only one it can verify outright."""
    assert agent_socket({"SSH_AUTH_SOCK": str(loaded_agent)}) == str(loaded_agent)


@needs_ssh
def test_a_key_that_opens_with_no_passphrase_is_usable_and_one_that_does_not_is_not(
    ssh_home: Path,
) -> None:
    """An encrypted key in a container is a file that can be read and not used.

    There is no agent, no terminal and nobody at the other end to type a
    passphrase into, so copying one is not a smaller version of working --
    it is a launch reporting a credential and a push asking for input no
    session can give.
    """
    made_key(ssh_home / ".ssh" / "open")
    made_key(ssh_home / ".ssh" / "locked", passphrase="not-empty")

    material = host_ssh_material(ssh_home, "github.com")

    assert [item.name for item in material.keys] == ["open"]
    assert [item.name for item in material.locked] == ["locked"]


@needs_ssh
def test_the_files_ssh_keeps_for_itself_are_never_mistaken_for_keys(
    ssh_home: Path,
) -> None:
    """`known_hosts` fingerprints perfectly well, which is exactly the danger.

    A file of the operator's host keys copied into a container as though it
    were a private key is the kind of wrong that produces no error anywhere.
    """
    trusted(ssh_home / ".ssh", "github.com")
    (ssh_home / ".ssh" / "config").write_text("Host *\n", encoding="utf-8")
    (ssh_home / ".ssh" / "authorized_keys").write_text("", encoding="utf-8")

    material = host_ssh_material(ssh_home, "github.com")

    assert material.keys == []
    assert material.locked == []


@needs_ssh
def test_a_forge_absent_from_known_hosts_cannot_be_verified_by_a_session(
    ssh_home: Path,
) -> None:
    """`StrictHostKeyChecking` is left at ssh's default, so this is a refusal.

    A session that cannot verify the forge fails host-key verification at the
    first connection. Accepting an unknown host to avoid that would be
    weakening the one check the copied `known_hosts` exists to preserve.
    """
    trusted(ssh_home / ".ssh", "gitlab.com")

    assert host_ssh_material(ssh_home, "github.com").known is False
    assert host_ssh_material(ssh_home, "gitlab.com").known is True


@needs_ssh
def test_a_hashed_known_hosts_still_answers_for_the_host_it_carries(
    tmp_path: Path,
) -> None:
    """Which is why the lookup is `ssh-keygen -F` and not a search of the file.

    `HashKnownHosts` is on by default on several distributions, and every
    line in such a file is an HMAC of the hostname rather than the hostname.
    A text search answers "no" for a host that is perfectly well known.
    """
    known = trusted(tmp_path, "github.com")
    sh.Command("ssh-keygen")("-H", "-f", str(known))

    assert "github.com" not in known.read_text()
    assert knows(known, "github.com")


def test_an_unreadable_ssh_directory_is_an_absence_rather_than_a_failure(
    tmp_path: Path,
) -> None:
    """The launch started from inside a session, where the boundary denies this.

    The same declaration that offers these credentials denies an agent
    reading them, so a launch made from in there finds the directory closed.
    That is the boundary working, and it has to arrive as a decline rather
    than as a traceback.
    """
    material = host_ssh_material(tmp_path / "no-such-home", "github.com")

    assert material.reachable is False
    assert material.keys == []


@needs_ssh
def test_a_lent_key_is_copied_at_a_mode_ssh_will_read(ssh_home: Path) -> None:
    """Stated rather than inherited, so the mode is this code's rather than the host's.

    The home around it matters as much as the file: a directory readable by
    anyone is a directory anyone can read the copies out of, for as long as
    the session runs.
    """
    made_key(ssh_home / ".ssh" / "open")
    trusted(ssh_home / ".ssh", "github.com")

    lent = ephemeral_home(
        host_ssh_material(ssh_home, "github.com"), "/run/lup/ssh", keys=True
    )

    assert lent is not None
    assert stat.S_IMODE((lent / "open").stat().st_mode) == 0o600
    assert stat.S_IMODE(lent.stat().st_mode) == 0o700
    assert (lent / "open.pub").is_file()
    assert stat.S_IMODE((lent / "config").stat().st_mode) == 0o600


@needs_ssh
def test_a_key_the_host_left_world_readable_is_refused_a_step_earlier(
    ssh_home: Path,
) -> None:
    """Measured rather than assumed, and it moves where the guard has to be.

    ssh-keygen refuses to read a private key that is group- or
    world-readable at all -- `UNPROTECTED PRIVATE KEY FILE` -- so such a key
    never reaches the copy: it fails the passphrase probe and is classified
    unusable beside the encrypted ones. Which is the right place for it,
    because it is equally unusable on the host.
    """
    made_key(ssh_home / ".ssh" / "open")
    (ssh_home / ".ssh" / "open").chmod(0o644)

    material = host_ssh_material(ssh_home, "github.com")

    assert material.keys == []
    assert [item.name for item in material.locked] == ["open"]


@needs_ssh
def test_the_forwarded_agent_is_lent_no_private_key_at_all(ssh_home: Path) -> None:
    """The whole difference between lending the use of a key and lending the key.

    A home built for the socket carries the compiled configuration and the
    host keys and nothing else; anything more would leave a copy behind, and
    leaving no copy is the reason that rung is preferred.
    """
    made_key(ssh_home / ".ssh" / "open")
    trusted(ssh_home / ".ssh", "github.com")

    lent = ephemeral_home(
        host_ssh_material(ssh_home, "github.com"), "/run/lup/ssh", keys=False
    )

    assert lent is not None
    assert sorted(item.name for item in lent.iterdir()) == ["config", "known_hosts"]


@needs_ssh
def test_nothing_lent_lands_in_the_checkout_or_a_profile(ssh_home: Path) -> None:
    """The three places a copy would outlive the session that needed it.

    The checkout is what `git clean -fdx` walks and what a commit could
    carry; a profile is what the next launch reuses.
    """
    made_key(ssh_home / ".ssh" / "open")
    trusted(ssh_home / ".ssh", "github.com")

    lent = ephemeral_home(
        host_ssh_material(ssh_home, "github.com"), "/run/lup/ssh", keys=True
    )

    assert lent is not None
    assert Path(tempfile.gettempdir()) in lent.parents


@needs_ssh
def test_what_was_lent_is_removed_when_the_launcher_exits(
    ssh_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Which is when the session it was made for ends, since the launcher waits.

    Registered rather than deferred to a caller: a copy whose removal is
    somebody else's job is a copy that survives every path that forgets.
    """
    removals: list[Path] = []
    monkeypatch.setattr(
        "lup.harness.ssh.atexit.register",
        lambda function, directory, ignore: removals.append(directory),
    )
    made_key(ssh_home / ".ssh" / "open")
    trusted(ssh_home / ".ssh", "github.com")

    lent = ephemeral_home(
        host_ssh_material(ssh_home, "github.com"), "/run/lup/ssh", keys=True
    )

    assert removals == [lent]


def test_the_compiled_configuration_names_container_paths_and_nothing_of_the_hosts() -> (
    None
):
    """It is written on the host and read inside, so every path in it is inside.

    Compiled rather than copied, because the host's own `config` names
    `IdentityFile` paths that do not exist in the container, `Include`s files
    that were not copied, and on macOS a `UseKeychain` no Linux ssh
    understands -- a configuration that parses and behaves differently from
    the one the operator tested.
    """
    with_keys = configuration([Path("/home/op/.ssh/id_ed25519")], True, "/run/lup/ssh")

    assert "IdentityFile /run/lup/ssh/id_ed25519" in with_keys
    assert "UserKnownHostsFile /run/lup/ssh/known_hosts" in with_keys
    assert "/home/op" not in with_keys


def test_identities_only_appears_with_keys_and_never_with_a_forwarded_agent() -> None:
    """It is what stops a forge answering `Too many authentication failures`.

    And it is precisely wrong on the socket, where the identities ssh finds
    are the entire credential.
    """
    assert "IdentitiesOnly" in configuration([Path("k")], True, "/run/lup/ssh")
    assert "IdentitiesOnly" not in configuration([], True, "/run/lup/ssh")


def test_a_network_that_carries_no_ssh_declines_both_ssh_rungs_before_probing(
    ssh_home: Path,
) -> None:
    """ssh reads none of the proxy variables, so a filtered session cannot use one.

    Holding a credential it cannot use is worse than holding none, because it
    reads as ready -- which is the whole reason the egress is asked before
    anything is probed.
    """
    selected = GitAccess().select({}, carries_ssh=False, home=ssh_home)

    assert selected.kind == "none"
    assert any("carries no ssh" in reason for reason in selected.declined)


def test_a_filtered_session_still_takes_the_token(ssh_home: Path) -> None:
    """HTTPS is the one transport a proxy carries, which is why it is the fallback."""
    selected = GitAccess().select(
        {"LUP_GIT_TOKEN": "tok"}, carries_ssh=False, home=ssh_home
    )

    assert selected.kind == "token"


@needs_ssh
@needs_sockets
def test_the_ladder_prefers_the_agent_to_the_keys_it_would_otherwise_copy(
    ssh_home: Path, loaded_agent: Path
) -> None:
    """Ordered by what each leaves behind rather than by what is easiest to find."""
    made_key(ssh_home / ".ssh" / "open")
    trusted(ssh_home / ".ssh", "github.com")

    selected = GitAccess().select(
        {"SSH_AUTH_SOCK": str(loaded_agent), "LUP_GIT_TOKEN": "tok"},
        carries_ssh=True,
        home=ssh_home,
    )

    assert selected.kind == "agent"


@needs_ssh
def test_a_host_running_no_agent_falls_to_the_keys_it_does_have(
    ssh_home: Path,
) -> None:
    """Which is most hosts that are not somebody's development laptop."""
    made_key(ssh_home / ".ssh" / "open")
    trusted(ssh_home / ".ssh", "github.com")

    selected = GitAccess().select({}, carries_ssh=True, home=ssh_home)

    assert selected.kind == "files"


@needs_ssh
def test_keys_that_all_need_a_passphrase_fall_through_rather_than_be_copied(
    ssh_home: Path,
) -> None:
    """Detected here rather than claimed as readiness and discovered at the push."""
    made_key(ssh_home / ".ssh" / "locked", passphrase="not-empty")
    trusted(ssh_home / ".ssh", "github.com")

    selected = GitAccess().select(
        {"LUP_GIT_TOKEN": "tok"}, carries_ssh=True, home=ssh_home
    )

    assert selected.kind == "token"


@needs_ssh
def test_a_forge_that_cannot_be_verified_falls_through_with_its_reason(
    ssh_home: Path,
) -> None:
    """Rather than accepting an unknown host, which is the check being preserved."""
    made_key(ssh_home / ".ssh" / "open")

    selected = GitAccess().select({}, carries_ssh=True, home=ssh_home)

    assert selected.kind == "none"
    assert any("no host key for github.com" in reason for reason in selected.declined)


def test_a_host_with_nothing_at_all_says_what_each_rung_wanted(ssh_home: Path) -> None:
    """Every rung names why it declined, and the reader gets all of them.

    "Configure a credential" is advice; "the agent you are running holds no
    identity" is somewhere to go.
    """
    selected = GitAccess().select({}, carries_ssh=True, home=ssh_home)

    assert selected.kind == "none"
    assert any("LUP_GIT_TOKEN is unset" in reason for reason in selected.declined)


def test_an_unreadable_home_says_that_rather_than_blaming_known_hosts(
    tmp_path: Path,
) -> None:
    """The decline a launch made from inside a session actually meets."""
    selected = GitAccess().select({}, carries_ssh=True, home=tmp_path / "no-such-home")

    assert selected.kind == "none"
    assert any("could not be read" in reason for reason in selected.declined)


@needs_ssh
def test_a_pinned_rung_is_the_only_one_walked(ssh_home: Path) -> None:
    """What an adopter reaches for when the automatic answer is right for their
    laptop and wrong for their build machine."""
    made_key(ssh_home / ".ssh" / "open")
    trusted(ssh_home / ".ssh", "github.com")

    pinned = GitAccess(source="token").select(
        {"LUP_GIT_TOKEN": "tok"}, carries_ssh=True, home=ssh_home
    )

    assert pinned.kind == "token"


def test_a_pinned_rung_that_is_unusable_degrades_rather_than_refusing_the_launch(
    ssh_home: Path,
) -> None:
    """A launch refused over a credential preference helps nobody.

    Plenty of work never touches a remote, and the degradation is reported
    with the reason the pinned rung could not be taken.
    """
    pinned = GitAccess(source="agent").select({}, carries_ssh=True, home=ssh_home)

    assert pinned.kind == "none"
    assert pinned.declined


def test_a_project_that_wants_no_credential_says_so_the_same_way(
    ssh_home: Path,
) -> None:
    """Reported as a degradation rather than as a silence, because it is one."""
    declined = GitAccess(source="none").select(
        {"LUP_GIT_TOKEN": "tok"}, carries_ssh=True, home=ssh_home
    )

    assert declined.kind == "none"
    assert any("selects no credential" in reason for reason in declined.declined)
