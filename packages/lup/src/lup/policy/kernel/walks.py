"""Which commands read everything beneath a path, and from which of their words.

A key or a login is withheld from every word that names it, and a recursive
reader names none: `grep -r password ~` and `tar czf out.tgz ~` spell a home
and read every key inside it. Which words a program walks is that program's
grammar, so it is read here one utility at a time, and the host then says
whether anything withheld lies beneath each root -- a fact only a filesystem
has.

A listing is not a read: `ls -R`, `du`, `tree` and `find` print names, and a
name is not the secret -- until something reads each name it printed. A
`find` handing `{}` to a payload names every file it meets to that payload,
and a line that also hands names read from its input to a program (`xargs
cat`, a `while read` loop) reads what its listings yield, so each is read as
the walk it feeds. `git grep` reads what Git tracks, which a login never is.

Every reading errs toward naming more roots rather than fewer. A word read as
a root that is really an option's value names a path the host finds nothing
beneath, which changes nothing; a root missed is a walk nobody measured.
"""

import posixpath
from collections.abc import Iterator
from fnmatch import fnmatchcase
from typing import TypedDict

from .lex import placed_path, read_segments
from .rows import ShellRuleRow
from .words import xargs_payload


class WalkedRoot(TypedDict):
    """One path a recursive reader reads everything beneath."""

    path: str
    hidden: bool
    """Whether the walk reads names beginning with a dot.

    `rg` skips them unless told otherwise, and every credential file a
    machine keeps in a home is one, so `rg password ~` walks into none of
    them where `grep -r password ~` walks into all."""

    excluded: list[str]
    """Directory names the walk leaves out, as globs its tool matches them by.

    `grep --exclude-dir=.lup` skips every directory so named, which is how a
    search of a checkout keeping a login beneath `.lup/` reads the rest."""

    skipped: list[str]
    """File names the walk leaves out, as globs: `grep --exclude=.env.local`.

    Kept apart from ``excluded`` because each option reaches only its own
    kind: `--exclude=.lup` still descends into a directory named `.lup`."""

    yielded: list[str]
    """File names the walk yields only, as globs every yielded name matches.

    Empty yields every name. `find . -name '*.py' | xargs grep x` hands its
    reader Python files alone, so a login beneath the root is never among
    what is read; matched without regard to case, which can only yield more."""


def excluded_name(name: str, globs: list[str]) -> bool:
    """Whether a walk told to leave out these globs leaves out this directory."""
    return any(fnmatchcase(name, glob) for glob in globs)


def skipped_file(name: str, root: WalkedRoot) -> bool:
    """Whether a walk leaves this file out, by what it skips or what it yields."""
    folded = name.lower()
    return excluded_name(name, root["skipped"]) or any(
        not fnmatchcase(folded, glob.lower()) for glob in root["yielded"]
    )


class SplitWords(TypedDict):
    """One command's arguments, parted into what is spelled as an option and the rest."""

    options: list[str]
    operands: list[str]


def short(word: str) -> str:
    """A short option cluster's letters, or nothing for anything else."""
    return word[1:] if word.startswith("-") and not word.startswith("--") else ""


def takes_next(word: str, valued: str, named: tuple[str, ...]) -> bool:
    """Whether this option takes the word after it as its value.

    A long one named among ``named`` does unless it carries its value after
    `=`. A short cluster does where its first value letter is its last: in
    `-A3` the value is attached, in `-rA` the next word is it.
    """
    if word in named:
        return True
    letters = short(word)
    held = next(
        (index for index, letter in enumerate(letters) if letter in valued), None
    )
    return held is not None and held == len(letters) - 1


def split_options(
    arguments: list[str], valued: str = "", named: tuple[str, ...] = ()
) -> SplitWords:
    """The words spelled as options, and the rest, with `--` ending the options.

    ``valued`` are the short option letters, and ``named`` the long options,
    whose value may stand as the next word; that word is neither an option
    nor an operand.
    """
    ended = arguments.index("--") if "--" in arguments else len(arguments)
    spelled = arguments[:ended]
    values = {
        index + 1
        for index, word in enumerate(spelled)
        if word.startswith("-") and takes_next(word, valued, named)
    }
    return SplitWords(
        options=[word for word in spelled if word.startswith("-")],
        operands=[
            word
            for index, word in enumerate(spelled)
            if not word.startswith("-") and index not in values
        ]
        + arguments[ended + 1 :],
    )


