"""Audit repository source for missing and spurious `# lup: ignore` markers.

Backs `lup-devtools dev check --antipatterns` (and the standalone
`dev check`-row). Walks every tracked or untracked `.py`/TS-family file and
runs the single `lup.harness.codescan.antipatterns` set over it — the same set the
edit hook enforces — reporting three classes the hook cannot catch after the
fact:

- **missing**: a line trips a rule but carries no `# lup: ignore` covering it
  (it slipped in past the hook, or predates the rule). Blocking.
- **spurious**: a `# lup: ignore[id]` (or a bare one) guards a rule the line
  does not trip — a dead directive to delete. Blocking.
- **untyped**: a bare `# lup: ignore` validly silences a line but names no
  rule. Advisory (does not fail the check) — surfaced so the migration to
  typed `# lup: ignore[id]` directives is gradual.

The set is shared with the hook; the verdicts need not be. This sweep reads
whole parseable files, so it hands `lup.harness.codescan.resolution` a type oracle and
decides some rules more narrowly than a hook judging an untyped edit fragment
ever could. Every finding that narrowing drops is reported as a **refuted**
row carrying the declaration that settled it, and the directive guarding it
turns up as spurious on the next line of the report.
"""

from collections import Counter, defaultdict
import cProfile
import pstats
from collections.abc import Iterator, Sequence, Set as AbstractSet
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from pathlib import Path, PurePosixPath

import typer
from pydantic import BaseModel

from lup.harness.codescan.antipatterns import (
    PYTHON_ANTI_PATTERNS,
    AntiPatternFinding,
    RuleSet,
    audit_text,
    patterns_for_suffix,
)
from lup.harness.codescan.common import (
    AntiPattern,
    PythonContext,
    PythonSource,
    Refutation,
    file_level_ignore,
    module_name,
)
from lup.policy.kernel.typescript import TYPESCRIPT_SUFFIXES
from lup.harness.codescan.resolution import refute
from lup.devtools.dev.refutations import remembered_refutations
from lup.workspace.paths import project_root, refutation_cache_path
from lup.harness.codescan.project import AuditedProject, retired_suppressions
from lup.harness.codescan.registry import RULE_REFERENCE
from lup.policy.kernel.edit import (
    IGNORE_RE,
    SUPPRESSION_COLUMN_LIMIT,
    inline_suppression,
    relocated_suppressions,
    standalone_suppression,
)
from lup.policy.kernel.roles import path_role
from lup.policy.kernel.rows import ResolutionRow
from lup.devtools.dev.pyright_oracle import default_oracle
from lup.devtools.dev.tracked import tracked_files
from lup.devtools.project import DevProject
from lup.devtools.utils import output_json


class FoundAntiPattern(AntiPatternFinding):
    """An :class:`~lup.harness.codescan.antipatterns.AntiPatternFinding` tagged with its file."""

    file: str


class FoundRefutation(Refutation, frozen=True):
    """A :class:`~lup.harness.codescan.common.Refutation` tagged with its file."""

    file: str


class ScannedFile(BaseModel, arbitrary_types_allowed=True):
    """One tracked file the sweep reads once and audits against its table."""

    rel: str
    path: Path
    patterns: list[AntiPattern]
    text: str


class AntiPatternScan(BaseModel):
    """One repository sweep: the findings it kept, and the ones it refuted."""

    findings: list[FoundAntiPattern]
    refuted: list[FoundRefutation]


