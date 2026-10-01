"""A seam over a library table: taking it as offered, saying what differs.

A seam is *a place this library holds an opinion a project is meant to
overrule*, which is the definition `dev seams` reads and writes by. Most of
them are declarations in the adopting project's own catalog and need no
mechanism here at all. The ones shaped like a table do, and this is theirs:
three reach a project as a starting point rather than a fixture — the
anti-patterns it holds its code to, the shell vocabulary it runs, the edit
gates it judges its own changes by — and in all three, without a seam, the only
way to disagree with one entry is to restate the table around it, where a
restatement fallen behind the library looks exactly like a decision. A project
names what it drops
and adds what the library lacks, keyed on the same id a directive, a denial and
the generated reference already use, so an override replaces its namesake in
place rather than sitting beside it.

The word carries a second load, and it is deliberate rather than a collision.
:mod:`lup.sessions.capabilities` and each subject's equivalent hold *capability*
seams — the narrow ABCs an implementation is swapped in at — and `seam-boundary`
polices where those may be imported from. One idea in two materials: a place
the library's answer is replaceable, by an implementation there and by a
declaration here. Nothing gathers both, because a capability seam belongs to
the subject that declares it and this belongs below every subject that has a
table.

:class:`Selection` is the one answer. A project names what it drops, adds what
it has that the library does not, and says nothing about the rest::

    SHELL = Selection(retired=["docker"], overrides=[lake_rule()])

Subtractive first, additive second, and both keyed on the same id a directive,
a denial, and the generated reference already use. An override sharing an id
with a library rule *replaces* it in place rather than sitting beside it,
because two rules answering to one name is the ambiguity this exists to
remove — whichever one a table walked into first would be the policy, and
which that is would depend on the order a composition happened to build.

The resolved table keeps the library's order and appends what the project
added, which is what lets a table read either way round: matched
first-to-last, a project's own rule is the only one left under its name;
matched last-to-first, a project's additions are the ones a broad library
statement no longer covers.
"""

from abc import ABC, abstractmethod

from pydantic import BaseModel


class SelectableRule(BaseModel, ABC, frozen=True):
    """A rule a project may retire or replace by name.

    The id is a method rather than a field because the three tables each
    name their rules and have no reason to agree on the spelling: an
    anti-pattern carries an ``id``, a shell command and an edit rule carry a
    ``name``. Asking each what its id *is* leaves those spellings alone, and
    naming ``ABC`` among the bases says outright that a rule which never
    answers cannot be built — a new table cannot join this by omission.

    Frozen, because a selection resolves one table out of two and hands the
    result to a policy that will be asked the same question many times. A rule
    that could be mutated after resolution would let the answer drift from the
    declaration somebody reviewed, and nothing here has a reason to change
    after it is written down.
    """

    @abstractmethod
    def selection_id(self) -> str:
        """The name this rule is retired or replaced under."""


class Selection[RuleT: SelectableRule](BaseModel, frozen=True):
    """Which of a library table's rules a project holds itself to, plus its own.

    An empty selection is the library's table unchanged, which is what a
    project that has not yet formed an opinion should get.
    """

    retired: list[str] = []
    """Rule ids this project does not hold itself to."""

    overrides: list[RuleT] = []
    """Rules this project declares — replacing a library rule of the same id."""

    def keeps(self, rule_id: str) -> bool:
        """Whether a library rule is live here, for a caller deciding to run it."""
        return rule_id not in self.retired

    def over(self, defaults: list[RuleT]) -> list[RuleT]:
        """The library's table as this project resolved it.

        Retired rules are gone, replaced ones are gone from their original
        position, and everything the project declared follows in the order it
        declared it.
        """
        replaced = {rule.selection_id() for rule in self.overrides}
        return [
            *[
                rule
                for rule in defaults
                if self.keeps(rule.selection_id())
                and rule.selection_id() not in replaced
            ],
            *self.overrides,
        ]
