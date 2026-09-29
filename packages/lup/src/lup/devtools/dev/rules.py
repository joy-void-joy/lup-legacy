"""Generate the checked-in Lup rule reference from the canonical rule registry."""

from pathlib import Path

import lup.harness.models as models
from lup.providers.harness import claude_prompt_renderer
from lup.harness.codescan.common import RuleStrength
from lup.harness.codescan.common import RuleSelection
from lup.harness.codescan.registry import RULE_REFERENCE, RegisteredRule, all_rules
from lup.formats.banner import REGENERATE_COMMAND, GeneratedBanner
from lup.harness.materialization import write_generated_file
from lup.harness.models import Artifact
from lup.formats.markdown import CodeCell, HtmlCodeCell, PlainCell, TableCell
from lup.workspace.paths import project_root

# lup: ignore[constant-declaration] — the rule reference's own prose, which is
# the document this module exists to render rather than an input it takes
INTRODUCTION = (
    "# Lup rule reference\n\n"
    "Every executable Lup rule family — anti-pattern, boundary, spelling, and "
    "architecture rules — is indexed here from `lup.harness.codescan.registry`, with the "
    "module that defines and enforces each rule. An edit-hook denial cites its "
    "rule id and this reference. Lup rules enforce repository-specific "
    "architecture and editing conventions; Ruff remains the source of standard "
    "Python diagnostics. Each anti-pattern's examples are declared on the rule "
    "itself and run through both the edit hook and the repository auditor by "
    "the suite, so a shape shown here is one both gates decide that way. The "
    "cleared column is the neighbouring shape a rule spares — a one-argument "
    "`.replace` renaming a file, an argless `.split` tokenizing prose — which "
    "is what says whether a site a denial named is really this rule's "
    "subject.\n\n"
    "## Typed suppressions\n\n"
    "Suppress one deliberate site with `# lup: ignore[rule-id]` and a reason. "
    "Comma-separated ids cover a line that intentionally matches several rules. "
    "A directive sits on the line it guards, or stands alone directly above "
    "it when the reason is too long to fit inline; nowhere else reaches. A "
    "typed directive in the file's opening comment block applies file-wide. "
    "Bare "
    "`# lup: ignore` remains parseable but is reported as untyped; a stale typed "
    "directive is blocking. `# noqa`, `# type: ignore`, and `# pyright: ignore` "
    "are separate forbidden shapes.\n\n"
    "The **Suppression** column says whether a directive reaches a rule at all. "
    "Most are soft: they name a shape that is usually wrong and occasionally the "
    "only thing that works, so a typed directive is a reasoned exception and the "
    "audit grades it. A rule marked **refused** is strong — its replacement is "
    "right every time, which leaves a directive nothing to express but a decision "
    "to keep the defect. Those rules ignore every directive, report the violation "
    "anyway, and report the directive itself as spurious; the only way past one is "
    "to write the replacement its diagnostic names.\n\n"
    "```python\n"
    "cache: dict[str, int] = {}  # lup: ignore[empty-collection] — mutable fold\n"
    "```\n\n"
    "## Structural rules\n\n"
)

# lup: ignore[constant-declaration] — a heading of that same document
ANTI_PATTERN_HEADING = "\n## Edit anti-patterns\n\n"

# lup: ignore[constant-declaration] — the section of that document explaining
# how a rule whose verdict turns on a declaration reaches each gate
REFINEMENT_INTRODUCTION = (
    "\n## Type-resolved rules\n\n"
    "Both gates read the same finished source, both parse it, and both reach "
    "a type checker for the rules below — so a rule the grammar has a word "
    "for decides identically at edit time and in `lup-devtools dev check`. "
    "The audit holds the oracle in `lup.harness.codescan.oracle` directly; the hook "
    "shells out to `lup-devtools dev refutations` with the text it is about "
    "to write, and only for a rule whose row says its verdict turns on a "
    "resolved declaration, so an edit tripping no such rule pays nothing. "
    "What differs is the answer when no checker answers at all: the audit "
    "keeps the broad verdict, and the hook asks rather than denying, because "
    "a denial it cannot substantiate leaves a directive as the only way "
    "past. A `# lup: ignore` left guarding a refuted line is reported as a "
    "dead directive.\n\n"
    "Source that will not parse has no shapes to read. There a rule that "
    "declares a matcher falls back to its pattern where a directive could "
    "still answer it, and reports nothing at all where none could — a "
    "**refused** rule admits no suppression, so a verdict its matcher never "
    "confirmed would be a denial with no way past.\n\n"
)


