"""Immutable semantic values shared by all runtime implementations."""

import json
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import BaseModel, Discriminator, Field

from lup.sessions.capabilities import (
    EventStream,
    Interrupt,
    Steer,
    SubmittedOutputStore,
    TurnEngine,
)
from lup.types import (
    JsonObject,
    LupContentBlock,
    LupNativeActivityBlock,
    LupTextBlock,
    LupThinkingBlock,
    LupToolResultBlock,
    LupToolUseBlock,
    PayloadText,
    Usage,
)


# The tool names a runtime spells native delegation with. Kept beside the
# block that answers about a delegation so no reader has to know them.
DELEGATION_TOOLS = ("Agent", "Task")

# What a delegation is called when its call named no role.
UNNAMED_SUBAGENT = "subagent"


class DelegatedRole(BaseModel, frozen=True):
    """The role a delegation call names, under either spelling a runtime uses.

    Claude's Agent tool spells the role `subagent_type` and the spec-driven
    delegation tool spells it `name`, so a call carries whichever its own tool
    declared and this reads both. A value that is not a string names no role,
    which is what a model emitting junk arguments produces.
    """

    subagent_type: PayloadText = None
    name: PayloadText = None


class SessionId(BaseModel, frozen=True):
    """Opaque native conversation identity."""

    value: str


class TurnId(BaseModel, frozen=True):
    """Opaque native turn identity."""

    value: str


class TurnIdentifiers(BaseModel, frozen=True):
    """Identities attached to one accepted turn."""

    session: SessionId
    turn: TurnId


class TurnInput(BaseModel, frozen=True):
    """Portable user input for one turn."""

    text: str


class TurnBlock(BaseModel, ABC, frozen=True):
    """One completed block of a turn, answering every question about itself.

    Whatever a caller needs to know about a block is declared here and
    answered — or declined — by the block, so a new kind of block is one class
    rather than an edit to every walk that would have to notice it. The
    declining answers are what make omission safe: a caller joining
    ``text_payload`` reaches every kind that carries prose, including kinds
    written long after the caller was.

    Pydantic's metaclass is an ``ABCMeta``, so ``telemetry_block`` binds like
    any abstract property: a kind that does not answer it cannot be built.
    """

    @property
    @abstractmethod
    def telemetry_block(self) -> LupContentBlock:
        """This block as the telemetry vocabulary spells it."""

    @property
    def text_payload(self) -> str | None:
        """Prose this block carries verbatim, if it carries any.

        Everything that reads what a turn actually said asks this instead of
        naming the kinds of block that hold text.
        """
        return None

    @property
    def tool_call_name(self) -> str | None:
        """The tool this block invokes, if it invokes one."""
        return None

    @property
    def tool_arguments(self) -> JsonObject | None:
        """The arguments this block invokes its tool with, if it invokes one."""
        return None

    @property
    def invoked_call_id(self) -> str | None:
        """The id of the call this block makes, if it makes one."""
        return None

    @property
    def answered_call_id(self) -> str | None:
        """The id of the call this block answers, if it answers one."""
        return None

    def delegated_role(
        self,
        tools: tuple[str, ...] = DELEGATION_TOOLS,
        unnamed: str = UNNAMED_SUBAGENT,
    ) -> str | None:
        """The subagent role this block delegates to, if it delegates.

        Asked of the block so a reader correlating a transcript never has to
        know which tool a runtime spells delegation with, nor which argument
        carries the role. Both are parameters because a runtime this library
        has not met spells them its own way, and a caller should not have to
        fork a block to say so.
        """
        return None

    @property
    def refusal(self) -> "ToolRefusal | None":
        """The refused call this block reports, if it reports one."""
        return None


class TurnTextBlock(TurnBlock, frozen=True):
    """One completed assistant text block."""

    type: Literal["text"] = "text"
    text: str

    @property
    def telemetry_block(self) -> LupContentBlock:
        return LupTextBlock(text=self.text)

    @property
    def text_payload(self) -> str | None:
        return self.text


class TurnThinkingBlock(TurnBlock, frozen=True):
    """One completed reasoning block."""

    type: Literal["thinking"] = "thinking"
    thinking: str
    redacted: bool = False

    @property
    def telemetry_block(self) -> LupContentBlock:
        return LupThinkingBlock(thinking=self.thinking, redacted=self.redacted)


