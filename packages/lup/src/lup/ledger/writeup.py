"""A document generated from the ledger, declared in Python the way guidance is.

A document corrected by hand folds every correction into the one being
edited and leaves the rest carrying stale figures behind a register a reader
has to consult first. A writeup is that folding made automatic: a Python
module declares the document as parts — the author's
prose, and parts that render from the ledger when the document is generated —
so a figure in it is the ledger's figure, read at generation with its standing
beside it, and never a number somebody typed.

**Parts are a union that answers for itself.** The base names one operation,
`render`, and each variant answers it over the store: prose with figures filled
in, a listing of nodes chosen by kind or standing or relation, a tally of
how many share each value of a field, a timeline of dated nodes in the order
of a named clock, what needs a person, a stamp saying what the document was
generated from. A new kind of part is a new variant, not a branch somewhere
else.

**Generated on demand, and drift-checked where every rendered kind is
committed.** Each part says which kinds it renders, or that it cannot say —
one naming nodes rather than kinds — and a writeup whose every kind the
project's layout commits renders the same on every machine, so it is a
repository writer like any other generated file. One rendering a local kind
reads live state under the repository's own git directory, so the same
declaration renders differently on a machine that has recorded and one that
has not; it is written by `ledger writeup` and committed like any other
document, and `--check` verifies it against *this* machine's log. What keeps
either honest everywhere is that every figure it renders is a `lup:` cite,
which the cite check holds to the node wherever the log is. The stamp names
the newest record rather than the wall clock, so one log renders one
document either way.
"""

from abc import ABC, abstractmethod
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from lup.coordination.identity import mint_member_id
from lup.coordination.refs import ActorRef
from lup.coordination.rendering import GROUPS, task_line, user_tasks
from lup.coordination.tasks import Task
from lup.formats.banner import GeneratedBanner
from lup.formats.markdown import PlainCell
from lup.harness.materialization import write_generated_file
from lup.harness.models import Artifact
from lup.ledger.journal import LedgerStore
from lup.ledger.kinds import kind_of
from lup.ledger.models import LedgerNode
from lup.ledger.store import LedgerLayout
from lup.workspace.paths import project_root

# lup: ignore[constant-declaration] — the command that regenerates a writeup,
# which the banner on every generated one has to spell identically
WRITEUP_COMMAND = "uv run lup-devtools ledger writeup"


class WriteupError(Exception):
    """A declaration names something the ledger does not hold, in the author's words."""


def handle(node: LedgerNode) -> str:
    """How a rendered document points at a node: its slug where it has one."""
    return node.slug or node.id


def cell(text: str) -> str:
    """Text as one table cell carries it: one line, every pipe escaped.

    A node's text is whatever its writer recorded — a paste body with its
    newlines, a title with a pipe — and a listing row is a markdown table
    row, which a newline ends and a pipe splits. Nothing is cut: the
    cell kind every generated table renders through is what escapes it, so a
    value survives a writeup's row as it survives any other generated table's.
    """
    return PlainCell(text=text).render()


def figure(store: LedgerStore, classes: list[type[LedgerNode]], spelling: str) -> str:
    """One node as a figure in prose: what it says, cited, with its standing.

    A sound node renders bold and cited, so the cite check holds the document
    to it. One that is not sound renders struck through with the reason, so
    the figure a reader would have copied is visibly not one to copy — and is
    still cited, so the check reports it too. The label is one line and safe
    in a table row, because a figure sits in a listing as often as in prose.
    """
    node = store.resolve(spelling, classes)
    if node is None:
        raise WriteupError(
            f"no node in this repository has the id or slug {spelling!r}"
        )
    where = store.standing(node, classes)
    link = f"[{cell(node.text or node.title)}](lup:{handle(node)})"
    if where.sound:
        return f"**{link}**"
    return f"~~{link}~~ ({where.label}: {cell(where.reason)})"


class Placeholder(BaseModel, frozen=True):
    """One `{name}` in a prose part, and the node whose figure fills it."""

    name: str = Field(min_length=1)
    node: str = Field(min_length=1)


