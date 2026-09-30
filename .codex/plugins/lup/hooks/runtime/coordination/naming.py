"""Naming a session for its work: the half every runtime's naming hook shares.

A session is called after its worktree until something renames it, and every
session opened in one checkout shares that worktree — so a roster of `dev`,
`dev-2` and `dev-3` tells a person nothing about which is doing what, and
neither does the runtime chrome that agrees with it. A person names a session
by its work, which the first prompt that says what the work is already
states. Each runtime's naming hook asks a model for that name through its own
CLI; everything around the asking is here.

**Nothing waits on the model.** The prompt that is due a name starts one ask in
a process of its own and goes on at once; the ask renames the roster when it
answers, and the runtime's own name for the session follows — at the next
prompt where only the hook's answer can set it, straight away where the
runtime takes a name from another process.

**Naming is due while a session answers to nothing but the name it joined
with.** One name in its history is a default nobody chose; a second is a
rename, by a peer's tool, a person, or an earlier ask's answer, and no answer
here overrides one. A prompt that says nothing about the work — a greeting, a
"continue" — gets no name, and the next prompt is asked again, up to the
declared number of attempts.

**A name is settled against every live session's**, under the roster lock the
typed rename takes, and numbered where another session answers to it: the
model proposed it, nobody chose it, so it is a default in all but origin.

**The runtime follows the roster, and the roster follows the runtime.**
Whatever last renamed the roster — an ask's answer or a peer's
`coordination_rename` — is carried to the runtime's own name for the session
once, and never again until the roster's name moves. A title somebody set
where the session is looked at — a ``/rename``, a resumed conversation's own
title — wins: the roster takes it up in the shape a session name has, and the
runtime keeps showing it exactly as it was set, since the shape is the
roster's and nobody asked the runtime to change. A runtime that reports no
title is taken up only on a resume, from the name its own record keeps for
the session it reopened.

Only the root session's own prompts count. A child session that inherited its
launcher's environment carries the same member id, so a prompt is taken as
this member's only where nothing marks it as a child, its working directory is
the member's worktree, and — once the member records which native session it
is — its session id is that one: the checks the arrival binder makes.

Shipped into each plugin's ``hooks/runtime/coordination/`` beside the store it
reads, so it resolves on the bare interpreter a runtime spawns: the standard
library and nothing else. **Every failure is silence**; a name is not
something a prompt waits on or is stopped by.
"""

import json
import os
import signal

# lup: ignore[subprocess] — `sh` is third-party and this package is shipped
# into a bare interpreter that has no virtual environment to resolve it from
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, TypedDict

from .store import (
    MEMBER_KIND,
    TITLES_DIR,
    Member,
    Wake,
    conversation_of,
    current_name,
    loaded,
    member_path,
    naming_settled,
    names_of,
    names_taken,
    published,
    read_member,
    renamed,
    revised,
    session_actor,
    spoken_at,
    stamped,
    text,
    unique_cli_name,
)


class Naming(TypedDict):
    """How this project names a session, as its declaration compiled it.

    Written beside the hook by the generator, in the runtime's own spellings:
    ``arguments`` are the words its CLI takes for the declared tier's model,
    the declared effort, and an ask with no native tool at all — compiled
    there from the adapter's own vocabulary, because a bare script cannot
    import it.
    """

    instruction: str
    attempts: int
    deadline_seconds: float
    longest: int
    arguments: list[str]


class Titling(TypedDict):
    """What one session's naming hook remembers between prompts.

    ``attempts`` counts the times a model was asked, answered or not;
    ``asked`` is when an ask still under way started, blank where none is,
    which keeps a slow ask from being started twice; ``pushed`` is the roster
    name the runtime was last given; ``shown`` is the title its chrome was
    last known to show, given or seen, which is what tells a title somebody
    set there from one this hook put there; ``resumed`` is the native session
    a resume reopened, whose own name the roster takes up at the next prompt,
    blank where none is waiting.
    """

    attempts: int
    asked: str
    pushed: str
    shown: str
    resumed: str


class Arrival(TypedDict, total=False):
    """What a runtime hands its prompt hook on stdin, as far as naming reads it.

    Partial because the runtime owns the record, and both document more.
    """

    session_id: str
    cwd: str
    prompt: str
    agent_id: str
    agent_type: str