def within_scope(rel: str, paths: Sequence[str] | None) -> bool:
    """Whether one repository-relative path is inside a sweep's scope.

    ``None`` is the whole repository. An empty scope is a scope rather than an
    absent one, so a tree that changed nothing is read for nothing rather than
    read entirely. A named path covers itself and everything beneath it, so a
    caller scopes by file or by directory without having to say which it meant.

    Both sides are compared as paths rather than as text, so the spelling a
    shell completes a directory to — with a trailing separator — names the same
    scope as the one without it. Comparing the strings made those two spellings
    different scopes, and the one a completion produces matched nothing: the
    sweep then reported a clean tree for a directory full of findings, which
    reads as an answer rather than as a scope that caught no files.

    This is what keeps a lease answerable for its own concern. A lease holds
    one concern's changes and `dev check` judges it; reading the whole
    repository made that verdict depend on state no worker in the run
    controls, so one finding nobody introduced blocked every lease at once
    with no revision able to converge on it.
    """
    if paths is None:
        return True

    def scoped(path: str) -> PurePosixPath:
        """One scope as the walk spells the files it is compared against.

        Those come from `git ls-files`, which names them relative to the
        repository, and the walk runs where that command runs. An absolute
        path names the same file in a spelling the other side never takes, so
        it matches nothing and the sweep reports a clean tree — an answer,
        rather than a scope that caught no files. That is the same failure a
        trailing separator causes, reached from the other direction.

        One outside this checkout keeps its spelling and goes on matching
        nothing, which is not the same bug: it names a file the sweep is not
        answerable for, and no relative form of it would be true.
        """
        named = Path(path)
        if not named.is_absolute():
            return PurePosixPath(path)
        try:
            return PurePosixPath(named.resolve().relative_to(Path.cwd().resolve()))
        except ValueError:
            return PurePosixPath(path)

    subject = PurePosixPath(rel)
    return any(subject.is_relative_to(scoped(path)) for path in paths)


def declared_rules(project: DevProject) -> RuleSet:
    """The rule set this project actually holds itself to, in one place.

    The tables this library ships plus whatever the project added, less
    whatever it turned off. Both the walk that decides which rules a file is
    read against and the resolution that decides which of them a checker is
    asked about read this, because a rule the project switched off is one no
    language server should be spending a session on.
    """
    return RuleSet(python=[*PYTHON_ANTI_PATTERNS, *project.anti_patterns]).selected(
        project.rules
    )


def scanned_files(
    project: DevProject, paths: Sequence[str] | None = None
) -> list[ScannedFile]:
    """Every tracked production file the audits read, with its table and text.

    ``paths`` narrows the walk to files under the given repository-relative
    prefixes; ``None`` is the whole repository, and an empty scope is a scope
    rather than an absent one, so a tree that changed nothing is read for
    nothing. The declared path roles decide the rest: a rule the edit hook
    never enforces in a test or scratch tree is not read there either.
    """
    roles = project.path_roles
    declared = declared_rules(project)

    def found() -> Iterator[ScannedFile]:
        for rel in tracked_files(others=True):
            path = Path(rel)
            patterns = patterns_for_suffix(path.suffix.lower(), declared)
            if patterns is None or path_role(rel, roles) != "production":
                continue
            if not within_scope(rel, paths):
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            yield ScannedFile(rel=rel, path=path, patterns=patterns, text=text)

    return list(found())


