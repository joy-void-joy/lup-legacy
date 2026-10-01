"""The supervisor web application: one door onto every persisted run.

The page answers a run by writing offers into that run's mailbox, so it
needs no channel to a resolver process and no run needs to have been started
with a page in mind. A run that is moving, one parked overnight, and one
that finished last week are all reachable through exactly the same routes.

Nothing here takes the resolver's state lock. A run holds that lock for its
entire life, so a door that wanted it could only ever serve dead runs.
"""

import asyncio
from pathlib import Path
from typing import Annotated

import typer
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from lup.providers.harness import AdapterName
from lup.channels.models import utc_now
from lup.resolver.record import Journal, JournalEntry
from lup.coordination.mailbox import (
    AnswerDoor,
    AnswerOffer,
    MailboxConflictError,
    ParkRequest,
)
from lup.coordination.questions import QuestionAnswer
from lup.resolver.mailbox import QuestionMailbox, run_cohort
from lup.resolver.state import ResolverStateRepository, StateCorruptionError
from lup.types import StringMap
from lup.web.serve import bundle_app, serve_local_page
from lup.workspace.paths import project_root
from lup.devtools.supervisor.events import FRESH_CATCHUP_ENTRIES, stream
from lup.devtools.supervisor.page import SUPERVISOR_PORT
from lup.devtools.utils import refuse
from lup.devtools.supervisor.projection import (
    ActorIndex,
    AnswerSubmission,
    MessageSubmission,
    NoticeSubmission,
    ParkSubmission,
    RunIndex,
    RunSummary,
    SupervisorState,
    answer_problems,
    supervisor_state,
    unanswered_questions,
)

DEFAULT_SSE_HEADERS: StringMap = {
    "Cache-Control": "no-cache",
    "X-Accel-Buffering": "no",
}
"""What the event stream asks of whatever sits between it and the reader.

``Cache-Control`` is the stream's own requirement — a cached event stream is
not one. ``X-Accel-Buffering`` is nginx's documented opt-out from response
buffering and means nothing to any other proxy, which is why this is a
default rather than a constant: a deployment behind Caddy or Traefik replaces
it, and the loopback bind this page ships with has no proxy to tell at all.
"""


def run_mailbox(state_root: Path, run_id: str) -> QuestionMailbox:
    """The mailbox for one run, whether or not that run ever existed."""
    return QuestionMailbox(state_root / run_id)


def run_journal(state_root: Path, run_id: str) -> Journal:
    """The record for one run, whether or not that run ever existed."""
    return Journal(state_root / run_id)


def last_seq(header: str) -> int:
    """Where a reconnecting reader got to, or the beginning if it says nothing.

    A malformed id replays everything rather than being rejected. The reader
    is a browser reconnecting on its own, so the recoverable reading of a
    value it cannot explain is that it has seen nothing.
    """
    try:
        return int(header)
    except ValueError:
        return -1


def read_run(state_root: Path, run_id: str, adapter: AdapterName) -> SupervisorState:
    """Project one persisted run, reporting absence and corruption distinctly."""
    repository = ResolverStateRepository(state_root, run_id)
    if not repository.exists():
        raise HTTPException(status_code=404, detail=f"no persisted run {run_id!r}")
    try:
        state = repository.load()
    except StateCorruptionError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return supervisor_state(state, run_mailbox(state_root, run_id), adapter)


def run_summary(state_root: Path, run_id: str, adapter: AdapterName) -> RunSummary:
    """One index row, reporting an unreadable run rather than hiding it.

    The catch is deliberately total, which is what a boundary this shape
    needs. Every other row depends on this one not raising, so a state file
    in a shape nobody anticipated has to degrade to a row that says so — a
    rail that 500s whole because one run is malformed takes the page away
    exactly when it is wanted.
    """
    try:
        projected = read_run(state_root, run_id, adapter)
    except Exception as error:
        return RunSummary(
            run_id=run_id,
            phase=None,
            concerns=0,
            pending_questions=0,
            unreadable=True,
            detail=str(error),
        )
    return RunSummary(
        run_id=run_id,
        phase=projected.phase,
        concerns=len(projected.concerns),
        pending_questions=len(unanswered_questions(projected.pending)),
        live=projected.live,
        last_activity=projected.last_activity,
    )


