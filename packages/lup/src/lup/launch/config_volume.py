"""A contained session's configuration home: one volume per repository and runtime.

Per repository, because every worktree of one repository shares the login,
the trust and the transcripts a ``--continue`` reopens, and keyed on the
shared git directory, the only name all of them agree on. Per runtime,
because a home both CLIs wrote into is a home each can read the other's
transcripts and credentials out of, holding both vendors' files mixed
together (``lup-claude-<repo>``, ``lup-codex-<repo>``). And not per person:
a volume every repository shared would be one repository's session reading
another's transcripts and writing settings every other project then applies.

Settings do not live here as decisions. A launch seeds them at every start
and carries a session's personal changes back to the host when it closes
(:mod:`lup.providers.claude.home_seed`), so what a volume holds is state:
the login, the trust, the history, the transcripts.

An engine can also hold homes in the unsplit layouts: one volume both
runtimes write into (``lup-cfg-<repo>``), one per worktree
(``lup-cfg-<worktree>``), and Codex's one per digest of its settings
(``lup-cfg-<repo>-codex-<digest>``). The first launch that finds any of them
moves what they hold into the split volumes once, by what each runtime
declares it keeps, and records them for removal.
"""

import io
from datetime import datetime, timedelta
import tarfile
from fnmatch import fnmatch
from pathlib import Path
from typing import Literal

import sh
from pydantic import BaseModel, TypeAdapter, ValidationError

from lup.channels.models import write_atomic
from lup.harness.assets.credential_seed import (
    chosen_login,
    login_fingerprint,
)
from lup.harness.assets.home_seed import (
    RECORD,
    SeedFile,
    merged_names,
    read_tree,
    settle,
    text_of,
    write,
)
from lup.launch.refusal import LaunchRefused
from lup.launch.superseded import SupersededFile
from lup.harness.image import ContainerEngine
from lup.harness.notice import Notice
from lup.providers.login import ProviderLogin
from lup.providers.user_config import UserConfigFile
from lup.sandbox.rail import repository_layout, sibling_worktrees
from lup.types import JsonObject
from lup.workspace.user_directories import UserDirectories

# lup: ignore[constant-declaration] — where a split reads the old volume from
# inside its helper container, a path no image holds and nothing else mounts
SPLIT_SOURCE = "/lup-split-from"
"""Where a helper container mounts the volume being split, read-only."""


def shared_volume_name(root: Path) -> str:
    """The unsplit configuration home both runtimes write into, by repository."""
    return f"lup-cfg-{repository_layout(root).name()}"


def answered_volumes(engine: ContainerEngine) -> list[str] | None:
    """Every volume this engine holds, or ``None`` when it cannot be asked."""
    try:
        listed = sh.Command(engine.binary)("volume", "ls", "--format", "{{.Name}}")
    except (sh.CommandNotFound, sh.ErrorReturnCode):
        return None
    return str(listed).split()


def existing_volumes(engine: ContainerEngine) -> list[str]:
    """Every volume this engine holds, or nothing when it cannot be asked.

    An engine that will not answer is not a reason to fail a launch that is
    otherwise fine -- a split waits for a launch whose engine answers.
    """
    return answered_volumes(engine) or []


def attached_containers(volume: str, engine: ContainerEngine) -> list[str]:
    """Every container, running or stopped, holding a volume."""
    try:
        listed = sh.Command(engine.binary)(
            "ps", "-a", "--filter", f"volume={volume}", "--format", "{{.Names}}"
        )
    except (sh.CommandNotFound, sh.ErrorReturnCode):
        return []
    return str(listed).split()


