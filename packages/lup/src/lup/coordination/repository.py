"""Everyone working in one repository, whichever worktree they are in.

A cohort is what one process assembled; this is the population nobody
assembled. Its members are sessions somebody started at different times in
different checkouts, and what makes them one population is the repository —
so joining is something a session does to itself rather than something done to
it, and leaving is too.

Built over the same roster, mail and journal a cohort uses, at the repository's
own directory. That is the whole of the reuse and it is the point: a message to
a peer and a message to a spawned worker land in the same kind of mailbox, are
read by the same fold, and are consumed the same way, so there is one delivery
path to get right rather than two that agree until they do not.

What this adds is the vocabulary a repository needs and a run does not — a
session names itself and may rename, says what it is doing as that changes, and
is addressed by that name as readily as by its id. A run's members are named by
the run; peers name themselves, and a name somebody wrote down has to go on
working after the session behind it has moved on.

Everything derived at the read — a row the pulse retired, a description a
rewind unsaid, a claim whose holders have stopped — is derived in
:mod:`lup.coordination.bare.store`, which a hook and the permission
dispatcher read the same store through. What is here is the vocabulary: the
verbs a session uses on the roster, and the rows a surface renders.

**A name is settled against every other member's**, which is the one decision
a member cannot take from its own file alone. Joining and renaming take the
store's roster lock for exactly that: long enough to read the directory and
rename one file, and held for nothing else a member writes.
"""

import logging
import shutil
from collections.abc import Callable
from datetime import datetime, timedelta
from functools import cached_property
from pathlib import Path

from pydantic import BaseModel, computed_field

from lup.channels.models import Door, utc_now
from lup.coordination.bare import store
from lup.coordination.bare.runtime import Runtime
from lup.coordination.cohort import ActorCohort
from lup.coordination.identity import (
    LaunchedMember,
    NameTakenError,
    derived_cli_name,
    member_ref,
    mint_member_id,
    session_cli_name,
)
from lup.coordination.bare.mail import new_post_id
from lup.coordination.mail import ActorDelivery, Posting
from lup.coordination.meeting import coordination_root
from lup.coordination.peers import USER_ADDRESS, USER_KIND, USER_TASK, user_peer
from lup.coordination.pulse import Pulse
from lup.coordination.refs import ActorRef
from lup.coordination.roster import (
    Delivery,
    Roster,
    RosterMember,
    folded_member,
    member_identity,
)
from lup.coordination.touches import HeldPath, folded_held_path
from lup.coordination.wake import WakePath

logger = logging.getLogger(__name__)

SUPERSEDED = (
    "touches.jsonl",
    "roster.jsonl",
    "messages.jsonl",
    "names.jsonl",
    "delivery",
    "heartbeats",
    "resets",
)
"""What the 0.2.x store kept in a repository's coordination directory.

Its records, folded whole by every reader — who joined, what each touched,
what each was called, what was said — and the directories its deliveries,
beats and resets sat in. The member-file store replaced all of it and nothing
reads any of it, so the first sweep of a clone that ran 0.2.x deletes it.
"""


def cleared(path: Path) -> None:
    """Delete one thing the 0.2.x store left, a directory with all it holds.

    A leftover that will not go is logged and left for the next sweep, since
    what a sweep is for — retiring the silent — must not wait on it.
    """
    try:
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink(missing_ok=True)
    except OSError:
        logger.warning("the sweep could not delete %s", path, exc_info=True)


class Retention(BaseModel, frozen=True):
    """How long a session that has stopped stays in a listing read with no arrival.

    A session reading the roster is told about the departures it was here for,
    which its own arrival bounds. A console has no arrival, so it is told about
    the recent ones instead, and this says how recent: long enough that a
    person coming back to a terminal finds out who left since they looked,
    short enough that a roster read a month on is not a history of everyone.

    The default is the store's own window, not a second figure beside it: the
    sweep deletes a departed stub past it, so a listing reaching further back
    would be asking for rows nothing keeps.
    """

    departed_seconds: float = store.DEPARTED_SECONDS

    def since(self, now: datetime) -> datetime:
        """The moment before which a departure is history rather than news."""
        return now - timedelta(seconds=self.departed_seconds)


