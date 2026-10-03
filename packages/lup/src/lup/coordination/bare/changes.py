# lup: ignore[constant-declaration]
# The envelope's names are the wire spelling both runtimes document for their
# prompt event — a fact about those runtimes rather than a taste. Everything
# the store itself is spelled with comes from the fold beside this.
"""What changed on this repository's roster since one session last looked.

A session learns who else is working in this clone only when it asks — a
`coordination_peers` call, or a native listing the policy attaches the roster
to — and the moment it most needs to know is the one it has no reason to ask
at: a prompt has just arrived, and whoever started since the last one is
invisible. This is the fold that answers at that moment, run by a hook the
runtime fires when a prompt is submitted, and it says only what is different
from the last time this session looked.

Shipped into each plugin's ``hooks/runtime/coordination/``, so everything
here resolves on a bare interpreter: standard library, and the fold beside it
in the same package. That is what lets it reach a person's own session, which
has the plugin and no process of lup's to close over.

**What it adds to the fold is the difference.** :mod:`.store` answers who is
here, what each holds, and what everyone is called; the whole of this module
is what a *prompt* does with that — keep one look per session, compare it
against the next, and say the part that moved.

**The first look is a baseline, not a replay** — the convention the watcher
keeps. A session attaching to a repository already at work is told where the
roster is, once, and everything after that is a difference. What is pushed is
what changes the reader's next decision: a path somebody holds under this
checkout, contested ones first, then who arrived, who left, and who now says
they are on something else. A quiet roster costs no context at all.

**It fails open.** Every failure is silence: a roster that cannot be read
costs the session one look and never the prompt it was attached to.

The output is the shape both runtimes document for the event that fires on a
submitted prompt — ``hookSpecificOutput`` carrying ``hookEventName`` and
``additionalContext``, read from stdout on exit 0. Claude Code documents it
under "UserPromptSubmit" at https://code.claude.com/docs/en/hooks, and Codex
under the same event name at https://learn.chatgpt.com/docs/hooks, where
https://developers.openai.com/codex/hooks redirects. The event's name is the
one word here a runtime owns, so it arrives as an argument from the adapter
that spells it.
"""

import json
import sys
from pathlib import Path
from typing import TypedDict

from .mail import Notice, notices
from .runtime import stdin_runtime
from .store import (
    LOOKS_DIR,
    MEMBER_KIND,
    conversation_of,
    Conversation,
    Held,
    Member,
    beat,
    called,
    held,
    own_member,
    parent_of,
    present,
    published,
    session_actor,
    revised,
    text,
)

MOVED_LINE = (
    "This conversation was rewound or cleared, so what this session's roster "
    "row said it was doing is cleared; `coordination_describe` once you know."
)
"""The one line a prompt gets when its conversation is not the one it recorded."""


class Seen(TypedDict):
    """One peer as this session last saw it: the facts a change is read against."""

    name: str
    worktree: str
    doing: str
    holding: list[str]


class TranscriptEntry(TypedDict, total=False):
    """One line of a runtime's transcript, as far as counting roots needs it."""

    type: str
    uuid: str
    parentUuid: str | None


class Look(TypedDict):
    """Everything one look at the roster records, keyed so the next can diff it.

    ``contested`` is keyed by the claim rather than by the peer, because a
    contested path is one fact about two sessions and a reader is told it
    once, with every name on it. ``standing`` is keyed by the notice's id
    rather than by its text, so a fact restated in other words is a second
    fact and a reader is told about it.

    Which conversation this was taken from is deliberately not here. It is on
    this session's own member file, where the description it qualifies is, so
    one write says both that the row was rewound and what it is now.
    """

    peers: dict[str, Seen]
    contested: dict[str, list[str]]
    # lup: ignore[dict-str-payload] — keyed by the id the store mints for one
    # notice, whose value is that notice's own words: two fields of a record
    # this half has no type of lup's to name
    standing: dict[str, str]


class Folded(TypedDict):
    """One look, and the roster fold it was read from.

    The fold rides beside the look because a departure needs what the look
    deliberately leaves out: only running peers are recorded, so the file does
    not grow with every session that ever stopped, and a departed peer's
    summary is read from the fold at the moment it is reported.
    """

    look: Look
    members: dict[str, Member]


class Prompt(TypedDict, total=False):
    """What the runtime hands the hook on stdin, as far as this reads it.

    Partial because the runtime owns this record: both document more fields
    than these, and a field added there must not make the payload unreadable
    here.
    """

    session_id: str
    cwd: str
    transcript_path: str


