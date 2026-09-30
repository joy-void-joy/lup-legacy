"""What one command line leaves in each file it writes, in the order it writes them.

The shell readers beside this each answer one question about a whole line --
which files a redirection opens, which files a sed rewrites, which bytes a
heredoc carries -- against the files as they stood before the line ran. That
is the right reading of a single write and the wrong one of a line that
writes a file twice: `sed -i 's/a/a/' f && sed -i '3a x' f` had its second
rewrite judged by the first one's document, so whatever the second introduced
was read by nothing. This reads a line as the shell runs it instead, segment
by segment, each write applied to what the writes before it left, and ends
with one document per file: what stood there before the line, and what
stands there after it.

Two halves, split where the boundary splits everything here. The walk names
each step from the words alone (:func:`file_steps`); the fold applies the
steps (:func:`followed_documents`) through readers the host hands it, because
what a file holds, and what sed or a patch make of it, are facts only a
filesystem answers. A step whose result the words do not state -- a program's
output redirected into a file, `sort -o`, a formatter, a script -- is never
worked out by running it: its files are reported as results that exist only
once the command has run.
"""

import posixpath
from collections.abc import Callable, Iterator
from typing import Literal, TypedDict

from .archives import archive_targets, archive_write
from .bindings import carried_words, command_lists, literal_loop_word
from .commands import matched_command_row
from .decision import KernelDecision, UnpreviewedCause, UnpreviewedRow
from .downloads import download_targets
from .effects import member_for
from .lex import (
    CHDIR_VERBS,
    NodeWrites,
    Placement,
    ReadSegment,
    joined_directory,
    node_writes,
    parse_shell,
    placed_path,
    read_segment,
    redirection_writes,
    simple_commands,
    substitutions,
    verb_path_words,
    writes_to_a_stream,
    written_verb_words,
)
from .roles import spells_its_path
from .rows import (
    EditOperation,
    RewriteReading,
    RewrittenDocumentRow,
    ShellRuleRow,
    UnproducedDocumentRow,
    unproduced_cause,
)
from .syntax import Command, Script, Word, word_text
from .words import (
    OptionWord,
    flag_matches,
    flag_write_targets,
    global_span,
    install_operands,
    operand_word,
    path_verb_operands,
    sed_invocation,
)

type StepAction = Literal[
    "author",
    "append",
    "empty",
    "touch",
    "rewrite",
    "copy",
    "move",
    "remove",
    "patch",
    "directory",
    "unknown",
    "run",
]
"""What one step does to the file it names.

``author`` replaces a file with bytes the command carries and ``append``
adds them; ``empty`` truncates it, ``touch`` brings an empty one into being
where none stands. ``rewrite`` is an in-place sed, ``copy`` and ``move`` land
another file's content, ``remove`` unlinks, ``patch`` applies a diff whose
files are inside it. ``directory`` makes the directory it names, which holds
no document and is somewhere a later copy lands. ``unknown`` writes a named
file with bytes only running produces, and ``run`` is a segment that may
write, naming nothing.
"""

type StepOption = Literal["-c", "-n", "into"]
"""What one step's own flags change about what it lands.

``-c`` is a `touch` or a `truncate` that creates nothing where nothing
stands, ``-n`` a move that replaces nothing where something does, and
``into`` a copy of several sources, which lands only where its target is a
directory.
"""


class FileStep(TypedDict):
    """One thing one segment does to the files it names, as its words state it.

    ``path`` is the file written, placed from the launch directory, and ``""``
    for a patch -- whose files are inside it -- and for a run. ``source`` is
    what a copy or a move reads, and the file a patch is read from;
    ``content`` the bytes an authored write carries, or the patch a command is
    handed on standard input. ``scripts`` and ``options`` are a rewrite's;
    ``flags`` what a patch is applied with, ``program`` naming which program
    applies it and ``directory`` where, placed as ``path`` is.
    """

    segment: int
    command: str
    action: StepAction
    path: str
    source: str
    content: str | None
    scripts: list[str]
    options: list[str]
    flags: list[StepOption]
    program: str
    directory: str


def step(
    segment: int,
    command: str,
    action: StepAction,
    path: str = "",
    *,
    source: str = "",
    content: str | None = None,
    scripts: list[str] | None = None,
    options: list[str] | None = None,
    flags: list[StepOption] | None = None,
    program: str = "",
    directory: str = "",
) -> FileStep:
    """One step, with every field its action does not read left empty."""
    return FileStep(
        segment=segment,
        command=command,
        action=action,
        path=path,
        source=source,
        content=content,
        scripts=scripts or [],
        options=options or [],
        flags=flags or [],
        program=program,
        directory=directory,
    )


# lup: ignore[constant-declaration] — the characters a POSIX shell word may hold
# unquoted and still read as itself, fixed by the shell rather than chosen here
UNQUOTED = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_@%+=:,./-"


