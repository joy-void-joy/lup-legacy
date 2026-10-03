"""Retain Claude conversations through an authenticated browser session."""

import hashlib
import json
import logging
import mimetypes
import tempfile
from datetime import UTC, datetime
from http.cookies import SimpleCookie
from pathlib import Path, PurePosixPath
from typing import ClassVar
from urllib.parse import quote

import httpx
from pydantic import BaseModel, TypeAdapter, ValidationError

from lup.types import JsonObject, JsonValue, StringMap
from lup.devtools.conversation.common import (
    Address,
    Payload,
    attachment_label,
    install_delivery,
)
from lup.devtools.conversation.errors import ConversationDownloadError

logger = logging.getLogger(__name__)

CLAUDE_ORIGIN = "https://claude.ai"
# lup: ignore[constant-declaration] — this provider flag requests complete blocks
RENDER_QUERY = "rendering_mode=messages&render_all_tools=true"


class ClaudeDownloadError(ConversationDownloadError):
    """A Claude conversation could not be retained faithfully."""


class ClaudeAuthenticationRequired(ClaudeDownloadError):
    """A Claude conversation needs a fresh browser login."""


class ConversationReference(Address, frozen=True):
    """An authenticated Claude chat URL or public share URL."""

    service: ClassVar[str] = "Claude"
    origin: ClassVar[str] = CLAUDE_ORIGIN
    hosts: ClassVar[tuple[str, ...]] = ("claude.ai", "www.claude.ai")
    conversation_segment: ClassVar[str] = "chat"
    refused: ClassVar[type[ConversationDownloadError]] = ClaudeDownloadError


class Organization(Payload, frozen=True):
    """One Claude organization visible to the browser session."""

    uuid: str
    capabilities: list[str] = []

    def chat_capable(self) -> bool:
        """Whether this organization serves chat conversations."""
        return "chat" in self.capabilities


class Attachment(Payload, frozen=True):
    """One uploaded file and the extraction Claude returns for it."""

    id: str = ""
    file_name: str = ""
    file_size: int = 0
    file_type: str = ""
    extracted_content: str = ""

    def extension(self) -> str:
        """The suffix implied by this attachment's declared type."""
        if guessed := mimetypes.guess_extension(self.file_type):
            return guessed
        return f".{self.file_type}" if self.file_type else ""

    def stored_name(self) -> str:
        """A safe single-component name for this attachment."""
        if "\x00" in self.file_name:
            raise ClaudeDownloadError("Attachment filename contains a null byte")
        return PurePosixPath(self.file_name).name or f"attachment{self.extension()}"

    def relative_path(self) -> Path:
        """The collision-proof path occupied by this attachment."""
        identifier = PurePosixPath(self.id).name
        if (
            not identifier
            or identifier != self.id
            or not all(
                character.isalnum() or character in {"-", "_"}
                for character in identifier
            )
        ):
            raise ClaudeDownloadError(
                f"Attachment {self.stored_name()!r} has no path-safe file id"
            )
        return Path("attachments") / identifier / self.stored_name()


def selected_attachment(attachments: list["Attachment"], artifact: str) -> "Attachment":
    """The one declared attachment a ``:artifact`` selector names."""
    matched = [
        item for item in attachments if artifact in {item.stored_name(), item.id} - {""}
    ]
    match matched:
        case [only]:
            return only
        case []:
            offered = ", ".join(sorted(item.stored_name() for item in attachments))
            raise ClaudeDownloadError(
                f"This conversation declares no artifact {artifact!r}. "
                f"It offers: {offered or '(no files)'}"
            )
        case ambiguous:
            raise ClaudeDownloadError(
                f"Artifact {artifact!r} names {len(ambiguous)} files here; "
                "select one by its file id instead: "
                + ", ".join(sorted(item.id for item in ambiguous))
            )


class BlockMetadata(Payload, frozen=True):
    """Where a page pulled into a Claude response came from."""

    site_name: str = ""
    site_domain: str = ""

    def describe(self) -> str:
        """The page's source site, when the payload names one."""
        return self.site_name or self.site_domain


