"""The one place a run's writer and its readers meet: a directory.

Neither end holds the other. A run writes here and never learns who is
watching; a monitor reads here and never touches what it is watching, so
following a job cannot perturb it and several people may follow the same one.
That is why every path is spelled once, in this class, rather than in the
runtime and again in the reader — the two would drift, and the failure would
be a monitor that quietly reports nothing rather than one that errors.

Every write goes through :func:`lup.channels.models.publish_atomic`, because
a reader holds no lock: it either sees a complete record or none.
"""

import hashlib
import logging
from pathlib import Path

from pydantic import BaseModel, ValidationError

from lup.channels.models import publish_atomic, utc_now
from lup.runs.models import (
    SINGLE_ITEM,
    RunManifest,
    RunSummary,
    UnitAttempt,
    UnitProgress,
    UnitResult,
)

logger = logging.getLogger(__name__)


CLAIM_LEASE_SECONDS = 90.0
"""How long a claim stands without being renewed before nobody holds it.

Generous against the renewal interval rather than tight against it, because
the two failures are not symmetric: renewing late costs nothing, and calling a
live unit dead frees a claim somebody is working under. A machine that
suspended, a loaded host, and a runner between renewals all look the same from
here, so the window has to cover the worst of them.
"""


# lup: ignore[constant-declaration] — an identity this layout defines, named
# like the manifest and the summary: a writer in another language and a reader
# in this one meet only by spelling it alike, so a caller free to replace it
# would be a caller free to publish where nothing looks
PROGRESS_RECORD = "progress.json"
"""What a unit's own progress record is called inside its workspace."""


# lup: ignore[constant-declaration] — the runtime's own binding, the same
# string at both ends of the doorway, and the only thing a unit in another
# language is given to find its workspace by
WORKSPACE_ENV = "LUP_RUN_WORKSPACE"
"""The variable a shell unit finds its own workspace under.

The runtime binds it above every shell command and ``run report`` reads it
back to know where to write.
"""

# lup: ignore[constant-declaration] — the layout's name for a shell unit's
# output, written by the runtime and read by the monitor
STDOUT_RECORD = "stdout.txt"
"""What a shell unit's output is called inside its workspace."""

# lup: ignore[constant-declaration] — the layout's name for a shell unit's
# errors, beside the output it is read with
STDERR_RECORD = "stderr.txt"
"""What a shell unit's errors are called inside its workspace."""


# lup: ignore[constant-declaration] — POSIX NAME_MAX, the filename limit of the
# filesystems a run directory is written to; fixed rather than asked of a disk,
# because a writer and a reader on two machines must file one unit alike
FILENAME_BYTES = 255
"""The longest filename a run directory may hold, in bytes."""


def filed_name(item: str) -> str:
    """The one path component a unit is filed under, derived from its item.

    An item that already is one keeps its own text — every item a run has
    filed so far, so no existing directory reads differently. Any other is
    empty, ``.`` or ``..``, holds a ``/`` or a NUL, or is too long for the
    longest file the layout derives from it: a result's atomic temporary,
    ``.<name>.json.tmp``. Such an item is filed under a readable prefix of
    its text and a digest of the whole of it, so two items never share a
    file and none can reach outside its step.

    The name is only where a unit sits. The item itself is never cut: the
    manifest, the claim and the result each carry it whole, and every reader
    names a unit by those rather than by its file.
    """
    overhead = len(".") + len(".json") + len(".tmp")
    spelled = item.encode("utf-8")
    if (
        item not in ("", ".", "..")
        and "/" not in item
        and "\0" not in item
        and len(spelled) + overhead <= FILENAME_BYTES
    ):
        return item
    fingerprint = hashlib.blake2b(spelled, digest_size=16).hexdigest()
    room = FILENAME_BYTES - overhead - len("~") - len(fingerprint)
    readable = "".join(
        char if char.isascii() and (char.isalnum() or char in "-_") else "-"
        for char in item
    )
    return f"{readable[:room]}~{fingerprint}"


