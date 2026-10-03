"""Wait on parked reviews, and carry out each one the operator approves.

A parked call is refused while it waits, so nothing retries it: the
conversation that asked runs `review wait` in its own shell -- a session's
own conversation once the operator's answer wakes it, a subagent in the
background from the start, since nothing else wakes a subagent -- and the
waiter reports each review as it settles. An approved one it carries out there, inside the session's own
sandbox: an edit as the after-document the operator saw, only where the file
still stands as the review recorded it; a command in the directory recorded
with it, in a fresh shell, so nothing the session did to its own shell since
reaches it -- the policy judged the command standing alone. Whatever the
verdict, the operator's note and line comments are printed beneath it.

It is the requester's one channel for everything the operator says on the
review. A remark -- a note and line comments sent without deciding -- ends it
too, saying the review is still pending and how to wait on it again, so the
agent can answer with `review reply` before carrying on. A review whose
recorded files moved is retired as stale here, as it is on the dashboard,
and reported as the one thing left to do: re-read the file and ask again.
And a waiter stopped with a review still pending -- its timeout, or the
runtime ending it -- says so last, with the command that waits again.

What it carries out is bounded three ways. The review must be one this
session asked -- its runtime's id for the session, or the roster member its
launch named -- so no session spends another's approval where it runs. The
answer must be the host's, which no session writes, given against the
fingerprint the review's own record still hashes to, so a record altered
since the operator read it carries nothing. And the approval is spent once,
by the same claim the hook takes for a retried call, so a call carried out
here is not also run by a retry, and the other way round.

A call a shell cannot carry out -- one placed outside the session's sandbox,
or a tool that is not a file write or a command -- is left to one exact retry
of the call, which the hook allows once, and the waiter says so.
"""

import fcntl
import json
import shlex
import signal
import sys
import time
from collections.abc import Iterator, Sequence
from contextlib import ExitStack, contextmanager
from datetime import datetime
from itertools import islice
from pathlib import Path
from types import FrameType
from typing import Literal

import sh
import typer
from pydantic import BaseModel, ValidationError

from lup.coordination.identity import session_member_id
from lup.workspace.checkout_state import CheckoutState
from lup.coordination.repository import RepositoryPeers
from lup.coordination.wake import WakePath, wake
from lup.devtools.review.preimages import PreimageWatch, moved
from lup.execution.locks import try_exclusive
from lup.devtools.review.propose import previewed
from lup.devtools.review.thread import ReviewThread
from lup.policy.assets.host import review_home
from lup.policy.kernel.review import literal_input
from lup.policy.relay import (
    LineComment,
    PersistentQuestion,
    QuestionRecord,
    QuestionRelay,
    RecordedQuestion,
    RecordedRemark,
    RelaySignature,
    Remark,
)
from lup.policy.review import ReviewedFile
from lup.providers.identity import native_session_ids

type Verdict = Literal[
    "ran",
    "applied",
    "declined",
    "commented",
    "stale",
    "expired",
    "cancelled",
    "conflict",
    "retry",
    "spent",
]
"""How one waited review settled, in the word its line starts with.

``commented`` is the one that did not settle it: the operator sent a note or
line comments without deciding, and the review still waits.
"""


class WaitedReview(BaseModel, frozen=True):
    """How one review settled, as the waiter reports it to the session.

    ``note`` and ``comments`` are what the operator wrote with it, printed
    beneath the verdict whatever the verdict is -- an approval's words are
    as much the operator's as a decline's.
    """

    review: str
    verdict: Verdict
    detail: str
    carried: bool
    """Whether what the operator approved was done, here or by a retry."""

    note: str = ""
    comments: list[LineComment] = []
    root: Path | None = None
    """The checkout the comments' paths are shown relative to."""

    at: datetime | None = None
    """When the operator said what this reports, which the notifier matches it by."""

    def line(self) -> str:
        return "".join(
            [
                f"review {self.review} — {self.verdict}: {self.detail}",
                *([f"\n  operator note: {self.note}"] if self.note else []),
                *(f"\n  {comment.spelled(self.root)}" for comment in self.comments),
            ]
        )