def retyped(word: Word) -> str:
    """A word as a reader would retype it: bare where the shell reads it as itself.

    A literal word is quoted where it holds a character the shell would read
    otherwise; one the shell expands -- a glob, a parameter -- is left as it
    was written, since quoting it would show a different command.
    """
    text = word_text(word)
    if not literal_loop_word(word) or (
        text and all(character in UNQUOTED for character in text)
    ):
        return text
    escaped = "".join("'\\''" if character == "'" else character for character in text)
    return f"'{escaped}'"


def spelled_command(node: Command) -> str:
    """One simple command as it reads: its words, then its redirections.

    A heredoc is named by its delimiter rather than carried, because the
    whole line is shown beside this and a reader matching a step to it needs
    the segment, not the document a second time.
    """
    redirections = [
        redirect["operator"]
        + (
            " " + retyped(redirect["target"][0])
            if redirect["target"]
            else "".join(
                f"'{heredoc['delimiter']}'"
                if heredoc["quoted"]
                else heredoc["delimiter"]
                for heredoc in redirect["heredoc"]
            )
        )
        for redirect in node["redirects"]
    ]
    return " ".join([*(retyped(word) for word in node["words"]), *redirections])


def counted_flag(word: str, flag: str) -> bool:
    """Whether a word is a short flag carrying a count attached, as `-p1` is."""
    return word.startswith(flag) and word[len(flag) :].isdigit()


def file_steps(command: str, rows: list[ShellRuleRow]) -> list[FileStep]:
    """Every file step one command line takes, in the order the shell takes them.

    A line the grammar does not read is one step that runs: nothing about
    what it writes can be named, and naming nothing is not the same as naming
    that it writes nothing. A loop is the same, because its body runs as many
    times as its words say, and a function body runs wherever it is called.
    """
    tree = parse_shell(command)
    if isinstance(tree, KernelDecision):
        return [step(0, command, "run")]
    counted = 0

    def walk(script: Script) -> Iterator[FileStep]:
        nonlocal counted
        for item in script["items"]:
            for pipeline in item["andor"]["pipelines"]:
                incoming: str | None = None
                for position, node in enumerate(pipeline["commands"]):
                    for inner in substitutions(carried_words(node)):
                        yield from walk(inner)
                    piping = pipeline["operators"][position : position + 1] == ["|"]
                    segment = counted
                    counted += 1
                    match node["kind"]:
                        case "simple":
                            writes = node_writes(node, incoming, piping)
                            yield from simple_steps(node, writes, rows, segment)
                            incoming = writes["outgoing"]
                        case "for" | "while" | "until" | "select" | "function":
                            body = [
                                found
                                for inner in command_lists(node)
                                for found in walk(inner)
                            ]
                            yield from repeated_steps(node, body, segment)
                            incoming = None
                        case "test" | "arithmetic":
                            incoming = None
                        case _:
                            for inner in command_lists(node):
                                yield from walk(inner)
                            yield from opened_steps(
                                node_writes(node, None, False), segment, ""
                            )
                            incoming = None

    return list(walk(tree))


def repeated_steps(node: Command, body: list[FileStep], segment: int) -> list[FileStep]:
    """A loop or a function as steps: a run, and every file its body names.

    Folded once, a body that appends would read as appending once. So nothing
    in it is applied: the construct runs, and each file its body writes holds
    what only running it makes.
    """
    inner = "; ".join(
        spelled_command(command)
        for script in command_lists(node)
        for command in simple_commands(script)
    )
    heading = " ".join(part for part in (node["kind"], node["name"]) if part)
    spelled = f"{heading}: {inner}"
    named = dict.fromkeys(
        found["path"] for found in body if found["path"] and found["action"] != "run"
    )
    return [
        step(segment, spelled, "run"),
        *(step(segment, spelled, "unknown", path) for path in named),
    ]


def opened_steps(writes: NodeWrites, segment: int, command: str) -> list[FileStep]:
    """The files a command opens with bytes only running it produces."""
    return [
        *(step(segment, command, "unknown", path) for path in writes["unread"]),
        *([step(segment, command, "run")] if writes["unplaced"] else []),
    ]


