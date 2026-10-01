"""A durable journal of executed cells, and the divergence check on replay.

The mechanism is the same wherever it is used: record every executed cell in
order with whether it succeeded, and reconstruct state by re-running the
journal rather than by serializing objects. What differs between users is the
*contract* they attach to it.

An environment that claims determinism — a sealed, networkless kernel — is
saying a replay must reproduce the recorded outcomes, so a divergence there is
a defect in a claim. An environment that claims nothing — a sandbox with
network access and package installs — still gets a replayable record and a
divergence report, and there the divergence is not a defect, it *is* the
finding: it says the result depended on something outside the journal.

Both are replayable; only the first is certifiable. Keeping one mechanism with
two contracts is what makes that distinction legible, instead of leaving the
environment that cannot promise determinism with no durable record at all.
"""

import hashlib
import logging
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, Field, TypeAdapter, computed_field

from lup.channels.models import publish_atomic

logger = logging.getLogger(__name__)


class UnreadableJournalError(RuntimeError):
    """Raised when a journal file exists but does not parse.

    Loud rather than quiet, because the alternative — starting a fresh
    journal over the unreadable one — would let a replay of nothing report
    itself as a replay with no divergence. A clean bill of health from a
    record that could not be read is the one outcome this module exists to
    make impossible.
    """


def timestamp_now() -> str:
    """The wall clock a journal records itself as created at."""
    return datetime.now().astimezone().isoformat()


class JournalCell(BaseModel, frozen=True):
    """One executed unit, recorded so the sequence can be re-run."""

    kind: str = Field(
        default="code",
        description="What sort of cell this is, for environments with several",
    )
    source: str = Field(description="Exactly what was executed")
    ok: bool = Field(description="Whether it succeeded when it was first run")


class CellOutcome(BaseModel, frozen=True):
    """How one cell went on being re-run.

    The outcome and the note that explains it are one value rather than two
    parallel lists, so a replay that reports on ten cells cannot hand back
    nine details and mis-attribute every one of them after the gap.
    """

    ok: bool = Field(description="Whether it succeeded this time")
    detail: str = Field(
        default="", description="What it said, for a divergence that needs explaining"
    )


class ReplayDivergence(BaseModel, frozen=True):
    """One cell whose replayed outcome differed from what was recorded."""

    index: int
    recorded_ok: bool
    replayed_ok: bool
    detail: str = ""


class ReplayReport(BaseModel, frozen=True):
    """The result of re-running one journal."""

    journal_id: str
    cells_replayed: int
    divergences: list[ReplayDivergence] = []
    determinism_claimed: bool = False

    @property
    def reproduced(self) -> bool:
        """Whether every cell replayed to its recorded outcome."""
        return not self.divergences

    @computed_field(
        description="What this replay established, in the terms of its own contract"
    )
    @property
    def finding(self) -> str:
        """What this replay established, in the terms of its own contract.

        Computed rather than derived by a reader, so it is carried by every
        serialization of this report — an agent holding the tool result reads
        the same sentence a library caller does, instead of being left to
        re-infer the contract from a list of divergences.
        """
        if not self.divergences:
            return f"replayed {self.cells_replayed} cells with no divergence"
        indices = ", ".join(str(item.index) for item in self.divergences)
        if self.determinism_claimed:
            return (
                f"determinism was claimed but cells [{indices}] replayed "
                "differently — the recorded outcome is not reproducible and "
                "nothing built on it should be treated as established"
            )
        return (
            f"cells [{indices}] replayed differently, which is the finding: "
            "these results depended on network or install state rather than "
            "on the journal alone"
        )


class ReplayJournal(BaseModel, frozen=True):
    """An ordered execution record plus its lineage and its contract."""

    id: str
    parent: str | None = Field(
        default=None, description="Journal this one was forked from"
    )
    created_at: str = Field(default_factory=timestamp_now)
    cells: list[JournalCell] = []
    determinism_claimed: bool = Field(
        default=False,
        description=(
            "Whether this environment asserts a replay must reproduce the "
            "recorded outcomes. False means divergence is information, not a "
            "defect — and that this journal can never back a certified claim"
        ),
    )

    def digest(self) -> str:
        """SHA-256 identity of the ordered cells alone.

        The cells and not the envelope, so a journal forked from another, or
        re-created with a fresh id, still digests to the same value when it
        records the same execution.
        """
        payload = TypeAdapter(list[JournalCell]).dump_json(self.cells)
        return hashlib.sha256(payload).hexdigest()

    def recording(self, cell: JournalCell) -> "ReplayJournal":
        """This journal with one more executed cell appended.

        A new journal rather than a mutated one, so a caller holding the
        value it read cannot find the record changing under it.
        """
        return self.model_copy(update={"cells": [*self.cells, cell]})

    def compare(self, outcomes: list[CellOutcome]) -> ReplayReport:
        """Compare a replay's per-cell outcomes against what was recorded.

        A short replay is compared as far as it went rather than refused: a
        run that stopped early still establishes something about the cells it
        reached, and reporting nothing would discard that.
        """
        return ReplayReport(
            journal_id=self.id,
            cells_replayed=len(outcomes),
            divergences=[
                ReplayDivergence(
                    index=index,
                    recorded_ok=cell.ok,
                    replayed_ok=outcome.ok,
                    detail=outcome.detail,
                )
                for index, (cell, outcome) in enumerate(
                    zip(self.cells, outcomes, strict=False)
                )
                if cell.ok != outcome.ok
            ],
            determinism_claimed=self.determinism_claimed,
        )


class JournalStore:
    """One journal kept on disk, replaced atomically as cells run.

    The contract is fixed here rather than at each append, because it
    belongs to the environment doing the executing and not to any one cell:
    a store built by a sealed kernel claims determinism for everything it
    records, and one built by a networked sandbox claims it for nothing.
    """

    def __init__(
        self, path: Path, journal_id: str, *, determinism_claimed: bool = False
    ) -> None:
        self.path = path
        self.journal_id = journal_id
        self.determinism_claimed = determinism_claimed

    def load(self) -> ReplayJournal:
        """Read the journal, or an empty one when nothing has run yet.

        A file that exists and does not parse raises instead: nothing has
        run is a different fact from the record is unreadable, and only the
        first of them means a replay has no divergence to report.
        """
        if not self.path.is_file():
            return ReplayJournal(
                id=self.journal_id, determinism_claimed=self.determinism_claimed
            )
        try:
            return ReplayJournal.model_validate_json(
                self.path.read_text(encoding="utf-8")
            )
        except ValueError as e:
            raise UnreadableJournalError(
                f"the execution journal at {self.path} does not parse, so what "
                "ran cannot be established from it; delete the file to start a "
                "fresh record, knowing the earlier cells are no longer accounted for"
            ) from e

    def record(self, cell: JournalCell) -> ReplayJournal:
        """Append one executed cell and replace the file whole."""
        journal = self.load().recording(cell)
        publish_atomic(self.path, journal)
        return journal