class WriteupPart(BaseModel, ABC, frozen=True):
    """One stretch of a writeup, rendered from the ledger when asked.

    Abstract, because a part that renders nothing is a declaration somebody
    forgot to finish, and the base saying so is better than an empty section
    a reader takes for an empty result. Every variant ends its lines with a
    blank, so joined parts read as paragraphs and the document ends in one
    newline.
    """

    kind: str

    @abstractmethod
    def render(self, store: LedgerStore, classes: list[type[LedgerNode]]) -> list[str]:
        """This part as lines of markdown, read from the store now."""

    @abstractmethod
    def kinds(self) -> list[str] | None:
        """The kinds this part renders, or nothing where the declaration cannot say.

        What decides whether the document is the same on every machine: a
        part reading only kinds the layout commits renders identically
        everywhere, one reading a local kind does not, and one naming nodes
        rather than kinds cannot tell until the log is read — which is too
        late for a decision the roster takes at composition.
        """


class Prose(WriteupPart, frozen=True):
    """The author's own markdown, with figures filled in where it names them.

    `{name}` in the text is replaced by the figure of the node the placeholder
    names — through the standard library's formatter, so a document with no
    placeholders is returned as written and one with a brace to keep spells
    it `{{`.
    """

    kind: Literal["prose"] = "prose"
    text: str
    figures: list[Placeholder] = []

    def render(self, store: LedgerStore, classes: list[type[LedgerNode]]) -> list[str]:
        if not self.figures:
            return [self.text, ""]
        filled = {each.name: figure(store, classes, each.node) for each in self.figures}
        try:
            return [self.text.format_map(filled), ""]
        except (KeyError, IndexError, ValueError) as unnamed:
            raise WriteupError(
                f"prose names a placeholder no figure fills: {unnamed}"
            ) from unnamed

    def kinds(self) -> list[str] | None:
        """Nothing where the prose stands alone; unknown where a figure names a node."""
        return None if self.figures else []


class Listing(WriteupPart, frozen=True):
    """A table of nodes chosen by kind, standing, relation, moment, or by name.

    The shape of "the results that matter most", "open questions" and "the
    correction log" alike: each row a node's figure with its standing read
    now, ordered by priority and then by age unless the author named the rows.
    Empty says so in the author's words rather than vanishing, because a
    section that disappears reads as a section nobody wrote.
    """

    kind: Literal["listing"] = "listing"
    heading: str = Field(min_length=1)
    of: str = ""
    """Only nodes of this kind."""

    standing: str = ""
    """Only nodes whose standing has this label right now."""

    excluding: str = ""
    """Only nodes whose standing is not this label right now.

    The other half of `standing`: what is still to do is every task but the
    finished ones, which no one label names — open, held, blocked and waiting
    are all of them.
    """

    since: datetime | None = None
    """Only nodes with a record newer than this moment."""

    nodes: list[str] = []
    """Exactly these nodes, in this order, by id or slug."""

    into: str = ""
    """Only nodes pointing at this one — the claims answering a question."""

    via: str = ""
    """…through edges of this kind, or through any edge where empty."""

    lacking: str = ""
    """Only nodes from which no edge of this kind runs.

    The selector for what is *ours*: a claim with no edge to the source that
    stated it first was found here, and that is a query over the edges rather
    than a stored count, which moves with every change of identity even
    when the evidence has not changed.
    """

    having: str = ""
    """Only nodes from which an edge of this kind runs — the other half of `lacking`.

    What others found first is every claim pointing at the source that stated
    it, listed beside what is ours so credit and novelty are one query.
    """

    sound: bool | None = None
    """Only nodes whose standing is sound (True) or is not (False) right now.

    The one bit every standing vocabulary shares, so a listing of what may
    still be cited needs no list of the labels that mean so.
    """

    min_priority: int = 0
    numbered: bool = False
    empty: str = "Nothing recorded."

    def admits(self, label: str) -> bool:
        """Whether a node standing under *label* is a row: the one asked for, not the one excluded."""
        asked = not self.standing or label == self.standing
        return asked and (not self.excluding or label != self.excluding)

    def chosen(
        self, store: LedgerStore, classes: list[type[LedgerNode]]
    ) -> list[LedgerNode]:
        """The rows, selected and ordered as the declaration asks."""
        if self.nodes:
            named = [store.resolve(spelling, classes) for spelling in self.nodes]
            return [node for node in named if node is not None]
        if self.into:
            target = store.resolve(self.into, classes)
            if target is None:
                raise WriteupError(
                    f"no node in this repository has the id or slug {self.into!r}"
                )
            sources = [
                edge.source
                for edge in store.into(target.id)
                if not self.via or edge.kind == self.via
            ]
            pointing = [store.resolve(source, classes) for source in sources]
            candidates = [node for node in pointing if node is not None]
        else:
            candidates = [
                node
                for node in store.all_nodes(classes)
                if not self.of or node.kind == self.of
            ]
        moved = store.moved_since(self.since) if self.since is not None else None
        edges = store.edges() if self.lacking or self.having else []
        pointing = {edge.source for edge in edges if edge.kind == self.lacking}
        carrying = {edge.source for edge in edges if edge.kind == self.having}

        def admitted(node: LedgerNode) -> bool:
            if not (self.standing or self.excluding) and self.sound is None:
                return True
            where = store.standing(node, classes)
            return self.admits(where.label) and (
                self.sound is None or where.sound == self.sound
            )

        kept = [
            node
            for node in candidates
            if node.priority >= self.min_priority
            and (moved is None or node.id in moved)
            and node.id not in pointing
            and (not self.having or node.id in carrying)
            and admitted(node)
        ]
        return sorted(kept, key=lambda node: (-node.priority, node.at))

    def render(self, store: LedgerStore, classes: list[type[LedgerNode]]) -> list[str]:
        rows = self.chosen(store, classes)
        lines = [f"## {self.heading}", ""]
        if not rows:
            return [*lines, self.empty, ""]
        lines.extend(["| # | What | Standing | Node |", "| --- | --- | --- | --- |"])
        for position, node in enumerate(rows, start=1):
            where = store.standing(node, classes)
            what = figure(store, classes, node.id) + (
                f" — {cell(node.title)}" if node.text else ""
            )
            number = str(position) if self.numbered else ""
            lines.append(f"| {number} | {what} | {where.label} | `{handle(node)}` |")
        return [*lines, ""]

    def kinds(self) -> list[str] | None:
        """The one kind chosen by `of`; unknown where rows are named, related, lacking, having, or every kind.

        Unknown for `lacking` and `having` because the edge each reads follows
        its far end's placement, and the declaration cannot name the far end's
        kind.
        """
        if self.nodes or self.into or self.lacking or self.having or not self.of:
            return None
        return [self.of]