def scan_antipatterns(
    project: DevProject, paths: Sequence[str] | None = None
) -> AntiPatternScan:
    """Every missing/spurious marker across tracked production `.py`/TS files.

    ``paths`` narrows the sweep to the files under the given repository-relative
    prefixes, for the fix-one-file loop where a whole-repository resolve is the
    dominant cost, and for a caller answerable only for what it changed. It
    decides which files are reported and type-checked. Declaration-based rules
    retain the whole project's class index, and the oracle resolves against
    the whole project, so a scoped verdict matches the sweep's verdict for that
    file. ``None`` is the whole repository; naming no path scopes the sweep to
    nothing, which is what a tree that changed nothing is answerable for.

    The audit reads the same declared path roles the edit hook does, so a rule
    the hook never enforces in a test or scratch tree is not reported there
    either. A gate that fires only after the fact is a gate agents cannot act
    on.

    The typed grammar resolves receivers before the tables run, so a finding
    a type oracle proved the rule is not about never reaches a verdict — and
    a `# lup: ignore` that guarded one becomes the dead directive the audit
    reports. Resolution costs a language-server session and `dev check` pays
    it every run; where the checker is absent the grammar refutes nothing and
    every broad regex verdict stands.
    """
    if paths is not None and not paths:
        return AntiPatternScan(findings=[], refuted=[])
    all_scanned = scanned_files(project)
    scanned = [item for item in all_scanned if within_scope(item.rel, paths)]
    declaration_sources = [
        PythonSource(
            path=item.path,
            module=module_name(item.path),
            text=item.text,
        )
        for item in all_scanned
        if item.path.suffix.lower() in {".py", ".pyi"}
    ]
    sources = [
        source
        for source in declaration_sources
        if within_scope(source.path.as_posix(), paths)
    ]
    # A scope holding no production file has nothing any rule would judge, and
    # the post-write review asks about every file a call wrote -- tests and
    # scratch scripts among them -- so the project's context is not built.
    if paths is not None and not scanned and not sources:
        return AntiPatternScan(findings=[], refuted=[])
    # A whole-repository sweep remembers what the checker said, because it
    # holds every module a refutation could resolve through and can therefore
    # key an answer by all of them. A scoped sweep holds only the files it was
    # asked about, so that key would be blind to a module it resolves through
    # and could not notice one changing — and a scoped run is the cheap one
    # anyway, since asking only about what changed is what remembering is for.
    resolved_rules = declared_rules(project).python
    resolving_refutations = (
        partial(
            remembered_refutations,
            sources,
            default_oracle(),
            resolved_rules,
            refutation_cache_path(),
            project_root(),
        )
        if paths is None
        else partial(refute, sources, default_oracle(), resolved_rules)
    )
    # The resolve spends most of its time waiting on a language server, and
    # every audit below waits on nothing, so the sweep reads while it waits
    # rather than after it. Only the text audit reads a refutation; the rest
    # are assembled in their own order once both halves are in.
    # Every project rule the set holds, over the whole tree: the class index
    # and the names a library's callers can replace are properties of the
    # project, so a scoped sweep hands the rules everything to read and names
    # the files it asks them to judge.
    audited = AuditedProject(
        sources=declaration_sources,
        application=project.roots,
        boundaries=project.resolved_import_boundaries(),
        judged=None if paths is None else [source.path for source in sources],
    )
    with ThreadPoolExecutor(max_workers=1) as pool:
        resolving = pool.submit(resolving_refutations)
        declared = [
            finding
            for rule in declared_rules(project).project
            for finding in rule.audit(audited)
            if within_scope(finding.path.as_posix(), paths)
        ]
        refuted = resolving.result()

    # A bare directive a project rule reports as untyped is covering that
    # rule's violation, so the line audit's reading of the same directive —
    # a bare marker on a line no line rule trips — is not the dead directive
    # it looks like from there.
    covering = {
        (finding.path.as_posix(), finding.line)
        for finding in declared
        if finding.kind == "untyped"
    }
    results = [
        FoundAntiPattern(file=item.rel, **finding.model_dump())
        for item in scanned
        for finding in audit_text(
            item.text,
            item.patterns,
            refuted[item.path.as_posix()] if item.path.as_posix() in refuted else None,
            typescript=item.path.suffix.lower() in TYPESCRIPT_SUFFIXES,
        )
        if not (
            finding.kind == "spurious"
            and not finding.rule_id
            and (item.rel, finding.line) in covering
        )
    ]
    results.extend(
        FoundAntiPattern(
            file=finding.path.as_posix(),
            kind=finding.kind,
            line=finding.line,
            text=finding.text,
            message=finding.message,
            rule_id=finding.rule_id,
        )
        for finding in declared
    )
    return AntiPatternScan(
        findings=[
            finding for finding in results if project.rules.keeps(finding.rule_id)
        ],
        refuted=[
            FoundRefutation(file=file, **refutation.model_dump())
            for file, refutations in sorted(refuted.items())
            for refutation in refutations
        ],
    )


