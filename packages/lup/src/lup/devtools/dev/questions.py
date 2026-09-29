"""The surface a reviewer answers a parked question through.

Every final ask is written to the relay before anybody sees it, which is what
makes a detached run answerable at all — but a durable record with no way to
read it is a queue that silently grows. So this is the other half: list what
is waiting, show one in full, and answer, reject, or cancel it.

The requester can read its own question's status here and cannot answer it.
That is enforced on the record rather than by leaving the command out of an
agent's tool list: a policy whose reviewer is "whoever could reach the
command" is one that changes when somebody adds a tool.
"""

from lup.devtools.dev.review_notifications import (
    ReviewNotification,
    ReviewNotifications,
)

import asyncio
import hmac
import secrets
import webbrowser
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from datetime import datetime
from difflib import SequenceMatcher
from hashlib import sha256
from itertools import count
from pathlib import Path
from tempfile import mkdtemp
from typing import TYPE_CHECKING, Literal
from uuid import NAMESPACE_URL, uuid5

import sh
import typer
from pydantic import BaseModel, Field
from rich.console import Console
from rich.syntax import Syntax

from lup.channels.models import Door
from lup.coordination.repository import RepositoryPeers
from lup.coordination.roster import RosterMember
from lup.coordination.wake import wake
from lup.devtools.sync import registered_difftool
from lup.devtools.utils import output_json
from lup.harness.codescan.markers import MarkerScan, scan_mode_for
from lup.policy.kernel.edit import (
    IGNORE_RE,
    added_line_numbers,
    ignore_rule_ids,
    removed_lines,
    resites_a_suppression,
    written_suppression,
)
from lup.policy.relay import CapturedFileReview, PersistentQuestion, QuestionRelay
from lup.policy.review import (
    ReviewedFile,
    reviewed_files,
    reviewed_preview,
)
from lup.providers.harness import patch_review
from lup.sandbox.rail import repository_layout, sibling_worktrees

if TYPE_CHECKING:
    from fastapi import BackgroundTasks, FastAPI


class ReviewRoot(BaseModel, frozen=True):
    """One operator-selected checkout and its opaque browser address."""

    id: str
    path: str

    @classmethod
    def of(cls, root: Path) -> "ReviewRoot":
        return cls(id=str(uuid5(NAMESPACE_URL, root.as_uri())), path=str(root))


class ReviewSummary(BaseModel, frozen=True):
    """The queue row, with eligibility and a descriptive evidence-based title."""

    key: str
    root_id: str
    id: str
    state: str
    requester: str
    reason: str
    title: str
    paths: list[str]
    total_files: int = 0
    operation: str
    rule: str
    created: datetime
    answerable: bool

    @staticmethod
    def key_for(root: Path, question_id: str) -> str:
        """Identify one root's question independently of its display projection."""
        return str(uuid5(NAMESPACE_URL, f"{root.as_uri()}#{question_id}"))

    @classmethod
    def of(
        cls, root: Path, question: PersistentQuestion, principal: str
    ) -> "ReviewSummary":
        return cls.from_files(
            root, question, principal, reviewed_files(question, patch_review)
        )

    @classmethod
    def from_files(
        cls,
        root: Path,
        question: PersistentQuestion,
        principal: str,
        complete: list[ReviewedFile],
    ) -> "ReviewSummary":
        """Project the same file preview already used by this response's detail."""
        operation = question.operation
        files = [
            change
            for change in complete
            if ReviewAttribution.of(change, question.file_reviews).effect != "allow"
        ]

        def relative(path: Path) -> str:
            return str(path.relative_to(root) if path.is_relative_to(root) else path)

        def described() -> str:
            if files and operation.tool != "Bash":
                if len(files) == 1:
                    match files[0].operation():
                        case "create":
                            verb = "Create"
                        case "delete":
                            verb = "Delete"
                        case "overwrite":
                            verb = "Replace"
                        case _:
                            verb = "Update"
                    return f"{verb} {relative(files[0].path)}"
                parent = files[0].path.parent
                location = (
                    f" in {relative(parent)}"
                    if all(file.path.parent == parent for file in files)
                    else ""
                )
                return f"Change {len(files)} files{location}"
            payload = operation.payload
            command = payload["command"] if "command" in payload else None
            if operation.tool == "Bash" and isinstance(command, str) and command:
                lines = command.splitlines()
                return (
                    f"Run: {command}"
                    if len(lines) == 1
                    else f"Run shell script ({len(lines)} lines)"
                )
            return f"Review {operation.tool}"

        return cls(
            key=cls.key_for(root, question.id),
            root_id=ReviewRoot.of(root).id,
            id=question.id,
            state="expired" if question.stale() else question.state,
            requester=operation.requester,
            reason=question.reason,
            title=described(),
            paths=[relative(file.path) for file in files],
            total_files=len(complete),
            operation=operation.summary(),
            rule=question.rule,
            created=question.created,
            answerable=question.answerable_by(principal) and not question.stale(),
        )


