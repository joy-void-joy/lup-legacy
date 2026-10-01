"""Handing the operator's clipboard into the container, and nothing else.

A contained session has no access to the operator's display or compositor.
Command-based clients use broker-backed shims. Native X11 clients use an
authenticated private virtual display and a selection bridge to that same
broker, never a connection to the operator's desktop server.

The obvious fix is to mount the host's display socket, and it is the wrong
one twice over. On X11 there is no isolation between clients: anything that
can reach the display can read every other window's input and synthesize its
own, so a session handed the display can type into the terminal the operator
is supervising it from -- around every gate this repository has. And it only
works on X11 at all, which leaves every Wayland and macOS operator with the
hole and none of the clipboard.

What crosses instead is a socket that answers four questions -- what the
clipboard holds as text, what types it is offering, one of those types as
bytes, and set this text -- served by a thread in the launcher, in the
operator's own session, using whatever backend that machine actually has.
The container learns nothing about the host's desktop; a session on a Wayland
laptop and one on an X11 workstation reach the same four questions.

**This is a channel through the boundary, and it is worth naming.** A
confined process can read what the operator has copied, which may be a
password on its way to a password field, and can replace what is on it. Three
things bound it: the four operations are the whole protocol, so nothing else
crosses whatever the container asks; a reply is bounded, so a clipboard
holding a video is refused rather than streamed; and the socket is the
session's own, created per launch and gone with it.
"""

import atexit
import base64
import logging
import shutil
import socketserver
import tempfile
import threading
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, Field, TypeAdapter, ValidationError

from lup.devtools.clipboard import (
    clipboard_image,
    clipboard_text,
    copy_to_clipboard,
    reachable_backend,
    readable_backends,
)
from lup.harness.notice import Notice
from lup.harness.requirements import Package
from lup.types import EnvVars

logger = logging.getLogger(__name__)

type ClipboardTransport = Literal["commands", "x11"]


class ClipboardReply(BaseModel, frozen=True):
    """What the broker answers, in the one shape every operation replies in.

    One reply model rather than one per operation because the client reads
    them all the same way, and a union here would be four shapes to keep in
    step across a socket for no reader that benefits.
    """

    ok: bool = True
    text: str = ""
    types: list[str] = []
    data: str = Field(default="", description="Base64, because JSON carries no bytes")
    error: str = ""
    code: Literal["unsupported_type"] | None = None

    def rendered(self) -> bytes:
        """The reply as one line on the wire."""
        return (self.model_dump_json() + "\n").encode("utf-8")


class ClipboardAsk(BaseModel, ABC, frozen=True):
    """One question the container may put to the operator's clipboard.

    A union answering through its members, so the protocol's surface is the
    list of members: adding a fifth thing a session may ask means declaring
    it here, where the header's claim about the width of the channel can be
    checked against the code that implements it.
    """

    @abstractmethod
    def answer(self, media_types: list[str], limit: int) -> ClipboardReply:
        """Do this on the host's clipboard and say what happened."""


class TextAsk(ClipboardAsk, frozen=True):
    """What the clipboard holds, as text."""

    op: Literal["text"] = "text"

    def answer(self, media_types: list[str], limit: int) -> ClipboardReply:
        """Read the clipboard's text through whatever backend this host has."""
        held = clipboard_text()
        if held is not None and len(held.encode("utf-8")) > limit:
            return ClipboardReply(ok=False, error=f"clipboard text exceeds {limit}")
        return ClipboardReply(text=held or "")


# lup: ignore[library-default] — X selection target names, fixed by the X
# conventions and by what the runtimes reading a clipboard actually ask for
TEXT_TYPES = ("text/plain", "text/plain;charset=utf-8", "UTF8_STRING", "STRING")
"""The names a caller asks for the clipboard's *text* by, as a typed read.

Measured rather than guessed: a runtime enumerating a clipboard asks for
`TARGETS` and then reads `text/plain` the same way it reads `image/png`, so a
bridge that served only the image types would refuse the ordinary paste while
answering the exotic one. These are X selection target names, which is the
vocabulary every caller here already speaks, and they resolve to one thing --
whatever the host's clipboard says its text is.
"""


