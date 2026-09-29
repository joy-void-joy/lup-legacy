"""Wait on parked reviews, and carry out each one the operator approves.

A parked call is refused while it waits, so nothing retries it: the session
starts `review wait` in its own shell -- in the background, where its runtime
wakes it when a background command ends -- and the waiter reports each review
as it settles. An approved one it carries out there, inside the session's own
sandbox: an edit as the after-document the operator saw, only where the file
still stands as the review recorded it; a command in the directory recorded
with it, in a fresh shell, so nothing the session did to its own shell since
reaches it -- the policy judged the command standing alone. A declined one
reports the operator's note.

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
import sys
import time
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from itertools import islice
from pathlib import Path
from typing import Literal

import sh
import typer
from pydantic import BaseModel

from lup.coordination.identity import session_member_id
from lup.coordination.repository import RepositoryPeers
from lup.coordination.wake import WakePath, wake
from lup.policy.kernel.review import literal_input
from lup.policy.relay import PersistentQuestion, QuestionRelay, RelaySignature
from lup.policy.review import ReviewedFile, reviewed_files
from lup.providers.harness import patch_review
from lup.providers.identity import native_session_ids

type Verdict = Literal[
    "ran", "applied", "declined", "expired", "cancelled", "conflict", "retry", "spent"
]
"""How one waited review settled, in the word its line starts with."""


class WaitedReview(BaseModel, frozen=True):
    """How one review settled, as the waiter reports it to the session."""

    review: str
    verdict: Verdict
    detail: str
    carried: bool
    """Whether what the operator approved was done, here or by a retry."""

    def line(self) -> str:
        return f"review {self.review} — {self.verdict}: {self.detail}"


class ReviewWaiters(BaseModel, frozen=True):
    """Which reviews a `review wait` is waiting on now, each held by a lock.

    Read by whoever tells the requester an answer landed: where a waiter
    holds the review, its own completion is what wakes the session, and a
    second wake would put the same answer to it twice.
    """

    root: Path

    def path(self, review: str) -> Path:
        return self.root / ".lup" / "review-waiters" / review

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
        with path.open("a", encoding="utf-8") as handle:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return False


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

    def asked(self, question: PersistentQuestion) -> bool:
        """Whether this session parked the question, or a subagent of its did."""
        return question.operation.session in self.sessions or bool(
            self.member and question.member == self.member
        )


def standing(path: Path) -> str | None:
    """A file's text as it stands, absent where nothing does."""
    return path.read_text(encoding="utf-8", newline="") if path.is_file() else None


def moved(question: PersistentQuestion) -> list[Path]:
    """Every file the review recorded that no longer stands as it did."""
    return [
        path
        for path, before in question.preconditions.items()
        if standing(path) != before
    ]


def edits(question: PersistentQuestion) -> list[ReviewedFile] | None:
    """The documents an approved edit writes, or ``None`` where it is not an edit.

    A write, an edit, or a patch envelope -- Codex's own tool, or the same
    envelope handed to its shell, which no shell here has a program for.
    """
    payload = question.operation.payload
    command = payload["command"] if "command" in payload else None
    match question.operation.tool:
        case "Write" | "Edit" | "apply_patch":
            return reviewed_files(question, patch_review)
        case "Bash" if isinstance(command, str) and literal_input(
            command, "apply_patch"
        ):
            return reviewed_files(question, patch_review)
        case _:
            return None


def written(files: list[ReviewedFile]) -> str:
    """Write each after-document, removing a file whose after is absence."""
    for change in files:
        if change.after is None:
            change.path.unlink(missing_ok=True)
            continue
        change.path.parent.mkdir(parents=True, exist_ok=True)
        change.path.write_text(change.after, encoding="utf-8", newline="")
    return ", ".join(str(change.path) for change in files)


def ran(question: PersistentQuestion, command: str) -> int:
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