class HomeSplit(BaseModel, frozen=True):
    """How one volume's entries are shared out between runtimes' volumes."""

    owned: dict[str, list[str]]
    """Each runtime's volume word, and the entries it takes."""

    unknown: list[str] = []
    """Entries no runtime declares, which every runtime takes."""

    debris: list[str] = []
    """Entries a runtime declares nothing reads again, which none takes."""

    @classmethod
    def of(cls, entries: list[str], logins: list[ProviderLogin]) -> "HomeSplit":
        """Share ``entries`` out by what each runtime declares it keeps."""

        def named(entry: str, patterns: list[str]) -> bool:
            return any(fnmatch(entry, pattern) for pattern in patterns)

        debris = [
            entry
            for entry in entries
            if any(named(entry, login.home_debris) for login in logins)
        ]
        kept = [entry for entry in entries if entry not in debris]
        unknown = [
            entry
            for entry in kept
            if not any(named(entry, login.home_entries) for login in logins)
        ]
        return cls(
            owned={
                login.state_volume: [
                    entry
                    for entry in kept
                    if entry in unknown or named(entry, login.home_entries)
                ]
                for login in logins
            },
            unknown=unknown,
            debris=debris,
        )


class UnsplitVolumes(BaseModel, frozen=True):
    """One repository's configuration homes in a layout not split per runtime."""

    shared: str | None = None
    """The one both runtimes write into, where the engine holds it."""

    scoped: dict[str, list[str]] = {}
    """Each runtime's volume word, and the volumes it holds per settings digest."""

    branches: list[str] = []
    """The per-worktree volumes, one for each worktree that has one."""

    @classmethod
    def found(
        cls,
        root: Path,
        existing: list[str],
        logins: list[ProviderLogin],
        worktrees: list[str],
    ) -> "UnsplitVolumes":
        """Which of this repository's unsplit volumes the engine holds."""
        shared = shared_volume_name(root)
        branches = [f"lup-cfg-{name}" for name in worktrees]
        return cls(
            shared=shared if shared in existing else None,
            scoped={
                login.state_volume: sorted(
                    name
                    for name in existing
                    if name.startswith(f"{shared}-{login.state_volume}-")
                )
                for login in logins
            },
            branches=sorted(
                name for name in existing if name in branches and name != shared
            ),
        )

    def every(self) -> list[str]:
        """Every unsplit volume, the shared one first."""
        return [
            *([self.shared] if self.shared is not None else []),
            *(name for names in self.scoped.values() for name in names),
            *self.branches,
        ]


class HomeHelper(BaseModel, frozen=True):
    """A throwaway container reading or filling configuration-home volumes.

    Run from the image a session runs in and as the identity it runs as, so
    what it copies arrives owned by whoever the next session is, with the
    volume mounted where a session mounts it. Never on a network, and never
    through the entrypoint, which would seed the very volume being filled.
    """

    engine: ContainerEngine
    tag: str
    uid: int
    gid: int
    config_home: str

    def argv(self, program: str, mounts: list[str], arguments: list[str]) -> list[str]:
        """The command starting one program in a fresh helper container."""
        return [
            self.engine.binary,
            "run",
            "--rm",
            "--network",
            "none",
            *self.engine.identity_arguments(self.uid, self.gid),
            "--entrypoint",
            program,
            *[word for mount in mounts for word in ("-v", mount)],
            self.tag,
            *arguments,
        ]

    def run(self, program: str, mounts: list[str], arguments: list[str]) -> str:
        """Run one program in a fresh helper container, answering what it printed."""
        argv = self.argv(program, mounts, arguments)
        return str(sh.Command(argv[0])(*argv[1:]))

    def entries(self, volume: str) -> list[str]:
        """Every name at the top of a volume."""
        listed = self.run("ls", [f"{volume}:{SPLIT_SOURCE}:ro"], ["-A", SPLIT_SOURCE])
        return listed.splitlines()

    def fill(self, source: str, target: str, entries: list[str]) -> None:
        """Copy entries between volumes, keeping whatever the target already holds."""
        if not entries:
            return
        self.run(
            "cp",
            [f"{source}:{SPLIT_SOURCE}:ro", f"{target}:{self.config_home}"],
            [
                "-a",
                "--update=none",
                *(f"{SPLIT_SOURCE}/{entry}" for entry in entries),
                f"{self.config_home}/",
            ],
        )

    def read(self, volume: str, names: list[str]) -> list["HomeFile"]:
        """The named files at the top of a volume, as they stand, and only those it has.

        One archive from one container rather than a container per file, and
        a name the volume lacks is left out of it rather than failing it.
        """
        argv = self.argv(
            "tar",
            [f"{volume}:{SPLIT_SOURCE}:ro"],
            ["-C", SPLIT_SOURCE, "-cf", "-", "--ignore-failed-read", *names],
        )
        archive = sh.Command(argv[0])(*argv[1:], _return_cmd=True).stdout
        with tarfile.open(fileobj=io.BytesIO(archive)) as held:
            return [
                HomeFile(name=member.name, content=extracted.read())
                for member in held.getmembers()
                if member.isfile() and (extracted := held.extractfile(member))
            ]