class Answer(TypedDict, total=False):
    """The structured answer the naming model is held to."""

    name: str | None


type Move = Literal["none", "ask", "adopt", "push"]
"""What one prompt calls for from the runtime's half."""


class Step(TypedDict):
    """One prompt's call on the runtime's half, and what it acts on.

    ``ask`` starts an ask for the prompt; ``adopt`` reads the name the
    reopened native session in ``value`` already has; ``push`` gives the
    runtime ``value`` as the session's name; ``none`` does nothing.
    """

    move: Move
    value: str


NOTHING = Step(move="none", value="")
"""The step a prompt takes where naming has nothing to do."""


def answer_schema() -> str:
    """The JSON Schema the model's answer must meet: a name, or null for none.

    Handed to each runtime's CLI as the shape its structured output is held
    to, so an answer arrives as JSON :func:`named` reads rather than prose to
    be picked apart.
    """
    return json.dumps(
        {
            "type": "object",
            "properties": {"name": {"type": ["string", "null"]}},
            "required": ["name"],
            "additionalProperties": False,
        }
    )


def settings(path: Path) -> Naming | None:
    """The compiled naming declaration beside the hook, or nothing unreadable.

    Checked field by field rather than trusted: the file is this generator's,
    but a hook that took a malformed one on faith would fail somewhere less
    obvious than here, where failing is silence.
    """
    found = loaded(path, Naming)
    if found is None:
        return None
    attempts, deadline, longest, arguments = (
        found.get("attempts"),
        found.get("deadline_seconds"),
        found.get("longest"),
        found.get("arguments"),
    )
    if not (
        isinstance(attempts, int)
        and isinstance(deadline, int | float)
        and isinstance(longest, int)
        and isinstance(arguments, list)
        and all(isinstance(word, str) for word in arguments)
    ):
        return None
    return Naming(
        instruction=text(found.get("instruction")),
        attempts=attempts,
        deadline_seconds=float(deadline),
        longest=longest,
        arguments=arguments,
    )


def request_for(prompt: str) -> str:
    """The prompt as the naming model reads it: quoted, as the thing to name.

    Handed over bare, a prompt is a request the model answers — a runtime
    whose naming model has tools was measured exploring a scratch directory
    to explain what it had been asked to name, until its deadline ran out.
    Between markers the declared instruction can point at, it is data.
    """
    return f"<request>\n{prompt}\n</request>"


def ran(arguments: list[str], given: str, deadline: float, cwd: str = "") -> str | None:
    """What *arguments* print when handed *given*, or nothing where they overran.

    Started in a session of its own and killed whole at the deadline. A CLI
    installed as a wrapper around its real binary leaves the binary running
    when only the wrapper is killed: measured with Codex's npm wrapper, whose
    binary went on past the deadline and wrote its answer into a directory
    already removed.
    """
    try:
        process = subprocess.Popen(
            arguments,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            cwd=cwd or None,
            start_new_session=True,
        )
    except OSError:
        return None
    try:
        printed, _ = process.communicate(given, timeout=deadline)
    except subprocess.TimeoutExpired:
        stopped(process)
        return None
    return printed


def stopped(process: subprocess.Popen[str]) -> None:
    """Kill *process* and everything in its session, and reap it."""
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        process.wait()
        return
    process.communicate()


def detached(host: Path, arguments: list[str], given: str) -> None:
    """Run the host half at *host* again in a session of its own, handed *given*.

    Its own session, so nothing that ends the hook's process group ends it,
    and nothing of the hook's output is held open by it: the runtime reads
    the hook as finished the moment the hook exits, which is what keeps a
    prompt from waiting on the model.
    """
    worker = subprocess.Popen(
        [sys.executable, "-s", str(host), *arguments],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        text=True,
    )
    if worker.stdin is not None:
        worker.stdin.write(given)
        worker.stdin.close()


def compiled_for(host: Path) -> Naming | None:
    """The compiled declaration for the host half at *host*, or nothing unreadable.

    Under the host half's own name, beside the hooks manifest that registers
    it rather than beside the host half itself: everything under the runtime
    directory is source a policy evaluator's accepted snapshot is hashed over,
    and a data file there is refused as code nothing hashed.
    """
    return settings(host.parent.parent / host.with_suffix(".json").name)


