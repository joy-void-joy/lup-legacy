"""What a ledger holds: nodes, the edges between them, and where each stands.

**This module declares no node type and no edge type.** It declares the two
bases every one of them extends, and nothing about what a task is, what a
claim owes, or what counts as verified — those belong to whoever declares the
type, because a library that answered them would be one every adopter had to
argue with.

**Types stay separate.** A `Task` and a `Claim` are unrelated models with
their own fields, sharing a log rather than a shape. Nothing unions them
outside the boundary where stored JSON becomes objects again, so reading a
task's holder is `task.holder` and not a lookup into an untyped bag.

**Standing is not a field.** A stored status is a label that outlives whatever
justified it: the evidence is withdrawn, the file it was checked against
changes, and the label sits there reading as current. So each type answers for
its own standing when somebody asks, over the edges pointing at it, and is
allowed to answer something weaker than it did yesterday. A claim cannot
outrun its support, because nothing records that it ever had any.
"""

from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, Field

from lup.coordination.refs import ActorRef

type Placement = Literal["committed", "local"]
"""Which half of one log a record is written to.

One log is two journals: the committed one travels with the code, the local
one sits under the git directory. Every reader folds both, and a kind is
declared into one of them; the store's layout says where each half is.
"""


class Standing(BaseModel, frozen=True):
    """One reading of where a node stands, as of whoever just asked.

    A label and the reason for it, because a status a reader cannot
    interrogate is one they either trust blindly or ignore. The reason is what
    makes a regression legible: "was verified, and a file it was checked
    against changed" is actionable where a label going quietly from green to
    amber is not.
    """

    label: str
    reason: str = ""

    sound: bool = True
    """Whether a document citing this node may go on citing it.

    A label is a project's vocabulary and a reader outside the project cannot
    rank one against another; this is the one bit every vocabulary shares. It
    is what a cite check reads, so a claim that was verified and is now stale
    fails a build without the check knowing what "stale" means — the type
    that said so knew.
    """


class LedgerEdge(BaseModel, frozen=True):
    """One relation between two nodes, in whichever direction its type means.

    Nodes rather than any particular type of node: an edge runs from a task to
    a claim as readily as between two tasks, which is the whole reason one log
    holds every type. An id is an id.
    """

    kind: str
    """Which relation this is, namespaced by whoever declared it.

    Carried on the record as well as in the class so a reader whose build
    lacks the declaring module can still say what it met, rather than seeing
    an edge it cannot name.
    """

    source: str
    target: str
    author: ActorRef
    at: datetime

    def refusal(self, source: "LedgerNode", target: "LedgerNode") -> str:
        """Why this edge may not be drawn between these two, or nothing.

        The one check a node cannot make about itself, which is why it is
        here: an edge is the only thing that sees both ends. Each type answers
        or declines, and declining is the base's answer — most relations are
        somebody organising their own notes and have nothing to object to.

        Words rather than a boolean, because a refusal a writer cannot read is
        one they will retry verbatim.
        """
        del source, target
        return ""

    def deciding_kinds(self, kind_at: Callable[[str], str]) -> list[str]:
        """The kinds whose placement decides which journal this record goes to.

        An edge's are its two ends', read through the log, because an edge
        is committed only where both ends are: git must never carry a
        reference to a record it does not hold. An end the log does not hold
        reads as no kind, which places the edge local.
        """
        return [kind_at(self.source), kind_at(self.target)]


