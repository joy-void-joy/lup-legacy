"""What the dashboard says of itself to every session, holding no capability.

The dashboard runs on the host as the operator's, and what it knows — how
many reviews wait on the operator, how many sessions hold it, which
repositories it serves — is read by sessions that must never hold the
capability that opens it: a session's status line, and `dashboard status`
run inside one. So the service publishes that much, and nothing else, as one
small file in a directory of its own, which every launch lends its session
read-only at the path the host has it, beside the variable naming it.

The file is rewritten while the service runs and removed when it stops, so a
pulse whose last beat is old says the service stopped without removing it.

A leaf, imported by the status line a session runs at every render, so it
reaches for nothing heavier than pydantic.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path

from pydantic import BaseModel, ValidationError

# lup: ignore[constant-declaration] — the launch that exports it and the session
# that reads it are different processes, so the name is an identity
DASHBOARD_PULSE_ENV = "LUP_DASHBOARD_PULSE"
"""The variable naming the pulse file, which a launch holding the dashboard exports."""


class DashboardPulse(BaseModel, frozen=True):
    """The dashboard as a session may read it: counts and its address, never its capability."""

    url: str
    """Where it serves, credential-free."""

    pid: int
    pending: int = 0
    """Reviews waiting on the operator, across every repository it serves."""

    sessions: int = 0
    """Running launches holding it."""

    repositories: list[str] = []
    tabs: int = 0
    """Pages following its stream now."""

    beat: datetime
    """When the service last wrote it."""

    def current(self, now: datetime, within: timedelta = timedelta(seconds=30)) -> bool:
        """Whether the service wrote it recently enough to still be running."""
        return now - self.beat <= within

    def line(self) -> str:
        """What waits and where, in one line; the address alone where nothing waits."""
        if not self.pending:
            return self.url
        noun = "review" if self.pending == 1 else "reviews"
        return f"{self.pending} {noun} pending · {self.url}"


class PulseFile(BaseModel, frozen=True):
    """Where one dashboard's pulse is kept: a directory of its own, lent read-only."""

    path: Path

    @classmethod
    def of(cls, state: Path) -> "PulseFile":
        """The pulse of the dashboard whose private state is ``state``."""
        return cls(path=state / "pulse" / "dashboard.json")

    def read(self) -> DashboardPulse | None:
        """The pulse as last written, or nothing where none is, or it does not parse."""
        try:
            return DashboardPulse.model_validate_json(self.path.read_bytes())
        except (OSError, ValidationError):
            return None


def status_line(path: Path, now: datetime | None = None) -> str:
    """The line a session's status line shows, or nothing where no dashboard answers."""
    pulse = PulseFile(path=path).read()
    if pulse is None or not pulse.current(now or datetime.now(UTC)):
        return ""
    return pulse.line()