def titling_path(root: Path, member_id: str) -> Path:
    """Where one member's naming hook keeps what it remembers."""
    return root / TITLES_DIR / f"{conversation_of(session_actor(member_id))}.json"


def recorded(root: Path, member_id: str, titling: Titling) -> Titling:
    """Remember *titling* for this member's next prompt, and hand it back."""
    published(titling_path(root, member_id), titling)
    return titling


def recalled(root: Path, member_id: str) -> Titling | None:
    """What this member's naming hook remembers, or nothing where it never looked."""
    found = loaded(titling_path(root, member_id), Titling)
    if found is None:
        return None
    attempts = found.get("attempts")
    return Titling(
        attempts=attempts if isinstance(attempts, int) else 0,
        asked=text(found.get("asked")),
        pushed=text(found.get("pushed")),
        shown=text(found.get("shown")),
        resumed=text(found.get("resumed")),
    )


def looked(root: Path, member_id: str, member: Member) -> Titling:
    """What this member's naming hook remembers, begun on a first look.

    A first look takes the roster's name as the one the runtime was given and
    shows: a launched session's runtime was handed it at launch, and one
    nobody launched has nothing better to show than its own default. A resume
    recorded before the member joined is kept, and begun around.
    """
    found = recalled(root, member_id)
    if found is not None and found["pushed"]:
        return found
    name = current_name(member)
    return recorded(
        root,
        member_id,
        Titling(
            attempts=found["attempts"] if found else 0,
            asked=found["asked"] if found else "",
            pushed=name,
            shown=name,
            resumed=found["resumed"] if found else "",
        ),
    )


def resuming(root: Path, member_id: str, session: str) -> Titling:
    """Record that *session* was reopened, so the next prompt takes up its name.

    Written at the moment the runtime says so, which can come before the
    session's tool server has put it on the roster: nothing here needs the
    member, and the first look begins the rest around it.
    """
    found = recalled(root, member_id)
    return recorded(
        root,
        member_id,
        Titling(
            attempts=found["attempts"] if found else 0,
            asked=found["asked"] if found else "",
            pushed=found["pushed"] if found else "",
            shown=found["shown"] if found else "",
            resumed=session,
        ),
    )


def owning(root: Path, member_id: str, arrival: Arrival) -> Member | None:
    """This member, where the prompt is its own root session's, and nothing else.

    A missing member is nothing to name: a session that has not joined yet is
    named at a later prompt, once its tool server has put it on the roster.
    """
    if not member_id or arrival.get("agent_id") or arrival.get("agent_type"):
        return None
    member = read_member(member_path(root, session_actor(member_id)), running=True)
    if member is None or member.get("kind") != MEMBER_KIND:
        return None
    cwd, worktree = text(arrival.get("cwd")), text(member.get("worktree"))
    if not cwd or not worktree or Path(cwd).resolve() != Path(worktree).resolve():
        return None
    bound = text(member.get("wake", Wake()).get("session"))
    if bound and bound != text(arrival.get("session_id")):
        return None
    return member


def said(arrival: Arrival) -> str:
    """The prompt an arrival carries, blank where it carries none."""
    match arrival:
        case {"prompt": str(prompt)}:
            return prompt
        case _:
            return ""


def session_of(arrival: Arrival) -> str:
    """The native session an arrival comes from, blank where it names none."""
    match arrival:
        case {"session_id": str(session)}:
            return session
        case _:
            return ""


def defaulted(member: Member, title: str) -> bool:
    """Whether *title* is a name nobody chose: its worktree's, or that one numbered.

    What a launch names a session in the runtime's chrome, and what a resumed
    conversation keeps where nothing renamed it — so taking it up would be
    reading a default as a choice, and would leave the session unnamed.
    """
    match member:
        case {"worktree": str(worktree)} if worktree:
            base = Path(worktree).name
            numbered = title.removeprefix(f"{base}-")
            return title == base or (numbered != title and numbered.isdigit())
        case _:
            return False


