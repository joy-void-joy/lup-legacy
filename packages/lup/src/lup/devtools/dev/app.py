"""Typer command tree for dev operations: worktrees, branches, PRs, checks.

Everything here is workflow rather than domain, so what it needs to know
about the repository it runs in arrives as a declaration: the project facts
the scans read, the harness targets and generated files the gate checks, and
the declarations the policy and plugin commands explain. An application adds
whatever else its own `dev` tree offers to the app it gets back.
"""

import datetime as dt
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Annotated

import sh
import typer

import lup.devtools.dev.antipatterns as antipatterns_mod
import lup.devtools.dev.boundaries as boundaries_mod
import lup.devtools.dev.check as check
import lup.devtools.dev.comments as comments
import lup.devtools.dev.guidance as guidance
import lup.devtools.dev.issues as issues_mod
import lup.devtools.dev.library as library_mod
import lup.devtools.dev.history as history
import lup.devtools.dev.undo as undo
import lup.devtools.dev.model_config as model_config_mod
import lup.devtools.dev.model_catalog as model_catalog_mod
import lup.devtools.dev.settings_schema as settings_schema_mod
import lup.devtools.dev.environment as environment_mod
import lup.devtools.dev.pending as pending_mod
import lup.devtools.dev.plugin as plugin_mod
import lup.devtools.dev.policy_explain as policy_explain
import lup.devtools.dev.questions as questions_mod
import lup.devtools.dev.reach as reach
import lup.devtools.dev.scaffold as scaffold_mod
import lup.devtools.dev.scaffold_fit as scaffold_fit
import lup.devtools.dev.update as update_mod
import lup.devtools.dev.migrations as migrations
import lup.devtools.dev.preservation as preservation
import lup.devtools.dev.modules as modules
import lup.devtools.dev.seams as seams
import lup.devtools.dev.relocate as relocate_mod
import lup.devtools.dev.rules as rules
from lup.devtools.changelog import Changelog
from lup.devtools.dev.branches import get_integration_branch
from lup.execution.shell import git
from lup.harness.codescan.markers import NoteKind
from lup.harness.codescan.registry import all_rules
import lup.devtools.py.app as py
from lup.devtools.dev.commands import CommandSurface
from lup.devtools.dev.declarations import DevDeclarations
from lup.devtools.hooks.app import create_hooks_app
from lup.devtools.report.app import create_report_app
from lup.observability.usage.app import UsageEntry, create_usage_app
from lup.devtools.utils import decode_stderr, output_json, repository_slug
from lup.devtools.harness.composition import NativeTargets
from lup.devtools.harness.drift import RepositoryWriter
from lup.ledger.models import LedgerNode
from lup.ledger.store import LedgerLayout
from lup.policy.kernel.edit import SUPPRESSION_COLUMN_LIMIT
from lup.policy.vocabulary import default_vocabulary
from lup.workspace.paths import is_template_scaffold, project_root

DryRun = Annotated[
    bool,
    typer.Option("--dry-run", "-n", help="Show what would change without writing"),
]
KeepVendored = Annotated[
    bool,
    typer.Option("--keep-vendored", help=f"Leave {library_mod.VENDORED_ROOT}/ on disk"),
]
Force = Annotated[
    bool,
    typer.Option("--force", help="Un-vendor even from an unrenamed template"),
]
"""The flags every mode change takes, spelled once beside the commands taking them.

Module scope because a Typer annotation is a type expression and a name bound
inside the factory is not one — the three commands would otherwise each restate
the same three options.
"""