def review_command(root: Path, words: Sequence[str]) -> str:
    """One `review` command over *root*'s queue, runnable from anywhere with *root*'s code.

    Every review command a session or the operator is handed is spelled this
    way: the checkout keeping the queue holds the code that parked what is in
    it, where a command run from another checkout reads and writes the queue
    with that checkout's code, or another checkout's queue.
    """
    return shlex.join(
        ["uv", "run", "--directory", str(root), "lup-devtools", "review", *words]
    )


def resume_command(root: Path, reviews: Sequence[str]) -> str:
    """The `review wait` that waits on *reviews* again, runnable from anywhere."""
    return review_command(root, ["wait", *reviews])


def kept_elsewhere(root: Path, words: Sequence[str]) -> str:
    """Where this session keeps its reviews, and the command reaching them, where that is not *root*.

    A session keeps its reviews in the checkout its launch opened, read with
    that checkout's code. The same command run from another checkout reads
    and writes that one's queue with that one's code -- how a review came to
    be parked where the operator's dashboard could not answer it.
    """
    home = review_home(root)
    if home.resolve() == root.resolve():
        return ""
    return (
        f"this session's reviews are kept in {home} and read with its code: run "
        f"`{review_command(home, words)}`"
    )


class ReviewReported(BaseModel, frozen=True):
    """One thing the operator said that a waiter has put to its session, by when it was said."""

    reported: datetime


class ReviewWaiters(BaseModel, frozen=True):
    """Which reviews a `review wait` is waiting on now, each held by a lock.

    Read by whoever tells the requester an answer landed: where a waiter
    holds the review, its own completion is what wakes the session, and a
    second wake would put the same answer to it twice. A waiter writes into
    the same file what it reported, so a notifier that looks a moment after
    the waiter finished still finds the news delivered.
    """

    root: Path

    def path(self, review: str) -> Path:
        return CheckoutState(root=self.root).review_waiters() / review

    def report(self, review: str, at: datetime) -> None:
        """Record that the operator's words given *at* reached the session through this waiter."""
        path = self.path(review)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(ReviewReported(reported=at).model_dump_json() + "\n")

    def carries(self, review: str, at: datetime | None) -> bool:
        """Whether a waiter holds this review, or already put the news given *at* to its session."""
        if self.held(review):
            return True
        path = self.path(review)
        if at is None or not path.is_file():
            return False

        def reported() -> Iterator[datetime]:
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    yield ReviewReported.model_validate_json(line).reported
                except ValidationError:
                    continue

        return any(each == at for each in reported())

    @contextmanager
    def holding(self, reviews: list[str]) -> Iterator[None]:
        """Hold each review for as long as this waiter waits on it."""
        with ExitStack() as held:
            for review in reviews:
                path = self.path(review)
                path.parent.mkdir(parents=True, exist_ok=True)
                handle = held.enter_context(path.open("a", encoding="utf-8"))
                fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
            yield

    def held(self, review: str) -> bool:
        """Whether any waiter is waiting on this review now."""
        path = self.path(review)
        if not path.is_file():
            return False
        with try_exclusive(path) as free:
            return not free


class Asker(BaseModel, frozen=True):
    """The session this waiter speaks for, by every id a review records it under.

    Its runtime's own id for it -- Claude Code hands it to every process the
    session starts, subagents' included -- and the roster member its launch
    named, whose row also carries the id Codex gave the thread and how the
    session is woken.
    """

    sessions: list[str] = []
    member: str = ""
    wake: WakePath = WakePath()
    worktree: str = ""

    @classmethod
    def here(cls, root: Path) -> "Asker":
        member = session_member_id()
        rows = [
            row
            for row in (RepositoryPeers(root).present() if member else [])
            if row.actor.id == member
        ]
        row = rows[0] if rows else None
        return cls(
            sessions=[
                identity
                for identity in (
                    *native_session_ids(),
                    row.wake.session if row is not None else "",
                )
                if identity
            ],
            member=member,
            wake=row.wake if row is not None else WakePath(),
            worktree=row.worktree if row is not None else "",
        )

    def asked(self, question: QuestionRecord) -> bool:
        """Whether this session parked the question, or a subagent of its did."""
        return question.operation.session in self.sessions or bool(
            self.member and question.member == self.member
        )