def recursing(options: list[str], letters: str, spelled: tuple[str, ...]) -> bool:
    """Whether any option asks for recursion, by name or by one of its letters."""
    return any(
        option in spelled or any(letter in letters for letter in short(option))
        for option in options
    )


def patterned(options: list[str]) -> bool:
    """Whether an option carries the pattern, so no operand is it."""
    return any(
        option in ("--regexp", "--file")
        or option.startswith(("--regexp=", "--file="))
        or any(letter in "ef" for letter in short(option))
        for option in options
    )


def searched_roots(
    split: SplitWords, hidden: bool, excluded: list[str], skipped: list[str]
) -> list[WalkedRoot]:
    """The roots a pattern search walks: every operand past its pattern.

    With the pattern handed to an option, every operand is a root. Naming
    none, the search walks the directory it stands in.
    """
    operands = split["operands"]
    roots = operands if patterned(split["options"]) else operands[1:]
    return [
        WalkedRoot(
            path=root, hidden=hidden, excluded=excluded, skipped=skipped, yielded=[]
        )
        for root in roots or ["."]
    ]


def option_values(arguments: list[str], option: str) -> list[str]:
    """Every value one long option is given, attached after `=` or apart."""
    return [
        *(
            word.removeprefix(f"{option}=")
            for word in arguments
            if word.startswith(f"{option}=")
        ),
        *(
            value
            for flag, value in zip(arguments, arguments[1:], strict=False)
            if flag == option
        ),
    ]


class SearchGrammar(TypedDict):
    """Which of one searching tool's options take a value, and what that value is.

    A pattern and a program are text the tool matches or runs, where a file
    of patterns is a file it opens: `grep -e X` hands it a pattern and `grep
    -f X` a path, and the first operand is the pattern only where neither
    was given.
    """

    valued: str
    """Short option letters whose value is the next word, when not attached."""

    named: tuple[str, ...]
    """Long options whose value is the next word, when not after `=`."""

    patterns: str
    """Short letters among ``valued`` whose value is a pattern or a program."""

    pattern_names: tuple[str, ...]
    """Long options among ``named`` whose value is a pattern or a program."""

    files: str
    """Short letters among ``valued`` whose value is a file of patterns."""

    file_names: tuple[str, ...]
    """Long options among ``named`` whose value is a file of patterns."""


SEARCH_GRAMMARS: dict[str, SearchGrammar] = {
    "grep": SearchGrammar(
        valued="ABCDdefm",
        named=(
            "--regexp",
            "--file",
            "--directories",
            "--devices",
            "--include",
            "--exclude",
            "--exclude-dir",
            "--exclude-from",
            "--max-count",
            "--after-context",
            "--before-context",
            "--context",
            "--label",
            "--binary-files",
        ),
        patterns="e",
        pattern_names=("--regexp",),
        files="f",
        file_names=("--file",),
    ),
    "rg": SearchGrammar(
        valued="ABCEMTdefgjmrt",
        named=(
            "--regexp",
            "--file",
            "--glob",
            "--iglob",
            "--type",
            "--type-not",
            "--type-add",
            "--type-clear",
            "--max-count",
            "--max-depth",
            "--after-context",
            "--before-context",
            "--context",
            "--threads",
            "--max-columns",
            "--max-filesize",
            "--encoding",
            "--engine",
            "--replace",
            "--ignore-file",
            "--pre",
            "--pre-glob",
            "--sort",
            "--sortr",
            "--colors",
            "--path-separator",
        ),
        patterns="e",
        pattern_names=("--regexp",),
        files="f",
        file_names=("--file",),
    ),
    "awk": SearchGrammar(
        valued="fFv",
        named=("--file", "--field-separator", "--assign"),
        patterns="",
        pattern_names=(),
        files="f",
        file_names=("--file",),
    ),
}
"""Each searching tool's grammar, keyed by the executable read with it."""


class ReadArgument(TypedDict):
    """One word of a search, and what its grammar makes of it."""

    at: int
    role: str
    """``pattern``, ``file``, ``value``, ``option`` or ``operand``."""