def progress_in(workspace: Path) -> Path:
    """Where a unit's progress record sits, given only the workspace it owns.

    A shell unit holds ``$LUP_RUN_WORKSPACE`` and nothing else — not the run
    root, not its own step and item — so the doorway it reports through can
    only be handed that. Both spellings of the path end here.
    """
    return workspace / PROGRESS_RECORD


def stdout_in(workspace: Path) -> Path:
    """Where a shell unit's output goes, which is also where a reader finds it.

    The runtime writes it and the monitor falls back to its last line for a
    unit that reports nothing, so the name is settled here rather than at
    each end.
    """
    return workspace / STDOUT_RECORD


def stderr_in(workspace: Path) -> Path:
    """Where a shell unit's errors go."""
    return workspace / STDERR_RECORD


class RunningUnit(BaseModel, frozen=True):
    """A claimed unit, how it is going, and whether anybody is still holding it."""

    attempt: UnitAttempt
    age_seconds: float
    """How long since the unit was first claimed — how long it is taking."""

    since_renewed_seconds: float
    """How long since its holder last said it was still working it."""

    lease_seconds: float = CLAIM_LEASE_SECONDS

    progress: UnitProgress | None = None
    """What the unit last said about its own inside, when it says anything.

    Filled by whoever takes the reading rather than by the claim, because a
    unit that reports nothing is the ordinary case and a claim is complete
    without it.
    """

    last_line: str = ""
    """The last line the unit printed, for one that reports nothing.

    A shell unit writing to stdout has already said what it is doing; the
    fallback carries that rather than a placeholder, so the only unit showing
    nothing is one that neither reported nor printed.
    """

    @property
    def slug(self) -> str:
        """How this unit is named to a reader."""
        return self.attempt.slug

    @property
    def stale(self) -> bool:
        """Whether the lease has lapsed, so no runner is holding this unit.

        The one question age alone cannot answer. A unit running for two hours
        and a unit whose runner was killed two hours ago have the same age; only
        the renewal separates them.
        """
        return self.since_renewed_seconds > self.lease_seconds


class DirectoryReading(BaseModel, frozen=True):
    """Every unit that has landed, and every file that could not be read.

    Unreadable files are carried rather than counted into a status, because
    the two mean different things to whoever is watching: a failed unit is the
    run working, and a file that will not parse is the run, the disk, or this
    reader being wrong. Naming the paths is what lets somebody go look.
    """

    results: list[UnitResult] = []
    unreadable: list[Path] = []