class ReviewError(BaseModel, frozen=True):
    """A checkout that could not be read, without hiding its absence."""

    root: str
    message: str


class ReviewInbox(BaseModel, frozen=True):
    """A complete snapshot of the configured review queues."""

    roots: list[ReviewRoot]
    reviews: list[ReviewSummary] = []
    errors: list[ReviewError] = []


class ReviewLine(BaseModel, frozen=True):
    """One source line, with original terminators and both document positions."""

    kind: Literal["context", "add", "remove", "meta"]
    text: str
    old_line: int | None = None
    new_line: int | None = None
    suppression: bool = False

    def annotated(self) -> Iterator["ReviewLine"]:
        yield self
        if not self.text.endswith("\n"):
            yield ReviewLine(kind="meta", text="\\ No newline at end of file")


class ReviewOpcode(BaseModel, frozen=True):
    """The named bounds of one sequence-matcher operation."""

    kind: str
    old_start: int
    old_stop: int
    new_start: int
    new_stop: int

    def lines(self, before: list[str], after: list[str]) -> Iterator[ReviewLine]:
        if self.kind == "equal":
            for old, new in zip(
                range(self.old_start, self.old_stop),
                range(self.new_start, self.new_stop),
                strict=True,
            ):
                yield from ReviewLine(
                    kind="context",
                    text=before[old],
                    old_line=old + 1,
                    new_line=new + 1,
                ).annotated()
        else:
            if self.kind in {"replace", "delete"}:
                for old in range(self.old_start, self.old_stop):
                    yield from ReviewLine(
                        kind="remove", text=before[old], old_line=old + 1
                    ).annotated()
            if self.kind in {"replace", "insert"}:
                for new in range(self.new_start, self.new_stop):
                    yield from ReviewLine(
                        kind="add", text=after[new], new_line=new + 1
                    ).annotated()


class ReviewHunk(BaseModel, frozen=True):
    """A contiguous group of changes with unchanged context around it."""

    header: str
    lines: list[ReviewLine]

    @classmethod
    def between(cls, change: ReviewedFile, context: int = 3) -> list["ReviewHunk"]:
        before, after = change.lines(change.before), change.lines(change.after)
        groups = [
            [
                ReviewOpcode(
                    kind=kind,
                    old_start=old_start,
                    old_stop=old_stop,
                    new_start=new_start,
                    new_stop=new_stop,
                )
                for kind, old_start, old_stop, new_start, new_stop in group
            ]
            for group in SequenceMatcher(
                a=before, b=after, autojunk=False
            ).get_grouped_opcodes(context)
        ]

        def span(start: int, stop: int) -> str:
            length = stop - start
            if length == 1:
                return str(start + 1)
            return f"{start if length == 0 else start + 1},{length}"

        return [
            cls(
                header=(
                    f"@@ -{span(group[0].old_start, group[-1].old_stop)}"
                    f" +{span(group[0].new_start, group[-1].new_stop)} @@"
                ),
                lines=[
                    line for opcode in group for line in opcode.lines(before, after)
                ],
            )
            for group in groups
        ]


class ReviewAttribution(BaseModel, frozen=True):
    """Original policy attribution, usable only for these exact file images."""

    effect: Literal["allow", "ask", "deny", "unknown"] = "unknown"
    reason: str = "No per-file decision was captured; this file remains visible as unclassified context."
    rules: list[str] = []

    @classmethod
    def of(
        cls, change: ReviewedFile, evidence: list[CapturedFileReview] | None
    ) -> "ReviewAttribution":
        matching = [row for row in evidence or [] if row.path == change.path]
        if len(matching) != 1:
            return cls()
        row = matching[0]
        before = (
            sha256(change.before.encode()).hexdigest()
            if change.before is not None
            else None
        )
        after = (
            sha256(change.after.encode()).hexdigest()
            if change.after is not None
            else None
        )
        if row.before_sha256 != before or row.after_sha256 != after:
            return cls(
                reason="The captured file verdict does not match these exact images; relevance is unknown."
            )
        if row.effect == "defer":
            return cls(
                reason=f"The captured policy deferred this file to the native provider: {row.reason}",
                rules=row.rules,
            )
        return cls(effect=row.effect, reason=row.reason, rules=row.rules)


