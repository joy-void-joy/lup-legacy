"""Internal content block and response types.

These types are the shared vocabulary for all consumer code — the
application's orchestration, the trace logger, the tools. SDK-specific
adapters convert to/from these types at the boundary — consumer code
never imports from SDK packages directly. The hook vocabulary lives in
:mod:`lup.policy.hooks`, not here.
"""

import json
from collections.abc import Callable, Sequence
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    BeforeValidator,
    Field,
    StringConstraints,
)


# ---------------------------------------------------------------------------
# JSON vocabulary
# ---------------------------------------------------------------------------

type JsonValue = (
    str | int | float | bool | None | list[JsonValue] | dict[str, JsonValue]
)
"""One JSON-decodable value — the shape ``json.loads`` yields.

The shared vocabulary for data whose schema is defined elsewhere (tool
arguments, JSON Schemas, vendor payloads): unlike ``object`` it stays
introspectable, and unlike ``Any`` it keeps the type checker honest.
"""

type JsonObject = dict[str, JsonValue]
"""A JSON object: tool inputs, JSON Schemas, structured outputs, session data."""

type EnvVars = dict[str, str]  # lup: ignore[dict-str-payload] — open env-var map
"""An environment-variable map — process env, dotenv values, MCP server env.

The keys are open and data-driven by nature (whatever variables exist), which
is exactly the shape the dict-str-payload rule otherwise flags: annotate env
maps with this alias instead of respelling ``dict[str, str]`` per site."""

type StringMap = dict[str, str]  # lup: ignore[dict-str-payload] — open string map
"""Any other open map of strings to strings — a reason table, response headers.

The same shape as ``EnvVars`` and deliberately a separate name: what makes
these keys open differs, and a header map annotated as environment variables
reads as a mistake even where the checker cannot see one. Reach for it when
the keys are data rather than a schema; a fixed set of fields is a
``TypedDict`` or a model, not this."""


def text_or_absent(value: JsonValue) -> str | None:
    """Read one text field of an open payload, or nothing where it is not text."""
    return value if isinstance(value, str) else None


type PayloadText = Annotated[str | None, BeforeValidator(text_or_absent)]
"""One optional text field of a payload whose schema is declared elsewhere.

A vendor notification or a model's tool arguments can carry anything under a
key, and a field declared ``str | None`` fails validation on that junk rather
than reading it as absent — which is what a reader asking "did this name a
URL?" means by it. Reach for this where a model reads an untyped payload;
a field the schema really does guarantee stays ``str``."""


type Namespace = dict[str, object]  # lup: ignore[dict-str-object] — live objects
"""A live Python namespace: names bound to whatever objects they name.

The one shape ``object`` is honest for, because the values genuinely are
arbitrary objects rather than data with a schema somewhere — a module, a
builtin, a class. Reach for ``JsonObject`` for anything that will be
serialized; this is for what an interpreter holds."""

type Decorator[T, R] = Callable[[T], R]
"""A decorator: applied with ``@`` to a `T`, yields an `R`. Names the intent
at a signature (``-> Decorator[Handler, Tool]``) where a bare ``Callable`` of
a callable reads as noise. ``R`` need not be ``T`` — a decorator may return a
different type than it wraps (a builder, a registration object)."""


# ---------------------------------------------------------------------------
# Tool vocabulary
# ---------------------------------------------------------------------------

type KnownToolName = Literal[
    "Agent",
    "AskUserQuestion",
    "Bash",
    "BashOutput",
    "Edit",
    "EnterWorktree",
    "ExitPlanMode",
    "ExitWorktree",
    "Glob",
    "Grep",
    "KillShell",
    "ListMcpResources",
    "MultiEdit",
    "NotebookEdit",
    "Read",
    "ReadMcpResource",
    "Skill",
    "SlashCommand",
    "StructuredOutput",
    "Task",
    "TodoWrite",
    "WebFetch",
    "WebSearch",
    "Workflow",
    "Write",
]
"""The well-known built-in tool names — the framework's lingua franca.

Claude Code's tool vocabulary is adopted as the neutral spelling: adapters
translate their backend's native tool identities onto these names, so hooks,
policies, and harness declarations all read one vocabulary."""

type McpToolName = Annotated[
    str, StringConstraints(pattern=r"^mcp__[A-Za-z0-9_-]+(?:__[A-Za-z0-9_-]+)*$")
]
"""A dynamically registered MCP tool: ``mcp__<server>`` or ``mcp__<server>__<tool>``."""

