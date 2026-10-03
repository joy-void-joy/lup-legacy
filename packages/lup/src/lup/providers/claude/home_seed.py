"""What one launch seeds a contained Claude session's config home with, and what comes back.

The seed is the selected account's ``settings.json`` as it stands — its
hooks included, which then run inside the container — and its
``keybindings.json``, with the person's lup config winning wherever it names
a value, and the handful of preferences only the configuration document
holds. Seeded at every launch rather than once, because a seed is the
account as it stood the day a repository first launched. Not the account's
user memory (``CLAUDE.md``), which belongs to the repository; not plugins or
user-scope MCP servers, which come from the repository too.

What comes back is decided per key by :mod:`lup.providers.claude.preferences`,
measured against the seed rather than against the account.
"""

import json
from pathlib import Path

from pydantic import BaseModel

from lup.channels.models import write_atomic
from lup.providers.claude.config_home import (
    CLAUDE_HOME_DOCUMENT,
    WORKSPACE_SETTINGS,
    ClaudeConfigHome,
    load_document,
)
from lup.providers.claude.preferences import (
    PORTABLE,
    document_homed,
    legacy_preference,
    setting_flow,
)
from lup.providers.claude.theme import ClaudeTheme
from lup.providers.user_config import UserConfig, UserConfigFile
from lup.types import JsonObject, JsonValue

# lup: ignore[constant-declaration] — the CLI's own file name for key bindings
KEYBINDINGS = "keybindings.json"
"""Where Claude Code keeps an account's key bindings, beside its settings."""


# lup: ignore[constant-declaration] — lup's own palette for a theme nobody named,
# the one a host launch fills in too
FALLBACK_THEME: ClaudeTheme = "dark-daltonized"
"""The theme a seed draws where neither the lup config nor the account names one."""


class ClaudeHomeSeed(BaseModel, frozen=True):
    """What one launch puts into a contained session's config home.

    ``settings`` replaces the volume's ``settings.json`` whole, so a key a
    session wrote there that never returns is gone at the next launch rather
    than kept per repository. ``document`` is merged into the volume's
    configuration document key by key, since that document is the CLI's own
    record and holds far more than preferences. ``keybindings`` is the
    account's file as written, or ``None`` where it keeps none.
    """

    settings: JsonObject
    document: JsonObject = {}
    keybindings: str | None = None

    @classmethod
    def compose(
        cls,
        home: ClaudeConfigHome,
        personal: UserConfig,
        model: str | None = None,
        effort: str | None = None,
    ) -> "ClaudeHomeSeed":
        """The seed an account and a person's lup config make together.

        The account's settings as they stand, each preference the settings
        files carry filled in from the document's own copy where the
        settings leave it out — the order the CLI itself reads them in —
        then the lup config over them: its ``[claude.settings]`` table, the
        editor and the theme it names, and the model and effort it declares.
        A theme nobody named is lup's own palette, as on the host.
        """
        settings = load_document(home.directory / WORKSPACE_SETTINGS)
        account = load_document(home.document)
        legacy = {
            key: value
            for key, value in account.items()
            if legacy_preference(key) and key not in settings
        }
        declared: JsonObject = {
            key: value
            for key, value in {
                "editorMode": personal.editor,
                "theme": personal.theme.claude,
                "model": model,
                "effortLevel": effort,
            }.items()
            if value is not None
        }
        seeded: JsonObject = {**settings, **legacy, **personal.claude.settings}
        seeded = {**seeded, **declared}
        seeded.setdefault("theme", FALLBACK_THEME)
        keybindings = home.directory / KEYBINDINGS
        return cls(
            settings=seeded,
            document={
                key: value for key, value in account.items() if document_homed(key)
            },
            keybindings=(
                keybindings.read_text(encoding="utf-8")
                if keybindings.is_file()
                else None
            ),
        )

    @classmethod
    def applied(cls, directory: Path) -> "ClaudeHomeSeed":
        """What a seed came to against a volume, read back from where the launch wrote it.

        The files a three-way settle wrote (see
        :func:`~lup.launch.config_volume.settle_home_seed`): the
        settings as applied, the document with only its preferences kept,
        and the key bindings.
        """
        keybindings = directory / KEYBINDINGS
        return cls(
            settings=load_document(directory / WORKSPACE_SETTINGS),
            document={
                key: value
                for key, value in load_document(
                    directory / CLAUDE_HOME_DOCUMENT
                ).items()
                if document_homed(key)
            },
            keybindings=(
                keybindings.read_text(encoding="utf-8")
                if keybindings.is_file()
                else None
            ),
        )

    def write(self, directory: Path) -> Path:
        """Lay the seed out as the image's entrypoint applies one.

        ``managed`` names the files the seed owns — the settings and the
        key bindings, the second removed from the volume where the account
        keeps none — ``replace/`` holds them, and ``merge/`` holds the
        document's preferences, merged into the volume's own document.
        """
        (directory / "replace").mkdir(parents=True, exist_ok=True)
        (directory / "merge").mkdir(exist_ok=True)
        managed = [WORKSPACE_SETTINGS, KEYBINDINGS]
        write_atomic(
            directory / "managed", "".join(f"{name}\n" for name in managed).encode()
        )
        write_atomic(directory / "replace" / WORKSPACE_SETTINGS, pretty(self.settings))
        if self.keybindings is not None:
            written = self.keybindings.encode("utf-8")
            write_atomic(directory / "replace" / KEYBINDINGS, written)
        write_atomic(directory / "merge" / CLAUDE_HOME_DOCUMENT, pretty(self.document))
        return directory


