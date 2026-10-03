"""Console doors onto a resolver run's question mailbox.

Each of these is a handful of lines because the mailbox is just files, and
each is strictly better than a prompt on the run's own stdin ever was: they
work from another shell, or another host, *while the run is still waiting*.

None of them may take :meth:`ResolverStateRepository.exclusive`. A run holds
that lock for its whole life, so a door that reached for it could only ever
serve runs that had already finished — which is exactly the class of runs a
door is useless for.
"""

from pathlib import Path
from time import sleep

import typer

from lup.channels.models import local_stamp, utc_now
from lup.workspace.checkout_state import CheckoutState
from lup.resolver.models import (
    ConcernRetirement,
    ConcernStatus,
    VerificationAcceptance,
)
from lup.resolver.state import ResolverStateRepository, StateTransitionError
from lup.resolver.status import RunStatus, run_status
from lup.coordination.cohort import ActorCohort
from lup.coordination.mailbox import (
    AnswerDoor,
    AnswerOffer,
    MailboxConflictError,
    ParkRequest,
)
from lup.resolver.mailbox import QuestionMailbox, run_cohort
from lup.policy.kernel.diagnostic import devtools, step
from lup.workspace.paths import project_root
from lup.devtools.harness.resolve import parse_answer_flags
from lup.devtools.supervisor.projection import PendingQuestionView
from lup.diagnostics import refuse


def resolve_state_root() -> Path:
    return CheckoutState(root=project_root()).resolve()


def open_mailbox(run_id: str) -> QuestionMailbox:
    """The mailbox for one run, refusing a run that was never recorded."""
    root = resolve_state_root() / run_id
    if not root.is_dir():
        refuse(f"names no resolver run under {root.parent}", what=run_id, code=2)
    return QuestionMailbox(root)


def open_cohort(run_id: str) -> ActorCohort:
    """This run's population, as the run's own process holds it.

    Over the same mail and the same journal the run writes, so a door and
    the process it is steering read one record rather than two — and the
    address this resolves is the address the run's own tools resolve.

    A cohort here opens no session and spawns nobody. It is the reading half:
    who the run holds, what each was asked, and what is queued for whom, all
    of it folded from files the run has already written.
    """
    return run_cohort(open_mailbox(run_id), run_id)


def pending_views(mailbox: QuestionMailbox) -> list[PendingQuestionView]:
    """Every question a run has asked, straight from its mailbox.

    The state file is never read here: a moving run's ``state.json`` lags
    its mailbox by design, and the mailbox alone is authoritative for
    anything still pending.
    """
    answered = {
        record.answer.question_id: record.answer.value for record in mailbox.answers()
    }
    offered = {offer.question_id: offer.value for offer in mailbox.offers()}
    return [
        PendingQuestionView(
            question=item.question,
            asked_by=item.asked_by,
            answered=(
                answered[item.question.id] if item.question.id in answered else None
            ),
            offer=offered[item.question.id] if item.question.id in offered else None,
        )
        for item in mailbox.questions()
    ]


def describe(view: PendingQuestionView) -> list[str]:
    """Render one question the way an operator needs to answer it cold."""
    question = view.question
    lines = [f"{question.id} (concern {question.concern_id})", f"  {question.prompt}"]
    if question.choices:
        lines.append("  choices: " + " | ".join(question.choices))
    if question.recommendation is not None:
        lines.append(f"  recommendation: {question.recommendation}")
    match view:
        case PendingQuestionView(answered=str() as answered):
            lines.append(f"  answered: {answered}")
        case PendingQuestionView(offer=str() as offer):
            lines.append(f"  offered, not yet promoted: {offer}")
    return lines


