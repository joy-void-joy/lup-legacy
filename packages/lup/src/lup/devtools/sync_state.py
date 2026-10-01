"""Review checkpoints shared by the worktrees of one consuming repository."""

from hashlib import sha256
from pathlib import Path

from pydantic import BaseModel

from lup.channels.models import publish_atomic
from lup.devtools.dev.records import shared_directory_of
from lup.execution.git import GitError


class ReviewSource(BaseModel, frozen=True):
    """The upstream and named ref whose history a review considers."""

    repository: str
    ref: str


class Checkpoint(BaseModel, frozen=True):
    """The reviewed commit, bound to the upstream and ref that were reviewed."""

    source: ReviewSource
    commit: str


def location(root: Path, name: str) -> Path:
    """Use the common Git directory; unpacked source trees keep local state."""
    try:
        common = shared_directory_of(root)
    except GitError:
        if (root / ".git").exists() or (root / "HEAD").is_file():
            raise
        common = root / ".lup"
    return common / "lup" / "sync" / f"{sha256(name.encode()).hexdigest()}.json"


def read(root: Path, name: str) -> Checkpoint | None:
    path = location(root, name)
    return Checkpoint.model_validate_json(path.read_text()) if path.exists() else None


def write(root: Path, name: str, checkpoint: Checkpoint) -> None:
    publish_atomic(location(root, name), checkpoint)
