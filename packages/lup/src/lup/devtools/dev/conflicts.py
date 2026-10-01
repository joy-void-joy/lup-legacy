# lup: ignore[import-re, re-call]
# Diff hunks are semi-structured text with no parser — significance detection
# is alternation over diff lines, so the regex rules are opted out file-wide.
"""Conflict scope classification, audit, and completion for merge/rebase conflicts.

After a failed merge or rebase, classifies conflicted files as in-scope
(touched by this branch) or out-of-scope (only changed on the other side).

The `conflicts`, `conflict_status`, `conflict_audit`, and `conflict_complete`
entry points back the `lup-devtools git conflict` subcommands wired in
`lup.devtools.dev.app`.

Alone among this project's commands these are spelled without ``uv run``,
which parses ``pyproject.toml`` before it will run anything: the moment the
conflict is *in* that file, ``uv`` refuses to start and the whole conflict
toolchain withdraws exactly where the manifest that configures the project is
at stake. :func:`lup.devtools.launcher.launcher_invocation` names the console
script in the project's own environment, which imports this package directly
and reads no manifest.

Examples, spelled as a project keeping its environment inside the checkout
reaches them; one keeping it anywhere else gets the bare name instead::

    $ .venv/bin/lup-devtools git conflict list
    $ .venv/bin/lup-devtools git conflict list --json
    $ .venv/bin/lup-devtools git conflict status --json
    $ .venv/bin/lup-devtools git conflict audit src/lup/agent/core.py --json
    $ .venv/bin/lup-devtools git conflict complete --dry-run
"""

import logging
import os
import re
from pathlib import Path
from collections.abc import Iterator
from typing import TypedDict

import sh
import typer
from pydantic import BaseModel

from lup.execution.git import Repository
from lup.devtools.launcher import (
    CONSOLE_SCRIPT,
    DEFAULT_ENVIRONMENT,
    launcher_invocation,
)
from lup.execution.shell import git
from lup.types import EnvVars
from lup.devtools.utils import (
    format_table,
    decode_stderr,
    output_json,
    short_sha,
)

logger = logging.getLogger(__name__)

# lup: ignore[constant-declaration] — what Python packaging calls the file
MANIFEST = "pyproject.toml"
"""The file ``uv`` must parse before it will run anything."""


DOCUMENTED_LAUNCHER = f"{DEFAULT_ENVIRONMENT}/bin/{CONSOLE_SCRIPT}"
"""The launcher the merge guidance names: the layout ``uv sync`` produces.

That guidance is prose and has to stay prose — an f-string in the prompt
would un-mask the whole document to the anti-pattern scanner — so it names
one spelling and says how to substitute for the environments that sit
elsewhere. This is that spelling, and it is what the test holding the two
together compares against.

Deliberately not :func:`lup.devtools.launcher.launcher_invocation`. That
answers for the machine it runs on, and a shared document pinned to it would
be correct for whoever generated it and failing for everyone else.
"""


def invocation(launcher: str, *words: str) -> str:
    """Spell one command so it starts while the manifest holds conflicts.

    The launcher is the caller's to decide, because the two callers want
    different ones: a notice printed into a live session names what will
    start *there*, and guidance shared by a project names the convention it
    documents.
    """
    return " ".join([launcher, *words])


def manifest_conflicted(root: Path) -> bool:
    """Whether a merge left markers in the manifest ``uv`` has to parse."""
    try:
        content = (root / MANIFEST).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return "<<<<<<<" in content and ">>>>>>>" in content


def conflicted_manifest_notice(root: Path) -> str:
    """What to run instead, once the toolchain notices it is mid-conflict.

    Named for the machine this is printed on rather than for the documented
    convention: the session reading it is about to run the command, so the
    spelling has to be one that starts here.
    """
    launcher = launcher_invocation(root)
    return (
        f"{MANIFEST} holds conflict markers, so `uv` cannot parse it and every "
        f"`uv run ...` command will fail to start until the merge is settled. "
        f"Reach this toolchain as `{launcher} ...` meanwhile — "
        f"`{invocation(launcher, 'git', 'conflict', 'status', '--json')}`."
    )


class ConflictFile(TypedDict):
    path: str
    conflict_count: int
    scope: str
    branch_touched: bool


class ConflictReport(TypedDict):
    state: str
    base: str
    files: list[ConflictFile]
    in_scope_count: int
    out_of_scope_count: int


def detect_conflict_state() -> str | None:
    """Detect whether we're in a merge, rebase, or cherry-pick."""
    repository = Repository(Path.cwd())
    if repository.merging() is not None:
        return "merge"
    git_dir = repository.git_dir()
    if (git_dir / "rebase-merge").is_dir() or (git_dir / "rebase-apply").is_dir():
        return "rebase"
    if (git_dir / "CHERRY_PICK_HEAD").exists():
        return "cherry-pick"
    return None


