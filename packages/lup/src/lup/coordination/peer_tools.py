"""The verbs a session uses to reach the other sessions in its repository.

Distinct from the cohort's verbs, and the difference is who the population is.
A cohort's tools steer agents this session started: it knows their addresses
because it minted them, and it may redirect them because it owns them. These
address peers nobody here started, so there is no listing of "what I spawned"
to fall back on and no authority to redirect — what is left is finding out who
is here, saying what you are doing so they can find you, and telling one of
them something.

The descriptions are the whole documentation an agent gets, so they carry the
one fact that changes a decision: what a message will and will not do. A peer
reached through its own hook will read this before its next tool call; a peer
whose mail only waits in a file will read it whenever it next looks, which may
be never. A sender told "sent" cannot tell those apart, so nothing here says
"sent".
"""

import asyncio
from pathlib import Path

from pydantic import BaseModel, Field
from pydantic.json_schema import SkipJsonSchema

from lup.channels.models import Door
from lup.coordination.identity import NameTakenError, member_ref
from lup.coordination.pulse import Pulse
from lup.coordination.refs import ActorRef
from lup.coordination.repository import (
    PeerDepartedError,
    PeerView,
    RepositoryPeers,
    nested,
)
from lup.coordination.bare.store import CALLER_FIELD, MEMBERS_DIR, Caller
from lup.coordination.roster import Delivery
from lup.coordination.wake import WakePath
from lup.tools.mcp import LupMcpTool, ServerCompanion, ToolError, lup_tool


class RosterPulse(ServerCompanion, frozen=True):
    """This session's pulse, beaten for as long as its tool server serves.

    The server is started when the session opens and stopped when it ends,
    however that ending comes, so its lifetime is the session's, and a beat
    every interval is what lets every other process read that. Each tick
    sweeps first, so every row whose pulse stopped is retired on the record
    by whichever session is up rather than by the one that died; then it
    joins, which the roster's own idempotence makes free while the row is
    standing and is what puts it back after a finish the session outlived —
    a cleared conversation, a rewound one, a sweep that ran while this
    server was stalled. A session that really ended writes its finish and
    stops ticking, so that finish stands.

    Nothing is written where no session has ever joined: a beat or a sweep
    there would create the store, and a session that never coordinates must
    leave no sign of having been able to. The members directory is the whole
    test, as it is for the prompt-time guard — no member has a file there
    until one joins, and the first join is a session's own act rather than
    this tick's.
    """

    root: Path
    member_id: str
    pulse: Pulse = Pulse()
    wake: WakePath = WakePath()

    async def run(self) -> None:
        peers = RepositoryPeers(self.root, pulse=self.pulse)
        members = peers.root / MEMBERS_DIR
        while True:
            if members.is_dir():
                peers.sweep(by=member_ref(self.member_id))
                peers.join(
                    self.member_id,
                    self.root,
                    delivery=Delivery.INBOX,
                    wake=self.wake,
                )
                peers.beat(self.member_id)
            await asyncio.sleep(self.pulse.interval_seconds)


class Called(BaseModel):
    """What every verb carries beside its own arguments: which conversation called.

    Stamped by the caller hook each runtime fires before a coordination tool
    runs, never by the agent: the field is left out of the schema the agent is
    shown, and the hook overwrites whatever arrives in it with its own
    payload's answer. No tool server's environment tells a session's
    conversations apart — every one of them shares the session's — so without
    it a subagent's call is indistinguishable from the session's —
    which is how a subagent's description came to replace its orchestrator's.

    Empty is the session's own conversation, and is also what a call no hook
    stamped reads as, so a runtime carrying no caller hook acts as the session
    it always did.
    """

    caller: SkipJsonSchema[Caller] = Field(
        default_factory=Caller, validation_alias=CALLER_FIELD
    )


class PeerListOutput(BaseModel):
    """Every session working in this repository, the live ones first.

    Each session carries its native subagents beneath it, as rows of their own.
    """

    peers: list[PeerView] = []


class DescribeInput(Called):
    description: str = Field(
        description=(
            "What you are working on now, in one line, as somebody deciding "
            "whether to interrupt you would want to read it"
        )
    )


