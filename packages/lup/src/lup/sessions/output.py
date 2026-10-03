"""Portable submitted-output validation and turn-scoped stores."""

import hashlib
import json
from collections.abc import Awaitable, Callable
from pathlib import Path

from pydantic import BaseModel, ValidationError

from lup.channels.models import publish_atomic
from lup.sessions.capabilities import SubmittedOutputStore
from lup.sessions.errors import ValidationAttempt
from lup.sessions.events import SubmissionDecision, TurnToolBinding
from lup.types import JsonObject, JsonValue


class SubmissionResponse(BaseModel, frozen=True):
    """Actionable response returned by the portable submission tool."""

    accepted: bool
    message: str


class InMemorySubmittedOutputStore(SubmittedOutputStore):
    """A fresh in-process store for one logical turn."""

    def __init__(self) -> None:
        self.value: BaseModel | None = None
        self.attempts: list[ValidationAttempt] = []

    def write(
        self,
        value: BaseModel,  # lup: ignore[bare-basemodel] — generic output-store boundary
    ) -> None:
        self.value = value.model_copy(deep=True)

    def read[T: BaseModel](self, output_type: type[T]) -> T | None:
        if self.value is None:
            return None
        return output_type.model_validate(self.value.model_dump(mode="json"))


class FileSubmittedOutputStore(SubmittedOutputStore):
    """An atomically replaced cross-process turn output store."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.attempts_path = path.with_name(f"{path.name}.attempts.json")

    def write(
        self,
        value: BaseModel,  # lup: ignore[bare-basemodel] — generic output-store boundary
    ) -> None:
        publish_atomic(self.path, value)

    def read[T: BaseModel](self, output_type: type[T]) -> T | None:
        if not self.path.exists():
            return None
        return output_type.model_validate_json(self.path.read_text(encoding="utf-8"))


class AttemptDocument(BaseModel, frozen=True):
    attempts: list[ValidationAttempt] = []


def record_attempt(store: SubmittedOutputStore, message: str) -> None:
    """Record rejected validation through known portable store implementations."""
    attempt = ValidationAttempt(message=message)
    match store:
        case InMemorySubmittedOutputStore():
            store.attempts.append(attempt)
        case FileSubmittedOutputStore():
            history = submission_history(store)
            document = AttemptDocument(attempts=[*history, attempt])
            publish_atomic(store.attempts_path, document)
        case _:
            return


def submission_history(store: SubmittedOutputStore) -> list[ValidationAttempt]:
    """Read immutable copies of rejected attempts from portable stores."""
    match store:
        case InMemorySubmittedOutputStore():
            return [attempt.model_copy() for attempt in store.attempts]
        case FileSubmittedOutputStore() if store.attempts_path.exists():
            document = AttemptDocument.model_validate_json(
                store.attempts_path.read_text(encoding="utf-8")
            )
            return [attempt.model_copy() for attempt in document.attempts]
        case _:
            return []


async def submit_output[T: BaseModel](
    binding: TurnToolBinding[T], value: JsonValue
) -> SubmissionResponse:
    """Validate, reflect on, and persist one portable tool submission."""
    try:
        output = binding.output_type.model_validate(value)
    except ValidationError as error:
        message = f"Output does not match the requested schema: {error}"
        record_attempt(binding.store, message)
        return SubmissionResponse(
            accepted=False,
            message=message,
        )
    if binding.gate is not None:
        decision: SubmissionDecision = await binding.gate(output)
        if not decision.accepted:
            record_attempt(binding.store, decision.message)
            return SubmissionResponse(accepted=False, message=decision.message)
    binding.store.write(output)
    return SubmissionResponse(accepted=True, message="Output accepted.")


type SubmissionHandler = Callable[[JsonValue], Awaitable[SubmissionResponse]]


class TurnSubmission:
    """The submission tool one turn installed, holding its own output type.

    A native tool advertises a single schema for the life of a connection, so
    what the connection stores cannot be generic in the turn's output type.
    Storing the bound submission rather than a widened `TurnToolBinding`
    is what keeps that type from being erased to `BaseModel` and then
    revalidated back into itself before every gate.
    """

    def __init__(self, schema: JsonObject, submit: SubmissionHandler) -> None:
        self.schema = schema
        self.submit = submit
        # Serialized without native string interpolation, so a binder can tell
        # a schema that actually moved from a turn reinstalling the same one.
        self.digest = hashlib.sha256(
            json.dumps(schema, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()


def bound_submission[T: BaseModel](binding: TurnToolBinding[T]) -> TurnSubmission:
    """Bind one turn's typed submission behind a handle that is not generic."""

    async def submit(arguments: JsonValue) -> SubmissionResponse:
        return await submit_output(binding, arguments)

    return TurnSubmission(
        schema=binding.output_type.model_json_schema(),
        submit=submit,
    )
