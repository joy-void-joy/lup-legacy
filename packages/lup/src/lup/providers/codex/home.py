"""Stable per-worktree Codex homes for locally installed harness plugins.

A worktree's home exists for what a home installs: this project's plugin, its
registration, and the trust Codex records for the hooks it runs. Everything
else in it is the person's — their login, their settings, the checkouts they
trust — and belongs to the account home it is derived from, which is theirs
across every project. So each launch derives the settings afresh from that
account home, keeping only what this home installed, and a session's own
changes to them go back to the account when it closes: a new checkout opens
with the settings the person has, not the ones some other checkout was
seeded with once.
"""

import asyncio
import json
import logging
import shutil
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

import jwt
import tomlkit
from tomlkit.container import Container
from tomlkit.items import Table
from pydantic import BaseModel, TypeAdapter, ValidationError

from lup.harness.assets.home_seed import RECORD, three_way
from lup.launch.homes import checkout_directory, claimed_directory, homes_root
from lup.channels.models import write_atomic
from lup.providers.codex.harness_runtime import (
    CodexPluginInstaller,
    PluginCacheConfig,
    codex_home_lock,
    replace_codex_config,
)
from lup.providers.codex.login import CODEX_LOGIN
from lup.providers.codex.marketplace import CodexMarketplace
from lup.providers.codex.preferences import (
    CodexSettingsReturn,
    SettingChange,
    changed_settings,
    codex_setting_flow,
    setting_leaves,
)
from lup.providers.codex.theme import CodexTheme, claude_daltonized_theme
from lup.providers.user_config import EditorMode, UserConfigFile
from lup.sandbox.rail import sibling_worktrees
from lup.types import JsonObject
from lup.providers.codex.trust import CodexHook, hooks_of, read_hooks, skipped
from lup.types import EnvVars
from lup.workspace.paths import declared_project_root, project_root


logger = logging.getLogger(__name__)


# lup: ignore[constant-declaration] — the keys Codex writes its own state under
CODEX_CONFIG_STATE_KEYS = ("marketplaces", "plugins")

# lup: ignore[constant-declaration] — Codex's own spelling for where a home
# records which checkouts it will read the configuration of
PROJECTS_KEY = "projects"

# lup: ignore[constant-declaration] — Codex's own spelling for the interface's
# table, as its `/theme` writes it
TUI_KEY = "tui"
# lup: ignore[constant-declaration] — Codex's own key for the theme drawn
THEME_KEY = "theme"
# lup: ignore[constant-declaration] — Codex's own key for opening the composer
# in vim mode, measured on 0.156.1
VIM_DEFAULT_KEY = "vim_mode_default"
# lup: ignore[constant-declaration] — where Codex reads a theme's file from
THEMES_DIR = "themes"

# lup: ignore[constant-declaration] — Codex's own word for that decision; a
# caller given it to set would be writing a record the runtime cannot read
TRUSTED_PROJECT = "trusted"
"""How a home records that it will read one checkout's own configuration."""

# lup: ignore[constant-declaration] — Codex's own spelling for the state a home
# keeps its hook decisions under; a caller given these to set would be writing
# records the runtime reads somewhere else
HOOKS_KEY = "hooks"
# lup: ignore[constant-declaration] — the sub-map each hook's decision sits in
STATE_KEY = "state"
# lup: ignore[constant-declaration] — the digest a decision was recorded against
TRUSTED_HASH_KEY = "trusted_hash"
# lup: ignore[constant-declaration] — whether the hook runs at all
ENABLED_KEY = "enabled"
"""How a home records which hook definitions it has answered for.

Keyed by the name the runtime reports for each hook, so nothing here
constructs one: a record written under a name this composed would be a
record Codex never reads, and the failure looks exactly like an untrusted
hook."""


class CodexHomeSelection(BaseModel, frozen=True):
    """The Codex home selected for one harness launch."""

    path: Path
    isolated: bool