class TypesAsk(ClipboardAsk, frozen=True):
    """Which of the types this bridge carries the clipboard is offering."""

    op: Literal["types"] = "types"

    def answer(self, media_types: list[str], limit: int) -> ClipboardReply:
        """Report the declared types on offer, so the answer is the channel's width.

        Text is reported under every name it is asked for, because a caller
        enumerating targets and then reading one is choosing from this list:
        a clipboard holding text and admitting to none would be read as empty.
        """
        offered = clipboard_image(tuple(media_types))
        held = clipboard_text()
        return ClipboardReply(
            types=[
                *([offered.media_type] if offered else []),
                *(TEXT_TYPES if held else ()),
            ]
        )


class TypedAsk(ClipboardAsk, frozen=True):
    """The clipboard as one specific type, which is what an image paste needs."""

    op: Literal["typed"] = "typed"
    media_type: str

    def answer(self, media_types: list[str], limit: int) -> ClipboardReply:
        """Hand back one carried type, refusing a type or a size not declared."""
        if self.media_type in TEXT_TYPES:
            return self.encoded((clipboard_text() or "").encode("utf-8"), limit)
        if self.media_type not in media_types:
            return ClipboardReply(
                ok=False,
                code="unsupported_type",
                error=f"{self.media_type} is not carried by this bridge",
            )
        offered = clipboard_image((self.media_type,))
        return self.encoded(offered.data, limit) if offered else ClipboardReply()

    def encoded(self, data: bytes, limit: int) -> ClipboardReply:
        """One payload as base64, refused rather than cut when it is too large."""
        if len(data) > limit:
            return ClipboardReply(
                ok=False,
                error=(
                    f"clipboard holds {len(data)} bytes of {self.media_type}, "
                    f"over this bridge's {limit}"
                ),
            )
        return ClipboardReply(data=base64.b64encode(data).decode("ascii"))


class SetAsk(ClipboardAsk, frozen=True):
    """Put this text on the operator's clipboard."""

    op: Literal["set"] = "set"
    text: str

    def answer(self, media_types: list[str], limit: int) -> ClipboardReply:
        """Write the text, reporting whether any backend on this host took it."""
        if len(self.text.encode("utf-8")) > limit:
            return ClipboardReply(ok=False, error=f"text exceeds {limit}")
        return ClipboardReply(ok=copy_to_clipboard(self.text))


type Ask = TextAsk | TypesAsk | TypedAsk | SetAsk

ClipboardAskAdapter = TypeAdapter[Ask](
    Annotated[TextAsk | TypesAsk | TypedAsk | SetAsk, Field(discriminator="op")]
)
"""The protocol's whole vocabulary, read off the union that implements it.

Discriminated on ``op`` so a request names one member rather than being tried
against each, which is what makes an unknown operation a refusal naming it
instead of four validation errors nobody can act on.
"""


SHIM_ASSET = Path(__file__).parent / "assets" / "clipboard_shim.py"
"""The program every clipboard name inside the container resolves to.

A file rather than a string in this module, so the thing that runs inside the
container is linted, type-checked and importable by a test like any other
Python here -- an embedded literal is a program whose first syntax error is
found by whoever it was copied to. :mod:`lup.harness.assets` says why the
rules it is held to differ.
"""


def shim_program() -> str:
    """The shim's source, read at render time rather than held in memory.

    Read rather than imported because what the image needs is the text: this
    is copied into a container that has neither this package nor its
    dependencies, and importing it here would prove only that *this* machine
    can run it.
    """
    return SHIM_ASSET.read_text(encoding="utf-8")


class ClipboardHandler(socketserver.StreamRequestHandler):
    """One connection: read a question, answer it, close.

    One question per connection rather than a session on the socket, because
    a clipboard read is a whole interaction and a connection that stayed open
    would need framing, timeouts and a reason to still be there.
    """

    def handle(self) -> None:
        """Answer the one line this connection carries.

        The listener is narrowed rather than trusted: a handler is
        constructed by the server class it is registered with, and the
        declaration that carries the bridge is that class rather than the
        base one this signature sees.

        A peer gone by the time the answer is written has already been
        answered or has stopped caring, and either way the clipboard has
        done its work: a client is entitled to close, and a write onto a
        closed socket is this exchange ending rather than failing.

        Both arms end here rather than escaping, because this server runs in
        the launcher -- inside the operator's own terminal, beside a session
        drawing a full-screen interface. The base class prints what escapes
        to stderr, so an exception reaches the operator as a traceback
        painted over the session, for a copy that worked.
        """
        listener = self.server
        if not isinstance(listener, ClipboardServer):
            return
        try:
            answer = listener.bridge.answered(self.rfile.readline())
            self.wfile.write(answer.rendered())
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception:
            logger.warning("clipboard request failed", exc_info=True)


