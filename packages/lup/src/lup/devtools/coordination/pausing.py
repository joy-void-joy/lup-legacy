"""Pausing and resuming agents: the operator's two verbs over the holds an agent's hook reads.

A pause is a hold the operator places (:mod:`lup.coordination.holds`), and the
hook of every agent it covers keeps that agent's next tool call waiting until
it is lifted. That is the first level, and the default: the agent sees no
words, a call already running finishes, and the work goes on the moment the
operator resumes.

**Freezing is the second level**, for an agent whose running command or
generating turn the operator wants stopped now rather than at its next call.
The pause is placed as well, and then each session it covers is reached
through its runtime: the process group of every command its tools started is
stopped (SIGSTOP, never the runtime itself), and its generating turn is
interrupted with one line telling it it is paused. Resuming continues those
groups and wakes the session with a bare "continue". Measured on Claude Code
2.1.285: a Bash command stopped inside its tool timeout finishes once
continued, and one stopped past it is moved to the background by the runtime
-- which stops a background command still running half an hour later -- with
the session healthy throughout. What cannot be frozen is paused all the
same, and the answer says why each was not frozen: a subagent, whose
commands run in its session's runtime beside the session's own; a runtime in
another pid namespace; a Codex session, whose commands run under an
app-server daemon every session of its configuration home shares, so that
its command groups are not one session's (measured on Codex 0.159.2, where
stopping a command's group was otherwise safe: it finished once continued,
no timeout fired, and the thread took its next turn).

**Resuming wakes who stopped.** A session frozen, or whose call was refused
at the hold's limit and has not asked again since, ended its turn waiting to
be told to go on, so a resume wakes it with "continue", recorded on its mail
as the prompt it was rather than as a message. One whose call was only held
needs nothing: the call goes on by itself.

Only the operator's surfaces reach these verbs: the dashboard, and the
command line from a terminal outside every agent session.
"""

import os
import signal
from collections.abc import Iterator
from pathlib import Path

from pydantic import BaseModel

from lup.coordination.bare import holds as bare
from lup.coordination.bare import store
from lup.coordination.bare.mail import prompted
from lup.coordination.bare.runtime import (
    STARTED_FIELD,
    Runtime,
    process_scope,
    runtime_alive,
    stat_fields,
)
from lup.coordination.holds import (
    FrozenGroup,
    Hold,
    HoldOwner,
    HoldReason,
    HoldScope,
    holding,
    lift,
    operator_pause,
    place,
)
from lup.coordination.repository import PeerDepartedError, RepositoryPeers
from lup.coordination.roster import RosterMember
from lup.providers.wake import wake


class NotFrozen(BaseModel, frozen=True):
    """One member a freeze could not reach, and why; it is paused all the same."""

    member: str
    why: str


class StillHeld(BaseModel, frozen=True):
    """One member a resume let go of that another hold still covers, in that hold's words."""

    member: str
    said: str


class Freezing(BaseModel, frozen=True):
    """What freezing one session did: the groups it stopped, or why it stopped none."""

    groups: list[FrozenGroup] = []
    why: str = ""


class Paused(BaseModel, frozen=True):
    """What one pause did: whom it holds, whom it froze, and why the rest were not."""

    hold: Hold
    held: list[str] = []
    """Every live member it covers now, by id."""

    frozen: list[str] = []
    """The sessions whose commands were stopped and whose turn was interrupted."""

    unfrozen: list[NotFrozen] = []
    detail: str


class Resumed(BaseModel, frozen=True):
    """What one resume did: the pause it lifted, what it continued, and whom it woke."""

    hold: Hold
    continued: list[int] = []
    """The process groups continued, by their leader's pid."""

    woken: list[str] = []
    """The members told "continue", by id."""

    still: list[StillHeld] = []
    detail: str


def held_members(peers: RepositoryPeers, hold: Hold) -> list[RosterMember]:
    """Every live member *hold* covers, read from the roster as a hook would."""
    here = bare.roster(peers.root)
    rows = [row for row in peers.present() if row.running and row.kind != "user"]
    return [
        row
        for row in rows
        for chain in [bare.chain_of(row.actor.id, here, row.parent)]
        if bare.covers(hold.record(), chain, bare.native_reach(chain, here, row.parent))
    ]


