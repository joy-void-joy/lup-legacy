"""Which roots host git may enter, and which repositories lup comes to trust.

The judgement is :func:`lup.sandbox.pointers.verdict`'s; this is the half that
acts on it for a command about to run host git -- refusing a real mismatch,
remembering every repository that vouches for itself, and deciding whether one
nothing vouches for is trusted on first sight or only reported.

A first sighting is remembered only from the host, and only where nothing
could have built the repository to be trusted: not inside territory a contained
session writes, and not while a lup session is running in it. Anything else is
reported rather than refused, since a repository lup has not met is not a
mismatch -- it is the gap the report names.
"""

from collections.abc import Sequence
from pathlib import Path

from pydantic import BaseModel

from lup.coordination.repository import RepositoryPeers
from lup.harness.environment import Placement
from lup.launch.companions import lent_by_a_companion
from lup.launch.environments import revisions_home
from lup.providers.user_config import UserConfigHome
from lup.sandbox.known import (
    answers_directory,
    known_repositories,
    remember,
    store_directory,
)
from lup.sandbox.pointers import Verdict, refusal, unvouchable, verdict, vouched_from
from lup.sandbox.rail import Lease


class Trust(BaseModel, frozen=True):
    """What a command says before host git runs in its roots."""

    refusal: str = ""
    notices: list[str] = []


class Sighting(BaseModel, frozen=True):
    """A root leading git to a repository nothing vouches for, and why it waits."""

    verdict: Verdict
    withheld: str


class HostOnly(BaseModel, frozen=True):
    """One directory the launcher keeps for itself, which no container mounts any part of."""

    path: Path
    holds: str
    """What it keeps, as a refusal names it."""

    moved_by: str
    """The variable that moves it, which is how an operator moves it out of a mount."""


def host_only_directories() -> list[HostOnly]:
    """What the launcher keeps on the host and trusts at the next launch.

    Each read at the launch, since each follows a variable of the process
    asking: the state directory, the person's lup config, and the plugin
    revisions a contained session's hooks run from.
    """
    return [
        HostOnly(
            path=store_directory(),
            holds="lup's launcher state, its store of trusted repositories among it",
            moved_by="XDG_STATE_HOME",
        ),
        HostOnly(
            path=UserConfigHome().directory(),
            holds="your lup config, each profile's account and credentials among it",
            moved_by="XDG_CONFIG_HOME",
        ),
        HostOnly(
            path=revisions_home(),
            holds="the plugin revisions contained sessions run their hooks from",
            moved_by="HOME",
        ),
    ]


def launcher_state_exposure(lease: Lease) -> str:
    """Why this lease would let a container reach what the launcher keeps, or empty.

    A mount at one of :func:`host_only_directories`, above one, or inside
    one: the store holds the repositories the host vouches for, the config
    holds the accounts a launch signs in with, the revisions the hooks a
    contained session is judged by -- so a container reaching any part of
    them could rewrite what the next launch trusts. Checked against every
    mount, read-only ones too: a container has no reason to read them either,
    and a mount it may not write today is one a later lease may widen.
    Compared resolved, since a mount is bound where its path leads.

    Two parts are lent, read-only. The operator's answers to parked reviews,
    at or inside :func:`~lup.sandbox.known.answers_directory`: a session must
    read the answer to its own review, and writing one is what the read-only
    mount refuses it. And what a companion publishes for sessions to read, at
    or inside its :func:`~lup.launch.companions.lent_directory` -- the
    dashboard's pulse, which a session's status line reads. A writable mount
    of either is refused like any other.
    """
    lent = answers_directory().resolve()

    def relation(mount: Path, directory: Path) -> str:
        """How a mount meets a directory the launcher keeps, or empty where it misses."""
        if mount == directory:
            return "which is"
        if directory.is_relative_to(mount):
            return "which holds"
        if mount.is_relative_to(directory):
            return "which lies inside"
        return ""

    return "\n".join(
        f"This launch would mount {mount}, {met} {held.holds}, at {directory}; "
        "a container could rewrite what the next launch trusts. Set "
        f"{held.moved_by} outside every mounted root, or leave {mount} unmounted."
        for mount in [
            *lease.writable,
            *(
                read
                for read in lease.read_only
                for resolved in [read.resolve()]
                if not resolved.is_relative_to(lent)
                and not lent_by_a_companion(resolved)
            ),
        ]
        for held in host_only_directories()
        for directory in [held.path.resolve()]
        if (met := relation(mount.resolve(), directory))
    )


def live_session(repository: Path) -> bool:
    """Whether a lup session is running in this repository, by its roster."""
    return any(member.running for member in RepositoryPeers(repository).present())


def withheld_because(judged: Verdict, trusted: Sequence[Path]) -> str:
    """Why a first sighting is not remembered, or empty where it may be."""
    if judged.candidate is None or judged.checkout is None:
        return ""
    if reason := unvouchable(judged.candidate, judged.checkout, trusted):
        return reason
    if live_session(judged.candidate):
        return f"a lup session is running in {judged.candidate}"
    return ""


def said(sighting: Sighting) -> str:
    """The line a first sighting is reported with, remembered or not."""
    judged = sighting.verdict
    if not sighting.withheld:
        return (
            f"First sighting: {judged.root} leads git to {judged.candidate}, "
            "which lup now trusts."
        )
    return (
        f"Unverified: {judged.root} leads git to {judged.candidate}, which lup "
        f"has not seen and cannot find by path. Not recorded, because "
        f"{sighting.withheld}. Host git there reads that repository's config."
    )


def judged_roots(roots: Sequence[Path], operator: Path | None = None) -> Trust:
    """Judge every root, remember what vouches for itself, and say the rest.

    ``operator`` is where the command was run from: the repositories found by
    path from there vouch for the roots beside the ones the store remembers.
    Inside a launched session nothing is remembered and no first sighting is
    reported -- the store is the host's, and a session has none -- but a
    mismatch refuses there all the same.
    """
    trusted = [
        *known_repositories(),
        *(vouched_from(operator) if operator is not None else []),
    ]
    verdicts = [verdict(root, trusted) for root in roots]
    refused = "\n".join(
        message for judged in verdicts if (message := refusal(judged.drifts))
    )
    if refused:
        return Trust(refusal=refused)
    if not Placement.here().host:
        return Trust()
    sightings = [
        Sighting(verdict=judged, withheld=withheld_because(judged, trusted))
        for judged in verdicts
        if judged.candidate is not None
    ]
    remember(
        [
            *(
                judged.repository
                for judged in verdicts
                if judged.repository is not None
            ),
            *(
                sighting.verdict.candidate
                for sighting in sightings
                if not sighting.withheld and sighting.verdict.candidate is not None
            ),
        ]
    )
    return Trust(notices=[said(sighting) for sighting in sightings])
