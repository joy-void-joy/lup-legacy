"""The ledger explorer: a data explorer over the log, served or exported.

The shape is a single HTML file — curated trails as list and detail, every
record searchable, a topic register, browser history for every view, the whole
dataset embedded so a memo attachment opens with no server behind it — over
the ledger, generic over whatever kinds a project declares: the page is the
TypeScript surface Vite builds into `lup.web`'s package data, and what it
reads is the same view models the tool group returns, served here as JSON
routes or embedded whole for an export.

**Two ways to open it, one page.** `serve` answers the routes on the loopback
and the page fetches them; `export` renders the template the build emitted
beside the page — the page itself, its script and stylesheet inline and made
safe to inline when they were built — over the log, which rides in an
attribute of the mount point, entity-escaped by the template engine, so a
node whose text happens to contain a closing tag cannot break the page it is
shown on.
"""

from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException
from jinja2 import Template

from lup.channels.models import utc_now
from lup.coordination.identity import mint_member_id
from lup.coordination.refs import ActorRef
from lup.ledger.journal import LedgerStore
from lup.ledger.models import LedgerEdge, LedgerNode
from lup.ledger.store import LedgerLayout
from lup.ledger.views import (
    ExportView,
    GraphView,
    KindsView,
    NodeDetail,
    graph_view,
    kinds_view,
    node_detail,
)
from lup.web.serve import bundle_app, bundle_template, serve_local_page

# lup: ignore[constant-declaration] — an identity this repository defines: the
# bun workspace entry and the bundle it builds to are both named by this word
SURFACE = "explorer"
"""Which built surface this is, under `lup.web`'s bundles.

An identity of the layout rather than a choice: the bun workspace's entry and
the bundle it builds to are both named by this word, and a caller spelling it
differently would serve nothing.
"""


def opened(root: Path, layout: LedgerLayout) -> LedgerStore:
    """The store, opened to read; a reader mints a console identity like the CLI."""
    return LedgerStore(root, ActorRef(kind="console", id=mint_member_id()), layout)


def since_moment(spelling: str) -> datetime | None:
    """An ISO 8601 moment from a query parameter, or a refusal a client reads."""
    if not spelling:
        return None
    try:
        return datetime.fromisoformat(spelling)
    except ValueError as invalid:
        raise HTTPException(
            status_code=422, detail=f"since must be ISO 8601: {invalid}"
        ) from invalid


def explorer_app(
    url: str,
    root: Path,
    classes: list[type[LedgerNode]],
    edges: list[type[LedgerEdge]],
    bundles: Path | None = None,
    layout: LedgerLayout = LedgerLayout(),
) -> FastAPI:
    """The explorer over one repository's log: its page, and the routes it reads.

    Every route serializes a `lup.ledger.views` model, so what the page is
    typed against is exactly what the tool group returns.
    """
    application = bundle_app("Ledger explorer", url, SURFACE, bundles)

    @application.get("/api/graph")
    async def graph(
        kind: str = "", standing: str = "", since: str = "", lacking: str = ""
    ) -> GraphView:
        return graph_view(
            opened(root, layout),
            classes,
            kind=kind,
            standing=standing,
            since=since_moment(since),
            lacking=lacking,
        )

    @application.get("/api/node/{spelling}")
    async def node(spelling: str) -> NodeDetail:
        store = opened(root, layout)
        found = store.resolve(spelling, classes)
        if found is None:
            raise HTTPException(
                status_code=404, detail=f"no node has the id or slug {spelling!r}"
            )
        return node_detail(store, classes, found)

    @application.get("/api/kinds")
    async def kinds() -> KindsView:
        return kinds_view(classes, edges, layout)

    return application


def export_view(
    root: Path,
    classes: list[type[LedgerNode]],
    edges: list[type[LedgerEdge]],
    layout: LedgerLayout = LedgerLayout(),
) -> ExportView:
    """The whole log as one value, for a page that cannot ask for more later."""
    store = opened(root, layout)
    with store.batch():
        return ExportView(
            graph=graph_view(store, classes),
            details=[
                node_detail(store, classes, node) for node in store.all_nodes(classes)
            ],
            kinds=kinds_view(classes, edges, layout),
            exported_at=utc_now(),
        )


def export_page(view: ExportView, template: Template) -> str:
    """One self-contained page: the surface's export template rendered over the log.

    The template is the built `index.html` with the bundle inline, emitted by
    the build beside it, so no markup is authored here; the one value handed
    to it is the log as JSON, which autoescape entity-escapes into the mount
    element's `data-lup-export` attribute. The app reads that attribute
    before it would fetch, so the same bundle serves both ways.

    The two other shapes are the user's refusals, not open alternatives: the
    bundle as base64 `data:` URLs, and escaping done by a substitution here.
    """
    return template.render(log=view.model_dump_json())


def export_explorer(
    root: Path,
    classes: list[type[LedgerNode]],
    edges: list[type[LedgerEdge]],
    destination: Path,
    bundles: Path | None = None,
    layout: LedgerLayout = LedgerLayout(),
) -> Path:
    """Write the explorer as one file holding the log as it is now.

    The result opens from a file, mails as an attachment, and shows exactly
    what the log held when it was written — the page says when.
    """
    page = export_page(
        export_view(root, classes, edges, layout), bundle_template(SURFACE, bundles)
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(page, encoding="utf-8")
    return destination


def serve_explorer(
    root: Path,
    classes: list[type[LedgerNode]],
    edges: list[type[LedgerEdge]],
    host: str,
    port: int,
    open_page: bool = True,
    layout: LedgerLayout = LedgerLayout(),
) -> None:
    """Bind the loopback and serve the explorer over this repository's log."""
    serve_local_page(
        lambda url: explorer_app(url, root, classes, edges, layout=layout),
        "Ledger explorer",
        host,
        port,
        open_page,
    )
