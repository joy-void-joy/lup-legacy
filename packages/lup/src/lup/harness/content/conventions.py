# lup: ignore[library-default]
# Every section here is a block of prose offered for composition, not a table
# of judgements imposed on a reader: a project that wants different words
# retires one and declares its own under the same id. That override is a
# spelling the rule can see, which is why `constant-declaration` needs no
# silencing beside it.
"""Convention text that is portable, held once and rendered by every flavor.

A project's guidance, the downstream template it publishes, and the reference
pages beside them would otherwise restate the same conventions in
near-identical prose, with a skill telling an editor to "mirror relevant
changes" between them by hand. That is the shape
``docs/self-improvement.md`` rejects — a prompt rule coexisting peacefully
with the failure it warns about — and the drift it produces is visible: em
dashes in one copy and double hyphens in the other, a bullet present in one
and missing from the other, for text that is supposed to be the same text.

What lives here is what every reader needs identically, which is why it is
the library's rather than any one project's. Anything true only of a
particular repository — its layout, its tooling paths — stays in that
project's guidance as an addition after the shared part, never as a
restatement of it: an addition cannot drift from what it adds to.

Blocks earn their place by what a gate cannot say in time. A rule the
checker enforces is described once in its own generated index, where it
carries its matching shape and its diagnostic; restating it here would be a
second copy that can drift from the first and is worse while it agrees.
What is held here instead is the shape of the gates themselves, and the
judgements no gate fires on.
"""

import lup.harness.models as models
from lup.harness.codescan.common import RuleSelection
from lup.formats.markdown import CodeCell, PlainCell

PLAN_AT_AGENT_SPEED = models.GuidanceSection(
    id="plan-at-agent-speed",
    chapter="orientation",
    parts=[
        models.Passage(module=__name__, name="plan-at-agent-speed"),
    ],
)

AGENT_VOCABULARY = models.GuidanceSection(
    id="agent-vocabulary",
    chapter="orientation",
    parts=[
        models.Passage(module=__name__, name="agent-vocabulary"),
    ],
)

THE_GATES = models.GuidanceSection(
    id="the-gates",
    chapter="gates",
    parts=[
        models.Passage(module=__name__, name="the-gates"),
    ],
)
"""The four gates, taught as mechanisms rather than as their contents.

An agent that knows a checker exists, that a denial names a rule id, and how
a suppression is spelled can meet a rule it has never read. An agent handed
twenty rules and no mechanism is stopped by the twenty-first. The contents
live in the generated index, which carries every rule rather than whichever
subset a prose list happened to name, and cannot fall behind the registry.
"""


SHAPING_RULES: list[str] = [
    "own-model-dispatch",
    "abc-capability",
    "constant-declaration",
]
"""The rules worth knowing by name before a shape is chosen.

Held as a declaration rather than inside the sentence because the sentence
has to name only the rules a project still enforces: one that retired a rule
and went on being told to know it by name would be reading advice about a
gate that cannot stop it, which is the drift this module exists to prevent.
Ids alone, because the shape each steers to is a paragraph in the generated
rule index, and a gloss beside the id here is a second copy of it.
"""


def shaping_sentence(selection: RuleSelection) -> str:
    """The paragraph naming the live shaping rules, or nothing where none are."""
    kept = [f"`{rule}`" for rule in SHAPING_RULES if selection.keeps(rule)]
    if not kept:
        return ""
    return (
        "\nSome rules shape a design before any gate catches it. Know these by name "
        "while choosing a shape, not after being stopped — `docs/rules.md` states "
        f"each: {', '.join(kept)}.\n"
    )


def design_principles(
    selection: RuleSelection | None = None,
) -> models.GuidanceSection:
    """What no rule fires on, plus the rule ids this project still enforces.

    Nothing mechanical is listed: a denial names that in time. What the
    section holds either has no executable rule at all, or has one that
    arrives too late to change the shape being chosen — for those, the id is
    a lookup key rather than the rule restated, and only while the project
    holds itself to it.

    A builder rather than a constant, and the one section here that has to be:
    what it says depends on a declaration only the reading project holds. Its
    id is fixed anyway, so a project retires or replaces it by the same name
    every other section answers to.
    """
    return models.GuidanceSection(
        id="design-principles",
        chapter="code",
        parts=[
            models.Passage(
                module=__name__,
                name="design-principles",
                values={
                    "shaping_sentence": models.TextPart(
                        text=shaping_sentence(selection or RuleSelection())
                    ),
                },
            ),
        ],
    )