def search_arguments(words: list[str], rules: SearchGrammar) -> Iterator[ReadArgument]:
    """Each word of one search, read by its tool's grammar, in order.

    An option's attached value stays in its word, and a detached one is the
    next word; a word after `--` or not spelled as an option is an operand,
    wherever it stands, since these tools read options after operands too.
    """
    pending = ""
    literal = False
    for index, word in enumerate(words[1:], start=1):
        if pending:
            yield ReadArgument(at=index, role=pending)
            pending = ""
            continue
        if literal or not word.startswith("-") or word == "-":
            yield ReadArgument(at=index, role="operand")
            continue
        if word == "--":
            literal = True
            continue
        # lup: ignore[string-split] — an argv word's attached value, whose only parser is the program's own
        option, attached, _value = word.partition("=")
        letters = "" if word.startswith("--") else word[1:]
        valued = next(
            (at for at, letter in enumerate(letters) if letter in rules["valued"]),
            None,
        )
        role = (
            "pattern"
            if option in rules["pattern_names"]
            or (valued is not None and letters[valued] in rules["patterns"])
            else "file"
            if option in rules["file_names"]
            or (valued is not None and letters[valued] in rules["files"])
            else "value"
        )
        inline = (
            bool(attached)
            if word.startswith("--")
            else (valued is not None and valued + 1 < len(letters))
        )
        takes = option in rules["named"] or valued is not None
        yield ReadArgument(at=index, role=role if takes and inline else "option")
        pending = role if takes and not inline else ""


def pattern_positions(
    words: list[str], grammars: dict[str, SearchGrammar] = SEARCH_GRAMMARS
) -> list[int]:
    """Where a search or an awk program is handed text rather than a path.

    A pattern or a program is matched or run, never opened, so it names no
    file whatever it spells: `grep '.*/token' src` searches for a pattern,
    and `.*/token` is not a path beneath a home. The value of a pattern
    option, and the first operand where no pattern or pattern file was
    given anywhere in the command. A file of patterns stays a path, and so
    does every other operand. An option the grammar does not list is read as
    consuming nothing, which can only leave a word read as the path it
    might be.
    """
    executable = posixpath.basename(words[0]) if words else ""
    family = {"egrep": "grep", "fgrep": "grep", "gawk": "awk", "mawk": "awk"}
    name = family[executable] if executable in family else executable
    if name not in grammars:
        return []
    read = list(search_arguments(words, grammars[name]))
    patterned = any(argument["role"] in ("pattern", "file") for argument in read)
    first = next(
        (argument["at"] for argument in read if argument["role"] == "operand"), None
    )
    return [
        *(argument["at"] for argument in read if argument["role"] == "pattern"),
        *([] if patterned or first is None else [first]),
    ]


def grep_split(
    arguments: list[str],
    valued: str = SEARCH_GRAMMARS["grep"]["valued"],
    named: tuple[str, ...] = SEARCH_GRAMMARS["grep"]["named"],
) -> SplitWords:
    """grep's arguments parted into its options and its pattern and paths.

    ``valued`` and ``named`` are grep's own options that take the next word as
    their value, so that word is read as neither the pattern nor a root.
    """
    return split_options(arguments, valued, named)


def grep_roots(arguments: list[str]) -> list[WalkedRoot]:
    """`grep -r`, in each of its spellings, and nothing else of grep.

    Each `--exclude-dir` is a directory the walk leaves out wherever it meets
    one, and each `--exclude` a file.
    """
    directed = any(
        (flag, value) in (("-d", "recurse"), ("--directories", "recurse"))
        for flag, value in zip(arguments, arguments[1:], strict=False)
    )
    split = grep_split(arguments)
    spelled = ("--recursive", "--dereference-recursive", "--directories=recurse")
    if not (directed or recursing(split["options"], "rR", spelled)):
        return []
    return searched_roots(
        split,
        True,
        option_values(arguments, "--exclude-dir"),
        option_values(arguments, "--exclude"),
    )


def rg_roots(
    arguments: list[str],
    valued: str = SEARCH_GRAMMARS["rg"]["valued"],
    named: tuple[str, ...] = SEARCH_GRAMMARS["rg"]["named"],
) -> list[WalkedRoot]:
    """`rg`, which always walks, and reads dot names only when told to.

    `-u` lifts the ignore files and a second `-u` the dot names too, so the
    count is what decides; `--hidden` and `-.` say it outright. `--files`
    lists the names it would search, which reads nothing. ``valued`` and
    ``named`` are rg's own options that take the next word as their value.
    """
    split = split_options(arguments, valued, named)
    options = split["options"]
    if "--files" in options or "--type-list" in options:
        return []
    hidden = (
        any(option in ("--hidden", "-.") or "." in short(option) for option in options)
        or sum(short(option).count("u") for option in options) >= 2
    )
    return searched_roots(split, hidden, [], [])


