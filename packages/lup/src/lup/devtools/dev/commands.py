"""Generate the checked-in command reference by walking the composed CLI.

Which commands a project's tooling serves is a fact about the wired app, and
prose naming a subset of them is a claim nothing reads the app to check. The
subset drifts in one direction only: a command added here is invisible until
somebody remembers the page, so the ones missing are always the newest.

Walked rather than declared, for the reason the layout diagram is walked: a
roster of names beside the CLI is a second place they live, and the copy that
falls behind looks exactly like the copy that has not. A command that is
renamed, added, or removed changes this page by being renamed, added, or
removed.

The walk takes the app as an argument rather than importing one. This library
serves whichever CLI an adopter composed, and the module that composes it is a
root nothing imports — naming it here would both pick one project's CLI and
close the import loop the harness content is kept out of.
"""

from collections.abc import Iterator
from itertools import takewhile
from pathlib import Path

import typer
from pydantic import BaseModel

# Typer vendors its own copy of Click, and `get_command` hands back that copy's
# classes rather than the installed `click` package's. Walking what it actually
# returns means naming the vendored module, private prefix and all: the public
# alternative is to match on a type these objects are not.
from typer._click.core import Command as ClickCommand
from typer.core import TyperGroup

from lup.devtools.dev.documented import generated_files, refuse_unresolved_commands
import lup.harness.models as models
from lup.providers.harness import claude_prompt_renderer
from lup.formats.banner import GeneratedBanner
from lup.harness.materialization import write_generated_file
from lup.harness.models import Artifact
from lup.formats.markdown import CodeCell, PlainCell
from lup.workspace.paths import project_root

# lup: ignore[constant-declaration] — one generated artifact's identity: the
# writer, the docs index, and the banner must all name the same file
COMMAND_REFERENCE = "docs/commands.md"
"""Where the generated command reference lands, relative to a checkout."""

COMMAND_REFERENCE_PATH = Path(COMMAND_REFERENCE)
"""The same path as the writer takes it, so neither can name another file."""

# lup: ignore[constant-declaration] — the command a reader types, whose words
# are the CLI's own rather than a preference this module holds
COMMAND_REFERENCE_COMMAND = "uv run lup-devtools harness generate all"
"""What regenerates this page, as its banner tells a reader to run."""

# lup: ignore[constant-declaration] — this page's own opening prose, which is
# the document the module renders rather than an input a caller supplies
INTRODUCTION = (
    "# Command reference\n\n"
    "Every command `lup-devtools` serves, walked from the composed CLI at "
    "generation time. A command reaches this page by existing, so nothing is "
    "left out for want of being remembered — including the ones a session "
    "rarely runs directly, which are exactly the ones a hand-written list "
    "loses first.\n\n"
    "Run any of them with `uv run lup-devtools <command>`, and add `--help` "
    "for its arguments and options: the summary here is the first line of "
    "each command's own documentation, not a substitute for reading it.\n\n"
)


def summarized(command: ClickCommand) -> str:
    """A command's first documented line, flattened to fit one table cell.

    The short help when a command declares one, and otherwise the opening
    paragraph of its docstring — the paragraph rather than its first line,
    because a summary long enough to wrap is still one sentence and cutting
    it at the wrap ends the cell mid-clause.
    """
    documented = command.short_help or command.help or ""
    # lup: ignore[string-split] — a docstring is prose, and the blank line
    # between its summary and its body is a convention no parser owns
    opening = documented.strip().split("\n\n")[0]
    return " ".join(opening.split())


