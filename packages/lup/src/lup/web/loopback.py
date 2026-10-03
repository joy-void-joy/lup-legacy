"""What a local-only web surface has to do to stay local-only.

A page served here is reachable by whatever else runs on this machine, and by
any site the browser is pointed at. Two different boundaries answer those,
and only the first is a bind:

**Binding loopback** stops packets from the network. That is the whole of it
— it says nothing about who on this host may connect, and nothing about what
a page from elsewhere may ask the browser to do.

**Checking the Host header** stops DNS rebinding, where a site the user is
merely visiting resolves its own name to ``127.0.0.1`` and then reads this
origin as its own. The same-origin policy does not help, because to the
browser it *is* the same origin; CORS does not help, because the request is
not cross-origin. What still differs is the ``Host`` header the browser
sends, which carries the attacker's name rather than a loopback one.

So the two go together, and a surface that takes the first without the second
is open to any page the user happens to have loaded. Both live here rather
than in one application's server, because the reasoning is the same wherever
a local page accepts a mutating request, and because a surface that skips it
has no way of knowing it has.

Not defended: other processes on this machine, which only a token would
address.

A page the person reaches through a reverse proxy of their own is reached at
the proxy's name, which the guard answers only where that origin is declared
for the surface: every other name is still one a rebinding site could send.
"""

from collections.abc import Callable, Sequence
from urllib.parse import urlsplit

from fastapi import FastAPI
from fastapi.responses import PlainTextResponse
from starlette.datastructures import Headers
from starlette.types import ASGIApp, Receive, Scope, Send

# lup: ignore[library-default] — the loopback interface's own spellings, fixed by the OS and the name it always resolves to; omitting one is a hole rather than a preference
LOOPBACK_HOSTS = [
    "127.0.0.1",
    "localhost",
    "::1",
]
"""Every spelling of this machine, as a browser or a bind address writes it."""

# lup: ignore[constant-declaration] — HTTP's own status code for the condition
MISDIRECTED_REQUEST = 421
"""The status for a request whose authority this server does not answer for."""


def refuse_non_loopback(host: str, surface: str) -> None:
    """Raise unless ``host`` is a loopback address this surface may bind.

    The check is on the value the operator asked for rather than on what the
    socket ended up bound to, so the refusal names the flag they passed.
    """
    if host not in LOOPBACK_HOSTS:
        raise ValueError(
            f"the {surface} binds loopback only; {host!r} is not one of: "
            + ", ".join(LOOPBACK_HOSTS)
        )


def origin_host_values(origin: str) -> list[str]:
    """Every ``Host`` header value a browser sends for a page at ``origin``.

    ``origin`` is written as a browser writes it, its default port left out:
    there the host is sent with and without that port, and a port of its own
    only with it.
    """
    split = urlsplit(origin)
    if split.port is not None:
        return [split.netloc]
    default = 443 if split.scheme == "https" else 80
    return [split.netloc, f"{split.netloc}:{default}"]


def allowed_host_values(url: str, origins: Sequence[str] = ()) -> list[str]:
    """Every ``Host`` header value a page served at ``url``, and at ``origins``, will answer to.

    Both the bare host and the host-with-port forms are admitted, because a
    browser omits the port when it is the scheme's default and sends it
    otherwise, and this surface is reached both ways.
    """
    port = urlsplit(url).port
    authorities = [f"[{host}]" if ":" in host else host for host in LOOPBACK_HOSTS]
    return [
        *authorities,
        *(f"{host}:{port}" for host in authorities if port is not None),
        *(value for origin in origins for value in origin_host_values(origin)),
    ]


class LoopbackHost:
    """Refuse any request whose ``Host`` is not one the surface answers for, before it is served.

    Plain ASGI rather than an ``http`` middleware function: that wraps every
    response body in a task group of its own, so a stream the server ends as
    it stops is cancelled there and logged as an error instead of ending.
    """

    def __init__(
        self, app: ASGIApp, allowed: Callable[[], list[str]], refusal: str
    ) -> None:
        self.app = app
        self.allowed = allowed
        self.refusal = refusal

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            headers = Headers(scope=scope)
            header = headers["host"] if "host" in headers else ""
            if header not in self.allowed():
                refused = PlainTextResponse(
                    status_code=MISDIRECTED_REQUEST, content=self.refusal
                )
                await refused(scope, receive, send)
                return
        await self.app(scope, receive, send)


def guard_loopback_host(
    app: FastAPI,
    url: str,
    origins: Callable[[], Sequence[str]] = tuple,
    refusal: str = "unexpected Host header",
) -> None:
    """Refuse any request whose ``Host`` is not one this surface answers for.

    That is loopback's, and each of the ``origins`` declared for it, asked
    on every request so a declaration changed while it serves holds from the
    next. ``refusal`` is what a refused request is told.
    """
    app.add_middleware(
        LoopbackHost,
        allowed=lambda: allowed_host_values(url, origins()),
        refusal=refusal,
    )