def queued(run_id: str, to: str) -> list[str]:
    """Say what a just-posted message is waiting on, rather than that it sent.

    Reporting `sent` on the strength of having written the mailbox is what
    let a redirect read as successful while reaching nobody at all. The
    honest report is that it is queued, for whom, and — when the address
    matches no actor the run has recorded — that nobody it can name will
    read it. Not a refusal: a message may legitimately be posted for a
    concern that has not started, and it waits at that actor's first turn.
    """
    # The population's own answer, rather than a second one assembled here.
    # Every consumer that resolved an address for itself disagreed with
    # whatever printed it, and a message sent to the spelling shown reached
    # nobody.
    reached = open_cohort(run_id).reaching(to)
    if reached is None:
        return [
            f"queued for {to!r}, which names no actor this run has recorded "
            "yet; it waits until one by that address takes a turn",
        ]
    return [
        f"queued for {reached.label()}; it arrives at that actor's next tool "
        "call or turn"
    ]


app = typer.Typer(help="Read and answer resolver runs from the console")


@app.command("questions")
def list_questions(
    run_id: str = typer.Option(..., "--run-id", help="Run whose mailbox to read"),
    pending_only: bool = typer.Option(
        False, "--pending", help="Show only questions still waiting on you"
    ),
) -> None:
    """List a run's questions and what each one has been answered.

    ``--pending`` is what a human reads after answering, so it means what
    they mean by it: still waiting on you. A question already offered is
    waiting on the run to take it, not on another answer — listed as
    pending it would read as though nothing had been recorded, and the only
    way to tell would be to go and count files on disk.
    """
    views = pending_views(open_mailbox(run_id))
    waiting = [view for view in views if view.answered is None and view.offer is None]
    offered = [view for view in views if view.answered is None and view.offer]
    selected = waiting if pending_only else views
    if not selected:
        typer.echo("No questions." if not views else "Nothing waiting on you.")
        return
    for view in selected:
        for line in describe(view):
            typer.echo(line)
    if pending_only and offered:
        typer.echo(
            f"{len(offered)} answered and awaiting promotion; "
            "the run takes them when it next advances"
        )


@app.command("answer")
def answer_questions(
    pairs: list[str] = typer.Argument(..., help="<question-id>=<value>, repeatable"),
    run_id: str = typer.Option(..., "--run-id", help="Run whose mailbox to write"),
) -> None:
    """Offer an answer to one or more of a run's questions.

    Offers stay correctable right up until a promoter takes one, so a
    mistyped free-text value is fixed by offering again rather than being
    permanent — nothing under ``.lup/resolve`` is ever unlinked.
    """
    mailbox = open_mailbox(run_id)
    known = {view.question.id: view for view in pending_views(mailbox)}
    for identifier, value in parse_answer_flags(pairs).items():
        if identifier not in known:
            refuse(
                "names no question this run asked",
                what=identifier,
                steps=[
                    step(
                        "see the questions it asked",
                        devtools("resolve", "questions", "--run-id", run_id),
                    )
                ],
                code=2,
            )
        question = known[identifier].question
        if question.closed_choices and value not in question.choices:
            refuse(
                "accepts only: " + ", ".join(question.choices),
                what=identifier,
                code=2,
            )
        try:
            mailbox.offer(
                AnswerOffer(
                    run_id=run_id,
                    question_id=identifier,
                    value=value,
                    door=AnswerDoor.CONSOLE,
                    offered_at=utc_now(),
                )
            )
        except MailboxConflictError as error:
            refuse(str(error), what=identifier, code=2)
        typer.echo(f"offered {identifier}={value}")


@app.command("actors")
def list_actors(
    run_id: str = typer.Option(..., "--run-id", help="Run whose record to read"),
) -> None:
    """List every actor this run has recorded, and what each has not read yet.

    Undelivered mail is shown because sending is not delivering: a door
    writes the stream and the actor reads it at its next tool call or turn,
    and nothing between those two moments otherwise says which has happened.
    A redirect sitting here through a whole concern is that concern being
    worked on the instructions it was supposed to abandon.
    """
    # Before the journal is read, not after. A run directory that is not
    # there yields no actors, so asking after would print "nothing recorded
    # yet" and exit zero — indistinguishable from a real run that has not
    # started, and the answer a sibling worktree with no `.lup` gives for any id.
    cohort = open_cohort(run_id)
    members = cohort.live()
    if not members:
        typer.echo("No actor has recorded anything yet.")
        return
    # What the person has been told, first, because it is addressed to whoever
    # is reading this. A worker's report that went out unaddressed is matched
    # and consumed by every actor, so the one message meant for a person
    # would be the one no surface shows.
    told = cohort.heard().messages
    if told:
        typer.echo(f"{cohort.user.label()} — said to you by this run's actors:")
        for message in told:
            typer.echo(f"  {message.text}")
    for member in members:
        # What each was asked and how it ended, which the population record
        # carries and a list of bare addresses never could: an operator
        # deciding which spawn they meant is choosing between tasks, not
        # between identifiers.
        state = "working" if member.running else (member.error or member.summary)
        typer.echo(
            f"{member.address} — {member.task}" + (f" [{state}]" if state else "")
        )
        for message in cohort.mail.waiting(member.actor).messages:
            kind = "redirect" if message.redirect else "message"
            typer.echo(f"  undelivered {kind} from {message.door}: {message.text}")


