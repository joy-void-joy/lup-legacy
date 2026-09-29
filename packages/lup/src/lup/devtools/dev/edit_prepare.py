"""Prepare one audited edit proposal without writing its target files."""

import ast
import difflib
from itertools import groupby
from pathlib import Path
from typing import Literal

import typer
from pydantic import BaseModel, Field, TypeAdapter, field_validator

from lup.devtools.dev.antipatterns import (
    ScannedFile,
    declared_rules,
    scanned_files,
    scanned_roots,
)
from lup.devtools.dev.pyright_oracle import default_oracle
from lup.devtools.project import DevProject
from lup.devtools.utils import output_json
from lup.harness.codescan.antipatterns import RuleSet, audit_text
from lup.harness.codescan.common import (
    PythonContext,
    PythonSource,
    Refutation,
    module_name,
)
from lup.harness.codescan.oracle import TypeOracle
from lup.harness.codescan.project import AuditedProject
from lup.harness.codescan.resolution import refute, resolved_sites
from lup.harness.enforcement import declared_path_rules, declared_role_rows
from lup.harness.models import HookSet
from lup.policy.edit_rules import erase_edit_rules
from lup.policy.kernel.edit import (
    covering_suppression_line,
    decide_edit,
    ignore_rule_ids,
    relocated_suppressions,
)
from lup.policy.kernel.roles import path_role
from lup.policy.kernel.rows import ResolutionRow
from lup.policy.kernel.typescript import TYPESCRIPT_SUFFIXES
from lup.policy.models import EditBatch, EditChange
from lup.policy.rules import antipattern_row, path_rule_row
from lup.providers.harness import patch_review


class SuppressionRequest(BaseModel, frozen=True, extra="forbid"):
    """One exception explicitly requested against a proposed document's line."""

    path: Path
    line: int = Field(ge=1)
    rule_id: str = Field(min_length=1)
    reason: str = Field(min_length=1)

    @field_validator("reason")
    @classmethod
    def single_line_reason(cls, value: str) -> str:
        if not value.strip() or len(value.splitlines()) != 1:
            raise ValueError("a suppression needs a nonempty, single-line reason")
        return value.strip()


class EditFinding(BaseModel, frozen=True):
    """One complete candidate finding, with whether an exception is justified."""

    path: Path
    line: int
    rule_id: str
    kind: str
    message: str
    suppressible: bool = False


class CandidateAudit(BaseModel, frozen=True):
    """The audit and type evidence for all documents in one proposal."""

    findings: list[EditFinding]
    refutations: dict[Path, list[Refutation]]
    resolved: bool


class EditReading(BaseModel, frozen=True):
    """The canonical edit gate's answer for a candidate file."""

    path: Path
    effect: Literal["allow", "ask", "deny", "defer"]
    reason: str
    recovery: str


class PreparedEdit(BaseModel, frozen=True):
    """A complete proposal and its evidence, never an authorization receipt."""

    batch: EditBatch
    findings: list[EditFinding]
    readings: list[EditReading]
    patch: str | None = None


def concrete_batch(batch: EditBatch, root: Path) -> EditBatch:
    """Resolve local targets and reject stale or ambiguous preimages."""
    if batch.cwd is not None and batch.cwd.resolve() != root:
        raise ValueError("EditBatch.cwd must name the current checkout")
    changes = [
        change.model_copy(update={"path": (root / change.path).resolve()})
        for change in batch.changes
    ]
    if len({change.path for change in changes}) != len(changes):
        raise ValueError("each target must occur exactly once in an EditBatch")
    for change in changes:
        if not change.path.is_relative_to(root):
            raise ValueError(f"{change.path} is outside this checkout")
        try:
            current = change.path.read_bytes().decode("utf-8")
        except FileNotFoundError:
            current = None
        if current != change.before:
            raise ValueError(
                f"Edit preimage does not match {change.path}; read it again"
            )
        if current is None and change.after is None:
            raise ValueError(f"{change.path} has no before or after document")
        if change.after is not None and change.path.suffix.lower() in {".py", ".pyi"}:
            ast.parse(change.after, filename=str(change.path))
        if (
            change.operation == "create"
            and current is not None
            or change.operation == "overwrite"
            and current is None
            or change.operation == "delete"
            and change.after is not None
            or change.operation in {"create", "overwrite"}
            and change.after is None
        ):
            raise ValueError(f"{change.path}: operation disagrees with its documents")
    return EditBatch(
        changes=[
            change.model_copy(
                update={
                    "operation": "delete"
                    if change.after is None
                    else "create"
                    if change.before is None
                    else change.operation
                }
            )
            for change in changes
        ],
        cwd=root,
    )


