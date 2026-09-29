"""Where Claude Code writes its session transcripts, and what its words mean.

Three readers share the files. The watcher follows new bytes into a journal
as a launched session writes them (the :class:`NativeTranscripts` half). A
session's ``history()`` reads one conversation back as the portable messages
its turns produced. An agent's ``sessions()`` lists the conversations filed
under its workspace.

All three read under a configuration home passed in, never the one this
process's environment names. The Agent SDK's own readers consult only that
environment, so a session running under a home of its own — every derived
workspace home — would be read from the wrong place, and changing this
process's environment to point them elsewhere would move every other session
in it too.
"""

import json
import unicodedata
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from pydantic import BaseModel, Field, TypeAdapter, ValidationError

from lup.providers.claude.login import CLAUDE_LOGIN
from lup.observability.audit import ObservableEventKind
from lup.observability.native import (
    NativeRecordOrigin,
    NativeSemanticBlock,
    NativeTranscripts,
    NativeTurn,
    NativeTurns,
    blocks_by_type,
    first_string,
)
from lup.sessions.events import (
    AnyTurnBlock,
    SessionId,
    SessionSummary,
    TurnMessage,
    TurnNativeActivityBlock,
    TurnTextBlock,
    TurnThinkingBlock,
    TurnToolCallBlock,
    TurnToolResultBlock,
)
from lup.types import JsonObject, JsonValue

# lup: ignore[constant-declaration] — Claude Code's own wire spellings, which
# it chooses and this only reads
CLAUDE_BLOCK_SPELLINGS: dict[str, ObservableEventKind] = {
    "thinking": "reasoning",
    "tool_use": "tool_call",
    "tool_result": "tool_result",
    "usage": "usage",
}

# lup: ignore[constant-declaration] — the directory Claude Code itself writes to
CLAUDE_SESSIONS_DIR = "projects"
"""Claude Code files a session under the project it was started in."""

# lup: ignore[constant-declaration] — Claude Code's own limit on a directory name
PROJECT_NAME_LIMIT = 200
"""How long a project directory's name runs before Claude Code cuts it.

Beyond it the name is cut here and a hash of the whole path appended, and the
CLI and the SDK compute that hash differently — so a long one is found by the
part both agree on."""

# lup: ignore[constant-declaration] — the record kinds Claude Code links into a
# conversation, in its own words
LINKED_KINDS = ("user", "assistant", "progress", "system", "attachment")
"""The record kinds a conversation's parent links run through."""

JSON_OBJECT = TypeAdapter(JsonObject)


def project_name(workspace: Path) -> str:
    """The directory name Claude Code files a workspace's sessions under.

    Every character but an ASCII letter or digit becomes a hyphen, over the
    workspace's real path, the way the CLI names the directory.
    """
    real = unicodedata.normalize("NFC", str(workspace.resolve()))
    return "".join(char if char.isascii() and char.isalnum() else "-" for char in real)


def session_file_name(session: SessionId) -> str | None:
    """The transcript's file name, or ``None`` where the id is not one Claude mints.

    Claude Code names every session with a UUID. Anything else cannot name a
    transcript, and refusing it here keeps an arbitrary string out of a glob.
    """
    try:
        UUID(session.value)
    except ValueError:
        return None
    return f"{session.value}.jsonl"


def result_text(content: JsonValue) -> str:
    """A tool result's content as the text a portable block carries."""
    match content:
        case str():
            return content
        case None:
            return ""
        case _:
            return json.dumps(content, default=str)


class TranscriptThinking(BaseModel, frozen=True, extra="ignore"):
    thinking: str = ""
    signature: str = ""


class TranscriptToolCall(BaseModel, frozen=True, extra="ignore"):
    input: JsonObject = {}


class TranscriptToolResult(BaseModel, frozen=True, extra="ignore"):
    content: JsonValue = None
    is_error: bool | None = None