def pretty(document: JsonObject) -> bytes:
    """One JSON document the way a person reading it back expects it laid out."""
    return (json.dumps(document, indent=2) + "\n").encode("utf-8")


class ClaudeHomeReturn(BaseModel, frozen=True):
    """Where every setting a session changed goes, and what stays behind.

    ``None`` stands for a setting the session removed.
    """

    portable: dict[tuple[str, ...], JsonValue | None] = {}
    """Changes for the person's lup config, at the dotted path it holds each."""

    settings: dict[str, JsonValue | None] = {}
    """Changes for the account's ``settings.json``."""

    document: dict[str, JsonValue | None] = {}
    """Changes for the account's configuration document."""

    keybindings: str | None = None
    """The session's key bindings, where they differ from the seeded ones."""

    withheld: list[str] = []
    """Settings a session changed that never leave its container."""

    session: list[str] = []
    """Settings a session changed that were its own to change."""

    @classmethod
    def between(
        cls,
        seed: ClaudeHomeSeed,
        settings: JsonObject,
        document: JsonObject,
        keybindings: str | None,
        personal: UserConfig,
    ) -> "ClaudeHomeReturn":
        """What changed between a seed and a volume after its session, sorted by flow.

        Compared with what this launch seeded rather than with the account,
        so a setting the person changed on the host while the session ran is
        not undone by a session that never touched it. Only the document's
        own preferences are read out of the document; the rest of it is the
        CLI's record of the volume.
        """
        changed = [
            key
            for key in sorted({*seed.settings, *settings})
            if seed.settings.get(key) != settings.get(key)
        ]
        flows = {key: setting_flow(key) for key in changed}

        def destination(key: str) -> tuple[str, ...]:
            portable = [path for known, path in PORTABLE.items() if known == key]
            if portable:
                return portable[0]
            if key in personal.claude.settings:
                return ("claude", "settings", key)
            return ()

        returning = [key for key in changed if flows[key] == "returns"]
        preferences = [
            key
            for key in sorted({*seed.document, *document})
            if document_homed(key) and seed.document.get(key) != document.get(key)
        ]
        return cls(
            portable={
                destination(key): settings.get(key)
                for key in returning
                if destination(key)
            },
            settings={
                key: settings.get(key) for key in returning if not destination(key)
            },
            document={key: document.get(key) for key in preferences},
            keybindings=(
                keybindings
                if keybindings is not None and keybindings != seed.keybindings
                else None
            ),
            withheld=[key for key in changed if flows[key] == "withheld"],
            session=[key for key in changed if flows[key] == "session"],
        )

    def carried(self) -> list[str]:
        """The dotted names of everything this return writes somewhere."""
        return [
            *(".".join(path) for path in self.portable),
            *self.settings,
            *self.document,
            *([KEYBINDINGS] if self.keybindings is not None else []),
        ]

    def apply(self, home: ClaudeConfigHome, config: UserConfigFile) -> None:
        """Write each change where it belongs, leaving every other line alone.

        A portable value the lup config refuses — a theme it does not know,
        say — is kept in the account's settings instead, where the CLI that
        wrote it reads it back.
        """
        refused: dict[str, JsonValue | None] = {}
        if self.portable:
            try:
                config.record(self.portable)
            except ValueError:
                refused = {
                    next(key for key, held in PORTABLE.items() if held == path)
                    if path in PORTABLE.values()
                    else path[-1]: value
                    for path, value in self.portable.items()
                }
        if self.settings or refused:
            path = home.directory / WORKSPACE_SETTINGS
            home.directory.mkdir(parents=True, exist_ok=True)
            merged = changed(load_document(path), self.settings | refused)
            write_atomic(path, pretty(merged))
        if self.document:
            merged = changed(load_document(home.document), self.document)
            write_atomic(home.document, pretty(merged))
        if self.keybindings is not None:
            home.directory.mkdir(parents=True, exist_ok=True)
            write_atomic(home.directory / KEYBINDINGS, self.keybindings.encode("utf-8"))


def changed(document: JsonObject, values: dict[str, JsonValue | None]) -> JsonObject:
    """A document with each value set, and each ``None`` removed."""
    return {
        **{key: value for key, value in document.items() if key not in values},
        **{key: value for key, value in values.items() if value is not None},
    }
