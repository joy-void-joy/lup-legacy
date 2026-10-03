"""The command tree a person reaches the repository's other sessions through.

A headless agent has tools; a person has this. They drive the same roster over
the same files, so what a console says is here is what a session addressing it
will reach — and a message a person sends arrives by the path an agent's
message arrives by, rather than by a second one that agrees until it does not.

Every command reads the shared git directory rather than a worktree, so any
checkout of the repository answers and none of them owns the answer. None of
them holds a lock either: a listing folds an append-only record, so a session
running flat out does not delay a console reading it, and a console cannot
wedge a session.
"""

import uuid
from pathlib import Path
from typing import Annotated

import typer

from lup.channels.models import Door
from lup.coordination.bare.mail import new_post_id
from lup.coordination.holds import (
    Hold,
    HoldScope,
    held_calls,
    operator_pause,
    standing,
)
from lup.coordination.identity import NameTakenError, mint_member_id, session_member_id
from lup.coordination.mail import Posting
from lup.coordination.peers import USER_ADDRESS
from lup.coordination.repository import (
    PeerDepartedError,
    PeerView,
    RepositoryPeers,
    nested,
)
from lup.coordination.roster import Delivery
from lup.devtools.coordination.pausing import pause, resume
from lup.coordination.touches import HeldPath
from lup.coordination.watch import Watcher
from lup.coordination.watcher import watcher_pipeline
from lup.runs.pipeline import RunRequest
from lup.workspace.paths import project_root


def peer_line(view: PeerView) -> str:
    """One roster row as a person reads it: who, where, and what they are on.

    What the session *holds* is a count rather than the paths, and the count
    is the decision: it says whether there is anything to ask about, and
    `coordination holdings` is where the paths already live. Contested is
    called out separately because it is the half a reader acts on — it means
    two sessions are in the same place and neither knows.

    The id rides beside a name, because it is the spelling that always
    reaches this row: a name may be what another row once answered to, and
    a person resolving that needs the one that cannot collide.
    """
    where = Path(view.member.worktree).name if view.member.worktree else ""
    state = "" if view.member.running else " [gone]"
    holding = f"holding {len(view.holding)}" if view.holding else ""
    contested = f"{len(view.contested)} contested" if view.contested else ""
    named = (
        f"{view.cli_name} ({view.member.actor.id})"
        if view.cli_name
        else view.member.actor.id
    )
    return " — ".join(
        part
        for part in (
            f"{named}{state}",
            where,
            view.doing,
            holding,
            contested,
            view.member.delivery,
        )
        if part
    )


def claim_line(claim: HeldPath) -> str:
    """One holding as a person reads it: what, who, and whether anyone is sure.

    The holders are spelled out in full where there is more than one, because
    that row means something different from the others — nothing could tell
    who made the change — and a reader skimming for whom to ask has to see
    that it is a question rather than an answer.
    """
    holders = ", ".join(holder.id for holder in claim.holders)
    return " — ".join(
        part
        for part in (
            f"{'under' if claim.prefix else 'at'} {claim.path}",
            f"contested by {holders}" if len(claim.holders) > 1 else holders,
        )
        if part
    )