def moment_of(node: LedgerNode, field: str) -> datetime | None:
    """The moment one named field holds on a node, or nothing.

    Read by name because the library declares no clock: which field says when
    a thing happened is the project's, and a node read back as the base class
    — a kind this build does not declare — has no such field and is undated.
    """
    if not field:
        return None
    value = getattr(node, field, None)
    return value if isinstance(value, datetime) else None


def spelled_moment(moment: datetime) -> str:
    """One moment as a timeline reads it: UTC, to the second, and marked so.

    A naive moment is read as local time, which is what a writer that spelled
    one meant by it, and every row is shown on one clock so two rows compare.
    """
    aware = moment if moment.tzinfo is not None else moment.astimezone()
    return f"{aware.astimezone(UTC):%Y-%m-%d %H:%M:%S}Z"


class Band(BaseModel, frozen=True):
    """Nodes of one kind whose two moments frame a stretch of the timeline.

    An incident with its window, a campaign with its first and last day. A
    band's nodes render as rows of their own — one where it opens, one where
    it closes — so a reader sees which stretch each dated row falls in
    without the rows being split across tables.
    """

    of: str = Field(min_length=1)
    start: str = Field(min_length=1)
    end: str = Field(min_length=1)


class Entry(BaseModel, frozen=True):
    """One dated row before it is written: when, in what order at that moment, and the cells."""

    when: datetime
    rank: int
    """Where a row sorts among rows at the same moment: a band opens before
    them and closes after them."""

    cells: list[str]

    def moment(self) -> datetime:
        """When the row sits, aware and in UTC, so rows from either kind of clock order together."""
        aware = self.when if self.when.tzinfo is not None else self.when.astimezone()
        return aware.astimezone(UTC)


class Placed(BaseModel, frozen=True):
    """Where a dated row sits and how that reads: the moment, `before` a bound, or a fallback clock named."""

    when: datetime
    reads: str