class HomeFile(BaseModel, frozen=True):
    """One file read out of a configuration-home volume."""

    name: str
    content: bytes

    def text(self) -> str:
        """The file as the UTF-8 text every file read here is."""
        return self.content.decode("utf-8")


def named_file(files: list[HomeFile], name: str) -> HomeFile | None:
    """The file of that name among those read, if the volume had it."""
    return next((held for held in files if held.name == name), None)


class HomeSeedPlaces(BaseModel, frozen=True):
    """Where one launch lays its seed out, and where it learns what the seed came to.

    ``seed`` is laid out as :attr:`~lup.harness.image.Image.home_seed`
    describes and mounted read-only; ``applied`` is written on the host with
    each file the seed comes to against the volume as it stands, which is what
    the session starts from and what its changes are measured against.
    """

    seed: Path
    applied: Path


def settle_home_seed(
    helper: HomeHelper, volume: str, places: HomeSeedPlaces
) -> list[Notice]:
    """Work out what a seed comes to against a volume, and say what it overrides.

    The same three-way merge the image's entrypoint applies at start
    (:mod:`lup.harness.assets.home_seed`), run here first on what the volume
    holds, so the launch knows what it applied — each session's own seed —
    and can say where the person's settings overrode a key a running session
    had changed too. A volume that cannot be read is taken as empty, which
    the entrypoint's own merge then corrects in the session's favour.
    """
    seed = read_tree(places.seed)
    managed = (text_of(seed, "managed") or "").split()
    names = [*managed, *merged_names(seed)]
    try:
        held = [
            SeedFile(file.name, file.text())
            for file in helper.read(volume, [*names, RECORD])
        ]
    except (sh.CommandNotFound, sh.ErrorReturnCode):
        held = []
    settled = settle(seed, held, managed)
    for outcome in settled:
        write(places.applied / outcome.file.name, outcome.file.text)
    return [
        Notice(
            text=(
                f"Settings: {conflict} was changed both by a session still running "
                "in this repository and in your own settings; yours win."
            ),
            urgency="warning",
        )
        for outcome in settled
        for conflict in outcome.conflicts
    ]


class RuntimeVolume(BaseModel, frozen=True):
    """One runtime's configuration-home declaration, and the volume it lives in."""

    login: ProviderLogin
    volume: str


type SessionsMove = Literal["refuse", "move", "keep"]
"""What a container start does where its login would replace the one running sessions use.

``refuse`` stops it, saying how many sessions it would move and the ways
through: a launch nobody asked to move anything. ``move`` hands the login
anyway and moves them: a launch passed ``--move-sessions``, or a switch.
``keep`` hands nothing and opens on the login the volume already holds: a
probe, which checks what a session would find rather than choosing an account.
"""


class LoginOwner(BaseModel, frozen=True):
    """Whose login a container start hands its repository's volume."""

    home: Path
    """The account's own configuration home: its profile's, the one named
    outright, or the runtime's default, compared resolved."""

    profile: str | None = None
    """The profile naming that account, ``None`` where none is selected."""

    def named(self) -> str:
        """The account as a sentence names it."""
        if self.profile is not None:
            return self.profile
        return f"the account at {self.home}"

    def same_account(self, other: "LoginOwner") -> bool:
        """Whether both name one account, by the home each resolves to."""
        return self.home.expanduser().resolve() == other.home.expanduser().resolve()


