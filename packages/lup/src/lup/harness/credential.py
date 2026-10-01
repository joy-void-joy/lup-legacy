"""Reaching the forge from inside the boundary, on a credential worth the reach.

A contained session has no identity of its own, so every remote it touches is
reached on something the operator lent it. Which thing is lent is the whole
of this module, and it is a ladder rather than a policy: a forwarded ssh
agent, then an ephemeral copy of the host's ssh material, then a forge token
over HTTPS, then nothing and the public half of the internet.

**The ladder is ordered by what is left behind, not by convenience.** A
forwarded agent lends the *use* of a key for the length of one container and
leaves no copy; an ephemeral home lends the key itself and removes it when
the launcher exits; a token is a separate credential the operator scopes
themselves. Each rung is taken only when it is *verified usable* -- an agent
holding an identity, a key that opens with no passphrase, a host key already
in ``known_hosts`` -- because the failure mode this exists to prevent is a
launch that reports a credential and a push that fails hours later in ssh's
vocabulary rather than the boundary's.

**What the ssh rungs cost is real and is not hidden.** An agent socket in a
container can sign anything that key can sign, for every host it reaches;
copied key files are the key. The operator grants that deliberately, and the
denials this repository keeps on ``~/.ssh`` remain what they always were --
defense in depth against an agent *reading* key material, not isolation from
``ssh`` and ``git`` using it. ``docs/permissions.md`` says so in those words
rather than implying a stronger boundary than exists.

**Where the rewrite is computed is load-bearing.** Remote spellings are
unbounded: ``git@host:``, ``ssh://``, an ssh config alias, an ``insteadOf``
already in play, a non-GitHub host entirely. So the rewrite cannot match on
spelling -- it has to resolve each remote to a URL and rewrite by resolved
host. But resolving means reading ``.git/config``, which carries
``core.sshCommand``, ``diff.external`` and ``url.*.insteadOf`` pointing at
``ext::sh -c`` -- the entries naming what reaching a remote runs, which is
why the boundary holds the shared ``config`` read-only. A rewrite resolved
inside would still be the confined thing's to compute, and a container that
could decide its own rewrite could decide to have none. The resolution
happens on the host, before the container starts, and arrives as
configuration the container cannot reach behind.

**Which direction it rewrites is the selected credential's to say.** A token
reaches HTTPS and nothing else, so ssh spellings are rewritten toward HTTPS;
an ssh credential reaches ssh, so HTTPS spellings and config aliases are
rewritten toward ``git@host:``. One transport per session, decided once,
outside. The alternative -- leaving each remote on whatever it was spelled as
-- is a checkout where half the remotes work.

**And the configuration arrives in the environment, not in a file.** Git
reads ``GIT_CONFIG_COUNT`` and its numbered pairs at the highest precedence,
above every file -- so nothing inside the checkout can override what the
launcher decided, and nothing has to be written into a tree the agent can
edit. A file would be both.

**The agent can read whatever it is lent.** That is not a leak this could
close: a session that can run ``git push`` with a credential can also read
it, and hiding it would be theatre. The scope is the boundary, not the
secrecy -- which is why the scope is the part worth getting right, and why
nothing lent lives in the checkout, in a profile, or in the argv of the
command that starts the container.
"""

from collections.abc import Iterator
from pathlib import Path, PurePosixPath
from typing import Annotated, Literal
from urllib.parse import urlsplit

import sh
from pydantic import BaseModel, Discriminator, Field

from lup.execution.git import Repository
from lup.devtools.utils import gh
from lup.execution.shell import git
from lup.harness.environment import NON_INTERACTIVE_SHELL_ENV
from lup.harness.notice import Notice
from lup.harness.ssh import (
    SshMaterial,
    agent_socket,
    ephemeral_home,
    host_ssh_material,
)
from lup.types import EnvVars


class RemoteAddress(BaseModel, frozen=True):
    """One remote URL taken apart the way git's own grammar takes it apart.

    Four fields, because four different questions get asked of a remote and
    matching on its spelling answers none of them reliably. ``prefix`` is what
    an ``insteadOf`` rewrite has to match, character for character. ``host`` is
    what the URL *names*, which may be an ssh config alias rather than a
    hostname. ``proxied`` is whether the transport survives an HTTP proxy as
    written -- which is the question that decides whether a filtered
    container can reach it at all, since such a container's only way out
    speaks HTTP and HTTPS and nothing else. ``repository`` is which repository
    the URL names, which is the one question whose answer must not change
    when the transport does: the same repository is reached over https here
    and over ssh there, and a comparison made on the spelling calls those two
    different.
    """

    prefix: str = Field(description="What an insteadOf rewrite must match")
    host: str = Field(description="What the URL names, possibly an ssh alias")
    proxied: bool = Field(
        description="Whether this transport can cross an HTTP proxy as written"
    )
    repository: str = Field(
        description="Which repository the URL names, without its `.git` suffix"
    )


class GitSetting(BaseModel, frozen=True):
    """One git configuration key and what it is set to."""

    key: str = Field(description="The configuration key, e.g. `commit.gpgsign`")
    value: str = Field(description="What it is set to")


class RemoteRewrite(BaseModel, frozen=True):
    """One spelling a remote uses, and the prefix standing in for it."""

    spelling: str = Field(description="The prefix a remote URL begins with")
    target: str = Field(description="What git should reach instead")

    def setting(self) -> GitSetting:
        """This rewrite as the configuration git reads it out of."""
        return GitSetting(key=f"url.{self.target}.insteadOf", value=self.spelling)


class HttpsTransport(BaseModel, frozen=True):
    """What a token reaches the forge over, and the only thing a proxy carries."""

    kind: Literal["https"] = "https"

    def prefix(self, host: str) -> str:
        """The URL prefix every remote on this transport begins with."""
        return f"https://{host}/"

    def carries(self, address: RemoteAddress, host: str) -> bool:
        """Whether this address already travels here, port and user included.

        Asked as "does it cross a proxy" rather than "does it start with
        https", because that is the property being relied on and because a
        remote that already crosses one must be left exactly as written --
        rewriting it to a bare prefix would drop a port or a user somebody
        put there on purpose.
        """
        return address.proxied