class BranchScope(BaseModel):
    """The merge base plus the files this branch touched since it."""

    base: str
    files: set[str]  # lup: ignore[set-shape] — membership-tested scope set


def get_branch_files(state: str) -> BranchScope:
    """The merge base and this branch's touched files, for scope classification."""
    repository = Repository(Path.cwd())
    git_dir = repository.git_dir()

    def ref_file(path: Path) -> str:
        return path.read_text().strip()

    match state:
        case "merge":
            merge_head = repository.merging()
            if merge_head is None:
                typer.echo("No merge is in progress", err=True)
                raise typer.Exit(1)
            base = git.out("merge-base", "HEAD", merge_head)
            tip = "HEAD"

        case "rebase":
            rebase_merge = git_dir / "rebase-merge"
            rebase_apply = git_dir / "rebase-apply"
            try:
                match (rebase_merge.exists(), rebase_apply.exists()):
                    case (True, _):
                        onto = ref_file(rebase_merge / "onto")
                        orig_head = ref_file(rebase_merge / "head")
                    case (False, True):
                        onto = ref_file(rebase_apply / "onto")
                        orig_head = ref_file(rebase_apply / "orig-head")
                    case _:
                        typer.echo("Cannot determine rebase state", err=True)
                        raise typer.Exit(1)
            except OSError as e:
                typer.echo(f"Cannot read rebase state: {e}", err=True)
                raise typer.Exit(1) from e
            base = git.out("merge-base", orig_head, onto)
            tip = orig_head

        case "cherry-pick":
            cherry_head = git.out("rev-parse", "CHERRY_PICK_HEAD")
            base = git.out("merge-base", "HEAD", cherry_head)
            tip = "HEAD"

        case _:
            typer.echo(f"Unknown conflict state: {state}", err=True)
            raise typer.Exit(1)

    touched = git.lines("diff", "--name-only", f"{base}..{tip}", _ok_code=[0])
    return BranchScope(base=base, files=set(touched))  # lup: ignore[set-shape]


def list_conflicted_files() -> list[str]:
    """List files with unresolved conflicts."""
    return [str(path) for path in Repository(Path.cwd()).conflicted()]


def count_conflict_markers(path: str) -> int:
    """Count the number of conflict marker blocks in a file."""
    try:
        content = Path(path).read_text(encoding="utf-8")
        return content.count("<<<<<<<")
    except OSError:
        return 0


def build_conflict_report(state: str) -> ConflictReport:
    """Build a structured conflict scope report."""
    conflicted = list_conflicted_files()
    scope = get_branch_files(state)

    files: list[ConflictFile] = [
        {
            "path": path,
            "conflict_count": count_conflict_markers(path),
            "scope": "in-scope" if path in scope.files else "out-of-scope",
            "branch_touched": path in scope.files,
        }
        for path in conflicted
    ]
    in_scope = sum(1 for f in files if f["branch_touched"])

    return {
        "state": state,
        "base": scope.base,
        "files": files,
        "in_scope_count": in_scope,
        "out_of_scope_count": len(files) - in_scope,
    }


def conflicts(as_json: bool) -> None:
    """Show conflicted files with scope classification."""
    state = detect_conflict_state()
    if not state:
        if as_json:
            output_json(
                ConflictReport(
                    state="none",
                    base="",
                    files=[],
                    in_scope_count=0,
                    out_of_scope_count=0,
                )
            )
        else:
            typer.echo("Not in a merge, rebase, or cherry-pick state")
        return

    conflicted = list_conflicted_files()
    if not conflicted:
        typer.echo("No conflicted files found")
        return

    report = build_conflict_report(state)

    if as_json:
        output_json(report)
        return

    typer.echo(f"\nConflict state: {report['state']}")
    typer.echo(f"Merge base: {short_sha(report['base'])}")
    rows = [(f["path"], str(f["conflict_count"]), f["scope"]) for f in report["files"]]
    typer.echo()
    typer.echo(
        format_table(
            ("File", "Conflicts", "Scope"),
            rows,
            aligns=("left", "right", "right"),
        )
    )

    typer.echo(
        f"\nIn-scope: {report['in_scope_count']}, "
        f"Out-of-scope: {report['out_of_scope_count']}"
    )


# -- Status, audit, and completion --


class ConflictStatusResult(BaseModel):
    operation: str
    conflicted_files: list[str]
    ours_ref: str
    theirs_ref: str
    ours_commits: list[str]
    theirs_commits: list[str]


