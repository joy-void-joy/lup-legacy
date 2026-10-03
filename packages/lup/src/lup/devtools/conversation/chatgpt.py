"""Retain complete ChatGPT conversations through an authenticated browser."""

import hashlib
import json
import logging
import mimetypes
import tempfile
from datetime import UTC, datetime
from functools import cache
from itertools import count
from pathlib import Path, PurePosixPath
from typing import ClassVar, Literal
from urllib.parse import quote, unquote, urljoin, urlparse

from markdown_it import MarkdownIt
from playwright.async_api import (
    APIRequestContext,
    APIResponse,
    Error as PlaywrightError,
)
from pydantic import (
    AliasChoices,
    BaseModel,
    Field,
    SecretStr,
    TypeAdapter,
)

from lup.types import JsonValue
from lup.devtools.conversation.common import (
    Address,
    Payload,
    attachment_label,
    install_delivery,
)
from lup.devtools.conversation.errors import ConversationDownloadError

logger = logging.getLogger(__name__)


class ChatGPTDownloadError(ConversationDownloadError):
    """A ChatGPT conversation could not be retained completely."""


class ChatGPTAuthenticationRequired(ChatGPTDownloadError):
    """An ordinary conversation needs a fresh ChatGPT browser login."""


class ChatGPTSession(Payload, frozen=True):
    """The bearer credential returned for one authenticated browser session."""

    access_token: SecretStr = Field(
        default=SecretStr(""), validation_alias="accessToken"
    )

    # lup: ignore[dict-str-payload] — Playwright requires an open HTTP header map
    def headers_for(self, value: str) -> dict[str, str]:
        """Authorize only requests sent to ChatGPT's exact web origin."""
        parsed = urlparse(urljoin("https://chatgpt.com", value))
        if parsed.scheme != "https" or parsed.hostname != "chatgpt.com":
            return {}
        return {"Authorization": f"Bearer {self.access_token.get_secret_value()}"}


class ConversationReference(Address, frozen=True):
    """An authenticated conversation URL or a public shared snapshot URL."""

    service: ClassVar[str] = "ChatGPT"
    origin: ClassVar[str] = "https://chatgpt.com"
    hosts: ClassVar[tuple[str, ...]] = (
        "chatgpt.com",
        "www.chatgpt.com",
        "chat.openai.com",
    )
    conversation_segment: ClassVar[str] = "c"
    refused: ClassVar[type[ConversationDownloadError]] = ChatGPTDownloadError

    def api_urls(self) -> list[str]:
        """The web endpoints known to serve this page's complete payload."""
        identifier = quote(self.identifier(), safe="")
        if self.route() == "share":
            return [
                f"https://chatgpt.com/backend-api/share/{identifier}",
                f"https://chatgpt.com/backend-api/shared_conversation/{identifier}",
            ]
        return [f"https://chatgpt.com/backend-api/conversation/{identifier}"]

    def interpreter_download_url(self, conversation_id: str = "") -> str:
        """The authenticated endpoint resolving one visible sandbox file."""
        identifier = conversation_id or self.identifier()
        if not identifier or not all(
            character.isalnum() or character in {"-", "_"} for character in identifier
        ):
            raise ChatGPTDownloadError("ChatGPT conversation id is malformed")
        return (
            "https://chatgpt.com/backend-api/conversation/"
            f"{quote(identifier, safe='')}/interpreter/download"
        )


class AttachmentIdentity(BaseModel, frozen=True):
    """The provider coordinates distinguishing one visible file."""

    representation: Literal["metadata", "sandbox"]
    identifier: str = ""
    message_id: str = ""
    sandbox_path: str = ""


class SandboxDownloadParameters(BaseModel, frozen=True):
    """The query identifying one code-interpreter file."""

    message_id: str
    sandbox_path: str


class AttachmentRequest(BaseModel, frozen=True):
    """One provider request capable of resolving a visible file."""

    url: str
    params: SandboxDownloadParameters | None = None