class PeerDepartedError(LookupError):
    """Raised when a message is addressed to a session that has stopped.

    Raised rather than queued, because mail to a session that will never read
    it is a message the sender goes on believing was delivered. What the row
    says about the departure travels with it, so the surface reporting this
    can say when the session left and what it concluded.
    """

    def __init__(self, member: RosterMember, cli_name: str) -> None:
        left = member.heard.isoformat() if member.heard is not None else "unknown"
        outcome = member.summary or member.error
        super().__init__(
            f"{cli_name or member.actor.id} left at {left}"
            + (f": {outcome}" if outcome else "")
        )
        self.member = member
        self.cli_name = cli_name


class NotReached(BaseModel, frozen=True):
    """One member a post was meant for and did not reach, and why."""

    address: str
    reason: str


class ThreadPost(BaseModel, frozen=True):
    """One post into a discussion: the id every copy shares, and whom it reached."""

    post: str
    thread: str
    reached: list[ActorRef] = []
    refused: list[NotReached] = []


class PeerView(BaseModel, frozen=True):
    """One row of a repository roster, as a person or an agent reads it.

    The name and the member are carried separately rather than merged, because
    a member with no name is a real state — a session that joined before
    anything named it — and a row that invented one would be a row an operator
    could type and nothing would answer.
    """

    member: RosterMember
    cli_name: str = ""

    holding: list[str] = []
    """What this session's calls have actually claimed, as the fold spells it.

    Observed rather than declared, which is the whole reason it rides beside
    ``doing``: a description is what a session said about itself whenever it
    last said anything, and a reader deciding whether it is safe to write
    needs what that session touched. Empty is the honest state for a session
    that has changed nothing yet.
    """

    contested: list[str] = []
    """The claims here that another live session also holds.

    Separate from ``holding`` rather than a flag inside it, because the two
    answer different questions — what this session has, and what it does not
    have to itself — and a reader about to write needs the second only when it
    is not empty. A contested claim appears on both sessions' rows, which is
    the honest rendering of a path two members' own files both claim.
    """

    subagents: list["PeerView"] = []
    """The native subagents running inside this session, each a row of its own.

    Beneath the session rather than beside it, because the relation is what a
    reader needs first: these are conversations of that session, reached and
    described apart from it, and gone when it is. Empty in a flat listing,
    where each row stands alone and carries its session on its member.
    """

    @computed_field
    @property
    def address(self) -> str:
        """The spelling to send to: the name where there is one, else the id.

        Names first because that is what a person has: a roster is read to
        find somebody to talk to, and an id is what you fall back on when
        nothing has been called anything yet.
        """
        return self.cli_name or self.member.actor.id

    @computed_field
    @property
    def doing(self) -> str:
        """What this member is working on, falling back to what it arrived for.

        A member that has not described itself is not silent about its
        purpose — it joined for something — so the fallback is the task rather
        than a blank, and the blank only appears where there is genuinely
        nothing to say.
        """
        return self.member.description or self.member.task


def nested(views: list[PeerView]) -> list[PeerView]:
    """A flat listing with each subagent's row moved beneath its session's.

    A subagent whose session is not in the listing stays where it is, so a
    listing nests what it can and hides nothing.
    """
    sessions = [view.member.actor.id for view in views if not view.member.parent]
    return [
        view.model_copy(
            update={
                "subagents": [
                    child
                    for child in views
                    if child.member.parent
                    and child.member.parent == view.member.actor.id
                ]
            }
        )
        for view in views
        if view.member.parent not in sessions
    ]


