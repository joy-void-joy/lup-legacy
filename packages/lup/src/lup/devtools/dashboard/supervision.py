"""What the operator does to the agents the dashboard shows, and as a peer among them.

Every verb here is one a session's own coordination tools have, taken by the
person — signed `user`, through the page's door — or one only the person has:
waking an agent with nothing new to say, interrupting its turn, stopping its
runtime. Each writes what it does where every agent reads it — a mailbox, a
member's own file, a standing notice — and wakes whom it wrote to the way the
message route always has: a wake carrying the mail whole, handed over once the
runtime accepted it, the rest left for the agent's delivery hook.

The routes sit behind the page's capability and its origin, as every write on
the dashboard does, and answer a refusal with what was refused and why: 404
where nothing answers to what the page named, 409 where something does and
the verb cannot be done to it.
"""

import asyncio
import os
import signal
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from lup.channels.models import Door
from lup.coordination.bare.mail import new_post_id
from lup.coordination.bare.runtime import process_scope
from lup.coordination.bare.scope import execution_scope
from lup.coordination.holds import HoldScope
from lup.coordination.identity import NameTakenError
from lup.coordination.mail import ActorDelivery, Posting
from lup.coordination.peers import USER_ADDRESS, join_user
from lup.coordination.repository import NotReached, PeerDepartedError, RepositoryPeers
from lup.coordination.roster import Delivery, RosterMember
from lup.coordination.wake import WakePriority
from lup.providers.wake import wake
from lup.coordination.watch import roused
from lup.devtools.coordination.pausing import Paused, Resumed, pause, resume
from lup.devtools.dashboard.companion import KnownRepository
from lup.devtools.dashboard.live import (
    TRANSCRIPT_PAGE,
    Feature,
    LiveNotice,
    TranscriptPage,
    member_transcript,
    session_process,
    transcript_page,
)
from lup.devtools.dashboard.stream import FollowOutcome, FollowRequest, LiveFeed
from lup.providers.interrupts import Interrupted, interrupted_turn

SUPERVISED: tuple[Feature, ...] = (
    "reply-thread",
    "redirect",
    "interrupt",
    "bare-wake",
    "rename",
    "stop",
    "transcript",
    "notices",
    "describe",
    "claims",
    "inbox-read",
    "thread-post",
    "pause",
)
"""Every piece of supervision these routes serve, which the stream tells the page."""

BARE_WAKE = (
    "The person watching asks you to look: nothing new waits in your mailbox. "
    "Carry on with what you were doing, or say where you are with "
    "`coordination_describe`."
)
"""What an agent woken with nothing waiting reads."""


class Refused(ValueError):
    """A verb the operator asked for that cannot be done to what it names, and why."""


class MessageRequest(BaseModel, frozen=True, extra="forbid"):
    """What the operator writes to one agent, and how it reaches it."""

    text: str = Field(min_length=1)
    in_reply_to: str = ""
    """The post it answers, which puts it in that post's thread."""

    redirect: bool = False
    """Refuse the agent's next tool call with the text, rather than inform it."""

    priority: WakePriority = "next"
    """`now` interrupts a Claude turn that is generating; a running tool call
    finishes first. Refused where nothing can interrupt the agent."""


class Nothing(BaseModel, frozen=True, extra="forbid"):
    """A write that carries nothing beyond whom it acts on."""


class NameRequest(BaseModel, frozen=True, extra="forbid"):
    name: str = Field(min_length=1)


class TextRequest(BaseModel, frozen=True, extra="forbid"):
    text: str = Field(min_length=1)


class DescriptionRequest(BaseModel, frozen=True, extra="forbid"):
    text: str = ""
    """What the person is on; empty says nothing."""


class ClaimRequest(BaseModel, frozen=True, extra="forbid"):
    path: str = Field(min_length=1)
    """An absolute path: a directory covers everything beneath it."""


