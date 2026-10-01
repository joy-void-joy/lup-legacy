"""Private per-session configuration homes, kept in the checkout they serve.

A provider CLI reads its configuration document at startup and writes it
back, and nothing about that exchange is atomic. Sessions opened together
therefore read one another's partial writes: a run that leased eleven
concerns opened eleven sessions at once, six of them read a document another
was still writing, and every one died on a truncated parse before any work
began. Serializing the startup would only make them take turns rewriting a
file they all still share, so what is isolated here is the file.

A derived home is not a copy. Every entry it does not have to own is a
symlink back to the shared home, so a session keeps the settings, plugins,
and stored login the selected profile already carries — the login most of
all, since a refresh rotates the token and one copy of it going stale
invalidates the rest. Only the entries a runtime names unshareable are real
files in the derived home, and a runtime that names none shares everything.

Where the homes live and what they point at are two separate questions. They
live in the checkout, because they are this project's own state and the
shared home is an account's. They point back at that account, because a
login is the account's and copying it is what a rotation invalidates. The
symlink is what lets both answers stand at once.
"""

import hashlib
from pathlib import Path

from pydantic import BaseModel

from lup.workspace.checkout_state import CheckoutState
from lup.workspace.paths import declared_project_root


class SessionHomeLayout(BaseModel, frozen=True):
    """Which of one runtime's configuration entries a session cannot share."""

    private_files: list[str] = []
    """Entries a starting session rewrites, so concurrent ones each need
    their own. Named by the runtime that spells them, because which file a
    startup rewrites is that runtime's own fact and no portable one."""

    derived_dir: Path = CheckoutState(root=Path()).sessions()
    """Where under the checkout the derived homes are kept.

    In the checkout, not under the shared home. A derived home is lup's own
    state, while the shared home belongs to whoever the profile selected —
    by default the operator's account, which a project has no business
    writing into. Relative, and resolved against the checkout: an absolute
    path would resolve to itself for every workspace."""


class SessionHomes:
    """One private configuration home per workspace, under a shared home."""

    def __init__(self, shared: Path, layout: SessionHomeLayout) -> None:
        self.shared = shared
        self.layout = layout

    def root(self, workspace: Path) -> Path:
        """Where the homes derived for one workspace's checkout are kept.

        The checkout holds them, so a run writes its own state into the tree
        it was given and leaves the selected home to the account that owns
        it. The entries inside still point back at that home, which is what
        keeps one login serving every session derived from it.
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
        the first had already derived — whose entries point at the first and
        are never re-pointed. Nothing would fail: the session would open and
        run as the wrong login, which is the one reading a profile exists to
        rule out.
        """
        identity = f"{self.shared.resolve()}\n{workspace.resolve()}"
        digest = hashlib.sha256(identity.encode("utf-8"))
        return f"{workspace.name}-{digest.hexdigest()[:12]}"

    def derive(self, workspace: Path) -> Path:
        """Create or refresh the home that one workspace's sessions open."""
        home = self.root(workspace) / self.name_for(workspace)
        home.mkdir(parents=True, exist_ok=True)
        self.unlink_private(home)
        self.link_shared(home)
        return home

    def unlink_private(self, home: Path) -> None:
        """Drop every link one derived home holds where an entry must be its own.

        A private entry is the derived home's own file or nothing. A link in
        its place hands every session opened through the home the shared file
        itself — the one the layout names private because sessions racing on
        it corrupt it — so a derivation refreshes the home toward the layout it
        is derived under rather than trusting what an earlier one left there.
        """
        for name in self.layout.private_files:
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
        reserved = [self.layout.derived_dir.parts[0], *self.layout.private_files]
        for entry in self.shared.iterdir():
            link = home / entry.name
            if entry.name in reserved or link.is_symlink() or link.exists():
                continue
            link.symlink_to(entry)