@app.command("say")
def say_to_actor(
    text: str = typer.Argument(..., help="What to tell the actor"),
    run_id: str = typer.Option(..., "--run-id", help="Run whose mailbox to write"),
    to: str = typer.Option(..., "--to", help="Actor label from `actors`"),
    in_reply_to: str = typer.Option(
        "", "--in-reply-to", help="Question or message id this answers, if any"
    ),
) -> None:
    """Tell one actor something. It reads this and keeps going.

    The recipient is required. A message is addressed and consumed, so
    "everyone" would have to mean everyone *working* — and something worth
    saying to a whole run is almost always a fact that stays true, which
    `notice` states where the actors starting next hour also read it.
    """
    # Through the cohort, which is the one place a message is built. A door
    # assembling its own reached the stream by a second route, and the two
    # had to be kept agreeing by hand about what an address means.
    open_cohort(run_id).post(to, text, door=AnswerDoor.AGENT, in_reply_to=in_reply_to)
    for line in queued(run_id, to):
        typer.echo(line)


@app.command("notice")
def post_notice(
    text: str = typer.Argument(..., help="What is true for this whole run"),
    run_id: str = typer.Option(..., "--run-id", help="Run whose store to write"),
) -> None:
    """State something true for every actor, now and for whoever starts next.

    A notice is state rather than mail: it is read at the head of every turn
    for as long as it stands, so an actor spawned an hour from now reads it
    at its first. Whoever is working is also sent it as an ordinary message,
    because a fact worth stating is worth hearing before the turn they are in
    ends. Take it down with `unnotice` once it stops being true.
    """
    cohort = open_cohort(run_id)
    cohort.notify(text, door=AnswerDoor.AGENT)
    live = [member.address for member in cohort.live() if member.running]
    typer.echo(
        "standing for every actor, including ones not yet started"
        + (f"; told now to {', '.join(live)}" if live else "; nobody is working yet")
    )


@app.command("notices")
def list_notices(
    run_id: str = typer.Option(..., "--run-id", help="Run whose store to read"),
) -> None:
    """Everything standing over this run, with the id that takes one down."""
    standing = open_cohort(run_id).mail.standing()
    if not standing:
        typer.echo("Nothing is standing over this run.")
        return
    for notice in standing:
        typer.echo(f"{notice.id} — {notice.text} [{notice.door}]")


@app.command("unnotice")
def retract_notice(
    notice_id: str = typer.Argument(..., help="Notice id from `notices`"),
    run_id: str = typer.Option(..., "--run-id", help="Run whose store to write"),
) -> None:
    """Take one standing fact down, so no later turn reads it.

    Deleted rather than marked retracted: a notice is read as state, so what
    is there is what is true. Nothing is re-read by the actors that already
    saw it, which is the point — they were told while it held.
    """
    if not open_cohort(run_id).mail.retract(notice_id):
        refuse(f"names nothing standing over {run_id}", what=notice_id, code=2)
    typer.echo(f"retracted {notice_id}")


