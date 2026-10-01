"""The command tree for moving work through git: branches, worktrees, PRs.

Its own sub-app rather than a corner of ``dev`` because a sub-app is a surface
of a module, and every command here is the git-workflow module's. While they
sat under ``dev`` they were owned by ``core``, so a project declining the git
loop kept all of them — the failure the module system exists to end, surviving
inside it.

The bodies are the same closures they were: what one repository declares about
itself arrives as :class:`~lup.devtools.dev.app.DevDeclarations`, read when a
command runs rather than when the CLI is composed, because each of these
resolves against a working directory a CLI is imported long before anyone
points it at.
"""

import sys
from collections.abc import Callable
from pathlib import Path
from typing import Annotated

import typer
import sh

import lup.devtools.dev.branches as branches
import lup.devtools.dev.git_guards as git_guards_mod
import lup.devtools.dev.pr as pr
import lup.devtools.dev.preview as preview
import lup.devtools.dev.worktree as worktree
from lup.devtools.dev.conflict_app import create_conflict_app
from lup.devtools.dev.declarations import DevDeclarations
from lup.devtools.harness.launch import relocation_hint
from lup.execution.process import LocalProcessLauncher
from lup.policy.vocabulary import protected_branches
from lup.workspace.paths import project_root
from lup.devtools.git.prepare import prepare
from lup.devtools.git.settle import settle
from lup.devtools.entrypoint import in_process
from lup.devtools.launcher import console_script
from lup.devtools.utils import decode_stderr