class Pushed(TypedDict):
    """The runtime's own envelope fields, in the runtime's own spelling."""

    hookEventName: str
    additionalContext: str


class HookOutput(TypedDict):
    """What this hook prints. ``additionalContext`` nested here is what is read."""

    hookSpecificOutput: Pushed


def conversation(transcript: str) -> Conversation:
    """The conversation this prompt belongs to: which transcript, and how many roots.

    A turn of the conversation is a ``user`` or ``assistant`` entry; the
    attachments, snapshots and bookkeeping in the same file parent nothing a
    turn descends from. A turn with no parent is a root, and a conversation
    has exactly one until the runtime rewinds it: the rewind appends a new
    root to the same file, which is the one sign it leaves — measured on
    Claude Code, whose documentation is silent on it. Codex has no rewind, and
    its transcript yields no such root, so the count stays at zero there.

    Read at prompt time, so a root the runtime writes only after this hook has
    run is noticed at the next prompt. No transcript reads as no conversation,
    which the member file then agrees with.
    """
    if not transcript:
        return Conversation(transcript="", roots=0)
    roots = sum(
        1
        for entry in turns(Path(transcript))
        if text(entry.get("type")) in ("user", "assistant")
        and text(entry.get("uuid"))
        and entry.get("parentUuid") is None
    )
    return Conversation(transcript=transcript, roots=roots)


def turns(transcript: Path) -> list[TranscriptEntry]:
    """Every record a runtime's transcript holds, skipping what will not read.

    One JSON object per line, written by the runtime while this reads, so the
    last line may be half a record: a line that will not parse is passed over
    rather than raised on, and the next prompt reads it whole.
    """
    try:
        lines = transcript.read_text("utf-8").splitlines()
    except OSError:
        return []
    return [entry for line in lines for entry in [entry_of(line)] if entry is not None]


def entry_of(line: str) -> TranscriptEntry | None:
    """One transcript line, or nothing where it is not a JSON object."""
    try:
        entry: TranscriptEntry = json.loads(line)
    except ValueError:
        return None
    return entry if isinstance(entry, dict) else None


def roots_of(spoken: Conversation) -> int:
    """How many roots one recorded conversation carries, none where it says nothing."""
    counted = spoken.get("roots")
    return counted if isinstance(counted, int) else 0


def moved(before: Conversation, now: Conversation) -> bool:
    """Whether these two records are two conversations rather than one.

    Compared field by field rather than whole, because either record may have
    been written by a library that carries one more of them: a key this
    reader does not know about must not read as a rewind on every prompt.
    """
    return (text(before.get("transcript")), roots_of(before)) != (
        text(now.get("transcript")),
        roots_of(now),
    )


def within(path: str, root: str) -> bool:
    """Whether one absolute path is the root or lies beneath it."""
    return bool(root) and (path == root or path.startswith(root + "/"))


def concerns(claim: Held, checkout: str) -> bool:
    """Whether a claim names something a session in this checkout could write.

    A touch or lock inside the checkout, or a lock over a prefix the checkout
    itself lies under. Claims are keyed by absolute path, so a session in
    another worktree holding its own copy of a file is not this session's
    business at prompt time — the merge reconciles those, and the roster
    still lists them for whoever asks.
    """
    return within(claim["path"], checkout) or (
        claim["prefix"] and within(checkout, claim["path"])
    )


def looked(root: Path, mine: str, checkout: str) -> Folded:
    """One look at the roster as this session reads it, and the fold behind it.

    The fold is :mod:`.store`'s, so a session the pulse has retired reads as
    finished here exactly as it does to a tool call, its claims stop being
    reported, and a description its own session cleared on a rewind reads as
    unsaid — a peer is told the task that session is on rather than what a
    discarded conversation said.

    A peer is a session. What its subagents hold is folded into its row, so a
    file one of them is writing reads as that session's, and two of one
    session's subagents on one path is that session's business rather than a
    contest another session is told about.
    """
    everyone = present(root, mine=mine)
    members = {
        text(member.get("id")): member
        for member in everyone
        if text(member.get("kind")) == MEMBER_KIND
    }
    sessions = {
        text(member.get("id")): parent_of(member) or text(member.get("id"))
        for member in everyone
        if member.get("running")
    }
    names = called(root)
    claims = [
        claim for claim in held(root, list(sessions)) if concerns(claim, checkout)
    ]

    def name(member_id: str) -> str:
        """What a peer is called, falling back to the id nothing has named."""
        return names.get(member_id) or member_id

    def holders(claim: Held) -> list[str]:
        """Every session on one claim, by id, as the look keys them.

        In the order the claim lists its holders, once each: two of one
        session's subagents on a path are that session once.
        """
        return list(
            dict.fromkeys(
                sessions.get(held_by, held_by)
                for held_by in [text(holder.get("id")) for holder in claim["holders"]]
            )
        )

    def worktree_of(member: Member) -> str:
        """Which checkout a peer is working in, by name, blank where it is nowhere."""
        found = text(member.get("worktree"))
        return Path(found).name if found else ""

    peers = {
        member_id: Seen(
            name=name(member_id),
            worktree=worktree_of(member),
            doing=text(member.get("description")) or text(member.get("task")),
            holding=sorted(
                claim["subject"] for claim in claims if member_id in holders(claim)
            ),
        )
        for member_id, member in members.items()
        if member_id != mine and member.get("running")
    }
    contested = {
        claim["subject"]: [name(holder) for holder in holders(claim)]
        for claim in claims
        if len(holders(claim)) > 1
    }
    standing = {
        text(notice.get("id")): stated(notice)
        for notice in notices(root)
        if text(notice.get("id"))
    }
    return Folded(
        look=Look(peers=peers, contested=contested, standing=standing),
        members=members,
    )