class ContentBlock(Payload, frozen=True):
    """One Claude message block, including tool and structured-data shapes."""

    type: str = ""
    text: str = ""
    content: str | list["ContentBlock"] = ""
    source: str = ""
    name: str = ""
    integration_name: str = ""
    input: JsonObject | None = None
    title: str = ""
    url: str = ""
    is_missing: bool = False
    metadata: BlockMetadata | None = None
    file_path: str = ""
    table: JsonValue = None
    json_block: JsonValue = None
    is_error: bool = False
    message: str = ""
    display_content: JsonValue = None

    def call_text(self) -> str:
        """A tool call with its integration and complete arguments."""
        via = f" via {self.integration_name}" if self.integration_name else ""
        called = f"[tool call: {self.name or '(unnamed tool)'}{via}]"
        if self.input is None:
            return f"{called}\n(the payload carried no arguments for this call)"
        return f"{called}\n{json.dumps(self.input, indent=2, ensure_ascii=False)}"

    def fetched_text(self) -> str:
        """One web result with its title, URL, and source site."""
        site = self.metadata.describe() if self.metadata is not None else ""
        where = " — ".join(part for part in (self.url, site) if part)
        missing = " (not retrieved)" if self.is_missing else ""
        titled = f"[web result: {self.title or '(untitled)'}{missing}]"
        return f"{titled}\n{where}" if where else titled

    def nested(self) -> str:
        """Every textual or structured part of a tool result."""
        match self.content:
            case str() as held:
                body = held
            case blocks:
                body = "\n\n".join(
                    text for block in blocks if (text := block.text_payload())
                )
        match self.display_content:
            case str() as shown:
                displayed = shown
            case None:
                displayed = ""
            case shown:
                displayed = json.dumps(shown, indent=2, ensure_ascii=False, default=str)
        failed = "[the tool reported an error]" if self.is_error else ""
        return "\n\n".join(
            part for part in (failed, body, self.message, displayed) if part
        )

    def text_payload(self) -> str:
        """What this block says without silently erasing unfamiliar content."""
        match self.type:
            case "image":
                return "[an image was in the conversation here; see conversation.json]"
            case "tool_use":
                return self.call_text()
            case "tool_result":
                return self.nested()
            case "knowledge":
                return self.fetched_text()
            case "local_resource":
                where = f" — {self.file_path}" if self.file_path else ""
                return f"[file: {self.name or '(unnamed)'}{where}]"
            case "table" | "json_block":
                held = self.table if self.table is not None else self.json_block
                return f"[{self.type}]\n" + json.dumps(
                    held, indent=2, ensure_ascii=False, default=str
                )
            case _:
                inline = self.content if isinstance(self.content, str) else ""
                if value := next(
                    (value for value in (self.text, inline, self.source) if value), ""
                ):
                    return value
                kind = self.type or "unknown"
                return f"[unrendered {kind} block; inspect conversation.json]"


class Message(Payload, frozen=True):
    """One Claude turn with its content, files, and completeness markers."""

    uuid: str = ""
    sender: str = ""
    content: str | list[ContentBlock] = ""
    attachments: list[Attachment] = []
    file_count: int = 0
    image_count: int = 0
    truncated: bool = False
    compaction_summary: str = ""

    def blocks(self) -> list[str]:
        """Every readable content block in this message."""
        match self.content:
            case str():
                return [self.content] if self.content else []
            case blocks:
                return [text for block in blocks if (text := block.text_payload())]

    def uploads(self, retained: dict[str, Path]) -> list[str]:
        """Every attachment, pointing to its extraction or to its absence."""
        return [
            attachment_label(attachment.stored_name(), retained.get(attachment.id))
            for attachment in self.attachments
        ]

    def completeness_notes(self) -> list[str]:
        """Provider-reported gaps that the rendered text must make visible."""
        unrepresented = max(self.file_count - len(self.attachments), 0)
        return [
            note
            for note in (
                f"[Claude reports {unrepresented} additional uploaded file(s); "
                "inspect conversation.json]"
                if unrepresented
                else "",
                f"[Claude reports {self.image_count} image(s); inspect conversation.json]"
                if self.image_count
                else "",
                f"[the service compacted this message, summarising it as]\n"
                f"{self.compaction_summary}"
                if self.compaction_summary.strip()
                else "",
                "[the service reports this message as truncated]"
                if self.truncated
                else "",
            )
            if note
        ]

    def spoken(self, retained: dict[str, Path]) -> str:
        """This turn as a speaker-tagged Markdown span."""
        body = "\n\n".join(
            self.blocks() + self.uploads(retained) + self.completeness_notes()
        )
        if not body.strip():
            return ""
        speaker = "user" if self.sender == "human" else "claude"
        return f"<{speaker}>\n{body}\n</{speaker}>"


