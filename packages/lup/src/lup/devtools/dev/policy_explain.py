"""Ask the live permission policy what it would decide, and why.

Tuning a vocabulary against a session's actual friction needs the verdict for
a spelling, not a reading of the table that produces it: the shell classifier
recurses through substitutions, loops, and redirections, resolves paths
against the filesystem, and consults four declared row sets, so which of them
answered is rarely obvious from the command alone.

This runs the same composition a session runs — ``semantic_policy_for`` over
the project's own ``HookSet`` — so a verdict here is the verdict there rather
than an approximation of it. The generated dispatchers reach the identical
kernel through their own compiled halves, which is why one answer covers both
the in-process session and the native plugin.
"""

import json
from pathlib import Path

import typer
from pydantic import AnyHttpUrl, BaseModel

from lup.devtools.utils import output_json
from lup.harness.enforcement import semantic_policy_for
from lup.harness.environment import Placement
from lup.harness.models import HookSet
from lup.policy.kernel.fetch import scope_text
from lup.policy.assets.host import (
    closed_deadline,
    contained,
    deadline_passed,
    defers_unjudged,
    delivers,
    measured_boundary,
    opened_deadline,
    text_at,
    unjudged_reason,
)
from lup.policy.bundle import hook_deadline
from lup.policy.kernel.lex import shell_write_targets, shell_written_targets
from lup.policy.kernel.semantics import UnjudgedAmbient
from lup.policy.models import EditBatch, EditChange, FetchUrl, ShellCommand
from lup.policy.rules import url_scope_row
from lup.policy.shell_rules import ShellCommandRule, erase_shell_rules
from lup.policy.survey import classify_forms, survey_shell_rules
from lup.types import StringMap

# Open keys only by spelling: the effects are lup's own closed set, but this
# reads them off a verdict rather than matching on them, so a caller may add
# a colour for an effect a later policy introduces.
EFFECT_STYLES: StringMap = {
    "allow": typer.colors.GREEN,
    "ask": typer.colors.YELLOW,
    "defer": typer.colors.BLUE,
    "deny": typer.colors.RED,
}
"""How each verdict reads at a glance, for a caller that does not say."""


class PolicyPlacement(BaseModel, frozen=True):
    """One placement a subject is read under, named as the launcher spells it.

    The two walls a launch can stand up are different facts and are set
    apart: ``sandboxed`` is the runtime's own per-call sandbox, which `inner`
    arms on the operator's machine, and ``contained`` is the container `outer`
    measures around the whole session. Read as one flag, the container's
    answer and the sandbox's were given under a single heading, and a question
    only the container settles could not be told from one both settle.
    """

    name: str
    sandboxed: bool
    contained: bool
    inside_placement: bool
    """Whether work is measured as placed inside the container: the term a
    container needs beside itself before it settles anything."""
    unjudged: UnjudgedAmbient | None = None
    """What legible work nothing judged answers uncontained; ``None`` is the
    composition's own answer, which every named placement reads."""
    host_executor: bool = False
    """Whether a host executor carries what has to run outside the boundary."""


PLACEMENTS: list[PolicyPlacement] = [
    PolicyPlacement(
        name="none", sandboxed=False, contained=False, inside_placement=False
    ),
    PolicyPlacement(
        name="inner", sandboxed=True, contained=False, inside_placement=False
    ),
    PolicyPlacement(
        name="outer", sandboxed=False, contained=True, inside_placement=True
    ),
]
"""Every placement a launch can stand up, whichever this process is in.

Placement is the single fact that moves the most verdicts -- an unclassified
command is settled by a boundary and refused without one, and a question whose
harm stays in a container is settled only by the container -- so a reader
asking about a session they have not launched names the one they mean.
"""