def create_coordination_app() -> typer.Typer:
    """Wire the command tree for reaching this repository's other sessions."""
    app = typer.Typer(no_args_is_help=True)

    def peers() -> RepositoryPeers:
        return RepositoryPeers(project_root())

    @app.command("roster")
    def roster_cmd() -> None:
        """List every session working in this repository, and the recent departures.

        The live rows and those that stopped within the retention window,
        rather than everyone who ever joined: whether the peer a person sent
        something to is still there is a question the listing has to answer,
        and a roster read a month on must not answer it with a month of
        history. A session's native subagents are indented beneath it.
        """
        listing = peers().recent()
        if not listing:
            typer.echo("No session is working in this repository.")
            return
        for view in nested(listing):
            typer.echo(peer_line(view))
            for child in view.subagents:
                typer.echo(f"  {peer_line(child)}")

    @app.command("join")
    def join_cmd(
        name: Annotated[
            str,
            typer.Option("--name", help="What to call this session on the roster"),
        ] = "",
        member_id: Annotated[
            str,
            typer.Option(
                "--id",
                help="A durable id; defaults to the launcher identity, then a minted id",
            ),
        ] = "",
        worktree: Annotated[
            Path | None,
            typer.Option("--worktree", help="Where it works; defaults to this tree"),
        ] = None,
    ) -> None:
        """Put a session on the roster and print the id it answers to.

        The id is printed because it is what the session has to carry: a
        launcher exports it, and a session that self-minted has no other way
        to learn what it was given.
        """
        chosen = member_id or session_member_id() or mint_member_id()
        tree = worktree or project_root()
        try:
            peers().join(chosen, tree, cli_name=name, delivery=Delivery.WAITING)
        except NameTakenError as taken:
            raise typer.BadParameter(str(taken)) from taken
        typer.echo(chosen)

    @app.command("describe")
    def describe_cmd(
        description: Annotated[str, typer.Argument(help="What it is working on now")],
        member_id: Annotated[
            str, typer.Option("--id", help="Which session, by its durable id")
        ],
    ) -> None:
        """Record what one session is doing, for whoever reads the roster next."""
        peers().describe(member_id, description)

    @app.command("rename")
    def rename_cmd(
        name: Annotated[str, typer.Argument(help="What to call it from now on")],
        member_id: Annotated[
            str, typer.Option("--id", help="Which session, by its durable id")
        ],
    ) -> None:
        """Rename one session, leaving the old name resolving to it."""
        try:
            peers().rename(member_id, name)
        except NameTakenError as taken:
            raise typer.BadParameter(str(taken)) from taken

    @app.command("leave")
    def leave_cmd(
        member_id: Annotated[
            str, typer.Option("--id", help="Which session, by its durable id")
        ],
        summary: Annotated[
            str, typer.Option("--summary", help="What it concluded, if anything")
        ] = "",
    ) -> None:
        """Record that a session has stopped, so nothing addresses it again."""
        peers().leave(member_id, summary=summary)

    @app.command("sweep")
    def sweep_cmd(
        dry_run: Annotated[
            bool,
            typer.Option(
                "--dry-run",
                "-n",
                help="Name the sessions that would be retired, retiring none",
            ),
        ] = False,
    ) -> None:
        """Retire every session whose pulse has stopped, so nothing addresses it again.

        What the server answering for a session does on each of its ticks,
        for a roster no server is up on: a machine whose sessions all ended without
        writing a departure, or a store whose sessions never beat at all.
        """
        found = peers()
        rows = found.lapsed() if dry_run else found.sweep()
        # The address beside the name, because a retired session's name may
        # since have been taken by a live one, and the label is what still
        # reaches its record.
        for member in rows:
            name = found.called(member.actor.id) or member.actor.id
            typer.echo(f"{name} — {member.actor.label()} — {member.error}")
        typer.echo(f"{'would retire' if dry_run else 'retired'} {len(rows)} session(s)")

    @app.command("send")
    def send_cmd(
        text: Annotated[str, typer.Argument(help="What the peer should read")],
        to: Annotated[
            str, typer.Option("--to", help="A name, an id, or a printed address")
        ],
        redirect: Annotated[
            bool,
            typer.Option("--redirect", help="Stop the peer rather than inform it"),
        ] = False,
        sender: Annotated[
            str,
            typer.Option(
                "--as",
                help="Who signs it, which is where a reply goes: `user` for you, "
                "or a member's id",
            ),
        ] = "",
        in_reply_to: Annotated[
            str,
            typer.Option(
                "--reply-to",
                help="The post it answers, as its mailbox heading names it",
            ),
        ] = "",
    ) -> None:
        """Send one message to a peer, and say what will carry it there.

        Reporting what carries it rather than that it sent, because those are
        different claims and only the first is knowable here: the mailbox
        accepting a message says nothing about anyone reading it, and a sender
        told "sent" goes on believing a peer was informed.

        Signed with `--as`, a reply comes back to that mailbox — `user` is the
        person's, which `mailbox --id user` reads. A reply names the post it
        answers with `--reply-to` and joins that post's thread.
        """
        known = peers()
        if sender and sender != USER_ADDRESS and known.row(sender) is None:
            raise typer.BadParameter(
                f"nobody here signs as {sender!r}: `--as` takes `user` or a "
                "member's id, which `dev coordination roster` lists"
            )
        post = new_post_id()
        try:
            found = known.send(
                to,
                text,
                redirect=redirect,
                door=Door.CONSOLE,
                in_reply_to=in_reply_to,
                sender=sender,
                posting=Posting(post=post),
            )
        except PeerDepartedError as departed:
            raise typer.BadParameter(
                f"{departed}; `dev coordination roster` lists who is here"
            ) from departed
        if found is None:
            raise typer.BadParameter(
                f"no session answers to {to!r}; "
                "`dev coordination roster` lists who is here"
            )
        carries = known.cohort.delivery(found)
        typer.echo(f"queued for {found.label()} as post {post}, carried by {carries}")

    @app.command("notice")
    def notice_cmd(
        text: Annotated[
            str, typer.Argument(help="What is true for everyone working here")
        ],
        by: Annotated[
            str, typer.Option("--by", help="Who is stating it, where it matters")
        ] = "",
    ) -> None:
        """State something true for every session, now and for whoever starts next.

        A notice is state rather than mail: every session reads it at the head
        of a prompt for as long as it stands, so one opened tomorrow is told at
        its first. Whoever is working is sent it now as well, because a fact
        worth stating is worth hearing before the turn they are in ends.

        Take it down with `unnotice` once it stops being true — what is there
        is what is true, so a notice nobody retracted goes on being read.
        """
        found = peers()
        found.notify(text, door=Door.CONSOLE, by=by)
        told = [view.address for view in found.listing()]
        typer.echo(
            "standing for every session, including ones not yet started"
            + (f"; told now to {', '.join(told)}" if told else "; nobody is here yet")
        )

    @app.command("notices")
    def notices_cmd() -> None:
        """Everything standing over this repository, with the id that takes one down."""
        standing = peers().cohort.mail.standing()
        if not standing:
            typer.echo("Nothing is standing over this repository.")
            return
        for notice in standing:
            said = notice.text + (f" ({notice.by})" if notice.by else "")
            typer.echo(f"{notice.id} — {said} [{notice.door}]")

    @app.command("unnotice")
    def unnotice_cmd(
        notice_id: Annotated[str, typer.Argument(help="Notice id from `notices`")],
    ) -> None:
        """Take one standing fact down, so no later prompt reads it.

        Deleted rather than marked retracted: a notice is read as state, so
        what is there is what is true. The sessions already told keep what they
        were told, which is right — it held while they were told it.
        """
        if not peers().cohort.mail.retract(notice_id):
            raise typer.BadParameter(
                f"{notice_id!r} names nothing standing over this repository; "
                "`dev coordination notices` lists what does"
            )
        typer.echo(f"retracted {notice_id}")

    @app.command("mailbox")
    def mailbox_cmd(
        member_id: Annotated[
            str,
            typer.Option(
                "--id", help="Whose mailbox to read: a member's id, or `user` for yours"
            ),
        ],
        take: Annotated[
            bool,
            typer.Option("--take", help="Consume what is read, rather than peek"),
        ] = False,
    ) -> None:
        """Read what is queued for one session, consuming it only when asked.

        Peeking by default, because reading a mailbox is how a person finds out
        whether a peer has been reached — and a read that consumed would be a
        read that stopped the peer ever seeing it. Each message is headed as
        its reader's hook heads it: who sent it, which is where a reply goes,
        and the post a reply names (`send --reply-to`).
        """
        found = peers()
        delivery = found.take(member_id) if take else found.waiting(member_id)
        for message in delivery.messages:
            typer.echo(f"{message.heading()} {message.text}")

    def operator_only(verb: str) -> None:
        """Refuse a shell an agent's runtime started: pausing and resuming are the operator's.

        A launch exports the session's member id into everything its runtime
        starts, every shell an agent runs included, so its presence is an
        agent's shell; the operator's own terminal carries none.
        """
        if session_member_id():
            raise typer.BadParameter(
                f"an agent's shell cannot {verb} any agent, itself included: the "
                "operator does, on the dashboard or from a terminal outside "
                "every agent session"
            )

    def scoped(
        known: RepositoryPeers, member: str, tree: bool, repository: bool
    ) -> Hold:
        """The pause a pause or resume names: an agent's, its tree's, or the repository's."""
        if repository:
            if member or tree:
                raise typer.BadParameter("--repository names no agent and no tree")
            return operator_pause(HoldScope.REPOSITORY)
        if not member:
            raise typer.BadParameter(
                "name an agent by its name or id, or pass --repository or --everything"
            )
        found = known.address(member)
        if found is None:
            raise typer.BadParameter(
                f"no agent answers to {member!r}; `coordination roster` lists who is here"
            )
        return operator_pause(HoldScope.TREE if tree else HoldScope.AGENT, found.id)

    def everywhere() -> list[RepositoryPeers]:
        """This repository and every other one the operator's dashboard serves."""
        from lup.devtools.dashboard.companion import Dashboard, DashboardRegistry

        root = project_root()
        state = Dashboard().standing(root).place.state
        checkouts = [
            known.checkout
            for known in DashboardRegistry(directory=state).repositories()
        ]
        found = {
            peers.root: peers for peers in map(RepositoryPeers, [root, *checkouts])
        }
        return list(found.values())

    @app.command("pause")
    def pause_cmd(
        member: Annotated[
            str, typer.Argument(help="The agent to pause: a name, an id, or an address")
        ] = "",
        tree: Annotated[
            bool,
            typer.Option("--tree", help="It and everything it spawned, at any remove"),
        ] = False,
        repository: Annotated[
            bool, typer.Option("--repository", help="Every agent of this repository")
        ] = False,
        everything: Annotated[
            bool,
            typer.Option(
                "--everything",
                help="Every agent of every repository the dashboard serves",
            ),
        ] = False,
        freeze: Annotated[
            bool,
            typer.Option(
                "--freeze",
                help="Also stop its running commands and interrupt its turn now",
            ),
        ] = False,
    ) -> None:
        """Hold an agent at its next tool call until it is resumed.

        The agent is told nothing: its next call simply waits, and goes on the
        moment it is resumed, a call already running finishing first. A
        session's subagents are paused with it. `--freeze` also stops the
        commands its tools are running and interrupts a turn that is
        generating, and says what it could not freeze and why. Messages to a
        paused agent wait without waking it. Only the operator pauses, from
        here or the dashboard: an agent's own shell is refused.
        """
        operator_only("pause")
        if everything:
            group = uuid.uuid4().hex[:12]
            for found in everywhere():
                outcome = pause(found, HoldScope.REPOSITORY, freeze=freeze, group=group)
                typer.echo(f"{found.root.parents[1].name}: {outcome.detail}")
            return
        known = peers()
        named = scoped(known, member, tree, repository)
        try:
            outcome = pause(known, named.scope, named.member, freeze=freeze)
        except (LookupError, PeerDepartedError) as missing:
            raise typer.BadParameter(str(missing)) from missing
        typer.echo(outcome.detail)

    @app.command("resume")
    def resume_cmd(
        member: Annotated[
            str,
            typer.Argument(help="The agent to resume: a name, an id, or an address"),
        ] = "",
        tree: Annotated[
            bool,
            typer.Option("--tree", help="Lift the pause of it and what it spawned"),
        ] = False,
        repository: Annotated[
            bool, typer.Option("--repository", help="Lift this repository's pause")
        ] = False,
        everything: Annotated[
            bool,
            typer.Option(
                "--everything",
                help="Lift every repository's pause the dashboard serves",
            ),
        ] = False,
    ) -> None:
        """Let a paused agent's next tool call go, and wake it where it stopped.

        Lifts the pause named -- an agent's own, its tree's, the repository's
        -- and no other: an agent paused through its session is resumed
        there, which the refusal names. What a freeze stopped is continued, and
        a session that ended its turn because of the pause is woken with a
        bare "continue". A hold the budget placed is not lifted by a resume.
        """
        operator_only("resume")
        if everything:
            for found in everywhere():
                try:
                    outcome = resume(found, HoldScope.REPOSITORY)
                except LookupError as missing:
                    typer.echo(f"{found.root.parents[1].name}: {missing}")
                    continue
                typer.echo(f"{found.root.parents[1].name}: {outcome.detail}")
            return
        known = peers()
        named = scoped(known, member, tree, repository)
        try:
            outcome = resume(known, named.scope, named.member)
        except LookupError as missing:
            raise typer.BadParameter(str(missing)) from missing
        typer.echo(outcome.detail)

    @app.command("held")
    def held_cmd() -> None:
        """List every hold standing in this repository, and every call a hook is holding now."""
        known = peers()
        holds = standing(known.root)
        if not holds:
            typer.echo("Nothing is held in this repository.")
        for hold in holds:
            whom = known.called(hold.member) or hold.member or "the whole repository"
            frozen = ", frozen" if hold.freeze else ""
            typer.echo(
                f"{hold.owner.value} {hold.scope.value} hold on {whom}{frozen}: "
                f"{hold.said} (since {hold.placed:%Y-%m-%d %H:%M:%S} UTC)"
            )
        for call in held_calls(known.root):
            since = f" since {call.since:%H:%M:%S} UTC" if call.since else ""
            whom = known.called(call.member) or call.member
            typer.echo(f"  holding {whom}'s {call.tool or 'call'}{since}")

    @app.command("holdings")
    def holdings_cmd() -> None:
        """List what each live session in this repository is holding.

        Nobody declared any of it. A row is either a file some session's calls
        actually changed or a prefix one of them took deliberately, and a row
        naming more than one session is a change nothing could attribute —
        which is worth reading as it is rather than as a guess.
        """
        listing = peers().held()
        if not listing:
            typer.echo("No session is holding anything in this repository.")
            return
        for claim in listing:
            typer.echo(claim_line(claim))

    @app.command("lock")
    def lock_cmd(
        prefix: Annotated[Path, typer.Argument(help="What to take, as a path")],
        member_id: Annotated[
            str, typer.Option("--id", help="Which session, by its durable id")
        ],
    ) -> None:
        """Take everything beneath a prefix, before having touched any of it."""
        target = prefix.resolve()
        if not target.exists():
            raise typer.BadParameter(
                f"{target} does not exist; a lock covers what is there to write"
            )
        peers().lock(member_id, target)

    @app.command("release")
    def release_cmd(
        prefix: Annotated[Path, typer.Argument(help="What to give back, as a path")],
        member_id: Annotated[
            str, typer.Option("--id", help="Which session, by its durable id")
        ],
    ) -> None:
        """Give a prefix back, refusing where this session does not hold it."""
        target = prefix.resolve()
        if not peers().release(member_id, target):
            raise typer.BadParameter(
                f"session {member_id} does not hold {target}; "
                "`dev coordination holdings` lists who holds what"
            )

    @app.command("watch")
    def watch_cmd(
        member_id: Annotated[
            str,
            typer.Option(
                "--id", help="Follow one session's mail; everyone's by default"
            ),
        ] = "",
        interval: Annotated[
            float, typer.Option("--interval", help="Seconds between looks")
        ] = 2.0,
        nudge: Annotated[
            bool,
            typer.Option(
                "--nudge",
                help="Wake a member that has new mail, where its runtime allows",
            ),
        ] = False,
        as_run: Annotated[
            Path | None,
            typer.Option(
                "--as-run",
                help="Run as a pipeline landing in this directory, until the roster is empty",
            ),
        ] = None,
    ) -> None:
        """Stream what changes: who arrives and leaves, what they are on, what reaches them.

        Nothing is consumed. Mail is read the way a peek reads it, so a person
        watching a peer's mailbox is not the reason the peer never saw it. The
        first look is a baseline — the live roster and the waiting mail once —
        rather than a replay.

        `--as-run` is the unattended form: the same watcher declared as a run,
        so it survives whoever launched it, is followed with `run monitor
        <dir> --events`, and lands when there is nobody left to watch. It
        nudges, because a process nobody reads exists to act.
        """
        root = project_root()
        if as_run is not None:
            summary = watcher_pipeline(
                root, member=member_id, interval=interval, nudge=True
            ).execute(RunRequest(directory=as_run))
            typer.echo(f"watch ended: {summary.landed} landed")
            return
        watcher = Watcher(
            RepositoryPeers(root), root=root, member=member_id, nudge=nudge
        )
        try:
            for event in watcher.follow(interval=interval):
                typer.echo(event.line())
        except KeyboardInterrupt:
            return

    return app
