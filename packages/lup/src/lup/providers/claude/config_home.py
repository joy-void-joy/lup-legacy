# lup: ignore[constant-declaration]
# Every constant here is what Claude Code itself calls one of these things, so
# each docstring below states the value's own provenance and a caller passing
# a different one would be reading a home no runtime writes.
"""Claude Code's configuration document, and a private one per workspace.

Where that document sits is Claude's own rule rather than a shape any
portable contract carries, and it is decided in two steps. A ``.config.json``
inside the configuration home is read wherever one exists. Otherwise the
document is ``.claude.json`` — inside the directory ``CLAUDE_CONFIG_DIR``
names, or, with the variable unset, beside the home directory rather than
inside ``~/.claude`` — spelled ``.claude-custom-oauth.json`` instead while a
custom OAuth server is selected. Every part of that matters, because a
session reading the wrong document starts holding no record of the projects
it is trusted in, no theme and no onboarding — the same degraded state as
having no document at all — and the first step is decided by existence
alone: a legacy file that is merely present is the one read, however empty.

Trust is recorded here too, and only for a workspace lup itself created.
Invoking a run against a repository is an explicit act of trust by whoever
ran it; lup extends exactly that to the checkouts it makes of that same
repository, and to nothing else it happens to open a session in.
"""

import json
import os
from pathlib import Path

from pydantic import BaseModel, field_validator

from lup.providers.claude.login import CLAUDE_LOGIN
from lup.channels.models import write_atomic
from lup.providers.session_home import SessionHomeLayout, SessionHomes
from lup.types import EnvVars, JsonObject, JsonValue

CLAUDE_LEGACY_DOCUMENT = ".config.json"
"""What Claude Code first called its configuration document, inside the home.

Read ahead of every other name wherever one exists, whatever the environment
selects, and never created by the runtime itself: a home holds one only
because something put it there, and from then on it is the only document a
session opened there reads."""

CLAUDE_SESSION_ENV = "session-env"
"""Where it keeps one entry per session's per-call environment.

The runtime's own name for it, and the directory it carves out of the
filesystem grants it computes — which is why a session that cannot write
here loses every shell call to an error naming no boundary."""

CLAUDE_HOME_DOCUMENT = ".claude.json"
"""What it calls that document wherever no legacy one exists.

An entry inside the directory ``CLAUDE_CONFIG_DIR`` names, and with nothing
named a file in the user's home directory rather than an entry inside
``~/.claude`` — so naming ``~/.claude`` outright selects a different document
than naming nothing."""

CLAUDE_OAUTH_URL_ENV = "CLAUDE_CODE_CUSTOM_OAUTH_URL"
"""The variable selecting an OAuth server other than Claude's own."""

CLAUDE_OAUTH_DOCUMENT = ".claude-custom-oauth.json"
"""The document read in ``.claude.json``'s place while that variable is set.

An account signed in through another OAuth server keeps a document of its
own, so one home can hold both and the variable decides which a session
reads."""

CLAUDE_HOME_DIR = CLAUDE_LOGIN.ambient_home.name
"""The directory Claude Code reads settings from, in a workspace as in the
user's home, where it is the configuration home a session falls back to."""

WORKSPACE_SETTINGS = "settings.json"
"""A workspace's own settings, inside the directory Claude reads it from."""

CLAUDE_BACKUP_DIR = "backups"
"""Where Claude Code copies a document it could not read, inside the home
wherever the document itself sits."""

TRUST_FIELD = "hasTrustDialogAccepted"
"""The field a project entry carries once its workspace has been trusted."""

CLAUDE_HOME_LAYOUT = SessionHomeLayout(
    private_files=[CLAUDE_LEGACY_DOCUMENT, CLAUDE_HOME_DOCUMENT, CLAUDE_OAUTH_DOCUMENT]
)
"""Claude keeps one document, which a startup rewrites, under any of these names.

Every one of them is private: a derived home linking any back to the shared
home would hand its sessions the shared document again, and a linked legacy
one would be read ahead of the home's own."""


