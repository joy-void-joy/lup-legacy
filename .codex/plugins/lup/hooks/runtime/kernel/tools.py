"""Refusal of a whole native call, by the name its runtime spells for it.

The other kernel families judge what a call would *do* — which paths it
writes, which origin it reads, which lines it adds. This one judges the call
itself, for the tools whose whole purpose a project has decided against. The
verdict carries where to go instead rather than only the refusal, because a
deny an agent can only guess its way out of is answered by guessing.
"""

from .decision import KernelDecision
from .rows import RefusedToolRow
from .shell import ESCALATE_RE

# lup: ignore[constant-declaration] — it quotes the marker's own spelling, so
# the words are fixed by what the kernel parses rather than by anyone's taste
TOOL_ESCALATE_HINT = (
    "Or resubmit with a leading '# lup: escalate[decision]: <why>' line in one"
    " of the call's own inputs to put it to a reviewer."
)


def escalated_reason(values: list[str]) -> str:
    """The reason an escalation marker among a call's inputs states.

    The marker is the shell lattice's own, read here from whichever input
    carries it: a tool call has no single line to lead, and which field an
    agent can write prose into is the runtime's business rather than this
    kernel's. An empty reason reads as no escalation, so a marker stating
    nothing leaves the refusal standing.
    """
    for value in values:
        marker = ESCALATE_RE.match(value)
        if marker is not None and marker.group("why").strip():
            return marker.group("why").strip()
    return ""


def matching_refusal(
    name: str, values: list[str], rows: list[RefusedToolRow]
) -> RefusedToolRow | None:
    """The declared refusal one call matches, if any.

    A row with no specifier refuses the whole tool. One with a specifier
    refuses a single subject of it, matched against every string the call
    carries rather than against a named field — which field a runtime spells
    a subject in is that runtime's own business, and a refusal knowing only
    one spelling would fail open on the rest.
    """
    return next(
        (
            row
            for row in rows
            if row["tool"] == name
            and (not row["specifier"] or row["specifier"] in values)
        ),
        None,
    )


def decide_tool(
    name: str, values: list[str], rows: list[RefusedToolRow]
) -> KernelDecision | None:
    """The verdict the declared refusals reach for one call, if they reach one.

    A matched row denies, unless the call's own input escalates it, in which
    case the refusal becomes the approval question the agent asked for.

    A call whose tool a row names but whose subject none of them selects is
    judged and passed: the table has an opinion about that tool and the
    opinion is "not this one", so ``Skill(artifact-design)`` can be refused
    without every other skill needing approval to run. Deferring rather than
    allowing leaves the runtime's ambient rules in force, so this table only
    ever subtracts.

    ``None`` means no row spoke at all, which leaves the caller's own answer
    for a tool it has no rule surface for.
    """
    row = matching_refusal(name, values, rows)
    if row is not None:
        why = escalated_reason(values)
        if why:
            return KernelDecision(
                "ask", f"escalated ({why}): {row['reason']}", recovery=row["recovery"]
            )
        return KernelDecision(
            "deny", row["reason"], recovery=f"{row['recovery']} {TOOL_ESCALATE_HINT}"
        )
    if any(row["tool"] == name for row in rows):
        return KernelDecision("defer", f"no refusal names this use of {name!r}")
    return None