class InboxReadRequest(BaseModel, frozen=True, extra="forbid"):
    ids: list[str] = Field(min_length=1)
    """The messages read, by the id each carries on the stream."""


class PostRequest(BaseModel, frozen=True, extra="forbid"):
    """One post into a discussion, to everyone in it."""

    text: str = Field(min_length=1)
    in_reply_to: str = ""
    """The post it answers; empty answers the discussion's latest."""

    to: list[str] = []
    """Members to bring into the discussion, by id or name, from this post on."""


class ReplyOutcome(BaseModel, frozen=True):
    """What became of one message the operator wrote: queued, and whether a wake landed."""

    session: str
    queued: bool
    woken: bool
    interrupted: bool = False
    """Whether the wake asked the agent's turn to stop for it, and the runtime took it."""

    post: str = ""
    """What a reply to it names."""

    thread: str = ""
    detail: str


class Renamed(BaseModel, frozen=True):
    session: str
    name: str


class Stopped(BaseModel, frozen=True):
    session: str
    pid: int
    detail: str


class Broadcast(BaseModel, frozen=True):
    """One message to every working member of a repository, one post between them."""

    post: str
    outcomes: list[ReplyOutcome]


class Withdrawn(BaseModel, frozen=True):
    id: str
    withdrawn: bool


class Described(BaseModel, frozen=True):
    """Every repository whose roster now says what the person is on."""

    repositories: list[str]


class Claimed(BaseModel, frozen=True):
    path: str
    holders: list[str]
    """Everyone holding it now, by the name each answers to; `user` is the person."""


class Released(BaseModel, frozen=True):
    path: str
    holders: list[str]
    """Whoever still holds it."""


class InboxRead(BaseModel, frozen=True):
    read: list[str]
    """The messages taken out of the person's mailbox; one already taken is not."""


class PauseRequest(BaseModel, frozen=True, extra="forbid"):
    """How far a pause reaches, and whether it stops what is running too."""

    tree: bool = False
    """The agent and everything it spawned, at any remove; else the agent
    and its native subagents. Names nothing for a repository's pause."""

    freeze: bool = False
    """Also stop the commands its tools are running and interrupt its turn now."""


class ResumeRequest(BaseModel, frozen=True, extra="forbid"):
    """Which pause a resume lifts: the agent's own, or the one over its tree."""

    tree: bool = False


class NotFrozenSession(BaseModel, frozen=True):
    """One session a freeze could not reach, by its session key, and why; it is paused."""

    session: str
    why: str


class PauseOutcome(BaseModel, frozen=True):
    """What one pause or resume did, in every repository it reached."""

    repositories: list[str]
    """The keys of the repositories it acted in."""

    held: int = 0
    """How many live agents a pause holds now; nothing for a resume."""

    frozen: list[str] = []
    """The sessions a freeze stopped, as session keys."""

    unfrozen: list[NotFrozenSession] = []

    continued: int = 0
    """The command groups a resume continued."""

    woken: list[str] = []
    """The sessions a resume woke with a bare "continue", as session keys."""

    detail: str


class PostOutcome(BaseModel, frozen=True):
    """One post into a discussion: the id every copy shares, and what became of each."""

    post: str
    thread: str
    deliveries: list[ReplyOutcome]
    refused: list[NotReached] = []


def standing(
    peers: RepositoryPeers, known: KnownRepository, member_id: str
) -> RosterMember:
    """The row one member id names here, refused where none does or it has stopped."""
    row = peers.row(member_id)
    if row is None:
        raise LookupError(f"no session of {known.name()} has the id {member_id!r}")
    if not row.running:
        raise PeerDepartedError(row, peers.called(member_id))
    return row


def worktree_of(row: RosterMember) -> Path | None:
    return Path(row.worktree) if row.worktree else None


