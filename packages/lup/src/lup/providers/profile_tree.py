"""The accounts a person keeps as directories: in a checkout, and beside their config.

A name is a directory rather than an entry in a registry, so the configuration
home an account runs under — and whatever else it earns — sit together under
``<root>/<name>/``. Making the directory the origin is what keeps one name
meaning one account however it is spelled, to a launch, a command tree, a
setup wizard or a declaration's ``profile``: there is no second list to
register a profile in, and so none to fall out of step.

Two roots hold them, resolved in this order. A checkout's ``.lup/profiles/``
is local to it — under ``.lup`` because a checkout's own state already lives
there, ignored, so a login cannot be committed by a rule nobody remembered to
write — and selected by the ``.active`` file beside its profiles. The global
root is ``profiles/`` in the person's lup config home
(:class:`~lup.providers.user_config.UserConfigFile`), shared by every checkout
they open, so an account signed in once opens in a new repository, and
selected by the ``profile`` that config file records. A checkout's own account
of a name wins inside it, and so does its selection.

Nothing here names a provider. The subdirectory a configuration home takes
inside each profile is the runtime login's word, so one name holds a home for
every runtime side by side, and ``profile=work`` means the same account on
Claude Code and on Codex.
"""

from abc import ABC, abstractmethod
from pathlib import Path

from lup.channels.models import write_atomic
from lup.workspace.checkout_state import CheckoutState
from lup.providers.login import ProviderLogin
from lup.providers.profiles import (
    ProfileDirectory,
    ProfileNames,
    ProfileRegistrar,
    ProfileRegistry,
    ProfileScope,
    ProfileStateLocations,
)
from lup.providers.user_config import UserConfigFile
from lup.types import EnvVars
from lup.workspace.paths import project_root


class ProfileSelection(ABC):
    """Where one root records the name answering for a caller naming none."""

    @abstractmethod
    def recorded(self) -> str | None:
        """The recorded name, where one is recorded."""

    @abstractmethod
    def record(self, name: str) -> None:
        """Record that name, replacing whatever was recorded."""

    @abstractmethod
    def described(self) -> str:
        """Where the selection is kept, as a sentence would name it."""


class ActiveFile(ProfileSelection):
    """A checkout's selection: one name, in a file beside its profiles."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def recorded(self) -> str | None:
        if not self.path.is_file():
            return None
        return self.path.read_text(encoding="utf-8").strip() or None

    def record(self, name: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        write_atomic(self.path, f"{name}\n".encode())

    def described(self) -> str:
        return str(self.path)


class ConfigSelection(ProfileSelection):
    """The global selection: the ``profile`` the person's config file records."""

    def __init__(self, config: UserConfigFile) -> None:
        self.config = config

    def recorded(self) -> str | None:
        return self.config.load().profile

    def record(self, name: str) -> None:
        self.config.select_profile(name)

    def described(self) -> str:
        return f"the `profile` line in {self.config.path()}"


class ProfileFolders:
    """One root's directory layout, as a plain collaborator its capabilities share.

    A collaborator rather than a base class: an implementation that inherited
    its reading would be inheriting behavior alongside a capability, and the
    classes below would then be one class answering for several powers.
    """

    def __init__(
        self, root: Path, home_subdir: str, selection: ProfileSelection
    ) -> None:
        self.root = root
        self.home_subdir = home_subdir
        self.selection = selection

    def names(self) -> list[str]:
        """Every profile directory under the root, in display order.

        A root that does not exist yet holds no profiles rather than
        failing: one is made the first time a profile is added there, and
        every reader before that should see an empty roster instead of an
        error about a directory nobody has had reason to create.
        """
        if not self.root.is_dir():
            return []
        return sorted(
            entry.name
            for entry in self.root.iterdir()
            if entry.is_dir() and not entry.name.startswith(".")
        )

    def home_for(self, name: str) -> Path:
        """The configuration home that name's directory holds."""
        return self.root / name / self.home_subdir

    def container_for(self, name: str) -> Path:
        """The directory holding every runtime home for one profile."""
        return self.root / name


class TreeProfileNames(ProfileNames):
    """Read which accounts one root's profile directories hold."""

    def __init__(self, folders: ProfileFolders) -> None:
        self.folders = folders

    def names(self) -> list[str]:
        return self.folders.names()

    def config_dir_for(self, name: str) -> Path:
        if name not in self.folders.names():
            raise KeyError(name)
        return self.folders.home_for(name)

    def active_profile(self) -> str | None:
        return self.folders.selection.recorded()