def transcript_block(block: JsonValue) -> AnyTurnBlock:
    """One content block of a transcript record, as the portable block it is.

    The same words the live session's blocks become, read from the file
    rather than from the SDK's objects; a kind with no portable shape is kept
    whole as the provider's own activity rather than flattened to text.
    """
    match block:
        case {"type": "text", "text": str(text)}:
            return TurnTextBlock(text=text)
        case {"type": "thinking"}:
            thought = TranscriptThinking.model_validate(block)
            return TurnThinkingBlock(
                thinking=thought.thinking,
                redacted=not thought.thinking and bool(thought.signature),
            )
        case {"type": "redacted_thinking"}:
            return TurnThinkingBlock(thinking="", redacted=True)
        case {
            "type": "tool_use" | "server_tool_use",
            "id": str(identifier),
            "name": str(name),
        }:
            call = TranscriptToolCall.model_validate(block)
            return TurnToolCallBlock(id=identifier, name=name, arguments=call.input)
        case {"type": str(), "tool_use_id": str(identifier)}:
            result = TranscriptToolResult.model_validate(block)
            return TurnToolResultBlock(
                tool_call_id=identifier,
                content=result_text(result.content),
                is_error=bool(result.is_error),
            )
        case {"type": str(kind)}:
            return TurnNativeActivityBlock(
                provider="claude",
                activity=kind,
                payload=JSON_OBJECT.validate_python(block),
            )
        case _:
            return TurnNativeActivityBlock(
                provider="claude", activity="unknown", payload={"value": block}
            )


def answers_a_tool(block: JsonValue) -> bool:
    """Whether a user record's block is a tool's result rather than a person's words."""
    match block:
        case {"type": "tool_result"}:
            return True
        case _:
            return False


class TranscriptMessage(BaseModel, frozen=True, extra="ignore"):
    content: JsonValue = None
    model: str | None = None
    id: str | None = None


class TranscriptEntry(BaseModel, frozen=True, extra="ignore"):
    """The fields of one transcript record this reader decides anything by."""

    type: str = ""
    uuid: str | None = None
    parent_uuid: str | None = Field(default=None, alias="parentUuid")
    is_sidechain: bool | None = Field(default=None, alias="isSidechain")
    is_meta: bool | None = Field(default=None, alias="isMeta")
    is_compact_summary: bool | None = Field(default=None, alias="isCompactSummary")
    team_name: str | None = Field(default=None, alias="teamName")
    cwd: str | None = None
    timestamp: datetime | None = None
    message: TranscriptMessage | None = None
    custom_title: str | None = Field(default=None, alias="customTitle")
    ai_title: str | None = Field(default=None, alias="aiTitle")

    def linked(self) -> bool:
        """Whether this record is a link of some conversation's chain."""
        return self.type in LINKED_KINDS and self.uuid is not None

    def spoken(self) -> bool:
        """Whether this record is a message, rather than bookkeeping around one."""
        return self.type in ("user", "assistant")

    def in_main_chain(self) -> bool:
        """Whether this record belongs to the conversation itself.

        A sidechain is a subagent's, a team name another agent's, and a meta
        record the CLI's own annotation; none of them is the conversation a
        person had.
        """
        return not (self.is_sidechain or self.team_name or self.is_meta)

    def visible(self) -> bool:
        """Whether this record is a message of the conversation itself."""
        return self.spoken() and self.in_main_chain()


