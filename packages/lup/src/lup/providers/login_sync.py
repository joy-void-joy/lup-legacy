"""One account's login kept the same in every copy of it, the newest winning.

Once lup copies a login it lives in several files: the profile's own home,
each home derived from it in a checkout (:mod:`lup.providers.session_home`),
and the configuration volume a repository's contained sessions share. A
runtime renews its login in whichever copy its session reads, and a renewal
can rotate the refresh credential with the access token, so every other copy
is left holding one the provider may no longer answer. A Codex launch
already converges its worktree home with the account, newer refresh winning,
at every launch and when the session closes
(:meth:`lup.providers.codex.home.CodexHome.publish`). A Claude copy renews in
place with no such moment, so this carries it while sessions run: the newest
copy is written into every other one, the profile's included.

**Newest by what the login says.** Each runtime names a number every renewal
moves forward (:attr:`ProviderLogin.renewed_at`): Claude Code's access token
expiry. A copy that can no longer renew never wins, and one whose number is
not larger than another's never overwrites it, so two copies renewed at once
end on whichever renewal came last.

**Compare and swap.** Every copy is read, then each older one replaced only
where it still holds the bytes it was read with (``write_atomic(expected=)``):
a session renewing a copy between the read and the write keeps its renewal,
and the next pass carries it. Only the login's own fields move; every other
record a copy keeps stays. One pass per account runs at a time, under a lock
beside the profile's login.
"""

import json
from abc import ABC, abstractmethod
from pathlib import Path

from pydantic import BaseModel, TypeAdapter, ValidationError

from lup.channels.models import ChannelConflictError, write_atomic
from lup.execution.locks import exclusive
from lup.providers.login import ProviderLogin
from lup.providers.session_home import renews, seed_lock
from lup.types import JsonObject, JsonValue


class LoginPlace(ABC):
    """Somewhere one copy of a login is kept, read and replaced whole."""

    @abstractmethod
    def named(self) -> str:
        """Where it is, as a reader names it."""

    @abstractmethod
    def read(self) -> bytes | None:
        """The copy's bytes; nothing where there is no copy."""

    @abstractmethod
    def write(self, content: bytes, expected: bytes | None) -> None:
        """Replace the copy, only where it still holds *expected*; ``ChannelConflictError`` where it moved."""


class LoginFile(LoginPlace):
    """A copy kept in a file this machine reaches: a profile's home, or a home derived from one."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def named(self) -> str:
        return str(self.path)

    def read(self) -> bytes | None:
        try:
            return self.path.read_bytes()
        except FileNotFoundError:
            return None

    def write(self, content: bytes, expected: bytes | None) -> None:
        write_atomic(self.path, content, mode=0o600, durable=True, expected=expected)


class Carried(BaseModel, frozen=True):
    """What one pass did: the copy every other took, and each it replaced or could not."""

    newest: str = ""
    replaced: list[str] = []
    moved: list[str] = []
    """Copies renewed again between the read and the write, left for the next pass."""


def document_of(raw: bytes | None) -> JsonObject:
    """The object one copy holds, empty where it holds none."""
    if not raw:
        return {}
    try:
        return TypeAdapter(JsonObject).validate_json(raw)
    except ValidationError:
        return {}


def renewed(login: ProviderLogin, document: JsonObject) -> float | None:
    """The number the login's last renewal moved forward, where the copy says it."""
    value: JsonValue = document
    for key in login.renewed_at:
        if not isinstance(value, dict) or key not in value:
            return None
        value = value[key]
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def credentials(login: ProviderLogin, document: JsonObject) -> JsonObject:
    """The login's own fields in a copy, which are what moves between copies."""
    if not login.credential_fields:
        return document
    return {key: document[key] for key in login.credential_fields if key in document}


class Held(BaseModel, frozen=True):
    """One copy as a pass read it: its place among the places, its bytes, and how new it is."""

    place: int
    raw: bytes | None
    document: JsonObject
    renewed: float | None
    renewable: bool


def carried(login: ProviderLogin, places: list[LoginPlace], lock: Path) -> Carried:
    """Write the newest renewable copy of one login into every older copy of it."""
    if not login.renewed_at:
        return Carried()
    with exclusive(lock):
        held = [
            Held(
                place=index,
                raw=raw,
                document=document_of(raw),
                renewed=renewed(login, document_of(raw)),
                renewable=renews(login, document_of(raw)),
            )
            for index, place in enumerate(places)
            for raw in [place.read()]
        ]
        dated = [each for each in held if each.renewed is not None and each.renewable]
        if not dated:
            return Carried()
        newest = max(dated, key=lambda each: each.renewed or 0.0)
        moving = credentials(login, newest.document)
        replaced: list[str] = []
        moved: list[str] = []
        for each in held:
            if each.place == newest.place:
                continue
            if credentials(login, each.document) == moving or (
                each.renewable
                and each.renewed is not None
                and each.renewed >= (newest.renewed or 0.0)
            ):
                continue
            kept = {
                key: value
                for key, value in each.document.items()
                if login.credential_fields and key not in login.credential_fields
            }
            content = (json.dumps({**kept, **moving}, indent=2) + "\n").encode("utf-8")
            place = places[each.place]
            try:
                place.write(content, each.raw)
            except ChannelConflictError:
                moved.append(place.named())
                continue
            replaced.append(place.named())
        return Carried(
            newest=places[newest.place].named(), replaced=replaced, moved=moved
        )


def profile_lock(login: ProviderLogin, home: Path) -> Path:
    """The lock one account's passes take turns on, beside its profile's login."""
    return seed_lock(login.credentials_path(home))
