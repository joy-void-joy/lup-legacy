"""One account's login kept the same in every copy of it, the newest winning.

Once lup copies a login it lives in several files: the profile's own home,
each home derived from it in a checkout (:mod:`lup.providers.session_home`),
and the configuration volume a repository's contained sessions share. A
runtime renews its login in whichever copy its session reads, and a renewal
can rotate the refresh credential with the access token, so every other copy
is left holding one the provider may no longer answer. A Codex launch
already converges its worktree home with the account, newer refresh winning,
at every launch and when the session closes
(:meth:`lup.providers.codex.home.CodexWorktreeHomeStore.publish`). A Claude copy renews in
place with no such moment, so this carries it while sessions run: the newest
copy is written into every other one, the profile's included.

**Newest by what the login says.** Each runtime names a number every renewal
moves forward (:attr:`ProviderLogin.renewed_at`): Claude Code's access token
expiry. A copy that can no longer renew never wins, and one whose number is
not larger than another's never overwrites it, so two copies renewed at once
end on whichever renewal came last.

**Never across accounts.** A copy is the profile's account only while it
holds that account's login, as the account document beside it says
(:attr:`ProviderLogin.account_id`): a person signing in to another account
from inside a session turns that copy into a login of the other account. So
only the copies whose account is the profile's take part, and a copy that
now holds another account is left as it is, and said to have switched; a
profile whose account cannot be told carries nothing at all.

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


class LoginCopy(BaseModel, frozen=True):
    """One copy as read: the login's bytes, and the bytes of the document saying whose it is."""

    login: bytes | None = None
    account: bytes | None = None


class LoginPlace(ABC):
    """Somewhere one copy of a login is kept, read and replaced whole."""

    @abstractmethod
    def named(self) -> str:
        """Where it is, as a reader names it."""

    @abstractmethod
    def read(self) -> LoginCopy:
        """The copy, and whose it says it is; nothing in either where there is none."""

    @abstractmethod
    def write(self, content: bytes, expected: bytes | None) -> None:
        """Replace the copy, only where it still holds *expected*; ``ChannelConflictError`` where it moved."""


def bytes_at(path: Path) -> bytes | None:
    """What a file holds, nothing where there is none."""
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


class LoginFile(LoginPlace):
    """A copy kept in a file this machine reaches: a profile's home, or a home derived from one.

    *account* is the document saying whose login it is
    (:meth:`ProviderLogin.account_document` of its home).
    """

    def __init__(self, path: Path, account: Path) -> None:
        self.path = path
        self.account = account

    def named(self) -> str:
        return str(self.path)

    def read(self) -> LoginCopy:
        return LoginCopy(login=bytes_at(self.path), account=bytes_at(self.account))

    def write(self, content: bytes, expected: bytes | None) -> None:
        write_atomic(self.path, content, mode=0o600, durable=True, expected=expected)


class Carried(BaseModel, frozen=True):
    """What one pass did: the copy every other took, and each it replaced or could not."""

    newest: str = ""
    replaced: list[str] = []
    moved: list[str] = []
    """Copies renewed again between the read and the write, left for the next pass."""

    switched: list[str] = []
    """Copies holding another account's login now, left as they are."""

    unknown: str = ""
    """Why nothing was carried where the profile's own account cannot be told."""


class AccountIdentity(BaseModel, frozen=True):
    """Who a login belongs to, as the runtime's account document says: its id, and how a person names it."""

    id: str
    name: str = ""


def walked(document: JsonObject, keys: list[str]) -> JsonValue:
    """The value under *keys* in a document, nothing where they lead nowhere."""
    value: JsonValue = document
    for key in keys:
        if not isinstance(value, dict) or key not in value:
            return None
        value = value[key]
    return value


def identity_of(login: ProviderLogin, account: bytes | None) -> AccountIdentity | None:
    """Whose login a copy holds, from its account document; nothing where it does not say."""
    document = document_of(account)
    found = walked(document, login.account_id) if login.account_id else None
    named = walked(document, login.account_name) if login.account_name else None
    if not isinstance(found, str) or not found:
        return None
    return AccountIdentity(id=found, name=named if isinstance(named, str) else "")


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
    value = walked(document, login.renewed_at)
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
    account: AccountIdentity | None = None


def carried(login: ProviderLogin, places: list[LoginPlace], lock: Path) -> Carried:
    """Write the newest renewable copy of one account's login into every older copy of it.

    The first place is the profile's own, whose account the copies are of.
    """
    if not login.renewed_at or not places:
        return Carried()
    with exclusive(lock):
        read = [place.read() for place in places]
        everyone = [
            Held(
                place=index,
                raw=copy.login,
                document=document_of(copy.login),
                renewed=renewed(login, document_of(copy.login)),
                renewable=renews(login, document_of(copy.login)),
                account=identity_of(login, copy.account),
            )
            for index, copy in enumerate(read)
        ]
        owner = everyone[0].account
        if owner is None:
            return Carried(
                unknown=f"{places[0].named()} says no account, so no copy is carried"
            )
        held = [
            each
            for each in everyone
            if each.account is not None and each.account.id == owner.id
        ]
        switched = [
            places[each.place].named()
            for each in everyone
            if each.raw is not None
            and (each.account is None or each.account.id != owner.id)
        ]
        dated = [each for each in held if each.renewed is not None and each.renewable]
        if not dated:
            return Carried(switched=switched)
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
            newest=places[newest.place].named(),
            replaced=replaced,
            moved=moved,
            switched=switched,
        )


def profile_lock(login: ProviderLogin, home: Path) -> Path:
    """The lock one account's passes take turns on, beside its profile's login."""
    return seed_lock(login.credentials_path(home))
