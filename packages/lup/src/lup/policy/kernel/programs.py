# lup: ignore[string-split]
# The dependency-free runtime deliberately uses primitive rows and stdlib scanners.
"""What an interpreter invocation hands the interpreter to run.

One criterion decides every interpreter form: an invocation is refused when
it leaves no reviewable artifact behind. Inline code leaves nothing to read,
an interpreter handed nothing runs whatever arrives on its input, and a
program fetched from elsewhere is read by nobody here. A script file is
openable, diffable and runnable again, so it is none of those.

Applying that needs the interpreter's own grammar, because the program sits
where the options stop: `bash -o pipefail x.sh` runs `x.sh`, and reading
`pipefail` as the script would judge a word that is only an option's value.
So each interpreter's options are listed as what they are -- carrying the
program, consuming the next word, or consuming nothing -- and an option no
list names is unread rather than guessed at, because a guess is exactly how
a value would come to be read as the script.
"""

import posixpath
from typing import Literal, TypedDict

from .decision import SUBSTITUTION_SENTINEL, KernelDecision
from .syntax import expands

type ProgramKind = Literal[
    "script",
    "inline",
    "bare",
    "unread",
    "remote",
    "module",
    "subcommand",
    "informational",
]
"""What an invocation turned out to hand its interpreter.

``module`` and ``subcommand`` are not answers but hand-offs: a module is
judged by whether this project declares its root, and a subcommand by the
vocabulary row of the tool that owns it. ``informational`` hands it nothing
and asks it only for its version or usage, which it prints before it would
read a program."""


class OptionGrammar(TypedDict):
    """Which of one tool's options consume a value, and which consume nothing.

    The tool's own spellings, a fact about it rather than a preference: a
    spelling missing here makes a real invocation unread, never permitted.
    """

    valued: list[str]
    """Options consuming the following word, or a value attached with ``=``."""

    flags: list[str]
    """Options consuming nothing."""

    families: list[str]
    """Prefixes of long options that consume nothing, such as ``--no-``."""

    open_attached: bool
    """Whether an unlisted long option carrying its value after ``=`` is inert.

    True only for a tool none of whose unlisted options can change what the
    reading is about: an attached value cannot move the operand after it, so
    only a name that carries meaning of its own has to be listed."""

    attached: list[str]
    """Short options whose value, when they take one, is pressed against them.

    Never the next word: `xargs -i` replaces `{}` and `-iX` replaces `X`, so
    the word after a bare `-i` is the command. Read as consuming it, the
    command was taken for the option's value."""


class ReadOption(TypedDict):
    """One option as a command line spelled it, and the value it consumed."""

    name: str
    value: str | None


class ReadWord(TypedDict):
    """The options one command-line word spelled, and the words they consumed."""

    options: list[ReadOption]
    width: int


class InterpreterGrammar(OptionGrammar):
    """How one interpreter's command line names the program it runs.

    A spelling missing from ``inline`` is a hole, where one missing from the
    option lists only makes a real invocation unread.
    """

    inline: list[str]
    """Options that carry the program itself, or have it read from stdin."""

    informational: list[str]
    """Options that print the interpreter's version or usage and run nothing.

    The tool's own spellings, and only those: `bash -h` hashes commands and
    `bash -v` echoes its input, where `node -v` prints a version, so a letter
    one tool spends on help another spends on something else. Each is read
    as a flag consuming nothing, since reading past it is how a program
    beside it is still found."""

    module: str
    """The option naming a module to run in place of a file, or empty."""

    runner: str
    """The subcommand that runs a file, or empty where the first operand is it."""

    evaluator: str
    """The subcommand that runs its operand as code, or empty."""

    suffixes: list[str]
    """What an operand ends in to be read as a script rather than a subcommand.

    Empty where every operand is a script. Set for a tool whose first operand
    may equally be one of its own subcommands, and whose subcommands never
    carry a path separator or a suffix."""