class CodexAccessClaims(BaseModel, extra="ignore"):
    """The one claim this adapter reads out of an access token."""

    exp: datetime | None = None


class CodexTokens(BaseModel, extra="ignore"):
    """The issued tokens, of which only the access token is ever read."""

    access_token: str = ""


class CodexCredential(BaseModel, extra="ignore"):
    """The parts of a Codex auth record this adapter reasons about.

    Extra keys are ignored, and the access token is read for one purpose: the
    issuer states its own expiry inside it, which beats inferring a lifetime
    this project would have to guess at. Nothing here logs or reproduces a
    token, and records move between homes as whole files.
    """

    last_refresh: datetime | None = None
    tokens: CodexTokens = CodexTokens()

    def expires_at(self) -> datetime | None:
        """When the issuer says this access token stops being accepted."""
        if not self.tokens.access_token:
            return None
        try:
            claims = jwt.decode(
                self.tokens.access_token,
                options={"verify_signature": False},
            )
        except jwt.PyJWTError:
            return None
        return CodexAccessClaims.model_validate(claims).exp


def sanitized_codex_config(content: str) -> str:
    """Keep personal settings, including hook trust, without installed state.

    ``marketplaces`` and ``plugins`` record what one home has installed, and a
    scoped home installs its own against a verified digest — carrying them
    over would claim an install that never happened here.

    Hook trust is the opposite and is kept. The runtime refuses to run a
    plugin's hooks until they are reviewed, so a scoped home seeded without
    that decision installs the policy plugin and then runs ungoverned: the
    dispatcher is present, never consulted, and nothing says so. Seeding the
    operator's own trust is what makes a session here run under the policy
    they already granted — and only that, since a decision they never made is
    not in the file being copied.
    """
    document = tomlkit.parse(content)
    for key in CODEX_CONFIG_STATE_KEYS:
        if key in document:
            document.remove(key)
    return tomlkit.dumps(document)


def derived_codex_config(account: str, scoped: str) -> str:
    """The configuration a worktree home opens with: the account's, and its own installs.

    The account's settings as they stand now, sanitized as a seed always was,
    with what only this home can say laid back over them — the plugins it
    installed and registered, and the trust it recorded for checkouts and
    hooks, which join the account's own trust rather than replacing it.
    """
    document = tomlkit.parse(sanitized_codex_config(account))
    own = tomlkit.parse(scoped)
    for key in CODEX_CONFIG_STATE_KEYS:
        if key in own:
            document[key] = own[key]
    for path in ((PROJECTS_KEY,), (HOOKS_KEY, STATE_KEY)):
        recorded = table_at(own, path)
        held = table_at(document, path, create=True) if recorded else None
        if recorded is not None and held is not None:
            for name, entry in recorded.items():
                held[name] = entry
    return tomlkit.dumps(document)


def themed_codex_config(content: str, named: str | None, fallback: str) -> str:
    """A derived configuration drawing the theme it should.

    ``named`` is the person's lup config naming one outright, which wins over
    the account's for the session; unnamed, the account's own stands, and
    only where it keeps none is ``fallback``, lup's own, filled in. Written
    into the derived home rather than passed as a launch override, which
    would outrank a session's ``/theme`` for as long as the session ran.
    """
    document = tomlkit.parse(content)
    tui = table_at(document, (TUI_KEY,), create=True)
    if tui is None or (named is None and THEME_KEY in tui):
        return content
    tui[THEME_KEY] = named or fallback
    return tomlkit.dumps(document)


