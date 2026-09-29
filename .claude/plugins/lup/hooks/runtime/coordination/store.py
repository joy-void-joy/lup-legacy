# lup: ignore[constant-declaration]
# Every name below is the store's own on-disk layout, which processes sharing
# no import must spell alike to meet at all — an identity of this store rather
# than a choice a caller can make. The typed writers import them from here
# instead of restating them, which is what a pin over two copies could only
# report after the fact.
"""The coordination store: one file per member, and everything else derived.

Append-only records folded whole by every reader on every call answer who is
here by replaying who has ever been here, which grows without bound, keeps
sixteen rows for two live sessions, and cannot be asked anything the records
were not written to answer — so a claim over a path in a deleted worktree
stands until another record retires it, and a change no window can attribute
is recorded with a guess and a list of rivals.

It is state. One file per member, written by nobody but that member's own
processes, and every relation between members derived at the read:

- **presence** is the session's runtime process, asked wherever it can be.
  A row names the process it answers for, and a reader in that process's
  namespace asks it directly; a reader elsewhere tests the pulse lock the
  session's tool server holds while that process runs; and a reader who can
  do neither reads the member file's modification time, which the owner
  touches while it lives — a file older than the window is a session that
  stopped without saying so. Nothing folds a departure to find out.
- **a claim** records the modification time of the path it was taken over. A
  reader stats that path: gone means vacant, newer than recorded means
  somebody else has written it since, and otherwise the claim stands. There is
  no vacating record to write and no rival to guess at.
- **a contest** is two live members' claims meeting on one path, which is a
  fact about their two files rather than a third record about both.
- **a name** is on the member that answers to it, with the names it answered
  to before beside it, so a reference somebody wrote down still resolves.
- **a subagent** is a member of its own, keyed under the session it runs in
  and naming that session as its parent, so it is present exactly while that
  session is and nothing has to beat for it.

What is left is bounded by the population rather than by its history: a member
that stops takes its file to ``departed/``, and the sweep deletes that after
the retention window.

Three processes read this and no two of them share an import — the typed
library inside a session's tool server, the hooks a runtime spawns as bare
scripts, and the compiled permission dispatcher. So the reading is written
once, here, under the strictest of the three constraints: the standard library
alone, no pydantic, no ``lup``. The library imports it; each plugin ships this
package into ``hooks/runtime/``.

**Writing is the owner's.** A member file is revised under its own lock, by
whichever of that member's processes is revising it. The lock is per member
rather than per store, so two sessions never wait on each other, and a lock
taken to add one claim is held for one read and one rename.

**Nothing here raises.** Every reader is on a path where failing would cost
more than not answering: a prompt, a tool call, a permission decision. An
unreadable file reads as absent, a half-written one is never seen because
every write lands by rename, and a record that is not an object is passed
over.

``TypedDict`` throughout, and partial for every shape another process writes:
a field a newer library adds must not make its file unreadable here.
"""

import fcntl
import json
import os
from collections.abc import Callable, Collection, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from itertools import count
from pathlib import Path
from typing import TypedDict
from uuid import uuid4

from .runtime import Runtime, process_scope, runtime_alive

STORE_DIR = "lup"
COORDINATION_DIR = "coordination"
"""Where one repository's peers meet, beneath its shared git directory.

Two worktrees that spelled this differently would coordinate with nobody, and
nothing anywhere would report the mismatch: both sessions work, and neither is
on the other's roster.
"""

MEMBERS_DIR = "members"
DEPARTED_DIR = "departed"
"""Who is here, and who was here recently enough to still be news.

Two directories rather than a flag inside one, because the question a reader
asks is almost always "who is here", and a flag would make answering it a
listing that opens every file ever written.
"""

MAILBOX_DIR = "mailbox"
NOTICES_DIR = "notices"
"""What is waiting for one member, and what is true for all of them.

Mail is addressed and consumed; a notice is neither. Keeping them apart is
what lets a notice be read by a member that did not exist when it was posted,
with no position for anybody to keep.
"""

MAIL_RECORD = "mail.jsonl"
"""Every message posted here, one line each, in the order they were posted.

Beside the mailboxes rather than instead of them: a mailbox is its reader's
position and empties as it is read, so what was said to a member, and by
whom, is answered here — by a reader following the file from where it last
stopped.
"""

LOOKS_DIR = "looks"
WINDOWS_DIR = "windows"
"""The two places one process keeps working state of its own under the store.

Declared beside the rest of the layout although nothing here reads either: the
dispatcher is the windows' only writer and reader, and the prompt fold is the
looks'. A name the layout carries in one place is a name a rename cannot leave
behind.
"""

ROSTER_LOCK = "roster.lock"
"""Taken for a join or a rename, and for nothing else.

Both decide a name against every other member's, which is the one question a
per-member lock cannot answer. Everything else a member writes is about itself
and is taken under its own lock, so the store-wide lock is held for as long as
it takes to read a directory and rename one file.
"""

MEMBER_KIND = "session"
"""What a repository peer is on the roster, beside spawned workers and the person.

The one kind that answers for its own presence, which is why the pulse applies
to it and to nothing else: a spawned agent is live because the process that
spawned it says so, and the person is never finished at all.
"""

SUBAGENT_KIND = "subagent"
"""What a native subagent is on the roster: one conversation inside a session.

A row of its own rather than the session's, because the subagent is somebody
else to the harness: it describes different work, holds different files, and
is addressed apart from the conversation that dispatched it. Sharing the
session's row made each subagent's description replace the orchestrator's,
and made a lock hold a file for the whole session rather than for the one
subagent writing it.

It answers for its presence through its session rather than a pulse of its
own: a subagent cannot outlive the session it runs in, and nothing beats for
it between the calls it makes.
"""

