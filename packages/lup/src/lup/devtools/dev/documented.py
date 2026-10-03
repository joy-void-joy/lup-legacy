"""Every `uv run lup-devtools ...` written down, against what the CLI serves.

A command named in prose is an instruction, and one naming a command that does
not exist fails for whoever follows it rather than for whoever wrote it.
Twenty-two shipped at once: `dev hookssweep` in the hooks workflow's own step
7, `py info` reached without its `dev` group in the introspection tools' own
docstring, `dashboard` without `setup` in the page describing the dashboard.
Each was written beside the command it named.

Two mechanisms answer that, and this is the one with reach.
:class:`~lup.harness.models.CommandInvocation` makes a *composed* document
right by construction and cannot reach a docstring, a checked-in Markdown
file, or a refusal string in a catalog — which is where most of these were. So
the sweep reads what is written, wherever it is written, and resolves it
against :class:`~lup.devtools.dev.commands.CommandSurface`.

Scanning text is what a whole family of rules here exists to avoid, and it is
right this once for one reason: the alternative is not deriving the prose, it
is not checking it. What the sweep never does is decide what a command
*means* — it asks the walked app whether the words reach one, and the app
answers.
"""

import ast
from itertools import takewhile

# lup: ignore[import-re] — the executable's own name inside arbitrary English,
# which no parser owns: what follows it is handed to the walked app rather
# than interpreted here
import re
from pathlib import Path

from collections.abc import Callable, Collection

from pydantic import BaseModel

from lup.devtools.dev.tracked import tracked_files
from lup.harness.ownership import load_manifest

# lup: ignore[re-call] — see the module note: recognizing the toolchain's name
# in prose, not parsing a structured format
MENTION = re.compile(r"uv run lup-devtools([^`\n\"']*)")
"""Where a mention starts, and everything up to whatever closes it.

Bounded by a backtick, a quote, or the line's end, because that is what
encloses a command every place one is written here: inline code in Markdown, a
docstring example line, a string literal in a catalog.
"""

# lup: ignore[re-call] — one plain command word, which is the shape the CLI's
# own names take; anything else ends the path and begins its arguments
COMMAND_WORD = re.compile(r"^[a-z][a-z0-9-]*$")
"""A word that could belong to a command path, rather than to its arguments."""


class WrittenCommand(BaseModel, frozen=True):
    """One `lup-devtools` invocation somebody wrote down, and where."""

    file: str
    line: int
    spelled: str

    def named(self) -> str:
        """This mention as a reader meets it: the site, then the words."""
        return f"{self.file}:{self.line}: uv run lup-devtools {self.spelled}"

    def command_words(self) -> list[str]:
        """The leading words that could name a command path.

        Stops where the first word that is not a plain command name appears,
        which is where the arguments begin: `--json`, a module path, or a
        placeholder the reader is meant to replace.
        """
        words = self.spelled.split()
        return [
            word
            for index, word in enumerate(words)
            if COMMAND_WORD.match(word)
            and all(COMMAND_WORD.match(earlier) for earlier in words[:index])
        ]


