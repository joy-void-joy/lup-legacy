"""The validated surface for paths no shell command may name, and its erasure.

The same shape a tool refusal takes, for a different subject: a set of paths
and what to reach for instead. :mod:`lup.policy.kernel.withheld` decides
against the erased rows, reading every word of every command, so a
declaration here holds whichever verb would have reached the path.

The library does ship one: the key and login files a developer's machine keeps
where every tool expects them. Which of those a project's sessions may read is
still that project's call, so they reach it as defaults it can replace, and a
runtime's own login is added by the composition root that knows the runtime.
"""

from collections.abc import Sequence

from pydantic import BaseModel, Field, field_validator

from lup.policy.kernel.rows import RefusedPathRow


class RefusedPaths(BaseModel, frozen=True):
    """Paths a shell command may not name, and what its agent does instead.

    Each pattern is spelled from where it is anchored. From the root (`/`) it
    names that one place. From a home (`~/`) or from anywhere (`**/`) it names
    whatever path its trailing names end, since the kernel cannot know which
    home a word meant -- `~/.ssh/id_rsa`, `$HOME/.ssh/id_rsa` and
    `/home/u/.ssh/id_rsa` are the one file. A name may be a glob, and ``**``
    spans any run of names, including none.

    ``exempt`` names what stays reachable beneath ``paths``: a word is refused
    unless every file it could name is exempt, so `~/.ssh/*.pub` passes where
    `~/.ssh/*` does not. ``reason`` says what naming one would have done and
    ``recovery`` what reaches the same end, because a refusal is never only a
    refusal. The escalation marker turns one into an approval question.
    """

    paths: list[str] = Field(min_length=1)
    exempt: list[str] = []
    reason: str = Field(min_length=1)
    recovery: str = Field(min_length=1)

    @field_validator("paths", "exempt")
    @classmethod
    def anchored(cls, patterns: list[str]) -> list[str]:
        """Refuse a pattern that says nowhere where it starts.

        A bare `.ssh/id_rsa` would read as relative to wherever a command
        stood, which is not a place a declaration can name.
        """
        loose = [
            pattern
            for pattern in patterns
            if not pattern.startswith(("/", "~/", "**/"))
        ]
        if loose:
            raise ValueError(
                f"{', '.join(loose)}: a withheld path is spelled from the root"
                " (`/`), from a home (`~/`), or from anywhere (`**/`)"
            )
        return patterns

    def erased(self) -> RefusedPathRow:
        """The primitive row the kernel matches words against."""
        return RefusedPathRow(
            paths=list(self.paths),
            exempt=list(self.exempt),
            reason=self.reason,
            recovery=self.recovery,
        )


def credential_files(
    paths: Sequence[str] = (
        "~/.ssh/**",
        "~/.gnupg/**",
        "~/.aws/credentials",
        "~/.netrc",
        "~/.git-credentials",
        "~/.config/gh/hosts.yml",
        "~/.docker/config.json",
        "~/.pypirc",
        "/proc/*/environ",
        "**/.env.local",
        "**/.env.*.local",
        "**/lup/companions/*/dashboard/token",
    ),
    exempt: Sequence[str] = (
        "~/.ssh/*.pub",
        "~/.ssh/known_hosts",
        "~/.ssh/known_hosts.old",
        "~/.ssh/config",
        "~/.ssh/authorized_keys",
    ),
    also: Sequence[str] = (),
) -> RefusedPaths:
    """The key and login files a machine keeps, withheld from every command.

    Private keys and every file beside them in `~/.ssh` except the public
    ones, the signing keyring, and the token files the common command-line
    clients write: AWS, netrc, git's credential store, gh, docker, PyPI. A
    process's environment file is the environment dump by another route.

    A checkout's own login file too: the gitignored `.env.local`, and the
    `.env.<mode>.local` beside it, are where a project's settings keep the API
    keys its `.env` leaves out, in whichever checkout or worktree holds one.
    The committed `.env` and `.env.example` name no secret and stay readable.

    And the operator's: the capability that opens the dashboard, kept in
    lup's own state, which a session holding it could answer its own reviews
    with.

    ``also`` is the composition root's, for a login only it can name -- the
    runtimes' own, which a neutral module does not spell.
    """
    return RefusedPaths(
        paths=[*paths, *also],
        exempt=list(exempt),
        reason="this path holds a key or a login, and reading it writes the"
        " secret into this transcript",
        recovery="Let the program that uses it read it -- ssh, git, gh and the"
        " cloud clients each do -- and ask the user for anything that needs"
        " its contents.",
    )


def secret_variable_names(
    patterns: Sequence[str] = (
        "*TOKEN*",
        "*SECRET*",
        "*PASSWORD*",
        "*PASSWD*",
        "*CREDENTIAL*",
        "*API_KEY*",
        "*APIKEY*",
        "*PRIVATE_KEY*",
        "*_KEY",
        "*_PAT",
        "*AUTH",
        "*AUTHORIZATION*",
    ),
    also: Sequence[str] = (),
) -> list[str]:
    """Name patterns of the variables whose values no command may print.

    Matched against the whole name, without case. `*AUTH` and not `*AUTH*`,
    because `GIT_AUTHOR_NAME` and `SSH_AUTH_SOCK` name an author and a socket
    rather than a secret, and a refusal nobody can see the reason for is one
    that teaches the agent to route around it.
    """
    return [*patterns, *also]