def personalized_codex_config(
    content: str, settings: JsonObject, editor: EditorMode | None
) -> str:
    """A derived configuration with the person's own settings laid over it.

    ``settings`` is the lup config's ``[codex.settings]`` table, each leaf
    replacing the account's; ``editor`` is its portable editor mode,
    spelled as the vim default Codex reads. Both win over the account the
    way the named theme does, and both are written into the derived home
    rather than passed as launch overrides a session could not change.
    """
    leaves = [
        *setting_leaves(settings),
        *(
            [SettingChange(path=(TUI_KEY, VIM_DEFAULT_KEY), value=editor == "vim")]
            if editor is not None
            else []
        ),
    ]
    return settled_codex_config(content, leaves) if leaves else content


def table_at(
    document: Container, path: tuple[str, ...], create: bool = False
) -> Container | Table | None:
    """The table at a dotted path, made where ``create`` asks and it is absent.

    ``None`` where the path is absent and nothing asked for it, or where
    something other than a table already sits on it.
    """
    table: Container | Table = document
    for key in path:
        if key not in table:
            if not create:
                return None
            table[key] = tomlkit.table(is_super_table=True)
        child = table[key]
        if not isinstance(child, Table):
            return None
        table = child
    return table


def personal_settings(content: str) -> JsonObject:
    """Everything in one configuration that is the person's to carry between homes.

    Not the installs, which each home makes for itself, and not the trust,
    which a home records for the checkouts and hooks it met: trust granted in
    one worktree's home is a decision about that worktree.
    """
    settings = dict(tomlkit.parse(content).unwrap())
    for key in (*CODEX_CONFIG_STATE_KEYS, PROJECTS_KEY):
        settings.pop(key, None)
    hooks = settings.get(HOOKS_KEY)
    if isinstance(hooks, dict):
        remaining = {name: value for name, value in hooks.items() if name != STATE_KEY}
        if remaining:
            settings[HOOKS_KEY] = remaining
        else:
            settings.pop(HOOKS_KEY)
    return settings


def settled_codex_config(account: str, changes: list[SettingChange]) -> str:
    """The account's configuration with a session's changes applied, lines kept."""
    document = tomlkit.parse(account)
    for change in changes:
        table = table_at(document, change.path[:-1], create=change.value is not None)
        if table is None:
            continue
        if change.value is None:
            table.pop(change.path[-1], None)
        else:
            table[change.path[-1]] = change.value
    return tomlkit.dumps(document)


SEED_RECORD = f"{RECORD}/config.json"
"""The record of what a launch last installed as a home's base settings."""


class CodexSeed(BaseModel, frozen=True):
    """What one launch's settings come to against a home a session may be running in."""

    settings: JsonObject
    conflicts: list[str] = []
    """Settings both a running session and the person changed, where the person won."""


def seeded_codex_settings(
    current: str | None, recorded: str | None, theirs: JsonObject
) -> CodexSeed:
    """Merge a launch's settings into a home's three ways, against the last launch's.

    ``current`` is the home's configuration as it stands, ``recorded`` what the
    last launch installed (:data:`SEED_RECORD`), ``theirs`` this launch's
    personal settings. A setting only a running session changed keeps its
    value, one only the person changed takes theirs, and where both changed
    the person's wins and is named. With no record, the settings replace.
    """
    ours = personal_settings(current) if current is not None else {}
    base = (
        TypeAdapter(JsonObject).validate_json(recorded)
        if recorded is not None
        else ours
    )
    merged = three_way(base, ours, theirs)
    return CodexSeed(
        settings=TypeAdapter(JsonObject).validate_python(
            merged.value if isinstance(merged.value, dict) else {}
        ),
        conflicts=merged.conflicts,
    )


def read_credential(path: Path) -> CodexCredential | None:
    """Read one auth record, or None when it is absent or unreadable."""
    if not path.is_file():
        return None
    try:
        return CodexCredential.model_validate_json(path.read_text(encoding="utf-8"))
    except ValidationError:
        logger.warning("Codex credential at %s is unreadable; leaving it alone", path)
        return None


def credential_refreshed_at(path: Path) -> datetime | None:
    """Read when a credential last rotated, or None when that is unknowable."""
    credential = read_credential(path)
    return None if credential is None else credential.last_refresh


