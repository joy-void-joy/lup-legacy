"""Holds as the operator and the budget governor place and lift them.

:mod:`lup.coordination.bare.holds` is where a hold is read -- by every hook
that holds a call, and by everything here -- so this half is the vocabulary a
typed caller holds instead: the model one hold reads back as, and the verbs
that place one, lift one, and say who is held and why.

**Two owners, never crossing.** A hold is placed by the operator -- a pause,
from the dashboard or the command line -- or by the budget governor, which
keeps agents inside the limits the operator set. Each lift names its owner
and reason as well as its scope and member, so the governor letting an agent
go never ends the operator's pause of it, and a resume never ends a hold the
governor still means. Several holds may cover one agent at once, each its
own file, and a call waits until none does.

**Agents never place or lift one.** Nothing an agent can call reaches these
verbs: the coordination tools offer none, and the command-line verbs refuse a
shell an agent's runtime started. A hold is somebody else's say over an
agent's next call, and one the agent could lift would be no hold.
"""

from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, Field, ValidationError

from lup.coordination.bare import holds as bare
from lup.coordination.bare import store


class HoldScope(StrEnum):
    """Whom one hold covers, worked out from the roster at every check."""

    SELF = store.SELF_SCOPE
    """The one conversation named: a session's own, or one subagent's."""

    AGENT = store.AGENT_SCOPE
    """The conversation named and the native subagents inside its runtime."""

    TREE = store.TREE_SCOPE
    """The member named and everything its spawning reaches, at any remove."""

    REPOSITORY = store.REPOSITORY_SCOPE
    """Every member of the repository."""


class HoldOwner(StrEnum):
    """Who placed a hold, and so who alone lifts it."""

    OPERATOR = store.OPERATOR_OWNER
    BUDGET = store.BUDGET_OWNER


class HoldReason(StrEnum):
    """Why a hold stands, as its file is named and its row reads."""

    PAUSED = store.PAUSED_REASON
    """The operator paused it."""

    RATE = "over-rate"
    """It is spending faster than its rate allows."""

    SLOT = "waiting-for-slot"
    """It waits for one of a limited number of agents allowed to work at once."""

    RESERVE = "reserve"
    """An account's remaining allowance has reached the reserve kept back."""

    WINDOW = "window-exhausted-until"
    """An account's window is used up until it resets."""

    CAP = "cap"
    """It has reached the spend cap set for it."""


class FrozenGroup(BaseModel, frozen=True):
    """One process group a freeze stopped, as a resume has to find it again.

    The leader's pid and its start, read from the process table, so a resume
    that comes after the leader exited never continues a stranger that took
    its pid; and the pid namespace it was read in.
    """

    pid: int
    started: str = ""
    scope: str = ""


class Hold(BaseModel, frozen=True):
    """One hold, as placed."""

    scope: HoldScope
    member: str = ""
    """The roster id the hold is on; blank for a whole repository."""

    reason: HoldReason
    owner: HoldOwner
    said: str
    """What the agent's row, the page and a refused call read."""

    until: datetime | None = None
    """When it lifts by itself; never, where blank, until its owner lifts it."""

    placed: datetime = Field(default_factory=lambda: datetime.now(UTC))
    freeze: bool = False
    """The operator's second level: the agent's turn was stopped and its commands too."""

    frozen: list[FrozenGroup] = []
    frozen_sessions: list[str] = []
    """The sessions a freeze reached: their commands stopped and their turn
    interrupted. A freeze that could not reach a session -- a subagent alone,
    a runtime elsewhere -- leaves it paused and out of this list."""

    group: str = ""
    """One id shared by the holds a single pause of every repository placed."""

    def froze(self, member: str, parent: str = "") -> bool:
        """Whether this hold's freeze reached *member*, or the session it runs in."""
        return self.freeze and any(
            each in self.frozen_sessions for each in (member, parent) if each
        )

    @classmethod
    def read(cls, record: bare.Hold) -> "Hold | None":
        """One hold file read back, or nothing where it is not one this reads.

        A file spells "no ``until``" as blank, which this reads as none.
        """
        try:
            return cls.model_validate({**record, "until": record.get("until") or None})
        except ValidationError:
            return None

    def record(self) -> bare.Hold:
        """What this hold's file says."""
        return bare.Hold(
            scope=self.scope.value,
            member=self.member,
            reason=self.reason.value,
            owner=self.owner.value,
            said=self.said,
            until=self.until.isoformat() if self.until is not None else "",
            placed=self.placed.isoformat(),
            freeze=self.freeze,
            frozen=[
                bare.Frozen(pid=group.pid, started=group.started, scope=group.scope)
                for group in self.frozen
            ],
            frozen_sessions=list(self.frozen_sessions),
            group=self.group,
        )

    def name(self) -> str:
        """The file this hold lives in, which another hold of the same four replaces."""
        return bare.hold_name(self.record())