def stated(notice: Notice) -> str:
    """One standing fact as a reader is told it, with whoever posted it."""
    by = text(notice.get("by"))
    return text(notice.get("text")) + (f" ({by})" if by else "")


# lup: ignore[dict-str-payload] — the look's own ``standing`` map, passed as
# the field it is rather than copied into a shape this half has no type for
def standing_lines(standing: dict[str, str]) -> list[str]:
    """Facts standing over this repository, as a prompt carries them.

    The same framing at a baseline as at a difference, so a session that
    arrived after a notice was posted reads what one already here read when it
    was — and neither is left to guess whether the line is news or a state.
    """
    return [f"standing: {said}" for _, said in sorted(standing.items())]


def last_look(cursor: Path) -> Look | None:
    """What this session saw last time, or nothing where it never looked.

    A look that will not parse reads as never having looked, which re-baselines
    rather than replays: the cost is one pointer line, and the alternative —
    reporting every peer as arrived — is the replay the baseline exists to
    avoid.
    """
    try:
        stored: Look = json.loads(cursor.read_text("utf-8"))
        return Look(
            peers=stored["peers"],
            contested=stored["contested"],
            standing=stored["standing"],
        )
    except (OSError, ValueError, KeyError, TypeError):
        return None


def remember(cursor: Path, look: Look) -> None:
    """Record this look atomically, so a crash re-baselines instead of tearing.

    A look that cannot be written leaves the last one standing, so what this
    look told is told once more at the next — a repeat, never a loss.
    """
    published(cursor, look)


def pointer(others: int) -> str:
    """The one line a first prompt gets: how many are here, and where to look."""
    match others:
        case 0:
            return (
                "No other session is working in this repository; "
                "`coordination_peers` lists whoever arrives."
            )
        case 1:
            return (
                "1 other session is working in this repository; "
                "`coordination_peers` lists it."
            )
        case _:
            return (
                f"{others} other sessions are working in this repository; "
                "`coordination_peers` lists them."
            )


def differences(before: Look, now: Look, members: dict[str, Member]) -> list[str]:
    """A line per change between two looks, the ones that stop a write first.

    Standing facts first, then contested paths, then paths a peer holds here,
    then arrivals, departures and redescriptions: the order a session about to
    write needs them in. A subject reported as contested is not repeated as a
    holding beneath it.

    A notice is reported once, when it starts standing and again when it is
    taken down, rather than restated at every prompt. A session in this
    repository reads one prompt after another for hours, and a fact repeated
    at each of them is read at none of them — the member that arrives next
    hour is told at its own first prompt, which is what a notice being state
    rather than mail buys.
    """

    def held_before(member_id: str) -> list[str]:
        seen = before["peers"].get(member_id)
        return seen["holding"] if seen else []

    def summary_of(member_id: str) -> str:
        member = members.get(member_id)
        return text(member.get("summary")) if member else ""

    posted = standing_lines(
        {
            notice_id: said
            for notice_id, said in now["standing"].items()
            if notice_id not in before["standing"]
        }
    )
    lifted = [
        f"no longer standing: {said}"
        for notice_id, said in sorted(before["standing"].items())
        if notice_id not in now["standing"]
    ]
    contested = [
        f"contested {subject} — {', '.join(holders)}"
        for subject, holders in sorted(now["contested"].items())
        if subject not in before["contested"]
    ]
    holding = [
        f"{seen['name']} holding {subject}"
        for member_id, seen in now["peers"].items()
        for subject in seen["holding"]
        if subject not in held_before(member_id) and subject not in now["contested"]
    ]
    arrived = [
        " — ".join(
            part
            for part in (f"{seen['name']} arrived", seen["worktree"], seen["doing"])
            if part
        )
        for member_id, seen in now["peers"].items()
        if member_id not in before["peers"]
    ]
    departed = [
        " — ".join(
            part for part in (f"{seen['name']} left", summary_of(member_id)) if part
        )
        for member_id, seen in before["peers"].items()
        if member_id not in now["peers"]
    ]
    redescribed = [
        f"{seen['name']} now: {seen['doing']}"
        for member_id, seen in now["peers"].items()
        if member_id in before["peers"]
        and seen["doing"] != before["peers"][member_id]["doing"]
    ]
    return [
        *posted,
        *lifted,
        *contested,
        *holding,
        *arrived,
        *departed,
        *redescribed,
    ]


