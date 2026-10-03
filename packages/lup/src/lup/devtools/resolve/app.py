"""The command tree for a resolver run: driving one, watching it, answering it.

Its own sub-app because a sub-app is a surface of a module, and every command
here is the resolver's. Spread across ``harness`` and ``dev``, a project
declining the resolver would keep every one of them — losing the skills and
the pages while still serving the whole run loop.

Nor do they belong under ``harness``. A run opens native sessions, which is
why the launcher and the run driver share a file; but ``harness resolve``
would tell a reader that answering a parked question is a kind of
generation, which it is not.
"""

from collections.abc import Callable
from pathlib import Path
from typing import Annotated

import typer

import lup.devtools.harness.resolve as resolve
import lup.devtools.dev.branches as branches
import lup.devtools.dev.resolve_review as resolve_review
from lup.devtools.dev.declarations import DevDeclarations
from lup.devtools.dev.issues import EXCLUDED_LABEL
from lup.devtools.resolve.cost import show_cost
from lup.devtools.resolve.recovery import recover_integration
from lup.devtools.resolve.actors import rebind_actor
from lup.devtools.harness.composition import NativeTargets, claude_profile_directory
from lup.devtools.supervisor.app import serve_supervisor
from lup.devtools.supervisor.doors import (
    accept_verification,
    answer_questions,
    drain_run,
    list_actors,
    list_questions,
    park_run,
    redirect_actor,
    retire_concern,
    say_to_actor,
    show_status,
)
from lup.devtools.supervisor.page import SUPERVISOR_PORT
from lup.providers.profiles import DefaultHomeProfile, ProfileDirectory
from lup.workspace.paths import project_root