class ConversationPayload(Payload, frozen=True):
    """A Claude conversation or share snapshot in its common wire shape."""

    uuid: str = ""
    name: str = ""
    snapshot_name: str = ""
    created_at: str = ""
    updated_at: str = ""
    chat_messages: list[Message] = []

    def title(self) -> str:
        """The title field used by either route."""
        return self.name or self.snapshot_name

    def retained_paths(self, artifact: str = "") -> dict[str, Path]:
        """Where each held extraction goes: every one, or the selected one."""
        declared = self.attachments()
        held = [selected_attachment(declared, artifact)] if artifact else declared
        return {attachment.id: attachment.relative_path() for attachment in held}

    def rendered_messages(self, retained: dict[str, Path]) -> list[str]:
        """Every readable speaker-tagged message."""
        messages = [
            text for message in self.chat_messages if (text := message.spoken(retained))
        ]
        if not messages:
            raise ClaudeDownloadError("Claude conversation has no readable messages")
        return messages

    def attachments(self) -> list[Attachment]:
        """Every distinct attachment anywhere in the payload."""
        attachments = [
            attachment
            for message in self.chat_messages
            for attachment in message.attachments
        ]
        missing = next((item for item in attachments if not item.id), None)
        if missing is not None:
            raise ClaudeDownloadError(
                f"Attachment {missing.stored_name()!r} has no file id"
            )
        found = {attachment.id: attachment for attachment in attachments}
        conflicting = next(
            (
                identifier
                for identifier, retained in found.items()
                if any(
                    candidate.id == identifier and candidate != retained
                    for candidate in attachments
                )
            ),
            None,
        )
        if conflicting is not None:
            raise ClaudeDownloadError(
                f"Claude reused attachment id {conflicting!r} for different files"
            )
        return list(found.values())


class AttachmentRecord(BaseModel, frozen=True):
    """The retained identity and digest of one Claude attachment extraction."""

    id: str
    name: str
    mime_type: str
    source_size_bytes: int
    relative_path: str
    retained_size_bytes: int
    sha256: str
    representation: str = "claude_api_extracted_text"


class DownloadManifest(BaseModel, frozen=True):
    """A replayable account of one retained Claude delivery.

    ``selected_artifact`` names the ``:artifact`` selector that narrowed this
    download, and is empty for a complete one. ``declared_attachment_count``
    is what the payload declares, so a delivery holding fewer files than it
    counts cannot be mistaken for a complete one.
    """

    provider: str = "claude"
    source_url: str
    fetched_at: str
    title: str
    route: str
    identifier: str
    message_count: int
    reported_file_count: int
    reported_image_count: int
    selected_artifact: str = ""
    declared_attachment_count: int = 0
    attachments: list[AttachmentRecord]


def conversation_from(payload: JsonValue) -> ConversationPayload:
    """Validate the complete Claude conversation payload."""
    try:
        conversation = ConversationPayload.model_validate(payload)
    except ValidationError as error:
        raise ClaudeDownloadError(
            "Claude conversation payload changed shape"
        ) from error
    conversation.rendered_messages(conversation.retained_paths())
    return conversation


