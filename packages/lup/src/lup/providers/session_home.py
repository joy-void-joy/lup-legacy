"""Private per-session configuration homes, kept in the checkout they serve.

A provider CLI reads its configuration document at startup and writes it
back, and nothing about that exchange is atomic. Sessions opened together
therefore read one another's partial writes: a run that leased eleven
concerns opened eleven sessions at once, six of them read a document another
was still writing, and every one died on a truncated parse before any work
began. Serializing the startup would only make them take turns rewriting a
file they all still share, so what is isolated here is the file.

A derived home is mostly links. Every entry it does not have to own is a
symlink back to the shared home, so a session keeps the settings and plugins
the selected profile carries, and an entry the runtime makes there later
reaches every home derived from it. Two kinds of entry are the home's own:
those a runtime names unshareable, because a starting session rewrites them,
and the stored login, because a session replaces it.

A link cannot carry the login. Claude Code (read from 2.1.285, the atomic
write its plaintext credential store uses) stages a refreshed login in a
temporary file beside the old one and renames it over the path, opening
nothing through a link. The first refresh in a home therefore replaces the
link with a file of the home's own, and from then on the home holds a login
nothing keeps in step: the profile signing in again never reaches it, and a
derivation finding a file where its link was leaves it be.

So a derived home holds a copy, and the copy follows the profile by one rule.
The profile's login is applied once per change of it: what was applied is
fingerprinted beside the copy, and a copy whose fingerprint still matches the
profile is kept, however far a session renewed it since. A copy that can no
longer renew is replaced even so, since nothing it holds will be answered.
Only the login's own fields move (:attr:`ProviderLogin.credential_fields`);
every other record the stored file keeps stays. The copy is written whole, by
rename, at mode 0600, under a lock beside it, so homes derived at once take
turns. A container's configuration volume is seeded by the same rule, by
``lup.harness.assets.credential_seed`` — a standalone program, since the
container running it has no lup to import — which keeps its fingerprint and
its lock under the names these are kept under.

Where the homes live and what they read are two separate questions. They live
in the checkout, because they are this project's own state and the shared
home is an account's. They read that account, because its settings and its
login are the account's: through a link where a session only reads, and
through a copy kept in step where a session rewrites.
"""

import json
import logging
from pathlib import Path

import sh
from pydantic import BaseModel, TypeAdapter, ValidationError

from lup.channels.models import write_atomic
from lup.execution.locks import exclusive
from lup.formats import digest
from lup.providers.login import ProviderLogin
from lup.types import JsonObject
from lup.workspace.checkout_state import CheckoutState
from lup.workspace.paths import declared_project_root

logger = logging.getLogger(__name__)


class LoginUnreadable(ValueError):
    """A stored login file exists but holds no JSON object.

    Raised rather than read as no login: replacing the file would discard
    whatever else it keeps. The reason names the path alone, since the
    content is a secret no log may carry.
    """


def seed_lock(stored: Path) -> Path:
    """The lock every seed of one stored login takes its turn on, beside it."""
    return stored.with_name(".lup-login-handoff.lock")


def seed_stamp(stored: Path) -> Path:
    """Where the fingerprint of the login last applied to ``stored`` is kept."""
    return stored.with_name(f".lup-seeded-{stored.name}.sha256")


def read_login(path: Path) -> JsonObject:
    """The object one stored login file holds, empty where it holds nothing."""
    if not path.is_file() or not path.stat().st_size:
        return {}
    try:
        return TypeAdapter(JsonObject).validate_json(path.read_bytes())
    except ValidationError:
        raise LoginUnreadable(f"{path} holds no JSON object") from None


def renews(login: ProviderLogin, document: JsonObject) -> bool:
    """Whether a stored login can still reach its account, by the runtime's filter.

    Asked of ``jq``, as the container entrypoint asks it, since the filter is
    written in it. A runtime declaring no filter, or a machine with no ``jq``
    to ask, cannot be asked, which reads as renewable: the answer is the
    runtime's to give, and guessing the other way would replace a working copy.
    """
    if not document:
        return False
    if not login.renewable:
        return True
    try:
        jq = sh.Command("jq")
    except sh.CommandNotFound:
        logger.warning(
            "No jq on this machine to ask whether a stored %s login can still "
            "renew, so it is read as one that can",
            login.config_home_env,
        )
        return True
    try:
        jq("-e", login.renewable, _in=json.dumps(document), _timeout=10)
    except sh.ErrorReturnCode:
        return False
    return True


def seed_login(login: ProviderLogin, seed: Path, stored: Path) -> bool:
    """Apply the login at ``seed`` to ``stored`` where it changed, saying whether it did.

    Nothing is applied from a seed holding no login, or one that cannot renew,
    so a copy a session is still renewing outlives a profile signed out or
    gone stale. Where the seed's login is the one last applied and the copy
    can still renew, the copy is the newer of the two and is kept.
    """
    with exclusive(seed_lock(stored)):
        incoming = read_login(seed)
        selected = (
            {key: incoming[key] for key in login.credential_fields if key in incoming}
            if login.credential_fields
            else incoming
        )
        if not selected or not renews(login, incoming):
            return False
        fingerprint = digest.text(
            json.dumps(selected, sort_keys=True, separators=(",", ":"))
        )
        stamp = seed_stamp(stored)
        current = read_login(stored)
        applied = stamp.read_text(encoding="ascii") if stamp.is_file() else None
        if applied == fingerprint and renews(login, current):
            return False
        kept = {
            key: value
            for key, value in current.items()
            if login.credential_fields and key not in login.credential_fields
        }
        content = json.dumps({**kept, **selected}, indent=2) + "\n"
        write_atomic(stored, content.encode("utf-8"), mode=0o600, durable=True)
        write_atomic(stamp, fingerprint.encode("ascii"), mode=0o600, durable=True)
        return True