def simple_steps(
    node: Command, writes: NodeWrites, rows: list[ShellRuleRow], segment: int
) -> list[FileStep]:
    """One simple command's steps: its redirections first, then what its verb does.

    The redirections come first because the shell opens them before the
    command runs -- which is also what makes a redirection with no command,
    or with `:` in front of it, a truncation.
    """
    spelled = spelled_command(node)
    words = [word_text(word) for word in node["words"]]
    if words in ([], [":"], ["true"]):
        return [
            step(segment, spelled, "empty", placed)
            for redirect in node["redirects"]
            if redirection_writes(redirect["operator"])
            and ">>" not in redirect["operator"]
            for target in redirect["target"]
            for written in [word_text(target)]
            if spells_its_path(written)
            and literal_loop_word(target)
            and not writes_to_a_stream(written)
            for placed in [placed_path(written, node["directory"])]
            if placed is not None
        ]
    carried = [
        step(
            segment,
            spelled,
            "append" if write["append"] else "author",
            write["path"],
            content=write["content"],
        )
        for write in writes["authored"]
    ]
    read = read_segment(Placement(words=words, directory=node["directory"]), rows)
    # A glob or a brace expansion names whatever it matches when the command
    # runs, so a path spelled with one names no file a step could land.
    unliteral = [
        word_text(word) for word in node["words"] if not literal_loop_word(word)
    ]
    verbs = (
        []
        if read is None
        else verb_steps(read, writes, rows, segment, spelled, unliteral)
    )
    return [*carried, *opened_steps(writes, segment, spelled), *verbs]


def observes(words: list[str], rows: list[ShellRuleRow]) -> bool:
    """Whether the row these words reach declares only effects that change nothing.

    The reading `xargs` already takes of a payload. A command no row names
    reads as one that may write, because nothing says otherwise; nor does
    one spelling a flag its row guards, which turns a reader into a writer.
    """
    matched = matched_command_row(words, rows)
    if isinstance(matched, KernelDecision):
        return False
    row = matched["row"]
    return all(
        member_for(effect["kind"]).observes for effect in row["effects"]
    ) and not any(flag_matches(word, row["ask_flags"]) for word in words[1:])


def verb_steps(
    read: ReadSegment,
    writes: NodeWrites,
    rows: list[ShellRuleRow],
    segment: int,
    spelled: str,
    unliteral: list[str],
) -> list[FileStep]:
    """What one segment's own verb does to the files it names.

    The verbs whose effect is a document -- sed in place, a copy, a move, an
    install, a removal, a truncation, a patch -- are read into steps. Every
    other verb names what it writes, where a row or a reader says which words
    are files, as results only running produces; one that names nothing is a
    run unless the row it reaches only reads. ``unliteral`` are the words
    the shell expands into others, which name no file this could place.
    """
    words = read["words"]
    directory = read["directory"]
    ran = [step(segment, spelled, "run")]

    def placed(word: str) -> str | None:
        if not spells_its_path(word) or word in unliteral:
            return None
        return placed_path(word, directory)

    def unknown(paths: list[str]) -> list[FileStep]:
        found = [placed(path) for path in paths]
        return [
            *(
                step(segment, spelled, "unknown", path)
                for path in found
                if path is not None
            ),
            *(ran if None in found else []),
        ]

    executable = posixpath.basename(words[0])
    # Where the shell stands, or a directory, changes; no document does. What
    # a `tee` writes is its redirection reading's, already stepped.
    if executable in (*CHDIR_VERBS, "rmdir", "tee"):
        return []
    applying = words[global_span(words, rows) : global_span(words, rows) + 1] == [
        "apply"
    ]
    match executable:
        case "mkdir":
            operands = path_verb_operands(words)
            made = [placed(operand) for operand in operands["operands"]]
            return [
                step(segment, spelled, "directory", path)
                for path in made
                if path is not None
            ]
        case "sed":
            return rewrite_steps(words, placed, segment, spelled)
        case "cp" | "mv":
            operands = path_verb_operands(words)
            landed = [placed(operand) for operand in operands["operands"]]
            if not operands["inert"] or len(landed) < 2 or None in landed:
                return ran
            *sources, target = [path for path in landed if path is not None]
            clobbers = not any(
                "n" in word[1:]
                for word in words[1:]
                if word.startswith("-") and not word.startswith("--")
            )
            flags: list[StepOption] = [
                *(["into"] if len(sources) > 1 else []),
                *([] if clobbers else ["-n"]),
            ]
            return [
                step(
                    segment,
                    spelled,
                    "copy" if executable == "cp" else "move",
                    target,
                    source=source,
                    flags=flags,
                )
                for source in sources
            ]
        case "install":
            installed = install_operands(words)
            if installed is None:
                return ran
            landed = [placed(operand) for operand in installed]
            if None in landed:
                return ran
            if not landed:
                return []
            *sources, target = [path for path in landed if path is not None]
            return [
                step(
                    segment,
                    spelled,
                    "copy",
                    target,
                    source=source,
                    flags=["into"] if len(sources) > 1 else [],
                )
                for source in sources
            ]
        case "rm":
            operands = path_verb_operands(words)
            landed = [placed(operand) for operand in operands["operands"]]
            if not operands["inert"] or None in landed:
                return ran
            return [
                step(segment, spelled, "remove", path)
                for path in landed
                if path is not None
            ]
        case "truncate":
            return truncate_steps(words, placed, segment, spelled)
        case "touch":
            operands = path_verb_operands(words)
            landed = [placed(operand) for operand in operands["operands"]]
            if not operands["inert"] or None in landed:
                return ran
            creates = not any(
                "c" in word[1:]
                for word in words[1:]
                if word.startswith("-") and not word.startswith("--")
            )
            return [
                step(segment, spelled, "touch", path, flags=[] if creates else ["-c"])
                for path in landed
                if path is not None
            ]
        case "patch":
            return patch_steps(words, writes, directory, segment, spelled)
        case "git" if applying:
            return apply_steps(words, rows, writes, directory, segment, spelled)
        case "git":
            named = [operand["path"] for operand in verb_path_words(words, rows)]
            if named:
                return unknown(named)
            return [] if observes(words, rows) else ran
        case "ln" | "rsync" | "scp":
            return unknown(
                [operand["path"] for operand in written_verb_words(words, rows)]
            )
    archived = archive_write(words)
    named = [
        *flag_write_targets(
            words,
            [
                flag
                for row in rows
                if row["command"] == executable
                for flag in row["write_flags"]
            ],
        ),
        *download_targets(words),
        *([] if archived is None else archive_targets(archived)),
    ]
    if named:
        return unknown(named)
    return [] if observes(words, rows) else ran