def edits(question: PersistentQuestion) -> list[ReviewedFile] | None:
    """The documents an approved edit writes, or ``None`` where it is not an edit.

    A write, an edit, a patch envelope -- Codex's own tool, or the same
    envelope handed to its shell, which no shell here has a program for --
    or a proposal of several files, written all at once or not at all.
    """
    payload = question.operation.payload
    command = payload["command"] if "command" in payload else None
    match question.operation.tool:
        case "Write" | "Edit" | "apply_patch" | "Propose":
            return previewed(question).files
        case "Bash" if isinstance(command, str) and literal_input(
            command, "apply_patch"
        ):
            return previewed(question).files
        case _:
            return None


def written(files: list[ReviewedFile], review: str) -> str:
    """Write every after-document, or none: removing a file whose after is absence.

    Each document is written beside its file first, named for the *review*,
    and only once every one is on disk are they moved into place, so a write
    that fails part way -- a full disk, a directory that cannot be made --
    leaves every file as it stood rather than half the change applied.
    """
    documents = [change for change in files if change.after is not None]
    staged = [
        change.path.parent / f".{change.path.name}.{review}.review-wait"
        for change in documents
    ]
    try:
        for change, temporary in zip(documents, staged, strict=True):
            change.path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(change.after or "", encoding="utf-8", newline="")
    except OSError:
        for temporary in staged:
            temporary.unlink(missing_ok=True)
        raise
    for change, temporary in zip(documents, staged, strict=True):
        temporary.replace(change.path)
    for change in files:
        if change.after is None:
            change.path.unlink(missing_ok=True)
    return ", ".join(str(change.path) for change in files)


def ran(question: QuestionRecord, command: str) -> int:
    """Run one approved command where it was asked, its output this waiter's own."""
    typer.echo(f"$ {command}")
    finished = sh.Command("bash")(
        "-c",
        command,
        _cwd=str(question.operation.cwd),
        _out=sys.stdout,
        _err_to_out=True,
        _ok_code=list(range(256)),
        _return_cmd=True,
    )
    assert isinstance(finished, sh.RunningCommand)
    return finished.exit_code


def claimed(root: Path, question: QuestionRecord) -> bool:
    """Spend the approval, or learn a retried call or another waiter already did.

    The claim the hook takes for a retry, under the same name, so exactly
    one of them ever carries the approved call out.
    """
    claim = CheckoutState(root=root).review_claims() / question.id
    claim.parent.mkdir(parents=True, exist_ok=True)
    try:
        with claim.open("x", encoding="utf-8") as handle:
            json.dump(
                {
                    "fingerprint": question.fingerprint,
                    "execution_id": "review-wait",
                    "stage": "review-wait",
                },
                handle,
                sort_keys=True,
            )
    except FileExistsError:
        return False
    return True