class CommandEntry(BaseModel, frozen=True):
    """One command the CLI serves, at the path a reader types to reach it."""

    path: list[str]
    summary: str

    def spelled(self) -> str:
        """The command as it is typed, without the executable in front."""
        return " ".join(self.path)

    def group(self) -> str:
        """The sub-app this command belongs to, or its own name at the top."""
        return self.path[0]

    @classmethod
    def beneath(
        cls, command: ClickCommand, path: list[str]
    ) -> Iterator["CommandEntry"]:
        """Every command under ``command`` that runs, in the order ``--help`` shows.

        Depth first over the group tree, so a sub-app's own sub-app is reached
        at the depth a reader types rather than flattened into its parent. A
        group is one of them where typing it runs its callback — a page
        mounted as a sub-app with no subcommand at all, or a wizard whose
        steps are subcommands too — and not where it only prints its help.
        """
        match command:
            case TyperGroup():
                if path and command.invoke_without_command:
                    yield cls(path=path, summary=summarized(command))
                for name, child in command.commands.items():
                    if not child.hidden:
                        yield from cls.beneath(child, [*path, name])
            case _:
                yield cls(path=path, summary=summarized(command))

    @classmethod
    def served_by(cls, app: typer.Typer) -> list["CommandEntry"]:
        """Every command the composed ``app`` answers to, hidden ones aside."""
        return list(cls.beneath(typer.main.get_command(app), []))


class ReachedCommand(BaseModel, frozen=True):
    """One path the walk reached, and whether typing it alone runs anything."""

    path: tuple[str, ...]
    runs_alone: bool


class CommandSurface(BaseModel, frozen=True):
    """Every path this CLI answers to, for judging one somebody wrote down.

    The reference page needs the leaves and nothing else. Deciding whether a
    written command is real needs more: `dev report` is a group that runs,
    `dev` is a group that only leads somewhere, and `dev pyinfo` is neither
    while sharing a prefix with both. A reader that tested prefixes at every
    depth called the third one reachable, because `dev` resolves — so the walk
    here is greedy and the answer is whatever it lands on.

    Read off the app rather than declared. Whether a group runs without a
    subcommand is a fact about how it was mounted, and a second list naming
    the ones that do would be right until somebody mounted another.
    """

    reached: list[ReachedCommand]
    """Every path the walk found, each saying whether it runs on its own."""

    @classmethod
    def beneath(
        cls, command: ClickCommand, path: list[str]
    ) -> Iterator[ReachedCommand]:
        """Each path under ``command``, saying whether it runs on its own."""
        match command:
            case TyperGroup():
                if path:
                    yield ReachedCommand(
                        path=tuple(path),
                        runs_alone=bool(command.invoke_without_command),
                    )
                for name, child in command.commands.items():
                    if not child.hidden:
                        yield from cls.beneath(child, [*path, name])
            case _:
                yield ReachedCommand(path=tuple(path), runs_alone=True)

    @classmethod
    def of(cls, app: typer.Typer) -> "CommandSurface":
        """The surface the composed ``app`` presents, hidden commands aside."""
        return cls(reached=list(cls.beneath(typer.main.get_command(app), [])))

    def admits(self, words: list[str]) -> bool:
        """Whether every word that could name a command actually names one.

        The question prose asks, and it has to survive two shapes that are not
        errors. A group named as a family reaches no command of its own —
        `git pr` in a grant pattern, `setup conversation <provider>` with the
        provider left to the reader — and an argument can be spelled exactly
        like a command name: `harness generate all` ends in one, and `all` is
        nobody's subcommand. So the walk takes the longest prefix the tree
        knows, and what is left over is admitted only where the walk landed
        somewhere that runs — because past a runnable command, every further
        word is an argument to it.

        `dev pyinfo` survives neither: the walk reaches `dev`, which does not
        run alone, and `pyinfo` is nobody's child.

        The cost is stated rather than hidden. A group that *does* run alone
        swallows whatever follows it, so `setup nonesuch` reads as `setup`
        with an argument. Telling those apart means knowing each command's
        arguments, which is a second grammar to keep true; the words that name
        nothing at all are the ones that were actually wrong.
        """
        named = {
            item.path[:depth]
            for item in self.reached
            for depth in range(1, len(item.path) + 1)
        }
        consumed = takewhile(
            lambda depth: tuple(words[:depth]) in named, range(1, len(words) + 1)
        )
        landed = max(consumed, default=0)
        return landed == len(words) or self.runs(words[:landed])

    def runs(self, path: list[str]) -> bool:
        """Whether typing exactly this path runs something.

        The stricter question, which a declaration asks: a construct naming a
        command is issuing an instruction, so a group answering only with its
        own help is not an answer.
        """
        return any(
            item.path == tuple(path) and item.runs_alone for item in self.reached
        )