def roster_form(title: str, longest: int) -> str:
    """*title* in the shape a session name takes on the roster, blank where none reads.

    Lowercased, each run of characters other than ASCII letters and digits one
    hyphen, and cut at a word to fit — the way a spawn's name is read into its
    shape. A title somebody set in the chrome is theirs to spell as they like;
    the roster's name is what a person types to reach the session, and that
    has a shape.
    """
    words = "".join(
        character if character.isascii() and character.isalnum() else " "
        for character in title.lower()
    ).split()
    name = ""
    for word in words:
        longer = f"{name}-{word}" if name else word
        if len(longer) > longest:
            break
        name = longer
    # A first word longer than the limit is cut: a name the roster refuses is
    # no name at all.
    return name or next((word[:longest] for word in words), "")


def due(
    member: Member,
    titling: Titling,
    naming: Naming,
    shown: str | None,
    now: datetime | None = None,
) -> bool:
    """Whether this prompt should ask a model what the session is called.

    *shown* is what the runtime reports its chrome showing, or ``None`` where
    it reports nothing. An ask still under way is not started again.
    """
    if len(names_of(member)) != 1 or titling["attempts"] >= naming["attempts"]:
        return False
    if shown and shown != current_name(member) and not defaulted(member, shown):
        return False
    return not under_way(titling, naming, now)


def under_way(titling: Titling, naming: Naming, now: datetime | None = None) -> bool:
    """Whether an ask started inside its deadline has yet to conclude.

    The answer it is waiting for settles the name either way, so a second one
    would only race it; one that died without concluding stops counting once
    the deadline it was given has passed.
    """
    started = spoken_at(titling["asked"]) if titling["asked"] else None
    return (
        started is not None
        and ((now or datetime.now(UTC)) - started).total_seconds()
        < naming["deadline_seconds"]
    )


def adoptable(member: Member, titling: Titling, shown: str) -> str:
    """The title the runtime's chrome shows that the roster should take up, else blank.

    One this hook did not put there or see before, and that is not a default:
    a resumed conversation's own title, a ``/rename``, a rename from another
    surface. Somebody chose it where the session is looked at, so the roster
    follows it the way the chrome follows the roster.
    """
    return (
        shown
        if shown and shown != titling["shown"] and not defaulted(member, shown)
        else ""
    )


def asking(root: Path, member_id: str, titling: Titling) -> Titling:
    """Record that one more ask has started, before it starts."""
    return recorded(
        root,
        member_id,
        Titling(
            attempts=titling["attempts"] + 1,
            asked=stamped(),
            pushed=titling["pushed"],
            shown=titling["shown"],
            resumed=titling["resumed"],
        ),
    )


def shown_as(root: Path, member_id: str, pushed: str, shown: str) -> Titling:
    """Record what the runtime was given and what its chrome shows, where either moved.

    Read afresh rather than from the caller's copy: an ask finishing in its
    own process and a prompt arriving at the hook write the same file, and
    each writes only what it knows. A blank field leaves what was recorded.
    """
    found = recalled(root, member_id)
    return recorded(
        root,
        member_id,
        Titling(
            attempts=found["attempts"] if found else 0,
            asked=found["asked"] if found else "",
            pushed=pushed or (found["pushed"] if found else ""),
            shown=shown or (found["shown"] if found else ""),
            resumed=found["resumed"] if found else "",
        ),
    )


def concluded(root: Path, member_id: str) -> Titling:
    """Record that no ask is under way, and that no resume is waiting any more.

    Read afresh, for the reason :func:`shown_as` is: what the ask found out
    was recorded by whoever learned it.
    """
    found = recalled(root, member_id)
    return recorded(
        root,
        member_id,
        Titling(
            attempts=found["attempts"] if found else 0,
            asked="",
            pushed=found["pushed"] if found else "",
            shown=found["shown"] if found else "",
            resumed="",
        ),
    )


def fitting(name: str, longest: int) -> bool:
    """Whether *name* has the shape a session name takes.

    Lowercase letters and digits in words joined by single hyphens, as a
    worktree is named, and no longer than the declaration allows: it is what a
    person types to reach the session, and what its runtime shows.
    """
    return (
        0 < len(name) <= longest
        and all(
            character.isascii()
            and (character.islower() or character.isdigit() or character == "-")
            for character in name
        )
        and not name.startswith("-")
        and not name.endswith("-")
        and "--" not in name
    )


