"""Scan tracked files for inline markers.

Backs two `lup-devtools dev` commands (wired in `lup.devtools.dev.app`):

- `dev comments` lists unresolved `# lup:` / `// lup:` feedback notes, with
  deferred (`defer:`) notes in their own section;
  `report`, `commit_prompts`, and `clear_markers` back its default listing
  and its `--commit` / `--clear` modes.
- `dev todos` lists `# lup: template:` customization markers — the template's
  domain decision points that `/lup:init` walks one by one. An alias for
  `dev comments --kind template`, which is the same scan narrowed to that
  flavor rather than a second scanner with a convention of its own.

Examples::

    $ uv run lup-devtools dev comments
    $ uv run lup-devtools dev comments --json
    $ uv run lup-devtools dev comments --commit
    $ uv run lup-devtools dev comments --clear path/to/file.py:42
    $ uv run lup-devtools dev todos --json
"""

from collections import defaultdict
from collections.abc import Callable
from pathlib import Path

import sh
import typer

from lup.execution.git import Repository
from lup.harness.codescan.markers import (
    MarkerComment,
    NoteKind,
    NoteTarget,
    find_feedback,
    remove_notes,
    restore_claims,
    retire_claims,
    scan_mode_for,
)
from lup.devtools.dev.tracked import tracked_files
from lup.devtools.utils import decode_stderr, output_json, refuse
from lup.execution.shell import git
from lup.policy.kernel.diagnostic import devtools, step


class FoundComment(MarkerComment):
    """One scanned note located in its tracked file, with read context."""

    file: str
    context: str

    def location_line(self) -> str:
        """This note's span and read window, the listing's per-note header."""
        return (
            f"{self.file}:{self.start_line}-{self.end_line}  "
            f"(read {self.read_start}-{self.read_end})"
        )


def scan_tracked(
    find: Callable[[str, str], list[MarkerComment]],
) -> list[FoundComment]:
    """Run one marker scan across every tracked text file.

    `find` maps a file's text and its scan mode to the markers it holds —
    :func:`lup.harness.codescan.markers.find_feedback` for review notes, or
    :func:`lup.harness.codescan.markers.find_markers` bound to another convention.
    """
    results: list[FoundComment] = []  # lup: ignore[empty-collection] — scan fold
    for rel in tracked_files():
        path = Path(rel)
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        lines = text.splitlines()
        for comment in find(text, scan_mode_for(path)):
            context = "\n".join(lines[comment.read_start - 1 : comment.read_end])
            results.append(
                FoundComment(file=rel, context=context, **comment.model_dump())
            )
    return results


def targets_by_file(targets: list[str]) -> dict[str, list[int]]:
    """Group `file:line` arguments per file, reporting malformed ones."""
    by_file: defaultdict[str, list[int]] = defaultdict(list)
    for target in targets:
        rel, _, line_str = target.rpartition(":")  # lup: ignore[string-split] — CLI arg
        if not rel or not line_str.isdigit():
            typer.echo(f"Skipping malformed target: {target}", err=True)
            continue
        if int(line_str) not in by_file[rel]:
            by_file[rel].append(int(line_str))
    return dict(by_file)


def revise_claims(
    targets: list[str], *, retire: bool, narrow: str | None = None
) -> None:
    """Retire or restore `# lup: solved:` claims at `file:line` targets.

    The verify-solved pass's instrument: the edit gate denies changing a
    claim marker in any session, so confirmation and restoration go through
    this command instead — loud in the transcript, visible in the diff, and
    shape-restricted to claims. A target landing on open feedback, parked
    work, or no note at all is refused rather than touched.
    """
    failed = False
    for rel, wanted in targets_by_file(targets).items():
        path = Path(rel)
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            typer.echo(f"Skipping unreadable file: {rel}", err=True)
            failed = True
            continue
        note_targets = [NoteTarget(line=line_no) for line_no in wanted]
        revision = (
            retire_claims(text, scan_mode_for(path), note_targets)
            if retire
            else restore_claims(text, scan_mode_for(path), note_targets, narrow)
        )
        for target in revision.missing:
            typer.echo(f"No solved claim at {rel}:{target.line}", err=True)
        for note in revision.refused:
            typer.echo(
                f"Refusing {rel}:{note.start_line}: a {note.kind} note is "
                "not a solved claim",
                err=True,
            )
        failed = failed or bool(revision.missing or revision.refused)
        if revision.revised:
            path.write_text(revision.text, encoding="utf-8")
        verb = "Retired" if retire else "Restored"
        typer.echo(f"{verb} {len(revision.revised)} claim(s) in {rel}")
    if failed:
        raise typer.Exit(1)