def handed(
    known: KnownRepository,
    peers: RepositoryPeers,
    member_id: str,
    posting: Posting,
    priority: WakePriority = "next",
) -> ReplyOutcome:
    """Wake one member with what waits for it, and say what became of the mail just put there.

    The wake carries everything waiting, as a session's own send would have
    it carried, and what the runtime accepted is handed over then; a redirect
    it carried stays for the member's hook to refuse its next call with.
    """
    session = f"{known.key()}/{member_id}"
    row = standing(peers, known, member_id)
    waiting = peers.waiting(member_id).messages
    redirected = any(message.redirect for message in waiting)
    woken = roused(peers, row, waiting, worktree_of(row), priority=priority)
    interrupted = woken.reached and priority == "now"
    queued = ReplyOutcome(
        session=session,
        queued=True,
        woken=False,
        post=posting.post,
        thread=posting.thread,
        detail="",
    )
    if woken.reached:
        said = "Handed over with the wake its runtime accepted."
        if interrupted:
            said = (
                "Handed over with a wake asking its turn to stop: a turn that is "
                "generating ends at once, and a tool call already running "
                "finishes first."
            )
        if redirected:
            said += " A redirect stays for its next tool call, which it refuses."
        return queued.model_copy(
            update={"woken": True, "interrupted": interrupted, "detail": said}
        )
    if row.delivery == Delivery.HOOK:
        said = (
            "Queued as a redirect: its next tool call is refused with your words."
            if redirected
            else "Queued in its mailbox; it is handed over before its next tool call."
        )
        return queued.model_copy(update={"detail": said})
    kept = (
        "Queued as a redirect in its mailbox, though it declared no delivery hook "
        "to refuse its next tool call with it."
        if redirected
        else "Queued in its mailbox."
    )
    return queued.model_copy(update={"detail": f"{kept} {woken.reason}"})


def interruptible(peers: RepositoryPeers, row: RosterMember) -> RosterMember:
    """The row whose runtime an interrupt reaches for *row*: its own, or its session's.

    A Claude session is interrupted through its wake socket. A Codex
    session's turn is stopped through the app-server its configuration home
    runs, which only a process in the execution scope its row recorded can
    reach, as its queue can. Refused where nothing can interrupt it.
    """
    target = peers.row(row.parent) if row.parent else row
    if target is None or not target.running:
        raise Refused(
            "its session has stopped, so nothing runs that could be interrupted"
        )
    wake_path = target.wake
    match wake_path.runtime:
        case "claude" if wake_path.handle:
            return target
        case "codex" if wake_path.handle and wake_path.home:
            if wake_path.scope != execution_scope():
                raise Refused(
                    "its Codex app-server runs in another execution scope than "
                    "the dashboard's, which cannot reach it"
                )
            return target
        case "codex":
            raise Refused(
                "nothing can interrupt it: its row records no Codex thread and "
                "home to reach its app-server through"
            )
        case _:
            raise Refused("nothing can interrupt it: its row declares no wake path")


def codex_stopped(target: RosterMember) -> Interrupted:
    """Stop the turn a Codex session's thread is running, where it is running one."""
    return asyncio.run(
        interrupted_turn("codex", Path(target.wake.home), target.wake.handle)
    )


def said_with(outcome: ReplyOutcome, stopped: Interrupted | None) -> ReplyOutcome:
    """A message's outcome, saying what became of the Codex turn it interrupted, where it did."""
    if stopped is None:
        return outcome
    said = (
        "Its running Codex turn was stopped, and the message queued for the next."
        if stopped.interrupted
        else f"Nothing was stopped: {stopped.reason}."
    )
    return outcome.model_copy(
        update={
            "interrupted": stopped.interrupted,
            "detail": f"{said} {outcome.detail}",
        }
    )