def session_placement(cwd: Path) -> PolicyPlacement:
    """The placement this session runs in, measured as its dispatcher measures it.

    The ledger its launch wrote says whether a container stands around it and
    places work inside, which posture answers legible work nothing judged, and
    whether a host executor carries what has to run outside; the runtime's own
    sandbox is the launcher's variable. So the answer is the one the running
    session meets, rather than one read under a placement somebody named. A
    runtime's own escape from its sandbox is an argument of one call, not a
    fact about the session, and is not read.
    """
    measured = measured_boundary(cwd)
    return PolicyPlacement(
        name="session",
        sandboxed=Placement.here().sandboxed,
        contained=contained(measured),
        inside_placement=delivers(measured, "inside_placement"),
        unjudged="defer" if defers_unjudged(measured) else "ask",
        host_executor=delivers(measured, "host_executor"),
    )


class PolicyReading(BaseModel, frozen=True):
    """What one placement's answer is, and why."""

    placement: str
    effect: str
    reason: str


class PolicyVerdict(BaseModel, frozen=True):
    """One classified input, read under every placement that could move it."""

    input: str
    kind: str
    readings: list[PolicyReading]
    assumed: list[str] = []
    """Session facts every reading supplied itself, where they could move one."""
    unavailable: list[str] = []
    """Edit facts a path-only preview cannot establish."""
    declared: list[str] = []
    """The fetch scopes a URL was read against, spelled as URLs they admit.

    Reference rather than verdict, and this is where reference is pulled from:
    the question a fetch outside every scope raises names the URL and nothing
    else, because a table repeated on every occurrence is read by nobody."""

    directory: str = ""
    """Where the session's commands start, which every path the input names
    was read against."""

    def settled(self) -> bool:
        """Whether placement changes nothing here, so one line says it all."""
        return len({reading.effect for reading in self.readings}) == 1

    def allows_anywhere(self) -> bool:
        """Whether any placement permits this, which is what an exit code says."""
        return any(reading.effect == "allow" for reading in self.readings)


def unresolved_facts(subject: str, kind: str, hooks: HookSet) -> list[str]:
    """Which session facts a reading of a bare command string had to assume.

    Only the ones that could move *this* subject. A line naming a fact that
    cannot reach the verdict teaches a reader to skip the line, which costs
    more than the line ever saves -- so a command that writes nothing says
    nothing, and the note appears exactly where the answer is soft.

    Every path the command writes is named, whichever spelling writes it --
    a redirection, a `cp` or `mv` destination, a write flag -- because the
    capture the reading assumed is what a dispatcher reads for each of them.
    """
    if kind != "shell":
        return []
    rows = erase_shell_rules(hooks.resolved_shell_rules())
    targets = list(
        dict.fromkeys(
            [*shell_write_targets(subject), *shell_written_targets(subject, rows)]
        )
    )
    if not targets:
        return []
    return [f"a capture holds {', '.join(targets)}"]


def concrete_edit_batch(document: Path, cwd: Path) -> EditBatch:
    """Read complete proposed documents and require their preimages still match."""
    batch = EditBatch.model_validate_json(document.read_text(encoding="utf-8"))
    changes = [
        change.model_copy(update={"path": (cwd / change.path).resolve()})
        for change in batch.changes
    ]
    for change in changes:
        current = text_at(cwd, str(change.path))
        if current != change.before:
            raise ValueError(
                f"Edit preimage does not match {change.path}; read the file again."
            )
    return EditBatch(changes=changes, cwd=cwd)


