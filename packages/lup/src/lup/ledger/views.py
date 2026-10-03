"""What a reader is shown of a node, built once for every surface that shows one.

The tool group, the explorer's routes and the console all answer the same
questions about a node — what is it, where does it stand right now and why,
what points at it and how much — and an answer built in three places is three
answers that drift. So the view models and the folds that fill them live here,
and each surface serializes the same value.
"""

from datetime import datetime

from pydantic import BaseModel

from lup.ledger.journal import LedgerStore
from lup.ledger.kinds import KindInfo, describe, kind_of
from lup.ledger.models import LedgerEdge, LedgerNode
from lup.ledger.store import LedgerLayout
from lup.types import JsonObject


class NodeView(BaseModel, frozen=True):
    """One node as a reader meets it: what it is and where it stands right now."""

    id: str
    kind: str
    title: str
    slug: str = ""
    text: str = ""
    priority: int = 0
    standing: str
    reason: str = ""
    sound: bool = True
    author: str
    """Who recorded it, as the store stamped it: the actor's label.

    Shown on every surface a node is met on, because who found a thing is
    the question a reader weighing it asks next, and a writer cannot spell
    the answer: the store stamps it from the session's identity.
    """

    moved: datetime
    """When the log last grew around this node: its own record, or an edge touching it.

    Read off the log's timestamps rather than stored, so a reader opening on
    what moved since they last looked, or sorting a listing by recency, reads
    the same fact `--since` narrows by.
    """


class EdgeView(BaseModel, frozen=True):
    kind: str
    source: str
    target: str


class EdgeCount(BaseModel, frozen=True):
    """How many edges of one kind point at a node — its weight in the log."""

    kind: str
    count: int


class NodeDetail(BaseModel, frozen=True):
    """One node in full: its own fields, its weight, and every edge in and out."""

    node: NodeView
    fields: JsonObject
    """The kind's own fields — a grade, a holder, a scope — beyond the base."""

    incoming: list[EdgeCount]
    edges_in: list[EdgeView]
    edges_out: list[EdgeView]
    attachments: list[str]


class GraphView(BaseModel, frozen=True):
    """The whole log as a graph, with the vocabularies a reader filters by."""

    nodes: list[NodeView]
    edges: list[EdgeView]
    kinds: list[str]
    standings: list[str]


class KindsView(BaseModel, frozen=True):
    """What a project declares: its node kinds and its relations, with their fields."""

    nodes: list[KindInfo]
    edges: list[KindInfo]


class ExportView(BaseModel, frozen=True):
    """Everything a standalone page needs, so it opens with no server behind it.

    The graph, every node's detail and the declared kinds, because a page
    opened from a file cannot ask for more later, and a memo attachment that
    needs a server is not one. Stamped with the moment
    it was taken, since what it shows is the log as it was then.
    """

    graph: GraphView
    details: list[NodeDetail]
    kinds: KindsView
    exported_at: datetime


def node_view(
    store: LedgerStore,
    classes: list[type[LedgerNode]],
    node: LedgerNode,
    movements: dict[str, datetime] | None = None,
) -> NodeView:
    """One node with its standing read now.

    ``movements`` is the store's fold of when the log last grew around each
    node, passed in by a caller building many views so the log is read once
    for all of them; a caller building one view lets this read it.
    """
    where = store.standing(node, classes)
    moved = movements if movements is not None else store.movements()
    return NodeView(
        id=node.id,
        kind=node.kind,
        title=node.title,
        slug=node.slug,
        text=node.text,
        priority=node.priority,
        standing=where.label,
        reason=where.reason,
        sound=where.sound,
        author=node.author.label(),
        moved=moved.get(node.id, node.at),
    )


def edge_view(kind: str, source: str, target: str) -> EdgeView:
    return EdgeView(kind=kind, source=source, target=target)


def kinds_view(
    classes: list[type[LedgerNode]],
    edges: list[type[LedgerEdge]],
    layout: LedgerLayout = LedgerLayout(),
) -> KindsView:
    """Every declared kind as a reader is told about it, with where each is kept."""
    return KindsView(
        nodes=[
            describe(declared, layout.placement(kind_of(declared)))
            for declared in classes
        ],
        edges=[describe(declared) for declared in edges],
    )


def node_detail(
    store: LedgerStore, classes: list[type[LedgerNode]], node: LedgerNode
) -> NodeDetail:
    """One node in full, the edges pointing at it counted by kind first."""
    incoming = store.into(node.id)
    outgoing = store.out_of(node.id)
    kinds = list(dict.fromkeys(edge.kind for edge in incoming))
    return NodeDetail(
        node=node_view(store, classes, node),
        fields={
            name: value
            for name, value in node.model_dump(mode="json").items()
            if name not in LedgerNode.model_fields
        },
        incoming=[
            EdgeCount(kind=kind, count=sum(1 for edge in incoming if edge.kind == kind))
            for kind in kinds
        ],
        edges_in=[edge_view(edge.kind, edge.source, edge.target) for edge in incoming],
        edges_out=[edge_view(edge.kind, edge.source, edge.target) for edge in outgoing],
        attachments=list(node.attachments),
    )


def graph_view(
    store: LedgerStore,
    classes: list[type[LedgerNode]],
    kind: str = "",
    standing: str = "",
    since: datetime | None = None,
    lacking: str = "",
) -> GraphView:
    """The log as nodes and edges, narrowed the way a reader asked.

    An edge is kept only where both its ends survived the narrowing, so a
    filtered graph never draws a line to a node it is not showing. The kind
    and standing vocabularies are read off the whole log, not the narrowed
    one, so the filters a reader is offered do not shrink as they are used.
    ``lacking`` keeps the nodes from which no edge of that kind runs — what
    was found here rather than read from somewhere — read off the whole
    log's edges before any narrowing, so a hidden far end still counts.
    """
    with store.batch():
        movements = store.movements()
        every = [
            node_view(store, classes, node, movements)
            for node in store.all_nodes(classes)
        ]
        edges = store.edges()
    moved = store.moved_since(since) if since is not None else None
    pointing = {edge.source for edge in edges if lacking and edge.kind == lacking}
    shown = [
        node
        for node in every
        if (not kind or node.kind == kind)
        and (not standing or node.standing == standing)
        and (moved is None or node.id in moved)
        and node.id not in pointing
    ]
    ids = {node.id: node for node in shown}
    return GraphView(
        nodes=shown,
        edges=[
            edge_view(edge.kind, edge.source, edge.target)
            for edge in edges
            if edge.source in ids and edge.target in ids
        ],
        kinds=list(dict.fromkeys(node.kind for node in every)),
        standings=list(dict.fromkeys(node.standing for node in every)),
    )