class DescribeOutput(BaseModel):
    description: str


class RenameInput(Called):
    name: str = Field(
        description=(
            "What this session is called from now on: what a person types to "
            "reach it, so short and telling. The name it had goes on reaching "
            "it too, until another session takes that name"
        ),
        min_length=1,
    )


class RenameOutput(BaseModel):
    name: str


class PeerSayInput(Called):
    address: str = Field(
        description=(
            "Which peer to reach: a subagent row's or a session's. Its id from "
            "the roster always reaches it, whatever anything is called; a name "
            "works too, including one somebody wrote down before that peer "
            "renamed"
        )
    )
    text: str = Field(description="What the peer should read")


class PeerSayOutput(BaseModel):
    address: str
    delivery: Delivery = Field(
        description=(
            "What carries this. `inbox` means the peer's own hook puts it in "
            "front of its next tool call. `mailbox` means it waits in the file "
            "until that peer next looks, and nothing will wake it"
        )
    )
    outstanding: int = Field(
        description=(
            "How much is queued for that peer and not yet handed over, this "
            "message included"
        )
    )


class PrefixInput(Called):
    path: str = Field(
        description=(
            "What to take or give back, as a path. A directory covers "
            "everything beneath it; a file covers only itself"
        )
    )


class ClaimOutput(BaseModel):
    path: str
    holders: list[str] = Field(
        description=(
            "Every session holding it. More than one means a change nothing "
            "could attribute — both had a window open over it — and the next "
            "named edit settles which of them it was"
        )
    )


class InboxOutput(BaseModel):
    """What was waiting for this session, consumed by the reading."""

    messages: list[str] = []