class RepositoryPeers:
    """The roster of one repository, joined and read from any of its worktrees.

    Holds no session and spawns nobody: every method is a fold of files under
    the shared git directory, so a console, a hook and a tool server reading it
    at once see the same population and none of them has to be running for the
    others to work.

    *superseded* names what an older store kept in the directory, which a
    sweep deletes wherever it finds it.
    """

    def __init__(
        self,
        root: Path,
        pulse: Pulse = Pulse(),
        retention: Retention = Retention(),
        superseded: tuple[str, ...] = SUPERSEDED,
    ) -> None:
        self.root = coordination_root(root)
        self.pulse = pulse
        self.retention = retention
        self.superseded = superseded

    @cached_property
    def roster(self) -> Roster:
        """The record of who is here, opened to read it and for nothing else.

        The cohort holds the same directory, but reaching the record through
        the cohort opens it, and opening writes. Every read here goes through
        this instead, so a launcher asking which names are taken, or a listing
        of a repository nobody has joined, finds what is there and creates
        nothing.
        """
        return Roster(self.root)

    @cached_property
    def cohort(self) -> ActorCohort:
        """The roster, mail and journal, opened the first time one is asked for.

        Lazy because opening a cohort *writes*: it publishes the manifest that
        makes the directory findable and joins the person onto the roster. That
        is right when a caller has asked to reach somebody and wrong as a side
        effect of construction — a session assembling its tool groups, or a
        test building a registry, would otherwise create this repository's
        coordination store by mentioning it.

        Cached because the roster's own idempotence is per member and not per
        call: re-opening costs a fold and a lock each time, and every verb here
        goes through it.
        """
        return ActorCohort(
            self.root,
            run_id=self.root.parents[1].name,
            description="every session working in this repository",
        )

    def join(
        self,
        member_id: str,
        worktree: Path,
        cli_name: str = "",
        delivery: Delivery = Delivery.WAITING,
        wake: WakePath = WakePath(),
        spawned_by: str = "",
    ) -> ActorRef:
        """Put this session on the roster, and hand back the address it answers to.

        Idempotent through the roster's own announce, so a session may call it
        on every coordination request rather than remembering whether it has
        joined — which is what lets a hook and a tool server in one session
        each arrive without a channel between them.

        The name is settled here too, and settled once: a session keeps what
        it is called across every rejoin. One given a name — by this call, or
        by whoever launched it — answers to that; one given nothing is called
        after its worktree, numbered where another live session already is,
        so the address a listing prints for each of two sessions in one
        checkout reaches that one. A chosen name a live session already
        answers to is refused rather than numbered, because the caller meant
        it, and the refusal comes before the arrival so a refused join leaves
        no row behind.

        The wake path arrives rather than being worked out here, because what
        makes a session look is its runtime's own arrangement and this module
        is neither runtime's. Empty is the honest default and the answer for
        anything that did not ask its adapter: a member with no wake path
        still has its mailbox, and a sender is told nothing will nudge it
        rather than told a nudge was sent.

        *spawned_by* is the session whose shell started this one's runtime,
        which inherited that session's launched id: its row names it, and one
        given no name is called after it — the launcher's name in its
        environment is that session's, not its own.

        The arrival and the naming happen under the store's roster lock, so
        two sessions starting together cannot read the same set of taken names
        and both take the free one.
        """
        with store.naming_settled(self.root):
            current = self.standing_name(member_id)
            taken = self.names_taken(except_id=member_id)
            if cli_name and cli_name != current and cli_name in taken:
                raise NameTakenError(cli_name, taken[cli_name])
            peer = member_ref(member_id)
            self.cohort.roster.joined(
                peer,
                task=f"working in {worktree}",
                delivery=delivery,
                worktree=str(worktree),
                wake=wake,
                spawned_by=spawned_by,
            )
            wanted = (
                f"{self.called(spawned_by) or spawned_by}-spawned"
                if spawned_by
                else session_cli_name() or derived_cli_name(worktree)
            )
            chosen = cli_name or current or store.unique_cli_name(wanted, taken)
            if chosen != current:
                self.record_name(member_id, chosen)
            return peer

    def join_subagent(self, member_id: str, caller: store.Caller) -> ActorRef:
        """Put one of this session's native subagents on the roster, and hand back its row.

        Beneath the session, which must already stand: a subagent is part of
        the session it runs in, and a row with nothing above it would be one
        no listing could place. Idempotent as a join is, so every call the
        subagent makes may ask.
        """
        joined = store.joined_subagent(self.root, member_id, caller)
        if joined is None:
            raise LookupError(
                f"session {member_id} has no row to hold a subagent beneath; "
                "join it first"
            )
        return ActorRef(kind=store.actor_kind(joined), id=store.actor_id(joined))

    def actor(self, member_id: str) -> ActorRef:
        """The member one bare id names here: a subagent's row or a session's.

        Resolved rather than assumed, so every verb taking an id — a console's
        `--id`, a handoff's lock — reaches a subagent's row by the id the
        roster prints for it. `user` is the person, whose row is the one no
        session or subagent can be.
        """
        if member_id == USER_ADDRESS:
            return user_peer()
        found = store.actor_named(self.root, member_id)
        return ActorRef(kind=store.actor_kind(found), id=store.actor_id(found))

    def rename(self, member_id: str, cli_name: str) -> None:
        """Record what this session is called from now on, keeping the old name.

        The old name stays on the member's own file, so a reference somebody
        wrote down before the rename goes on reaching this session until
        another one claims that name. A name a live session currently answers
        to is refused, because the roster would then print one address for two.
        """
        with store.naming_settled(self.root):
            taken = self.names_taken(except_id=member_id)
            if cli_name in taken:
                raise NameTakenError(cli_name, taken[cli_name])
            self.record_name(member_id, cli_name)

    # lup: ignore[dict-str-payload] — keyed by the name a session answers to,
    # whose value is the member id holding it: two spellings of an identity
    # rather than an open payload
    def names_taken(self, except_id: str = "") -> dict[str, str]:
        """Every name a live session other than this one currently answers to.

        The store's own reading, over the kinds this layer counts as live, so
        a name this layer refuses is one a session's naming hook numbers past.
        """
        return store.names_taken(
            self.root, except_id, self.pulse.stale_after_seconds, USER_KIND
        )

    def record_name(self, member_id: str, cli_name: str) -> None:
        """Append one name to this member's own file, under that member's lock."""
        store.revised(
            self.root,
            member_identity(self.actor(member_id)),
            lambda member: store.renamed(member, cli_name),
        )

    def standing_name(self, member_id: str) -> str:
        """What this session is called on a file still under ``members/``.

        Deliberately blind to the departed, which is what makes a rejoin after
        a departure name itself again. The stub a departure leaves keeps the
        name for whoever reads *that* row, and a fresh file carrying no name
        because a stub beside it had one would be a session nothing addresses.
        """
        found = store.read_member(
            store.member_path(self.root, member_identity(self.actor(member_id))),
            running=True,
        )
        return store.current_name(found) if found is not None else ""

    def called(self, member_id: str) -> str:
        """What this session is called now, wherever its file sits.

        The departed included, because a surface reporting who left has the id
        and wants the name — and a name a live session has since taken reaches
        that one, which is why this is read for rendering and never for
        deciding what is free.
        """
        member = store.member_of(self.root, member_identity(self.actor(member_id)))
        return store.current_name(member) if member is not None else ""

    def describe(self, member_id: str, description: str) -> None:
        """Record what this session is doing now, for whoever reads the roster."""
        self.cohort.roster.describes(self.actor(member_id), description)

    def leave(self, member_id: str, summary: str = "") -> None:
        """Record that this session has stopped, so nobody addresses it again."""
        self.cohort.roster.finished(self.actor(member_id), summary=summary)

    def address(self, spelling: str) -> ActorRef | None:
        """The member one spelling reaches: an id, a printed label, or a name.

        An id first, because it is the spelling that cannot collide: a name is
        chosen and may spell anything, another member's id included, and a
        reader resolving a clash between names needs a spelling that always
        reaches exactly the member it was read off. A name is then resolved
        to the id it reaches and handed to the same fold, so a name and an id
        cannot reach different members and a spelling one surface accepts is
        not one the next rejects. A name resolves to the live member answering
        to it ahead of any that has stopped, so a name reused after a
        departure reaches the newcomer.
        """
        return self.cohort.reaching(spelling) or self.cohort.reaching(
            self.answering(spelling)
        )

    def answering(self, cli_name: str) -> str:
        """The member id one name reaches, blank where no member claimed it.

        Every claim on the name, newest first, live ones ahead of stopped
        ones. A name somebody wrote down before a rename goes on reaching the
        member that answered to it, and a name reused after a departure
        reaches whoever holds it now.
        """
        if not cli_name:
            return ""
        claims = [
            claiming["id"]
            for claiming in reversed(store.naming(self.root))
            if claiming["cli_name"] == cli_name
        ]
        live = self.live_ids()
        return next(
            (member_id for member_id in claims if member_id in live),
            claims[0] if claims else "",
        )

    def row(self, member_id: str, now: datetime | None = None) -> RosterMember | None:
        """One session as the record and the pulses say, or nothing where none stands."""
        return next(
            (member for member in self.present(now) if member.actor.id == member_id),
            None,
        )

    def listing(self, since: datetime | None = None) -> list[PeerView]:
        """The sessions here, and those that stopped since a moment the reader names.

        The sessions, which is one member short of the roster: the person is on
        it and is not a session. They need no listing either, being reachable
        at the same word in every repository — where a session's address is
        exactly what a reader cannot know without asking.

        A session that stopped is listed only back to *since*, and says so on
        its row. A session reading the roster names its own arrival, because
        what left before it came is history it could never have written to;
        a console reading with no arrival names the retention window. Naming
        nothing lists the live rows alone, which is what a roster a month old
        should read as: who is here, not everyone who ever was.

        Each row carries what its session is *holding* as well as what it says
        it is doing, because the question this listing is read to answer — is
        it safe to start here — is one a self-description cannot answer. The
        claims are folded once for the whole roster rather than once per row,
        so a listing costs the same read whatever the population.
        """
        claims = self.held()
        names = store.called(self.root)

        def told(member: RosterMember) -> bool:
            """Whether this row is news to a reader whose look starts at *since*."""
            if member.running:
                return True
            heard = member.heard
            return since is not None and heard is not None and heard >= since

        def row(member: RosterMember) -> PeerView:
            """One session, with what it is observed to hold folded in."""
            held = [
                claim
                for claim in claims
                if any(holder.id == member.actor.id for holder in claim.holders)
            ]
            return PeerView(
                member=member,
                cli_name=names.get(member.actor.id, ""),
                holding=[claim.subject() for claim in held],
                contested=[claim.subject() for claim in held if len(claim.holders) > 1],
            )

        return [row(member) for member in self.present() if told(member)]

    def person(self) -> PeerView:
        """The person's own row: what they say they are on, and what they hold.

        Read like any session's, from their own file, and addressed at `user`
        in every repository. They hold a path the way a session does, and
        never stop, so what they lock stands until they release it.
        """
        found = store.member_of(self.root, member_identity(user_peer()))
        member = (
            folded_member(found)
            if found is not None
            else RosterMember(actor=user_peer(), task=USER_TASK, running=True)
        )
        held = [
            claim
            for claim in self.held()
            if any(holder.id == USER_ADDRESS for holder in claim.holders)
        ]
        return PeerView(
            member=member,
            cli_name=USER_ADDRESS,
            holding=[claim.subject() for claim in held],
            contested=[claim.subject() for claim in held if len(claim.holders) > 1],
        )

    def recent(self, now: datetime | None = None) -> list[PeerView]:
        """The listing a reader with no arrival of its own gets: the retention window."""
        return self.listing(since=self.retention.since(now or utc_now()))

    def present(self, now: datetime | None = None) -> list[RosterMember]:
        """Every member as the files and their modification times say, live ones first.

        The shared fold's, so a row the prompt hook reads as gone and a row a
        tool call reads as gone are the same row. Only the window is this
        caller's, which is what a test moves.
        """
        return [
            folded_member(member)
            for member in store.present(
                self.root, now, window=self.pulse.stale_after_seconds
            )
            if store.text(member.get("kind")) != USER_KIND
        ]

    def beat(self, member_id: str) -> None:
        """Record that this session is here now."""
        store.beat(self.root, store.session_actor(member_id))

    def lapsed(self, now: datetime | None = None) -> list[RosterMember]:
        """Every session a sweep would retire: still listed, and no longer heard.

        What a console prints before it sweeps, which is why it is read rather
        than derived from a second reading of the record — there is no record
        beside the file, and the file's own time is the whole of the answer.
        """
        return [
            folded_member(member)
            for member in store.lapsed(self.root, now, self.pulse.stale_after_seconds)
            if store.text(member.get("kind")) != USER_KIND
        ]

    def sweep(
        self, now: datetime | None = None, by: ActorRef = user_peer()
    ) -> list[RosterMember]:
        """Move what the read already derives, and delete what nobody reads.

        A member whose pulse stopped has its file moved to the departed, so a
        reader that lists the directory agrees with one that stats the file;
        and a departed stub past the retention window is deleted, which is
        what keeps the roster the size of the population rather than of its
        history. A session that beats again after this re-joins on its next
        call, which the roster's own idempotence allows once the file has
        moved. The mail record is not touched: every message stays on it, and
        its readers page it rather than read it whole.

        What the 0.2.x store kept here, and the member-file store replaced, is
        deleted by the first sweep that finds it; a sweep finding none of it
        touches nothing.

        A claim needs no sweeping and *by* names nobody: a claim stands or it
        does not, and the filesystem is what says which. Whoever swept is a
        parameter the surfaces pass and this has no record to attribute to
        them.
        """
        moment = now or utc_now()
        # lup: solved: nothing deletes what the member-file store replaced in
        # this directory -- `touches.jsonl`, `roster.jsonl`, `messages.jsonl`,
        # `names.jsonl`, `delivery/`, `heartbeats/` and `resets/` stay on every
        # clone that ran 0.2.x; the user settled that the first sweep of this
        # store deletes them, and no sweep or migration does
        # lup: solved: nothing bounds `mail.jsonl` -- every message posted in
        # this clone stays on it, and the dashboard reads all of it; the user
        # has not settled whether this sweep keeps it to the retention window
        # (taking the dashboard's older history with it) or it stays whole
        retired = store.swept(self.root, moment, self.pulse.stale_after_seconds)
        for leftover in self.leftovers():
            cleared(leftover)
        return [folded_member(member) for member in retired]

    def leftovers(self) -> list[Path]:
        """What an older store left in this directory, of what is still there."""
        return [
            path
            for path in (self.root / name for name in self.superseded)
            if path.is_symlink() or path.exists()
        ]

    def send(
        self,
        to: str,
        text: str,
        redirect: bool = False,
        door: Door = Door.AGENT,
        in_reply_to: str = "",
        sender: str = "",
        posting: Posting = Posting(),
    ) -> ActorRef | None:
        """Post one message to whatever a sender spelled, or say it reached nobody.

        Returning the member rather than raising, because "nobody answers to
        that" is an answer a caller has to render differently on each surface —
        a tool says who is here, a console prints the roster — and a layer that
        chose for them would be choosing the wording of somebody else's error.

        A member that has stopped is the other answer, and that one raises:
        the address was right, the session is gone, and queuing for it would
        tell the sender nothing while the message waits for nobody.

        *sender* signs it with the address a reply reaches: the sending
        member's id, or `user` for the person. A reply names the post it
        answers in *in_reply_to*, and goes into that post's thread unless
        *posting* names one.
        """
        member = self.address(to)
        if member is None:
            return None
        standing = self.row(member.id)
        if standing is not None and not standing.running:
            raise PeerDepartedError(standing, self.called(member.id))
        self.cohort.say(
            member,
            text,
            redirect=redirect,
            door=door,
            in_reply_to=in_reply_to,
            sender=sender,
            posting=posting
            if posting.thread or not in_reply_to
            else posting.model_copy(update={"thread": self.thread_of(in_reply_to)}),
        )
        return member

    def thread_of(self, post: str) -> str:
        """The thread one post is in, which a reply to it goes into; the post itself where the record has no thread for it."""
        found = self.cohort.mail.found(post)
        if found is None:
            return post
        return found.message.thread or found.message.post or found.message.id

    def post_into(
        self,
        thread: str,
        text: str,
        sender: str,
        door: Door = Door.AGENT,
        in_reply_to: str = "",
        joining: tuple[str, ...] = (),
    ) -> ThreadPost:
        """Post once into a discussion: one copy to everyone in it but the sender, sharing a post.

        Everyone is whoever wrote in the thread or was written to, and whoever
        *joining* names, who is in it from then on. Each copy answers
        *in_reply_to*, or the thread's latest post, and carries the thread's
        title and everyone else in it, so its reader can answer them all.
        A member that has stopped is left out and said so, rather than
        failing the post for everyone still here.
        """
        found = self.cohort.mail.discussion(thread)
        if found is None:
            raise LookupError(
                f"no post on this repository's record began a thread {thread!r}"
            )
        joined = [self.address(each) for each in joining]
        missing = [
            each for each, ref in zip(joining, joined, strict=True) if ref is None
        ]
        if missing:
            raise LookupError(f"nobody here answers to {', '.join(missing)}")
        everyone = list(
            dict.fromkeys(
                [
                    *found.participants,
                    sender,
                    *(ref.id for ref in joined if ref is not None),
                ]
            )
        )
        named = {each: self.called(each) or each for each in everyone}
        post = new_post_id()
        reached: list[ActorRef] = []
        refused: list[NotReached] = []
        for reader in [each for each in everyone if each != sender]:
            posting = Posting(
                post=post,
                thread=thread,
                title=found.title,
                participants=[named[each] for each in everyone if each != reader],
            )
            try:
                landed = self.send(
                    reader,
                    text,
                    door=door,
                    in_reply_to=in_reply_to or found.last,
                    sender=sender,
                    posting=posting,
                )
            except PeerDepartedError as departed:
                refused.append(NotReached(address=named[reader], reason=str(departed)))
                continue
            if landed is None:
                refused.append(
                    NotReached(
                        address=named[reader], reason="nobody here answers to it"
                    )
                )
            else:
                reached.append(landed)
        return ThreadPost(post=post, thread=thread, reached=reached, refused=refused)

    def notify(self, text: str, door: Door = Door.AGENT, by: str = "") -> None:
        """State something true for every session here, and for whoever starts next.

        A notice is state rather than mail: it is read at the head of every
        prompt for as long as it stands, so a session opened tomorrow reads it
        at its first. Whoever is working is told now as well, because a fact
        worth stating is worth hearing before the turn they are in ends.
        """
        self.cohort.notify(text, door=door, by=by)

    def waiting(self, member_id: str) -> ActorDelivery:
        """What is queued for this session, consuming none of it."""
        return self.cohort.mail.waiting(self.actor(member_id))

    def take(self, member_id: str) -> ActorDelivery:
        """Take everything queued for this session, and record it as handed over.

        The delivery is returned rather than the bare messages so a caller
        holds what was consumed: the files it names have been deleted, so a
        caller that dropped the result has lost the mail rather than deferred
        it, and the type it gets back is the one it would have peeked at.
        """
        mailbox = self.cohort.mailbox(self.actor(member_id))
        delivery = mailbox.waiting()
        mailbox.commit(delivery)
        return delivery

    def delivered(self, member_id: str, delivery: ActorDelivery) -> None:
        """Record exactly these messages as handed over to this member by something else.

        What a wake the member's runtime accepted does: it carried them whole,
        so the member's own hook must not hand them over a second time. The
        rest of the mailbox — what arrived since, or what no wake carried —
        stays for that hook.
        """
        self.cohort.mailbox(self.actor(member_id)).commit(delivery)

    def carried(self, member_id: str, delivery: ActorDelivery) -> None:
        """Record that a wake carried these messages to this member, leaving them for its hook.

        What a redirect a wake carried needs: the member has read it, and its
        next tool call has still to be refused with it, which only its hook
        handing it over does; a wake does not carry it again.
        """
        self.cohort.mail.carried(self.actor(member_id), delivery)

    def live_ids(self) -> list[str]:
        """Every member still working here, by id, which is what expires a claim.

        The store's own reading, the person included: the person is never
        finished, so what they lock stands until they release it, and the
        compiled dispatcher asking who holds a path reads the same members.
        """
        return store.live_ids(self.root, window=self.pulse.stale_after_seconds)

    def held(self) -> list[HeldPath]:
        """Every claim a live session is holding, newest first."""
        return [folded_held_path(row) for row in store.held(self.root, self.live_ids())]

    def holding(self, path: Path) -> list[HeldPath]:
        """Every live claim a write to this path would land under."""
        return [
            folded_held_path(row)
            for row in store.covering(self.root, path, self.live_ids())
        ]

    def revise(
        self, member_id: str, revise: Callable[[store.Member], store.Member]
    ) -> bool:
        """Change what one member's own file says about what it holds.

        Under that member's own lock, because its tool server, its prompt hook
        and the permission dispatcher recording what a command just changed
        are three processes revising one file. Nothing is written for a member
        that has no file: a revision of nobody would put a row on the roster
        that never joined.
        """
        return (
            store.revised(self.root, member_identity(self.actor(member_id)), revise)
            is not None
        )

    def touched(self, member_id: str, *paths: Path) -> None:
        """Record that this session changed exactly these files.

        A path claimed again replaces its earlier claim rather than joining
        it: what a claim carries is the state this session left the path in,
        and the older reading is what the newer write has just made wrong.
        """
        self.revise(
            member_id,
            lambda member: store.claimed(member, [str(path) for path in paths], False),
        )

    def lock(self, member_id: str, prefix: Path) -> HeldPath:
        """Take everything beneath a prefix, ahead of having touched any of it."""
        self.revise(
            member_id, lambda member: store.claimed(member, [str(prefix)], True)
        )
        return HeldPath(
            path=str(prefix), prefix=True, holders=[self.actor(member_id)], at=utc_now()
        )

    def holds(self, member_id: str, prefix: Path) -> bool:
        """Whether this session took exactly this prefix and still holds it."""
        return any(
            claim.prefix
            and claim.path == str(prefix)
            and any(holder.id == member_id for holder in claim.holders)
            for claim in self.held()
        )

    def release(self, member_id: str, prefix: Path) -> bool:
        """Give a prefix back, saying whether this session held it to give.

        Nothing is written for a prefix this session does not hold: a release
        by somebody else would take nothing off that session's own file, and
        the write would be a write of nothing having happened.
        """
        if not self.holds(member_id, prefix):
            return False
        return self.revise(member_id, lambda member: store.unclaimed(member, prefix))