def sync_credential(source: Path, target: Path) -> bool:
    """Copy a credential toward the side that is provably out of date.

    A Codex login is one rotating chain, not a file: the runtime refreshes it
    in place under whichever home it runs, and a rotation spends the refresh
    token every other copy still holds. Copying once and diverging therefore
    hands stale homes a credential that cannot even refresh itself, so the
    account home is the record and each launch converges against it.

    Without two readable timestamps there is no proof of which side is
    current, so the target is preserved rather than guessed at.
    """
    if not source.is_file():
        return False
    if not target.exists():
        shutil.copy2(source, target)
        return True
    source_refreshed = credential_refreshed_at(source)
    target_refreshed = credential_refreshed_at(target)
    if source_refreshed is None or target_refreshed is None:
        return False
    if source_refreshed <= target_refreshed:
        return False
    shutil.copy2(source, target)
    return True


def carried_themes(
    source: Path, destination: Path, overwrite: bool = False
) -> list[str]:
    """Copy the theme files one home keeps to another, by name.

    A theme is selected by name and drawn from a file in the selecting home,
    so a home derived from an account draws the account's own themes only if
    it holds their files — refreshed at every derivation, since the account's
    are the originals — and an account a session's choice returns to draws it
    only if the file came back too, never over one the account already has.
    """
    themes = source / THEMES_DIR
    if not themes.is_dir():
        return []
    missing = [
        theme
        for theme in sorted(themes.iterdir())
        if theme.is_file()
        and (overwrite or not (destination / THEMES_DIR / theme.name).exists())
    ]
    for theme in missing:
        (destination / THEMES_DIR).mkdir(parents=True, exist_ok=True)
        shutil.copy2(theme, destination / THEMES_DIR / theme.name)
    return [theme.name for theme in missing]


def trust_project(home: Path, worktree: Path) -> bool:
    """Record that a scoped home trusts the checkout it was made for.

    The runtime reads a project's own ``.codex/config.toml`` only for a
    project its home trusts, and says nothing at all when it declines to.
    That file is where a generated tree declares its tool servers, so a first
    session in a fresh home opens with every one of them silently absent —
    the config written, never read, and a session with no instruments looking
    exactly like one that had none to declare. The same shape
    :func:`sanitized_codex_config` keeps hook trust for, and the same reason.

    Nothing is granted that was not already: the launch generated that file
    from this repository's own declaration moments earlier, and trusting what
    we just wrote is not a decision about anybody's code. Only ever in a home
    this project made for this checkout — an operator's own home carries
    their own decisions, and those are not ours to write.
    """
    config = home / "config.toml"
    with codex_home_lock(home):
        document = (
            tomlkit.parse(config.read_text(encoding="utf-8"))
            if config.is_file()
            else tomlkit.document()
        )
        projects = document.setdefault(PROJECTS_KEY, tomlkit.table(is_super_table=True))
        named = str(worktree.expanduser().resolve())
        if named in projects:
            return False
        entry = tomlkit.table()
        entry["trust_level"] = TRUSTED_PROJECT
        projects[named] = entry
        replace_codex_config(home, tomlkit.dumps(document))
    return True


class CodexLoginState(BaseModel, frozen=True):
    """Whether a home holds a login, and when the issuer stops accepting it."""

    present: bool
    expires_at: datetime | None = None

    def usable_at(self, moment: datetime) -> bool:
        """A login is usable until the issuer's own expiry passes.

        An unreadable record, or one whose token states no expiry, is taken as
        usable: the runtime is the authority on a shape this cannot read, and
        guessing the other way would send someone to a sign-in they may not
        need.
        """
        return self.present and (self.expires_at is None or moment < self.expires_at)


def login_state(home: Path) -> CodexLoginState:
    """Read the login one Codex home would start a session with."""
    credential = read_credential(home / CODEX_LOGIN.credentials_file)
    if credential is None:
        return CodexLoginState(present=False)
    return CodexLoginState(present=True, expires_at=credential.expires_at())


