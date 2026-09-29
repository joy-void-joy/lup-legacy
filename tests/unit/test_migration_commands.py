"""A command a migration hands an adopter is one this CLI accepts as written.

A step's command is the one part of a declaration somebody runs without
reading, so a command missing a required argument or spelling a pattern for
a search that takes it literally fails exactly where it was meant to save the
reading. Each is parsed here against the composed CLI — arguments and all,
without running anything — so a declaration cannot ship one that only looks
like a command.
"""

from collections.abc import Sequence

import typer
from typer._click.core import Command as ClickCommand
from typer._click.exceptions import UsageError
from typer.core import TyperGroup

from lup.devtools.dev.migrations import MigrationRecord
from lup_template.devtools.main import app

DEVTOOLS = ("uv", "run", "lup-devtools")


def refusal(command: ClickCommand, words: Sequence[str]) -> str:
    """Why ``words`` do not parse under ``command``, or nothing where they do."""
    match command, words:
        case TyperGroup(), [name, *rest] if name in command.commands:
            return refusal(command.commands[name], rest)
        case TyperGroup(), _ if not command.invoke_without_command:
            return f"{' '.join(words) or 'nothing'} names no command of the group"
        case _:
            try:
                command.make_context(command.name, list(words))
            except UsageError as refused:
                return refused.format_message()
            return ""


def test_every_declared_devtools_command_parses_as_written() -> None:
    root = typer.main.get_command(app)
    refused = {
        " ".join(step.command): reason
        for migration in MigrationRecord().declared()
        for step in migration.steps
        if tuple(step.command[: len(DEVTOOLS)]) == DEVTOOLS
        if (reason := refusal(root, step.command[len(DEVTOOLS) :]))
    }

    assert refused == {}


def test_a_command_missing_its_required_paths_is_refused() -> None:
    """The shape that shipped: `dev py text` given a pattern and no path."""
    root = typer.main.get_command(app)

    assert refusal(root, ["dev", "py", "text", "store_exposure"])
    assert not refusal(root, ["dev", "py", "text", "store_exposure", "."])
