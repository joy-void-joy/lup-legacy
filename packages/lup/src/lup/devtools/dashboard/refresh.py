"""The dashboard's own code, and moving onto its checkout's newer code in place.

The dashboard is one long-lived process on the host, started by whichever
launch first held it, from the lup source that launch's environment imports —
for an editable install, a checkout, which moves every time something lands
on it. A process keeps the code it imported: once a review's fingerprint is
spelled differently on disk, sessions park reviews under the new spelling
while the dashboard recomputes the old one, and refuses every answer as
though the record had been altered.

So the dashboard watches the files it imported. When its checkout moves past
them, it waits for the files to settle, starts the new code once in a fresh
interpreter to see that it starts, and then re-executes itself in place once
no write is in flight: the same process, port and capability, the same
herald state, and every open tab reconnecting on its own. Until then it says
so — on the page, in the status line, and to a write it refuses — rather
than refusing an answer for a reason that is not the one.
"""

import hashlib
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import sh
from pydantic import BaseModel
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from lup.devtools.dashboard.pulse import RunningCode


class SourceFile(BaseModel, frozen=True):
    """One file of the running code, as it stood when it was imported."""

    size: int
    modified: int
    digest: str

    @classmethod
    def read(cls, path: Path) -> "SourceFile | None":
        """The file as it stands, or nothing where none does."""
        try:
            status = path.stat()
            content = path.read_bytes()
        except OSError:
            return None
        return cls(
            size=status.st_size,
            modified=status.st_mtime_ns,
            digest=hashlib.sha256(content).hexdigest(),
        )


class ImportedSource:
    """Every file of one package this process imported, as it stood when imported.

    Taken again on every look, so a module imported late — on the first
    answer, say — joins with what it held then. ``extra`` are files of the
    package the process reads rather than imports, such as the page it serves.
    A file is read again only where its size or time moved, so a look costs a
    stat per file.
    """

    def __init__(self, package: Path, extra: tuple[Path, ...] = ()) -> None:
        self.package = package.resolve()
        self.extra = extra
        self.files: dict[Path, SourceFile] = {}

    def taken(self) -> None:
        """Record every file of the package imported since the last look."""
        loaded = [
            Path(spelled)
            for module in list(sys.modules.values())
            if isinstance(spelled := getattr(module, "__file__", None), str)
        ]
        for path in [*loaded, *self.extra]:
            resolved = path.resolve()
            if resolved in self.files or not resolved.is_relative_to(self.package):
                continue
            recorded = SourceFile.read(resolved)
            if recorded is not None:
                self.files[resolved] = recorded

    def digest(self) -> str:
        """The code as imported, as one digest."""
        whole = hashlib.sha256()
        for path, recorded in sorted(self.files.items()):
            whole.update(str(path).encode())
            whole.update(recorded.digest.encode())
        return whole.hexdigest()[:16]

    def moved(self) -> dict[Path, str]:
        """Every file whose content moved since it was imported, by the digest it holds now.

        A file gone is held as nothing. One whose time moved and content did
        not — a checkout rewriting it as it was — is recorded afresh, so it
        is not read again.
        """
        drift: dict[Path, str] = {}
        for path, recorded in list(self.files.items()):
            try:
                status = path.stat()
            except OSError:
                drift[path] = ""
                continue
            if (status.st_size, status.st_mtime_ns) == (
                recorded.size,
                recorded.modified,
            ):
                continue
            match SourceFile.read(path):
                case None:
                    drift[path] = ""
                case SourceFile() as current if current.digest == recorded.digest:
                    self.files[path] = current
                case SourceFile(digest=digest):
                    drift[path] = digest
        return drift


def probed(executable: str = sys.executable) -> str:
    """Why the code on disk would not start the dashboard, or nothing where it would.

    A fresh interpreter from this environment imports everything the service
    imports. A checkout caught mid-merge, or holding an error, answers with
    the last line it printed, and the dashboard stays on the code it runs.
    """
    try:
        sh.Command(executable)(
            "-m",
            "lup.devtools.dashboard.service",
            "--probe",
            _timeout=120,
            _err_to_out=True,
        )
    except sh.ErrorReturnCode as failed:
        said = failed.stdout.decode(errors="replace").strip().splitlines()
        return said[-1] if said else f"it exited {failed.exit_code}"
    except sh.TimeoutException:
        return "it did not finish importing within two minutes"
    return ""


class Refresh:
    """Whether the dashboard runs its checkout's code, and when it restarts onto it.

    Each look takes what was imported since and compares every file with the
    disk. A change is waited on until a look finds the files as the last one
    did, since a checkout moving writes many files over a moment; the code on
    disk is then started once, and only where it starts is the restart due.
    A settled change whose code would not start is not tried again until the
    files move once more. The operator's ask is due the same way, with
    nothing moved.
    """

    def __init__(
        self,
        source: ImportedSource,
        probe: Callable[[], str] = probed,
        since: datetime | None = None,
    ) -> None:
        self.source = source
        self.probe = probe
        self.since = since or datetime.now(UTC)
        self.drift: dict[Path, str] = {}
        self.tried: dict[Path, str] | None = None
        self.failing = ""
        self.asked = False
        self.due = False

    def look(self) -> None:
        """Compare the running code with its checkout once, and settle whether to restart."""
        self.source.taken()
        drift = self.source.moved()
        settled = drift == self.drift
        self.drift = drift
        if not drift:
            self.failing = ""
        if self.due or not settled or not (drift or self.asked):
            return
        if drift == self.tried and not self.asked:
            return
        self.tried = drift
        self.asked = False
        self.failing = self.probe()
        self.due = not self.failing

    def code(self) -> RunningCode:
        """Which code runs, since when, and whether its checkout moved past it."""
        return RunningCode(
            source=self.source.digest(),
            root=str(self.source.package),
            since=self.since,
            older=bool(self.drift),
            failing=self.failing,
        )

    def refusal(self) -> str:
        """Why a write is refused now: only while the dashboard is about to restart."""
        if self.drift and not self.failing:
            return (
                "The dashboard runs older code than its checkout and is restarting "
                "onto it; answer again once the page reconnects."
            )
        if self.due:
            return "The dashboard is restarting; answer again once the page reconnects."
        return ""


class WriteGate:
    """Every write the dashboard serves, held in view, so a restart waits for none in flight.

    An ASGI wrapper round the whole application rather than a route's own
    count, since a write's reply is sent before the delivery it started in
    the background ends, and that delivery is part of the write. A write
    arriving while the dashboard is about to restart is refused with the
    reason, so it is not answered by code about to be replaced.
    """

    def __init__(self, app: ASGIApp, refusal: Callable[[], str]) -> None:
        self.app = app
        self.refusal = refusal
        self.writing = 0

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] != "POST":
            await self.app(scope, receive, send)
            return
        refused = self.refusal()
        if refused:
            await JSONResponse({"detail": refused}, status_code=503)(
                scope, receive, send
            )
            return
        self.writing += 1
        try:
            await self.app(scope, receive, send)
        finally:
            self.writing -= 1
