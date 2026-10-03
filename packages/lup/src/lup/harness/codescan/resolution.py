"""Judging a rule's sites by what a type oracle says their subjects are.

The anti-pattern set detects *shapes*. A shape is all the hermetic edit hook
can see — it classifies fragments of a proposed edit, which carry no types and
often do not parse — so `.get(` there means every `.get(` on every receiver.
The whole-file audit reads finished, parseable source and can do better:
through the `lup.harness.codescan.oracle` port it can ask a type checker what a
subject is declared as. `payload.get(...)` on a mapping is the schema-hiding
access the rule is about; the identical spelling on an HTTP client is not,
and the audit should say so instead of leaving a contributor to suppress by
reflex.

Nothing here selects anything. The sites are the ones the rule's own matcher
already chose, carrying the positions it recorded while it had the nodes in
hand, and the family is the one the rule declares. So a rule is one object
with one statement of its shape, and this is the engine that measures its
sites against its family — which is why a decorator or a module-qualified
call, ruled out by the matcher, never becomes a question for a checker.

Resolution is deliberately structural. The oracle answers what a subject is
*declared as*, never what a type checker would print for it, so membership is
decided by real declarations — `dict` in typeshed's `builtins.pyi`,
`_Environ(MutableMapping[...])` in `os`'s stub — instead of by
pattern-matching rendered type strings, which are display markup that no
protocol specifies and every version is free to change.

A line is refuted only when every site on it is shown to be outside its
rule's family. Membership is established rather than merely left undisproven:
a subject the checker resolves into the family keeps its finding, and one it
resolves outside the family *and* one nothing can be shown about are both
refuted — a rule about mappings has nothing to say about a value nobody can
show is one, and denying there is denying on a guess with a directive as the
only way past. The two carry different evidence, so a reader can tell which
happened. Without an oracle nothing resolves, nothing is refuted, and every
broad verdict stands as the matcher alone gives it.
"""

from pathlib import Path

from pydantic import BaseModel

from lup.harness.codescan.common import AntiPattern, PythonSource, Refutation
from lup.harness.codescan.oracle import (
    Declaration,
    SourceBuffer,
    SymbolQuery,
    SourcePosition,
    TypeOracle,
)
from lup.policy.kernel.edit import MatchSite


class SelectedSite(BaseModel, frozen=True):
    """One site a rule selected, with the file, the rule, and what settles it."""

    file: str
    rule: AntiPattern
    line: int
    subject: str
    query: SymbolQuery


def query_for(path: Path, site: MatchSite) -> SymbolQuery | None:
    """The symbols one site is settled by, or None where it names none.

    A site with no member position is one a selector found without resolving
    anything — a rule that is about a line and nothing more, or text no tree
    could be had from. Neither is a question a checker can be asked.
    """
    if "member" not in site:
        return None
    member = site["member"]
    receiver = site["receiver"] if "receiver" in site else None
    return SymbolQuery(
        member=SourcePosition(path=path, line=member["line"], column=member["column"]),
        receiver=None
        if receiver is None
        else SourcePosition(
            path=path, line=receiver["line"], column=receiver["column"]
        ),
    )


def resolved_sites(
    sources: list[PythonSource], rules: list[AntiPattern]
) -> list[SelectedSite]:
    """Every site of every resolving rule that carries a symbol to ask about.

    A rule's own matcher answers, so what is judged here is exactly what the
    gates flag — a shape the matcher ruled out never becomes a question, and
    a checker never spends a session on one.
    """
    return [
        SelectedSite(
            file=source.path.as_posix(),
            rule=rule,
            line=site["line"],
            subject=site["subject"] if "subject" in site else "",
            query=query,
        )
        for source in sources
        for rule in rules
        if rule.family is not None and rule.matcher is not None
        for site in rule.matcher.select(source.text)
        if (query := query_for(source.path, site)) is not None
    ]


def refute(
    sources: list[PythonSource],
    oracle: TypeOracle | None,
    rules: list[AntiPattern],
) -> dict[str, list[Refutation]]:
    """Refute every line no site of which is shown to be in its rule's family.

    Returns the surviving refutations per repository-relative posix path, for
    `lup.harness.codescan.antipatterns.audit_text` to drop and for the auditor to
    report.

    A line carrying several sites of one rule is refuted only when every one
    of them is: one mapping access among three client calls still hides a
    schema, and the directive guarding that line still guards something.

    Every source's own text goes to the oracle, so what is resolved is what
    is audited. Letting the checker re-read the path instead would answer
    about whatever disk holds — the same file for a sweep that read it from
    there, and a different one entirely for a caller judging an edit before
    it is written, which is the caller that most needs the answer.
    """
    selected = resolved_sites(sources, rules)
    if oracle is None or not selected:
        return {}

    declared = oracle.declarations(
        [chosen.query for chosen in selected],
        [SourceBuffer(path=source.path, text=source.text) for source in sources],
    )

    def refutation_for(
        chosen: SelectedSite, declaration: Declaration
    ) -> Refutation | None:
        """One site's verdict, or None where its subject is in the family."""
        family = chosen.rule.family
        if family is None or declaration.in_family(family):
            return None
        return Refutation(
            rule_id=chosen.rule.id,
            line=chosen.line,
            subject=chosen.subject,
            evidence=declaration.refutation(chosen.subject, family),
            settled=declaration.settled(),
        )

    judged = [
        (chosen, refutation_for(chosen, declaration))
        for chosen, declaration in zip(selected, declared, strict=True)
    ]
    standing = {
        (chosen.file, chosen.rule.id, chosen.line)
        for chosen, refutation in judged
        if refutation is None
    }
    # A line is settled only when every site on it is. One receiver nobody
    # can show anything about keeps the line open whatever the rest resolved
    # to, because the directive guarding it may be guarding exactly that site.
    open_lines = {
        (chosen.file, chosen.rule.id, chosen.line)
        for chosen, refutation in judged
        if refutation is not None and not refutation.settled
    }

    surviving = [
        (
            chosen.file,
            refutation.model_copy(
                update={
                    "settled": (chosen.file, chosen.rule.id, chosen.line)
                    not in open_lines
                }
            ),
        )
        for chosen, refutation in judged
        if refutation is not None
        and (chosen.file, chosen.rule.id, chosen.line) not in standing
    ]
    return {
        file: sorted(
            [row for named, row in surviving if named == file],
            key=lambda row: (row.line, row.rule_id),
        )
        for file in dict.fromkeys(named for named, _ in surviving)
    }