def rewrite_steps(
    words: list[str],
    placed: Callable[[str], str | None],
    segment: int,
    spelled: str,
) -> list[FileStep]:
    """An in-place sed as a rewrite of each file it names, in order.

    A backup suffix is the copy `-i` makes first, so it is one: the original
    lands beside itself under the suffix, and then the file is rewritten. A
    suffix naming another directory, or spelling the file's own name into it,
    is a place this does not work out.
    """
    invocation = sed_invocation(words)
    if isinstance(invocation, KernelDecision) or not invocation["screened"]:
        return [step(segment, spelled, "run")]
    if not invocation["in_place"] or not invocation["targets"]:
        return []
    targets = [placed(target) for target in invocation["targets"]]
    backup = invocation["backup"]
    if None in targets or "/" in backup or "*" in backup:
        return [step(segment, spelled, "run")]
    return [
        found
        for target in targets
        if target is not None
        for found in [
            *(
                [step(segment, spelled, "copy", target + backup, source=target)]
                if backup
                else []
            ),
            step(
                segment,
                spelled,
                "rewrite",
                target,
                scripts=invocation["scripts"],
                options=invocation["options"],
            ),
        ]
    ]


def truncate_word(word: str) -> OptionWord:
    """One `truncate` word that takes no value after it, read by its spelling."""
    if word.startswith("--size="):
        return OptionWord(kind="value", name="-s", word=word.removeprefix("--size="))
    if word.startswith("-s"):
        return OptionWord(kind="value", name="-s", word=word.removeprefix("-s"))
    return operand_word(word)


def truncate_words(words: list[str]) -> Iterator[OptionWord]:
    """A `truncate`'s words by its own grammar: its size, its no-create flag, its files."""
    remaining = iter(words[1:])
    for word in remaining:
        match word:
            case "-s" | "--size":
                yield OptionWord(kind="value", name="-s", word=next(remaining, ""))
            case "-c" | "--no-create":
                yield OptionWord(kind="option", name="-c", word="-c")
            case _:
                yield truncate_word(word)


def truncate_steps(
    words: list[str],
    placed: Callable[[str], str | None],
    segment: int,
    spelled: str,
) -> list[FileStep]:
    """A `truncate` to nothing as an emptying of each file; any other size is run."""
    read = list(truncate_words(words))
    landed = [placed(word["word"]) for word in read if word["kind"] == "operand"]
    if any(word["kind"] == "unmodelled" for word in read) or None in landed:
        return [step(segment, spelled, "run")]
    sizes = [word["word"] for word in read if word["kind"] == "value"]
    action: StepAction = "empty" if sizes[-1:] == ["0"] else "unknown"
    creates = not any(word["kind"] == "option" for word in read)
    return [
        step(segment, spelled, action, path, flags=[] if creates else ["-c"])
        for path in landed
        if path is not None
    ]


# lup: ignore[library-default] — git apply's own flags that change which patch lands
# or how it is matched, carried to the copy it is applied to
APPLY_FLAGS = (
    "-R",
    "--reverse",
    "--unidiff-zero",
    "--recount",
    "--allow-empty",
    "-v",
    "--verbose",
    "-q",
    "--quiet",
    "--ignore-space-change",
    "--ignore-whitespace",
    "--inaccurate-eof",
)


def apply_word(word: str) -> OptionWord:
    """One `git apply` word past its subcommand, read by its spelling."""
    if word in ("--stat", "--numstat", "--summary", "--check"):
        return OptionWord(kind="reading", name=word, word=word)
    if word == "--apply":
        return OptionWord(kind="value", name=word, word=word)
    if (
        word in APPLY_FLAGS
        or counted_flag(word, "-p")
        or counted_flag(word, "-C")
        or word.startswith(("--whitespace=", "--directory="))
    ):
        return OptionWord(kind="option", name=word, word=word)
    return operand_word(word)