class Entries(BaseModel, frozen=True):
    """A timeline's rows: the dated ones in order, and the nodes no clock could place."""

    dated: list[Entry]
    undated: list[LedgerNode]


class Timeline(WriteupPart, frozen=True):
    """Every dated node in the order it happened, with the clock that dated it beside.

    One `created_at` standing for three clocks files observation time as
    event time, so the clocks are kept apart. Here a row is
    ordered by the moment the author named (`moment`), and a node without
    one takes its upper bound (`bound`) and says so, or the clock named as
    `fallback`, or goes under the undated heading at the end; a second clock
    (`beside`) is shown on every row and never folded into the order. Bands
    — a kind with a start and an end — open and close as rows of their own,
    so incidents frame the rows without splitting the table.

    Field names rather than fields, because the library declares no clock:
    which field says when a thing happened is the project's vocabulary, and
    the same part declares an event timeline over one clock and a discovery
    timeline over another.
    """

    kind: Literal["timeline"] = "timeline"
    heading: str = Field(min_length=1)
    of: list[str] = Field(min_length=1)
    """The kinds whose nodes are rows."""

    moment: str = Field(min_length=1)
    """The field a row is ordered by."""

    bound: str = ""
    """The field holding an upper bound where the moment is unknown; the row says `before`."""

    fallback: str = ""
    """The field a row takes where it has neither moment nor bound — a record time, say."""

    beside: str = ""
    """A second clock shown beside every row, never ordered by."""

    bands: Band | None = None
    empty: str = "Nothing is dated."
    undated: str = "Undated"
    """The heading over the rows no clock could place."""

    def placed(self, node: LedgerNode) -> Placed | None:
        """When a row sits, and how it reads: the moment, `before` a bound, or the fallback."""
        if (when := moment_of(node, self.moment)) is not None:
            return Placed(when=when, reads=spelled_moment(when))
        if (before := moment_of(node, self.bound)) is not None:
            return Placed(when=before, reads=f"before {spelled_moment(before)}")
        if (taken := moment_of(node, self.fallback)) is not None:
            return Placed(
                when=taken, reads=f"{spelled_moment(taken)} ({self.fallback})"
            )
        return None

    def cells_of(
        self, store: LedgerStore, classes: list[type[LedgerNode]], node: LedgerNode
    ) -> list[str]:
        """The cells every row shares: what, the second clock, standing, node."""
        seen = moment_of(node, self.beside)
        return [
            figure(store, classes, node.id),
            spelled_moment(seen) if seen is not None else "",
            cell(store.standing(node, classes).label),
            f"`{handle(node)}`",
        ]

    def entries(self, store: LedgerStore, classes: list[type[LedgerNode]]) -> Entries:
        """The dated rows in order, and the nodes no clock could place."""
        dated: list[Entry] = []  # lup: ignore[empty-collection] — filled below
        undated: list[LedgerNode] = []  # lup: ignore[empty-collection] — filled below
        for node in store.all_nodes(classes):
            if node.kind in self.of:
                placed = self.placed(node)
                if placed is None:
                    undated.append(node)
                    continue
                dated.append(
                    Entry(
                        when=placed.when,
                        rank=1,
                        cells=[placed.reads, *self.cells_of(store, classes, node)],
                    )
                )
            if self.bands is not None and node.kind == self.bands.of:
                what, seen, standing, spelled_handle = self.cells_of(
                    store, classes, node
                )
                for field, rank, verb in (
                    (self.bands.start, 0, "opens"),
                    (self.bands.end, 2, "closes"),
                ):
                    if (edge := moment_of(node, field)) is not None:
                        dated.append(
                            Entry(
                                when=edge,
                                rank=rank,
                                cells=[
                                    spelled_moment(edge),
                                    f"{what} {verb}",
                                    seen,
                                    standing,
                                    spelled_handle,
                                ],
                            )
                        )
        return Entries(
            dated=sorted(
                dated, key=lambda entry: (entry.moment(), entry.rank, entry.cells[-1])
            ),
            undated=sorted(undated, key=lambda node: node.at),
        )

    def render(self, store: LedgerStore, classes: list[type[LedgerNode]]) -> list[str]:
        entries = self.entries(store, classes)
        dated, undated = entries.dated, entries.undated
        beside = self.beside or "also"
        lines = [f"## {self.heading}", ""]
        if not dated:
            lines.extend([self.empty, ""])
        else:
            lines.extend(
                [
                    f"| {self.moment} | What | {beside} | Standing | Node |",
                    "| --- | --- | --- | --- | --- |",
                    *(f"| {' | '.join(entry.cells)} |" for entry in dated),
                    "",
                ]
            )
        if undated:
            lines.extend(
                [
                    f"### {self.undated}",
                    "",
                    f"| What | {beside} | Standing | Node |",
                    "| --- | --- | --- | --- |",
                    *(
                        f"| {' | '.join(self.cells_of(store, classes, node))} |"
                        for node in undated
                    ),
                    "",
                ]
            )
        return lines

    def kinds(self) -> list[str] | None:
        """The row kinds and the band kind, each once."""
        band = [self.bands.of] if self.bands is not None else []
        return list(dict.fromkeys([*self.of, *band]))