class ReviewSuppression(BaseModel, frozen=True):
    """A result document's explicit rule exception, located for review."""

    line: int
    rule_ids: list[str] | None
    reason: str
    introduced: bool
    review_effect: Literal["allow", "ask", "deny", "unknown"] = "unknown"
    review_reason: str = "No directive-level decision was captured."
    review_rule_ids: list[str] | None = None

    @classmethod
    def in_source(cls, change: ReviewedFile, source: str) -> list["ReviewDirective"]:
        lines = change.lines(source)
        scan = MarkerScan(
            source, scan_mode_for(change.path), marker=IGNORE_RE, ignore=None
        )
        context = scan.context
        return [
            ReviewDirective(
                marker=cls(
                    line=marker.start_line,
                    rule_ids=list(ids) if ids is not None else None,
                    reason=marker.text,
                    introduced=False,
                ),
                text=lines[marker.start_line - 1][matched.start() :],
            )
            for marker in scan.notes()
            if (
                matched := (
                    context.suppression_at(
                        marker.start_line, lines[marker.start_line - 1]
                    )
                    if context is not None
                    else written_suppression(lines[marker.start_line - 1])
                )
            )
            is not None
            for ids in [ignore_rule_ids(matched)]
        ]

    @classmethod
    def of(
        cls, change: ReviewedFile, attribution: ReviewAttribution | None = None
    ) -> list["ReviewSuppression"]:
        original, result = change.lines(change.before), change.lines(change.after)
        added = added_line_numbers(change.before, change.after)
        removed = removed_lines(change.before, change.after)
        original_lines = (change.before or "").splitlines()
        previous = [
            directive
            for directive in cls.in_source(change, change.before or "")
            if original_lines[directive.marker.line - 1] in removed
        ]
        current = cls.in_source(change, change.after or "")
        opcodes = list(
            SequenceMatcher(a=original, b=result, autojunk=False).get_opcodes()
        )
        verdict = attribution or ReviewAttribution()

        def prior_at(marker: ReviewSuppression) -> ReviewSuppression | None:
            for kind, old_start, old_stop, new_start, new_stop in opcodes:
                if kind != "replace" or not new_start < marker.line <= new_stop:
                    continue
                earlier = [
                    directive.marker
                    for directive in previous
                    if old_start < directive.marker.line <= old_stop
                ]
                later = [
                    directive.marker
                    for directive in current
                    if new_start < directive.marker.line <= new_stop
                ]
                if len(earlier) == 1 and len(later) == 1:
                    return earlier[0]
            return None

        def projected(marker: ReviewSuppression, text: str) -> ReviewSuppression:
            if marker.line not in added or resites_a_suppression(
                text, [directive.text for directive in previous]
            ):
                return marker.model_copy(
                    update={
                        "review_effect": "allow",
                        "review_reason": "This directive introduces no rule exception.",
                        "review_rule_ids": [],
                    }
                )
            prior = prior_at(marker)
            scoped = marker.rule_ids
            introduced = (
                [
                    rule
                    for rule in scoped
                    if prior is None or rule not in (prior.rule_ids or [])
                ]
                if scoped is not None
                else None
            )
            required = verdict.effect != "allow" and (
                verdict.effect == "unknown" or "edit:anti-pattern" in verdict.rules
            )
            return marker.model_copy(
                update={
                    "introduced": True,
                    "review_rule_ids": introduced,
                    "review_effect": verdict.effect if required else "allow",
                    "review_reason": verdict.reason
                    if required
                    else "The captured gate did not require review of this directive.",
                }
            )

        return [projected(directive.marker, directive.text) for directive in current]


class ReviewDirective(BaseModel, frozen=True):
    """One lexically parsed directive and the original text the policy reads."""

    marker: ReviewSuppression
    text: str


class ReviewFile(BaseModel, frozen=True):
    """Captured file contents and the corresponding proposed change."""

    path: str
    operation: str
    before: str | None
    after: str | None
    unified: str
    unchanged: bool
    hunks: list[ReviewHunk]
    additions: int
    deletions: int
    suppressions: list[ReviewSuppression]
    review_effect: Literal["allow", "ask", "deny", "unknown"] = "unknown"
    review_reason: str = "No per-file decision was captured."

    @classmethod
    def of(
        cls, change: ReviewedFile, evidence: list[CapturedFileReview] | None = None
    ) -> "ReviewFile":
        attribution = ReviewAttribution.of(change, evidence)
        suppressions = ReviewSuppression.of(change, attribution)
        marked = {suppression.line for suppression in suppressions}
        hunks = [
            ReviewHunk(
                header=hunk.header,
                lines=[
                    line.model_copy(update={"suppression": line.new_line in marked})
                    for line in hunk.lines
                ],
            )
            for hunk in ReviewHunk.between(change)
        ]
        return cls(
            path=str(change.path),
            operation=change.operation(),
            before=change.before,
            after=change.after,
            unified=change.unified(),
            unchanged=change.unchanged(),
            hunks=hunks,
            additions=sum(line.kind == "add" for hunk in hunks for line in hunk.lines),
            deletions=sum(
                line.kind == "remove" for hunk in hunks for line in hunk.lines
            ),
            suppressions=suppressions,
            review_effect=attribution.effect,
            review_reason=attribution.reason,
        )