class ChatGPTAttachment(Payload, frozen=True):
    """One file referenced by a conversation message."""

    representation: Literal["metadata", "sandbox"] = "metadata"
    identifier: str = Field(default="", validation_alias=AliasChoices("id", "file_id"))
    name: str = Field(default="", validation_alias=AliasChoices("name", "file_name"))
    mime_type: str = ""
    size_bytes: int = Field(
        default=0, validation_alias=AliasChoices("size_bytes", "size")
    )
    download_url: str = ""
    message_id: str = ""
    sandbox_path: str = ""

    def stored_name(self) -> str:
        """A single safe filename, with a useful fallback."""
        supplied = self.sandbox_path if self.representation == "sandbox" else self.name
        if "\x00" in supplied:
            raise ChatGPTDownloadError("Attachment filename contains a null byte")
        named = PurePosixPath(supplied).name
        if named == "..":
            raise ChatGPTDownloadError("Attachment filename selects its parent")
        if named:
            return named
        extension = mimetypes.guess_extension(self.mime_type) or ""
        return f"attachment{extension}"

    def identity(self) -> AttachmentIdentity:
        """A stable provider identity for deduplication and path allocation."""
        match self.representation:
            case "sandbox":
                path = PurePosixPath(self.sandbox_path)
                root = PurePosixPath("/mnt/data")
                if (
                    not self.message_id
                    or not path.is_absolute()
                    or not path.is_relative_to(root)
                    or path == root
                    or ".." in path.parts
                ):
                    raise ChatGPTDownloadError("Sandbox attachment path is malformed")
                return AttachmentIdentity(
                    representation=self.representation,
                    message_id=self.message_id,
                    sandbox_path=path.as_posix(),
                )
            case "metadata":
                if not self.identifier or not all(
                    character.isalnum() or character in {"-", "_"}
                    for character in self.identifier
                ):
                    raise ChatGPTDownloadError(
                        f"Attachment {self.stored_name()!r} has no safe file id"
                    )
                return AttachmentIdentity(
                    representation=self.representation, identifier=self.identifier
                )

    def requests(
        self, reference: ConversationReference, conversation_id: str = ""
    ) -> list[AttachmentRequest]:
        """Provider requests for this file, in preferred fallback order."""
        self.identity()
        match self.representation:
            case "sandbox":
                return [
                    AttachmentRequest(
                        url=reference.interpreter_download_url(conversation_id),
                        params=SandboxDownloadParameters(
                            message_id=self.message_id,
                            sandbox_path=self.sandbox_path,
                        ),
                    )
                ]
            case "metadata":
                identifier = quote(self.identifier, safe="")
                urls = [
                    self.download_url,
                    f"/backend-api/files/{identifier}/download",
                    f"/backend-api/files/{identifier}",
                ]
                return [AttachmentRequest(url=url) for url in urls if url]

    def relative_path(self) -> Path:
        """The direct named path preferred when this filename is unique."""
        return Path("attachments") / self.stored_name()


class ChatGPTMetadata(Payload, frozen=True):
    """The message metadata fields needed to retain uploaded files."""

    attachments: list[ChatGPTAttachment] = []


class ChatGPTContent(Payload, frozen=True):
    """The typed envelope around one message's content parts."""

    content_type: str = ""
    parts: list[JsonValue] = []
    text: str = ""


class ChatGPTAuthor(Payload, frozen=True):
    """Who authored one ChatGPT message."""

    role: str = "unknown"


class ChatGPTMessage(Payload, frozen=True):
    """One node's message, including its attachment declarations."""

    id: str = ""
    author: ChatGPTAuthor = ChatGPTAuthor()
    content: ChatGPTContent = ChatGPTContent()
    metadata: ChatGPTMetadata = ChatGPTMetadata()

    def attachments(self) -> list[ChatGPTAttachment]:
        """Files presented by this user-visible message."""
        if self.author.role == "tool":
            return []
        found = list(self.metadata.attachments)
        if self.content.content_type not in {"text", "multimodal_text"}:
            return found
        fragments = [part for part in self.content.parts if isinstance(part, str)]
        if self.content.text:
            fragments.append(self.content.text)
        for fragment in fragments:
            tokens = MarkdownIt("commonmark").parse(fragment)
            inline = [child for token in tokens for child in token.children or []]
            for token in inline:
                match token.type:
                    case "link_open":
                        target = token.attrGet("href")
                    case "image":
                        target = token.attrGet("src")
                    case _:
                        continue
                if not isinstance(target, str):
                    continue
                parsed = urlparse(target)
                if parsed.scheme != "sandbox":
                    continue
                found.append(
                    ChatGPTAttachment(
                        representation="sandbox",
                        message_id=self.id,
                        sandbox_path=unquote(parsed.path),
                    )
                )
        return found