def apply_steps(
    words: list[str],
    rows: list[ShellRuleRow],
    writes: NodeWrites,
    directory: str | None,
    segment: int,
    spelled: str,
) -> list[FileStep]:
    """A `git apply` as a patch step per patch it is handed, in order.

    A reading form -- `--check`, `--stat` -- lands nothing unless `--apply`
    beside it says it lands too. An option that reaches the index, a three-way
    merge, or a filter over which files apply, is run.
    """
    read = [apply_word(word) for word in words[global_span(words, rows) + 1 :]]
    kinds = [word["kind"] for word in read]
    if "unmodelled" in kinds:
        return [step(segment, spelled, "run")]
    if "reading" in kinds and "value" not in kinds:
        return []
    return handed_patches(
        "git",
        [word["word"] for word in read if word["kind"] == "option"],
        [word["word"] for word in read if word["kind"] == "operand"],
        writes,
        directory,
        segment,
        spelled,
    )


def patch_word(word: str) -> list[OptionWord]:
    """One `patch` word that takes no value after it, read by its spelling.

    The flags that change nothing about what a clean apply lands read as
    nothing; the strip count is carried in the one spelling `git apply` takes.
    """
    if word in ("-s", "--silent", "--quiet", "-t", "--batch", "-f", "--force"):
        return []
    if word in ("-u", "--unified", "-N", "--forward", "--no-backup-if-mismatch"):
        return []
    if counted_flag(word, "-p"):
        return [OptionWord(kind="option", name="-p", word=word)]
    if word.startswith("--strip="):
        return [
            OptionWord(
                kind="option", name="-p", word="-p" + word.removeprefix("--strip=")
            )
        ]
    if word.startswith("--input="):
        return [OptionWord(kind="value", name="-i", word=word.removeprefix("--input="))]
    if word.startswith("--directory="):
        return [
            OptionWord(kind="value", name="-d", word=word.removeprefix("--directory="))
        ]
    # An operand names the file to patch, which `git apply` has no reading of.
    return [OptionWord(kind="unmodelled", name=word, word=word)]


def patch_words(words: list[str]) -> Iterator[OptionWord]:
    """A `patch`'s words by its own grammar, as far as `git apply` shares its meaning.

    The strip count and a reverse are carried; the directory it changes to
    and the file it reads are values.
    """
    remaining = iter(words[1:])
    for word in remaining:
        match word:
            case "-p" | "--strip":
                yield OptionWord(
                    kind="option", name="-p", word="-p" + next(remaining, "")
                )
            case "-R" | "--reverse":
                yield OptionWord(kind="option", name="-R", word="-R")
            case "-i" | "--input":
                yield OptionWord(kind="value", name="-i", word=next(remaining, ""))
            case "-d" | "--directory":
                yield OptionWord(kind="value", name="-d", word=next(remaining, ""))
            case "--dry-run":
                yield OptionWord(kind="reading", name=word, word=word)
            case _:
                yield from patch_word(word)


def patch_steps(
    words: list[str],
    writes: NodeWrites,
    directory: str | None,
    segment: int,
    spelled: str,
) -> list[FileStep]:
    """A `patch` as the same step a `git apply` of its diff takes.

    Only with its strip count spelled: without one, `patch` reads a file name
    by rules of its own that `git apply` does not share. Naming the file to
    patch as an operand, or asking for a backup, is run for the same reason.
    Where `patch` would apply with fuzz and `git apply` refuses, the copy
    refuses, and applying it is a result only running shows.
    """
    read = list(patch_words(words))
    kinds = [word["kind"] for word in read]
    if "unmodelled" in kinds or not any(word["name"] == "-p" for word in read):
        return [step(segment, spelled, "run")]
    if "reading" in kinds:
        return []
    into = [word["word"] for word in read if word["name"] == "-d"][-1:]
    moved = joined_directory(directory, into[0]) if into else directory
    return handed_patches(
        "patch",
        [word["word"] for word in read if word["kind"] == "option"],
        [word["word"] for word in read if word["name"] == "-i"],
        writes,
        moved,
        segment,
        spelled,
    )


def handed_patches(
    program: str,
    options: list[str],
    patches: list[str],
    writes: NodeWrites,
    directory: str | None,
    segment: int,
    spelled: str,
) -> list[FileStep]:
    """One patch step per patch a command applies, read from a file or its input.

    A file named on the command wins; failing one, what `<` hands it, then a
    heredoc or a pipe carrying its own bytes. Where none says what the patch
    is, applying it is a result only running shows.
    """
    if directory is None or not all(spells_its_path(patch) for patch in patches):
        return [step(segment, spelled, "run")]
    named = [
        placed
        for patch in patches
        if patch != "-"
        for placed in [placed_path(patch, directory)]
        if placed is not None
    ]
    stdin = patches in ([], ["-"])
    sources = named or (
        [writes["read_from"]] if stdin and writes["read_from"] is not None else []
    )
    carried = writes["stdin"] if stdin and not sources else None
    if not sources and carried is None:
        return [step(segment, spelled, "run")]
    return [
        step(
            segment,
            spelled,
            "patch",
            source=source,
            content=carried,
            options=options,
            program=program,
            directory=directory,
        )
        for source in sources or [""]
    ]


