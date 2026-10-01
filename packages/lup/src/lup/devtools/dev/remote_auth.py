"""Remote-auth probing: what can this session actually do at the origin remote?

**Two questions, and they have different answers.** Whether git can reach the
remote is :func:`check_remote_auth`: ssh-style remotes are probed by having
git reach them, https remotes via ``gh auth status``, and local or
unrecognized remotes pass. Whether the forge
client can authenticate is :func:`check_forge_api`, which asks ``gh`` however
the remote happens to be spelled.

Conflating them is how a pull request fails in the client's own words with
nothing saying which credential was missing. On an ssh remote the transport
probe answers for a key and never asks about the API -- and a forwarded agent
carries a fetch perfectly while holding no token at all, because a token is
a separate credential that travels separately. So a session can push all day
and be unable to open the request describing what it pushed. Anything reading
or writing pull requests, issues or checks gates on :func:`check_forge_api`;
anything that only moves commits gates on :func:`check_remote_auth`.

Every probe answers with a :class:`RemoteRefusal` rather than printing one,
because two readers ask this and want different things back. A command
reporting to a person wants to know whether an operation needing the remote
can go ahead at all; the base-freshness reading wants words to put in front
of a fetch that just failed, and only a declined credential is worth putting
there -- git's own stderr says which key it was refused for and never which
one to load, but a probe that cannot reach the host has learned nothing git
did not already say better.
"""

import os
import shlex
from urllib.parse import urlparse

import sh
import typer
from pydantic import BaseModel

from lup.devtools.utils import decode_stderr, gh
from lup.execution.shell import git
from lup.harness.environment import inherited
from lup.harness.credential import GitAccess


class RemoteRef(BaseModel):
    """A git remote reduced to what auth probing needs."""

    scheme: str
    destination: str


class RemoteRefusal(BaseModel, frozen=True):
    """What probing one remote's credential found, empty when it answered."""

    complaint: str = ""
    """What to tell a reader; empty means the credential answered."""

    credential: bool = False
    """Whether the host declined a credential, rather than never being reached."""

    def diagnoses(self) -> str:
        """The words worth putting in front of a failed fetch, empty when none are.

        A declined credential is the one finding that beats git's own
        account, because git names the key it was refused for and never the
        one to load. Anything else loses to it: this probe runs a second
        command, and a second command can disagree with the first about
        whether a host is even reachable -- so where it has not positively
        identified the credential, what the fetch itself said stands.
        """
        return self.complaint if self.credential else ""


def parse_remote(remote_url: str) -> RemoteRef | None:
    """Parse a git remote into (scheme, destination) for auth probing.

    URL forms (https://host/org/repo, ssh://git@host/org/repo) carry ``://``
    and are parsed with urllib. Anything else is git's SCP form
    ``[user@]host:path``, equivalent to ``ssh://[user@]host/path`` and
    detected by a colon preceding the first slash; it is normalized to
    ``ssh://`` and parsed the same way. Returns None for local paths and
    unrecognized remotes.

    The presence of ``://`` decides, rather than whether urllib finds a
    scheme, because it finds one either way: ``forge:org/repo`` -- an ssh
    alias, which is what a remote looks like once a person has an
    ``~/.ssh/config`` -- parses as the scheme ``forge`` with no host, and a
    remote nothing recognizes is a remote nothing checks.

    :func:`lup.devtools.utils.slug_from_remote` reads the other half of the
    same string for the same reason. It keeps what follows the colon and
    discards the host, because `gh` needs the repository; this needs the
    host and discards the rest, because ssh needs a destination. Neither can
    be written in terms of the other, so what they share is the shape they
    both have to know about.
    """
    if "://" not in remote_url:
        colon, slash = remote_url.find(":"), remote_url.find("/")
        # A one-letter head is a Windows drive rather than a host, and
        # `C:/src/repo` is a path git clones from, not a machine it reaches.
        if colon < 2 or (slash != -1 and slash < colon):
            return None
        remote_url = "ssh://" + remote_url[:colon] + "/" + remote_url[colon + 1 :]
    parsed = urlparse(remote_url)
    if parsed.scheme and parsed.hostname:
        user_prefix = f"{parsed.username}@" if parsed.username else ""
        return RemoteRef(
            scheme=parsed.scheme, destination=f"{user_prefix}{parsed.hostname}"
        )
    return None


def git_ssh_program() -> str:
    """The ssh command line git runs for a remote, with non-interactivity added.

    Git speaks ssh through ``GIT_SSH_COMMAND``, falling back to
    ``core.sshCommand`` and then to whatever ``ssh`` the path holds. A
    sandbox that supplies its own ``known_hosts`` sets one of them, so a
    probe spelling ``ssh`` on its own reads a configuration git never reads
    and fails host key verification against a remote git reaches perfectly.
    That is a false negative on the only question the probe was asked, and
    the readers downstream spend it as a fact: the sweep gates its pull
    request lookup and its remote branch listing on this answer, so a probe
    that cannot reach the host reports every branch as carrying no request
    and the remote as carrying no branches -- two claims nothing checked.

    Batch mode and a connect timeout are appended to the configured command
    rather than put in place of it, so the probe cannot stop on a prompt
    while keeping the options that let it connect at all.
    """
    # lup: ignore[os-environ, dict-get] — git's transport setting, read where git
    # reads it, out of an environment whose keys are the machine's and not a schema
    configured = os.environ.get("GIT_SSH_COMMAND", "") or git.out(
        "config", "--get", "core.sshCommand", _ok_code=[0, 1]
    )
    return f"{configured or 'ssh'} -o BatchMode=yes -o ConnectTimeout=5"