SUBAGENT_DELIVERY = "hook"
"""How mail reaches a subagent: its runtime's tool hook, before its next call.

The typed roster's spelling of that mode, written here because this is the one
writer of a subagent's row and it cannot import the roster.
"""

CALLER_FIELD = "lup_caller"
"""The argument a coordination call carries naming the conversation that made it.

Written by the caller hook both runtimes fire before a coordination tool runs,
and read by the tool server, which one session's conversations all share: the
call alone does not say whether the session or one of its subagents made it,
and the hook's payload does.
"""

STALE_AFTER_SECONDS = 120.0
"""How long a member's silence reads as absence, where nothing can ask its runtime.

A few beats wide rather than one, so a stalled scheduler or a slow disk is not
read as a departure; short because the roster is read to decide whether a path
is safe to write, and a dead session holding that decision open for an hour is
the failure this closes.
"""

DEPARTED_SECONDS = 18000.0
"""How long a member that stopped stays readable before the sweep deletes it.

Long enough that somebody back at a terminal finds out who left while they
were away, short enough that the store is the population and not its history.
"""


class Actor(TypedDict, total=False):
    """Whom a record attributes itself to, as every file spells it."""

    kind: str
    id: str
    round: int


class Wake(TypedDict, total=False):
    """What would make one member look, where anything can."""

    runtime: str
    handle: str
    session: str
    home: str
    scope: str


class Named(TypedDict, total=False):
    """One name a member answered to, and from when."""

    cli_name: str
    at: str


class Caller(TypedDict, total=False):
    """Which conversation of a session made a call, as its runtime's hook says.

    A blank ``agent_id`` is the session's own conversation. Both runtimes put
    the id and the type in every tool hook payload fired inside a subagent,
    and leave them off the session's own. ``name`` is what the spawn called
    the subagent, blank where that runtime's hook cannot read it.
    """

    agent_id: str
    agent_type: str
    cwd: str
    name: str


class Claiming(TypedDict):
    """One name, the member it reaches, and when that member claimed it."""

    cli_name: str
    id: str
    at: str


class Conversation(TypedDict, total=False):
    """Which conversation this member's row is describing.

    Carried on the member rather than stamped beside it, because what it
    qualifies — what this member says it is doing — is on the member too. A
    runtime that rewinds or clears keeps the process, the id and the pulse and
    signals none of it, so the prompt fold notices the transcript's roots move
    and clears its own description in the same write.
    """

    transcript: str
    roots: int


class Holding(TypedDict, total=False):
    """One path a member holds, and the state it left that path in.

    The modification time is what makes the claim answerable later. A record
    of *having written* a file says nothing about whether it still holds — the
    writer may be long gone, or somebody else may have written it since —
    while a recorded time can be compared against the path as it is now.
    """

    path: str
    prefix: bool
    mtime: float
    at: str


class Member(TypedDict, total=False):
    """One member as its own file holds it, plus what the read derives.

    Everything down to ``left_at`` is written; ``running`` and ``heard`` are
    not in the file at all — they are read as presence, from the process the
    row names where a reader can ask it and the file's modification time where
    it cannot. That is the whole of what replaces a departure record nobody
    wrote: a member is here while its runtime runs.
    """

    kind: str
    id: str
    round: int
    parent: str
    names: list[Named]
    task: str
    description: str
    summary: str
    error: str
    worktree: str
    liveness: str
    delivery: str
    wake: Wake
    runtime: Runtime
    conversation: Conversation
    claims: list[Holding]
    arrived: str
    left_at: str
    running: bool
    heard: str


class Held(TypedDict):
    """One claim as a reader about to write asks about it, with everyone on it.

    Derived across member files rather than recorded: more than one holder is
    two live members whose own files both claim the path, which is honest
    where a single record with a guessed author was not.
    """

    subject: str
    path: str
    prefix: bool
    holders: list[Actor]
    at: str


def text(value: str | None) -> str:
    """A field as a string, blank where the file carries something else.

    The declared type says string; the file another process wrote says
    whatever it says, so the value is checked rather than trusted.
    """
    return value if isinstance(value, str) else ""


def moment(value: float | None) -> float:
    """A field as a modification time, zero where it is not one."""
    return value if isinstance(value, float | int) else 0.0


def whole(value: int | None) -> int:
    """A field as a counting number, the first where it is not one."""
    return value if isinstance(value, int) and value >= 1 else 1


def actor_kind(actor: Actor | None) -> str:
    """What kind of member a record attributes itself to, blank where none."""
    return text(actor.get("kind")) if isinstance(actor, dict) else ""


def actor_id(actor: Actor | None, kind: str = "") -> str:
    """The id an actor object carries, blank where it is not one of *kind*."""
    if not isinstance(actor, dict) or (kind and actor_kind(actor) != kind):
        return ""
    return text(actor.get("id"))


def actor_round(actor: Actor | None) -> int:
    """Which attempt this record is about, the first where it does not say."""
    return whole(actor.get("round")) if isinstance(actor, dict) else 1


def conversation_of(actor: Actor | None) -> str:
    """Which conversation an actor speaks through, which outlives its round.

    Spelled as :meth:`~lup.coordination.refs.ActorRef.conversation` spells it:
    a member taken through a second round is that member further on and not a
    second one, and anything held per conversation is keyed by this.
    """
    kind, held = actor_kind(actor), actor_id(actor)
    return f"{kind}-{held}" if kind and held else ""