def tool_groups(runtime: int) -> list[FrozenGroup]:
    """The process groups a runtime's tools run in: its children that lead a session of their own.

    Measured on Claude Code 2.1.285: each Bash call, foreground or
    background, and each hook runs as a direct child of the runtime leading
    its own session and group, while its tool servers share the runtime's own
    group. Measured on Codex 0.159.2, a command and a hook likewise lead
    their own session, while a tool server leads a group inside the runtime's
    session. So a child leading a session is a command or a hook, and
    stopping those stops every command the runtime started and no part of
    the runtime or its tool servers.
    """
    own = stat_fields(runtime)
    if len(own) <= 2:
        return []
    scope = process_scope()
    return [
        FrozenGroup(pid=int(entry.name), started=fields[STARTED_FIELD], scope=scope)
        for entry in Path("/proc").iterdir()
        if entry.name.isdigit()
        for fields in [stat_fields(int(entry.name))]
        if len(fields) > STARTED_FIELD
        and fields[1] == str(runtime)
        and fields[2] == entry.name
        and fields[3] == entry.name
        and fields[2] != own[2]
    ]


def stopped(groups: list[FrozenGroup], sent: signal.Signals) -> list[FrozenGroup]:
    """Send *sent* to each group, and hand back those that took it."""

    def took() -> Iterator[FrozenGroup]:
        for group in groups:
            try:
                os.killpg(group.pid, sent)
            except (ProcessLookupError, PermissionError):
                continue
            yield group

    return list(took())


def frozen_session(
    row: RosterMember,
    told: str = f"{store.PAUSED_SAID}; end your turn and wait to be told to continue",
) -> Freezing:
    """Stop one session's commands and interrupt its turn; what was stopped, or why nothing was.

    *told* is the line its interrupted turn starts with, so a turn that was
    generating ends, and the next one it starts ends at once, waiting.
    """
    if row.parent:
        return Freezing(
            why="a subagent's commands run in its session's runtime beside the "
            "session's own, so it is paused but not frozen; freeze its session "
            "to stop them"
        )
    if row.wake.runtime == "codex":
        return Freezing(
            why="a Codex session's commands run under the app-server its "
            "configuration home shares with every other session there, so "
            "stopping them could stop another session's; it is paused but not "
            "frozen"
        )
    if not row.process.pid:
        return Freezing(why="its row records no runtime process to stop commands under")
    recorded = Runtime(
        pid=row.process.pid, started=row.process.started, scope=row.process.scope
    )
    match runtime_alive(recorded, process_scope()):
        case None:
            return Freezing(
                why="its runtime runs in another pid namespace than this one -- "
                "a contained session -- so only its launcher could stop its commands"
            )
        case False:
            return Freezing(why="the runtime its row recorded is no longer running")
        case True:
            pass
    groups = stopped(tool_groups(row.process.pid), signal.SIGSTOP)
    worktree = Path(row.worktree) if row.worktree else None
    wake(row.wake, told, worktree, priority="now")
    return Freezing(groups=groups)


