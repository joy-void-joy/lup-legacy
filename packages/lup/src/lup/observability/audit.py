# lup: ignore[own-model-dispatch]
# Which journal event a turn event becomes is this layer's reading of it, not
# something the event knows about itself: `ObservableEventKind` is this
# layer's vocabulary, and declaring the projection on the runtime event union
# would have `lup.sessions` depend on `lup.observability`, inverting the one
# direction these two layers are allowed to point in.
"""Complete observable, hierarchical runtime transcripts.

The ordinary :mod:`lup.observability.trace` sidecar is a compact feedback index —
what a later reader skims to find a session worth opening. This is the other
half: the lossless audit stream, where every event the provider or runtime
exposes is appended immediately, correlated to the actor and span that
produced it, and chained by hash so a gap or an edit is visible rather than
silent.

Three properties are deliberate. It appends *as it happens*, because a
transcript assembled at the end is missing exactly the runs that crashed. It
is chained, so the record can be checked rather than trusted. And it never
claims to recover reasoning a provider keeps private — an absent thought is
absent from the journal too, rather than reconstructed into something that
reads like evidence.
"""

import asyncio
import hashlib
import logging
from abc import ABC, abstractmethod
from collections.abc import AsyncGenerator, AsyncIterator, Iterator, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field, TypeAdapter

from lup.channels.stream import Stream
from lup.observability.journal import ChainedWriter, Journal, JournalRecord, last_record
from lup.sessions.composition import is_output_model
from lup.sessions.capabilities import (
    EventStream,
    SessionEngine,
    SessionWrapper,
    TurnEngine,
)
from lup.sessions.errors import DeltaStreamingDisabled, TurnError
from lup.sessions.events import (
    BlockCompletedEvent,
    BlockDeltaEvent,
    BlockStartedEvent,
    LiveTurnEvent,
    MessageCompletedEvent,
    SessionId,
    TurnCompletedEvent,
    TurnEvent,
    StartedTurn,
    TurnMessage,
    TurnRequest,
    TurnResult,
    TurnStartedEvent,
    UNNAMED_SUBAGENT,
)
from lup.types import JsonObject, JsonValue

logger = logging.getLogger(__name__)

TRACE_SCHEMA_VERSION = 2
"""Which record shape the chain in a transcript was computed over.

The digest covers every field of a draft, so a transcript written under an
earlier shape verifies against the rules it was written by and not against
these. Bumping here is what keeps that legible: an old transcript reads as
old rather than as tampered with.
"""

BLOB_THRESHOLD_BYTES = 64 * 1024
# lup: ignore[constant-declaration] — the marker this repository writes into a
# transcript in place of a secret, which a reader recognizes by sight
REDACTED = "[REDACTED]"

# Matched as substrings of a field name, so one fragment covers the spellings
# a provider might pick ("api_key", "x-api-key", "apiKeyId"). Over-matching
# costs a redacted field somebody wanted; under-matching writes a credential
# into a durable file, which is the direction worth erring away from.
SECRET_KEY_FRAGMENTS = (
    "access_token",
    "api_key",
    "authorization",
    "cookie",
    "credential",
    "password",
    "refresh_token",
    "secret",
    "session_token",
)

type ActorKind = Literal[
    "harness",
    "orchestrator",
    "native_agent",
    "native_subagent",
    "nested_agent",
    "background_agent",
    "tool",
    "job",
    "system",
]
type ObservableEventKind = Literal[
    "run_start",
    "run_end",
    "session_start",
    "session_end",
    "turn_input",
    "turn_start",
    "block_start",
    "block_delta",
    "message",
    "native_activity",
    "reasoning",
    "tool_call",
    "tool_result",
    "turn_result",
    "turn_end",
    "subagent_start",
    "subagent_end",
    "job_state",
    "sleep",
    "wake",
    "artifact",
    "derived_status",
    "usage",
    "error",
]


class TraceActor(BaseModel, frozen=True):
    """One participant in the observable agent tree."""

    kind: ActorKind
    name: str
    provider: str | None = None
    model: str | None = None