def suppression_cell(strength: RuleStrength) -> TableCell:
    """What a strength offers a reader who has just been denied by a rule."""
    match strength:
        case "soft":
            return PlainCell(text="typed directive")
        case "strong":
            return PlainCell(text="**refused**")


def rule_table(rules: list[RegisteredRule]) -> models.MarkdownTable:
    """One card per rule, as a table part the document composes like any other."""
    return models.MarkdownTable(
        headers=[
            "Rule id",
            "Family",
            "Scope",
            "Matching example",
            "Cleared instead",
            "Diagnostic",
            "Suppression",
            "Defined in",
        ],
        rows=[
            [
                CodeCell(text=rule.id),
                PlainCell(text=rule.family),
                PlainCell(text=rule.scope),
                # An example may quote a backtick, which no fence survives.
                HtmlCodeCell(text=rule.example),
                HtmlCodeCell(text=rule.cleared)
                if rule.cleared
                else PlainCell(text="—"),
                PlainCell(text=rule.message),
                suppression_cell(rule.strength),
                CodeCell(text=rule.defined_in),
            ]
            for rule in rules
        ],
    )


def rule_reference_document(
    selection: RuleSelection | None = None,
) -> models.PromptDocument:
    """Every Lup rule this repository enforces, and how each is suppressed.

    Generated from the project's own selection, so a rule it retired is
    absent here rather than documented as enforced by a page whose whole
    job is to be the thing a denial can be looked up in.
    """
    rules = all_rules(selection=selection)
    structural = sorted(
        (rule for rule in rules if rule.family != "anti-pattern"),
        key=lambda item: item.id,
    )
    anti_patterns = sorted(
        (rule for rule in rules if rule.family == "anti-pattern"),
        key=lambda item: (item.scope, item.id),
    )
    refined = [rule for rule in rules if rule.refinement]
    refinements = "\n\n".join(
        f"### `{rule.id}`\n\n{rule.refinement}" for rule in refined
    )

    return models.PromptDocument(
        source=__name__,
        parts=[
            models.TextPart(text=INTRODUCTION),
            rule_table(structural),
            models.TextPart(text=ANTI_PATTERN_HEADING),
            rule_table(anti_patterns),
            models.TextPart(text=f"{REFINEMENT_INTRODUCTION}{refinements}\n"),
        ],
    )


RULE_REFERENCE_PATH = Path(RULE_REFERENCE)
"""Where this writes the reference, taken from the path deny messages cite so
the two cannot name different files."""


def rule_reference_artifact(selection: RuleSelection | None = None) -> Artifact:
    """The rule reference as one artifact, gated like any other generated file.

    Rendered through one runtime's vocabulary because a document has to be
    rendered through some runtime's, and this one names none: it is prose and
    tables end to end, so either vocabulary produces the same bytes.
    """
    document = rule_reference_document(selection)
    return Artifact.generated(
        path=RULE_REFERENCE_PATH,
        # A generated artifact ends in exactly one newline. The document's
        # last part opens the refinement paragraph and then lists the
        # refinements, so with every rule retired the opener's own separator
        # is all that would end the file.
        # lup: ignore[string-strip] — a rendered page's ending, not structured data
        body=claude_prompt_renderer().render(document).rstrip("\n") + "\n",
        semantic_id="docs.rules",
        banner=GeneratedBanner(
            source=document.declared_source(), command=REGENERATE_COMMAND
        ),
    )


def write_rule_reference(
    root: Path | None = None,
    *,
    check: bool = False,
    selection: RuleSelection | None = None,
) -> Path:
    """Write or verify the generated rule reference."""
    return write_generated_file(
        rule_reference_artifact(selection),
        root or project_root(),
        REGENERATE_COMMAND,
        check=check,
    )