class DirectiveSite(BaseModel, frozen=True):
    """One `# lup: ignore`, where it sits and how wide each placement makes it.

    ``inline_width`` is what the canonical placement would cost: the width of
    the line as written when the directive is already on it, and the width of
    the merged line when the directive stands above one. A site with nothing
    beneath it to merge into reports its own width, because there is no
    inline form of it to measure.
    """

    file: str
    line: int
    standalone: bool
    width: int
    inline_width: int
    text: str


def directive_sites(rel: str, text: str) -> list[DirectiveSite]:
    """Every real directive in one file, measured against both placements.

    The file-level opt-out is skipped: it governs the whole file rather than
    a line, so neither placement is about it.
    """
    context = PythonContext.parse(text)
    file_ignore = file_level_ignore(text)
    lines = text.splitlines()

    def found() -> Iterator[DirectiveSite]:
        for number, line in enumerate(lines, start=1):
            match = IGNORE_RE.search(line)
            if match is None or not context.comment_at(number, match.start()):
                continue
            if file_ignore is not None and number == file_ignore.line:
                continue
            standalone = standalone_suppression(line) is not None
            below = lines[number] if standalone and number < len(lines) else ""
            merged = inline_suppression(below, line) if below.strip() else line
            yield DirectiveSite(
                file=rel,
                line=number,
                standalone=standalone,
                width=len(line),
                inline_width=len(merged) if standalone else len(line),
                text=line.strip(),
            )

    return list(found())


def place_directives(
    project: DevProject, limit: int = SUPPRESSION_COLUMN_LIMIT
) -> list[str]:
    """Put every directive at its canonical placement, reporting each file moved.

    Nothing the audit decides turns on this: both placements are accepted, so
    the move only settles which one a file uses. It exists so no author weighs
    a reason against a column count — the reason gets written, and whichever
    placement holds it whole is the one it lands in.
    """

    def moved() -> Iterator[str]:
        for item in scanned_files(project):
            if item.path.suffix.lower() not in {".py", ".pyi"}:
                continue
            revised = relocated_suppressions(item.text, limit)
            if revised == item.text:
                continue
            item.path.write_text(revised, encoding="utf-8")
            yield item.rel

    return list(moved())


class RetiredDirectiveFile(BaseModel, frozen=True):
    """One file a rule's retirement changed, and where it changed it."""

    rel: str
    removed: list[int] = []


def retire_directives(project: DevProject, rule_id: str) -> list[RetiredDirectiveFile]:
    """Stop every tracked file from naming a rule this project has retired.

    A rule that stops running leaves its directives covering nothing, and the
    audit reports each one as spurious — so retiring a rule and sweeping its
    directives are one operation rather than two, and doing the second by
    hand across a tree is how a reason gets deleted along with the id that
    justified it. Each file reports the lines it lost, so the sweep can be
    read rather than trusted.
    """

    def swept() -> Iterator[RetiredDirectiveFile]:
        for item in scanned_files(project):
            if item.path.suffix.lower() not in {".py", ".pyi"}:
                continue
            source = PythonSource(
                path=item.path,
                module=module_name(item.path),
                text=item.text,
            )
            revised = retired_suppressions(source, rule_id)
            if revised.text == item.text:
                continue
            item.path.write_text(revised.text, encoding="utf-8")
            yield RetiredDirectiveFile(rel=item.rel, removed=revised.removed)

    return list(swept())


class RepairedDirective(BaseModel, frozen=True):
    """One dead directive the sweep took out rather than reporting."""

    file: str
    line: int
    rule_id: str


