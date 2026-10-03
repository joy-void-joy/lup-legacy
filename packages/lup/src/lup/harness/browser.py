"""Handing a URL out of the container to a browser on the operator's machine.

A sign-in is the one thing a contained session cannot finish alone. The CLI
reaches its servers through the proxy and gets an authorization URL, and then
needs a browser -- which is on the other side of the boundary, along with the
operator, the password manager and the second factor. Nothing in the image
can open one, and nothing should: a browser inside would be a browser with
the session's filesystem under it.

So the URL crosses instead, and only the URL. The container writes it to a
named pipe; a thread in the launcher reads that pipe and hands admitted URLs
to :mod:`webbrowser`, which is the standard library's answer to what "open a
browser" means on this operating system.

**This is a channel out of the boundary, and it is the only one.** A confined
process can make the operator's desktop open a page, which is a real
capability and worth naming rather than burying: a page can begin a download,
present a form, or simply be a URL the operator then trusts because their own
tooling opened it. Three things bound it. The pipe carries a URL and nothing
else -- no path, no command, no reply back. Only https is opened. And the
host is compared after parsing, against a declared list of the addresses a
sign-in actually visits, so a page is opened for the flow this exists for and
for nothing else.

One-way is a decision with a visible cost, and it is better stated here than
rediscovered. The CLI asks to be redirected to a loopback port it is listening
on, and whether the operator's browser reaches that port is the network
posture's answer rather than this module's. A session holding its own network
namespace listens on a loopback that browser cannot reach, so the sign-in ends
on a browser error and finishes by hand -- the page's code, pasted back at the
prompt. A session sharing the host's namespace is listening on the very port
the browser opens, and the redirect lands with nothing left to do.

Both are said at launch, and saying the wrong one is the failure worth naming:
an operator not told about the dead tab reads it as the bridge being broken and
goes looking in the wrong place, and one told to expect a dead tab that never
arrives waits to be rescued from a flow that already finished. What stays
refused in either posture is carrying the reply back *through this pipe*. The
sharing that completes the flow is the network's to grant and is declared where
the posture is declared; nothing here opens a path inward to make up for its
absence.

The comparison is the part worth being careful about, because every cheap
version of it is wrong in the same direction. ``startswith`` admits
``https://claude.ai.evil.test``; ``in`` admits
``https://evil.test/?next=https://claude.ai``; ``endswith`` admits
``https://notclaude.ai``. Only a parser knows which span of a URL is the
host, which is the general rule this repository keeps relearning about
structured data, arriving here as a security property rather than a
tidiness one.
"""

import atexit
import os
import shlex
import shutil
import tempfile
import threading
import webbrowser
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import BaseModel, Field

from lup.harness.egress import AllowedHost
from lup.harness.notice import Notice
from lup.types import EnvVars

SIGN_IN_ADDRESSES = [
    AllowedHost(host="claude.ai", because="claude.ai account authentication"),
    AllowedHost(
        host="claude.com",
        because="the page a claude.ai sign-in opens, which redirects to claude.ai",
    ),
    AllowedHost(
        host="platform.claude.com",
        because="Console authentication, and the OAuth token exchange for both",
    ),
    AllowedHost(host="auth.openai.com", because="Codex sign-in"),
    AllowedHost(host="chatgpt.com", because="the account a Codex sign-in lands on"),
]
"""Where a sign-in actually goes, per runtime, with what needed each.

Taken from each vendor's own published list rather than from a memory of one,
and kept as :class:`~lup.harness.egress.AllowedHost` so the reason travels
with the entry -- an address nobody can say why about is one to take out, and
this list has exactly the accretion problem that model exists for.

Both runtimes are here because one image starts both and neither should be
the one that silently does not work. An adopter whose runtime is neither
replaces the list; that is what makes it a default rather than a constant.
"""


