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

import threading
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from difflib import SequenceMatcher
from hashlib import sha256
from pathlib import Path
from tempfile import mkdtemp
from typing import Literal
from uuid import NAMESPACE_URL, uuid5

import sh
import typer
from pydantic import BaseModel, ValidationError
from rich.console import Console
from rich.syntax import Syntax

from lup.coordination.bare import store as roster
from lup.coordination.identity import session_member_id
from lup.coordination.repository import RepositoryPeers
from lup.devtools.dashboard.companion import refuse_inside_a_session
from lup.devtools.review.preimages import MovedPreimage, moved
from lup.devtools.review.propose import (
    MANIFEST,
    ProposalRefused,
    asking_agent,
    gathered,
    parked,
    proposal_of,
    previewed,
)
from lup.devtools.review.thread import ReviewThread, ThreadEntry, spoken_on
from lup.devtools.review.wait import (
    Asker,
    kept_elsewhere,
    resume_command,
    review_command,
    wait_on,
)
from lup.devtools.review.notifications import (
    ReviewNotification,
    ReviewNotifications,
    notify_requester,
)
from lup.devtools.sync import registered_difftool
from lup.devtools.utils import output_json
from lup.harness.models import HookSet
from lup.harness.codescan.markers import (
    MarkerScan,
    NoteKind,
    find_feedback,
    scan_mode_for,
)
from lup.policy.kernel.edit import (
    IGNORE_RE,
    added_line_numbers,
    ignore_rule_ids,
    removed_lines,
    resites_a_suppression,
    written_suppression,
)
from lup.policy.assets.host import (
    append_review_record,
    checkout_home,
    worktree_root,
)
from lup.policy.relay import (
    AppendedRecords,
    CapturedFileReview,
    PersistentQuestion,
    QuestionRecord,
    QuestionRelay,
    RecordedQuestion,
    RecordedRemark,
    Reply,
    UnpreviewedStep,
)
from lup.policy.review import ReviewedFile
from lup.types import JsonObject
from lup.workspace.paths import manifest_table


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


def terminal_answer(root: Path, question: str, principal: str) -> str:
    """The commands that answer one review from a terminal, with the code of the checkout keeping it.

    The way out of a review this reader's code cannot answer: a session's
    reviews are kept in the checkout whose code parks them, so that
    checkout's review commands read what an older reader could not.
    """
    approve = review_command(root, ["approve", question, "--as", principal])
    decline = review_command(root, ["decline", question, "--as", principal])
    return f"`{approve}` or `{decline}`, from a terminal outside every session"


