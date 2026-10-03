"""A document naming a node, and whether the node still says what it cites.

A register declaring itself authoritative over every other document is
applied to none of them: the canonical prose carries stale figures behind a
list of corrections a reader has to consult first. A cite is the other way
round. A document names a node by id, in the one place a
markdown parser already finds links, and a check asks the node where it
stands — so prose cannot go on citing a claim that was corrected or whose
evidence went away, because the build says so.

**The marker is a link.** ``[three of four](lup:9f2a1c)`` is ordinary
markdown to every renderer, and to the parser it is a link whose href names a
node. Found through the parser's own tokens rather than by scanning for
brackets, because a fenced block full of shell spells the same shape.

**Nothing here reads files or runs git.** It takes text and a store, so the
same check runs from the gate over tracked documents and from a command over
one file somebody named, and a test hands it a string.
"""

from collections.abc import Iterator

from markdown_it import MarkdownIt
from markdown_it.token import Token
from pydantic import BaseModel

from lup.ledger.journal import LedgerStore
from lup.ledger.models import LedgerNode, Standing

# lup: ignore[constant-declaration] — the scheme this repository defines a
# cite by, which every document and the check have to spell identically
CITE_SCHEME = "lup:"

parser = MarkdownIt()


class Cite(BaseModel, frozen=True):
    """One place a document names a node."""

    line: int
    node_id: str
    label: str
    """The link text, which is what a reader sees and a render replaces."""


def cite_href(token: Token) -> str:
    """The href of a link that is a cite, or nothing for any other token."""
    value = token.attrGet("href")
    if token.type != "link_open" or not isinstance(value, str):
        return ""
    return value if value.startswith(CITE_SCHEME) else ""


def cites_in(text: str) -> list[Cite]:
    """Every cite in one document, in the order it is read.

    Block tokens carry the line span and inline tokens carry the links, so a
    cite's line is its enclosing block's first line — which is where a reader
    is sent, and close enough for a paragraph.
    """

    def linked(block: Token) -> Iterator[Cite]:
        children = block.children or []
        for index, token in enumerate(children):
            href = cite_href(token)
            if not href:
                continue
            close = next(
                (
                    position
                    for position in range(index + 1, len(children))
                    if children[position].type == "link_close"
                ),
                len(children),
            )
            yield Cite(
                line=(block.map[0] if block.map else 0) + 1,
                node_id=href[len(CITE_SCHEME) :],
                label="".join(
                    inner.content
                    for inner in children[index + 1 : close]
                    if inner.type == "text"
                ),
            )

    return [
        cite
        for block in parser.parse(text)
        if block.type == "inline"
        for cite in linked(block)
    ]


class CiteReading(BaseModel, frozen=True):
    """One cite against what its node says now."""

    cite: Cite
    node: LedgerNode | None = None
    standing: Standing | None = None

    def holds(self) -> bool:
        """Whether this cite may stand: the node exists and its standing is sound."""
        return (
            self.node is not None and self.standing is not None and self.standing.sound
        )

    def problem(self) -> str:
        """Why it does not, in the words a reader fixes it by."""
        if self.node is None:
            return f"no node in this repository has the id {self.cite.node_id!r}"
        if self.standing is None:
            return "standing could not be read"
        return f"{self.standing.label}: {self.standing.reason}"


def read_cites(
    text: str, store: LedgerStore, classes: list[type[LedgerNode]]
) -> list[CiteReading]:
    """Every cite in one document, each resolved and asked where it stands.

    Under one fold of the log: a generated document cites every node it
    lists, and a memo over a trove lists tens of thousands, so each cite is
    a lookup rather than a read of the journals.
    """

    def reading(cite: Cite) -> CiteReading:
        node = store.resolve(cite.node_id, classes)
        return CiteReading(
            cite=cite,
            node=node,
            standing=store.standing(node, classes) if node is not None else None,
        )

    cites = cites_in(text)
    if not cites:
        # A document with nothing to hold to the log opens no fold of it.
        return []
    with store.batch():
        return [reading(cite) for cite in cites]
