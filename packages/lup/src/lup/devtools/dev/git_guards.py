"""The git hooks that refuse work before it leaves the checkout.

A check that runs when somebody remembers to run it is a warning, not a
guarantee. What earns a hook is the ratio: a check standing between somebody
and their next keystroke has to cost far less than the mistake it catches,
because it is charged on every commit and the mistake is not.

Drift, at ``pre-commit``: the sources compiled into the native trees are
copied there verbatim, so rewording a comment in one of them makes both
trees stale without changing anything either does, and a commit that skips
the check writes that staleness into history. Reading it back costs seconds
rather than the gate's minutes, which is the ratio this hook is here for.

The whole gate — ruff, pyright, and both suites — is the pipeline's, at the
boundary where a runner spends the two minutes instead of the person who is
still working. A hook charging that to every push would buy only the
interval between pushing and the pipeline answering, and would charge it
against the loop that has to stay tight.

Each guard runs a command the pipeline also runs, so the places that can
refuse the same work run one computation rather than several that can
disagree, and a hook nobody installed is still refused by the pipeline at the
same line.

What git runs names no guard. Every worktree of a clone resolves its hooks
through the one shared directory, and each worktree sits at a revision of its
own, so a hook body spelling its guards out was written by one revision and
run by all of them: a check that grew an option failed every commit in a
worktree cut before it, and a body written from an older checkout ran none of
the guards a newer one declares. The installed hook is a trampoline instead
(:meth:`HookScript.body`) handing the moment to ``git hooks run <hook>``, and
the checkout git fired it in answers from its own declaration which guards
run there (:func:`fire`). Written from any revision it is the same file, so
arming stays a once-per-clone act rather than one owed after every change to
a guard.

Which hooks a project arms is its own declaration — :data:`DECLARED_GUARDS`
is what lup ships, not a set an adopter has to fork this module to
change. A project may declare several guards at one moment, which git runs
as the one script it runs per hook: :class:`HookScript` is that moment, and
it is the unit installed and read, because a guard is not what git has a
name for.
"""

import pkgutil
import shlex
import sys
import tomllib
from abc import ABC, abstractmethod
from collections.abc import Callable
from pathlib import Path
from typing import Literal, TextIO

import sh
import tomlkit
import tomlkit.items
import typer
from pydantic import (
    BaseModel,
    TypeAdapter,
    ValidationError,
    field_serializer,
    field_validator,
)

from lup.formats.banner import REGENERATE_COMMAND
from lup.types import JsonObject
from lup.workspace.paths import project_root
from lup.execution.shell import git
from lup.execution.writability import on_read_only_mount, refuses_a_new_file

DRIFT_COMMAND = "uv run lup-devtools harness check all"
"""The read-only drift check every path that refuses stale output runs."""

# lup: ignore[constant-declaration] — the command a reader types, whose words
# are the CLI's own rather than a preference this module holds
CHECK_COMMAND = "uv run lup-devtools dev check"
"""The project's whole gate, run by the pipeline at the boundary it guards.

Beside :data:`DRIFT_COMMAND` because the two are the same kind of thing —
what a refusal actually runs — and because the workflow renders both as
steps, so the pipeline and the hook lup arms name the same commands.
"""

# lup: ignore[constant-declaration] — the command a reader types, whose words
# are the CLI's own rather than a preference this module holds
INSTALL_COMMAND = "uv run lup-devtools git hooks install"
"""How a checkout arms its guards, named in each hook it writes."""

# lup: ignore[constant-declaration] — the marker this command writes into a
# hook and reads back to know the hook is its own, so it is an identity rather
# than a setting: a caller changing it would orphan every hook already installed
GUARD_MARKER = "lup-git-guard"
"""How an installed hook says it is this command's to rewrite."""

# lup: ignore[constant-declaration] — the identity earlier versions wrote, kept
# so a checkout armed by one is still recognized rather than read as a stranger
LEGACY_GUARD_MARKER = "lup-commit-guard"
"""What this wrote while only the commit hook existed.

Recognized on read and never written. Dropping it would make every hook a
previous version installed report as one nobody here wrote, which is the
state that needs ``--force`` to leave — so the compatibility is worth one
line rather than a migration note nobody reads.
"""

GIT_ENVIRONMENT = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_COMMON_DIR",
)
"""What names a repository to git ahead of any directory a command is given.

`-C <throwaway>` chooses where git works, while these choose which repository
it resolves, and the environment wins. Git exports them to every hook, so a
guard whose suite builds throwaway repositories would commit into the very
checkout the hook fired in, however carefully each helper bound its own git.
Scrubbed where the hook is written, because unlike a misbound command this
arrives through one door.
"""