class Count(BaseModel, frozen=True):
    """One value of a tallied field and how many nodes carry it."""

    value: str
    count: int


class Tally(WriteupPart, frozen=True):
    """How many nodes of one kind share each value of one field, largest first.

    The shape of "what is open, by host" over twenty thousand leads: a
    reader deciding where to start wants the groups and their sizes, not a
    row per node. Each row is one value of the field and the count of nodes
    carrying it, narrowed to one standing where the author asked. A node
    read back as the base class carries no such field and tallies under
    the empty value, spelled so.
    """

    kind: Literal["tally"] = "tally"
    heading: str = Field(min_length=1)
    of: str = Field(min_length=1)
    """Only nodes of this kind."""

    by: str = Field(min_length=1)
    """The field whose values are the rows."""

    standing: str = ""
    """Only nodes whose standing has this label right now."""

    empty: str = "Nothing recorded."
    none: str = "(none)"
    """How a node with no value, or no such field, is spelled."""

    def tallied(
        self, store: LedgerStore, classes: list[type[LedgerNode]], node: LedgerNode
    ) -> bool:
        """Whether a node is of the kind counted and, when one is named, at the standing."""
        if node.kind != self.of:
            return False
        return not self.standing or store.standing(node, classes).label == self.standing

    def value_of(self, node: LedgerNode) -> str:
        value = getattr(node, self.by, "")
        return str(value) if value not in (None, "") else self.none

    def counted(
        self, store: LedgerStore, classes: list[type[LedgerNode]]
    ) -> list[Count]:
        """Each value with its count, largest first and then by value."""
        counts = Counter(
            self.value_of(node)
            for node in store.all_nodes(classes)
            if self.tallied(store, classes, node)
        )
        return sorted(
            (Count(value=value, count=count) for value, count in counts.items()),
            key=lambda row: (-row.count, row.value),
        )

    def render(self, store: LedgerStore, classes: list[type[LedgerNode]]) -> list[str]:
        rows = self.counted(store, classes)
        lines = [f"## {self.heading}", ""]
        if not rows:
            return [*lines, self.empty, ""]
        lines.extend([f"| {self.by} | count |", "| --- | --- |"])
        lines.extend(f"| {cell(row.value)} | {row.count} |" for row in rows)
        return [*lines, ""]

    def kinds(self) -> list[str] | None:
        return [self.of]


class NeedsPerson(WriteupPart, frozen=True):
    """What is waiting on the person, grouped by what each row costs them.

    The task list whose holder is a person, folded into the document rather
    than kept as a file beside it, which is the part that goes missing.
    Grouped by `needs`, so a command to paste and a judgement
    to make are not one list.
    """

    kind: Literal["needs_person"] = "needs_person"
    heading: str = Field(min_length=1)

    def render(self, store: LedgerStore, classes: list[type[LedgerNode]]) -> list[str]:
        del classes
        tasks = user_tasks(store)
        lines = [f"## {self.heading}", ""]
        if not tasks:
            return [*lines, "Nothing is waiting on a person.", ""]
        for group in GROUPS:
            rows = [task for task in tasks if task.needs == group.needs]
            if not rows:
                continue
            lines.extend([f"### {group.heading}", ""])
            lines.extend(task_line(task) for task in rows)
            lines.append("")
        return lines

    def kinds(self) -> list[str] | None:
        return [kind_of(Task)]


