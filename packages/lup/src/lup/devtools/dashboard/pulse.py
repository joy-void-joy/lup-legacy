"""What the dashboard says of itself to every session, holding no capability, and the line that shows it.

The dashboard runs on the host as the operator's, and what it knows — how
many reviews wait on the operator and which session parked each, the
messages agents sent the operator, which agents need the operator, how many
sessions hold it, which repositories it serves — is read by sessions that
must never hold the capability that opens it: a session's status line, and
`dashboard status` run inside one. So the service publishes that much, and
nothing else, as one small file in a directory of its own, which every
launch lends its session read-only at the path the host has it, beside the
variable naming it.

The file is rewritten while the service runs and removed when it stops, so a
pulse whose last beat is old says the service stopped without removing it.
Where the operator stopped it to stay stopped, the stop leaves a pulse saying
so in its place (``halted``), which holds until a start replaces it.

A session's status line (:func:`status_line`) reads, left to right:

- **the session**, dimmed: the repository's name as the dashboard's tree
  names it, what the roster calls the session, and the worktree it was
  launched in, relative to its repository's directory —
  ``lup · dev · tree/fix-x`` — found by the ids the runtime hands the line on
  stdin (:class:`StatusInput`);
- **what waits on the operator**, in the warning colour and only while
  something does: the reviews waiting, with how many of them this session or
  its subagents parked — naming the one by its short id, counting several —
  ``?2 reviews (1 here: 41cb73e1)``, and the messages agents sent the
  operator that still wait in its mailbox, ``✉1``;
- **what other agents need**, only while one does: ``⚠ 1 quiet``, an agent
  with a call outstanding and nothing new in its transcript for ten minutes,
  and ``⚠ held twice``, a path two sessions hold;
- **the dashboard**, as one glyph and the whole address the operator opens
  it at — the first origin declared for it, else loopback's — as a terminal
  hyperlink whose text is that address, never its capability:
  ``● http://127.0.0.1:8767`` while it serves current code,
  ``◐ http://127.0.0.1:8767 restarting`` while it moves onto newer code,
  ``○ http://127.0.0.1:8767 down · dashboard restart`` where nothing serves,
  and the operator's stop in the words it left.

Where the terminal is narrower than the whole line, whole pieces drop and no
word is cut: first what other agents need, then the session's worktree, then
the review's id, then the session's name, and last the repository's name.
What waits on the operator and the dashboard's glyph and address always stay,
and a line still too wide is left to the runtime. A pulse written before it
carried sessions, their repository's name, the operator's address and what
needs the operator reads as one with none of them, so the line shows what it
can.

A leaf, imported by the status line a session runs at every render, so it
reaches for nothing heavier than pydantic.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePath
from shutil import get_terminal_size
from typing import Literal, TextIO

from pydantic import BaseModel, ValidationError

# lup: ignore[constant-declaration] — the launch that exports it and the session
# that reads it are different processes, so the name is an identity
DASHBOARD_PULSE_ENV = "LUP_DASHBOARD_PULSE"
"""The variable naming the pulse file, which a launch holding the dashboard exports."""


class RunningCode(BaseModel, frozen=True):
    """Which lup source the dashboard runs, and whether its checkout has moved past it."""

    source: str = ""
    """A digest of the package files it imported, as it imported them."""

    root: str = ""
    """Where it imported them from."""

    since: datetime | None = None
    """When this process began running it."""

    older: bool = False
    """Its checkout holds newer code than it runs."""

    failing: str = ""
    """Why that newer code would not start, where it would not."""

    restarted: str = ""
    """Why the dashboard before this one stopped, where the sessions holding
    it started this one after it stopped unexpectedly; said for a few minutes."""

    def said(self) -> str:
        """What the page and the status line say of it; nothing while it is current and ran on."""
        restarted = (
            f"restarted after it stopped: {self.restarted}" if self.restarted else ""
        )
        if not self.older:
            return restarted
        older = (
            "dashboard runs older code; its newer code does not start"
            if self.failing
            else "dashboard runs older code; restarting"
        )
        return "; ".join(part for part in (older, restarted) if part)


def repository_name(repository: PurePath) -> str:
    """What a reader calls a repository, by its shared git directory: the directory it is checked out as.

    The name the dashboard's tree gives it and every status line starts with.
    """
    if repository.name == ".git":
        return repository.parent.name
    return repository.name.removesuffix(".git")


class PulseSession(BaseModel, frozen=True):
    """One running session the dashboard serves, as its own status line finds it.

    Keyed by its repository and its roster id, since ids are minted per
    repository; found by the ids its runtime knows it by, which are what the
    runtime hands a status line.
    """

    repository: str
    """The repository it works in, by its shared git directory."""

    project: str = ""
    """That repository's name, as the dashboard's tree names it (:func:`repository_name`)."""

    id: str
    """Its roster id."""

    name: str = ""
    """What the roster calls it now."""

    worktree: str = ""
    """The checkout it was launched in."""

    runtime: list[str] = []
    """The runtime's own ids for its conversation: the one its launch named,
    and the one the transcript of its last prompt is written under, which
    differ once the runtime has opened another conversation for it."""

    reviews: list[str] = []
    """The reviews waiting on the operator that it or its subagents parked, by id."""


class DashboardPulse(BaseModel, frozen=True):
    """The dashboard as a session may read it: counts and its address, never its capability."""

    url: str
    """Where it serves, credential-free."""

    address: str = ""
    """Where the operator opens it, credential-free: the first origin declared
    for it (``[dashboard] origins``), else where it serves. The page keeps its
    capability per origin, so a link to it opens signed in only where the
    operator signed in at this origin."""

    pid: int
    pending: int = 0
    """Reviews waiting on the operator, across every repository it serves."""

    sessions: int = 0
    """Running launches holding it."""

    repositories: list[str] = []
    tabs: int = 0
    """Pages following its stream now."""

    beat: datetime
    """When the service last wrote it."""

    code: RunningCode = RunningCode()
    """Which code it runs."""

    restarts: int = 0
    """How many times the sessions holding it started it again after it stopped."""

    halted: str = ""
    """Why nothing serves, where the operator stopped it to stay stopped. Left
    by that stop rather than beaten by a service, so it holds, whatever its
    age, until a start replaces it."""

    members: list[PulseSession] = []
    """Every running session, with the reviews it parked that wait on the operator."""

    unread: int = 0
    """Messages agents sent the operator (`user`) that still wait in its mailbox."""

    quiet: int = 0
    """Agents with a call outstanding and nothing new in their transcript for
    ten minutes or more, none of whose subagents runs."""

    contested: int = 0
    """Paths two sessions hold at once, each counted with its subagents."""

    def current(self, now: datetime, within: timedelta = timedelta(seconds=30)) -> bool:
        """Whether the service wrote it recently enough to still be running."""
        return now - self.beat <= within


class PulseFile(BaseModel, frozen=True):
    """Where one dashboard's pulse is kept: a directory of its own, lent read-only."""

    path: Path

    @classmethod
    def of(cls, lent: Path) -> "PulseFile":
        """The pulse of the dashboard whose lent directory is ``lent``.

        Handed in as :func:`lup.launch.companions.lent_directory` lays it out,
        rather than derived here, since this leaf imports nothing of the launcher.
        """
        return cls(path=lent / "dashboard.json")

    def read(self) -> DashboardPulse | None:
        """The pulse as last written, or nothing where none is, or it does not parse."""
        try:
            return DashboardPulse.model_validate_json(self.path.read_bytes())
        except (OSError, ValidationError):
            return None


