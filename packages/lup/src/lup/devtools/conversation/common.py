"""What retaining a conversation shares, whichever service it was held on.

ChatGPT and Claude serve different payloads behind the same shape of address
— a conversation page or a shared snapshot of one, named by an identifier
after its route segment — and a retained conversation lands the same way for
both: staged whole beside its destination, then swapped in so a reader finds
the previous delivery or the complete new one and never half of either. Each
retainer keeps what is its service's own; this is the rest, written once so a
fix to how an address is read or a delivery lands reaches both.
"""

import shutil
from pathlib import Path, PurePosixPath
from typing import ClassVar
from urllib.parse import ParseResult, urlparse

from pydantic import BaseModel, model_validator

from lup.devtools.conversation.errors import ConversationDownloadError
from lup.types import JsonValue


class Payload(BaseModel, frozen=True, extra="ignore"):
    """A tolerant typed view over a service's undocumented web payload."""

    @model_validator(mode="before")
    @classmethod
    def absent_where_null(cls, data: JsonValue) -> JsonValue:
        """Treat a null service field like an omitted optional field."""
        if not isinstance(data, dict):
            return data
        return {name: value for name, value in data.items() if value is not None}


class Address(BaseModel, frozen=True):
    """A conversation page's URL or a shared snapshot's, on one service.

    Each service says who it is through the class variables, and the reading
    is the same: an HTTPS URL on one of its hosts, a route segment naming a
    live conversation or a share, and a path-safe identifier after it.
    """

    service: ClassVar[str]
    """The service as a reader names it, in every refusal."""
    origin: ClassVar[str]
    """The canonical origin a browser opens its pages on."""
    hosts: ClassVar[tuple[str, ...]]
    """Every host an address on this service may be spelled with."""
    conversation_segment: ClassVar[str]
    """The path segment naming a live conversation, beside ``share``."""
    refused: ClassVar[type[ConversationDownloadError]]
    """What an address this service cannot read is refused as."""

    value: str

    def parsed(self) -> ParseResult:
        """This reference as a validated HTTPS URL on the service."""
        supplied = self.value
        located = urlparse(supplied if "://" in supplied else f"https://{supplied}")
        if (
            located.scheme != "https"
            or (located.hostname or "").lower() not in self.hosts
        ):
            raise self.refused(
                f"Expected an {self.origin}/{self.conversation_segment}/... "
                "or /share/... URL"
            )
        return located

    def route(self) -> str:
        """Whether this URL names a live conversation or a shared snapshot."""
        parts = PurePosixPath(self.parsed().path).parts
        if "share" in parts:
            return "share"
        if self.conversation_segment in parts:
            return "conversation"
        raise self.refused(
            f"{self.service} URL must contain /{self.conversation_segment}/"
            "<conversation-id> or /share/<share-id>"
        )

    def segment(self) -> str:
        """The route segment this URL's identifier follows."""
        return "share" if self.route() == "share" else self.conversation_segment

    def identifier(self) -> str:
        """The service identifier following this URL's route segment."""
        parts = PurePosixPath(self.parsed().path).parts
        segment = self.segment()
        position = parts.index(segment)
        if position + 1 >= len(parts):
            raise self.refused(f"{self.service} URL has no id after /{segment}/")
        identifier = parts[position + 1]
        if not identifier or not all(
            character.isalnum() or character in {"-", "_"} for character in identifier
        ):
            raise self.refused(f"{self.service} conversation id is malformed")
        return identifier

    def page_url(self) -> str:
        """The canonical page a browser opens for this reference."""
        return f"{self.origin}/{self.segment()}/{self.identifier()}"


def attachment_label(stored_name: str, path: Path | None) -> str:
    """The transcript pointer to one attachment, retained or deliberately not."""
    if path is None:
        return (
            f"[Attachment: {stored_name} → not retained; "
            "this delivery selected a single artifact]"
        )
    return f"[Attachment: {stored_name} → {path.as_posix()}]"


def install_delivery(
    staged: Path,
    destination: Path,
    backup: Path,
    service: str,
    refused: type[ConversationDownloadError],
) -> Path:
    """Put a complete staged delivery where the last one was, restoring that one on failure.

    The previous delivery is moved to ``backup`` first and removed only once
    the new one is in place, so a rename the filesystem refuses leaves the
    destination as it was rather than empty. A destination that is a link,
    or a file rather than a directory, is refused before anything moves.
    """
    if destination.is_symlink():
        raise refused("Conversation destination is a symlink")
    if destination.exists():
        if not destination.is_dir():
            raise refused("Conversation destination exists and is not a directory")
        destination.rename(backup)
    try:
        staged.rename(destination)
    except OSError as error:
        if backup.exists():
            backup.rename(destination)
        raise refused(f"Could not install the complete {service} delivery") from error
    if backup.exists():
        shutil.rmtree(backup)
    return destination
