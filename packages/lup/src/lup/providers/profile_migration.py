"""Move accounts into the global profiles beside the person's lup config.

Two places hold accounts a checkout alone can reach. A checkout's own
``.lup/profiles/<name>/`` is read, first, by every launch in it; moving one
is a choice to share it with every checkout, never a repair. A personal
registry at ``~/.lup/profiles.json``, naming a Claude home per account —
``~/.lup/homes/<name>`` unless one was registered elsewhere — is read by no
launch, so an account left there is one no launch can select. This moves
what either holds, once, into
:meth:`~lup.providers.user_config.UserConfigFile.profiles_root`.

Moving rather than copying, because a login is a rotating chain: two copies
of one credential diverge the first time either renews, and the one left
behind is a stranger's. A registered home that lives somewhere of the
person's own choosing stays there and is linked, since the registry only
ever pointed at it. Nothing is overwritten: a name the destination already
holds keeps what it holds, and the source is left where it is and reported,
so running this again — or after a conflict is settled by hand — only ever
finishes the job.
"""

from collections.abc import Iterator
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from lup.channels.models import publish_atomic
from lup.providers.claude.login import CLAUDE_LOGIN
from lup.providers.profile_tree import checkout_profiles
from lup.providers.user_config import UserConfigFile

type MoveOutcome = Literal["moved", "linked", "kept", "refused"]
"""What became of one account: ``moved`` and ``linked`` finished; ``kept``
means the destination already held that name and the source is still where it
was; ``refused`` means the source named the runtime's default home, which
naming no profile already selects."""


class RegistryAccount(BaseModel, frozen=True):
    """One entry of the personal registry, as the file holds it."""

    config_dir: str


class PersonalRegistry(BaseModel, frozen=True):
    """The personal registry file, read only to be emptied."""

    profiles: dict[str, RegistryAccount] = {}
    active: str | None = None


class ProfileMove(BaseModel, frozen=True):
    """What became of one account found in a checkout or the personal registry."""

    name: str
    source: Path
    destination: Path
    outcome: MoveOutcome

    def line(self) -> str:
        """This move as the command reports it."""
        match self.outcome:
            case "moved":
                return f"moved {self.name}: {self.source} → {self.destination}"
            case "linked":
                return f"linked {self.name}: {self.destination} → {self.source}"
            case "kept":
                return (
                    f"kept {self.name}: {self.destination} already exists, so "
                    f"{self.source} was left in place — merge it by hand, then "
                    "remove it"
                )
            case "refused":
                return (
                    f"skipped {self.name}: it named {self.source}, the default "
                    "home, which naming no profile already selects"
                )


class ProfileMigration(BaseModel, frozen=True):
    """Everything one run moved, and the selection it carried over."""

    moves: list[ProfileMove] = []
    selected: str | None = None
    """The carried selection, where it became the person's; ``None`` where
    there was none to carry or the config file already recorded one."""

    def lines(self) -> list[str]:
        """The run as the command reports it, or a line saying it had nothing."""
        if not self.moves and self.selected is None:
            return ["nothing to migrate"]
        carried = [] if self.selected is None else [f"selected {self.selected}"]
        return [*(move.line() for move in self.moves), *carried]


def personal_registry_home() -> Path:
    """Where the personal registry and the homes it made sit."""
    return Path.home() / ".lup"


def moved(source: Path, destination: Path) -> bool:
    """Move one directory where nothing is yet, answering whether it went."""
    if destination.exists() or destination.is_symlink():
        return False
    destination.parent.mkdir(parents=True, exist_ok=True)
    source.rename(destination)
    return True