def render_conversation(
    reference: ConversationReference,
    conversation: ConversationPayload,
    retained: dict[str, Path],
) -> str:
    """Render all messages beside the untouched provider payload.

    ``retained`` carries the path of every attachment this delivery holds,
    which is every declared one unless a ``:artifact`` selector narrowed it.
    """
    title = conversation.title() or "Untitled Claude conversation"
    messages = "\n\n".join(conversation.rendered_messages(retained))
    return (
        f"# {title}\n\n"
        f"Source: {reference.page_url()}\n\n"
        "The untouched service payload is in `conversation.json`; uploaded files "
        "are retained as the extracted content Claude supplied.\n\n"
        f"{messages}\n"
    )


def request_headers(cookie: str = "", referer: str = "") -> StringMap:
    """Browser-shaped headers for Claude's conversation endpoints."""
    headers: StringMap = {
        "Accept": "application/json",
        "Origin": CLAUDE_ORIGIN,
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "Chrome/137.0 Safari/537.36"
        ),
    }
    if cookie:
        headers["Cookie"] = cookie
    if referer:
        headers["Referer"] = referer
    return headers


def response_json(response: httpx.Response) -> JsonValue:
    """Validate one Claude response as JSON-shaped data."""
    try:
        return TypeAdapter(JsonValue).validate_python(response.json())
    except (ValueError, TypeError) as error:
        raise ClaudeDownloadError(
            f"Claude returned non-JSON data from {response.url}"
        ) from error


def organization_hint(cookie: str) -> str:
    """The last active organization carried by the browser cookie."""
    parsed = SimpleCookie()
    try:
        parsed.load(cookie)
    except ValueError:
        return ""
    return parsed["lastActiveOrg"].value if "lastActiveOrg" in parsed else ""


async def organizations(client: httpx.AsyncClient, cookie: str) -> list[Organization]:
    """Every organization visible to the authenticated Claude session."""
    if not cookie:
        raise ClaudeAuthenticationRequired("No Claude browser session is available")
    response = await client.request(
        "GET",
        f"{CLAUDE_ORIGIN}/api/organizations",
        headers=request_headers(cookie),
    )
    if response.status_code in {401, 403}:
        raise ClaudeAuthenticationRequired("The Claude browser session has expired")
    if response.status_code != 200:
        raise ClaudeDownloadError(
            f"Claude organization lookup returned HTTP {response.status_code}"
        )
    payload = response_json(response)
    if not isinstance(payload, list):
        raise ClaudeDownloadError("Claude organizations payload changed shape")
    try:
        return TypeAdapter(list[Organization]).validate_python(payload)
    except ValidationError as error:
        raise ClaudeDownloadError(
            "Claude organizations payload changed shape"
        ) from error


def ordered_organizations(entries: list[Organization], cookie: str) -> list[str]:
    """Chat-capable organizations, preferring the browser's active one."""
    chatting = [entry.uuid for entry in entries if entry.chat_capable()]
    available = chatting or [entry.uuid for entry in entries]
    if not available:
        raise ClaudeDownloadError("This Claude account has no organizations")
    hint = organization_hint(cookie)
    return ([hint] if hint in available else []) + [
        identifier for identifier in available if identifier != hint
    ]


async def fetch_authenticated(
    client: httpx.AsyncClient,
    reference: ConversationReference,
    cookie: str,
    organization: str,
) -> httpx.Response:
    """Fetch one organization-scoped conversation or snapshot."""
    identifier = quote(reference.identifier(), safe="")
    if reference.route() == "share":
        path = f"chat_snapshots/{identifier}"
    else:
        path = f"chat_conversations/{identifier}"
    return await client.request(
        "GET",
        f"{CLAUDE_ORIGIN}/api/organizations/{organization}/{path}?{RENDER_QUERY}",
        headers=request_headers(cookie, reference.page_url()),
    )