class ChatGPTNode(Payload, frozen=True):
    """One node in ChatGPT's branched conversation mapping."""

    id: str = ""
    parent: str | None = None
    children: list[str] = []
    message: ChatGPTMessage | None = None


class ChatGPTConversation(Payload, frozen=True):
    """The complete mapping and selected branch returned for one conversation."""

    title: str = ""
    conversation_id: str = ""
    create_time: float | None = None
    update_time: float | None = None
    mapping: dict[str, ChatGPTNode]
    current_node: str

    def active_messages(self) -> list[ChatGPTMessage]:
        """Messages on the selected branch, root to current node."""
        current = self.current_node
        visited: tuple[str, ...] = ()
        messages: tuple[ChatGPTMessage, ...] = ()
        while current:
            if current in visited:
                raise ChatGPTDownloadError("Conversation mapping contains a cycle")
            visited += (current,)
            if current not in self.mapping:
                raise ChatGPTDownloadError(
                    f"Conversation mapping omits active node {current}"
                )
            node = self.mapping[current]
            if node.message is not None:
                messages = (node.message, *messages)
            current = node.parent or ""
        if not messages:
            raise ChatGPTDownloadError("Conversation has no readable messages")
        return list(messages)

    def attachments(self) -> list[ChatGPTAttachment]:
        """Every distinct user-visible file anywhere in the complete mapping."""
        attachments = [
            attachment
            for node in self.mapping.values()
            if node.message is not None
            for attachment in node.message.attachments()
        ]
        return list(
            {attachment.identity(): attachment for attachment in attachments}.values()
        )


def attachment_paths(
    attachments: list[ChatGPTAttachment],
) -> dict[AttachmentIdentity, Path]:
    """Allocate direct filenames, adding a familiar numeric suffix on collision."""

    @cache
    def selected_name(position: int) -> str:
        """Name one file against every preceding allocation."""
        attachment = attachments[position]
        name = attachment.stored_name()
        occupied = {selected_name(index) for index in range(position)}
        if name not in occupied:
            return name
        supplied = Path(name)
        candidates = (
            f"{supplied.stem} ({index}){supplied.suffix}" for index in count(2)
        )
        return next(candidate for candidate in candidates if candidate not in occupied)

    return {
        attachment.identity(): Path("attachments") / selected_name(position)
        for position, attachment in enumerate(attachments)
    }


class AttachmentRecord(BaseModel, frozen=True):
    """The retained identity and digest of one downloaded file."""

    representation: Literal["metadata", "sandbox"]
    id: str
    name: str
    mime_type: str
    relative_path: str
    size_bytes: int
    sha256: str
    message_id: str = ""
    sandbox_path: str = ""


class DownloadManifest(BaseModel, frozen=True):
    """A replayable account of one retained ChatGPT delivery.

    ``selected_artifact`` names the ``:artifact`` selector that narrowed this
    download, and is empty for a complete one. ``declared_attachment_count``
    is what the payload declares, so a delivery holding fewer files than it
    counts cannot be mistaken for a complete one.
    """

    provider: str = "chatgpt"
    source_url: str
    fetched_at: str
    title: str
    route: str
    identifier: str
    active_message_count: int
    mapping_node_count: int
    selected_artifact: str = ""
    declared_attachment_count: int = 0
    attachments: list[AttachmentRecord]


class FetchedAttachment(BaseModel, frozen=True):
    """One attachment after its bytes have been fetched successfully."""

    declaration: ChatGPTAttachment
    body: bytes


def conversation_from(payload: JsonValue) -> ChatGPTConversation:
    """Find and validate the conversation object in a web response."""
    match payload:
        case {"mapping": dict()} as direct:
            candidate = direct
        case {"conversation": {"mapping": dict()} as nested}:
            candidate = nested
        case {"data": {"mapping": dict()} as nested}:
            candidate = nested
        case _:
            raise ChatGPTDownloadError(
                "ChatGPT returned no complete conversation mapping"
            )
    try:
        return ChatGPTConversation.model_validate(candidate)
    except ValueError as error:
        raise ChatGPTDownloadError(
            "ChatGPT conversation payload changed shape"
        ) from error