def audit_candidate(
    batch: EditBatch,
    project: DevProject,
    rules: RuleSet,
    oracle: TypeOracle | None,
    existing: list[ScannedFile],
) -> CandidateAudit:
    """Run every source rule against staged documents and their shared context."""
    root = batch.cwd or Path.cwd()
    changed = {change.path.relative_to(root): change for change in batch.changes}
    staged = [item for item in existing if item.path not in changed]
    staged.extend(
        ScannedFile(
            rel=path.as_posix(), path=path, text=change.after, patterns=patterns
        )
        for path, change in changed.items()
        if change.after is not None
        and path_role(path.as_posix(), project.path_roles) == "production"
        and (patterns := rules.for_suffix(path.suffix.lower())) is not None
    )
    sources = [
        PythonSource(
            path=item.path,
            module=module_name(item.path, scanned_roots(project)),
            text=item.text,
        )
        for item in staged
        if item.path.suffix.lower() in {".py", ".pyi"}
    ]
    candidates = [source for source in sources if source.path in changed]
    resolution_sources = [
        *candidates,
        *(
            PythonSource(
                path=path, module=module_name(path, scanned_roots(project)), text=""
            )
            for path, change in changed.items()
            if change.after is None
            and path.suffix.lower() in {".py", ".pyi"}
            and path_role(path.as_posix(), project.path_roles) == "production"
        ),
    ]
    refutations = refute(resolution_sources, oracle, rules.python)
    if oracle is None:
        for selected in resolved_sites(candidates, rules.python):
            refutations.setdefault(selected.file, []).append(
                Refutation(
                    rule_id=selected.rule.id,
                    line=selected.line,
                    subject=selected.subject,
                    evidence="type resolution is unavailable",
                    settled=False,
                )
            )
    audited = AuditedProject(
        sources=sources,
        application=project.roots,
        boundaries=project.resolved_import_boundaries(),
    )
    declared = [
        EditFinding(
            path=finding.path,
            line=finding.line,
            rule_id=finding.rule_id,
            kind=finding.kind,
            message=finding.message,
            suppressible=finding.kind == "missing" and rule.strength == "soft",
        )
        for rule in rules.project
        for finding in rule.audit(audited)
        if finding.path in changed
    ]
    covering = {
        (finding.path, finding.line)
        for finding in declared
        if finding.kind == "untyped"
    }
    findings = [
        EditFinding(
            path=item.path,
            line=finding.line,
            rule_id=finding.rule_id,
            kind=finding.kind,
            message=finding.message,
            suppressible=finding.kind == "missing"
            and any(
                rule.id == finding.rule_id and rule.strength == "soft"
                for rule in item.patterns
            ),
        )
        for item in staged
        if item.path in changed
        for finding in audit_text(
            item.text,
            item.patterns,
            refutations[item.rel] if item.rel in refutations else [],
            typescript=item.path.suffix.lower() in TYPESCRIPT_SUFFIXES,
        )
        if not (
            finding.kind == "spurious"
            and not finding.rule_id
            and (item.path, finding.line) in covering
        )
    ]
    findings.extend(declared)
    findings.extend(
        EditFinding(
            path=Path(path),
            line=row.line,
            rule_id=row.rule_id,
            kind="unresolved",
            message=row.evidence,
        )
        for path, rows in refutations.items()
        for row in rows
        if not row.settled
    )
    return CandidateAudit(
        findings=findings,
        refutations={Path(path): rows for path, rows in refutations.items()},
        resolved=oracle is not None,
    )