class TurnToolCallBlock(TurnBlock, frozen=True):
    """One completed tool invocation."""

    type: Literal["tool_call"] = "tool_call"
    id: str
    name: str
    arguments: JsonObject = {}

    @property
    def telemetry_block(self) -> LupContentBlock:
        return LupToolUseBlock(id=self.id, name=self.name, input=self.arguments)

    @property
    def tool_call_name(self) -> str | None:
        return self.name

    @property
    def tool_arguments(self) -> JsonObject | None:
        return self.arguments

    @property
    def invoked_call_id(self) -> str | None:
        return self.id

    def delegated_role(
        self,
        tools: tuple[str, ...] = DELEGATION_TOOLS,
        unnamed: str = UNNAMED_SUBAGENT,
    ) -> str | None:
        if self.name not in tools:
            return None
        requested = DelegatedRole.model_validate(self.arguments)
        return requested.subagent_type or requested.name or unnamed


class ToolRefusal(BaseModel, frozen=True):
    """One tool call that returned an error instead of a result."""

    call_id: str
    detail: str


class TurnToolResultBlock(TurnBlock, frozen=True):
    """One completed tool result."""

    type: Literal["tool_result"] = "tool_result"
    tool_call_id: str
    content: str
    is_error: bool = False

    @property
    def answered_call_id(self) -> str | None:
        return self.tool_call_id

    @property
    def refusal(self) -> ToolRefusal | None:
        if not self.is_error:
            return None
        return ToolRefusal(call_id=self.tool_call_id, detail=self.content)

    @property
    def telemetry_block(self) -> LupContentBlock:
        rendered = (
            json.dumps({"is_error": True, "content": self.content})
            if self.is_error
            else self.content
        )
        return LupToolResultBlock(tool_use_id=self.tool_call_id, content=rendered)


class TurnNativeActivityBlock(TurnBlock, frozen=True):
    """Complete provider evidence with no equivalent portable content shape."""

    type: Literal["native_activity"] = "native_activity"
    provider: str
    activity: str
    payload: JsonObject

    @property
    def telemetry_block(self) -> LupContentBlock:
        return LupNativeActivityBlock(
            provider=self.provider, activity=self.activity, payload=self.payload
        )


type AnyTurnBlock = Annotated[
    TurnTextBlock
    | TurnThinkingBlock
    | TurnToolCallBlock
    | TurnToolResultBlock
    | TurnNativeActivityBlock,
    Discriminator("type"),
]
"""One block as a pydantic *field* validates it: the closed set, discriminated.

Annotations that only read a block name :class:`TurnBlock`, the base. A field
must name this alias instead — validating against the base alone would rebuild
every block as a base instance and drop its payload.
"""


class TurnMessage(BaseModel, frozen=True):
    """A portable transcript message derived from canonical blocks."""

    role: Literal["user", "assistant", "tool", "system"]
    blocks: list[AnyTurnBlock]
    native: JsonObject | None = None
    """Complete provider message payload retained beside normalized blocks."""
    parent_tool_call_id: str | None = Field(
        default=None,
        description=(
            "The delegation tool call this message was produced under, when "
            "the provider attributes it to one — the only evidence that "
            "separates a native subagent's messages from its parent's"
        ),
    )
    model: str | None = Field(
        default=None,
        description="Model the provider reports for this message, when it does",
    )
    message_id: str | None = Field(
        default=None,
        description="Provider's own message identifier, for correlating replays",
    )


class TurnEventBase(BaseModel, frozen=True):
    """One thing that happened during a turn, answering about itself.

    The same arrangement :class:`TurnBlock` uses, for the same reason: a walk
    over events asks the event, so a new kind of event is one class rather
    than an edit to every filter that would have to notice it. The declining
    answers are what make omission safe — a caller folding ``completed_message``
    reaches every kind that carries one, including kinds written later.
    """

    @property
    def durable(self) -> "Self | None":
        """This event, if it survives into the transcript.

        Returning the event rather than a flag is what lets a caller keep the
        narrower type: a walk filtering on this gets exactly the durable
        kinds, the way naming them in an ``isinstance`` would. Only
        in-flight fragments decline, so the default is every terminal event's
        answer.
        """
        return self

    @property
    def completed_message(self) -> "TurnMessage | None":
        """The whole transcript message this event completed, if it completed one."""
        return None

    @property
    def completed_block(self) -> "AnyTurnBlock | None":
        """The content block this event completed, if it completed one.

        What iterating a turn yields, asked of the event for the reason every
        answer here is: a kind of event that also completes a block says so
        itself, rather than a walk having to learn its type.
        """
        return None


class TurnStartedEvent(TurnEventBase, frozen=True):
    """A native turn was accepted."""

    type: Literal["turn_started"] = "turn_started"
    identifiers: TurnIdentifiers


class BlockStartedEvent(TurnEventBase, frozen=True):
    """A native content block started."""

    type: Literal["block_started"] = "block_started"
    identifiers: TurnIdentifiers
    block: AnyTurnBlock


