"""The wired cost command reads a journal without needing run state or a lock."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from lup.channels.stream import Stream
from lup.coordination.refs import ActorRef
from lup.devtools.resolve import cost
from lup.resolver.cost import CostReport
from lup.resolver.record import ENTRY_ADAPTER, JournalEntry, RunFailedEvent
from lup.sessions.events import (
    SessionId,
    TurnCompletedEvent,
    TurnId,
    TurnIdentifiers,
    TurnStartedEvent,
)
from lup_template.devtools.main import app


def test_cost_is_wired_and_reports_json_without_changing_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cost, "resolve_state_root", lambda: tmp_path)
    path = tmp_path / "observed" / "journal.jsonl"
    stream = Stream(path, ENTRY_ADAPTER)
    identifiers = TurnIdentifiers(session=SessionId(value="s"), turn=TurnId(value="t"))
    start = datetime(2026, 1, 1, tzinfo=UTC)
    for seq, event in enumerate(
        [
            TurnStartedEvent(identifiers=identifiers),
            TurnCompletedEvent(identifiers=identifiers),
        ]
    ):
        stream.append(
            JournalEntry(
                seq=seq,
                at=start + timedelta(seconds=seq * 15),
                actor=ActorRef(kind="worker", id="one"),
                event=event,
            )
        )
    before = path.read_bytes()

    result = CliRunner().invoke(
        app, ["resolve", "cost", "--run-id", "observed", "--json"]
    )

    assert result.exit_code == 0, result.output
    report = CostReport.model_validate_json(result.output)
    assert report.active_seconds == 15
    assert report.actors[0].completed == 1
    assert path.read_bytes() == before
    assert list(path.parent.iterdir()) == [path]
    displayed = CliRunner().invoke(app, ["resolve", "cost", "--run-id", "observed"])
    assert displayed.exit_code == 0, displayed.output
    assert "Wall 15s" in displayed.output
    assert "worker" in displayed.output


def test_cost_reports_an_unknown_run_without_creating_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cost, "resolve_state_root", lambda: tmp_path)

    result = CliRunner().invoke(app, ["resolve", "cost", "--run-id", "absent"])

    assert result.exit_code != 0
    assert "no resolver journal" in result.output
    assert not (tmp_path / "absent").exists()


def test_cost_exposes_uncertainty_and_full_bounds_in_json_and_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cost, "resolve_state_root", lambda: tmp_path)
    stream = Stream(tmp_path / "interrupted" / "journal.jsonl", ENTRY_ADAPTER)
    start = datetime(2026, 1, 1, tzinfo=UTC)
    ids = TurnIdentifiers(session=SessionId(value="s"), turn=TurnId(value="crashed"))
    for seq, event in enumerate(
        [
            TurnStartedEvent(identifiers=ids),
            RunFailedEvent(reason="Complete failure evidence from the journal"),
        ]
    ):
        stream.append(
            JournalEntry(
                seq=seq,
                at=start + timedelta(seconds=seq * 7200),
                actor=ActorRef(kind="worker", id="one"),
                event=event,
            )
        )
    arguments = ["resolve", "cost", "--run-id", "interrupted"]
    result = CliRunner().invoke(app, [*arguments, "--json"])
    assert result.exit_code == 0, result.output
    report = CostReport.model_validate_json(result.output)
    assert report.active_seconds == report.idle_seconds == report.peak_concurrency == 0
    assert report.uncertain_seconds == 7200
    assert report.unresolved_intervals[0].ended.event == RunFailedEvent(
        reason="Complete failure evidence from the journal"
    )
    displayed = CliRunner().invoke(app, arguments)
    assert displayed.exit_code == 0, displayed.output
    assert "uncertain 2h" in displayed.output
    assert "peak concurrent completed turns 0" in displayed.output
    assert "2026-01-01T00:00:00+00:00 → 2026-01-01T02:00:00+00:00" in displayed.output
    assert "Complete failure evidence from the journal" in displayed.output
