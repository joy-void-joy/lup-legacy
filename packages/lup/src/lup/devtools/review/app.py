"""The surface a reviewer answers a parked review through.

Every final ask is written to the relay before anybody sees it, which is what
makes a detached run answerable at all — but a durable record with no way to
read it is a queue that silently grows. So this is the other half: list what
is waiting, show one in full, and approve, decline, or cancel it — and the
projection of one review into files, hunks and captured evidence that the
dashboard reads it through.

The requester can read its own review's status here and cannot answer it.
That is enforced on the record rather than by leaving the command out of an
agent's tool list: a policy whose reviewer is "whoever could reach the
command" is one that changes when somebody adds a tool.
"""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from difflib import SequenceMatcher
from hashlib import sha256
from pathlib import Path
from tempfile import mkdtemp
from typing import Literal
from uuid import NAMESPACE_URL, uuid5

import sh
import typer
from pydantic import BaseModel
from rich.console import Console
from rich.syntax import Syntax

from lup.coordination.bare import store as roster
from lup.coordination.repository import RepositoryPeers
from lup.devtools.dashboard.companion import refuse_inside_a_session
from lup.devtools.review.wait import wait_on
from lup.devtools.review.notifications import (
    ReviewNotification,
    ReviewNotifications,
)
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
from lup.policy.relay import (
    CapturedFileReview,
    PersistentQuestion,
    QuestionRelay,
    UnpreviewedStep,
)
from lup.policy.review import (
    ReviewedFile,
    reviewed_files,
    reviewed_preview,
)
from lup.providers.harness import patch_review


class ReviewRoot(BaseModel, frozen=True):
    """One operator-selected checkout, its opaque browser address, and its repository."""

    id: str
    path: str
    repository: str = ""
    """The repository it is a checkout of, by its shared git directory."""

    repository_name: str = ""
    """What a reader calls that repository."""

    @classmethod
    def of(cls, root: Path) -> "ReviewRoot":
        return cls(id=str(uuid5(NAMESPACE_URL, root.as_uri())), path=str(root))

    def within(self, repository: Path) -> "ReviewRoot":
        """This checkout, grouped under the repository it belongs to."""
        name = (
            repository.parent.name
            if repository.name == ".git"
            else repository.name.removesuffix(".git")
        )
        return self.model_copy(
            update={"repository": str(repository), "repository_name": name}
        )


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
    session: str = ""
    """What the roster calls the session that asked, where it knows it."""

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
            if ReviewAttribution.of(change, question.file_reviews).effect
            not in {"allow", "defer"}
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

    effect: Literal["allow", "ask", "deny", "defer", "unknown"] = "unknown"
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
        return cls(effect=row.effect, reason=row.reason, rules=row.rules)


class ReviewSuppression(BaseModel, frozen=True):
    """A result document's explicit rule exception, located for review."""

    line: int
    rule_ids: list[str] | None
    reason: str
    introduced: bool
    review_effect: Literal["allow", "ask", "deny", "defer", "unknown"] = "unknown"
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
    review_effect: Literal["allow", "ask", "deny", "defer", "unknown"] = "unknown"
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


class Sighting(BaseModel, frozen=True):
    """What the roster says of one session: what it is called, and whether it runs."""

    name: str = ""
    running: bool


class RequesterPresence(BaseModel, frozen=True):
    """Every session one repository's roster knows, by each identity it answers to.

    Read once for a whole checkout, so asking after every review in a long
    record costs one roster read rather than one each.
    """

    sessions: dict[str, Sighting] = {}

    @classmethod
    def of(cls, root: Path) -> "RequesterPresence":
        peers = RepositoryPeers(root)
        members = peers.present()
        called = roster.called(peers.root)

        def sighted(running: bool) -> dict[str, Sighting]:
            return {
                identity: Sighting(
                    name=called[member.actor.id] if member.actor.id in called else "",
                    running=member.running,
                )
                for member in members
                if member.running is running
                for identity in (member.actor.id, member.wake.session)
                if identity
            }

        return cls(sessions={**sighted(False), **sighted(True)})

    def sightings(self, question: PersistentQuestion) -> list[Sighting]:
        """What the roster knows of the session that asked, by either identity."""
        operation = question.operation
        return [
            self.sessions[identity]
            for identity in (operation.requester, operation.session)
            if identity and identity in self.sessions
        ]

    def called(self, question: PersistentQuestion) -> str:
        """What the session that asked is called: its roster name, else its id."""
        operation = question.operation
        return next(
            (sighting.name for sighting in self.sightings(question) if sighting.name),
            operation.requester or operation.session,
        )

    def gone(
        self, question: PersistentQuestion, now: datetime, grace: timedelta
    ) -> bool:
        """Whether no session that could retry this question runs any more.

        Gone at once where the roster saw its requester leave; after ``grace``
        where the roster never knew it, since a session the roster never
        recorded cannot be told apart from one that has not joined yet.
        """
        sightings = self.sightings(question)
        if any(sighting.running for sighting in sightings):
            return False
        return bool(sightings) or now - question.created >= grace