def rewound(root: Path, mine: str, here: Conversation) -> None:
    """Take back what a discarded conversation said, and record the one now.

    One revision under this member's own lock, because the two facts belong
    together: what this session says it is doing, and which conversation said
    it. Written apart, a reader could meet a row describing work from a
    conversation that no longer exists and have nothing to tell it so.

    The description alone is cleared. The task, the worktree, the name and the
    claims are facts about the session rather than about the conversation, and
    a rewind is not a departure.
    """

    def afresh(member: Member) -> Member:
        """This member, saying nothing about itself and belonging to *here*."""
        settled = member.copy()
        settled["description"] = ""
        settled["conversation"] = here
        return settled

    revised(root, session_actor(mine), afresh)


def opened(root: Path, mine: str, here: Conversation) -> None:
    """Record which conversation this session's row now belongs to."""

    def belongs(member: Member) -> Member:
        """This member, keeping what it says and moving which turn said it."""
        settled = member.copy()
        settled["conversation"] = here
        return settled

    revised(root, session_actor(mine), belongs)


def changes(root: Path, mine: str, checkout: Path, transcript: str = "") -> list[str]:
    """What this session is told at this prompt, and nothing where nothing moved.

    The first look writes the baseline and answers with the pointer and
    whatever is standing. Every later look answers with the differences and
    advances the baseline only where something differed, so a quiet roster
    costs no write either.

    A prompt from a conversation that is not the one this session's row
    records — a rewind appended a root to the transcript, or a clear opened
    another — clears what that row said and starts over from a baseline,
    because what the row said and what this session last saw both belong to
    the conversation that is gone.
    """
    if not mine:
        return []
    folded = looked(root, mine, str(checkout))
    if not folded["members"]:
        return []
    beat(root, session_actor(mine))
    cursor = root / LOOKS_DIR / f"{conversation_of(session_actor(mine))}.json"
    before = last_look(cursor)
    now = folded["look"]
    here = conversation(transcript)
    said = folded["members"].get(mine, Member()).get("conversation", Conversation())
    if before is None:
        opened(root, mine, here)
        remember(cursor, now)
        return [*standing_lines(now["standing"]), pointer(len(now["peers"]))]
    if moved(said, here):
        rewound(root, mine, here)
        remember(cursor, now)
        return [MOVED_LINE, pointer(len(now["peers"]))]
    lines = differences(before, now, folded["members"])
    if now != before:
        remember(cursor, now)
    return lines


def envelope(event: str, lines: list[str]) -> HookOutput:
    """The hook output carrying these lines, in the shape both runtimes read."""
    return HookOutput(
        hookSpecificOutput=Pushed(
            hookEventName=event, additionalContext="\n".join(lines)
        )
    )


def main() -> None:
    """Fold, or say nothing at all and let the prompt through.

    The store root, this member's launcher-proven id (blank where nothing
    launched it) and the event name arrive as arguments; the prompt payload on
    stdin supplies the session's own id as the fallback, the checkout the
    prompt was submitted from, and the transcript it belongs to. A runtime
    that inherited the launcher's id from the session's shell folds as the
    member it is, not as that session. Every failure is silence, because a
    prompt is not something a broken roster may stop.
    """
    try:
        root, member, event = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
        runtime = stdin_runtime()
        prompt: Prompt = json.load(sys.stdin)
        lines = changes(
            root,
            own_member(root, member, runtime) or prompt.get("session_id", ""),
            Path(prompt.get("cwd", "")),
            prompt.get("transcript_path", ""),
        )
    except Exception:
        return
    if lines:
        print(json.dumps(envelope(event, lines)))


if __name__ == "__main__":
    main()