type Tone = Literal["plain", "dim", "warn", "good", "bad"]


class Palette(BaseModel, frozen=True):
    """The SGR parameters each tone is painted with; a tone missing here is left unpainted."""

    tones: dict[Tone, str] = {"dim": "2", "warn": "33", "good": "32", "bad": "31"}


class Piece(BaseModel, frozen=True):
    """One run of the status line's text in one tone, and the address it links to, if any."""

    text: str
    tone: Tone = "plain"
    link: str = ""
    """Where the text leads, as a terminal hyperlink (OSC 8), which a
    terminal without them shows as the text alone."""

    def painted(self, palette: Palette) -> str:
        """The text in its tone's colour, wrapped in its link."""
        code = palette.tones.get(self.tone, "")
        text = f"\x1b[{code}m{self.text}\x1b[0m" if code else self.text
        if not self.link:
            return text
        return f"\x1b]8;;{self.link}\x07{text}\x1b]8;;\x07"


def joined(groups: list[list[Piece]], between: str = " · ") -> list[Piece]:
    """The groups that say anything, one after another, a dimmed divider between each two."""
    spoken = [group for group in groups if group]
    return [
        piece
        for index, group in enumerate(spoken)
        for piece in [*([Piece(text=between, tone="dim")] if index else []), *group]
    ]