def create_resolve_app(
    declared: Callable[[], DevDeclarations],
    targets: NativeTargets,
    model: resolve.ConfiguredModel | None = None,
    profiles: ProfileDirectory | None = None,
) -> typer.Typer:
    """Wire the resolver command tree over what one project declares.

    Takes the same three facts the launcher does and for the same reasons: the
    native targets because a run opens sessions against one, the configured
    model because a run that names none still has to select one, and the
    profile directory because every session a run opens is opened as an
    account somebody keeps.
    """
    directory = profiles or claude_profile_directory()
    app = typer.Typer(
        help="Drive the persisted resolver, and browse or answer its runs",
        invoke_without_command=True,
        no_args_is_help=False,
    )
    app.command("status")(show_status)
    app.command("cost")(show_cost)
    app.command("recover-integration")(recover_integration)
    app.command("supervise")(serve_supervisor)
    app.command("questions")(list_questions)
    app.command("answer")(answer_questions)
    app.command("actors")(list_actors)
    app.command("rebind-actor")(rebind_actor)
    app.command("say")(say_to_actor)
    app.command("accept")(accept_verification)
    app.command("retire")(retire_concern)
    app.command("redirect")(redirect_actor)
    app.command("park")(park_run)
    app.command("drain")(drain_run)
    app.command("refresh")(resolve.refresh_run)
    app.command("intake")(resolve.preview_intake)
    app.command("admissions")(resolve.list_admissions)

    @app.command("serve-tools")
    def serve_resolver_tools_command() -> None:
        """Serve one worker's question tools over stdio, for out-of-process runtimes."""
        resolve.run_resolver_tool_server()

    @app.command("branch")
    def resolve_branch_cmd(
        concern_id: Annotated[
            str, typer.Argument(help="Concern slug; becomes the resolve/<id> branch")
        ],
    ) -> None:
        """Create + switch to the resolve/<id> branch (a resolve editor's first step).

        Runs through the allowlisted `uv run lup-devtools` path so the bash hook
        needs no special case for the editor — autonomy for the editor lives in
        the edit hook.
        """
        branches.create_resolve_branch(concern_id)

    @app.command("review")
    def resolve_review_cmd(
        manifest: Annotated[
            Path,
            typer.Argument(help="Manifest JSON: workflow task output or a bare array"),
        ],
        base: Annotated[
            str,
            typer.Option(
                "--base", help="Snapshot base ref the resolve branches diff against"
            ),
        ],
        out: Annotated[
            Path,
            typer.Option("--out", help="Output HTML path"),
        ] = Path("tmp/resolve-review.html"),
        intro: Annotated[
            Path | None,
            typer.Option(
                "--intro", help="HTML fragment prepended as a run-specific header"
            ),
        ] = None,
    ) -> None:
        """Render a resolve manifest and its branch diffs into one static HTML review.

        One section per concern: the generalized spec, each original note paired
        with the verifier's per-note finding, the editor summary, the verdict, and
        the full colored diff against the base. The resolve skill runs this in
        its human gate so review reads concrete diffs instead of prose.
        """
        resolve_review.build_review(manifest, base, out, intro)

    @app.command("summary")
    def resolve_summary_cmd(
        manifest: Annotated[
            Path,
            typer.Argument(help="Manifest JSON: workflow task output or a bare array"),
        ],
    ) -> None:
        """Print per-concern verdicts from a resolve manifest.

        The terminal companion to `resolve review`: one block per concern with
        the committed/accepted flags, verdict reason, and residual, for planning
        merge order and approval batches before opening the HTML page.
        """
        resolve_review.summarize(manifest)

    @app.callback(invoke_without_command=True)
    def resolve_command(
        context: typer.Context,
        adapter: Annotated[
            str | None,
            typer.Option("--adapter", help=", ".join(targets.builders)),
        ] = None,
        profile: Annotated[
            str | None,
            typer.Option(
                "--profile",
                "-p",
                help="Account every session this run opens; defaults to the active one",
            ),
        ] = None,
        run_id: Annotated[
            str | None,
            typer.Option(
                "--run-id", help="Stable run id; defaults to the source commit"
            ),
        ] = None,
        answer: Annotated[
            list[str] | None,
            typer.Option(
                "--answer",
                help="Answer a parked material question as <question-id>=<value> "
                "(repeatable)",
            ),
        ] = None,
        abort: Annotated[
            str | None,
            typer.Option(
                "--abort",
                help="End this run with the given reason, freeing every concern "
                "worktree and branch. Retains the review branch and the run's "
                "recorded evidence. Requires the run's process to have exited.",
            ),
        ] = None,
        adopt_config: Annotated[
            bool,
            typer.Option(
                "--adopt-config",
                help="Resume a run whose composition moved, re-stamping it onto "
                "the current one. The refusal names which fields moved; adopt "
                "once they read as compatible, rather than aborting and losing "
                "every answer the run has collected.",
            ),
        ] = False,
        admit: Annotated[
            list[str] | None,
            typer.Option(
                "--admit",
                help="Work described in the human's own words (repeatable). It "
                "seeds a run that does not exist yet, alongside whatever notes "
                "the tree holds, and joins one that does — where only the new "
                "evidence is planned and recorded answers and completed work "
                "are kept.",
            ),
        ] = None,
        admit_note: Annotated[
            list[str] | None,
            typer.Option(
                "--admit-note",
                help="Admit a `# lup:` note already written in the tree, as "
                "<file>:<line> (repeatable). Its text is read from the file, so "
                "the admitted concern stays traceable to code.",
            ),
        ] = None,
        admit_issue: Annotated[
            list[int] | None,
            typer.Option(
                "--admit-issue",
                help="Admit an open tracker issue by number (repeatable). Its "
                "title and body are read from the tracker, so the admitted "
                "concern stays traceable to what was filed.",
            ),
        ] = None,
        issues: Annotated[
            bool,
            typer.Option(
                "--issues/--no-issues",
                help="Take the project's open issues as evidence alongside the "
                f"tree's notes, minus anything labelled `{EXCLUDED_LABEL}`.",
            ),
        ] = True,
        wait: Annotated[
            float,
            typer.Option(
                "--wait",
                help="Seconds to wait for a human to answer a material question "
                "before parking the run. Zero parks immediately, so an unattended "
                "invocation is deterministic.",
            ),
        ] = 0.0,
        supervise: Annotated[
            bool,
            typer.Option(
                "--supervise",
                help="Open the supervisor page beside this run. Sugar for a long "
                "--wait plus `lup-devtools resolve supervise`, which you "
                "can also run yourself against any run at any time.",
            ),
        ] = False,
        supervise_port: Annotated[
            int, typer.Option("--supervise-port", help="Port for the supervisor page")
        ] = SUPERVISOR_PORT,
        supervise_linger: Annotated[
            bool,
            typer.Option(
                "--supervise-linger",
                help="Leave the supervisor page running after the run exits",
            ),
        ] = False,
        host_retries: Annotated[
            int,
            typer.Option(
                "--host-retries",
                help="How many times to come back to a host that refused — an "
                "exhausted allowance, a rate limit, an unreachable upstream — "
                "before parking the run for a human. Zero parks on the first "
                "refusal. A fault only a person can clear, such as an empty "
                "balance, parks however this is set — after the single probe "
                "that rules out a sibling having rotated the credential.",
            ),
        ] = resolve.HOST_RETRIES,
        host_backoff: Annotated[
            float,
            typer.Option(
                "--host-backoff",
                help="Seconds to wait after the first refusal; each later wait "
                "doubles, up to half an hour between probes.",
            ),
        ] = resolve.HOST_BACKOFF_SECONDS,
        auth_probe_delay: Annotated[
            float,
            typer.Option(
                "--auth-probe-delay",
                help="Seconds to let a credential settle before the one fresh "
                "session that tells a rotated token from a dead one. Sessions "
                "share a credential file, so a sibling's refresh denies every "
                "other session in the words a dead credential uses; the probe "
                "is what separates them.",
            ),
        ] = resolve.AUTH_PROBE_SECONDS,
        max_parallel_workers: Annotated[
            int,
            typer.Option(
                "--max-parallel-workers",
                help="How many concerns may hold a session at once. Uncapped, a "
                "batch opens one per runnable concern, all in the same "
                "second, which spends the host's allowance "
                "at the width of the batch, races the credential file every "
                "session shares, and loses all of it to one interruption.",
            ),
        ] = 4,
        start_new: Annotated[
            bool,
            typer.Option(
                "--new",
                help="Start a fresh run even though this project has an "
                "unfinished one. Without it, an unfinished run is put to you "
                "rather than left behind: a run id defaults to the commit it "
                "started from, so the default moves at every commit and a bare "
                "rerun would otherwise strand every answer already collected.",
            ),
        ] = False,
        recheck_standing_per_join: Annotated[
            bool,
            typer.Option(
                "--recheck-standing-per-join",
                help="After each join, re-check every concern already in the tree "
                "that the join touched. Buys attribution — the join that broke a "
                "criterion is named — at a reviewer turn per overlapping pair, "
                "which grows quadratically. The final pass examines every concern "
                "against the finished tree either way.",
            ),
        ] = False,
        detach: Annotated[
            bool,
            typer.Option(
                "--detach",
                help=(
                    "Start the run and return, instead of holding this terminal "
                    "until it parks. The run directory is the only contract, so "
                    "the page and an agent reach it as peers afterwards"
                ),
            ),
        ] = False,
    ) -> None:
        """Drive the shared persisted resolver through one explicit native adapter."""
        if context.invoked_subcommand is not None:
            return
        admitted = resolve.AdmissionFlags(
            statements=admit or [], notes=admit_note or [], issues=admit_issue or []
        )
        if abort is None and resolve.queue_existing_admission(
            admitted, run_id, answer or [], start_new
        ):
            return
        # Resolved before a detached child is started, which resolves it again
        # and would meet a refused profile after this command reported a run.
        try:
            account = directory.account(profile)
        except (KeyError, DefaultHomeProfile) as error:
            raise typer.BadParameter(str(error), param_hint="--profile") from error
        if detach:
            if adapter is None:
                raise typer.BadParameter(
                    "--adapter is required to drive a resolver run"
                )
            # Ending a run reads recorded state and frees worktrees: it takes
            # no turn, so there is nothing for a child to outlive this command
            # with, and a relaunch carrying no `--abort` would start the run it
            # was asked to end.
            if abort is not None:
                raise typer.BadParameter(
                    "a run cannot be started detached and ended in one command"
                )
            resolve.detach_resolve(
                resolve.DetachedRun(
                    adapter=adapter,
                    run_id=run_id,
                    answers=answer or [],
                    admitted=admitted,
                    issues=issues,
                    wait=wait,
                    host_retries=host_retries,
                    host_backoff=host_backoff,
                    supervisor=resolve.SupervisorSpawn(
                        enabled=supervise,
                        port=supervise_port,
                        linger=supervise_linger,
                    ),
                    adopt_config=adopt_config,
                    auth_probe_delay=auth_probe_delay,
                    max_parallel_workers=max_parallel_workers,
                    recheck_standing_per_join=recheck_standing_per_join,
                    profile=profile,
                )
            )
            return
        # Ending a run reads its recorded state and frees its worktrees; no turn
        # is taken and no skill invocation is rendered, so the one thing an
        # adapter decides never comes up.
        if adapter is None and abort is None:
            raise typer.BadParameter("--adapter is required to drive a resolver run")
        spawn = resolve.SupervisorSpawn(
            enabled=supervise, port=supervise_port, linger=supervise_linger
        )
        resolve.run_resolve(
            # An abort needs no adapter, and asking for one reads as a bug. The
            # core still holds a composition, so ending a run without the flag
            # takes the first declared adapter and never asks it for anything.
            targets.resolve(adapter or next(iter(targets.builders)), project_root())[0],
            account,
            run_id,
            answer or [],
            abort,
            spawn.waiting(wait),
            spawn,
            resolve.admission_request(admitted),
            model,
            adopt_config,
            issues,
            host_retries,
            host_backoff,
            auth_probe_delay,
            max_parallel_workers,
            recheck_standing_per_join,
            start_new,
        )

    return app