def migrate_checkout(root: Path, config: UserConfigFile) -> list[ProfileMove]:
    """Move a checkout's profile directories, a runtime's home at a time.

    A name the destination lacks moves whole. One it already holds moves only
    the homes it lacks, since the same person may have signed one runtime in
    here and another there; a home both hold stays behind, reported.
    """
    kept = checkout_profiles(root)
    if not kept.is_dir():
        return []

    def settled(source: Path) -> Iterator[ProfileMove]:
        destination = config.profiles_root() / source.name
        if moved(source, destination):
            yield ProfileMove(
                name=source.name,
                source=source,
                destination=destination,
                outcome="moved",
            )
            return
        for home in sorted(source.iterdir()):
            target = destination / home.name
            yield ProfileMove(
                name=source.name,
                source=home,
                destination=target,
                outcome="moved" if moved(home, target) else "kept",
            )
        if not any(source.iterdir()):
            source.rmdir()

    names = sorted(entry for entry in kept.iterdir() if entry.is_dir())
    return [move for source in names for move in settled(source)]


def migrate_registry(config: UserConfigFile, registry_home: Path) -> list[ProfileMove]:
    """Move the personal registry's accounts, and empty it of every one that went.

    A home lup made for an entry, under the registry's home, moves into the
    profile; a home registered somewhere of the person's own is linked from it
    and stays where they put it. The registry holds Claude homes alone.
    """
    registry_path = registry_home / "profiles.json"
    if not registry_path.is_file():
        return []
    registry = PersonalRegistry.model_validate_json(
        registry_path.read_text(encoding="utf-8")
    )
    made = (registry_home / "homes").resolve()

    def settled(name: str, account: RegistryAccount) -> ProfileMove:
        source = Path(account.config_dir).expanduser()
        destination = config.profiles_root() / name / CLAUDE_LOGIN.home_subdir

        def outcome() -> MoveOutcome:
            if not CLAUDE_LOGIN.nameable(source):
                return "refused"
            if destination.exists() or destination.is_symlink():
                return "kept"
            if source.is_dir() and source.resolve().is_relative_to(made):
                return "moved" if moved(source, destination) else "kept"
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.symlink_to(source, target_is_directory=True)
            return "linked"

        return ProfileMove(
            name=name, source=source, destination=destination, outcome=outcome()
        )

    moves = [
        settled(name, account) for name, account in sorted(registry.profiles.items())
    ]
    remaining = {
        move.name: registry.profiles[move.name]
        for move in moves
        if move.outcome == "kept"
    }
    if remaining:
        publish_atomic(
            registry_path,
            registry.model_copy(
                update={
                    "profiles": remaining,
                    "active": registry.active if registry.active in remaining else None,
                }
            ),
        )
    else:
        registry_path.unlink()
    if made.is_dir() and not any(made.iterdir()):
        made.rmdir()
    return moves


def migrate_profiles(
    root: Path, config: UserConfigFile, registry_home: Path | None = None
) -> ProfileMigration:
    """Move what the checkout and the personal registry hold, and carry a selection.

    The checkout's selection is preferred to the registry's; either becomes
    the person's only where their config file records none, since a
    selection made there is theirs and one of these is only a checkout's,
    and only where that account arrived.
    """
    home = registry_home or personal_registry_home()
    kept = checkout_profiles(root)
    active_file = kept / ".active"
    checkout_active = (
        active_file.read_text(encoding="utf-8").strip() or None
        if active_file.is_file()
        else None
    )
    registry_path = home / "profiles.json"
    registry_active = (
        PersonalRegistry.model_validate_json(
            registry_path.read_text(encoding="utf-8")
        ).active
        if registry_path.is_file()
        else None
    )
    moves = [*migrate_checkout(root, config), *migrate_registry(config, home)]
    if active_file.is_file():
        active_file.unlink()
    if kept.is_dir() and not any(kept.iterdir()):
        kept.rmdir()
    carried = checkout_active or registry_active
    selected = (
        carried
        if carried is not None
        and config.load().profile is None
        and (config.profiles_root() / carried).is_dir()
        else None
    )
    if selected is not None:
        config.select_profile(selected)
    return ProfileMigration(moves=moves, selected=selected)
