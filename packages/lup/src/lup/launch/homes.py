"""The home a runtime keeps for each checkout, in lup's state for the person.

A runtime whose host sessions run in a home derived per checkout keeps that
home here rather than in the checkout, because a home holds a copy of the
account's login: kept inside the checkout, it made the tree credential-bearing,
so every recursive read of it walked a login, and an archive, a build context
or an editor index carried one. State rather than cache, since a home holds
the history a resumed session reopens and a rotated login not yet returned to
the account.

One directory per checkout, named by its last component and a digest of its
whole path, as a project environment is (:mod:`lup.launch.environments`), with
a claim inside naming the checkout it serves, so a sweep can tell a home whose
checkout is gone. Each runtime's home sits in it under the runtime's own word
for one, which the policy withholds a login beneath wherever it sits.
"""

import hashlib
import shutil
from pathlib import Path

from pydantic import BaseModel

from lup.sandbox.known import store_directory

# lup: ignore[constant-declaration] — the file a home's directory names its
# checkout in, which the store writing it and every sweep reading it share
CHECKOUT_CLAIM = "checkout"
"""What the file naming the checkout a home's directory serves is called."""


def homes_root(state: Path | None = None) -> Path:
    """Where every checkout's runtime homes are kept: lup's state, outside every checkout."""
    return (state or store_directory()) / "homes"


def checkout_directory(root: Path, checkout: Path) -> Path:
    """The directory under ``root`` holding one checkout's runtime homes.

    The readable half is what makes a listing legible, and the digest what
    keeps two repositories' `dev` apart.
    """
    digest = hashlib.sha256(str(checkout).encode()).hexdigest()[:12]
    return root / f"{checkout.name}-{digest}"


def claimed_directory(root: Path, checkout: Path) -> Path:
    """One checkout's directory, made, with the checkout it serves written into it."""
    directory = checkout_directory(root, checkout)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    claim = directory / CHECKOUT_CLAIM
    if not claim.is_file() or claim.read_text(encoding="utf-8") != str(checkout):
        claim.write_text(str(checkout), encoding="utf-8")
    return directory


class KeptHomes(BaseModel, frozen=True):
    """One checkout's runtime homes as lup's state keeps them, and the checkout they serve."""

    directory: Path
    checkout: Path | None = None
    """The checkout they serve, where the claim can be read."""

    def finished(self) -> bool:
        """Whether the checkout these homes served is gone."""
        return self.checkout is not None and not self.checkout.exists()

    def size(self) -> int:
        """The bytes its files hold, links not followed."""
        return sum(
            path.lstat().st_size
            for path in self.directory.rglob("*")
            if path.is_file() and not path.is_symlink()
        )

    def remove(self) -> None:
        """Remove the homes, their claim with them."""
        shutil.rmtree(self.directory)


def remove_checkout_homes(checkout: Path, root: Path | None = None) -> Path | None:
    """Remove the homes one checkout had, answering where they were, if it had any."""
    directory = checkout_directory(root or homes_root(), checkout)
    if not directory.is_dir():
        return None
    shutil.rmtree(directory)
    return directory


def kept_homes(root: Path | None = None) -> list[KeptHomes]:
    """Every checkout's runtime homes on this machine, with the checkout each claims."""
    kept = root or homes_root()
    if not kept.is_dir():
        return []
    return [
        KeptHomes(
            directory=directory,
            checkout=(
                Path(claim.read_text(encoding="utf-8"))
                if (claim := directory / CHECKOUT_CLAIM).is_file()
                else None
            ),
        )
        for directory in sorted(kept.iterdir())
        if directory.is_dir()
    ]