class TreeProfileRegistrar(ProfileRegistrar):
    """Start and select one root's profile directories, and refuse to forget one."""

    def __init__(self, folders: ProfileFolders) -> None:
        self.folders = folders

    def add_profile(self, name: str, config_dir: Path | None = None) -> Path:
        """Start a profile directory, for a login to fill in.

        Where its configuration home sits is derived from the name, so a
        caller naming one elsewhere is asking for something a directory
        profile cannot be rather than for a variation on one. An account
        whose home already exists elsewhere is reached by making that path a
        symlink, which resolves like any other — except onto the runtime's
        default home, which the directory refuses however it is reached,
        because naming no profile is what selects that account.
        """
        home = self.folders.home_for(name)
        if config_dir is not None and config_dir != home:
            raise ValueError(
                f"a directory profile keeps its configuration home at {home}, "
                f"derived from the name — {config_dir} cannot be one; symlink "
                "that path to point this profile at a home already elsewhere, "
                "other than the runtime's default home, which naming no profile "
                "already selects"
            )
        home.mkdir(parents=True, exist_ok=True)
        return home

    def set_active(self, name: str) -> None:
        self.folders.selection.record(name)

    def remove_profile(self, name: str) -> None:
        """Refuse, because forgetting one here would mean deleting it.

        Nothing registers a directory profile, so there is no registration to
        drop: the directory is the profile, and it holds the login that
        account earned. Answered as a ``ValueError`` whose message is the
        explanation, which is what a command tree renders in place of one.

        Where that profile is this root's selection, the place recording it
        is named too: removed alone, the directory leaves every launch naming
        none refused for a profile that no longer exists, told to add it back.
        """
        selection = self.folders.selection
        selected = (
            f", and {selection.described()}, which selects it"
            if selection.recorded() == name
            else ""
        )
        raise ValueError(
            f"a directory profile is {self.folders.container_for(name)}, which "
            f"holds its login — remove that directory to remove the "
            f"profile{selected}"
        )


class TreeProfileStateLocations(ProfileStateLocations):
    """Keep auxiliary state beside every runtime home under one profile."""

    def __init__(self, folders: ProfileFolders) -> None:
        self.folders = folders

    def container_for(self, name: str) -> Path:
        if name not in self.folders.names():
            raise KeyError(name)
        return self.folders.container_for(name)


def checkout_profiles(root: Path) -> Path:
    """Where a checkout keeps its own profiles."""
    return CheckoutState(root=root).profiles()


def tree_registry(scope: ProfileScope, folders: ProfileFolders) -> ProfileRegistry:
    """One root's profile directories, as the registry a directory resolves through."""
    return ProfileRegistry(
        scope,
        TreeProfileNames(folders),
        TreeProfileRegistrar(folders),
        TreeProfileStateLocations(folders),
    )


def profile_directory(
    login: ProviderLogin,
    config: UserConfigFile | None = None,
    checkout: Path | None = None,
) -> ProfileDirectory:
    """One runtime's side of the accounts a person keeps, as a directory to curate.

    The one directory a name resolves against — a launch, the usage display,
    a resolver run, a declaration's ``profile`` — so a name opens the same
    account wherever it is spelled: the checkout's own profile of that name,
    else the person's global one. Curating acts on the checkout's unless a
    caller names the global scope. Which runtime's homes it reads is the
    ``login``'s to say, through the subdirectory each takes inside a profile.

    ``config`` is the person's lup config home, read from the environment
    unless a caller names another; ``checkout`` is the checkout whose own
    profiles come first, the project root unless a caller names another.
    """
    person = config or UserConfigFile()
    local = checkout_profiles(checkout or project_root())
    return ProfileDirectory(
        [
            tree_registry(
                "local",
                ProfileFolders(local, login.home_subdir, ActiveFile(local / ".active")),
            ),
            tree_registry(
                "global",
                ProfileFolders(
                    person.profiles_root(), login.home_subdir, ConfigSelection(person)
                ),
            ),
        ],
        login,
    )


def profile_environment(
    login: ProviderLogin,
    name: str | None,
    config: UserConfigFile | None = None,
    checkout: Path | None = None,
) -> EnvVars:
    """The environment a declaration naming ``profile=name`` runs under.

    What an agent's ``profile`` field resolves to, the same way a launch
    naming that profile does: the account's home for this runtime, exported
    through the variable the runtime reads. A name neither the checkout nor
    the person keeps a profile under is refused listing the ones they do,
    and one whose home is the runtime's default is refused as a launch
    refuses it.

    Naming none exports nothing, rather than the selection: a declaration
    opened inside a session stays on the account that session was started
    under, which a launch already chose from that selection.
    """
    if name is None:
        return {}
    return profile_directory(login, config, checkout).account(name).variables