class HandedLogin(BaseModel, frozen=True):
    """A host login a container start offers its repository's volume, and whose it is."""

    credential: Path
    """The file the entrypoint applies, offered read-only."""

    owner: LoginOwner
    moving: SessionsMove = "refuse"

    def fingerprint(self, login: ProviderLogin) -> str:
        """What the volume's stamp records once this login is applied, empty where unreadable."""
        try:
            incoming = TypeAdapter(JsonObject).validate_json(
                self.credential.read_bytes()
            )
        except (OSError, ValidationError):
            return ""
        chosen = chosen_login(incoming, login.credential_fields)
        return login_fingerprint(chosen) if chosen else ""


class VolumeLogin(BaseModel, frozen=True):
    """The login lup last handed one configuration-home volume: whose, and when."""

    volume: str
    runtime: str
    """The runtime's word for its volume, as its login declares it."""

    owner: LoginOwner
    fingerprint: str = ""
    """What the volume's stamp records for that login, empty where it was unreadable."""

    handed_at: datetime


class VolumeLogins:
    """Whose login lup last handed each configuration-home volume, kept on the host.

    A volume is read only by starting a container, and its labels are fixed
    when it is made, so what it was handed is recorded where lup keeps the
    person's state, a file per volume: every launch, probe, run worker and
    switch that hands one a login writes it. It says what the volume holds
    unless a session inside signed in to another account since, or the seed
    program declined a login nothing could renew.
    """

    def __init__(self, home: Path | None = None) -> None:
        self.home = (
            home if home is not None else UserDirectories().state() / "volume-logins"
        )

    def path(self, volume: str) -> Path:
        """The file one volume's record is kept in."""
        return self.home / f"{volume}.json"

    def held(self, volume: str) -> VolumeLogin | None:
        """What that volume was last handed, ``None`` where lup never recorded a handoff."""
        path = self.path(volume)
        if not path.is_file():
            return None
        return VolumeLogin.model_validate_json(path.read_text(encoding="utf-8"))

    def record(self, handed: VolumeLogin) -> None:
        """Keep what a volume was just handed, so no reader catches it half-written."""
        write_atomic(
            self.path(handed.volume), handed.model_dump_json(indent=2).encode("utf-8")
        )


def running_containers(volume: str, engine: ContainerEngine) -> list[str]:
    """Every running container holding a volume, by name; none where the engine cannot be asked."""
    try:
        listed = sh.Command(engine.binary)(
            "ps", "--filter", f"volume={volume}", "--format", "{{.Names}}"
        )
    except (sh.CommandNotFound, sh.ErrorReturnCode):
        return []
    return str(listed).split()


class Handoff(BaseModel, frozen=True):
    """What one container start hands its volume, settled before anything starts."""

    credential: Path | None = None
    """The file offered for the entrypoint to apply, ``None`` where nothing is."""

    record: VolumeLogin | None = None
    """What to record once the container's argv stands, ``None`` where nothing is handed."""

    notices: list[Notice] = []


def moving_refusal(
    handed: HandedLogin, held: VolumeLogin, running: list[str], login: ProviderLogin
) -> str:
    """Why a container start would move running sessions, and the ways through, as one diagnostic."""
    count = len(running)
    sessions = f"{count} running contained {login.state_volume} session" + (
        "" if count == 1 else "s"
    )
    new, old = handed.owner.named(), held.owner.named()
    consequence = (
        f"moves each onto {new}'s at its next request"
        if login.rereads_login
        else (
            f"replaces it under each: each keeps {old}'s until it is opened "
            f"again, and opens on {new}'s"
        )
    )
    ways = [
        "→ move them with this launch: pass `--move-sessions`",
        *(
            [
                "→ or move them first, opening nothing: `uv run lup-devtools "
                f"harness profile switch {handed.owner.profile} --runtime "
                f"{login.state_volume}`"
            ]
            if handed.owner.profile is not None
            else []
        ),
        *(
            [
                "→ or open this session on the login they use: pass "
                f"`--profile {held.owner.profile}`"
            ]
            if held.owner.profile is not None
            else []
        ),
    ]
    return "\n".join(
        [
            f"refused: `{new}` — {sessions} of this repository use {old}'s "
            f"login, and handing {held.volume} {new}'s {consequence}",
            *ways,
        ]
    )