class CodexWorktreeHomeStore:
    """Derive persistent Lup-owned Codex homes from one personal account.

    ``account_home`` is the account's own home — the operator's default,
    joined onto this process's home when the login is imported, unless a
    profile names another. ``launched_record`` is where a home keeps the
    personal settings it was derived with, so what a session changed can be
    told from what the account changed meanwhile. ``theme`` is the one the
    person's lup config names, if it names one; ``fallback_theme`` is lup's
    own, drawn only where neither that nor the account names one.
    ``editor`` and ``settings`` are the lup config's editor mode and
    ``[codex.settings]`` table, laid over the account's at every launch.
    ``homes`` is where the homes are kept, :func:`codex_homes_root` unless
    named.
    """

    def __init__(
        self,
        account_home: Path = CODEX_LOGIN.ambient_home,
        homes: Path | None = None,
        launched_record: str = ".lup-launched.json",
        theme: str | None = None,
        fallback_theme: CodexTheme | None = None,
        editor: EditorMode | None = None,
        settings: JsonObject | None = None,
    ) -> None:
        self.account_home = account_home
        self.homes = homes or homes_root()
        self.launched_record = launched_record
        self.theme = theme
        self.fallback_theme = fallback_theme or claude_daltonized_theme()
        self.editor: EditorMode | None = editor
        self.settings = settings or {}

    def checkout_for(self, worktree: Path) -> Path:
        """The checkout whose home a worktree opens: the declared project enclosing it.

        A worktree named below the root answers with the root's, rather than
        opening a second home beneath itself; one enclosed by no declared
        project answers for itself.
        """
        canonical = worktree.expanduser().resolve()
        return declared_project_root(canonical) or canonical

    def home_for(self, worktree: Path) -> Path:
        """The home belonging to the checkout that encloses one worktree.

        In lup's state rather than the checkout, as :mod:`lup.launch.homes`
        says why, under the runtime's own word for a home, so the policy
        withholds the login in it the way it withholds one in any profile's.
        """
        return (
            checkout_directory(self.homes, self.checkout_for(worktree))
            / CODEX_LOGIN.home_subdir
        )

    def derived(self, home: Path) -> bool:
        """Whether this store made that home, which decides what may be written.

        Asked of the path rather than carried beside it because a session
        reaches its home as one environment variable, and a second variable
        saying "this one may be written into" would be a claim any caller
        who controls the environment could make. The path is the claim: only
        this store puts a home under :attr:`homes`, and a home somewhere
        else is somebody's own.
        """
        resolved = home.expanduser().resolve()
        return (
            resolved.name == CODEX_LOGIN.home_subdir
            and resolved.parent.parent == self.homes.expanduser().resolve()
        )

    def claimed(self, worktree: Path) -> Path:
        """The checkout's home, where its directory is made and says whose it is.

        So a sweep can tell a home whose checkout is gone, which a digest
        cannot say by running backwards.
        """
        directory = claimed_directory(self.homes, self.checkout_for(worktree))
        return directory / CODEX_LOGIN.home_subdir

    def prepare(self, worktree: Path) -> Path:
        """Refresh Lup-owned files and derive the account's settings into a scoped home.

        Derived at every launch rather than seeded once, because a seed is
        the account as it stood the day this checkout first launched: every
        setting the person changed since — in their own home, or in a
        session in any other checkout — would never arrive here.
        """
        scoped_home = self.claimed(worktree)
        scoped_home.mkdir(mode=0o700, exist_ok=True)
        scoped_home.chmod(0o700)
        carried_themes(self.account_home, scoped_home, overwrite=True)
        self.fallback_theme.write(scoped_home)
        sync_credential(
            self.account_home / CODEX_LOGIN.credentials_file,
            scoped_home / CODEX_LOGIN.credentials_file,
        )
        account = self.account_home / "config.toml"
        config = scoped_home / "config.toml"
        with codex_home_lock(scoped_home):
            derived = themed_codex_config(
                personalized_codex_config(
                    derived_codex_config(
                        account.read_text(encoding="utf-8")
                        if account.is_file()
                        else "",
                        config.read_text(encoding="utf-8") if config.is_file() else "",
                    ),
                    self.settings,
                    self.editor,
                ),
                self.theme,
                self.fallback_theme.slug,
            )
            replace_codex_config(scoped_home, derived)
        trust_project(scoped_home, worktree)
        write_atomic(
            scoped_home / self.launched_record,
            json.dumps(personal_settings(derived)).encode("utf-8"),
        )
        return scoped_home

    def publish(self, worktree: Path) -> bool:
        """Return a credential the session rotated back to the account home.

        The account home is made where it does not exist yet: a profile's
        first Codex sign-in happens in a worktree home, and has to arrive in
        the account it signed in as.
        """
        scoped_home = self.home_for(worktree)
        self.account_home.mkdir(mode=0o700, parents=True, exist_ok=True)
        return sync_credential(
            scoped_home / CODEX_LOGIN.credentials_file,
            self.account_home / CODEX_LOGIN.credentials_file,
        )

    def return_settings(
        self,
        worktree: Path,
        config: UserConfigFile,
        current: str | None = None,
        applied: JsonObject | None = None,
    ) -> CodexSettingsReturn:
        """Carry back every personal setting a session changed, each where it belongs.

        Measured against what the home was derived with rather than against
        the account, so a setting the person changed in their own home while
        this session ran is not undone by a session that never touched it.
        ``current`` is the configuration the session left, where it ran
        somewhere other than this home — a container's volume, read back —
        and ``applied`` what that home was given, where a three-way settle
        kept what another session running there had changed.
        Sorted by :mod:`lup.providers.codex.preferences`: a portable
        setting to the lup config (to the account where the lup config
        would refuse it), another returning one to the account's own
        ``config.toml`` through its parsed document, keeping its lines;
        the session's own model and effort, and anything withheld, nowhere.
        """
        scoped_home = self.home_for(worktree)
        record = scoped_home / self.launched_record
        held = scoped_home / "config.toml"
        left = (
            current
            if current is not None
            else (held.read_text(encoding="utf-8") if held.is_file() else None)
        )
        if left is None or (applied is None and not record.is_file()):
            return CodexSettingsReturn()
        launched = (
            applied
            if applied is not None
            else TypeAdapter(JsonObject).validate_json(
                record.read_text(encoding="utf-8")
            )
        )
        now = personal_settings(left)
        changes = list(changed_settings(launched, now))
        returned = CodexSettingsReturn.sorted_out(changes, config.load())
        account_changes = list(returned.account)
        try:
            if returned.portable:
                config.record(returned.portable)
        except ValueError:
            account_changes = [
                change
                for change in changes
                if codex_setting_flow(change.path) == "returns"
            ]
        if account_changes:
            account = self.account_home / "config.toml"
            self.account_home.mkdir(mode=0o700, parents=True, exist_ok=True)
            with codex_home_lock(self.account_home):
                replace_codex_config(
                    self.account_home,
                    settled_codex_config(
                        account.read_text(encoding="utf-8")
                        if account.is_file()
                        else "",
                        account_changes,
                    ),
                )
        write_atomic(record, json.dumps(now).encode("utf-8"))
        carried_themes(scoped_home, self.account_home)
        return returned


