"""What a host's ssh identity offers a contained session, and how it travels in.

The container has no ssh identity of its own, and the operator's is the only
one that reaches their forge. Two ways in exist and they are not the same
grant. A forwarded agent socket lends the *use* of a key without lending the
key: the container asks for signatures for as long as it runs and holds
nothing afterwards. Copied key files lend the key itself, which is the
stronger grant and the only one available on a host running no agent -- which
is most hosts that are not somebody's development laptop.

Both are the operator's to give. This module's whole job is to answer
honestly whether either is actually there, because both look like readiness
from a distance and fail identically at the first push: inside a session, in
ssh's vocabulary, hours after the launch that could have said so.
``SSH_AUTH_SOCK`` is set in nearly every desktop session and points at an
agent holding no identity about as often as not; a key file that exists may
be encrypted, which without an agent is material the container can read and
cannot use. So each question is put to ssh's own tools rather than inferred
from a filename, and each answer is a fact rather than a likelihood.

**Nothing here copies the host's ``~/.ssh/config``.** It is the obvious
move and it is wrong in a way that is hard to see: that file names
``IdentityFile`` paths that do not exist inside, ``Include``s files that were
not copied, ``Match exec`` blocks that run host commands, ``ControlPath``
sockets under a directory the container does not have, and on macOS a
``UseKeychain`` no Linux ssh understands. What arrives is a configuration
that parses and then behaves differently from the one the operator tested. A
configuration *compiled* from what was actually copied cannot diverge from
it, and the one thing the host's file uniquely knows -- that ``forge:`` means
``github.com`` -- is resolved on the host and carried in as a remote rewrite
instead.

**And nothing here weakens host-key verification.** The copied
``known_hosts`` is the whole of what makes a non-interactive ssh get past
verification, so its absence is a reason to decline the credential rather
than a reason to accept an unknown host: ``StrictHostKeyChecking`` stays at
ssh's own default, and a session that cannot verify the forge falls to a
credential that does not need to.
"""

import atexit
import shutil
import stat
import tempfile
from pathlib import Path

import sh
from pydantic import BaseModel, Field

from lup.types import EnvVars


def agent_socket(environ: EnvVars, variable: str = "SSH_AUTH_SOCK") -> str:
    """The host agent's socket, when there is one worth forwarding.

    Three questions rather than one, because a set variable is worthless in
    three different ways and only the third is rare. It may name a socket
    that was removed when the agent it belonged to exited, which is what a
    reattached terminal multiplexer leaves behind. It may name a path that is
    no longer a socket at all. And it may reach an agent that is running and
    holding nothing, which is what a fresh login session has before the first
    ``ssh-add`` -- reachable, well-formed, and unable to sign anything.

    ``ssh-add -l`` separates the last two by exit status alone: zero when
    identities are loaded, one when the agent answers and holds none, two
    when nothing answers. Only zero is a credential, so both failures leave
    by the same door.
    """
    named = environ[variable] if variable in environ else ""
    if not named:
        return ""
    try:
        if not stat.S_ISSOCK(Path(named).stat().st_mode):
            return ""
    except OSError:
        return ""
    try:
        sh.Command("ssh-add")("-l", _env={**environ, variable: named})
    except (sh.CommandNotFound, sh.ErrorReturnCode):
        return ""
    return named


def key_material(path: Path) -> bool:
    """Whether ssh reads this file as a key at all, asked of ssh rather than of the name.

    Naming conventions are what a filename check would rest on, and they are
    a convention: ``id_ed25519`` is the common spelling and ``work``,
    ``gh-personal`` and ``deploy.pem`` are all ordinary. ``ssh-keygen -l``
    reads the file and reports its fingerprint, which is the same question
    asked of the thing that will have to use it.
    """
    try:
        sh.Command("ssh-keygen")("-l", "-f", str(path))
    except (sh.CommandNotFound, sh.ErrorReturnCode):
        return False
    return True


def opens_unattended(path: Path) -> bool:
    """Whether this key opens with no passphrase, which is the only way in here.

    An encrypted key reaches a container that has no agent, no terminal and
    nobody at the other end, and there is nothing there to type a passphrase
    into. Copying one is not a smaller version of working -- it is a launch
    that reported a credential and a push that fails asking for input no
    session can give. So the passphrase is offered as empty and the key is
    counted only if that opens it.
    """
    try:
        sh.Command("ssh-keygen")("-y", "-P", "", "-f", str(path))
    except (sh.CommandNotFound, sh.ErrorReturnCode):
        return False
    return True


def knows(known_hosts: Path, host: str) -> bool:
    """Whether this file already carries a host key for that name.

    Through ``ssh-keygen -F`` rather than by searching the file, because the
    file may not contain the name: ``HashKnownHosts`` is on by default on
    several distributions, and every line in such a file is an HMAC of the
    hostname rather than the hostname. A text search answers "no" for a host
    that is perfectly well known, which would decline a working credential.

    Exit status is not enough on its own -- some builds exit zero having
    found nothing -- so the answer is whether anything was reported.
    """
    try:
        found = sh.Command("ssh-keygen")("-F", host, "-f", str(known_hosts))
    except (sh.CommandNotFound, sh.ErrorReturnCode):
        return False
    return bool(str(found).strip())