def settle_handoff(
    handed: HandedLogin,
    login: ProviderLogin,
    volume: str,
    engine: ContainerEngine,
    logins: VolumeLogins,
    now: datetime,
) -> Handoff:
    """Settle the login a container start hands its repository's volume, before anything starts.

    The entrypoint applies whatever login it is offered, and every session
    running on the volume shares the file it lands in, so offering another
    account's login is a choice made for each of them. Where the volume was
    last handed another account's and a container holding it still runs,
    the start answers as ``handed.moving`` says: refused, saying how many
    it would move and how else to go; handed anyway, saying it moved them;
    or offered nothing, opening on the login the volume holds. Where nothing
    runs there, the volume's login is nobody's but the next session's, and
    where lup never recorded what it holds there is nothing to compare, so
    either hands the login as every start always has.
    """
    record = VolumeLogin(
        volume=volume,
        runtime=login.state_volume,
        owner=handed.owner,
        fingerprint=handed.fingerprint(login),
        handed_at=now,
    )
    held = logins.held(volume)
    if held is None or held.owner.same_account(handed.owner):
        return Handoff(credential=handed.credential, record=record)
    running = running_containers(volume, engine)
    if not running:
        return Handoff(credential=handed.credential, record=record)
    match handed.moving:
        case "refuse":
            raise LaunchRefused(moving_refusal(handed, held, running, login))
        case "keep":
            return Handoff(
                notices=[
                    Notice(
                        text=(
                            f"Login: {volume} keeps {held.owner.named()}'s, which "
                            f"{len(running)} running session(s) use; this opens on "
                            f"it rather than handing {handed.owner.named()}'s."
                        ),
                        urgency="detail",
                    )
                ]
            )
        case "move":
            return Handoff(
                credential=handed.credential,
                record=record,
                notices=[
                    Notice(
                        text=(
                            f"Login: {volume} now holds {handed.owner.named()}'s, "
                            f"in place of {held.owner.named()}'s that "
                            f"{len(running)} running session(s) use"
                            + (
                                "; each takes it at its next request."
                                if login.rereads_login
                                else "; each keeps its own until it is opened again."
                            )
                        ),
                        urgency="warning",
                    )
                ],
            )


def remove_volume(name: str, engine: ContainerEngine) -> bool:
    """Remove one volume, answering whether it went."""
    try:
        sh.Command(engine.binary)("volume", "rm", name)
    except (sh.CommandNotFound, sh.ErrorReturnCode):
        return False
    return True


def kept_for_superseded(config: UserConfigFile | None = None) -> timedelta:
    """How long the person keeps a superseded config volume before it is removed."""
    days = (config or UserConfigFile()).load().cleanup.superseded_volumes_after_days
    return timedelta(days=days)