def create_peer_tools(
    peers: RepositoryPeers,
    member_id: str,
    worktree: Path,
    wake: WakePath = WakePath(),
    door: Door = Door.AGENT,
) -> list[LupMcpTool]:
    """The repository verbs, bound to one roster and one session's identity.

    The identity is bound here rather than taken as an argument for the reason
    a resolver worker's concern is: a session that could name itself in a call
    could describe another session's work as its own, or read another
    session's inbox — and neither is a thing to be trusted rather than made
    unspellable.

    Which conversation of the session called is the one thing a call carries,
    because every server serving them starts under the session's environment
    and nothing else tells them apart.
    It reaches only rows beneath this session: a subagent's row is keyed under
    the session it runs in, so no spelling of a caller names somebody else's.
    """

    def present(caller: Caller) -> ActorRef:
        """Put this session, and the subagent calling where one is, on the roster.

        Every verb calls this, because a session that never joined is one
        nothing else can reach: a description applies to no member and the
        fold drops it, and a peer looking for who is working here reads a
        roster this session is absent from. The row handed back is the one
        the call acts on — the calling subagent's own, beneath the session,
        or the session's where the session itself called.

        Here rather than where the tools are built, because building them
        must not create the store — a session that never coordinates should
        leave no sign of having been able to. Idempotent through the roster's
        own announce, so every call after the first costs a fold rather than
        a record.

        ``INBOX`` because this session has the plugin carrying the delivery
        hook. What a member says about itself is what a sender is told, so
        claiming the weaker mode here would understate what a message does.
        """
        peers.join(member_id, worktree, delivery=Delivery.INBOX, wake=wake)
        if not caller.get("agent_id"):
            return member_ref(member_id)
        return peers.join_subagent(member_id, caller)

    def spoken(acting: ActorRef) -> None:
        """Refuse to act on the roster for a row that has not said what it is on.

        The roster is read by sessions deciding whether they can touch the
        same code, and a row saying only where a session is answers them
        wrongly; a session that has run all day without describing itself
        is the ordinary case, not the exception. So every verb that reads
        peers or reaches them is refused until the calling row has described
        itself in this conversation — a rewind or a clear unsays the
        description, and the next such call asks for it again. A subagent's
        row is read the same way as its session's, so a subagent says what
        it is on as well.
        """
        standing = peers.row(acting.id)
        if standing is None or not standing.description:
            raise ToolError(
                "say what you are working on with `coordination_describe` "
                "first: the roster is read by sessions deciding whether they "
                "can safely touch the same code, and your row answers them "
                "only where you are, not what you are doing"
            )

    @lup_tool(
        "List every session working in this repository, including the ones in "
        "other worktrees, with the ones still working first, and any that "
        "stopped since you joined, saying so. Each row is who they are, "
        "which checkout they are in, what they are doing, what they are "
        "holding, and what reaches them; a session's native subagents are "
        "rows of their own under its `subagents`.\n\n"
        "Reach for it before starting something substantial: another session "
        "may already be on it, may hold the file you are about to rewrite, or "
        "may have settled the question you are about to re-derive. It costs "
        "one call and the alternative is finding out at merge time. It is "
        "refused until you have said what you are on with "
        "`coordination_describe`, because your row is read the same way.\n\n"
        "`doing` is what a row said about itself and may be old; `holding` is "
        "what its calls actually changed or locked, so that is the field to "
        "read before writing. Anything in `contested` is held by more than one "
        "row already. A path under somebody's `holding` is not forbidden — say "
        "so with `coordination_send` first.\n\n"
        "The person watching is not a row and needs no listing — they are "
        "always reachable at `user`. Returns {peers: [{address, cli_name, "
        "doing, holding, contested, member, subagents}]}.",
        name="coordination_peers",
    )
    async def coordination_peers(params: Called) -> PeerListOutput:
        spoken(present(params.caller))
        # Swept first, so the listing says what is so at this call rather
        # than at the last tick: a claim over a path that has gone is ended
        # now, and a session whose pulse stopped is retired now.
        peers.sweep(by=member_ref(member_id))
        standing = peers.row(member_id)
        return PeerListOutput(
            peers=nested(
                peers.listing(since=standing.arrived if standing is not None else None)
            )
        )

    @lup_tool(
        "Say what you are working on now, so the roster tells the truth about "
        "you. Anyone listing this repository's sessions reads it, and every "
        "other coordination verb is refused until you have.\n\n"
        "Call it at the start and again whenever what you are doing changes: "
        "the roster is read by somebody deciding whether they can safely "
        "touch the same code, and a description from an hour ago answers "
        "that question wrongly. One line is enough — what you are on, not "
        "how it is going. From a subagent it describes that subagent's own "
        "row, beneath its session's, and leaves the session's alone.",
        name="coordination_describe",
    )
    async def coordination_describe(params: DescribeInput) -> DescribeOutput:
        peers.describe(present(params.caller).id, params.description)
        return DescribeOutput(description=params.description)

    @lup_tool(
        "Call yourself something a person would type to reach you. A "
        "session's name defaults to its worktree's, numbered where another "
        "session in the same one got there first, and that is usually "
        "enough — rename when a name would tell peers more than the checkout "
        "does. A subagent starts under the name its spawn gave it, and its id "
        "reaches it whatever it is called.\n\n"
        "A name another live row answers to is refused. The name you had goes "
        "on reaching you until some other row takes it. Returns {name}.",
        name="coordination_rename",
    )
    async def coordination_rename(params: RenameInput) -> RenameOutput:
        acting = present(params.caller)
        try:
            peers.rename(acting.id, params.name)
        except NameTakenError as taken:
            raise ToolError(str(taken)) from taken
        return RenameOutput(name=params.name)

    @lup_tool(
        "Tell another session in this repository something, or one of its "
        "subagents. Use it to hand over a fact it could not have had — a "
        "bound you just computed, a decision that makes half its work moot, a "
        "file you are about to rewrite underneath it. A subagent reaches the "
        "session that dispatched it at that session's address.\n\n"
        "This never blocks and never stops anything. It also never guarantees "
        "arrival: the reply says what carries it, and `mailbox` means nothing "
        "will wake that peer — it reads when it next looks. If what you need "
        "is a decision before you can continue, that is a question for the "
        "person, not a message to a peer.\n\n"
        "Address `user` to reach whoever is watching. Returns {address, "
        "delivery, outstanding}.",
        name="coordination_send",
    )
    async def coordination_send(params: PeerSayInput) -> PeerSayOutput:
        acting = present(params.caller)
        spoken(acting)
        addressed = peers.address(params.address)
        if addressed is not None and addressed.id == acting.id:
            raise ToolError(
                f"{params.address!r} is your own address; "
                "`coordination_peers` lists the others"
            )
        try:
            found = peers.send(params.address, params.text, door=door)
        except PeerDepartedError as departed:
            raise ToolError(
                f"{departed}; `coordination_peers` lists who is here"
            ) from departed
        if found is None:
            known = ", ".join(view.address for view in peers.listing()) or "none"
            raise ToolError(
                f"no session in this repository answers to {params.address!r}; "
                f"present: {known}"
            )
        return PeerSayOutput(
            address=found.label(),
            delivery=peers.cohort.delivery(found),
            outstanding=peers.cohort.outstanding(found),
        )

    @lup_tool(
        "Read what other sessions have said to you, and consume it. Use it "
        "when you want to check for messages deliberately — a session whose "
        "mail waits in a file has nothing that will interrupt it, so this is "
        "the only way it hears anything.\n\n"
        "Reading consumes: what this hands back will not be handed back "
        "again. Returns {messages: [text]}.",
        name="coordination_inbox",
    )
    async def coordination_inbox(params: Called) -> InboxOutput:
        delivery = peers.take(present(params.caller).id)
        return InboxOutput(
            messages=[
                f"[{'redirect' if message.redirect else 'message'} by "
                f"{message.door}] {message.text}"
                for message in delivery.messages
            ]
        )

    @lup_tool(
        "Take everything beneath a path, before you have touched any of it. "
        "Reach for it when you are about to rewrite a package or move a tree: "
        "what your calls change is recorded for you as you go, but that is "
        "after the fact, and the moment worth telling anybody about is before "
        "the first write.\n\n"
        "Another session about to edit under it is asked first, and told your "
        "name. From a subagent the lock is that subagent's own, so its "
        "siblings are asked too. It is never refused — two sessions in one "
        "tree is sometimes right — so this makes the collision visible rather "
        "than impossible.\n\n"
        "It expires when you do, or when the path does. There is nothing to "
        "remember to release, and releasing early is `coordination_release`. "
        "A path that does not exist is refused: a lock covers what is there to "
        "write. Returns {path, holders}.",
        name="coordination_lock",
    )
    async def coordination_lock(params: PrefixInput) -> ClaimOutput:
        acting = present(params.caller)
        spoken(acting)
        target = Path(params.path).resolve()
        if not target.exists():
            raise ToolError(
                f"{target} does not exist; a lock covers what is there to "
                "write, so name the directory or file you are about to change"
            )
        held = peers.lock(acting.id, target)
        return ClaimOutput(
            path=held.path, holders=[holder.id for holder in held.holders]
        )

    @lup_tool(
        "Give back a prefix you took, so nobody is asked about it again. Use "
        "it when you have finished with a tree you locked and expect to keep "
        "working on other things — otherwise just stop, because a claim ends "
        "with whoever holds it.\n\n"
        "Only a holder can release: a prefix you do not hold is refused, "
        "naming who does, which is what stops one session — or one subagent — "
        "unlocking another's work. Returns {path, holders} for what is still "
        "held there.",
        name="coordination_release",
    )
    async def coordination_release(params: PrefixInput) -> ClaimOutput:
        acting = present(params.caller)
        spoken(acting)
        target = Path(params.path).resolve()
        if not peers.release(acting.id, target):
            holders = [
                holder.id for claim in peers.holding(target) for holder in claim.holders
            ]
            raise ToolError(
                f"you do not hold {target}"
                + (f"; held by {', '.join(holders)}" if holders else "")
            )
        remaining = peers.holding(target)
        return ClaimOutput(
            path=str(target),
            holders=[holder.id for claim in remaining for holder in claim.holders],
        )

    return [
        coordination_peers,
        coordination_describe,
        coordination_rename,
        coordination_send,
        coordination_inbox,
        coordination_lock,
        coordination_release,
    ]