def member_actor(member: Member) -> Actor:
    """One member's address, as a claim or a message attributes itself to it."""
    return Actor(
        kind=text(member.get("kind")),
        id=text(member.get("id")),
        round=whole(member.get("round")),
    )


def stamped(value: datetime | None = None) -> str:
    """This moment as every file in the store spells one."""
    return (value or datetime.now(UTC)).isoformat()


def spoken_at(recorded: str) -> datetime | None:
    """A field's time as a moment, or nothing where it does not read as one."""
    try:
        parsed = datetime.fromisoformat(recorded)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def loaded[Record](path: Path, shape: type[Record]) -> Record | None:
    """One file read as *shape*, or nothing where it is not a JSON object.

    A file that will not parse reads as absent. Every write here lands by
    rename, so a half-written one is never seen under its own name — what this
    catches is a file some other tool wrote, and refusing to read the rest of
    the store over it would be the wrong trade on every path that reads.
    """
    try:
        record: Record = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) else None


def published[Record](path: Path, record: Record) -> Record | None:
    """Write one file so that no reader ever sees it half written.

    Into a neighbour and renamed, which is atomic within a directory on every
    filesystem this runs on: a reader either sees what was there before or
    sees the whole of what replaced it. That is what lets every read here go
    unlocked — only a writer that must first read takes the member's lock.

    Hands back what landed rather than whether it did, so a caller that wants
    the written shape has it and one that wants the verdict reads it as one.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        writing = path.with_name(f"{path.name}.{uuid4().hex[:8]}.writing")
        writing.write_text(json.dumps(record), encoding="utf-8")
        writing.replace(path)
    except OSError:
        return None
    return record


def listed(directory: Path, suffix: str = ".json") -> list[Path]:
    """Every file of this kind in one directory, in name order."""
    try:
        return sorted(
            path
            for path in directory.iterdir()
            if path.is_file() and path.name.endswith(suffix)
        )
    except OSError:
        return []


def discarded(path: Path) -> bool:
    """Remove one file, saying whether it was there to remove."""
    try:
        path.unlink()
    except OSError:
        return False
    return True


def session_actor(member_id: str) -> Actor:
    """The identity a repository session is a member under.

    The one kind a hook can assume: a prompt fold, a departure writer and the
    permission dispatcher each know a session id and nothing else, because the
    runtime that spawned them is a session's.
    """
    return Actor(kind=MEMBER_KIND, id=member_id)


def subagent_id(session: str, agent: str) -> str:
    """The roster id of one native subagent, nested under the session it runs in.

    The runtime's own id for the subagent, qualified by its session's. Nothing
    here can prove the runtime's id unique beyond the process that minted it,
    and a row keyed by it alone would let one session's call act on another
    session's subagent; keyed under the session, a call can only ever name a
    row of its own.
    """
    return f"{session}-{agent}"


def subagent_actor(session: str, agent: str) -> Actor:
    """The identity one native subagent is a member under."""
    return Actor(kind=SUBAGENT_KIND, id=subagent_id(session, agent))


def parent_of(member: Member) -> str:
    """The session a subagent's row runs in, blank for every other member."""
    if text(member.get("kind")) != SUBAGENT_KIND:
        return ""
    return text(member.get("parent"))


def actor_named(root: Path, member_id: str) -> Actor:
    """The member one bare id names here: a subagent where one carries it, else a session.

    An id alone is what a person types and what a claim or a message records,
    so every verb taking one resolves it here rather than assuming the kind —
    which is what lets a subagent's row be described, locked and read by the
    id the roster prints for it.
    """
    subagent = Actor(kind=SUBAGENT_KIND, id=member_id)
    return (
        subagent if member_of(root, subagent) is not None else session_actor(member_id)
    )


def member_path(root: Path, member: Actor) -> Path:
    """Where one member's own file sits while it is here.

    Named for the conversation rather than the bare id, because an id is
    unique only within a kind: a worker and a reviewer taken on over one
    concern are two members of one id, and a single file between them would
    let each answer for the other's presence, task and claims.
    """
    return root / MEMBERS_DIR / f"{conversation_of(member)}.json"


def departed_path(root: Path, member: Actor) -> Path:
    """Where one member's file sits once it has stopped."""
    return root / DEPARTED_DIR / f"{conversation_of(member)}.json"


def member_lock(root: Path, member: Actor) -> Path:
    """The lock one member's own processes revise its file under."""
    return root / MEMBERS_DIR / f"{conversation_of(member)}.lock"


def pulse_path(root: Path, member: Actor) -> Path:
    """The lock a session's tool server holds for as long as it answers for that session.

    Apart from :func:`member_lock`, which is taken for one read and one
    rename: this one is held for the life of the process holding it, and the
    kernel lets it go when that process ends however it ends — which is what
    makes it a fact a reader in another container can test, where the process
    itself is out of its sight. Kept when the member departs, because a
    server answering for a cleared conversation holds it across the
    departure and joins again.
    """
    return root / MEMBERS_DIR / f"{conversation_of(member)}.pulse"


def answered(root: Path, member: Actor) -> bool:
    """Whether a live process holds this member's pulse.

    Asked for a moment with a shared lock that yields at once: refused means
    somebody holds it. Nothing is created — a member nobody ever answered for
    has no pulse to test, which reads as nobody answering.
    """
    try:
        with pulse_path(root, member).open("rb") as handle:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            return False
    except OSError:
        return False