def clear_markers(targets: list[str], *, wake: bool = False) -> None:
    """Remove specific feedback markers named as `file:line` targets.

    This is the `file:line` door onto :func:`remove_notes`; the resolver's
    own clearance path calls that primitive directly, carrying note text so
    its match survives drift. A standalone comment line is dropped whole; an
    inline trailing marker keeps its code and loses only the comment.

    A `defer` note is parked work, not open feedback: a target that lands on
    one is skipped unless *wake* is set, so a concern sweeping its own notes
    can never strip a deferral it was not asked to wake.

    Refuses to run unless HEAD is a disposable `resolve/*` branch, so a note
    can never be silently stripped from a real checkout — there, a note is
    removed only through an `Edit` (which prompts for review) or a reviewed
    merge of a resolve branch.
    """
    branch = Repository(Path.cwd()).branch() or "HEAD"
    if not branch.startswith("resolve/"):
        refuse(
            "is checked out, not a resolve/* branch, and markers are cleared only "
            "inside a disposable resolve worktree",
            what=branch,
            steps=[
                step("on a real checkout, edit the note, which prompts"),
                step("or merge a resolve branch"),
            ],
        )

    for rel, wanted in targets_by_file(targets).items():
        path = Path(rel)
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            typer.echo(f"Skipping unreadable file: {rel}", err=True)
            continue
        removal = remove_notes(
            text,
            scan_mode_for(path),
            [NoteTarget(line=line_no) for line_no in wanted],
            wake=wake,
        )
        for target in removal.missing:
            typer.echo(f"No clearable marker at {rel}:{target.line}", err=True)
        path.write_text(removal.text, encoding="utf-8")
        typer.echo(f"Cleared {len(removal.removed)} marker(s) from {rel}")


def withdraw_notes(targets: list[str], reason: str) -> None:
    """Retract notes that should not have been written, recording why.

    Conversion answers a note that was asked and addressed. It is the wrong
    shape for one that was mistaken — writing `# lup: solved:` in front of a
    question nobody needed answered claims work that never happened, and the
    verify pass then checks a claim against an ask that was never real. So
    withdrawal is its own act, and the reason is mandatory: what the edit
    gate refuses is a note vanishing with nothing left to review, and a
    recorded reason is exactly what that absence otherwise lacks.

    Naming a `file:line` withdraws whatever note sits there, deferrals
    included — a parked task can be mistaken the same way, and the target
    was named deliberately with a reason attached.

    The removal is committed on its own so the note's words and the reason
    for dropping them stay together in history. Files carrying other
    uncommitted work are refused rather than swept into that commit.
    """
    wanted = targets_by_file(targets)
    if not wanted:
        refuse(
            "no notes are named to withdraw",
            steps=[
                step(
                    "name each as file:line",
                    devtools(
                        "dev",
                        "comments",
                        "--withdraw",
                        "<file>:<line>",
                        "--reason",
                        "<why>",
                    ),
                )
            ],
            code=2,
        )
    dirty = [rel for rel in wanted if git.out("diff", "--", rel).strip()]
    if dirty:
        refuse(
            "hold uncommitted changes, and a withdrawal is committed by itself so "
            "its reason stays attached to the note it removed",
            what=", ".join(sorted(dirty)),
            steps=[step("commit or stash the other work first")],
        )

    withdrawn: list[str] = []
    for rel, lines in wanted.items():
        path = Path(rel)
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            typer.echo(f"Skipping unreadable file: {rel}", err=True)
            continue
        removal = remove_notes(
            text,
            scan_mode_for(path),
            [NoteTarget(line=line_no) for line_no in lines],
            wake=True,
        )
        for target in removal.missing:
            typer.echo(f"No note to withdraw at {rel}:{target.line}", err=True)
        if not removal.removed:
            continue
        path.write_text(removal.text, encoding="utf-8")
        withdrawn.extend(
            f"{rel}:{note.start_line} {note.marker_text()}" for note in removal.removed
        )
    if not withdrawn:
        typer.echo("No notes withdrawn.")
        return
    git("add", "--", *sorted(wanted))
    body = "\n".join(withdrawn)
    subject = f"chore(review): withdraw {len(withdrawn)} inline note(s)"
    git("commit", "-m", f"{subject}\n\n{reason}\n\n{body}")
    typer.echo(f"Withdrew {len(withdrawn)} note(s):\n{body}")