class SshTransport(BaseModel, frozen=True):
    """What a key or an agent reaches the forge over, once the network carries it."""

    kind: Literal["ssh"] = "ssh"
    user: str = Field(
        default="git",
        description=(
            "Who a forge expects an ssh connection as. Every forge that "
            "serves git over ssh multiplexes all of its users onto one "
            "account and tells them apart by key -- `git` on GitHub, GitLab "
            "and Gitea alike -- so this is the forge's own name for that "
            "account rather than the operator's"
        ),
    )

    def prefix(self, host: str) -> str:
        """The scp-like prefix every remote on this transport begins with."""
        return f"{self.user}@{host}:"

    def carries(self, address: RemoteAddress, host: str) -> bool:
        """Whether this address is already spelled exactly the way this rewrites to.

        Character equality rather than "is it ssh", because the spellings
        this has to rewrite *are* ssh: a config alias reaches the forge over
        ssh and names something no container can resolve, and an
        ``ssh://`` URL reaches it over ssh under a spelling that carries a
        user this session may not connect as.
        """
        return address.prefix == self.prefix(host)


type ForgeTransport = Annotated[HttpsTransport | SshTransport, Discriminator("kind")]
"""The one transport a session's remotes are rewritten onto."""


class GitIdentity(BaseModel, frozen=True):
    """Who a commit made inside the boundary is authored as.

    Read off the host checkout and passed in, for the same reason the remote
    rewrites are: resolved in there, an identity is one the confined thing
    chose for itself, and one it would have to write into the shared
    `config` that the boundary holds read-only.

    Sent at all because git has no usable fallback. With nothing configured
    it assembles one from the container's hostname and then refuses to use
    it -- `Author identity unknown ... got 'agent@9c9dff017051.(none)'` --
    which stops every commit and names a machine that exists for the length
    of one session.

    Authorship rather than a claim about who wrote the code. The signing
    member is what makes that claim, and declines to; a `Co-Authored-By`
    trailer is what records the agent. A commit authored as the operator is
    the same arrangement an editor has always had -- the person the work is
    being done for.
    """

    name: str = Field(description="The author name a contained commit carries")
    email: str = Field(description="The author address a contained commit carries")

    def environment(self) -> EnvVars:
        """The author and the committer, as the variables git ranks above config.

        Variables rather than `user.*` keys, because git consults them before
        any configuration at all: a session never has a reason to write
        `user.name` into the read-only shared `config`, and a `-c` or an
        include cannot re-author what it commits.
        """
        return {
            "GIT_AUTHOR_NAME": self.name,
            "GIT_AUTHOR_EMAIL": self.email,
            "GIT_COMMITTER_NAME": self.name,
            "GIT_COMMITTER_EMAIL": self.email,
        }


def committer(root: Path) -> GitIdentity | None:
    """The identity this checkout commits under, as git itself resolves it.

    Asked of `git config` rather than read out of a file, for the reason
    :func:`resolved_host` is asked of ssh: the answer is assembled from the
    system, global, local and worktree scopes in a precedence only git
    implements, and an adopter who set theirs per repository would be missed
    by anything reading one file.

    Nothing comes back unless both halves are there. Git needs both, and a
    name with no address refuses exactly as an absent one does -- so half an
    identity would be a launch that reported success and a commit that
    failed anyway.
    """

    def configured(key: str) -> str:
        try:
            return git.out("-C", str(root), "config", "--get", key).strip()
        except sh.ErrorReturnCode:
            return ""

    name, email = configured("user.name"), configured("user.email")
    return GitIdentity(name=name, email=email) if name and email else None


class SigningOff(BaseModel, frozen=True):
    """Agent commits are not signed, and the launch says so in one line.

    The default, and the honest one. A signature claims that a particular
    human vouched for a particular commit; an agent commit is not that, and
    signing it with the operator's key would make the signature assert
    something untrue.

    What it costs is real: a branch protection rule requiring signed commits
    fails on agent branches -- as a visible check, at push time, rather than
    as `gpg: signing failed` in the middle of a commit the agent then debugs
    as a GPG problem. That is a fact about a repository's rules rather than
    about this launch, so it lives in `docs/permissions.md` and the launch
    says only which posture is in force.
    """

    kind: Literal["off"] = "off"

    def configuration(self) -> list[GitSetting]:
        """Turn signing off inside the container, whatever the checkout says."""
        return [
            GitSetting(key="commit.gpgsign", value="false"),
            GitSetting(key="tag.gpgsign", value="false"),
        ]

    def notice(self) -> list[Notice]:
        """A capability the session does not have, which is not a fault."""
        return [
            Notice(
                text=(
                    "Commit signing: off — agent commits do not use your signing identity."
                ),
                urgency="boundary",
            ),
        ]


class AgentKey(BaseModel, frozen=True):
    """Agent commits sign, as the agent rather than as the operator.

    An ssh key held inside the container, which git has been able to sign
    with since 2.34 -- no GPG agent, no hardware token, nothing of the
    operator's reachable. The signature then says something true: a commit
    made by an agent in this container.

    What it does not buy is a green badge, and saying so is the point.
    Verification needs the public half registered somewhere: an
    ``allowed_signers`` file committed to the repository makes
    ``git log --show-signature`` verify it locally under a distinct identity,
    which is honest; registering it on the operator's forge account instead
    would render agent commits as *theirs*, which is the untrue claim this
    member exists to avoid. A bot account or a forge app is the
    honest-and-green answer, and is real per-adopter setup.
    """

    kind: Literal["agent_key"] = "agent_key"
    path: str = Field(
        default="/cfg/agent-signing",
        description=(
            "Where the key lives, inside the config home so it outlives one "
            "container and never reaches the host's own key material"
        ),
    )
    identity: str = Field(
        default="agent@lup.local",
        description="Who the signature says made the commit, which is not the operator",
    )

    def configuration(self) -> list[GitSetting]:
        """Sign with an ssh key, which needs no agent and no hardware."""
        return [
            GitSetting(key="gpg.format", value="ssh"),
            GitSetting(key="user.signingkey", value=f"{self.path}.pub"),
            GitSetting(key="commit.gpgsign", value="true"),
        ]

    def notice(self) -> list[Notice]:
        """A capability the session has, said in the terms it is true in."""
        return [
            Notice(
                text=(
                    f"Commit signing: on as {self.identity}, with a key held in this "
                    "container, which a forge shows as unverified."
                ),
                urgency="boundary",
            )
        ]


