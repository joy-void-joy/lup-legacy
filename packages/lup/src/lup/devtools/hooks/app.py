"""Ask a project's permission policy what it thinks, without running it.

Tuning a vocabulary you cannot query is guesswork. Before this, finding out
whether a command asks meant adding a fixture to the semantic-policy suite and
running the file — so nobody swept the everyday commands, and each one earned
its allowance only after annoying somebody enough to be reported. The same
question is also what an agent wants at the moment it is stopped, where the
reason string alone leaves it guessing which clause it tripped.

The policy read here is the declared one — the same ``HookSet`` the generated
plugins are compiled from — so an answer here is the answer a session gets.
That declaration is the one fact this tree cannot work out alone, and it
arrives as a callable read when a command runs rather than when the CLI is
composed, the way the rest of the dev tooling takes what a repository knows
about itself.
"""

import sys
from collections.abc import Callable
from pathlib import Path
from typing import Annotated

import typer

from pydantic import AnyHttpUrl

from lup.devtools.hooks.approvals import forget, remembered
from lup.devtools.hooks.classify import shell_decision as classify_shell
from lup.devtools.hooks.corpus import read_corpus
from lup.harness.enforcement import declared_path_rules, semantic_policy_for
from lup.harness.models import HookSet
from lup.policy.everyday import SESSION_SHAPES, SessionShape
from lup.policy.foreign import foreign_warnings
from lup.policy.models import Decision, FetchUrl
from lup.workspace.paths import project_root
from lup.devtools.utils import output_json


def input_text(file: Path) -> str:
    """A named file's text, or stdin's where the path is ``-``.

    The dash is the spelling every shell already reads as "the stream rather
    than a name", so a caller holding text and a caller holding a file reach
    the same reader without either of them learning a second option.
    """
    return sys.stdin.read() if str(file) == "-" else file.read_text(encoding="utf-8")


def command_text(command: str | None, file: Path | None) -> str:
    """One command's text, taken from an argument or from a file.

    A command carrying a newline cannot arrive as an argument. An escalation
    marker has to be the command's leading line, so asking about such a
    command from an argument means spelling the marker inside the shell line
    carrying the question -- where the gate in front of that line reads it and
    answers the marker instead of the question. A path keeps the text out of
    the asking line entirely.

    Exactly one of the two, because a command supplied twice has no answer to
    which spelling was meant, and neither leaves nothing to classify.
    """
    if command is not None and file is None:
        return command
    if file is not None and command is None:
        return input_text(file)
    raise typer.BadParameter(
        "name the command once: as an argument, or with --file (`-` for stdin)"
    )


def report(
    subject: str, decision: Decision, as_json: bool, warnings: list[str] | None = None
) -> None:
    """Print one verdict, and exit non-zero on anything but an allow.

    The exit code is what makes this usable from a sweep: a batch of commands
    a project expects to pass fails the run when one of them stops passing.

    ``warnings`` carry gates that are not this policy's, and they deliberately
    reach neither the effect nor the exit code. This command answers what lup
    decides; a runtime's own rail is somebody else's code on somebody else's
    release schedule, so folding it into the verdict would be refusing work in
    lup's name for a gate lup does not own — and a sweep would start failing
    on the day upstream changed a token set. Said beside the verdict instead,
    where a reader who is about to run the command sees it and a reader
    checking the policy is not misled about whose refusal it is.
    """
    if as_json:
        output_json(
            {"subject": subject, **decision.model_dump(), "warnings": warnings or []}
        )
    else:
        typer.echo(f"{decision.effect:>5}  {subject}")
        match decision.sandbox:
            case "ambient":
                pass
            case placement:
                typer.echo(f"       runs {placement} the sandbox")
        if decision.reason:
            typer.echo(f"       {decision.reason}")
        for said in warnings or []:
            typer.echo(f"       warning: {said}")
    if decision.effect != "allow":
        raise typer.Exit(1)