def read_member(path: Path, running: bool) -> Member | None:
    """One member file, with the presence its modification time carries.

    The file says what the member is; the stat says when it last said so.
    Neither is written twice, which is what stops a row claiming to be present
    under a process that stopped.
    """
    found = loaded(path, Member)
    if found is None or not text(found.get("id")):
        return None
    try:
        heard = datetime.fromtimestamp(path.stat().st_mtime, UTC)
    except OSError:
        return None
    settled = found.copy()
    settled["running"] = running
    settled["heard"] = stamped(heard)
    return settled


def blank_member(kind: str, member_id: str) -> Member:
    """A member with nothing said about it yet, for a writer about to say it."""
    return Member(
        kind=kind,
        id=member_id,
        round=1,
        parent="",
        names=[],
        task="",
        description="",
        summary="",
        error="",
        worktree="",
        liveness="",
        delivery="",
        wake=Wake(),
        runtime=Runtime(),
        conversation=Conversation(transcript="", roots=0),
        claims=[],
        arrived=stamped(),
        left_at="",
        running=True,
        heard=stamped(),
    )


def stored(member: Member) -> Member:
    """This member as its file holds it, without what the read derives.

    ``running`` and ``heard`` are the file's modification time. Writing them
    into the file would give a reader two answers to one question, and the
    written one would be the stale one.
    """
    settled = member.copy()
    settled.pop("running", None)
    settled.pop("heard", None)
    return settled


def write_member(root: Path, member: Member) -> bool:
    """Put one member's file down whole, by rename."""
    landed = published(member_path(root, member_actor(member)), stored(member))
    return landed is not None