async def fetch_payload(reference: ConversationReference, cookie: str) -> JsonValue:
    """Fetch a complete Claude payload, anonymously or through browser cookies."""
    async with httpx.AsyncClient(timeout=30) as client:
        if reference.route() == "share":
            public = await client.request(
                "GET",
                f"{CLAUDE_ORIGIN}/api/chat_snapshots/"
                f"{quote(reference.identifier(), safe='')}?{RENDER_QUERY}",
                headers=request_headers(referer=reference.page_url()),
            )
            if public.status_code == 200:
                return response_json(public)
        entries = await organizations(client, cookie)
        refusals: tuple[str, ...] = ()
        for organization in ordered_organizations(entries, cookie):
            response = await fetch_authenticated(
                client, reference, cookie, organization
            )
            if response.status_code == 200:
                return response_json(response)
            if response.status_code in {401, 403} and reference.route() != "share":
                raise ClaudeAuthenticationRequired(
                    "The Claude browser session was rejected"
                )
            refusals += (f"HTTP {response.status_code} in {organization}",)
    if refusals and all(
        refusal.startswith(("HTTP 401 ", "HTTP 403 ")) for refusal in refusals
    ):
        raise ClaudeAuthenticationRequired("The Claude browser session was rejected")
    raise ClaudeDownloadError(
        f"Claude refused {reference.page_url()}: {', '.join(refusals)}"
    )


def write_delivery(
    root: Path,
    reference: ConversationReference,
    raw: JsonValue,
    conversation: ConversationPayload,
    output: Path | None = None,
    artifact: str = "",
) -> Path:
    """Atomically replace one Claude delivery with the API response's files."""
    retained = conversation.retained_paths(artifact)
    workspace = (
        (output if output is not None else root / "tmp" / "conversations") / "claude"
    ).resolve()
    destination = workspace / reference.identifier()
    if not destination.resolve().is_relative_to(workspace):
        raise ClaudeDownloadError(
            "Conversation destination escapes tmp/conversations/claude"
        )
    workspace.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=".claude-staging-", dir=workspace
    ) as temporary:
        staged = Path(temporary) / reference.identifier()
        staged.mkdir(parents=True)
        (staged / "conversation.json").write_text(
            json.dumps(raw, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        (staged / "conversation.md").write_text(
            render_conversation(reference, conversation, retained), encoding="utf-8"
        )
        records: tuple[AttachmentRecord, ...] = ()
        for attachment in conversation.attachments():
            relative = retained.get(attachment.id)
            if relative is None:
                continue
            body = attachment.extracted_content.encode()
            path = staged / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(body)
            records += (
                AttachmentRecord(
                    id=attachment.id,
                    name=attachment.stored_name(),
                    mime_type=attachment.file_type,
                    source_size_bytes=attachment.file_size,
                    relative_path=relative.as_posix(),
                    retained_size_bytes=len(body),
                    sha256=hashlib.sha256(body).hexdigest(),
                ),
            )
        manifest = DownloadManifest(
            source_url=reference.page_url(),
            fetched_at=datetime.now(UTC).isoformat(),
            title=conversation.title(),
            route=reference.route(),
            identifier=reference.identifier(),
            message_count=len(conversation.chat_messages),
            reported_file_count=sum(
                message.file_count for message in conversation.chat_messages
            ),
            reported_image_count=sum(
                message.image_count for message in conversation.chat_messages
            ),
            selected_artifact=artifact,
            declared_attachment_count=len(conversation.attachments()),
            attachments=list(records),
        )
        (staged / "manifest.json").write_text(
            manifest.model_dump_json(indent=2) + "\n", encoding="utf-8"
        )
        install_delivery(
            staged,
            destination,
            Path(temporary) / "prior",
            ConversationReference.service,
            ClaudeDownloadError,
        )
    return destination


async def download_claude(
    reference: ConversationReference,
    *,
    root: Path,
    cookie: str,
    output: Path,
    artifact: str = "",
) -> Path:
    """Fetch and retain one Claude conversation and its attachment extracts.

    An ``artifact`` selector retains that one extraction instead of every
    declared one; the transcript, the raw payload, and the manifest are
    retained either way, and every extraction Claude supplied is in the raw
    payload whether or not this delivery wrote it out as a file.
    """
    raw = await fetch_payload(reference, cookie)
    conversation = conversation_from(raw)
    return write_delivery(root, reference, raw, conversation, output, artifact)
