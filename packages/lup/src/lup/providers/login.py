"""Where a runtime keeps a stored login, and how to point it at one.

An application that spawns a provider CLI under a chosen account needs facts no
other portable contract carries: the environment variable selecting a
configuration home, the file the runtime writes a completed login into, and how
to tell a login that is merely stale from one nothing can renew. Each is a word
only the provider gets to choose, so each adapter declares its own and every
consumer above holds this value instead of spelling any of them.

The value is a transparent carrier — it composes no seam and decides nothing,
so an application stores one the way it stores the runtime's name.
"""

from pathlib import Path

from pydantic import BaseModel, Field

from lup.types import EnvVars, StringMap


class HomePreparation(BaseModel, frozen=True):
    """A shipped executable that prepares a runtime's private configuration home."""

    executable: str

    def command(
        self,
        root: Path,
        home: Path,
        force: bool = False,
        settings: bool = False,
        report: bool = False,
    ) -> list[str]:
        """Run installed library code in the checkout's own Python environment.

        ``report`` asks for what was installed as JSON on stdout, and nothing
        else there.
        """
        return [
            "uv",
            "run",
            "--locked",
            "--directory",
            str(root),
            self.executable,
            "--root",
            str(root),
            "--home",
            str(home),
            "--trust-project",
            *(["--force"] if force else []),
            *(["--settings-stdin"] if settings else []),
            *(["--report"] if report else []),
        ]


