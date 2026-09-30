"""What one person has decided for every project they run lup in.

A checkout holds what everyone working on it shares, and nothing about the
person running it: which account they sign in as, how their terminal is
drawn, how hard and on which model a session thinks when nobody said. Kept in
a checkout, each of those starts over in the next one — a new repository
opened signed out, in the runtime's default theme. So they live once per
person, at ``$XDG_CONFIG_HOME/lup`` (``~/.config/lup`` where that is unset)::

    config.toml         the decisions below
    profiles/<name>/    one account per name, a home per runtime inside it

Every field carries lup's own answer, so a person who writes nothing gets it
and one line changes one answer. The same precedence holds wherever a value is
chosen: lup's default, then this file, then what the project declares, then
what the invocation names — each overruling the one before, so this file never
decides what a project or a flag already did. The container a session opens
in is the one exception, since what a machine can grant is its person's to
say: ``[container]`` overrules the project's, and a mode and a flag overrule
it.

Read where a run starts rather than cached, so an edit reaches the next launch
with nothing to restart; and refused when it does not parse, because a
decision silently dropped reads exactly like one never made.
"""

from pathlib import Path
from typing import Literal

import tomlkit
from pydantic import BaseModel, Field, ValidationError, field_validator
from pydantic_settings import BaseSettings
from tomlkit.exceptions import TOMLKitError
from tomlkit.items import Table

from lup.channels.models import write_atomic
from lup.harness.models import NativeName
from lup.launch.declaration import OuterContainer
from lup.providers.claude.theme import ClaudeTheme
from lup.providers.catalog import SessionEffort
from lup.types import JsonObject, JsonValue, ModelTier


class UserTheme(BaseModel, frozen=True, extra="forbid"):
    """How each runtime's interface is drawn, in that runtime's own name for it.

    A name per runtime rather than one for both, because the two share no
    vocabulary: Claude Code ships its themes, and Codex reads TextMate files
    by name. Named here, a theme wins over the one the account keeps; left
    unset, the account's own stands, and a session's ``/theme`` returns to
    it. Only where neither names one does lup draw its own colorblind palette
    — Claude Code's ``dark-daltonized``, and its Codex port.
    """

    claude: ClaudeTheme | None = None
    codex: NativeName | None = None


type EditorMode = Literal["normal", "vim"]
"""How a runtime's prompt takes keys: plainly, or as vim's modes do."""


class UserRuntimeSettings(BaseModel, frozen=True, extra="forbid"):
    """Settings one runtime is handed as written, in that runtime's own keys.

    What the portable keys cannot say, said once for every project: a table
    here wins over the account's own setting of the same name, the way a
    portable key does, and a session's change to one of them comes back here
    rather than to the account, where this table would hide it.
    """

    settings: JsonObject = {}


class UserCleanup(BaseModel, frozen=True, extra="forbid"):
    """How long lup keeps what it replaced before a launch removes it."""

    superseded_volumes_after_days: int = Field(default=14, ge=0)
    """Days a config volume a split superseded is kept, its history readable,
    before a launch or `harness clean` removes it; `harness clean --yes`
    removes it sooner."""


class UserDashboard(BaseModel, frozen=True, extra="forbid"):
    """How the dashboard reaches for the person when a review parks."""

    reopen: bool = True
    """Whether a review parking while no tab follows the page opens it in
    the browser, at most once per quiet period; `dashboard reopen --off`
    turns it off. The desktop notice is sent either way."""