def select_codex_home(
    explicit_home: Path | None,
    environment: EnvVars,
    worktree: Path,
    store: CodexWorktreeHomeStore | None = None,
) -> CodexHomeSelection:
    """Prefer explicit homes, otherwise prepare the worktree-scoped default.

    A home the environment names is read the way Codex reads it: an empty
    ``CODEX_HOME`` names none, and Codex falls back to its default rather
    than resolving it. Taken as a path, it is ``.`` — whichever directory
    the caller happens to stand in — and a session routed there has its
    policy plugin installed into that directory as though it were a home.
    """
    if explicit_home is not None:
        return CodexHomeSelection(path=explicit_home, isolated=False)
    if named := environment.get(CODEX_LOGIN.config_home_env):
        return CodexHomeSelection(path=Path(named), isolated=False)
    active_store = store or CodexWorktreeHomeStore()
    return CodexHomeSelection(
        path=active_store.prepare(worktree),
        isolated=True,
    )


def move_codex_homes(
    root: Path, store: CodexWorktreeHomeStore, *, dry_run: bool = False
) -> list[str]:
    """Move each checkout's Codex home out of ``.lup/codex-home``, to where ``store`` keeps it.

    The one step a checkout that kept its home inside itself takes, once:
    every worktree of the repository, since each kept one. Moved rather than
    copied, so one login chain goes on renewing and the history a resumed
    session reopens goes with it; a home already kept for that checkout is
    left beside the one here rather than overwritten, since either may hold
    history the other does not.
    """

    def moved(worktree: Path) -> Iterator[str]:
        kept = worktree / ".lup" / "codex-home"
        if not kept.is_dir():
            return
        destination = store.home_for(worktree)
        if destination.exists():
            yield (
                f"{kept}: kept, since {destination} already holds this checkout's "
                "home; merge the two by hand and remove this one"
            )
            return
        if not dry_run:
            shutil.move(kept, store.claimed(worktree))
        yield f"{kept} -> {destination}"

    return [
        line
        for worktree in [root, *sibling_worktrees(root)]
        for line in moved(worktree)
    ]