def reply(known: KnownRepository, member_id: str, said: MessageRequest) -> ReplyOutcome:
    """Write the operator's message to one session or subagent, by its member id, and wake it.

    The path a session's own `coordination_send` takes — the recipient's
    mailbox, where its hook hands it over at its next tool call — then its
    wake. A reply goes into the thread of the post it answers. With `now`, a
    session's turn is asked to stop for it: a Claude session's through the
    wake's own priority, a Codex session's through its app-server before its
    queue takes the message for the next turn. A subagent has no wake of its
    own, so its message waits for its next call and its session is
    interrupted with a copy naming it. Refused for an id nothing here answers
    to, for a session that has stopped, and for `now` where nothing can
    interrupt.
    """
    peers = RepositoryPeers(known.checkout)
    row = standing(peers, known, member_id)
    target = interruptible(peers, row) if said.priority == "now" else row
    codex = said.priority == "now" and target.wake.runtime == "codex"
    priority: WakePriority = "next" if codex else said.priority
    post = new_post_id()
    posting = Posting(
        post=post,
        thread=peers.thread_of(said.in_reply_to) if said.in_reply_to else post,
    )
    peers.send(
        member_id,
        said.text,
        redirect=said.redirect,
        door=Door.PAGE,
        in_reply_to=said.in_reply_to,
        sender=USER_ADDRESS,
        posting=posting,
    )
    if target.actor.id == member_id:
        stopped = codex_stopped(target) if codex else None
        return said_with(handed(known, peers, member_id, posting, priority), stopped)
    named = peers.called(member_id) or member_id
    peers.send(
        target.actor.id,
        f"For your subagent {named} ({member_id}), from the person watching: {said.text}",
        door=Door.PAGE,
        sender=USER_ADDRESS,
        posting=posting,
    )
    stopped = codex_stopped(target) if codex else None
    session = said_with(
        handed(known, peers, target.actor.id, posting, priority), stopped
    )
    return ReplyOutcome(
        session=f"{known.key()}/{member_id}",
        queued=True,
        woken=False,
        interrupted=session.interrupted,
        post=posting.post,
        thread=posting.thread,
        detail=(
            "Queued for its next tool call; its session was sent a copy "
            f"naming it. {session.detail}"
        ),
    )


def wake_member(
    known: KnownRepository, member_id: str, asked: str = BARE_WAKE
) -> ReplyOutcome:
    """Make one session look, with whatever waits for it or, where nothing does, *asked*.

    A subagent has no wake of its own: its mail is handed over at its next
    call, and it is its session a wake would reach.
    """
    peers = RepositoryPeers(known.checkout)
    row = standing(peers, known, member_id)
    if row.parent:
        raise Refused(
            "a subagent has no wake of its own: what is written to it is handed "
            "over at its next tool call, and a wake reaches its session"
        )
    session = f"{known.key()}/{member_id}"
    waiting = [
        message for message in peers.waiting(member_id).messages if not message.carried
    ]
    if waiting:
        return handed(known, peers, member_id, Posting())
    woken = wake(row.wake, asked, worktree_of(row))
    return ReplyOutcome(
        session=session,
        queued=False,
        woken=woken.reached,
        detail="Woken with nothing new to read." if woken.reached else woken.reason,
    )


def rename(known: KnownRepository, member_id: str, name: str) -> Renamed:
    """Call one session or subagent something else; the name it had reaches it until another takes it."""
    peers = RepositoryPeers(known.checkout)
    standing(peers, known, member_id)
    try:
        peers.rename(member_id, name)
    except NameTakenError as taken:
        raise Refused(str(taken)) from taken
    return Renamed(session=f"{known.key()}/{member_id}", name=name)


