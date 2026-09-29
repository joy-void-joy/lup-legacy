"""One index over every Lup rule for discovery and the rule reference.

A rule id met in a `# lup: ignore[...]` directive, a hook denial, or an
auditor finding resolves here: every rule the set holds — a line rule the
kernel runs per edit, a project rule the sweep runs over the tree, a
composition rule generation runs over the assembled harness — is projected
into one card carrying its family, scope, diagnostic, and the module that
enforces it. `uv run lup-devtools dev rules` renders the cards into the
checked-in `docs/rules.md` reference that deny messages point at, so no rule
is discoverable only through the scanner that owns it.

A card is a rendering of its declaration and is never written beside it: a
card kept by hand describes the verdict its rule gave when somebody last
copied it. A rule whose verdict a type oracle sharpens carries that
refinement on its card, read off the rule's own declaration — the reference
is where the two surfaces are reconciled: the edit hook decides on the
spelling alone and the whole-file audit may decide otherwise once the subject
is resolved.
"""

from pydantic import BaseModel

import lup.harness.codescan.antipatterns as antipatterns
from lup.harness.codescan.antipatterns import RuleSet
from lup.harness.codescan.common import (
    ExampleVerdict,
    Rule,
    RuleFamily,
    RuleSelection,
    RuleStrength,
)

# lup: ignore[constant-declaration] — one generated artifact's identity: the
# writer and every deny message that cites it must name the same file
RULE_REFERENCE = "docs/rules.md"
"""Repository-relative path of the generated reference deny messages cite."""

# lup: ignore[constant-declaration] — a rendering of the reference's own table,
# declared with the projection that writes it rather than chosen per caller
CLEARED_SEPARATOR = "  ·  "
"""What separates the near-misses sharing one table cell."""


class RegisteredRule(BaseModel, frozen=True):
    """One rule's discovery card: identity, family, diagnostic, and home.

    ``refinement`` is empty for a rule that decides the same way everywhere.
    Where it is set, the edit hook's verdict is the broad one the ``example``
    shows and the whole-file audit narrows it — the card says how, so a
    contributor who meets a denial the repository sweep does not report can
    tell which surface is speaking.
    """

    id: str
    family: RuleFamily
    scope: str
    example: str
    cleared: str = ""
    """The near-miss this rule spares, in the rule's own words.

    The neighbouring shape that keeps the same spelling and is not the defect
    — which is the half of the rule a reader meeting a denial most needs,
    because it says whether their site is one the gate will go on refusing.
    Empty only for a rule whose cleared examples all span several lines and
    were shown flattened as the first instead.
    """
    message: str
    defined_in: str
    refinement: str = ""
    strength: RuleStrength = "soft"
    """Soft by default: a rule earns ``strong`` by having a replacement that is
    right every time, and until someone can say what that replacement is, the
    honest answer is that an exception might exist."""


def showable(rule: Rule, verdict: ExampleVerdict) -> list[str]:
    """This rule's examples of one verdict that survive a table cell.

    A row is one line, and a snippet spanning several — a class body, a
    try/except — arrives there with its newlines flattened into spaces,
    which reads as a rendering fault rather than as code. Those examples
    exist for the suite, which runs them whole; a rule with nothing but
    multi-line examples of a verdict shows its first one flattened rather
    than showing nothing at all.
    """
    declared = [
        example.shown() for example in rule.examples if example.verdict == verdict
    ]
    single = [code for code in declared if "\n" not in code]
    return single or declared[:1]


def card(rule: Rule, family: RuleFamily, scope: str, defined_in: str) -> RegisteredRule:
    """Project one declaration into its card, whichever surface decides it.

    The examples are the rule's own, which is what keeps this page from
    drifting: the same snippets are run through the gates that enforce the
    rule by the suite, so a card showing a shape a gate no longer decides
    that way fails the suite. Every cleared example is shown rather than the
    first, because the replacement and the neighbours that keep the same
    spelling answer different questions — the message already says what to
    write instead, while only these say whether the site a denial named is
    this rule's subject at all.
    """
    flagged = showable(rule, "flagged")
    return RegisteredRule(
        id=rule.id,
        family=family,
        scope=scope,
        example=flagged[0] if flagged else "",
        cleared=CLEARED_SEPARATOR.join(showable(rule, "cleared")),
        message=rule.message,
        defined_in=defined_in,
        refinement=rule.refinement,
        strength=rule.strength,
    )


def all_rules(
    rules: RuleSet | None = None,
    selection: RuleSelection | None = None,
) -> list[RegisteredRule]:
    """Every rule this repository holds itself to, project rules first.

    The reference is generated from the same selection the sweep and the
    compiled plugin read, so a rule a project retired is absent from all
    three rather than documented as enforced by a page nothing enforces.
    """
    kept = (rules or RuleSet()).selected(selection or RuleSelection())
    return [
        *(
            card(rule, rule.family, rule.scope, rule.defined_in)
            for rule in kept.project
        ),
        *(
            card(rule, rule.family, rule.scope, rule.defined_in)
            for rule in kept.composition
        ),
        *(
            card(rule, "anti-pattern", scope, antipatterns.__name__)
            for scope, scoped in (
                ("Python", kept.python),
                ("TypeScript", kept.typescript),
            )
            for rule in scoped
        ),
    ]


def every_rule_retired() -> RuleSelection:
    """A selection holding a project to none of the rules this library ships.

    Spelled as every id rather than as a flag meaning "all of them", because
    the selection is subtractive: a project that drops the family and one
    that dropped them a denial at a time are the same project, and a rule
    added later is one this selection has visibly not answered for. It is
    also what makes a relaxation legible — the retirement names what it
    retired instead of standing for it.
    """
    return RuleSelection(retired=[rule.id for rule in all_rules()])