def selected_attachment(
    attachments: list[ChatGPTAttachment], artifact: str
) -> ChatGPTAttachment:
    """The one declared file a ``:artifact`` selector names."""
    paths = attachment_paths(attachments)
    named = next(
        (item for item in attachments if paths[item.identity()].name == artifact), None
    )
    if named is not None:
        return named
    matched = [
        item
        for item in attachments
        if artifact in {item.stored_name(), item.identifier, item.sandbox_path} - {""}
    ]
    match matched:
        case [only]:
            return only
        case []:
            offered = ", ".join(
                sorted(paths[item.identity()].name for item in attachments)
            )
            raise ChatGPTDownloadError(
                f"This conversation declares no artifact {artifact!r}. "
                f"It offers: {offered or '(no files)'}"
            )
        case ambiguous:
            raise ChatGPTDownloadError(
                f"Artifact {artifact!r} names {len(ambiguous)} files here; "
                "select one by the filename it is retained under: "
                + ", ".join(sorted(paths[item.identity()].name for item in ambiguous))
            )


def render_part(part: JsonValue) -> str:
    """One content part without silently dropping an unfamiliar shape."""
    if isinstance(part, str):
        return part
    if not isinstance(part, dict):
        return json.dumps(part, ensure_ascii=False)
    match part:
        case {"text": str() as text}:
            return text
        case {"asset_pointer": str() as pointer}:
            return f"[Asset: {pointer}]"
        case _:
            return (
                "```json\n" + json.dumps(part, indent=2, ensure_ascii=False) + "\n```"
            )


def render_message(
    message: ChatGPTMessage, paths: dict[AttachmentIdentity, Path]
) -> str:
    """One speaker-tagged message plus the files it names."""
    content = [render_part(part) for part in message.content.parts]
    if message.content.text:
        content.append(message.content.text)
    content.extend(
        attachment_label(item.stored_name(), paths.get(item.identity()))
        for item in message.attachments()
    )
    body = "\n\n".join(text for text in content if text)
    if not body:
        body = "[empty message payload; see conversation.json]"
    role = message.author.role or "unknown"
    return f"<{role}>\n{body}\n</{role}>"


def render_conversation(
    reference: ConversationReference,
    conversation: ChatGPTConversation,
    retained: dict[AttachmentIdentity, Path] | None = None,
) -> str:
    """The selected branch as speaker-tagged Markdown beside the raw mapping.

    ``retained`` carries the path of every file this delivery actually holds,
    which is every declared file unless a ``:artifact`` selector narrowed it.
    """
    title = conversation.title or "Untitled ChatGPT conversation"
    declared = attachment_paths(conversation.attachments())
    messages = "\n\n".join(
        render_message(message, declared if retained is None else retained)
        for message in conversation.active_messages()
    )
    return (
        f"# {title}\n\n"
        f"Source: {reference.page_url()}\n\n"
        "The complete branched payload is in `conversation.json`; this file "
        "renders its selected branch.\n\n"
        f"{messages}\n"
    )


async def response_json(response: APIResponse) -> JsonValue:
    """Validate one Playwright response as JSON-shaped data."""
    try:
        return TypeAdapter(JsonValue).validate_python(await response.json())
    except (PlaywrightError, ValueError) as error:
        raise ChatGPTDownloadError(
            f"ChatGPT returned non-JSON data from {response.url}"
        ) from error


async def fetch_session(
    request: APIRequestContext, reference: ConversationReference
) -> ChatGPTSession | None:
    """Resolve bearer authorization for a private conversation when required."""
    if reference.route() == "share":
        return None
    url = "https://chatgpt.com/api/auth/session"
    response = await request.get(
        url,
        headers={"Referer": reference.page_url(), "Accept": "application/json"},
        fail_on_status_code=False,
    )
    if response.status in {401, 403}:
        raise ChatGPTAuthenticationRequired(
            f"The ChatGPT browser session returned HTTP {response.status}"
        )
    if response.status != 200:
        raise ChatGPTDownloadError(
            f"ChatGPT refused its browser session endpoint with HTTP {response.status}"
        )
    try:
        session = ChatGPTSession.model_validate(await response_json(response))
    except ValueError as error:
        raise ChatGPTDownloadError("ChatGPT session payload changed shape") from error
    if not session.access_token.get_secret_value():
        raise ChatGPTAuthenticationRequired(
            "The ChatGPT browser session returned no access token"
        )
    return session