# lup: ignore[constant-declaration] — the name a hook this command wrote hands
# the index on under, which every revision's guards read: an identity between
# the installed hook and the checkout answering it, not a setting
INDEX_VARIABLE = "LUP_GIT_INDEX_FILE"
"""The index a commit is being made from, as the hook hands it to its guards.

Git names it to the hook as ``GIT_INDEX_FILE`` -- `index.lock` under `git
commit -a`, an index made for the purpose under `git commit <path>` -- and the
hook drops that name with the rest of :data:`GIT_ENVIRONMENT`. So it goes on
under this one, which a guard judging the commit reads and a runner predating
it ignores.
"""

type DevtoolsRunner = Callable[[tuple[str, ...]], int]
"""Runs one ``lup-devtools`` invocation in the devtools already loaded.

Handed the words after ``lup-devtools`` and answering the exit status, so a
moment whose guards are devtools commands pays for one loaded application
rather than one process, and one load, per guard.
"""


class HookMoment(BaseModel, frozen=True):
    """One firing of a git hook, as the guards declared at it are handed it."""

    hook: str
    root: Path
    """The checkout git fired the hook in, where each guard runs."""

    arguments: tuple[str, ...] = ()
    """What git passed the hook: a remote and its URL at ``pre-push``, the
    squash flag at ``post-merge``, nothing at ``pre-commit``."""

    stdin: str | None = None
    """What git handed the hook on stdin, read only where a guard declares it.

    None is not the empty string: it says nothing here read stdin at all,
    which is what keeps a moment fired by hand from a terminal from waiting
    on a keyboard for input no guard wants.
    """


class Standdown(BaseModel, ABC, frozen=True):
    """What a moment says about itself that leaves one guard nothing to judge.

    Answered here rather than inside the check, which would otherwise have to
    be taught a second job and read a stdin it never asked for. A standdown
    stands its own guard down and no other: a moment guarded twice still runs
    the second guard whatever the first one's standdown said.
    """

    @abstractmethod
    def applies(self, moment: HookMoment) -> bool:
        """Whether this firing stands the guard down."""

    def reads_stdin(self) -> bool:
        """Whether answering needs what git handed the moment on stdin.

        Declared by the standdown rather than by the guard carrying it, so a
        guard cannot carry one while forgetting to ask for its input, which
        would read an empty stdin as a push of nothing and stand down always.
        """
        return False


class MergeInProgress(Standdown, frozen=True):
    """A commit concluding a merge, which the generated trees cannot be judged at.

    A commit concluding a merge is mid-transaction, and the generated trees
    are compiled from declarations that merge has not finished writing: a
    resolution is where a project decides what of upstream it takes, so it is
    where those declarations change. Git draws the same line itself — the
    merge it completes on its own runs ``pre-merge-commit``, not this moment —
    so the check would refuse exactly the merges somebody had to resolve by
    hand and no other. ``dev update`` regenerates once the merge lands, and
    every commit that concludes no merge is read as always.

    Declared on the drift guard rather than made every guard's default: at
    the push moment an unfinished merge decides nothing.
    """

    def applies(self, moment: HookMoment) -> bool:
        named = git.out("-C", str(moment.root), "rev-parse", "--git-path", "MERGE_HEAD")
        return (moment.root / named).exists()


# lup: ignore[constant-declaration] — the command a reader types, whose words
# are the CLI's own rather than a preference this module holds
SETTLE_COMMAND = "uv run lup-devtools git settle"
"""What folds regenerated trees into the merge commit a merge just made.

Hung off two moments because git makes a merge commit at two: the merge it
completes itself runs `post-merge`, and one concluded by hand after a
conflict runs `post-commit` — no single hook sees both.
"""


class NoMergeCommit(Standdown, frozen=True):
    """A commit with one parent, which the settling moments have nothing to fold into.

    Only a merge commit holds a tree two branches' generated files were
    merged into; every other commit carries a tree one side generated, so
    paying for a regeneration there would buy nothing.
    """

    def applies(self, moment: HookMoment) -> bool:
        second = git.out(
            "-C",
            str(moment.root),
            "rev-parse",
            "-q",
            "--verify",
            "HEAD^2",
            _ok_code=[0, 1],
        )
        return not second


class DeletionOnly(Standdown, frozen=True):
    """A push that only deletes refs, which uploads nothing for a check to judge.

    Offered rather than declared: lup leaves its gate to the pipeline, and a
    project that does want one at ``pre-push`` wants this in front of it.
    Deleting a branch is the case that makes it worth having — it uploads no
    tree at all, so a gate there could only re-judge what the remote already
    holds, and it would charge the whole suite for the privilege, long enough
    that a delete can time out having removed the local branch and left the
    remote copy standing.

    Never every guard's default: at the commit moment git hands a hook no
    stdin, which reads as a push of nothing and would stand that guard down
    on every commit while it still reported as armed.
    """

    def reads_stdin(self) -> bool:
        return True

    def applies(self, moment: HookMoment) -> bool:
        """Whether no line git handed the push names content it uploads.

        Git names each update as ``<local ref> <local oid> <remote ref>
        <remote oid>`` and writes an all-zero local oid where a ref is being
        deleted. Anything else is content this push answers for, and a line
        that does not parse counts as content rather than being trusted into
        a standdown.
        """

        def carries_content(update: str) -> bool:
            match update.split():
                case [_, local_oid, *_] if local_oid == "0" * len(local_oid):
                    return False
                case _:
                    return True

        lines = (moment.stdin or "").splitlines()
        return not any(carries_content(update) for update in lines)