class BlockDeltaEvent(TurnEventBase, frozen=True):
    """One text or thinking delta from an active native block."""

    type: Literal["block_delta"] = "block_delta"
    identifiers: TurnIdentifiers
    delta: str

    @property
    def durable(self) -> None:
        """A fragment of a block still being written survives nothing."""
        return None


class BlockCompletedEvent(TurnEventBase, frozen=True):
    """One native content block completed."""

    type: Literal["block_completed"] = "block_completed"
    identifiers: TurnIdentifiers
    block: AnyTurnBlock

    @property
    def completed_block(self) -> AnyTurnBlock:
        return self.block


class MessageCompletedEvent(TurnEventBase, frozen=True):
    """One whole transcript message completed.

    The message is carried rather than reconstructed. Folding loose blocks
    back into messages would need contiguous-role grouping, which silently
    merges two consecutive assistant messages into one; carrying the whole
    message makes the fold exact.
    """

    type: Literal["message_completed"] = "message_completed"
    identifiers: TurnIdentifiers
    message: TurnMessage

    @property
    def completed_message(self) -> TurnMessage:
        return self.message


class TurnCompletedEvent(TurnEventBase, frozen=True):
    """A native turn reached a terminal state."""

    type: Literal["turn_completed"] = "turn_completed"
    identifiers: TurnIdentifiers


type TurnEvent = (
    TurnStartedEvent
    | BlockStartedEvent
    | BlockCompletedEvent
    | MessageCompletedEvent
    | TurnCompletedEvent
)
"""Everything durable. A transcript folds from exactly these.

Deltas are deliberately absent: :func:`lup.sessions.transcript.fold_transcript`
takes this union, so a partial fragment cannot reach the fold at all rather
than being filtered out inside it.
"""

type LiveTurnEvent = TurnEvent | BlockDeltaEvent
"""Everything durable, plus in-flight deltas, in order.

A strict superset of :data:`TurnEvent`, so a consumer picks one accessor and
gets consistent behaviour either way instead of two views that disagree
about what happened.
"""


class SubmissionDecision(BaseModel, frozen=True):
    """A reflection gate's decision about validated output."""

    accepted: bool
    message: str = ""


type SubmissionGate[T] = Callable[[T], Awaitable[SubmissionDecision]]
type SubmissionGateResolver = Callable[
    [type[BaseModel]], SubmissionGate[BaseModel] | None
]


class TurnRequest[T: BaseModel | None](
    BaseModel, frozen=True, arbitrary_types_allowed=True
):
    """Per-turn input and optional validated output type.

    What a session engine is asked to start. A program never builds one: it
    asks a session for a turn, and the session builds this from the prompt and
    the output type it was given.
    """

    input: TurnInput
    output_type: type[T] | None = None


class TurnResult[T: BaseModel | None](BaseModel, frozen=True):
    """Successful terminal result; failures are represented only by errors."""

    output: T
    messages: list[TurnMessage]
    blocks: list[AnyTurnBlock]
    usage: Usage
    duration: timedelta
    identifiers: TurnIdentifiers


class SessionSummary(BaseModel, frozen=True):
    """One conversation a provider has on record, as a listing shows it.

    Read from the provider's own record rather than from anything this
    library kept, so a conversation started outside it — a terminal session,
    another program — lists beside the ones it opened, and ``id`` resumes
    either kind.
    """

    id: SessionId
    title: str | None = Field(
        default=None,
        description="The name the provider or a person gave it, where it has one",
    )
    preview: str = Field(
        default="",
        description="The first prompt, as the provider recorded it",
    )
    cwd: Path | None = Field(
        default=None, description="The working directory it was started in"
    )
    created_at: datetime | None = None
    updated_at: datetime


class StartedTurn[T: BaseModel | None](
    BaseModel, frozen=True, arbitrary_types_allowed=True
):
    """What a session engine hands back for one accepted turn.

    A carrier rather than a surface: it holds the seams a provider filled and
    no behaviour, so ``turn.result()`` reaches an engine. The public turn a
    caller holds is composed over one of these.

    Events and interrupt are not optional, because both providers supply them
    and a turn a caller cannot watch or stop is not one this library opens.
    Steering is, because only one of them can: a provider's own turn class
    says whether it steers, and this field is where that turn finds how.
    """

    turn: TurnEngine[T]
    events: EventStream
    interrupt: Interrupt
    steer: Steer | None = None


class TurnToolBinding[T: BaseModel](
    BaseModel, frozen=True, arbitrary_types_allowed=True
):
    """Turn-local schema, store, and optional reflection gate."""

    output_type: type[T]
    store: SubmittedOutputStore
    gate: SubmissionGate[T] | None = None
