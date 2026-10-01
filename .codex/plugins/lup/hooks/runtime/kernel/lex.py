# lup: ignore[empty-collection]
# The dependency-free runtime deliberately uses primitive rows and stdlib scanners.
"""Shell readers: what a parsed command writes, carries, runs and rewrites.

Every reader here takes its words from one tree -- :func:`parse_shell`, the
grammar in ``syntax.py`` with its variables bound once by ``bindings.py`` --
so the classifier and the host readers that stat targets and produce
rewritten documents cannot come to read one command two ways.
"""

import posixpath
from pathlib import PurePosixPath
from typing import TypedDict

from .archives import archive_write
from .bindings import (
    bind_script,
    carried_words,
    command_lists,
    literal_loop_word,
    rebuilt_lists,
    unrollable,
)
from .decision import KernelDecision, unjudged
from .downloads import download_targets
from .effects import EffectEvidence, declare, question_reach, verdict_for
from .roles import git_state, spells_its_path, writes_to_a_stream
from .rows import (
    DisplacedTargetRow,
    PathRoleRow,
    PathRuleRow,
    PathWord,
    ShellRuleRow,
)
from .syntax import (
    AndOr,
    Clause,
    Command,
    Item,
    Pipeline,
    Redirect,
    Script,
    Word,
    WordPart,
    expands,
    parse_script,
    part,
    word_text,
)
from .words import (
    PATH_VERBS,
    carried_setting,
    effective_command,
    fills_a_stream,
    flag_write_targets,
    flag_write_words,
    git_apply_words,
    global_span,
    git_checkout_operands,
    git_init_operands,
    git_restore_operands,
    git_rm_operands,
    opaque_argument,
    path_verb_operands,
    protected_write_target,
    refuses_generated_plugin_target,
    sed_invocation,
    sed_rewrite_words,
    unlocated_write,
    unread_over_tracked,
    unread_question,
    uv_command_words,
    uv_run_words,
    SCOPE_PHRASES,
    write_checkpoint,
    write_scope,
)


def parse_shell(command: str) -> Script | KernelDecision:
    """One command line as a tree, bound and placed, or why it is not one.

    The only way a reader here reaches a command's words, which is what makes
    the binding pass -- and the placing pass after it, which says where each
    command's words resolve from -- the ones every reader shares.
    """
    tree = parse_script(command)
    if isinstance(tree, KernelDecision):
        return tree
    return place_script(bind_script(tree))


def substitutions(words: list[Word]) -> list[Script]:
    """The commands every `$(…)` and `<(…)` in these words runs, in order."""

    def within(parts: list[WordPart]) -> list[Script]:
        return [
            script
            for item in parts
            for script in [*item["script"], *within(item["parts"])]
        ]

    return [script for word in words for script in within(word["parts"])]


def list_commands(script: Script) -> list[Command]:
    """Every command a list runs directly, in reading order."""
    return [
        command
        for item in script["items"]
        for pipeline in item["andor"]["pipelines"]
        for command in pipeline["commands"]
    ]


def simple_commands(script: Script) -> list[Command]:
    """Every simple command and `[[ ]]` test a list would run, in reading order.

    Nested lists are walked where they stand, and a substitution's commands
    follow the command whose words carry it -- the placement the classifier
    judges them in.
    """
    found: list[Command] = []
    for command in list_commands(script):
        if command["kind"] in ("simple", "test"):
            found.append(command)
        else:
            for inner in command_lists(command):
                found.extend(simple_commands(inner))
        for inner in substitutions(carried_words(command)):
            found.extend(simple_commands(inner))
    return found


# lup: ignore[library-default] — the shell's own directory builtins; omitting one is a move read as no move, not a preference
CHDIR_VERBS = ("cd", "pushd", "popd")


class Move(TypedDict):
    """Where a directory builtin leaves the shell once it has run."""

    directory: str | None
    """``None`` where it landed somewhere nothing here can name."""


def joined_directory(directory: str | None, operand: str) -> str | None:
    """Where an operand names, read from the directory the shell stands in.

    An absolute operand re-establishes a directory the walk had lost, because
    where the shell was standing does not bear on where it lands. Whether the
    operand names a directory at all is settled before this, by the grammar:
    :func:`~lup.policy.kernel.bindings.literal_loop_word` is what a tilde, an
    unsettled parameter, a substitution and a glob each fail.
    """
    if posixpath.isabs(operand):
        return posixpath.normpath(operand)
    if directory is None:
        return None
    return posixpath.normpath(posixpath.join(directory, operand))


# lup: ignore[library-default] — bash's builtins that run the builtin named after them, in this shell
RUNNING_BUILTINS = ("builtin", "command")


def chdir_move(words: list[Word], directory: str | None) -> Move | None:
    """Where this segment leaves the shell, or None where it moves it nowhere.

    Each of these is a shell builtin, so it is the first word, or the word
    after ``builtin`` or ``command``, which run it in this same shell; no
    other wrapper reaches a builtin, and no path spells one. Bash's ``time``
    keyword times it in this shell too, while a ``time`` program on the path
    would run it in a child, so a ``cd`` behind ``time`` lands somewhere
    nothing here can name.

    Only the one-operand ``cd`` naming a word the grammar calls literal is
    read as a move to a named directory. ``cd`` with no operand goes to a home
    this cannot name, ``cd -`` to a directory only the shell's own history
    holds, ``pushd``/``popd`` to a stack nothing here keeps, and an operand
    carrying an expansion to wherever the running shell expands it. Each of
    those is a move that *happened*, so the honest reading is not that the
    shell stayed where it was but that where it stands is no longer known --
    which is what makes a path word after one unresolvable rather than
    resolved against a directory the command never entered.
    """
    spelled = [word_text(word) for word in words]
    if spelled[:1] == ["time"] and any(text in CHDIR_VERBS for text in spelled):
        return Move(directory=None)
    if spelled[:1] and spelled[0] in RUNNING_BUILTINS:
        lead = next(
            (
                index
                for index, text in enumerate(spelled)
                if text not in (*RUNNING_BUILTINS, "-p")
            ),
            len(spelled),
        )
        words = words[lead:]
    if not words or word_text(words[0]) not in CHDIR_VERBS:
        return None
    operands = [word for word in words[1:] if not word_text(word).startswith("-")]
    if (
        word_text(words[0]) != "cd"
        or len(operands) != 1
        or not literal_loop_word(operands[0])
    ):
        return Move(directory=None)
    # lup: defer: a relative operand not opening on `./` or `../` is searched
    # through `$CDPATH` first, so `cd lup` lands in `packages/lup` under a
    # shell holding `CDPATH=packages`. Assigning it on the line already asks
    # (a security-sensitive name), but a CDPATH the session's shell inherited
    # is read by nothing here; the host could hand the dispatcher's own
    # CDPATH in as a fact, and a set one leave such a move unnamed.
    return Move(directory=joined_directory(directory, word_text(operands[0])))


def chdir_within(command: Command) -> bool:
    """Whether anything a construct runs could move the shell."""
    return any(
        chdir_move(inner["words"], "") is not None
        for script in command_lists(command)
        for inner in simple_commands(script)
    )


def placed_parts(parts: list[WordPart], directory: str | None) -> list[WordPart]:
    """Parts whose substituted commands are placed where the word stands.

    A substitution runs in a shell of its own, opened where the word carrying
    it is read -- so its commands start from this directory and whatever they
    do to it reaches nothing outside the parentheses.
    """
    return [
        part(
            item["kind"],
            item["text"],
            item["name"],
            item["operator"],
            parts=placed_parts(item["parts"], directory),
            script=[place_script(inner, directory) for inner in item["script"]],
        )
        for item in parts
    ]


class Outcome(TypedDict):
    """Where the shell stands once something has run, by how it ended.

    ``ok`` where it succeeded and ``failed`` where it did not, because the
    shell's own operators choose what runs next by exactly that: `&&` runs
    the next command where the last one succeeded, `||` where it failed. A
    ``cd`` that fails leaves the shell where it was, so `cd a || rm x` removes
    `x` from the directory the `cd` never left. ``None`` in either is a
    directory nothing here can name.
    """

    ok: str | None
    failed: str | None


def merged(first: str | None, second: str | None) -> str | None:
    """Where the shell stands when it could have come by either of two ways.

    One directory where both ways lead there, and one nothing here can name
    where they part: a word after `true && cd a; …` is read in `a` if the
    `true` succeeded and where the shell already stood if it did not, and
    naming either would judge a file the command may not reach.
    """
    return first if first == second else None


def steady(directory: str | None) -> Outcome:
    """The outcome of something that moves the shell nowhere, however it ends."""
    return Outcome(ok=directory, failed=directory)