def create_hooks_app(declared: Callable[[], HookSet]) -> typer.Typer:
    """Wire the policy-query tree over whichever declaration a project holds."""
    app = typer.Typer(help="Query the permission policy", no_args_is_help=True)

    def shell_decision(
        command: str,
        autonomous: bool,
        interactive: bool,
        trapped: bool = False,
        contained: bool = False,
    ) -> Decision:
        """This project's declaration, taken through the shared classifier.

        Which the everyday sweep in `dev check` also takes: two callers asking
        what a command earns must not answer it differently, or the sweep is
        checking a policy nobody runs.
        """
        return classify_shell(
            declared(), command, autonomous, interactive, trapped, contained
        )

    @app.command("classify")
    def classify_command(
        command: Annotated[
            str | None, typer.Argument(help="The shell command to classify")
        ] = None,
        file: Annotated[
            Path | None,
            typer.Option(
                "--file",
                help="Read the command from this file, or `-` for stdin",
            ),
        ] = None,
        autonomous: Annotated[
            bool,
            typer.Option("--autonomous", help="Judge as a reviewed worker session"),
        ] = False,
        headless: Annotated[
            bool,
            typer.Option("--headless", help="Judge with no human to answer an ask"),
        ] = False,
        trapped: Annotated[
            bool,
            typer.Option(
                "--trapped",
                help="Judge as a confined session whose runtime cannot escape",
            ),
        ] = False,
        outer: Annotated[
            bool,
            typer.Option(
                "--outer",
                help="Judge as a session inside the container `outer` measures",
            ),
        ] = False,
        as_json: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
    ) -> None:
        """Say what the policy decides about one shell command, and why.

        A verdict is this policy's. Beneath it may sit a warning about a gate
        that is not — a runtime rail this project can recognise and neither
        predict nor lift. It never moves the effect or the exit code.

        A command spanning more than one line is asked about with ``--file``,
        which is the only route an escalation marker survives: the marker has
        to lead the command, and a marker spelled inside this asking line is
        read by the gate in front of it rather than by the policy being asked.

        Examples::

            $ uv run lup-devtools dev hooks classify 'gh api /repos/o/r/pulls/1'
            $ uv run lup-devtools dev hooks classify 'rm build/out' --json
            $ uv run lup-devtools dev hooks classify 'uv run lup-devtools dev check' --trapped
            $ uv run lup-devtools dev hooks classify --file tmp/escalated.sh
            $ some-writer | uv run lup-devtools dev hooks classify --file -
        """
        asked = command_text(command, file)
        report(
            asked,
            shell_decision(asked, autonomous, not headless, trapped, outer),
            as_json,
            foreign_warnings(asked),
        )

    @app.command("classify-fetch")
    def classify_fetch(
        url: Annotated[str, typer.Argument(help="The URL to classify")],
        as_json: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
    ) -> None:
        """Say whether a URL is inside this project's declared fetch scopes."""
        policy = semantic_policy_for(declared())
        report(url, policy.decide(FetchUrl(url=AnyHttpUrl(url))), as_json)

    @app.command("sweep")
    def sweep_commands(
        file: Annotated[
            Path | None,
            typer.Argument(
                help=(
                    "A file of commands, one per line, or `-` for stdin; "
                    "omit for the declared corpus"
                )
            ),
        ] = None,
        autonomous: Annotated[
            bool,
            typer.Option("--autonomous", help="Judge as a reviewed worker session"),
        ] = False,
        headless: Annotated[
            bool,
            typer.Option("--headless", help="Judge with no human to answer an ask"),
        ] = False,
        trapped: Annotated[
            bool,
            typer.Option(
                "--trapped",
                help="Judge as a confined session whose runtime cannot escape",
            ),
        ] = False,
        outer: Annotated[
            bool,
            typer.Option(
                "--outer",
                help="Judge as a session inside the container `outer` measures",
            ),
        ] = False,
        as_json: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
    ) -> None:
        """Classify a list of commands at once, and exit non-zero if any is not allowed.

        This is the shape a change to the vocabulary should be checked with: a
        rule that tightens something it did not mean to tighten fails here
        instead of in somebody's session.

        With no file, the list is the one this project declared as its
        everyday commands — the same list `dev check` sweeps, in the same
        postures, so a failure there is reproduced here with the reason each
        command was stopped for. A file is for a question this project has not
        settled: a candidate corpus, or the commands a recorded session was
        interrupted about.

        The declared corpus is swept in every posture a session runs in,
        because it asserts that these commands allow and a verdict is only
        ever reached for somebody. Naming one with a flag asks about that one
        instead, which is also what a file gets: a question is asked from
        somewhere, and the flags say where.

        Examples::

            $ uv run lup-devtools dev hooks sweep
            $ uv run lup-devtools dev hooks sweep --autonomous --headless
            $ uv run lup-devtools dev hooks sweep tmp/recorded_asks.txt
            $ some-writer | uv run lup-devtools dev hooks sweep -
        """
        commands = (
            [
                command
                for family in declared().everyday_commands
                for command in family.commands
            ]
            if file is None
            else [
                line.strip()
                for line in input_text(file).splitlines()
                if line.strip() and not line.startswith("#")
            ]
        )
        shapes = (
            list(SESSION_SHAPES)
            if file is None and not (autonomous or headless or trapped or outer)
            else [
                SessionShape(
                    what=", ".join(
                        [
                            "worker" if autonomous else "attended",
                            *(["headless"] if headless else []),
                            *(["inner"] if trapped else []),
                            *(["outer"] if outer else []),
                        ]
                    ),
                    autonomous=autonomous,
                    interactive=not headless,
                    trapped=trapped,
                    contained=outer,
                )
            ]
        )
        decisions = [
            (
                shape,
                command,
                shell_decision(
                    command,
                    shape.autonomous,
                    shape.interactive,
                    shape.trapped,
                    shape.contained,
                ),
            )
            for shape in shapes
            for command in commands
        ]
        if as_json:
            output_json(
                [
                    {"subject": command, "shape": shape.what, **decision.model_dump()}
                    for shape, command, decision in decisions
                ]
            )
        else:
            for shape, command, decision in decisions:
                posture = f"  {shape.what}:" if len(shapes) > 1 else ""
                typer.echo(f"{decision.effect:>5}{posture}  {command}")
        if any(decision.effect != "allow" for _, _, decision in decisions):
            raise typer.Exit(1)

    @app.command("roots")
    def show_roots(
        as_json: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
    ) -> None:
        """List the path roles and protected roots the declaration carries.

        What a verdict on a path depends on, in one place — so "why did that
        ask?" is answerable without reading the catalog.
        """
        hooks = declared()
        rules = declared_path_rules(hooks)
        if as_json:
            output_json(
                {
                    "roles": [
                        role.model_dump(mode="json") for role in hooks.path_roles
                    ],
                    "protected": [str(root) for root in hooks.protected_edit_roots],
                    "human_owned": [str(path) for path in hooks.human_owned_files],
                    "rules": len(rules),
                }
            )
            return
        for role in hooks.path_roles:
            typer.echo(f"{role.role:>10}  {role.root}")
        for root in hooks.protected_edit_roots:
            typer.echo(f"{'protected':>10}  {root}")
        for path in hooks.human_owned_files:
            typer.echo(f"{'human':>10}  {path}")

    @app.command("learn")
    def learn_command(
        as_json: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
    ) -> None:
        """Review the commands the policy declined to interrupt about.

        The log half of allow-and-log. A deferral hands the call to the
        runtime's own gate and its reason reaches no human, so this is where
        it is read back.

        Two lists, and only the first is asking for anything. **Gaps** are
        commands nobody has ever judged, which a boundary carried rather than
        a rule -- each is a candidate for a row in the shell vocabulary.
        **Settled** are commands a rule judged and the boundary answered for,
        which is the relaxation working; read them to check it is letting
        through what you meant.

        Nothing here writes a rule, and the refusal is the point: from one
        deferred `ruff check .`, a row of `ruff` permits `ruff format --write`
        forever and a row of `ruff check` permits `ruff check --fix`; the same
        mechanism over `rm tmp/scratch` permits `rm -rf`. What separates them
        is the judgement you are here to make.
        """
        corpus = read_corpus(project_root())
        if as_json:
            output_json(corpus.model_dump(mode="json"))
            return
        gaps = corpus.gaps()
        settled = corpus.settled()
        if not corpus.deferrals:
            typer.echo("Nothing deferred yet — no command has reached the runtime.")
            return
        typer.echo(f"{len(gaps)} unjudged — candidates for a vocabulary row:")
        for item in gaps:
            typer.echo(f"  {item.command}")
            typer.echo(f"      {item.reason}")
        typer.echo(f"\n{len(settled)} settled by the boundary — the audit trail:")
        for item in settled:
            typer.echo(f"  {item.command}")

    @app.command("approvals")
    def approvals_command(
        as_json: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
    ) -> None:
        """List execution observations, including unverified historical approvals.

        These records grant no reusable authority. Explicit, single-use answers
        and their disposition are shown by ``dev questions``.
        """
        held = remembered(project_root())
        if as_json:
            output_json([item.model_dump() for item in held])
            return
        if not held:
            typer.echo("No execution observations recorded.")
            return
        typer.echo("Execution observations only; historical approvals are unverified.")
        for item in held:
            typer.echo(
                f"{item.fingerprint[:12]}  {item.at[:10]}  {item.kind}  {item.subject}"
            )
            typer.echo(f"              from {item.cwd}")

    @app.command("forget")
    def forget_command(
        selector: Annotated[
            str,
            typer.Argument(
                help="A fingerprint prefix as `approvals` prints it, or the exact "
                "command or URL"
            ),
        ],
    ) -> None:
        """Retire an execution observation without changing authorization."""
        gone = forget(project_root(), selector)
        if not gone:
            typer.echo(f"Nothing remembered matches {selector!r}.", err=True)
            raise typer.Exit(1)
        for item in gone:
            typer.echo(f"forgotten {item.fingerprint[:12]}  {item.subject}")

    return app