def stop(
    known: KnownRepository,
    member_id: str,
    kill: Callable[[int, int], None] = os.kill,
) -> Stopped:
    """End one session's runtime process, where this dashboard can be sure it is that process.

    Only where the dashboard shares the runtime's pid namespace does the
    recorded id name it, and only while the process with that id still
    started when the row recorded is it that one — both checked as the
    signal is sent. A contained session's runtime is its launcher's to stop,
    and a subagent ends with its session.
    """
    peers = RepositoryPeers(known.checkout)
    standing(peers, known, member_id)
    rows = {member.actor.id: member for member in peers.present()}
    process = session_process(rows[member_id], rows, process_scope())
    if process is None:
        raise Refused("its row records no runtime process to stop")
    if not process.stoppable:
        raise Refused(f"this dashboard cannot stop it: {process.why}")
    try:
        kill(process.pid, signal.SIGTERM)
    except ProcessLookupError as gone:
        raise Refused("its runtime had already stopped") from gone
    except PermissionError as denied:
        raise Refused(
            f"this dashboard may not signal its runtime: {denied}"
        ) from denied
    return Stopped(
        session=f"{known.key()}/{member_id}",
        pid=process.pid,
        detail=(
            f"Sent SIGTERM to pid {process.pid}, the runtime its row recorded; "
            "its session ends as that runtime does."
        ),
    )


def broadcast(known: KnownRepository, text: str) -> Broadcast:
    """Say one thing to every working member of a repository, each woken as a message is."""
    peers = RepositoryPeers(known.checkout)
    posting = Posting(post=new_post_id())
    working = [row.actor.id for row in peers.present() if row.running]
    for member_id in working:
        peers.send(
            member_id, text, door=Door.PAGE, sender=USER_ADDRESS, posting=posting
        )
    return Broadcast(
        post=posting.post,
        outcomes=[handed(known, peers, member_id, posting) for member_id in working],
    )


def notice(known: KnownRepository, text: str) -> LiveNotice:
    """State something every session here reads at the head of each prompt, until it is withdrawn."""
    stated = RepositoryPeers(known.checkout).notify(
        text, door=Door.PAGE, by=USER_ADDRESS
    )
    return LiveNotice(
        id=stated.id,
        text=stated.text,
        by=stated.by,
        door=str(stated.door),
        posted_at=stated.posted_at,
    )


def withdraw(known: KnownRepository, notice_id: str) -> Withdrawn:
    """Take one standing notice down."""
    if not RepositoryPeers(known.checkout).withdraw(notice_id):
        raise LookupError(
            f"no notice standing in {known.name()} has the id {notice_id!r}"
        )
    return Withdrawn(id=notice_id, withdrawn=True)


def describe(served: list[KnownRepository], text: str) -> Described:
    """Say what the person is on, on their row in every repository served."""
    for known in served:
        peers = RepositoryPeers(known.checkout)
        join_user(peers.roster)
        peers.describe(USER_ADDRESS, text)
    return Described(repositories=[known.key() for known in served])


def holders_of(peers: RepositoryPeers, target: Path) -> list[str]:
    """Everyone a write to *target* lands under, by the name each answers to."""
    return list(
        dict.fromkeys(
            peers.called(holder.id) or holder.id
            for claim in peers.holding(target)
            for holder in claim.holders
        )
    )


def claim(known: KnownRepository, path: str) -> Claimed:
    """Hold a path as the person, the way a session's lock does: a write under it asks them first."""
    target = Path(path)
    if not target.is_absolute():
        raise Refused(f"{path} is not absolute: a hold names the path it covers whole")
    target = target.resolve()
    if not target.exists():
        raise Refused(
            f"{target} does not exist; a hold covers what is there to write, so "
            "name the directory or file to hold"
        )
    peers = RepositoryPeers(known.checkout)
    join_user(peers.roster)
    peers.lock(USER_ADDRESS, target)
    return Claimed(path=str(target), holders=holders_of(peers, target))


def release(known: KnownRepository, path: str) -> Released:
    """Give back a path the person holds; one somebody else holds is refused, naming them."""
    target = Path(path).resolve()
    peers = RepositoryPeers(known.checkout)
    if not peers.release(USER_ADDRESS, target):
        holders = holders_of(peers, target)
        raise Refused(
            f"you do not hold {target}"
            + (f"; held by {', '.join(holders)}" if holders else "")
        )
    return Released(path=str(target), holders=holders_of(peers, target))


