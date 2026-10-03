"""Render a /lup:resolve run's manifest into one static HTML review page.

The execute workflow returns a manifest — one entry per concern with the
editor's summary, the verifier's verdict, and per-note findings. This module
joins that manifest with each branch's diff against the snapshot base and
renders a single self-contained page (inline CSS, no external assets), so the
human gate can read every concern's implementation before approving merges.
Exposed as ``lup-devtools resolve review``.
"""

import html
import json
from importlib import resources
from pathlib import Path

import typer
from pydantic import BaseModel, ValidationError

from lup.diagnostics import refuse
from lup.execution.shell import git

CSS = (
    resources.files("lup.devtools.dev")
    .joinpath("resolve_review.css")
    .read_text("utf-8")
)


class NoteRef(BaseModel):
    file: str
    line: int
    text: str


class NoteFinding(BaseModel):
    file: str
    line: int
    addressed: bool
    how: str


class ManifestEntry(BaseModel):
    """One concern's outcome, as returned by the execute workflow."""

    id: str
    title: str
    spec: str = ""
    branch: str | None = None
    committed: bool = False
    accepted: bool = False
    generalized: bool = False
    reason: str = ""
    residual: str = ""
    summary: str = ""
    files_changed: list[str] = []
    swept_beyond_scope: list[str] = []
    note_findings: list[NoteFinding] = []
    notes: list[NoteRef] = []

    def finding_for(self, note: NoteRef) -> NoteFinding:
        """What the verifier recorded about one of this concern's notes."""
        for finding in self.note_findings:
            if finding.file == note.file and finding.line == note.line:
                return finding
        return NoteFinding(
            file=note.file,
            line=note.line,
            addressed=False,
            how="(verifier recorded no finding for this note)",
        )


def load_manifest(path: Path) -> list[ManifestEntry]:
    """Read a manifest from a workflow task-output file or a bare JSON list.

    Accepts the three shapes a resolve run produces: the raw manifest array,
    ``{"manifest": [...]}``, and the task-output envelope
    ``{"result": {"manifest": [...]}}``.
    """
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        refuse(f"is not valid JSON: {e}", what=str(path), code=2)
    match parsed:
        case list():
            raw = parsed
        case {"manifest": list() as entries}:
            raw = entries
        case {"result": {"manifest": list() as entries}}:
            raw = entries
        case _:
            refuse("holds no manifest array", what=str(path), code=2)
    try:
        return [ManifestEntry.model_validate(entry) for entry in raw]
    except ValidationError as e:
        refuse(
            f"holds a manifest entry that does not validate: {e}",
            what=str(path),
            code=2,
        )


def summarize(manifest_path: Path) -> None:
    """Print each concern's verdict as a compact terminal listing.

    The quick pre-review triage: one block per concern with the editor/verifier
    flags, the verdict reason, and any residual — enough to plan merge order
    and approval batches before opening the full HTML review.
    """

    def prose(text: str, width: int) -> str:
        return " ".join(text.split())[:width]

    entries = load_manifest(manifest_path)
    for entry in entries:
        typer.echo(
            f"{entry.id}: committed={entry.committed} accepted={entry.accepted} "
            f"generalized={entry.generalized} files={len(entry.files_changed)} "
            f"swept={len(entry.swept_beyond_scope)}"
        )
        if entry.reason:
            typer.echo(f"  reason: {prose(entry.reason, 300)}")
        if entry.residual:
            typer.echo(f"  residual: {prose(entry.residual, 400)}")
    accepted = sum(1 for entry in entries if entry.accepted)
    typer.echo(f"\n{accepted}/{len(entries)} concerns verifier-accepted")


def render_diff(text: str) -> str:
    def render_line(line: str) -> str:
        esc = html.escape(line)
        if line.startswith(
            ("diff --git", "index ", "+++", "---", "new file", "deleted file")
        ):
            return f'<span class="fhead">{esc}</span>'
        if line.startswith("@@"):
            return f'<span class="hunk">{esc}</span>'
        if line.startswith("+"):
            return f'<span class="add">{esc}</span>'
        if line.startswith("-"):
            return f'<span class="del">{esc}</span>'
        return esc

    return "\n".join(render_line(line) for line in text.splitlines())