class HeldCall(BaseModel, frozen=True):
    """One call a hook is holding now: whose, which tool, and since when."""

    member: str
    since: datetime | None = None
    tool: str = ""
    call: str = ""


def operator_pause(
    scope: HoldScope, member: str = "", freeze: bool = False, group: str = ""
) -> Hold:
    """The operator's pause of *member* (blank for a repository) over *scope*."""
    return Hold(
        scope=scope,
        member=member,
        reason=HoldReason.PAUSED,
        owner=HoldOwner.OPERATOR,
        said=store.PAUSED_SAID,
        freeze=freeze,
        group=group,
    )


def place(root: Path, hold: Hold) -> Hold:
    """Put *hold* in the store, replacing the one of the same owner, reason, scope and member.

    *root* is the repository's coordination store. A governor that moves an
    ``until`` places the hold again, and the call it covers reads the new one
    at its next check.
    """
    if store.published(bare.hold_path(root, hold.record()), hold.record()) is None:
        raise OSError(f"could not write the hold {hold.name()} under {root}")
    return hold


def placed(root: Path) -> list[Hold]:
    """Every hold file in the store, whether or not it still stands."""
    return [
        hold
        for record in bare.placed_holds(root)
        for hold in [Hold.read(record)]
        if hold is not None
    ]


def lift(
    root: Path,
    owner: HoldOwner,
    reason: HoldReason,
    scope: HoldScope,
    member: str = "",
) -> Hold | None:
    """Take one hold out of the store; the hold lifted, or nothing where none stood."""
    named = bare.Hold(
        owner=owner.value, reason=reason.value, scope=scope.value, member=member
    )
    path = bare.hold_path(root, named)
    found = store.loaded(path, bare.Hold)
    if found is None or not store.discarded(path):
        return None
    return Hold.read(found)


def standing(root: Path, now: datetime | None = None) -> list[Hold]:
    """Every hold that still stands: not lapsed, and naming a member still here."""
    return [
        hold
        for record in bare.standing(root, now=now)
        for hold in [Hold.read(record)]
        if hold is not None
    ]


def holding(root: Path, member_id: str, parent: str = "") -> list[Hold]:
    """Every standing hold over one member, the operator's first.

    *parent* is the session a subagent runs in, which matters only for a
    subagent the roster does not hold a row for yet.
    """
    return [
        hold
        for record in bare.covering(root, member_id, parent)
        for hold in [Hold.read(record)]
        if hold is not None
    ]


def held_calls(root: Path) -> list[HeldCall]:
    """Every call a hook is holding now, oldest first."""
    calls = [
        HeldCall(
            member=store.text(marker.get("member")),
            since=store.spoken_at(store.text(marker.get("since"))),
            tool=store.text(marker.get("tool")),
            call=store.text(marker.get("call")),
        )
        for marker in bare.waiting(root)
    ]
    return sorted(
        calls, key=lambda held: held.since or datetime.min.replace(tzinfo=UTC)
    )


def swept(root: Path, now: datetime | None = None) -> list[Hold]:
    """Remove every hold that no longer stands, and hand back what went.

    A hold ends without anybody lifting it when its ``until`` passes or its
    member leaves; readers already pass over such a file, and this is what
    keeps the directory to the holds that mean something.
    """
    return [
        hold
        for record in bare.swept(root, now)
        for hold in [Hold.read(record)]
        if hold is not None
    ]
