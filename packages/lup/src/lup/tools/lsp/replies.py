"""The language-server replies these tools read, as validated shapes.

A protocol reply arrives as open JSON, and reading it with `.get` chains
spreads the schema across the call sites that happen to look. Declaring each
reply once means an unspecified or half-supported answer fails validation in
one place and degrades to "no evidence" — the same answer as a genuinely
unresolvable symbol — instead of raising somewhere further along.
"""

from pathlib import Path
from urllib.parse import unquote, urlparse

from pydantic import BaseModel, Field, TypeAdapter


def path_of(uri: str) -> str:
    """The filesystem path a `file:` URI names."""
    return Path(unquote(urlparse(uri).path)).as_posix()


class Position(BaseModel):
    """A zero-based LSP line and UTF-16 character offset."""

    line: int
    character: int = 0


class Range(BaseModel):
    """The span an LSP location covers."""

    start: Position


class Location(BaseModel):
    """One LSP `Location`: a document and the span within it."""

    uri: str
    range: Range


LOCATIONS = TypeAdapter(list[Location] | Location | None)
"""Every shape a location-answering request is specified to reply with."""


class SemanticLegend(BaseModel):
    """The names a server's semantic tokens are numbered by, as it declared them."""

    types: list[str] = Field(default=[], alias="tokenTypes")
    modifiers: list[str] = Field(default=[], alias="tokenModifiers")


class SemanticProvider(BaseModel):
    """What a server declared it serves of semantic tokens: its legend."""

    legend: SemanticLegend


SEMANTIC_PROVIDER = TypeAdapter(SemanticProvider | None)


class SemanticTokens(BaseModel):
    """A `semanticTokens/full` reply: five numbers a token, each placed relative to the one before."""

    data: list[int] = []


SEMANTIC_TOKENS = TypeAdapter(SemanticTokens | None)


class MarkupContent(BaseModel):
    """Rendered documentation, in whichever markup the server chose."""

    value: str


class Hover(BaseModel):
    """A `Hover` reply, whose contents field has three specified shapes."""

    contents: MarkupContent | str | list[MarkupContent | str]

    def text(self) -> str:
        """The prose, however the server chose to carry it.

        Matched on the foreign alternatives — a bare string, a list — so the
        markup shape arrives by exclusion rather than by name.
        """
        match self.contents:
            case str(value):
                return value
            case list(parts):
                return "\n".join(
                    part if isinstance(part, str) else part.value for part in parts
                )
            case markup:
                return markup.value


HOVER = TypeAdapter(Hover | None)


class DeclaredSymbol(BaseModel):
    """One symbol a document declares, in the terms an editor shows."""

    name: str
    kind: int
    line: int


class DocumentSymbol(BaseModel):
    """One declared symbol, and the symbols declared inside it.

    The nested shape, returned to a client that declared hierarchical
    support.
    """

    name: str
    kind: int
    range: Range
    children: list["DocumentSymbol"] = []

    def declared(self) -> list[DeclaredSymbol]:
        """This symbol and every symbol nested beneath it, depth first."""
        return [
            DeclaredSymbol(
                name=self.name, kind=self.kind, line=self.range.start.line + 1
            ),
            *(nested for child in self.children for nested in child.declared()),
        ]


class SymbolInformation(BaseModel):
    """The flat shape, returned to a client that declared nothing.

    Both shapes answer :meth:`declared`, so a caller reads the reply without
    asking which one the server chose to send.
    """

    name: str
    kind: int
    location: Location

    def declared(self) -> list[DeclaredSymbol]:
        return [
            DeclaredSymbol(
                name=self.name,
                kind=self.kind,
                line=self.location.range.start.line + 1,
            )
        ]


DOCUMENT_SYMBOLS = TypeAdapter(list[DocumentSymbol] | list[SymbolInformation] | None)


class TextEdit(BaseModel):
    """One replacement a workspace edit would make."""

    range: Range


class EditedFile(BaseModel):
    """One document a workspace edit touches, and how many edits it makes."""

    uri: str
    edits: int


class TextDocumentIdentifier(BaseModel):
    """The document a versioned edit applies to."""

    uri: str


class TextDocumentEdit(BaseModel):
    """The edits one document receives, in the versioned shape."""

    text_document: TextDocumentIdentifier = Field(alias="textDocument")
    edits: list[TextEdit] = []


class WorkspaceEdit(BaseModel):
    """Every edit a rename implies, in whichever of the two shapes arrived.

    A server answers with ``changes`` or with ``documentChanges`` depending on
    what the client declared, and the two are not interchangeable. Both are
    read into the same answer here, so a caller never learns which it got —
    reading only one is a rename that silently reports touching no files.
    """

    changes: dict[str, list[TextEdit]] = {}
    document_changes: list[TextDocumentEdit] = Field(
        alias="documentChanges", default=[]
    )

    def touched(self) -> list[EditedFile]:
        """Every document this edit changes, whichever shape carried it."""
        return [
            *(
                EditedFile(uri=uri, edits=len(edits))
                for uri, edits in self.changes.items()
            ),
            *(
                EditedFile(uri=change.text_document.uri, edits=len(change.edits))
                for change in self.document_changes
            ),
        ]


WORKSPACE_EDIT = TypeAdapter(WorkspaceEdit | None)
