"""Resolve unpinned agent CLIs to the registry's current release at launch.

The image declaration leaves an agent CLI's version empty to say "the
registry's current release" -- but an empty version rendered literally is a
lie twice over: the install layer is cached, so `bun add -g name` fetches
latest exactly once and serves that forever, and the image tag stops naming
what the image holds. So the launch resolves the release *before* rendering
and writes a concrete pin into the Dockerfile. The tag is content-addressed
on that text, which is what turns a new release into a rebuild and an
unchanged one into a cache hit, with no machinery beyond what already
decides both.

Resolution belongs to the launch and never to generation: the generated
trees' ownership digests hash the declaration, and a resolved version folded
in there would mark every checkout stale each time the registry moved. What
this module returns is a launch-local copy of the image, used to render and
build, then dropped.

The registry not answering is an expected state, not an error: the last
release this machine resolved is kept in a ledger and pinned in its place,
said out loud with its age. A machine that has never resolved one keeps the
version empty -- the build needs the registry anyway, and fails at that
layer by name.
"""

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pydantic
from pydantic import BaseModel, Field

from lup.harness.image import Image
from lup.harness.notice import Notice
from lup.harness.requirements import Package
from lup.workspace.user_directories import UserDirectories


class CurrentRelease(BaseModel, frozen=True):
    """What a registry answers when asked for one package's current release.

    One field, extras ignored: npm's ``/latest`` document is the package
    manifest whole, and everything in it beyond the version is somebody
    else's concern.
    """

    version: str = Field(description="The release the registry currently serves")


class ReleaseRecord(BaseModel, frozen=True):
    """One package's last successfully resolved release, and when."""

    name: str = Field(description="The registry name the release was resolved for")
    version: str = Field(description="What the registry answered")
    resolved_at: datetime = Field(
        description="When it answered, so a fallback can date itself"
    )


class ReleaseLedger(BaseModel, frozen=True):
    """Every release this machine has resolved, read when the registry will not answer.

    A list of records rather than a name-keyed mapping, so each entry is a
    model the file format validates whole. It never expires: an old record is
    merely old, still names a version the build can install, and the notice
    that reaches for one dates it -- unlike crash litter, which is wrong the
    moment it is read.
    """

    records: list[ReleaseRecord] = Field(
        default=[], description="One record per package, latest resolution only"
    )

    def held(self, name: str) -> ReleaseRecord | None:
        """The record for this package, if this machine ever resolved one."""
        return next((record for record in self.records if record.name == name), None)

    def noted(self, name: str, version: str, when: datetime) -> "ReleaseLedger":
        """This ledger with the package's record replaced by a fresh one."""
        kept = [record for record in self.records if record.name != name]
        fresh = ReleaseRecord(name=name, version=version, resolved_at=when)
        return ReleaseLedger(records=[*kept, fresh])


class ResolvedReleases(BaseModel, frozen=True):
    """What resolution hands the launch: the image to render, and what to say."""

    image: Image = Field(
        description="A launch-local copy with every resolvable CLI pinned"
    )
    said: list[Notice] = Field(
        description="The banner's lines about what resolution did instead, if anything"
    )


def ledger_path(cache: Path | None = None) -> Path:
    """Where the ledger lives: machine-wide, because a release is a machine-wide fact.

    Under the same cache root the contained environments use, so one
    resolution serves every worktree of every repository on this machine.
    """
    return cache or UserDirectories().cache() / "releases.json"


def loaded_ledger(path: Path) -> ReleaseLedger:
    """The ledger as the file holds it, or empty where there is none to read.

    An unreadable or malformed file answers empty rather than raising: the
    ledger is a fallback, and a launch is not the place to fail over one.
    """
    try:
        return ReleaseLedger.model_validate_json(path.read_bytes())
    except (ValueError, OSError):
        return ReleaseLedger()


def current_release(
    url: str, client: httpx.Client | None = None, patience: float = 10
) -> str:
    """Ask a registry for a package's current release, one bounded attempt.

    No retry wrapper, deliberately: backoff would stall every launch on a
    registry that is down, and the ledger is the retry story -- the caller
    falls back to the last answer this machine got. ``client`` exists so a
    test hands in a mock transport instead of the network.
    """
    answered = (
        client.get(url) if client is not None else httpx.get(url, timeout=patience)
    )
    answered.raise_for_status()
    return CurrentRelease.model_validate(answered.json()).version


def resolved_agent_clis(
    image: Image,
    cache: Path | None = None,
    ask: Callable[[str], str] = current_release,
    say: Callable[[Notice], None] = Notice.say,
) -> ResolvedReleases:
    """The image with every unpinned agent CLI pinned to its current release.

    A declared version is a decision resolution does not touch, so an image
    whose CLIs are all pinned comes back untouched with nothing said. The
    returned notices are the launch banner's -- warnings when a registry did
    not answer and what was done instead -- while the one line naming the
    network wait is said immediately through ``say``, before the wait, which
    is what separates a slow registry from a stopped launch.
    """
    held = ledger_path(cache)
    ledger = loaded_ledger(held)
    stamped = datetime.now(UTC)
    notices: list[Notice] = []
    resolved: list[Package] = []
    waited = False
    for package in image.agent_clis:
        if package.version:
            resolved.append(package)
            continue
        registry = next(
            (
                entry
                for entry in image.registries
                if entry.manager == package.manager and entry.release_url
            ),
            None,
        )
        if registry is None:
            resolved.append(package)
            notices.append(
                Notice(
                    text=(
                        f"No registry declares where {package.name}'s current "
                        "release is answered, so the build will install "
                        "whatever its manager serves it."
                    ),
                    urgency="warning",
                )
            )
            continue
        if not waited:
            say(
                Notice(
                    text="Asking the registry for current agent CLI releases…",
                    urgency="progress",
                )
            )
            waited = True
        try:
            version = ask(registry.release_url.format(name=package.name))
        except (httpx.HTTPError, pydantic.ValidationError):
            remembered = ledger.held(package.name)
            if remembered is None:
                resolved.append(package)
                notices.append(
                    Notice(
                        text=(
                            f"The registry did not answer for {package.name} "
                            "and this machine has never resolved it, so the "
                            "build will install whatever the registry serves "
                            "it -- or fail at that layer, by name, if the "
                            "registry stays unreachable."
                        ),
                        urgency="warning",
                    )
                )
                continue
            resolved.append(package.model_copy(update={"version": remembered.version}))
            notices.append(
                Notice(
                    text=(
                        f"The registry did not answer for {package.name}, so "
                        "this launch pins the last release this machine "
                        "resolved."
                    ),
                    urgency="warning",
                )
            )
            notices.append(
                Notice(
                    text=(
                        f"{remembered.version}, resolved "
                        f"{remembered.resolved_at.date().isoformat()}."
                    ),
                    urgency="detail",
                    indent=1,
                )
            )
            continue
        resolved.append(package.model_copy(update={"version": version}))
        ledger = ledger.noted(package.name, version, stamped)
    if any(record.resolved_at == stamped for record in ledger.records):
        try:
            held.parent.mkdir(parents=True, exist_ok=True)
            held.write_text(ledger.model_dump_json(indent=2), encoding="utf-8")
        except OSError:
            notices.append(
                Notice(
                    text=(
                        f"Could not record the resolved releases at {held}, "
                        "so an offline launch will not have them to fall "
                        "back on."
                    ),
                    urgency="detail",
                )
            )
    return ResolvedReleases(
        image=image.model_copy(update={"agent_clis": resolved}), said=notices
    )
