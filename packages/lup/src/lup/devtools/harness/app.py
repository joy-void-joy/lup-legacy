"""Typer command tree for ``lup-devtools harness``: wiring only, no bodies.

Each command delegates to the module owning its concern: ``drift`` for
generation and checking, ``reconcile`` for local-difference workflows,
``doctor`` for runtime evidence, ``resolve`` for the persisted resolver,
and ``launch`` for the native launchers. Every one of them works on already
concrete compositions, so the targets a project declares are what this tree
operates on and no command names a runtime of its own.

A launch command exists exactly when its adapter is among those targets: a
project generating one native tree is not offered a launcher for the other.
"""

from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer

import lup.devtools.harness.clean as clean
import lup.devtools.harness.doctor as doctor
import lup.devtools.harness.drift as drift
import lup.devtools.harness.launch as launch
from lup.launch.declaration import LaunchSandbox, OuterContainer
import lup.devtools.harness.policy_refresh as policy_refresh
import lup.devtools.harness.reconcile as reconcile
import lup.devtools.harness.resolve as resolve
from lup.coordination.refs import ActorRef
from lup.harness.codescan.registry import every_rule_retired
from lup.devtools.harness.composition import NativeTargets, claude_profile_directory
from lup.ledger.models import LedgerNode
from lup.ledger.store import LedgerLayout
from lup.observability.sessions import SessionRecorder, session_recorder
from lup.launch.config_volume import HomeHelper, kept_for_superseded
from lup.launch.superseded import SupersededFile
from lup.launch.session import (
    ambient_config_home,
    report_inside_requirements,
    report_requirements,
)
from lup.launch.container import (
    checkout_tag,
    image_tag,
    report_egress,
    retire_images,
    superseded_images,
)
from lup.harness.environment import Placement
from lup.harness.image import Image, detected_client
from lup.harness.generate import NativeHarnessComposition
from lup.devtools.harness.profile_app import create_profile_app
from lup.harness.models import Resumption
from lup.harness.notice import Banner
from lup.harness.releases import resolved_agent_clis
from lup.harness.requirements import Manifest
from lup.providers.profiles import ProfileDirectory
from lup.providers.runtime_homes import runtime_logins
from lup.devtools.harness.drift import RepositoryWriter
from lup.diagnostics import refuse
from lup.policy.kernel.diagnostic import devtools, spelled, step
from lup.workspace.paths import project_root
from lup.policy.assets.host import boundary_description
from lup.sandbox.models import NetworkMode
from lup.sandbox.observed import unheld


def refuse_inside_a_container(command: Sequence[str], because: str) -> None:
    """Stop a command whose answer is the host's, where it runs inside a session's container.

    ``command`` is the command's words after ``lup-devtools``, which the way
    through runs on the host.
    """
    if Placement.here().contained:
        refuse(
            f"runs inside a lup container here, where {because}",
            what=spelled(command),
            steps=[step("run it from a terminal on the host", devtools(*command))],
        )