class GitGuard(BaseModel, frozen=True):
    """One check a repository installs as a git hook, and what it refuses.

    Every field is a judgement rather than a fact — another project may guard
    a different check, hang it off a different git hook, or say something
    else about why — so each is a default a caller replaces rather than a
    constant it would have to fork this module to change.
    """

    command: str = DRIFT_COMMAND
    """What the guard runs, and refuses the moment on a nonzero exit from.

    A shell line, run by ``sh -c`` in the checkout with the hook's name as
    ``$0`` and what git passed the hook as ``$1`` onwards — what the same line
    saw when it stood in the hook script itself. One that is nothing but a
    ``lup-devtools`` invocation runs in the devtools already loaded instead
    (:meth:`devtools_arguments`), which is the same command without a second
    interpreter paying to load the application again.
    """

    hook: str = "pre-commit"
    """Which git hook the check runs at."""

    standdown: Standdown | None = None
    """What the moment may say about itself to stand this guard down.

    None by default: most moments say nothing a guard could stand down on,
    and a guard that stands down silently is worse than one that runs.
    """

    refusal: str = (
        "A generated artifact differs from what its source renders. "
        f"Settle it with `{REGENERATE_COMMAND}`."
    )
    """What whoever this guard just stopped is told, beside the command.

    A hook that only exits nonzero leaves its reader guessing which check
    fired and what settles it, so the refusal is said as it happens, where
    that reader is certainly looking.
    """

    reads_stdin: bool = False
    """Whether this check reads what git handed the moment on stdin.

    Git delivers a moment's stdin once, so it is read once and each guard at
    the moment is handed the same copy — declared rather than detected,
    because whether a command reads is a fact about that command and nothing
    here can see inside it. A check that does not declare it is handed an
    empty stdin. False by default: only the push moment describes itself on
    stdin at all.
    """

    @field_serializer("standdown")
    def named_standdown(self, standdown: Standdown | None) -> JsonObject | None:
        """A standdown as the class it is and the fields it holds, so it compiles.

        Named by import path because the class is the answer — the fields are
        only what it was given — and a project's own standdown compiles the
        same way lup's do.
        """
        if standdown is None:
            return None
        kind = type(standdown)
        return {
            "kind": f"{kind.__module__}:{kind.__qualname__}",
            **standdown.model_dump(mode="json"),
        }

    @field_validator("standdown", mode="before")
    @classmethod
    def resolved_standdown(
        cls, value: JsonObject | Standdown | None
    ) -> JsonObject | Standdown | None:
        """A compiled standdown back as the class it names, holding its fields."""
        match value:
            case {"kind": str(kind), **fields}:
                try:
                    named = pkgutil.resolve_name(kind)
                except (ImportError, AttributeError) as error:
                    raise ValueError(f"no standdown resolves as {kind}") from error
                match named:
                    case type() if issubclass(named, Standdown):
                        return named.model_validate(fields)
                    case _:
                        raise ValueError(f"{kind} names no Standdown")
            case _:
                return value

    def reads(self) -> bool:
        """Whether this check or its standdown needs the moment's stdin."""
        return self.reads_stdin or (
            self.standdown is not None and self.standdown.reads_stdin()
        )

    def devtools_arguments(self) -> tuple[str, ...] | None:
        """The words after ``lup-devtools``, where the line is that invocation alone.

        Read with the shell's own lexer, so quoting parses as the shell would
        parse it. Anything the shell would do besides run that one command — a
        second command, a pipe or redirect, an expansion, a glob, a variable —
        leaves the line to the shell, and so does a check that reads stdin,
        which the devtools already loaded has spent. Refusing a line is never
        wrong, only slower: it runs exactly as it always did.
        """
        if self.reads_stdin:
            return None
        lexer = shlex.shlex(self.command, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        try:
            words = list(lexer)
        except ValueError:
            return None

        def literal(word: str) -> bool:
            return not any(character in "$`*?[]{}~()<>|&;\\'\"\n" for character in word)

        match words:
            case ["uv", "run", "lup-devtools", *arguments] if all(
                literal(word) for word in arguments
            ):
                return tuple(arguments)
            case _:
                return None

    def run(self, moment: HookMoment, devtools: DevtoolsRunner) -> int:
        """Run the check at this firing, and answer its exit status.

        Its output is the moment's own, so it reaches whoever git is running
        the hook for as it is written rather than once the check ends.
        """
        arguments = self.devtools_arguments()
        if arguments is not None:
            return devtools(arguments)
        try:
            sh.Command("sh")(
                "-c",
                self.command,
                moment.hook,
                *moment.arguments,
                _cwd=str(moment.root),
                _in=moment.stdin or "",
                _out=sys.stdout,
                _err=sys.stderr,
            )
        except sh.ErrorReturnCode as error:
            return error.exit_code
        return 0


DECLARED_GUARDS = [
    GitGuard(standdown=MergeInProgress()),
    # No standdown: a merge's own commit is where its markers get committed.
    # lup: solved: the hook scrubs GIT_INDEX_FILE before this runs, so under
    # `git commit -a` or `git commit <path>` it reads the checkout's index
    # rather than the `index.lock` git is committing, and misses a conflict
    # block only those staged. Hand the moment's index to the guards that
    # judge it through the environment, which a runner predating it ignores.
    GitGuard(
        command=f"{CHECK_COMMAND} --conflict-markers --staged",
        refusal=(
            "A staged file holds a conflict block a merge left behind. Resolve "
            "it, or excuse a fixture holding one on purpose with a "
            "`lup: ignore[conflict-marker]` line heading it."
        ),
    ),
    *(
        GitGuard(
            command=SETTLE_COMMAND,
            hook=moment,
            standdown=NoMergeCommit(),
            refusal=(
                "Refuses nothing, since the merge commit is already made: what "
                "regeneration writes over it was not folded in, so its "
                f"generated trees may be stale until `{SETTLE_COMMAND}` does."
            ),
        )
        for moment in ("post-merge", "post-commit")
    ),
]
"""The hooks lup arms, offered to a project as the set it usually wants.

A default rather than a fixture: a project that runs its gate somewhere else,
or that has a second moment worth guarding, names its own list. What makes
drift the one lup arms is the ratio in :data:`DRIFT_COMMAND`'s favour — a
second, against a staleness that is otherwise written into history and read
back by whoever next regenerates.

The two settling moments are the other half of that guard. A merge of two
branches that both regenerated leaves the kept side's ownership proof stale,
which the drift guard refuses at the next commit, so the settle regenerates
there and then, once per merge commit, and a merge stops being followed by a
regenerate-and-commit by hand.
"""


class MomentOutcome(BaseModel, frozen=True):
    """How one firing ended: passed, or refused by the first guard to refuse."""

    refused_by: GitGuard | None = None
    status: int = 0

    def report(self) -> str:
        """What whoever the moment stopped is told; empty where nothing refused."""
        if self.refused_by is None:
            return ""
        return (
            f"{GUARD_MARKER}: {self.refused_by.hook} refused by "
            f"`{self.refused_by.command}` (exit {self.status}). "
            f"{self.refused_by.refusal}"
        )


class HookScript(BaseModel, frozen=True):
    """Every guard a repository declares at one moment, and the file git runs there.

    Git runs a single script per hook, so a moment guarded twice is one file
    rather than two — which is why the installed unit is a moment and not a
    guard. The guards run in the order they were declared and the first to
    refuse ends the operation, so the cheap check goes before the expensive
    one.
    """

    hook: str
    guards: list[GitGuard]

    environment: tuple[str, ...] = GIT_ENVIRONMENT
    """What the hook drops before handing its moment over.

    Dropped in the hook rather than by any guard, because the checkout's
    devtools is the first thing to ask git which repository it is in.
    """

    def captures_stdin(self) -> bool:
        """Whether this moment reads git's stdin, once, for every guard at it.

        Git delivers it once, so the first guard to read would drain it for
        every guard after — a push whose data check read the ref list would
        leave the gate beside it judging a push that looks empty, and
        standing down. Read here and handed to each instead. A moment where
        nothing reads does not read at all, so firing one by hand from a
        terminal does not wait on the keyboard.
        """
        return any(guard.reads() for guard in self.guards)

    def fired(
        self, arguments: tuple[str, ...], root: Path, stdin: TextIO
    ) -> HookMoment:
        """This moment as git fired it in ``root``."""
        return HookMoment(
            hook=self.hook,
            root=root,
            arguments=arguments,
            stdin=stdin.read() if self.captures_stdin() else None,
        )

    def run(self, moment: HookMoment, devtools: DevtoolsRunner) -> MomentOutcome:
        """Run each guard in declaration order, until one refuses.

        A guard is judged against its standdown just before it would run, and
        ``devtools`` is reached only by a guard that runs — so a moment where
        each guard stands down ends having loaded nothing a guard needs.
        """
        for guard in self.guards:
            if guard.standdown is not None and guard.standdown.applies(moment):
                continue
            status = guard.run(moment, devtools)
            if status != 0:
                return MomentOutcome(refused_by=guard, status=status)
        return MomentOutcome()

    def body(self) -> str:
        """The file git runs: the marker claiming it, then the hand-off to the checkout.

        Names no guard, so every revision writes the same file and every
        revision can answer it: a checkout reads its own declaration at
        ``git hooks run``. The scrub stays here because it has to precede
        the checkout's devtools itself — git names this repository to a hook
        through the environment, which outranks the ``-C`` any command binds
        itself with, so a check whose suite builds throwaway repositories
        would resolve this one instead and commit into the branch being
        worked on.

        The call is spelled here and nowhere else, because it is the one
        contract between a hook and every revision that may answer it:
        ``git hooks run <hook> -- <what git passed the hook>``. Whatever a
        later revision wants a hook to carry besides goes through the
        environment, which a runner predating it ignores, rather than
        through an option a runner predating it refuses.

        One failure is let through, and only one: a checkout whose devtools
        predates ``git hooks run``. Such a checkout declares no guard this
        file could reach, and refusing its every commit would leave the one
        way past to be skipping hooks altogether — so the moment passes and
        says so, naming the checkout. It is told apart narrowly. Typer's
        usage error, an unknown command among them, exits 2; the verb is then
        asked for its help, which exits 2 only where it is missing; and the
        CLI is asked for its own, which exits 0 only where the checkout's
        devtools runs at all — so a broken environment, which ``uv`` also
        reports as 2, still refuses. Every other failure, a guard's own 2
        included, refuses as it came, and the probing costs only a moment
        that already failed.
        """
        run = "uv run lup-devtools git hooks run"
        passed = shlex.quote(
            f"`{run}`, so it ran no {self.hook} guard; merging its base arms them."
        )
        return (
            "#!/bin/sh\n"
            f"# {GUARD_MARKER}: written by `{INSTALL_COMMAND}`.\n"
            "# Names no guard. Every worktree of this clone runs this one file,\n"
            "# each at a revision of its own, so the checkout it fires in runs\n"
            "# the guards its own devtools declares, which\n"
            "# `uv run lup-devtools git hooks status` lists.\n"
            "#\n"
            "# The index a commit is made from, handed on under lup's own name\n"
            "# for the guards that judge what it holds, since the name git gave\n"
            "# it is dropped below with the rest.\n"
            'if [ -n "${GIT_INDEX_FILE-}" ]; then\n'
            f'    {INDEX_VARIABLE}="$GIT_INDEX_FILE"\n'
            f"    export {INDEX_VARIABLE}\n"
            "else\n"
            f"    unset {INDEX_VARIABLE}\n"
            "fi\n"
            "# Dropped so what runs below resolves this repository from the\n"
            "# directory it runs in. Git names it here too, and that name would\n"
            "# outrank the `-C` a test's throwaway repository binds git with.\n"
            f"unset {' '.join(self.environment)}\n"
            f'{run} {shlex.quote(self.hook)} -- "$@"\n'
            "status=$?\n"
            '[ "$status" -eq 2 ] || exit "$status"\n'
            "# 2 is a usage error as well as a refusal. Let through only where\n"
            "# the checkout's devtools runs but predates the verb above, which\n"
            "# leaves it no guard to run; any other 2 refuses as it came.\n"
            f"{run} --help </dev/null >/dev/null 2>&1\n"
            '[ "$?" -eq 2 ] || exit 2\n'
            "uv run lup-devtools --help </dev/null >/dev/null 2>&1 || exit 2\n"
            f'echo "{GUARD_MARKER}: $(pwd) predates" {passed} >&2\n'
            "exit 0\n"
        )


def fire(
    guards: list[GitGuard],
    hook: str,
    arguments: tuple[str, ...],
    root: Path,
    stdin: TextIO,
    devtools: DevtoolsRunner,
) -> int:
    """Run the guards declared at ``hook`` as git fired it, answering its status.

    What ``git hooks run`` does, and so what every installed hook reaches:
    the guards are this checkout's own declaration at the revision it is at,
    whichever revision wrote the hook. A moment nothing here declares runs
    nothing and passes, which is how a hook armed by a newer declaration
    reads to an older one. ``devtools`` runs the guards that are devtools
    commands, and is not called at all where none is left standing.
    """
    script = HookScript(hook=hook, guards=[g for g in guards if g.hook == hook])
    outcome = script.run(script.fired(arguments, root, stdin), devtools)
    if outcome.refused_by is not None:
        typer.echo(outcome.report(), err=True)
    return outcome.status


def hook_scripts(guards: list[GitGuard]) -> list[HookScript]:
    """Group guards into the moments they are installed as, order preserved.

    Declaration order is the running order, so a project states its cheap
    refusal before its expensive one and gets exactly that.
    """
    moments = dict.fromkeys(guard.hook for guard in guards)
    return [
        HookScript(hook=hook, guards=[g for g in guards if g.hook == hook])
        for hook in moments
    ]


def compiled_guards(root: Path) -> list[GitGuard] | None:
    """The guards ``root``'s manifest compiles, or None where it compiles none.

    What ``git hooks run`` reads before the project's application loads,
    which is what lets a moment whose every guard stands down end in the
    time it takes to import this module rather than to build the whole CLI.
    It is the declaration itself, compiled into ``[tool.lup]`` by ``harness
    generate all`` and held to it by the drift check, so a checkout reads the
    declaration at the revision it is at.

    None sends the moment to the application, which reads the declaration
    directly: a manifest that is missing, one a merge left unparseable, one
    that compiles no table, or one naming a standdown that no longer
    resolves. Every one of those is slower and none of them skips a guard.
    """
    try:
        manifest = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return None
    match manifest:
        case {"tool": {"lup": {"git-guards": list(compiled)}}}:
            try:
                return TypeAdapter(list[GitGuard]).validate_python(compiled)
            except ValidationError:
                return None
        case _:
            return None


def write_git_guards(
    guards: list[GitGuard], root: Path | None = None, *, check: bool = False
) -> Path:
    """Write or verify the guards compiled into ``pyproject.toml`` for the hooks.

    A repository writer: ``harness generate all`` writes it, and the drift
    check reads it with *check*, which reports the manifest stale rather than
    writing it. Compiled rather than read from the declaration at hook time
    because reading the declaration means loading the application, which is
    most of what a moment costs.
    """
    manifest = (root or project_root()) / "pyproject.toml"
    document = tomlkit.parse(manifest.read_text(encoding="utf-8"))
    compiled = [guard.model_dump(mode="json", exclude_none=True) for guard in guards]
    match document.unwrap():
        case {"tool": {"lup": {"git-guards": list(current)}}}:
            stale = current != compiled
        case _:
            stale = True
    if stale and check:
        raise RuntimeError(f"{manifest} is behind the declared git guards")
    if not stale:
        return manifest

    tables = tomlkit.aot()
    last = len(compiled) - 1
    for index, fields in enumerate(compiled):
        table = tomlkit.table()
        if index == 0:
            table.add(tomlkit.comment(f"Compiled by `{REGENERATE_COMMAND}` from the"))
            table.add(tomlkit.comment("git guards the dev tree declares: edit those."))
        for key, value in fields.items():
            match value:
                case dict():
                    inline = tomlkit.inline_table()
                    inline.update(value)
                    table[key] = inline
                case _:
                    table[key] = value
        if index == last:
            table.add(tomlkit.nl())
        tables.append(table)
    tool = document.setdefault("tool", tomlkit.table())
    lup = tool.setdefault("lup", tomlkit.table())
    # An empty declaration is spelled as one, so it reads as compiled.
    lup["git-guards"] = tables if compiled else tomlkit.array()
    manifest.write_text(tomlkit.dumps(document), encoding="utf-8")
    return manifest


type GuardStatus = Literal[
    "current", "stale", "absent", "foreign", "orphaned", "retired"
]
"""What sits at the hook path, judged against what this moment would write.

``orphaned`` and ``retired`` are the two halves of a moment leaving the
declaration: what a reading finds still installed there, and what an install
reports having cleared. Separate words because one of them asks a reader for
something and the other tells them it is already done.
"""


class GuardState(BaseModel, frozen=True):
    """Where one of a repository's guarded moments lives, and what is there."""

    path: Path
    status: GuardStatus

    @property
    def armed(self) -> bool:
        """Whether this repository now runs the guarded check at that moment."""
        return self.status == "current"

    def describe(self) -> str:
        """One line saying what is installed, and what to do when it is not.

        The path ends in the hook's own name, so it says which moment this
        is about without the sentence having to repeat it.
        """
        match self.status:
            case "current":
                return f"guard armed: {self.path}"
            case "stale":
                return f"guard at {self.path} is an older body; reinstall it"
            case "absent":
                return f"guard not installed at {self.path}; run `{INSTALL_COMMAND}`"
            case "foreign":
                return f"{self.path} holds a hook this did not write"
            case "orphaned":
                return (
                    f"{self.path} guards a moment nothing declares; "
                    f"`{INSTALL_COMMAND}` clears it"
                )
            case "retired":
                return f"guard retired: {self.path}"


class GuardConflict(RuntimeError):
    """A hook nobody here wrote already occupies the guard's path."""


def hooks_directory(root: Path) -> Path:
    """Where git looks for this clone's hooks, asked from any of its worktrees.

    ``core.hooksPath`` moves the whole directory, so a guard written to
    ``.git/hooks`` under a clone that sets it would be a hook git never runs.
    Asking git resolves both that setting and the shared common directory a
    linked worktree hooks through.
    """
    configured = git.out(
        "-C", str(root), "config", "--get", "core.hooksPath", _ok_code=[0, 1]
    )
    named = configured or git.out(
        "-C", str(root), "rev-parse", "--git-path", "hooks", _ok_code=[0]
    )
    return root / named


def guard_state(script: HookScript, directory: Path) -> GuardState:
    """Read what is installed at this moment's hook path."""
    path = directory / script.hook
    if not path.is_file():
        return GuardState(path=path, status="absent")
    installed = path.read_text(encoding="utf-8")
    if not any(marker in installed for marker in (GUARD_MARKER, LEGACY_GUARD_MARKER)):
        return GuardState(path=path, status="foreign")
    current = installed == script.body()
    return GuardState(path=path, status="current" if current else "stale")


def orphaned_guards(guards: list[GitGuard], directory: Path) -> list[GuardState]:
    """Hooks this command wrote at moments the declaration no longer names.

    Every other reading here is driven by the declaration, so a moment that
    leaves it takes its own reporting with it: git goes on running the file
    while the gate that would have said so has stopped looking at that path.
    This is the one reading that starts from the directory instead, which is
    what lets a checkout be told about a hook nothing asks for any more.

    Only files carrying the marker. A hook somebody else wrote at a moment
    lup never declared is not this command's to name, let alone remove.
    """
    if not directory.is_dir():
        return []
    declared = {script.hook for script in hook_scripts(guards)}
    ours = [
        path
        for path in sorted(directory.iterdir())
        if path.name not in declared and path.is_file()
    ]
    return [
        GuardState(path=path, status="orphaned")
        for path in ours
        if any(
            marker in path.read_text(encoding="utf-8", errors="replace")
            for marker in (GUARD_MARKER, LEGACY_GUARD_MARKER)
        )
    ]


def retire_guards(guards: list[GitGuard], root: Path) -> list[GuardState]:
    """Clear every hook on disk at a moment the declaration does not name.

    Run by the same install that arms the declared moments, because a
    declaration naming one fewer is only half applied while the hook standing
    at the moment it dropped is still on disk and still running.
    """

    def retired(state: GuardState) -> GuardState:
        state.path.unlink()
        return GuardState(path=state.path, status="retired")

    return [retired(state) for state in orphaned_guards(guards, hooks_directory(root))]


def read_guards(guards: list[GitGuard], root: Path) -> list[GuardState]:
    """What one checkout would run, or fail to run, at each moment declared."""
    directory = hooks_directory(root)
    return [guard_state(script, directory) for script in hook_scripts(guards)]


class HooksReading(BaseModel, frozen=True):
    """Where a checkout looks for hooks, and what it has armed there."""

    directory: Path
    reachable: bool
    """Whether that directory is there to hold a hook at all.

    The one breakage no environment makes ambiguous, and the quiet one. Git
    runs no hook and reports nothing, so every guard the repository declares
    is off while the gate that would have said so keeps passing — a checkout
    running no hooks reads exactly like one whose hooks are green. A
    ``core.hooksPath`` still naming a directory that has since been removed
    is how a repository arrives here.

    Absence is the signal rather than the count of hooks inside, because an
    empty hooks directory is the normal state of a fresh clone and says
    nothing about whether this repository's own guards belong in it.
    """

    guards: list[GuardState]

    orphaned: list[GuardState]
    """Hooks this wrote at moments the declaration has since stopped naming.

    The mirror of :attr:`reachable`, and quiet in the same way: git runs the
    file whatever the declaration says, so a checkout still charging itself
    for a retired guard reads exactly like one that never had it.
    """

    def unarmed(self) -> list[GuardState]:
        """Each declared moment this checkout would not currently guard.

        Reported rather than refused: a clone that never ran the install
        command is a working clone, and the pipeline refuses the same drift
        and the same gate on the way in.
        """
        return [state for state in self.guards if not state.armed]


def read_hooks(guards: list[GitGuard], root: Path) -> HooksReading:
    """Where `root` resolves its hooks, and the state of each moment declared for it."""
    directory = hooks_directory(root)
    return HooksReading(
        directory=directory,
        reachable=directory.is_dir(),
        guards=[guard_state(script, directory) for script in hook_scripts(guards)],
        orphaned=orphaned_guards(guards, directory),
    )


def outstanding_arming(guards: list[GitGuard], root: Path) -> list[GuardState]:
    """Every moment an install would still have to write a file for.

    Both halves of what installing does, because either one is a write: a
    declared moment whose hook is missing, is an older body, or is somebody
    else's, and a hook this command wrote at a moment the declaration has
    since dropped. A checkout where this is empty arms nothing and opens the
    hooks directory not at all, which is the whole of why that directory can
    be held read-only around it.
    """
    reading = read_hooks(guards, root)
    return [*reading.unarmed(), *reading.orphaned]


def arming_is_refused(directory: Path) -> bool:
    """Whether the hooks directory would refuse the file an install writes.

    Asked of the nearest ancestor that is there, because installing creates
    the directory where it is missing: a path nothing holds refuses nothing,
    and the write that would actually be refused is the `mkdir` beside it.
    """
    holder = next(path for path in (directory, *directory.parents) if path.exists())
    return on_read_only_mount(holder) or refuses_a_new_file(
        holder, probe_prefix=".lup-hook-probe"
    )


def host_install(root: Path) -> str:
    """The exact command that arms this clone's hooks, typed on the host.

    Installing is a host step: a contained session holds the shared hooks
    directory read-only, because what it holds are scripts git executes at
    the operator's next commit. Named with the checkout it runs in, which is
    any of the clone's, since every worktree resolves the same directory.
    """
    return f"cd {shlex.quote(str(root))} && {INSTALL_COMMAND}"


def blocked_arming(guards: list[GitGuard], root: Path) -> str:
    """Why this checkout cannot arm its guards, empty where it can or need not.

    Conditional on an arming being outstanding, which is what makes holding
    `<common>/hooks/` read-only cost nothing. Hooks resolve through the
    shared directory from every worktree of a clone — `git rev-parse
    --git-path hooks` in a linked worktree names `<common>/hooks` — so a
    guard armed once on the host is armed for every worktree cut afterwards,
    whenever it is cut, and a confined run that finds each declared moment
    current writes nothing here.

    Where one is outstanding it is named rather than waved through. A hook
    holding an older body is the case a softer answer misses: the file is
    there and git runs it, so the moment reads as guarded while the script
    executing is not the one the declaration describes.
    """
    outstanding = outstanding_arming(guards, root)
    if not outstanding:
        return ""
    directory = hooks_directory(root)
    if not arming_is_refused(directory):
        return ""
    return "\n".join(
        [
            f"{directory} is held read-only and these guards are not armed in it:",
            *(f"  - {state.describe()}" for state in outstanding),
            f"Run `{host_install(root)}` from a terminal on the host. Hooks "
            "resolve through the shared directory from every worktree of this "
            "clone, so arming them once there covers this checkout and every "
            "worktree cut after it.",
        ]
    )


def install_script(
    script: HookScript, root: Path, *, force: bool = False
) -> GuardState:
    """Write one moment's hook, refusing to displace one written elsewhere.

    A moment already current is left as it is, so installing over an armed
    clone writes nothing — which is what lets it succeed where the hooks
    directory is held read-only and there is nothing to write.
    """
    directory = hooks_directory(root)
    existing = guard_state(script, directory)
    if existing.armed:
        return existing
    if existing.status == "foreign" and not force:
        raise GuardConflict(
            f"{existing.path} holds a hook this did not write; read it, then pass "
            "--force to replace it"
        )
    directory.mkdir(parents=True, exist_ok=True)
    existing.path.write_text(script.body(), encoding="utf-8", newline="\n")
    existing.path.chmod(0o755)
    return guard_state(script, directory)


def install_guards(
    guards: list[GitGuard], root: Path, *, force: bool = False
) -> list[GuardState]:
    """Arm every moment these guards declare, and clear the ones they dropped.

    Both halves, so a checkout armed by an older declaration converges by
    running the install command it was already told to run, rather than by
    somebody being sent to delete a file they never knew was there.
    """
    return [
        *[install_script(script, root, force=force) for script in hook_scripts(guards)],
        *retire_guards(guards, root),
    ]


def uninstall_script(script: HookScript, root: Path) -> GuardState:
    """Remove one moment's hook, leaving one written elsewhere alone."""
    state = guard_state(script, hooks_directory(root))
    match state.status:
        case "current" | "stale":
            state.path.unlink()
            return GuardState(path=state.path, status="absent")
        # Nothing of this command's to remove. The last two cannot arrive from
        # a declared moment's own path at all — they are what a scan of the
        # directory says about some other path — and are named so the match
        # stays total rather than because the reading can produce them here.
        case "absent" | "foreign" | "orphaned" | "retired":
            return state


def uninstall_guards(guards: list[GitGuard], root: Path) -> list[GuardState]:
    """Remove every moment these guards declare, leaving foreign hooks alone."""
    return [uninstall_script(script, root) for script in hook_scripts(guards)]


def arm(guards: list[GitGuard], root: Path) -> list[str]:
    """Install each moment where a setup command can only report a failure.

    Worktree creation runs under sandboxes that mount the hooks directory
    read-only, and a checkout without the hooks is still a working checkout —
    the pipeline refuses the same drift and the same gate on the way in. So
    this says what happened and lets setup carry on, rather than failing it
    over a second line of defence.

    Each moment is reported on its own line and none stops the next: they
    guard different moments, and one hook path already occupied is no reason
    to leave the other unarmed.
    """

    def armed(script: HookScript) -> str:
        try:
            return install_script(script, root).describe()
        except (OSError, GuardConflict, sh.ErrorReturnCode) as error:
            return f"{script.hook} guard not installed: {error}"

    def cleared() -> list[str]:
        try:
            return [state.describe() for state in retire_guards(guards, root)]
        except sh.ErrorReturnCode:
            # Asking git where the hooks live is the first thing the arming
            # above did too, so a checkout git cannot read has already said so
            # once and does not need the same sentence in other words.
            return []
        except OSError as error:
            return [f"retired guard not removed: {error}"]

    return [*[armed(script) for script in hook_scripts(guards)], *cleared()]