def tar_members(arguments: list[str]) -> Iterator[str]:
    """The members an archive being written reads, each from where `-C` left it.

    Written means created, appended to, or updated: a listing or an
    extraction reads the archive rather than the disk. The operation and its
    value letters may come bundled in a first word with no dash (`czf`), and
    each value letter takes the next word in turn -- the archive after `f`,
    a directory after `C` that every later member is read from.
    """
    bundled = arguments[:1] if arguments and not arguments[0].startswith("-") else []
    letters = "".join([*bundled, *(short(word) for word in arguments)])
    writing = any(letter in "cru" for letter in letters) or any(
        word in ("--create", "--append", "--update") for word in arguments
    )
    if not writing or "--no-recursion" in arguments:
        return
    directory = ""
    pending = ""
    for index, word in enumerate(arguments):
        if pending:
            directory = word if pending[0] == "C" else directory
            pending = pending[1:]
            continue
        if word.startswith("--directory="):
            directory = word.removeprefix("--directory=")
            continue
        if word in ("--directory", "--file", "--files-from", "--exclude-from"):
            pending = "C" if word == "--directory" else "f"
            continue
        if word.startswith("--"):
            continue
        if word.startswith("-") or (index == 0 and bundled):
            cluster = short(word) or word
            pending = "".join(letter for letter in cluster if letter in "fCTXb")
            continue
        yield posixpath.join(directory, word) if directory else word


def copied_roots(
    arguments: list[str], letters: str, spelled: tuple[str, ...], sources: bool
) -> list[WalkedRoot]:
    """The trees a recursive copy or comparison reads: its sources, or every operand."""
    split = split_options(arguments)
    if not recursing(split["options"], letters, spelled):
        return []
    operands = split["operands"]
    read = operands[:-1] if sources else operands
    return [
        WalkedRoot(path=root, hidden=True, excluded=[], skipped=[], yielded=[])
        for root in read
    ]


def find_roots(arguments: list[str]) -> list[str]:
    """The starting points `find` walks from: the operands before its expression.

    Past the options that come first (`-H`, `-L`, `-P`, `-D <list>`,
    `-O<level>`), every word up to the first that opens the expression --
    an option, `(`, `!` -- names a root. Naming none, it walks where it
    stands.
    """
    leading = 0
    for index, word in enumerate(arguments):
        if index < leading:
            continue
        if word not in ("-H", "-L", "-P", "-D") and not word.startswith("-O"):
            break
        leading = index + (2 if word == "-D" else 1)
    rest = arguments[leading:]
    opened = next(
        (
            index
            for index, word in enumerate(rest)
            if word.startswith("-") or word in ("(", "!", ")", ",")
        ),
        len(rest),
    )
    return rest[:opened] or ["."]


def names_every_file(arguments: list[str]) -> bool:
    """Whether a `find` hands each file it meets to a program by name.

    An `-exec`, `-execdir`, `-ok` or `-okdir` whose payload carries `{}`,
    which find replaces with every name it yields.
    """

    def payload(start: int) -> list[str]:
        """The words one action runs, up to the `;` or `+` ending it."""
        rest = arguments[start + 1 :]
        ended = next(
            (index for index, word in enumerate(rest) if word in (";", "+")),
            len(rest),
        )
        return rest[:ended]

    actions = ("-exec", "-execdir", "-ok", "-okdir")
    return any(
        "{}" in word
        for start, action in enumerate(arguments)
        if action in actions
        for word in payload(start)
    )


def find_names(arguments: list[str]) -> list[str]:
    """The name globs a `find` expression yields only, where it is a plain AND.

    `-name` and `-iname` tests joined by nothing but juxtaposition restrict
    every name it yields; an `-o`, a `!` or `-not`, a `,` or a parenthesis
    makes it an expression this does not evaluate, so it yields every name.
    """
    joined = ("-o", "-or", "!", "-not", ",", "(", ")")
    if any(word in joined for word in arguments):
        return []
    return [
        value
        for test, value in zip(arguments, arguments[1:], strict=False)
        if test in ("-name", "-iname")
    ]