type ToolName = KnownToolName | McpToolName
"""One tool identity: a well-known built-in or a registered ``mcp__*`` tool."""

type ScopedToolGrant = Annotated[
    str, StringConstraints(pattern=r"^[A-Za-z][A-Za-z0-9_]*\([^()]+\)$")
]
"""A tool grant narrowed by a parenthesized specifier, e.g. ``Bash(git:*)``."""

type ToolGrant = ToolName | ScopedToolGrant
"""One entry of a declared tool grant list: a whole tool or a scoped rule."""


# ---------------------------------------------------------------------------
# Content blocks
# ---------------------------------------------------------------------------


def normalize_content(content: str | Sequence[object] | None) -> str:
    """Flatten tool-result content — MCP block list or scalar — to a string."""
    if content is None:
        return "(empty)"
    if isinstance(content, list):
        texts: list[str] = []  # lup: ignore[empty-collection] — block fold
        for item in content:
            match item:
                case {"type": "text", "text": text}:
                    texts.append(str(text))
        return "\n".join(texts)
    return str(content)


class LupContentBlock(BaseModel):
    """One block of a message, answering every question about itself.

    Whatever a transcript, a console, or a trace needs to know about a block is
    declared here and answered — or declined — by the block, so a new kind of
    block is one class rather than an edit to every walk that would have to
    notice it. The declining answers are what make omission safe: a caller
    asking ``spoken_text`` reaches every kind that voices prose, including
    kinds written long after the caller was.
    """

    @property
    def display_emoji(self) -> str:
        """The glyph a console leads this block with."""
        return "❓"

    @property
    def display_label(self) -> str:
        """The short name a console and a trace heading give this block."""
        return "Unknown"

    @property
    def display_body(self) -> str:
        """This block's content as one readable string."""
        return str(self)

    @property
    def markdown_fence(self) -> str | None:
        """The code-fence language a markdown trace wraps the body in, if any."""
        return None

    @property
    def spoken_text(self) -> str | None:
        """Prose the assistant voiced, if this block voices any."""
        return None

    @property
    def opens_pairing(self) -> str | None:
        """The id this block opens, which a later block closes."""
        return None

    @property
    def closes_pairing(self) -> str | None:
        """The id this block closes, opened by an earlier block."""
        return None

    @property
    def tool_call_name(self) -> str | None:
        """The tool this block invokes, if it invokes one."""
        return None

    @property
    def result_payload(self) -> str | Sequence[object] | None:
        """Raw tool output this block carries, for a caller that reformats it."""
        return None

    def log_summary(self, body: str) -> str:
        """One stream-log line for this block, given its rendered `body`."""
        return f"{self.display_label.upper()}: {body}"


class LupTextBlock(LupContentBlock):
    """Text content from the assistant."""

    type: Literal["text"] = "text"
    text: str

    @property
    def display_emoji(self) -> str:
        return "💬"

    @property
    def display_label(self) -> str:
        return "Response"

    @property
    def display_body(self) -> str:
        return self.text

    @property
    def spoken_text(self) -> str | None:
        return self.text


class LupThinkingBlock(LupContentBlock):
    """Extended thinking / reasoning content."""

    type: Literal["thinking"] = "thinking"
    thinking: str = ""
    redacted: bool = False

    @property
    def display_emoji(self) -> str:
        return "💭"

    @property
    def display_label(self) -> str:
        return "Thinking"

    @property
    def display_body(self) -> str:
        return "[redacted]" if self.redacted else self.thinking


class LupToolUseBlock(LupContentBlock):
    """A tool invocation by the agent."""

    type: Literal["tool_use"] = "tool_use"
    id: str
    name: str
    input: JsonObject | None = None

    @property
    def display_emoji(self) -> str:
        return "🔧"

    @property
    def display_label(self) -> str:
        return f"Tool: {self.name}"

    @property
    def display_body(self) -> str:
        return json.dumps(self.input, indent=2) if self.input else ""

    @property
    def markdown_fence(self) -> str | None:
        return "json"

    @property
    def opens_pairing(self) -> str | None:
        return self.id

    @property
    def tool_call_name(self) -> str | None:
        return self.name

    def log_summary(self, body: str) -> str:
        arguments = json.dumps(self.input) if self.input else ""
        return f"TOOL_USE [{self.id}] {self.name}: {arguments}"