class InheritedSigning(BaseModel, frozen=True):
    """Whatever the checkout's own configuration says, and what that will cost.

    Kept expressible because an adopter may have arranged for a key to be
    reachable inside the container and this policy has no business overruling
    them. Kept loud because the usual outcome is the failure the whole
    manifest exists to prevent: ``commit.gpgsign=true`` in a repository whose
    key is on the host, discovered as ``gpg: signing failed: No secret key``
    in the middle of a commit, and debugged as a GPG problem for as long as
    it takes somebody to remember there is a boundary.
    """

    kind: Literal["inherit"] = "inherit"

    def configuration(self) -> list[GitSetting]:
        """Nothing. The checkout's configuration decides, for better or worse."""
        return []

    def notice(self) -> list[Notice]:
        """The loud one, because its usual outcome is a mid-commit failure."""
        return [
            Notice(
                text=(
                    "Commit signing: inherited from this checkout — a key that lives "
                    "on the host fails mid-commit as `gpg: signing failed`, "
                    "which is a boundary, not a GPG fault."
                ),
                urgency="warning",
            )
        ]


type CommitSigning = Annotated[
    SigningOff | AgentKey | InheritedSigning, Discriminator("kind")
]
"""What a commit made inside the boundary claims, if anything."""


def parse_remote(url: str) -> RemoteAddress | None:
    """Take a remote URL apart, or decline when it is not one this can rewrite.

    Git accepts three shapes and only one of them is a URL that a parser in
    the standard library reads. ``scheme://[user@]host/path`` is; the
    scp-like ``[user@]host:path`` is not, and neither is a bare local path.
    Measured rather than assumed: ``urlsplit`` reads ``forge:owner/repo.git``
    as a URL whose *scheme* is ``forge``, and reads ``git@github.com:owner/repo``
    as a path with no scheme at all -- so it cannot even be used to tell the
    two apart, let alone to take either one apart correctly.

    The obvious PyPI parser was tried and refuted for this job. ``giturlparse``
    does handle scp-like syntax, but validates against a roster of known
    forges, so it answers ``valid: False`` for a URL spelled through an ssh
    config alias -- which is precisely the case this exists for.

    A local path yields nothing, which is right: there is no transport to
    rewrite, and a checkout whose remote is a directory keeps working
    unchanged inside the container as long as that directory is mounted.
    """

    def named(path: str) -> str:
        """The repository one remote's path names, however that path is spelled.

        Through ``PurePosixPath`` rather than by trimming characters: a
        leading slash, a trailing one and a doubled separator are all the
        same path to git, and the suffix is optional on every forge that
        serves one, so ``/owner/repo.git`` and ``owner/repo`` name the same
        repository and have to compare equal.
        """
        posix = PurePosixPath(path)
        relative = posix.relative_to("/") if posix.is_absolute() else posix
        return str(relative).removesuffix(".git")

    if "://" in url:
        split = urlsplit(url)
        return RemoteAddress(
            prefix=f"{split.scheme}://{split.netloc}/",
            # The host without the port and without the user, because that is
            # what ssh resolves and what a forge is compared against.
            host=split.hostname or "",
            proxied=split.scheme == "https",
            repository=named(split.path),
        )
    colon = url.find(":")
    slash = url.find("/")
    if colon < 0 or (0 <= slash < colon):
        # A bare path. It names no transport, so there is nothing to rewrite.
        return None
    # lup: ignore[string-split] — git's scp-like remote syntax, which no
    # parser in the standard library reads and which `urlsplit` actively
    # misreads; both measurements are in this function's docstring
    authority, _, path = url.partition(":")
    # lup: ignore[string-split] — the same syntax one field further in: an
    # optional user sits before an `@`, which a hostname may not contain
    _user, _, hostname = authority.rpartition("@")
    return RemoteAddress(
        prefix=f"{authority}:",
        host=hostname or authority,
        proxied=False,
        repository=named(path),
    )


def resolved_host(alias: str) -> str:
    """What ssh says a name actually reaches, which may not be the name itself.

    An ssh config alias is a hostname only ssh knows: ``forge`` in a remote
    reaches ``github.com`` because ``~/.ssh/config`` says so, and nothing in
    the URL records it. Comparing the written name against a forge would
    therefore miss every remote spelled through an alias -- which is what any
    checkout looks like once its author keeps an ssh config, and is exactly
    the case a list of prefixes silently failed.

    Asked of ssh rather than read out of its configuration file, because the
    file is not the whole answer: ``Include`` directives, ``Match`` blocks, a
    system-wide config and per-user overrides all contribute, and ssh is the
    only thing that composes them. ``ssh -G`` is the documented way to ask.

    Falls back to the name as written when ssh cannot answer. That is the safe
    direction: an unresolved alias fails to match the forge, produces no
    rewrite, and leaves the remote exactly as it was outside the container.
    """
    try:
        reported = sh.Command("ssh")("-G", alias)
    except (sh.CommandNotFound, sh.ErrorReturnCode):
        return alias
    for line in str(reported).splitlines():
        if line.startswith("hostname "):
            return line.removeprefix("hostname ").strip() or alias
    return alias


