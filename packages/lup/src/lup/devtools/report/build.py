"""Where a report's items come from: the surfaces that already compute them.

Nothing here scans anything itself. Each topic is answered by the command
that owns it — the marker scan behind `dev comments`, the drift inspection
behind `harness check`, the containment walk behind `git branches`, the run
state behind `resolve supervise` — and this composes their answers
into one shape. A second scanner beside those would be a second answer to
compare against theirs, which is the reason the surface composes rather than
replaces.

It also reads one back. A written report is two halves — these topics, and
what a session knew — and telling them apart is the same question as building
one, asked in the other direction, so it is answered against the same roster
rather than by a second description of what a report looks like.
"""

from collections.abc import Sequence
from pathlib import Path

from markdown_it import MarkdownIt

from lup.harness.codescan.markers import NoteKind, find_feedback
from lup.devtools.dev.branches import (
    leased_on_disk,
    parse_branches,
    unlanded_siblings,
)
from lup.devtools.dev.comments import FoundComment, scan_tracked
from lup.devtools.harness.drift import RepositoryWriter, inspect_drift
from lup.harness.generate import NativeHarnessComposition
from lup.harness.ownership import GeneratedArtifacts, generated_artifacts
from lup.devtools.report.models import (
    CLAIMS,
    DEFERRALS,
    DRIFT,
    LEASES,
    NOTES,
    REPORT_TOPICS,
    UNLANDED,
    Report,
    ReportItem,
    ReportPart,
    ReportTopic,
)
from lup.policy.kernel.diagnostic import devtools, spelled
from lup.resolver.state import live_lease_branches

parser = MarkdownIt()


def authored_headings(
    written: str, topics: Sequence[ReportTopic] = REPORT_TOPICS
) -> list[str]:
    """Every section of a written report that no walk put there.

    The half this command writes is exactly the declared topics, one ``##``
    each, so a second-level heading outside that set was written by a session
    out of what it knew. That half is the one worth protecting: the walked
    sections rebuild from the tree in a second, while a session's own reading
    of what it left half-done has no other copy anywhere and cannot be
    recovered by anybody once that session is over.

    Read against the same roster the report renders from, so a topic added
    there is accounted for here by existing rather than by being remembered.

    Parsed rather than scanned for lines opening with ``##``, because a
    fenced code block spells the same shape, and a report quoting one would
    read as carrying prose it does not.
    """
    declared = tuple(topic.title for topic in topics)
    tokens = parser.parse(written)
    return [
        tokens[index + 1].content
        for index, token in enumerate(tokens)
        if token.type == "heading_open"
        and token.tag == "h2"
        and not tokens[index + 1].content.startswith(declared)
    ]


def note_items(
    found: list[FoundComment], kind: NoteKind, owned: GeneratedArtifacts
) -> list[ReportItem]:
    """Every scanned note of one flavour this tree can act on.

    A note inside a generated artifact was written against the generator's
    source and copied here when the harness materialized, so it is the same
    note twice and only one of the two can be resolved. Reporting both would
    tell a reader there is more outstanding than there is.
    """
    return [
        ReportItem(
            where=f"{comment.file}:{comment.start_line}-{comment.end_line}",
            what=comment.text,
            gate=comment.condition or "",
        )
        for comment in found
        if comment.kind == kind and owned.owning(comment.file) is None
    ]


def drift_items(
    compositions: list[NativeHarnessComposition], writers: list[RepositoryWriter]
) -> list[ReportItem]:
    """Every generated tree the typed source has moved out from under.

    One reading answers both halves. A verdict names each stale artifact
    outside the native trees rather than only whether any is, so a reader is
    told which file is behind instead of being sent to look for it.
    """
    verdict = inspect_drift(compositions, writers)
    regenerate = spelled(devtools("harness", "generate", "all"))
    return [
        *[
            ReportItem(
                where=report.target,
                what=f"generated tree is stale — `{regenerate}`",
            )
            for report in verdict.stale_trees
        ],
        *[
            ReportItem(
                where="repository artifacts",
                what=f"{message} — `{regenerate}`",
            )
            for message in verdict.stale_repository
        ],
    ]


def unlanded_items() -> list[ReportItem]:
    """Every sibling branch holding work the integration branch lacks."""
    return [
        ReportItem(
            where=branch.name, what=branch.standing(), gate=branch.worktree or ""
        )
        for branch in unlanded_siblings()
    ]


def lease_items(state_root: Path) -> list[ReportItem]:
    """Every concern a resolver run is holding a branch for.

    Read as the branch survey reads it: only a leased branch still in
    ``refs/heads`` is outstanding. The rest went with the run's own cleanup,
    and listing them would count finished work as held.
    """
    names = [branch["name"] for branch in parse_branches()]
    return [
        ReportItem(where=held.branch, what=held.standing, gate=f"run {held.run_id}")
        for held in leased_on_disk(live_lease_branches(state_root), names).values()
    ]


def build_report(
    compositions: list[NativeHarnessComposition],
    repository_writers: list[RepositoryWriter],
    state_root: Path,
    root: Path,
) -> Report:
    """Ask every surface what it still has outstanding, in reading order.

    The notes scan runs once and is split three ways rather than three times,
    because walking every tracked file is what it costs and the flavour of a
    note is a field on what came back.
    """
    found = scan_tracked(find_feedback)
    owned = generated_artifacts(root)
    return Report(
        parts=[
            ReportPart(topic=NOTES, items=note_items(found, NoteKind.note, owned)),
            ReportPart(topic=DEFERRALS, items=note_items(found, NoteKind.defer, owned)),
            ReportPart(topic=CLAIMS, items=note_items(found, NoteKind.solved, owned)),
            ReportPart(
                topic=DRIFT, items=drift_items(compositions, repository_writers)
            ),
            ReportPart(topic=UNLANDED, items=unlanded_items()),
            ReportPart(topic=LEASES, items=lease_items(state_root)),
        ]
    )
