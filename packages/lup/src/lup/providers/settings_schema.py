"""What a runtime's CLI says about the settings it takes, recorded as data.

A contained session's settings travel into its container and some of what the
session changes travels back, and which may is a decision per key. That
decision is only as complete as the list of keys it was made over, and a
vendor adds keys most releases. :class:`SettingsSchema` is that list as one
CLI reported it, committed beside the module compiled from it — so the
compiled key types and the decisions typed against them are checked offline
against the snapshot, and the snapshot against the live CLI only by the
command that refreshes it (`uv run lup-devtools dev settings`).

Claude Code's alone: it ships its settings schema inside its program, where
Codex ships only a parser, so there is no Codex list to read — and a Codex
key nothing classifies never leaves its container either way.
"""

from pathlib import Path
from typing import Literal

from pydantic import BaseModel

type SettingFlow = Literal["returns", "withheld", "session"]
"""Whether a session's change to one setting leaves its container, and why not.

``returns``: a display, input or behaviour preference, carried back to the
person. ``withheld``: anything that runs code or changes what may run, an
organization's or an installation's key, or the runtime's own housekeeping
— never carried out. ``session``: the model and how hard it thinks, the
session's own to pick. A key no decision names is withheld.
"""

# lup: ignore[constant-declaration] — the file the snapshot is committed as,
# which the reader and the command refreshing it both have to name
SNAPSHOT_NAME = "settings_schema.json"
"""The committed snapshot's file, beside the provider that reads it."""


class SettingsSchema(BaseModel, frozen=True, extra="forbid"):
    """The keys one release takes, as its program declares them."""

    observed: str
    """The CLI version the keys were read from."""

    settings: list[str]
    """Every key of the schema each settings file is validated against."""

    document: list[str]
    """Every key the CLI's own configuration command accepts for its document.

    For Claude Code that document is ``.claude.json``, the CLI's own record,
    and these are what ``claude config`` lets a person set there: preferences,
    some of which the settings files also carry, and state the CLI keeps
    beside them.
    """

    @classmethod
    def read(cls, path: Path) -> "SettingsSchema":
        """The snapshot committed at ``path``."""
        return cls.model_validate_json(path.read_text(encoding="utf-8"))

    def text(self) -> str:
        """The snapshot as it is committed."""
        return self.model_dump_json(indent=2) + "\n"

    def drift(self, live: "SettingsSchema") -> list[str]:
        """One line per key the live CLI added (``+``) or dropped (``-``)."""
        return [
            *(f"  + {key}" for key in live.settings if key not in self.settings),
            *(f"  - {key}" for key in self.settings if key not in live.settings),
            *(
                f"  + document {key}"
                for key in live.document
                if key not in self.document
            ),
            *(
                f"  - document {key}"
                for key in self.document
                if key not in live.document
            ),
        ]


def installed_settings_schema() -> SettingsSchema:
    """The keys the installed Claude Code CLI takes, asked now.

    The reader is imported where it is asked for: it reaches the provider's
    adapter, which nothing importing this module for the data model needs.
    """
    from lup.providers.claude.settings_schema import claude_settings_schema

    return claude_settings_schema()


def unclassified_settings() -> list[str]:
    """Every key the compiled schema holds that no flow decision names."""
    from lup.providers.claude.preferences import unclassified

    return unclassified()