def read_inbox(known: KnownRepository, ids: list[str]) -> InboxRead:
    """Take exactly these messages out of the person's mailbox, as read."""
    peers = RepositoryPeers(known.checkout)
    chosen = [
        message for message in peers.waiting(USER_ADDRESS).messages if message.id in ids
    ]
    peers.delivered(USER_ADDRESS, ActorDelivery(messages=chosen))
    return InboxRead(read=[message.id for message in chosen])


def post(known: KnownRepository, thread: str, said: PostRequest) -> PostOutcome:
    """Post into one discussion as the person: a copy to everyone in it, each woken."""
    peers = RepositoryPeers(known.checkout)
    posted = peers.post_into(
        thread,
        said.text,
        sender=USER_ADDRESS,
        door=Door.PAGE,
        in_reply_to=said.in_reply_to,
        joining=tuple(said.to),
    )
    posting = Posting(post=posted.post, thread=posted.thread)
    return PostOutcome(
        post=posted.post,
        thread=posted.thread,
        deliveries=[
            handed(known, peers, member.id, posting) for member in posted.reached
        ],
        refused=posted.refused,
    )


def paused_in(known: KnownRepository, outcome: Paused) -> PauseOutcome:
    """One repository's pause, as the page reads it."""
    return PauseOutcome(
        repositories=[known.key()],
        held=len(outcome.held),
        frozen=[f"{known.key()}/{member}" for member in outcome.frozen],
        unfrozen=[
            NotFrozenSession(session=f"{known.key()}/{each.member}", why=each.why)
            for each in outcome.unfrozen
        ],
        detail=outcome.detail,
    )


def resumed_in(known: KnownRepository, outcome: Resumed) -> PauseOutcome:
    """One repository's resume, as the page reads it."""
    return PauseOutcome(
        repositories=[known.key()],
        continued=len(outcome.continued),
        woken=[f"{known.key()}/{member}" for member in outcome.woken],
        detail=outcome.detail,
    )


def pause_member(
    known: KnownRepository, member_id: str, asked: PauseRequest
) -> PauseOutcome:
    """Hold one agent -- with its subagents, or with everything it spawned -- at its next call."""
    peers = RepositoryPeers(known.checkout)
    standing(peers, known, member_id)
    scope = HoldScope.TREE if asked.tree else HoldScope.AGENT
    return paused_in(known, pause(peers, scope, member_id, freeze=asked.freeze))


def resume_member(
    known: KnownRepository, member_id: str, asked: ResumeRequest
) -> PauseOutcome:
    """Lift the pause on one agent, or on its tree, and wake it where it stopped."""
    peers = RepositoryPeers(known.checkout)
    scope = HoldScope.TREE if asked.tree else HoldScope.AGENT
    try:
        return resumed_in(known, resume(peers, scope, member_id))
    except LookupError as nothing:
        raise Refused(str(nothing)) from nothing


def pause_repositories(
    served: list[KnownRepository], asked: PauseRequest
) -> PauseOutcome:
    """Hold every agent of each repository named at its next call, as one pause."""
    if asked.tree:
        raise Refused("a repository's pause names no tree: it holds every agent there")
    group = uuid.uuid4().hex[:12]
    outcomes = [
        paused_in(
            known,
            pause(
                RepositoryPeers(known.checkout),
                HoldScope.REPOSITORY,
                freeze=asked.freeze,
                group=group,
            ),
        )
        for known in served
    ]
    return PauseOutcome(
        repositories=[key for each in outcomes for key in each.repositories],
        held=sum(each.held for each in outcomes),
        frozen=[key for each in outcomes for key in each.frozen],
        unfrozen=[missed for each in outcomes for missed in each.unfrozen],
        detail=" ".join(each.detail for each in outcomes),
    )