@app.command("accept")
def accept_verification(
    reason: str = typer.Argument(..., help="Why this failure is accepted"),
    run_id: str = typer.Option(..., "--run-id", help="Run whose state to write"),
    concern: str = typer.Option(..., "--concern", help="Concern to accept"),
    verification: str = typer.Option(
        ..., "--verification", help="The failing verification, by name"
    ),
) -> None:
    """Accept one concern over one failing verification, on the human's word.

    A verdict is an exit code, and some failures are true but unfixable from
    inside the lease that meets them — a finding the worker did not
    introduce and cannot converge on. Resubmitting into it spends a revision
    round each time until the concern fails with its criteria never read.

    The reason is required because this is what review sees in place of a
    green check that was never green.
    """
    root = resolve_state_root() / run_id
    if not root.is_dir():
        refuse(f"names no resolver run under {root.parent}", what=run_id, code=2)
    ResolverStateRepository(resolve_state_root(), run_id).accept(
        VerificationAcceptance(
            concern_id=concern, verification=verification, reason=reason
        )
    )
    typer.echo(f"accepted {concern} over {verification}: {reason}")


@app.command("redirect")
def redirect_actor(
    text: str = typer.Argument(..., help="What the actor should do instead"),
    run_id: str = typer.Option(..., "--run-id", help="Run whose mailbox to write"),
    to: str = typer.Option(
        "",
        "--to",
        help="Actor label from `actors`; empty stops every actor that is working",
    ),
) -> None:
    """Stop an actor and put it on something else.

    Where `say` rides alongside the actor's next tool call, this refuses that
    call and hands back this text as the reason — so an actor going the wrong
    way cannot take one more step down it before reading why it was stopped.

    With no `--to` it stops whoever is working, and nobody else. A redirect
    denies a tool call, so an actor that has not started has no call to deny
    and no reason to be stopped: one spawned after this was spawned *knowing*
    about it, and stopping it would refuse its first call with somebody
    else's reason.
    """
    cohort = open_cohort(run_id)
    if to:
        cohort.post(to, text, redirect=True, door=AnswerDoor.AGENT)
        for line in queued(run_id, to):
            typer.echo(line)
        return
    cohort.redirect_all(text, door=AnswerDoor.AGENT)
    stopped = [member.address for member in cohort.live() if member.running]
    typer.echo(
        f"queued for {', '.join(stopped)}; it denies each one's next tool call"
        if stopped
        else "no actor in this run is working, so nothing was stopped"
    )


@app.command("park")
def park_run(
    run_id: str = typer.Option(..., "--run-id", help="Run to park"),
    reason: str = typer.Option(
        "parked from the console", "--reason", help="Why the run is being parked"
    ),
) -> None:
    """Ask every open wait in this run to give up now.

    This reaches a run sitting on an answer and no other. To stop one that
    is *working*, use `drain`: a worker inside a model turn waits on
    nothing, so park does not reach it.
    """
    open_mailbox(run_id).park(ParkRequest(run_id=run_id, reason=reason))
    typer.echo(f"parked {run_id}: {reason}")


@app.command("drain")
def drain_run(
    run_id: str = typer.Option(..., "--run-id", help="Run to drain"),
    reason: str = typer.Option(
        "drained from the console", "--reason", help="Why the run is being stopped"
    ),
) -> None:
    """Ask a busy run to finish what is in flight and stop, resumably.

    The other half of `park`, and a different request. Park ends every open
    wait, which never reached a worker mid-turn — so the only way to end a
    busy run was to kill it, discarding the uncommitted edits of each
    interrupted round along with its reviewer feedback and round counter.

    A drain is observed at the top of a round, where the previous round is
    already committed, and at the boundary between dependency batches.
    Nothing is failed and nothing is written off. It takes effect when the
    run next reaches one of those, so a long turn finishes first.
    """
    open_mailbox(run_id).drain(ParkRequest(run_id=run_id, reason=reason))
    typer.echo(f"draining {run_id}: {reason}")
    typer.echo("It stops at its next round or batch boundary; resuming costs nothing.")