def repair_spurious(
    project: DevProject, findings: Sequence[FoundAntiPattern]
) -> list[RepairedDirective]:
    """Delete the directives the audit calls spurious, and say which went.

    A spurious finding is the one class of finding whose fix is not a
    judgement. "Missing" says a line trips a rule and somebody has to decide
    whether the rule is right or the line is; "untyped" says a directive needs
    the reason nobody has written yet. "Spurious" says a directive guards
    something that is not there -- there is one correct edit, this is it, and
    printing it for a human to perform is asking for typing rather than for a
    decision.

    Deleted per site rather than per rule. The same id can be dead on one line
    and live on another in the same file, and :func:`retired_suppressions`
    narrowed to the audited line is what keeps repairing the first from
    stranding the second.

    Files are rewritten once each, with every site they hold applied in one
    pass, because each rewrite shifts the lines under it -- applied one at a
    time against re-read text, the second finding in a file would name a line
    that has moved.
    """
    by_file: dict[str, list[FoundAntiPattern]] = defaultdict(list)
    for finding in findings:
        if finding.kind == "spurious":
            by_file[finding.file].append(finding)
    if not by_file:
        return []

    def repaired() -> Iterator[RepairedDirective]:
        for item in scanned_files(project):
            if item.rel not in by_file or item.path.suffix.lower() not in {
                ".py",
                ".pyi",
            }:
                continue
            text = item.text
            # Descending, so a rewrite that drops a line cannot move a site
            # this file has not reached yet.
            for finding in sorted(by_file[item.rel], key=lambda f: -f.line):
                source = PythonSource(
                    path=item.path,
                    module=module_name(item.path),
                    text=text,
                )
                revised = retired_suppressions(
                    source, finding.rule_id, at={finding.line}
                )
                if revised.text == text:
                    continue
                text = revised.text
                yield RepairedDirective(
                    file=item.rel, line=finding.line, rule_id=finding.rule_id
                )
            if text != item.text:
                item.path.write_text(text, encoding="utf-8")

    return list(repaired())


def report_directives(
    project: DevProject, as_json: bool, limit: int = SUPPRESSION_COLUMN_LIMIT
) -> None:
    """Measure every directive in the tree against the canonical placement.

    What the report answers is whether the canonical inline placement can
    actually hold a directive and its reason: how many sites fit the column
    budget inline, and how many only fit standing above. A tree whose
    overflow concentrates in the longest reasons is a tree whose fallback is
    carrying the justifications that say the most.
    """
    sites = [
        site
        for item in scanned_files(project)
        for site in directive_sites(item.rel, item.text)
    ]
    inline = [site for site in sites if not site.standalone]
    above = [site for site in sites if site.standalone]
    overflow = [site for site in sites if site.inline_width > limit]
    if as_json:
        output_json(
            {
                "limit": limit,
                "total": len(sites),
                "inline": len(inline),
                "inline_overflowing": sum(1 for s in inline if s.inline_width > limit),
                "above": len(above),
                "above_fitting_inline": sum(
                    1 for s in above if s.inline_width <= limit
                ),
                "sites": [site.model_dump() for site in sites],
            }
        )
        return
    typer.echo(f"{len(sites)} directive(s), budget {limit} columns")
    typer.echo(
        f"  inline: {len(inline)} — "
        f"{sum(1 for s in inline if s.inline_width <= limit)} fit, "
        f"{sum(1 for s in inline if s.inline_width > limit)} already over"
    )
    typer.echo(
        f"  above:  {len(above)} — "
        f"{sum(1 for s in above if s.inline_width <= limit)} would fit inline, "
        f"{sum(1 for s in above if s.inline_width > limit)} only fit above"
    )
    for site in sorted(overflow, key=lambda site: site.inline_width, reverse=True):
        typer.echo(f"{site.file}:{site.line} [{site.inline_width}] {site.text}")


PROFILE_ROWS = 40
"""Rows a profile prints, as a default a caller may raise."""


def profile(
    project: DevProject,
    paths: Sequence[str] | None = None,
    rows: int = PROFILE_ROWS,
) -> None:
    """Run one sweep under a profiler and report where its time went.

    The sweep reads every production file several times over — a parse per
    audit, then a walk per rule, then a language server resolving what the
    walk selected — and which of those dominates decides whether the answer
    is to parse once, to read fewer files, or to remember the last answer.
    A stopwatch around the whole command cannot tell them apart; this can.
    """
    profiler = cProfile.Profile()
    profiler.enable()
    scan_antipatterns(project, paths)
    profiler.disable()
    pstats.Stats(profiler).sort_stats("cumulative").print_stats(rows)


ADVISORY_KINDS = {"untyped"}