def render_concern(entry: ManifestEntry, base: str) -> str:
    branch = entry.branch or "(none)"
    badges: list[str] = []
    badges.append(
        '<span class="badge ok">verifier: accepted</span>'
        if entry.accepted
        else '<span class="badge warn">verifier: doubted</span>'
    )
    if entry.generalized:
        badges.append('<span class="badge ok">generalized</span>')
    badges.append(
        f'<span class="badge info">{html.escape(branch)}</span>'
        if entry.committed
        else '<span class="badge warn">no commit</span>'
    )

    def note_row(note: NoteRef) -> str:
        finding = entry.finding_for(note)
        state = "✅" if finding.addressed else "⚠️"
        return (
            "<tr>"
            f'<td class="note-loc">{html.escape(note.file)}:{note.line}</td>'
            f"<td>{html.escape(note.text)}</td>"
            f"<td>{state} {html.escape(finding.how)}</td>"
            "</tr>"
        )

    rows = [note_row(note) for note in entry.notes]
    notes_table = (
        '<table class="notes">'
        "<tr><th>Original note</th><th>Text</th><th>How it was addressed</th></tr>"
        + "".join(rows)
        + "</table>"
    )

    if entry.committed:
        stat = str(git("diff", "--stat", f"{base}...{branch}"))
        diff = str(git("diff", f"{base}...{branch}"))
    else:
        stat = "(no commit — no diff)"
        diff = ""

    files = ", ".join(entry.files_changed)
    swept = ", ".join(entry.swept_beyond_scope)
    swept_html = (
        "<p class='filelist'><strong>Swept beyond declared scope:</strong> "
        f"{html.escape(swept)}</p>"
        if swept
        else ""
    )
    residual_html = (
        f"<div class='residual'><strong>Residual:</strong> {html.escape(entry.residual)}</div>"
        if entry.residual
        else ""
    )

    return f"""
<section class="concern" id="{html.escape(entry.id)}">
<h2>{html.escape(entry.title)}</h2>
<div class="badges">{"".join(badges)}</div>
<details><summary>Generalized spec (what the editor was told)</summary>
<div class="spec">{html.escape(entry.spec)}</div></details>
{notes_table}
<p><strong>Editor summary:</strong> {html.escape(entry.summary)}</p>
<p class="filelist"><strong>Files changed:</strong> {html.escape(files)}</p>
{swept_html}
<p><strong>Verifier reason:</strong> {html.escape(entry.reason)}</p>
{residual_html}
<pre>{html.escape(stat)}</pre>
<details><summary>Full diff vs {html.escape(base[:7])}</summary>
<pre class="diff">{render_diff(diff)}</pre></details>
</section>
"""


def build_review(manifest_path: Path, base: str, out: Path, intro: Path | None) -> None:
    """Assemble and write the review page (the ``resolve-review`` command)."""
    entries = load_manifest(manifest_path)
    ordered = sorted(entries, key=lambda e: (not e.accepted, not e.committed))
    accepted = sum(1 for e in entries if e.accepted)
    committed = sum(1 for e in entries if e.committed)
    intro_html = (
        f'<div class="meta-box">{intro.read_text(encoding="utf-8")}</div>'
        if intro
        else ""
    )
    header = (
        f"<h1>lup:resolve review — base <code>{html.escape(base[:7])}</code></h1>"
        f'<div class="meta-box"><p><strong>{len(entries)} concerns</strong>: '
        f"{committed} committed, {accepted} verifier-accepted. Nothing is merged "
        "yet — approval at the gate merges a concern's branch and thereby clears "
        "its notes.</p></div>"
    )
    sections = "".join(render_concern(entry, base) for entry in ordered)
    page = (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<title>lup resolve review</title>"
        f"<style>{CSS}</style></head><body><main>"
        f"{header}{intro_html}{sections}"
        "</main></body></html>"
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(page, encoding="utf-8")
    typer.echo(f"wrote {out} ({len(page)} bytes, {len(entries)} concerns)")
