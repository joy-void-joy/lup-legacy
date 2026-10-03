"""The config volumes a split superseded, and when each may go.

A split copies an old config volume's history into the per-runtime volumes
(:func:`~lup.launch.config_volume.split_config_volumes`) and keeps
the old one: a copy nobody has read yet is not a reason to destroy the
original. What is kept has to be removed by something, though, and the day
it was superseded cannot live on the volume itself — an engine's volume
labels are fixed when the volume is made. So lup records it where it keeps
its own state for the person, ``$XDG_STATE_HOME/lup`` (``~/.local/state/lup``
where that is unset), and any launch or `harness clean` removes a superseded
volume once the person's ``[cleanup] superseded_volumes_after_days`` have
passed.
"""

from datetime import date, datetime, timedelta
from pathlib import Path

from pydantic import BaseModel

from lup.channels.models import write_atomic
from lup.workspace.user_directories import UserDirectories


class SupersededVolume(BaseModel, frozen=True):
    """One volume a split replaced, when, and what holds its history now."""

    name: str
    superseded_at: datetime
    moved_into: list[str] = []

    def removal_date(self, kept_for: timedelta) -> date:
        """The day from which a launch removes it."""
        return (self.superseded_at + kept_for).date()

    def expired(self, kept_for: timedelta, now: datetime) -> bool:
        """Whether it has been kept as long as the person asked."""
        return now >= self.superseded_at + kept_for


class SupersededRecord(BaseModel, frozen=True):
    """Every volume a split superseded that has not been removed yet."""

    volumes: list[SupersededVolume] = []

    def names(self) -> list[str]:
        """The volumes recorded."""
        return [volume.name for volume in self.volumes]

    def with_superseded(
        self, names: list[str], moved_into: list[str], now: datetime
    ) -> "SupersededRecord":
        """The record with each named volume added, keeping the first date a volume got."""
        return SupersededRecord(
            volumes=[
                *self.volumes,
                *(
                    SupersededVolume(
                        name=name, superseded_at=now, moved_into=moved_into
                    )
                    for name in names
                    if name not in self.names()
                ),
            ]
        )

    def without(self, names: list[str]) -> "SupersededRecord":
        """The record with the named volumes forgotten."""
        return SupersededRecord(
            volumes=[volume for volume in self.volumes if volume.name not in names]
        )


class SupersededFile:
    """The record, where lup keeps it for the person."""

    def __init__(self, home: Path | None = None) -> None:
        self.home = home if home is not None else UserDirectories().state()

    def path(self) -> Path:
        """The file the record is read from and written to."""
        return self.home / "superseded-volumes.json"

    def load(self) -> SupersededRecord:
        """The record, empty where none has been written."""
        path = self.path()
        if not path.is_file():
            return SupersededRecord()
        return SupersededRecord.model_validate_json(path.read_text(encoding="utf-8"))

    def save(self, record: SupersededRecord) -> None:
        """Write the record so no reader catches it half-written."""
        write_atomic(self.path(), record.model_dump_json(indent=2).encode("utf-8"))
