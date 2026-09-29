"""A schema migration retires one conversation without rewriting run evidence."""

from pathlib import Path
from unittest.mock import Mock

import pytest
from pydantic import BaseModel

from lup.channels.models import publish_atomic
from lup.coordination.cohort import SESSION_DIR
from lup.coordination.refs import ActorRef
from lup.coordination.sessions import ActorRecord, ActorSchemaChangedError, ActorSession
from lup.resolver.actor_recovery import retire_actor_binding
from lup.resolver.record import ActorBindingRetiredEvent, Journal
from lup.resolver.state import ResolverStateRepository, StateTransitionError
from lup.sessions.events import (
    SessionId,
)


class Report(BaseModel):
    accepted: bool


@pytest.fixture
def recorded(tmp_path: Path) -> tuple[ResolverStateRepository, ActorRecord]:
    repository = ResolverStateRepository(tmp_path, "run-1")
    repository.root.mkdir(parents=True)
    # The recovery does not interpret or rewrite the stopped run's state.
    (repository.root / "state.json").write_text('{"preserve":"run checkpoint"}\n')
    (repository.root / "questions.json").write_text('{"preserve":"answer"}\n')
    previous = ActorRecord(
        actor=ActorRef(kind="merger", id="integration"),
        session=SessionId(value="old-native-thread"),
        schema_digests={"Report": "incompatible-shape"},
    )
    publish_atomic(repository.root / SESSION_DIR / "merger-integration.json", previous)
    return repository, previous


def test_rebinding_preserves_evidence_and_requires_a_fresh_conversation(
    recorded: tuple[ResolverStateRepository, ActorRecord],
) -> None:
    repository, previous = recorded
    actor = ActorSession(previous.actor, Mock(), Mock(), previous)
    with pytest.raises(ActorSchemaChangedError, match="rebind-actor"):
        actor.check_schema(Report)
    untouched = {
        path: path.read_bytes() for path in repository.root.iterdir() if path.is_file()
    }
    event = retire_actor_binding(
        repository, previous.actor.label(), "Report schema moved"
    )
    rebound = ActorRecord.model_validate_json(
        (repository.root / SESSION_DIR / "merger-integration.json").read_text()
    )
    assert rebound.session is None
    assert rebound.schema_digests == {}
    assert event.previous == previous
    assert all(path.read_bytes() == content for path, content in untouched.items())
    entries = Journal(repository.root).read()
    assert len(entries) == 1
    assert isinstance(entries[0].event, ActorBindingRetiredEvent)
    assert entries[0].event.previous == previous
    ActorSession(rebound.actor, Mock(), Mock(), rebound).check_schema(Report)


def test_rebinding_refuses_a_live_run_without_changing_its_binding(
    recorded: tuple[ResolverStateRepository, ActorRecord],
) -> None:
    repository, previous = recorded
    path = repository.root / SESSION_DIR / "merger-integration.json"
    before = path.read_bytes()
    with (
        repository.exclusive(),
        pytest.raises(StateTransitionError, match="already active"),
    ):
        retire_actor_binding(repository, previous.actor.label(), "Report schema moved")
    assert path.read_bytes() == before
    assert not (repository.root / "journal.jsonl").exists()


def test_journal_failure_prevents_retirement(
    recorded: tuple[ResolverStateRepository, ActorRecord],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, previous = recorded
    path = repository.root / SESSION_DIR / "merger-integration.json"
    before = path.read_bytes()
    monkeypatch.setattr(Journal, "record", Mock(side_effect=OSError("disk full")))
    with pytest.raises(OSError, match="disk full"):
        retire_actor_binding(repository, previous.actor.label(), "Report schema moved")
    assert path.read_bytes() == before


def test_rebinding_selects_only_one_actor(
    recorded: tuple[ResolverStateRepository, ActorRecord],
) -> None:
    repository, previous = recorded
    sibling = previous.model_copy(
        update={"actor": ActorRef(kind="reviewer", id="integration")}
    )
    path = repository.root / SESSION_DIR / "reviewer-integration.json"
    publish_atomic(path, sibling)
    before = path.read_bytes()
    with pytest.raises(StateTransitionError, match="matches 2"):
        retire_actor_binding(repository, "integration", "Report schema moved")
    retire_actor_binding(repository, previous.actor.label(), "Report schema moved")
    assert path.read_bytes() == before