class ShownLine(BaseModel, frozen=True):
    """The status line as it is shown: its segments, each a run of pieces, a rule between them."""

    segments: list[list[Piece]] = []
    divider: str = " │ "

    def plain(self) -> str:
        """The line as a terminal without colours or links shows it."""
        return self.divider.join(
            "".join(piece.text for piece in segment) for segment in self.segments
        )

    def width(self) -> int:
        """How many columns it takes."""
        return len(self.plain())

    def painted(self, palette: Palette = Palette()) -> str:
        """The line with its colours and links, as the runtime prints it."""
        divider = Piece(text=self.divider, tone="dim").painted(palette)
        return divider.join(
            "".join(piece.painted(palette) for piece in segment)
            for segment in self.segments
        )


class Detail(BaseModel, frozen=True):
    """How much of the status line a width leaves room for: each piece left out is one dropped."""

    others: bool = True
    """What other agents need of the operator."""

    worktree: bool = True
    """The worktree this session works in."""

    review: bool = True
    """The id of the one review this session parked."""

    name: bool = True
    """What the roster calls this session."""

    project: bool = True
    """The repository's name."""


class StatusWorkspace(BaseModel, frozen=True):
    """Where the runtime says the session works."""

    current_dir: str = ""
    project_dir: str = ""


class StatusInput(BaseModel, frozen=True):
    """What the runtime hands its status line command on stdin, as much of it as the line reads.

    Claude Code's ``statusLine`` input, documented at
    https://code.claude.com/docs/en/statusline#available-data: the session's
    id, the transcript it writes, and the directory it was launched in. The
    rest of what it hands is left unread.
    """

    session_id: str = ""
    transcript_path: str = ""
    cwd: str = ""
    workspace: StatusWorkspace = StatusWorkspace()

    @classmethod
    def read(cls, stream: TextIO | None) -> "StatusInput":
        """What *stream* holds; nothing where it is a terminal, cannot be read, or holds something else.

        A terminal is never read, since a person running the line by hand
        typed nothing into it and it would wait on them.
        """
        try:
            if stream is None or stream.isatty():
                return cls()
            return cls.model_validate_json(stream.read())
        except (OSError, ValueError):
            return cls()

    def identities(self) -> list[str]:
        """The ids it names the session's conversation by: its own, and its transcript's."""
        return [
            each
            for each in (self.session_id, PurePath(self.transcript_path).stem)
            if each
        ]

    def directory(self) -> str:
        """The directory the session was launched in."""
        return self.workspace.project_dir or self.workspace.current_dir or self.cwd


def anchor(repository: str) -> PurePath:
    """The directory a repository's worktrees are named from, by its shared git directory.

    The checkout holding its ``.git``, and a git directory of its own —
    ``lup.git``, its worktrees beneath it — itself.
    """
    path = PurePath(repository)
    return path.parent if path.name == ".git" else path