class FileAuditResult(BaseModel):
    path: str
    ours_removals: list[str]
    theirs_removals: list[str]
    warning: bool
    partial: bool = False


class AuditResult(BaseModel):
    files: list[FileAuditResult]
    has_warnings: bool


SIGNIFICANT_PATTERN = re.compile(
    r"^-(def |class |async def |@app\.|@[a-z]+_tool|    def )"
)


def theirs_ref_for(operation: str) -> str:
    """Return the git ref for the *other* side of an in-progress operation.

    A rebase exposes the commit being replayed as ``REBASE_HEAD`` — not
    ``CHERRY_PICK_HEAD`` — so diffing against the wrong ref yields empty
    "theirs" diffs during a rebase.
    """
    match operation:
        case "merge":
            return "MERGE_HEAD"
        case "rebase":
            return "REBASE_HEAD"
        case _:
            return "CHERRY_PICK_HEAD"


def extract_removals(diff_output: str) -> list[str]:
    """Find removed functions/classes/decorators in a diff."""
    return [
        line.removeprefix("-").strip()
        for line in diff_output.splitlines()
        if SIGNIFICANT_PATTERN.match(line)
    ]


def conflict_status(as_json: bool) -> None:
    """Detect conflict state, list files, and show both sides' history."""
    operation = detect_conflict_state()

    if operation is None:
        if as_json:
            result = ConflictStatusResult(
                operation="none",
                conflicted_files=[],
                ours_ref="HEAD",
                theirs_ref="",
                ours_commits=[],
                theirs_commits=[],
            )
            output_json(result)
        else:
            typer.echo("No merge/rebase/cherry-pick in progress")
        return

    conflicted = list_conflicted_files()

    ours_ref = "HEAD"
    theirs_ref = theirs_ref_for(operation)

    def log_range(base: str, tip: str) -> list[str]:
        rows = git.lines("log", "--oneline", f"{base}..{tip}", _ok_code=[0])
        return [r for r in rows if r]

    try:
        merge_base = git.out("merge-base", "HEAD", theirs_ref, _ok_code=[0])
        ours_commits = log_range(merge_base, "HEAD")
        theirs_commits = log_range(merge_base, theirs_ref)
    except sh.ErrorReturnCode:
        logger.warning("No shared history with %s; showing bare status", theirs_ref)
        ours_commits = []
        theirs_commits = []

    result = ConflictStatusResult(
        operation=operation,
        conflicted_files=conflicted,
        ours_ref=ours_ref,
        theirs_ref=theirs_ref,
        ours_commits=ours_commits,
        theirs_commits=theirs_commits,
    )

    if as_json:
        output_json(result)
    else:
        typer.echo(f"Operation: {operation}")
        typer.echo(f"Conflicted files ({len(conflicted)}):")
        for f in conflicted:
            typer.echo(f"  {f}")
        if ours_commits:
            typer.echo(f"\nOurs ({ours_ref}):")
            for c in ours_commits:
                typer.echo(f"  {c}")
        if theirs_commits:
            typer.echo(f"\nTheirs ({theirs_ref}):")
            for c in theirs_commits:
                typer.echo(f"  {c}")


def conflict_audit(files: list[str], as_json: bool) -> None:
    """Post-resolution deletion audit: check for accidentally dropped code."""
    operation = detect_conflict_state()
    if operation is None:
        typer.echo("No merge/rebase/cherry-pick in progress", err=True)
        raise typer.Exit(1)

    theirs_ref = theirs_ref_for(operation)

    def audit_file(path: str) -> FileAuditResult:
        ours_diff = git.out("diff", "HEAD", "--", path, _ok_code=[0, 1])
        ours_removals = extract_removals(ours_diff)

        theirs_diff = ""
        partial = False
        try:
            theirs_diff = git.out("diff", theirs_ref, "--", path, _ok_code=[0, 1])
        except sh.ErrorReturnCode as e:
            partial = True
            typer.echo(
                f"Warning: could not diff {path} against {theirs_ref} — "
                f"theirs-side audit is partial ({decode_stderr(e)})",
                err=True,
            )
        theirs_removals = extract_removals(theirs_diff)

        return FileAuditResult(
            path=path,
            ours_removals=ours_removals,
            theirs_removals=theirs_removals,
            warning=bool(ours_removals or theirs_removals),
            partial=partial,
        )

    file_results = [audit_file(path) for path in files]

    audit_result = AuditResult(
        files=file_results,
        has_warnings=any(f.warning for f in file_results),
    )

    if as_json:
        output_json(audit_result)
    else:
        for f in file_results:
            status = "WARNING" if f.warning else "OK"
            if f.partial:
                status += " (partial)"
            typer.echo(f"  {f.path}: {status}")
            for r in f.ours_removals:
                typer.echo(f"    - [ours] {r}")
            for r in f.theirs_removals:
                typer.echo(f"    - [theirs] {r}")

        if audit_result.has_warnings:
            typer.echo("\nSome files have removals — review before completing.")


