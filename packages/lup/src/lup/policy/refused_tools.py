"""The validated surface for refusing a whole native call, and its erasure.

The library ships no refusals. Whether publishing a page, opening a browser,
or reaching any other tool is against the point of a project is a judgement
about that project, so this declares the shape one is stated in and each
composition root states its own — the same inversion the shell vocabulary
takes. :mod:`lup.policy.kernel.tools` decides against the erased rows.
"""

from collections.abc import Sequence

from pydantic import BaseModel, Field

from lup.policy.kernel.diagnostic import Step
from lup.policy.kernel.rows import RefusedToolRow


class RefusedTool(BaseModel, frozen=True):
    """One native call a project refuses, and where its agent goes instead.

    ``specifier`` narrows the refusal to a single subject of a tool — the
    ``artifact-design`` in ``Skill(artifact-design)`` — and is empty when the
    whole tool is refused. ``reason`` says what the call would have done, in
    words the person answering an escalated one reads. ``recovery`` is required
    because it is what the agent does next: a refusal that says no and nothing
    else leaves the next attempt to guesswork, which is what the generated-tree
    refusal avoids by naming the command that does reach the same end.

    A refusal is never absolute. The kernel promotes it to an approval
    question when the call's own input carries the escalation marker, so a
    reflex is stopped without a deliberate use being walled off.
    """

    tool: str
    specifier: str = ""
    reason: str = Field(min_length=1)
    recovery: list[Step] = Field(min_length=1)

    def spelling(self) -> str:
        """``Tool`` or ``Tool(specifier)``, as a permission rule names it."""
        return f"{self.tool}({self.specifier})" if self.specifier else self.tool


def routed_for(
    routed: list[str], refused: list[RefusedTool], refusable: Sequence[str] = ()
) -> list[str]:
    """The tools a hook registers for: those it decodes, plus those refused.

    A refusal is only enforced over a call that reaches the judge, so the
    declaration that states one is also what widens the routed set — rather
    than a runtime listing the names some adopter might refuse, which would
    register every one of them for the adopters who refuse none. That costs
    exactly what this avoids: a tool routed with no rule to meet earns the
    conservative ``ask``, and ``ask`` outranks the ``allow`` a directory ACL
    beside it granted.

    A refusal naming a tool the runtime already decodes is refused rather than
    returned, because it would never be reached: that tool's own family answers
    first and the table is only consulted for what falls past them. Silently
    keeping it would read as a refusal in force while the call went through.
    The one exception is *refusable*: decoded tools whose branch asks this
    table before its own family, as a spawn's does — its family judges only
    the name it goes out under, so a refusal of spawning has nowhere else to
    be stated.
    """
    inert = [
        rule.spelling()
        for rule in refused
        if rule.tool in routed and rule.tool not in refusable
    ]
    if inert:
        raise ValueError(
            f"{', '.join(inert)}: this runtime decodes that tool, so its own"
            " rules answer first and the refusal would never be reached —"
            " express it as a rule of that family instead"
        )
    return [
        *routed,
        *dict.fromkeys(rule.tool for rule in refused if rule.tool not in routed),
    ]


def erase_refused_tools(rules: list[RefusedTool]) -> list[RefusedToolRow]:
    """Erase validated refusals into the primitive rows the kernel matches."""
    return [
        RefusedToolRow(
            tool=rule.tool,
            specifier=rule.specifier,
            reason=rule.reason,
            recovery=rule.recovery,
        )
        for rule in rules
    ]