def create_git_app(declared: Callable[[], DevDeclarations]) -> typer.Typer:
    """Wire the git command tree over what one repository declares about itself."""
    app = typer.Typer(no_args_is_help=True)
    worktree_app = typer.Typer(no_args_is_help=True)
    pr_app = typer.Typer(no_args_is_help=True)
    guard_app = typer.Typer(no_args_is_help=True)
    app.add_typer(worktree_app, name="worktree", help="Worktree management")
    app.add_typer(pr_app, name="pr", help="PR lifecycle (status, merge, push, checks)")
    app.add_typer(
        create_conflict_app(), name="conflict", help="Merge/rebase conflict resolution"
    )
    app.add_typer(
        guard_app,
        name="hooks",
        help="The git hooks refusing stale artifacts and a failing gate",
    )

    @app.callback()
    def guard_worktree_pointers(ctx: typer.Context) -> None:
        """Refuse any git-workflow command run over a redirected worktree set.

        The one host-side chokepoint for this command tree: every `dev git`
        subcommand -- and so every `/lup:land` step, which runs them -- passes
        through here before running git across the worktrees, so a pointer a
        contained session moved is caught once rather than at each command. A
        layout with no sibling worktrees no-ops, leaving a plain checkout's
        commands untouched. The hooks group judges for itself, one level down.
        """
        if ctx.invoked_subcommand != "hooks":
            worktree.refuse_redirected_pointers()

    @guard_app.callback()
    def guard_hook_pointers(ctx: typer.Context) -> None:
        """Judge the worktree set for every hooks command but the one git runs.

        `run` is reached from inside a hook, after git has resolved the
        repository it is working in, and reads that repository alone; each
        guard it runs that is a git-workflow command passes through the
        judgement above on its own. Judging every worktree of the clone there
        would charge each commit seconds more for nothing.
        """
        if ctx.invoked_subcommand != "run":
            worktree.refuse_redirected_pointers()

    def scaffold_branch() -> str:
        """The branch this project's copied half is compiled onto, if it has one."""
        source = declared().scaffold
        return source.branch if source is not None else ""

    # -- worktree commands --

    @worktree_app.command("create")
    def worktree_create_cmd(
        name: Annotated[
            str, typer.Argument(help="Name for the worktree (e.g., feat-name)")
        ],
        no_sync: Annotated[
            bool,
            typer.Option(
                "--no-sync",
                help="Skip restoring the environment (uv sync) and the bun workspaces",
            ),
        ] = False,
        no_copy_data: Annotated[
            bool,
            typer.Option("--no-copy-data", help="Skip copying gitignored extras"),
        ] = False,
        base_branch: Annotated[
            str | None,
            typer.Option(
                "--base",
                "-b",
                help="Branch to cut from (default: the integration branch, "
                "asked for where this checkout is ahead of it)",
            ),
        ] = None,
        force: Annotated[
            bool,
            typer.Option(
                "--force",
                help="Delete an existing unregistered directory at the worktree path",
            ),
        ] = False,
        no_record: Annotated[
            bool,
            typer.Option(
                "--no-record",
                help="Create with no base recorded, instead of refusing to guess",
            ),
        ] = False,
        clipboard: Annotated[
            bool,
            typer.Option(
                "--clipboard",
                help="Also copy the shell line to your clipboard, for pasting",
            ),
        ] = False,
    ) -> None:
        """Create or re-attach a git worktree."""
        worktree.create(
            name,
            no_sync,
            no_copy_data,
            base_branch,
            relocation_hint,
            force=force,
            no_record=no_record,
            clipboard=clipboard,
            guards=declared().git_guards,
            workspaces=declared().restored_workspaces(),
            projects=declared().sub_projects.synced(),
        )

    @worktree_app.command("list")
    def worktree_list_cmd() -> None:
        """List all git worktrees with branch and status info."""
        worktree.list_worktrees()

    @worktree_app.command("remove")
    def worktree_remove_cmd(
        name: Annotated[str, typer.Argument(help="Worktree name or path to remove")],
        force: Annotated[
            bool,
            typer.Option("--force", help="Force removal even if dirty"),
        ] = False,
    ) -> None:
        """Remove a git worktree."""
        worktree.remove(name, force)

    @worktree_app.command("adopt-records")
    def worktree_adopt_records_cmd() -> None:
        """Move lup's `branch.*.lup-*` config keys into the shared `lup/` directory.

        Once per clone, and on the host: it is the one step that writes the
        shared config. Reads answer from the records alone, so a base still
        held in the config counts for nothing until this has run.
        """
        worktree.adopt_records()

    # -- branch commands --

    @app.command("branches")
    def branches_cmd(
        branch: Annotated[
            str | None,
            typer.Argument(help="Specific branch to check (default: all)"),
        ] = None,
        as_json: Annotated[
            bool,
            typer.Option("--json", help="Output as JSON"),
        ] = False,
    ) -> None:
        """Analyze branch containment, PR status, and worktree info."""
        branches.branch_status(branch, as_json)

    @app.command("base-branch")
    def base_branch_cmd(
        branch: Annotated[
            str | None,
            typer.Argument(help="Branch to analyze (default: current)"),
        ] = None,
        as_json: Annotated[
            bool,
            typer.Option("--json", help="Output as JSON"),
        ] = False,
    ) -> None:
        """Detect the base branch for the current (or specified) branch."""
        branches.base_branch(branch, as_json)

    @app.command("freshness")
    def freshness_cmd(
        settle: Annotated[
            bool,
            typer.Option(
                "--settle",
                help="Settle a clean checkout rather than only reporting: pull "
                "what the remote holds, then push what it lacks",
            ),
        ] = False,
    ) -> None:
        """Report how far this checkout sits behind its own remote and its base.

        The reading a session is opened on, asked on its own — a checkout
        cannot tell from its own contents that either has moved, and the
        answer otherwise only appears in front of a session nobody asked for.
        """
        if settle:
            branches.settle_base_freshness(
                LocalProcessLauncher(), project_root(), publish=True
            )
            return
        typer.echo(
            branches.probe_base_freshness(
                LocalProcessLauncher(), project_root()
            ).report()
        )

    # -- pr-body command --

    @app.command("pr-body")
    def pr_body_cmd(
        base: Annotated[
            str | None,
            typer.Option("--base", "-b", help="Override base branch"),
        ] = None,
    ) -> None:
        """Generate a PR body (summary, commits, test plan) from branch commits."""
        branches.pr_body(base)

    # -- branch survey and delete --

    @app.command("survey")
    def survey_cmd(
        as_json: Annotated[
            bool,
            typer.Option("--json", help="Output as JSON"),
        ] = False,
    ) -> None:
        """Full branch inventory: containment, PRs, unique commits, diff sizes."""
        branches.survey(as_json, scaffold=scaffold_branch())

    @app.command("preview")
    def preview_cmd(
        names: Annotated[
            list[str], typer.Argument(help="Branches or commits to preview")
        ],
        into: Annotated[
            str | None,
            typer.Option(
                "--into",
                help="What they would land in (default: the integration branch)",
            ),
        ] = None,
        as_json: Annotated[
            bool,
            typer.Option("--json", help="Output as JSON"),
        ] = False,
    ) -> None:
        """Say what landing each branch would change, conflict on, and share.

        Merges each in memory, touching no index or working tree, so the
        answer is about content: a branch every change of which already
        stands in the target reads as nothing new, however its commits were
        rewritten. Each commit no patch-id matches is paired with the target
        commit carrying its subject, and read as rewritten where the two
        change the same lines. Given several, it names the files each pair
        of them both touched, which is what orders a sweep.
        """
        target = into if into is not None else branches.get_integration_branch()
        preview.run_preview(names, target, as_json)

    @app.command("settle")
    def settle_cmd() -> None:
        """Regenerate over the merge commit HEAD just became, and fold it in.

        The post-merge and post-commit guards run this; by hand it settles a
        merge made where they were not armed. It rewrites only a merge commit
        no other branch holds, with the same parents, message and author, and
        commits nothing regeneration did not write.
        """
        root = project_root()
        launcher = console_script(root)
        if launcher is None:
            typer.echo(
                "This checkout's environment is not synced, so the merge was not "
                "settled: regenerate and commit what it writes by hand.",
                err=True,
            )
            raise typer.Exit(1)

        def regenerate() -> None:
            sh.Command(str(launcher))("harness", "generate", "all", _cwd=str(root))

        try:
            settled = settle(root, regenerate)
        except sh.ErrorReturnCode as error:
            typer.echo(
                "The merge was not settled, so its generated trees may be stale: "
                f"{decode_stderr(error).strip()}",
                err=True,
            )
            raise typer.Exit(1) from error
        if settled is not None:
            typer.echo(settled.report())

    @app.command("merge-driver")
    def merge_driver_cmd() -> None:
        """Register the ownership-manifest merge driver `.gitattributes` names."""
        worktree.register_merge_driver()
        typer.echo(f"Registered merge driver: {worktree.OWNERSHIP_MERGE_DRIVER}")

    @app.command("delete")
    def delete_cmd(
        name: Annotated[str, typer.Argument(help="Branch name to delete")],
        dry_run: Annotated[
            bool,
            typer.Option("--dry-run", "-n", help="Show what would happen"),
        ] = False,
        force: Annotated[
            bool,
            typer.Option(
                "--force",
                "-f",
                help="Force delete the branch and a worktree holding modified files",
            ),
        ] = False,
        remote: Annotated[
            bool | None,
            typer.Option(
                "--remote/--no-remote",
                help="Delete origin's copy too (default: only if merged)",
            ),
        ] = None,
    ) -> None:
        """Delete a branch and its worktree, and origin's copy if it is spent.

        Its session records are archived first, since the worktree usually holds
        the only copy; a deletion whose archive fails is refused.
        """
        branches.delete_branch(name, dry_run, force, remote, scaffold=scaffold_branch())

    @app.command("retire")
    def retire_cmd(
        name: Annotated[str, typer.Argument(help="Branch name to retire")],
        reason: Annotated[
            str,
            typer.Option("--reason", help="Why this work is not being landed"),
        ],
        dry_run: Annotated[
            bool,
            typer.Option("--dry-run", "-n", help="Show what would happen"),
        ] = False,
        base: Annotated[
            str | None,
            typer.Option("--base", help="Branch the request targets"),
        ] = None,
    ) -> None:
        """Retire a branch through a pull request, so its commits outlive it.

        For work that is not being landed and is not in the integration
        branch either — where a plain delete leaves the commits reachable
        from nothing. Pushes, opens a request, closes it without merging,
        and then deletes: the head stays at `refs/pull/<number>/head`, which
        outlives both the branch and origin's copy of it.
        """
        branches.retire_branch(name, reason, dry_run, base, scaffold=scaffold_branch())

    # -- git hook commands --

    @guard_app.command("install")
    def guard_install_cmd(
        force: Annotated[
            bool,
            typer.Option("--force", help="Replace a hook written elsewhere"),
        ] = False,
    ) -> None:
        """Install every git hook this repository declares.

        Idempotent, and shared by every worktree of the clone it is run from,
        so re-running it after a library upgrade refreshes an older body.

        One occupied hook path stops the whole command rather than half of
        it: `--force` is an answer about a file somebody wrote deliberately,
        and installing the rest first would leave the reader working out
        which of them the error was about.
        """
        root = project_root()
        guards = declared().git_guards
        # A host step: the shared hooks directory is held read-only inside a
        # contained session, so where there is something to write and nothing
        # here may write it, the host's command is what the reader needs.
        blocked = git_guards_mod.blocked_arming(guards, root)
        if blocked:
            typer.echo(blocked, err=True)
            raise typer.Exit(1)
        try:
            installed = git_guards_mod.install_guards(guards, root, force=force)
        except git_guards_mod.GuardConflict as error:
            typer.echo(str(error), err=True)
            raise typer.Exit(1) from error
        except OSError as error:
            # One clone's hooks directory is shared by every worktree cut from
            # it and sits outside all of them, so a sandbox confining writes to
            # the checkout refuses this — as an errno naming a path, which says
            # nothing about hooks to whoever reads it out of a traceback.
            typer.echo(
                f"the hooks could not be written: {error}. Where this session "
                "cannot write them, run "
                f"`{git_guards_mod.host_install(root)}` from a terminal on the host.",
                err=True,
            )
            raise typer.Exit(1) from error
        for state in installed:
            typer.echo(state.describe())

    @guard_app.command("run")
    def guard_run_cmd(
        hook: Annotated[str, typer.Argument(help="The git hook that fired")],
        arguments: Annotated[
            list[str] | None, typer.Argument(help="What git passed the hook")
        ] = None,
    ) -> None:
        """Run the guards this checkout declares at one git hook.

        Every hook `install` writes calls this and names no guard, so what
        runs is this checkout's own declaration at the revision it is at,
        whichever revision wrote the hook. The first guard to refuse ends
        the moment, and says why. A checkout that compiles its guards into
        its manifest answers this before the application loads; this is the
        answer for one that does not, and a guard that is a devtools command
        runs in this same process either way.
        """
        status = git_guards_mod.fire(
            declared().git_guards,
            hook,
            tuple(arguments or ()),
            project_root(),
            sys.stdin,
            in_process,
        )
        raise typer.Exit(status)

    @guard_app.command("status")
    def guard_status_cmd() -> None:
        """Report what this clone refuses, at every moment a hook sits at.

        Both directions, because either alone reads as fully armed: a moment
        this declares with nothing installed at it, and a hook this installed
        at a moment nothing declares any more. Each moment lists the guards
        this checkout runs there, which the installed hook does not name.
        """
        guards = declared().git_guards
        hooks = git_guards_mod.read_hooks(guards, project_root())
        scripts = git_guards_mod.hook_scripts(guards)
        for script, state in zip(scripts, hooks.guards, strict=True):
            typer.echo(state.describe())
            for guard in script.guards:
                typer.echo(f"  runs `{guard.command}`")
        for state in hooks.orphaned:
            typer.echo(state.describe())

    @guard_app.command("uninstall")
    def guard_uninstall_cmd() -> None:
        """Remove them, leaving hooks written elsewhere alone."""
        removed = git_guards_mod.uninstall_guards(declared().git_guards, project_root())
        for state in removed:
            typer.echo(state.describe())

    # -- pr commands --

    @pr_app.command("status")
    def pr_status_detail_cmd(
        branch: Annotated[
            str | None,
            typer.Option("--branch", "-b", help="Branch name (default: current)"),
        ] = None,
        as_json: Annotated[
            bool,
            typer.Option("--json", help="Output as JSON"),
        ] = False,
    ) -> None:
        """Fetch PR review status, checks, and comments for a branch."""
        pr.status(branch, as_json)

    @pr_app.command("merge")
    def pr_merge_cmd(
        pr_number: Annotated[int, typer.Argument(help="PR number to merge")],
        dry_run: Annotated[
            bool,
            typer.Option("--dry-run", "-n", help="Show what would happen"),
        ] = False,
        as_json: Annotated[
            bool,
            typer.Option("--json", help="Output as JSON"),
        ] = False,
        method: Annotated[
            pr.MergeMethod,
            typer.Option("--method", help="How the commits reach the base branch"),
        ] = pr.MergeMethod.merge,
        gh_args: Annotated[
            list[str] | None,
            typer.Option("--gh", help="Further flag handed to `gh pr merge` untouched"),
        ] = None,
        retarget: Annotated[
            bool,
            typer.Option(
                "--retarget",
                help="Point a stacked PR's base at the integration branch first",
            ),
        ] = False,
    ) -> None:
        """Merge a PR and pull changes into the integration branch."""
        pr.merge(pr_number, dry_run, as_json, method, tuple(gh_args or ()), retarget)

    @pr_app.command("prepare")
    def pr_prepare_cmd(
        base: Annotated[
            str, typer.Option("--base", help="Explicit, freshly fetched target ref")
        ],
        as_json: Annotated[bool, typer.Option("--json", help="Output as JSON")] = False,
    ) -> None:
        """Merge an explicit local base, regenerate every harness, and commit."""
        root = project_root()
        launcher = console_script(root)
        if launcher is None:
            raise typer.BadParameter(
                "Sync this checkout's environment before preparing it."
            )

        def regenerate() -> None:
            sh.Command(str(launcher))("harness", "generate", "all", _cwd=str(root))

        try:
            result = prepare(base, root, regenerate)
        except (RuntimeError, sh.ErrorReturnCode) as error:
            typer.echo(str(error), err=True)
            raise typer.Exit(1) from error
        typer.echo(
            result.model_dump_json()
            if as_json
            else f"Prepared {result.head} against {result.base_commit}; no branch was pushed."
        )

    @pr_app.command("sync-base")
    def pr_sync_base_cmd(
        base: Annotated[
            str | None,
            typer.Option("--base", "-b", help="Base branch (default: auto-detect)"),
        ] = None,
        as_json: Annotated[
            bool,
            typer.Option("--json", help="Output as JSON"),
        ] = False,
    ) -> None:
        """Sync the base branch and merge it into the current feature branch."""
        pr.sync_base(base, as_json)

    @pr_app.command("push")
    def pr_push_cmd(
        force: Annotated[
            bool,
            typer.Option(
                "--force",
                "-f",
                help="Force push with a lease, refused on an integration branch",
            ),
        ] = False,
        as_json: Annotated[
            bool,
            typer.Option("--json", help="Output as JSON"),
        ] = False,
    ) -> None:
        """Push the current branch and report any existing PR."""
        rules = declared().hooks.resolved_shell_rules()
        pr.push(force, as_json, protected_branches(rules))

    @pr_app.command("create")
    def pr_create_cmd(
        base: Annotated[str, typer.Option("--base", help="Target branch for PR")],
        title: Annotated[str, typer.Option("--title", help="PR title")],
        body: Annotated[
            str | None, typer.Option("--body", help="PR body (markdown)")
        ] = None,
        body_file: Annotated[
            Path | None,
            typer.Option("--body-file", help="Read the PR body from this file"),
        ] = None,
        as_json: Annotated[
            bool,
            typer.Option("--json", help="Output as JSON"),
        ] = False,
    ) -> None:
        """Create a new PR."""
        pr.create(base, title, pr.resolve_body(body, body_file), as_json)

    @pr_app.command("update")
    def pr_update_cmd(
        pr_number: Annotated[int, typer.Argument(help="PR number to update")],
        body: Annotated[
            str | None, typer.Option("--body", help="New PR body (markdown)")
        ] = None,
        body_file: Annotated[
            Path | None,
            typer.Option("--body-file", help="Read the new PR body from this file"),
        ] = None,
    ) -> None:
        """Update a PR body."""
        pr.update(pr_number, pr.resolve_body(body, body_file))

    return app
