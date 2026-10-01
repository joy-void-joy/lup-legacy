"""One handoff, written out for whoever is going to read it.

Three readers, one record. A peer working in this repository can resolve a
node id and does not want it spelled out; an agent with no access to the
repository can resolve nothing, so what the peer would look up has to arrive
inlined; a person wants a file they can open on a machine that cannot query
the log. Written by hand as three separate artifacts, they drift apart: one
goes stale while another names paths that have since moved.

**They differ in rendering and not in substance.** Every one of them carries
the established results with their sources and grades, the open questions,
what not to redo, and who wrote it when. A variant that dropped a section
would be a variant whose reader silently got less, and the reason the inlining
one exists at all is that it proves the record stands alone: if the detached
brief is enough to work from, nothing important was living in the sender's
head.

**Each audience answers for itself.** The base names the rendering and the
variants answer it, rather than a table keyed by an audience name — a table
goes stale the moment a fourth reader appears, and the fourth reader is the
one nobody remembers to add.
"""

from typing import Literal

from pydantic import BaseModel

from lup.coordination.handoffs import Established, Handoff
from lup.coordination.tasks import Task


class Audience(BaseModel, frozen=True):
    """Who a handoff is being written for, and how much they can look up.

    A model rather than a string because the difference between these readers
    is behaviour and not a label: what changes is whether an id is enough, and
    that is a question each of them answers about itself.
    """

    kind: str

    def resolves_ids(self) -> bool:
        """Whether this reader can turn a node id into what it names.

        The one fact the renderings actually differ on. A reader who can look
        an id up is better served by the id — it stays true as the task moves,
        where a copy goes stale — and a reader who cannot is served by nothing
        else at all.
        """
        return True

    def task_lines(self, tasks: list[Task]) -> list[str]:
        """The transferred work, spelled for whoever is receiving it."""
        return [f"- {task.title}  `{task.id}`" for task in tasks]

    def closing(self) -> list[str]:
        """Anything this reader needs after the substance, or nothing."""
        return []


class PeerBrief(Audience, frozen=True):
    """For a session working in this repository, which can resolve every id."""

    kind: Literal["peer"] = "peer"

    def closing(self) -> list[str]:
        return [
            "Resolve any id above with `dev ledger show <id>`, and take the"
            " work with `dev ledger mine --holder <you>`.",
            "",
        ]


class DetachedBrief(Audience, frozen=True):
    """For an agent with no access to this repository, so nothing is a pointer.

    Everything a peer would look up is written out. The id stays on the row
    anyway, because the reply comes back to somebody who *can* resolve it and
    a brief that stripped the ids would make that reply unattributable.
    """

    kind: Literal["detached"] = "detached"

    def resolves_ids(self) -> bool:
        return False

    def task_lines(self, tasks: list[Task]) -> list[str]:
        return [
            line
            for task in tasks
            for line in (
                [f"- {task.title}  `{task.id}`"]
                + ([f"  {task.text}"] if task.text else [])
            )
        ]

    def closing(self) -> list[str]:
        return [
            "You are working without the repository. Everything above is"
            " stated in full rather than referenced; if something reads as a"
            " pointer you cannot follow, say so rather than guessing at it.",
            "",
        ]


class PersonBrief(Audience, frozen=True):
    """For the person, who reads a file rather than querying anything."""

    kind: Literal["person"] = "person"

    def task_lines(self, tasks: list[Task]) -> list[str]:
        return [
            f"- [ ] {task.title}" + (f" — {task.text}" if task.text else "")
            for task in tasks
        ]


def result_line(entry: Established, inline: bool) -> str:
    """One established result, with as much of its provenance as fits the reader.

    The grade rides on every rendering. It is the field a careful writer fills
    unprompted, explicit negatives included, and the one a receiver uses to
    decide whether checking is worth their time.
    """
    source = f" (source: {entry.source})" if inline else f" — {entry.source}"
    return f"- [{entry.grade}] {entry.statement}{source}"


def render_brief(
    handoff: Handoff,
    tasks: list[Task],
    audience: Audience,
    watched: list[str] | None = None,
) -> str:
    """One handoff as its reader should meet it, in the order they need it.

    What moved comes first, because it is the one thing the receiver must
    read before anything else: a premise that fell since the handoff makes
    the established results below it suspect. Then established results before
    open questions before dead ends, which is the order somebody picking work
    up asks — what is true, what is not settled, what has already been tried.
    The provenance stamp goes last, where it is available and not in the way.

    ``watched`` is rendered by the caller, one line per watched node with its
    standing and whether it moved, because reading standing takes the store
    and this takes none.
    """
    lines = [f"# {handoff.title}", ""]
    if watched:
        lines.extend(["## Watched since this handoff", ""])
        lines.extend(watched)
        lines.append("")
    if handoff.text:
        lines.extend([handoff.text, ""])
    if handoff.to:
        lines.extend([f"Handed to **{handoff.to}**.", ""])
    if tasks:
        lines.extend(["## Work transferred", ""])
        lines.extend(audience.task_lines(tasks))
        lines.append("")
    if handoff.established:
        lines.extend(["## Established", ""])
        lines.extend(
            result_line(entry, not audience.resolves_ids())
            for entry in handoff.established
        )
        lines.append("")
    lines.extend(["## Still open", ""])
    lines.extend(f"- {question}" for question in handoff.open_questions)
    lines.append("")
    if handoff.not_again:
        lines.extend(["## Tried already, do not repeat", ""])
        lines.extend(f"- {dead_end}" for dead_end in handoff.not_again)
        lines.append("")
    lines.extend(audience.closing())
    lines.extend(
        [
            f"Recorded by {handoff.author.label()} at"
            f" {handoff.at.isoformat()} as `{handoff.id}`.",
            "",
        ]
    )
    return "\n".join(lines)