class TraceContext(BaseModel, frozen=True):
    """Stable run and parentage identifiers inherited by emitted events."""

    run_id: str
    trace_id: str
    span_id: str
    parent_span_id: str | None = None
    actor: TraceActor

    @classmethod
    def root(cls, run_id: str, actor: TraceActor) -> "TraceContext":
        """Create the root span for one run."""
        return cls(
            run_id=run_id,
            trace_id=uuid4().hex,
            span_id=uuid4().hex,
            actor=actor,
        )

    def child(self, actor: TraceActor) -> "TraceContext":
        """Create a correlated child agent, tool, or job span."""
        return TraceContext(
            run_id=self.run_id,
            trace_id=self.trace_id,
            span_id=uuid4().hex,
            parent_span_id=self.span_id,
            actor=actor,
        )


class ObservableEventDraft(JournalRecord[TraceActor], frozen=True):
    """Hash input for one event, excluding the chain-derived digest.

    Where the record sits in the order, and whose it is, come from the shared
    journal record — so a transcript is paged by the same reader that pages a
    decision log. The digest covers the whole draft including those two,
    which is what :data:`TRACE_SCHEMA_VERSION` names.
    """

    schema_version: int = TRACE_SCHEMA_VERSION
    timestamp: str
    run_id: str
    trace_id: str
    span_id: str
    parent_span_id: str | None = None
    session_id: str | None = None
    turn_id: str | None = None
    kind: ObservableEventKind
    payload: JsonObject = {}
    previous_hash: str | None = None

    def digest(self) -> str:
        """Hash this canonical draft, including the prior chain hash."""
        encoded = self.model_dump_json(by_alias=True, exclude_none=False)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class ObservableEvent(ObservableEventDraft, frozen=True):
    """One persisted event in a tamper-evident transcript."""

    event_hash: str

    @property
    def tool_name(self) -> str | None:
        """Tool leaf name for a canonical tool-actor event, else ``None``.

        Actor names are dotted, so the leaf is what a caller means when it
        asks which tool ran.
        """
        if self.actor.kind != "tool":
            return None
        separator = self.actor.name.rfind(".")
        return self.actor.name[separator + 1 :]


def bare_name(text: str) -> str:
    """One field name reduced to what every spelling of it has in common."""
    return "".join(letter for letter in text.casefold() if letter not in "-_")


class Redaction(ABC):
    """One way of removing credential material from a payload.

    An engine rather than a surface: a journal is handed one and applies it,
    and callers hold the journal. Keeping it abstract is what lets a
    deployment add a rule — masking a command line, scanning values for
    secret-shaped substrings — by composing another rule in, rather than by
    editing a walk that every other rule also runs through.
    """

    @abstractmethod
    def apply(self, value: JsonValue) -> JsonValue:
        """Return ``value`` with whatever this rule redacts replaced."""


class KeyRedaction(Redaction):
    """Redact a value whose own field name denotes credential material.

    The common case, and the cheap one: a payload that labels its secret is
    one lookup away from being safe. It says nothing about values, so a
    credential sitting under an innocuous name passes through — that is what
    a second, composed rule is for.
    """

    def __init__(self, fragments: Sequence[str] = SECRET_KEY_FRAGMENTS) -> None:
        self.fragments = tuple(fragments)

    def names_a_secret(self, key: str) -> bool:
        """Whether a structured field name denotes credential material.

        Separators are dropped from both sides before matching, because the
        same credential is spelled three ways depending on where it is
        written — ``api_key`` in JSON, ``--api-key`` on a command line,
        ``apiKey`` in a header — and all three name the same thing.
        """
        folded = bare_name(key)
        return any(bare_name(fragment) in folded for fragment in self.fragments)

    def apply(self, value: JsonValue) -> JsonValue:
        match value:
            case dict() as mapping:
                return {
                    str(key): (
                        REDACTED if self.names_a_secret(str(key)) else self.apply(child)
                    )
                    for key, child in mapping.items()
                }
            case list() as items:
                return [self.apply(item) for item in items]
            case _:
                return value