class Stamp(WriteupPart, frozen=True):
    """What this document was generated from, so a reader knows how current it is.

    The newest record's time rather than the wall clock, so one log renders
    one document and a regeneration that changed nothing changes nothing.
    """

    kind: Literal["stamp"] = "stamp"
    counting: list[str] = []
    """Kinds worth a count of their own beside the totals — corrections, say."""

    of: list[str] = []
    """The kinds this document is generated from; every kind where empty.

    Named so the stamp counts what the document renders and nothing else: a
    document over committed kinds stamped over the whole log would change
    with every local record, on every machine differently. An edge counts
    only where both of its ends are among these kinds, which is exactly the
    edges the committed half holds.
    """

    def kinds(self) -> list[str] | None:
        return self.of or None

    def render(self, store: LedgerStore, classes: list[type[LedgerNode]]) -> list[str]:
        nodes = [
            node
            for node in store.all_nodes(classes)
            if not self.of or node.kind in self.of
        ]
        edges = [
            edge
            for edge in store.edges()
            if not self.of
            or (
                store.kind_at(edge.source) in self.of
                and store.kind_at(edge.target) in self.of
            )
        ]
        newest = max((node.at for node in nodes), default=None)
        counted = "; ".join(
            f"{sum(1 for node in nodes if node.kind == kind)} {kind}"
            for kind in self.counting
        )
        parts = [
            f"Generated from this repository's ledger: {len(nodes)} node(s),"
            f" {len(edges)} edge(s)",
            counted,
            f"newest record {newest.isoformat()}"
            if newest is not None
            else "nothing recorded yet",
        ]
        stamped = "; ".join(part for part in parts if part)
        return [f"{stamped}. Regenerate with `{WRITEUP_COMMAND}`.", ""]


class Writeup(BaseModel, frozen=True):
    """One declared document: where it is written, and the parts it is made of."""

    name: str = Field(min_length=1)
    path: str = Field(min_length=1)
    source: str = Field(min_length=1)
    """The module declaring it, which the banner names as where to edit."""

    parts: list[WriteupPart] = Field(min_length=1)

    def kinds(self) -> list[str] | None:
        """Every kind this document renders, or nothing where one part cannot say.

        Each kind once, in the order the parts first name it. One part that
        cannot say makes the document one that cannot, since a reader is
        asking whether the whole file is the same everywhere.
        """
        answers = [part.kinds() for part in self.parts]
        if any(answer is None for answer in answers):
            return None
        return list(dict.fromkeys(kind for answer in answers for kind in answer or []))


def render_writeup(
    store: LedgerStore, classes: list[type[LedgerNode]], writeup: Writeup
) -> str:
    """The document as its parts render it now, in declaration order.

    A plain join: every part ends its lines with a blank, so the document ends
    in exactly one newline without anything being trimmed off it.
    """
    return "\n".join(
        line for part in writeup.parts for line in part.render(store, classes)
    )


def write_writeup(
    writeup: Writeup,
    classes: list[type[LedgerNode]],
    root: Path | None = None,
    *,
    check: bool = False,
    layout: LedgerLayout = LedgerLayout(),
) -> Path:
    """Write one writeup from this machine's ledger, or verify the one on disk.

    Through the same machinery as every other generated repository file, so
    the banner says where to edit and what to run, and `--check` says stale
    in the same words — against this machine's log, which is the only one it
    can see. The signature past the writeup and its classes is a repository
    writer's, so a writeup whose every kind is committed is listed among a
    project's generated files.
    """
    base = root or project_root()
    store = LedgerStore(base, ActorRef(kind="console", id=mint_member_id()), layout)
    # One fold for the whole document: every part reads the log as it was
    # when generation started, and what points at each node is a lookup.
    with store.batch():
        body = render_writeup(store, classes, writeup)
    artifact = Artifact.generated(
        path=Path(writeup.path),
        body=body,
        semantic_id=f"writeup.{writeup.name}",
        banner=GeneratedBanner(source=writeup.source, command=WRITEUP_COMMAND),
    )
    return write_generated_file(artifact, base, WRITEUP_COMMAND, check=check)