def read_under(
    subject: str,
    kind: str,
    placement: PolicyPlacement,
    autonomous: bool,
    cwd: Path,
    hooks: HookSet,
) -> PolicyReading:
    """Classify one input under one placement's composition of the policy.

    The placement decides containment as well as the native sandbox, the way
    the launcher's one flag does: `none` opens on the host with neither wall,
    `inner` stands the runtime's own sandbox up there, and `outer` stands a
    container up with the runtime's sandbox off inside it. A named placement
    takes both facts from its name rather than from the ledger this process
    runs behind, so a reading taken inside a contained session can say what
    happens without the container, and one taken on the host what happens
    inside one; :func:`session_placement` takes them from the ledger instead.

    What an `outer` reading measures, it measures here: which paths this
    machine's mount table lends from elsewhere, and which loopback ports a
    process out of sight holds. Inside a container those are the session's own
    answers; on a host they place the checkout as lent and little else.
    """
    policy = semantic_policy_for(
        hooks,
        sandbox_active=placement.sandboxed,
        escapable=placement.host_executor,
        autonomous=autonomous,
        recovered=True,
        contained=placement.contained,
        inside_placement=placement.inside_placement,
        unjudged_ambient=placement.unjudged,
    )
    match kind:
        case "shell":
            event = ShellCommand(command=subject, cwd=cwd)
        case "fetch":
            event = FetchUrl(url=AnyHttpUrl(subject))
        case "edit-batch":
            event = concrete_edit_batch(Path.cwd() / subject, cwd)
        case _:
            path = (cwd / subject).resolve()
            current = text_at(cwd, str(path))
            event = EditBatch(
                cwd=cwd,
                changes=[
                    EditChange(path=path, before=current or "", after=current or "")
                ],
            )
    # Bounded as the hook it previews is, from the same declaration: a
    # reading waiting on what never returns -- a walk of a whole disk -- is
    # refused at the deadline, as the dispatcher refuses the call.
    previous = opened_deadline(hook_deadline(hooks.policy_timeout))
    try:
        decision = policy.decide(event)
    except RuntimeError as overran:
        if not deadline_passed():
            raise
        return PolicyReading(
            placement=placement.name,
            effect="deny",
            reason=unjudged_reason(overran, True),
        )
    finally:
        closed_deadline(previous)
    return PolicyReading(
        placement=placement.name, effect=decision.effect, reason=decision.reason
    )


def verdict_for(
    subject: str,
    kind: str,
    autonomous: bool,
    cwd: Path,
    hooks: HookSet,
    placements: list[PolicyPlacement] = PLACEMENTS,
) -> PolicyVerdict:
    """Classify one input under each placement given, every named one unless told.

    ``edit`` is judged as a whole-file write of unchanged content, which is
    the shape that isolates the path gates from the anti-pattern and size
    ones: the question this command answers is whether a path may be written
    at all, not whether some particular diff passes review.

    Each reading carries the placement it was taken under, because
    containment settles an unclassified command inside the boundary and
    refuses it outside: an answer is only as good as knowing which session it
    described.

    **What no placement resolves, it says.** A session measures one fact per
    command that no reader of a bare string can: whether the snapshot in front
    of it succeeded, which exists only once a session takes it. That is
    answered optimistically here, because a reader asking what they will be
    asked about is better served by the common answer than by a pessimistic
    one they would learn to discount -- :func:`unresolved_facts` names it
    beside the verdict instead. What the launch mounted writable is not
    assumed: the policy reads the ledger the session's dispatcher reads.
    """
    return PolicyVerdict(
        input=subject,
        kind=kind,
        readings=[
            read_under(subject, kind, placement, autonomous, cwd, hooks)
            for placement in placements
        ],
        assumed=unresolved_facts(subject, kind, hooks),
        unavailable=(
            [
                "proposed content and operation: this is a path-only preview of unchanged content; use --kind edit-batch with a JSON EditBatch for a concrete verdict",
                *(
                    ["current file content: no readable preimage at this path"]
                    if text_at(cwd, subject) is None
                    else []
                ),
            ]
            if kind == "edit"
            else []
        ),
        declared=declared_scopes(kind, hooks),
        directory=str(cwd),
    )


def declared_scopes(kind: str, hooks: HookSet) -> list[str]:
    """Every fetch scope the declaration admits, for the one kind judged by them.

    Read off the same composition the verdict was read from, so the list a
    reader is shown is the list the classifier consulted rather than a second
    reading of the declaration.
    """
    if kind != "fetch":
        return []
    return [scope_text(url_scope_row(scope)) for scope in hooks.allowed_fetch]


def chosen_placements(
    name: str | None, cwd: Path, placements: list[PolicyPlacement] = PLACEMENTS
) -> list[PolicyPlacement]:
    """Which placement to read, given a caller who may have named one.

    ``None`` is this session's own, measured from the ledger its dispatcher
    reads, so the answer is the one the session running this command meets.
    A name reads that placement instead, whatever this session measured. A
    name no placement carries is refused with the names that exist.
    """
    if name is None:
        return [session_placement(cwd)]
    chosen = [placement for placement in placements if placement.name == name]
    if not chosen:
        known = ", ".join(placement.name for placement in placements)
        raise ValueError(f"no placement {name!r}: expected one of {known}")
    return chosen