class ArgvRedaction(Redaction):
    """Redact credential values in a command line, named by the flag beside them.

    The one place :class:`KeyRedaction` cannot help: the secret is a value,
    and what identifies it is the flag before it rather than a key above it.
    Both spellings carry one — ``--api-key=X`` in the same word, ``--api-key
    X`` in the next — so the fold remembers whether the argument it is about
    to see is a secret.

    Applied only to a vector a caller hands over as a command line. It is not
    in the journal's default chain, because most lists of strings in a
    payload are not argv and reading them as argv would mangle them.
    """

    def __init__(self, fragments: Sequence[str] = SECRET_KEY_FRAGMENTS) -> None:
        self.keys = KeyRedaction(fragments)

    def arguments(self, argv: Sequence[str]) -> list[str]:
        """Redact one argv vector while preserving its shape."""

        def folded() -> Iterator[str]:
            redact_next = False
            for argument in argv:
                if redact_next:
                    redact_next = False
                    yield REDACTED
                    continue
                # lup: ignore[string-split] — `--flag=value` is argv's own
                # syntax, and no standard-library parser reads a command line
                option, separator, _value = argument.partition("=")
                if not (option.startswith("-") and self.keys.names_a_secret(option)):
                    yield argument
                    continue
                if separator:
                    yield f"{option}={REDACTED}"
                else:
                    redact_next = True
                    yield argument

        return list(folded())

    def apply(self, value: JsonValue) -> JsonValue:
        match value:
            case list() as items if all(isinstance(item, str) for item in items):
                return list(self.arguments([str(item) for item in items]))
            case _:
                return value


class PortableRoot(BaseModel, frozen=True):
    """One directory a record should name by role rather than by location."""

    label: str = Field(min_length=1)
    path: Path


class PathRedaction(Redaction):
    """Rewrite absolute paths to labels that name a root instead of a machine.

    The rule the other two cannot state. A credential is identified by the
    name beside it; a filesystem path identifies nothing and is still the
    thing that makes a transcript unshareable — an operator's home, the
    checkouts beside this one, the account directory a launch selected. None
    of it is secret and all of it is theirs.

    Substring rewriting rather than a parse, because a path in a payload is
    rarely a path-shaped field: it is quoted inside an error message, printed
    mid-sentence by a tool, or one word of a command line. Roots are applied
    longest first, so a checkout nested under the home directory is labelled
    as the checkout rather than as a directory inside a home.

    Deliberately not in :class:`TraceJournal`'s default chain. A rule that
    rewrites every string in every payload is the caller's decision to take
    for a durable record, not a default every in-process journal inherits.
    """

    def __init__(self, roots: Sequence[PortableRoot]) -> None:
        resolved = [(root.label, str(root.path.resolve())) for root in roots]
        self.roots = sorted(resolved, key=lambda root: len(root[1]), reverse=True)

    def rewrite(self, text: str) -> str:
        """Replace every labelled root prefix appearing anywhere in ``text``."""
        for label, root in self.roots:
            # lup: ignore[string-replace] — the subject is prose that happens
            # to contain a path, not a path: a traceback line, a shell word, a
            # sentence a tool printed. `pathlib` parses a path once it has been
            # found, and finding one inside arbitrary text is the whole problem.
            text = text.replace(root, label)
        return text

    def apply(self, value: JsonValue) -> JsonValue:
        match value:
            case str() as text:
                return self.rewrite(text)
            case dict() as mapping:
                return {
                    self.rewrite(str(key)): self.apply(child)
                    for key, child in mapping.items()
                }
            case list() as items:
                return [self.apply(item) for item in items]
            case _:
                return value


class Redactions(Redaction):
    """Apply several redactions in order, each over the last one's result."""

    def __init__(self, *rules: Redaction) -> None:
        self.rules = rules

    def apply(self, value: JsonValue) -> JsonValue:
        for rule in self.rules:
            value = rule.apply(value)
        return value


def store_large_values(value: JsonValue, directory: Path) -> JsonValue:
    """Move large strings into content-addressed blobs without losing bytes.

    A single oversized payload would otherwise make every later line of the
    transcript expensive to scan past. The bytes are kept, addressed by their
    own digest, so the reference is stable and identical payloads collapse.
    """
    match value:
        case str() as text if len(text.encode("utf-8")) >= BLOB_THRESHOLD_BYTES:
            encoded = text.encode("utf-8")
            digest = hashlib.sha256(encoded).hexdigest()
            directory.mkdir(parents=True, exist_ok=True)
            destination = directory / f"{digest}.txt"
            if not destination.exists():
                destination.write_bytes(encoded)
            return {
                "$blob": digest,
                "media_type": "text/plain; charset=utf-8",
                "path": str(destination),
                "size_bytes": len(encoded),
            }
        case dict() as mapping:
            return {
                str(key): store_large_values(child, directory)
                for key, child in mapping.items()
            }
        case list() as items:
            return [store_large_values(item, directory) for item in items]
        case _:
            return value