def run_index(state_root: Path, adapter: AdapterName) -> RunIndex:
    """Every run directory holding persisted state, most recently active first.

    A run id is a commit digest, so directory order is hash order — which is
    arbitrary with respect to when anything happened. The rail is what an
    operator scans to find the run they were just working on, so it is
    ordered by when each run last wrote. An unreadable run has no stamp and
    sorts last, which is also where it belongs.
    """
    if not state_root.is_dir():
        return RunIndex(runs=[])
    rows = [
        run_summary(state_root, entry.name, adapter)
        for entry in sorted(state_root.iterdir())
        if (entry / "state.json").is_file()
    ]
    return RunIndex(runs=sorted(rows, key=lambda row: -row.last_activity))


def offer_answers(
    state_root: Path, run_id: str, answers: list[QuestionAnswer], door: AnswerDoor
) -> None:
    """Write one offer per answer, correctable until a promoter takes it."""
    mailbox = run_mailbox(state_root, run_id)
    for answer in answers:
        mailbox.offer(
            AnswerOffer(
                run_id=run_id,
                question_id=answer.question_id,
                value=answer.value,
                door=door,
                offered_at=utc_now(),
            )
        )


def create_supervisor(
    state_root: Path,
    url: str,
    run_id: str | None = None,
    adapter: AdapterName = AdapterName.CLAUDE,
    sse_headers: StringMap = DEFAULT_SSE_HEADERS,
) -> FastAPI:
    """Build the supervisor app over every run under the state root.

    The page is the ``supervisor`` surface Vite built into ``lup.web``'s
    package data, typed against the projections below and the journal's own
    entries, so a route renamed here fails its build rather than its reader.
    """
    supervisor = bundle_app("Lup resolver supervisor", url, "supervisor")

    def selected_run() -> str:
        if run_id is None:
            raise HTTPException(
                status_code=409,
                detail="no run selected; pass --run-id or use /api/runs",
            )
        return run_id

    @supervisor.get("/api/state")
    async def read_state() -> SupervisorState:
        return read_run(state_root, selected_run(), adapter)

    @supervisor.get("/api/runs")
    async def list_runs() -> RunIndex:
        return run_index(state_root, adapter)

    @supervisor.get("/api/runs/{selected}")
    async def read_selected(selected: str) -> SupervisorState:
        return read_run(state_root, selected, adapter)

    @supervisor.get("/api/runs/{selected}/events")
    async def read_events(selected: str, request: Request) -> StreamingResponse:
        """Follow one run's record, resuming from whatever the reader last saw."""
        resume = (
            request.headers["last-event-id"]
            if "last-event-id" in request.headers
            else ""
        )
        return StreamingResponse(
            stream(run_journal(state_root, selected), last_seq(resume)),
            media_type="text/event-stream",
            headers=sse_headers,
        )

    @supervisor.get("/api/runs/{selected}/actors")
    async def read_actors(selected: str) -> ActorIndex:
        """Every actor that has produced an entry, in first-seen order."""
        return ActorIndex(actors=run_journal(state_root, selected).actors())

    @supervisor.get("/api/runs/{selected}/journal/{seq}")
    async def read_entry(selected: str, seq: int) -> JournalEntry:
        """One entry whole, for a reader expanding a truncated block."""
        found = run_journal(state_root, selected).entry(seq)
        if found is None:
            raise HTTPException(status_code=404, detail=f"no journal entry {seq}")
        return found

    @supervisor.get("/api/runs/{selected}/journal")
    async def read_earlier(
        selected: str,
        before: int,
        count: Annotated[int, Query(ge=1, le=1000)] = FRESH_CATCHUP_ENTRIES,
    ) -> list[JournalEntry]:
        """One page of record older than ``before``, oldest first.

        The stream hands a fresh reader a bounded tail, so this is how the
        page walks further back — pulled one page at a time, instead of the
        whole-run replay the bound exists to prevent.
        """
        return run_journal(state_root, selected).before(before, count)

    @supervisor.post("/api/runs/{selected}/answers")
    async def submit_answers(
        selected: str, submission: AnswerSubmission
    ) -> SupervisorState:
        projected = read_run(state_root, selected, adapter)
        problems = answer_problems(
            [view.question for view in projected.pending], submission.answers
        )
        if problems:
            raise HTTPException(status_code=400, detail="; ".join(problems))
        try:
            offer_answers(state_root, selected, submission.answers, AnswerDoor.PAGE)
        except MailboxConflictError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return read_run(state_root, selected, adapter)

    @supervisor.post("/api/runs/{selected}/messages")
    async def send_to_actor(
        selected: str, submission: MessageSubmission
    ) -> SupervisorState:
        """Say something to one actor, or stop all of them, without deciding.

        A message settles nothing, so this can never park a run — which is
        the whole reason messages ride a member's mailbox and decisions ride slots. A
        redirect settles nothing either: it refuses one tool call and states
        why, which retargets the actor without ending the turn it is in.

        Through the run's own cohort, because an address is a member and only
        a population can say which. A redirect naming nobody stops whoever is
        working, and a message naming nobody is refused: mail is addressed and
        consumed, so there is no honest reading of it for a member that does
        not exist yet.
        """
        cohort = run_cohort(run_mailbox(state_root, selected), selected)
        match (submission.to_actor, submission.redirect):
            case ("", False):
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "a message needs an actor to address; to state "
                        "something for the whole run, post a notice"
                    ),
                )
            case ("", True):
                cohort.redirect_all(submission.text, door=AnswerDoor.PAGE)
            case (address, redirect) if not cohort.post(
                address,
                submission.text,
                redirect=redirect,
                door=AnswerDoor.PAGE,
                in_reply_to=submission.in_reply_to,
            ):
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"{address!r} names no actor this run has recorded, "
                        "so nothing was told anything"
                    ),
                )
        return read_run(state_root, selected, adapter)

    @supervisor.post("/api/runs/{selected}/notices")
    async def post_notice(
        selected: str, submission: NoticeSubmission
    ) -> SupervisorState:
        """State something true for every actor, now and for whoever starts next.

        A notice is state rather than mail: nothing consumes it, so it is
        read at the head of every turn for as long as it stands, including
        the first turn of an actor spawned an hour from now. Whoever is
        working is also sent it as a message, because a fact worth stating is
        worth hearing before the turn they are in ends.
        """
        run_cohort(run_mailbox(state_root, selected), selected).notify(
            submission.text, door=AnswerDoor.PAGE
        )
        return read_run(state_root, selected, adapter)

    @supervisor.delete("/api/runs/{selected}/notices/{notice_id}")
    async def retract_notice(selected: str, notice_id: str) -> SupervisorState:
        """Take one standing fact down, so no later turn reads it."""
        if not run_cohort(run_mailbox(state_root, selected), selected).mail.retract(
            notice_id
        ):
            raise HTTPException(
                status_code=404,
                detail=f"{notice_id!r} names nothing standing over {selected}",
            )
        return read_run(state_root, selected, adapter)

    @supervisor.post("/api/runs/{selected}/park")
    async def park_run(selected: str, submission: ParkSubmission) -> SupervisorState:
        projected = read_run(state_root, selected, adapter)
        run_mailbox(state_root, selected).park(
            ParkRequest(run_id=projected.run_id, reason=submission.reason)
        )
        return read_run(state_root, selected, adapter)

    @supervisor.post("/api/runs/{selected}/resume")
    async def resume_run(selected: str) -> SupervisorState:
        """Start a resolver over a run the page has just supplied answers for.

        Offers only become answers when a promoter takes them, and the
        promoter lives inside a run. A parked run therefore needs something
        to start it again; doing that here means the page that collected
        the decisions is also the thing that spends them.
        """
        projected = read_run(state_root, selected, adapter)
        if projected.live:
            raise HTTPException(status_code=409, detail="this run is already moving")
        await asyncio.create_subprocess_exec(
            "uv",
            "run",
            "lup-devtools",
            "harness",
            "resolve",
            "--adapter",
            adapter,
            "--run-id",
            selected,
        )
        return projected

    return supervisor


def serve_supervisor(
    run_id: Annotated[
        str | None, typer.Option("--run-id", help="Run to open on load")
    ] = None,
    adapter: Annotated[
        AdapterName, typer.Option("--adapter", help="Adapter named in the rerun recipe")
    ] = AdapterName.CLAUDE,
    host: Annotated[str, typer.Option(help="Interface to bind")] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="TCP port to bind")] = SUPERVISOR_PORT,
    open_page: Annotated[
        bool, typer.Option("--open/--no-open", help="Open the page in a browser")
    ] = True,
) -> None:
    """Answer any run under ``.lup/resolve``, live or parked."""
    state_root = project_root() / ".lup" / "resolve"
    try:
        serve_local_page(
            lambda url: create_supervisor(state_root, url, run_id, adapter),
            "Resolver supervisor",
            host,
            port,
            open_page,
        )
    except ValueError as error:
        refuse(str(error), code=2)
