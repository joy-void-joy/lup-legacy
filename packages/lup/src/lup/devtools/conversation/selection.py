"""What one conversation command was asked to retain, and what came of it."""

from pathlib import Path

from pydantic import BaseModel


class RetentionRequest(BaseModel, frozen=True):
    """One conversation URL, optionally narrowed to a single named artifact."""

    url: str
    artifact: str = ""

    @classmethod
    def parse(cls, supplied: str) -> "RetentionRequest":
        """Read one ``<url>`` or ``<url>:<artifact>`` command-line argument."""
        # lup: ignore[string-split] — the ':' selector is this CLI's own grammar
        head, separator, tail = supplied.rpartition(":")
        if not head or not separator or not tail or "/" in tail:
            return cls(url=supplied)
        return cls(url=head, artifact=tail)

    def describe(self) -> str:
        """This request as the operator wrote it."""
        return f"{self.url}:{self.artifact}" if self.artifact else self.url


class RetentionAttempt(BaseModel, frozen=True):
    """What one requested retention produced, or why it produced nothing."""

    request: RetentionRequest
    destination: Path | None = None
    error: str = ""
    unauthenticated: bool = False