def claimed(root: Path, question: PersistentQuestion) -> bool:
    """Spend the approval, or learn a retried call or another waiter already did.

    The claim the hook takes for a retry, under the same name, so exactly
    one of them ever carries the approved call out.
    """
    claim = root / ".lup/review-claims" / question.id
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
    root: Path, store: QuestionRelay, question: PersistentQuestion
) -> WaitedReview:
    """Carry out one approved review here, or say why it is not.

    Checked in the order that spends nothing on a refusal: the record still
    shows what the operator approved, the call is one a shell here can carry
    out, and every file it recorded stands as it did; only then is the
    approval spent and the call made.
    """
    tool = question.operation.tool

    def settled(verdict: Verdict, detail: str, carried: bool) -> WaitedReview:
        return WaitedReview(
            review=question.id, verdict=verdict, detail=detail, carried=carried
        )

    if not question.bound():
        return settled(
            "conflict",
            "the review changed after it was parked, so its approval covers "
            "something else; nothing ran",
            False,
        )
    files = edits(question)
    payload = question.execution_payload or question.operation.payload
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
    if changed := moved(question):
        detail = ", ".join(str(path) for path in changed)
        store.advance(question.id, "failed", f"conflict: {detail} changed since")
        return settled(
            "conflict",
            f"{detail} changed since the review was recorded; nothing ran — "
            "make the change again against what stands now",
            False,
        )
    if not claimed(root, question):
        return settled(
            "spent", "already carried out, by a retried call or another wait", True
        )
    store.advance(question.id, "dispatched", "carried out by review wait")
    if files is not None:
        paths = written(files)
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
    root: Path, store: QuestionRelay, question: PersistentQuestion
) -> WaitedReview | None:
    """What one review came to, or ``None`` while it still waits."""
    note = question.answer.note if question.answer is not None else ""
    match question.state:
        case "pending" | "preparing":
            return None
        case "approved":
            return carried_out(root, store, question)
        case "rejected":
            return WaitedReview(
                review=question.id,
                verdict="declined",
                detail=note or "no note; change course, or ask the user",
                carried=False,
            )
        case "expired" | "cancelled":
            return WaitedReview(
                review=question.id,
                verdict=question.state,
                detail=question.outcome or "nobody answered it",
                carried=False,
            )
        case _:
            return WaitedReview(
                review=question.id,
                verdict="spent",
                detail=(
                    "already carried out, by a retried call or another wait"
                    + (f": {question.outcome}" if question.outcome else "")
                ),
                carried=True,
            )


class WaitRefused(ValueError):
    """A review this waiter may not wait on, said before anything waits."""


def chosen(
    store: QuestionRelay, asker: Asker, reviews: list[str]
) -> list[PersistentQuestion]:
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
    waiting: list[PersistentQuestion],
    poll: float = 1.0,
) -> Iterator[WaitedReview]:
    """Each review as it settles, reported the moment it does, until all have.

    The queue is read again only when the relay or the host's answers to it
    change on disk, so a waiter left running for hours costs two file checks
    a poll.
    """
    remaining = [question.id for question in waiting]
    seen: RelaySignature | None = None
    while remaining:
        signature = store.signature()
        if signature == seen:
            time.sleep(poll)
            continue
        seen = signature
        current = {question.id: question for question in store.questions()}
        for review in [review for review in remaining if review in current]:
            report = settled_now(root, store, current[review])
            if report is None:
                continue
            typer.echo(report.line())
            remaining.remove(review)
            yield report


def woken(asker: Asker, reports: list[WaitedReview]) -> None:
    """Hand the result to a Codex session through its queue, where it may be idle.

    A Claude session is woken by its runtime when this background command
    ends; a Codex session's shell tool keeps the command running after the
    turn and starts no turn when it ends -- measured on 0.158.0, where
    `codex queue` did start one in the idle thread -- so the waiter queues
    what it reported.
    """
    if asker.wake.runtime != "codex" or not reports:
        return
    wake(
        asker.wake,
        "`review wait` finished:\n" + "\n".join(report.line() for report in reports),
        Path(asker.worktree) if asker.worktree else None,
    )


def wait_on(root: Path, reviews: list[str], first: bool) -> int:
    """Wait on reviews this session asked, carry out what was approved, report it all.

    Exits 0 where everything waited on was carried out, 1 where anything was
    declined, expired, cancelled or in conflict, and 2 where nothing could be
    waited on at all.
    """
    store = QuestionRelay(root / ".lup/questions.jsonl")
    asker = Asker.here(root)
    try:
        waiting = chosen(store, asker, reviews)
    except WaitRefused as refusal:
        typer.echo(str(refusal))
        return 2
    if not waiting:
        typer.echo("nothing this session asked is waiting for review")
        return 0
    typer.echo("waiting on " + ", ".join(question.id for question in waiting))
    with ReviewWaiters(root=root).holding([question.id for question in waiting]):
        reports = list(
            islice(settling(root, store, waiting), 1)
            if first
            else settling(root, store, waiting)
        )
    woken(asker, reports)
    return 0 if all(report.carried for report in reports) else 1