class ReviewDetail(BaseModel, frozen=True):
    """Everything the operator reviews before answering."""

    summary: ReviewSummary
    question: PersistentQuestion
    files: list[ReviewFile]
    stale_reason: str
    command: str | None
    preview_unavailable: str
    preview_notice: str = ""
    notification: ReviewNotification | None = None

    @classmethod
    def of(
        cls, root: Path, entry: PersistentQuestion, principal: str
    ) -> "ReviewDetail":
        preview = reviewed_preview(entry, patch_review)
        files = [ReviewFile.of(change, entry.file_reviews) for change in preview.files]
        payload = entry.operation.payload
        requested = payload["command"] if "command" in payload else None
        command = (
            requested
            if entry.operation.tool == "Bash" and isinstance(requested, str)
            else None
        )
        return cls(
            summary=ReviewSummary.from_files(root, entry, principal, preview.files),
            question=entry,
            files=files,
            stale_reason=stale_preimages(entry),
            command=command,
            preview_unavailable=preview.unavailable,
            preview_notice=preview.notice,
            notification=ReviewNotifications(root=root).read(entry),
        )


class ReviewAnswer(BaseModel, frozen=True, extra="forbid"):
    """A decision bound to the fingerprint the browser displayed."""

    approved: bool
    note: str = ""
    fingerprint: str


class ReviewDecision(BaseModel, frozen=True):
    """The recorded answer and the result of notifying its requester."""

    review: ReviewDetail
    notification: ReviewNotification


class QuestionView(BaseModel, frozen=True):
    """One question as a reader sees it, without the operation's whole payload.

    The payload is in the record and reaching it is what ``show`` is for. A
    listing that printed it would bury the one line a reviewer triages on.
    """

    id: str
    state: str
    reviewer: str
    requester: str
    reason: str
    operation: str
    rule: str
    answered_by: str = ""
    note: str = ""

    @classmethod
    def of(cls, question: PersistentQuestion) -> "QuestionView":
        return cls(
            id=question.id,
            state=question.state,
            reviewer=question.requirement,
            requester=question.operation.requester,
            reason=question.reason,
            operation=question.operation.summary(),
            rule=question.rule,
            answered_by=question.answer.principal if question.answer else "",
            note=question.answer.note if question.answer else "",
        )


def relay(root: Path, log: Path = Path(".lup/questions.jsonl")) -> QuestionRelay:
    """The relay for one checkout, at the path that checkout keeps it in.

    Under ``.lup`` by default because a parked question is managed
    active-session state: excluded from ordinary captures, and not something
    an ordinary destructive shell operation is authorized to remove just
    because a snapshot exists. A deployment that keeps it elsewhere passes
    its own path rather than editing this one.
    """
    return QuestionRelay(root / log)


def listing(root: Path, principal: str, everything: bool, as_json: bool) -> None:
    """Print what is waiting, narrowed to what this principal may answer.

    Narrowed by eligibility rather than by ownership, because a supervisor
    shown a question it may not answer is a supervisor about to try — and the
    refusal it then gets teaches it nothing about which questions are its.
    """
    store = relay(root)
    questions = store.questions() if everything else store.pending(principal)
    if as_json:
        output_json([QuestionView.of(entry).model_dump() for entry in questions])
        return
    if not questions:
        typer.echo("nothing is waiting")
        return
    for entry in questions:
        typer.echo(entry.summary())


def changes(entry: PersistentQuestion) -> list[ReviewedFile]:
    """The file changes this question proposes, patch envelopes included.

    The patch reader is supplied here rather than reached for inside
    :mod:`lup.policy.review`, which both runtimes read: the envelope's grammar
    is one provider's word, and this command is already where one is named.
    """
    return reviewed_files(entry, patch_review)


def render_diffs(entry: PersistentQuestion, console: Console) -> bool:
    """Print what each file would become, and say whether anything was shown.

    A diff rather than the payload, because a payload carrying the new
    contents with the preimage printed underneath is two documents a reviewer
    compares by eye — which is the whole of what was wrong with this surface,
    and most of why an operator would rather answer somewhere else.
    """
    rendered = False
    for change in changes(entry):
        rendered = True
        console.print(f"  {change.operation():<9} {change.path}", style="bold")
        if change.unchanged():
            console.print("    this would leave the file exactly as it stands")
            continue
        console.print(Syntax(change.unified(), "diff", theme="ansi_dark"))
    return rendered