def listed_roots(words: list[str]) -> list[str]:
    """The roots a listing prints every name beneath, read or not.

    `find`, `du` and `tree` always walk; `ls` only with `-R`; `fd` and `rg
    --files` take a pattern before their roots, and `rg --files` none.
    """
    arguments = words[1:]
    split = split_options(arguments)
    operands = split["operands"]
    match posixpath.basename(words[0]) if words else "":
        case "find":
            return find_roots(arguments)
        case "du" | "tree":
            return operands or ["."]
        case "ls" if recursing(split["options"], "R", ("--recursive",)):
            return operands or ["."]
        case "fd" | "fdfind":
            return operands[1:] or ["."]
        case "rg" if "--files" in split["options"]:
            return operands or ["."]
        case _:
            return []


def reads_names(words: list[str]) -> bool:
    """Whether a command runs a program over names it reads from its input.

    `xargs` with a payload -- or with options nobody read, which could hide
    one -- and `read` or `mapfile` binding names a loop then hands on.
    `xargs` alone prints what it read.
    """
    match posixpath.basename(words[0]) if words else "":
        case "xargs":
            payload = xargs_payload(words)
            return payload is None or bool(payload)
        case "read" | "mapfile" | "readarray":
            return True
        case _:
            return False


def walked_roots(words: list[str], listed: bool = False) -> list[WalkedRoot]:
    """Every root one command reads everything beneath, by its own grammar.

    ``listed`` says the line reads each name its listings print, which makes
    every listing's roots walked roots too.
    """
    arguments = words[1:]
    executable = posixpath.basename(words[0]) if words else ""
    if listed or (executable == "find" and names_every_file(arguments)):
        roots = listed_roots(words)
        yielded = find_names(arguments) if executable == "find" else []
        if roots:
            return [
                WalkedRoot(
                    path=root, hidden=True, excluded=[], skipped=[], yielded=yielded
                )
                for root in roots
            ]
    match executable:
        case "grep" | "egrep" | "fgrep":
            return grep_roots(arguments)
        case "rg":
            return rg_roots(arguments)
        case "tar":
            return [
                WalkedRoot(
                    path=member, hidden=True, excluded=[], skipped=[], yielded=[]
                )
                for member in tar_members(arguments)
            ]
        case "zip":
            split = split_options(arguments)
            spelled = ("--recurse-paths", "--recurse-patterns")
            if not recursing(split["options"], "rR", spelled):
                return []
            return [
                WalkedRoot(
                    path=member, hidden=True, excluded=[], skipped=[], yielded=[]
                )
                for member in split["operands"][1:]
            ]
        case "cp":
            return copied_roots(arguments, "rRa", ("--recursive", "--archive"), True)
        case "rsync":
            return copied_roots(arguments, "ra", ("--recursive", "--archive"), True)
        case "scp":
            return copied_roots(arguments, "r", (), True)
        case "diff":
            return copied_roots(arguments, "r", ("--recursive",), False)
        case _:
            return []


def placed_root(word: str, directory: str | None) -> str | None:
    """Where one walked root stands, as the host is asked to walk it.

    A home is spelled from the home, wherever a `cd` left the segment, so a
    root opening on `~` or on the variable naming it keeps its spelling for
    the host to expand; every other root is placed as any path operand is.
    """
    if word.startswith("~") or word.startswith(("$HOME", "${HOME}")):
        return word
    return placed_path(word, directory)


def shell_walked_roots(command: str, rows: list[ShellRuleRow]) -> list[WalkedRoot]:
    """Every root a command's recursive readers walk, placed where each segment stands.

    Handed to the host, which walks each as the reader would and says what
    withheld path it met; the classifier then refuses the segment whose root
    it was. A root a `cd` left unreadable is not placed, and the segment's own
    reading answers it.
    """
    segments = read_segments(command, rows)
    listed = names_read(command, rows)
    return [
        WalkedRoot(
            path=placed,
            hidden=root["hidden"],
            excluded=root["excluded"],
            skipped=root["skipped"],
            yielded=root["yielded"],
        )
        for segment in segments
        for root in walked_roots(segment["words"], listed)
        for placed in [placed_root(root["path"], segment["directory"])]
        if placed is not None
    ]


def names_read(command: str, rows: list[ShellRuleRow]) -> bool:
    """Whether a line hands names read from its input to a program anywhere.

    Read over the whole line rather than one pipeline, because a listing can
    reach its reader by more than a pipe -- a file written first, a `while
    read` loop over a substitution -- and every reading here errs toward
    naming more roots rather than fewer.
    """
    return any(
        reads_names(segment["words"]) for segment in read_segments(command, rows)
    )