class UserConfig(BaseModel, frozen=True, extra="forbid"):
    """One person's standing answers, each defaulting to lup's own."""

    profile: str | None = None
    """The account a launch runs as when none is named and the checkout's own
    ``.lup/profiles/.active`` selects none: a directory under ``profiles/``,
    holding that account's home for each runtime, unless the checkout keeps a
    profile of that name, which wins there. Unset, a launch keeps whichever
    home its environment already selects."""

    theme: UserTheme = UserTheme()

    effort: SessionEffort | None = None
    """How hard a session thinks when nothing names an effort. Unset, the
    model's default: ``xhigh`` where its catalog row takes it, the row's
    highest rung below that otherwise. Named, it is the rung a session starts
    from before stepping down to one the model takes, so a model lacking it
    still opens rather than being refused a default."""

    tier: ModelTier = "strongest"
    """The model a session runs on when nothing names one, as a portable tier
    each runtime spells in its own lineup; ``inherit`` leaves the runtime's."""

    editor: EditorMode | None = None
    """How every runtime's prompt takes keys: Claude Code's ``editorMode``,
    Codex's ``tui.vim_mode_default``. Unset, each account's own stands."""

    claude: UserRuntimeSettings = UserRuntimeSettings()
    """``[claude.settings]``: Claude Code settings handed to every session."""

    codex: UserRuntimeSettings = UserRuntimeSettings()
    """``[codex.settings]``: Codex configuration handed to every session."""

    cleanup: UserCleanup = UserCleanup()

    dashboard: UserDashboard = UserDashboard()

    container: OuterContainer = OuterContainer()
    """``[container]``: what every contained session this person launches is
    granted — its network, memory, sudo, devices, folders and held trees —
    over what the project declares and under a mode and the command line.
    A repository's own facts, its image, the repositories kept inside it and
    the guidance a kind of its sessions reads, are not a person's to state."""

    @field_validator("container")
    @classmethod
    def container_states_no_repository_s_facts(
        cls, value: OuterContainer
    ) -> OuterContainer:
        """Refuse an image, nested repositories or guidance, which are one repository's own."""
        named = sorted(
            {"image", "nested_repositories", "guidance"} & value.model_fields_set
        )
        if named:
            raise ValueError(
                f"[container] names {', '.join(named)}, which are one repository's "
                "own; declare them in that repository's OuterContainer"
            )
        return value


class UserConfigHome(BaseSettings):
    """Where the XDG base directory specification says a person's config lives.

    Its one variable, read the specification's way: an absolute path moves
    every program's configuration, and an empty or relative one is ignored in
    favour of ``~/.config``.
    """

    xdg_config_home: str = ""

    def directory(self) -> Path:
        """lup's own directory under that base."""
        named = Path(self.xdg_config_home)
        base = named if named.is_absolute() else Path.home() / ".config"
        return base / "lup"


class UserConfigFile:
    """The directory holding one person's decisions and the accounts they name."""

    def __init__(self, home: Path | None = None) -> None:
        self.home = home if home is not None else UserConfigHome().directory()

    def path(self) -> Path:
        """The file :class:`UserConfig` is read from and written to."""
        return self.home / "config.toml"

    def profiles_root(self) -> Path:
        """Where each named account keeps its directory."""
        return self.home / "profiles"

    def document(self) -> tomlkit.TOMLDocument:
        """The file as written, comments and order included, or an empty one."""
        path = self.path()
        if not path.is_file():
            return tomlkit.document()
        try:
            return tomlkit.parse(path.read_text(encoding="utf-8"))
        except TOMLKitError as error:
            raise ValueError(f"{path} is not valid TOML: {error}") from error

    def load(self) -> UserConfig:
        """What this person decided, lup's default wherever they decided nothing."""
        try:
            return UserConfig.model_validate(self.document().unwrap())
        except ValidationError as error:
            raise ValueError(
                f"{self.path()} holds a setting lup cannot read: {error}"
            ) from error

    def select_profile(self, name: str | None) -> None:
        """Record the account a launch naming none runs as, changing nothing else.

        Written through the parsed document, so a person's comments and the
        order they wrote things in survive a selection made by a command.
        """
        self.record({("profile",): name})

    def record(self, values: dict[tuple[str, ...], JsonValue | None]) -> None:
        """Write each value at its dotted path, ``None`` removing it, and nothing else.

        Written through the parsed document, so a person's comments and the
        order they wrote things in survive a value a command or a session
        put there. Refused as a whole, writing nothing, where the result is
        a file lup could not read back — a value no launch would then accept.
        """
        document = self.document()
        for path, value in values.items():
            table: tomlkit.TOMLDocument | Table = document
            for key in path[:-1]:
                held = table.get(key)
                if not isinstance(held, Table):
                    if value is None:
                        break
                    table[key] = tomlkit.table()
                    held = table[key]
                table = held
            else:
                if value is None:
                    table.pop(path[-1], None)
                else:
                    table[path[-1]] = value
        UserConfig.model_validate(document.unwrap())
        write_atomic(self.path(), tomlkit.dumps(document).encode("utf-8"))