def show_status(
    run_id: str = typer.Option(..., "--run-id", help="Run to report on"),
    watch: bool = typer.Option(
        False,
        "--watch",
        help="Keep reporting until the run parks or finishes, instead of "
        "answering once",
    ),
    heartbeat: float = typer.Option(
        60.0,
        "--heartbeat",
        help="Seconds between verdict lines while nothing changes, so silence "
        "is never ambiguous",
    ),
    poll: float = typer.Option(
        2.0, "--poll", help="Seconds between readings of the run directory"
    ),
    startup: float = typer.Option(
        30.0,
        "--startup",
        help="Seconds a watch allows a just-launched run to take its lock "
        "before an unheld run reads as parked rather than as starting",
    ),
    line: bool = typer.Option(
        False,
        "--line",
        help="Print this report's first line and stop. It carries what "
        "needs you — the time, the phase, how far along, how much longer, "
        "and a question waiting, a stop or a failure. What the full report "
        "adds below it is the per-status breakdown and the last event",
    ),
) -> None:
    """Say whether a run is alive, where it stands, and what it last did.

    The liveness answer comes from the run's own lock rather than from the
    process table, because under a sandbox `/proc` is PID-isolated: `ps` and
    `pgrep` list nothing outside the current shell, so a healthy run looks
    exactly like one that died. That ambiguity has produced confident wrong
    conclusions in both directions, and the run directory is the only thing
    a reader is guaranteed to be able to see.
    """
    root = resolve_state_root()
    repository = ResolverStateRepository(root, run_id)
    status = run_status(repository, run_id)
    if not status.exists:
        refuse(f"names no resolver run under {root}", what=run_id, code=2)
    if line and not watch:
        typer.echo(status_header(status))
        return
    if not watch:
        report_status(status)
        return
    typer.echo(status_header(status))
    report_waiting(run_id)
    watch_status(repository, run_id, heartbeat, poll, startup, status, line)


def status_header(status: RunStatus) -> str:
    """The report's first line, and on its own what ``--line`` prints.

    One composition rather than two, so the compact form cannot drift from
    the full one: what a full report adds below this line is only what this
    line leaves out — the per-status breakdown, and the last journal event.

    A breakdown of every status is progress rather than attention: nobody
    acts on "9 retired", and a reader scanning for what needs them has to
    find it among nine figures that do not. What is here instead is how far
    along the run is, how much longer the phase it is in should take, what
    it has lost on the way, and whether it is holding anybody up.

    A loss is named only where there is one, so its presence means it, and
    it joins the liveness rather than standing in for it: a run works
    quietly through a concern that failed days ago, and a line carrying the
    failure alone reads as though it were happening now.

    The fraction counts every concern that reached a terminal state rather
    than only the ones that produced work, so it can actually arrive at its
    own total. Measured against the plan it would stop short by every
    concern retired or found ineligible, and a progress figure that can
    never complete teaches a reader to stop believing it.

    Its numerator is the run's own tally, read rather than re-summed from
    the breakdown below it. Summing the settled statuses out of ``counts``
    is the same arithmetic on a projection that has dropped the stamp the
    tally counts by, and it reports a fall of nine the moment assembly
    moves verified concerns to ``integrating``.
    """
    counts = {count.status: count.concerns for count in status.counts}
    done = status.settled_count()
    total = sum(counts.values())
    failed = counts[ConcernStatus.FAILED] if ConcernStatus.FAILED in counts else 0
    waiting = status.unanswered
    iterator = status.progress
    return " · ".join(
        [
            local_stamp(),
            str(status.phase or "initializing"),
            f"{done}/{total} settled",
            *(
                [f"{iterator.label} {iterator.render(active=status.held)}"]
                if iterator
                else []
            ),
            *([f"{failed} failed"] if failed else []),
            *(
                [f"{waiting} question{'' if waiting == 1 else 's'} waiting"]
                if waiting
                else []
            ),
            status.verdict(),
        ]
    )


def report_status(status: RunStatus) -> None:
    """Print one reading of a run: the header, then what it leaves out.

    Stamped because whoever reads this reads it inside a report written
    later still, and "how long since I was last told anything" is the
    question they are actually asking. The run's own relative ages answer a
    different one: how long a worker has been quiet.
    """
    typer.echo(status_header(status))
    for count in status.counts:
        typer.echo(f"  {count.concerns:>3} {count.status}")
    if status.last is not None:
        typer.echo(
            f"  last: {status.last.event} by {status.last.actor} "
            f"at {status.last.at:%Y-%m-%d %H:%M:%S}Z"
        )