class CommandGroup(BaseModel, frozen=True):
    """One sub-app's commands, under the name the CLI mounts them at."""

    name: str
    commands: list[CommandEntry]

    def heading(self, first: bool) -> str:
        """This group's heading, spaced off the table above it unless it leads."""
        return f"## `{self.name}`\n\n" if first else f"\n## `{self.name}`\n\n"

    def table(self) -> models.MarkdownTable:
        """This group as the two-column table the page renders it as."""
        return models.MarkdownTable(
            headers=["Command", "What it does"],
            rows=[
                [CodeCell(text=entry.spelled()), PlainCell(text=entry.summary)]
                for entry in self.commands
            ],
        )

    @classmethod
    def over(cls, entries: list[CommandEntry]) -> list["CommandGroup"]:
        """The entries per sub-app, each group in the CLI's own order."""
        names = list(dict.fromkeys(entry.group() for entry in entries))
        return [
            cls(name=name, commands=[e for e in entries if e.group() == name])
            for name in names
        ]


def command_reference_document(app: typer.Typer) -> models.PromptDocument:
    """Every command the CLI serves, one table per sub-app.

    The blank line between groups rides on the *next* heading rather than
    trailing its table, because a table already ends in its own newline and
    the document has to end in exactly one.
    """
    groups = CommandGroup.over(CommandEntry.served_by(app))
    return models.PromptDocument(
        source=__name__,
        parts=[
            models.TextPart(text=INTRODUCTION),
            *(
                part
                for index, group in enumerate(groups)
                for part in (
                    models.TextPart(text=group.heading(first=index == 0)),
                    group.table(),
                )
            ),
        ],
    )


def command_reference_artifact(app: typer.Typer) -> Artifact:
    """The command reference as one artifact, gated like any generated file.

    Rendered through one runtime's vocabulary because a document has to be
    rendered through some runtime's, and this one names none: it is prose and
    tables end to end, so either vocabulary produces the same bytes.
    """
    document = command_reference_document(app)
    return Artifact.generated(
        path=COMMAND_REFERENCE_PATH,
        body=claude_prompt_renderer().render(document),
        semantic_id="docs.commands",
        banner=GeneratedBanner(
            source=document.declared_source(), command=COMMAND_REFERENCE_COMMAND
        ),
    )


def write_command_reference(
    app: typer.Typer,
    root: Path | None = None,
    *,
    check: bool = False,
    declined: list[str] | None = None,
) -> Path:
    """Write or verify the generated command reference, and what names a command.

    The page and the sweep run together because they are one question asked
    twice. This writer is where the whole CLI is in scope, and it runs after
    every tree has been rendered — so the artifacts a session will read are on
    disk by the time their commands are resolved against the app that serves
    them. A miss raises rather than reports: generation is where a document is
    being *made*, and a document is not finished while it tells its reader to
    run something that does not exist.

    *declined* are the command trees of modules this project did not take,
    which its hand-written files may still name — see
    :func:`~lup.devtools.dev.documented.unresolved`.
    """
    checkout = root or project_root()
    written = write_generated_file(
        command_reference_artifact(app),
        checkout,
        COMMAND_REFERENCE_COMMAND,
        check=check,
    )
    refuse_unresolved_commands(
        CommandSurface.of(app).admits, declined or [], generated_files(checkout)
    )
    return written