class ClaudeConfigUnreadable(RuntimeError):
    """A configuration document exists but cannot be read as one.

    Raised rather than treated as absent: an unreadable document holds every
    project the profile has been trusted in, so continuing from an empty one
    would start each session with the repository's declared permissions
    silently dropped — the exact degradation a caller reads this to avoid.

    Unparseable and unopenable are the same fact to a caller, and the reason
    above does not distinguish them: a document behind a permission the
    session lacks holds those projects exactly as a malformed one does. Only
    the first was caught, so the second escaped as a bare `PermissionError`
    past every caller written to answer this — the fault report that exists
    to say a run cannot open a session anywhere crashed instead of saying it.
    """


class ClaudeConfigHome(BaseModel, frozen=True):
    """One selected configuration home, and the document it reads."""

    directory: Path
    document: Path

    def configuration_fault(self) -> str | None:
        """Why no session can be opened under this home yet, if anything.

        Every private home a run derives is seeded from this one document, so a
        run that cannot read it cannot open a session anywhere — a fact about
        the environment rather than about any one piece of work. Answered once
        and up front, it is a single message before anything is leased, instead
        of the same fault rediscovered by every session that races to start.
        """
        try:
            load_document(self.document)
        except ClaudeConfigUnreadable as error:
            return f"{error}. {restoration_advice(self.directory)}"
        return None

    def shell_fault(self) -> str | None:
        """Why a session opened under this home would lose every shell call.

        A session keeps each Bash call's environment under ``session-env`` and
        makes its own entry at startup. The runtime carves that directory out
        of its own filesystem grants, so a launcher that is itself confined
        opens sessions whose every shell call dies on
        ``EROFS: read-only file system, mkdir`` — with nothing in that error
        naming a boundary, which is the failure a session then debugs as a
        broken repository.

        Measured rather than inferred, twice over. The carve-out survives a
        grant: a write here is refused inside the very repository root the
        same declaration grants, so widening the declaration is not the
        remedy. And the variable saying a sandbox is running is inherited by a
        command placed outside it, so it answers a different question than
        this one. Writing is the only thing that establishes writing.

        Claude's alone: Codex keeps no per-call environment directory, so
        there is nothing there to be carved out and nothing here to check.
        """
        probe = self.directory / CLAUDE_SESSION_ENV / "lup-writable-probe"
        try:
            probe.mkdir(parents=True)
            probe.rmdir()
        except OSError as error:
            return (
                f"{probe.parent} cannot be written ({error.strerror}), so every "
                "shell call in a session opened here would fail as a read-only "
                "filesystem. That is the boundary rather than the repository: "
                "the runtime carves this directory out of its own grants, so "
                "widening the declaration does not reach it. Open the session "
                "from outside the sandbox instead."
            )
        return None


def home_document(environment: EnvVars) -> str:
    """What Claude Code calls its document under this environment, legacy aside."""
    if environment.get(CLAUDE_OAUTH_URL_ENV):
        return CLAUDE_OAUTH_DOCUMENT
    return CLAUDE_HOME_DOCUMENT


def selected_config_home(environment: EnvVars) -> ClaudeConfigHome:
    """The home and document a session opened under this environment reads.

    Claude Code's own resolution, in its own order: a legacy document inside
    the home wherever one exists, and otherwise the current one — inside a
    named home, or beside the user's home directory when none is named. The
    home is :meth:`ProviderLogin.selected_home`'s, read off the same
    environment, ``HOME`` included.

    An exported-but-empty ``CLAUDE_CONFIG_DIR`` names no home, as it names
    none for Codex. Claude Code itself reads an empty value as its own
    working directory for the home while its document falls back beside
    ``HOME``; that directory is not something every reader of this answer can
    know — a mount, an editor beside the CLI, the launcher seeding a home —
    and reading it as this process's directory seeded a home from wherever
    the launcher happened to run.
    """
    # lup: solved: an exported empty CLAUDE_CONFIG_DIR is read here as the
    # directory `.`, this process's working directory, with the document
    # inside it. Claude Code 2.1.282 splits that value: its home keeps it
    # (`CLAUDE_CONFIG_DIR ?? join(homedir(), ".claude")`, so its own working
    # directory) while its document falls back beside the user's home
    # (`CLAUDE_CONFIG_DIR || homedir()`). A session homed under one is seeded
    # from `./.claude.json` and links `.`'s entries by relative name, each
    # link pointing at itself. Decide whether an empty value names no home,
    # as Codex's does, or mirror both halves against the session's directory.
    directory = CLAUDE_LOGIN.selected_home(environment)
    legacy = directory / CLAUDE_LEGACY_DOCUMENT
    user = environment["HOME"] if "HOME" in environment else ""
    beside = (
        directory
        if CLAUDE_LOGIN.named_home(environment) is not None
        else Path(user)
        if user
        else Path.home()
    )
    current = beside / home_document(environment)
    return ClaudeConfigHome(
        directory=directory, document=legacy if legacy.exists() else current
    )