def opened(entry: PersistentQuestion, difftool: list[str]) -> None:
    """Hand each before/after pair to whatever this machine opens diffs with.

    Written to a directory that outlives this process, because a difftool that
    forks and returns — which an editor does — would otherwise be handed two
    paths deleted before it read them. Each side keeps the reviewed file's own
    name under a ``before``/``after`` directory, so the editor's tab titles say
    which file is being reviewed rather than which temporary it landed in.
    """
    if not difftool:
        typer.echo(
            "this machine registers no difftool. Add one to sync.json.local as"
            ' `"difftool": ["code", "--diff"]`, whose argv takes the two paths'
            " last — it is a fact about this machine, so it is not committed",
            err=True,
        )
        raise typer.Exit(2)
    staged = Path(mkdtemp(prefix="lup-review-"))
    for change in changes(entry):
        pair = [staged / side / change.path.name for side in ("before", "after")]
        for target, document in zip(pair, [change.before, change.after]):
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(document or "", encoding="utf-8")
        typer.echo(f"  opening {change.path}")
        sh.Command(difftool[0])(*difftool[1:], *[str(path) for path in pair])
    typer.echo(f"  the pair stays under {staged} until you remove it")


def show(
    root: Path,
    question: str,
    as_json: bool,
    open_externally: bool = False,
    difftool: list[str] | None = None,
) -> None:
    """Print one question whole, including the operation it would resume.

    The operation whole rather than summarized, because what an approval binds
    to is what a reviewer should have been able to read: an approval given
    against a summary is an approval of the summary.
    """
    entry = relay(root).find(question)
    if entry is None:
        typer.echo(f"no question {question!r} is recorded", err=True)
        raise typer.Exit(2)
    if as_json:
        output_json(entry.model_dump(mode="json"))
        return
    typer.echo(entry.summary())
    typer.echo(f"  requester   {entry.operation.requester}")
    typer.echo(f"  eligible    {', '.join(entry.eligible) or 'nobody'}")
    typer.echo(f"  purpose     {entry.purpose or 'unclassified'}")
    typer.echo(f"  rule        {entry.rule or 'unattributed'}")
    typer.echo(f"  fingerprint {entry.fingerprint}")
    if entry.escalation:
        typer.echo(f"  escalated   {entry.escalation}")
    if entry.checkpoint_failure:
        typer.echo(f"  capture     failed: {entry.checkpoint_failure}")
    if open_externally:
        opened(entry, difftool if difftool is not None else registered_difftool())
        return
    if not render_diffs(entry, Console()):
        # No file change to render, which is every shell command: the payload
        # *is* what a reviewer reads there, so it stands rather than being
        # replaced by a diff of nothing.
        typer.echo(f"  payload     {entry.operation.payload}")
        for path, before in entry.preconditions.items():
            typer.echo(
                f"  preimage {path}\n{before if before is not None else '(absent)'}"
            )
    if entry.answer is not None:
        typer.echo(
            f"  answered    {'yes' if entry.answer.approved else 'no'}"
            f" by {entry.answer.principal} ({entry.answer.receipt})"
        )
        if entry.answer.note:
            typer.echo(f"  note        {entry.answer.note}")


def answer(
    root: Path, question: str, principal: str, approved: bool, note: str
) -> None:
    """Record one decision, and say what it means for the operation.

    The refusal path prints the relay's own message rather than a generic
    one, because every way this can fail is a distinct thing the reviewer
    needs to know: the question is gone, already answered, expired, or theirs
    to read and not to answer.
    """
    try:
        settled = relay(root).answer(question, principal, approved, note)
    except ValueError as refusal:
        typer.echo(str(refusal), err=True)
        raise typer.Exit(2) from refusal
    verb = {"approved": "approved", "rejected": "rejected"}
    typer.echo(
        f"{settled.id}: {verb[settled.state] if settled.state in verb else settled.state}"
    )
    if settled.state == "approved":
        if settled.resumption == "native_retry":
            typer.echo(
                "Retry the exact tool call; its preimages are rechecked and approval is spent once."
            )
            return
        typer.echo(
            "the coordinator revalidates and resumes it — the requester does"
            " not reissue it, and this approval is spent once"
        )


def cancel(root: Path, question: str, reason: str) -> None:
    """Withdraw a question nobody needs answered any more."""
    try:
        settled = relay(root).cancel(question, reason)
    except ValueError as refusal:
        typer.echo(str(refusal), err=True)
        raise typer.Exit(2) from refusal
    typer.echo(f"{settled.id}: cancelled")


