"""Everything lup keeps on this machine for contained sessions, and what nothing points at.

A launch sweeps as it goes — superseded images after a build, environments
whose worktree is gone, stopped proxies — but a machine that has not launched
in a while, or one whose projects moved, keeps what it kept. `harness clean`
lists all of it with sizes, says what points at each thing, and removes what
nothing does; a dry run unless ``--yes``, since what it removes is gigabytes
and some of it is a person's history.

What points at each kind:

images
    A checkout's readable tag. A digest tag with none beside it is finished
    (:func:`~lup.launch.container.superseded_images`).
volumes
    A container holding it; a repository's config home
    (``lup-<runtime>-<repo>``), which only that repository can say it no
    longer needs; a cache this project declares. This repository's old
    shared config home is split into the per-runtime ones rather than
    removed; another repository's waits for that repository's next launch. A
    sandbox workspace no container holds is finished.
environments
    The checkout each was made for, while it exists
    (:mod:`lup.launch.environments`).
revisions
    A running container binding it: the plugin revision a contained Codex
    session runs its hooks from, written on the host and held read-only in
    its home (:func:`~lup.launch.environments.revisions_home`). One nothing
    running binds is finished; the next launch needing it writes it again.
containers
    Egress proxies, which are left standing when they stop so their log can
    be read; a stopped one is finished. Sandbox and job containers keep
    lifecycles of their own and are not this command's to judge.
"""

import shutil
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal

import sh
from pydantic import BaseModel, Field, TypeAdapter, ValidationError

from lup.launch.config_volume import (
    HomeHelper,
    LegacyVolumes,
    RuntimeVolume,
    attached_containers,
    existing_volumes,
    remove_volume,
    split_config_volumes,
    sweep_superseded,
)
from lup.launch.container import (
    IMAGE_PREFIX,
    checkout_tag,
    finished_containers,
    retire_images,
    state_volume_name,
    superseded_images,
)
from lup.launch.environments import (
    HeldEnvironment,
    recorded_environments,
    revisions_home,
)
from lup.launch.superseded import SupersededFile
from lup.harness.egress import PROXY_LABEL
from lup.harness.image import ContainerEngine, Image
from lup.harness.notice import Notice
from lup.providers.login import ProviderLogin
from lup.sandbox.rail import sibling_worktrees

type HeldKind = Literal["image", "volume", "environment", "revision", "container"]
"""Which kind of thing lup keeps for contained sessions."""

# lup: ignore[constant-declaration] — the name every sandbox workspace volume
# is minted under, which this reads rather than chooses
SANDBOX_VOLUME_PREFIX = "lup-sandbox-ws-"
"""What a sandbox's workspace volume is called before its own suffix."""


class Held(BaseModel, frozen=True):
    """One thing lup keeps, how big it is, and what points at it."""

    kind: HeldKind
    name: str
    size: str = ""
    """As the engine or the filesystem reports it; empty where unknown."""

    why: str
    """What points at it, or, where it is finished, why nothing does."""

    finished: bool = False

    def line(self) -> str:
        """One row of the listing."""
        marker = "remove" if self.finished else "keep  "
        size = f"{self.size:>9}" if self.size else " " * 9
        return f"  {marker} {size}  {self.name}  — {self.why}"


class Kept:
    """What decides how long a superseded volume stays: the record, the person's days, now."""

    def __init__(self, record: SupersededFile, after: timedelta, now: datetime) -> None:
        self.record = record
        self.after = after
        self.now = now


class ListedImage(BaseModel, frozen=True):
    """One image as the engine lists it, through the template below."""

    tag: str
    size: str


def readable_size(size: int) -> str:
    """Bytes as a person reads them."""
    scaled = float(size)
    for unit in ["B", "KB", "MB", "GB"]:
        if scaled < 1024:
            return f"{scaled:.1f} {unit}"
        scaled /= 1024
    return f"{scaled:.1f} TB"


def images(engine: ContainerEngine, root: Path) -> list[Held]:
    """Every image lup built, sized, the finished ones marked."""
    try:
        listed = sh.Command(engine.binary)(
            "images",
            "--format",
            '{"tag": "{{.Repository}}:{{.Tag}}", "size": "{{.Size}}"}',
        )
    except (sh.CommandNotFound, sh.ErrorReturnCode):
        return []
    rows: list[ListedImage] = []
    for line in str(listed).splitlines():
        try:
            rows.append(ListedImage.model_validate_json(line))
        except ValidationError:
            continue
    finished = superseded_images(engine, checkout_tag(root))
    named = [
        (row.tag[row.tag.index(IMAGE_PREFIX) :], row.size)
        for row in rows
        if IMAGE_PREFIX in row.tag
    ]
    return [
        Held(
            kind="image",
            name=tag,
            size=size,
            why="a digest tag no checkout points at"
            if tag in finished
            else "a checkout's image",
            finished=tag in finished,
        )
        for tag, size in named
    ]