def carried_out(
    root: Path, store: QuestionRelay, question: RecordedQuestion
) -> WaitedReview:
    """Carry out one approved review here, or say why it is not.

    Checked in the order that spends nothing on a refusal: the record still
    shows what the operator approved, read back whole from the relay's
    store, the call is one a shell here can carry out, and every file it
    recorded stands as it did; only then is the approval spent and the call
    made.
    """
    tool = question.operation.tool

    def settled(verdict: Verdict, detail: str, carried: bool) -> WaitedReview:
        return WaitedReview(
            review=question.id, verdict=verdict, detail=detail, carried=carried
        )

    if question.unverifiable():
        return settled(
            "conflict",
            "this review wait runs older code than the hook that parked the "
            "review, so it cannot check the approval covers it; nothing ran",
            False,
        )
    try:
        shown = store.resolve(question)
    except ValueError as unread:
        return settled(
            "conflict",
            f"the review cannot be read back whole ({unread}), so its approval "
            "covers nothing it can check; nothing ran",
            False,
        )
    if not shown.bound():
        return settled(
            "conflict",
            "the review changed after it was parked, so its approval covers "
            "something else; nothing ran",
            False,
        )
    files = edits(shown)
    payload = shown.execution_payload or shown.operation.payload
    command = payload["command"] if "command" in payload else None
    if files is None and (tool != "Bash" or not isinstance(command, str)):
        return settled(
            "retry",
            f"approved; retry the exact {tool} call, which the approval releases once",
            True,
        )
    if files is None and question.operation.placement == "outside":
        return settled(
            "retry",
            "approved to run outside this session's sandbox, which a command "
            "started here cannot leave; retry the exact call, which the "
            "approval releases once, placed outside",
            True,
        )
    if changed := moved(question, sources=True):
        store.retire_stale(
            question.id, [each.path for each in changed], "its requester's review wait"
        )
        return settled(
            "stale",
            "; ".join(each.sentence() for each in changed)
            + "; nothing ran — re-read it and ask again",
            False,
        )
    if not claimed(root, question):
        return settled(
            "spent", "already carried out, by a retried call or another wait", True
        )
    store.advance(question.id, "dispatched", "carried out by review wait")
    if files is not None:
        paths = written(files, question.id)
        store.advance(question.id, "completed", f"applied by review wait: {paths}")
        return settled("applied", paths, True)
    assert isinstance(command, str)
    status = ran(question, command)
    store.advance(
        question.id,
        "completed" if status == 0 else "failed",
        f"ran by review wait, exit {status}",
    )
    return settled("ran", f"exit {status} in {question.operation.cwd}", status == 0)


def settled_now(
    root: Path, store: QuestionRelay, question: RecordedQuestion
) -> WaitedReview | None:
    """What one review came to, or ``None`` while it still waits.

    Whatever it came to, the operator's note and line comments ride beneath
    it: an approval with instructions is as much the operator's word as a
    decline, and printing the note for a decline alone would lose it.
    """
    answer = question.answer
    words = (
        {
            "note": answer.note,
            "comments": answer.comments,
            "root": question.operation.worktree,
            "at": answer.at,
        }
        if answer is not None
        else {}
    )

    def plain(verdict: Verdict, detail: str, carried: bool) -> WaitedReview:
        return WaitedReview(
            review=question.id, verdict=verdict, detail=detail, carried=carried
        )

    match question.state:
        case "pending" | "preparing":
            return None
        case "approved":
            return carried_out(root, store, question).model_copy(update=words)
        case "rejected":
            detail = (
                "change course, or ask the user"
                if answer is not None and (answer.note or answer.comments)
                else "no note; change course, or ask the user"
            )
            return plain("declined", detail, False).model_copy(update=words)
        case "stale":
            named = ", ".join(str(path) for path in question.moved)
            return plain(
                "stale",
                f"{named} changed since this was recorded; re-read it and ask again",
                False,
            )
        case "expired" | "cancelled":
            return plain(
                question.state, question.outcome or "nobody answered it", False
            )
        case _:
            return plain(
                "spent",
                "already carried out, by a retried call or another wait"
                + (f": {question.outcome}" if question.outcome else ""),
                True,
            )


class WaitRefused(ValueError):
    """A review this waiter may not wait on, said before anything waits."""


def chosen(
    store: QuestionRelay, asker: Asker, reviews: list[str]
) -> list[RecordedQuestion]:
    """The reviews to wait on: those named, else every one this session has waiting.

    Waiting is unanswered, or approved and not yet carried out. A named
    review must exist and be this session's: waiting on another's would carry
    out an approval given to a session that may run elsewhere, under another
    boundary.
    """
    questions = {question.id: question for question in store.questions()}
    if not reviews:
        return [
            question
            for question in questions.values()
            if question.state in ("pending", "approved") and asker.asked(question)
        ]
    missing = [review for review in reviews if review not in questions]
    if missing:
        raise WaitRefused(f"no review {', '.join(missing)} is recorded here")
    foreign = [review for review in reviews if not asker.asked(questions[review])]
    if foreign:
        raise WaitRefused(
            f"review {', '.join(foreign)} was asked by another session; only "
            "the session that asked may carry it out"
        )
    return [questions[review] for review in dict.fromkeys(reviews)]