def same_repository(left: str, right: str) -> bool:
    """Whether two remote URLs name one repository, over whatever transport each takes.

    Separating the two questions a URL answers. *Which repository* is what a
    registration means and what two of them have to agree on; *how this
    machine reaches it* is a fact about keys, a proxy and an ssh config, and
    it differs between two checkouts of the same repository as a matter of
    course. A committed registration naming the https URL while this machine
    clones over ssh is therefore the ordinary case and not a disagreement, so
    the comparison reads what each URL names -- the host, and the path with
    its optional `.git` gone -- and never the spelling that reaches it.

    Hosts compare case-insensitively because DNS does, and paths do not,
    because a git server's paths are a filesystem's. An ssh config alias is
    resolved before two hosts are called different, which is the one case
    where the host as written is not the host: `forge:owner/repo` reaches
    github.com because `~/.ssh/config` says so, and nothing in the URL
    records it. Resolved only once the two disagree as written, so an
    ordinary comparison spends no subprocess.

    A spelling neither side parses as a remote is a local path, and two
    spellings of one directory are one repository -- which is what a test
    fixture, a mirror on a workstation and a bare clone beside its worktrees
    all look like. A local path against a URL is not the same repository:
    there is nothing in either to say the directory was cloned from the forge.
    """

    def directory(spelling: str) -> Path:
        """One local spelling as the single directory it names."""
        return Path(spelling).expanduser().resolve()

    def reaches(first: RemoteAddress, second: RemoteAddress) -> bool:
        """Whether two addresses name one host, resolving an alias if they differ."""
        if first.host.lower() == second.host.lower():
            return True
        return resolved_host(first.host).lower() == resolved_host(second.host).lower()

    match (parse_remote(left), parse_remote(right)):
        case (None, None):
            return directory(left) == directory(right)
        case (RemoteAddress() as first, RemoteAddress() as second):
            return first.repository == second.repository and reaches(first, second)
        case _:
            return False


def remote_rewrites(
    root: Path, host: str, transport: ForgeTransport
) -> list[RemoteRewrite]:
    """Each spelling this checkout's remotes use that the session cannot reach.

    Resolved here, on the host, and never inside: a rewrite computed in there
    is one the confined thing chose, even over a ``.git/config`` the boundary
    holds read-only.

    Decided on the *resolved* host rather than on the URL's spelling, which
    is the whole of what remote spellings being unbounded meant. A first
    draft matched three prefixes and produced no rewrite at all for a remote
    spelled `forge:owner/repo.git` -- an ssh config alias, which no prefix
    list can recognize and only ssh can resolve. The container would have
    kept a remote it has no way to reach, and the push would have failed in
    the transport's vocabulary rather than in the boundary's.

    ``transport`` is the selected credential's answer to what this session
    can actually reach, so the same walk serves both directions: toward
    HTTPS for a token, toward ``git@host:`` for a key or an agent.
    """
    try:
        names = git.lines("-C", str(root), "remote")
    except sh.ErrorReturnCode:
        return []
    addresses = [
        address
        for name in names
        if name
        for address in [parse_remote(Repository(root).remote_url(name) or "")]
        if address is not None and not transport.carries(address, host)
    ]
    found = {
        address.prefix: RemoteRewrite(
            spelling=address.prefix, target=transport.prefix(host)
        )
        for address in addresses
        if resolved_host(address.host) == host
    }
    return list(found.values())


def fleet_rewrites(
    roots: list[Path], host: str, transport: ForgeTransport
) -> list[RemoteRewrite]:
    """Every spelling the checkouts this session can open use and cannot reach.

    One walk per root rather than one over the session's own checkout, because
    what a session can open is not a single repository. A mounted
    project is one a session commits and pushes in, and its remotes are
    spelled however its own author spells them -- an ssh config alias, an
    HTTPS URL, a `git@` address -- none of which the checkout that launched
    the session has to use. A table built from that checkout alone leaves the
    mounted one addressed exactly as it was outside, on a transport this
    session may hold no credential for, and the push then fails in the
    transport's vocabulary rather than in the boundary's.

    Settled by spelling across all of them, because the settings this becomes
    are global to the container: git holds one `insteadOf` per spelling, and
    two roots reaching the same forge by the same name are one rewrite.
    """
    return list(
        {
            rewrite.spelling: rewrite
            for root in roots
            for rewrite in remote_rewrites(root, host, transport)
        }.values()
    )


def contained_ssh(home_inside: str) -> str:
    """The ssh git runs inside, reading the configuration compiled for this launch.

    ``GIT_SSH_COMMAND`` rather than ``core.sshCommand``, which is what this
    first reached for and what git would have ignored. The variable takes
    precedence over the setting, and the image bakes one already -- so a
    session would have started with every git setting correctly in place,
    run ``ssh -o BatchMode=yes`` with no ``-F`` at all, never opened the
    configuration built for it, and failed on a key it was holding.

    Measured against a launch's real argv rather than reasoned about. The
    unit tests all passed: they asked whether the setting was present, and
    it was. What they could not see is the variable sitting beside it in the
    same argv, three hundred lines away in a different module, winning.

    Composed onto the baked value rather than replacing it, and read from the
    one place that value is declared. ``BatchMode=yes`` is what stops ssh
    prompting in a session with nobody at the other end, so dropping it to
    add a config file would trade a silent failure for a hanging one.
    """
    return f"{NON_INTERACTIVE_SHELL_ENV['GIT_SSH_COMMAND']} -F {home_inside}/config"


class AgentSocket(BaseModel, frozen=True):
    """The host's ssh agent, lent for the length of one container.

    The narrowest of the ssh rungs and the only one that leaves nothing
    behind: the container holds a socket rather than a key, so what it can do
    ends when the socket goes. What it can do *while* it holds it is not
    narrow at all -- every signature that key can make, for every host it
    reaches -- and the operator grants exactly that.
    """

    kind: Literal["agent"] = "agent"
    socket: str = Field(description="The host socket this forwards")
    inside: str = Field(description="Where that socket appears in the container")
    home: str = Field(
        description=(
            "The session-owned ssh home this reads its configuration and host "
            "keys from, holding no private key of any kind"
        )
    )
    home_inside: str = Field(description="Where that home is mounted")

    def mount_arguments(self) -> list[str]:
        """The socket writable and the home read-only, which is not symmetry.

        Connecting to a unix socket needs write permission on it, so a
        read-only mount there is a forwarded agent that refuses every
        signature -- the one failure that looks exactly like a wrong key. The
        home beside it is read: nothing inside has business editing the
        configuration this launch compiled for it.
        """
        return [
            "-v",
            f"{self.socket}:{self.inside}",
            "-v",
            f"{self.home}:{self.home_inside}:ro",
        ]

    def environment(self) -> EnvVars:
        """Where ssh looks for the agent, and which configuration git's ssh reads."""
        return {
            "SSH_AUTH_SOCK": self.inside,
            "GIT_SSH_COMMAND": contained_ssh(self.home_inside),
        }

    def configuration(self) -> list[GitSetting]:
        """Nothing: the handover is a variable, for a reason spelled at :func:`contained_ssh`."""
        return []

    def transport(self, user: str) -> ForgeTransport:
        """Every forge remote is spelled as ssh, because ssh is what this reaches."""
        return SshTransport(user=user)

    def notice(self) -> list[Notice]:
        """One line. Which credential was selected is the whole of what a reader needs."""
        return [
            Notice(
                text=(
                    "Forge access: this session may authenticate through your "
                    "forwarded SSH agent."
                ),
                urgency="boundary",
            )
        ]