async def fetch_payload(
    request: APIRequestContext,
    reference: ConversationReference,
    session: ChatGPTSession | None,
) -> JsonValue:
    """Fetch one complete conversation through the browser's live session."""
    refusals: tuple[str, ...] = ()
    for url in reference.api_urls():
        response = await request.get(
            url,
            headers={
                "Referer": reference.page_url(),
                "Accept": "application/json",
                **(session.headers_for(url) if session is not None else {}),
            },
            fail_on_status_code=False,
        )
        if response.status == 200:
            return await response_json(response)
        refusals += (f"{response.status} from {url}",)
    if reference.route() == "conversation" and any(
        refusal.startswith(("401 ", "403 ")) for refusal in refusals
    ):
        raise ChatGPTAuthenticationRequired(
            "The ChatGPT browser session is missing or expired"
        )
    route = "shared snapshot" if reference.route() == "share" else "conversation"
    recovery = " Check that the link is still available."
    raise ChatGPTDownloadError(
        f"ChatGPT refused the {route}: {', '.join(refusals)}.{recovery}"
    )


def allowed_download_url(value: str) -> str:
    """Accept only attachment URLs served by OpenAI-controlled origins."""
    absolute = urljoin("https://chatgpt.com", value)
    parsed = urlparse(absolute)
    host = (parsed.hostname or "").lower()
    allowed = any(
        host == suffix or host.endswith(f".{suffix}")
        for suffix in ("chatgpt.com", "openai.com", "oaiusercontent.com")
    )
    if parsed.scheme != "https" or not allowed:
        raise ChatGPTDownloadError(
            f"Attachment download escaped OpenAI-controlled origins: {absolute}"
        )
    return absolute


def redirected_download(payload: JsonValue) -> str | None:
    """A signed file URL returned by an attachment endpoint, when present."""
    match payload:
        case {"download_url": str() as target}:
            return target
        case {"url": str() as target}:
            return target
        case _:
            return None


async def attachment_response_body(
    request: APIRequestContext, response: APIResponse
) -> bytes:
    """Read an attachment response, following a JSON signed-URL envelope."""
    content_type = (
        response.headers["content-type"] if "content-type" in response.headers else ""
    )
    if "application/json" not in content_type:
        return await response.body()
    payload = await response_json(response)
    target = redirected_download(payload)
    if target is None:
        return await response.body()
    redirected = await request.get(
        allowed_download_url(target), fail_on_status_code=False
    )
    if redirected.status != 200:
        raise ChatGPTDownloadError(
            f"Signed attachment URL returned HTTP {redirected.status}"
        )
    return await redirected.body()


async def fetch_attachment(
    request: APIRequestContext,
    reference: ConversationReference,
    attachment: ChatGPTAttachment,
    session: ChatGPTSession | None,
    *,
    conversation_id: str = "",
) -> FetchedAttachment:
    """Download one declared attachment or fail the whole retention pass."""
    refusals: tuple[str, ...] = ()
    for candidate in attachment.requests(reference, conversation_id):
        url = allowed_download_url(candidate.url)
        headers = session.headers_for(url) if session is not None else {}
        if urlparse(url).hostname == "chatgpt.com":
            headers = {"Referer": reference.page_url(), **headers}
        if candidate.params is None:
            response = await request.get(
                url, headers=headers, fail_on_status_code=False
            )
        else:
            response = await request.get(
                url,
                params=candidate.params.model_dump(),
                headers=headers,
                fail_on_status_code=False,
            )
        if response.status == 200:
            body = await attachment_response_body(request, response)
            return FetchedAttachment(declaration=attachment, body=body)
        refusals += (f"HTTP {response.status} from {url}",)
    raise ChatGPTDownloadError(
        f"Could not download attachment {attachment.stored_name()!r}: "
        + "; ".join(refusals)
    )


def delivery_directory(
    root: Path, reference: ConversationReference, output: Path | None = None
) -> Path:
    """The directory one retained conversation occupies, refusing an escape."""
    workspace = (
        (output if output is not None else root / "tmp" / "conversations") / "chatgpt"
    ).resolve()
    destination = workspace / reference.identifier()
    if not destination.resolve().is_relative_to(workspace):
        raise ChatGPTDownloadError(
            "Conversation destination escapes tmp/conversations/chatgpt"
        )
    return destination