def summarize(
    project: DevProject,
    as_json: bool,
    paths: Sequence[str] | None = None,
    advisory: AbstractSet[str] = ADVISORY_KINDS,
) -> None:
    """Tally anti-pattern findings by rule and kind — the sweep triage view.

    A per-rule count (most-frequent first, with how many files each spans) and
    a per-kind split, so a cleanup targets the noisiest rules first instead of
    reading the whole listing, plus how many findings the typed grammar
    refuted. ``--json`` emits the same tally for tooling.
    """
    scan = scan_antipatterns(project, paths)
    found = scan.findings
    by_rule: Counter[str] = Counter()
    by_kind: Counter[str] = Counter()
    files_by_rule: defaultdict[str, list[str]] = defaultdict(list)
    for finding in found:
        rule = finding.rule_id or "(bare)"
        by_rule[rule] += 1
        by_kind[finding.kind] += 1
        if finding.file not in files_by_rule[rule]:
            files_by_rule[rule].append(finding.file)

    blocking = sum(1 for finding in found if finding.kind not in advisory)
    file_count = len(dict.fromkeys(finding.file for finding in found))

    if as_json:
        output_json(
            {
                "total": len(found),
                "blocking": blocking,
                "files": file_count,
                "refuted": len(scan.refuted),
                "by_kind": dict(by_kind),
                "by_rule": {
                    rule: {"count": count, "files": len(files_by_rule[rule])}
                    for rule, count in by_rule.most_common()
                },
            }
        )
        return

    if not found:
        typer.echo("No anti-pattern findings.")
        return
    typer.echo(
        f"{len(found)} findings ({blocking} blocking) across {file_count} file(s)"
    )
    typer.echo("  by kind: " + ", ".join(f"{k}={v}" for k, v in by_kind.most_common()))
    typer.echo("  by rule:")
    for rule, count in by_rule.most_common():
        typer.echo(f"    {count:5}  {rule}  ({len(files_by_rule[rule])} file(s))")
    if scan.refuted:
        typer.echo(f"  refuted by receiver type: {len(scan.refuted)}")
    typer.echo(f"Rule reference: {RULE_REFERENCE} (`uv run lup-devtools dev rules`)")


def report(
    project: DevProject,
    as_json: bool,
    paths: Sequence[str] | None = None,
    advisory: AbstractSet[str] = ADVISORY_KINDS,
    fix: bool = False,
    refutations: bool = False,
) -> None:
    """List anti-pattern findings; exit non-zero when a blocking one remains.

    "untyped" findings are advisory (a bare `# lup: ignore` to migrate to a
    typed one) and never fail the command; "missing" and "spurious" do, and
    are listed first. Every finding the typed grammar refuted is counted, and
    with *refutations* listed with the declaration that settled it, so a
    dropped verdict is accountable rather than invisible
    (:func:`report_lines`).

    ``fix`` takes out the dead directives instead of printing them, and the
    sweep runs again over what it wrote so the report is about the tree as it
    now stands rather than as it was found. The second sweep is what makes
    this honest: deleting a directive can uncover a violation it was sitting
    on top of, and a repair pass that printed its own pre-repair findings
    would claim to have fixed something it had just moved.
    """
    scan = scan_antipatterns(project, paths)
    repaired: list[RepairedDirective] = []
    if fix:
        repaired = repair_spurious(project, scan.findings)
        # Under --json the same report is a payload key. Echoing it here as
        # well would put prose in front of the document, which is the one
        # thing a caller reading this as data cannot recover from.
        for item in repaired if not as_json else []:
            named = f"[{item.rule_id}]" if item.rule_id else ""
            typer.echo(
                f"{item.file}:{item.line} [repaired] removed dead `# lup: ignore{named}`"
            )
        if repaired:
            if not as_json:
                typer.echo(f"{len(repaired)} dead directive(s) removed\n")
            scan = scan_antipatterns(project, paths)
    blocking = [finding for finding in scan.findings if finding.kind not in advisory]
    if as_json:
        output_json(
            {
                "findings": [finding.model_dump() for finding in scan.findings],
                "refuted": [refutation.model_dump() for refutation in scan.refuted],
                "repaired": [item.model_dump() for item in repaired],
            }
        )
        if blocking:
            raise typer.Exit(1)
        return
    for line in report_lines(scan, advisory, refutations):
        typer.echo(line)
    if blocking:
        raise typer.Exit(1)