def resume_repositories(served: list[KnownRepository]) -> PauseOutcome:
    """Lift the pause over each repository named; one that stood unpaused is passed over.

    Asked of one repository, one that stood unpaused is refused, saying so.
    """

    def lifted() -> Iterator[PauseOutcome]:
        for known in served:
            try:
                yield resumed_in(
                    known, resume(RepositoryPeers(known.checkout), HoldScope.REPOSITORY)
                )
            except LookupError as nothing:
                if len(served) == 1:
                    raise Refused(str(nothing)) from nothing

    outcomes = list(lifted())
    return PauseOutcome(
        repositories=[key for each in outcomes for key in each.repositories],
        continued=sum(each.continued for each in outcomes),
        woken=[key for each in outcomes for key in each.woken],
        detail=" ".join(each.detail for each in outcomes)
        or "Nothing to resume: no repository stood paused.",
    )


def supervision_models() -> list[type[BaseModel]]:
    """Every model the routes below read and answer with, which the page is typed against."""
    return [
        MessageRequest,
        ReplyOutcome,
        Nothing,
        NameRequest,
        Renamed,
        Stopped,
        TextRequest,
        Broadcast,
        LiveNotice,
        Withdrawn,
        DescriptionRequest,
        Described,
        ClaimRequest,
        Claimed,
        Released,
        InboxReadRequest,
        InboxRead,
        PostRequest,
        PostOutcome,
        PauseRequest,
        ResumeRequest,
        NotFrozenSession,
        PauseOutcome,
        TranscriptPage,
        FollowRequest,
        FollowOutcome,
    ]