def requested_suppressions(
    batch: EditBatch, requests: list[SuppressionRequest], audit: CandidateAudit
) -> EditBatch:
    """Insert only explicit, substantiated Python exceptions and preserve semantics."""
    root = batch.cwd or Path.cwd()
    requests = [
        request.model_copy(update={"path": (root / request.path).resolve()})
        for request in requests
    ]
    for request in requests:
        if any(
            root / finding.path == request.path
            and finding.line == request.line
            and finding.rule_id == request.rule_id
            and finding.kind == "unresolved"
            for finding in audit.findings
        ):
            raise ValueError(
                f"{request.path}:{request.line}: rule evidence is unresolved"
            )
        if not any(
            root / finding.path == request.path
            and finding.line == request.line
            and finding.rule_id == request.rule_id
            and finding.suppressible
            for finding in audit.findings
        ):
            raise ValueError(
                f"{request.path}:{request.line}: {request.rule_id} is not a proven,"
                " suppressible missing directive"
            )

    def revised(change: EditChange) -> EditChange:
        wanted = [request for request in requests if request.path == change.path]
        if not wanted:
            return change
        if change.after is None or change.path.suffix.lower() not in {".py", ".pyi"}:
            raise ValueError("explicit insertion requires a Python after document")
        lines = change.after.splitlines()
        ending = "\n" if change.after.endswith("\n") else ""
        if "\n".join(lines) + ending != change.after:
            raise ValueError(
                "suppression insertion cannot preserve non-LF line endings"
            )
        original = ast.parse(change.after, type_comments=True)
        context = PythonContext.parse(change.after)
        if context.comment_columns is None:
            raise ValueError(f"{change.path} cannot be tokenized")

        def holder_for(request: SuppressionRequest) -> int:
            return (
                covering_suppression_line(lines, request.line, context.comment_columns)
                or request.line
            )

        grouped = groupby(sorted(wanted, key=holder_for), key=holder_for)
        for number, group in grouped:
            additions = list(group)
            line = lines[number - 1]
            column = (
                context.comment_columns[number]
                if number in context.comment_columns
                else len(line)
            )
            marker = context.suppression_at(number, line)
            existing_ids = ignore_rule_ids(marker) if marker is not None else []
            if existing_ids is None:
                raise ValueError("a bare suppression cannot be extended as typed")
            ids = sorted({*existing_ids, *(request.rule_id for request in additions)})
            reasons = "; ".join(
                f"{request.rule_id}: {request.reason}" for request in additions
            )
            retained = line[marker.end() :] if marker is not None else line[column:]
            directive = f"# lup: ignore[{', '.join(ids)}] — {reasons}"
            if retained.strip():
                directive += f"; {retained.strip()}"
            prefix = line[:column]
            separator = "  " if prefix.strip() else ""
            lines[number - 1] = (
                f"{prefix.rstrip() if prefix.strip() else prefix}{separator}{directive}"
            )
        text = "\n".join(lines) + ending
        text = relocated_suppressions(text)
        if ast.dump(ast.parse(text, type_comments=True)) != ast.dump(original):
            raise ValueError(
                f"suppression insertion changes Python semantics: {change.path}"
            )
        return change.model_copy(update={"after": text})

    return batch.model_copy(
        update={"changes": [revised(change) for change in batch.changes]}
    )


def candidate_readings(
    batch: EditBatch, hooks: HookSet, rules: RuleSet, audit: CandidateAudit
) -> list[EditReading]:
    """Apply the declared edit lattice to the same typed documents as the audit."""
    root = batch.cwd or Path.cwd()

    def reading_for(change: EditChange) -> EditReading:
        path = change.path.relative_to(root)
        rows = audit.refutations[path] if path in audit.refutations else []
        resolution = ResolutionRow(
            refuted={
                rule: [row.line for row in rows if row.rule_id == rule and row.settled]
                for rule in {row.rule_id for row in rows}
            },
            unresolved={
                rule: [
                    row.line for row in rows if row.rule_id == rule and not row.settled
                ]
                for rule in {row.rule_id for row in rows}
            },
        )
        verdict = decide_edit(
            path.as_posix(),
            change.before,
            change.after,
            path_exists=change.before is not None,
            path_rules=[path_rule_row(rule) for rule in declared_path_rules(hooks)],
            antipattern_rows=[
                antipattern_row(rule)
                for rule in rules.for_suffix(path.suffix.lower()) or []
            ],
            path_roles=declared_role_rows(hooks.path_roles),
            python_source=path.suffix.lower() in {".py", ".pyi"},
            acceptance_guard=hooks.acceptance_guard.erased()
            if hooks.acceptance_guard
            else None,
            resolution=resolution if audit.resolved else None,
            suffix=path.suffix.lower(),
            operation=change.operation,
            edit_rules=erase_edit_rules(hooks.resolved_edit_rules()),
            import_boundaries=[
                boundary.erased() for boundary in hooks.resolved_import_boundaries()
            ],
        )
        return EditReading(
            path=path,
            effect=verdict.effect,
            reason=verdict.reason,
            recovery=verdict.recovery,
        )

    return [reading_for(change) for change in batch.changes]