def conflict_complete(dry_run: bool) -> None:
    """Finalize the merge/rebase/cherry-pick after all conflicts are resolved."""
    operation = detect_conflict_state()
    if operation is None:
        typer.echo("No merge/rebase/cherry-pick in progress")
        return

    remaining = list_conflicted_files()
    if remaining:
        typer.echo(f"Error: {len(remaining)} conflicted file(s) remain:", err=True)
        for f in remaining:
            typer.echo(f"  {f}", err=True)
        raise typer.Exit(1)

    match operation:
        case "merge":
            cmd_desc = "git commit --no-edit"
        case "rebase":
            cmd_desc = "git rebase --continue"
        case "cherry-pick":
            cmd_desc = "git cherry-pick --continue"
        case _:
            typer.echo(f"Error: unknown operation {operation!r}", err=True)
            raise typer.Exit(1)

    if dry_run:
        typer.echo(f"Would run: {cmd_desc}")
        return

    try:
        match operation:
            case "merge":
                git("commit", "--no-edit")
            case "rebase":
                git("rebase", "--continue")
            case "cherry-pick":
                git("cherry-pick", "--continue")
        typer.echo(f"Completed {operation}")
    except sh.ErrorReturnCode as e:
        typer.echo(f"Failed to complete {operation}: {decode_stderr(e)}", err=True)
        raise typer.Exit(1)


def conflict_blocks(
    text: str,
    opening: str = "<<<<<<< ",
    middle: str = "=======",
    closing: str = ">>>>>>> ",
    excused_by: str = "lup: ignore[conflict-marker]",
) -> list[int]:
    """The line each conflict block a merge left in this text opens on.

    A merge committed its markers into a markdown passage and the page
    generated from it, and the whole gate passed: nothing it runs reads
    markdown for them. A block is the three markers git writes, in order —
    a line opening with *opening*, a *middle* line, a line opening with
    *closing* — and any one alone is reachable in honest text: a setext
    heading underlines with ``=======``, a docstring quotes a marker.

    The one exemption is a marker in the file, never a path: a line carrying
    *excused_by* excuses the blocks opening in the paragraph it heads, up to
    the next blank line. A fixture holding a conflict on purpose writes it
    inside a string, where no comment can share the marker's line, so the
    paragraph is how far the marker has to reach — and no farther, so a real
    conflict elsewhere in that file is still found.
    """
    lines = text.splitlines()

    def opened() -> Iterator[int]:
        start = 0
        divided = False
        excused = False
        for number, line in enumerate(lines, start=1):
            match line.strip():
                case "":
                    excused = False
                case str() if excused_by in line:
                    excused = True
                case str() if line.startswith(opening):
                    start, divided = (0 if excused else number), False
                case str() if start and line.rstrip() == middle:
                    divided = True
                case str() if start and divided and line.startswith(closing):
                    yield start
                    start = 0

    return list(opened())


def committing(index: Path | None) -> EnvVars | None:
    """The environment git reads a commit's own index through, or ``None`` for the checkout's.

    `git commit -a` and `git commit <path>` commit from an index git makes for
    the purpose, not the checkout's, and name it to their hooks only through
    ``GIT_INDEX_FILE`` -- so a reader of what the commit holds is handed the
    same name.
    """
    if index is None:
        return None
    # lup: ignore[os-environ] — inherited, not read: git keeps its PATH and
    # configuration, and the one name added is the index the commit is made from
    return {**os.environ, "GIT_INDEX_FILE": str(index)}


def staged_text(root: Path, index: Path | None, path: str) -> str | None:
    """A file's text as the index being committed holds it, which is what a commit records.

    ``index`` is that index where it is not the checkout's own
    (:func:`committing`), and ``None`` reads the checkout's.
    """
    try:
        return str(
            git(
                "-C",
                str(root),
                "show",
                f":{path}",
                _tty_out=False,
                _env=committing(index),
            )
        )
    except (sh.ErrorReturnCode, UnicodeDecodeError):
        return None


def staged_paths(root: Path, index: Path | None = None) -> list[str]:
    """Every path the next commit adds or changes, read from the index it is made from."""
    named = git.lines(
        "-C",
        str(root),
        "diff",
        "--cached",
        "--name-only",
        "--diff-filter=ACMR",
        _env=committing(index),
    )
    return [path for path in named if path]