def ssh_auth_refusal(destination: str, remote_url: str) -> RemoteRefusal:
    """Probe the transport by having git reach the remote, the way git reaches it.

    Asked through ``git ls-remote`` rather than through a hand-built ``ssh``,
    because git is what the question is about. It applies the ``insteadOf``
    rewrites, reads the ssh configuration this session was actually given,
    and refuses exactly where a fetch would refuse; a probe spelling its own
    ``ssh`` answers for a session nobody is running.

    A key the host would not accept is reported with the identity ssh itself
    says it would offer this destination, so the reply names the key to load
    rather than leaving that to whoever reads it -- asked with the same
    configuration git just used, since a different one offers a different
    key and a confident answer naming the wrong one is worse than none.

    Anything else is a complaint that does not claim to be about a
    credential. A host that was never reached has no key to load, and a
    machine on a train told to run ``ssh-add`` has been sent after the wrong
    thing by a message that sounds certain -- so what separates the two is
    ssh's refusal wording, which is the only place either of them says which
    one happened.
    """
    program = git_ssh_program()
    reaching = inherited({"GIT_SSH_COMMAND": program})
    try:
        git("ls-remote", "--quiet", remote_url, "HEAD", _ok_code=[0], _env=reaching)
        return RemoteRefusal()
    except sh.ErrorReturnCode as refusal:
        said = decode_stderr(refusal)
        # OpenSSH's wording for every credential it declines, whatever the
        # method named in the parenthesis after it. No structured form of
        # this exists to read instead: the exit status is 255 for a declined
        # key and 255 for a host that never answered, and git hands both
        # back with ssh's own stderr intact.
        if "Permission denied" not in said:
            return RemoteRefusal(complaint=said)

        def offered_identity() -> str:
            """The key ssh says it would offer here, empty where it will not say."""
            words = shlex.split(program)  # lup: ignore[string-split] — shell lexer
            try:
                asked = sh.Command(words[0])(
                    *words[1:], "-G", destination, _ok_code=[0], _tty_out=False
                )
            except (sh.CommandNotFound, sh.ErrorReturnCode):
                return ""
            for line in str(asked).splitlines():
                if line.startswith("identityfile "):
                    return line.removeprefix("identityfile ")
            return ""

        identity = offered_identity()
        complaint = f"SSH authentication failed for remote '{remote_url}'."
        if identity:
            complaint += f"\nLoad the key with:  ssh-add {identity}"
        return RemoteRefusal(complaint=complaint, credential=True)


def gh_auth_refusal(remote_url: str) -> RemoteRefusal:
    """Whether the forge client holds a credential, whatever transport git speaks.

    Named for the client rather than for a scheme, because the client is what
    it asks about. It answers the https transport question as a side effect --
    a remote git reaches over https is reached on this same credential -- and
    reading it as *the https probe* is exactly what kept it off every ssh
    remote, where it is the only probe that would have said anything.

    The remediation names both places a credential comes from. A session that
    cannot reach the API is as likely to be a contained one launched without a
    token as a host nobody ever signed in on, and only one of those is fixed
    by signing in.
    """
    try:
        gh("auth", "status", _ok_code=[0])
        return RemoteRefusal()
    except (sh.ErrorReturnCode, sh.CommandNotFound):
        return RemoteRefusal(
            complaint=(
                f"The forge API is unauthenticated for '{remote_url}', so "
                "pull requests, issues and checks fail at the API rather "
                "than here.\n"
                "  On a host:  gh auth login\n"
                "  In a contained session the token travels from the "
                f"launching shell: export {GitAccess().token_variable} there, "
                "or declare `token_source='forge-login'` so the launcher "
                "carries the host's own login in."
            ),
            credential=True,
        )


def remote_auth_refusal(remote_url: str) -> RemoteRefusal:
    """Why this remote's credential would refuse, empty when it answers.

    A remote nothing here recognizes answers empty rather than guessing: a
    local path has no credential to fail, and a scheme this does not probe
    is one whose refusal it could not describe.
    """
    remote = parse_remote(remote_url)
    if remote is None:
        return RemoteRefusal()
    match remote.scheme:
        case "ssh" | "git":
            return ssh_auth_refusal(remote.destination, remote_url)
        case "http" | "https":
            return gh_auth_refusal(remote_url)
        case _:
            return RemoteRefusal()


def origin_auth_complaint() -> str:
    """Why the origin remote would refuse this checkout, empty when it answers.

    The sentence rather than the verdict, for a caller that has to carry the
    reason somewhere a message on stderr does not reach — a structured
    result whose reader is another command.
    """
    return remote_auth_refusal(git.out("remote", "get-url", "origin")).complaint


def check_remote_auth() -> bool:
    """Verify auth for the origin remote. Returns True if remote ops can proceed.

    ssh-style remotes are probed by having git reach them; https remotes are
    verified via ``gh auth status``. Local and unrecognized remotes pass.

    Every complaint counts here, not only a declined credential: this asks
    whether an operation needing the remote can go ahead, and one that never
    reaches the host cannot either.
    """
    complaint = origin_auth_complaint()
    if complaint:
        typer.echo(complaint, err=True)
    return not complaint


def check_forge_api() -> bool:
    """Verify the forge client can authenticate. True if API operations may proceed.

    Asked of the client rather than of the remote, so the answer does not
    change with how the remote is spelled. That independence is the point:
    the transport probe already covers the spelling, and what it cannot cover
    is the credential the API needs, which no remote URL mentions.
    """
    refusal = gh_auth_refusal(git.out("remote", "get-url", "origin"))
    if refusal.complaint:
        typer.echo(refusal.complaint, err=True)
    return not refusal.complaint