class SshMaterial(BaseModel, frozen=True):
    """What a host's ssh directory offers, as opposed to what it contains."""

    keys: list[Path] = Field(
        default=[], description="Private keys that open with no passphrase"
    )
    locked: list[Path] = Field(
        default=[],
        description=(
            "Key material that needs a passphrase, so a container holding no "
            "agent and no terminal can read it and cannot use it"
        ),
    )
    known_hosts: Path | None = Field(
        default=None, description="The file carrying host keys, when there is one"
    )
    known: bool = Field(
        default=False,
        description=(
            "Whether that file carries a key for the forge, which is what "
            "decides whether a non-interactive ssh gets past verification"
        ),
    )
    reachable: bool = Field(
        default=False,
        description=(
            "Whether the ssh directory could be read at all, which separates "
            "a host holding no ssh identity from a launch whose own boundary "
            "denies reading the one it has"
        ),
    )


def host_ssh_material(home: Path, host: str) -> SshMaterial:
    """Everything in the operator's ssh directory that could be carried in.

    Nothing comes back when the directory cannot be read, which is a real
    case rather than a defensive one: the same declaration that offers these
    credentials also denies reading them to an agent, so a launch started
    from inside a session finds this directory closed and falls to a
    credential that is not behind it. That is the boundary working, and it
    reads as an ordinary absence here.

    Names ssh reserves for its own files are set aside before anything is
    probed. Not for speed -- ``known_hosts`` genuinely answers
    :func:`key_material`, since ``ssh-keygen -l`` will happily fingerprint
    every host key in it, and a file of the operator's host keys copied in as
    though it were a private key is the kind of wrong that produces no error
    anywhere.
    """
    root = home / ".ssh"
    reserved = {
        "config",
        "known_hosts",
        "known_hosts.old",
        "authorized_keys",
        "environment",
        "rc",
    }
    try:
        entries = sorted(item for item in root.iterdir() if item.is_file())
    except OSError:
        return SshMaterial()
    material = [
        item
        for item in entries
        if item.name not in reserved and item.suffix != ".pub" and key_material(item)
    ]
    opened = {item: opens_unattended(item) for item in material}
    hosts = root / "known_hosts"
    carried = hosts if hosts.is_file() else None
    return SshMaterial(
        keys=[item for item, unattended in opened.items() if unattended],
        locked=[item for item, unattended in opened.items() if not unattended],
        known_hosts=carried,
        known=carried is not None and knows(carried, host),
        reachable=True,
    )


def configuration(keys: list[Path], known_hosts: bool, inside: str) -> str:
    """The ssh configuration this home is used through, compiled from its contents.

    Every path it names is a container path, because the file is written on
    the host and read inside; a home-relative spelling would resolve against
    whichever home the session happens to run with and find nothing.

    ``IdentitiesOnly`` appears exactly when keys were copied. It is what
    stops ssh from offering every identity it can find before the one that
    works, which forges answer with ``Too many authentication failures``
    rather than with a hint -- and it is precisely wrong on the forwarded
    agent, where the identities ssh finds are the entire credential.
    """
    lines = ["Host *"]
    if keys:
        lines.append("    IdentitiesOnly yes")
        lines.extend(f"    IdentityFile {inside}/{key.name}" for key in keys)
    if known_hosts:
        lines.append(f"    UserKnownHostsFile {inside}/known_hosts")
    return "\n".join(lines) + "\n"


def ephemeral_home(material: SshMaterial, inside: str, keys: bool) -> Path | None:
    """A session-owned ssh home holding only what this launch decided to lend.

    Under the system temporary directory rather than in the checkout, the
    profile, or any generated tree, and those exclusions are the point rather
    than a preference. The checkout is what ``git clean -fdx`` walks and what
    a commit could carry; a profile is what the next launch reuses, so a copy
    left there outlives the session it was made for and every session after
    it. This is removed when the launcher exits, which is when the session
    it was made for ends.

    ``keys`` is false for the forwarded agent, which needs the compiled
    configuration and the host keys and must not be handed private key files
    it did not ask for: the whole difference between lending the use of a key
    and lending the key is that the second one leaves a copy.

    Modes are stated rather than inherited, so the copy's mode is a property
    of this function rather than of whatever the operator's directory
    happened to have. What that guard is *not* is the defence against a
    world-readable key, which was the first reason written down for it and is
    wrong: ssh-keygen refuses to read such a key at all --
    ``UNPROTECTED PRIVATE KEY FILE`` -- so one never reaches here. It fails
    :func:`opens_unattended` and is classified beside the encrypted keys,
    which is the honest place for it, since it is equally unusable on the
    host. The mode here answers the narrower thing: a copy landing under a
    umask or on a filesystem that carried no mode across.
    """
    try:
        directory = Path(tempfile.mkdtemp(prefix="lup-ssh-"))
    except OSError:
        return None
    atexit.register(shutil.rmtree, directory, True)
    carried = material.keys if keys else []
    for key in carried:
        copied = directory / key.name
        shutil.copy2(key, copied)
        copied.chmod(0o600)
        public = key.parent / f"{key.name}.pub"
        if public.is_file():
            shutil.copy2(public, directory / public.name)
    if material.known_hosts is not None:
        hosts = directory / "known_hosts"
        shutil.copy2(material.known_hosts, hosts)
        hosts.chmod(0o600)
    config = directory / "config"
    config.write_text(
        configuration(carried, material.known_hosts is not None, inside),
        encoding="utf-8",
    )
    config.chmod(0o600)
    return directory