def settling(
    root: Path,
    store: QuestionRelay,
    waiting: list[RecordedQuestion],
    heard: list[RecordedRemark],
    stop: "StopRequest",
    poll: float = 1.0,
    timeout: float | None = None,
    announce: float = 600.0,
) -> Iterator[WaitedReview]:
    """Each review as it settles, reported the moment it does, until all have.

    The queue is read again only when the relay or the host's answers to it
    change on disk, so a waiter left running for hours costs a few file
    checks a poll, and each review's state and the remarks on it come from
    one read of both: a remark appended with the answer it came with is
    read with that answer, never beside the review still waiting. It has no
    limit of its own: only a ``timeout`` it was
    handed ends it with reviews still waiting. Every ``announce`` seconds it
    says which it still waits on, so its output shows it alive to whoever
    reads it.

    Two more things end a wait. A remark the operator made since
    ``heard`` -- a note and line comments sent without deciding -- is
    reported as ``commented`` and ends the wait with the review still
    pending, so the session reads it now rather than when the review
    settles. A waiting review whose recorded files moved is retired as stale
    here, where the files are read as the session reads them, and reported.
    What each report carries is marked as reported, so the dashboard mails
    nothing beside it.
    """
    waiters = ReviewWaiters(root=root)
    watch = PreimageWatch(sources=True)
    remaining = [question.id for question in waiting]
    latest = {question.id: question for question in waiting}
    seen: RelaySignature | None = None
    started = time.monotonic()
    announced = started

    def told(report: WaitedReview) -> WaitedReview:
        typer.echo(report.line())
        if report.at is not None:
            waiters.report(report.review, report.at)
        return report

    def fresh(review: str, remarks: dict[str, list[RecordedRemark]]) -> list[Remark]:
        """The remarks on one waiting review nobody has put to the session yet."""
        question = latest[review]
        return [
            recorded.remark
            for recorded in (remarks[review] if review in remarks else [])
            if recorded.fingerprint == question.fingerprint
            and not any(
                before.question == review and before.remark.at == recorded.remark.at
                for before in heard
            )
        ]

    while remaining:
        now = time.monotonic()
        if stop.signal or (timeout is not None and now - started >= timeout):
            return
        if now - announced >= announce:
            minutes = round((now - started) / 60)
            typer.echo(f"still waiting on {', '.join(remaining)} ({minutes} min)")
            announced = now
        for review in remaining:
            question = latest[review]
            if question.state == "pending" and (drifted := watch.moved(question)):
                latest[review] = store.retire_stale(
                    review,
                    [each.path for each in drifted],
                    "its requester's review wait",
                )
        signature = store.signature()
        if signature == seen:
            time.sleep(poll)
            continue
        seen = signature
        reading = store.read()
        current = {question.id: question for question in reading.questions}
        latest.update(
            {review: current[review] for review in remaining if review in current}
        )
        remarks = reading.threads.remarks
        commented = [
            WaitedReview(
                review=review,
                verdict="commented",
                detail="the operator commented without deciding; it is still pending",
                carried=False,
                note=remark.note,
                comments=remark.comments,
                root=latest[review].operation.worktree,
                at=remark.at,
            )
            for review in remaining
            if latest[review].state == "pending"
            for remark in fresh(review, remarks)
        ]
        for report in commented:
            yield told(report)
        if commented:
            return
        for review in [review for review in remaining if review in current]:
            report = settled_now(root, store, latest[review])
            if report is None:
                continue
            remaining.remove(review)
            yield told(report)


def woken(asker: Asker, said: list[str], waited: list[RecordedQuestion]) -> None:
    """Hand what a waiter left running said to a Codex session through its queue, where it may be idle.

    A Claude session is woken by its runtime when this background command
    ends; a Codex session's shell tool keeps the command running after the
    turn and starts no turn when it ends -- measured on 0.158.0, where
    `codex queue` does start one in the idle thread -- so a waiter that
    waited queues what it reported, the line saying how to wait again
    included. One run once every answer was in reports in the call that ran
    it, and queuing that too would start a turn for nothing. A subagent's
    waiter queues nothing: the session's thread is not the subagent, which
    reads its waiter itself before it reports.
    """
    if asker.wake.runtime != "codex" or not said:
        return
    if all(question.agent for question in waited):
        return
    if all(question.state != "pending" for question in waited):
        return
    wake(
        asker.wake,
        "`review wait` finished:\n" + "\n".join(said),
        Path(asker.worktree) if asker.worktree else None,
    )