class EphemeralKeys(BaseModel, frozen=True):
    """A copy of the host's usable ssh keys, made for one launch and removed after it.

    The rung for a host running no agent, which is most hosts. It is a
    stronger grant than the socket and it is bounded the only way a copy can
    be: the copy lives outside the checkout, outside every profile and
    outside every generated tree, and the launcher deletes it on the way out.
    """

    kind: Literal["files"] = "files"
    home: str = Field(description="The session-owned ssh home holding the copies")
    home_inside: str = Field(description="Where that home is mounted")

    def mount_arguments(self) -> list[str]:
        """Read-only: a key the session could rewrite is a key it could replace."""
        return ["-v", f"{self.home}:{self.home_inside}:ro"]

    def environment(self) -> EnvVars:
        """Which configuration git's ssh reads, which names every copied key."""
        return {"GIT_SSH_COMMAND": contained_ssh(self.home_inside)}

    def configuration(self) -> list[GitSetting]:
        """Nothing: the handover is a variable, for a reason spelled at :func:`contained_ssh`."""
        return []

    def transport(self, user: str) -> ForgeTransport:
        """Every forge remote is spelled as ssh, because ssh is what this reaches."""
        return SshTransport(user=user)

    def notice(self) -> list[Notice]:
        """One line, and `ephemeral` is the load-bearing word in it."""
        return [
            Notice(
                text=(
                    "Forge access: this session may use an ephemeral copy of your "
                    "host SSH credentials."
                ),
                urgency="boundary",
            )
        ]


class ForgeToken(BaseModel, frozen=True):
    """A forge token over HTTPS: the rung that needs no ssh and no network for it.

    Last of the credentialed rungs by preference and first by reach. It is
    the only one that crosses a filtered egress, because HTTPS is the only
    thing a proxy carries, and the only one that is scoped -- a fine-grained
    token names the repositories it opens, where a key opens whatever the key
    opens.
    """

    kind: Literal["token"] = "token"
    variable: str = Field(description="Where the launching shell holds it")

    def mount_arguments(self) -> list[str]:
        """Nothing crosses as a file."""
        return []

    def environment(self) -> EnvVars:
        """Nothing by value.

        The token reaches the container by name through
        :meth:`GitAccess.inherited`, and the credential helper expands it
        inside. A value here would be a value in the argv that starts the
        container, which every process on the host can read.
        """
        return {}

    def configuration(self) -> list[GitSetting]:
        """Nothing of its own: the helper that answers with it is the access's."""
        return []

    def transport(self, user: str) -> ForgeTransport:
        """Every forge remote is spelled as HTTPS, because that is what a token answers."""
        return HttpsTransport()

    def notice(self) -> list[Notice]:
        """One line, naming the variable so a reader knows which one is in play."""
        return [
            Notice(
                text=(
                    f"Forge access: this session may use the token in {self.variable}."
                ),
                urgency="boundary",
            )
        ]


class NoCredential(BaseModel, frozen=True):
    """Nothing was usable, so the session reads what the public internet serves.

    A degradation rather than a failure -- plenty of work never touches a
    remote -- and the one arm that earns more than a line, because the reader
    who meets it needs to know both that it happened and what would fix it.
    Every rung that declined says why it declined, which is the difference
    between "configure a credential" and "the agent you are running holds no
    identity".
    """

    kind: Literal["none"] = "none"
    variable: str = Field(description="The token variable that would have answered")
    host: str = Field(description="The forge these credentials would have reached")
    declined: list[str] = Field(
        default=[], description="Why each rung of the ladder was not taken"
    )

    def mount_arguments(self) -> list[str]:
        """Nothing crosses."""
        return []

    def environment(self) -> EnvVars:
        """Nothing crosses."""
        return {}

    def configuration(self) -> list[GitSetting]:
        """Nothing of its own: the refusing helper is the access's."""
        return []

    def transport(self, user: str) -> ForgeTransport:
        """HTTPS, which is what a public read crosses a filtered egress on.

        Rewriting toward a transport this session cannot authenticate on is
        not a contradiction: an anonymous clone and fetch work over it, and
        an ssh remote left as written is one a filtered container cannot
        resolve at all. Half the capability beats none of it.
        """
        return HttpsTransport()

    def notice(self) -> list[Notice]:
        """The line, then the remediation -- which only this arm prints."""
        return [
            Notice(
                text=(
                    "Forge authentication: unavailable — public reads only; "
                    f"configure SSH credentials or {self.variable} for push, "
                    "private repositories, and gh."
                ),
                urgency="warning",
            ),
            *[
                Notice(text=reason, urgency="detail", indent=1)
                for reason in self.declined
            ],
            Notice(
                text=(
                    f"Export {self.variable} in the launching shell holding a "
                    f"{self.host} token with contents and pull requests "
                    "readable and writable, or run an ssh agent, or leave a "
                    "passphrase-free key and a `known_hosts` entry for "
                    f"{self.host} in ~/.ssh."
                ),
                urgency="detail",
                indent=1,
            ),
        ]


type ForgeCredential = Annotated[
    AgentSocket | EphemeralKeys | ForgeToken | NoCredential, Discriminator("kind")
]
"""What one contained session proves who it is with, chosen at its launch."""


