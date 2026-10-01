"""Secrets kept on the host alone: one file per project, outside every checkout and session.

A checkout's ``.env.local`` is inside the checkout, which every contained
session mounts and every session reads: the right place for a value the
application runs on, the wrong one for a key a session must never hold — one
a host companion uses, running beside the session on the operator's machine.
Such a key is kept in the project's **host store** instead, under the person's
lup config::

    $XDG_CONFIG_HOME/lup/secrets/<project>.env     (~/.config where unset)

The directory is its owner's alone and so is the file, from the moment it
exists: every change is made to a private copy beside it and renamed over it,
so no reader meets half a file and no instant leaves a copy anybody else may
read. The project is the one the checkout's ``pyproject.toml`` names, so every
worktree of it shares one store.

Nothing a session reads holds the store. A contained launch refuses a mount
reaching the person's lup config, a launch takes every name the store holds
out of what the session inherits, even where the operator's shell exported
one, and the store's values leave it only for the host companions naming them.
"""

from collections.abc import Callable, Iterable
from pathlib import Path
from tempfile import TemporaryDirectory

from dotenv import dotenv_values, set_key, unset_key
from pydantic import BaseModel, Field

from lup.channels.models import write_atomic
from lup.harness.environment import Placement
from lup.types import EnvVars
from lup.workspace.paths import read_project_name
from lup.workspace.user_directories import UserDirectories


class HostOnlyRefused(RuntimeError):
    """A host-only secret written where the host store is not the host's."""


def secrets_directory() -> Path:
    """Where every project's host store lives: the person's lup config, beside their profiles."""
    return UserDirectories().config() / "secrets"


class HostSecrets(BaseModel, frozen=True):
    """One project's host store: outside every checkout, and never mounted into one."""

    project: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]*$")
    """The project the store belongs to, as its manifest names it."""

    directory: Path = Field(default_factory=secrets_directory)

    @classmethod
    def for_checkout(cls, root: Path) -> "HostSecrets":
        """The store of the project a checkout is, which every worktree of it shares."""
        return cls(project=read_project_name(root))

    def path(self) -> Path:
        """The file the store is kept in."""
        return self.directory / f"{self.project}.env"

    def read(self) -> EnvVars:
        """Every value the store holds, or nothing where there is no store."""
        if not self.path().is_file():
            return {}
        return {
            key: value
            for key, value in dotenv_values(self.path()).items()
            if value is not None
        }

    def write(self, values: EnvVars) -> None:
        """Set these keys, keeping everything else the store holds."""
        if not values:
            return

        def assigned(staging: Path) -> None:
            for key, value in values.items():
                set_key(staging, key, value, quote_mode="never")

        self.edited(assigned)

    def clear(self, keys: Iterable[str]) -> None:
        """Drop these keys; a key the store does not hold is no change at all."""
        present = [key for key in keys if key in self.read()]
        if not present:
            return

        def removed(staging: Path) -> None:
            for key in present:
                unset_key(staging, key)

        self.edited(removed)

    def edited(self, edit: Callable[[Path], None]) -> None:
        """Apply one edit to a private copy of the store, then put the copy in its place.

        Refused inside a container before anything is made: a value written
        there lands in the container's own configuration, where no host
        companion reads it and the session can, and would read as saved.
        """
        if Placement.here().contained:
            raise HostOnlyRefused(
                "This runs inside a lup container, where a host-only secret "
                "would land in the container rather than in the host store the "
                "companions read. Set it from a terminal on the host."
            )
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.directory.chmod(0o700)
        with TemporaryDirectory(dir=self.directory, prefix=".editing-") as private:
            staging = Path(private) / self.path().name
            staging.write_bytes(
                self.path().read_bytes() if self.path().is_file() else b""
            )
            edit(staging)
            write_atomic(self.path(), staging.read_bytes(), mode=0o600)


def withheld_secrets(environment: EnvVars, root: Path) -> EnvVars:
    """``environment`` without any name the checkout's host store holds.

    What a launched session inherits from the process launching it, which
    may have exported a host-only key for its own reasons: the session holds
    none of them, whatever its launcher's shell holds.
    """
    withheld = HostSecrets.for_checkout(root).read()
    return {name: value for name, value in environment.items() if name not in withheld}