EVENT_ADAPTER: TypeAdapter[ObservableEvent] = TypeAdapter(ObservableEvent)

type ObservableJournal = Journal[TraceActor, ObservableEvent]
"""The ordered file every span of one trace tree appends to."""


def open_observable_journal(path: Path) -> ObservableJournal:
    """One transcript, written durably and chained under a cross-process lock."""
    return Journal(
        path, EVENT_ADAPTER, ChainedWriter(path.with_suffix(path.suffix + ".lock"))
    )


class TraceJournal:
    """Append complete observable events immediately and durably.

    One span's view of a shared record. The ordering, the lock and the chain
    belong to the journal underneath; what is here is the span — which actor
    is speaking, and under which parent — and the redaction a payload passes
    through before anything durable holds it.
    """

    def __init__(
        self,
        path: Path,
        context: TraceContext,
        journal: ObservableJournal | None = None,
        redaction: Redaction | None = None,
    ) -> None:
        self.context = context
        self.journal = journal or open_observable_journal(path)
        self.redaction = redaction or KeyRedaction()

    @property
    def path(self) -> Path:
        """The shared JSONL path this actor span appends to."""
        return self.journal.path

    def child(self, actor: TraceActor) -> "TraceJournal":
        """Write a child span into the same canonical event stream.

        The journal itself is handed over rather than reopened, so every span
        in one tree shares the order, the lock, and the chain head.
        """
        return TraceJournal(
            self.path, self.context.child(actor), self.journal, self.redaction
        )

    def emit(
        self,
        kind: ObservableEventKind,
        payload: JsonObject | None = None,
        *,
        session_id: str | None = None,
        turn_id: str | None = None,
    ) -> ObservableEvent:
        """Redact, chain, append, flush, and return one event.

        Everything happens inside the journal's lock, including the redaction
        and the blob spilling: the chain head is whatever the file holds at
        the moment this record is built, and a second process appending to
        the same transcript must not be able to move it in between.
        """
        return self.journal.write(
            lambda seq, previous: self.event(
                seq, previous, kind, payload, session_id, turn_id
            )
        )

    def event(
        self,
        seq: int,
        previous: ObservableEvent | None,
        kind: ObservableEventKind,
        payload: JsonObject | None,
        session_id: str | None,
        turn_id: str | None,
    ) -> ObservableEvent:
        """Build one chained event, with the transcript lock already held."""
        safe_payload = self.redaction.apply(payload or {})
        if not isinstance(safe_payload, dict):
            raise TypeError("event payload redaction changed object shape")
        persisted = store_large_values(safe_payload, self.path.with_suffix(".blobs"))
        if not isinstance(persisted, dict):
            raise TypeError("event payload storage changed object shape")
        draft = ObservableEventDraft(
            seq=seq,
            timestamp=datetime.now().astimezone().isoformat(),
            run_id=self.context.run_id,
            trace_id=self.context.trace_id,
            span_id=self.context.span_id,
            parent_span_id=self.context.parent_span_id,
            actor=self.context.actor,
            session_id=session_id,
            turn_id=turn_id,
            kind=kind,
            payload=persisted,
            previous_hash=previous.event_hash if previous is not None else None,
        )
        return ObservableEvent(
            **draft.model_dump(mode="python"),
            event_hash=draft.digest(),
        )


def read_last_observable_event(path: Path) -> ObservableEvent | None:
    """Read only the final complete record.

    Reading backwards from a bounded window rather than parsing the file is
    what keeps the per-event cost flat: a transcript is appended to once per
    event, and re-reading all of it each time would make a long run
    quadratic.
    """
    return last_record(path, EVENT_ADAPTER)


def read_observable_events(path: Path) -> list[ObservableEvent]:
    """Load every valid record from an observable JSONL transcript.

    A malformed line is reported and skipped rather than ending the read: a
    run killed mid-write leaves a torn final line, and the events before it
    are still the evidence somebody opened the file for.
    """
    return Stream(path, EVENT_ADAPTER).read_all()