class ProgramReading(TypedDict):
    """What one invocation hands its interpreter, and the word that says so."""

    kind: ProgramKind
    subject: str


def grammar(
    inline: tuple[str, ...] = (),
    valued: tuple[str, ...] = (),
    flags: tuple[str, ...] = (),
    families: tuple[str, ...] = (),
    open_attached: bool = False,
    module: str = "",
    runner: str = "",
    evaluator: str = "",
    suffixes: tuple[str, ...] = (),
    attached: tuple[str, ...] = (),
    informational: tuple[str, ...] = (),
) -> InterpreterGrammar:
    """One grammar row, with every list it does not name empty."""
    return InterpreterGrammar(
        inline=list(inline),
        informational=list(informational),
        valued=list(valued),
        flags=list(flags),
        families=list(families),
        open_attached=open_attached,
        attached=list(attached),
        module=module,
        runner=runner,
        evaluator=evaluator,
        suffixes=list(suffixes),
    )


SHELL_GRAMMAR = grammar(
    inline=("-c", "-s"),
    valued=("-o", "+o", "-O", "+O", "--rcfile", "--init-file", "--emulate"),
    flags=(
        *(f"{sign}{letter}" for sign in "-+" for letter in "abefhkmnptuvxBCEHPT"),
        *(f"-{letter}" for letter in "ilrD"),
        "--debug",
        "--debugger",
        "--dump-po-strings",
        "--dump-strings",
        "--login",
        "--noediting",
        "--noprofile",
        "--norc",
        "--posix",
        "--pretty-print",
        "--restricted",
        "--verbose",
    ),
    informational=("--help", "--version"),
)
"""The POSIX shells' shared invocation grammar, as bash spells its superset.

`-c` hands over a command string and `-s` reads commands from stdin, so both
carry the program. `-o` and `-O` name a setting, which is the value a script
position would otherwise be mistaken for."""

PYTHON_GRAMMAR = grammar(
    inline=("-c",),
    valued=("-W", "-X", "--check-hash-based-pycs"),
    flags=(*(f"-{letter}" for letter in "bBdEiIOPqsSuvx"),),
    informational=(
        "-h",
        "-V",
        "--help",
        "--help-env",
        "--help-xoptions",
        "--help-all",
        "--version",
    ),
    module="-m",
)
"""Python's invocation grammar, read where `uv run` hands it a program."""