def newer_code(restarting: bool) -> str:
    """What a reader about to restart onto newer code adds to a review it cannot read."""
    return (
        " The dashboard restarts onto its checkout's newer code shortly, which "
        "may answer it here."
        if restarting
        else ""
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
    unanswerable: str = ""
    """Why nobody on this page may answer it, where it is waiting and cannot be answered."""

    stale: list[MovedPreimage] = []
    """The recorded files that no longer stood as recorded, which is why it went stale.

    Only a ``stale`` review carries any: a waiting one whose file moves is
    retired the moment that is noticed, and keeps what moved.
    """
    said: int = 0
    """How many remarks and replies its thread holds beside the answer."""

    target: str = ""
    """The checkout the call changes, which the paths in its title are relative to."""

    session: str = ""
    """What the roster calls the session that asked, where it knows it."""

    settled: datetime | None = None
    """When it left the waiting queue, which History is ordered by; nothing while it waits."""

    archived: bool = False
    """Whether it was retired to the archive, keeping its record and summary but not its documents."""

    @staticmethod
    def key_for(root: Path, question_id: str) -> str:
        """Identify one root's question independently of its display projection."""
        return str(uuid5(NAMESPACE_URL, f"{root.as_uri()}#{question_id}"))

    @classmethod
    def of(
        cls,
        root: Path,
        question: PersistentQuestion,
        principal: str,
        said: int = 0,
        restarting: bool = False,
    ) -> "ReviewSummary":
        return cls.from_files(
            root, question, principal, previewed(question).files, said, restarting
        )

    @classmethod
    def from_files(
        cls,
        root: Path,
        question: PersistentQuestion,
        principal: str,
        complete: list[ReviewedFile],
        said: int = 0,
        restarting: bool = False,
    ) -> "ReviewSummary":
        """Project the same file preview already used by this response's detail.

        *restarting* says the reader is about to restart onto newer code,
        which a review this code cannot read may be answered by.
        """
        operation = question.operation
        files = [
            change
            for change in complete
            if ReviewAttribution.of(change, question.file_reviews).effect
            not in {"allow", "defer"}
        ]

        def holding() -> Path:
            """The checkout every file lies in, which the paths are read relative to.

            Read off the files themselves, since the session that asked may
            sit in another checkout than the one its call changes; the
            checkout recorded with the call where the files lie in none, or
            in several.
            """
            match sorted({worktree_root(str(file.path)) for file in files}):
                case [str(only)] if only:
                    return Path(only)
            if operation.worktree.is_absolute() and all(
                file.path.is_relative_to(operation.worktree) for file in files
            ):
                return operation.worktree
            return root

        target = holding()

        def relative(path: Path) -> str:
            return str(
                path.relative_to(target) if path.is_relative_to(target) else path
            )

        def unanswerable() -> str:
            """Why a waiting review cannot be answered here, and the way out, or nothing where it can."""
            if question.state != "pending":
                return ""
            if question.overdue():
                return "It expired before anybody answered it."
            if principal == operation.requester:
                return "The session that asked cannot answer its own review."
            if not question.answerable_by(principal):
                eligible = ", ".join(question.eligible) or "nobody"
                return f"Only {eligible} may answer it" + (
                    f": {terminal_answer(root, question.id, question.eligible[0])}."
                    if question.eligible
                    else "."
                )
            if unverifiable := question.unverifiable():
                return (
                    f"{unverifiable.capitalize()}. The code that parked it can "
                    f"answer it: {terminal_answer(root, question.id, principal)}."
                    + newer_code(restarting)
                )
            if not question.bound():
                return (
                    "Its record changed after it was parked: what it shows is not "
                    "what its fingerprint covers, so nothing here may answer it. "
                    "Where newer code parked it, that code can: "
                    f"{terminal_answer(root, question.id, principal)}."
                    + newer_code(restarting)
                )
            return ""

        refused = unanswerable()

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
            state="expired" if question.overdue() else question.state,
            requester=operation.requester,
            reason=question.reason,
            title=described(),
            paths=[relative(file.path) for file in files],
            total_files=len(complete),
            operation=operation.summary(),
            rule=question.rule,
            created=question.created,
            answerable=question.state == "pending" and not refused,
            unanswerable=refused,
            stale=[
                MovedPreimage(path=path, cause="changed") for path in question.moved
            ],
            said=said,
            target=str(target),
            settled=settled_at(question),
        )

    @classmethod
    def retired(cls, root: Path, entry: "ArchivedReview") -> "ReviewSummary":
        """An archived review's row, from what the archive kept of how it read."""
        question = entry.question
        return cls(
            key=cls.key_for(root, question.id),
            root_id=ReviewRoot.of(root).id,
            id=question.id,
            state=question.state,
            requester=question.operation.requester,
            reason=question.reason,
            title=entry.title,
            paths=entry.paths,
            total_files=entry.total_files,
            operation=question.operation.summary(),
            rule=question.rule,
            created=question.created,
            answerable=False,
            stale=[
                MovedPreimage(path=path, cause="changed") for path in question.moved
            ],
            said=sum(said.kind != "answer" for said in entry.thread),
            target=entry.target,
            settled=settled_at(question),
            archived=True,
        )


def settled_at(question: QuestionRecord) -> datetime | None:
    """When a review left the waiting queue: when it came to its state, its expiry for one that lapsed, nothing while it waits."""
    if question.state != "pending":
        return question.since()
    return question.expires if question.overdue() else None


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
    """A contiguous group of changes with unchanged context around it.

    Where it sits in each document, as the first and last line it covers:
    what lies between two hunks, and around them, is unchanged, so a reader
    asking for the whole file in context reads those lines off the documents
    by these numbers. A side the hunk takes no line of ends before it starts.
    """

    header: str
    lines: list[ReviewLine]
    old_start: int
    old_end: int
    new_start: int
    new_end: int

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
                old_start=group[0].old_start + 1,
                old_end=group[-1].old_stop,
                new_start=group[0].new_start + 1,
                new_end=group[-1].new_stop,
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