def revised(
    root: Path, member: Actor, revise: Callable[[Member], Member]
) -> Member | None:
    """Read this member's file, apply *revise*, and put it back, under its lock.

    The one read-modify-write in the store, and the reason each member has a
    lock of its own: a session's tool server, its prompt hook and the
    permission dispatcher recording what a command just changed are three
    processes revising one file, and two of them reading before either writes
    would lose whichever wrote first.

    Per member rather than per store, so one session's writes never wait on
    another's, and the region is one read and one rename wide.

    Nothing is created for a member that has no file: a revision of nobody
    would put a row on the roster that never joined.
    """
    lock = member_lock(root, member)
    try:
        lock.parent.mkdir(parents=True, exist_ok=True)
        with lock.open("a", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                found = read_member(member_path(root, member), running=True)
                if found is None:
                    return None
                settled = revise(found)
                return settled if write_member(root, settled) else None
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError:
        return None


def beat(root: Path, member: Actor) -> None:
    """Record that this member is here now, without rewriting what it says.

    The modification time is the whole of the pulse, so a beat moves the time
    rather than revising the file: nothing is read, nothing is locked, and a
    beat cannot race a description being written in another process.

    Nothing is created. A beat is a member saying it is still here, so one for
    a member that never joined has nobody to be about — and a file put down by
    a beat would be a row on the roster that says nothing about who it is.
    """
    try:
        os.utime(member_path(root, member))
    except OSError:
        return


def named_runtime(root: Path, member: Actor) -> Runtime:
    """The runtime this member's row names, standing or departed, empty where it names none.

    The departed stub counts: a departure is written under a runtime, and a
    server deciding whether to put the row back is asking whose it was.
    """
    found = read_member(member_path(root, member), running=True) or read_member(
        departed_path(root, member), running=False
    )
    recorded = found.get("runtime") if found is not None else None
    return recorded if isinstance(recorded, dict) else Runtime()


def adopt(root: Path, member: Actor, runtime: Runtime) -> None:
    """Record *runtime* as the process this member's row answers for, where it names none."""

    def answering(found: Member) -> Member:
        """This member, naming the process it answers for."""
        if found.get("runtime"):
            return found
        settled = found.copy()
        settled["runtime"] = runtime
        return settled

    found = read_member(member_path(root, member), running=True)
    if found is not None and not found.get("runtime"):
        revised(root, member, answering)


@contextmanager
def naming_settled(root: Path) -> Iterator[None]:
    """Hold the one lock a member cannot decide its own name without.

    Every other write a member makes is about itself and is taken under its
    own lock. A name is decided against every other member's, so two members
    choosing at once would both read the same directory and both take the
    same name — which is precisely the collision the numbered default exists
    to rule out.

    Held for a read of the members directory and one write, and released
    whatever happens inside: a refused name must not leave the store's lock
    standing for the next member to wait on.
    """
    lock = root / ROSTER_LOCK
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open("a", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def unique_cli_name(wanted: str, taken: Collection[str]) -> str:
    """*wanted* where nobody else answers to it, else the first free numbered form.

    Numbered rather than refused, because the name is a default nobody chose:
    two sessions opened in one worktree are two peers and both are called
    after it, and two sessions' subagents spawned under one name are two rows,
    so the second has to be reachable by a name that is not the first's. A
    name somebody chose is refused instead, by the caller that knows it was
    chosen.
    """
    if wanted not in taken:
        return wanted
    return next(
        candidate
        for candidate in (f"{wanted}-{ordinal}" for ordinal in count(2))
        if candidate not in taken
    )


def joined_subagent(root: Path, session: str, caller: Caller) -> Actor | None:
    """Put one subagent's row down beneath its session, unless one stands already.

    A subagent joins by acting rather than by announcing itself: its first
    coordination call, or the first file a call of its changes, is what puts
    it here. Nothing is created beneath a session that never joined, for the
    reason a beat creates nothing — a session that never coordinates leaves no
    sign of having been able to, and its subagents are part of it.

    Named what its spawn called it, numbered where a live member already
    answers to that — which is why this takes the store's naming lock rather
    than the subagent's own: the name is decided against every other member's,
    and the tool server and the permission dispatcher recording what one call
    changed can both arrive first. Its id reaches it whatever it is called.
    ``None`` where no row stands for the call to act on.
    """
    agent = text(caller.get("agent_id"))
    parent = read_member(member_path(root, session_actor(session)), running=True)
    if not session or not agent or parent is None:
        return None
    actor = subagent_actor(session, agent)
    try:
        with naming_settled(root):
            if read_member(member_path(root, actor), running=True) is None:
                member = blank_member(SUBAGENT_KIND, text(actor.get("id")))
                member["parent"] = session
                kind = text(caller.get("agent_type")) or "native"
                member["task"] = f"{kind} subagent of {current_name(parent) or session}"
                member["delivery"] = SUBAGENT_DELIVERY
                member["worktree"] = text(caller.get("cwd")) or text(
                    parent.get("worktree")
                )
                spawned = text(caller.get("name"))
                taken = [
                    current_name(standing)
                    for standing in present(root)
                    if standing.get("running")
                ]
                if spawned:
                    member = renamed(member, unique_cli_name(spawned, taken))
                write_member(root, member)
    except OSError:
        return None
    return actor


def called_by[Value](
    arguments: dict[str, Value], caller: Caller
) -> dict[str, Value | Caller]:
    """A coordination call's arguments, with the conversation making it written in.

    Written over whatever arrived under :data:`CALLER_FIELD`, so what an agent
    put there is never what the server reads. Each runtime's caller hook reads
    its own payload into *caller* and hands the result back in its own
    envelope; this is the one part they share.
    """
    return {**arguments, CALLER_FIELD: caller}


def acting_id(session: str, caller: Caller) -> str:
    """The id of the row one call acts on, read without joining anything.

    For a check that must not write — whose claims a call would be asked
    about, which window a command's snapshot is kept under.
    """
    agent = text(caller.get("agent_id"))
    return subagent_id(session, agent) if agent else session


def acting(root: Path, session: str, caller: Caller) -> Actor:
    """The row one call acts on: its subagent's own where one made it, else its session's.

    The subagent's row is joined on the way, so the first thing a subagent
    does on the roster is enough to put it there.
    """
    agent = text(caller.get("agent_id"))
    if not agent:
        return session_actor(session)
    joined_subagent(root, session, caller)
    return subagent_actor(session, agent)


def members(root: Path) -> list[Member]:
    """Every member whose file is still under ``members/``, oldest arrival first.

    Present on the record, which the pulse may still contradict: a session
    killed a minute ago has a file here and a modification time that says so.
    :func:`present` is what applies that.
    """
    found = [
        member
        for path in listed(root / MEMBERS_DIR)
        for member in [read_member(path, running=True)]
        if member is not None
    ]
    return sorted(found, key=lambda member: text(member.get("arrived")))


def departed(root: Path, now: datetime | None = None) -> list[Member]:
    """Every member that stopped recently enough to still be worth reporting.

    Bounded by the retention window here rather than by the sweep alone, so a
    reader gets the same answer whether or not a sweep has run since.
    """
    since = (now or datetime.now(UTC)) - timedelta(seconds=DEPARTED_SECONDS)

    def recent(member: Member) -> bool:
        """Whether this departure is still news rather than history."""
        left = spoken_at(text(member.get("left_at")))
        return left is None or left >= since

    return [
        member
        for path in listed(root / DEPARTED_DIR)
        for member in [read_member(path, running=False)]
        if member is not None and recent(member)
    ]


def stale(heard: datetime | None, now: datetime, window: float) -> bool:
    """Whether a silence since *heard* has outlasted the window at *now*.

    A member nothing can be read for is one no reader can vouch for, which
    reads as absent rather than as present by default.
    """
    if heard is None:
        return True
    return now - heard > timedelta(seconds=window)


def absent(member: Member, why: str) -> Member:
    """This member read as no longer here, saying why."""
    gone = member.copy()
    gone["running"] = False
    gone["error"] = why
    return gone


def pulsed(
    member: Member, now: datetime, window: float, held: bool = False, scope: str = ""
) -> Member:
    """This member as its runtime, its pulse, or its file's modification time leaves it.

    Only a session answers for itself this way. A spawned agent's presence is
    the word of the process that spawned it, and the person is never finished
    at all, so both pass through as their files say.

    The process first, wherever the reader can ask it — the reader in *scope*
    whose pid namespace the row's runtime was recorded in. A runtime that runs
    is a session present however long its file was quiet: a machine that
    slept stopped every beat and no process. One that stopped is a session
    gone at once, whatever beat last. A reader who cannot ask takes *held* —
    a live process holding the session's pulse — as the same answer, and the
    clock only where neither speaks.
    """
    if text(member.get("kind")) != MEMBER_KIND or not member.get("running"):
        return member
    match runtime_alive(member.get("runtime"), scope):
        case True:
            return member
        case False:
            return absent(member, "its runtime stopped")
        case None if held:
            return member
        case None:
            heard = spoken_at(text(member.get("heard")))
            if not stale(heard, now, window):
                return member
            since = heard.isoformat() if heard else "it joined"
            return absent(member, f"unheard since {since}")


def housed(member: Member, sessions: list[str]) -> Member:
    """A subagent as its session leaves it: here only while that session is.

    *sessions* are the sessions still running. Every other member passes
    through, since only a subagent answers for itself through somebody else.
    """
    parent = parent_of(member)
    if not parent or not member.get("running") or parent in sessions:
        return member
    return absent(member, f"its session {parent} stopped")


def pulsed_members(
    root: Path, now: datetime, mine: str = "", window: float = STALE_AFTER_SECONDS
) -> list[Member]:
    """Every member still under ``members/``, as its own pulse and its session's leave it.

    *mine* is the reading member's own id, which is never read as absent.
    """
    scope = process_scope()
    here = [
        member
        if text(member.get("id")) == mine
        else pulsed(
            member,
            now,
            window,
            held=text(member.get("kind")) == MEMBER_KIND
            and answered(root, member_actor(member)),
            scope=scope,
        )
        for member in members(root)
    ]
    sessions = [
        text(member.get("id"))
        for member in here
        if text(member.get("kind")) == MEMBER_KIND and member.get("running")
    ]
    return [housed(member, sessions) for member in here]


def present(
    root: Path,
    now: datetime | None = None,
    mine: str = "",
    window: float = STALE_AFTER_SECONDS,
) -> list[Member]:
    """Every member this store holds, live ones first, as the read leaves them.

    The departed are here too, back to the retention window, because a reader
    that arrived after somebody left still wants to know what they concluded —
    and a sender addressing them has to be told they are gone rather than have
    the message wait for nobody.

    *mine* is the reading member's own id, which is never read as absent: the
    reader is manifestly here, and its own beat may land after this read.

    A member that left and came back is here once. Its departed stub is what
    the previous standing ended as, and reporting both would give a listing two
    rows for one session and a sender two answers about whether it can be
    reached.
    """
    moment_now = now or datetime.now(UTC)
    here = pulsed_members(root, moment_now, mine, window)
    standing = {conversation_of(member_actor(member)) for member in here}
    return sorted(
        [
            *here,
            *[
                member
                for member in departed(root, moment_now)
                if conversation_of(member_actor(member)) not in standing
            ],
        ],
        key=lambda member: not member.get("running"),
    )


def member_of(root: Path, member: Actor) -> Member | None:
    """One member, wherever its file sits, or nothing where none does."""
    here = read_member(member_path(root, member), running=True)
    if here is not None:
        return here
    return read_member(departed_path(root, member), running=False)


def live_ids(
    root: Path,
    now: datetime | None = None,
    mine: str = "",
    window: float = STALE_AFTER_SECONDS,
    without: str = "",
) -> list[str]:
    """Every member still working here, by id, which is what expires a claim.

    A claim is alive while its holder is, so this is the whole of the expiry
    rule: no timeout to tune, no release to forget, and the failure mode is a
    member that stopped taking its own claims with it.

    ``without`` leaves out one kind, which is how a caller asking who holds a
    claim and a caller asking which spellings reach somebody take one reading.
    The person is on the roster and holds nothing, so a claim reading leaves
    them out and an addressing reading must not.
    """
    return [
        text(member.get("id"))
        for member in present(root, now, mine, window)
        if member.get("running")
        and (not without or text(member.get("kind")) != without)
    ]


def names_of(member: Member) -> list[Named]:
    """Every name one member has answered to, oldest first."""
    found = member.get("names")
    return [
        named
        for named in (found if isinstance(found, list) else [])
        if isinstance(named, dict) and text(named.get("cli_name"))
    ]


def current_name(member: Member) -> str:
    """What this member is called now, or nothing where nothing named it."""
    found = names_of(member)
    return text(found[-1].get("cli_name")) if found else ""


def renamed(member: Member, cli_name: str) -> Member:
    """This member answering to one more name, keeping the ones before it.

    Kept rather than replaced, so a reference somebody wrote down an hour ago
    still reaches the member it named. There is no error a sender could be
    shown for using it: the name was correct when they read it.
    """
    settled = member.copy()
    settled["names"] = [*names_of(member), Named(cli_name=cli_name, at=stamped())]
    return settled


# lup: ignore[dict-str-payload] — keyed by member id, an identity the roster's
# own module mints and that a half with no type of lup's cannot name
def called(root: Path, now: datetime | None = None) -> dict[str, str]:
    """What each member is called now, by id."""
    return {
        text(member.get("id")): current_name(member)
        for member in present(root, now)
        if current_name(member)
    }


def naming(root: Path, now: datetime | None = None) -> list[Claiming]:
    """Every name any member has answered to, oldest claim first.

    The whole history rather than the current names: a name somebody wrote
    down before a rename goes on reaching the member it named until something
    else claims it. Ordered by when each was claimed, so a reader taking the
    last match takes the newest claim on that name.
    """
    return sorted(
        (
            Claiming(
                cli_name=text(named.get("cli_name")),
                id=text(member.get("id")),
                at=text(named.get("at")),
            )
            for member in present(root, now)
            for named in names_of(member)
        ),
        key=lambda claiming: claiming["at"],
    )


def subject_of(path: str, prefix: bool) -> str:
    """What a reader keys a claim by, which a lock and a touch differ in.

    A prefix and an exact path can be spelled identically and mean different
    things — locking ``src/lup`` is not touching a file of that name — so the
    kind is part of the key.
    """
    return f"{'under' if prefix else 'at'} {path}"


def path_mtime(path: str) -> float:
    """When the filesystem says this path was last written, zero where it is gone."""
    try:
        return Path(path).stat().st_mtime
    except OSError:
        return 0.0


def standing(claim: Holding) -> bool:
    """Whether this claim still says something about the path it names.

    Three ways it stops. The path is **gone**, and a claim over nothing names
    nothing anybody could write. Somebody has **written it since**, which the
    recorded time is there to notice — the claim was evidence of what this
    member left, and what stands there now is not it. Otherwise it **holds**.

    A prefix lock is exempt from the second: a directory's modification time
    moves whenever anything inside it is created or removed, including by the
    holder, so comparing it would retire a lock the moment it was used. A lock
    ends when its holder leaves, when it is released, or when the prefix goes.
    """
    path = text(claim.get("path"))
    if not path:
        return False
    current = path_mtime(path)
    if not current:
        return False
    return bool(claim.get("prefix")) or current <= moment(claim.get("mtime"))


def claims_of(member: Member) -> list[Holding]:
    """Every claim one member's file holds that still stands."""
    found = member.get("claims")
    return [
        claim
        for claim in (found if isinstance(found, list) else [])
        if isinstance(claim, dict) and standing(claim)
    ]


def held(root: Path, live: list[str], now: datetime | None = None) -> list[Held]:
    """Every claim a live member holds, one row per path, newest first.

    Two members holding one path meet here, which is the whole of how a
    contest is found: no record says they contest, their two files do.
    """
    # lup: ignore[empty-collection] — a fold gathering holders across files
    # into one row per subject, which no comprehension expresses: each member
    # contributes to a row an earlier member may already have created
    rows: dict[str, Held] = {}
    for member in present(root, now):
        if text(member.get("id")) not in live:
            continue
        for claim in claims_of(member):
            path = text(claim.get("path"))
            prefix = bool(claim.get("prefix"))
            subject = subject_of(path, prefix)
            at = text(claim.get("at"))
            found = rows.get(subject)
            rows[subject] = Held(
                subject=subject,
                path=path,
                prefix=prefix,
                holders=[*(found["holders"] if found else []), member_actor(member)],
                at=max(at, found["at"]) if found else at,
            )
    return sorted(rows.values(), key=lambda row: row["at"], reverse=True)


def covers(row: Held, candidate: str) -> bool:
    """Whether a path about to be written falls under this claim."""
    if not row["prefix"]:
        return candidate == row["path"]
    return candidate == row["path"] or candidate.startswith(row["path"] + "/")


def covering(
    root: Path, target: Path, live: list[str], now: datetime | None = None
) -> list[Held]:
    """Every live claim a write to this path would land under."""
    return [row for row in held(root, live, now) if covers(row, str(target))]


def claim_holders(
    root: Path,
    target: str,
    mine: str,
    now: datetime | None = None,
    session: str = "",
) -> list[str]:
    """Who else, still working here, is holding the path a write would land on.

    Live holders other than the asker. A claim expires with the member that
    made it, so a departed holder is nobody to ask; and a member meeting its
    own claim on every edit would be asked about its own work.

    *session* is the session *mine* runs in, where the asker is one of its
    subagents, and what it holds is not asked about either: the session
    dispatched this subagent into that work. The other way round is asked —
    a session writing under a subagent it has running is the one overwriting
    work in progress — and so is one subagent writing under its sibling.
    """
    live = live_ids(root, now)
    names = called(root, now)
    return sorted(
        {
            names.get(holder) or holder
            for row in covering(root, Path(target).resolve(), live, now)
            for holder in [actor_id(found) for found in row["holders"]]
            if holder and holder not in (mine, session)
        }
    )


def claimed(member: Member, paths: list[str], prefix: bool) -> Member:
    """This member holding these paths as well as whatever it held already.

    A path claimed again replaces its earlier claim rather than joining it:
    what a claim carries is the state this member left the path in, and the
    older reading is what the newer write has just made wrong.
    """
    at = stamped()
    taken = [
        Holding(path=path, prefix=prefix, mtime=path_mtime(path), at=at)
        for path in paths
    ]
    subjects = [subject_of(text(claim.get("path")), prefix) for claim in taken]
    settled = member.copy()
    settled["claims"] = [
        *[
            claim
            for claim in claims_of(member)
            if subject_of(text(claim.get("path")), bool(claim.get("prefix")))
            not in subjects
        ],
        *taken,
    ]
    return settled


def unclaimed(member: Member, prefix: Path) -> Member:
    """This member without the lock it took over *prefix*."""
    settled = member.copy()
    settled["claims"] = [
        claim
        for claim in claims_of(member)
        if not (bool(claim.get("prefix")) and text(claim.get("path")) == str(prefix))
    ]
    return settled


def record_claims(root: Path, mine: Actor, paths: list[str]) -> bool:
    """Write down what one member's call just changed, under that member's lock.

    The dispatcher's write. It runs after the work has already happened, so
    the call it belongs to cannot be undone by refusing, and a claim nobody
    could record costs a later reader an attribution while a raised exception
    would cost the session its ability to work.

    No rival is recorded and none is guessed at. Where two sessions had
    windows open over one path, both record a claim of their own and the
    contest is what a reader derives from meeting them — the same answer,
    arrived at from evidence rather than from a list of suspects.
    """
    if not actor_id(mine) or not paths:
        return False
    return revised(root, mine, lambda member: claimed(member, paths, False)) is not None


def family(root: Path, session: str, now: datetime | None = None) -> list[str]:
    """Every live member one session answers for: itself, and its subagents.

    What never leaves that session. A native send between two of them is one
    conversation of a session reaching another inside the same process, so
    nothing any other worktree could read is lost by carrying it natively.
    """
    return [
        text(member.get("id"))
        for member in present(root, now)
        if member.get("running")
        and session
        and session in (text(member.get("id")), parent_of(member))
    ]


def addresses(root: Path, now: datetime | None = None, beside: str = "") -> list[str]:
    """Every spelling that currently reaches a live member of this roster.

    Ids, the kind-qualified label a door prints, and whatever each member is
    called now, because a sender types whichever of those it last read — a
    check knowing only one of them would let the others through, which is the
    failure that made a redirect reach nobody.

    Every name a live member ever claimed, rather than only the one it answers
    to now. A name given up in a rename goes on reaching its old holder until
    somebody else takes it, and a member that went quiet long enough to read as
    gone, lost its name to a newcomer, and came back once the newcomer had left
    is still reachable under the name every listing prints for it. Both are one
    reading here, because a claim sits on the member that made it.

    Live members only. A session that has left is not somewhere a durable
    message would arrive either, so redirecting a send to it would trade one
    call reaching nobody for another.

    *beside* is the session asking, whose own :func:`family` is left out —
    and so is every name one of them answers to now, whoever else once did:
    a sender naming it means the conversation it reaches in its own process.
    """
    standing = [member for member in present(root, now) if member.get("running")]
    own = family(root, beside, now)
    reached = [member for member in standing if text(member.get("id")) not in own]
    ids = [text(member.get("id")) for member in reached]
    kept = [
        current_name(member) for member in standing if text(member.get("id")) in own
    ]
    return sorted(
        {
            *ids,
            *[
                f"{text(member.get('kind'))}:{text(member.get('id'))}"
                for member in reached
            ],
            *[
                claiming["cli_name"]
                for claiming in naming(root, now)
                if claiming["id"] in ids and claiming["cli_name"] not in kept
            ],
        }
    )


def listing_lines(root: Path, now: datetime | None = None) -> list[str]:
    """One line per live member, as somebody choosing who to reach reads it.

    A subagent's line is indented beneath its session's, which is the one
    relation a reader needs to tell a session from a conversation inside it.
    """

    def described(member: Member) -> str:
        """One member's line, joined from whichever of its parts are said."""
        worktree = text(member.get("worktree"))
        parts = [
            current_name(member) or text(member.get("id")),
            Path(worktree).name if worktree else "",
            text(member.get("description")) or text(member.get("task")),
            text(member.get("delivery")),
        ]
        return " — ".join(part for part in parts if part)

    standing = [member for member in present(root, now) if member.get("running")]
    sessions = [text(member.get("id")) for member in standing if not parent_of(member)]
    return [
        line
        for head in standing
        if parent_of(head) not in sessions
        for line in [
            described(head),
            *[
                f"  {described(child)}"
                for child in standing
                if parent_of(child) and parent_of(child) == text(head.get("id"))
            ],
        ]
    ]


def depart(root: Path, member: Actor, summary: str = "", error: str = "") -> bool:
    """Move this member's file to the departed, saying whether one was moved.

    A session's ending hook runs in a process with no typed writer to reach,
    and a file nobody moved reads as present until the pulse retires it —
    which is right for a session that was killed and needlessly vague for one
    that exited.

    A member that never joined leaves nothing: there is no file to move, and a
    departed stub for a member that never arrived is a row no listing should
    carry.

    A session takes its subagents with it, since none of them can outlive the
    process it runs in, and a row left standing beneath a departed session
    would be a subagent the read calls gone and nothing ever moves.
    """
    if not actor_id(member):
        return False
    found = read_member(member_path(root, member), running=False)
    if found is None:
        return False
    settled = found.copy()
    settled["summary"] = summary or text(found.get("summary"))
    settled["error"] = error or text(found.get("error"))
    settled["left_at"] = stamped()
    if published(departed_path(root, member), stored(settled)) is None:
        return False
    discarded(member_path(root, member))
    discarded(member_lock(root, member))
    if actor_kind(member) == MEMBER_KIND:
        for child in members(root):
            if parent_of(child) == actor_id(member):
                depart(root, member_actor(child), error="its session left")
    return True


def lapsed(
    root: Path, now: datetime | None = None, window: float = STALE_AFTER_SECONDS
) -> list[Member]:
    """Every member still under ``members/`` whose pulse, or whose session's, has stopped.

    What a sweep would move, without moving it — which is what lets a console
    say what it is about to do before doing it. The departed are not here:
    they have already been moved, and a sweep that reported them would report
    the same rows on every run.
    """
    return [
        member
        for member in pulsed_members(root, now or datetime.now(UTC), window=window)
        if not member.get("running")
    ]


def swept(
    root: Path, now: datetime | None = None, window: float = STALE_AFTER_SECONDS
) -> list[Member]:
    """Retire what the read already derives, and delete what nobody reads.

    A member whose pulse stopped is moved to the departed, so a reader that
    lists the directory agrees with one that stats the file; and a departed
    stub older than the retention window is deleted, which is what keeps the
    store the size of the population rather than of its history.

    The window is the caller's, and the caller is a session's own tool server:
    a sweep reading a silence for longer than the listings beside it do would
    leave rows every reader calls gone and nothing ever moves.

    A claim needs no sweeping: it stands or it does not, and the filesystem is
    what says which.
    """
    moment_now = now or datetime.now(UTC)
    retired = lapsed(root, moment_now, window)
    for member in retired:
        depart(root, member_actor(member), error=text(member.get("error")))
    since = moment_now - timedelta(seconds=DEPARTED_SECONDS)
    for path in listed(root / DEPARTED_DIR):
        found = read_member(path, running=False)
        left = spoken_at(text(found.get("left_at"))) if found is not None else None
        match found:
            case None:
                discarded(path)
            case dict() if left is not None and left < since:
                discarded(path)
                if not answered(root, member_actor(found)):
                    discarded(pulse_path(root, member_actor(found)))
    return retired