type ChainFault = Literal["schema", "restart", "sequence", "link", "digest"]
"""How a transcript's chain fails at one record.

``schema`` is a record written under another :data:`TRACE_SCHEMA_VERSION`,
whose digest rules are not these, so it cannot be checked rather than failing
a check. ``restart`` is a second chain beginning mid-file — sequence zero,
nothing before it — which a writer that lost its own head leaves as surely
as an edit does. ``sequence`` is a record missing before this one: removed,
reordered, or unreadable and skipped. ``link`` is a record whose predecessor
is not the one it was chained onto. ``digest`` is a record whose content is
not the content it was hashed over: edited in place.
"""


class ChainBreak(BaseModel, frozen=True):
    """The first record at which a transcript stops verifying, and how.

    Only the first, because nothing after a break is vouched for by what
    came before it: a reader takes the records up to ``position`` as
    evidence and the rest as text.
    """

    position: int
    """Where the record sits among the transcript's readable records."""

    seq: int
    """The sequence number the record carries."""

    fault: ChainFault

    def explained(self) -> str:
        """What the fault means at this record, in words."""
        match self.fault:
            case "schema":
                return (
                    f"record {self.position} was written under another record "
                    "shape, whose chain this reader cannot check"
                )
            case "restart":
                return (
                    f"a second chain starts at record {self.position}: the "
                    "writer lost its place, or the file was spliced"
                )
            case "sequence":
                return (
                    f"record {self.position} carries seq {self.seq}: a record "
                    "before it is missing, moved, or unreadable"
                )
            case "link":
                return (
                    f"record {self.position} was chained onto a record that "
                    "is not the one before it"
                )
            case "digest":
                return (
                    f"record {self.position} is not what was hashed: its "
                    "content changed after it was written"
                )


def chain_break(events: Sequence[ObservableEvent]) -> ChainBreak | None:
    """Where a transcript's chain first fails, or nothing where it holds.

    Each record is checked against its own place, against the record before
    it, and against its own digest — which together make an edit, a removal,
    an insertion or a reordering anywhere but the very end visible. What it
    cannot see is a tail cut off cleanly, or a whole chain rewritten; a
    ledger record that pinned the file's digest when the session closed is
    what answers for those.
    """
    prior: str | None = None
    for position, event in enumerate(events):
        draft = ObservableEventDraft.model_validate(
            event.model_dump(exclude={"event_hash"})
        )
        # Checked in this order, so the first fault named is the most telling:
        # a restart also fails the sequence, and a missing record the link.
        failing: dict[ChainFault, bool] = {
            "schema": event.schema_version != TRACE_SCHEMA_VERSION,
            "restart": position > 0 and event.seq == 0 and event.previous_hash is None,
            "sequence": event.seq != position,
            "link": event.previous_hash != prior,
            "digest": event.event_hash != draft.digest(),
        }
        fault: ChainFault | None = next(
            (named for named, failed in failing.items() if failed), None
        )
        if fault is not None:
            return ChainBreak(position=position, seq=event.seq, fault=fault)
        prior = event.event_hash
    return None


def block_event_kind(block_type: str) -> ObservableEventKind:
    """Which canonical event kind one completed block is recorded as."""
    match block_type:
        case "thinking":
            return "reasoning"
        case "tool_call":
            return "tool_call"
        case "tool_result":
            return "tool_result"
        case "native_activity":
            return "native_activity"
        case _:
            return "message"