class Attempt(BaseModel, frozen=True):
    """What one rung of the ladder answered when it was asked.

    Both halves rather than an exception, because a decline is not a failure
    -- it is the next rung's cue, and its sentence is what the reader who
    ends up with no credential at all needs to see.
    """

    credential: ForgeCredential | None = Field(
        default=None, description="What this rung produced, if it produced one"
    )
    because: str = Field(
        default="", description="Why it could not be taken, when it could not"
    )


type CredentialSource = Literal["auto", "agent", "files", "token", "none"]
"""Which rungs of the ladder a project will take.

``auto`` walks all three credentialed rungs in order of what they leave
behind. The four named values pin one rung, which is what an adopter reaches
for when the automatic answer is right for their laptop and wrong for their
build machine -- and pinning a rung that turns out to be unusable degrades to
``none`` with the reason said, rather than refusing a launch over a
preference.
"""


type ForgeTokenSource = Literal["variable", "forge-login"]
"""Where a launch looks for the forge token.

Two answers because two kinds of host hold a token in two places. One exports
it, having scoped it themselves; the other signed into the forge client once
and has no variable at all -- and on that host the variable is unset for the
same reason it is unset on a machine with no credential whatsoever, which is
why nothing can infer the difference and a project has to say.

The distinction is worth a declaration rather than a fallback taken silently:
a stored login is scoped to whatever the person signed in with, which on
GitHub is every repository the account can reach and whichever of `workflow`
and `admin:public_key` they granted. A variable holds what the operator put
in it. Reaching for the first when the second is empty widens what a session
holds without anybody having asked for it.
"""