class TranscriptRecord(BaseModel, frozen=True):
    """One record of a transcript: where it sits, what it says, and all of it."""

    position: int
    entry: TranscriptEntry
    native: JsonObject

    def message(self) -> TurnMessage | None:
        """This record as a portable message, where it is one."""
        spoken = self.entry.message
        content = spoken.content if spoken is not None else None
        match self.entry.type, content:
            case "user", str(text):
                return TurnMessage(
                    role="user", blocks=[TurnTextBlock(text=text)], native=self.native
                )
            case "user", list(blocks):
                answered = any(answers_a_tool(block) for block in blocks)
                return TurnMessage(
                    role="tool" if answered else "user",
                    blocks=[transcript_block(block) for block in blocks],
                    native=self.native,
                )
            case "assistant", list(blocks):
                return TurnMessage(
                    role="assistant",
                    blocks=[transcript_block(block) for block in blocks],
                    native=self.native,
                    model=spoken.model if spoken is not None else None,
                    message_id=spoken.id if spoken is not None else None,
                )
            case _:
                return None

    def prompt(self) -> str:
        """What a person typed in this record, or nothing where it is not a prompt."""
        if self.entry.type != "user" or not self.entry.in_main_chain():
            return ""
        if self.entry.is_compact_summary:
            return ""
        message = self.message()
        if message is None or message.role != "user":
            return ""
        return " ".join(
            text for block in message.blocks if (text := block.text_payload)
        ).strip()


def transcript_records(path: Path) -> list[TranscriptRecord]:
    """Every whole record in one transcript, in file order.

    A line that is not one JSON object is skipped rather than fatal: the CLI
    may be writing the last one as this reads it.
    """

    def records() -> Iterator[TranscriptRecord]:
        with path.open(encoding="utf-8") as lines:
            for position, line in enumerate(lines):
                if not line.strip():
                    continue
                try:
                    native = JSON_OBJECT.validate_json(line)
                    entry = TranscriptEntry.model_validate(native)
                except ValidationError:
                    continue
                yield TranscriptRecord(position=position, entry=entry, native=native)

    return list(records())


def conversation_chain(records: list[TranscriptRecord]) -> list[TranscriptRecord]:
    """The records of the conversation a transcript holds now, oldest first.

    A transcript is a tree: a rewind or an edited prompt leaves the old branch
    in the file and starts a new one. The conversation is the branch ending in
    the latest message of the main chain, walked back through each record's
    parent — the reading Claude Code's own resume makes.
    """
    linked = [record for record in records if record.entry.linked()]
    by_uuid = {record.entry.uuid: record for record in linked}
    parents = {
        record.entry.parent_uuid for record in linked if record.entry.parent_uuid
    }

    def ancestry(start: TranscriptRecord) -> Iterator[TranscriptRecord]:
        # lup: ignore[set-shape, empty-collection] — a walk's own visited marks
        seen: set[str] = set()
        current: TranscriptRecord | None = start
        while current is not None and current.entry.uuid not in seen:
            if current.entry.uuid is not None:
                seen.add(current.entry.uuid)
            yield current
            parent = current.entry.parent_uuid
            current = by_uuid.get(parent) if parent is not None else None

    ends = [record for record in linked if record.entry.uuid not in parents]
    leaves = [
        spoken
        for end in ends
        if (spoken := next((r for r in ancestry(end) if r.entry.spoken()), None))
        is not None
    ]
    if not leaves:
        return []
    main = [leaf for leaf in leaves if leaf.entry.in_main_chain()]
    leaf = max(main or leaves, key=lambda record: record.position)
    return list(reversed(list(ancestry(leaf))))