class LedgerNode(BaseModel, frozen=True):
    """One durable claim somebody recorded, in the shape every type shares.

    A node is what survives the session that wrote it, which is the whole
    distinction between this and the observed state
    :mod:`lup.coordination.touches` keeps: a touch says what a live session
    holds right now and expires with it, while a node is meant to be read by
    somebody who was not there.
    """

    id: str
    kind: str
    """Which type this is, namespaced by whoever declared it.

    On the record rather than inferred from the class, because one log holds
    every vocabulary and a reader will meet types its build never loaded. It
    is also what tells two modules' ``task`` apart.
    """

    title: str = Field(min_length=1)
    text: str = ""
    """What it says, in whoever wrote it's own words. Empty is honest."""

    author: ActorRef
    """Who recorded it, stamped by the store from the caller's identity.

    Provenance a writer cannot spell is provenance a writer cannot get wrong,
    which is the difference between a record that attributes and one that
    repeats what somebody claimed about themselves.
    """

    at: datetime
    attachments: list[str] = []
    """Content digests of the blobs this node carries, in the order attached."""

    slug: str = ""
    """A readable handle the project chose, accepted wherever an id is.

    Optional everywhere: a task is often one line and naming it is a cost,
    while a claim somebody means to cite will get one. Unique across the log
    once taken — the store refuses a second node under the same slug — so a
    cite reads `lup:renewal-prefix-ceiling` rather than twelve hex characters
    nobody can maintain by eye.
    """

    priority: int = 0
    """How much this matters, higher first — a manual property, and meant to be.

    On the base rather than on the kinds that most often carry it, because a
    listing that orders a mixed page — questions beside tasks beside claims —
    has to read one number off every row. A number rather than a word,
    because the library owns no vocabulary of importance; a project maps its
    own words onto it. What the log itself can say about a node's weight is
    read fresh and shown beside this, never folded into it.
    """

    def finished(self) -> bool:
        """Whether this node is done with, for whatever it means to be done.

        On the base because a neighbour asks it: a task reads whether the
        tasks blocking it are finished, and it holds them as
        :class:`LedgerNode` because an edge names an id and not a type. Each
        variant answers or declines, and the base declines — most nodes are
        claims and records, which are never "done".
        """
        return False

    def completed(self) -> "LedgerNode | None":
        """This node marked finished, or nothing where it cannot be.

        On the base because a surface that closes things holds a
        :class:`LedgerNode` — it resolved an id, and an id does not say what is
        at the other end. Each variant answers or declines, and the base
        declines: a claim and a correction are records rather than work, and
        there is nothing for "done" to mean about them.

        A new node rather than a mutation, because nothing here is mutable:
        the caller appends it, and the earlier version stays in the log.
        """
        return None

    def standing(self, around: "Surroundings") -> Standing:
        """Where this node stands, given its neighbourhood.

        Run when somebody asks and never stored. The base's answer is what a
        ledger can say without being told anything about evidence — it was
        written down — which is honest rather than a placeholder, and it means
        a type with no epistemics is one class and no overrides.
        """
        del around
        return Standing(label="recorded")

    def prepared(self, root: Path) -> Self:
        """This node with whatever it derives from the working tree filled in.

        Called by the store once, just before a node is appended, so a type
        whose record depends on the tree as it is *now* — evidence pinned to
        the digests of the files it was checked against — computes that at the
        one moment it is true, and a generic `record` never has to know which
        types need it. The base derives nothing and returns itself, which is
        every type that is only what somebody wrote.
        """
        del root
        return self

    def deciding_kinds(self, kind_at: Callable[[str], str]) -> list[str]:
        """The kinds whose placement decides which journal this record goes to.

        A node's is its own: the one declaration a project makes about where
        a kind lives is read off the record's `kind` and nothing else.
        """
        del kind_at
        return [self.kind]


class Surroundings(BaseModel, frozen=True):
    """One node's neighbourhood, which is everything its standing may read.

    The edges and the nodes at their far ends, resolved once by whoever is
    asking. Both are needed and neither alone is enough: an edge says which
    relation holds, and the node at the other end says whether it still does —
    a task is blocked by a blocker that has not finished, and the edge cannot
    know that about the far end.

    The far ends arrive as :class:`LedgerNode`, so what a neighbour can be
    asked is what the base declares. That is a real constraint and the right
    one: a question one type wants to ask of its neighbours is a question the
    base should name, answered or declined by each.
    """

    incoming: list[LedgerEdge] = []
    outgoing: list[LedgerEdge] = []
    neighbours: list[LedgerNode] = []
    """Every node at the far end of one of those edges, in no order.

    By id rather than by position, because one node may sit at the end of
    several edges and duplicating it would make a count of blockers wrong.
    """

    root: Path | None = None
    """The working tree this node's evidence was recorded against, if any.

    Part of the neighbourhood because evidence rots against files: an
    artifact checked over a scope of paths stops standing the moment one of
    them changes, and only the tree can say whether one has. Absent where a
    caller has no tree — a snapshot read on another machine — and a type that
    needs one says it could not check rather than guessing.
    """

    standing_of: Callable[[str], Standing] | None = None
    """How to ask any node in the log where it stands, as deep as that goes.

    Supplied by the store, which remembers each answer and refuses to loop.
    Without it a neighbour can be asked only about itself over an empty
    neighbourhood — one hop — which is enough for evidence and not for a claim
    resting on a claim resting on a refuted claim. The transitive reading is
    what lets one landing invalidate every claim resting on it at once, and
    it has to reach the whole chain.
    """

    def at(self, node_id: str) -> LedgerNode | None:
        """The neighbour with this id, or nothing where it was not resolved."""
        return next((node for node in self.neighbours if node.id == node_id), None)

    def standing_at(self, node_id: str) -> Standing | None:
        """Where a neighbour stands, as deep as this asker can see.

        Through the store's reader where there is one, so a premise's own
        premises count; otherwise the neighbour over an empty neighbourhood,
        which is the one-hop reading a caller without a store still gets.
        Nothing where the id resolves to no neighbour at all.
        """
        if self.standing_of is not None:
            return self.standing_of(node_id)
        node = self.at(node_id)
        return node.standing(Surroundings(root=self.root)) if node is not None else None

    def held_by(self, kind: str) -> list[LedgerEdge]:
        """Every incoming edge of one relation whose far end is not finished.

        The shape a standing evaluator actually wants: what is still holding
        this node back, rather than what once did. An edge whose source has
        since finished stops counting without anybody having to go back and
        amend it — which is the same reason nothing here stores a status.
        """
        return [
            edge
            for edge in self.incoming
            if edge.kind == kind
            and not ((source := self.at(edge.source)) and source.finished())
        ]