def written_commands() -> list[WrittenCommand]:
    """Every `uv run lup-devtools` mention in the tracked Python and Markdown.

    Generated trees are read too. A wrong command reaches a session through
    the rendered skill rather than through the module that declared it, and
    one mention rendered into both is a defect reported twice — the cheaper
    mistake than trusting a generated tree because it was generated.

    A test tree is not read. What a fixture spells is an input to a gate
    rather than an instruction to a reader: `dev worktree create feature` is
    there because the classifier must answer it, and it names no command on
    purpose. Reading those would make the sweep report the tests that hold
    this project to its policy.
    """

    def mentions(file: str) -> list[WrittenCommand]:
        try:
            text = Path(file).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return []
        return [
            WrittenCommand(file=file, line=number, spelled=tail.strip())
            for number, line in enumerate(text.splitlines(), start=1)
            for tail in MENTION.findall(line)
            if tail.strip()
        ]

    def structured(file: str) -> list[WrittenCommand]:
        """The commands a module names as the words that run them, through `devtools(...)`.

        A diagnostic's way through holds its command as words, so no prose
        mention of it exists to read; the call that spells it is read instead,
        up to the first word only the running code knows.
        """

        def literal(argument: ast.expr) -> str | None:
            match argument:
                case ast.Constant(value=str() as word):
                    return word
            return None

        def named(node: ast.AST) -> list[str]:
            match node:
                case ast.Call(
                    func=ast.Name(id="devtools") | ast.Attribute(attr="devtools"),
                    args=arguments,
                ):
                    found = [literal(argument) for argument in arguments]
                    leading = takewhile(lambda word: word is not None, found)
                    return [word for word in leading if word is not None]
            return []

        if not file.endswith(".py"):
            return []
        try:
            tree = ast.parse(Path(file).read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, SyntaxError):
            return []
        return [
            WrittenCommand(file=file, line=node.lineno, spelled=" ".join(words))
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and (words := named(node))
        ]

    return [
        mention
        for file in tracked_files(suffixes=(".py", ".md"))
        if "tests/" not in file
        for mention in [*mentions(file), *structured(file)]
    ]


def unresolved(
    admits: Callable[[list[str]], bool],
    declined: Collection[str] = (),
    generated: Collection[str] = (),
) -> list[WrittenCommand]:
    """Every written mention naming no command the composed CLI serves.

    Takes the question rather than the surface that answers it —
    :meth:`~lup.devtools.dev.commands.CommandSurface.admits` — because the
    module owning that surface also writes the page this sweep guards, and one
    of the two has to be able to import the other.

    A mention carrying no command word at all — `uv run lup-devtools --help`,
    or the toolchain named as a toolchain — is left alone: it instructs
    nothing this can check, and refusing it would refuse the sentence that
    introduces the CLI.

    *declined* are the command trees a module this project declined owns, and
    a hand-written file naming one is left alone too. The module's own code is
    still in the tree — its sub-app's docstrings, its skill's prose, the page
    it would publish — and each names that module's commands correctly, for
    the project that takes it: refusing them made declining ``conversation``
    fail generation on ``conversation``'s own skill. What *reaches a reader*
    is what generation wrote, so a file in *generated* is still held to the
    CLI this project serves, declined trees and all.
    """
    return [
        mention
        for mention in written_commands()
        if (words := mention.command_words())
        and not admits(words)
        and (words[0] not in declined or mention.file in generated)
    ]


def generated_files(root: Path) -> list[str]:
    """Every file a native tree's ownership manifest says generation wrote.

    Read off the manifests rather than re-composed, because the sweep runs
    once the trees are on disk and a manifest is its tree's own record of what
    it holds — the pages under ``docs/`` among them. Each runtime keeps one in
    its own tree root, which is why they are found rather than named here.
    """
    return sorted(
        item.path.as_posix()
        for manifest_path in root.glob(".*/.lup-ownership.json")
        if (manifest := load_manifest(manifest_path)) is not None
        for item in manifest.files
    )


def refuse_unresolved_commands(
    admits: Callable[[list[str]], bool],
    declined: Collection[str] = (),
    generated: Collection[str] = (),
) -> None:
    """Raise unless every written command names one the CLI serves.

    What generation does with the sweep's answer. A gate reports and leaves
    the tree as it found it, which is right for a check somebody runs; this
    runs while a document is being made, and a document telling its reader to
    run something that does not exist is not finished.
    """
    written = unresolved(admits, declined, generated)
    if written:
        named = "\n  ".join(mention.named() for mention in written)
        raise ValueError(
            f"{len(written)} documented command(s) name nothing this CLI "
            f"serves:\n  {named}\nRun the command as written to see what it "
            "answers, then spell it as the CLI does."
        )
