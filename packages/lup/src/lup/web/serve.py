"""Standing a local-only page up, once, for every surface that has one.

:mod:`lup.web.loopback` argues that binding loopback and checking the ``Host``
header belong together, and that a surface skipping the second has no way of
knowing it has. The half that *invokes* them belongs here too: left to each
application, every surface writes its own construct-guard-open-run sequence,
each with the same one line to forget.

What is shared is the whole sequence: refuse a non-loopback bind, build the
URL the guard will answer for, say where the page is, open it, serve it. What
differs is only the routes, which arrive as a factory taking the URL — it has
to, because the ``Host`` values a page answers for are derived from the port
it ends up on.
"""

import mimetypes
import time
import webbrowser
from collections.abc import Callable, Sequence
from html.parser import HTMLParser
from importlib import resources
from importlib.resources.abc import Traversable
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

import typer
import uvicorn
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, PlainTextResponse, Response
from jinja2 import Environment, StrictUndefined, Template
from pydantic import BaseModel

from lup.web.loopback import guard_loopback_host, refuse_non_loopback


def bundle_root(surface: str, bundles: Path | None = None) -> Traversable:
    """Where one built surface sits: under ``lup.web``'s package data, or a tree named.

    Refused with the command that builds it where nothing is built, so a
    missing bundle is a clear sentence rather than a directory listing or a
    file-not-found somewhere inside a request.
    """
    root: Traversable = (
        bundles / surface
        if bundles is not None
        else resources.files("lup.web").joinpath("bundles", surface)
    )
    if not root.joinpath("index.html").is_file():
        raise ValueError(
            f"no bundle is built for the {surface!r} surface; run"
            " `uv run lup-devtools harness generate all` with bun installed"
        )
    return root


def bundle_template(surface: str, bundles: Path | None = None) -> Template:
    """One built surface's export template, compiled to autoescape what it is handed.

    Where a surface exports, its build emits ``export.html.j2`` beside
    ``index.html``: the page itself with the script and the stylesheet inline,
    made safe to inline when they were built, and the mount element carrying
    ``data-lup-export="{{ log }}"``. Compiled with autoescape on, so what a
    render hands it is entity-escaped into that attribute, and with an
    undefined name a refusal rather than an empty string; the bundle's own
    text sits in raw blocks the engine never reads. A surface whose bundle
    holds no template is refused naming the command that builds one.
    """
    found = bundle_root(surface, bundles).joinpath("export.html.j2")
    if not found.is_file():
        raise ValueError(
            f"the {surface!r} bundle holds no export template: the surface does"
            " not export, or its bundle predates one — run"
            " `uv run lup-devtools harness generate all` with bun installed"
        )
    environment = Environment(
        autoescape=True, undefined=StrictUndefined, keep_trailing_newline=True
    )
    return environment.from_string(found.read_text(encoding="utf-8"))


class NamedAssets(HTMLParser):
    """Every file a built page names under its ``assets/``: its scripts and its stylesheets."""

    def __init__(self) -> None:
        super().__init__()
        self.named: list[str] = []

    def handle_starttag(self, tag: str, attrs: Sequence[Sequence[str | None]]) -> None:
        del tag
        for attribute, value in attrs:
            path = PurePosixPath(urlsplit(value or "").path)
            if attribute in ("src", "href") and path.parent.name == "assets":
                self.named.append(path.name)


class BundledAsset(BaseModel, frozen=True):
    """One file of a built surface's ``assets/``, as read."""

    name: str
    content: bytes


class PageBundle(BaseModel, frozen=True):
    """One built surface as it stood when read whole: its page, and every asset beside it."""

    index: str
    assets: list[BundledAsset]

    @classmethod
    def read(cls, root: Traversable) -> "PageBundle":
        """The bundle under ``root`` as it stands now."""
        folder = root.joinpath("assets")
        return cls(
            index=root.joinpath("index.html").read_text(encoding="utf-8"),
            assets=[
                BundledAsset(name=entry.name, content=entry.read_bytes())
                for entry in (folder.iterdir() if folder.is_dir() else [])
                if entry.is_file()
            ],
        )

    def asset(self, name: str) -> BundledAsset | None:
        """The asset called ``name``, where the bundle holds one."""
        return next((asset for asset in self.assets if asset.name == name), None)

    def missing(self) -> list[str]:
        """The assets the page names that the bundle does not hold."""
        named = NamedAssets()
        named.feed(self.index)
        return [name for name in named.named if self.asset(name) is None]