class CodexPolicyUntrusted(RuntimeError):
    """A home carries the policy plugin and would run none of its hooks."""


def seed_hook_trust(home: Path, hooks: list[CodexHook]) -> list[str]:
    """Answer for this project's own hooks, in a home this project made.

    Only ever for a home derived for this checkout, and only over hooks the
    runtime has just reported for the plugin generated from this
    repository's declarations — the same act, and the same reasoning, as
    :func:`trust_project`. What an operator's grant protects them from is
    third-party code they did not write; these bytes were rendered here
    minutes ago, are guarded by the ownership manifest and the drift check,
    and the decision to run them is the decision to launch this checkout.

    Granting it per hash is what the alternative could not do. A generated
    plugin's digest moves every time anything it is compiled from moves, so
    the interactive grant is not one decision but a prompt at every
    regeneration — and a security question asked that often is answered by
    reflex, which is worse than the record written here in the open.

    The hash comes from the runtime rather than from a digest recomputed
    here, so a record either names the definition Codex is holding or is not
    written at all.
    """
    config = home / "config.toml"
    with codex_home_lock(home):
        document = (
            tomlkit.parse(config.read_text(encoding="utf-8"))
            if config.is_file()
            else tomlkit.document()
        )
        hooks_table = document.setdefault(HOOKS_KEY, tomlkit.table(is_super_table=True))
        state = hooks_table.setdefault(STATE_KEY, tomlkit.table(is_super_table=True))
        for hook in hooks:
            entry = tomlkit.table()
            entry[TRUSTED_HASH_KEY] = hook.current_hash
            entry[ENABLED_KEY] = True
            state[hook.key] = entry
        replace_codex_config(home, tomlkit.dumps(document))
    return [hook.key for hook in hooks]