SANCTIONED_EXCEPTIONS = models.GuidanceSection(
    id="sanctioned-exceptions",
    chapter="code",
    parts=[
        models.Passage(module=__name__, name="sanctioned-exceptions"),
    ],
)
"""The carve-outs a rule id cannot deliver.

Leaving a convention unlisted is safe exactly where the checker says the
same thing at the moment it matters. The private-prefix diagnostics
carry their own exemptions — nest inside the caller, an unused parameter
keeps its underscore — so those are left to them. These two are what the
checker does *not* say: its diagnostic names the refused shape and stops, so
an agent obeying it literally would remove a package's public API or hide a
wrapper that should have been inlined.
"""

FAILURE_ANALYSIS = models.GuidanceSection(
    id="failure-analysis",
    chapter="meta",
    parts=[
        models.Passage(module=__name__, name="failure-analysis"),
    ],
)
"""The two paragraphs every self-improvement reader needs identically.

The question and the worked example travel together: the question alone
states a lesson a reader can agree with and not apply, and the example is
what makes it operable. The page that teaches the loop and the downstream
template both compose this block, and two copies of one lesson drift.

The always-loaded guidance carries neither, and points at the page instead:
what it keeps is the principle those paragraphs argue for, which is one
sentence, where the pair is two paragraphs a reader pays for every turn.
"""

LONG_RUNNING_WORK = models.GuidanceSection(
    id="long-running-work",
    chapter="tooling",
    parts=[
        models.Passage(module=__name__, name="long-running-work"),
    ],
)

WORKING_ALONGSIDE = models.GuidanceSection(
    id="working-alongside",
    chapter="orientation",
    parts=[
        models.Passage(module=__name__, name="working-alongside"),
    ],
)

DEFECT_DISPOSITION = models.GuidanceSection(
    id="defect-disposition",
    chapter="process",
    parts=[
        models.Passage(module=__name__, name="defect-disposition"),
    ],
)
"""Where a fault nobody in this session caused is allowed to end up.

Composed after the friction rules because the third disposition is theirs:
the note and the issue are already spelled there, and this only says that
one of them is owed. Held here rather than in a project's own guidance
because the reflex it answers — naming a defect in a report and calling its
age a decision — belongs to how agents report, not to any one repository.
"""

MERGE_CONFLICT_RESOLUTION = models.GuidanceSection(
    id="merge-conflict-resolution",
    chapter="workflow",
    parts=[
        models.Passage(
            module=__name__,
            name="merge-conflict-resolution",
            values={
                "merge_skill": models.SkillInvocation(plugin="lup", skill="merge"),
            },
        ),
    ],
)

COMMIT_TYPES = models.GuidanceSection(
    id="commit-types",
    chapter="workflow",
    parts=[
        models.MarkdownTable(
            headers=["Type", "Use"],
            rows=[
                [CodeCell(text="feat"), PlainCell(text="New feature or capability")],
                [CodeCell(text="fix"), PlainCell(text="Bug fix")],
                [
                    CodeCell(text="refactor"),
                    PlainCell(text="Neither fixes a bug nor adds a feature"),
                ],
                [CodeCell(text="docs"), PlainCell(text="Documentation only")],
                [CodeCell(text="test"), PlainCell(text="Adding or updating tests")],
                [
                    CodeCell(text="chore"),
                    PlainCell(text="Maintenance — dependencies, build config"),
                ],
                [
                    CodeCell(text="meta"),
                    PlainCell(
                        text="Harness content and the trees it generates: guidance,"
                        " settings, skills, hooks"
                    ),
                ],
                [CodeCell(text="data"), PlainCell(text="Generated data and outputs")],
                [
                    CodeCell(text="release"),
                    PlainCell(
                        text="What `dev release` commits for a release or a"
                        " candidate of one — never written by hand"
                    ),
                ],
            ],
        ),
        models.TextPart(text="\n"),
    ],
)
"""The commit vocabulary, held once because three documents state it.

A skill telling an agent how to commit, the contributing page a human reads,
and the guidance both compose from would each carry a copy of this table, and
copies drift apart in wording — `refactor` as "code restructuring without
behavior change" in one and "neither fixes a bug nor adds a feature" in
another, for a row that is supposed to say one thing. Rows rather than prose so
the escaping is the table's, and so a type added here reaches every reader at
once.
"""

COMMIT_GUIDELINES = models.GuidanceSection(
    id="commit-guidelines",
    chapter="workflow",
    parts=[
        models.Passage(module=__name__, name="commit-guidelines"),
    ],
)
"""The habits, without the vocabulary — each consumer supplies that itself.

A page a human reads puts `COMMIT_TYPES` directly after this, because the
type is being chosen there. The always-loaded guidance points at that page
instead: a table it carries is one an agent pays for on every turn to read
once, and the skill that writes commits renders it at the moment it matters.
"""