class RunDirectory(BaseModel, frozen=True):
    """One run's evidence on disk, addressed the same way by both ends."""

    root: Path

    @property
    def manifest_path(self) -> Path:
        """Where the scheduled units are declared."""
        return self.root / "manifest.json"

    @property
    def units_root(self) -> Path:
        """Where one result per landed unit goes."""
        return self.root / "units"

    @property
    def attempts_root(self) -> Path:
        """Where one claim per running unit goes."""
        return self.root / "attempts"

    @property
    def log_path(self) -> Path:
        """Where the run's own heartbeat goes, one line per thing that happened."""
        return self.root / "run.log"

    def unit_path(self, step: str, item: str = SINGLE_ITEM) -> Path:
        """Where one unit's result lives."""
        return self.units_root / step / f"{filed_name(item)}.json"

    def attempt_path(self, step: str, item: str = SINGLE_ITEM) -> Path:
        """Where one unit's claim lives while it runs."""
        return self.attempts_root / step / f"{filed_name(item)}.json"

    def workspace(self, step: str, item: str = SINGLE_ITEM) -> Path:
        """Where one unit puts whatever it produces besides its result.

        Spelled here rather than by whoever wants it, because a later step
        reading what an earlier one wrote is the ordinary case: two ends
        computing the same path by hand is how they stop agreeing.
        """
        return self.root / "artifacts" / step / filed_name(item)

    def progress_path(self, step: str, item: str = SINGLE_ITEM) -> Path:
        """Where one unit publishes how far into its own work it has got.

        Inside the workspace rather than beside the claim, because the claim
        is dropped the instant the unit lands and this reading is worth most
        exactly then: a failed unit's last sample sits beside its traceback.
        """
        return progress_in(self.workspace(step, item))

    def write_progress(self, step: str, item: str, progress: UnitProgress) -> None:
        """Publish one unit's reading of itself, replacing the one before it."""
        publish_atomic(self.progress_path(step, item), progress)

    def read_progress_record(
        self, step: str, item: str = SINGLE_ITEM
    ) -> UnitProgress | None:
        """What one unit last said about itself, or None when it has said nothing.

        A record half-written cannot be seen — every write here is an atomic
        rename — so one that will not parse is the disk or this reader being
        wrong, and a reading with none is simply a unit that does not report.
        """
        path = self.progress_path(step, item)
        if not path.is_file():
            return None
        try:
            return UnitProgress.model_validate_json(path.read_text(encoding="utf-8"))
        except (ValidationError, OSError) as error:
            logger.warning("unreadable progress record at %s: %s", path, error)
            return None

    def write_manifest(self, manifest: RunManifest) -> None:
        """Record what this run scheduled, replacing any earlier declaration."""
        publish_atomic(self.manifest_path, manifest)

    def read_manifest(self) -> RunManifest | None:
        """What this run scheduled, or None when nothing has declared it yet."""
        if not self.manifest_path.is_file():
            return None
        try:
            return RunManifest.model_validate_json(
                self.manifest_path.read_text(encoding="utf-8")
            )
        except (ValidationError, OSError) as error:
            logger.warning("unreadable manifest at %s: %s", self.manifest_path, error)
            return None

    def write_result(self, result: UnitResult) -> None:
        """Land one unit and drop its claim, in that order.

        The claim goes second so no instant exists in which a reader can see
        neither: a unit is running until the moment it has landed.
        """
        publish_atomic(self.unit_path(result.step, result.item), result)
        self.release(result.step, result.item)

    def read_result(self, step: str, item: str = SINGLE_ITEM) -> UnitResult | None:
        """One landed unit, or None when it has not landed or will not parse."""
        path = self.unit_path(step, item)
        if not path.is_file():
            return None
        try:
            return UnitResult.model_validate_json(path.read_text(encoding="utf-8"))
        except (ValidationError, OSError) as error:
            logger.warning("unreadable unit result at %s: %s", path, error)
            return None

    def read(self) -> DirectoryReading:
        """Every unit that has landed, in a stable order, with the failures to read."""
        if not self.units_root.is_dir():
            return DirectoryReading()
        readings = [
            (path, self.parse_result(path))
            for path in sorted(self.units_root.glob("*/*.json"))
        ]
        return DirectoryReading(
            results=[result for _, result in readings if result is not None],
            unreadable=[path for path, result in readings if result is None],
        )

    def read_step(self, step: str) -> list[UnitResult]:
        """Every unit one step has landed, read from the files themselves.

        The item is taken from each result rather than from its filename,
        because a unit is filed under :func:`filed_name` of its item, which is
        the item only when that is already a safe path component.
        """
        return [
            result
            for result in (
                self.parse_result(path)
                for path in sorted((self.units_root / step).glob("*.json"))
            )
            if result is not None
        ]

    def parse_result(self, path: Path) -> UnitResult | None:
        """One result file, or None when it will not parse."""
        try:
            return UnitResult.model_validate_json(path.read_text(encoding="utf-8"))
        except (ValidationError, OSError) as error:
            logger.warning("unreadable unit result at %s: %s", path, error)
            return None

    def claim(self, attempt: UnitAttempt) -> None:
        """Record that a unit has started."""
        publish_atomic(self.attempt_path(attempt.step, attempt.item), attempt)

    def renew(self, step: str, item: str = SINGLE_ITEM) -> None:
        """Say this unit is still being worked, without disturbing when it began.

        Re-stamping rather than re-claiming, so ``started_at`` goes on
        answering how long the unit is taking while ``renewed_at`` answers
        whether anybody is still on it. A claim that has since been released —
        the unit landed as this was called — is not resurrected: renewing what
        is gone would put a claim back on a finished unit.
        """
        held = self.parse_attempt(self.attempt_path(step, item))
        if held is None:
            return
        publish_atomic(
            self.attempt_path(step, item),
            held.model_copy(update={"renewed_at": utc_now()}),
        )

    def release(self, step: str, item: str = SINGLE_ITEM) -> None:
        """Drop a unit's claim, whether it landed or its runner gave up."""
        self.attempt_path(step, item).unlink(missing_ok=True)

    def running(self, lease_seconds: float = CLAIM_LEASE_SECONDS) -> list[RunningUnit]:
        """The units claimed and not landed, longest-running first.

        Every claim on disk, stale ones included, because a reader wants to see
        a unit nobody is holding rather than have it quietly omitted — a run
        whose process was killed reads as several stale claims and no live ones,
        which is the diagnosis.
        """
        if not self.attempts_root.is_dir():
            return []
        now = utc_now()
        claimed = [
            self.parse_attempt(path)
            for path in sorted(self.attempts_root.glob("*/*.json"))
        ]
        return sorted(
            (
                RunningUnit(
                    attempt=attempt,
                    age_seconds=max(0.0, (now - attempt.started_at).total_seconds()),
                    since_renewed_seconds=max(
                        0.0, (now - attempt.renewed_at).total_seconds()
                    ),
                    lease_seconds=lease_seconds,
                )
                for attempt in claimed
                if attempt is not None
            ),
            key=lambda unit: unit.age_seconds,
            reverse=True,
        )

    def parse_attempt(self, path: Path) -> UnitAttempt | None:
        """One claim file, or None when it will not parse."""
        try:
            return UnitAttempt.model_validate_json(path.read_text(encoding="utf-8"))
        except (ValidationError, OSError) as error:
            logger.warning("unreadable attempt at %s: %s", path, error)
            return None

    def clear_claims(self, lease_seconds: float = CLAIM_LEASE_SECONDS) -> list[str]:
        """Drop the claims nobody is holding, and say which those were.

        A resumed run calls this before starting: a claim left by a process that
        was killed would otherwise report a unit as running for as long as the
        directory survives, and a watcher has no way to see through it.

        Only the lapsed ones. Dropping every claim is right exactly once — when
        this is the only runner and the previous one is gone — and wrong the
        moment two share a directory, where it frees units a live sibling is
        working and lets both run them at once. The lease is what separates the
        cases, so nothing has to assume which it is in.
        """
        if not self.attempts_root.is_dir():
            return []
        lapsed = [unit for unit in self.running(lease_seconds) if unit.stale]
        for unit in lapsed:
            self.release(unit.attempt.step, unit.attempt.item)
        return [unit.slug for unit in lapsed]

    def append_heartbeat(self, line: str) -> None:
        """Add one line to the run's log, which is what a follower re-reads.

        The runtime writes this itself rather than relying on the launch line
        redirecting a terminal: a run is monitorable because of what it does,
        not because of how somebody remembered to start it.
        """
        self.root.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(f"{line}\n")

    @property
    def summary_path(self) -> Path:
        """Where the runtime records that the run is over, however it ended."""
        return self.root / "summary.json"

    def write_summary(self, summary: RunSummary) -> None:
        """Record that this run has ended."""
        publish_atomic(self.summary_path, summary)

    def read_summary(self) -> RunSummary | None:
        """How the run ended, or None while it is still going or was killed."""
        if not self.summary_path.is_file():
            return None
        try:
            return RunSummary.model_validate_json(
                self.summary_path.read_text(encoding="utf-8")
            )
        except (ValidationError, OSError) as error:
            logger.warning("unreadable summary at %s: %s", self.summary_path, error)
            return None