def volume_sizes(helper: HomeHelper | None, names: list[str]) -> list[str]:
    """Each volume's size, in order, from one helper container reading them all."""
    if helper is None or not names:
        return ["" for _ in names]
    places = [f"/lup-sized/{index}" for index in range(len(names))]
    mounts = [f"{name}:{place}:ro" for name, place in zip(names, places, strict=True)]
    try:
        measured = helper.run("du", mounts, ["-sk", *places])
    except (sh.CommandNotFound, sh.ErrorReturnCode):
        return ["" for _ in names]
    sized = {
        words[1]: readable_size(int(words[0]) * 1024)
        for words in (line.split() for line in measured.splitlines())
        if len(words) == 2 and words[0].isdigit()
    }
    return [sized.get(place, "") for place in places]


def volumes(
    root: Path,
    engine: ContainerEngine,
    image: Image,
    logins: list[ProviderLogin],
    helper: HomeHelper | None,
    kept: Kept,
) -> list[Held]:
    """Every volume lup named, sized, with what points at each."""
    names = [name for name in existing_volumes(engine) if name.startswith("lup-")]
    worktrees = [root.name, *(path.name for path in sibling_worktrees(root))]
    recorded = kept.record.load()
    unsplit = [name for name in names if name not in recorded.names()]
    ours = LegacyVolumes.found(root, unsplit, logins, worktrees).every()
    homes = [f"lup-{login.state_volume}-" for login in logins]
    caches = [cache.name for cache in image.caches]

    def judged(name: str) -> Held:
        held = attached_containers(name, engine)
        if held:
            return Held(kind="volume", name=name, why=f"held by {', '.join(held)}")
        superseded = [item for item in recorded.volumes if item.name == name]
        if superseded:
            removal = superseded[0].removal_date(kept.after).isoformat()
            return Held(
                kind="volume",
                name=name,
                why=(
                    f"superseded by {', '.join(superseded[0].moved_into)}; "
                    f"a launch removes it from {removal}"
                ),
                finished=True,
            )
        if name in ours:
            return Held(
                kind="volume",
                name=name,
                why="this repository's old config home: copied into one per runtime",
                finished=True,
            )
        if any(name.startswith(home) for home in homes):
            return Held(kind="volume", name=name, why="a repository's config home")
        if name in caches:
            return Held(kind="volume", name=name, why="a cache this project declares")
        if name.startswith("lup-cfg-"):
            return Held(
                kind="volume",
                name=name,
                why="another repository's old config home; split at its next launch",
            )
        if name.startswith(SANDBOX_VOLUME_PREFIX):
            return Held(
                kind="volume",
                name=name,
                why="a sandbox workspace no container holds",
                finished=True,
            )
        return Held(kind="volume", name=name, why="not lup's to judge")

    judgements = [judged(name) for name in names]
    return [
        held.model_copy(update={"size": size})
        for held, size in zip(judgements, volume_sizes(helper, names), strict=True)
    ]


def environments(root: Path) -> list[Held]:
    """Every project environment on this machine, sized, with its checkout."""

    def why(held: HeldEnvironment) -> str:
        if held.root is None:
            return "unclaimed: launch from its checkout to claim it, or remove it"
        if held.finished():
            return f"its checkout {held.root} is gone"
        return f"the environment of {held.root}"

    return [
        Held(
            kind="environment",
            name=str(held.directory),
            size=readable_size(held.size()),
            why=why(held),
            finished=held.finished(),
        )
        for held in recorded_environments([root, *sibling_worktrees(root)])
    ]


class BoundMount(BaseModel, frozen=True):
    """One mount of a container, as the engine's inspection spells it."""

    source: str = Field(alias="Source")


def bound_mounts(engine: ContainerEngine) -> list[BoundMount] | None:
    """Every mount a running container holds, or ``None`` where the engine does not say."""
    try:
        running = str(sh.Command(engine.binary)("ps", "-q")).split()
        answered = [
            str(
                sh.Command(engine.binary)(
                    "inspect", "--format", "{{json .Mounts}}", container
                )
            )
            for container in running
        ]
    except (sh.CommandNotFound, sh.ErrorReturnCode):
        return None
    try:
        return [
            mount
            for inspected in answered
            for mount in TypeAdapter(list[BoundMount]).validate_json(inspected)
        ]
    except ValidationError:
        return None