def create_harness_app(
    targets: NativeTargets,
    repository_writers: list[RepositoryWriter],
    model: resolve.ConfiguredModel | None = None,
    profiles: ProfileDirectory | None = None,
    launch_modes: list[launch.LaunchMode] | None = None,
    checkpoint: launch.LaunchCheckpoint | None = None,
    node_classes: list[type[LedgerNode]] | None = None,
    ledger: LedgerLayout = LedgerLayout(),
    container: OuterContainer = OuterContainer(),
) -> typer.Typer:
    """Wire the harness command tree over the targets one project declares.

    ``profiles`` is where a name finds its account. Supplying none takes the
    person's own, under their lup config home, which every checkout shares —
    so one name selects the same account here as in any other repository.

    ``launch_modes`` are that project's own kinds of session, each selected
    by ``--mode <name>`` on every launcher: a preset laid over the
    declaration a launch builds, and the tree it declares compiled instead of
    the default where it declares one.

    ``checkpoint`` saves application data before generation and after closing.

    ``node_classes`` and ``ledger`` are what the project records and where:
    a launch that finds the session kinds among them records itself in the
    ledger as a pointer at its transcript directory, and one that does not
    records nothing.

    ``container`` is what the project's contained sessions are granted, under
    the person's ``[container]`` config, a mode and the command line.
    """
    directory = profiles or claude_profile_directory()
    modes = launch_modes or []
    app = typer.Typer(no_args_is_help=True, help="Generate and launch a native harness")
    selector = f"{', '.join(targets.builders)}, or {targets.every}"

    def recorder_for(provider: str) -> SessionRecorder | None:
        """The ledger recorder a launch of one runtime records through, resolved now.

        Built at launch rather than when the tree is wired, because the
        project root is resolved against the working directory the command
        runs in, and the recorder stamps the launcher as the author.
        """
        return session_recorder(
            project_root(),
            ActorRef(kind="harness", id=provider),
            node_classes or [],
            ledger,
        )

    def repository_wide(target: str) -> list[RepositoryWriter]:
        """The writers a selector reaches: every one of them, or none.

        A generated file outside a native tree belongs to no single target,
        so only the selector naming all of them is answerable for it.
        """
        return repository_writers if target == targets.every else []

    @app.command("generate")
    def generate_command(
        target: Annotated[str, typer.Argument(help=selector)] = targets.every,
    ) -> None:
        """Deterministically generate owned native artifacts without launching."""
        drift.generate_targets(
            targets.resolve(target, project_root()), repository_wide(target)
        )

    @app.command("policy-refresh")
    def policy_refresh_command(
        nonce: Annotated[
            str, typer.Option(help="Live launch nonce from its policy diagnostic")
        ],
        repository: Annotated[
            Path, typer.Option(help="Already granted destination checkout")
        ],
    ) -> None:
        """Accept changed destination policy from an independent operator terminal."""
        policy_refresh.refresh_command(project_root(), nonce, repository)

    @app.command("check")
    def check_command(
        target: Annotated[str, typer.Argument(help=selector)] = targets.every,
    ) -> None:
        """Read-only ownership and generated-artifact drift check for CI."""
        drift.check_targets(
            targets.resolve(target, project_root()), repository_wide(target)
        )

    @app.command("reconcile")
    def reconcile_command(
        target: Annotated[str, typer.Argument(help=selector)] = targets.every,
    ) -> None:
        """Classify local differences without rewriting canonical Python source."""
        reconcile.classify_targets(targets.resolve(target, project_root()))

    @app.command("apply-reconciliation")
    def apply_reconciliation(
        proposal_id: Annotated[str, typer.Argument(help="Persisted proposal id")],
    ) -> None:
        """Apply a stale-base-checked source patch, then regenerate every target."""
        reconcile.apply_proposal(
            proposal_id, targets.resolve(targets.every, project_root())
        )

    @app.command("propose-reconciliation")
    def propose_reconciliation(
        patch: Annotated[
            Path,
            typer.Argument(help="Git-format patch against canonical Python source"),
        ],
    ) -> None:
        """Persist a source patch for separate review and stale-base-checked apply."""
        reconcile.propose_patch(patch)

    @app.command("doctor")
    def doctor_command(
        target: Annotated[str, typer.Argument(help=selector)] = targets.every,
        strict_evidence: Annotated[
            bool,
            typer.Option(
                "--strict-evidence",
                help="Exit nonzero when an installed component is newer than the "
                "evidence register (the nightly lane's re-probe trigger)",
            ),
        ] = False,
    ) -> None:
        """Report installed native runtime evidence without updating either CLI."""
        doctor.run_doctor(targets.resolve(target, project_root()), strict_evidence)

    @app.command("requirements")
    def requirements_command(
        target: Annotated[str, typer.Argument(help=selector)] = targets.every,
        launch_only: Annotated[
            bool,
            typer.Option(
                "--launch-only",
                help="Run only startup checks; omit setup-only checks",
            ),
        ] = False,
        inside: Annotated[
            bool,
            typer.Option(
                "--inside",
                help=(
                    "Check inside the session container. Build its image and "
                    "start its network if needed"
                ),
            ),
        ] = False,
    ) -> None:
        """Check dependencies on the host, or in the session container with --inside.

        Host checks include the commands an agent needs when running on the
        host. A normal container launch checks those commands inside instead.
        Every result names the environment checked.

        --inside uses the session's image, mounts, credentials and network.
        It builds the image if needed. Full checks include a test model turn;
        add --launch-only to run only the checks used at startup.

        Exits nonzero if a needed capability fails a check. Optional
        conveniences alone do not cause failure. Refused inside a session's
        container, where neither half can answer: run it on the host, and
        `harness binds` to check from inside that the read-only binds hold.
        """
        refuse_inside_a_container(
            ["harness", "requirements"],
            "the host's checks would take the container for the host and "
            "--inside cannot start one",
        )
        compositions = targets.resolve(target, project_root())
        # The two halves span the targets differently, because they answer
        # differently-scoped questions. What the image must carry is the
        # image's own, and every runtime here composes against one image, so
        # a check the first runtime exercised is not paid for again by the
        # second: only what the second declares differently -- its own
        # session probe -- runs, and the boundary notice is said once. What
        # the host must carry is the machine's, asked once for all of them.
        manifests = [
            composition.recipe.source.requirements for composition in compositions
        ]
        exercised_before = [
            {
                signature
                for manifest in manifests[:index]
                for signature in manifest.inside_signatures(not launch_only)
            }
            for index in range(len(manifests))
        ]
        with launch.usage_refusals():
            findings = (
                [
                    finding
                    for index, composition in enumerate(compositions)
                    for finding in report_inside_requirements(
                        composition.recipe.source.image,
                        composition.recipe.source.requirements,
                        project_root(),
                        ambient_config_home(
                            composition.login, composition.default_config_home
                        ),
                        composition.login,
                        setting_up=not launch_only,
                        skipped=sorted(exercised_before[index]),
                        banner=None if index == 0 else Banner(),
                        standing=launch.standing_grants(),
                    )
                ]
                if inside
                else report_requirements(
                    Manifest.across(
                        [
                            composition.recipe.source.requirements
                            for composition in compositions
                        ]
                    ),
                    project_root(),
                    setting_up=not launch_only,
                    standing=launch.standing_grants(),
                )
            )
        if not findings:
            typer.echo(
                "No container requirements selected."
                if inside
                else "No host requirements selected."
            )
            return
        if any(
            not finding.working and finding.requirement.absence.costly()
            for finding in findings
        ):
            raise typer.Exit(1)

    @app.command("binds")
    def binds_command() -> None:
        """Check, inside a session, that every read-only bind it launched with holds.

        Compares the paths the launch recorded as read-only against this
        container's mount table. A host-side git write that replaces a file
        bound read-only, such as a config rewrite, detaches that bind. Exits
        nonzero naming each path no longer mounted read-only; relaunch the
        session to restore it.
        """
        recorded = boundary_description(project_root())
        expected = recorded["read_only"] if "read_only" in recorded else []
        if not expected:
            typer.echo(
                "No read-only binds are recorded: this is not a contained session."
            )
            return
        missing = unheld(expected)
        if not missing:
            typer.echo(f"All {len(expected)} read-only binds are in place.")
            return
        refuse(
            "not mounted read-only any more, so writable from this session",
            what=", ".join(missing),
            steps=[step("relaunch the session to restore them")],
        )

    @app.command("sandbox-check")
    def sandbox_check_command(
        image: Annotated[
            str | None, typer.Option(help="Sandbox image; defaults to the library's")
        ] = None,
    ) -> None:
        """Evaluate arithmetic in a disposable Python sandbox without network access."""
        from lup.devtools.harness.sandbox import check_sandbox

        outcome = check_sandbox(image)
        typer.echo(outcome.detail, err=not outcome.proved)
        if not outcome.proved:
            raise typer.Exit(1)

    @app.command("image")
    def image_command(
        target: Annotated[str, typer.Argument(help=selector)] = targets.every,
        prune: Annotated[
            bool,
            typer.Option(
                "--prune", help="Remove images no checkout is pointing at any more"
            ),
        ] = False,
    ) -> None:
        """Render the container image this project's sessions run in.

        Printed rather than written, because a Dockerfile on disk is a second
        place the toolchain is stated and the first thing to drift from the
        declaration. A build reads this on stdin -- ``harness image | docker
        build -f - .`` -- so what is built is what is declared, every time,
        with nothing in between to edit.

        ``--prune`` sweeps instead of printing. An image is named after the
        declaration it was built from, which is what lets two checkouts share
        one -- and means editing the declaration leaves the old image
        standing rather than replacing it. What goes is every digest tag with
        no checkout tag on it; what stays is anything a checkout still points
        at, and the image this declaration would build right now.
        """

        def pinned(image: Image) -> Image:
            # Everything resolution says goes to stderr: stdout is the
            # Dockerfile a build reads, and a progress line in the pipe is a
            # parse error inside `docker build -f -`.
            resolution = resolved_agent_clis(
                image, say=lambda notice: typer.echo(notice.painted(), err=True)
            )
            for notice in resolution.said:
                typer.echo(notice.painted(), err=True)
            return resolution.image

        # One resolution per distinct image declaration and one Dockerfile per
        # distinct rendering, however many runtimes compose against them:
        # every runtime here shares one image, so the default target rendered
        # it once per runtime, asked the registry as often, and handed a build
        # reading stdin two files.
        sources = {
            composition.recipe.source.image.model_dump_json()
            + composition.recipe.source.requirements.model_dump_json(): (
                composition.recipe.source
            )
            for composition in targets.resolve(target, project_root())
        }
        images = {
            key: pinned(image)
            for key, image in {
                source.image.model_dump_json(): source.image
                for source in sources.values()
            }.items()
        }
        renderings = dict.fromkeys(
            images[source.image.model_dump_json()].dockerfile(source.requirements)
            for source in sources.values()
        )
        for rendered in renderings:
            if not prune:
                typer.echo(rendered)
                continue
            client = detected_client()
            if client is None:
                typer.echo("No container client answered, so nothing was swept.")
                return
            finished = superseded_images(client.engine(), image_tag(rendered))
            if not finished:
                typer.echo("Nothing superseded — every image has a checkout on it.")
                return
            for tag in retire_images(finished, client.engine()):
                typer.echo(f"removed {tag}")

    @app.command("clean")
    def clean_command(
        yes: Annotated[
            bool,
            typer.Option(
                "--yes", help="Remove what nothing points at, not just list it"
            ),
        ] = False,
    ) -> None:
        """List everything lup keeps for sessions, and what nothing points at.

        Images, volumes, project environments, held Codex revisions, egress
        proxies and each checkout's runtime homes, with its size and what
        points at it. A dry run unless ``--yes``: then this repository's old
        shared config home is split into one per runtime, and every image no
        checkout points at, every environment and every home whose checkout
        is gone, every Codex revision no running container binds, every
        stopped proxy and every sandbox workspace no container holds is
        removed. A repository's own config home is never removed here.
        """
        root = project_root()
        compositions = targets.resolve(targets.every, root)
        image = compositions[0].recipe.source.image
        client = detected_client()
        engine = client.engine() if client is not None else None
        helper = (
            HomeHelper(
                engine=engine,
                tag=checkout_tag(root),
                uid=root.stat().st_uid,
                gid=root.stat().st_gid,
                config_home=image.config_home,
            )
            if engine is not None
            else None
        )
        logins = runtime_logins()
        kept = clean.Kept(SupersededFile(), kept_for_superseded(), datetime.now(UTC))
        held = clean.inventory(root, image, engine, logins, helper, kept)
        for line in clean.listing(held, engine):
            typer.echo(line)
        finished = [item for item in held if item.finished]
        if not yes:
            typer.echo(
                f"{len(finished)} finished; `harness clean --yes` removes them."
                if finished
                else "Nothing is finished."
            )
            return
        for notice in clean.cleaned(root, held, engine, logins, helper, kept):
            typer.echo(notice.text)

    @app.command("egress")
    def egress_command(
        target: Annotated[str, typer.Argument(help=selector)] = targets.every,
        down: Annotated[
            bool,
            typer.Option("--down", help="Remove the proxy and its network"),
        ] = False,
    ) -> None:
        """Report or remove the network boundary this project's sessions run behind.

        The proxy outlives a session deliberately -- starting one costs a
        second and every launch would pay it -- which means something has to
        be able to say what is running and take it away. Without that the
        only answer is a raw engine command against a name the operator has
        to know, which is the friction this whole harness exists to remove.
        """
        for composition in targets.resolve(target, project_root()):
            report_egress(composition.recipe.source.image.egress, project_root(), down)

    def selected_target(
        mode: launch.LaunchMode | None,
        runtime: str,
        allowance: int,
        relaxed: bool = False,
    ) -> NativeHarnessComposition:
        """The composition to generate and open, under whichever mode is in force.

        A mode carries its own targets, so this is where "the tree differs by
        mode" actually happens: everything downstream takes an already
        concrete composition and cannot tell which declaration produced it.

        ``relaxed`` is the other thing that differs by launch, and it reaches
        the same place for the same reason: the anti-pattern table is
        projected into each plugin's hermetic edit policy at generation time,
        so a switch that stopped at this command line would leave the session
        judged by the tree it opened against rather than by what was asked
        for.
        """
        source = (mode.targets_at(allowance) if mode is not None else None) or targets
        build = source.builder(runtime)
        if build is None:
            refuse(
                f"declares no {runtime} tree",
                what=f"--mode {mode.name}" if mode is not None else "this project",
                code=2,
            )
        return build(project_root(), every_rule_retired() if relaxed else None)

    def companion_targets(
        mode: launch.LaunchMode | None, runtime: str, allowance: int
    ) -> list[NativeHarnessComposition]:
        """The trees a launch regenerates without opening, which is all the others.

        What makes launching one runtime mean what `generate all` means. A
        shared source moves both trees, so a launcher that generated only its
        own would leave the other stale until somebody ran the selector by
        hand -- surfacing as `dev check` failing on drift the session did not
        introduce.

        Never relaxed. Relaxation is a statement about the session being
        opened, and projecting it into a tree nobody is opening would leave
        that runtime on disk judged by rules its source never declared.
        """
        source = (mode.targets_at(allowance) if mode is not None else None) or targets
        return [
            composition
            for composition in source.resolve(source.every, project_root())
            if composition.recipe.label != runtime
        ]

    def launch_help(subject: str) -> str:
        """One launcher's help, with whatever modes this project declares."""
        return f"{subject}{launch.modes_help(modes)}"

    claude_target = targets.builder("claude")
    if claude_target is not None:
        app.add_typer(create_profile_app(directory), name="profile")

        @app.command(
            "claude",
            context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
            help=launch_help(
                "Generate/reconcile Claude artifacts and launch the verified plugin."
            ),
        )
        def claude(
            ctx: typer.Context,
            profile: Annotated[
                str | None,
                typer.Option(
                    "--profile",
                    "-p",
                    help="Account profile under ~/.config/lup/profiles; "
                    "default: the one config.toml selects",
                ),
            ] = None,
            model: Annotated[
                str | None,
                typer.Option(
                    "--model",
                    "-m",
                    help="Native model override; default: the tier in "
                    "~/.config/lup/config.toml, strongest unless it names another",
                ),
            ] = None,
            effort: Annotated[
                str | None,
                typer.Option(
                    "--effort",
                    help="Reasoning effort: low, medium, high, xhigh, max, or "
                    "ultra (xhigh with ultracode on); refused where the "
                    "model's catalog row lacks it. Default: the effort in "
                    "~/.config/lup/config.toml, else xhigh, stepped down to a "
                    "rung the row takes",
                ),
            ] = None,
            generate_only: Annotated[
                bool,
                typer.Option(
                    "--generate-only",
                    help="Generate every tree and ready the home a host session "
                    "opens against, without launching",
                ),
            ] = False,
            continue_latest: Annotated[
                bool,
                typer.Option(
                    "--continue", "-c", help="Reopen the most recent session here"
                ),
            ] = False,
            resume: Annotated[
                bool,
                typer.Option("--resume", help="Pick a session to reopen"),
            ] = False,
            session: Annotated[
                str | None,
                typer.Option("--session", help="Reopen this session by id"),
            ] = None,
            ignore_antipatterns: Annotated[
                bool,
                typer.Option(
                    "--ignore-antipatterns",
                    help="Open a session the anti-pattern gate leaves alone",
                ),
            ] = False,
            sandbox: Annotated[
                LaunchSandbox | None,
                typer.Option(
                    "--sandbox",
                    help="Which sandbox holds the session: the verified "
                    "container (outer), the runtime's own on the host "
                    "(inner), or the semantic policy alone (none). Defaults "
                    "to outer, or to inner with a warning when no Docker or "
                    "Podman client is found; an explicit outer is refused there",
                ),
            ] = None,
            mount: Annotated[
                list[Path],
                typer.Option(
                    "--mount",
                    exists=True,
                    file_okay=False,
                    resolve_path=True,
                    help="Extra folder this session may read and write "
                    "(repeatable); registered for this launch only",
                ),
            ] = [],
            mount_ro: Annotated[
                list[Path],
                typer.Option(
                    "--mount-ro",
                    exists=True,
                    file_okay=False,
                    resolve_path=True,
                    help="Extra folder this session may read and must not "
                    "write (repeatable)",
                ),
            ] = [],
            device: Annotated[
                list[str],
                typer.Option(
                    "--device",
                    help="Host device this session is granted, by CDI name "
                    "such as nvidia.com/gpu=all (repeatable); for this "
                    "launch only",
                ),
            ] = [],
            sudo: Annotated[
                bool | None,
                typer.Option(
                    "--sudo/--no-sudo",
                    help="Let the contained session become its container's "
                    "root through sudo, on a rootless engine only; default: "
                    "the mode's, your [container] config's, or the project's",
                ),
            ] = None,
            network: Annotated[
                NetworkMode | None,
                typer.Option(
                    "--network",
                    help="The container's network: filtered (behind the "
                    "egress proxy), bridge, host or none; default: the "
                    "mode's, your [container] config's, or the image's",
                ),
            ] = None,
            memory: Annotated[
                str | None,
                typer.Option(
                    "--memory",
                    help="How much memory the container may hold: an amount "
                    "such as 12GiB, or a share such as 75%; default: the "
                    "mode's, your [container] config's, or no limit",
                ),
            ] = None,
            hold_generated: Annotated[
                bool | None,
                typer.Option(
                    "--hold-generated/--release-generated",
                    help="Hold the generated trees the runtime runs from "
                    "read-only in the container, so the session cannot change "
                    "the hooks judging it, and regenerates on the host; "
                    "default: the mode's, your [container] config's, or the "
                    "project's",
                ),
            ] = None,
            max_recursive_agent: Annotated[
                int | None,
                typer.Option(
                    "--max-recursive-agent",
                    min=-1,
                    help="Maximum child-agent depth; -1 allows unlimited recursion",
                ),
            ] = None,
            transcribe_session: Annotated[
                bool,
                typer.Option(
                    "--transcribe-session",
                    help="Mirror the native CLI transcript when this mode disables it",
                ),
            ] = False,
            mode: Annotated[
                str | None,
                typer.Option(
                    "--mode",
                    help="Open a kind of session this project declares, laid "
                    "over its own declaration",
                ),
            ] = None,
            move_sessions: Annotated[
                bool,
                typer.Option(
                    "--move-sessions",
                    help="Hand this profile's login to the repository's "
                    "container volume even where running contained sessions "
                    "use another's, moving each onto it at its next request; "
                    "without it such a launch is refused",
                ),
            ] = False,
        ) -> None:
            selected = launch.selected_mode(modes, mode)
            request = launch.LaunchArguments(
                words=list(ctx.args),
                model=model,
                effort=effort,
                profile=profile,
                resume=Resumption(latest=continue_latest, pick=resume, session=session),
                sandbox=sandbox,
                mounts=mount,
                read_only=mount_ro,
                devices=device,
                sudo=sudo,
                hold_generated=hold_generated,
                network=network,
                memory=launch.memory_limit(memory),
                container=container,
                max_recursive_agent=max_recursive_agent,
                transcribe_session=transcribe_session,
                relaxed=ignore_antipatterns,
                mode=selected,
                recorder=recorder_for("claude"),
                move_sessions=move_sessions,
            )
            allowance = request.allowance("claude")
            launch.launch_claude(
                selected_target(selected, "claude", allowance, ignore_antipatterns),
                request,
                directory,
                generate_only,
                checkpoint=checkpoint,
                companions=companion_targets(selected, "claude", allowance),
                repository_writers=repository_writers,
            )

    codex_target = targets.builder("codex")
    if codex_target is not None:
        codex_plugin = typer.Typer(
            help="Install and verify this project's Codex plugin"
        )
        app.add_typer(codex_plugin, name="codex-plugin")

        @codex_plugin.command("install")
        def install_codex_plugin(
            codex_home: Annotated[
                Path,
                typer.Option("--codex-home", help="The home the runtime will open"),
            ],
            force: Annotated[
                bool,
                typer.Option("--force", help="Reinstall a matching cached revision"),
            ] = False,
            trust_project: Annotated[
                bool,
                typer.Option(
                    "--trust-project", help="Trust this checkout in a launch-owned home"
                ),
            ] = False,
        ) -> None:
            """Install the declared plugin and verify native discovery in the selected home."""
            launch.install_codex_plugin_home(codex_home, force, trust_project)

        codex_home_app = typer.Typer(
            help="The Codex home each checkout's host sessions run in"
        )
        app.add_typer(codex_home_app, name="codex-home")

        @codex_home_app.command("migrate")
        def migrate_codex_home(
            dry_run: Annotated[
                bool,
                typer.Option("--dry-run", help="Say what would move, and move nothing"),
            ] = False,
        ) -> None:
            """Move each worktree's Codex home out of the checkout, into lup's state.

            A home holds a copy of the login, so one inside a checkout made every
            recursive read of the tree walk a credential. Every worktree of this
            repository is moved once; a home lup's state already keeps for that
            checkout is left for a merge by hand.
            """
            refuse_inside_a_container(
                ["harness", "codex-home", "migrate"],
                "lup's state is the container's own rather than the host's",
            )
            moved = launch.move_checkout_codex_homes(dry_run)
            if not moved:
                typer.echo(
                    "No checkout of this repository keeps a Codex home inside it."
                )
                return
            if dry_run:
                typer.echo("Dry run; nothing moved:")
            for line in moved:
                typer.echo(f"  {line}")

        @app.command(
            "codex",
            context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
            help=launch_help(
                "Generate/reconcile Codex artifacts and launch without updating the CLI."
            ),
        )
        def codex(
            ctx: typer.Context,
            codex_home: Annotated[
                Path | None,
                typer.Option(
                    "--codex-home", help="Override the worktree-scoped Codex home"
                ),
            ] = None,
            profile: Annotated[
                str | None,
                typer.Option(
                    "--profile",
                    "-p",
                    help="Account profile under ~/.config/lup/profiles, the "
                    "worktree's Codex home derived from its; default: the one "
                    "config.toml selects",
                ),
            ] = None,
            model: Annotated[
                str | None,
                typer.Option(
                    "--model",
                    "-m",
                    help="Native model override; default: the tier in "
                    "~/.config/lup/config.toml, strongest unless it names "
                    "another",
                ),
            ] = None,
            effort: Annotated[
                str | None,
                typer.Option(
                    "--effort",
                    help="Reasoning effort: low, medium, high, xhigh, max, or "
                    "ultra; refused where the model's catalog row lacks it. "
                    "Default: the effort in ~/.config/lup/config.toml, else "
                    "xhigh, stepped down to a rung the row takes",
                ),
            ] = None,
            generate_only: Annotated[
                bool,
                typer.Option(
                    "--generate-only",
                    help="Generate every tree and ready the home a host session "
                    "opens against, without launching",
                ),
            ] = False,
            force_install: Annotated[
                bool,
                typer.Option(
                    "--force-install",
                    help="Reinstall even when the cached digest matches",
                ),
            ] = False,
            continue_latest: Annotated[
                bool,
                typer.Option(
                    "--continue", "-c", help="Reopen the most recent session here"
                ),
            ] = False,
            resume: Annotated[
                bool,
                typer.Option("--resume", help="Pick a session to reopen"),
            ] = False,
            session: Annotated[
                str | None,
                typer.Option("--session", help="Reopen this session by id"),
            ] = None,
            ignore_antipatterns: Annotated[
                bool,
                typer.Option(
                    "--ignore-antipatterns",
                    help="Open a session the anti-pattern gate leaves alone",
                ),
            ] = False,
            sandbox: Annotated[
                LaunchSandbox | None,
                typer.Option(
                    "--sandbox",
                    help="Which sandbox holds the session: the verified "
                    "container (outer), the runtime's own on the host "
                    "(inner), or the semantic policy alone (none). Defaults "
                    "to outer, or to inner with a warning when no Docker or "
                    "Podman client is found; an explicit outer is refused there",
                ),
            ] = None,
            mount: Annotated[
                list[Path],
                typer.Option(
                    "--mount",
                    exists=True,
                    file_okay=False,
                    resolve_path=True,
                    help="Extra folder this session may read and write "
                    "(repeatable); registered for this launch only",
                ),
            ] = [],
            mount_ro: Annotated[
                list[Path],
                typer.Option(
                    "--mount-ro",
                    exists=True,
                    file_okay=False,
                    resolve_path=True,
                    help="Extra folder this session may read and must not "
                    "write (repeatable)",
                ),
            ] = [],
            device: Annotated[
                list[str],
                typer.Option(
                    "--device",
                    help="Host device this session is granted, by CDI name "
                    "such as nvidia.com/gpu=all (repeatable); for this "
                    "launch only",
                ),
            ] = [],
            sudo: Annotated[
                bool | None,
                typer.Option(
                    "--sudo/--no-sudo",
                    help="Let the contained session become its container's "
                    "root through sudo, on a rootless engine only; default: "
                    "the mode's, your [container] config's, or the project's",
                ),
            ] = None,
            network: Annotated[
                NetworkMode | None,
                typer.Option(
                    "--network",
                    help="The container's network: filtered (behind the "
                    "egress proxy), bridge, host or none; default: the "
                    "mode's, your [container] config's, or the image's",
                ),
            ] = None,
            memory: Annotated[
                str | None,
                typer.Option(
                    "--memory",
                    help="How much memory the container may hold: an amount "
                    "such as 12GiB, or a share such as 75%; default: the "
                    "mode's, your [container] config's, or no limit",
                ),
            ] = None,
            hold_generated: Annotated[
                bool | None,
                typer.Option(
                    "--hold-generated/--release-generated",
                    help="Hold the generated trees the runtime runs from "
                    "read-only in the container, so the session cannot change "
                    "the hooks judging it, and regenerates on the host; "
                    "default: the mode's, your [container] config's, or the "
                    "project's",
                ),
            ] = None,
            max_recursive_agent: Annotated[
                int | None,
                typer.Option(
                    "--max-recursive-agent",
                    min=-1,
                    help="Maximum child-agent depth; -1 allows unlimited recursion",
                ),
            ] = None,
            transcribe_session: Annotated[
                bool,
                typer.Option(
                    "--transcribe-session",
                    help="Mirror the native CLI transcript when this mode disables it",
                ),
            ] = False,
            mode: Annotated[
                str | None,
                typer.Option(
                    "--mode",
                    help="Open a kind of session this project declares, laid "
                    "over its own declaration",
                ),
            ] = None,
            move_sessions: Annotated[
                bool,
                typer.Option(
                    "--move-sessions",
                    help="Hand this profile's login to the repository's "
                    "container volume even where running contained sessions "
                    "use another's; each keeps its own until it is opened "
                    "again. Without it such a launch is refused",
                ),
            ] = False,
        ) -> None:
            selected = launch.selected_mode(modes, mode)
            request = launch.LaunchArguments(
                words=list(ctx.args),
                model=model,
                effort=effort,
                profile=profile,
                resume=Resumption(latest=continue_latest, pick=resume, session=session),
                sandbox=sandbox,
                mounts=mount,
                read_only=mount_ro,
                devices=device,
                sudo=sudo,
                hold_generated=hold_generated,
                network=network,
                memory=launch.memory_limit(memory),
                container=container,
                max_recursive_agent=max_recursive_agent,
                transcribe_session=transcribe_session,
                relaxed=ignore_antipatterns,
                mode=selected,
                recorder=recorder_for("codex"),
                move_sessions=move_sessions,
            )
            allowance = request.allowance("codex")
            launch.launch_codex(
                selected_target(selected, "codex", allowance, ignore_antipatterns),
                request,
                codex_home,
                generate_only,
                force_install,
                checkpoint=checkpoint,
                companions=companion_targets(selected, "codex", allowance),
                repository_writers=repository_writers,
            )

    return app