def stale_preimages(entry: PersistentQuestion) -> str:
    """Explain any changed preimage without substituting it into the review."""

    def failures() -> Iterator[str]:
        for path, before in entry.preconditions.items():
            try:
                current = (
                    path.read_text(encoding="utf-8", newline="")
                    if path.exists()
                    else None
                )
            except (OSError, UnicodeError) as error:
                yield f"Cannot read {path}: {error}"
                continue
            if current != before:
                yield f"{path} changed since this request was recorded"

    return "\n".join(failures())


class ReviewRecipient(BaseModel, frozen=True, arbitrary_types_allowed=True):
    """The requesting member and the repository that can reach it."""

    peers: RepositoryPeers
    member: RosterMember


def notify_requester(
    roots: tuple[Path, ...], entry: PersistentQuestion
) -> ReviewNotification:
    """Queue the answer for its requester and try its declared wake route."""
    candidates = [RepositoryPeers(root) for root in roots]
    rosters = {peers.root: peers for peers in candidates}
    identities = {entry.operation.requester, entry.operation.session}
    members = [
        ReviewRecipient(peers=peers, member=member)
        for peers in rosters.values()
        for member in peers.present()
        if member.running
        and (
            entry.resumption != "native_retry"
            or not entry.operation.session
            or member.wake.session == entry.operation.session
        )
        and (
            member.actor.id in identities
            or bool(member.wake.session and member.wake.session in identities)
        )
    ]
    if len(members) != 1:
        return ReviewNotification(
            queued=False,
            woken=False,
            detail="Decision recorded; no unique live requester is registered.",
        )
    recipient = members[0]
    member = recipient.member
    instruction = (
        "Retry the exact tool call; its preimages and policy are rechecked."
        if entry.state == "approved" and entry.resumption == "native_retry"
        else "Read the recorded decision before continuing."
    )
    message = (
        f"Review {entry.id} in {entry.operation.worktree} was {entry.state}. {instruction}\n"
        f"Operator note: {entry.answer.note if entry.answer else ''}"
    )
    delivered = recipient.peers.send(member.address, message, door=Door.PAGE)
    if delivered is None:
        return ReviewNotification(
            queued=False, woken=False, detail="Decision recorded; requester left."
        )
    try:
        nudged = wake(
            member.wake, message, Path(member.worktree) if member.worktree else None
        )
    except Exception as error:
        return ReviewNotification(
            queued=True,
            woken=False,
            detail=f"Decision recorded and notification queued; wake failed: {error}",
        )
    return ReviewNotification(
        queued=True,
        woken=nudged.reached,
        detail=(
            "Decision recorded and notification accepted by the requester runtime."
            if nudged.reached
            else f"Decision recorded and notification queued. {nudged.reason}"
        ),
    )


class ReviewHeaders(BaseModel, frozen=True):
    """The request headers relevant to inbox authentication."""

    authorization: str = ""
    origin: str = ""
    content_type: str = Field(default="", alias="content-type")


class LocatedReview(BaseModel, frozen=True):
    """A stored question paired with the checkout that owns its relay."""

    root: Path
    question: PersistentQuestion


class ReviewScan(BaseModel, frozen=True):
    """Discovered checkouts and any operator-selected roots that failed."""

    roots: tuple[Path, ...] = ()
    errors: list[ReviewError] = []

    @classmethod
    def of(cls, root: Path) -> "ReviewScan":
        try:
            return cls(roots=review_roots(root, []))
        except (OSError, ValueError, sh.ErrorReturnCode) as error:
            return cls(errors=[ReviewError(root=str(root), message=str(error))])


class ReviewQueue(BaseModel, frozen=True):
    """A relay read whose failure does not hide other checkouts' reviews."""

    root: Path
    questions: list[PersistentQuestion] = []
    errors: list[ReviewError] = []

    @classmethod
    def read(cls, root: Path) -> "ReviewQueue":
        try:
            return cls(root=root, questions=relay(root).questions())
        except (OSError, ValueError) as error:
            return cls(
                root=root, errors=[ReviewError(root=str(root), message=str(error))]
            )