class LupToolResultBlock(LupContentBlock):
    """Result returned from a tool invocation."""

    type: Literal["tool_result"] = "tool_result"
    tool_use_id: str
    content: str | Sequence[object] | None = None

    @property
    def display_emoji(self) -> str:
        return "📋"

    @property
    def display_label(self) -> str:
        return "Result"

    @property
    def display_body(self) -> str:
        return normalize_content(self.content)

    @property
    def markdown_fence(self) -> str | None:
        return ""

    @property
    def closes_pairing(self) -> str | None:
        return self.tool_use_id

    @property
    def result_payload(self) -> str | Sequence[object] | None:
        return self.content

    def log_summary(self, body: str) -> str:
        return f"TOOL_RESULT [{self.tool_use_id}]: {body}"


class LupNativeActivityBlock(LupContentBlock):
    """Provider activity retained without presenting it as speech or a tool call."""

    type: Literal["native_activity"] = "native_activity"
    provider: str
    activity: str
    payload: JsonObject

    @property
    def display_label(self) -> str:
        return f"{self.provider}: {self.activity}"

    @property
    def display_body(self) -> str:
        return json.dumps(self.payload, indent=2)

    @property
    def markdown_fence(self) -> str | None:
        return "json"


# ---------------------------------------------------------------------------
# Subagent specification
# ---------------------------------------------------------------------------

type ModelTier = Literal["inherit", "frontier", "strongest", "balanced", "fast"]
"""Portable model preference for one role.

Runtimes name and version their own model lineups, so a declaration states the
need and each adapter spells whichever tier it can honor — or omits the choice
where it has no proven vocabulary to spell it in. One alias for every
declaration that states a preference, because a role's tier and a delegated
spec's tier are the same vocabulary: spelled twice, an adapter honouring one
copy would silently ignore whichever tier the other copy grew.

``frontier`` sits above ``strongest``: the newest model a runtime ships, where
``strongest`` is the established one work defaults to."""

type SessionEffort = Literal["low", "medium", "high", "xhigh", "max", "ultra"]
"""How hard a session is asked to think before it answers.

Every rung is one both runtimes' catalogs list, so none is narrowed on the
way to either: ``ultra`` is Codex's own top rung, and Claude's ``xhigh`` with
ultracode on. Nothing sits below ``low``, because neither catalog lists a
rung there — ``minimal`` and ``none`` left Codex's, and admitting either here
would turn "barely reason" into "reason a little" without saying so. Which
rungs one *model* takes is narrower still, and refused where it is declared.

Beside :data:`ModelTier` because a declaration naming work for a model names
both, and neither needs a runtime's catalog to be spelled.
"""


class CustomModel(BaseModel, frozen=True, extra="forbid"):
    """A model id outside a runtime's catalog, named as one on purpose.

    Every model field takes the runtime's catalog of names, so a misspelt id
    fails where it is written. An id the catalog does not list — a compatible
    endpoint's own model, a release newer than the catalog — is still a model
    a session can open with, and wrapping it says that leaving the catalog was
    the choice rather than the typo. Nothing is checked against it: its
    efforts are the endpoint's to refuse.
    """

    id: str = Field(min_length=1)


type SubagentCapability = Literal["workspace-read", "web-search"]
"""A provider-neutral facility a delegated role may use."""


class SubagentSpec(BaseModel, extra="forbid"):
    """Provider-neutral subagent definition used by injected factory recipes."""

    name: str
    description: str
    prompt: str
    capabilities: list[SubagentCapability] = []
    tools: list[ToolGrant] = []
    model: ModelTier = Field(
        default="strongest",
        description="Portable model tier for this subagent; inherit reuses the "
        "session's main model on every backend",
    )
    max_turns: int | None = Field(
        default=None,
        gt=0,
        description="Turn cap for delegated one-shot runs (None = backend default)",
    )


class Usage(BaseModel):
    """Portable token usage — the counts every backend can provide.

    Adapters produce this through a ``usage_normalizer`` callback supplied
    at construction, defaulting to each adapter's standard converter. A
    custom normalizer may return a *subclass* carrying vendor-specific
    fields (service tier, per-iteration costs, …); fields holding it are
    declared ``SerializeAsAny[Usage]`` so subclass data survives into
    session JSON.
    """

    input_tokens: int = Field(
        default=0,
        description=(
            "Complete input token count, cached reads and cache creation"
            " included; adapters normalize cache-exclusive native counts"
        ),
    )
    cost_usd: float | None = Field(
        default=None,
        description="Optional provider-reported complete cost for this usage span",
    )
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0


type UsageCost = Callable[[Usage], float]
"""Estimates the USD cost of accumulated token usage.

Adapters that report token counts but no cost take one of these to enforce a
budget; build it with :func:`lup.observability.cost.per_mtok_usage_cost`.
"""