class TurnRecorder:
    """Project one turn's live events into the journal as they arrive.

    Stateful because native delegation reaches us in two halves that the
    provider never joins: the delegating call names the role, and the
    messages produced under it carry only that call's id. The call always
    streams past first, so remembering it as it goes is enough — and it
    keeps the delegated spans written *during* the turn, which is the whole
    point of an append-as-it-happens transcript. Reconstructing them from
    the finished result instead would lose exactly the turns that died.
    """

    def __init__(self, journal: TraceJournal) -> None:
        self.journal = journal
        # lup: ignore[dict-str-payload] — keyed by provider-minted call ids
        self.roles: dict[str, str] = {}
        self.children: dict[str, TraceJournal] = {}

    def child_for(self, call_id: str, model: str | None) -> TraceJournal:
        """The child span for one delegation, opened on first sight."""
        if call_id not in self.children:
            role = self.roles.get(call_id, UNNAMED_SUBAGENT)
            child = self.journal.child(
                TraceActor(
                    kind="native_subagent",
                    name=role,
                    provider=self.journal.context.actor.provider,
                    model=model,
                )
            )
            self.children[call_id] = child
            child.emit("subagent_start", {"parent_tool_call_id": call_id})
        return self.children[call_id]

    def record(self, event: LiveTurnEvent) -> None:
        """Journal one live event, opening delegated spans as they appear."""
        session_id = event.identifiers.session.value
        turn_id = event.identifiers.turn.value
        match event:
            case TurnStartedEvent():
                self.journal.emit("turn_start", session_id=session_id, turn_id=turn_id)
            case BlockStartedEvent(block=block):
                self.journal.emit(
                    "block_start",
                    {"block": block.model_dump(mode="json")},
                    session_id=session_id,
                    turn_id=turn_id,
                )
            case BlockDeltaEvent(delta=delta):
                self.journal.emit(
                    "block_delta",
                    {"delta": delta},
                    session_id=session_id,
                    turn_id=turn_id,
                )
            case BlockCompletedEvent(block=block):
                role = block.delegated_role()
                call_id = block.invoked_call_id
                if role is not None and call_id is not None:
                    self.roles[call_id] = role
                self.journal.emit(
                    block_event_kind(block.type),
                    {"block": block.model_dump(mode="json")},
                    session_id=session_id,
                    turn_id=turn_id,
                )
            case MessageCompletedEvent(message=message):
                self.record_message(message, session_id, turn_id)
            case TurnCompletedEvent():
                self.close(session_id, turn_id)
                self.journal.emit("turn_end", session_id=session_id, turn_id=turn_id)

    def record_message(
        self, message: TurnMessage, session_id: str, turn_id: str
    ) -> None:
        """Attribute a delegated message to its own span.

        Only delegated messages are recorded here. The parent's own blocks
        already went by individually, and repeating them at message
        granularity would double every line of the transcript.
        """
        call_id = message.parent_tool_call_id
        if message.native is not None:
            target = (
                self.journal
                if call_id is None
                else self.child_for(call_id, message.model)
            )
            target.emit(
                "native_activity",
                {"message": message.model_dump(mode="json")},
                session_id=session_id,
                turn_id=turn_id,
            )
        if call_id is None:
            return
        child = self.child_for(call_id, message.model)
        for block in message.blocks:
            child.emit(
                block_event_kind(block.type),
                {
                    "role": message.role,
                    "message_id": message.message_id,
                    "block": block.model_dump(mode="json"),
                },
                session_id=session_id,
                turn_id=turn_id,
            )

    def close(self, session_id: str, turn_id: str) -> None:
        """Close every delegated span still open at the end of the turn."""
        for call_id, child in self.children.items():
            child.emit(
                "subagent_end",
                {"parent_tool_call_id": call_id},
                session_id=session_id,
                turn_id=turn_id,
            )
        self.children.clear()


class JournalEventStream(EventStream):
    """Drain one native stream while mirroring it to callers and the journal.

    The source is consumed exactly once, by this class, because a provider
    stream cannot be replayed: recording it and handing it on have to be the
    same pass, or one of the two gets nothing.
    """

    def __init__(
        self,
        inner: EventStream,
        journal: TraceJournal,
        mirrored: asyncio.Queue[LiveTurnEvent | None],
    ) -> None:
        self.inner = inner
        self.recorder = TurnRecorder(journal)
        self.journal = journal
        self.queue = mirrored
        self.consumed = False
        self.deltas: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        self.drain = asyncio.create_task(self.drain_inner())

    async def drain_inner(self) -> None:
        """Record and mirror the complete source stream exactly once."""
        try:
            try:
                async for event in self.inner.live():
                    if not self.deltas.done():
                        self.deltas.set_result(True)
                    self.recorder.record(event)
                    self.queue.put_nowait(event)
            except DeltaStreamingDisabled:
                if self.deltas.done():
                    raise
                self.deltas.set_result(False)
                async for event in self.inner.events():
                    self.recorder.record(event)
                    self.queue.put_nowait(event)
        except Exception as error:
            self.journal.emit("error", {"message": str(error)})
            raise
        finally:
            if not self.deltas.done():
                self.deltas.set_result(True)
            self.queue.put_nowait(None)

    async def wait(self) -> None:
        """Wait until the provider event stream reaches its terminal marker."""
        await self.drain

    async def iterate(self) -> AsyncIterator[LiveTurnEvent]:
        if self.consumed:
            raise RuntimeError("journal event stream can only be consumed once")
        self.consumed = True
        while (event := await self.queue.get()) is not None:
            yield event

    async def durable(self) -> AsyncIterator[TurnEvent]:
        async for event in self.iterate():
            if (kept := event.durable) is not None:
                yield kept

    def events(self) -> AsyncIterator[TurnEvent]:
        return self.durable()

    async def live(self) -> AsyncIterator[LiveTurnEvent]:
        if not await self.deltas:
            raise DeltaStreamingDisabled(
                "this journaled session does not stream deltas"
            )
        async for event in self.iterate():
            yield event