class ReviewStore(BaseModel, frozen=True):
    """Read only operator-selected queues and answer their exact records."""

    roots: tuple[Path, ...]
    principal: str = "operator"
    discover: bool = False

    def scan_roots(self) -> ReviewScan:
        if not self.roots or not self.discover:
            return ReviewScan(roots=self.roots)
        scans = [ReviewScan.of(root) for root in self.roots]
        return ReviewScan(
            roots=tuple(dict.fromkeys(root for scan in scans for root in scan.roots)),
            errors=[error for scan in scans for error in scan.errors],
        )

    def checkout_roots(self) -> tuple[Path, ...]:
        return self.scan_roots().roots

    def read_root(self, root: Path) -> ReviewInbox:
        queue = ReviewQueue.read(root)
        return ReviewInbox(
            roots=[ReviewRoot.of(root)],
            reviews=[
                ReviewSummary.of(root, entry, self.principal)
                for entry in queue.questions
            ],
            errors=queue.errors,
        )

    def snapshot(self) -> ReviewInbox:
        scan = self.scan_roots()
        queues = [self.read_root(root) for root in scan.roots]
        return ReviewInbox(
            roots=[root for queue in queues for root in queue.roots],
            reviews=sorted(
                (row for queue in queues for row in queue.reviews),
                key=lambda row: row.created,
                reverse=True,
            ),
            errors=scan.errors + [error for queue in queues for error in queue.errors],
        )

    def locate(self, key: str) -> LocatedReview:
        from fastapi import HTTPException

        scan = self.scan_roots()
        queues = [ReviewQueue.read(root) for root in scan.roots]
        for queue in queues:
            for entry in queue.questions:
                if ReviewSummary.key_for(queue.root, entry.id) == key:
                    return LocatedReview(root=queue.root, question=entry)
        errors = scan.errors + [error for queue in queues for error in queue.errors]
        if errors:
            raise HTTPException(
                status_code=503, detail="\n".join(error.message for error in errors)
            )
        raise HTTPException(status_code=404, detail="No review has that key")

    def detail(self, key: str) -> ReviewDetail:
        located = self.locate(key)
        return ReviewDetail.of(located.root, located.question, self.principal)

    def answer(
        self,
        key: str,
        decision: ReviewAnswer,
        background: "BackgroundTasks | None" = None,
    ) -> ReviewDecision:
        from fastapi import HTTPException

        located = self.locate(key)
        entry = located.question
        if entry.stale():
            raise HTTPException(status_code=409, detail="Review has expired")
        if not hmac.compare_digest(
            decision.fingerprint.encode("utf-8"), entry.fingerprint.encode("utf-8")
        ):
            raise HTTPException(status_code=409, detail="Review fingerprint changed")
        if decision.approved and (reason := stale_preimages(entry)):
            raise HTTPException(status_code=409, detail=reason)
        try:
            settled = relay(located.root).answer(
                entry.id, self.principal, decision.approved, decision.note
            )
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        if settled.answer is None:
            raise HTTPException(status_code=409, detail=f"Review is {settled.state}")
        notifications = ReviewNotifications(root=located.root)
        attempt = notifications.prepare(settled)

        def deliver() -> ReviewNotification:
            return notify_requester(self.checkout_roots(), settled)

        if background is None:
            notification = notifications.complete(settled, attempt, deliver)
        else:
            background.add_task(notifications.complete, settled, attempt, deliver)
            notification = attempt.notification
        return ReviewDecision(
            review=ReviewDetail.of(located.root, settled, self.principal),
            notification=notification,
        )


