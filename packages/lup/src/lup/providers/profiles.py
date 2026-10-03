"""Where named configuration homes come from, and the surface over them.

What a profile name means is an origin's to decide. Two capabilities answer
for one, because reading one and curating one are separate powers and most
callers only ever need the first — :class:`ProfileNames` says which names
exist and what each selects, :class:`ProfileRegistrar` registers, selects,
and forgets them. Both are engines, a :class:`ProfileRegistry` holds one
origin's pair under the scope it reaches, and :class:`ProfileDirectory` is the
concrete surface a command tree and a launcher hold over the registries an
application supplied, resolving a name through them in order. The ones lup
ships are :mod:`lup.providers.profile_tree`'s: a checkout's own accounts, then
the person's global ones beside their lup config, a directory each.

Nothing here names a provider. A directory carries the :class:`ProviderLogin`
of the runtime whose homes it holds, so reporting whether one is signed in —
and telling a caller how to sign it in, or whether a profile may name that
runtime's default home at all — is answered in that runtime's own words
rather than in words this module would have to choose.
"""

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from lup.providers.login import ProviderLogin
from lup.types import EnvVars


class SessionAccount(BaseModel, frozen=True):
    """The account every session one entry point opens will run under.

    Named where a run starts rather than inherited from the console, because
    inheriting is what makes a new entry point wrong by omission: it takes
    whichever identity the launching shell happened to export, which is an
    operator's own account whenever the run was not started from a launcher.
    Carried as a value so anything that opens sessions has to be handed one —
    a parameter cannot be forgotten the way an ambient variable can, and that
    is the whole difference between this and reading the environment.

    A ``home`` of ``None`` is a real answer rather than a gap: a project that
    keeps no profiles has none, and a session opened inside another one
    should stay on the account it was started under. It counts as a decision
    because somebody had to construct this to say it.
    """

    name: str | None
    home: Path | None
    variables: EnvVars = {}

    def exported(self, environment: EnvVars) -> EnvVars:
        """That environment with this account's identity written into it."""
        return {**environment, **self.variables}


class UnknownProfile(KeyError):
    """A name no origin answers to, carrying the roster that would.

    Raised by :class:`ProfileDirectory` rather than formatted wherever one is
    caught, because both callers that resolve a name — the command tree and
    the launcher — owe the reader the same answer, and two spellings of it
    drift apart. It stays a ``KeyError`` because that is what an origin
    already raises, so no origin has to learn a new type to be reported well.
    """

    def __init__(self, name: str, known: list[str]) -> None:
        super().__init__(name)
        self.name = name
        self.known = known

    def __str__(self) -> str:
        """The whole diagnostic, since a caller renders this and nothing else."""
        return (
            f"unknown profile {self.name!r}; known: {', '.join(self.known) or 'none'}"
            f" — register one with `profile add {self.name}` for this checkout,"
            " or with `--global` for every checkout"
        )


class DefaultHomeProfile(ValueError):
    """A profile naming the home its runtime opens when no profile is named.

    Refused only where the runtime's login says naming that home outright is
    not naming nothing — :attr:`ProviderLogin.ambient_home_nameable` — which
    turns such a profile into a way to open the default account on state it
    never wrote rather than a way to reach it. One worded refusal for every
    place that registers, selects or resolves a profile, for the reason
    :class:`UnknownProfile` is one; a ``ValueError`` because the name is known
    and its home is what is wrong, which a command tree already renders.

    ``registered`` says whether the profile already exists, and so whether
    the way out starts with removing it. ``name`` is absent where a bare
    selection is refused before it belongs to any name.
    """

    def __init__(
        self, name: str | None, home: Path, variable: str, registered: bool
    ) -> None:
        super().__init__(name, home)
        self.name = name
        self.home = home
        self.variable = variable
        self.registered = registered

    def __str__(self) -> str:
        """The whole diagnostic, since a caller renders this and nothing else."""
        subject = "a profile" if self.name is None else f"profile {self.name!r}"
        default = self.home.expanduser().resolve()
        consequence = (
            f"naming it in {self.variable} reads different configuration than "
            "leaving that unset"
        )
        way_out = "leave the profile unset to use the default account"
        if self.registered and self.name is not None:
            return (
                f"{subject} names the default home {default}, and {consequence} "
                f"— remove it (`profile remove {self.name}`) and {way_out}"
            )
        return (
            f"{subject} cannot name the default home {default}: "
            f"{consequence} — {way_out}"
        )


def named_home(
    login: ProviderLogin, name: str | None, home: Path, registered: bool = True
) -> Path:
    """That home, unless it is one this runtime refuses to have a profile name.

    The one check behind every refusal of a profile naming the default home,
    whether the directory, an origin curating its own names, or a typed
    selection is asking — so each says the same thing about the same home.
    """
    if not login.nameable(home):
        raise DefaultHomeProfile(name, home, login.config_home_env, registered)
    return home