type Reading = dict[Literal["text", "cause"], str | None]
"""What the host read at one path: the text, or the word for why there is none.

``cause`` is ``missing`` where nothing stands, ``directory`` where a
directory does, ``irregular`` for anything else that is not a file,
``unreadable`` where the file does not read as text, and ``refused`` where a
program the host ran to produce it declined. The host half may name no kernel
type, so a reading crosses as this plain shape.
"""

type Patched = dict[Literal["path", "after"], str | None]
"""One file a patch applied to a copy leaves: its resolved path, and its text,
``None`` where the patch removes it. Plain for the reason a reading is."""

type Reader = Callable[[str], Reading]
type Resolver = Callable[[str], str]
type Rewriter = Callable[[list[str], list[str], str], Reading]
type Patcher = Callable[[str, list[str], str, str, Reader], list[Patched] | None]
"""Apply one patch to a copy: its text, options, program and directory, and a
reader of what the line has left at each path; every file it leaves, or
``None`` where it does not apply."""


class FollowedDocument(TypedDict):
    """One file a command line writes: what stood there, and what the line leaves.

    ``before`` is its text before the line, ``None`` where nothing stood or
    what stood does not read as text -- ``existed`` tells the two apart. It is
    ``known`` where every step that wrote it could be worked out, and then
    ``after`` is what it holds, ``None`` where the line removes it. Where it
    is not known, ``after`` is what it held when a step whose result only
    running shows reached it, which is the last thing about it anybody could
    judge; ``cause`` is why it is not known. ``whole`` is whether some step
    replaced the document rather than changing it, and ``authored`` whether
    bytes the command carries reached it: the route the authored review
    judges, as an in-place rewrite is the route the rewrite rows judge.
    """

    target: str
    targets: list[str]
    path: str
    before: str | None
    existed: bool
    after: str | None
    known: bool
    whole: bool
    authored: bool
    cause: str | None


class RewriteOutcome(TypedDict):
    """What became of one file an in-place sed names, by the spelling it names it with.

    ``cause`` is ``None`` where the rewrite was worked out and the reading's
    word where it was not, ``run`` among them: a file an earlier segment
    writes by running holds nothing a rewrite of it can be read against.
    """

    target: str
    path: str
    cause: str | None


class FollowedReading(TypedDict):
    """A whole line's documents in the order it first writes them, and the rest.

    ``rewrites`` are the in-place seds among them, for the classifier that
    judges a rewrite by what it leaves; ``unpreviewed`` every segment whose
    effect no document states.
    """

    documents: list[FollowedDocument]
    rewrites: list[RewriteOutcome]
    unpreviewed: list[UnpreviewedRow]


class Unseen(TypedDict):
    """One step whose result the fold could not state, and the file it left so."""

    step: FileStep
    path: str | None
    cause: str


def unpreviewed_cause(cause: str) -> UnpreviewedCause:
    """Whether a result exists only by running, or exists and is not text."""
    return "run" if cause in ("run", "refused", "directory") else "unread"