class JournalTurn[T: BaseModel | None](TurnEngine[T]):
    """Persist the terminal result or complete failure of one logical turn."""

    def __init__(
        self,
        inner: TurnEngine[T],
        journal: TraceJournal,
        event_stream: JournalEventStream,
    ) -> None:
        self.inner = inner
        self.journal = journal
        self.event_stream = event_stream

    async def result(self) -> TurnResult[T]:
        try:
            result = await self.inner.result()
        except TurnError as error:
            failure = error.failure
            await self.event_stream.wait()
            identifiers = failure.identifiers
            self.journal.emit(
                "error",
                {
                    "message": failure.message,
                    "blocks": [
                        block.model_dump(mode="json") for block in failure.blocks
                    ],
                    "messages": [
                        message.model_dump(mode="json") for message in failure.messages
                    ],
                    "usage": failure.usage.model_dump(mode="json"),
                },
                session_id=(
                    identifiers.session.value if identifiers is not None else None
                ),
                turn_id=(identifiers.turn.value if identifiers is not None else None),
            )
            raise
        await self.event_stream.wait()
        self.journal.emit(
            "turn_result",
            {
                "messages": [
                    message.model_dump(mode="json") for message in result.messages
                ],
                "blocks": [block.model_dump(mode="json") for block in result.blocks],
                "output": (
                    result.output.model_dump(mode="json")
                    if isinstance(result.output, BaseModel)
                    else None
                ),
                "usage": result.usage.model_dump(mode="json"),
                "duration_seconds": result.duration.total_seconds(),
            },
            session_id=result.identifiers.session.value,
            turn_id=result.identifiers.turn.value,
        )
        return result


class JournalSession(SessionEngine):
    """Record turn input and attach lossless event/result journaling."""

    def __init__(self, inner: SessionEngine, journal: TraceJournal) -> None:
        self.inner = inner
        self.journal = journal

    async def start[T: BaseModel | None](
        self, request: TurnRequest[T]
    ) -> StartedTurn[T]:
        self.journal.emit(
            "turn_input",
            {
                "text": request.input.text,
                "output_schema": (
                    request.output_type.model_json_schema()
                    if is_output_model(request.output_type)
                    else None
                ),
            },
        )
        handle = await self.inner.start(request)
        mirrored: asyncio.Queue[LiveTurnEvent | None] = asyncio.Queue()
        event_stream = JournalEventStream(handle.events, self.journal, mirrored)
        return StartedTurn[T](
            turn=JournalTurn(handle.turn, self.journal, event_stream),
            events=event_stream,
            interrupt=handle.interrupt,
            steer=handle.steer,
        )


class JournalWrapper(SessionWrapper):
    """Give every turn of a session the canonical observable journal.

    The session's opening and close are recorded around it, so a session
    that failed to open is in the journal as well as one that ran.
    """

    def __init__(self, journal: TraceJournal) -> None:
        self.journal = journal

    def around(
        self,
        opened: AbstractAsyncContextManager[SessionEngine],
        resume: SessionId | None,
    ) -> AbstractAsyncContextManager[SessionEngine]:
        return self.journaled(opened, resume)

    @asynccontextmanager
    async def journaled(
        self,
        opened: AbstractAsyncContextManager[SessionEngine],
        resume: SessionId | None,
    ) -> AsyncGenerator[SessionEngine]:
        self.journal.emit(
            "session_start",
            {"resume_session_id": resume.value if resume is not None else None},
        )
        try:
            async with opened as inner:
                yield JournalSession(inner, self.journal)
        except Exception as error:
            self.journal.emit("error", {"message": str(error)})
            raise
        finally:
            self.journal.emit("session_end")
