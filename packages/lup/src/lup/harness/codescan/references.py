# lup: ignore[import-re, re-call, string-split, stale-reference, constant-declaration]
# A documentation role is prose and a dotted path is Python's own spelling of a
# name: neither has a parser to reach for, which is this module's subject. The
# paths written below are illustrations — of the shape this rule reads, and of
# one that leads nowhere — so they are the two references in the tree that have
# to stay unresolvable. A rule's id and the sentence it denies with are this
# repository's identity for it, cited by directives and by the rule reference,
# so neither is a caller's to replace.
"""The symbols prose names, checked against the symbols that exist.

A sentence naming a module or a function is a claim, and the only claim in a
docstring a machine can settle. Everything else a comment says has to be read
to be judged; this one either resolves or it does not, so it is the part of
prose drift a gate can hold.

Scoped to the fully-qualified references, ``:func:`lup.policy.x``` and its
siblings, because those resolve with no context to supply. A bare name is
resolved by the module it sits in and says nothing about whether a reader
would find it; an attribute of a class is declared as an annotation as often
as it is bound, and reading only what is bound reports the declaration as
missing. Both are left to the reader, so that what this reports is wrong
rather than merely unproven.
"""

import importlib
import re

from lup.harness.codescan.common import PythonSource, RuleExample
from lup.harness.codescan.project import (
    AuditedProject,
    ProjectRule,
    RuleFinding,
    RuleViolation,
    audit_suppressions,
)

STALE_REFERENCE = "stale-reference"

QUALIFIED_REFERENCE = re.compile(
    r":(?:func|class|mod|meth|attr|data|exc):`~?(lup\.[\w.]+)`"
)
"""A documentation role naming one symbol by its whole path."""

REFERENCE_MESSAGE = (
    "A docstring naming a symbol by its full path is a claim the reader "
    "follows, and this one leads nowhere: the module does not import, or the "
    "name is not in it. Name what exists, or drop the path and say the thing "
    "in words. Where the path is a downstream project's rather than this "
    "one's, `# lup: ignore[stale-reference]` says so"
)

DOCUMENTED = "packages/lup/src/lup/example.py"


def module_of(dotted: str) -> str:
    """The longest prefix of `dotted` that imports, or `""` where none does."""
    parts = dotted.split(".")
    for split in range(len(parts), 0, -1):
        try:
            importlib.import_module(".".join(parts[:split]))
        except Exception:
            continue
        return ".".join(parts[:split])
    return ""


def resolves(dotted: str) -> bool:
    """Whether a documented path reaches something this tree declares.

    A path naming a module answers on its own. One naming something inside a
    module is answered by that module's namespace, and anything deeper is an
    attribute of a class, which this rule does not judge — so a path is
    refused only where the module is gone or the first name inside it is,
    which is what a moved or deleted symbol looks like.
    """
    held = module_of(dotted)
    if not held:
        return False
    if held == dotted:
        return True
    return hasattr(importlib.import_module(held), dotted[len(held) + 1 :].split(".")[0])


def stale_reference_violations(sources: list[PythonSource]) -> list[RuleViolation]:
    """Every documented path across these modules that reaches nothing."""
    return [
        RuleViolation(
            path=source.path,
            line=number,
            message=f"documented symbol {dotted} resolves to nothing",
        )
        for source in sources
        for number, line in enumerate(source.text.splitlines(), start=1)
        for dotted in QUALIFIED_REFERENCE.findall(line)
        if not resolves(dotted)
    ]


def stale_reference_findings(audited: AuditedProject) -> list[RuleFinding]:
    """This rule's verdicts across every module the project holds, or its scope."""
    judged = audited.judged_sources()
    return audit_suppressions(
        judged, stale_reference_violations(judged), STALE_REFERENCE
    )


STALE_REFERENCE_RULE = ProjectRule(
    id=STALE_REFERENCE,
    family="spelling",
    scope="Python docstrings and comments",
    examples=[
        RuleExample(
            code='"""Read through :func:`lup.policy.kernel.gone.vanished`."""',
            verdict="flagged",
            path=DOCUMENTED,
        ),
        RuleExample(
            code='"""Read through :mod:`lup.policy.kernel.edit`."""',
            verdict="cleared",
            path=DOCUMENTED,
        ),
        RuleExample(
            code='"""The `queued_review` one dispatcher holds."""',
            verdict="cleared",
            path=DOCUMENTED,
        ),
    ],
    message=REFERENCE_MESSAGE,
    audit=stale_reference_findings,
)
"""The reference rule: a documented path names something that is there."""
