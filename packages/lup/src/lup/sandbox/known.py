"""The repositories lup has seen from the host, kept where no container reaches.

Most repositories are found by where they are -- a plain checkout's `.git`, the
bare clone holding `tree/`, a clone a worktree sits inside. One kind is not: a
linked worktree placed anywhere, `git worktree add ../x` beside a plain clone,
reached from itself, has no path back to its repository but the pointer under
test. So the repository is remembered the first time lup sees it from the
host, before any container ran for it -- at a launch's preflight, or when lup
cuts the worktree -- and the worktree is discovered from it thereafter.

One set of repository git directories, not a record per worktree: a worktree
is found through its repository, so remembering the repository is what makes
every worktree it later lists reachable, including the ones a contained session
cuts after the launch.

The store is the user's state, under `$XDG_STATE_HOME/lup/`, and only the host
writes it. A launched session is refused the write by its placement, and
no lease may grant a mount over it -- the launch checks that before a container
starts -- because a store a container could write is a store a container could
add its own repository to.
"""

from collections.abc import Sequence
from pathlib import Path

from pydantic import BaseModel, ValidationError

from lup.channels.models import publish_atomic
from lup.workspace.user_directories import UserDirectories


class KnownRepositories(BaseModel):
    """Every repository git directory lup has vouched for from the host."""

    repositories: list[str] = []


def store_file() -> Path:
    """The one file the store keeps, in lup's state for this person."""
    return UserDirectories().state() / "repositories.json"


def answers_directory() -> Path:
    """Where the operator's answers to parked reviews are kept, in lup's state for this person.

    The one part of the launcher's state a launch lends a container, and only
    read-only, each repository's answers apart: an answer is authority a
    session may read and never write. The hooks derive the same directory
    for themselves (:func:`lup.policy.assets.host.review_answers_home`).
    """
    return UserDirectories().state() / "reviews"


def known_repositories() -> list[Path]:
    """Every repository the store remembers, or none where it cannot be read.

    Unreadable reads as empty: the store only ever adds trust, so losing it
    costs first sightings reported again rather than a repository trusted
    wrongly.
    """
    try:
        raw = store_file().read_text()
        return [
            Path(path)
            for path in KnownRepositories.model_validate_json(raw).repositories
        ]
    except (OSError, ValidationError):
        return []


def remember(repositories: Sequence[Path]) -> list[Path]:
    """Add these repositories to the store, and return the ones it lacked.

    Written whole and renamed into place, so a reader never meets half a file
    and two launches racing lose nothing but their own addition, which the
    next launch makes again. Nothing is written where nothing is new.
    """
    held = {str(path) for path in known_repositories()}
    added = sorted({str(path.resolve()) for path in repositories} - held)
    if not added:
        return []
    publish_atomic(
        store_file(), KnownRepositories(repositories=sorted({*held, *added}))
    )
    return [Path(path) for path in added]
