"""What keeps an agent's next tool call waiting until something lets it go.

A hold is a file in the store, placed by somebody other than the agent it
covers -- the operator pausing it, the budget governor keeping it inside a
limit -- and read by that agent's own tool hook before each call it makes.
While one covers the call the hook does not answer, so to the agent the call
only takes long. A runtime cancels a hook at the timeout its plugin declares
and lets the call run when it does, so the hook stops waiting short of that
and refuses the call in one sentence saying what holds it and to try again;
the retry is held afresh.

**Who a hold covers** is worked out from the roster at every check rather than
fixed when the hold is placed, so an agent that arrives after a pause is under
it as well:

- ``self``: the one conversation named, a session's own or one subagent's.
- ``agent``: that conversation and the native subagents beneath it, which run
  inside its runtime and pause with it.
- ``tree``: that member and everything its spawning reaches -- its subagents,
  and the runtimes started from its shell -- at any remove.
- ``repository``: every member of this store.

**A hold lasts** until its owner lifts it, until the moment its ``until``
names, or until the member it names leaves the roster -- whichever comes
first. Each lift names its owner, so a budget's release never ends the
operator's pause and a resume never ends a budget's hold.

**A held call is marked.** The hook writes one file under ``waiting/`` for
each call it holds, saying whose call it is and since when, and removes it as
the call goes on or is refused; that is how a reader tells an agent held at a
call from one simply idle while paused.

Shipped into each plugin's ``hooks/runtime/coordination/`` beside the fold it
reads the roster through, so it is written under that fold's constraints: the
standard library alone, and nothing that raises over a file somebody else
wrote. Every spelling a hold file carries is the store's, declared with the
rest of its layout.
"""

import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TypedDict

from . import store
from .store import (
    HOLDS_DIR,
    OPERATOR_OWNER,
    PAUSED_SAID,
    SUBAGENT_KIND,
    UNTRIED,
    WAITING_DIR,
    WHOLE_REPOSITORY,
    Member,
    discarded,
    listed,
    loaded,
    members,
    parent_of,
    published,
    spawner_of,
    spoken_at,
    stamped,
    text,
)


class Frozen(TypedDict, total=False):
    """One process group a freeze stopped, as resuming has to find it again.

    The group's leader by its pid and its start, so a resume that comes after
    the leader exited never continues a stranger that took its pid.
    """

    pid: int
    started: str
    scope: str


class Hold(TypedDict, total=False):
    """One hold as its file holds it."""

    scope: str
    member: str
    reason: str
    owner: str
    said: str
    until: str
    placed: str
    freeze: bool
    frozen: list[Frozen]
    frozen_sessions: list[str]
    group: str


class Waiting(TypedDict, total=False):
    """One call a hook is holding: whose, which, and since when.

    ``refused`` is when the hook gave up holding it at its limit and refused
    it, after which the marker stays, saying its caller may have ended its
    turn rather than asked again, until the caller's next held call or a
    resume clears it.
    """

    member: str
    since: str
    tool: str
    call: str
    refused: str


def hold_name(hold: Hold) -> str:
    """The file one hold lives in, named for everything that makes it this hold.

    Owner, reason, scope and member together, so placing a hold that already
    stands replaces it rather than standing twice, and a lift naming those four
    finds exactly the one it means.
    """
    member = text(hold.get("member")) or WHOLE_REPOSITORY
    owner, reason, scope = (
        text(hold.get("owner")),
        text(hold.get("reason")),
        text(hold.get("scope")),
    )
    return f"{owner}-{reason}-{scope}-{member}.json"


def hold_path(root: Path, hold: Hold) -> Path:
    """Where one hold sits in the store."""
    return root / HOLDS_DIR / hold_name(hold)


def placed_holds(root: Path) -> list[Hold]:
    """Every hold file in the store as written, whether or not it still stands."""
    return [
        hold
        for path in listed(root / HOLDS_DIR)
        for hold in [loaded(path, Hold)]
        if hold is not None
    ]


def holds_placed(root: Path) -> bool:
    """Whether any hold file is in the store at all, standing or not.

    The question a hook asks before every call, answered by one directory
    listing: nearly always nothing is held, and nothing more is read.
    """
    return bool(listed(root / HOLDS_DIR))


def lapsed(hold: Hold, now: datetime) -> bool:
    """Whether a hold's own ``until`` has passed, which ends it without anybody lifting it."""
    until = spoken_at(text(hold.get("until")))
    return until is not None and until <= now