class SessionHomeLayout(BaseModel, frozen=True):
    """Which of one runtime's configuration entries a session cannot share."""

    private_files: list[str] = []
    """Entries a starting session rewrites, so concurrent ones each need
    their own. Named by the runtime that spells them, because which file a
    startup rewrites is that runtime's own fact and no portable one."""

    login: ProviderLogin | None = None
    """The login a derived home keeps a copy of, seeded from the shared home's.

    Its file is the home's own, with the fingerprint and lock kept beside it,
    and is applied by :func:`seed_login` at every derivation. ``None`` where
    the runtime keeps no login in the home, which leaves every entry linked."""

    derived_dir: Path = CheckoutState(root=Path()).sessions()
    """Where under the checkout the derived homes are kept.

    In the checkout, not under the shared home. A derived home is lup's own
    state, while the shared home belongs to whoever the profile selected —
    by default the operator's account, which a project has no business
    writing into. Relative, and resolved against the checkout: an absolute
    path would resolve to itself for every workspace."""

    def owned(self) -> list[str]:
        """Every entry a derived home keeps as its own rather than linking."""
        if self.login is None:
            return self.private_files
        stored = Path(self.login.credentials_file)
        kept = [stored, seed_stamp(stored), seed_lock(stored)]
        return [*self.private_files, *(entry.name for entry in kept)]


class SessionHomes:
    """One private configuration home per workspace, under a shared home."""

    def __init__(self, shared: Path, layout: SessionHomeLayout) -> None:
        self.shared = shared
        self.layout = layout

    def root(self, workspace: Path) -> Path:
        """Where the homes derived for one workspace's checkout are kept.

        The checkout holds them, so a run writes its own state into the tree
        it was given and leaves the selected home to the account that owns
        it. The entries inside still read that home: its settings through a
        link, its login through a copy each derivation brings in step.
        """
        canonical = workspace.resolve()
        return (declared_project_root(canonical) or canonical) / self.layout.derived_dir

    def name_for(self, workspace: Path) -> str:
        """The directory name the sessions of one workspace and account share.

        The workspace decides rather than a per-session identifier, because
        the workspace is what actually runs concurrently: a run leases one
        checkout per concern and opens them together, while the turns inside
        a single lease are sequential. Keying on it gives every racing
        session a document of its own and still lets a concern's worker and
        its reviewer read one. The digest carries the whole path so two
        leases with the same basename cannot land on one home; the basename
        rides in front of it so a human can tell them apart on disk.

        The account is in the digest too, and has to be. The homes sit in the
        checkout, so no parent directory carries the account, and a name
        derived from the workspace alone would hand a second account the home
        the first had already derived — whose links point at the first and
        are never re-pointed. Nothing would fail: the session would open under
        the second account's login reading the first account's settings,
        history and plugins, which is the mix a profile exists to rule out.
        """
        identity = f"{self.shared.resolve()}\n{workspace.resolve()}"
        return f"{workspace.name}-{digest.text(identity)[:12]}"

    def derive(self, workspace: Path) -> Path:
        """Create or refresh the home that one workspace's sessions open."""
        home = self.root(workspace) / self.name_for(workspace)
        home.mkdir(parents=True, exist_ok=True)
        self.unlink_private(home)
        self.link_shared(home)
        self.carry_login(home)
        return home

    def unlink_private(self, home: Path) -> None:
        """Drop every link one derived home holds where an entry must be its own.

        Such an entry is the derived home's own file or nothing. A link in
        its place hands every session opened through the home the shared file
        itself — a document sessions racing on corrupt, or a login the first
        refresh replaces with a file nothing keeps in step — so a derivation
        refreshes the home toward the layout it is derived under rather than
        trusting what an earlier one left there.
        """
        for name in self.layout.owned():
            entry = home / name
            if entry.is_symlink():
                entry.unlink(missing_ok=True)

    def link_shared(self, home: Path) -> None:
        """Point every shareable entry of one derived home at the shared one.

        Re-linked on each derivation rather than once when the home is
        created: a runtime makes directories in its home the first time it
        needs one, so an entry that did not exist yet when a home was first
        derived would stay invisible to every session opened through it
        afterwards.
        """
        if not self.shared.is_dir():
            return
        # The derived homes sit in the checkout, so the shared home does not
        # contain them — except where somebody selected the checkout itself
        # as the shared home, which would otherwise link the tree into a home
        # inside it. Reserving the leading segment keeps that from closing.
        reserved = [self.layout.derived_dir.parts[0], *self.layout.owned()]
        for entry in self.shared.iterdir():
            link = home / entry.name
            if entry.name in reserved or link.is_symlink() or link.exists():
                continue
            # A session of the same workspace deriving this home at once can
            # link the entry between the look above and this write; the entry
            # is then there, which is all this asked for.
            try:
                link.symlink_to(entry)
            except FileExistsError:
                continue

    def carry_login(self, home: Path) -> None:
        """Bring one derived home's copy of the login in step with the shared home's.

        At every derivation, so a profile that signed in again reaches each
        home derived from it by the next session opened there, while a copy a
        session renewed under an unchanged profile stays as that session left
        it.
        """
        login = self.layout.login
        if login is not None:
            seed_login(
                login, login.credentials_path(self.shared), login.credentials_path(home)
            )