def create_dev_app(
    declared: Callable[[], DevDeclarations],
    native_targets: NativeTargets,
    repository_writers: list[RepositoryWriter],
    relocate_roots: list[Path],
    usage_entries: list[UsageEntry] | None = None,
    node_classes: list[type[LedgerNode]] | None = None,
    ledger: LedgerLayout = LedgerLayout(),
    command_surface: Callable[[], CommandSurface] | None = None,
) -> typer.Typer:
    """Wire the dev command tree over what one repository declares about itself."""
    app = typer.Typer(no_args_is_help=True)
    plugin_app = typer.Typer(no_args_is_help=True)
    migrate_app = typer.Typer(no_args_is_help=True)
    library_app = typer.Typer(no_args_is_help=True)
    scaffold_app = typer.Typer(no_args_is_help=True)
    env_app = typer.Typer(no_args_is_help=True)
    tracker_app = typer.Typer(no_args_is_help=True)
    app.add_typer(
        env_app,
        name="env",
        help="This project's own environment, and who else is installed in it",
    )
    app.add_typer(plugin_app, name="plugin", help="Local plugin marketplace wiring")
    app.add_typer(
        tracker_app,
        name="tracker",
        help="Answer an issue here or on a declared tracker (comment, close, reopen)",
    )
    app.add_typer(
        migrate_app,
        name="migrate",
        help="What a range of this repository asks of a project built on it",
    )
    app.add_typer(library_app, name="library", help="How this project obtains lup")
    app.add_typer(
        scaffold_app,
        name="scaffold",
        help="Upstream's copied half, as a branch of this repository",
    )
    app.add_typer(
        model_config_mod.create_model_config_app(),
        name="model-config",
        help="Pydantic configuration census and equivalence",
    )
    app.add_typer(
        questions_mod.create_questions_app(Path.cwd()),
        name="questions",
        help="The parked asks a reviewer answers, and what each is waiting on",
    )
    # Three trees that were top-level and are core's either way, so being
    # top-level bought nothing and cost a reader three names to learn instead
    # of one. Each is a way of reading this repository rather than a workflow
    # over it — what the policy decides, what a name resolves to, what is left
    # to implement — which is the same subject `dev` already is.
    app.add_typer(
        create_hooks_app(lambda: declared().hooks),
        name="hooks",
        help="Query the permission policy",
    )
    app.add_typer(py.SUBAPP.app, name="py", help=py.SUBAPP.spec.help)
    app.add_typer(
        create_report_app(native_targets, repository_writers),
        name="report",
        help="Everything left to implement, in one place",
    )
    # Not with the trace tree, though "what a session spent" reads like the
    # third of a set with "what it did" and "where its records went". Those two
    # read what a session wrote into this repository; this reads the backend's
    # own metered account, which answers whether or not a session ever ran
    # here. A project that stopped reading traces would still want its spend.
    app.add_typer(
        create_usage_app(usage_entries or []),
        name="usage",
        help="What this project has spent, per backend account",
    )

    # -- tracker commands --

    def tracker_routes() -> issues_mod.TrackerRoutes:
        """Where a forge operation may land, read when the command runs.

        `origin` answers for this checkout and the declaration answers for
        everywhere else, both resolved here rather than held on the app: a
        CLI is imported long before anybody knows which repository it will be
        pointed at, and a worktree created mid-session is a different answer
        to the first question.
        """
        return issues_mod.TrackerRoutes(
            own=repository_slug(), declared=list(declared().project.trackers)
        )

    def answer(
        verb: issues_mod.TrackerVerb, number: int, note: str, repository: str
    ) -> None:
        """Run one compensable verb, saying where it landed or why it did not.

        The three verbs differ only in the word, so they share this rather
        than restating the resolution, the failure handling and the refusal
        three times — which is where a fourth verb would have quietly skipped
        one of them.
        """
        routes = tracker_routes()
        try:
            target = routes.chosen(
                issues_mod.issue_arguments(verb, number, note), named=repository
            )
        except RuntimeError as refused:
            typer.echo(str(refused), err=True)
            raise typer.Exit(1) from refused
        if not target:
            typer.echo(
                "No repository to reach: origin names none and no tracker was"
                " given. `--repo <owner/name>` names one this project declares.",
                err=True,
            )
            raise typer.Exit(1)
        try:
            typer.echo(issues_mod.act_on_issue(verb, number, target, note))
        except sh.ErrorReturnCode as failure:
            typer.echo(decode_stderr(failure), err=True)
            raise typer.Exit(1) from failure

    @tracker_app.command("comment")
    def tracker_comment_cmd(
        number: Annotated[int, typer.Argument(min=1, help="Issue number")],
        body: Annotated[str, typer.Option("--body", help="What to say")],
        repository: Annotated[
            str,
            typer.Option("--repo", help="Which repository, if not this checkout's"),
        ] = "",
    ) -> None:
        """Say something on an issue, here or on a declared tracker.

        Compensable: a comment that was wrong is answered by the next one.
        What this will not do is anything a follow-up does not restore — the
        surface is these three verbs, and reaching further means `gh`, which
        the permission policy asks about.
        """
        answer("comment", number, body, repository)

    @tracker_app.command("close")
    def tracker_close_cmd(
        number: Annotated[int, typer.Argument(min=1, help="Issue number")],
        comment: Annotated[
            str,
            typer.Option("--comment", help="Why it is being closed"),
        ] = "",
        repository: Annotated[
            str,
            typer.Option("--repo", help="Which repository, if not this checkout's"),
        ] = "",
    ) -> None:
        """Close an issue, here or on a declared tracker.

        Reopening restores it, which is what puts closing in reach at all. A
        comment is optional and worth writing: whoever was watching sees the
        state change either way, and only the note says why.
        """
        answer("close", number, comment, repository)

    @tracker_app.command("reopen")
    def tracker_reopen_cmd(
        number: Annotated[int, typer.Argument(min=1, help="Issue number")],
        comment: Annotated[
            str,
            typer.Option("--comment", help="Why it is being reopened"),
        ] = "",
        repository: Annotated[
            str,
            typer.Option("--repo", help="Which repository, if not this checkout's"),
        ] = "",
    ) -> None:
        """Reopen an issue, here or on a declared tracker."""
        answer("reopen", number, comment, repository)

    @tracker_app.command("list")
    def tracker_list_cmd() -> None:
        """Which repositories this project may reach, and what each is for.

        The same list a refusal reads out, answerable before meeting one:
        where a report can go is a question with an answer, and finding it
        by being told no is a worse way to learn it.
        """
        routes = tracker_routes()
        typer.echo(f"this checkout: {routes.own or 'origin names no repository'}")
        for entry in routes.declared:
            claimed = f"  components: {', '.join(entry.components)}"
            typer.echo(f"declared: {entry.repository} — {entry.what}")
            if entry.components:
                typer.echo(claimed)
        if not routes.declared:
            typer.echo("declared: none — every other repository goes through gh")

    # -- environment commands --

    @env_app.command("status")
    def env_status_cmd() -> None:
        """Where this project's environment is, and who is installed in it."""
        environment_mod.environment_status()

    @env_app.command("sync")
    def env_sync_cmd(
        take_over: Annotated[
            bool,
            typer.Option(
                "--take-over",
                help="Sync even where another project is installed, uninstalling it",
            ),
        ] = False,
    ) -> None:
        """Install this project's dependencies into its own environment.

        `uv sync` with the one question `uv` cannot ask asked first: an
        absolute ``UV_PROJECT_ENVIRONMENT`` names a single directory for
        every project on the machine, and a sync into a shared one
        uninstalls whoever was there.
        """
        environment_mod.sync_environment(take_over)

    @app.command("pending")
    def pending_cmd(
        as_json: Annotated[
            bool,
            typer.Option("--json", help="Output as JSON"),
        ] = False,
    ) -> None:
        """Report the real pending changes, excluding sandbox-masked device paths."""
        pending_mod.report(as_json)

    # -- check command --

    @app.command("check")
    def check_cmd(
        fix: Annotated[
            bool,
            typer.Option(
                "--fix",
                help="Auto-fix formatting and lint issues; with --antipatterns, "
                "delete the dead directives instead of reporting them",
            ),
        ] = False,
        no_test: Annotated[
            bool,
            typer.Option(
                "--no-test", help="Skip the test suites, and hold no gate slot"
            ),
        ] = False,
        antipatterns: Annotated[
            bool,
            typer.Option(
                "--antipatterns",
                help="Audit repository files for missing/spurious `# lup: ignore` "
                "markers only — the same lup.harness.codescan.antipatterns rules the edit "
                "hook enforces",
            ),
        ] = False,
        boundaries: Annotated[
            bool,
            typer.Option(
                "--boundaries",
                help="Scan for native adapter imports outside composition roots "
                "only — the seam rules the full check's sweep also runs",
            ),
        ] = False,
        placement: Annotated[
            bool,
            typer.Option(
                "--placement",
                help="List library data tables no adopter can replace only — the "
                "library-default rule the full check's sweep also runs",
            ),
        ] = False,
        stats: Annotated[
            bool,
            typer.Option(
                "--stats",
                help="With --antipatterns: tally findings by rule and kind instead "
                "of listing each — the sweep triage view",
            ),
        ] = False,
        profiled: Annotated[
            bool,
            typer.Option(
                "--profile",
                help="With --antipatterns: report where the sweep spent its time "
                "instead of what it found — parsing, walking, or resolving",
            ),
        ] = False,
        as_json: Annotated[
            bool,
            typer.Option(
                "--json", help="Output findings as JSON (with --antipatterns)"
            ),
        ] = False,
        since: Annotated[
            str | None,
            typer.Option(
                "--since",
                help="Scope the note and anti-pattern gates to paths changed since "
                "this ref, for a tree that holds work it is not answerable for",
            ),
        ] = None,
        path: Annotated[
            list[str] | None,
            typer.Option(
                "--path",
                help="With --antipatterns: audit only files under these paths, for "
                "the fix-one-file loop. Repeatable",
            ),
        ] = None,
        changed: Annotated[
            bool,
            typer.Option(
                "--changed",
                help="Run ruff and pyright over the Python files changed since "
                "the merge base with --since (default: this branch's recorded "
                "base), and the declared-migrations row from that base, naming "
                "what else changed and every gate left unrun — the loop while a "
                "change is moving, not the bar a commit passes",
            ),
        ] = False,
        base: Annotated[
            str | None,
            typer.Option(
                "--base",
                help="Judge removed capabilities from the merge base with this "
                "ref instead of the one detection reaches, for a checkout whose "
                "base it cannot work out",
            ),
        ] = None,
    ) -> None:
        """Run ruff format, ruff check, pyright, and pytest. Read-only by default."""
        declarations = declared()
        if changed:
            from lup.devtools.dev.branches import get_integration_branch

            check.run_changed(
                declarations.project,
                check.change_base(since, get_integration_branch()),
                declarations.test_roots,
                declarations.spread,
                fix=fix,
            )
            return
        if antipatterns:
            match (profiled, stats):
                case (True, _):
                    antipatterns_mod.profile(declarations.project, path)
                case (False, True):
                    antipatterns_mod.summarize(declarations.project, as_json, path)
                case _:
                    antipatterns_mod.report(
                        declarations.project, as_json, path, fix=fix
                    )
            return
        if boundaries:
            boundaries_mod.report(declarations.project, as_json)
            return
        if placement:
            boundaries_mod.report_placement(as_json)
            return
        check.run_checks(
            fix=fix,
            no_test=no_test,
            project=declarations.project,
            test_roots=declarations.test_roots,
            compositions=native_targets.resolve(native_targets.every, project_root()),
            repository_writers=repository_writers,
            git_guards=declarations.git_guards,
            hooks_declaration=declarations.hooks,
            command_surface=command_surface,
            scope=check.changed_paths(since) if since is not None else None,
            node_classes=node_classes or [],
            ledger=ledger,
            scaffold_source=declarations.scaffold,
            spread=declarations.spread,
            migration_base=check.named_gate_base(base) if base is not None else None,
        )

    # -- test command --

    @app.command("test")
    def test_cmd(
        paths: Annotated[
            list[str] | None,
            typer.Argument(
                help="Test files or directories to run. Each is dispatched to the "
                "declared test root that installs it; with none, every root runs "
                "its whole suite"
            ),
        ] = None,
    ) -> None:
        """Run named tests in the suite that installs each, one run per suite.

        Holds one of the clone's gate slots as `dev check` does, and spreads
        each suite over its share of the machine; with every slot held, it
        waits and says so.
        """
        declarations = declared()
        check.run_selected(
            test_roots=declarations.test_roots,
            selections=paths or [],
            excluded_roots=check.non_code_roots(declarations.project),
        )

    # -- comments command --

    @app.command("comments")
    def comments_cmd(
        targets: Annotated[
            list[str] | None,
            typer.Argument(help="file:line markers to remove with --clear"),
        ] = None,
        as_json: Annotated[
            bool,
            typer.Option("--json", help="Output as JSON"),
        ] = False,
        kind: Annotated[
            NoteKind | None,
            typer.Option("--kind", help="Show only this note flavor"),
        ] = None,
        commit: Annotated[
            bool,
            typer.Option(
                "--commit", help="Commit comment-bearing files as a prompt snapshot"
            ),
        ] = False,
        clear: Annotated[
            bool,
            typer.Option(
                "--clear",
                help="Strip the file:line markers given (only on a resolve/* branch)",
            ),
        ] = False,
        wake: Annotated[
            bool,
            typer.Option(
                "--wake",
                help="With --clear: also strip the defer notes named, waking them",
            ),
        ] = False,
        retire: Annotated[
            bool,
            typer.Option(
                "--retire",
                help="Delete the solved claims named as file:line (verify pass only)",
            ),
        ] = False,
        restore: Annotated[
            bool,
            typer.Option(
                "--restore",
                help="Reopen the solved claims named as file:line as open feedback",
            ),
        ] = False,
        narrow: Annotated[
            str | None,
            typer.Option(
                "--narrow",
                help="With --restore and one target: the still-outstanding text",
            ),
        ] = None,
        withdraw: Annotated[
            bool,
            typer.Option(
                "--withdraw",
                help="Retract the notes named as file:line; needs --reason",
            ),
        ] = False,
        reason: Annotated[
            str | None,
            typer.Option(
                "--reason",
                help="With --withdraw: why the note should not have been written",
            ),
        ] = None,
    ) -> None:
        """List unresolved `# lup:` feedback comments, or act on specific ones.

        With --clear, removes each `file:line` marker named as an argument; used
        at fork time to strip a concern's own notes from an editor's worktree.
        Deferred notes are skipped unless --wake is passed as well.

        With --retire or --restore, applies the verify-solved pass's verdicts to
        `# lup: solved:` claims — and only to claims: any other note is refused.

        With --withdraw and --reason, retracts notes that should not have been
        written at all. Conversion to `# lup: solved:` is for a note that was
        answered; a note that was mistaken has no answer to claim, and the
        reason is committed alongside the removal in its place.
        """
        if sum([clear, retire, restore, withdraw]) > 1:
            typer.echo(
                "--clear, --retire, --restore, and --withdraw are exclusive", err=True
            )
            raise typer.Exit(2)
        if narrow is not None and (not restore or len(targets or []) != 1):
            typer.echo("--narrow needs --restore and exactly one target", err=True)
            raise typer.Exit(2)
        if (reason is not None) != withdraw:
            typer.echo("--withdraw and --reason require each other", err=True)
            raise typer.Exit(2)
        if withdraw and reason is not None:
            comments.withdraw_notes(targets or [], reason)
            return
        if retire or restore:
            comments.revise_claims(targets or [], retire=retire, narrow=narrow)
            return
        if clear:
            comments.clear_markers(targets or [], wake=wake)
            return
        comments.report(as_json, commit, kind)

    @app.command("todos")
    def todos_cmd(
        as_json: Annotated[
            bool,
            typer.Option("--json", help="Output as JSON"),
        ] = False,
    ) -> None:
        """List `# lup: template:` markers — a scaffold's open decisions.

        Every place a scaffolded project needs a domain decision carries a
        `# lup: template:` note. Initialization runs this to gather them all
        and walk them one by one, so no customization point depends on someone
        remembering to mention it. An alias for `dev comments --kind template`:
        same scan, same file/line/text/context shape, narrowed to that flavor.
        """
        comments.report(as_json, commit=False, kind=NoteKind.template)

    @app.command("seams")
    def seams_cmd(
        own: Annotated[
            list[str] | None,
            typer.Option("--own", help="Hand a file to its human owner (repeatable)"),
        ] = None,
        disown: Annotated[
            list[str] | None,
            typer.Option("--disown", help="Let the agent write this file again"),
        ] = None,
        retire: Annotated[
            list[str] | None,
            typer.Option("--retire", help="Stop holding this project to a rule id"),
        ] = None,
        keep: Annotated[
            list[str] | None,
            typer.Option("--keep", help="Hold this project to a rule id again"),
        ] = None,
        retire_all: Annotated[
            bool,
            typer.Option("--retire-all", help="Retire every rule the library ships"),
        ] = False,
    ) -> None:
        """Show what this project settled about itself, or settle one of them.

        With no options this prints each seam, its current value and where it
        is written — which is what makes putting them to a person possible at
        all, during initialization or afterwards. A default nobody was shown
        is not a decision, and neither is one whose declaration somebody would
        have to go find.

        The options write that declaration rather than asking anyone to edit
        it, and print what to regenerate. Nothing regenerates here: what
        compiles from a declaration is the project's own set of trees, and a
        command that guessed at them would be answering for a layout it does
        not own.
        """
        project = declared().project
        catalog = project.catalog
        answers = seams.Answers(
            own=own or [],
            disown=disown or [],
            retire=retire or [],
            keep=keep or [],
            retire_all=retire_all,
        )
        if not answers.given():
            for line in seams.survey(catalog, project.seams):
                typer.echo(line)
            return
        if catalog is None:
            raise typer.BadParameter(
                "this project declares no catalog path, so there is nothing to "
                "write a seam into; name one on its `DevProject`"
            )
        # Every id the library ships, not every id this project still keeps:
        # retiring all of them has to name the ones already retired too, or
        # the answer would silently exclude what a previous answer dropped.
        shipped = [rule.id for rule in all_rules()]
        try:
            settled = answers.settled(catalog, shipped, project.seams)
        except ValueError as refused:
            # A seam that cannot be written into — never written down, or
            # naming a module that is not there — says why and where.
            raise typer.BadParameter(str(refused)) from refused
        for line in settled:
            typer.echo(line)

    @app.command("refutations")
    def refutations_cmd(
        path: Annotated[
            str,
            typer.Option("--path", help="The file the content on stdin belongs to"),
        ],
    ) -> None:
        """Resolve one file's proposed content and report what it refutes.

        The edit gate's route to a checker. It judges a change before anything
        is written, so the text is read from stdin rather than from *path* —
        which names where the content belongs, and is what imports, the
        module's own name, and every declaration reached through either
        resolve against.

        Always JSON: the only caller is a hook, and `resolved` tells it
        whether a checker answered at all. A gate that gets no answer asks
        instead of refusing, so "nothing was refuted" and "nothing could be
        resolved" have to arrive as different replies.
        """
        antipatterns_mod.report_refutations(
            declared().project, Path(path), sys.stdin.read()
        )

    @app.command("directives")
    def directives_cmd(
        as_json: Annotated[
            bool,
            typer.Option("--json", help="Output as JSON"),
        ] = False,
        limit: Annotated[
            int,
            typer.Option("--limit", help="Column budget the inline placement gets"),
        ] = SUPPRESSION_COLUMN_LIMIT,
        fix: Annotated[
            bool,
            typer.Option(
                "--fix", help="Move each directive to its canonical placement"
            ),
        ] = False,
        retire: Annotated[
            str | None,
            typer.Option(
                "--retire", help="Take one retired rule out of every directive"
            ),
        ] = None,
    ) -> None:
        """Measure every `# lup: ignore` against the canonical inline placement.

        Placement is uniform — a directive sits on the line it guards, or
        stands alone directly above it — and the inline form is the canonical
        one. This reports which sites the column budget lets stay inline and
        which only fit above, so the fallback is sized against the tree rather
        than assumed.
        """
        if retire is not None:
            swept = antipatterns_mod.retire_directives(declared().project, retire)
            lost = sum(len(item.removed) for item in swept)
            typer.echo(f"{len(swept)} file(s) rewritten, {lost} line(s) removed")
            for item in swept:
                lines = ", ".join(str(number) for number in item.removed)
                typer.echo(f"  {item.rel}  {lines or 'rule id dropped'}")
            return
        if fix:
            moved = antipatterns_mod.place_directives(declared().project, limit)
            typer.echo(f"{len(moved)} file(s) replaced")
            for rel in moved:
                typer.echo(f"  {rel}")
            return
        antipatterns_mod.report_directives(declared().project, as_json, limit)

    @app.command("report-friction")
    def report_friction_cmd(
        summary: Annotated[str, typer.Option("--summary")],
        component: Annotated[str, typer.Option("--component")],
        command: Annotated[str, typer.Option("--command")],
        error: Annotated[str, typer.Option("--error")],
        state: Annotated[str, typer.Option("--state")],
        recovery_cost: Annotated[str, typer.Option("--recovery-cost")],
        issue: Annotated[int | None, typer.Option("--issue", min=1)] = None,
        repository: Annotated[
            str,
            typer.Option("--repo", help="Which tracker, if not the one routing picks"),
        ] = "",
    ) -> None:
        """File or correct workflow friction, on the tracker that owns the fix.

        Routed by the component the report names rather than by where the
        session was standing. A project consuming a library as a dependency
        meets most of its friction in machinery it cannot edit, and a report
        filed here about code that is not here is worse than misplaced: the
        resolver takes every open issue in this repository as evidence, so
        the next run plans a repair it cannot make. A component no declared
        tracker claims stays here, which is every defect this tree owns.
        """
        routes = tracker_routes()
        report = issues_mod.FrictionReport(
            summary=summary,
            component=component,
            command=command,
            error=error,
            state=state,
            recovery_cost=recovery_cost,
        )
        try:
            target = routes.chosen(
                ["issue", "create", "--title", summary],
                named=repository,
                component=component,
            )
            url = report.file(repository=target, issue=issue)
        except RuntimeError as refused:
            typer.echo(str(refused), err=True)
            raise typer.Exit(1) from refused
        except sh.ErrorReturnCode as failure:
            spoken = decode_stderr(failure)
            advice = issues_mod.disabled_issues_advice(spoken, routes)
            typer.echo(spoken if not advice else f"{spoken}\n{advice}", err=True)
            raise typer.Exit(1) from failure
        typer.echo(url)

    @app.command("undo")
    def undo_cmd(
        repair: Annotated[
            bool,
            typer.Option(
                "--repair", help="Quarantine empty undo refs that break Git fetch"
            ),
        ] = False,
        take: Annotated[
            str,
            typer.Option("--take", help="Snapshot the tree now, naming why"),
        ] = "",
        expire_days: Annotated[
            int | None,
            typer.Option("--expire", help="Drop snapshots older than this many days"),
        ] = None,
        keep_most: Annotated[
            int | None,
            typer.Option("--keep", help="Keep at most this many snapshots"),
        ] = None,
    ) -> None:
        """List, take, expire, or repair recoverable snapshots of this tree.

        Restoring is deliberately not offered here. Putting a snapshot back
        overwrites present work with past work -- the same class of act as
        the destruction it undoes -- so each entry prints the command that
        would do it and leaves running it to somebody who can see what is
        currently there.
        """
        root = project_root()
        if repair:
            try:
                for path in undo.repair_refs(root):
                    typer.echo(f"quarantined broken undo ref: {path}")
            except OSError as error:
                typer.echo(
                    f"Undo repair stopped: {error}. Check active ref locks and directory permissions before retrying.",
                    err=True,
                )
                raise typer.Exit(1) from error
            return
        for damaged in undo.damaged_refs(root):
            typer.echo(
                f"Broken undo ref {damaged.ref}; run `dev undo --repair` before fetching.",
                err=True,
            )
        if take:
            taken = undo.snapshot(root, take)
            typer.echo(
                f"{taken.ref}  {taken.commit[:12]}"
                if taken is not None
                else "No snapshot taken — this checkout could not be written to."
            )
            return
        if expire_days is not None or keep_most is not None:
            days = undo.DEFAULT_RETENTION_DAYS if expire_days is None else expire_days
            most = undo.DEFAULT_RETENTION_COUNT if keep_most is None else keep_most
            for gone in undo.expire(root, days, keep_most=most):
                typer.echo(f"expired {gone.ref}")
            return
        found = undo.points(root)
        if not found:
            typer.echo("No snapshots. `--take <reason>` writes one.")
            return
        for item in found:
            typer.echo(f"{item.taken_at:%Y-%m-%d %H:%M}  {item.reason}")
            typer.echo(f"    {item.restore_command()}")

    @app.command("history")
    def history_cmd(
        text: Annotated[str, typer.Argument(help="The symbol or literal to trace")],
        regex: Annotated[
            bool,
            typer.Option("--regex", help="Read the text as a regular expression"),
        ] = False,
        path: Annotated[
            list[Path] | None,
            typer.Option("--path", help="Limit the search to these paths"),
        ] = None,
        as_json: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
    ) -> None:
        """Trace a symbol through every branch, past this tree's own snapshots.

        `git log --all -S <symbol>` cannot answer this in a guarded checkout.
        The permission dispatcher snapshots the tree before every command
        under `refs/lup/undo`, each snapshot a parentless commit whose whole
        tree reads as an addition -- so the pickaxe matches every symbol the
        checkout holds, once per snapshot, and the first match is the one
        taken in front of the search. Those refs are left out of the
        traversal here and nothing they recorded is disturbed.

        Examples::

            $ uv run lup-devtools dev history undo_retention_count
            $ uv run lup-devtools dev history 'def parse' --path packages/lup
            $ uv run lup-devtools dev history 'Hook(Set|Rule)' --regex
        """
        found = history.commits_matching(project_root(), text, regex, path)
        if as_json:
            output_json([hit.model_dump(mode="json") for hit in found])
            return
        for hit in found:
            typer.echo(hit.line())
        typer.echo(
            f"{len(found)} commit(s), snapshots under {history.SNAPSHOT_REFS} aside"
        )

    @app.command("issues")
    def issues_cmd(
        excluded: Annotated[
            str,
            typer.Option("--excluded", help="Label that withholds an issue"),
        ] = issues_mod.EXCLUDED_LABEL,
        repository: Annotated[
            str,
            typer.Option("--repo", help="Which repository, if not this checkout's"),
        ] = "",
    ) -> None:
        """List the open issues a resolver run would take as evidence.

        Answerable without starting a run, which is the whole point: a run
        leases a worktree per concern, so "what would this plan from?" should
        not cost one.

        A declared tracker can be read the same way. Reading somebody else's
        issues is a read wherever it points, and asking what is open upstream
        is how a session finds out that the defect in front of it is already
        filed.
        """
        routes = tracker_routes()
        try:
            slug = routes.chosen(["issue", "list", "--state", "open"], named=repository)
        except RuntimeError as refused:
            typer.echo(str(refused), err=True)
            raise typer.Exit(1) from refused
        answered = issues_mod.read_open_issues(excluded, repository=slug)
        if not answered.reached:
            typer.echo(
                f"could not read the issues of {slug or 'this repository'}:"
                f" {answered.why}",
                err=True,
            )
            raise typer.Exit(1)
        typer.echo(
            f"{len(answered.issues)} open issue(s) in {slug or 'this repository'}"
        )
        for issue in answered.issues:
            typer.echo(f"  {issue.reference()}  {issue.title}")

    @app.command("rules")
    def rules_cmd(
        check_only: Annotated[
            bool,
            typer.Option("--check", help="Fail when docs/rules.md is stale"),
        ] = False,
    ) -> None:
        """Generate the Lup rule and typed-suppression reference.

        Rendered against the selection this repository holds itself to, which
        is the same one the edit hook and the sweep read. Rendering the whole
        library table instead writes a reference naming rules the gate here
        does not enforce — and a project that retired one then has two
        documents disagreeing about what it is held to, the generated file
        saying it still applies.
        """
        try:
            destination = rules.write_rule_reference(
                check=check_only, selection=declared().hooks.rules
            )
        except RuntimeError as error:
            typer.echo(str(error), err=True)
            raise typer.Exit(1) from error
        verb = "verified" if check_only else "written"
        typer.echo(f"Lup rule reference {verb}: {destination}")

    @app.command("models")
    def models_cmd(
        check_only: Annotated[
            bool,
            typer.Option("--check", help="Fail where a CLI's lineup moved"),
        ] = False,
    ) -> None:
        """Read each runtime's model lineup from its CLI, and compile its types.

        Rewrites the snapshot each provider package commits and the module
        compiled from it. The gate never runs this — it needs both CLIs
        installed — and recompiles the modules from the snapshots instead.
        """
        drifts = model_catalog_mod.refresh_catalogs(
            model_catalog_mod.library_catalogs(), project_root(), check=check_only
        )
        for line in (line for drift in drifts for line in drift.lines()):
            typer.echo(line)
        if check_only and not all(drift.settled() for drift in drifts):
            typer.echo(f"Run `{model_catalog_mod.MODELS_COMMAND}`.", err=True)
            raise typer.Exit(1)

    @app.command("settings")
    def settings_cmd(
        check_only: Annotated[
            bool,
            typer.Option("--check", help="Fail where the CLI's settings keys moved"),
        ] = False,
    ) -> None:
        """Read the settings keys Claude Code takes from its CLI, and compile their types.

        Rewrites the snapshot the Claude provider commits and the key types
        compiled from it, which the settings flow decisions are typed
        against. The gate never runs this — it needs the CLI installed — and
        instead refuses a compiled key no decision names.
        """
        moved = settings_schema_mod.refresh_settings_schema(
            settings_schema_mod.SettingsSchemaSource(), project_root(), check=check_only
        )
        typer.echo(
            "claude: settings keys "
            + ("differ from the committed snapshot" if moved else "match the snapshot")
        )
        for line in moved:
            typer.echo(line)
        if check_only and moved:
            typer.echo(f"Run `{settings_schema_mod.SETTINGS_COMMAND}`.", err=True)
            raise typer.Exit(1)

    @app.command("modules")
    def modules_cmd(
        verbose: Annotated[
            bool,
            typer.Option("--verbose", "-v", help="What each module is and contributes"),
        ] = False,
    ) -> None:
        """Report which modules this project takes, and what each one's prose costs.

        The roster is the one selection with no surface of its own: a retired
        sub-app is missing from `--help` and a retired rule from the rule
        reference, but a module is five surfaces at once, so what a project
        settled is otherwise readable only out of its catalog against defaults
        held in the reader's head.
        """
        project = declared().project
        modules.report(project.coverage.modules, project.modules, verbose)

    @app.command("reach")
    def reach_cmd(
        since: Annotated[
            str,
            typer.Option("--since", help="How far back to read, in git's own grammar"),
        ] = "12 months ago",
        limit: Annotated[
            int,
            typer.Option("--limit", help="How many copied modules the ranking names"),
        ] = 10,
    ) -> None:
        """Report how this repository's work reaches a project built on it.

        The question a scaffold cannot answer about itself by reading its own
        tree: a commit's cost to an adopter is decided by which trees it
        touched, and nothing records that at the time. The split row is the
        one to watch — a bump lands its library half and leaves the call site,
        so it arrives as a breakage rather than as work anybody chose to read.
        """
        spread = declared().spread
        if spread is None:
            typer.echo(
                "no scaffold declared: nothing here is copied into another "
                "repository, so every commit reaches an adopter by import or "
                "not at all"
            )
            return
        reach.report(since, spread, project_root(), limit)

    @app.command("guidance")
    def guidance_cmd(
        by_size: Annotated[
            bool,
            typer.Option("--by-size", help="Heaviest section first, not reading order"),
        ] = False,
    ) -> None:
        """Report what each section of the always-loaded guidance costs."""
        guidance.report(
            compositions=native_targets.resolve(native_targets.every, project_root()),
            scaffold=is_template_scaffold(project_root()),
            by_size=by_size,
        )

    @app.command("relocate")
    def relocate_cmd(
        moves: Annotated[
            list[str],
            typer.Argument(
                help="Module relocations, each spelled old.module=new.module"
            ),
        ],
        root: Annotated[
            list[Path] | None,
            typer.Option("--root", help="Source root to rewrite (repeatable)"),
        ] = None,
    ) -> None:
        """Move a module and repoint every import of it."""

        def parsed(move: str) -> relocate_mod.Relocation:
            # This CLI's own flag grammar, not structured data with a parser.
            old, separator, new = move.partition("=")  # lup: ignore[string-split]
            sides = [relocate_mod.name_parts(old), relocate_mod.name_parts(new)]
            if not separator or any(side is None for side in sides):
                typer.echo(f"expected old.module=new.module; got {move!r}", err=True)
                raise typer.Exit(2)
            return relocate_mod.Relocation(old=sides[0] or [], new=sides[1] or [])

        declared = [parsed(move) for move in moves]
        roots = [path for path in root or relocate_roots if path.exists()]
        # Refused whole, before any file moves or any import is rewritten: a
        # module that cannot land must not have its importers aimed at the
        # one already standing where it would.
        taken = relocate_mod.occupied(roots, declared)
        for plan in taken:
            typer.echo(
                f"{plan.new} already exists, so {plan.old} cannot move there; "
                "nothing was moved or repointed. Merge the two modules by hand, "
                "or relocate to a name no module holds.",
                err=True,
            )
        if taken:
            raise typer.Exit(2)
        # The module's own file first, so every import repointed below is
        # pointed at something already there. Leaving this to the caller is
        # what made the command's name a lie: it reported success over a tree
        # where nothing resolved, and the type check named the wreckage
        # somewhere else entirely.
        for move in declared:
            carried = relocate_mod.carry_module(roots, move)
            if carried is not None:
                typer.echo(f"moved {carried.old} -> {carried.new}")
        for edit in relocate_mod.relocate(roots, declared):
            typer.echo(f"{edit.path}: {edit.imports} import(s)")
        for mention in relocate_mod.surviving_mentions(roots, declared):
            typer.echo(f"still mentions a moved module: {mention}", err=True)

    # -- library commands --

    @library_app.command("status")
    def library_status_cmd() -> None:
        """Report where the lup library is resolved from."""
        library_mod.library_status()

    @library_app.command("release")
    def library_release_cmd() -> None:
        """Ask the package index whether a release exists, and which mode that settles."""
        library_mod.library_release()

    @library_app.command("use")
    def library_use_cmd(
        mode: Annotated[
            library_mod.LibraryMode,
            typer.Argument(help="published, local, or linked"),
        ],
        version: Annotated[
            str | None,
            typer.Option(
                "--version", help="Lower version bound for the published release"
            ),
        ] = None,
        keep_vendored: KeepVendored = False,
        force: Force = False,
        dry_run: DryRun = False,
    ) -> None:
        """Resolve lup from the package index, or from the vendored copy."""
        library_mod.use_library(mode, version, keep_vendored, force, dry_run)

    @library_app.command("git")
    def library_git_cmd(
        url: Annotated[
            str | None,
            typer.Option(
                "--url", help="Repository URL, overriding the pin or sync registration"
            ),
        ] = None,
        branch: Annotated[
            str | None, typer.Option("--branch", help="Branch to resolve lup at")
        ] = None,
        tag: Annotated[
            str | None, typer.Option("--tag", help="Tag to resolve lup at")
        ] = None,
        rev: Annotated[
            str | None, typer.Option("--rev", help="Commit to pin lup at")
        ] = None,
        keep_vendored: KeepVendored = False,
        force: Force = False,
        dry_run: DryRun = False,
    ) -> None:
        """Resolve lup from its repository, for use before a release is published."""
        scaffold = declared().scaffold
        project = scaffold.project if scaffold is not None else library_mod.REGISTRATION
        source_url = library_mod.repository_url(project_root(), url, project)
        library_mod.git_library(
            library_mod.git_source(source_url, branch=branch, tag=tag, rev=rev),
            keep_vendored,
            force,
            dry_run,
        )

    # -- the copied half, and the update that moves every carrier --

    def adopted_source() -> scaffold_mod.ScaffoldSource:
        """This project's declared scaffold, refused where there is none.

        Two ways there is none, and they are different answers. A project that
        wrote its own modules declares no source, and nothing here applies to
        it. The scaffold itself declares one — every copy inherits the
        declaration — and is still the origin of all of them, which the
        template flag is what says.
        """
        source = declared().scaffold
        if source is None:
            raise typer.BadParameter(
                "this project declares no scaffold source, so it has no copied "
                "half to merge: nothing upstream stamped it out"
            )
        if is_template_scaffold(project_root()):
            raise typer.BadParameter(
                "this checkout is the scaffold itself rather than a project "
                "built on it, so there is nothing upstream of it to merge. "
                "`dev init rename-package <project>` is what adopts it."
            )
        return source

    @scaffold_app.command("compile")
    def scaffold_compile_cmd(
        commit: Annotated[
            str, typer.Argument(help="The upstream commit to compile the scaffold at")
        ],
        out: Annotated[
            Path, typer.Option("--out", help="Where to write the compiled tree")
        ],
        decline: Annotated[
            list[str] | None,
            typer.Option(
                "--decline",
                help="An upstream path to leave out, beside the declared ones",
            ),
        ] = None,
    ) -> None:
        """Materialize upstream's copied half at one commit, under this name.

        The pure function the update rests on, exposed so it can be looked at:
        what an update would merge, written to a directory rather than to a
        branch. `--decline` asks what a wider selection would produce without
        declaring it first, which is how a project decides what to declare —
        including whether it is coherent, since a selection taking one half of
        a declaration and declining the other is refused here.
        """
        declared_source = adopted_source()
        source = declared_source.model_copy(
            update={"declined": [*declared_source.declined, *(decline or [])]}
        )
        built = scaffold_mod.compiled(
            update_mod.upstream_checkout(source.project, typer.echo),
            commit,
            source,
            declared().project.package,
            out,
        )
        typer.echo(f"{out}: {len(built.files)} file(s) at {built.commit}")

    @scaffold_app.command("fit")
    def scaffold_fit_cmd(
        base: Annotated[
            str,
            typer.Option(
                "--base", help="Measure this one commit rather than searching"
            ),
        ] = "",
        tip: Annotated[
            str,
            typer.Option(
                "--tip", help="Search from this ref rather than from the library pin"
            ),
        ] = "",
        depth: Annotated[
            int,
            typer.Option(
                "--depth",
                help="How many commits one round of the search measures",
            ),
        ] = scaffold_fit.CANDIDATE_DEPTH,
    ) -> None:
        """Measure which upstream commit this project's copied half corresponds to.

        The question adoption turns on, answered by counting rather than by
        remembering: the compiled scaffold is a pure function of upstream and
        this checkout is right here, so each candidate is compiled and its
        files compared byte for byte against the project's own. A copy
        stamped from one commit and edited since reads highest at that commit
        — which is the base `dev scaffold adopt` wants.

        Every commit that changed the copied half is in range, read at
        descending resolution: one round spreads `--depth` measurements over
        the whole of it and the next takes the interval around the sample
        that read highest, so a history of sixteen hundred commits answers in
        seventy-odd compiles.
        """
        source = adopted_source()
        package = declared().project.package
        root = project_root()
        repository = update_mod.upstream_checkout(source.project, typer.echo)
        if base:
            reading = scaffold_fit.measured(
                root,
                repository,
                source,
                package,
                scaffold_fit.resolved(repository, base),
            )
            typer.echo(reading.reported())
            return
        survey = scaffold_fit.surveyed(
            root,
            repository,
            source,
            package,
            scaffold_fit.resolved(repository, tip)
            if tip
            else scaffold_fit.searched_tip(root, repository),
            depth,
        )
        for line in survey.lines():
            typer.echo(line)

    @scaffold_app.command("adopt")
    def scaffold_adopt_cmd(
        base: Annotated[
            str,
            typer.Option(
                "--base", help="The upstream commit this project was stamped from"
            ),
        ],
        accept_fit: Annotated[
            int | None,
            typer.Option(
                "--accept-fit",
                help="Root at a base the measurement argues against, restating "
                "the identical-file count it read",
            ),
        ] = None,
    ) -> None:
        """Root the scaffold branch, once, at the commit this project came from.

        What gives git the ancestor it has been missing: after this, every
        update is a merge against the commit this project last took rather
        than against an unrelated history.

        The base is measured against this checkout before anything is
        written, because that one argument decides every later merge: rooting
        at the commit the pin already resolves to leaves the first update
        nothing to carry, and rooting behind the copy re-offers what somebody
        already ported by hand. A refusal carries the reading, and restating
        the reading is what overrules it.
        """
        update_mod.adopted(
            project_root(),
            adopted_source(),
            declared().project.package,
            base,
            typer.echo,
            accept_fit,
        )

    @app.command("update")
    def update_cmd(
        commit: Annotated[
            str,
            typer.Option("--commit", help="Pin every carrier at this upstream commit"),
        ] = "",
    ) -> None:
        """Move the library, the native trees, and the copied half to one commit.

        The pin resolves first and decides the commit; the copied half is
        compiled at exactly that commit and merged; the trees are regenerated
        under the library that just landed. A conflicted merge stops the run
        and says what to resolve, because everything after it is compiled from
        declarations the merge has not finished writing.

        Run again over a resolved merge, it finishes that pass rather than
        starting a new one: the pin stays where the interrupted pass put it,
        the merge is concluded here, and the copied half is compiled again
        against the declaration the resolution wrote — which is how a
        resolution that widens what this project takes from upstream lands
        what it widened.
        """
        update_mod.updated(
            project_root(),
            adopted_source(),
            declared().project.package,
            commit,
            library_mod.DISTRIBUTION,
            typer.echo,
        )

    # -- what a range asks of a project built on this one --

    def span_over(spelled: str) -> preservation.Span:
        """The two ends a ``base..head`` argument names, the head optional."""
        # git's own range grammar, taken as given rather than invented here.
        base, separator, head = spelled.partition("..")  # lup: ignore[string-split]
        if not separator or not base:
            raise typer.BadParameter(
                f"expected <base>..<head>, or <base>.. for the working tree; "
                f"got {spelled!r}"
            )
        return preservation.Span(base=base, head=head)

    @migrate_app.command("map")
    def migrate_map_cmd(
        over: Annotated[
            str, typer.Argument(help="The range to derive over, as <base>..<head>")
        ],
    ) -> None:
        """Print the relocation that repoints an importer across a range.

        Derived from two surfaces rather than read from a record, so the map
        costs nothing to keep and cannot go stale: a module that declares
        none of its own names any more, all of them now in one other module,
        becomes a pair. A module still declaring some of them has not moved
        however many of its names turn up elsewhere, and a pair for it would
        be applied silently and break every import of a name that stayed — so
        what cannot be spelled as a pair is spelled out instead, name by
        name, for a reader to judge and repoint by hand.
        """
        divergence = span_over(over).divergence(declared().project)
        moves = divergence.module_moves()
        unmapped = divergence.unmapped_modules()
        if not moves and not unmapped:
            typer.echo(f"no module moved over {over}")
            return
        if moves:
            pairs = " ".join(f"{old}={new}" for old, new in sorted(moves.items()))
            typer.echo(f"uv run lup-devtools dev relocate {pairs}")
        if unmapped:
            typer.echo(
                f"{len(unmapped)} module(s) no pair can repoint, since `dev "
                f"relocate` respells a whole module path. Where their names "
                f"resolve now — a name declared elsewhere may have moved there, "
                f"or two modules may have chosen one word:"
            )
            for move in unmapped:
                typer.echo(f"  {move.spelled()}")

    @migrate_app.command("pending")
    def migrate_pending_cmd(
        revision: Annotated[
            str,
            typer.Argument(help="Where the project stands, as a commit of this one"),
        ],
        repository: Annotated[
            Path | None,
            typer.Option(help="Upstream checkout holding the migration commits"),
        ] = None,
        as_json: Annotated[
            bool,
            typer.Option("--json", help="Render an installed-library report as JSON"),
        ] = False,
    ) -> None:
        """What a project standing at that commit still owes, beyond the map.

        The declared residue: a signature that gained parameters, a refusal
        that split. Read from every release's record and the pending window,
        so a project crossing several releases hears each one's; a project
        already past the commit that made a break has applied it, and is told
        nothing.
        """
        owed = migrations.unapplied(
            migrations.MigrationRecord().declared(), revision, repository or Path.cwd()
        )
        if as_json:
            output_json(
                migrations.RenderedMigrations(
                    count=len(owed), lines=migrations.rendered(owed)
                )
            )
            return
        if not owed:
            typer.echo(f"nothing declared since {revision}")
            return
        typer.echo(f"{len(owed)} migration(s) since {revision}:")
        for line in migrations.rendered(owed):
            typer.echo(f"  {line}")

    @app.command("release")
    def release_cmd(
        level: Annotated[
            str,
            typer.Argument(help="Which part of the version moves: patch, minor, major"),
        ],
        dry_run: DryRun = False,
        as_json: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
    ) -> None:
        """Cut a release: close the changelog, move the version, tag it.

        One transaction over the files a release touches, because four prose
        steps in a skill are three steps that never run. What stays outside
        is what cannot be derived — which level the release is, and what the
        entries under `## Unreleased` say — and everything downstream of
        those is arithmetic, carried out the same way each time.

        Refused on a dirty tree and on an undeclared break, in that order. The
        first because a release commit should hold the release and not
        whatever somebody left lying about; the second because the gate exists
        to stop a break shipping with no instruction, and a release is the
        moment it would ship.
        """
        from lup.devtools.dev.release import (
            ReleasePlan,
            is_level,
            next_version,
            published_version,
            release_subject,
            released,
            with_version,
        )

        if not is_level(level):
            typer.echo(f"{level} is not patch, minor or major", err=True)
            raise typer.Exit(1)
        # Only where something is about to be written. A dry run is what
        # somebody asks *while* the tree is dirty, to see what a release would
        # do before deciding what to do with the rest of it.
        if not dry_run and git.out("status", "--porcelain").strip():
            typer.echo(
                "the working tree has uncommitted changes — a release commit "
                "holds the release, so land or discard them first",
                err=True,
            )
            raise typer.Exit(1)

        declarations = declared()
        spec = declarations.release
        root = project_root()
        record = migrations.MigrationRecord()
        base = migrations.gate_base(get_integration_branch())
        undeclared = (
            migrations.undeclared_breaks(declarations.project, base, record)
            if base
            else []
        )
        if undeclared:
            for capability in undeclared:
                typer.echo(f"undeclared break: {capability.spelled()}", err=True)
            typer.echo(
                "a release cannot carry a break with nothing to read — "
                f"{record.instruction(root)}",
                err=True,
            )
            raise typer.Exit(1)

        manifest = root / spec.version_file
        changelog_path = root / spec.changelog
        previous = published_version(manifest)
        version = next_version(previous, level)
        today = dt.date.today()
        pending = record.pending()
        folded = migrations.rendered(pending)
        log = Changelog.read(changelog_path)
        plan = ReleasePlan(
            previous=previous,
            version=version,
            date=today,
            tag=f"{spec.tag_prefix}{version}",
            migrations=folded,
            breaks=len(pending),
            entries=bool(log.unreleased),
        )

        if dry_run:
            if as_json:
                output_json(plan)
            else:
                for line in plan.spelled():
                    typer.echo(f"would release: {line}")
            return

        changelog_path.write_text(released(log, version, today, folded).render())
        manifest.write_text(with_version(manifest.read_text(), version))
        record.release(version, root)

        # The version is a source a generated artifact compiles from, so
        # writing it leaves the trees that embed it behind — and the commit
        # guard refuses exactly that, which is how a release came to be the
        # one commit this repository could not make. Regenerating here is
        # what the guard is asking for, and everything it writes belongs in
        # the same commit as the bump that caused it.
        update_mod.regenerated(root, lambda line: typer.echo(line, err=True))
        git.add("-A")
        git.commit("-m", release_subject(previous, version))
        git.tag("-a", plan.tag, "-m", f"{spec.version_file} {version}")

        if as_json:
            output_json(plan)
        else:
            for line in plan.spelled():
                typer.echo(f"released: {line}")
            typer.echo(f"tagged {plan.tag} — pushing the tag is what publishes")

    @migrate_app.command("check")
    def migrate_check_cmd(
        over: Annotated[
            str,
            typer.Option("--over", help="The range to judge, as <base>..<head>"),
        ] = "",
        as_json: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
    ) -> None:
        """Refuse a capability that went with no migration speaking for it.

        A name declared nowhere any more is a break an adopter meets as an
        import that stopped resolving. One that moved is not, because the map
        is derived — so what fails here is the difference: something gone, and
        nothing in this repository saying what to do about it. Every release's
        record is read beside the pending window, so a range spanning a
        release hears what that release declared.
        """
        from lup.devtools.dev.branches import detect_base_branch

        # The branch's own base rather than a branch named here: what this
        # change took away is measured against where it started, and creation
        # recorded that where topology can no longer recover it.
        span = span_over(over or f"{detect_base_branch().merge_base}..")
        divergence = span.divergence(declared().project)
        record = migrations.MigrationRecord()
        unnamed = migrations.unnamed_between(
            divergence.disappeared, record.declared(), span.base, span.head
        )
        if as_json:
            output_json(divergence)
        else:
            for capability in unnamed:
                typer.echo(f"gone, undeclared: {capability.spelled()}", err=True)
            if unnamed:
                typer.echo(f"  {record.instruction(project_root())}", err=True)
            typer.echo(
                f"{len(divergence.relocated)} moved, {len(divergence.arrived)} "
                f"arrived, {len(divergence.disappeared)} gone "
                f"({len(unnamed)} with no migration)"
            )
        if unnamed:
            raise typer.Exit(1)

    @app.command("policy")
    def policy_cmd(
        subjects: Annotated[
            list[str],
            typer.Argument(help="Commands, URLs, or paths to classify"),
        ],
        kind: Annotated[
            str,
            typer.Option(
                "--kind", help="Input: shell, fetch, edit path, or edit-batch JSON"
            ),
        ] = "shell",
        placement: Annotated[
            str | None,
            typer.Option(
                "--placement",
                help="Answer for this placement instead: none, inner or outer"
                " (default: this session's own, read from its launch ledger)",
            ),
        ] = None,
        autonomous: Annotated[
            bool,
            typer.Option(
                "--autonomous", help="Judge as a self-reviewing identity would"
            ),
        ] = False,
        as_json: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
    ) -> None:
        """Show what the declared permission policy decides about an input, and why."""
        if kind not in ("shell", "fetch", "edit", "edit-batch"):
            typer.echo(
                f"unknown kind {kind!r}: expected shell, fetch, edit, or edit-batch",
                err=True,
            )
            raise typer.Exit(2)
        # This session's answer unless a placement is named: the guidance sends
        # a reader here before they spend a turn, and the turn they spend is
        # judged by the ledger their own dispatcher reads.
        policy_explain.explain(
            subjects,
            kind,
            autonomous,
            as_json,
            declared().hooks,
            placement,
        )

    @app.command("vocabulary")
    def vocabulary_cmd(
        offered: Annotated[
            bool,
            typer.Option("--offered", help="Survey lup's offered defaults instead"),
        ] = False,
        as_json: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
        output: Annotated[
            Path | None,
            typer.Option("--output", help="Write the survey here instead of stdout"),
        ] = None,
        provenance: Annotated[
            bool,
            typer.Option(
                "--provenance", help="List rules and where each axis came from"
            ),
        ] = False,
    ) -> None:
        """Show every shell form the declared vocabulary judges, and how."""
        # lup: defer: the runner targets' verb tables are judged too -- every
        # `uv run lup-devtools` verb that asks, or is refused to the requester,
        # sits there -- and this survey reads the command table alone, so `sync
        # setup`'s question shows in `dev policy` and nowhere here; decide how a
        # target's verbs read beside the bare spelling of the same executable
        rules = (
            default_vocabulary() if offered else declared().hooks.resolved_shell_rules()
        )
        policy_explain.survey(rules, as_json, output, provenance)

    # -- plugin commands --

    @plugin_app.command("name")
    def plugin_name_cmd(
        name: Annotated[
            str | None,
            typer.Argument(help="Marketplace name (default: pyproject [project].name)"),
        ] = None,
        dry_run: Annotated[
            bool,
            typer.Option("--dry-run", "-n", help="Show changes without writing"),
        ] = False,
    ) -> None:
        """Name this repo's plugin marketplace uniquely (the plugin entry is kept).

        Marketplace names share one global namespace, so a shared name collides
        across repos and worktrees and installs shadow each other. Naming the
        marketplace after the project fixes that.
        """
        plugin_mod.name_marketplace(declared().plugin, name, dry_run)

    return app