def outlived(hold: Hold, here: dict[str, Member]) -> bool:
    """Whether the member a hold names has left the roster, which ends the hold with it."""
    named = text(hold.get("member"))
    return text(hold.get("scope")) != store.REPOSITORY_SCOPE and named not in here


def roster(root: Path) -> dict[str, Member]:
    """Every member with a file under ``members/``, by id."""
    return {text(member.get("id")): member for member in members(root)}


def standing(
    root: Path, here: dict[str, Member] | None = None, now: datetime | None = None
) -> list[Hold]:
    """Every hold that still stands: not lapsed, and naming somebody still here.

    *here* is the roster's members by id, read from the store where it is not
    handed in.
    """
    moment = now or datetime.now(UTC)
    present = here if here is not None else roster(root)
    return [
        hold
        for hold in placed_holds(root)
        if not lapsed(hold, moment) and not outlived(hold, present)
    ]


def chain_of(member_id: str, here: dict[str, Member], parent: str = "") -> list[str]:
    """*member_id* and everything above it, nearest first: who spawned whom.

    A subagent's row names the subagent that forked it or its session, and a
    session's the session whose shell started it, so the walk passes through
    the native subagents first, then the session they run in, then whoever
    launched that. *parent* is the session a subagent runs in, for a caller
    whose own row the roster does not hold yet. The walk goes no further than
    there are members, so a row naming itself or a loop ends it.
    """
    chain = [member_id]
    current = member_id
    for _ in range(len(here) + 1):
        found = here[current] if current in here else None
        if found is not None:
            above = spawner_of(found)
        else:
            above = parent if current == member_id else ""
        if not above or above in chain:
            return chain
        chain.append(above)
        current = above
    return chain


def native_reach(
    chain: list[str], here: dict[str, Member], parent: str = ""
) -> list[str]:
    """The part of *chain* inside one runtime: the subagents, then their session.

    Everything up to and including the first member that is not a subagent,
    which is the session the caller's conversation lives in. A caller whose
    row the roster does not hold yet is a subagent exactly when it has a
    *parent*.
    """
    for index, member_id in enumerate(chain):
        found = here[member_id] if member_id in here else None
        if found is not None:
            subagent = text(found.get("kind")) == SUBAGENT_KIND
        else:
            subagent = index == 0 and bool(parent)
        if not subagent:
            return chain[: index + 1]
    return chain


def covers(hold: Hold, chain: list[str], native: list[str]) -> bool:
    """Whether *hold* covers the caller whose ancestry is *chain*.

    *native* is the part of the chain inside the caller's own runtime, as
    :func:`native_reach` gives it. A scope this reader does not know covers
    nobody: a newer writer's hold is no reason to hold every call.
    """
    target = text(hold.get("member"))
    match text(hold.get("scope")):
        case store.REPOSITORY_SCOPE:
            return True
        case store.SELF_SCOPE:
            return bool(chain) and chain[0] == target
        case store.AGENT_SCOPE:
            return target in native
        case store.TREE_SCOPE:
            return target in chain
        case _:
            return False


def first_said(holds: list[Hold]) -> list[Hold]:
    """These holds with the operator's first, then the oldest: whose words a refusal says."""
    return sorted(
        holds,
        key=lambda hold: (
            text(hold.get("owner")) != OPERATOR_OWNER,
            text(hold.get("placed")),
        ),
    )


def covering(
    root: Path, member_id: str, parent: str = "", now: datetime | None = None
) -> list[Hold]:
    """Every standing hold over one caller, the one whose words it reads first.

    *parent* is the session the caller runs in where the caller is one of its
    subagents, which places a subagent nobody has seen yet under its session.
    The roster is read only where some hold is placed at all, so a call
    nothing holds costs one directory listing.
    """
    if not holds_placed(root):
        return []
    here = roster(root)
    chain = chain_of(member_id, here, parent)
    native = native_reach(chain, here, parent)
    return first_said(
        [hold for hold in standing(root, here, now) if covers(hold, chain, native)]
    )