class GitAccess(BaseModel, frozen=True):
    """How a contained session reaches the forge, and what it claims when it does."""

    host: str = Field(
        default="github.com",
        description=(
            "The forge this reaches. A fine-grained, repository-scoped token "
            "is a GitHub feature; an adopter on GitLab, Gitea or a corporate "
            "host names their own host here and supplies whatever their "
            "forge's equivalent is, and gets the same rewrites"
        ),
    )
    source: CredentialSource = Field(
        default="auto",
        description=(
            "Which rung of the credential ladder this project takes. `auto` "
            "walks them in order of what each leaves behind and takes the "
            "first that is verified usable"
        ),
    )
    token_variable: str = Field(
        default="LUP_GIT_TOKEN",
        description=(
            "Where the launcher reads the token from. An environment "
            "variable rather than a file inside the checkout, because a "
            "gitignored file is exactly what the undo layer cannot restore "
            "and what `git clean -fdx` destroys — the one command the "
            "recoverability argument has to keep refusing"
        ),
    )
    token_source: ForgeTokenSource = Field(
        default="variable",
        description=(
            "Where the launcher looks when it needs a token. `variable` "
            "reads `token_variable` and stops, which is what an operator "
            "scoping their own credential wants; `forge-login` falls back "
            "to the forge client's stored login, which is what a host "
            "authenticated by `gh auth login` has instead of a variable. "
            "The default is the narrow one: a project opting into the "
            "fallback is saying its sessions may hold whatever that login "
            "is scoped to"
        ),
    )
    username: str = Field(
        default="x-access-token",
        description="What the forge expects as the username beside a token",
    )
    ssh_user: str = Field(
        default="git",
        description=(
            "Who an ssh remote connects to the forge as. One account serves "
            "every user on every forge that speaks git over ssh, which tells "
            "them apart by key rather than by name"
        ),
    )
    ssh_inside: str = Field(
        default="/run/lup/ssh",
        description=(
            "Where the session-owned ssh home is mounted. Outside every home "
            "directory on purpose: a path under one would be shadowed by "
            "whatever the runtime keeps there, and this has to be reachable "
            "by an absolute path the compiled configuration can name"
        ),
    )
    agent_inside: str = Field(
        default="/run/lup/ssh-agent",
        description="Where a forwarded agent socket appears inside the container",
    )
    signing: CommitSigning = Field(
        default=SigningOff(),
        description="What a commit made inside the boundary claims, if anything",
    )
    maintains: bool = Field(
        default=False,
        description=(
            "Whether a session runs git's own repository maintenance. False "
            "because it cannot: the gitdir's root is mounted read-only, so "
            "the `pack-refs` task the automatic run starts fails on "
            "`packed-refs.lock` and prints three errors after every commit "
            "that otherwise landed — which made one commit read as failed "
            "when it had not. A project mounting its gitdir writable turns "
            "this back on and gets git's ordinary housekeeping"
        ),
    )

    def rungs(self) -> list[CredentialSource]:
        """The ladder this declaration will walk, in order.

        One rung when the project pinned one, and the three credentialed
        rungs when it did not. ``none`` is a rung that declines, so a project
        that wants no credential at all gets the same reported degradation as
        one whose credentials were unusable rather than a separate silence.
        """
        match self.source:
            case "auto":
                return ["agent", "files", "token"]
            case pinned:
                return [pinned]

    def select(
        self, environ: EnvVars, carries_ssh: bool, home: Path
    ) -> ForgeCredential:
        """Walk the ladder and take the first rung that is verified usable.

        Verified rather than present, which is the entire difference between
        this and reading a variable. ``SSH_AUTH_SOCK`` set is not an agent
        holding an identity, a key file is not a key that opens without a
        passphrase, and a checkout is not a ``known_hosts`` that can verify
        the forge. Each of those looks like a credential at launch and fails
        identically at the first push -- inside a session, in ssh's
        vocabulary, with nobody there to read it.

        ``carries_ssh`` is the network's answer rather than this
        declaration's, and it gates both ssh rungs before anything is
        probed. ssh reads none of the proxy variables, so on a filtered
        egress a forwarded agent is a credential the session holds and cannot
        use -- which is worse than not holding it, because it reads as ready.

        ``home`` is the operator's, and a launch started from inside a
        session finds it closed: the declaration that offers these
        credentials also denies an agent reading them. That is the boundary
        working, and it arrives here as an ordinary decline with its reason.
        """
        rungs = self.rungs()
        wanted = "agent" in rungs or "files" in rungs
        material = (
            host_ssh_material(home, self.host)
            if wanted and carries_ssh
            else SshMaterial()
        )

        nowhere = "no writable temporary directory for a session ssh home"

        def reached(rung: CredentialSource) -> Attempt:
            """What one rung answered: a credential, or why it could not be taken."""
            match rung:
                case "agent" | "files" if not carries_ssh:
                    return Attempt(
                        because=(
                            "this session's network carries no ssh, which "
                            "reads none of the proxy variables it would have "
                            "to cross"
                        )
                    )
                case "agent" | "files" if not material.reachable:
                    return Attempt(
                        because=(
                            f"{home / '.ssh'} could not be read, so no ssh "
                            "credential could be selected from it"
                        )
                    )
                case "agent" | "files" if not material.known:
                    return Attempt(
                        because=(
                            f"no host key for {self.host} in "
                            f"{home / '.ssh' / 'known_hosts'}, so a "
                            "non-interactive ssh cannot verify the forge"
                        )
                    )
                case "agent":
                    socket = agent_socket(environ)
                    if not socket:
                        return Attempt(
                            because=(
                                "no ssh agent holding an identity answers at "
                                "SSH_AUTH_SOCK"
                            )
                        )
                    lent = ephemeral_home(material, self.ssh_inside, keys=False)
                    if lent is None:
                        return Attempt(because=nowhere)
                    return Attempt(
                        credential=AgentSocket(
                            socket=socket,
                            inside=self.agent_inside,
                            home=str(lent),
                            home_inside=self.ssh_inside,
                        )
                    )
                case "files":
                    if not material.keys:
                        return Attempt(
                            because=(
                                f"{len(material.locked)} ssh key(s) in "
                                f"{home / '.ssh'} need a passphrase, which a "
                                "container has nobody to type"
                                if material.locked
                                else f"no ssh key in {home / '.ssh'} opens "
                                "without a passphrase"
                            )
                        )
                    lent = ephemeral_home(material, self.ssh_inside, keys=True)
                    if lent is None:
                        return Attempt(because=nowhere)
                    return Attempt(
                        credential=EphemeralKeys(
                            home=str(lent), home_inside=self.ssh_inside
                        )
                    )
                case "token" if self.granted(environ):
                    return Attempt(credential=ForgeToken(variable=self.token_variable))
                case "token":
                    return Attempt(
                        because=f"{self.token_variable} is unset in the launching shell"
                    )
                case _:
                    return Attempt(
                        because="this project's image declaration selects no credential"
                    )

        def walked() -> Iterator[Attempt]:
            """Each rung's answer, stopping at the first that produced one.

            Lazily, which is the load-bearing part: taking a rung copies key
            material into a directory that then has to be cleaned up, so a
            walk that evaluated every rung before choosing would build ssh
            homes for the rungs it was about to discard.
            """
            for rung in rungs:
                reply = reached(rung)
                yield reply
                if reply.credential is not None:
                    return

        tried = list(walked())
        taken = tried[-1].credential if tried else None
        if taken is not None:
            return taken
        return NoCredential(
            variable=self.token_variable,
            host=self.host,
            declined=list(dict.fromkeys(item.because for item in tried)),
        )

    def sourced(self, environ: EnvVars) -> str:
        """The token this launch carries, from wherever the declaration points.

        The variable wins wherever it holds something, so an operator who
        exported one for this launch gets the one they exported rather than
        whatever the forge client is signed in as.

        A stored login is read through the client that owns it rather than
        out of its configuration file. Where that file lives and how it
        encodes a token are the client's to change; ``auth token`` is the
        part it promises, and asking it also answers for a login that has
        expired, which reading the file cannot. A client that is absent,
        signed out, or refuses answers the empty string -- no token is an
        ordinary posture here, reported by the launch like any other rather
        than raised.
        """
        held = environ.get(self.token_variable, "")
        if held or self.token_source == "variable":
            return held
        try:
            return gh.out("auth", "token", "--hostname", self.host).strip()
        except (sh.ErrorReturnCode, sh.CommandNotFound):
            return ""

    def granted(self, environ: EnvVars) -> bool:
        """Whether this launch holds a token at all, whatever rung was taken.

        Asked separately from the selection because the token is not only a
        git transport: `gh` reads it for every API call it makes, and a
        session on an ssh credential still wants one. So it travels whenever
        it exists, and what the ssh rungs change is which transport git
        speaks, not whether the forge client can authenticate.
        """
        return bool(self.sourced(environ))

    def maintenance(self) -> list[GitSetting]:
        """Whether git may start its own housekeeping, as a setting or nothing.

        Nothing when it may, so a session that can maintain its repository is
        one this said nothing about rather than one told to do what it would
        have done anyway.

        Not about the forge, unlike everything else here, and it sits beside
        them because :meth:`configuration` is the one channel a contained
        session's git settings travel through -- ``GIT_CONFIG_COUNT`` has to
        match the pairs beneath it, so a second assembly would be a second
        thing to keep in step with this one.
        """
        return [] if self.maintains else [GitSetting(key="maintenance.auto", value="0")]

    def tracking(self) -> list[GitSetting]:
        """Branches that record no upstream, and a push that needs none.

        A branch's upstream is a `[branch]` section in the shared `config`,
        which a contained session cannot write. Git's default records one
        whenever a branch is cut from a remote-tracking ref, and a record it
        cannot write fails the whole command: measured, `git switch -c x
        origin/main` exits 1 and `git worktree add -b x path origin/main`
        leaves no worktree at all. With `branch.autoSetupMerge` off the cut
        records nothing and succeeds, and with `push.default` at `current` a
        bare `git push` sends the branch to its own name with no upstream to
        consult. An explicit `--track`, `--set-upstream-to` or `push -u` still
        asks for the record and still cannot have it.
        """
        return [
            GitSetting(key="branch.autoSetupMerge", value="false"),
            GitSetting(key="push.default", value="current"),
        ]

    def helper(self, granted: bool) -> GitSetting:
        """What git answers an HTTPS challenge with, token or not.

        With a token, the variable is read out of the environment rather than
        out of a file, so nothing lands in the tree and nothing survives the
        container: ``store`` would write it into the config home, and a
        prompt would hang a non-interactive session forever. The setting
        carries the variable's *name* -- the shell inside expands it -- so
        the secret is not in the configuration either.

        Without one, a helper is installed anyway and refuses on stderr. That
        is the boundary's only chance to get a sentence in at the moment
        somebody needs it -- git's own account of an unanswerable challenge
        names the URL and never the variable that would have answered it, and
        the launch notice that did name it has scrolled past by then. A
        project selecting no credential at all is not invited to export one:
        its sessions hold none by design, and its pushes happen on the host.

        Installed on an ssh session too, where it answers for every remote
        the rewrite did not touch: a submodule on another forge, a
        `pip install` from an HTTPS repository. Those are the ones that would
        otherwise meet a prompt.
        """
        match granted, self.source:
            case True, _:
                answered = (
                    f"echo username={self.username}; "
                    f"echo password=${self.token_variable}"
                )
            case False, "none":
                answered = (
                    'echo "lup: this project gives its sessions no credential, '
                    "so git cannot authenticate over HTTPS here; pushes happen "
                    'on the host" >&2; exit 1'
                )
            case False, _:
                answered = (
                    f'echo "lup: no token in {self.token_variable}, so this '
                    "session cannot authenticate over HTTPS; export it on the "
                    'host before the launch" >&2; exit 1'
                )
        return GitSetting(key="credential.helper", value=f"!f() {{ {answered}; }}; f")

    def configuration(
        self,
        rewrites: list[RemoteRewrite],
        credential: ForgeCredential,
        granted: bool,
    ) -> list[GitSetting]:
        """Every git setting the container starts with, in one list.

        The rewrites point each unreachable spelling at the transport this
        session can reach, the credential contributes whatever it needs git
        to know, the helper answers for the token or refuses in its name, and
        the signing member says what it claims. Who a commit is authored as
        is not a setting: :meth:`GitIdentity.environment` carries it.

        Withholding a rewrite unless a token comes with it would reason that
        half this arrangement is worse than none: a remote redirected to
        HTTPS and then asked for a password, in a session with no human at
        the other end. What refutes it is that the prompt is gone. Terminal
        prompting is off inside the image, so the unanswerable challenge that
        argument fears is a refusal with a reason, and the rewrite is the
        only thing making a remote addressable at all.
        """
        return [
            *[rewrite.setting() for rewrite in rewrites],
            *credential.configuration(),
            *self.signing.configuration(),
            *self.maintenance(),
            *self.tracking(),
            self.helper(granted),
        ]

    def environment(
        self,
        rewrites: list[RemoteRewrite],
        credential: ForgeCredential,
        granted: bool,
        identity: GitIdentity | None = None,
    ) -> EnvVars:
        """The variables carrying the configuration and the credential's own inside.

        ``GIT_CONFIG_COUNT`` and its numbered pairs, because git reads them
        above every configuration file: nothing inside the checkout can
        override what was decided out here, and nothing has to be written
        into a tree the agent can edit. A file would be both.

        Sent whether or not there is a credential. A remote on a transport
        this session cannot reach fails for a reason that has nothing to do
        with credentials -- under a filtered egress the session resolves no
        names at all -- so the rewrite is what makes a remote addressable,
        and a public repository is readable through it on no credential
        whatsoever. Holding it back until a token appears turns every
        tokenless fetch into a hostname that will not resolve, which reads as
        a broken container rather than as a boundary, and leaves the signing
        settings unsent beside it: the launch says commits are unsigned while
        the checkout's own ``commit.gpgsign`` still stands.

        No secret is in here. The token crosses by name through
        :meth:`inherited`, and every value below is a configuration key, a
        path, or the *name* of a variable the container expands itself.
        """
        settings = self.configuration(rewrites, credential, granted)
        return {
            **credential.environment(),
            **(identity.environment() if identity is not None else {}),
            "GIT_CONFIG_COUNT": str(len(settings)),
            **{
                name: value
                for index, setting in enumerate(settings)
                for name, value in (
                    (f"GIT_CONFIG_KEY_{index}", setting.key),
                    (f"GIT_CONFIG_VALUE_{index}", setting.value),
                )
            },
        }

    def inherited(self, granted: bool) -> list[str]:
        """Variables the container takes from the launching shell by name.

        By name and never by value, because a value here is a value in the
        argv of the command that starts the container -- which every process
        on the host can read out of `ps` for as long as the session runs, and
        which anything logging the launch records verbatim. Both engines read
        a bare ``-e NAME`` as "take this one from my own environment", so the
        secret crosses without ever being written down.
        """
        return [self.token_variable] if granted else []

    def authorship(self, identity: GitIdentity | None) -> list[Notice]:
        """Said only when there is none, which is the case worth a line.

        Git's own account of an absent identity names a container hostname
        and no way to fix it from where the reader is standing, and it
        arrives in the middle of the commit that was the point of the session
        rather than before it. The present case says nothing: a launch that
        reports the author of commits nobody has made yet is a paragraph
        between the reader and the one line that mattered.
        """
        if identity is not None:
            return []
        return [
            Notice(
                text=(
                    "Commits: this checkout sets no `user.name` and "
                    "`user.email`, so a commit in here refuses with `Author "
                    "identity unknown` and a hostname that lives as long as "
                    "the container. Set them on the host and relaunch."
                ),
                urgency="warning",
            )
        ]

    def notice(
        self,
        credential: ForgeCredential,
        identity: GitIdentity | None = None,
    ) -> list[Notice]:
        """What the launch says about the forge, which is one line when all is well.

        The selected credential names itself and stops. Everything that used
        to travel beside it -- how many spellings were rewritten, that the
        agent can read the token, what a token should be scoped to, who
        commits are authored as -- was true and was noise: five paragraphs in
        which the one sentence that decides whether the session can work sat
        indistinguishable from four that do not. The rationale is in this
        module and in ``docs/permissions.md``; the launch carries the verdict.

        Remediation appears on exactly one arm, because it is the only one
        where the reader has something to do.
        """
        return [
            *credential.notice(),
            *self.authorship(identity),
            *self.signing.notice(),
        ]
