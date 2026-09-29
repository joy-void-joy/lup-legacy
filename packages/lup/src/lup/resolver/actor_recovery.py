"""Explicit retirement of a stopped run's incompatible actor conversation."""

from lup.channels.models import publish_atomic
from lup.coordination.cohort import SESSION_DIR
from lup.coordination.sessions import ActorRecord
from lup.resolver.record import ActorBindingRetiredEvent, Journal
from lup.resolver.state import ResolverStateRepository, StateTransitionError


def retire_actor_binding(
    repository: ResolverStateRepository, address: str, reason: str
) -> ActorBindingRetiredEvent:
    """Preserve the old binding in the journal before selecting a fresh session.

    The run lease excludes a live driver, including its outstanding turns.
    Questions, answers, joins, worktrees and delivery positions remain durable;
    only this actor's provider conversation and schema bindings are retired.
    """
    if not reason.strip():
        raise ValueError("an actor retirement requires a reason")
    if not repository.exists():
        raise StateTransitionError(
            f"resolver run {repository.root.name!r} does not exist"
        )
    with repository.exclusive():
        matches = [
            (path, record)
            for path in sorted((repository.root / SESSION_DIR).glob("*.json"))
            for record in [ActorRecord.model_validate_json(path.read_text("utf-8"))]
            if address in record.actor.addresses()
        ]
        if len(matches) != 1:
            raise StateTransitionError(
                f"actor {address!r} matches {len(matches)} recorded bindings; "
                "use the exact kind:id#round shown by resolve actors"
            )
        path, previous = matches[0]
        if previous.session is None and not previous.schema_digests:
            raise StateTransitionError(f"actor {address!r} has no binding to retire")
        event = ActorBindingRetiredEvent(previous=previous, reason=reason)
        # This durable authorization includes the full prior binding. A crash
        # before publication leaves it recoverable and the same reset safe to retry.
        Journal(repository.root).record(event)
        publish_atomic(path, ActorRecord(actor=previous.actor))
        return event