def native_patch(batch: EditBatch) -> str:
    """Encode one native patch and prove its decoded file transitions are exact."""
    root = batch.cwd or Path.cwd()
    changed = [change for change in batch.changes if change.before != change.after]
    if not changed:
        raise ValueError("the batch contains no changed documents")

    def with_context(context: int) -> str:
        lines = ["*** Begin Patch"]
        for change in changed:
            path = change.path.as_posix()
            if "\n" in path or "\r" in path:
                raise ValueError("native patch paths cannot contain newlines")
            match change:
                case EditChange(after=None):
                    lines.append(f"*** Delete File: {path}")
                case (
                    EditChange(before=None, after=str() as after)
                    | EditChange(operation="overwrite", after=str() as after)
                ):
                    lines.append(f"*** Add File: {path}")
                    lines.extend(f"+{line}" for line in after.splitlines())
                case EditChange(before=str() as before, after=str() as after):
                    old, new = before.splitlines(), after.splitlines()
                    lines.append(f"*** Update File: {path}")
                    if old == new:
                        lines.append("@@")
                        lines.extend(f"-{line}" for line in old)
                        lines.extend(f"+{line}" for line in new)
                        continue
                    groups = difflib.SequenceMatcher(a=old, b=new, autojunk=False)
                    for group in groups.get_grouped_opcodes(context):
                        lines.append("@@")
                        for tag, start, stop, revised_start, revised_stop in group:
                            if tag == "equal":
                                lines.extend(f" {line}" for line in old[start:stop])
                            if tag in {"replace", "delete"}:
                                lines.extend(f"-{line}" for line in old[start:stop])
                            if tag in {"replace", "insert"}:
                                lines.extend(
                                    f"+{line}"
                                    for line in new[revised_start:revised_stop]
                                )
        return "\n".join([*lines, "*** End Patch", ""])

    expected = [(row.path, row.before, row.after, row.operation) for row in changed]
    complete_context = max(
        len((change.before or "").splitlines()) for change in changed
    )
    for context in [3, complete_context]:
        patch = with_context(context)
        decoded = patch_review(
            patch, root, {change.path: change.before for change in changed}, False
        )
        actual = [(row.path, row.before, row.after, row.operation()) for row in decoded]
        if actual == expected:
            return patch
    raise ValueError(
        "native patch cannot preserve these documents exactly; check empty creates,"
        " newline termination, and line endings"
    )


def prepare_edit(
    batch: EditBatch,
    project: DevProject,
    hooks: HookSet,
    oracle: TypeOracle | None,
    existing: list[ScannedFile],
    requests: list[SuppressionRequest],
) -> PreparedEdit:
    """Audit a complete candidate, optionally insert requested exceptions, then encode."""
    batch = concrete_batch(batch, Path.cwd().resolve())
    rules = declared_rules(project)
    audit = audit_candidate(batch, project, rules, oracle, existing)
    if requests:
        batch = requested_suppressions(batch, requests, audit)
        audit = audit_candidate(batch, project, rules, oracle, existing)
    readings = candidate_readings(batch, hooks, rules, audit)
    batch = concrete_batch(batch, Path.cwd().resolve())
    blocked = any(row.kind in {"missing", "spurious"} for row in audit.findings) or any(
        row.effect == "deny" for row in readings
    )
    return PreparedEdit(
        batch=batch,
        findings=audit.findings,
        readings=readings,
        patch=None if blocked else native_patch(batch),
    )


def run(
    document: Path,
    output: Path,
    suppressions: Path | None,
    project: DevProject,
    hooks: HookSet,
    as_json: bool,
) -> None:
    """Write only a fresh scratch artifact; leave targets and review queues untouched."""
    root = Path.cwd().resolve()
    output = output.resolve()
    try:
        if (
            not output.is_relative_to(root)
            or path_role(output.relative_to(root).as_posix(), project.path_roles)
            != "scratch"
        ):
            raise ValueError("--output must be inside a declared scratch path")
        if output.exists():
            raise ValueError("--output already exists; choose a fresh artifact path")
        batch = EditBatch.model_validate_json(document.read_text(encoding="utf-8"))
        if any(
            output.is_relative_to((root / change.path).resolve())
            for change in batch.changes
        ):
            raise ValueError("--output cannot be a proposed edit target or beneath one")
        requests = (
            TypeAdapter(list[SuppressionRequest]).validate_json(
                suppressions.read_text(encoding="utf-8")
            )
            if suppressions is not None
            else []
        )
        prepared = prepare_edit(
            batch, project, hooks, default_oracle(), scanned_files(project), requests
        )
        if prepared.patch is not None:
            output.parent.mkdir(parents=True, exist_ok=True)
            with output.open("x", encoding="utf-8", newline="") as stream:
                stream.write(prepared.patch)
    except (OSError, ValueError, SyntaxError) as error:
        raise typer.BadParameter(str(error)) from error
    if as_json:
        output_json(prepared)
    else:
        for finding in prepared.findings:
            typer.echo(
                f"{finding.path}:{finding.line}: {finding.kind} {finding.rule_id}: {finding.message}"
            )
        for reading in prepared.readings:
            typer.echo(f"{reading.effect} {reading.path}: {reading.reason}")
        if prepared.patch is not None:
            typer.echo(
                f"Prepared {output}. Submit its exact contents to apply_patch once."
            )
            typer.echo(
                "No review is queued by this preview. If submission reports a pending review, await its answer and retry exactly; do not add an escalation."
            )
    if prepared.patch is None:
        raise typer.Exit(1)