def pause(
    peers: RepositoryPeers,
    scope: HoldScope,
    member: str = "",
    freeze: bool = False,
    group: str = "",
) -> Paused:
    """Pause *member* (blank for the whole repository) over *scope*, and freeze it where asked.

    A member named must be on the roster and running. Pausing what is
    already paused replaces that pause, so a pause can be raised to a freeze.
    """
    if scope is HoldScope.REPOSITORY:
        member = ""
    else:
        row = peers.row(member)
        if row is None:
            raise LookupError(f"no agent here has the id {member!r}")
        if not row.running:
            raise PeerDepartedError(row, peers.called(member))
    hold = place(peers.root, operator_pause(scope, member, freeze=freeze, group=group))
    covered = held_members(peers, hold)
    sessions = [row for row in covered if not row.parent]
    targets = (
        (sessions or [row for row in covered if row.actor.id == member])
        if freeze
        else []
    )
    freezes = {row.actor.id: frozen_session(row) for row in targets}
    groups = [group for each in freezes.values() for group in each.groups]
    if freeze:
        hold = place(
            peers.root,
            hold.model_copy(
                update={
                    "frozen": groups,
                    "frozen_sessions": [
                        member_id for member_id, each in freezes.items() if not each.why
                    ],
                }
            ),
        )
    frozen = [member_id for member_id, each in freezes.items() if not each.why]
    unfrozen = [
        NotFrozen(member=member_id, why=each.why)
        for member_id, each in freezes.items()
        if each.why
    ]
    whom = "the whole repository" if not member else peers.called(member) or member
    said = f"Paused {whom}: {len(covered)} agent(s) are held at their next tool call."
    if freeze:
        said += (
            f" Froze {len(frozen)} session(s), stopping {len(groups)} command group(s)."
        )
    if unfrozen:
        said += " Not frozen: " + "; ".join(
            f"{peers.called(each.member) or each.member} ({each.why})"
            for each in unfrozen
        )
    return Paused(
        hold=hold,
        held=[row.actor.id for row in covered],
        frozen=frozen,
        unfrozen=unfrozen,
        detail=said,
    )


def resume(
    peers: RepositoryPeers,
    scope: HoldScope,
    member: str = "",
    continuing: str = "continue",
) -> Resumed:
    """Lift the operator's pause of *member* over *scope*, continue what it froze, wake who stopped.

    Only a pause placed on that member over that scope is lifted; an agent
    paused through its session or its repository is resumed there, which the
    refusal names. *continuing* is the bare prompt a session that stopped
    because of the pause is woken with: one frozen, or one whose call was
    refused at the hold's limit and has not asked again.
    """
    if scope is HoldScope.REPOSITORY:
        member = ""
    lifted = lift(peers.root, HoldOwner.OPERATOR, HoldReason.PAUSED, scope, member)
    if lifted is None:
        covering = holding(peers.root, member) if member else []
        named = [
            f"{hold.scope.value} pause of {peers.called(hold.member) or hold.member or 'the repository'}"
            for hold in covering
            if hold.owner is HoldOwner.OPERATOR
        ]
        raise LookupError(
            "nothing to resume: no pause stands on it over that scope"
            + (f"; it is held by the {', '.join(named)}" if named else "")
        )
    continued = stopped(
        [
            group
            for group in lifted.frozen
            if runtime_alive(
                Runtime(pid=group.pid, started=group.started, scope=group.scope),
                process_scope(),
            )
            is True
        ],
        signal.SIGCONT,
    )
    covered = held_members(peers, lifted)
    remaining = {
        row.actor.id: holding(peers.root, row.actor.id, row.parent) for row in covered
    }
    still = [
        StillHeld(member=member_id, said=holds[0].said)
        for member_id, holds in remaining.items()
        if holds
    ]
    refused = {store.text(call.get("member")) for call in bare.refused(peers.root)}
    stopping = [
        row
        for row in covered
        if not row.parent
        and not remaining[row.actor.id]
        and (lifted.froze(row.actor.id) or row.actor.id in refused)
    ]
    woken = [row.actor.id for row in stopping if continued_with(peers, row, continuing)]
    whom = "the whole repository" if not member else peers.called(member) or member
    said = f"Resumed {whom}."
    if continued:
        said += f" Continued {len(continued)} command group(s)."
    if woken:
        said += f' Woke {len(woken)} session(s) with a bare "continue".'
    if still:
        said += " Still held by something else: " + "; ".join(
            f"{peers.called(each.member) or each.member} ({each.said})"
            for each in still
        )
    return Resumed(
        hold=lifted,
        continued=[group.pid for group in continued],
        woken=woken,
        still=still,
        detail=said,
    )


def continued_with(peers: RepositoryPeers, row: RosterMember, continuing: str) -> bool:
    """Wake one session with a bare prompt, record it as the prompt it was; whether it reached.

    Its refused calls are forgotten either way: it has been told to go on.
    """
    worktree = Path(row.worktree) if row.worktree else None
    told = wake(row.wake, continuing, worktree)
    prompted(peers.root, store.Actor(kind=row.actor.kind, id=row.actor.id), continuing)
    bare.forgot_refusals(peers.root, row.actor.id)
    return told.reached