def commit_prompts() -> None:
    """Snapshot the current feedback prompts into one commit before resolving them."""
    found = scan_tracked(find_feedback)
    if not found:
        typer.echo("No # lup: comments to commit.")
        return
    files = sorted({comment.file for comment in found})
    git("add", "--", *files)
    body = "\n".join(
        f"{comment.file}:{comment.start_line} {comment.marker_text()}"
        for comment in found
    )
    subject = f"chore(review): {len(found)} inline feedback prompt(s)"
    try:
        git("commit", "-m", f"{subject}\n\n{body}")
    except sh.ErrorReturnCode as e:
        typer.echo(f"Nothing committed: {decode_stderr(e)}")
        return
    typer.echo(f"Committed {len(found)} prompt(s) across {len(files)} file(s).")


def render(found: list[FoundComment], *, as_json: bool, empty: str) -> None:
    """Print one scan's results as a listing or JSON (same shape either way).

    Deferred notes and resolution claims each get their own section, so
    parked work never blends into open feedback and a claim waiting to be
    checked is never mistaken for one somebody has checked.
    """
    if as_json:
        output_json([comment.model_dump() for comment in found])
        return
    if not found:
        typer.echo(empty)
        return
    deferred = [comment for comment in found if comment.kind == "defer"]
    solved = [comment for comment in found if comment.kind == "solved"]
    customization = [comment for comment in found if comment.kind == "template"]
    for comment in found:
        if comment.kind != "note":
            continue
        typer.echo(comment.location_line())
        typer.echo(f"    {comment.text}")
    if deferred:
        typer.echo("\nDeferred — parked until explicitly woken:")
        for comment in deferred:
            typer.echo(comment.location_line())
            typer.echo(f"    {comment.marker_text()}")
    if solved:
        typer.echo("\nClaimed resolved — awaiting review of each claim:")
        for comment in solved:
            typer.echo(comment.location_line())
            typer.echo(f"    solved: {comment.text}")
    if customization:
        typer.echo("\nCustomization points — decisions this scaffold leaves open:")
        for comment in customization:
            typer.echo(comment.location_line())
            typer.echo(f"    {comment.text}")
    files = {comment.file for comment in found}
    summary = f"\n{len(found)} comment(s) in {len(files)} file(s)"
    counted = [
        f"{len(deferred)} deferred" if deferred else "",
        f"{len(solved)} claimed" if solved else "",
        f"{len(customization)} customization" if customization else "",
    ]
    stated = [item for item in counted if item]
    if stated:
        summary += " (" + ", ".join(stated) + ")"
    typer.echo(summary)


def report(as_json: bool, commit: bool, kind: NoteKind | None = None) -> None:
    """List unresolved feedback comments; --json for tooling, --commit to snapshot them.

    `kind` narrows the listing to one flavor. Left unset, the listing is the
    repo's open feedback and excludes `template:` customization markers: those
    are the scaffold's product rather than anybody's outstanding request, and
    letting them join the count would put a permanent floor under it.
    """
    if commit:
        commit_prompts()
        return
    found = scan_tracked(find_feedback)
    selected = [
        note
        for note in found
        if note.kind == kind or (kind is None and note.kind != "template")
    ]
    match kind:
        case "template":
            empty = "No # lup: template: markers — no customization decisions pending."
        case _:
            empty = "No unresolved # lup: comments."
    render(selected, as_json=as_json, empty=empty)