INTERPRETER_GRAMMARS: dict[str, InterpreterGrammar] = {
    **{shell: SHELL_GRAMMAR for shell in ("sh", "bash", "zsh", "dash", "ksh")},
    "fish": grammar(
        inline=("-c", "--command", "-C", "--init-command"),
        flags=("-i", "--interactive", "-l", "--login", "-n", "--no-execute", "-N"),
        informational=("-h", "--help", "-v", "--version"),
    ),
    "python": PYTHON_GRAMMAR,
    "python3": PYTHON_GRAMMAR,
    "node": grammar(
        inline=("-e", "--eval", "-p", "--print", "-i", "--interactive"),
        valued=(
            "-r",
            "--require",
            "--import",
            "--loader",
            "--experimental-loader",
            "-C",
            "--conditions",
            "--input-type",
            "--env-file",
            "--env-file-if-exists",
            "--run",
            "--title",
            "--inspect-port",
            "--redirect-warnings",
            "--report-dir",
            "--report-directory",
            "--report-filename",
            "--diagnostic-dir",
            "--cpu-prof-dir",
            "--cpu-prof-name",
            "--heap-prof-dir",
            "--heap-prof-name",
            "--watch-path",
            "--test-reporter",
            "--test-reporter-destination",
            "--test-name-pattern",
            "--test-skip-pattern",
            "--unhandled-rejections",
            "--disable-warning",
            "--dns-result-order",
            "--icu-data-dir",
            "--openssl-config",
        ),
        flags=(
            "-c",
            "--check",
            "--inspect",
            "--inspect-brk",
            "--inspect-wait",
            "--watch",
            "--watch-preserve-output",
            "--test",
            "--enable-source-maps",
            "--preserve-symlinks",
            "--preserve-symlinks-main",
            "--abort-on-uncaught-exception",
            "--expose-gc",
            "--frozen-intrinsics",
            "--pending-deprecation",
            "--throw-deprecation",
            "--zero-fill-buffers",
            "--cpu-prof",
            "--heap-prof",
            "--prof",
            "--permission",
            "--jitless",
        ),
        families=("--no-", "--experimental-", "--trace-", "--allow-", "--test-"),
        open_attached=True,
        informational=("-v", "--version", "-h", "--help"),
    ),
    "bun": grammar(
        inline=("-e", "--eval", "-p", "--print"),
        valued=(
            "-r",
            "--preload",
            "--require",
            "--import",
            "-d",
            "--define",
            "-l",
            "--loader",
            "-c",
            "--config",
            "--cwd",
            "--env-file",
            "--tsconfig-override",
            "--main-fields",
            "--extension-order",
            "--jsx-factory",
            "--jsx-fragment",
            "--jsx-import-source",
            "--jsx-runtime",
            "--conditions",
            "--port",
            "-F",
            "--filter",
            "--console-depth",
            "--title",
        ),
        flags=(
            "--watch",
            "--hot",
            "--smol",
            "-b",
            "--bun",
            "--silent",
            "-i",
            "--prefer-offline",
            "--prefer-latest",
            "--inspect",
            "--inspect-wait",
            "--inspect-brk",
            "--if-present",
            "--expose-gc",
            "--zero-fill-buffers",
            "--throw-deprecation",
        ),
        families=("--no-",),
        open_attached=True,
        informational=("-v", "--version", "--revision", "-h", "--help"),
        suffixes=(".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".mts", ".cts"),
    ),
    "deno": grammar(
        valued=(
            "-c",
            "--config",
            "--import-map",
            "--cert",
            "--location",
            "--seed",
            "--ext",
            "--preload",
            "--conditions",
            "-L",
            "--log-level",
        ),
        flags=(
            "-A",
            "--allow-all",
            "-P",
            "--permission-set",
            "-q",
            "--quiet",
            "-r",
            "--reload",
            "--check",
            "--cached-only",
            "--lock",
            "--frozen",
            "--env-file",
            "--node-modules-dir",
            "--vendor",
            "--inspect",
            "--inspect-brk",
            "--inspect-wait",
            "--v8-flags",
            "--watch",
            "--watch-hmr",
            "--watch-exclude",
            "--coverage",
            "--unstable",
            "--unsafely-ignore-certificate-errors",
        ),
        families=("--allow-", "--deny-", "--no-", "--unstable-"),
        open_attached=True,
        runner="run",
        evaluator="eval",
        informational=("-V", "--version", "-h", "--help"),
    ),
    "perl": grammar(inline=("-e", "-E"), informational=("-v", "-V", "-h")),
    "ruby": grammar(inline=("-e",), informational=("--version", "-h", "--help")),
    "php": grammar(
        inline=("-r", "-a"),
        informational=("-v", "--version", "-h", "--help"),
    ),
}
"""Every interpreter's invocation grammar, keyed by executable name.

Each interpreter the kernel refuses bare has a row, so a form handed to one
through `uv run` is read by the same grammar; a row naming only ``inline``
reads every other option as unread, which refuses rather than guesses."""

SCRIPT_INTERPRETERS = ("bash", "sh", "zsh", "node", "bun", "deno")
"""The interpreters this policy lets run a named script file directly.

Python is absent on purpose: it runs through `uv run python <script>`, in
this project's environment, and the bare spelling keeps pointing there."""

PROGRAM_RULE = "shell:interpreter-program"
"""The rule an interpreter's refusal of the program it was handed carries.

Named so a reader of the verdict can tell a refusal about the program from
one about the tool: a `--help` beside that program is the program's argument
(`bash -c ls --help` runs `ls`), so no usage question lifts it."""


def read_options(
    word: str, following: list[str], rules: OptionGrammar
) -> ReadWord | None:
    """The options one word spells, and how many words they consumed.

    A single-dash word is read letter by letter, the way these tools read a
    cluster: `-ec` sets `-e` and then `-c`, `-Wignore` attaches its value,
    and `-sSo out` ends in an option consuming the next word. ``None`` where
    an option is unlisted or its value is missing, so nothing after it is
    read in a position it might not hold.
    """
    name, equals, attached = word.partition("=")
    if name in rules["valued"]:
        if equals:
            return ReadWord(options=[ReadOption(name=name, value=attached)], width=1)
        if not following:
            return None
        return ReadWord(options=[ReadOption(name=name, value=following[0])], width=2)
    if name in rules["flags"] or (
        word.startswith("--")
        and any(name.startswith(family) for family in rules["families"])
    ):
        flagged = ReadOption(name=name, value=attached if equals else None)
        return ReadWord(options=[flagged], width=1)
    if word.startswith("--"):
        opened = equals and rules["open_attached"]
        spelled = ReadOption(name=name, value=attached)
        return ReadWord(options=[spelled], width=1) if opened else None
    letters = [word[0] + letter for letter in word[1:]]
    ends = next(
        (
            at
            for at, option in enumerate(letters)
            if option in rules["valued"] or option in rules["attached"]
        ),
        len(letters),
    )
    if any(option not in rules["flags"] for option in letters[:ends]):
        return None
    read = [ReadOption(name=option, value=None) for option in letters[:ends]]
    if ends == len(letters):
        return ReadWord(options=read, width=1)
    # The letter at `ends` sits at `ends + 1` in the word, after its sign.
    rest = word[ends + 2 :]
    if letters[ends] in rules["attached"]:
        return ReadWord(
            options=[*read, ReadOption(name=letters[ends], value=rest)], width=1
        )
    if not rest and not following:
        return None
    return ReadWord(
        options=[
            *read,
            ReadOption(name=letters[ends], value=rest if rest else following[0]),
        ],
        width=1 if rest else 2,
    )


def operand_reading(word: str, rules: InterpreterGrammar) -> ProgramReading:
    """What the first operand hands the interpreter.

    A stream alias reads the program from a descriptor, not a file anybody
    can open afterwards, so it is inline. A URL or package specifier is
    fetched from elsewhere. A word this reading cannot see into is unread.
    """
    if expands(word):
        return ProgramReading(kind="unread", subject=word)
    if "://" in word or word.startswith(("data:", "npm:", "jsr:", "node:")):
        return ProgramReading(kind="remote", subject=word)
    if word == "-" or word.startswith(("/dev/", "/proc/")):
        return ProgramReading(kind="inline", subject=word)
    if (
        rules["suffixes"]
        and "/" not in word
        and not word.endswith(tuple(rules["suffixes"]))
    ):
        return ProgramReading(kind="subcommand", subject=word)
    return ProgramReading(kind="script", subject=word)


def read_program(
    words: list[str],
    grammars: dict[str, InterpreterGrammar] = INTERPRETER_GRAMMARS,
) -> ProgramReading:
    """Read what one interpreter invocation hands the interpreter to run.

    Options are consumed until the first operand, which is the program; the
    words after it are the program's own arguments and are never read as the
    interpreter's. A tool with a runner subcommand has it first, and its
    evaluating subcommand carries code the way `-c` does.
    """
    executable = posixpath.basename(words[0])
    rules = grammars[executable] if executable in grammars else grammar()
    # An inline option consumes nothing here: the reading stops at it, so what
    # it would have consumed is never read in a position it does not hold.
    readable = OptionGrammar(
        valued=rules["valued"],
        flags=[*rules["flags"], *rules["inline"], *rules["informational"]],
        families=rules["families"],
        open_attached=rules["open_attached"],
        attached=rules["attached"],
    )
    signed = any(
        option.startswith("+") for option in [*rules["valued"], *rules["flags"]]
    )
    awaiting_runner = bool(rules["runner"])
    # Asked for its version or usage, it runs nothing -- but only where that
    # is all it was handed: a program beside the question is read as before.
    asked = ""

    def found(reading: ProgramReading) -> ProgramReading:
        """The reading, spelled from the runner on where one was consumed."""
        if awaiting_runner or not rules["runner"]:
            return reading
        spelled = f"{rules['runner']} {reading['subject']}".rstrip()
        return ProgramReading(kind=reading["kind"], subject=spelled)

    position = 1
    while position < len(words):
        word = words[position]
        following = words[position + 1 :]
        if word == "--":
            return found(
                operand_reading(following[0], rules)
                if following and not awaiting_runner
                else ProgramReading(kind="bare", subject=word)
            )
        if rules["module"] and word == rules["module"]:
            return ProgramReading(
                kind="module" if following else "bare",
                subject=following[0] if following else word,
            )
        if len(word) > 1 and (word.startswith("-") or (signed and word[0] == "+")):
            read = read_options(word, following, readable)
            if read is None:
                return found(ProgramReading(kind="unread", subject=word))
            if any(
                option["name"] in rules["inline"]
                or (option["value"] or "").startswith("data:")
                for option in read["options"]
            ):
                return found(ProgramReading(kind="inline", subject=word))
            if not asked and any(
                option["name"] in rules["informational"] for option in read["options"]
            ):
                asked = word
            position += read["width"]
            continue
        if awaiting_runner:
            if word == rules["runner"]:
                awaiting_runner = False
                position += 1
                continue
            kind: ProgramKind = "inline" if word == rules["evaluator"] else "subcommand"
            return ProgramReading(kind=kind, subject=word)
        return found(operand_reading(word, rules))
    if asked:
        return found(ProgramReading(kind="informational", subject=asked))
    return found(ProgramReading(kind="bare", subject=""))


def program_verdict(spelled: str, reading: ProgramReading) -> KernelDecision | None:
    """What one reading earns, spelled as the command that reached it.

    ``None`` for the two hand-offs, a module or a subcommand, which the
    caller judges by what it declares rather than by this criterion.
    """
    subject = reading["subject"]
    match reading["kind"]:
        case "script":
            return KernelDecision(
                "allow", "a script file can be read, where inline code cannot"
            )
        case "informational":
            return KernelDecision(
                "allow", f"{spelled} {subject} prints its own version or usage"
            )
        case "inline":
            return KernelDecision(
                "deny",
                f"{spelled} {subject}: inline code leaves nothing behind to review",
                recovery="Write the code to a named script file, which can be"
                " reviewed and run again.",
                rule=PROGRAM_RULE,
            )
        case "bare":
            return KernelDecision(
                "deny",
                f"{spelled} with no script file runs whatever it is fed, and"
                " leaves nothing behind to review",
                recovery="Name a script file.",
                rule=PROGRAM_RULE,
            )
        case "unread" if expands(subject):
            named = (
                "a word a command substitution builds"
                if SUBSTITUTION_SENTINEL in subject
                else f"`{subject}`"
            )
            return KernelDecision(
                "deny",
                f"{spelled} runs {named}, which only the run can read, and it"
                " could as well be inline code or its input as a script file",
                recovery="Name the script file the interpreter runs.",
                rule=PROGRAM_RULE,
            )
        case "unread":
            return KernelDecision(
                "deny",
                f"{spelled} {subject}: an option this policy does not read, so"
                " the script it would run is unread",
                recovery="Spell the option's value with `=`, or run the script"
                " without it.",
                rule=PROGRAM_RULE,
            )
        case "remote":
            return KernelDecision(
                "deny",
                f"{spelled} {subject}: a program fetched from elsewhere leaves"
                " nothing here to review",
                recovery="Save the script to a file in this checkout, read it,"
                " and run that.",
                rule=PROGRAM_RULE,
            )
    return None