class ClaudeTranscripts(NativeTranscripts):
    """Read Claude Code's persisted session records under one configuration home."""

    def __init__(self, config_home: Path | None = None) -> None:
        self.config_home = config_home or CLAUDE_LOGIN.ambient_home

    def roots(self) -> list[Path]:
        return [self.config_home / CLAUDE_SESSIONS_DIR]

    def belongs_to(self, record: JsonObject) -> NativeRecordOrigin:
        # Claude Code stamps `cwd` per record and spells the session camelCase,
        # carrying the same identifier into the fresh transcript a directory
        # change opens.
        directory = first_string(record, "cwd")
        return NativeRecordOrigin(
            directory=Path(directory) if directory is not None else None,
            session=first_string(record, "sessionId"),
        )

    def semantic_blocks(self, record: JsonObject) -> list[NativeSemanticBlock]:
        return blocks_by_type(record, CLAUDE_BLOCK_SPELLINGS)

    def project_directories(self, workspace: Path) -> list[Path]:
        """The directories this home files the workspace's sessions under."""
        root = self.config_home / CLAUDE_SESSIONS_DIR
        named = project_name(workspace)
        if len(named) <= PROJECT_NAME_LIMIT:
            exact = root / named
            return [exact] if exact.is_dir() else []
        # lup: ignore[silent-truncation] — the length Claude Code cuts the name to
        agreed = named[:PROJECT_NAME_LIMIT]
        return sorted(path for path in root.glob(f"{agreed}-*") if path.is_dir())

    def transcript(self, session: SessionId, workspace: Path | None) -> Path | None:
        """The file one conversation is kept in, or ``None`` where there is none.

        Looked for under the workspace first, and then under every project,
        because a session filed under the directory it started in keeps its
        file there after its working directory moves.
        """
        name = session_file_name(session)
        if name is None:
            return None
        near = self.project_directories(workspace) if workspace is not None else []
        candidates = [
            *(directory / name for directory in near),
            *sorted((self.config_home / CLAUDE_SESSIONS_DIR).glob(f"*/{name}")),
        ]
        return next((path for path in candidates if path.is_file()), None)

    def conversation(
        self, session: SessionId, workspace: Path | None
    ) -> list[TurnMessage] | None:
        """Every message of one conversation, or ``None`` where it has no transcript."""
        path = self.transcript(session, workspace)
        if path is None:
            return None
        return [
            message
            for record in conversation_chain(transcript_records(path))
            if record.entry.visible() and (message := record.message()) is not None
        ]

    def sessions(self, workspace: Path) -> list[SessionSummary]:
        """Every conversation filed under this workspace, newest first."""
        summaries = [
            summary
            for directory in self.project_directories(workspace)
            for path in sorted(directory.glob("*.jsonl"))
            if (summary := session_summary(path)) is not None
        ]
        return sorted(summaries, key=lambda summary: summary.updated_at, reverse=True)


class ClaudeTurns(NativeTurns):
    """Read one Claude Code transcript line as a turn, and find a subagent's transcript."""

    def turn(self, record: JsonObject) -> NativeTurn | None:
        try:
            entry = TranscriptEntry.model_validate(record)
        except ValidationError:
            return None
        if not entry.spoken() or entry.is_meta:
            return None
        message = TranscriptRecord(position=0, entry=entry, native=record).message()
        return NativeTurn(message=message, at=entry.timestamp) if message else None

    def subagent(self, transcript: Path, agent: str) -> Path | None:
        # Measured on 2.1.283: a subagent's records go to a file of its own
        # beside the session's, named for the subagent's id.
        kept = transcript.with_suffix("") / "subagents" / f"agent-{agent}.jsonl"
        return kept if kept.is_file() else None


def session_summary(path: Path) -> SessionSummary | None:
    """One transcript as a listing shows it, or ``None`` where it is no conversation.

    A subagent's transcript is its parent's to show, and a file holding no
    message at all is bookkeeping; neither is listed. A title a person gave
    wins over one the CLI generated.
    """
    if session_file_name(SessionId(value=path.stem)) is None:
        return None
    records = transcript_records(path)
    if not records or records[0].entry.is_sidechain:
        return None
    if not any(record.entry.spoken() for record in records):
        return None
    given = [
        record.entry.custom_title for record in records if record.entry.custom_title
    ]
    generated = [record.entry.ai_title for record in records if record.entry.ai_title]
    stamps = [record.entry.timestamp for record in records if record.entry.timestamp]
    directory = next((record.entry.cwd for record in records if record.entry.cwd), None)
    return SessionSummary(
        id=SessionId(value=path.stem),
        title=(given or generated or [None])[-1],
        preview=next((text for record in records if (text := record.prompt())), ""),
        cwd=Path(directory) if directory is not None else None,
        created_at=stamps[0] if stamps else None,
        updated_at=datetime.fromtimestamp(path.stat().st_mtime, tz=UTC),
    )