def review_app(
    url: str, token: str, roots: tuple[Path, ...], *, discover: bool = False
) -> "FastAPI":
    """Build an authenticated browser surface over the durable review queues."""
    from fastapi import BackgroundTasks, Request
    from fastapi.responses import JSONResponse, Response, StreamingResponse

    from lup.web.serve import bundle_app

    def anchor(root: Path) -> Path:
        try:
            return repository_layout(root).common.resolve()
        except (OSError, ValueError, sh.ErrorReturnCode):
            # Keep unavailable selections so scans report them and can recover.
            return root

    if discover:
        roots = tuple(dict.fromkeys(anchor(root) for root in roots))
    app = bundle_app("Review inbox", url, "reviews")
    store = ReviewStore(roots=roots, discover=discover)

    @app.middleware("http")
    async def authorize(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if request.url.path.startswith("/api/"):
            headers = ReviewHeaders.model_validate(request.headers)
            provided = headers.authorization.encode("utf-8")
            expected = f"Bearer {token}".encode("utf-8")
            if not hmac.compare_digest(provided, expected):
                return JSONResponse(
                    {"detail": "Authentication required"}, status_code=401
                )
            if request.method == "POST":
                if headers.origin != url:
                    return JSONResponse({"detail": "Origin refused"}, status_code=403)
                if headers.content_type != "application/json":
                    return JSONResponse({"detail": "JSON required"}, status_code=415)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = "frame-ancestors 'none'"
        return response

    @app.get("/api/reviews")
    def reviews() -> ReviewInbox:
        return store.snapshot()

    @app.get("/api/reviews/{key}")
    def detail(key: str) -> ReviewDetail:
        return store.detail(key)

    @app.post("/api/reviews/{key}/answer")
    def decide(
        key: str, decision: ReviewAnswer, background_tasks: BackgroundTasks
    ) -> ReviewDecision:
        return store.answer(key, decision, background_tasks)

    @app.get("/api/events")
    async def events(request: Request) -> StreamingResponse:
        async def snapshots() -> AsyncIterator[str]:
            previous = ""
            for tick in count():
                if await request.is_disconnected():
                    return
                snapshot = await asyncio.to_thread(store.snapshot)
                encoded = snapshot.model_dump_json()
                if encoded != previous or tick % 15 == 0:
                    yield encoded + "\n"
                    previous = encoded
                await asyncio.sleep(1)

        return StreamingResponse(snapshots(), media_type="application/x-ndjson")

    return app


def review_roots(root: Path, additional: list[Path]) -> tuple[Path, ...]:
    """Discover sibling worktrees only for repositories named by the operator."""

    def candidates() -> Iterator[Path]:
        for source in [root, *additional]:
            resolved = source.resolve(strict=True)
            for candidate in [resolved, *sibling_worktrees(resolved)]:
                if (candidate / ".git").exists():
                    yield candidate.resolve()

    selected = tuple(dict.fromkeys(candidates()))
    if not selected:
        raise ValueError("No Git worktrees were found for the selected roots")
    return selected


def create_questions_app(root: Path) -> typer.Typer:
    """Wire the reviewer's surface over one checkout's relay."""
    app = typer.Typer(no_args_is_help=True)

    @app.command("serve")
    def serve_cmd(
        selected_roots: list[Path] | None = typer.Option(
            None,
            "--root",
            help="Watch this repository's worktrees instead of the current repository; repeatable",
        ),
        host: str = typer.Option("127.0.0.1", help="Loopback address to bind"),
        port: int = typer.Option(8766, min=1, max=65535, help="Browser inbox port"),
        open_page: bool = typer.Option(
            True, "--open/--no-open", help="Open the browser"
        ),
    ) -> None:
        """Keep an operator browser inbox open across the selected worktrees."""
        import uvicorn

        from lup.web.loopback import refuse_non_loopback

        refuse_non_loopback(host, "Review inbox")
        roots = tuple(
            dict.fromkeys(
                path.resolve(strict=True) for path in (selected_roots or [root])
            )
        )
        authority = f"[{host}]" if ":" in host else host
        url = f"http://{authority}" if port == 80 else f"http://{authority}:{port}"
        token = secrets.token_urlsafe(32)
        app = review_app(url, token, roots, discover=True)
        browser_url = f"{url}/#token={token}"
        typer.echo(f"Review inbox: {browser_url}")
        if open_page:
            webbrowser.open(browser_url)
        uvicorn.run(app, host=host, port=port, access_log=False)

    @app.command("list")
    def list_cmd(
        principal: str = typer.Option(
            "", "--as", help="Show only what this principal may answer"
        ),
        everything: bool = typer.Option(
            False, "--all", help="Include answered, expired, and cancelled questions"
        ),
        as_json: bool = typer.Option(False, "--json", help="Emit JSON"),
    ) -> None:
        """List the questions this run has parked, and what each is waiting on."""
        listing(root, principal, everything, as_json)

    @app.command("show")
    def show_cmd(
        question: str = typer.Argument(help="The question id"),
        as_json: bool = typer.Option(False, "--json", help="Emit JSON"),
        open_externally: bool = typer.Option(
            False,
            "--open",
            help="Open each change in this machine's difftool from sync.json.local",
        ),
    ) -> None:
        """Show one question whole, including the operation it would resume."""
        show(root, question, as_json, open_externally)

    @app.command("answer")
    def answer_cmd(
        question: str = typer.Argument(help="The question id"),
        principal: str = typer.Option(..., "--as", help="Who is answering"),
        note: str = typer.Option(
            "", "--note", help="What the agent should know alongside the answer"
        ),
    ) -> None:
        """Approve one question, optionally with a note for the agent."""
        answer(root, question, principal, True, note)

    @app.command("reject")
    def reject_cmd(
        question: str = typer.Argument(help="The question id"),
        principal: str = typer.Option(..., "--as", help="Who is answering"),
        note: str = typer.Option("", "--note", help="What the agent should do instead"),
    ) -> None:
        """Refuse one question, optionally saying what to do instead."""
        answer(root, question, principal, False, note)

    @app.command("cancel")
    def cancel_cmd(
        question: str = typer.Argument(help="The question id"),
        reason: str = typer.Option("", "--reason", help="Why it is being withdrawn"),
    ) -> None:
        """Withdraw a question nobody needs answered any more."""
        cancel(root, question, reason)

    return app