def revisions(engine: ContainerEngine | None) -> list[Held]:
    """Every held Codex revision on this machine, sized, the ones nothing running binds marked."""
    home = revisions_home()
    if not home.is_dir():
        return []
    held = sorted(
        path
        for path in home.iterdir()
        if path.is_dir() and not path.name.startswith(".")
    )
    mounts = bound_mounts(engine) if engine is not None and held else None
    bound = None if mounts is None else {Path(mount.source) for mount in mounts}

    def judged(directory: Path) -> Held:
        size = readable_size(
            sum(
                path.lstat().st_size
                for path in directory.rglob("*")
                if path.is_file() and not path.is_symlink()
            )
        )
        if bound is None:
            return Held(
                kind="revision",
                name=str(directory),
                size=size,
                why="no container engine said what binds it",
            )
        if directory in bound:
            return Held(
                kind="revision",
                name=str(directory),
                size=size,
                why="a running container holds it",
            )
        return Held(
            kind="revision",
            name=str(directory),
            size=size,
            why="no running container holds it; a launch needing it writes it again",
            finished=True,
        )

    return [judged(directory) for directory in held]


def containers(engine: ContainerEngine) -> list[Held]:
    """Every egress proxy lup started, the stopped ones marked."""
    try:
        listed = sh.Command(engine.binary)(
            "ps", "-a", "--filter", f"label={PROXY_LABEL}", "--format", "{{.Names}}"
        )
    except (sh.CommandNotFound, sh.ErrorReturnCode):
        return []
    stopped = finished_containers(engine, keep="")
    return [
        Held(
            kind="container",
            name=name,
            why="a stopped egress proxy"
            if name in stopped
            else "a running egress proxy",
            finished=name in stopped,
        )
        for name in str(listed).split()
    ]


def inventory(
    root: Path,
    image: Image,
    engine: ContainerEngine | None,
    logins: list[ProviderLogin],
    helper: HomeHelper | None,
    kept: Kept,
) -> list[Held]:
    """Everything lup keeps for contained sessions on this machine."""
    engined = (
        [
            *images(engine, root),
            *volumes(root, engine, image, logins, helper, kept),
            *containers(engine),
        ]
        if engine is not None
        else []
    )
    return [*engined, *environments(root), *revisions(engine)]


def listing(held: list[Held], engine: ContainerEngine | None) -> list[str]:
    """The inventory as a person reads it, kind by kind."""
    kinds: list[HeldKind] = ["image", "volume", "environment", "revision", "container"]
    unasked = (
        "No container client answered, so images, volumes and containers are unlisted."
    )
    return [
        *([] if engine is not None else [unasked]),
        *(
            line
            for kind in kinds
            for line in [
                f"{kind}s:",
                *([item.line() for item in held if item.kind == kind] or ["  none"]),
            ]
        ),
    ]


def removed_container(name: str, engine: ContainerEngine) -> bool:
    """Remove one container, answering whether it went."""
    try:
        sh.Command(engine.binary)("rm", name)
    except (sh.CommandNotFound, sh.ErrorReturnCode):
        return False
    return True


def cleaned(
    root: Path,
    held: list[Held],
    engine: ContainerEngine | None,
    logins: list[ProviderLogin],
    helper: HomeHelper | None,
    kept: Kept,
) -> list[Notice]:
    """Remove every finished thing, answering what went.

    This repository's old config home is copied into the per-runtime
    volumes first, and every superseded volume — that one included — is
    removed now, whatever its date; one a container holds is kept.
    """
    finished = [item for item in held if item.finished]
    split = (
        split_config_volumes(
            root,
            helper,
            [
                RuntimeVolume(login=login, volume=state_volume_name(root, login))
                for login in logins
            ],
            kept.record,
            kept.after,
            kept.now,
        )
        if helper is not None
        else []
    )
    superseded = (
        sweep_superseded(engine, kept.record, kept.after, kept.now, early=True)
        if engine is not None
        else []
    )
    gone = [
        *(
            retire_images(
                [item.name for item in finished if item.kind == "image"], engine
            )
            if engine is not None
            else []
        ),
        *(
            item.name
            for item in finished
            if engine is not None
            and (
                (
                    item.kind == "volume"
                    and item.name.startswith(SANDBOX_VOLUME_PREFIX)
                    and remove_volume(item.name, engine)
                )
                or (item.kind == "container" and removed_container(item.name, engine))
            )
        ),
    ]
    environments_gone = [item for item in finished if item.kind == "environment"]
    for item in environments_gone:
        HeldEnvironment(directory=Path(item.name)).remove()
    revisions_gone = [item for item in finished if item.kind == "revision"]
    for item in revisions_gone:
        shutil.rmtree(item.name)
    gone = [
        *gone,
        *superseded,
        *(item.name for item in environments_gone),
        *(item.name for item in revisions_gone),
    ]
    return [
        *split,
        Notice(
            text=f"Removed {len(gone)}: {', '.join(gone)}"
            if gone
            else "Removed nothing.",
            urgency="detail",
        ),
    ]