def supervision_routes(
    app: FastAPI, feed: LiveFeed, served: tuple[Feature, ...] = SUPERVISED
) -> None:
    """Serve every verb above on *app*, over the repositories *feed* serves.

    *app* is the dashboard's, whose gate holds every write to its capability,
    its own origin and JSON. The stream tells the page what is *served*, so a
    piece of supervision is offered exactly where a route here answers for it.
    """
    feed.serves(served)

    def known_by(key: str) -> KnownRepository:
        found = next((each for each in feed.served() if each.key() == key), None)
        if found is None:
            raise HTTPException(status_code=404, detail="No repository has that key")
        return found

    def answered[Outcome](action: Callable[[], Outcome]) -> Outcome:
        try:
            return action()
        except (Refused, PeerDepartedError) as refused:
            raise HTTPException(status_code=409, detail=str(refused)) from refused
        except LookupError as missing:
            raise HTTPException(status_code=404, detail=str(missing)) from missing

    @app.get("/api/repositories/{repository}/sessions/{member}/transcript")
    def transcript(
        repository: str,
        member: str,
        before: int | None = Query(None, ge=0),
        limit: int = Query(TRANSCRIPT_PAGE, ge=1),
    ) -> TranscriptPage:
        """One page of a session's or subagent's transcript, its lines ending by byte ``before``."""
        known = known_by(repository)
        path = answered(lambda: member_transcript(known, member))
        return transcript_page(f"{repository}/{member}", path, before, limit)

    @app.post("/api/transcripts/follow")
    def follow(asked: FollowRequest) -> FollowOutcome:
        """Follow these transcripts on the stream for a lease more; one left out lapses."""
        return feed.follow_transcripts(asked.sessions)

    @app.post("/api/repositories/{repository}/sessions/{member}/messages")
    def message(repository: str, member: str, said: MessageRequest) -> ReplyOutcome:
        """The operator's message to one session or subagent, by its member id."""
        known = known_by(repository)
        return answered(lambda: reply(known, member, said))

    @app.post("/api/repositories/{repository}/sessions/{member}/wake")
    def wake_route(repository: str, member: str, asked: Nothing) -> ReplyOutcome:
        """Make one session look, with whatever waits for it."""
        del asked
        known = known_by(repository)
        return answered(lambda: wake_member(known, member))

    @app.post("/api/repositories/{repository}/sessions/{member}/name")
    def name(repository: str, member: str, asked: NameRequest) -> Renamed:
        """Call one session or subagent something else."""
        known = known_by(repository)
        return answered(lambda: rename(known, member, asked.name))

    @app.post("/api/repositories/{repository}/sessions/{member}/stop")
    def stop_route(repository: str, member: str, asked: Nothing) -> Stopped:
        """End one session's runtime, where this dashboard can tell it is that runtime."""
        del asked
        known = known_by(repository)
        return answered(lambda: stop(known, member))

    @app.post("/api/repositories/{repository}/broadcast")
    def broadcast_route(repository: str, said: TextRequest) -> Broadcast:
        """Say one thing to every working member of a repository."""
        known = known_by(repository)
        return answered(lambda: broadcast(known, said.text))

    @app.post("/api/repositories/{repository}/notices")
    def notice_route(repository: str, said: TextRequest) -> LiveNotice:
        """State something every session here reads at the head of each prompt."""
        known = known_by(repository)
        return answered(lambda: notice(known, said.text))

    @app.delete("/api/repositories/{repository}/notices/{notice_id}")
    def withdraw_route(repository: str, notice_id: str) -> Withdrawn:
        """Take one standing notice down."""
        known = known_by(repository)
        return answered(lambda: withdraw(known, notice_id))

    @app.post("/api/user/description")
    def description(said: DescriptionRequest) -> Described:
        """Say what the person is on, in every repository served."""
        return describe(feed.served(), said.text)

    @app.post("/api/repositories/{repository}/claims")
    def claim_route(repository: str, asked: ClaimRequest) -> Claimed:
        """Hold a path as the person."""
        known = known_by(repository)
        return answered(lambda: claim(known, asked.path))

    @app.delete("/api/repositories/{repository}/claims")
    def release_route(repository: str, asked: ClaimRequest) -> Released:
        """Give back a path the person holds."""
        known = known_by(repository)
        return answered(lambda: release(known, asked.path))

    @app.post("/api/repositories/{repository}/inbox/read")
    def inbox_read(repository: str, asked: InboxReadRequest) -> InboxRead:
        """Take these messages out of the person's mailbox, as read."""
        known = known_by(repository)
        return answered(lambda: read_inbox(known, asked.ids))

    @app.post("/api/repositories/{repository}/threads/{thread}/posts")
    def thread_post(repository: str, thread: str, said: PostRequest) -> PostOutcome:
        """Post into one discussion, to everyone in it."""
        known = known_by(repository)
        return answered(lambda: post(known, thread, said))

    @app.post("/api/repositories/{repository}/sessions/{member}/pause")
    def pause_route(repository: str, member: str, asked: PauseRequest) -> PauseOutcome:
        """Hold one agent at its next tool call, freezing it where asked."""
        known = known_by(repository)
        return answered(lambda: pause_member(known, member, asked))

    @app.post("/api/repositories/{repository}/sessions/{member}/resume")
    def resume_route(
        repository: str, member: str, asked: ResumeRequest
    ) -> PauseOutcome:
        """Let one paused agent go on."""
        known = known_by(repository)
        return answered(lambda: resume_member(known, member, asked))

    @app.post("/api/repositories/{repository}/pause")
    def pause_repository_route(repository: str, asked: PauseRequest) -> PauseOutcome:
        """Hold every agent of one repository at its next tool call."""
        known = known_by(repository)
        return answered(lambda: pause_repositories([known], asked))

    @app.post("/api/repositories/{repository}/resume")
    def resume_repository_route(repository: str, asked: Nothing) -> PauseOutcome:
        """Lift one repository's pause."""
        del asked
        known = known_by(repository)
        return answered(lambda: resume_repositories([known]))

    @app.post("/api/pause")
    def pause_everything_route(asked: PauseRequest) -> PauseOutcome:
        """Hold every agent of every repository served at its next tool call."""
        return answered(lambda: pause_repositories(feed.served(), asked))

    @app.post("/api/resume")
    def resume_everything_route(asked: Nothing) -> PauseOutcome:
        """Lift every repository's pause."""
        del asked
        return answered(lambda: resume_repositories(feed.served()))
