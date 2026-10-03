"""The nodes a generated TOML document is declared as.

The third container, and the one that looks safest: TOML quotes a basic
string much as JSON does, which is why `json.dumps` is a tempting stand-in
for its quoting. The two agree until they do not — a literal TOML
reader is entitled to its own escapes, a multi-line value wants its own
delimiter, and the day a value carries something the two spell differently is
the day a generated agent file stops parsing with nothing in the generator
having changed.

So this says what :mod:`lup.formats.yaml` says, through the library that
already ships here: a node holds the value it stands for, :mod:`tomlkit`
writes it, and :meth:`TomlDocument.text` is parsed back and held against what
the nodes declare before the file exists.

A document somebody else wrote — a project's ``pyproject.toml`` — is the
other direction: not generated, only changed, and changed through
:func:`edited_manifest`, so the layout its author chose survives the change.
"""

from abc import ABC, abstractmethod
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Literal

import tomlkit
from pydantic import BaseModel, Discriminator, model_validator
from tomlkit import TOMLDocument

type TomlValue = str | int | float | bool
"""What TOML writes as one value. A key with nothing under it is omitted
rather than written null, TOML having no such value to write."""

type TomlData = dict[str, TomlValue]
"""A flat table, which is every generated TOML document this repository has."""


class TomlNode(BaseModel, ABC, frozen=True):
    """One value of a generated TOML document, as the writer receives it."""

    @abstractmethod
    def plain(self) -> TomlValue:
        """The value this node stands for, as a parser would hand it back."""


class TomlScalar(TomlNode, frozen=True):
    """One value, spelled the way the writer spells it."""

    type: Literal["scalar"] = "scalar"
    value: TomlValue

    def plain(self) -> TomlValue:
        return self.value


type TomlAny = Annotated[TomlScalar, Discriminator("type")]
"""Any node a generated TOML document holds."""


class TomlEntry(BaseModel, frozen=True):
    """One key of the document, with the comment a reader needs above it."""

    key: str
    value: TomlAny
    comment: str = ""


class TomlDocument(BaseModel, frozen=True):
    """A whole TOML file as the values it carries, written and then read back."""

    entries: list[TomlEntry]

    @model_validator(mode="after")
    def keys_are_distinct(self) -> "TomlDocument":
        keys = [entry.key for entry in self.entries]
        if len(keys) != len(dict.fromkeys(keys)):
            raise ValueError(f"document declares one key twice: {keys}")
        return self

    @model_validator(mode="after")
    def renders_what_it_holds(self) -> "TomlDocument":
        parsed = tomlkit.parse(self.text())
        if parsed != self.plain():
            raise ValueError(
                "toml document does not parse back to what it declares: "
                f"{parsed!r} from {self.plain()!r}"
            )
        return self

    def plain(self) -> TomlData:
        """The data this document stands for, as a parser would hand it back."""
        return {entry.key: entry.value.plain() for entry in self.entries}

    def text(self) -> str:
        """This document as TOML, newline-terminated."""
        written = tomlkit.document()
        for entry in self.entries:
            if entry.comment:
                written.add(tomlkit.comment(entry.comment))
            written.add(entry.key, entry.value.plain())
        return tomlkit.dumps(written)


def edited_manifest[Answer](
    path: Path, change: Callable[[TOMLDocument], Answer], write: bool = True
) -> Answer:
    """One TOML file changed in place, keeping everything the change did not touch.

    Parsed and dumped through :mod:`tomlkit`, so the comments, key order,
    blank lines and quoting its author left survive a change to one value:
    re-emitting a parsed table would put a diff nobody wrote in front of every
    reviewer, with the real change somewhere inside it. ``change`` edits the
    document it is handed and answers whatever its caller wants back.

    Written only where the change moved something and ``write`` holds, so a
    dry run and a staleness check ask the same question without touching the
    file, and an edit that changes nothing leaves its modification time alone.
    """
    before = path.read_text(encoding="utf-8")
    document = tomlkit.parse(before)
    answer = change(document)
    after = tomlkit.dumps(document)
    if write and after != before:
        path.write_text(after, encoding="utf-8")
    return answer