def expire_orphaned(
    root: Path,
    presence: RequesterPresence | None = None,
    grace: timedelta = timedelta(hours=1),
) -> list[PersistentQuestion]:
    """Expire every review in this checkout whose requester is gone.

    Its answer could release nothing: a native retry comes only from the
    session that asked, and a coordinator dispatches only for the run that
    parked it. Left pending, such a review is a question nobody is waiting
    on, and a queue of them buries the ones somebody is.
    """
    seen = presence if presence is not None else RequesterPresence.of(root)
    now = datetime.now(UTC)
    store = relay(root)
    return store.expire(
        [entry.id for entry in store.pending() if seen.gone(entry, now, grace)],
        "its requester ended: no session that could retry it is running",
    )


def listing(root: Path, principal: str, everything: bool, as_json: bool) -> None:
    """Print what is waiting, narrowed to what this principal may answer.

    Narrowed by eligibility rather than by ownership, because a supervisor
    shown a question it may not answer is a supervisor about to try — and the
    refusal it then gets teaches it nothing about which questions are its.
    A review whose requester is gone is expired first, so what is listed as
    waiting is something a session still waits on.
    """
    expire_orphaned(root)
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
    for step in entry.unpreviewed or []:
        console.print(f"  {unpreviewed_caption(step)}  {step.command}", style="bold")
        for path in step.paths:
            console.print(f"    {path}")
    return rendered


def unpreviewed_caption(step: UnpreviewedStep) -> str:
    """What a step no document shows is, in the words a reviewer reads it by."""
    match step.cause:
        case "run":
            return "result known only after running"
        case "unread":
            return "leaves a file that is not text"


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
        typer.echo(f"no review {question!r} is recorded", err=True)
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

    The operator's alone: the answer is written into the host's state, which
    a session's own command has no business writing, so a launched session
    is refused before anything is read. The refusal path prints the relay's
    own message rather than a generic one, because every way this can fail
    is a distinct thing the reviewer needs to know: the question is gone,
    already answered, expired, altered, or theirs to read and not to answer.
    """
    try:
        refuse_inside_a_session(f"review {'approve' if approved else 'decline'}")
        settled = relay(root).answer(question, principal, approved, note)
    except (PermissionError, ValueError) as refusal:
        typer.echo(str(refusal), err=True)
        raise typer.Exit(2) from refusal
    verb = {"approved": "approved", "rejected": "declined"}
    typer.echo(
        f"{settled.id}: {verb[settled.state] if settled.state in verb else settled.state}"
    )
    if settled.state == "approved":
        if settled.resumption == "native_retry":
            typer.echo(
                "the session's `review wait` carries it out where the files still "
                "stand as they did, or one exact retry of the call does; the "
                "approval is spent once"
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


def create_review_app(root: Path) -> typer.Typer:
    """Wire the `review` group: the reviewer's verbs over one checkout's relay."""
    app = typer.Typer(no_args_is_help=True)

    @app.command("list")
    def list_cmd(
        principal: str = typer.Option(
            "", "--as", help="Show only what this principal may answer"
        ),
        everything: bool = typer.Option(
            False, "--all", help="Include answered, expired, and cancelled reviews"
        ),
        as_json: bool = typer.Option(False, "--json", help="Emit JSON"),
    ) -> None:
        """List the reviews parked in this checkout, and what each is waiting on."""
        listing(root, principal, everything, as_json)

    @app.command("show")
    def show_cmd(
        review: str = typer.Argument(help="The review id"),
        as_json: bool = typer.Option(False, "--json", help="Emit JSON"),
        open_externally: bool = typer.Option(
            False,
            "--open",
            help="Open each change in this machine's difftool from sync.json.local",
        ),
    ) -> None:
        """Show one review whole, including the operation it would resume."""
        show(root, review, as_json, open_externally)

    @app.command("approve")
    def approve_cmd(
        review: str = typer.Argument(help="The review id"),
        principal: str = typer.Option(..., "--as", help="Who is answering"),
        note: str = typer.Option(
            "", "--note", help="What the agent should know alongside the answer"
        ),
    ) -> None:
        """Approve one review, optionally with a note for the agent."""
        answer(root, review, principal, True, note)

    @app.command("decline")
    def decline_cmd(
        review: str = typer.Argument(help="The review id"),
        principal: str = typer.Option(..., "--as", help="Who is answering"),
        note: str = typer.Option("", "--note", help="What the agent should do instead"),
    ) -> None:
        """Decline one review, optionally saying what to do instead."""
        answer(root, review, principal, False, note)

    @app.command("cancel")
    def cancel_cmd(
        review: str = typer.Argument(help="The review id"),
        reason: str = typer.Option("", "--reason", help="Why it is being withdrawn"),
    ) -> None:
        """Withdraw a review nobody needs answered any more."""
        cancel(root, review, reason)

    @app.command("wait")
    def wait_cmd(
        reviews: list[str] | None = typer.Argument(
            None,
            help="The reviews to wait on; none named waits on every review this "
            "session and its subagents have waiting",
        ),
        first: bool = typer.Option(
            False, "--any", help="Return once the first of them settles"
        ),
        timeout: float | None = typer.Option(
            None,
            "--timeout",
            min=0,
            help="Stop after this many seconds with a review still waiting "
            "(exit 3); unset, wait until every one settles",
        ),
    ) -> None:
        """Wait on this session's reviews, carrying out each one the operator approves.

        Reports each as it settles: an approved edit is written as the operator
        saw it, an approved command runs where it was asked, a declined one
        brings the operator's note. Start it in the background and carry on;
        it waits as long as the operator takes, saying now and then that it
        still is.
        """
        raise typer.Exit(wait_on(root, reviews or [], first, timeout))

    return app