def carried_attachments(
    destination: Path, declared: list[ChatGPTAttachment]
) -> list[FetchedAttachment]:
    """Bytes a prior delivery holds, readmitted only on their recorded digest.

    A selective download re-fetches one file, and everything a previous pass
    already paid for would otherwise be discarded by the atomic replacement.
    """
    manifest_path = destination / "manifest.json"
    if not manifest_path.is_file():
        return []
    try:
        prior = DownloadManifest.model_validate_json(
            manifest_path.read_text(encoding="utf-8")
        )
    except ValueError:
        logger.warning("Ignoring an unreadable prior manifest at %s", manifest_path)
        return []
    wanted = {attachment.identity(): attachment for attachment in declared}
    carried: tuple[FetchedAttachment, ...] = ()
    for record in prior.attachments:
        attachment = wanted.get(
            AttachmentIdentity(
                representation=record.representation,
                identifier=record.id,
                message_id=record.message_id,
                sandbox_path=record.sandbox_path,
            )
        )
        path = destination / record.relative_path
        if attachment is None or not path.is_file():
            continue
        if not path.resolve().is_relative_to(destination.resolve()):
            logger.warning("A prior manifest points outside its delivery: %s", path)
            continue
        body = path.read_bytes()
        if hashlib.sha256(body).hexdigest() != record.sha256:
            logger.warning(
                "A retained attachment no longer matches its digest: %s", path
            )
            continue
        carried += (FetchedAttachment(declaration=attachment, body=body),)
    return list(carried)


def write_delivery(
    root: Path,
    reference: ConversationReference,
    raw: JsonValue,
    conversation: ChatGPTConversation,
    fetched: list[FetchedAttachment],
    output: Path | None = None,
    artifact: str = "",
) -> Path:
    """Atomically replace one retained conversation with what was downloaded."""
    expected = {item.identity() for item in conversation.attachments()}
    received = {item.declaration.identity() for item in fetched}
    if received - expected or (not artifact and expected != received):
        raise ChatGPTDownloadError("Downloaded attachments do not match the payload")
    declared = attachment_paths(conversation.attachments())
    paths = {identity: declared[identity] for identity in received}
    destination = delivery_directory(root, reference, output)
    workspace = destination.parent
    workspace.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=".chatgpt-staging-", dir=workspace
    ) as temporary:
        staged = Path(temporary) / reference.identifier()
        staged.mkdir(parents=True)
        (staged / "conversation.json").write_text(
            json.dumps(raw, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        (staged / "conversation.md").write_text(
            render_conversation(reference, conversation, paths), encoding="utf-8"
        )
        records: tuple[AttachmentRecord, ...] = ()
        for item in fetched:
            relative_path = paths[item.declaration.identity()]
            path = staged / relative_path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(item.body)
            records += (
                AttachmentRecord(
                    representation=item.declaration.representation,
                    id=item.declaration.identifier,
                    name=item.declaration.stored_name(),
                    mime_type=item.declaration.mime_type,
                    relative_path=relative_path.as_posix(),
                    size_bytes=len(item.body),
                    sha256=hashlib.sha256(item.body).hexdigest(),
                    message_id=item.declaration.message_id,
                    sandbox_path=item.declaration.sandbox_path,
                ),
            )
        manifest = DownloadManifest(
            source_url=reference.page_url(),
            fetched_at=datetime.now(UTC).isoformat(),
            title=conversation.title,
            route=reference.route(),
            identifier=reference.identifier(),
            active_message_count=len(conversation.active_messages()),
            mapping_node_count=len(conversation.mapping),
            selected_artifact=artifact,
            declared_attachment_count=len(expected),
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
            ChatGPTDownloadError,
        )
    return destination


async def download_chatgpt(
    reference: ConversationReference,
    *,
    root: Path,
    api: APIRequestContext,
    output: Path,
    artifact: str = "",
) -> Path:
    """Fetch, validate, and retain one conversation and the files it declares.

    An ``artifact`` selector downloads that one file instead of every declared
    one; the transcript, the raw mapping, and the manifest are retained either
    way, and files a previous delivery already holds are carried forward.
    """
    session = await fetch_session(api, reference)
    raw = await fetch_payload(api, reference, session)
    conversation = conversation_from(raw)
    declared = conversation.attachments()
    wanted = [selected_attachment(declared, artifact)] if artifact else declared
    fetched = [
        await fetch_attachment(
            api,
            reference,
            attachment,
            session,
            conversation_id=conversation.conversation_id,
        )
        for attachment in wanted
    ]
    downloaded = {item.declaration.identity() for item in fetched}
    prior = (
        carried_attachments(delivery_directory(root, reference, output), declared)
        if artifact
        else []
    )
    fetched += [item for item in prior if item.declaration.identity() not in downloaded]
    return write_delivery(root, reference, raw, conversation, fetched, output, artifact)