def named(answer: Answer | None, longest: int) -> str:
    """The name a model's structured answer carries, blank for none or a malformed one."""
    name = answer.get("name") if isinstance(answer, dict) else None
    return name if isinstance(name, str) and fitting(name, longest) else ""


def answered(reply: str, longest: int) -> str:
    """The name a model's answer carries where it arrives as JSON text."""
    try:
        answer: Answer = json.loads(reply)
    except ValueError:
        return ""
    return named(answer, longest)


def settled(
    root: Path, member_id: str, wanted: str, over_a_rename: bool = False
) -> str:
    """Rename this member to *wanted*, numbered past any live session's name.

    Decided under the roster lock and against the member as it is then. A
    name a model proposed goes only on a member still answering to its
    default, so one renamed since this hook read it keeps that rename and
    nothing is written; a title somebody set in the chrome is taken up
    *over_a_rename*, since it is the newer choice. Hands back the name taken,
    or blank where none was.
    """
    taken = ""

    def rename(member: Member) -> Member:
        nonlocal taken
        if not over_a_rename and len(names_of(member)) != 1:
            return member
        taken = unique_cli_name(wanted, names_taken(root, member_id))
        return member if taken == current_name(member) else renamed(member, taken)

    try:
        with naming_settled(root):
            written = revised(root, session_actor(member_id), rename)
    except OSError:
        return ""
    return taken if written is not None else ""


def adopted(root: Path, member_id: str, title: str, longest: int) -> str:
    """Take up *title*, set where the session is looked at, as the roster's name.

    In the roster's shape, and numbered where a live session answers to it;
    the runtime goes on showing *title* exactly as it was set, so nothing is
    handed back to it — not even a number the roster had to add. Both are
    recorded, so neither is taken up or given again. Blank where the roster
    could not take it up, which leaves it unrecorded for the next prompt; a
    title with nothing in it the roster could spell is recorded as seen.
    """
    wanted = roster_form(title, longest)
    name = settled(root, member_id, wanted, over_a_rename=True) if wanted else ""
    if name or not wanted:
        shown_as(root, member_id, name, title)
    return name


def reopened(root: Path, member_id: str, kept: str, longest: int) -> bool:
    """Take up *kept*, the name a reopened session already has, saying whether it did.

    Not where it kept none, or only a default nobody chose: that session is
    still to be named, and is asked for a name the way a new one is.
    """
    member = read_member(member_path(root, session_actor(member_id)), running=True)
    if member is None or not kept or defaulted(member, kept):
        return False
    adopted(root, member_id, kept, longest)
    return True


def pending(member: Member, titling: Titling) -> str:
    """The roster's name where the runtime has not been given it yet, else blank."""
    name = current_name(member)
    return name if name != titling["pushed"] else ""


def prompted(
    root: Path, member_id: str, arrival: Arrival, naming: Naming, shown: str | None
) -> Step:
    """What this prompt calls for, recorded before the runtime's half acts on it.

    *shown* is the title the runtime reports its chrome showing, ``None`` for
    one that reports none. In order: a title somebody set there is taken up,
    and wins over anything an ask proposed; a resume waiting has its
    session's own name read; a prompt due a name starts an ask; and a roster
    name the runtime has not been given yet is handed to it.
    """
    member = owning(root, member_id, arrival)
    if member is None:
        return NOTHING
    titling = looked(root, member_id, member)
    title = adoptable(member, titling, shown) if shown is not None else ""
    if title:
        adopted(root, member_id, title, naming["longest"])
        return NOTHING
    if titling["resumed"] and titling["attempts"] < naming["attempts"]:
        if under_way(titling, naming):
            return NOTHING
        asking(root, member_id, titling)
        return Step(move="adopt", value=titling["resumed"])
    if said(arrival).strip() and due(member, titling, naming, shown):
        asking(root, member_id, titling)
        return Step(move="ask", value="")
    name = pending(member, titling)
    if not name:
        return NOTHING
    shown_as(root, member_id, name, name)
    return Step(move="push", value=name)