def launched_member(root: Path, name: str | None = None) -> LaunchedMember:
    """The identity a launcher mints for the session it is about to open in *root*.

    The id is minted; the name is ``name``, or the worktree's where none was
    given, numbered where a live session of this repository is already called
    that, so the runtime's own chrome and the roster agree on a name that
    reaches this session and no other. Read without joining, because a launch
    that only generates has to leave the store as it found it — the session
    joins for itself once it is open.
    """
    peers = RepositoryPeers(root)
    wanted = derived_cli_name(root) if name is None else name
    return LaunchedMember(
        member_id=mint_member_id(),
        cli_name=store.unique_cli_name(wanted, peers.names_taken()),
    )


class RuntimeMember(BaseModel, frozen=True):
    """Who one runtime's processes are on the roster, and whose shell started it."""

    member_id: str
    spawned_by: str = ""
    """The session whose launched id this runtime inherited, empty where the id is its own."""


def runtime_member(
    root: Path, launched: str, fallback: str, runtime: Runtime
) -> RuntimeMember:
    """The member a process of *runtime* answers as in *root*'s roster.

    *launched* is the id a launcher exported, which every process the
    launched runtime starts inherits: where *runtime* is not the one it was
    minted for, it is a member of its own, spawned by that session. With no
    launched id, *fallback* — what the runtime itself calls this session —
    is all there is, and no other runtime carries it.
    """
    if not launched:
        return RuntimeMember(member_id=fallback)
    own = store.own_member(coordination_root(root), launched, runtime)
    if own == launched:
        return RuntimeMember(member_id=launched)
    return RuntimeMember(member_id=own, spawned_by=launched)