def session_config_home(environment: EnvVars) -> Path:
    """The configuration home a session spawned with ``environment`` runs under.

    The CLI a session starts inherits this process's environment beneath the
    variables it is handed, so the home it writes to is the one those name
    over this process's own. Reading it here is what lets the session's
    transcripts be found under its home rather than under whichever home the
    calling process happens to name — which is all the SDK's own readers
    consult.
    """
    # lup: ignore[os-environ] — what a spawned session inherits
    inherited = dict(os.environ)
    return selected_config_home({**inherited, **environment}).directory


def load_document(path: Path) -> JsonObject:
    """Read one Claude JSON document, or answer that there is not one yet."""
    if not path.exists():
        return {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise ClaudeConfigUnreadable(
            f"Claude configuration at {path} cannot be read: {error}"
        ) from error
    try:
        decoded = json.loads(text)
    except json.JSONDecodeError as error:
        raise ClaudeConfigUnreadable(
            f"Claude configuration at {path} does not parse: {error}"
        ) from error
    if not isinstance(decoded, dict):
        raise ClaudeConfigUnreadable(
            f"Claude configuration at {path} is not a JSON object"
        )
    return decoded


def save_document(path: Path, document: JsonObject) -> None:
    """Write one Claude JSON document so no reader can catch it half-written."""
    write_atomic(path, json.dumps(document).encode("utf-8"))


class ProjectSection(BaseModel):
    """The per-project section of Claude's own configuration document.

    Claude writes that document itself and has been seen writing it corrupt,
    so a `projects` that is not an object reads here as no projects rather
    than as a failure — the recovery path that reads it is the one repairing
    exactly that, and it must be able to look.
    """

    projects: JsonObject = {}

    @field_validator("projects", mode="before")
    @classmethod
    def an_object_or_none_at_all(cls, value: JsonValue) -> JsonValue:
        return value if isinstance(value, dict) else {}


def project_entries(document: JsonObject) -> JsonObject:
    """The per-project section of one document, empty when it carries none."""
    return ProjectSection.model_validate(document).projects


def project_entry(document: JsonObject, workspace: Path) -> JsonObject:
    """One workspace's entry in a document, empty when it has none yet."""
    found = project_entries(document).get(str(workspace.resolve()))
    return dict(found) if isinstance(found, dict) else {}


def restorable_backups(directory: Path) -> list[Path]:
    """Every backup of one home's document that could actually restore it.

    Claude Code answers a document it cannot parse with a hint naming the
    backup it just wrote, and the backup it wrote for a truncated document
    was zero bytes — so following the hint replaces a since-healed
    configuration with an empty one. What makes a backup worth restoring is
    not that it exists but that it parses and still carries the project
    entries trust and permissions live in, which is what is answered here.

    Newest first, by the time the file was written rather than by the
    timestamp in its name, so a caller naming one names the least lost.
    """

    def restores(path: Path) -> bool:
        """Whether one backup parses and carries what a restore would need."""
        try:
            return bool(project_entries(load_document(path)))
        except ClaudeConfigUnreadable:
            return False

    backups = directory / CLAUDE_BACKUP_DIR
    if not backups.is_dir():
        return []
    return sorted(
        (path for path in backups.iterdir() if path.is_file() and restores(path)),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )


def restoration_advice(directory: Path) -> str:
    """How to get one unreadable configuration home back, in this state."""
    restorable = restorable_backups(directory)
    if not restorable:
        return (
            f"No backup under {directory / CLAUDE_BACKUP_DIR} both parses and "
            "carries project entries, so none of them can restore it: move the "
            "document aside and open one session to write a fresh one, which "
            "starts out trusting nothing and knowing no projects"
        )
    return (
        f"Restore it from {restorable[0]}, the most recently written backup "
        "that both parses and carries project entries"
    )


def trusts(document: JsonObject, workspace: Path) -> bool:
    """Whether one document already records that workspace as trusted."""
    entry = project_entry(document, workspace)
    return entry.get(TRUST_FIELD) is True


def record_trust(path: Path, workspace: Path) -> None:
    """Record one workspace as trusted, in a document lup owns.

    Trust has no project-level home to write to. Claude Code keeps it in the
    user-level configuration document and offers nowhere else, so this is a
    deliberate, narrow exception to the convention that lup writes harness
    settings project-level and never user-level. It stays narrow in two
    ways: the document written is the private per-session one derived under
    the profile rather than the operator's own, and the only workspace ever
    recorded is a worktree lup created for this run.
    """
    document = load_document(path)
    entries = project_entries(document)
    entry = project_entry(document, workspace)
    entry[TRUST_FIELD] = True
    entries[str(workspace.resolve())] = entry
    document["projects"] = entries
    save_document(path, document)


def declared_allowances(workspace: Path, settings_dir: str = CLAUDE_HOME_DIR) -> int:
    """How many ``permissions.allow`` entries a workspace declares for itself.

    Counted rather than listed because a caller reports a loss, not a
    policy: what a reader needs to know is that entries exist and are being
    ignored, and the workspace's own settings file is where to read them.
    """
    match load_document(workspace / settings_dir / WORKSPACE_SETTINGS):
        case {"permissions": {"allow": [*entries]}}:
            return len(entries)
    return 0


def untrusted_degradation(workspace: Path, document: Path) -> str | None:
    """What a session opened in this workspace would silently lose, if anything.

    An untrusted workspace does not fail: Claude drops the repository's
    declared permissions, warns once into that session's own stderr, and
    carries on. A run made of many sessions therefore reports a changed
    permission posture only as noise interleaved with progress, so the fact
    is derived here for a caller that can refuse to open the session at all.
    """
    declared = declared_allowances(workspace)
    if declared == 0 or trusts(load_document(document), workspace):
        return None
    return (
        f"{workspace} is not a trusted workspace, so the {declared} "
        "permissions.allow entries its .claude/settings.json declares are "
        "ignored by every session opened there. Sessions still run, under a "
        "different permission posture than the repository declares."
    )


def workspace_config_environment(
    environment: EnvVars,
    workspace: Path,
    *,
    trust: bool = False,
    layout: SessionHomeLayout = CLAUDE_HOME_LAYOUT,
) -> EnvVars:
    """Point the sessions of one workspace at a configuration home of their own.

    Returns the variables to merge over ``environment``. The home is derived
    under whichever one that environment already selects, so a profile
    naming the account still decides the account: this narrows what a
    session writes, never which login it writes under.

    The home keeps its document under the current name, seeded from whichever
    document the selected home is read from, legacy or current. It holds no
    legacy one, whatever put one there, because Claude Code would read that
    in place of the home's own — and the home is lup's to keep that way.

    Read with this process's ``HOME``, the operator's, over any ``HOME`` the
    environment carries for a session's tools: with no home named, the
    account a derived home is seeded from and linked back to is the one this
    program was launched as, for the reason ``CLAUDE_LOGIN`` gives.
    """
    selected = selected_config_home({**environment, "HOME": str(Path.home())})
    home = SessionHomes(selected.directory, layout).derive(workspace)
    (home / CLAUDE_LEGACY_DOCUMENT).unlink(missing_ok=True)
    document = home / home_document(environment)
    if not document.exists():
        save_document(document, load_document(selected.document))
    if trust:
        record_trust(document, workspace)
    return CLAUDE_LOGIN.environment(home)