class ProfileNames(ABC):
    """Which named configuration homes one origin knows, and what each selects.

    Injected into :class:`ProfileDirectory` and never held by a consumer
    directly, so an application supplies its own origin without any caller
    above learning where profiles are kept.
    """

    @abstractmethod
    def names(self) -> list[str]:
        """Every profile name this origin knows, in display order."""

    @abstractmethod
    def config_dir_for(self, name: str) -> Path:
        """The configuration home that name selects, or ``KeyError``."""

    @abstractmethod
    def active_profile(self) -> str | None:
        """The name that answers for a caller naming none."""


class ProfileRegistrar(ABC):
    """Curating one origin's named homes: registering, selecting, forgetting.

    Separate from reading them because it is a separate power. A launcher
    resolves a name on every run and never curates; keeping the two apart
    is what lets an origin offer the first without offering the second.
    """

    @abstractmethod
    def add_profile(self, name: str, config_dir: Path | None = None) -> Path:
        """Register name at ``config_dir``, or where this origin puts one."""

    @abstractmethod
    def set_active(self, name: str) -> None:
        """Record name as this origin's selection, already judged reachable."""

    @abstractmethod
    def remove_profile(self, name: str) -> None:
        """Forget name, if this origin knows it."""


class ProfileStateLocations(ABC):
    """Where auxiliary personal state for one named profile belongs."""

    @abstractmethod
    def container_for(self, name: str) -> Path:
        """The directory holding every credential and state for one name."""


class ConfigHomeStateLocations(ProfileStateLocations):
    """Keep auxiliary state inside the configuration home itself."""

    def __init__(self, names: ProfileNames) -> None:
        self.names = names

    def container_for(self, name: str) -> Path:
        return self.names.config_dir_for(name)


type ProfileScope = Literal["local", "global"]
"""How far a registry's profiles reach: ``local`` to one checkout, ``global``
to every checkout its person opens. Narrowest first is resolution order, so a
checkout's own account of a name wins inside it."""


class ProfileRegistry:
    """One place profiles are kept: the names it holds, and how it is curated.

    A holder rather than a capability, bundling the powers one origin offers
    under the scope a caller names it by, so a directory over several can
    resolve through each and curate whichever one it is asked to.
    """

    def __init__(
        self,
        scope: ProfileScope,
        names: ProfileNames,
        registrar: ProfileRegistrar,
        state_locations: ProfileStateLocations | None = None,
    ) -> None:
        self.scope: ProfileScope = scope
        self.names = names
        self.registrar = registrar
        self.state_locations = state_locations or ConfigHomeStateLocations(names)

    def holds(self, name: str) -> bool:
        """Whether this registry keeps a profile under that name."""
        return name in self.names.names()


class Profile(BaseModel, frozen=True):
    """One profile, resolved far enough for a caller to act on it."""

    name: str
    scope: ProfileScope
    config_dir: Path
    resolved: bool
    """Whether a launch naming this profile opens this entry: false where an
    earlier registry keeps the same name, which shadows this one."""
    active: bool
    logged_in: bool
    """Whether that home already holds a completed login, so a listing can
    say which profile a launch would drop into a sign-in prompt."""