class PlacedCommand(TypedDict):
    """One command stamped with where it runs, and where it leaves the shell."""

    command: Command
    outcome: Outcome


def placed_command(command: Command, directory: str | None) -> PlacedCommand:
    """One command stamped with ``directory``, its lists placed where they run.

    A simple command moves the shell only where it is a ``cd`` that
    succeeded. An ``if`` runs each body where its condition succeeded and
    tries the next condition, or the ``else``, where it failed, so `if cd a;
    then rm x; fi` removes `a/x`. A loop may run its lists any number of
    times, each pass starting where the last one left the shell, so one
    that moves the shell anywhere places none of them where a later pass
    would run. A function's body runs wherever the function is called, which
    nothing here follows. A subshell is entered at the top of a list of its
    own and leaves nothing behind it; a brace group runs in this shell, and
    leaves the shell where its last command did. After any other construct
    that could have moved the shell, where it stands is not known.
    """
    within = Command(
        kind=command["kind"],
        words=[
            Word(parts=placed_parts(word["parts"], directory))
            for word in command["words"]
        ],
        redirects=[
            Redirect(
                operator=redirect["operator"],
                target=[
                    Word(parts=placed_parts(target["parts"], directory))
                    for target in redirect["target"]
                ],
                heredoc=redirect["heredoc"],
            )
            for redirect in command["redirects"]
        ],
        name=command["name"],
        listed=command["listed"],
        clauses=command["clauses"],
        body=command["body"],
        arms=command["arms"],
        directory=directory,
    )
    moves = chdir_within(command)
    after = steady(None if moves else directory)
    match command["kind"]:
        case "simple" | "test":
            move = chdir_move(command["words"], directory)
            if move is None:
                return PlacedCommand(command=within, outcome=steady(directory))
            return PlacedCommand(
                command=within,
                outcome=Outcome(ok=move["directory"], failed=directory),
            )
        case "subshell":
            return PlacedCommand(
                command=rebuilt_lists(
                    within, lambda inner: place_script(inner, directory)
                ),
                outcome=steady(directory),
            )
        case "brace":
            body = placed_list(command["body"], directory)
            return PlacedCommand(
                command=rebuilt_lists(within, lambda _inner: body["script"]),
                outcome=body["outcome"],
            )
        case "if":
            clauses: list[Clause] = []
            unmet = directory
            for clause in command["clauses"]:
                condition = placed_list(clause["condition"], unmet)
                body = placed_list(clause["body"], condition["outcome"]["ok"])
                clauses.append(
                    Clause(condition=condition["script"], body=body["script"])
                )
                unmet = condition["outcome"]["failed"]
            otherwise = placed_list(command["body"], unmet)
            return PlacedCommand(
                command=Command(
                    kind=within["kind"],
                    words=within["words"],
                    redirects=within["redirects"],
                    name=within["name"],
                    listed=within["listed"],
                    clauses=clauses,
                    body=otherwise["script"],
                    arms=within["arms"],
                    directory=directory,
                ),
                outcome=after,
            )
        case "for" if unrollable(command):
            # The binding pass laid every pass out in order already, so the
            # body is read once, each pass where the last one left the shell.
            return PlacedCommand(
                command=rebuilt_lists(
                    within, lambda inner: place_script(inner, directory)
                ),
                outcome=after,
            )
        case "for" | "select" | "while" | "until" | "function":
            start = None if moves or command["kind"] == "function" else directory
            return PlacedCommand(
                command=rebuilt_lists(within, lambda inner: place_script(inner, start)),
                outcome=after,
            )
        case _:
            return PlacedCommand(
                command=rebuilt_lists(
                    within, lambda inner: place_script(inner, directory)
                ),
                outcome=after,
            )


class PlacedList(TypedDict):
    """A list with every command stamped, and where its last one leaves the shell."""

    script: Script
    outcome: Outcome


def chained(joined: str, ended: Outcome, before: Outcome) -> Outcome:
    """Where a chain stands once one more pipeline has run, or been skipped.

    `&&` runs it where the chain succeeded, and a failure before it passes
    by untouched; `||` the other way round.
    """
    match joined:
        case "&&":
            return Outcome(
                ok=ended["ok"], failed=merged(ended["failed"], before["failed"])
            )
        case "||":
            return Outcome(ok=merged(ended["ok"], before["ok"]), failed=ended["failed"])
        case _:
            return ended


class PlacedAndOr(TypedDict):
    """One chain placed, where it leaves the shell by how it ended, and after it.

    ``outcome`` routes what the operators run next; ``after`` is where the
    next item starts, which takes each literal `cd` to have succeeded
    (:func:`placed_andor`).
    """

    andor: AndOr
    outcome: Outcome
    after: str | None


def placed_andor(andor: AndOr, directory: str | None) -> PlacedAndOr:
    """One `&&`/`||` chain placed, and where it leaves the shell.

    Each pipeline runs where the one before it left the shell by the ending
    its operator asks for, and one it skips passes the other ending on
    untouched. A pipeline of several commands runs each in a process of its
    own, so none of them moves this shell, and `!` swaps the two endings.

    Where the next item starts is read the same way with one assumption: a
    `cd` naming a directory the line spells is taken to have succeeded. Its
    failure is followed where the line routes on it -- `cd a || rm x` removes
    the `x` beside it -- and not past a `;`, where `cd /abs/wt && make; date
    > tmp/log` writes the log in the tree the `cd` names.
    """

    # lup: defer: a `cd` into a directory that does not exist leaves the shell
    # where it stood, so `cd tmp/missing; rm README.md` removes the human-owned
    # file while it is judged as `tmp/missing/README.md`. Reading every `;`
    # after a `cd` as reaching neither directory closes it and asks about the
    # scratch logs sessions write after `cd /abs/wt && …;` -- 249 of the
    # 16,424 recorded commands, and 57 read-only lines refused in a measured
    # container, where the unknown directory names a landing nobody can
    # place. Carrying both directories on the command and judging a write at
    # each would close it without those questions; every reader of a
    # command's directory would read a list of them.
    def assumed(pipeline: Pipeline, lands: str | None) -> Outcome:
        if len(pipeline["commands"]) != 1:
            return steady(lands)
        (command,) = pipeline["commands"]
        if command["kind"] == "simple":
            move = chdir_move(command["words"], lands)
            if move is not None:
                return steady(move["directory"])
        ended = placed_command(command, lands)["outcome"]
        if pipeline["negated"]:
            return Outcome(ok=ended["failed"], failed=ended["ok"])
        return ended

    pipelines: list[Pipeline] = []
    outcome = steady(directory)
    settled = steady(directory)
    for index, pipeline in enumerate(andor["pipelines"]):
        joined = andor["operators"][index - 1] if index else ""
        runs = outcome["failed"] if joined == "||" else outcome["ok"]
        placed = [placed_command(command, runs) for command in pipeline["commands"]]
        ended = placed[0]["outcome"] if len(placed) == 1 else steady(runs)
        if pipeline["negated"]:
            ended = Outcome(ok=ended["failed"], failed=ended["ok"])
        pipelines.append(
            Pipeline(
                commands=[each["command"] for each in placed],
                operators=pipeline["operators"],
                negated=pipeline["negated"],
            )
        )
        lands = settled["failed"] if joined == "||" else settled["ok"]
        outcome = chained(joined, ended, outcome)
        settled = chained(joined, assumed(pipeline, lands), settled)
    return PlacedAndOr(
        andor=AndOr(pipelines=pipelines, operators=andor["operators"]),
        outcome=outcome,
        after=merged(settled["ok"], settled["failed"]),
    )


def placed_list(script: Script, directory: str | None) -> PlacedList:
    """A list placed in order, each item starting where the last one left the shell.

    An item run in the background with `&` runs in a process of its own, so
    it leaves the shell where it found it. Any other item leaves it where its
    chain does (:func:`placed_andor`), and the list ends as its last item does.
    """
    items: list[Item] = []
    outcome = steady(directory)
    for item in script["items"]:
        placed = placed_andor(item["andor"], directory)
        items.append(Item(andor=placed["andor"], terminator=item["terminator"]))
        backgrounded = item["terminator"] == "&"
        outcome = steady(directory) if backgrounded else placed["outcome"]
        directory = directory if backgrounded else placed["after"]
    return PlacedList(script=Script(items=items), outcome=outcome)