class ClipboardServer(socketserver.ThreadingUnixStreamServer):
    """The listener, threaded so two questions at once do not queue.

    Threaded because a paste and a copy can genuinely overlap -- an agent
    reading an image while the operator's own command writes a line -- and a
    serialized listener would make the second wait on whichever backend the
    first is talking to.
    """

    daemon_threads = True
    bridge: "ClipboardBridge"


class ClipboardBridge(BaseModel, frozen=True):
    """The socket a contained session reaches the operator's clipboard through.

    Declared as one model for the reason the browser bridge is: the launcher
    creates the socket, the image bakes shims pointing at where it will be
    mounted, and the handler decides what is answered. Split up, the shim and
    the mount drift, and the failure is a paste that silently does nothing --
    which is the failure this exists to end.
    """

    inside: str = Field(
        default="/run/lup/clipboard",
        description=(
            "Where the socket's directory is mounted in the container. A "
            "directory rather than the socket itself, for the reason the "
            "browser bridge mounts one: a bind mount of a special file is a "
            "shape the engines disagree about, and a directory is not"
        ),
    )
    channel: str = Field(
        default="socket", description="The socket's name inside that directory"
    )
    variable: str = Field(
        default="LUP_CLIPBOARD_SOCKET",
        description="What tells the shims inside where to reach the broker",
    )
    shims: list[str] = Field(
        default=[
            "xclip",
            "xsel",
            "wl-copy",
            "wl-paste",
            "pbcopy",
            "pbpaste",
            "tmux",
        ],
        description=(
            "The names a clipboard is asked for by, all pointed at one "
            "program. Every name a CLI might reach for is carried because the "
            "session's runtimes are not this repository's to change: a tool "
            "that shells out to `wl-paste` on a host that had X11 would "
            "otherwise find nothing, and the operator would be told their "
            "clipboard is broken rather than that this bridge missed a name. "
            "`tmux` is carried on the same reasoning and is not a clipboard "
            "program: the terminal handoff crosses `TMUX` while the socket it "
            "names stays on the host, so a session is told a multiplexer owns "
            "its screen and then handed no `tmux` to ask -- and a runtime "
            "spelling a copy the way a multiplexer takes one, `load-buffer` "
            "on standard input, finds the contradiction rather than the "
            "clipboard. The shim answers the buffer verbs and refuses the "
            "rest by name, because the server is genuinely out of reach"
        ),
    )
    media_types: list[str] = Field(
        default=["image/png", "image/jpeg", "image/gif", "image/webp"],
        description=(
            "The types that may cross as bytes, which is the whole of what a "
            "paste can carry. A list rather than anything the container asks "
            "for, because 'hand me the clipboard as this type' is a request "
            "to read the operator's machine and the answer is a declaration"
        ),
    )
    limit: int = Field(
        default=32 * 1024 * 1024,
        description=(
            "The largest reply this bridge will assemble, in bytes. A "
            "clipboard holding a video is refused rather than streamed, and "
            "refused rather than cut: a truncated image is a file that opens "
            "to nothing and blames the paste"
        ),
    )

    native_packages: list[str] = ["xorg-server-xvfb", "xorg-xauth", "python-xlib"]

    def path(self) -> str:
        """The socket, spelled as the container sees it."""
        return f"{self.inside}/{self.channel}"

    def packages(self) -> list[Package]:
        """Native compatibility dependencies; command clients start no display."""
        return [Package(name=name) for name in self.native_packages]

    def wrap(self, command: list[str], transport: ClipboardTransport) -> list[str]:
        """Open the native clipboard transport declared by this CLI's adapter."""
        match transport:
            case "commands":
                return command
            case "x11":
                return ["lup-clipboard-x11", "--limit", str(self.limit), "--", *command]

    def native_program(self) -> str:
        """The independently checked program installed in the image."""
        return (Path(__file__).parent / "assets" / "clipboard_x11.py").read_text(
            encoding="utf-8"
        )

    def environment(self) -> EnvVars:
        """What tells the shims inside where to reach this broker, and nothing else.

        No display. The bridge speaks no X protocol, and a `DISPLAY` naming
        it would send every X client in the session to an address with no
        server behind it -- headless Chromium among them, which then gives
        every page no WebGL at all. A runtime that finds no display reads the clipboard
        through the shims by name and copies through the `tmux` shim or the
        terminal's own escape sequence, as Claude Code does (measured on
        2.1.283: it looks for `xclip` to copy only where `DISPLAY` is set); a
        runtime whose clipboard is native X11 is given a real display of its
        own by the private-display wrapper, which speaks it.
        """
        return {self.variable: self.path()}

    def answered(self, line: bytes) -> ClipboardReply:
        """One request line, answered or refused.

        A malformed line is refused rather than raised: the alternative is a
        launcher thread dying of whatever the container wrote, taking the
        clipboard down for the rest of a session over one bad request.
        """
        try:
            ask = ClipboardAskAdapter.validate_json(line.decode("utf-8"))
        except (ValidationError, UnicodeDecodeError, ValueError) as error:
            return ClipboardReply(ok=False, error=f"unreadable request: {error}")
        return ask.answer(self.media_types, self.limit)

    def serve(self) -> Path | None:
        """Start listening, and answer with the directory to mount.

        Nothing comes back when the socket cannot be made, which is a launch
        without a clipboard rather than a launch that fails -- the session
        still runs, and the notice says what is missing.

        The directory is this launch's own, so two sessions open two brokers
        on two sockets and neither can see the other's. They reach one
        clipboard, because the operator has one.
        """
        try:
            directory = Path(tempfile.mkdtemp(prefix="lup-clipboard-"))
            server = ClipboardServer(str(directory / self.channel), ClipboardHandler)
        except OSError as error:
            # Logged rather than swallowed: a launch without a clipboard is an
            # ordinary outcome, but "why" is the difference between a machine
            # that cannot have one and a path this got wrong.
            logger.warning("clipboard bridge did not open: %s", error)
            return None
        server.bridge = self
        (directory / self.channel).chmod(0o600)
        atexit.register(server.shutdown)
        atexit.register(shutil.rmtree, directory, True)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return directory

    def notice(self, serving: bool) -> list[Notice]:
        """What an operator is told about their clipboard crossing.

        Said whichever way it went, because all three answers change what they
        do next: an operator who does not know the bridge is there is one who
        will not paste a screenshot, one who does not know it is absent reads a
        dead Ctrl+V as the runtime being broken, and one told the bridge works
        when the host end answers nothing debugs the container.

        Three rather than two, and the third is the one a socket cannot give.
        The socket opening says a listener exists; it says nothing about
        whether anything on this machine answers a clipboard read, and a
        promise made on the strength of it alone -- "this session can read and
        replace what you have copied" -- is asserted every launch and measured
        on none. The capability requirement that would catch it is
        ``checked="setup"`` by design, so the one moment it matters is the one
        moment nothing asks.

        Named backends rather than a platform. Which tool answers is a fact
        about the machine and the process asking, not about the desktop: a
        Wayland session usually also answers through XWayland, an X11 session
        over a forwarded display answers only while that display is reachable
        from *this* process, and a headless host answers through none of them.
        So the absent case lists what was tried, in order, and leaves the
        reader to see whether their own is missing or simply not answering
        here. Naming a package would be right on one distribution and wrong on
        the rest.
        """
        if not serving:
            return [
                Notice(
                    text=(
                        "Clipboard: not available in this session; copy and "
                        "paste reach the container's own empty clipboard."
                    ),
                    urgency="boundary",
                )
            ]
        answering = reachable_backend()
        if not answering:
            return [
                Notice(
                    text=(
                        "Clipboard: the bridge is up and nothing on this "
                        "machine answered a clipboard read, so copy and paste "
                        "will come back empty. Tried "
                        f"{', '.join(readable_backends())} in that order — one "
                        "of them being installed is not enough, it also has to "
                        "reach a display from the process that launched this "
                        "session."
                    ),
                    urgency="warning",
                )
            ]
        return [
            Notice(
                text=(
                    f"Clipboard broker: {answering} answered on the host; "
                    f"{len(self.media_types)} image types are allowed. "
                    "Command clients use its shims; native X11 clients require "
                    "the private display checked at CLI startup. Neither "
                    "transport reaches the rest of your desktop."
                ),
                urgency="boundary",
            )
        ]
