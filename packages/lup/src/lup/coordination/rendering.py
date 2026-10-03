"""The person's tasks, written out because they read where they cannot query.

`TODO_USER.md` is not a document of its own: it is the tasks whose holder is
the person, rendered, because they read on a machine that cannot ask the log.
What a reader of it needs — that each entry says what *class* of thing it is
waiting for, so a reader knows whether a row is a decision to make or a
command to paste — is a field here rather than a convention somebody keeps up.

**Ordered by what it needs, not by urgency.** Nobody writes "most urgent
first" at the top of a derived document and nobody keeps it true. Grouping by
``needs`` gives a reader the ordering they actually wanted: every command
together and every judgement together, so one sitting clears one kind.

**A rendering is a view and is never edited.** A document written once goes
stale as the register behind it moves on. This is regenerated, so the only way
to change a row is to change the task.
"""

from pydantic import BaseModel

from lup.coordination.tasks import Needs, Task
from lup.ledger.journal import LedgerStore

USER_HOLDER = "user"
"""Who these tasks belong to, in the spelling the roster already reaches.

The person is a member addressed by the verbs that address an agent, so their
task list is the ordinary query rather than a second concept.
"""


class Group(BaseModel, frozen=True):
    """One heading of the rendering: what it waits for, and what to call it."""

    needs: Needs
    heading: str


# lup: ignore[library-default] — the order a reader works through them, and what
# each is called: both properties of what that kind costs to clear
GROUPS = [
    Group(needs="command", heading="Commands to run"),
    Group(needs="account", heading="Accounts to create"),
    Group(needs="identity", heading="Identities to supply"),
    Group(needs="payment", heading="Payments to make"),
    Group(needs="judgement", heading="Judgements to make"),
    Group(needs="review", heading="Reviews owed"),
    Group(needs="", heading="Everything else"),
]
"""What to put first: rows that cost a minute before rows that cost thought.

A command to paste and an account to create are mechanical, and clearing them
often unblocks whoever was waiting. A judgement is the expensive kind and goes
last, where a reader arrives having already cleared the noise.
"""


def position_of(needs: Needs) -> int:
    """Where a kind sorts, with anything unlisted going last."""
    return next(
        (index for index, group in enumerate(GROUPS) if group.needs == needs),
        len(GROUPS),
    )


def task_line(task: Task) -> str:
    """One task as a person reads it: what it is, and where to look it up."""
    body = f" — {task.text}" if task.text else ""
    return f"- [ ] {task.title}{body}  `{task.id}`"


def user_tasks(store: LedgerStore, holder: str = USER_HOLDER) -> list[Task]:
    """Every unfinished task whose holder is the person, in the order to work.

    Unfinished only, because a rendering of a to-do list is a list of what is
    left. What was done stays in the log for anybody asking what happened.
    """
    held = [
        task for task in store.read(Task) if task.holder == holder and not task.done
    ]
    return sorted(held, key=lambda task: (position_of(task.needs), task.at))


def render(store: LedgerStore, holder: str = USER_HOLDER) -> str:
    """The person's outstanding tasks as one document, grouped by what they cost.

    Empty of rows is still a document, and says so: a file that vanished when
    there was nothing in it would leave a reader unsure whether the list was
    empty or the rendering had broken.
    """
    tasks = user_tasks(store, holder)
    lines = [
        "# For you",
        "",
        "Everything here is blocked on something only you can supply. The"
        " collectors stop at these boundaries rather than acting through them.",
        "",
        "Generated from this repository's ledger — edit the task, not this file.",
        "",
    ]
    if not tasks:
        return "\n".join([*lines, "Nothing is waiting on you.", ""])
    for group in GROUPS:
        rows = [task for task in tasks if task.needs == group.needs]
        if not rows:
            continue
        lines.extend([f"## {group.heading}", ""])
        lines.extend(task_line(task) for task in rows)
        lines.append("")
    return "\n".join(lines)