class StopRequest:
    """Whether the waiter was told to stop, and by which signal.

    A runtime ends a background command it has run too long with SIGTERM, and
    a closing terminal sends SIGHUP. Either sets this rather than killing the
    waiter outright, and the wait returns at its next look, so it can say
    what it leaves waiting and how to wait on it again -- where dying at the
    signal left its session never learning the review still waited on it.
    """

    def __init__(self) -> None:
        self.signal = ""

    @contextmanager
    def armed(self) -> Iterator["StopRequest"]:
        """Note SIGTERM and SIGHUP here for as long as the wait runs."""

        def stop(number: int, _frame: FrameType | None) -> None:
            self.signal = signal.Signals(number).name

        handled = [signal.SIGTERM, signal.SIGHUP]
        before = [signal.signal(number, stop) for number in handled]
        try:
            yield self
        finally:
            for number, previous in zip(handled, before, strict=True):
                signal.signal(number, previous)


def wait_on(
    root: Path,
    reviews: list[str],
    first: bool,
    timeout: float | None = None,
    announce: float = 600.0,
    poll: float = 1.0,
) -> int:
    """Wait on reviews this session asked, carry out what was approved, report it all.

    Exits 0 where everything waited on was carried out, 1 where anything was
    declined, went stale, expired, was cancelled or in conflict, 2 where
    nothing could be waited on at all, and 3 where it ended with a review
    still waiting -- the operator commented without deciding, the
    ``timeout`` it was handed passed, or its runtime stopped it -- saying
    last which, and the command that waits on them again.
    """
    store = QuestionRelay(CheckoutState(root=root).questions())
    asker = Asker.here(root)
    try:
        waiting = chosen(store, asker, reviews)
    except WaitRefused as refusal:
        elsewhere = kept_elsewhere(root, ["wait", *reviews])
        typer.echo(f"{refusal}; {elsewhere}" if elsewhere else str(refusal))
        return 2
    if not waiting:
        typer.echo("nothing this session asked is waiting for review")
        return 0
    typer.echo("waiting on " + ", ".join(question.id for question in waiting))
    heard = [
        recorded
        for remarks in ReviewThread.of(store).remarks().values()
        for recorded in remarks
    ]
    with (
        ReviewWaiters(root=root).holding([question.id for question in waiting]),
        StopRequest().armed() as stop,
    ):
        settled = settling(root, store, waiting, heard, stop, poll, timeout, announce)
        reports = list(islice(settled, 1) if first else settled)
    settles = {report.review for report in reports if report.verdict != "commented"}
    left = [question.id for question in waiting if question.id not in settles]
    said = [report.line() for report in reports]
    commented = [report.review for report in reports if report.verdict == "commented"]
    if not left or not (stop.signal or commented or not (first and reports)):
        woken(asker, said, waiting)
        return 0 if all(report.carried for report in reports) else 1
    again = resume_command(root, left)
    match commented, stop.signal:
        case [review, *_], _:
            ending = (
                f"{', '.join(commented)} still pending, with the operator's words "
                "above: answer on the review with "
                f"`{review_command(root, ['reply', review])} <text>`, or cancel "
                f"it and ask again; `{again}` waits on it again"
            )
        case [], str(name) if name:
            ending = (
                f"stopped by {name} with {', '.join(left)} still pending and "
                f"nothing carried out for it — start `{again}` again to keep "
                "waiting, quietly: a waiter ending is news to nobody"
            )
        case _:
            ending = (
                f"still waiting on {', '.join(left)}: its timeout passed and nothing "
                f"was carried out for it — start `{again}` again to keep waiting, "
                "quietly: a waiter ending is news to nobody"
            )
    typer.echo(ending)
    woken(asker, [*said, ending], waiting)
    return 3