class ProfileDirectory:
    """Resolve, list, and curate profiles over the registries that hold them.

    A name resolves through ``registries`` in order, so one an earlier
    registry keeps shadows the same name in a later one, and a caller naming
    none gets the first selection any of them records, resolved the same
    way. Curating acts on the first registry unless a caller names another's
    scope, which is why the narrowest reach comes first: adding a profile
    should not reach further than the caller asked.
    """

    def __init__(self, registries: list[ProfileRegistry], login: ProviderLogin) -> None:
        if not registries:
            raise ValueError("a profile directory needs at least one registry")
        self.registries = registries
        self.login = login

    def registry(self, scope: ProfileScope | None = None) -> ProfileRegistry:
        """The registry that scope names, the first where it names none."""
        if scope is None:
            return self.registries[0]
        for registry in self.registries:
            if registry.scope == scope:
                return registry
        raise ValueError(f"this project keeps no {scope} profiles")

    def unknown(
        self, name: str, searched: list[ProfileRegistry] | None = None
    ) -> UnknownProfile:
        """The error for a name no searched registry answers to, roster included."""
        held = self.registries if searched is None else searched
        return UnknownProfile(
            name,
            sorted({known for registry in held for known in registry.names.names()}),
        )

    def holder(
        self, name: str, searched: list[ProfileRegistry] | None = None
    ) -> ProfileRegistry:
        """The first registry keeping that name, reporting the roster where none does."""
        held = self.registries if searched is None else searched
        for registry in held:
            if registry.holds(name):
                return registry
        raise self.unknown(name, held)

    def resolve(self, name: str) -> Path:
        """The home one name selects, reporting the roster when there is none."""
        try:
            return self.holder(name).names.config_dir_for(name)
        except KeyError as error:
            raise self.unknown(name) from error

    def active_name(self) -> str | None:
        """The first selection a registry records, in resolution order."""
        recorded = (registry.names.active_profile() for registry in self.registries)
        return next((name for name in recorded if name is not None), None)

    def profile(self, name: str, scope: ProfileScope | None = None) -> Profile:
        """One name as it resolves, or as the registry that scope names keeps it."""
        registry = self.holder(
            name, self.registries if scope is None else [self.registry(scope)]
        )
        config_dir = registry.names.config_dir_for(name)
        resolved = registry is self.holder(name)
        return Profile(
            name=name,
            scope=registry.scope,
            config_dir=config_dir,
            resolved=resolved,
            active=resolved and name == self.active_name(),
            logged_in=self.login.logged_in(config_dir),
        )

    def entries(self) -> list[Profile]:
        """Every profile every registry keeps, one name's entries together."""
        names = sorted(
            {name for registry in self.registries for name in registry.names.names()}
        )
        return [
            self.profile(name, registry.scope)
            for name in names
            for registry in self.registries
            if registry.holds(name)
        ]

    def active(self) -> Profile | None:
        """The profile answering for a caller naming none, where there is one."""
        name = self.active_name()
        return None if name is None else self.profile(name)

    def launch_home(self, name: str | None) -> Path | None:
        """The configuration home a launch exports, or None to inherit.

        Naming none while no profile is active is not an error: it leaves
        whatever home the surrounding environment already selected, which is
        both what a project with no profiles expects and what keeps a session
        launched from inside another one on the account it was started under.

        A profile naming the default home is refused here rather than
        launched, whether named or merely active — a refusal at registering
        cannot stop a registration that is already on disk.
        """
        selected = name or self.active_name()
        if selected is None:
            return None
        return named_home(self.login, selected, self.resolve(selected))

    def account(self, name: str | None) -> SessionAccount:
        """The account a run naming this profile opens every session under.

        The seam an entry point reaches for instead of reading the console.
        Resolving once, where the run starts, is what lets everything below
        take the answer as an argument — so a planner, a worker and a
        reviewer opened by the same run are on the same account by
        construction rather than by all happening to inherit one shell.
        """
        home = self.launch_home(name)
        return SessionAccount(
            name=name or self.active_name(),
            home=home,
            variables=self.login.environment(home) if home is not None else dict(),
        )

    def state_dir(self, name: str | None, subdir: str) -> Path | None:
        """One profile-scoped state directory, explicit then active."""
        child = Path(subdir)
        if child.parent != Path(".") or child.name != subdir:
            raise ValueError(f"profile state subdirectory must be one name: {subdir!r}")
        selected = name or self.active_name()
        if selected is None:
            return None
        try:
            return self.holder(selected).state_locations.container_for(selected) / child
        except KeyError as error:
            raise self.unknown(selected) from error

    def add(
        self,
        name: str,
        config_dir: Path | None = None,
        scope: ProfileScope | None = None,
    ) -> Profile:
        """Register a profile in the registry that scope names, and resolve it.

        Judged before the origin sees anything, so a refusal leaves it as it
        was, and refuses as the default home rather than as whatever else the
        origin would say: a home named outright, or else the home the name
        already holds — which is where a directory profile symlinked onto the
        default home is caught, since adding it is what would select it.

        The first profile added where nothing is selected yet becomes the
        selection, recorded in the registry it was added to: somebody with
        exactly one account should not also have to say so. One added beside
        a selection leaves it standing, so a checkout's first profile does not
        quietly take over from the one its person already selected.
        """
        registry = self.registry(scope)
        match config_dir:
            case Path():
                named_home(self.login, name, config_dir, registered=False)
            case None if registry.holds(name):
                named_home(self.login, name, registry.names.config_dir_for(name))
        registry.registrar.add_profile(name, config_dir)
        if self.active_name() is None:
            registry.registrar.set_active(name)
        return self.profile(name, registry.scope)

    def use(self, name: str, scope: ProfileScope | None = None) -> Profile:
        """Record the selection in the registry that scope names, and resolve it.

        A registry's selection may name a profile it keeps or one a registry
        after it keeps, since those reach at least as far as it does: a
        checkout's may select a global account, and the global selection only
        an account every checkout can reach. What it resolves to here still
        follows the order, and an earlier registry's selection still answers
        first — which :attr:`Profile.active` on the result reports.
        """
        registry = self.registry(scope)
        reachable = self.registries[self.registries.index(registry) :]
        holder = self.holder(name, reachable)
        named_home(self.login, name, holder.names.config_dir_for(name))
        registry.registrar.set_active(name)
        return self.profile(name)

    def remove(self, name: str, scope: ProfileScope | None = None) -> Profile:
        """Forget a profile in the registry that scope names, reported as it was."""
        registry = self.registry(scope)
        removed = self.profile(name, registry.scope)
        registry.registrar.remove_profile(name)
        return removed