def followed_documents(
    steps: list[FileStep],
    read: Reader,
    resolve: Resolver,
    rewrite: Rewriter,
    patch: Patcher,
) -> FollowedReading:
    """Apply a line's steps in order, each to what the steps before it left.

    ``read`` is the file as it stands on disk, ``resolve`` the one name every
    spelling of it shares, ``rewrite`` a sed over a text, and ``patch`` a diff
    applied to a copy. A step whose input is not known leaves its file not
    known, and a later step that replaces the file whole makes it known
    again -- which is what `gen > f && printf x > f` leaves.
    """
    states: dict[str, FollowedDocument] = {}
    rewrites: dict[str, RewriteOutcome] = {}
    unseen: list[Unseen] = []
    # lup: ignore[empty-collection] — the directories the line has made so far,
    # where a later copy lands under its name: state the fold carries in step
    # order, since a copy before the `mkdir` that makes its target finds none
    made: list[str] = []

    def current(target: str) -> Reading:
        """What the line has left at one path so far, or the disk where it wrote nothing."""
        key = resolve(target)
        if key in made and key not in states:
            return {"text": None, "cause": "directory"}
        if key not in states:
            return read(target)
        state = states[key]
        if not state["known"]:
            return {"text": None, "cause": state["cause"] or "run"}
        return {
            "text": state["after"],
            "cause": None if state["after"] is not None else "missing",
        }

    def written(target: str, taken: FileStep) -> FollowedDocument:
        """The document one step writes, read from disk the first time any step does.

        A file that stands there and does not read as text is named as the
        step that first writes it: whatever the line leaves, no document
        shows what it replaced.
        """
        key = resolve(target)
        if key in states:
            if target not in states[key]["targets"]:
                states[key]["targets"].append(target)
            return states[key]
        reading = read(target)
        cause = reading["cause"]
        states[key] = FollowedDocument(
            target=target,
            targets=[target],
            path=key,
            before=reading["text"],
            existed=cause != "missing",
            after=reading["text"],
            known=cause in (None, "missing"),
            whole=False,
            authored=False,
            cause=None if cause in (None, "missing") else cause,
        )
        if cause == "unreadable":
            unseen.append(Unseen(step=taken, path=key, cause=cause))
        return states[key]

    def unknown(taken: FileStep, document: FollowedDocument | None, cause: str) -> None:
        """Leave a file not known, and say which segment left it so."""
        if document is not None:
            document["known"] = False
            document["cause"] = cause
        unseen.append(
            Unseen(
                step=taken,
                path=None if document is None else document["path"],
                cause=cause,
            )
        )

    def landed(
        document: FollowedDocument, text: str | None, *, whole: bool, authored: bool
    ) -> None:
        """Set what one file holds now, known, and how it came to hold it."""
        document["after"] = text
        document["known"] = True
        document["cause"] = None
        document["whole"] = document["whole"] or whole
        document["authored"] = document["authored"] or authored

    def copied(taken: FileStep) -> None:
        """Land a copy or a move: the source's text at the target, a move's source gone.

        A target that is a directory takes the source under its own name,
        which is what `cp` and `mv` do with one; several sources land only
        where it is one, and a source that is not there lands nothing and
        stops the line -- what follows is a result only running shows. A
        source that does not read as text leaves the target holding something
        no document shows, and a move that may not replace an existing target
        leaves both where they stand.
        """
        source = current(taken["source"])
        target = taken["path"]
        into = current(target)["cause"] == "directory"
        if source["cause"] == "missing" or ("into" in taken["flags"] and not into):
            unknown(taken, None, "run")
            return
        if into:
            target = posixpath.join(target, posixpath.basename(taken["source"]))
        if "-n" in taken["flags"] and current(target)["cause"] != "missing":
            return
        document = written(target, taken)
        if source["text"] is None:
            unknown(taken, document, source["cause"] or "unreadable")
        else:
            landed(document, source["text"], whole=True, authored=False)
        if taken["action"] != "move":
            return
        moved = written(taken["source"], taken)
        if source["cause"] in (None, "unreadable"):
            landed(moved, None, whole=True, authored=False)
        else:
            unknown(taken, moved, source["cause"] or "irregular")

    def patched(taken: FileStep) -> None:
        """Land a patch applied to a copy of what the line has left so far."""
        text = (
            taken["content"]
            if taken["content"] is not None
            else current(taken["source"])["text"]
        )
        results = (
            None
            if text is None
            else patch(
                text, taken["options"], taken["program"], taken["directory"], current
            )
        )
        if results is None:
            unknown(taken, None, "run")
            return
        for result in results:
            path = result["path"]
            if path is not None:
                landed(
                    written(path, taken), result["after"], whole=False, authored=False
                )

    def rewrote(taken: FileStep) -> None:
        """Land an in-place sed over what its file holds so far, and say how it went."""
        document = written(taken["path"], taken)
        cause = rewritten(document, taken, rewrite)
        if cause is not None and cause != "missing":
            unknown(taken, document, cause)
        earlier = rewrites[taken["path"]] if taken["path"] in rewrites else None
        if earlier is None or earlier["cause"] is None:
            rewrites[taken["path"]] = RewriteOutcome(
                target=taken["path"], path=document["path"], cause=cause
            )

    def authored(taken: FileStep) -> None:
        """Land bytes the command carries over a whole file, or add them to its end."""
        document = written(taken["path"], taken)
        if document["cause"] in ("directory", "irregular"):
            unknown(taken, document, document["cause"] or "irregular")
            return
        if taken["action"] == "append":
            if not document["known"]:
                unknown(taken, document, document["cause"] or "run")
                return
            text = (document["after"] or "") + (taken["content"] or "")
            landed(document, text, whole=False, authored=True)
            return
        if "-c" in taken["flags"] and not document["existed"] and document["known"]:
            return
        landed(
            document,
            taken["content"] or "",
            whole=True,
            authored=taken["action"] == "author",
        )

    for taken in steps:
        match taken["action"]:
            case "run":
                unknown(taken, None, "run")
            case "unknown":
                unknown(taken, written(taken["path"], taken), "run")
            case "author" | "empty" | "append":
                authored(taken)
            case "touch":
                if current(taken["path"])["cause"] == "missing" and not taken["flags"]:
                    landed(
                        written(taken["path"], taken), "", whole=True, authored=False
                    )
            case "rewrite":
                rewrote(taken)
            case "copy" | "move":
                copied(taken)
            case "remove":
                document = written(taken["path"], taken)
                if current(taken["path"])["cause"] == "directory":
                    unknown(taken, document, "directory")
                else:
                    landed(document, None, whole=True, authored=False)
            case "patch":
                patched(taken)
            case "directory":
                made.append(resolve(taken["path"]))
    return FollowedReading(
        documents=[
            document
            for document in states.values()
            if not (
                document["known"]
                and not document["existed"]
                and document["after"] is None
            )
        ],
        rewrites=list(rewrites.values()),
        unpreviewed=unpreviewed_rows(unseen, states),
    )