def install_declared_policy(
    home: Path,
    root: Path | None = None,
    seed: bool = False,
    workspace: Path | None = None,
    *,
    executable: Path = Path("codex"),
    environment: EnvVars | None = None,
) -> CodexMarketplace | None:
    """Install the project's own plugin into one home, and say which it was.

    The half ``sanitized_codex_config`` promises and nothing performed for a
    session. Stripping account-wide plugin state is right — a scoped home
    should install its own against a verified digest — but a home that then
    installs nothing carries no dispatcher, and every refusal the project
    declares goes unenforced with nothing saying so.

    Installing it is not the whole of carrying it, which is why the trust is
    read here too, and read from the runtime: Codex resolves a home's hooks
    against records this does not write the names of, so what a home would
    actually run is its answer to give.

    ``seed`` is for a home this project derived for this checkout, where
    :func:`seed_hook_trust` answers for the plugin generated here. Off by
    default, so a home the operator brought — an explicit ``--codex-home``,
    or whatever the environment already selected — keeps their decisions
    and is refused rather than written into.

    ``root`` selects the application's policy declaration. ``workspace`` is
    the native session directory whose effective hook configuration is checked;
    a temporary working directory need not contain another marketplace.
    ``executable`` and ``environment`` select the same native process boundary
    as the session, including plugin discovery and hook trust queries.

    Failure is raised rather than warned past, either way. A session that
    opened without the policy it was meant to run under is
    indistinguishable from one running under it, which is the property that
    made this worth finding.
    """
    project = root or project_root()
    native_cwd = workspace or project
    declared = CodexMarketplace.declared(project)
    if declared is None:
        return None
    installer = CodexPluginInstaller(
        PluginCacheConfig(
            codex_home=home, marketplace=declared.name, plugin=declared.plugin
        ),
        executable=executable,
        environment=environment,
    )
    evidence = installer.ensure(declared.source, project)
    installer.verify(evidence, native_cwd)
    unanswered = policy_hooks_skipped(
        home,
        native_cwd,
        declared,
        policy_root=project,
        executable=executable,
        environment=environment,
    )
    if unanswered and seed:
        # Verified rather than asserted: the records were written from the
        # hashes this read reported, and whether Codex then honours them is
        # the runtime's answer rather than a property of having written a
        # file. A record it rejects reads exactly like one never written.
        seed_hook_trust(home, unanswered)
        unanswered = policy_hooks_skipped(
            home,
            native_cwd,
            declared,
            policy_root=project,
            executable=executable,
            environment=environment,
        )
    if unanswered:
        raise CodexPolicyUntrusted(
            f"{home} carries {declared.selector} from policy root {project} in native "
            f"workspace {native_cwd} and would skip "
            f"{len(unanswered)} declared hook(s): "
            f"{', '.join(hook.key for hook in unanswered)}. Codex skips a hook it "
            "does not trust rather than refusing it, so this session would run "
            "ungoverned and read exactly like one that is governed. Open an "
            "interactive session in this checkout and trust the plugin there, "
            "which is where Codex asks."
        )
    return declared


def policy_hooks_skipped(
    home: Path,
    project: Path,
    marketplace: CodexMarketplace,
    *,
    policy_root: Path | None = None,
    executable: Path = Path("codex"),
    environment: EnvVars | None = None,
) -> list[CodexHook]:
    """Which of this plugin's hooks the home resolves but would not run."""
    report = asyncio.run(
        read_hooks(home, project, executable=executable, environment=environment)
    )
    if report.warnings():
        logger.warning(
            "Native hook discovery reported %s warning(s) for %s in %s; "
            "inspect native hooks/list for details",
            len(report.warnings()),
            marketplace.selector,
            project,
        )
    if marketplace.declares_hooks() and (
        not hooks_of(report, marketplace.selector) or report.failures()
    ):
        raise CodexPolicyUntrusted(
            f"{home} lacks complete hook evidence for {marketplace.selector} from policy root "
            f"{policy_root or marketplace.source} in native workspace {project}. "
            f"Native discovery reported {len(report.failures())} unresolved error(s). "
            "Inspect native hooks/list and this workspace's plugin and hook settings "
            "before opening a governed session."
        )
    return skipped(report, marketplace.selector)