def split_config_volumes(
    root: Path,
    helper: HomeHelper,
    runtimes: list[RuntimeVolume],
    record: SupersededFile,
    kept_for: timedelta,
    now: datetime,
    existing: list[str] | None = None,
) -> list[Notice]:
    """Copy this repository's unsplit configuration homes into one volume per runtime.

    Once: a volume already recorded as superseded is not copied again. Safely
    again where a split is interrupted: every copy keeps what the target
    already holds, so it finishes on the next launch without overwriting what
    a session wrote in between, and nothing is recorded until every copy
    lands. A volume some container still holds — a session that opened on
    the unsplit home — postpones the whole split to a launch after it closes,
    since its files are still being written.

    The shared volume's entries go to each runtime that declares them, an
    entry nobody declares to every runtime, said aloud; debris goes nowhere.
    A per-digest volume is one runtime's alone and goes to that runtime.
    Per-worktree volumes hold nothing the shared one does not, and are not
    read.

    The unsplit volumes are kept, not removed: each is recorded as superseded
    (:mod:`lup.launch.superseded`) and a launch removes it once
    ``kept_for`` has passed, so a history nobody has checked the copy of is
    still there to check.
    """
    engine = helper.engine
    logins = [runtime.login for runtime in runtimes]
    targets = {runtime.login.state_volume: runtime.volume for runtime in runtimes}
    volumes = existing if existing is not None else existing_volumes(engine)
    worktrees = [root.name, *(path.name for path in sibling_worktrees(root))]
    recorded = record.load()
    unsplit = [name for name in volumes if name not in recorded.names()]
    found = UnsplitVolumes.found(root, unsplit, logins, worktrees)
    old = found.every()
    if not old:
        return []
    held = {name: attached_containers(name, engine) for name in old}
    busy = [f"{name} ({', '.join(users)})" for name, users in held.items() if users]
    if busy:
        return [
            Notice(
                text=(
                    "Config home: still in use by an open session, so its move "
                    "into one volume per runtime waits for the first launch "
                    f"after it closes: {'; '.join(busy)}. This session starts "
                    "in the new volume without it."
                ),
                urgency="warning",
            )
        ]
    split = HomeSplit(owned={})
    try:
        if found.shared is not None:
            split = HomeSplit.of(helper.entries(found.shared), logins)
            for word, entries in split.owned.items():
                helper.fill(found.shared, targets[word], entries)
        for word, scoped in found.scoped.items():
            for volume in scoped:
                entries = helper.entries(volume)
                debris = HomeSplit.of(entries, logins).debris
                kept = [entry for entry in entries if entry not in debris]
                helper.fill(volume, targets[word], kept)
    except (sh.CommandNotFound, sh.ErrorReturnCode) as error:
        return [
            Notice(
                text=(
                    f"Config home: copying {', '.join(old)} into "
                    f"{', '.join(targets.values())} failed, so the next launch "
                    f"tries again: {error}"
                ),
                urgency="warning",
            )
        ]
    record.save(recorded.with_superseded(old, list(targets.values()), now))
    removal = (now + kept_for).date().isoformat()
    said = (
        f"Config home: the history in {', '.join(old)} now lives in "
        f"{', '.join(targets.values())}, one volume per runtime. The old "
        f"{'volume stays' if len(old) == 1 else 'volumes stay'} until {removal}, "
        "when a launch removes "
        + ("it" if len(old) == 1 else "them")
        + "; `harness clean --yes` removes them sooner."
    )
    return [
        Notice(text=said, urgency="warning"),
        *(
            [
                Notice(
                    text=(
                        "Config home: no runtime declares "
                        f"{', '.join(split.unknown)}, so each runtime's volume "
                        "was given a copy."
                    ),
                    urgency="warning",
                )
            ]
            if split.unknown
            else []
        ),
        *(
            [
                Notice(
                    text=(
                        "Config home: not copied, since nothing reads it again: "
                        f"{', '.join(split.debris)}."
                    ),
                    urgency="detail",
                )
            ]
            if split.debris
            else []
        ),
    ]


def sweep_superseded(
    engine: ContainerEngine,
    record: SupersededFile,
    kept_for: timedelta,
    now: datetime,
    early: bool = False,
) -> list[str]:
    """Remove every superseded volume kept as long as the person asked, answering which.

    ``early`` removes every recorded one now, whatever its date — what
    `harness clean --yes` asks for. A volume a container holds is never
    removed, whenever it was superseded; one already gone is forgotten. An
    engine that cannot be asked removes nothing and forgets nothing.
    """
    volumes = answered_volumes(engine)
    if volumes is None:
        return []
    recorded = record.load()
    gone = [name for name in recorded.names() if name not in volumes]
    due = [
        volume.name
        for volume in recorded.volumes
        if volume.name in volumes and (early or volume.expired(kept_for, now))
    ]
    removed = [
        name
        for name in due
        if not attached_containers(name, engine) and remove_volume(name, engine)
    ]
    if gone or removed:
        record.save(recorded.without([*gone, *removed]))
    return removed


def swept_superseded_notice(removed: list[str], kept_for: timedelta) -> list[Notice]:
    """Say, in one line, which superseded volumes a sweep removed."""
    if not removed:
        return []
    return [
        Notice(
            text=(
                f"Config home: removed {', '.join(removed)}, superseded more than "
                f"{kept_for.days} days ago."
            ),
            urgency="detail",
        )
    ]