class ReviewMarker(BaseModel, frozen=True):
    """One `# lup:` marker in a document the review shows, located and classified.

    Every kind the vocabulary has, found the way `dev comments` finds them
    and the edit gate counts them: an open note, parked work (``condition``
    carrying a `defer[<gate>]:` head's gate), a resolution claim, a
    customization point, and a rule exception. ``side`` says which document
    the lines number.
    """

    side: Literal["before", "after"]
    line: int
    end_line: int
    kind: NoteKind | Literal["ignore"]
    condition: str | None = None
    text: str

    @classmethod
    def in_document(
        cls, change: ReviewedFile, side: Literal["before", "after"]
    ) -> list["ReviewMarker"]:
        """Every marker in one side of a change, in the order it stands."""
        source = change.before if side == "before" else change.after
        if not source:
            return []
        notes = [
            cls(
                side=side,
                line=note.start_line,
                end_line=note.end_line,
                kind=note.kind,
                condition=note.condition,
                text=note.text,
            )
            for note in find_feedback(source, scan_mode_for(change.path))
        ]
        exceptions = [
            cls(
                side=side,
                line=directive.marker.line,
                end_line=directive.marker.line,
                kind="ignore",
                text=directive.text,
            )
            for directive in ReviewSuppression.in_source(change, source)
        ]
        return sorted([*notes, *exceptions], key=lambda marker: marker.line)


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
    markers: list[ReviewMarker] = []
    """Every `# lup:` marker on either side, the ones the change leaves alone included."""

    review_effect: Literal["allow", "ask", "deny", "defer", "unknown"] = "unknown"
    review_reason: str = "No per-file decision was captured."
    about: str = ""
    """What the requester said of this file, where a proposal of several said anything."""

    @classmethod
    def of(
        cls,
        change: ReviewedFile,
        evidence: list[CapturedFileReview] | None = None,
        about: str = "",
    ) -> "ReviewFile":
        attribution = ReviewAttribution.of(change, evidence)
        suppressions = ReviewSuppression.of(change, attribution)
        marked = {suppression.line for suppression in suppressions}
        hunks = [
            hunk.model_copy(
                update={
                    "lines": [
                        line.model_copy(update={"suppression": line.new_line in marked})
                        for line in hunk.lines
                    ]
                }
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
            markers=[
                *ReviewMarker.in_document(change, "before"),
                *ReviewMarker.in_document(change, "after"),
            ],
            review_effect=attribution.effect,
            review_reason=attribution.reason,
            about=about,
        )


def requested_command(question: QuestionRecord) -> str | None:
    """The command a shell call asks to run, or ``None`` where the call is not one."""
    payload = question.operation.payload
    requested = payload["command"] if "command" in payload else None
    return (
        requested
        if question.operation.tool == "Bash" and isinstance(requested, str)
        else None
    )


class ReviewDocuments(BaseModel, frozen=True):
    """What a review shows of the documents it binds, worked out once for its fingerprint.

    Each file as a diff with its markers and exceptions, and why any is not
    shown: nothing here moves while the review keeps its fingerprint, so a
    reader that shows the same review again -- the answer that settles it
    included -- reuses it rather than diffing every document anew.
    """

    changes: list[ReviewedFile]
    files: list[ReviewFile]
    unavailable: str
    notice: str = ""

    @classmethod
    def of(cls, entry: PersistentQuestion) -> "ReviewDocuments":
        preview = previewed(entry)
        proposal = proposal_of(entry)
        about = (
            {proposed.path: proposed.about for proposed in proposal.files}
            if proposal is not None
            else {}
        )
        return cls(
            changes=preview.files,
            files=[
                ReviewFile.of(
                    change,
                    entry.file_reviews,
                    about[change.path] if change.path in about else "",
                )
                for change in preview.files
            ],
            unavailable=preview.unavailable,
            notice=preview.notice,
        )


class ReviewDetail(BaseModel, frozen=True):
    """Everything the operator reviews before answering.

    ``question`` is the record with its call whole and each document named
    by digest (:meth:`~lup.policy.relay.PersistentQuestion.shown`); the
    documents themselves are ``files``.
    """

    summary: ReviewSummary
    question: RecordedQuestion
    files: list[ReviewFile]
    command: str | None
    preview_unavailable: str
    preview_notice: str = ""
    notification: ReviewNotification | None = None
    thread: list[ThreadEntry] = []
    """Everything said on it -- the operator's remarks, the requester's replies, the answer -- oldest first."""

    @classmethod
    def of(
        cls,
        root: Path,
        entry: PersistentQuestion,
        principal: str,
        store: QuestionRelay | None = None,
        documents: ReviewDocuments | None = None,
    ) -> "ReviewDetail":
        """One review whole, its thread read through *store* and its documents reused where given."""
        shown = documents if documents is not None else ReviewDocuments.of(entry)
        thread = ReviewThread.of(store if store is not None else relay(root)).said(
            entry
        )
        return cls(
            summary=ReviewSummary.from_files(
                root,
                entry,
                principal,
                shown.changes,
                sum(said.kind != "answer" for said in thread),
            ),
            question=entry.shown(),
            files=shown.files,
            command=requested_command(entry),
            preview_unavailable=shown.unavailable,
            preview_notice=shown.notice,
            notification=ReviewNotifications(root=root).read(entry),
            thread=thread,
        )

    @classmethod
    def retired(cls, root: Path, entry: "ArchivedReview") -> "ReviewDetail":
        """An archived review, as far as the archive kept it: its record, its summary and its thread."""
        return cls(
            summary=ReviewSummary.retired(root, entry),
            question=entry.question,
            files=[],
            command=requested_command(entry.question),
            preview_unavailable=(
                f"Archived {entry.archived:%Y-%m-%d}, past the retention window: "
                "its documents left the relay's store, and its record, summary "
                "and thread are what remain."
            ),
            notification=ReviewNotifications(root=root).read(entry.question),
            thread=entry.thread,
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
    def of(cls, question: QuestionRecord) -> "QuestionView":
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

    def sightings(self, question: QuestionRecord) -> list[Sighting]:
        """What the roster knows of the session that asked, by each identity it asked under.

        Its runtime's ids, and the roster member its launch named, which
        outlives them: a session resumed under a new runtime id keeps its
        member and hands its commands the id it started with, so a review a
        command parked names an id whose row has ended while the session that
        asked still runs, and `review wait` still takes it as that session's.
        """
        operation = question.operation
        return [
            self.sessions[identity]
            for identity in (operation.requester, operation.session, question.member)
            if identity and identity in self.sessions
        ]

    def called(self, question: QuestionRecord) -> str:
        """What the session that asked is called: its roster name, else its id."""
        operation = question.operation
        return next(
            (sighting.name for sighting in self.sightings(question) if sighting.name),
            operation.requester or operation.session,
        )

    def gone(self, question: QuestionRecord, now: datetime, grace: timedelta) -> bool:
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
    store: QuestionRelay | None = None,
) -> list[RecordedQuestion]:
    """Expire every review in this checkout whose requester is gone.

    Its answer could release nothing: a native retry comes only from the
    session that asked, and a coordinator dispatches only for the run that
    parked it. Left pending, such a review is a question nobody is waiting
    on, and a queue of them buries the ones somebody is. The roster is read
    only where something waits.
    """
    held = store if store is not None else relay(root)
    waiting = held.pending()
    if not waiting:
        return []
    seen = presence if presence is not None else RequesterPresence.of(root)
    now = datetime.now(UTC)
    return held.expire(
        [entry.id for entry in waiting if seen.gone(entry, now, grace)],
        "its requester ended: no session that could retry it is running",
    )


def retire_moved(
    root: Path, by: str, store: QuestionRelay | None = None
) -> list[RecordedQuestion]:
    """Retire every waiting review in this checkout a recorded file moved under.

    No approval could release one any more, so it leaves the queue as
    ``stale`` and its requester asks again against what stands now.
    """
    held = store if store is not None else relay(root)
    return [
        held.retire_stale(entry.id, [each.path for each in drifted], by)
        for entry in held.pending()
        if (drifted := moved(entry))
    ]


def at_rest(question: QuestionRecord) -> bool:
    """Whether a review came to a state nothing moves it out of again.

    Carried out, whether or not it succeeded or anybody saw it finish;
    declined; withdrawn; lapsed; or retired as stale.
    """
    match question.state:
        case (
            "completed"
            | "failed"
            | "in_doubt"
            | "rejected"
            | "cancelled"
            | "expired"
            | "stale"
        ):
            return True
    return False


def review_retention_days(root: Path) -> int:
    """How many days a settled review keeps its documents before it moves to the archive.

    A week unless the checkout's `pyproject.toml` says otherwise, as
    ``review-retention-days`` under ``[tool.lup]``: long enough to reread
    what a week of sessions asked and was answered, diffs and all; short
    enough that the relay holds a week's reviews rather than every one ever
    parked, which is what every reader of it -- a hook on each call, the
    dashboard every second -- pays for. Past it the archive keeps each
    review's record, summary and thread, never its documents.
    """
    match manifest_table(root / "pyproject.toml"):
        case {"tool": {"lup": {"review-retention-days": int(days)}}} if (
            days >= 0 and not isinstance(days, bool)
        ):
            return days
    return 7


class ArchivedReview(BaseModel, frozen=True):
    """One settled review the relay retired, as the archive keeps it.

    Its record, as the relay kept it; how it read -- its title, the files it
    named, the checkout they are relative to -- worked out while its
    documents were still in the store, since they left with it; and
    everything said on it.
    """

    question: RecordedQuestion
    title: str
    paths: list[str]
    total_files: int
    target: str
    thread: list[ThreadEntry] = []
    archived: datetime

    @classmethod
    def of(
        cls,
        root: Path,
        store: QuestionRelay,
        entry: RecordedQuestion,
        remarks: dict[str, list[RecordedRemark]],
        replies: dict[str, list[Reply]],
        at: datetime,
    ) -> "ArchivedReview":
        """One review as it reads now, kept for after its documents are gone.

        A review whose documents cannot be read back is summarized from its
        record alone, which names its command or tool.
        """
        thread = spoken_on(entry, remarks, replies)
        said = sum(each.kind != "answer" for each in thread)
        try:
            summary = ReviewSummary.of(root, store.resolve(entry), "operator", said)
        except ValueError:
            summary = ReviewSummary.from_files(
                root,
                PersistentQuestion.model_validate(
                    entry.model_dump(exclude={"preconditions", "file_reviews"})
                ),
                "operator",
                [],
                said,
            )
        return cls(
            question=entry,
            title=summary.title,
            paths=summary.paths,
            total_files=summary.total_files,
            target=summary.target,
            thread=thread,
            archived=at,
        )


class ArchivedReviews(BaseModel):
    """What one archive has read so far: each review it keeps, by id."""

    kept: dict[str, ArchivedReview] = {}


class ReviewArchive:
    """One relay's archive of retired reviews, beside its store, read from where the last read stopped.

    Appended to as reviews retire, and read back by id: a review archived
    twice -- two sweeps that both caught it before either removed it -- is
    the later copy.
    """

    def __init__(self, store: QuestionRelay) -> None:
        self.path = store.store / "archive.jsonl"
        self.log = AppendedRecords(self.path)
        self.read = ArchivedReviews()
        self.reading = threading.Lock()

    def reviews(self) -> dict[str, ArchivedReview]:
        """Every archived review, by id."""

        def valid(records: list[JsonObject]) -> Iterator[ArchivedReview]:
            for record in records:
                try:
                    yield ArchivedReview.model_validate(record)
                except ValidationError:
                    continue

        with self.reading:
            appended = self.log.read()
            if appended.began:
                self.read = ArchivedReviews()
            self.read.kept.update(
                {archived.question.id: archived for archived in valid(appended.records)}
            )
            return dict(self.read.kept)

    def keep(self, reviews: list[ArchivedReview]) -> None:
        """Append these reviews to the archive."""
        for review in reviews:
            append_review_record(self.path, review.model_dump_json())


def retire_settled(
    root: Path,
    store: QuestionRelay | None = None,
    archive: ReviewArchive | None = None,
    now: datetime | None = None,
    batch: int = 200,
) -> list[str]:
    """Move each review settled longer ago than the retention window into the archive.

    Worked out from the fold already read, so a checkout with nothing past
    the window costs nothing more. Each one's summary and thread are worked
    out while its documents are still in the store, kept in the archive
    under the relay's lock, and only then dropped from the log, its
    documents with it (:meth:`~lup.policy.relay.QuestionRelay.retire`). At
    most *batch* a call, so a checkout with a long history catching up
    spreads the work over several sweeps rather than holding one.
    """
    held = store if store is not None else relay(root)
    moment = now or datetime.now(UTC)
    window = timedelta(days=review_retention_days(root))
    due = [
        entry
        for entry in held.questions()
        if at_rest(entry) and moment - entry.since() >= window
    ][:batch]
    if not due:
        return []
    kept_in = archive if archive is not None else ReviewArchive(held)
    remarks, replies = held.remarks(), held.replies()
    archived = [
        ArchivedReview.of(root, held, entry, remarks, replies, moment) for entry in due
    ]
    held.retire([entry.id for entry in due], lambda: kept_in.keep(archived))
    return [entry.id for entry in due]


def listing(root: Path, principal: str, everything: bool, as_json: bool) -> None:
    """Print what is waiting, narrowed to what this principal may answer.

    Narrowed by eligibility rather than by ownership, because a supervisor
    shown a question it may not answer is a supervisor about to try — and the
    refusal it then gets teaches it nothing about which questions are its.
    A review whose requester is gone is expired first, one a recorded file
    moved under is retired as stale, and one settled past the retention
    window moves to the archive, so what is listed as waiting is something a
    session still waits on. ``--all`` lists the archived ones too, first,
    from what the archive kept.
    """
    store = relay(root)
    expire_orphaned(root, store=store)
    retire_moved(root, "review list", store)
    retire_settled(root, store)
    archived = (
        [kept.question for kept in ReviewArchive(store).reviews().values()]
        if everything
        else []
    )
    questions: list[QuestionRecord] = [
        *archived,
        *(store.questions() if everything else store.pending(principal)),
    ]
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
    return previewed(entry).files


def render_diffs(entry: PersistentQuestion, console: Console) -> bool:
    """Print what each file would become, and say whether anything was shown.

    A proposal's note on a file is printed whole under its name, as the page
    shows it on the file's header.

    A diff rather than the payload, because a payload carrying the new
    contents with the preimage printed underneath is two documents a reviewer
    compares by eye — which is the whole of what was wrong with this surface,
    and most of why an operator would rather answer somewhere else.
    """
    rendered = False
    proposal = proposal_of(entry)
    notes = (
        {proposed.path: proposed.about for proposed in proposal.files}
        if proposal is not None
        else {}
    )
    for change in changes(entry):
        rendered = True
        console.print(f"  {change.operation():<9} {change.path}", style="bold")
        if change.path in notes and notes[change.path]:
            console.print(
                f"    agent's note: {notes[change.path]}", markup=False, highlight=False
            )
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
    against a summary is an approval of the summary. A review retired to the
    archive shows what the archive kept of it, since its documents are gone.
    """
    store = relay(root)
    found = store.find(question)
    kept = ReviewArchive(store).reviews() if found is None else {}
    if found is None and question in kept:
        shown_archived(kept[question], as_json)
        return
    if found is None:
        typer.echo(f"no review {question!r} is recorded", err=True)
        raise typer.Exit(2)
    try:
        entry = store.resolve(found)
    except ValueError as unread:
        typer.echo(f"review {question!r} cannot be read back whole: {unread}", err=True)
        raise typer.Exit(2) from unread
    if as_json:
        output_json(entry.model_dump(mode="json"))
        return
    typer.echo(entry.summary())
    typer.echo(f"  requester   {entry.operation.requester}")
    typer.echo(
        f"  eligible    {', '.join(entry.eligible) or 'nobody'}"
        if entry.chain_resolved
        else "  eligible    anyone but the requester: no supervisor chain was resolved"
    )
    typer.echo(f"  purpose     {entry.purpose or 'unclassified'}")
    typer.echo(f"  rule        {entry.rule or 'unattributed'}")
    typer.echo(f"  fingerprint {entry.fingerprint}")
    if entry.escalation:
        typer.echo(f"  escalated   {entry.escalation}")
    if entry.checkpoint_failure:
        typer.echo(f"  capture     failed: {entry.checkpoint_failure}")
    for said in entry.account:
        typer.echo(f"  {said.source:<11} {said.text}")
    segments = entry.segments or []
    for segment in segments if len(segments) > 1 else []:
        typer.echo(f"  {segment.effect:<11} {segment.command or 'the line as a whole'}")
        if segment.effect != "allow":
            typer.echo(f"              {segment.reason}")
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
    told(entry, ReviewThread.of(store).said(entry))


def told(entry: QuestionRecord, thread: list[ThreadEntry]) -> None:
    """Print how a review was answered, and everything said on it."""
    if entry.answer is not None:
        typer.echo(
            f"  answered    {'yes' if entry.answer.approved else 'no'}"
            f" by {entry.answer.principal} ({entry.answer.receipt})"
        )
    for said in thread:
        match said.kind:
            case "answer":
                heading = "note"
            case "remark":
                heading = f"remark by {said.author}"
            case "reply":
                heading = f"reply by {said.author}"
        if said.text:
            typer.echo(f"  {heading}: {said.text}")
        for comment in said.comments:
            typer.echo(f"    {comment.spelled(entry.operation.worktree)}")


def shown_archived(kept: ArchivedReview, as_json: bool) -> None:
    """Print a review the archive keeps: its record, how it read, and what was said on it."""
    if as_json:
        output_json(kept.model_dump(mode="json"))
        return
    entry = kept.question
    typer.echo(entry.summary())
    typer.echo(f"  requester   {entry.operation.requester}")
    typer.echo(f"  rule        {entry.rule or 'unattributed'}")
    typer.echo(f"  fingerprint {entry.fingerprint}")
    typer.echo(
        f"  archived    {kept.archived:%Y-%m-%d}, its documents gone: {kept.title}"
    )
    for path in kept.paths:
        typer.echo(f"    {path}")
    told(entry, kept.thread)


def reply(root: Path, question: str, text: str) -> None:
    """Answer the operator on this session's own review, which the page shows in its thread.

    The requester's alone, checked the way `review wait` checks it: the
    session that asked, by its runtime's id or the roster member its launch
    named. It settles nothing; the review waits as it did.
    """
    store = relay(root)
    entry = store.find(question)
    if entry is None:
        elsewhere = kept_elsewhere(root, ["reply", question, text])
        typer.echo(
            f"no review {question!r} is recorded here"
            + (f"; {elsewhere}" if elsewhere else ""),
            err=True,
        )
        raise typer.Exit(2)
    asker = Asker.here(root)
    if not asker.asked(entry):
        typer.echo(
            f"review {question} was asked by another session; only the session "
            "that asked may reply on it",
            err=True,
        )
        raise typer.Exit(2)
    ReviewThread.of(store).reply(entry, asker.member or entry.operation.session, text)
    typer.echo(
        f"{entry.id}: reply recorded; the operator reads it on the review, "
        f"which is still {entry.state}"
    )


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

    The session that asked hears of it as it hears of an answer given on the
    dashboard, by its one channel: its waiter where one holds the review,
    else its mailbox and its wake, since a session's own conversation holds
    no waiter.
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
    if settled.answer is not None:
        notifications = ReviewNotifications(root=root)
        heard = notifications.complete(
            settled,
            notifications.prepare(settled),
            lambda: notify_requester((root,), root, settled),
        )
        typer.echo(f"the session that asked: {heard.detail}")
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
        elsewhere = kept_elsewhere(root, ["cancel", question, "--reason", reason])
        typer.echo(f"{refusal}; {elsewhere}" if elsewhere else str(refusal), err=True)
        raise typer.Exit(2) from refusal
    typer.echo(f"{settled.id}: cancelled")


def proposal_waiting(wait: str, agent: str | None) -> str:
    """How the conversation that proposed hears the answer, as a parked call's refusal says it.

    A session's own conversation holds no waiter: the operator's answer
    wakes it. A subagent is woken by nothing but its own work, so it holds
    one. Where nothing can tell which asked, both are said.
    """
    session = (
        "Carry on with other work, or end your turn: the operator's answer wakes "
        f"this session, and `{wait}` then writes every file at once. Don't start "
        "a waiter."
    )
    subagent = (
        f"Carry on with other work, and hold `{wait}`: nothing else wakes a "
        "subagent. On Claude Code, in the background with the longest timeout "
        "the tool takes (run_in_background, 7200000 ms) and `--timeout 7140`; "
        "on Codex, in your shell tool, read before you report. If it ends with "
        "the review still waiting, start it again quietly, reporting that to "
        "nobody."
    )
    match agent:
        case None:
            return f"From a session's own conversation: {session} From a subagent: {subagent}"
        case "":
            return session
        case _:
            return subagent


def propose(
    root: Path,
    hooks: HookSet,
    directory: Path,
    why: str,
    checkout: Path | None = None,
) -> None:
    """Park one review for every file under *directory*, in this checkout's queue.

    The files land in *checkout*, else in the checkout holding *directory*
    -- a proposal is written under the tmp/ of the checkout it changes --
    and in this one where the directory lies in none. The review is kept
    here, in the session's own checkout, read with its code and labelled
    with the checkout its files land in; run from another checkout than the
    session's, it is refused with the command that parks it there. Each
    file meets the gates a direct write of it would; a file they refuse
    refuses the proposal, naming it, and nothing is parked.
    """
    written = directory.resolve()
    target = (
        checkout_home(checkout.resolve())
        if checkout is not None
        else Path(worktree_root(str(written)) or root)
    )
    named = ["--checkout", str(target)] if checkout is not None else []
    elsewhere = kept_elsewhere(root, ["propose", str(written), "--why", why, *named])
    if elsewhere:
        typer.echo(f"nothing was parked: {elsewhere}", err=True)
        raise typer.Exit(2)
    store = relay(root)
    agent = asking_agent(root, session_member_id())
    try:
        question = parked(
            root, target, hooks, gathered(target, written, why), store, agent or ""
        )
    except ProposalRefused as refusal:
        typer.echo(str(refusal), err=True)
        raise typer.Exit(2) from refusal
    proposal = proposal_of(question)
    count = len(proposal.files) if proposal is not None else 0
    typer.echo(
        f"Queued for the operator as review {question.id}: {count} "
        f"{'file' if count == 1 else 'files'} in {target}, approved or declined "
        "as one. "
        + proposal_waiting(resume_command(root, [question.id]), agent)
        + " Approved, it writes every file where each still stands as recorded "
        "and reports the operator's note and line comments. Declined, revise "
        f"the files under {written} and propose again."
    )
    unnoted = proposal.unnoted() if proposal is not None else []
    if unnoted:
        reply = review_command(root, ["reply", question.id])
        typer.echo(
            "Warning: the operator sees no note from you on "
            + ", ".join(str(path.relative_to(target)) for path in unnoted)
            + f'. Say what changes in each with `{reply} "…"`, '
            f'or next time give each one under "about" in {MANIFEST}.',
            err=True,
        )


def create_review_app(
    root: Path, hooks: Callable[[], HookSet] | None = None
) -> typer.Typer:
    """Wire the `review` group: the reviewer's verbs over one checkout's relay.

    The relay is the checkout's own, at its top, however deep in it *root*
    names: a command run from a subdirectory reads and writes the queue the
    hooks and the dashboard do, never one of its own.

    ``hooks`` is the declared hook set, read only by `review propose`, which
    judges each proposed file as a direct write of it is judged; a
    composition declaring none has no gates to judge a proposal by, and
    refuses one.
    """
    root = checkout_home(root.resolve())
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

    @app.command("propose")
    def propose_cmd(
        directory: Path = typer.Argument(
            help="A directory under the tmp/ of the checkout the files land in, "
            "holding the new version of each file at its path there; its "
            ".proposal.json notes files and names deletions"
        ),
        why: str = typer.Option(
            ...,
            "--why",
            help="A short paragraph for the operator: what the whole change "
            "does, in plain words, then why (see above)",
        ),
        checkout: Path | None = typer.Option(
            None,
            "--checkout",
            help="The checkout the files land in, where it is not the one "
            "holding the directory",
        ),
    ) -> None:
        """Park one review for a batch of edits written under scratch, approved as one.

        Run it with your session's own checkout's code, into its queue, the
        way a parked call's refusal spells every review command:

          uv run --directory <session checkout> lup-devtools review propose \\
            <checkout>/tmp/<name> --why "…"

        The files land in the checkout holding the directory, or --checkout.

        The operator reads your --why and notes before deciding, so write them
        the way you would tell a colleague at their desk. Lead with what
        changes, in ordinary words, then why. Name the file, function or
        command. Say what behaves differently, or say "no behaviour change".

        --why is a short paragraph on the whole change. With more than one
        file, give each one or two short sentences in .proposal.json, by its
        path in the checkout, and list any files the change deletes:

          {"about": {"src/app/host.py": "…"}, "delete": ["docs/old.md"]}

        A plain note:
          "host.py: patched_documents now reads all the files first and only
          then writes them, replacing a 3-way if/elif. Same behaviour."

        Not this:
          "the patch copy reads every file before writing one, and the fold's
          directory list says why it is state"

        Avoid terse house style, sentences where the code does the talking
        ("X says why", "Y is recorded as Z reads it"), and abstract words
        standing in for the thing you mean.
        """
        if hooks is None:
            typer.echo(
                "this composition declares no hook set, so nothing can judge a "
                "proposal's files",
                err=True,
            )
            raise typer.Exit(2)
        propose(root, hooks(), directory, why, checkout)

    @app.command("reply")
    def reply_cmd(
        review: str = typer.Argument(help="The review id"),
        text: str = typer.Argument(
            help="What to tell the operator, in plain words, as you would at "
            "their desk: name the file, function or command, and say what you "
            "changed or will change and why"
        ),
    ) -> None:
        """Answer the operator's note on a review this session asked, in its thread.

        Run it with your session's own checkout's code, into its queue:
        `uv run --directory <session checkout> lup-devtools review reply <id> "…"`.
        """
        reply(root, review, text)

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
        brings the operator's note. A session's own conversation runs it once
        the operator's answer wakes it, and it carries the call out at once; a
        subagent, which nothing else wakes, holds it in the background and
        carries on, starting it again quietly whenever it ends still waiting.
        It waits as long as the operator takes, saying now and then that it
        still is. Run it with your session's own checkout's code, into its
        queue: `uv run --directory <session checkout> lup-devtools review wait <id>`.
        """
        raise typer.Exit(wait_on(root, reviews or [], first, timeout))

    return app