def whole_bundle(
    root: Traversable, attempts: int = 50, pause: float = 0.1
) -> PageBundle:
    """The bundle under ``root`` read whole, again until its page names nothing it lacks.

    A rebuild writes the page and its assets one file at a time, so a read
    landing between two of them can hold a page naming an asset not written
    yet, or one already removed. It is read again, ``attempts`` times
    ``pause`` apart; one still lacking an asset after that is refused,
    naming what it lacks and the command that rebuilds it.
    """
    bundle = PageBundle.read(root)
    for _ in range(attempts):
        if not bundle.missing():
            return bundle
        time.sleep(pause)
        bundle = PageBundle.read(root)
    lacking = bundle.missing()
    if lacking:
        raise ValueError(
            f"the bundle at {root} names assets it does not hold "
            f"({', '.join(lacking)}); run `uv run lup-devtools harness generate "
            "all` with bun installed"
        )
    return bundle


def bundle_app(
    title: str,
    url: str,
    surface: str,
    bundles: Path | None = None,
    *,
    origins: Callable[[], Sequence[str]] = tuple,
    refusal: str = "unexpected Host header",
) -> FastAPI:
    """A Host-guarded app serving one built surface: its page and its assets.

    The third shape beside a generated page and an edited asset: a bundle
    Vite built from TypeScript into ``lup.web``'s package data, under
    ``bundles/<surface>/``. Read whole once, as the app is built, and served
    from what was read: the page and every asset it names come from one
    build for as long as the app serves, whatever is rebuilt or removed on
    disk beneath it. Served by name rather than mounted as a directory, so a
    request reaches exactly one file the bundle holds and nothing else the
    package does — and a missing bundle is a clear refusal naming the command
    that builds it, not a directory listing. An asset the bundle does not
    hold answers a 404 in words, which a page that asked for it from another
    build cannot mistake for the script it wanted. ``origins`` and
    ``refusal`` are its Host guard's, as :func:`page_app` takes them.
    """
    bundle = whole_bundle(bundle_root(surface, bundles))
    application = page_app(title, url, bundle.index, origins=origins, refusal=refusal)

    @application.get("/assets/{name}")
    async def asset(name: str) -> Response:
        found = bundle.asset(name)
        if found is None:
            return PlainTextResponse(
                f"{name} is not part of the page this server serves; reload the page.",
                status_code=404,
            )
        media_type, _encoding = mimetypes.guess_type(name)
        return Response(
            content=found.content,
            media_type=media_type or "application/octet-stream",
        )

    return application


def page_app(
    title: str,
    url: str,
    html: str,
    *,
    origins: Callable[[], Sequence[str]] = tuple,
    refusal: str = "unexpected Host header",
) -> FastAPI:
    """A Host-guarded app serving one page of HTML at ``/``.

    Takes the markup rather than a package to read it from, so a surface that
    *generates* its page has somewhere to hand it. That is the safer of the two
    by construction: an asset file has to be named in ``package-data`` to reach
    a wheel, and one that is not turns every request to ``/`` into a
    ``FileNotFoundError`` that nothing catches until somebody opens the page.

    The docs routes are off because a local-only page has no audience for
    them and they widen what an attacker reaching this origin can enumerate.
    The Host guard answers loopback and each of the ``origins`` declared for
    the page, telling any other request ``refusal``.
    """
    application = FastAPI(title=title, docs_url=None, redoc_url=None)
    guard_loopback_host(application, url, origins, refusal)

    @application.get("/", response_class=HTMLResponse)
    async def home() -> HTMLResponse:
        return HTMLResponse(html)

    return application


def serve_local_page(
    build: Callable[[str], FastAPI],
    surface: str,
    host: str,
    port: int,
    open_page: bool = True,
) -> None:
    """Bind loopback and serve one page, announcing where it came up.

    ``surface`` names this page in the refusal a bad ``--host`` earns and in
    the line pointing a reader at it, so the message says which of several
    local surfaces is being talked about.

    Raises ``ValueError`` when ``host`` is not a loopback address — the
    caller is a CLI and turns that into whatever its own framework spells a
    bad parameter as.
    """
    refuse_non_loopback(host, surface)
    url = f"http://{host}:{port}"
    typer.echo(f"{surface}: {url}")
    if open_page:
        webbrowser.open(url)
    uvicorn.run(build(url), host=host, port=port)