def report_waiting(run_id: str) -> None:
    """Print any question now waiting, whole, beside the change that raised it.

    A watch reports what moved; a question that moved is one somebody has to
    read, and naming it without carrying it makes every wake-up start with a
    round trip to fetch what happened. The reader is an agent that must
    measure a question's claims against the tree before relaying it, so what
    it needs is the question's own words — the notes it was raised from, the
    concern's specification, its acceptance criteria — not a count.

    Carrying it also survives a channel that drops: a notification lost after
    this one still leaves the question in hand, where a count that went
    missing leaves no trace that anything was ever asked.
    """
    waiting = [
        view
        for view in pending_views(open_mailbox(run_id))
        if view.answered is None and view.offer is None
    ]
    for view in waiting:
        for line in describe(view):
            typer.echo(line)


def watch_status(
    repository: ResolverStateRepository,
    run_id: str,
    heartbeat: float,
    poll: float,
    startup: float,
    first: RunStatus,
    terse: bool = False,
) -> None:
    """Report a run until it parks or finishes, so nobody hand-rolls the loop.

    The supervisor page serves the same projection, but it is server-sent
    events to a browser and an agent in a terminal cannot consume it. What
    an agent reinvented instead was a `tail`, which cannot see a question
    queued by a worker that keeps its siblings running — the run does not
    park, prints nothing, and the answer waits indefinitely.

    Both halves are load-bearing. Emitting on change alone leaves a reader
    unable to tell a quiet run from a dead watch, and the heartbeat alone
    would report a change up to a minute after it happened.
    """
    frame = first.watched()
    running_yet = first.held
    quiet = 0.0
    waited = 0.0
    while not (status := run_status(repository, run_id)).settled(running_yet):
        running_yet = running_yet or status.held or waited >= startup
        sleep(poll)
        quiet += poll
        waited += poll
        if status.watched() != frame:
            frame = status.watched()
            quiet = 0.0
            if terse:
                typer.echo(status_header(status))
            else:
                report_status(status)
            report_waiting(run_id)
            continue
        if quiet >= heartbeat:
            quiet = 0.0
            typer.echo(status_header(status))
    # Nothing is reported here. Every way this loop ends moves a field the
    # frame is taken over — the lock is released, or the phase becomes
    # terminal — so the reading that ended it was already printed as a
    # change, and a run settled before the first poll was printed by the
    # caller.
    if status.phase is None:
        typer.echo(
            f"{local_stamp()} — watch ended before initialization; inspect {repository.root / 'detached.log'}."
        )
        return
    typer.echo(f"{local_stamp()} — watch ended: {run_id} is waiting on you.")
    report_waiting(run_id)


def retire_concern(
    reason: str = typer.Argument(
        ..., help="Where this concern was settled — a commit, branch or issue"
    ),
    run_id: str = typer.Option(..., "--run-id", help="Run whose state to write"),
    concern: str = typer.Option(..., "--concern", help="Concern to retire"),
) -> None:
    """Retire one concern whose work was settled somewhere other than this run.

    A run parked while its branch moved forward routinely finds the branch
    already did some of its work, and base refresh makes that the expected
    consequence of following a branch rather than a rare accident. Without
    this, every route is wrong: hand-resolving an add/add conflict between
    two independent implementations of one thing, letting a worker open on a
    concern whose notes no longer exist in its tree, or aborting the whole
    run — discarding every settled answer — to retire one concern.

    The concern leaves the eligible set without failing, its dependents build
    from the base where the work that settled it now lives, and its lease
    stops being active. The worktree and branch stay: a retired concern often
    built its own answer to what landed upstream, and that is worth reading
    before it is thrown away.
    """
    root = resolve_state_root() / run_id
    if not root.is_dir():
        refuse(f"names no resolver run under {root.parent}", what=run_id, code=2)
    try:
        ResolverStateRepository(resolve_state_root(), run_id).retire(
            ConcernRetirement(concern_id=concern, reason=reason)
        )
    except StateTransitionError as error:
        refuse(str(error), what=concern, code=2)
    typer.echo(f"retired {concern}: {reason}")