class ProviderLogin(BaseModel, frozen=True):
    """One runtime's stored-login location, in that runtime's own words."""

    config_home_env: str
    """Environment variable pointing this runtime's CLI at a config home."""

    home_preparation: HomePreparation | None = None
    """Runtime-owned state required in a private home before its process starts.

    Kept with the home declaration so a generic container builder cannot
    select a runtime's home while forgetting the preparation that makes it usable.
    A provider needing no private-home installation leaves this absent.
    """

    credentials_file: str
    """What this runtime writes a completed login into, inside that home."""

    credential_fields: list[str] = []
    """Top-level login fields to replace; empty means a dedicated login file."""

    renewable: str = ""
    """A jq filter answering whether a stored login can still reach an account.

    Not whether its access token is current — that expires hourly and renews
    itself, and a consumer acting on it would churn a working login. What this
    asks is whether renewal is still possible at all, because once it is not,
    the file is inert: no request it makes will be answered, and the only way
    back is a fresh sign-in. That distinction is the whole value of the field,
    and it is why the filter reads the refresh credential's life rather than
    the access token's.

    The word is the runtime's own, like the others here. A runtime whose
    stored login states no such deadline declares nothing here, and an empty
    filter means "cannot be asked" rather than "expired". An unchanged host
    login leaves private renewals alone; a changed host login is applied
    independently of whether the previous login can still renew.
    """

    renewed_at: list[str] = []
    """The keys down to a number every renewal of a stored login moves forward.

    How two copies of one login are told apart when each was renewed on its
    own: the larger number is the newer login, and the one every other copy
    takes (:mod:`lup.providers.login_sync`). Empty where lup carries no
    renewal between copies -- a runtime that publishes a renewed login back
    to its account itself, or one whose login says nothing of when it was
    renewed.
    """

    rereads_login: bool = False
    """Whether a running session takes a login written into its home at its next request.

    True where the runtime reads its stored login afresh for every request,
    so handing a contained repository's volume another account's login moves
    every session running on that volume to it. False where a session keeps
    the login it started with until it is opened again, so the same handoff
    reaches each running session only when it relaunches. The answer is the
    runtime's own, measured rather than inferred, and it decides what a
    launch or a switch says it would do to the sessions already running.
    """

    ambient_home: Path
    """Where this runtime's CLI keeps configuration when nothing selects one.

    The provider's own default, held here for the reason the spellings above
    are: a caller that has to name a concrete directory -- a mount, a probe --
    asks the declaration instead of writing one out, and a runtime that moves
    its default moves it in one place. Everywhere a *policy* is being decided
    rather than a file named, ``None`` still means "inherit whatever the
    environment selected" and this is not consulted.
    """

    canonical_home: bool = False
    """Whether the runtime canonicalises the home it selects before it uses it.

    ``True`` where it resolves links and ``..`` in the variable's value and
    in its own default alike, so the home it reads is the resolved path
    rather than the spelling it was handed, and a caller comparing homes
    has to compare that.
    """

    ambient_home_nameable: bool = True
    """Whether naming ``ambient_home`` outright selects what naming nothing does.

    ``False`` where the runtime reads state beside that home only while the
    variable is unset, so a session pointed at the very home it would have
    chosen anyway opens on state its account never wrote. No profile may name
    such a home: the one account it could mean is the one naming no profile
    already selects, and leaving the profile unset is how to reach it.
    """

    editor_lockfiles: str = ""
    """Subdirectory of the configuration home an editor rendezvous sits in.

    One editor window and one CLI find each other by a lockfile here: a port
    and a token, written by one and read by the other. Empty means this
    runtime offers no such bridge, which is a fact about the vendor and not
    an omission -- Codex's extension drives an app-server and spawns its own
    core, so there is no rendezvous to bridge and nothing to mount.
    """

    state_volume: str = Field(pattern=r"^[a-z0-9-]+$")
    """This runtime's word in the name of its per-repository state volume.

    A contained session's configuration home is a volume per repository and
    per runtime, so one runtime's sessions never read another's transcripts
    or write the files it reads its settings from. Two runtimes sharing a
    volume is what left a home holding both CLIs' files mixed together.
    """

    home_entries: list[str] = []
    """What this runtime keeps at the top of its configuration home, as names or globs.

    The table a volume holding more than one runtime's home is split by:
    each entry goes to every runtime naming it, so one both runtimes write
    (a shared cache, a ``sessions`` directory both use) goes to both, and one
    no runtime names goes to both too, said aloud as unknown.
    """

    home_debris: list[str] = []
    """What this runtime leaves at the top of its home that nothing reads again.

    A write it interrupted between its temporary file and the rename, say. A
    split drops these rather than carrying them into a fresh volume.
    """

    trust_document: str = ""
    """The document in this runtime's home that records workspace trust, if any.

    A contained session's entrypoint seeds it and merges the checkout's
    trust into it at every start. Empty where the runtime keeps trust
    somewhere a launch writes itself, so its home gains no stray file.
    """

    account_id: list[str] = []
    """The keys in :attr:`trust_document` down to the id of the account signed in.

    Who a login is, as opposed to which home it is kept in: two copies of
    one login share it, and a home someone signed in to another account
    from changes it. Empty where the runtime records no such id."""

    account_name: list[str] = []
    """The keys in :attr:`trust_document` down to how a person names that account."""

    def account_document(self, home: Path) -> Path:
        """The document saying whose login *home* keeps: beside the runtime's own default, inside any other."""
        if not self.ambient_home_nameable and home.expanduser() == self.ambient_home:
            return home.expanduser().parent / self.trust_document
        return home / self.trust_document

    home_subdir: str
    """Subdirectory this runtime's configuration home takes inside a profile.

    A project keeping its accounts as directories gives each one a directory
    and each runtime a place inside it, so one name can hold a login for every
    runtime that name runs. The word is the runtime's own, for the same reason
    the two above are: naming it anywhere else would decide for one provider in
    a module every provider passes through.
    """

    def environment(self, home: Path) -> EnvVars:
        """The environment routing a spawned CLI at that configuration home."""
        return {self.config_home_env: str(home)}

    def named_home(self, environ: StringMap) -> Path | None:
        """The home that environment names outright, or ``None`` where it names none.

        An exported-but-empty variable names none: it is how a shell says
        nothing, and reading it as a path would name whatever directory the
        reader happened to run in.
        """
        named = environ[self.config_home_env] if self.config_home_env in environ else ""
        return Path(named).expanduser() if named else None

    def default_home(self, environ: StringMap) -> Path:
        """Where the runtime keeps its configuration under that environment when nothing names one.

        Its own default directory, named as :attr:`ambient_home` is, in the
        effective user's home: the environment's ``HOME`` where it carries
        one, which is what the CLI itself reads, and :attr:`ambient_home` as
        declared where it carries none.
        """
        user = environ["HOME"] if "HOME" in environ else ""
        joined = Path(user) / self.ambient_home.name if user else self.ambient_home
        return joined.resolve() if self.canonical_home else joined

    def selected_home(self, environ: StringMap) -> Path:
        """The home this runtime's CLI would use under that environment.

        The one resolver from an environment to a configuration home: what a
        spawned session will write to, where its transcripts are found, and
        what a *sibling process* resolves, which is the question a mount asks
        — the editor on the host and the CLI in the container are two
        programs reading the same variable, and bridging them means naming
        the directory the one outside is using rather than the one this
        launch chose.
        """
        named = self.named_home(environ)
        if named is None:
            return self.default_home(environ)
        return named.resolve() if self.canonical_home else named

    def nameable(self, home: Path) -> bool:
        """Whether a profile may point this runtime at that home by name.

        Both sides are expanded and resolved before they are compared, so a
        spelling with ``~`` or ``..``, and a symlink onto the ambient home,
        all name the ambient home.
        """
        return self.ambient_home_nameable or (
            home.expanduser().resolve() != self.ambient_home.expanduser().resolve()
        )

    def editor_rendezvous(self, environ: StringMap) -> Path | None:
        """Where an editor on this machine keeps its lockfiles, if it can.

        ``None`` where the runtime declares no bridge, so a caller that would
        mount one asks a single question instead of testing the spelling and
        then building the path from it.
        """
        if not self.editor_lockfiles:
            return None
        return self.selected_home(environ) / self.editor_lockfiles

    def credentials_path(self, home: Path) -> Path:
        """Where a completed login sits inside that configuration home."""
        return home / self.credentials_file

    def logged_in(self, home: Path) -> bool:
        """Whether that configuration home already holds a completed login."""
        return self.credentials_path(home).exists()

    def withheld_logins(self) -> list[str]:
        """Where a completed login sits, spelled as a path policy withholds it.

        The runtime's default home, spelled from a home rather than from this
        machine's, and the directory a profile gives it wherever a project
        keeps its profiles.
        """
        ambient = (
            f"~/{self.ambient_home.relative_to(Path.home()).as_posix()}"
            if self.ambient_home.is_relative_to(Path.home())
            else self.ambient_home.as_posix()
        )
        return [
            f"{ambient}/{self.credentials_file}",
            f"**/{self.home_subdir}/{self.credentials_file}",
        ]