def rewritten(
    document: FollowedDocument, taken: FileStep, rewrite: Rewriter
) -> str | None:
    """Apply one rewrite to what its file holds so far; the reason where it cannot be."""
    if not document["known"]:
        return document["cause"] or "run"
    if document["after"] is None:
        return "missing"
    result = rewrite(taken["scripts"], taken["options"], document["after"])
    if result["text"] is None:
        return result["cause"] or "refused"
    document["after"] = result["text"]
    return None


def unpreviewed_rows(
    unseen: list[Unseen], states: dict[str, FollowedDocument]
) -> list[UnpreviewedRow]:
    """One row per segment whose effect no document states, in the order they run.

    A file is named on its row while the line leaves it not known, or where
    what it replaced was not text: one a later step replaced whole is shown
    as a document, and the segment that wrote it first is still listed,
    since it still runs.
    """
    segments = dict.fromkeys(entry["step"]["segment"] for entry in unseen)
    return [
        UnpreviewedRow(
            command=found[0]["step"]["command"],
            paths=list(
                dict.fromkeys(
                    entry["path"]
                    for entry in found
                    if entry["path"] is not None and not shown(states[entry["path"]])
                )
            ),
            cause="run"
            if any(unpreviewed_cause(entry["cause"]) == "run" for entry in found)
            else "unread",
        )
        for segment in segments
        for found in [
            [entry for entry in unseen if entry["step"]["segment"] == segment]
        ]
        if any(
            entry["path"] is None or not shown(states[entry["path"]]) for entry in found
        )
    ]


def changed(document: FollowedDocument) -> bool:
    """Whether the line leaves a file other than it found it."""
    return document["before"] != document["after"] or document["existed"] != (
        document["after"] is not None
    )


def shown(document: FollowedDocument) -> bool:
    """Whether a reviewer can be shown a file as two documents: before, and after.

    Only where every step that wrote it was worked out, and what it replaced
    is text or nothing at all.
    """
    return document["known"] and (
        document["before"] is not None or not document["existed"]
    )


def shown_documents(reading: FollowedReading) -> list[FollowedDocument]:
    """The files a reviewer is shown as documents, in the order the line writes them."""
    return [
        document
        for document in reading["documents"]
        if shown(document) and changed(document)
    ]


def judged_documents(reading: FollowedReading) -> list[FollowedDocument]:
    """The files the edit gates judge: those the command's bytes or a rewrite reached.

    The document each is judged by is what the whole line leaves, or the last
    one anybody could state where a later step's result only running shows.
    """
    rewritten_paths = {
        outcome["path"] for outcome in reading["rewrites"] if outcome["cause"] is None
    }
    return [
        document
        for document in reading["documents"]
        if document["authored"] or document["path"] in rewritten_paths
    ]


def document_operation(document: FollowedDocument) -> EditOperation:
    """Which class of edit the line makes of one file, as the edit gates read it."""
    if not document["existed"]:
        return "create"
    if document["after"] is None:
        return "delete"
    return "overwrite" if document["whole"] else "modify"


def rewrite_reading(
    reading: FollowedReading,
    row: Callable[[str, FollowedDocument], RewrittenDocumentRow],
) -> RewriteReading:
    """The classifier's reading of every in-place rewrite, off one line's fold.

    Each rewritten file is handed on as the document the whole line leaves
    there, or the last one anybody could judge, so a rewrite is judged with
    every other write to its file; ``row`` is the host's, which resolves
    where the file sits and puts it to the edit gates. A rewrite nothing
    could work out carries the reading's word for why.
    """
    documents = {document["path"]: document for document in reading["documents"]}
    return RewriteReading(
        documents=[
            row(outcome["target"], documents[outcome["path"]])
            for outcome in reading["rewrites"]
            if outcome["cause"] is None and outcome["path"] in documents
        ],
        unproduced=[
            UnproducedDocumentRow(
                target=outcome["target"], cause=unproduced_cause(outcome["cause"])
            )
            for outcome in reading["rewrites"]
            if outcome["cause"] is not None
        ],
    )