def place_script(script: Script, directory: str | None = "") -> Script:
    """Every command in a list stamped with the directory the shell runs it in.

    What :func:`~lup.policy.kernel.bindings.bind_script` is for variables,
    for the one other thing a segment leaves behind it: where the shell is
    standing. It runs once, over the tree every reader shares, so a path word
    means the same file to the classifier and to the host readers that stat
    it -- rather than each joining the word to a launch directory the command
    had already left.

    A move is followed the way the shell follows it, by :class:`Outcome`:
    through `&&`, `||` and `!`, and into a branch its condition chose. A
    construct that may or may not have moved the shell leaves the directory
    unknown rather than either answer, because the words after it resolve
    against a directory nothing here decided.
    """
    return placed_list(script, directory)["script"]


def placed_path(word: str, directory: str | None) -> str | None:
    """The file a segment's path operand names, spelled from the launch directory.

    The one qualification every reader here shares, so a path word means the
    same file whichever reader pulled it out of the command. An absolute word
    already names its file; a word standing in the launch directory is left
    untouched rather than normalized, so the everyday command's spellings --
    and the fixtures that pin them -- stay exactly what was typed.

    ``None`` where the segment's directory is unknown, which is the reading
    that keeps a wrong resolution from being made: the word names a file, but
    not one this can name, and a caller turns that into a question.
    """
    if posixpath.isabs(word):
        return word
    if directory is None:
        return None
    if not directory:
        return word
    return posixpath.normpath(posixpath.join(directory, word))


# lup: ignore[constant-declaration] — the shell's own name for where it stands, not a choice
UNPLACED_ROOT = "$PWD"
"""How a path named from a directory nothing here can name is spelled.

Where the shell stands is exactly what ``$PWD`` holds, and a word opening on
an expansion is one every rule reads as a path only the run resolves: a write
to one asks, whatever it would have been written as."""


def landed_path(word: str, directory: str | None) -> str:
    """Where a path operand lands, spelled under :data:`UNPLACED_ROOT` where unknown.

    :func:`placed_path` for a reader that has to judge the write rather than
    stat it: a path named from a directory a `cd` left unknown is still a
    write, to a file only the run can name, and a reader that dropped it
    left the write to whatever judged the rest of the command.
    """
    placed = placed_path(word, directory)
    return placed if placed is not None else posixpath.join(UNPLACED_ROOT, word)


class Placement(TypedDict):
    """One simple command's words, and the directory the shell runs them in."""

    words: list[str]
    directory: str | None


def placed_segments(script: Script) -> list[Placement]:
    """Every simple command a list runs, each with the directory it runs in.

    The flattened view a reader takes when it pulls path words out of a
    command's argv, which is every reader that hands a path to a caller able
    to stat it. A `[[ ]]` test reads as the single word `[[`, and a command
    made only of redirections reads as none.
    """
    return [
        Placement(
            words=["[["] if command["kind"] == "test" else texts,
            directory=command["directory"],
        )
        for command in simple_commands(script)
        for texts in [[word_text(word) for word in command["words"]]]
        if texts or command["kind"] == "test"
    ]


def command_segments(script: Script) -> list[list[str]]:
    """The words of every simple command a list runs, each as it reads.

    The one flattened view of a tree, for readers that ask a question of a
    command's argv and none of its structure.
    """
    return [placement["words"] for placement in placed_segments(script)]


def shell_placements(command: str) -> list[Placement]:
    """Every segment of one command line with the directory it runs in.

    A line that does not parse yields nothing, on the same terms as every
    reader that takes a command string: an unparseable line keeps whatever
    verdict it already earned rather than gaining a relaxation from a reading
    that failed.
    """
    tree = parse_shell(command)
    if isinstance(tree, KernelDecision):
        return []
    return placed_segments(tree)


def parse_shell_words(command: str) -> list[list[str]] | KernelDecision:
    """Every simple command's words in one command line, or why it is not read."""
    tree = parse_shell(command)
    if isinstance(tree, KernelDecision):
        return tree
    segments = command_segments(tree)
    if not segments:
        return unjudged("shell command has no executable segment")
    return segments


class PlacedRedirect(TypedDict):
    """One redirection, and the directory the command carrying it runs in."""

    redirect: Redirect
    directory: str | None


def placed_redirects(script: Script) -> list[PlacedRedirect]:
    """Every redirection a list carries, in reading order, substitutions included.

    A compound command's own redirections follow its body, where they are
    written. Each carries the directory of the command it hangs off, because
    a redirection's target is spelled from wherever that command runs.
    """
    found: list[PlacedRedirect] = []
    for command in list_commands(script):
        for inner in command_lists(command):
            found.extend(placed_redirects(inner))
        found.extend(
            PlacedRedirect(redirect=redirect, directory=command["directory"])
            for redirect in command["redirects"]
        )
        for inner in substitutions(carried_words(command)):
            found.extend(placed_redirects(inner))
    return found


def all_pipelines(script: Script) -> list[Pipeline]:
    """Every pipeline a list runs, nested lists included, substitutions not."""
    found: list[Pipeline] = []
    for item in script["items"]:
        for pipeline in item["andor"]["pipelines"]:
            found.append(pipeline)
            for command in pipeline["commands"]:
                for inner in command_lists(command):
                    found.extend(all_pipelines(inner))
    return found


def redirection_writes(operator: str) -> bool:
    """Whether one redirection operator opens its target for writing.

    Every writing form spells ``>``, which also separates them from the
    control operators that carry a following word of their own.
    """
    if ">" not in operator:
        return False
    if "<<" in operator and "<<<" not in operator:
        return False
    return not ("&" in operator and (operator[-1].isdigit() or operator[-1] == "-"))


def python_script_targets(command: str, interpreters: tuple[str, ...]) -> list[str]:
    """Name every script an interpreter segment of this command would run.

    The ladder allows a script where it refuses inline code, on the grounds
    that a file can be read afterwards. That makes "which file" a fact worth
    having: a caller that can reach the filesystem counts how often each one
    is run, and a script being run over and over is one that stopped being
    the one-off the rung was for.

    Judged from the parsed commands rather than from the raw string, so a
    script named inside a pipeline or after a redirection is still found, and
    a command that does not parse yields nothing.
    """
    segments = parse_shell_words(command)
    if not isinstance(segments, list):
        return []
    named: list[str] = []
    for words in segments:
        run = uv_run_words(words) if words[:2] == ["uv", "run"] else words
        if not run or posixpath.basename(run[0]) not in interpreters:
            continue
        rest = run[1:]
        # `-c` and `-m` take their program as the next word, so a plain
        # "not a flag" filter reads that word as a filename and counts a
        # script that does not exist. Neither form names a file at all, and
        # both are refused anyway, so the segment contributes nothing.
        if any(word in ("-c", "-m") for word in rest):
            continue
        named.extend(word for word in rest if not word.startswith("-"))
    return named


class AuthoredWrite(TypedDict):
    """One file a command writes, and the bytes the command itself carries.

    The pair an edit is judged on. A shell write is answered by its path
    alone, on the argument that a redirection produces its content by running
    and there is nothing to read in advance — which is true of `dev render >
    docs/api.md` and false of `cat > f <<'EOF'`, where the bytes are sitting
    in the command. What separates them is exactly this: whether the content
    is here.
    """

    path: str
    content: str
    append: bool


class TeeWrite(TypedDict):
    """The files a `tee` writes through its operands, and how it opens them.

    `tee` is the everyday write that names its files as operands instead of
    through a redirection, so the walk that finds every other authored write
    cannot see it at all. Append is a flag rather than the `>>` that walk
    reads, which is the same fact spelled the utility's own way.
    """

    paths: list[str]
    append: bool


def tee_operands(words: list[str]) -> TeeWrite | None:
    """Where a `tee` puts what it is handed, or ``None`` where it is unmodelled.

    Only the flags that leave the operands meaning what they say are read.
    Anything else returns ``None`` -- `--output-error` and every spelling this
    does not name, and a bare `-`, which is a file called `-` to `tee` and a
    stream to whoever wrote it.
    """
    if not words or posixpath.basename(words[0]) != "tee":
        return None
    paths: list[str] = []
    append = False
    for word in words[1:]:
        if word in ("--", "--ignore-interrupts"):
            continue
        if word == "--append":
            append = True
            continue
        if word.startswith("--") or word == "-":
            return None
        if word.startswith("-") and len(word) > 1:
            if any(letter not in "aip" for letter in word[1:]):
                return None
            append = append or "a" in word
            continue
        paths.append(word)
    return TeeWrite(
        paths=[path for path in paths if not writes_to_a_stream(path)], append=append
    )


# lup: ignore[library-default] — printf's own escape table, spelled the way
# printf spells it; no adopter has a different `\n` to declare here
PRINTF_ESCAPES = {
    "\\": "\\",
    "a": "\a",
    "b": "\b",
    "f": "\f",
    "n": "\n",
    "r": "\r",
    "t": "\t",
    "v": "\v",
}
"""The escapes a format may carry and this reader can state exactly.

An octal `\\ddd` and the output-stopping `\\c` are absent rather than
approximated. Both change what lands, and a document the command never wrote
is worse to hand a gate than no document at all.
"""


