"""Which commands read everything beneath a path, and from which of their words.

A key or a login is withheld from every word that names it, and a recursive
reader names none: `grep -r password ~` and `tar czf out.tgz ~` spell a home
and read every key inside it. Which words a program walks is that program's
grammar, so it is read here one utility at a time, and the host then says
whether anything withheld lies beneath each root -- a fact only a filesystem
has.

A listing is not a read: `ls -R`, `du` and `tree` print names, and a name is
not the secret. `find` walks as well, but what it hands a payload is decided
by predicates only the run evaluates, and the payload is judged as the command
it is, so its roots are not read as walked here. `git grep` reads what Git
tracks, which a login never is.

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


def excluded_name(name: str, globs: list[str]) -> bool:
    """Whether a walk told to leave out these globs leaves out this directory."""
    return any(fnmatchcase(name, glob) for glob in globs)


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
        WalkedRoot(path=root, hidden=hidden, excluded=excluded, skipped=skipped)
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


def grep_split(
    arguments: list[str],
    valued: str = "ABCDdefm",
    named: tuple[str, ...] = (
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
    valued: str = "ABCEMTdefgjmrt",
    named: tuple[str, ...] = (
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
        WalkedRoot(path=root, hidden=True, excluded=[], skipped=[]) for root in read
    ]


def walked_roots(words: list[str]) -> list[WalkedRoot]:
    """Every root one command reads everything beneath, by its own grammar."""
    arguments = words[1:]
    match posixpath.basename(words[0]) if words else "":
        case "grep" | "egrep" | "fgrep":
            return grep_roots(arguments)
        case "rg":
            return rg_roots(arguments)
        case "tar":
            return [
                WalkedRoot(path=member, hidden=True, excluded=[], skipped=[])
                for member in tar_members(arguments)
            ]
        case "zip":
            split = split_options(arguments)
            spelled = ("--recurse-paths", "--recurse-patterns")
            if not recursing(split["options"], "rR", spelled):
                return []
            return [
                WalkedRoot(path=member, hidden=True, excluded=[], skipped=[])
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
    return [
        WalkedRoot(
            path=placed,
            hidden=root["hidden"],
            excluded=root["excluded"],
            skipped=root["skipped"],
        )
        for segment in read_segments(command, rows)
        for root in walked_roots(segment["words"])
        for placed in [placed_root(root["path"], segment["directory"])]
        if placed is not None
    ]