def holder(worktree: str, repositories: list[str]) -> str:
    """The repository whose directory holds *worktree*, by its shared git directory; empty where none does."""
    if not worktree:
        return ""
    path = PurePath(worktree)
    return next(
        (each for each in repositories if path.is_relative_to(anchor(each))), ""
    )


def place(worktree: str, repository: str) -> str:
    """Where a session works, as its status line names it.

    Its checkout relative to the repository's directory — ``tree/fix-x`` —
    and nothing where it is that directory, which the repository's name
    already says; the checkout's own name where no repository holds it.
    """
    if not worktree:
        return ""
    path = PurePath(worktree)
    home = anchor(repository) if repository else None
    if home is None or not path.is_relative_to(home):
        return path.name
    return path.relative_to(home).as_posix() if path != home else ""


class LineFacts(BaseModel, frozen=True):
    """What one render of the status line knows, before a width settles how much of it shows."""

    pulse: DashboardPulse | None = None
    """The dashboard's pulse, whatever its age; none where it took it down."""

    live: bool = False
    """Whether the pulse is current, so what it counts holds now."""

    session: PulseSession | None = None
    """This session's row, where the pulse lists it."""

    project: str = ""
    """The name of the repository this session works in."""

    place: str = ""
    """Where this session works, as the line names it."""

    @classmethod
    def of(
        cls, pulse: DashboardPulse | None, asking: StatusInput, now: datetime
    ) -> "LineFacts":
        """What the pulse says of the session the runtime's input names.

        A session the pulse does not list — one the dashboard has not seen
        yet, or any session in a pulse from before it listed them — is still
        placed, by the directory the runtime says it was launched in, and
        named by the repository whose directory holds that.
        """
        if pulse is None:
            return cls(place=place(asking.directory(), ""))
        named = asking.identities()
        session = next(
            (
                each
                for each in pulse.members
                if any(identity in named for identity in each.runtime)
            ),
            None,
        )
        worktree = session.worktree if session is not None else asking.directory()
        repository = (
            session.repository
            if session is not None
            else holder(worktree, pulse.repositories)
        )
        listed = session.project if session is not None else ""
        return cls(
            pulse=pulse,
            live=not pulse.halted and pulse.current(now),
            session=session,
            project=listed
            or (repository_name(PurePath(repository)) if repository else ""),
            place=place(worktree, repository),
        )

    def counted(self) -> DashboardPulse | None:
        """The pulse, where what it counts holds now."""
        return self.pulse if self.live else None

    def who(self, detail: Detail) -> list[Piece]:
        """Which session this is: its repository's name, its own, and where it works."""
        session = self.session
        said = " · ".join(
            [
                *([self.project] if detail.project and self.project else []),
                *(
                    [session.name]
                    if detail.name and session is not None and session.name
                    else []
                ),
                *([self.place] if detail.worktree and self.place else []),
            ]
        )
        return [Piece(text=said, tone="dim")] if said else []

    def waiting(self, detail: Detail) -> list[Piece]:
        """What waits on the operator, and this session's share of it."""
        counted = self.counted()
        if counted is None:
            return []
        noun = "review" if counted.pending == 1 else "reviews"
        here = self.session.reviews if self.session is not None else []
        match here:
            case [only] if detail.review:
                share = f" (1 here: {only[:8]})"
            case [_, *_]:
                share = f" ({len(here)} here)"
            case _:
                share = ""
        reviews = (
            [Piece(text=f"?{counted.pending} {noun}{share}", tone="warn")]
            if counted.pending
            else []
        )
        letters = (
            [Piece(text=f"✉{counted.unread}", tone="warn")] if counted.unread else []
        )
        return joined([reviews, letters])

    def others(self) -> list[Piece]:
        """What other agents need of the operator: one gone quiet, a path held twice."""
        counted = self.counted()
        if counted is None:
            return []
        quiet = (
            [Piece(text=f"⚠ {counted.quiet} quiet", tone="warn")]
            if counted.quiet
            else []
        )
        held = (
            "⚠ held twice"
            if counted.contested == 1
            else f"⚠ {counted.contested} paths held twice"
        )
        twice = [Piece(text=held, tone="warn")] if counted.contested else []
        return joined([quiet, twice])

    def serving(self, pulse: DashboardPulse, reached: Piece) -> list[Piece]:
        """A dashboard that serves: on current code, moving onto newer, or held on older."""
        match pulse.code:
            case RunningCode(older=True, failing=str() as failing) if failing:
                return [
                    Piece(text="◐ ", tone="warn"),
                    reached,
                    Piece(
                        text=" runs older code; its newer code does not start",
                        tone="warn",
                    ),
                ]
            case RunningCode(older=True):
                return [
                    Piece(text="◐ ", tone="warn"),
                    reached,
                    Piece(text=" restarting", tone="warn"),
                ]
            case _:
                return [Piece(text="● ", tone="good"), reached]

    def dashboard(self) -> list[Piece]:
        """The dashboard as one glyph and the address the operator opens it at, and why it stopped where it was started again.

        The address is a link whose text is the address itself, so a
        terminal that links a bare address and one that takes the link both
        open it; it never carries the capability.
        """
        restart = Piece(text=" · dashboard restart", tone="dim")
        pulse = self.pulse
        if pulse is None:
            return [Piece(text="○ dashboard down", tone="bad"), restart]
        address = pulse.address or pulse.url
        reached = Piece(text=address, link=address)
        match (pulse.halted, self.live):
            case (str() as halted, _) if halted:
                return [
                    Piece(text="○ ", tone="dim"),
                    reached,
                    Piece(text=f" · {halted}", tone="dim"),
                ]
            case (_, False):
                return [
                    Piece(text="○ ", tone="bad"),
                    reached,
                    Piece(text=" down", tone="bad"),
                    restart,
                ]
            case _:
                restarted = pulse.code.restarted
                return joined(
                    [
                        self.serving(pulse, reached),
                        [
                            Piece(
                                text=f"restarted after it stopped: {restarted}",
                                tone="warn",
                            )
                        ]
                        if restarted
                        else [],
                    ]
                )

    def shown(self, detail: Detail) -> ShownLine:
        """The line with as much as *detail* keeps."""
        return ShownLine(
            segments=[
                segment
                for segment in (
                    self.who(detail),
                    self.waiting(detail),
                    self.others() if detail.others else [],
                    self.dashboard(),
                )
                if segment
            ]
        )

    def fitted(self, columns: int) -> ShownLine:
        """The fullest line that fits *columns*, whole pieces dropped in their order; 0 fits any."""
        narrowing = [
            Detail(),
            Detail(others=False),
            Detail(others=False, worktree=False),
            Detail(others=False, worktree=False, review=False),
            Detail(others=False, worktree=False, review=False, name=False),
            Detail(
                others=False, worktree=False, review=False, name=False, project=False
            ),
        ]
        lines = [self.shown(each) for each in narrowing]
        return next(
            (line for line in lines if not columns or line.width() <= columns),
            lines[-1],
        )


def status_line(
    path: Path,
    asking: StatusInput = StatusInput(),
    now: datetime | None = None,
    columns: int = 0,
) -> ShownLine:
    """The status line of the session *asking* names, from the pulse at *path*, fitted to *columns*."""
    pulse = PulseFile(path=path).read()
    return LineFacts.of(pulse, asking, now or datetime.now(UTC)).fitted(columns)


def answered(pulse: Path, stdin: TextIO | None, margin: int = 2) -> str:
    """What a status line command prints: the line for the session on *stdin*, painted, fitted to its terminal.

    The runtime says how wide its terminal is in ``COLUMNS``, since it
    captures what the command prints; *margin* is left for its own spacing
    around the row, which its documentation does not size.
    """
    columns = get_terminal_size((0, 0)).columns
    fitted = status_line(
        pulse,
        StatusInput.read(stdin),
        columns=max(columns - margin, 1) if columns else 0,
    )
    return fitted.painted()