def report_lines(
    scan: AntiPatternScan, advisory: AbstractSet[str], refutations: bool = False
) -> list[str]:
    """What a sweep's listing says, in the order it is acted on.

    The findings that fail the run come first, then the advisory ones.
    What the typed grammar refuted or could not settle fails nothing, so it
    is counted unless *refutations* asks for each: listed ahead of the
    findings, one adopter's 260 such lines buried the 85 that blocked, and
    the agent tidying them filtered the listing before it could begin.
    """
    blocking = [finding for finding in scan.findings if finding.kind not in advisory]
    advised = [finding for finding in scan.findings if finding.kind in advisory]
    listed = [
        line
        for finding in [*blocking, *advised]
        for line in (
            f"{finding.file}:{finding.line} [{finding.kind}] {finding.message}",
            f"    {finding.text}",
        )
    ]
    settled = [
        f"{refutation.file}:{refutation.line} "
        f"[{'refuted' if refutation.settled else 'unresolved'} {refutation.rule_id}] "
        f"{refutation.evidence}"
        for refutation in scan.refuted
    ]
    counted = (
        [
            f"{len(scan.refuted)} refuted or unresolved by receiver type, which "
            "fail nothing — `--refutations` lists each"
        ]
        if scan.refuted and not refutations
        else []
    )
    if not scan.findings:
        return [*(settled if refutations else counted), "No anti-pattern findings."]
    files = {finding.file for finding in scan.findings}
    tail = f" (+{len(advised)} untyped, advisory)" if advised else ""
    return [
        *listed,
        *(settled if refutations else []),
        "",
        f"{len(blocking)} blocking finding(s){tail} in {len(files)} file(s)",
        *counted,
        f"Rule reference: {RULE_REFERENCE} (`uv run lup-devtools dev rules`)",
    ]


def report_refutations(project: DevProject, path: Path, text: str) -> None:
    """Emit what resolution refutes in one file's proposed content.

    The edit gate's answer for a rule whose verdict turns on a declaration.
    It holds the text before anything is written, so what is on disk is not
    what is being judged, and every sweep entry point beside this one reads
    files — which is the one thing that cannot answer here.

    ``resolved`` is the whole difference between a verdict and a question. A
    gate told nothing cannot tell the defect from the shape the rule permits
    and has to ask rather than refuse, so a session with no language server
    must not report an empty refutation: that reads as "resolved, and nothing
    was refuted", which is the one wrong answer of the three available.

    The refutations go out as the kernel's own resolution row, settled and
    unsettled apart, because the gate reads them oppositely: a directive on a
    refuted line is dead, one on an unresolved line stands. This is the same
    `refute` the sweep runs, so what the gate is told is what `dev check`
    will say — the hook may know less than the sweep, and knowing less comes
    out here as unresolved, never as refuted.
    """
    oracle = default_oracle()
    if oracle is None:
        output_json({"resolved": False, **ResolutionRow(refuted={}, unresolved={})})
        return
    source = PythonSource(path=path, module=module_name(path), text=text)
    found = refute([source], oracle, declared_rules(project).python)
    rows = found[path.as_posix()] if path.as_posix() in found else []

    def lines_where(settled: bool) -> dict[str, list[int]]:
        """Each rule's refuted lines, on the side of the verdict named."""
        chosen = [row for row in rows if row.settled is settled]
        return {
            rule: [row.line for row in chosen if row.rule_id == rule]
            for rule in dict.fromkeys(row.rule_id for row in chosen)
        }

    output_json(
        {
            "resolved": True,
            **ResolutionRow(refuted=lines_where(True), unresolved=lines_where(False)),
        }
    )