def covered(root: Path, now: datetime | None = None) -> dict[str, list[Hold]]:
    """Every member something holds, with the holds over it, the operator's first.

    One read of the store for the whole roster, for a reader listing every
    member -- the dashboard, a resume -- where :func:`covering` would read it
    once per member. A member nothing holds is left out.
    """
    if not holds_placed(root):
        return {}
    here = roster(root)
    holds = standing(root, here, now)

    def over(member_id: str, member: Member) -> list[Hold]:
        """The holds over one member of the roster."""
        parent = parent_of(member)
        chain = chain_of(member_id, here, parent)
        native = native_reach(chain, here, parent)
        return first_said([hold for hold in holds if covers(hold, chain, native)])

    return {
        member_id: found
        for member_id, member in here.items()
        for found in [over(member_id, member)]
        if found
    }


def swept(root: Path, now: datetime | None = None) -> list[Hold]:
    """Remove every hold that no longer stands and every mark a member that left made.

    A hold ends without anybody lifting it when its ``until`` passes or its
    member leaves; readers already pass over such a file, and this keeps the
    directory to the holds that mean something. A call's mark outlives its
    hook only where the hook was killed, which is a member that left. The
    holds removed are handed back.
    """
    moment = now or datetime.now(UTC)
    here = roster(root)
    for marker in markers(root):
        member_id = text(marker.get("member"))
        if member_id not in here:
            discarded(waiting_path(root, member_id, text(marker.get("call")) or "call"))
    return [
        hold
        for hold in placed_holds(root)
        if lapsed(hold, moment) or outlived(hold, here)
        if discarded(hold_path(root, hold))
    ]


def refusal(holds: list[Hold]) -> str:
    """Why a call refused at the hook's limit was refused, in the hold's own words.

    The refusal's one clause: the hook that refuses gives it the diagnostic
    shape every refusal speaks, with :data:`~.store.RETRY` its way through.
    """
    said = text(holds[0].get("said")) if holds else ""
    return f"{said or PAUSED_SAID}; {UNTRIED}"


def waiting_path(root: Path, member_id: str, call: str) -> Path:
    """Where the marker for one held call sits."""
    return root / HOLDS_DIR / WAITING_DIR / f"{member_id}.{call}.json"


def markers(root: Path) -> list[Waiting]:
    """Every marker a hook left: the calls it holds, and the ones it refused."""
    return [
        marker
        for path in listed(root / HOLDS_DIR / WAITING_DIR)
        for marker in [loaded(path, Waiting)]
        if marker is not None
    ]


def waiting(root: Path) -> list[Waiting]:
    """Every call a hook is holding now, as its marker says."""
    return [marker for marker in markers(root) if not text(marker.get("refused"))]


def refused(root: Path) -> list[Waiting]:
    """Every call refused at a hold's limit whose caller has not asked again since."""
    return [marker for marker in markers(root) if text(marker.get("refused"))]


def forgot_refusals(root: Path, member_id: str) -> None:
    """Clear *member_id*'s refused calls: it asked again, or was told to go on."""
    for marker in refused(root):
        if text(marker.get("member")) == member_id:
            discarded(waiting_path(root, member_id, text(marker.get("call")) or "call"))


def held_call(
    root: Path,
    member_id: str,
    call: Waiting,
    began: float,
    seconds: float,
    parent: str = "",
    poll: float = 1.0,
    clock: Callable[[], float] = time.monotonic,
    pause: Callable[[float], None] = time.sleep,
) -> list[Hold]:
    """Hold one call while anything covers its caller; the holds still standing at the limit.

    Empty once nothing covers the caller -- straight away, almost always --
    and the holds that stand where *seconds* past *began* (on the monotonic
    clock) came first, which the caller refuses the call over. The store is
    read again every *poll* seconds, so a resume lets the call go within one.

    The call is marked as held from its first wait until it is let go,
    under the call's own id so two calls held at once mark apart. A call
    refused at the limit stays marked as refused, so a resume knows its
    caller may be waiting to be told to go on; a caller held again has
    asked again, and its refused calls are forgotten.
    """
    named = text(call.get("call")) or "call"
    marker = waiting_path(root, member_id, named)
    held = Waiting(member=member_id, tool=text(call.get("tool")), call=named)
    marked = False
    holds: list[Hold] = []
    try:
        for _ in iter(int, 1):
            holds = covering(root, member_id, parent)
            left = began + seconds - clock()
            if not holds or left <= 0:
                return holds
            if not marked:
                forgot_refusals(root, member_id)
                held["since"] = stamped()
                published(marker, held)
                marked = True
            pause(min(poll, left))
        return []
    finally:
        if holds:
            held["since"] = held.get("since") or stamped()
            held["refused"] = stamped()
            published(marker, held)
        if marked and not holds:
            discarded(marker)