class BrowserBridge(BaseModel, frozen=True):
    """The pipe a contained session hands a sign-in URL across.

    Declared as one model because the three facts have to agree and are read
    in three different places: the launcher creates the pipe, the image bakes
    a script pointing at where it will be mounted, and the reader decides
    what is admitted. Split up, the script and the mount drift and the
    failure is a sign-in that prints a URL nobody receives.
    """

    inside: str = Field(
        default="/run/lup/browser",
        description=(
            "Where the pipe's directory is mounted in the container. A "
            "directory rather than the pipe itself, because a bind mount of "
            "a named pipe is a special file the engines disagree about, and "
            "a directory is the shape both take"
        ),
    )
    channel: str = Field(
        default="open",
        description="The pipe's name inside that directory",
    )
    opener: str = Field(
        default="/usr/local/bin/lup-open",
        description=(
            "The script `BROWSER` names, which is how a CLI is told what "
            "'open a browser' means here. Claude Code documents honouring "
            "this variable; a runtime that opens a page by some other route "
            "reaches nothing, and prints its URL as it would have anyway"
        ),
    )
    admits: list[AllowedHost] = Field(
        default=SIGN_IN_ADDRESSES,
        description=(
            "Addresses whose pages are opened on the operator's machine. "
            "Anything else the container writes is dropped, because this is "
            "a channel out of the boundary and its width is this list"
        ),
    )
    paste_back: str = Field(
        default=(
            "The tab lands on `Unable to connect`: that address is a loopback "
            "port inside this container, and the browser resolving it is on "
            "your machine. Nothing has gone wrong — copy `code` and `state` "
            "out of its address bar, join them with `#`, and paste that at "
            "the `Paste code here if prompted` prompt."
        ),
        description=(
            "How the second half of the flow is asked for. Held once and "
            "rendered twice -- into the launch notice and into the opener -- "
            "because two spellings of one instruction drift, and the one "
            "that drifts is the one nobody reads until a sign-in is stuck"
        ),
    )
    completes: str = Field(
        default=(
            "Sign-in callbacks return directly to this session through your "
            "machine's shared localhost; nothing needs to be pasted."
        ),
        description=(
            "The same instruction for the posture that has no second half. "
            "Held beside its opposite rather than derived from it, because "
            "both are read in the same two places, and a launch saying the "
            "wrong one sends an operator to copy a code out of a page that "
            "already worked -- or, the other way, leaves them waiting to be "
            "told how to rescue a tab that never failed"
        ),
    )

    def ending(self, lands: bool) -> str:
        """How the flow finishes, in the posture it actually finishes under."""
        return self.completes if lands else self.paste_back

    def path(self) -> str:
        """The pipe, spelled as the container sees it."""
        return f"{self.inside}/{self.channel}"

    def environment(self) -> EnvVars:
        """What tells a CLI inside how to open a browser."""
        return {"BROWSER": self.opener}

    def script(self, lands: bool = False) -> str:
        """The opener the image bakes, which is handed one URL as its argument.

        Prints before it writes, and writes only to a pipe that is really
        there. Both halves are about a launch where nothing is listening -- a
        probe, a one-off `run`, an operator who turned the bridge off -- in
        which a plain redirection would block until a reader appeared and
        take the sign-in with it. Printing first means the URL survives that
        either way, which is the whole of what the flow needs from this in
        the worst case.

        Says how the flow ends at the moment it starts one, which is the
        difference between an instruction and a warning nobody has. The
        launch says the same thing, but it says it among every other
        boundary notice and minutes before anyone types `/login`; this runs
        as the tab opens, and the operator reads it while looking at the
        error it is about. Quoted by :mod:`shlex` rather than by hand,
        because the text is prose someone will edit and one apostrophe in it
        would close the string and leave the rest as shell.
        """
        return "\n".join(
            [
                "#!/bin/sh",
                "printf '\\nlup: opening on the host:\\n  %s\\n\\n' \"$1\" >&2",
                f"printf '  %s\\n\\n' {shlex.quote(self.ending(lands))} >&2",
                f'[ -p "{self.path()}" ] || exit 0',
                f'timeout 5 sh -c \'printf "%s\\n" "$1" > "$2"\' _ "$1" '
                f'"{self.path()}" 2>/dev/null || true',
            ]
        )

    def admitted(self, line: str) -> str:
        """The URL to open, or nothing at all.

        Parsed rather than matched. ``hostname`` is the only thing that knows
        which span of a URL is the host, and every substring test put in its
        place admits a different forgery -- a subdomain of an attacker's
        domain, the same name in a query parameter, or a name this one is a
        suffix of.
        """
        parsed = urlsplit(line.strip())
        admitted = {item.host for item in self.admits}
        return (
            line.strip()
            if parsed.scheme == "https" and parsed.hostname in admitted
            else ""
        )

    def notice(self, serving: bool, lands: bool = False) -> list[Notice]:
        """Say that this channel exists, because it is one and nothing else does.

        A boundary nobody was told about is one whose refusals get debugged
        as something else, and the same holds for a boundary's one opening:
        an operator whose browser opens by itself should have been told what
        can do that, before it happens rather than while it does.

        One line for the posture, and the sign-in instructions subordinate to
        it rather than beside it. Three clauses in that line explaining that
        a channel carrying a URL and nothing else carries nothing else would
        print at every launch, whether or not anybody is about to sign in.
        What stands is the fact an operator acts on: that their browser may
        open by itself, and for how many addresses. The reasoning is this
        module's header.
        """
        if not serving:
            return [
                Notice(
                    text=(
                        "Browser access: sign-in URLs are printed instead of "
                        "opening automatically."
                    ),
                    urgency="boundary",
                ),
                Notice(text=self.ending(lands), urgency="detail", indent=1),
            ]
        return [
            Notice(
                text=(
                    "Browser access: this session may open HTTPS sign-in pages "
                    f"on your machine for {len(self.admits)} approved hosts; the "
                    "bridge carries no other data."
                ),
                urgency="boundary",
            ),
            Notice(text=self.ending(lands), urgency="detail", indent=1),
        ]

    def serve(self) -> Path | None:
        """Start listening, and answer with the directory to mount.

        Nothing comes back when the pipe cannot be made -- a filesystem with
        no FIFO support, a temporary directory that is not writable. That is
        a launch without the bridge rather than a launch that fails: the
        sign-in still prints its URL, which is what it did before this
        existed.
        """
        try:
            directory = Path(tempfile.mkdtemp(prefix="lup-browser-"))
            os.mkfifo(directory / self.channel, 0o600)
        except OSError:
            return None
        atexit.register(shutil.rmtree, directory, True)
        threading.Thread(
            target=self.read_requests,
            args=(directory / self.channel,),
            daemon=True,
        ).start()
        return directory

    def read_requests(self, pipe: Path) -> None:
        """Open every admitted URL the container writes, for as long as it runs.

        Reopens rather than looping inside one open, because a pipe reports
        end-of-file every time its last writer closes and a sign-in is one
        writer that opens, writes and goes. Reopening is what makes the
        second sign-in of a session work, and it does not spin: opening a
        pipe for reading waits for the next writer rather than returning.

        A daemon thread, so the session's own exit ends it rather than
        something having to notice and stop it. The catch-all is deliberate
        and this is the boundary that earns one: the alternative to dropping
        a malformed line is a launcher that dies of it, taking a session with
        it over a sign-in nobody was attempting.
        """
        while True:
            try:
                with pipe.open(encoding="utf-8") as channel:
                    for line in channel:
                        if admitted := self.admitted(line):
                            webbrowser.open(admitted)
            except OSError:
                return