def explain(
    subjects: list[str],
    kind: str,
    autonomous: bool,
    as_json: bool,
    hooks: HookSet,
    placement: str | None = None,
    styles: StringMap = EFFECT_STYLES,
    session: Path | None = None,
) -> None:
    """Print each subject's verdict, exiting non-zero when none of them allow.

    The verdict is this session's unless a placement is named. A subject
    every reading agrees on prints once, because repeating an answer to say
    it did not change is how a table teaches its reader to stop reading it.

    ``session`` is the directory the session's commands start from, which is
    what a dispatcher is handed and reads every path against: which checkout
    a capture holds, whether a destination is this checkout's or a sibling
    worktree's. Asked from somewhere else -- a `cd` into the worktree the
    change is in, or `uv run --directory` choosing whose code answers -- the
    same command reads as a different write, so the directory is named
    wherever it is not this process's own. ``None`` is this directory.
    """
    root = (session or Path.cwd()).resolve()
    if not as_json and root != Path.cwd().resolve():
        typer.echo(f"judged from {root}, where this session's commands start")
    try:
        verdicts = [
            verdict_for(
                subject,
                kind,
                autonomous,
                root,
                hooks,
                chosen_placements(placement, root),
            )
            for subject in subjects
        ]
    except (OSError, ValueError) as error:
        raise typer.BadParameter(str(error)) from error
    if as_json:
        output_json([verdict.model_dump() for verdict in verdicts])
        if not any(verdict.allows_anywhere() for verdict in verdicts):
            raise typer.Exit(1)
        return
    for verdict in verdicts:
        shown = verdict.readings[:1] if verdict.settled() else verdict.readings
        head = " / ".join(
            typer.style(reading.effect, fg=styles[reading.effect], bold=True)
            for reading in shown
        )
        typer.echo(f"{head}  {verdict.input}")
        for reading in shown:
            label = "" if verdict.settled() else f"{reading.placement}: "
            typer.echo(f"       {label}{reading.reason}")
        for assumed in verdict.assumed:
            typer.echo(f"       assuming {assumed}")
        for unavailable in verdict.unavailable:
            typer.echo(f"       unavailable {unavailable}")
        for scope in verdict.declared:
            typer.echo(f"       scope {scope}")
        # The declaration's answer, which a remembered approval overrides in
        # a session: said only under a question, since it moves nothing else.
        if any(reading.effect == "ask" for reading in shown):
            typer.echo(
                "       a matching approval may authorize an exact retry;"
                " use the runtime's review channel"
            )
    if not any(verdict.allows_anywhere() for verdict in verdicts):
        raise typer.Exit(1)


def survey(
    rules: list[ShellCommandRule],
    as_json: bool,
    output: Path | None,
    provenance: bool,
) -> None:
    """Print every command form the shell vocabulary declares, and its verdict.

    Reshaping a table is where verdicts move without anybody deciding to move
    them, so ``output`` exists to capture the whole table before a change and
    diff it against the same capture after — an equality no reading of the
    rules can establish.

    ``provenance`` answers the other question, one row per rule rather than one
    per form: which level of the nesting supplied each axis, so a verdict that
    was inherited from three levels up says so instead of being re-derived.
    """
    lines = (
        [rule.provenance() for rule in survey_shell_rules(rules)]
        if provenance
        else [
            json.dumps(form.model_dump(), sort_keys=True)
            if as_json
            else f"{form.effect:>5} {form.sandbox:>7}  {form.command}"
            for form in classify_forms(rules)
        ]
    )
    if output is None:
        for line in lines:
            typer.echo(line)
        return
    output.write_text("\n".join([*lines, ""]), encoding="utf-8")
    typer.echo(f"{len(lines)} line(s) written: {output}")