def printf_template(text: str) -> list[str] | None:
    """A printf format's literal pieces, split where each `%s` stands.

    The format is printf's whole difficulty, and the reason it was left
    unread: it carries escapes, conversions and an argument cycle, so a
    reading of it that was wrong would hand the gates a document the command
    never writes. What that argues for is a stated grammar rather than an
    absence -- `%s`, `%%`, the escapes :data:`PRINTF_ESCAPES` names, and
    literal text -- and a refusal of everything outside it.

    ``None`` for every other conversion, every escape not named there, and a
    trailing backslash or `%`. Each leaves the write judged by its path, which
    is the answer it had before this read anything.
    """
    pieces = [""]
    index = 0
    while index < len(text):
        character = text[index]
        if character not in ("\\", "%"):
            pieces[-1] += character
            index += 1
            continue
        if index + 1 == len(text):
            return None
        following = text[index + 1]
        match (character, following):
            case ("\\", _) if following in PRINTF_ESCAPES:
                pieces[-1] += PRINTF_ESCAPES[following]
            case ("%", "%"):
                pieces[-1] += "%"
            case ("%", "s"):
                pieces.append("")
            case _:
                return None
        index += 2
    return pieces


def printf_text(arguments: list[str]) -> str | None:
    """The bytes a `printf` writes, where its format states them exactly.

    printf reuses its format until the operands run out, filling a conversion
    with nothing where nothing is left, so what lands is the whole cycle
    rather than one pass. A format carrying no conversion is written once, and
    an operand beside it is simply unread -- which printf's own specification
    leaves unstated, so an operand there is ``None`` rather than a guess.

    A leading `-` is ``None`` for the same reason: `-v` assigns to a variable
    and writes nothing at all, and `--` ends the options without saying which
    word became the format.
    """
    if not arguments or arguments[0].startswith("-"):
        return None
    pieces = printf_template(arguments[0])
    if pieces is None:
        return None
    slots = len(pieces) - 1
    operands = arguments[1:]
    if not slots:
        return pieces[0] if not operands else None
    cycles = max(1, (len(operands) + slots - 1) // slots)
    filled = [*operands, *[""] * (cycles * slots - len(operands))]
    return "".join(
        "".join(
            piece + filled[cycle * slots + position]
            for position, piece in enumerate(pieces[:-1])
        )
        + pieces[-1]
        for cycle in range(cycles)
    )


def carried_text(
    words: list[str], bodies: list[str], incoming: str | None = None
) -> str | None:
    """The bytes a segment's own words carry, or ``None`` where it makes them.

    Four shapes. `cat` handed nothing but a quoted heredoc emits that body;
    `echo` handed literal words emits them; `printf` emits whatever
    :func:`printf_text` can render of its format; and `tee` emits what it was
    handed, since a `tee` copies its input to every file it names.

    ``incoming`` is what the segment before this one piped in -- its standard
    output, where it carried its own bytes and redirected none of them
    elsewhere. `echo 'x = 1' | tee f` is the everyday spelling of the shape
    `tee` is read for, and a segment read on its own is handed nothing.

    ``None`` everywhere the reading is not certain: an unquoted heredoc the
    shell substitutes into, a word carrying an expansion, an `echo` flag that
    changes what the words mean, a printf conversion this cannot render. Each
    of those leaves the write answered by its path, which is the answer a
    write nothing read the content of gets.
    """
    if not words:
        return None
    if any(opaque_argument(word) or expands(word) for word in words):
        return None
    match posixpath.basename(words[0]):
        case "cat":
            return stdin_text(bodies, incoming) if len(words) == 1 else None
        case "printf":
            return None if bodies else printf_text(words[1:])
        case "tee":
            return None if tee_operands(words) is None else stdin_text(bodies, incoming)
        case "echo":
            if bodies:
                return None
            arguments = words[1:]
            trailing = "\n"
            if arguments and arguments[0] == "-n":
                arguments = arguments[1:]
                trailing = ""
            # Every remaining flag changes what the words mean -- `-e` reads
            # escapes, `-E` stops reading them, and a cluster does both -- so
            # a word still shaped like one is a reading this cannot make.
            if any(word.startswith("-") for word in arguments):
                return None
            return " ".join(arguments) + trailing
        case _:
            return None


def authored_writes(command: str) -> list[AuthoredWrite]:
    """Every write this command carries the content of, path and bytes together.

    What it is for: a redirection is judged by its path, because a command
    produces its output by running and nothing could read it first. Where the
    command carries the bytes, that premise is simply untrue, and the gates an
    edit is judged by -- the anti-pattern audit, the review-note gate, the size
    budget -- can read exactly what an `Edit` would have shown them. Measured
    before this: `cat > packages/lup/src/lup/seams.py <<'EOF'` replaced a
    tracked library module with one line, allowed and unprompted, because the
    write row was told the route is reviewed.

    Read per segment, and a segment yields nothing unless the whole of it is
    legible: no substitution, at most one redirection target and that target
    spelling the path it lands on once the bindings are applied, and a content
    shape :func:`carried_text` can state. Everything else keeps the answer it
    had.

    Two routes reach a file rather than one. A redirection names its target in
    the operator that follows the command, and `tee` names its targets as
    operands -- one utility, but the everyday one, and what it is handed comes
    down a pipe as often as from a heredoc. So the walk carries the segment's
    standard output forward across a `|`, which is what lets `echo 'x = 1' |
    tee packages/lup/src/lup/seams.py` be read as the module replacement it is.
    """
    tree = parse_shell(command)
    if isinstance(tree, KernelDecision):
        return []
    return carried_writes(tree)


def carried_writes(tree: Script) -> list[AuthoredWrite]:
    """The same reading over a script already parsed, for a caller holding one.

    :func:`authored_writes` is the entry from a command line; the redirection
    verdict runs over the tree it parsed once, and re-parsing to ask whether
    this redirection's bytes are in the call would be the same walk twice.
    """
    authored: list[AuthoredWrite] = []
    for pipeline in all_pipelines(tree):
        incoming: str | None = None
        for index, node in enumerate(pipeline["commands"]):
            if node["kind"] != "simple":
                incoming = None
                continue
            piping = pipeline["operators"][index : index + 1] == ["|"]
            writes = node_writes(node, incoming, piping)
            authored.extend(writes["authored"])
            incoming = writes["outgoing"]
    return authored


def stdin_text(bodies: list[str], incoming: str | None) -> str | None:
    """What a command reads on its input, taking a heredoc over a pipe as the shell does.

    ``bodies`` are its heredocs' texts, an unquoted one as ``""`` because the
    shell substitutes into it; ``incoming`` is what the command before it
    piped in, where that command's bytes were its own.
    """
    if not bodies:
        return incoming
    return bodies[0] if len(bodies) == 1 and bodies[0] else None


class NodeWrites(TypedDict):
    """What one command writes through its redirections and a `tee`, and what it reads.

    ``authored`` are the writes whose bytes the command carries, placed;
    ``unread`` the files it opens for writing with bytes only running makes,
    placed too; ``unplaced`` whether it writes somewhere no placed word names.
    ``outgoing`` is what it hands the next command down a pipe, where those
    bytes are its own. ``read_from`` is the file a `<` hands it, placed, and
    ``stdin`` the bytes a quoted heredoc or the pipe hands it instead.
    """

    authored: list[AuthoredWrite]
    unread: list[str]
    unplaced: bool
    outgoing: str | None
    read_from: str | None
    stdin: str | None


def node_writes(node: Command, incoming: str | None, piping: bool) -> NodeWrites:
    """One command's writes, handed what the command before it piped in.

    The reading :func:`carried_writes` takes of every command, and the one a
    reader following a line file by file takes too, so the two cannot differ
    about what a command lands. A compound command's own redirections are read
    the same way, with no words of its own: what lands through them is
    whatever its body prints.
    """
    words = (
        [word_text(word) for word in node["words"]] if node["kind"] == "simple" else []
    )
    legible = not substitutions(carried_words(node))
    bodies: list[str] = []
    targets: list[str] = []
    appends: list[bool] = []
    sources: list[str | None] = []
    # A glob or a brace expansion names whatever it matches when the command
    # runs, so a word spelled with one names no file this could place.
    unliteral = [
        word_text(word)
        for word in node["words"]
        if node["kind"] == "simple" and not literal_loop_word(word)
    ]
    for redirect in node["redirects"]:
        operator = redirect["operator"]
        if redirect["heredoc"]:
            bodies.extend(
                heredoc["body"] if heredoc["quoted"] else ""
                for heredoc in redirect["heredoc"]
            )
            continue
        if "&" in operator and (operator[-1].isdigit() or operator[-1] == "-"):
            continue
        if not redirect["target"]:
            legible = False
            continue
        spelled = word_text(redirect["target"][0])
        literal = spells_its_path(spelled) and literal_loop_word(redirect["target"][0])
        if not literal:
            unliteral.append(spelled)
        # `<` and `0<` hand the command a file to read, which is where a patch
        # it applies comes from; `<<<` and `<>` are neither.
        if operator.endswith("<") and not operator.endswith("<<"):
            sources.append(placed_path(spelled, node["directory"]) if literal else None)
            continue
        # A stream sink is not a target here for the reason it is not
        # one in `shell_write_targets`: nothing is destroyed and no
        # gate has anything to read. Dropped as it is met rather than
        # at the end, or a `2>/dev/null` beside the write would count
        # as a second target and take the whole command out of reach.
        if redirection_writes(operator) and not writes_to_a_stream(spelled):
            # A target the binding pass could not resolve names no
            # file, and naming none reads here exactly as naming one
            # that does not exist: `cat > $P` judged a create takes an
            # overwrite of tracked source past the size budget, the
            # note gate and the audit alike, and shows a reviewer a
            # `$P` they cannot resolve. Illegible, so the redirection
            # row answers it in the words it already has for a path
            # known only when the command runs -- as is `> *.py`, which
            # writes whichever file the glob matches.
            legible = legible and literal
            targets.append(spelled)
            appends.append(">>" in operator)
    content = carried_text(words, bodies, incoming) if legible else None
    piped = tee_operands(words)
    # An operand that names no path is illegible for the reason a
    # redirection target is, and the tee is judged by that path alone.
    landings = [
        *(
            []
            if piped is None
            else [
                (path, piped["append"])
                for path in piped["paths"]
                if spells_its_path(path) and path not in unliteral
            ]
        ),
        *([(targets[0], appends[0])] if len(targets) == 1 else []),
    ]
    authored = [
        AuthoredWrite(path=placed, content=content, append=append)
        for path, append in landings
        if content is not None
        for placed in [placed_path(path, node["directory"])]
        if placed is not None
    ]
    carried = [write["path"] for write in authored]
    opened = [
        placed_path(path, node["directory"])
        if spells_its_path(path) and path not in unliteral
        else None
        for path in [*targets, *([] if piped is None else piped["paths"])]
    ]
    # A `tee` whose flags nothing models writes to files nobody named.
    unmodelled = (
        piped is None and posixpath.basename(words[0] if words else "") == "tee"
    )
    return NodeWrites(
        authored=authored,
        unread=list(
            dict.fromkeys(
                path for path in opened if path is not None and path not in carried
            )
        ),
        unplaced=None in opened or unmodelled,
        outgoing=content if piping and not targets else None,
        read_from=sources[-1] if sources else None,
        stdin=None if sources else stdin_text(bodies, incoming),
    )


def shell_write_targets(command: str) -> list[str]:
    """Name every path this command's redirections would open for writing.

    A caller that can reach the filesystem stats these and hands back the
    ones that already exist, so the kernel can tell creating a file from
    overwriting one without ever reading the filesystem itself. A command
    that does not parse yields nothing and keeps its unjudged verdict.

    A stream sink is not among them. :func:`writes_to_a_stream` already holds
    that a redirection into one destroys nothing, and every caller asks a
    filesystem question of what this hands back -- whether the target exists,
    whether a capture holds it, whether the lease covers it. ``/dev/null``
    answers all three the wrong way: it exists, no capture holds it, and no
    writable root contains it, so naming it here puts an approval question in
    front of every ``2>/dev/null``.
    """
    tree = parse_shell(command)
    if isinstance(tree, KernelDecision):
        return []
    return [
        placed
        for carried in placed_redirects(tree)
        if redirection_writes(carried["redirect"]["operator"])
        for target in carried["redirect"]["target"]
        for spelled in [word_text(target)]
        if not writes_to_a_stream(spelled)
        for placed in [placed_path(spelled, carried["directory"])]
        if placed is not None
    ]


def resolve_redirection(
    redirect: Redirect,
    existing_targets: list[str] | None = None,
    path_roles: list[PathRoleRow] | None = None,
    path_rules: list[PathRuleRow] | None = None,
    recoverable_targets: list[str] | None = None,
    contained: bool = False,
    checkout_root: str = "",
    landing: list[str] | None = None,
    tracked_targets: list[str] | None = None,
    displaced_targets: list[DisplacedTargetRow] | None = None,
    directory: str | None = "",
) -> KernelDecision | None:
    """Classify one redirection, or ``None`` where it is safe.

    The operator decides whether it writes at all: a heredoc, a descriptor
    duplication and a read open nothing. Where it does, its target is judged
    by :func:`written_path_verdict`, which is the judgement a `tee` operand
    gets too, at the file it names from ``directory`` -- where the command
    carrying it runs. Judged as spelled, `cd tests && ls > ../README.md` was
    a create outside the checkout rather than an overwrite of a human-owned
    file, while the host stat'd the file the command reaches. ``landing`` is
    where the line's carried writes land, placed the same way.
    """
    operator = redirect["operator"]
    if redirect["heredoc"]:
        return None
    if "&" in operator and (operator[-1].isdigit() or operator[-1] == "-"):
        return None
    if not redirect["target"]:
        return KernelDecision(
            "ask",
            f"file redirection {operator} names no target, so where it"
            " writes is unknown",
            # The shell refuses the line before anything is opened, so nothing
            # is written anywhere a container does not hold.
            reach="container",
        )
    if not redirection_writes(operator):
        return None
    spelled = word_text(redirect["target"][0])
    if writes_to_a_stream(spelled):
        return None
    placed = placed_path(spelled, directory)
    if placed is None:
        # Wherever the `cd` left the shell, a protected file among the places
        # it could be: asked about as a path only the run resolves, which no
        # boundary settles, rather than handed to one that confines the call
        # and not the checkout it writes in.
        return unlocated_write(
            f"the redirection target {landed_path(spelled, directory)}"
        ).revised(
            recovery="Spell the path in full, or run the command in its own call."
        )
    return written_path_verdict(
        placed,
        "the redirection",
        existing_targets,
        path_roles,
        path_rules,
        contained,
        checkout_root,
        placed in (landing or []),
        tracked_targets,
        displaced_targets,
    )


def written_path_verdict(
    spelled: str,
    writer: str,
    existing_targets: list[str] | None = None,
    path_roles: list[PathRoleRow] | None = None,
    path_rules: list[PathRuleRow] | None = None,
    contained: bool = False,
    checkout_root: str = "",
    carried: bool = False,
    tracked_targets: list[str] | None = None,
    displaced_targets: list[DisplacedTargetRow] | None = None,
) -> KernelDecision | None:
    """Classify one path a command opens and writes whole, or ``None`` where safe.

    ``writer`` names the spelling in the reason -- the redirection, the tee
    -- and nothing else about it is consulted: `> f` and `| tee f` land the
    same bytes at the same path, so a difference between their answers could
    only be a difference in which code happened to read them.

    What a write costs decides it, exactly as it decides for ``rm`` and
    ``cp``. Creating a file destroys nothing, so it passes once the caller
    has established the target is not there. Replacing one Git can restore
    byte for byte costs a checkout rather than any information, so it passes
    too. Anything else overwrites something no one can bring back, and asks.
    ``existing_targets`` of ``None`` means no caller established anything,
    and every target is treated as already there.

    The shape of the writing command is deliberately not consulted. A heredoc
    body and an ``echo`` argument author identical content, and the create
    case already admits both — so gating one of them on an existing path drew
    the line where the cost was lowest rather than where the risk was. What
    the edit gate reads is content, and that gate is reached through Edit and
    Write, not by re-deriving a weaker copy of it here.

    A generated plugin tree is refused ahead of every relaxation, including
    the create case: authoring a file there by hand is editing a build
    product, whether or not one is already sitting at that path. Only this
    checkout's own scratch is exempt, where no generated tree lands, and a
    target the host found landing elsewhere is not exempted by its spelling.
    """
    refused = refuses_generated_plugin_target(
        spelled, path_roles, checkout_root, displaced_targets
    )
    if refused is not None:
        return refused
    protected = protected_write_target(
        [spelled],
        path_rules or [],
        existing_targets is None or spelled in existing_targets,
        path_roles or [],
    )
    if protected is not None:
        return protected
    scope = write_scope(spelled, path_roles or [], checkout_root)
    # A target still carrying an expansion names no path to scope, so what a
    # reviewer would be shown is `$B` and where that lands is the question.
    # Read off the scope rather than before it, because a declared root is one
    # of the things a role recognizes *through* the variable that names it:
    # `$TMPDIR/out.txt` spells no path and is still scratch.
    if scope == "unbounded":
        return unlocated_write(f"{writer} target {spelled}")
    existing = existing_targets is None or spelled in existing_targets
    if unread_over_tracked(
        scope, carried, existing, spelled in (tracked_targets or [])
    ):
        return unread_question(spelled)
    effects = [
        declare(
            "writes_path",
            scope=scope,
            write="overwrite" if existing else "create",
            # Reviewed, because the gates do read what this wrote --
            # afterwards, against the file itself. That is the whole of
            # what stops the row refusing here: a redirection produces its
            # content by running, so nothing could read it in advance, and
            # a refusal on the strength of "nobody read this" would be
            # refusing the only writes for which that is unavoidable.
            reviewed=True,
        )
    ]
    evidence = EffectEvidence(existing=existing)
    placement = "inside" if contained else "ambient"
    decided = verdict_for(effects, evidence, placement)
    if decided == "allow":
        return None
    written = "overwrites" if existing else "creates"
    return KernelDecision(
        decided,
        f"{writer} {written} {spelled}, {SCOPE_PHRASES[scope]}",
        checkpoint=write_checkpoint(scope),
        purpose="unrecovered_local_mutation",
        # Where the write lands is what settles it in a container: the
        # container's own directory holds it, and one the host lent does not.
        reach=question_reach(effects, evidence, placement),
    )


def tee_targets(script: Script) -> list[str]:
    """Every file a `tee` in this list writes through its own operands, placed.

    Only the operands the command itself names. A `tee` handed operands by
    `xargs` or `find -exec` writes files no word here spells, so it is not
    read here and keeps its own row's question. An operand whose directory a
    `cd` left unreadable is a file only the run can name (:func:`landed_path`),
    and is judged as one, as `> f` after the same `cd` is.
    """
    return [
        landed_path(path, command["directory"])
        for command in simple_commands(script)
        if command["kind"] == "simple"
        for tee in [
            tee_operands(
                effective_command([word_text(word) for word in command["words"]])[
                    "words"
                ]
            )
        ]
        if tee is not None
        for path in tee["paths"]
    ]


def named_write_verdict(
    script: Script,
    existing_targets: list[str] | None = None,
    path_roles: list[PathRoleRow] | None = None,
    path_rules: list[PathRuleRow] | None = None,
    recoverable_targets: list[str] | None = None,
    contained: bool = False,
    checkout_root: str = "",
    tracked_targets: list[str] | None = None,
    displaced_targets: list[DisplacedTargetRow] | None = None,
) -> KernelDecision | None:
    """The first file this line opens and writes whole that is not safe.

    Two spellings name such a file: a redirection's target and a `tee`
    operand. Both are judged by :func:`written_path_verdict`, so `> f` and
    `| tee f` reach one answer for one path. Measured before this, a write
    into a sibling checkout's `tmp/` was allowed by the redirection inside
    the sandbox and asked by `tee` in both placements.

    Each is told whether the command carries the bytes it writes, because that
    is what decides who judges the content: the edit gates read a write whose
    content is in the call, and nothing reads one produced by running.
    """
    landing = [write["path"] for write in carried_writes(script)]
    redirected = [
        resolve_redirection(
            carried["redirect"],
            existing_targets,
            path_roles,
            path_rules,
            recoverable_targets,
            contained,
            checkout_root,
            landing,
            tracked_targets,
            displaced_targets,
            carried["directory"],
        )
        for carried in placed_redirects(script)
    ]
    teed = [
        written_path_verdict(
            path,
            "the tee",
            existing_targets,
            path_roles,
            path_rules,
            contained,
            checkout_root,
            path in landing,
            tracked_targets,
            displaced_targets,
        )
        for path in tee_targets(script)
    ]
    return next(
        (decided for decided in [*redirected, *teed] if decided is not None), None
    )


def shell_flag_write_targets(command: str, rows: list[ShellRuleRow]) -> list[str]:
    """Name every path a declared write flag in this command lands a file at.

    The third way a command names something it writes, beside a redirection
    and a path verb's operand, and the one that needs the table: `-o` is a
    path for `sort` and nothing at all for `rg`, so which words are files is
    a fact about the rule rather than about the shell.

    Every row that could match the executable contributes its flags, rather
    than the one row that will: which subcommand and operation a command
    resolves to is the matcher's own walk, and repeating it here would be a
    second copy of it to keep in step. Gathering wide costs nothing *provided
    what reads this only relaxes* -- a path named for a flag the matched row
    does not guard is a fact nobody consults.

    That proviso is the whole of why these stay out of the lease's target
    list. Naming a word the command does not write is what reports `sed`'s
    script as a write outside the lease, and the lease is the one reader that
    escalates on what it is handed.

    Read through :func:`read_segments` like every other path reader here, so
    the words are the ones the classifier matched a row against and the
    directory is the command's own joined with the one a `cd` left. Taking
    the placement directly reads the subcommand one word further along than
    the classifier does, and resolves an operand from the `cd` alone — so
    `git -C ../other <sub> -o out.txt` names `out.txt` where the verb reader
    beside it names `../other/out.txt`. A stream a flag names -- `curl -o
    /dev/null` -- is no file, as a redirection into one is not.
    """
    return [
        placed
        for segment in read_segments(command, rows)
        for executable in [posixpath.basename(segment["words"][0])]
        for declared in [
            [
                flag
                for row in rows
                if row["command"] == executable
                for flag in row["write_flags"]
            ]
        ]
        for target in [
            *flag_write_targets(segment["words"], declared),
            *download_targets(segment["words"]),
        ]
        if not writes_to_a_stream(target)
        for placed in [placed_path(target, segment["directory"])]
        if placed is not None
    ]


def shell_patch_operands(command: str, rows: list[ShellRuleRow]) -> list[str]:
    """Name every patch file this command hands to something that applies one.

    The fourth way a command names what it writes, and the only one that names
    it indirectly: the operand is a description of the write rather than its
    target. What reads this turns each into the paths the patch touches by
    asking Git, so the two steps stay on the side of the boundary that can do
    them -- the words here, the file's contents there.

    A command that does not parse yields nothing, on the same terms as every
    reader beside it: an unparseable line keeps whatever verdict it already
    earned rather than gaining a relaxation from a reading that failed.
    """
    return [
        placed
        for segment in read_segments(command, rows)
        for patch in git_apply_words(segment["words"], rows)
        for placed in [placed_path(patch["path"], segment["directory"])]
        if placed is not None
    ]


def shell_path_verb_targets(command: str, rows: list[ShellRuleRow]) -> list[str]:
    """Name every operand a path-writing verb in this command acts on.

    ``git restore`` is one of them: it rewrites the paths it names from the
    index, so whether that costs anything is the same filesystem question the
    delete verbs ask.

    A caller that can reach the filesystem resolves facts about these — which
    are directories, which Git could restore — and hands them back, so the
    kernel decides from primitive data without ever reading a disk. Naming
    only these operands keeps that resolution proportional to the command: a
    session running ``ls`` pays for none of it.

    Under-naming is conservative, because the answers are consulted as a table
    of facts rather than trusted as a target list — the kernel decides for
    itself which words a verb writes. Over-naming is *not* the same kind of
    safe, though it reads that way: two of the three questions asked of these
    stat the path and drop whatever is not on disk, and the third resolves the
    string and asks whether it sits under a writable root. A word that is not
    a path at all still answers that one, and answers it wrongly.

    So ``sed`` names the files it rewrites in place, read by the one reader
    the classifier judges it with, and nothing where it only prints: neither
    its script, which is a program rather than a path, nor the file it reads,
    which is at the path unchanged afterwards. A command that does not parse
    yields nothing and keeps its unjudged verdict. A stream a `tee` or a copy
    only writes into is not named either, as no redirection into one is: it
    is a device every process holds, and the lease question answers it wrongly.
    """
    return [
        placed
        for segment in read_segments(command, rows)
        for operand in verb_path_words(segment["words"], rows)
        if not fills_a_stream(segment["words"], operand["path"])
        for placed in [placed_path(operand["path"], segment["directory"])]
        if placed is not None
    ]


def command_rows(words: list[str], rows: list[ShellRuleRow]) -> list[ShellRuleRow]:
    """This command's own rows, which are where its globals are declared."""
    executable = posixpath.basename(words[0])
    return [
        row for row in rows if row["command"] == executable and not row["subcommand"]
    ]


def command_directory(words: list[str], rows: list[ShellRuleRow]) -> str | None:
    """Where this command runs its paths from, when a global of its own says so.

    A second way a command reaches a directory, beside the ``cd`` before it:
    ``git -C ../other restore src/mod.py`` restores a file in another
    checkout, and read without this it named ``src/mod.py`` in *this* one --
    so whether the loss was captured, and the roles the path is judged under,
    were both answered about a file the command was never going to touch.

    Only the globals a rule declares as naming a directory, which is narrower
    than the globals that consume a word: ``--git-dir`` names a repository and
    moves no operand, and ``--work-tree`` moves a pathspec alone, which
    :func:`command_words_read` places. Only before the subcommand, because
    after it the spelling is somebody else's -- ``git commit -C`` reuses a
    message.

    ``""`` where the command names none, and ``None`` where it names one this
    cannot read -- a work tree included -- because a guess there puts every
    path in a directory the command may never have entered.
    """
    if git_work_tree(words, rows) is None:
        return None
    head = words[: global_span(words, rows)]
    directory = ""
    declared = [
        flag for row in command_rows(words, rows) for flag in row["directory_flags"]
    ]
    for named in flag_write_words(head, declared):
        if opaque_argument(named["path"]):
            return None
        directory = posixpath.join(directory, named["path"])
    return directory


def git_work_tree(words: list[str], rows: list[ShellRuleRow]) -> str | None:
    """The tree a git command reads its pathspecs from, where it stands outside it.

    `--work-tree` names the tree git checks files out into. Standing inside
    it, git reads a pathspec from where it stands, so nothing moves; standing
    outside -- the case the flag exists for -- it reads one from the tree's
    top, and `git --work-tree=/home/x restore .bashrc` rewrites
    `/home/x/.bashrc`. A relative tree is read from where `-C` stood git, and
    one that only climbs holds the directory it climbs from. An absolute tree
    is read as one the command stands outside or at the top of, which is
    where a session stands.

    ``""`` where the command names no tree or one it stands inside, and
    ``None`` where it names one this cannot read.
    """
    if not words or posixpath.basename(words[0]) != "git":
        return ""
    head = words[: global_span(words, rows)]
    named = [row["path"] for row in flag_write_words(head, ["--work-tree"])]
    if not named:
        return ""
    if opaque_argument(named[-1]):
        return None
    climbed = PurePosixPath(posixpath.normpath(named[-1])).parts
    return "" if all(part in (".", "..") for part in climbed) else named[-1]


def git_worked_tree(
    words: list[str], directory: str | None, rows: list[ShellRuleRow]
) -> str | None:
    """Where a git command changes a working tree, when a global moved it there.

    The tree `--work-tree` names where the command stands outside it, and
    otherwise the directory `-C` stands git in, each read from ``directory``,
    where the shell stands. A verb that names no path -- `reset --hard`, a
    bare `clean` -- changes that whole tree, which is why it is named apart
    from any operand. ``""`` where no global moved it, and ``None`` where one
    moved it somewhere this cannot read.
    """
    if not words or posixpath.basename(words[0]) != "git":
        return ""
    moved = command_directory(words, rows)
    if moved is None:
        return None
    here = directory if not moved else joined_directory(directory, moved)
    tree = git_work_tree(words, rows)
    if tree:
        return joined_directory(here, tree)
    return here if moved else ""


def shell_worked_trees(command: str, rows: list[ShellRuleRow]) -> list[str]:
    """Every tree a git segment of this command changes away from where it stands.

    ``$`` for one no reader can place, as the posture question takes a path
    it cannot place.
    """
    return [
        "$" if tree is None else tree
        for spelled in shell_placements(command)
        for tree in [
            git_worked_tree(
                effective_command(spelled["words"])["words"], spelled["directory"], rows
            )
        ]
        if tree != ""
    ]


def command_words_read(words: list[str], rows: list[ShellRuleRow]) -> list[str]:
    """This command with the globals a rule declares as value-carrying removed.

    Every reader of a subcommand matches it where it is written -- ``git``,
    then ``restore`` -- so one global in front of it made each of them answer
    ``None`` about a command they do model. The row still matched, because
    the matcher steps over these globals to find the subcommand; the readers
    the row's verdict is then relaxed or tightened by did not, so ``git -C .
    restore <protected path>`` reached neither the ownership gate nor the
    index check, and was allowed where the same restore spelled without the
    flag asked.

    So they are consumed here, once, and every reader below sees the command
    as it would have been written without them. Only the value-carrying ones:
    a global the row asks about -- ``git -c`` -- stays exactly where it is,
    because the question it raises is the row's to raise.

    A work tree the command stands outside is consumed into its pathspecs:
    each operand a path verb reads is spelled from the tree, as git reads it,
    while a flag's file stays where the command stands -- `git
    --work-tree=../wt diff --output=o.txt` writes `./o.txt`.
    """
    head = words[: global_span(words, rows)]
    valued = [flag for row in command_rows(words, rows) for flag in row["value_flags"]]
    consumed = [
        position
        for named in flag_write_words(head, valued)
        for position in (
            [named["at"]] if named["prefix"] else [named["at"] - 1, named["at"]]
        )
    ]
    read = [word for index, word in enumerate(words) if index not in consumed]
    tree = git_work_tree(words, rows)
    if not tree:
        return read
    moved = {
        operand["at"]: f"{operand['prefix']}{posixpath.join(tree, operand['path'])}"
        for operand in verb_path_words(read, rows)
        if not posixpath.isabs(operand["path"])
    }
    return [moved.get(index, word) for index, word in enumerate(read)]


def path_words(words: list[str], rows: list[ShellRuleRow]) -> list[PathWord]:
    """Every word this segment names a file with, by whichever route names it.

    The union of the three routes a word reaches a file by inside a segment --
    a verb's operand, a declared write flag's value, a patch handed to
    something that applies one. A redirection is the fourth, and it is not
    here because it is not among these words: it hangs off the command, and
    :func:`placed_redirects` carries it with its own directory.

    Each route reports the word it read rather than the path it read, so a
    caller resolving one puts it back where it was found. Naming a word twice
    costs nothing: the routes overlap where a verb's operand is also a
    declared flag's value, and both name the same word to the same end.
    """
    executable = posixpath.basename(words[0])
    declared = [
        flag
        for row in rows
        if row["command"] == executable
        for flag in row["write_flags"]
    ]
    return [
        *verb_path_words(words, rows),
        *flag_write_words(words, declared),
        *git_apply_words(words, rows),
    ]


class ReadSegment(TypedDict):
    """One segment as every reader of it sees it: its words, and where they resolve.

    The composition each reader would otherwise make for itself, made once:
    the wrappers stepped over, the command's own value-carrying globals
    consumed, and the directory those globals name joined onto the one a
    ``cd`` left. A reader taking this cannot see a different command from the
    classifier, which is the disagreement that let a global before a
    subcommand blind every reader while the row matched anyway.
    """

    words: list[str]
    directory: str | None


def uv_run_placement(words: list[str], directory: str | None) -> Placement | None:
    """The command a `uv run` hands its words to, standing where uv runs it.

    `uv run` puts this project's environment on the path and runs the words
    after its own options as their own command, from the directory
    `--directory` names where one does -- uv's global spelling or run's own.
    ``None`` where the words are not a `uv run` of a program: another verb,
    nothing to run, or `-c` and `-m` standing where a program would.
    """
    if not words or posixpath.basename(words[0]) != "uv":
        return None
    normalized = uv_command_words(words)
    if normalized is None or normalized[1:2] != ["run"]:
        return None
    program = uv_run_words(normalized)
    if not program or program[0].startswith("-"):
        return None
    options = normalized[2 : len(normalized) - len(program)]
    moved = [
        carried["value"]
        for position, word in enumerate(options)
        if (carried := carried_setting(word, ["--directory"], options[position + 1 :]))[
            "value"
        ]
    ]
    if not moved:
        return Placement(words=program, directory=directory)
    if opaque_argument(moved[-1]):
        return Placement(words=program, directory=None)
    return Placement(words=program, directory=joined_directory(directory, moved[-1]))


def read_segments(command: str, rows: list[ShellRuleRow]) -> list[ReadSegment]:
    """Every segment of one command line, as the readers of it see them.

    A `uv run` handing its words to a program is read as that program, where
    uv runs it: `uv run rm x` removes the `x` that `rm x` removes, and a
    reader that stopped at `uv` named nothing the host could place.
    """
    return [
        segment
        for spelled in shell_placements(command)
        for segment in [read_segment(spelled, rows)]
        if segment is not None
    ]


def read_segment(spelled: Placement, rows: list[ShellRuleRow]) -> ReadSegment | None:
    """One segment as the readers of it see it, or ``None`` where it runs nothing.

    For a reader walking a line's structure itself, which holds each simple
    command as it goes rather than the flattened list :func:`read_segments`
    reads.
    """
    effective = effective_command(spelled["words"])["words"]
    handed = uv_run_placement(effective, spelled["directory"])
    placement = (
        Placement(words=effective, directory=spelled["directory"])
        if handed is None
        else Placement(
            words=effective_command(handed["words"])["words"],
            directory=handed["directory"],
        )
    )
    words = placement["words"]
    if not words:
        return None
    carried = command_directory(words, rows)
    return ReadSegment(
        words=command_words_read(words, rows),
        directory=None
        if carried is None
        else placement["directory"]
        if not carried
        else joined_directory(placement["directory"], carried),
    )


class PlacedWords(TypedDict):
    """One segment's words with every path spelled where it lands.

    ``unplaced`` where at least one path was named from a directory nothing
    here can name, and so spelled under :data:`UNPLACED_ROOT`.
    """

    words: list[str]
    unplaced: bool


def placed_words(
    words: list[str], directory: str | None, rows: list[ShellRuleRow]
) -> PlacedWords:
    """This segment as it would read with nothing standing between its words.

    Two things stand between them, and both make a rule answer about a file
    the command was never going to touch. A ``cd`` before the segment moves
    where every operand resolves from; a global of the command's own —
    ``git -C`` — moves it again, and sits where each reader of a subcommand
    expects the subcommand. So the globals are consumed into the directory
    and out of the words, the operands are resolved against what the two name
    together, and every rule below reads one spelling of one command without
    being handed a directory of its own to remember.

    Where that directory is unknown and a relative path is named from it, the
    path is spelled under :data:`UNPLACED_ROOT`: the words name something,
    but nothing this can resolve, so a rule reads it as the path only the run
    knows that it is, and a write to it asks, rather than answering about
    whichever file the spelling happens to name here. An absolute path names
    its own file wherever the shell stands. A command naming no path is not
    made unjudgeable by an unreadable directory -- ``git -C "$W" add -A``
    stages whatever is in a checkout nobody here can name, and staging is
    reversible wherever it happens, so the verb behind the flag answers as it
    always did.
    """
    if not words:
        return PlacedWords(words=words, unplaced=False)
    read = command_words_read(words, rows)
    named = path_words(read, rows)
    carried = command_directory(words, rows)
    here = (
        None
        if carried is None
        else directory
        if not carried
        else joined_directory(directory, carried)
    )
    if not named or here == "":
        return PlacedWords(words=read, unplaced=False)
    placed = {
        row["at"]: f"{row['prefix']}{landed_path(row['path'], here)}" for row in named
    }
    return PlacedWords(
        words=[placed.get(index, word) for index, word in enumerate(read)],
        unplaced=any(placed_path(row["path"], here) is None for row in named),
    )


def verb_path_words(words: list[str], rows: list[ShellRuleRow]) -> list[PathWord]:
    """The words one segment's path-writing verb reads its operands out of.

    A restore's, a checkout's and a removal's are found past git's globals, as
    every reader naming paths finds them: `git --no-pager restore <path>` and
    `git checkout HEAD -- <path>` rewrite the path, and a reading that missed
    it placed nothing and asked the host about nothing.
    """
    restore = git_restore_operands(words, global_span(words, rows))
    if restore is not None:
        return restore["named"]
    checkout = git_checkout_operands(words, global_span(words, rows))
    if checkout is not None:
        return checkout
    removed = git_rm_operands(words, rows)
    if removed is not None:
        return removed
    initialized = git_init_operands(words, global_span(words, rows))
    if initialized is not None:
        return initialized["named"]
    archived = archive_write(words)
    if archived is not None:
        return archived["named"]
    rewritten = sed_rewrite_words(words)
    if rewritten is not None:
        return rewritten
    if posixpath.basename(words[0]) not in PATH_VERBS:
        return []
    return path_verb_operands(words)["named"]


def written_verb_words(words: list[str], rows: list[ShellRuleRow]) -> list[PathWord]:
    """The words one segment's path verb writes, out of those it acts on.

    :func:`verb_path_words` names a copy's sources and a link's target too,
    which is right for what reads it -- facts consulted about a path. A
    refusal is not consulted, so the operands a copy or a link only reads are
    left out here, as :func:`~kernel.words.written_operands` leaves them out
    of the loss: they are at their paths unchanged afterwards. A move keeps
    every operand, because its source is unlinked.
    """
    named = verb_path_words(words, rows)
    executable = posixpath.basename(words[0]) if words else ""
    if executable not in PATH_VERBS or len(named) < 2:
        return named
    if PATH_VERBS[executable]["lands"] != "last":
        return named
    return named[-1:] if path_verb_operands(words)["inert"] else named


def shell_written_targets(command: str, rows: list[ShellRuleRow]) -> list[str]:
    """Name every operand a path verb in this command writes, placed.

    :func:`shell_path_verb_targets` narrowed to the writes, for a reader that
    refuses on what it is handed; a flag it cannot read widens back to every
    operand, the conservative reading a refusal owes.
    """
    return [
        placed
        for segment in read_segments(command, rows)
        for operand in written_verb_words(segment["words"], rows)
        if not fills_a_stream(segment["words"], operand["path"])
        for placed in [placed_path(operand["path"], segment["directory"])]
        if placed is not None
    ]


def guarded_write_targets(
    command: str,
    rows: list[ShellRuleRow],
    displaced: list[DisplacedTargetRow] | None = None,
) -> list[str]:
    """Name every path this command writes that is one of git's pointers.

    The guarded half of the read-only regions a launch measures, and guarded
    rather than mounted for a reason the files themselves give: `git worktree
    remove` unlinks exactly these, and a read-only bind would refuse it. So
    they are recognized by what they are, from the spelling, wherever the
    command reaches them -- a redirection, `tee`, `cp`, `mv`, `ln`, `truncate`,
    a write flag, or a `mv` of an entry of `worktrees/` itself -- and by where
    a link the host resolved lands, so a symlink planted in scratch reaches
    nothing through its own harmless name.

    Pointers and not refs. A pointer is where host git reads its configuration
    from, which is the config wall by another route; a ref names a commit, and
    a write to one keeps the question its path already earns.

    The flag targets are gathered wide -- every row of the executable -- which
    costs a refusal only where some other row's flag names a pointer, and
    nothing of this command's names one for any other reason. git's own
    commands name none of these as operands, which is what leaves `git
    worktree add`, `move`, `remove` and `prune` their work.
    """
    landings = {row["path"]: row["lands"] for row in displaced or []}
    return [
        target
        for target in dict.fromkeys(
            [
                *shell_write_targets(command),
                *shell_written_targets(command, rows),
                *shell_flag_write_targets(command, rows),
            ]
        )
        if git_state(target) == "pointer"
        or (target in landings and git_state(landings[target]) == "pointer")
    ]


class SedRewrite(TypedDict):
    """One in-place rewrite a command carries: what to run, and over what.

    Named here for the reason :func:`shell_patch_operands` is: the words are
    on this side of the boundary and the files are on the other. A caller that
    can reach a filesystem runs these scripts over copies of these targets and
    hands the documents back, so the kernel judges a rewrite by what it would
    produce without ever having produced it.
    """

    scripts: list[str]
    options: list[str]
    targets: list[str]


def shell_sed_rewrites(command: str, rows: list[ShellRuleRow]) -> list[SedRewrite]:
    """Name every in-place sed this command runs, with the scripts it runs.

    Only the screened ones. A script carrying a write or execute primitive is
    refused by the classifier on its own terms, and running it to find out
    what it would produce would be running exactly what the screen exists to
    keep from running — so an unscreened call yields nothing here and meets
    its refusal there.

    A command that does not parse yields nothing, on the same terms as every
    reader beside it: an unparseable line keeps whatever verdict it already
    earned rather than gaining a relaxation from a reading that failed.
    """
    rewrites: list[SedRewrite] = []
    for segment in read_segments(command, rows):
        words = segment["words"]
        if posixpath.basename(words[0]) != "sed":
            continue
        invocation = sed_invocation(words)
        if isinstance(invocation, KernelDecision):
            continue
        if not invocation["in_place"] or not invocation["screened"]:
            continue
        if not invocation["targets"]:
            continue
        placed = [
            placed_path(target, segment["directory"])
            for target in invocation["targets"]
        ]
        if any(target is None for target in placed):
            continue
        rewrites.append(
            SedRewrite(
                scripts=invocation["scripts"],
                options=invocation["options"],
                targets=[target for target in placed if target is not None],
            )
        )
    return rewrites
