# lup: ignore[empty-collection, set-shape, string-split]
# The dependency-free runtime deliberately uses primitive rows and stdlib scanners.
"""Word-level shell helpers: expansion safety, flags, and payloads."""

import posixpath
from collections.abc import Iterator, Sequence
from fnmatch import fnmatchcase
from pathlib import PurePosixPath
from typing import Literal, TypedDict

from .archives import archive_targets, archive_write
from .decision import (
    CheckpointRequirement,
    KernelDecision,
    SUBSTITUTION_SENTINEL,
    unjudged,
)
from .edit import path_rule_matches, protected_path_reason, yields_to_scratch
from .programs import InterpreterGrammar, ReadOption, grammar, read_options
from .roles import (
    GENERATED_PLUGIN_RECOVERY,
    GENERATED_PLUGIN_REFUSAL,
    declared_scratch,
    is_generated_plugin_target,
    path_role,
    repository_relative,
    spells_its_path,
)
from .semantics import Reach
from .rows import (
    DisplacedTargetRow,
    PathRoleRow,
    PathRuleRow,
    PathWord,
    ShellRuleRow,
)
from .syntax import VerbatimText, verbatim_piece


class EffectiveCommand(TypedDict):
    """The words the shell finally executes, and the dangerous names it binds.

    The names rather than a flag, because the verdict they earn has to say
    which variable tripped it: the reviewer reads the reason and nothing else.
    """

    words: list[str]
    dangerous: list[str]


class VerbOperands(TypedDict):
    """A path verb's operands, and whether every flag among them was inert.

    ``named`` is the same operands with the word each was read from, for a
    caller that has to put one back after resolving it; ``operands`` is what
    it reads as, for the callers that only ask which files.
    """

    operands: list[str]
    named: list[PathWord]
    inert: bool


# lup: ignore[library-default] — real wrappers that exec the argument after them
PASS_THROUGH_WORDS = (
    "env",
    "command",
    "exec",
    "time",
    "nohup",
    "setsid",
    "stdbuf",
)
# lup: ignore[library-default] — variables the shell and language runtimes read to redirect execution
DANGEROUS_ENV_NAMES = (
    "PATH",
    "IFS",
    "ENV",
    "TMPDIR",
    "BASH_ENV",
    "CDPATH",
    "SHELL",
    "HOME",
    "XDG_CONFIG_HOME",
    "PAGER",
    "MANPAGER",
    "EDITOR",
    "VISUAL",
    "NODE_OPTIONS",
    "PERL5LIB",
    "PERL5OPT",
    "RUBYLIB",
    "RUBYOPT",
)
# lup: ignore[library-default] — variable prefixes the OS, those runtimes, and these tools read to redirect execution or retarget a command
DANGEROUS_ENV_PREFIXES = ("LD_", "DYLD_", "PYTHON", "GIT_", "GH_", "BASH_FUNC_")
# lup: ignore[library-default] — variable prefixes that swap the identity, token or repository a later git, gh or ssh acts on
OUTWARD_ENV_PREFIXES = ("GIT_", "GH_", "GITHUB_", "SSH_")
# lup: ignore[library-default] — the shell builtins that bind or unbind a variable a later command sees
BINDING_BUILTINS = ("export", "declare", "typeset", "readonly", "local", "unset")
# lup: ignore[library-default] — real interpreter executables; omitting one is a hole, not a preference
INTERPRETERS = (
    "python",
    "python3",
    "perl",
    "ruby",
    "node",
    "deno",
    "bun",
    "php",
    "sh",
    "bash",
    "zsh",
    "dash",
    "ksh",
    "fish",
)


def timeout_payload(segment: list[str], position: int) -> int:
    """Skip timeout's own options and duration to its wrapped command.

    The options by timeout's grammar, clusters included: `-vk 5 10 cmd` sets
    `-k 5`, and read a word at a time it made `10` the command. An option the
    grammar cannot read is left where it stands, so the command word begins
    with a dash and is refused rather than read one word off.
    """
    payload = read_wrapper(segment, position, "timeout")["payload"]
    unread = payload < len(segment) and segment[payload].startswith("-")
    return payload if unread else payload + 1


def nice_payload(segment: list[str], position: int) -> int:
    """Skip nice's adjustment options to its wrapped command.

    By hand rather than by a grammar, because nice also takes the obsolete
    `-N` adjustment no option list can spell.
    """
    while position < len(segment) and segment[position].startswith("-"):
        option = segment[position]
        position += 2 if option in ("-n", "--adjustment") else 1
    return position


def effective_command(segment: list[str]) -> EffectiveCommand:
    """Skip assignments and transparent wrappers, noting dangerous assignments.

    Wrappers that take values (``timeout 5``, ``nice -n 10``) consume them, so
    the returned words start at the command the shell finally executes. A bare
    ``env`` wrapping nothing this can read is itself the command — a bare one,
    one carrying only options or assignments, and one whose ``-S`` re-splits
    the rest of the line by its own rules — so the segment reading decides it
    rather than being handed a word that is not the program. ``command -v``
    asks where a program is rather than running one, so it is the command too.
    """
    dangerous: list[str] = []
    position = 0
    while position < len(segment):
        word = segment[position]
        name, separator, _value = word.partition("=")
        if separator and name.isidentifier():
            if dangerous_env_name(name) and name not in dangerous:
                dangerous.append(name)
            position += 1
            continue
        executable = posixpath.basename(word)
        if executable == "env" and not env_payload(segment[position:]):
            return EffectiveCommand(words=segment[position:], dangerous=dangerous)
        if executable == "command" and segment[position + 1 : position + 2] in (
            ["-v"],
            ["-V"],
        ):
            return EffectiveCommand(words=segment[position:], dangerous=dangerous)
        if executable in PASS_THROUGH_WORDS:
            reading = read_wrapper(segment, position + 1, executable)
            judged = WRAPPER_JUDGED_OPTIONS.get(executable, ())
            if any(option["name"] in judged for option in reading["options"]):
                return EffectiveCommand(words=segment[position:], dangerous=dangerous)
            position = reading["payload"]
            continue
        if executable == "timeout":
            position = timeout_payload(segment, position + 1)
            continue
        if executable == "nice":
            position = nice_payload(segment, position + 1)
            continue
        return EffectiveCommand(words=segment[position:], dangerous=dangerous)
    return EffectiveCommand(words=[], dangerous=dangerous)


def command_words(words: list[str]) -> list[str]:
    """Skip assignments and transparent wrappers to the effective command."""
    return effective_command(words)["words"]


UV_GLOBAL_VALUE_OPTIONS = (
    "--cache-dir",
    "--color",
    "--allow-insecure-host",
    "--directory",
    "--project",
    "--config-file",
)
"""uv's global options that consume one following value, as `uv help` lists them.

The vocabulary's uv row takes them as its default ``value_flags``, so the walker
finds a declared uv verb past them exactly as the kernel's own reading does."""

UV_TOOL_RUN_GRAMMAR = grammar(
    valued=(
        *UV_GLOBAL_VALUE_OPTIONS,
        "--from",
        "-w",
        "--with",
        "--with-editable",
        "--with-requirements",
        "-c",
        "--constraints",
        "-b",
        "--build-constraints",
        "--overrides",
        "--env-file",
        "--python-platform",
        "--torch-backend",
        "--index",
        "--default-index",
        "-i",
        "--index-url",
        "--extra-index-url",
        "-f",
        "--find-links",
        "--index-strategy",
        "--keyring-provider",
        "-P",
        "--upgrade-package",
        "--upgrade-group",
        "--resolution",
        "--prerelease",
        "--prerelease-package",
        "--fork-strategy",
        "--exclude-newer",
        "--exclude-newer-package",
        "--no-sources-package",
        "--reinstall-package",
        "--link-mode",
        "-C",
        "--config-setting",
        "--config-settings-package",
        "--no-build-isolation-package",
        "--no-build-package",
        "--no-binary-package",
        "--refresh-package",
        "-p",
        "--python",
    ),
    flags=(
        "--isolated",
        "--no-env-file",
        "--lfs",
        "-V",
        "--version",
        "--no-index",
        "-U",
        "--upgrade",
        "--no-sources",
        "--reinstall",
        "--compile-bytecode",
        "--no-build-isolation",
        "--no-build",
        "--no-binary",
        "-n",
        "--no-cache",
        "--refresh",
        "--managed-python",
        "--no-managed-python",
        "--no-python-downloads",
        "-q",
        "--quiet",
        "-v",
        "--verbose",
        "--system-certs",
        "--native-tls",
        "--offline",
        "--no-progress",
        "--no-config",
        "-h",
        "--help",
    ),
)
"""How `uvx` and `uv tool run` spell their options ahead of the tool they run.

The tool is the first operand past these, which is where an interpreter handed
inline code has to be seen: `uvx --from foo python -c 1` runs Python exactly
as `uvx python -c 1` does. An option not listed here could consume the next
word, so it leaves the tool unread rather than guessed at."""


def operand_positions(words: list[str], value_flags: Sequence[str]) -> list[int]:
    """Where a command's operands stand, each value flag's value stepped over.

    A verb chosen from among operands is chosen from these. Read as every word
    not beginning with a dash, a value flag's value was an operand too, so
    `uv pip --cache-dir list install x` named `list` as the verb it runs.
    """
    positions: list[int] = []
    valued = False
    for index, word in enumerate(words):
        if valued:
            valued = False
            continue
        if word.startswith("-"):
            valued = word in value_flags
            continue
        positions.append(index)
    return positions


def uv_command_words(words: list[str]) -> list[str] | None:
    """Place global options after uv's verb without losing their arguments."""
    pending = iter(words[1:])
    options: list[str] = []
    for word in pending:
        if not word.startswith("-"):
            return [words[0], word, *options, *pending]
        name, separator, value = word.partition("=")
        if name in UV_GLOBAL_VALUE_OPTIONS:
            argument = value if separator else next(pending, None)
            if argument is None or (not separator and argument.startswith("-")):
                return None
            options.extend([name, argument])
            continue
        if word in (
            "--quiet",
            "--verbose",
            "--no-cache",
            "--managed-python",
            "--no-managed-python",
            "--no-python-downloads",
            "--system-certs",
            "--native-tls",
            "--offline",
            "--no-progress",
            "--no-config",
        ) or (
            word.startswith("-")
            and len(word) > 1
            and all(letter in "qvn" for letter in word[1:])
        ):
            options.append(word)
            continue
        return None
    return None


def uv_run_words(words: list[str]) -> list[str]:
    """Return the executable portion of a ``uv run`` invocation."""
    position = 2
    value_options = (
        *UV_GLOBAL_VALUE_OPTIONS,
        "--extra",
        "--no-extra",
        "--group",
        "--no-group",
        "--only-group",
        "--no-editable-package",
        "--package",
        "-w",
        "--with",
        "--with-editable",
        "--with-requirements",
        "--env-file",
        "--python-platform",
        "--index",
        "--default-index",
        "-i",
        "--index-url",
        "--extra-index-url",
        "-f",
        "--find-links",
        "--index-strategy",
        "--keyring-provider",
        "-P",
        "--upgrade-package",
        "--upgrade-group",
        "--resolution",
        "--prerelease",
        "--prerelease-package",
        "--fork-strategy",
        "--exclude-newer",
        "--exclude-newer-package",
        "--no-sources-package",
        "--reinstall-package",
        "--link-mode",
        "-C",
        "--config-setting",
        "--config-settings-package",
        "--no-build-isolation-package",
        "--no-build-package",
        "--no-binary-package",
        "--refresh-package",
        "-p",
        "--python",
    )
    while position < len(words) and words[position].startswith("-"):
        option = words[position]
        # `-c` and `-m` stand where a file would, so the form is handed on
        # whole and judged by what it names. `--script` is the long spelling
        # of `-s` and names a path just as `-s` and `--gui-script` do, so it
        # is stepped over and the path behind it is what gets judged.
        if option in ("-c", "-m"):
            return words[position:]
        position += 2 if option in value_options else 1
    return words[position:]


def uv_run_module_root(run_words: list[str]) -> str | None:
    """The root package a ``-m`` names, or ``None`` where nothing names one.

    Only two interpreters are asked: ``uv`` itself, whose ``-m`` is the first
    word of the executable portion, and a Python spelled after it, whose
    ``-m`` sits among its own options. Every other target keeps its ``-m`` —
    ``uv run pytest -m slow`` selects a marker expression, and reading that as
    a module would refuse the way a project runs its own tests.

    The root segment rather than the whole name, because that is what a
    project declares: ``examples`` on the table admits every module beneath
    it, one declaration for a tree rather than one per entry point. A ``-m``
    carrying no module names nothing, which is the interpreter's own usage
    error rather than a root to look up, so it reads as ``None`` too.
    """
    if not run_words:
        return None
    if run_words[0] == "-m":
        return run_words[1].partition(".")[0] if len(run_words) > 1 else None
    if posixpath.basename(run_words[0]) not in INTERPRETERS:
        return None
    for position, word in enumerate(run_words[1:], start=1):
        if word == "-c":
            return None
        if word == "-m":
            following = run_words[position + 1 :]
            return following[0].partition(".")[0] if following else None
    return None


class PathVerb(TypedDict):
    """What one verb that takes paths does to the operands it is given.

    ``inert`` holds the short flags whose presence leaves those operands
    meaning what they read. A long flag or an unrecognized cluster falls
    through to the verb's own effect, and a flag that consumes the following
    word is left out rather than modelled, so `truncate -s 0 f` reads as
    non-inert: callers widen to every operand there, which names the size as
    a target -- harmless, since no size spells a path any scope reading grades
    beyond this checkout.

    ``lands`` is which operands it writes: ``each`` one it is given; the
    ``last``, reading the others -- a copy, or a link that stands where its
    last operand does; or the last with every other one ``moved`` away from
    where it stood. ``creates`` is whether a last operand nothing occupies is
    brought into being with the content of the rest, which is the reading the
    creation grant takes. ``remote`` marks a verb that can reach another
    machine besides, so it keeps its question wherever it lands: no scratch or
    recoverable grant reads it, and its entry only says where a local
    destination is, so a protected one is asked about as the path it is.
    """

    inert: str
    lands: Literal["each", "last", "moved"]
    creates: bool
    remote: bool


# Every verb that acts on paths. Membership is about taking paths, not about
# asking: `mkdir` and `touch` are allowed and still listed, because the
# refusals that read this map — a write inside a generated plugin tree, above
# all — are owed by every verb that names a path. A verb that overwrites one in
# place belongs here for the same reason a verb that removes one does: what is
# at the path afterwards is not what was there before.
# lup: ignore[library-default] — each verb's own POSIX grammar, fixed by what the utility does rather than by who is asking
PATH_VERBS = {
    "rm": PathVerb(inert="rfv", lands="each", creates=False, remote=False),
    "rmdir": PathVerb(inert="pv", lands="each", creates=False, remote=False),
    "mv": PathVerb(inert="fnv", lands="moved", creates=True, remote=False),
    "cp": PathVerb(inert="aprRvL", lands="last", creates=True, remote=False),
    # A copy with modes attached, which is how it writes launch authority as
    # surely as `cp` does.
    "install": PathVerb(inert="cCDpvT", lands="last", creates=True, remote=False),
    "mkdir": PathVerb(inert="pv", lands="each", creates=False, remote=False),
    "touch": PathVerb(inert="acm", lands="each", creates=False, remote=False),
    "ln": PathVerb(inert="sfnvrihTPL", lands="last", creates=False, remote=False),
    "tee": PathVerb(inert="aip", lands="each", creates=False, remote=False),
    "truncate": PathVerb(inert="co", lands="each", creates=False, remote=False),
    "rsync": PathVerb(
        inert="vqcarRbulLkKHpEAXogDtOJSnWxyCzhPimIUNFs0468",
        lands="last",
        creates=False,
        remote=True,
    ),
    "scp": PathVerb(inert="346ABCOpqRrTv", lands="last", creates=False, remote=True),
}


def leaves_the_checkout(path_text: str) -> bool:
    """Whether this spelling reaches somewhere the checkout does not cover.

    Read off the spelling rather than resolved against a root, because the
    readings that need it run before any root is in hand. That makes it
    conservative in the one direction that is safe: an absolute path *inside*
    the checkout reads as outside it and earns the question anyway, while
    nothing outside can read as inside.

    Both escapes are spellings rather than places. An absolute path names
    somewhere without reference to where this session is, and a leading `..`
    climbs out of wherever it is -- and a `..` further along cannot climb past
    what preceded it without an absolute segment, which the first test already
    holds.
    """
    if path_text.startswith("/"):
        return True
    return path_text == ".." or path_text.startswith("../")


# lup: ignore[library-default] — git's own directory contents, a vocabulary
# fixed outside this repository rather than a choice made for an adopter
GIT_ADMIN_ENTRIES = (
    "HEAD",
    "ORIG_HEAD",
    "FETCH_HEAD",
    "MERGE_HEAD",
    "COMMIT_EDITMSG",
    "config",
    "description",
    "index",
    "packed-refs",
    "shallow",
    "branches",
    "hooks",
    "info",
    "logs",
    "modules",
    "objects",
    "refs",
    "worktrees",
)
"""What git itself keeps at the top of a repository directory.

The discriminator between the repository and whatever else is stored beside
it: everything here is git's, and a name that is not is somebody else's.
"""


def reaches_git_administration(path_text: str) -> bool:
    """Whether this spelling reaches git's own content inside a repository.

    A ``.git`` segment alone does not say so. A bare repository is a directory
    *named* ``<name>.git``, and a layout that keeps its checkouts inside it --
    which `lup.devtools.layout` is, placing every sibling worktree under
    ``tree/`` -- puts ordinary source under a segment ending in ``.git``. So
    the segment test on its own grades every file of every checkout as the
    repository, and in that layout no absolute path to source can be spelled
    that does not.

    What separates them is the segment after it, because git's top-level
    contents are a closed vocabulary: ``config`` and ``hooks`` and
    ``worktrees`` are the repository, and ``tree`` and ``trace-archive`` are
    things a layout put beside it. A path that stops at the segment is the
    directory itself and counts as reaching it.
    """
    segments = path_text.split("/")
    for index, segment in enumerate(segments):
        if not segment.endswith(".git"):
            continue
        beneath = segments[index + 1 :]
        return not beneath or beneath[0] in GIT_ADMIN_ENTRIES
    return False


def write_scope(
    path_text: str, path_roles: list[PathRoleRow], checkout: str = ""
) -> str:
    """Which tree a write's target is in, as :class:`WritesPath` names them.

    One reading for every spelling of a write, which is the whole point: a
    redirection and a write flag land the same bytes at the same path, so
    what separates the answers has to be the path rather than the syntax that
    named it.

    ``.git`` is ``protected`` rather than ``production`` because it is not
    reviewable source and not the working tree either -- it is the repository
    the working tree is checked out *of*. A write there is what `git` itself
    does through verbs this table judges one by one; reaching it with a
    redirection goes around all of them, which is the shape an approval
    question exists for. Nothing declares this, because a checkout that did
    not hold it would not be a checkout.

    Read as a segment anywhere rather than as a leading ``.git``, because a
    linked worktree has no leading one: its ``.git`` is a *file* pointing at
    ``<somewhere>/repo.git/worktrees/<name>``, and the config and hooks it
    shares live under that ``repo.git`` directory. So read as a leading
    ``.git``, the repository these sessions run out of would be reachable by
    absolute path and graded ``outside``, where a contained placement writes
    freely — and the measured rule that
    catches an unleased write cannot hold it either, since a launch mounts the
    shared administrative directory writable on purpose. A hook written there
    runs on the operator's next Git command, outside whatever granted it.

    Which segment follows decides it, through
    :func:`reaches_git_administration`, because the same layout that puts the
    administrative directory on an absolute path puts every checkout there
    too: under a bare ``repo.git`` the worktrees are ``repo.git/tree/<name>``,
    so a segment test alone grades all of their source as the repository and
    leaves no absolute spelling of a source file that is not ``protected``.

    A declared role is read before either spelling, because a role is somebody
    saying where a path belongs and a spelling is only this reading guessing.
    The session scratchpad is the case that settles it: it is absolute, so the
    escape test would call it outside, and it is declared scratch, which is
    what it is.

    ``checkout`` is where this repository sits, and every reading below runs
    on the spelling :func:`repository_relative` returns for it, because the
    declared roots are anchored at the repository top and reach no absolute
    spelling of the same file. Without it ``tmp/run.log`` is scratch and
    ``/home/you/project/tmp/run.log`` is production, for one file and one
    write. It is a fact about the machine rather than about this repository,
    so it arrives from the host per call and is never declared; empty is the
    honest answer where the caller has none, and leaves the reading exactly as
    it was.

    A spelling that still carries an expansion is ``unbounded``: it names a
    different file at run time than the one written down, so it could be
    tracked source, the repository, or a path no capture of this checkout
    holds, and it is read as the strictest of them. Read any other way,
    `sort --output=a$X` would be granted as a create of a file named ``a$X``,
    and `> ~/f` settled as captured by a snapshot that never holds a home
    directory. A declared scratch root reached through the
    variable naming it is still scratch, which is read first.
    """
    spelled = repository_relative(path_text, checkout)
    if path_role(spelled, path_roles) == "scratch":
        return "scratch"
    if reaches_git_administration(spelled):
        return "protected"
    if not spells_its_path(spelled):
        return "unbounded"
    if leaves_the_checkout(spelled):
        return "outside"
    return "production"


# lup: ignore[library-default] — the scope vocabulary this repository defines,
# spelled once; `WritesPath.scopes` is the same closed set
SCOPE_PHRASES = {
    "scratch": "a scratch path",
    "production": "a production path",
    "protected": "a protected path",
    "outside": "an outside path",
    "unbounded": "an unbounded path",
}
"""How a reason line names each scope, written out rather than assembled.

One entry per member of the same closed set :attr:`WritesPath.scopes` holds,
so a scope added there has a spelling here or has none at all. Assembling the
phrase instead means deciding the article from the word, which is a rule about
English -- "an hour" against "a university" -- standing in for a table of five
entries that were known when the vocabulary was written.
"""


def unread_over_tracked(
    scope: str, carried: bool, existing: bool, tracked: bool
) -> bool:
    """Whether this write replaces reviewed content with content nobody read.

    Four readings, and every one of them has to hold. The scope keeps scratch
    and everything beyond the checkout out of it. ``carried`` is whether the
    command holds its own bytes, which sends the write to the content gates
    instead and is the case this must not answer twice. ``existing`` and
    ``tracked`` are what makes the content reviewed: a create replaces
    nothing, and a file Git never held has no reviewed version being
    replaced -- yesterday's log rewritten in place is ordinary work.

    Asked of the path rather than of the spelling that named it, because
    ``sort -o src.py f`` and ``sort f > src.py`` land the same bytes at the
    same path. That parity is the whole reason this is one function two
    callers read rather than a check written twice.
    """
    return scope == "production" and not carried and existing and tracked


def unread_question(path: str) -> KernelDecision:
    """The question such a write puts, and the two ways past it.

    Asking rather than refusing is the concession to the premise underneath:
    nothing can read this content in advance, and refusing on that ground
    would fall on exactly the writes for which that is unavoidable.
    """
    return KernelDecision(
        "ask",
        f"{path} is replaced with content only running the command produces,"
        " so nothing reads it before it lands",
        checkpoint=write_checkpoint("production"),
        purpose="quality_review",
        recovery=(
            "write into a scratch path and move the result in once it has been"
            " read, or carry the content in the command so the edit gates read"
            " it as they would an Edit"
        ),
    )


def unlocated_write(named: str) -> KernelDecision:
    """The question a write to an ``unbounded`` path puts, however it is spelled.

    ``named`` is the path as the reason opens on it: a redirection, a write
    flag and a verb's destination are one unknown each. No capture discharges
    it, because the snapshot holds this checkout and nothing says the path
    lands there; binding the path to a literal first is what lets the write be
    judged where it lands.
    """
    return KernelDecision(
        "ask",
        f"{named} is a path that is only known when the command runs",
        checkpoint=write_checkpoint("unbounded"),
        purpose="unrecovered_local_mutation",
        recovery=(
            "bind the path to a literal value first, so the write is judged"
            " where it lands"
        ),
    )


def write_checkpoint(scope: str) -> CheckpointRequirement:
    """Which capture would put back what a write to this scope replaced.

    Read off the scope rather than off the row, because it is the same fact
    the scope already states and a row carries one value for every path it
    might touch. A snapshot of this checkout holds the checkout: a write
    inside it is a targeted loss that capture answers for, and a write to
    ``/etc/hosts`` or into ``.git`` is not held by it at all.

    Getting this from the row would let a redirection outside the tree be
    settled by the capture row -- "the affected paths are captured and
    restorable", said of a path no capture has ever seen.
    """
    return "targeted" if scope in ("scratch", "production") else "unrecoverable"


def written_beyond_the_checkout(
    targets: list[str], path_roles: list[PathRoleRow]
) -> bool:
    """Whether any of these paths is somewhere this checkout cannot answer for.

    The grants below reason about a path from what the checkout knows of it: a
    role declared it disposable, Git could put it back, nothing stands there
    yet. Each of those is a fact about a path inside the checkout, and none of
    them says anything about ``/etc/newfile`` -- where no role reaches, the
    object store holds nothing, and "nothing stands there" is a fact about
    somebody else's filesystem.

    Read through :func:`write_scope`, which is what a redirection and a delete
    already read, so one answer covers every spelling of a write. Read any
    other way, `ls > /etc/newfile` would ask while `cp README.md /etc/newfile`,
    `touch /etc/newfile`, `mkdir /etc/newdir` and `tar -cf /etc/backup.tar
    src` are allowed -- one place, five spellings, two answers.

    ``True`` gives the line back to the row that judges it rather than
    refusing it, which is where a contained session's placement is read: the
    write row allows a write outside the checkout when the call cannot leave
    the boundary, and that is a reading no grant here is holding.
    """
    return any(
        write_checkpoint(write_scope(target, path_roles)) == "unrecoverable"
        for target in targets
    )


def flag_write_targets(words: list[str], write_flags: list[str]) -> list[str]:
    """The paths this command's declared write flags name, in the order given.

    Three spellings reach the same place and all three are read, because a
    guard that recognized two of them would be the flag guard's own history
    repeating: ``--output=path`` carries the value attached, ``--output path``
    and ``-o path`` carry it in the following word.

    Matched exactly rather than through :func:`flag_matches`, which the guard
    beside this one uses. That reader accepts a short flag anywhere inside a
    cluster, which is right for asking whether a guarded flag is present and
    wrong for deciding which word is the path: ``-no`` would carry ``-o``, and
    the following word is then somebody else's operand. A flag whose value is
    missing, clustered, or otherwise unresolvable yields nothing rather than a
    guess -- what reads this uses it to relax a row, so an unnamed target
    leaves the row's own verdict standing and a misnamed one would not.
    """
    return [named["path"] for named in flag_write_words(words, write_flags)]


def global_span(words: list[str], rows: list[ShellRuleRow]) -> int:
    """Where this command's global options end and its subcommand begins.

    The boundary :func:`carried_subcommand` walks to, asked for the position
    rather than for the word. A flag before it belongs to the command and a
    flag after it to the subcommand, which is not a distinction any reader
    can skip: ``git -C`` names a directory to run in and ``git commit -C``
    reuses a commit message, so one spelling means two things and only the
    boundary tells them apart.

    ``len(words)`` where every word is a flag and no subcommand is named.

    Stepped the way :func:`~lup.policy.kernel.commands.split_subcommand`
    steps, because a reader finding the subcommand one word away from where
    the row walk found it answers about a different command: a value flag
    consumes the word after it, and a setting flag consumes the setting it
    carries there, so `git -c color.ui=false rm` names `rm` and not the
    setting. Every reader that refuses or asks about a subcommand's operands
    finds them from here, so `git --no-pager rm README.md` is read as the
    `git rm README.md` it runs. A reader that grants keeps reading the
    subcommand where it is written: a global it did not model could change
    what the grant covers, and a grant missed only costs a question.
    """
    executable = posixpath.basename(words[0])
    defaults = [
        row for row in rows if row["command"] == executable and not row["subcommand"]
    ]
    value_flags = [flag for row in defaults for flag in row["value_flags"]]
    setting_flags = [flag for row in defaults for flag in row["setting_flags"]]
    position = 1
    while position < len(words):
        word = words[position]
        if not word.startswith("-"):
            return position
        if word in value_flags:
            position += 2
            continue
        position += carried_setting(word, setting_flags, words[position + 1 :])["words"]
    return len(words)


def flag_write_words(words: list[str], write_flags: list[str]) -> list[PathWord]:
    """Which words those paths were read out of, and how each spells its path.

    The positional reading :func:`flag_write_targets` is the path list of.
    Both spellings of a value are here because both have to be put back once a
    path is resolved against the directory its command runs in: a following
    word is replaced whole, an attached one keeps its ``--output=`` in front.
    """
    named: list[PathWord] = []
    following = False
    for index, word in enumerate(words[1:], start=1):
        if following:
            following = False
            if not word.startswith("-"):
                named.append(PathWord(at=index, prefix="", path=word))
            continue
        name, sign, value = word.partition("=")
        if sign and name in write_flags:
            if value:
                named.append(
                    PathWord(
                        at=index, prefix=f"{name}=", path=verbatim_piece(word, value)
                    )
                )
            continue
        following = word in write_flags
    return named


def is_trusted_script(word: str, roots: list[str]) -> bool:
    """Recognize an absolute script confined to a native-managed package root."""
    if "$" in word or not word.startswith("/"):
        return False
    normalized = posixpath.normpath(word)
    return any(
        normalized.startswith(posixpath.join(posixpath.normpath(root), ""))
        for root in roots
        if root.startswith("/") and posixpath.normpath(root) != "/"
    )


def path_verb_operands(words: list[str]) -> VerbOperands:
    """A path verb's operands, and whether every flag among them was inert.

    An inert flag does not change what the verb does to its operands, so the
    operand list means what it reads. An unrecognized one can move the
    destination, add one, or change which paths are touched at all — which is
    why the two directions diverge on it: a caller granting something must
    decline outright, and a caller refusing something must widen to every
    operand rather than trust their positions.

    `install` is read by its own grammar (:func:`install_verb_operands`),
    because its everyday spelling carries a mode, an owner or a group with a
    value after it, and a value read as an operand made the file it only reads
    the file it writes.
    """
    if posixpath.basename(words[0]) == "install":
        return install_verb_operands(words)
    allowed = PATH_VERBS[posixpath.basename(words[0])]["inert"]
    named: list[PathWord] = []
    inert = True
    for index, word in enumerate(words[1:], start=1):
        if word == "--":
            continue
        if word.startswith("-") and len(word) > 1:
            if word.startswith("--") or not all(
                letter in allowed for letter in word[1:]
            ):
                inert = False
            continue
        named.append(PathWord(at=index, prefix="", path=word))
    return VerbOperands(
        operands=[operand["path"] for operand in named], named=named, inert=inert
    )


class OptionWord(TypedDict):
    """One word of a command's own option grammar, read with what it consumed.

    ``kind`` is what the word is to the reader asking: an ``operand``, an
    ``option`` carried on, a ``value`` some option names -- ``name`` says
    which -- a ``reading`` form that lands nothing, or ``unmodelled``.
    """

    kind: Literal["operand", "option", "value", "reading", "unmodelled"]
    name: str
    word: str


def operand_word(word: str) -> OptionWord:
    """A word no option of the grammar claimed: a flag nothing models, or an operand."""
    if word.startswith("-") and word != "-":
        return OptionWord(kind="unmodelled", name=word, word=word)
    return OptionWord(kind="operand", name="", word=word)


class PlacedWord(OptionWord):
    """An option-grammar word with where it stands, for a reader that puts paths back.

    ``at`` is the word's position in the command and ``prefix`` whatever it
    carries before the text read out of it -- ``--target-directory=`` -- so a
    path read from one is rewritten where it was spelled, as a
    :class:`~lup.policy.kernel.rows.PathWord` is.
    """

    at: int
    prefix: str


def placed_word(word: OptionWord, at: int, prefix: str = "") -> PlacedWord:
    """One read word, with the position and prefix it was read at."""
    return PlacedWord(
        kind=word["kind"], name=word["name"], word=word["word"], at=at, prefix=prefix
    )


# lup: ignore[library-default] — install's options that leave the source's own bytes at the destination
INSTALL_INERT_OPTIONS = (
    "--compare",
    "--preserve-timestamps",
    "--verbose",
    "--no-target-directory",
)


def inert_install_cluster(word: str) -> bool:
    """Whether a short-option cluster leaves the bytes `install` lands alone.

    Its flags compare, preserve, report, make parents or name a file; the last
    of them may be a mode, an owner or a group with its value attached, which
    changes who may read the file and not what it holds.
    """
    if not word.startswith("-") or word.startswith("--") or len(word) < 2:
        return False
    for position, letter in enumerate(word[1:], start=1):
        if letter in "mog":
            return position + 1 < len(word)
        if letter not in "cCpvDT":
            return False
    return True


def install_word(word: str, at: int) -> list[PlacedWord]:
    """One `install` word that takes no value after it, read by its spelling."""
    if word.startswith("--target-directory="):
        named = word.removeprefix("--target-directory=")
        return [
            placed_word(
                OptionWord(kind="value", name="-t", word=named),
                at,
                "--target-directory=",
            )
        ]
    if (
        word in INSTALL_INERT_OPTIONS
        or word.startswith(("--mode=", "--owner=", "--group="))
        or inert_install_cluster(word)
    ):
        return []
    return [placed_word(operand_word(word), at)]


def install_words(words: list[str]) -> Iterator[PlacedWord]:
    """An `install`'s words by its own grammar, each option read with its value.

    Each is placed where the command spells it, so the operand reading and the
    document fold read one grammar rather than two that could come to differ.
    """
    remaining = iter(enumerate(words[1:], start=1))
    for at, word in remaining:
        match word:
            case "--":
                yield from (
                    placed_word(OptionWord(kind="operand", name="", word=rest), place)
                    for place, rest in remaining
                )
                return
            case "-m" | "-o" | "-g" | "--mode" | "--owner" | "--group":
                next(remaining, None)
            case "-t" | "--target-directory":
                place, named = next(remaining, (at, ""))
                yield placed_word(
                    OptionWord(kind="value", name="-t", word=named), place
                )
            case "-d" | "--directory":
                yield placed_word(OptionWord(kind="reading", name=word, word=word), at)
            case _:
                yield from install_word(word, at)


def install_operands(words: list[str]) -> list[str] | None:
    """The sources of an `install`, then where they land, or ``None`` where unmodelled.

    A mode, an owner or a group changes who may read the file and not what it
    holds, so each is read past with its value. `-d` makes directories and
    lands no document, so it names nothing; `-s` strips a binary and a backup
    adds a file, so each is a place this does not work out.
    """
    read = list(install_words(words))
    if any(word["kind"] == "unmodelled" for word in read):
        return None
    if any(word["kind"] == "reading" for word in read):
        return []
    operands = [word["word"] for word in read if word["kind"] == "operand"]
    directory = [word["word"] for word in read if word["kind"] == "value"][-1:]
    if directory:
        return [*operands, *directory] if operands else None
    return operands if len(operands) >= 2 else None


def install_verb_operands(words: list[str]) -> VerbOperands:
    """An `install`'s operands by its own grammar, the directory `-t` names last.

    Last because that is where every other path verb's destination stands,
    so each reader of :func:`path_verb_operands` finds it there. `-d` makes
    every operand a directory and an option the grammar does not model could
    move any of them, so either leaves the operands unread by position, and
    a reader refusing something widens to every one of them.
    """
    read = list(install_words(words))
    named = [
        PathWord(at=word["at"], prefix=word["prefix"], path=word["word"])
        for word in [
            *(word for word in read if word["kind"] == "operand"),
            *[word for word in read if word["kind"] == "value"][-1:],
        ]
    ]
    return VerbOperands(
        operands=[operand["path"] for operand in named],
        named=named,
        inert=not any(word["kind"] in ("unmodelled", "reading") for word in read),
    )


class RestoreOperands(TypedDict):
    """A ``git restore``'s source ref, if it named one, and the paths it rewrites.

    ``source`` is ``None`` for the index-sourced form, which is the one whose
    safety depends on whether those paths carry uncommitted work.
    """

    source: str | None
    paths: list[str]
    named: list[PathWord]
    """The same paths with the word each was read from, for a caller that has
    to put one back after resolving it against the restore's directory."""


def git_restore_operands(words: list[str], at: int) -> RestoreOperands | None:
    """Split ``git restore`` into the ref it reads from and the paths it writes.

    ``None`` where the line is not a restore, carries a flag beyond the source
    and target selectors, or holds a word that expands at run time — each of
    which leaves the restore row's ask to answer for it, because a flag this
    does not know could move which paths are touched.

    ``at`` is where the caller reads the subcommand: a grant reads it where it
    is written, and a reader naming paths reads it past git's globals, from
    :func:`global_span`.
    """
    if posixpath.basename(words[0]) != "git" or words[at : at + 1] != ["restore"]:
        return None
    source: str | None = None
    named: list[PathWord] = []
    position = at + 1
    while position < len(words):
        word = words[position]
        if word == "--source" and position + 1 < len(words):
            source = words[position + 1]
            position += 2
            continue
        if word.startswith("--source="):
            source = word[len("--source=") :]
            position += 1
            continue
        if word in ("--staged", "--worktree", "-S", "-W", "--"):
            position += 1
            continue
        if word.startswith("-"):
            return None
        named.append(PathWord(at=position, prefix="", path=word))
        position += 1
    paths = [path["path"] for path in named]
    if not paths or (source is not None and source.startswith("-")):
        return None
    if opaque_argument(source or "") or any(opaque_argument(word) for word in paths):
        return None
    return RestoreOperands(source=source, paths=paths, named=named)


def git_checkout_operands(words: list[str], at: int) -> list[PathWord] | None:
    """The paths ``git checkout [<ref>] -- <path>...`` writes, past its `--`.

    The spelling that names files rather than a branch: whatever stands
    before the `--` is a ref or nothing, and every word after it is a path the
    working tree takes from there. ``None`` for any other checkout -- a branch
    switch, a flag, a word that expands at run time -- which leaves the row to
    answer for it.
    """
    if posixpath.basename(words[0]) != "git" or words[at : at + 1] != ["checkout"]:
        return None
    rest = words[at + 1 :]
    if "--" not in rest:
        return None
    split = rest.index("--")
    before, paths = rest[:split], rest[split + 1 :]
    if len(before) > 1 or any(word.startswith("-") for word in before) or not paths:
        return None
    if any(opaque_argument(word) for word in [*before, *paths]):
        return None
    return [
        PathWord(at=at + 2 + split + index, prefix="", path=path)
        for index, path in enumerate(paths)
    ]


def git_apply_words(words: list[str], rows: list[ShellRuleRow]) -> list[PathWord]:
    """Which words a `git apply` reads its patch files out of.

    The words rather than the paths, because where each sits in the command is
    what resolves it: a list of paths alone cannot say which of them a `-C`
    moved, and every reader here places a path from the word that named it.

    Found past git's globals, because what the patch touches is judged from
    these: read only where `apply` is written second, `git --no-pager apply`
    would hand nobody its patch, and a protected file it rewrites would go
    unasked.
    """
    at = global_span(words, rows)
    if posixpath.basename(words[0]) != "git" or words[at : at + 1] != ["apply"]:
        return []
    return [
        PathWord(at=index, prefix="", path=word)
        for index, word in enumerate(words[at + 1 :], start=at + 1)
        if not word.startswith("-") and word != "--" and not opaque_argument(word)
    ]


def written_operands(executable: str, operands: list[str]) -> list[str]:
    """The operands a path verb modifies, as opposed to the ones it reads.

    Copying reads every source and writes only the destination, so a path
    named as a source is an ordinary read however protected it is. Linking
    reads its source the same way -- the link stands where the last operand
    does. A move writes every operand, since each source is unlinked, and
    every other verb here removes or creates each path it is given. Which is
    which is the verb's own ``lands``.
    """
    if PATH_VERBS[executable]["lands"] == "last" and len(operands) > 1:
        return operands[-1:]
    return operands


def written_targets(
    words: list[str], write_flags: Sequence[str] = ()
) -> list[str] | None:
    """Every path this line would write over, or ``None`` where none can be named.

    Three grammars answer one question. A row that names the options carrying
    its destination has already said where it writes, so that column is read
    before anything here guesses; a path verb takes paths and nothing else, so
    its operands are its targets; an archive verb states separately where it
    authors, what it consumes and which directory it unpacks into. What a
    caller wants of any of them is the same list, because what it asks of that
    list is the same question -- where the loss lands.

    The declared column wins outright rather than adding to the others: a row
    saying ``of=`` is where ``dd`` lands is also saying its remaining words are
    not paths, and reading them as operands would name ``if=/dev/zero`` as a
    write. A row that declares nothing there falls through, which is every row
    whose destination is positional.

    A flag this cannot read could move which paths are touched, so the
    positions stop meaning what they read and every operand is named instead.
    Widening is the reading a caller that *refuses* something owes, and every
    caller here is one: ``rm --interactive=never /etc/hosts`` names a path no
    capture of this checkout holds whatever the flag turns out to do, and
    declining to answer would have left the row claiming otherwise.

    ``None`` only for an unmodelled line, which leaves every caller with the
    answer it had before it asked.
    """
    if not words:
        return None
    if write_flags:
        return flag_write_targets(words, list(write_flags))
    archived = archive_write(words)
    if archived is not None:
        return archive_targets(archived)
    executable = posixpath.basename(words[0])
    if executable not in PATH_VERBS:
        return None
    verb = path_verb_operands(words)
    if not verb["inert"]:
        return verb["operands"]
    return written_operands(executable, verb["operands"])


def created_destination(
    executable: str,
    operands: list[str],
    existing_targets: list[str] | None,
    path_roles: list[PathRoleRow],
) -> str | None:
    """The operand a copy or move would bring into being, if it would.

    Both write their last operand and take the rest, so a destination nothing
    occupies yet is a creation, and creating a file destroys nothing
    — the same reason a redirection to a fresh path is written freely. That
    is what leaves ``mv`` and ``rm`` agreeing about a tracked, clean file
    instead of the move asking where the delete did not.

    Destroying nothing is only half of it, because a create also *places*
    content, and the edit gate reads every line that enters production.
    Content already in production has passed it; content in a scratch root
    never did, and arriving by rename is how it would skip it. So a source
    there withholds the grant even though the destination is empty.

    ``existing_targets`` of ``None`` means no caller established anything, so
    every destination is treated as occupied. An expansion is never resolved:
    it names a different path at run time than the one that was stat'd.
    """
    if not PATH_VERBS[executable]["creates"] or len(operands) < 2:
        return None
    destination = operands[-1]
    if existing_targets is None or destination in existing_targets:
        return None
    if opaque_argument(destination) or destination.startswith("~"):
        return None
    if path_role(destination, path_roles) != "scratch" and any(
        path_role(source, path_roles) == "scratch" for source in operands[:-1]
    ):
        return None
    return destination


def refuses_generated_plugin_target(
    word: str,
    path_roles: list[PathRoleRow] | None = None,
    checkout_root: str = "",
    displaced: list[DisplacedTargetRow] | None = None,
) -> KernelDecision | None:
    """Refuse one path that would write inside a generated plugin tree.

    Every writing form routes its targets here — a path verb's operands, a
    redirection's target — so the refusal and the reason it carries are
    written once and cannot drift between the paths that reach them.

    Scratch this checkout declares is the exception, for the reason the edit
    gate gives: nothing this project generates lands there, so a plugin tree
    under it is somebody's own — a probe kit's hand-written plugin. The word
    is read back to the checkout's own spelling before it is asked, and one
    the host found landing under another role keeps the refusal, because its
    spelling is then not where the bytes go. Without the roles nothing is
    scratch, and every plugin-shaped path is refused.
    """
    if not is_generated_plugin_target(word):
        return None
    if declared_scratch(
        repository_relative(word, checkout_root), path_roles or []
    ) and all(row["path"] != word for row in displaced or []):
        return None
    return KernelDecision(
        "deny", GENERATED_PLUGIN_REFUSAL, recovery=GENERATED_PLUGIN_RECOVERY
    )


def refuses_generated_plugin_write(
    words: list[str],
    path_roles: list[PathRoleRow] | None = None,
    checkout_root: str = "",
    displaced: list[DisplacedTargetRow] | None = None,
) -> KernelDecision | None:
    """Refuse a verb that would write inside a generated plugin tree.

    Every verb naming a path owes this, not only the ones the flag map
    models: an archive unpacked over a generated tree replaces it exactly as
    a copy would, and the regeneration that repairs it is the same one.
    """
    executable = posixpath.basename(words[0])
    archived = archive_write(words)
    if archived is not None:
        for word in archive_targets(archived):
            refused = refuses_generated_plugin_target(
                word, path_roles, checkout_root, displaced
            )
            if refused is not None:
                return refused
        return None
    if executable not in PATH_VERBS:
        return None
    verb = path_verb_operands(words)
    operands = verb["operands"]
    inert = verb["inert"]
    targets = written_operands(executable, operands) if inert else operands
    for word in targets:
        refused = refuses_generated_plugin_target(
            word, path_roles, checkout_root, displaced
        )
        if refused is not None:
            return refused
    return None


def asks_before_removing_a_directory(
    words: list[str],
    path_roles: list[PathRoleRow],
    directory_targets: list[str] | None = None,
) -> KernelDecision | None:
    """Ask before a verb takes a whole directory, and let a capture answer it.

    A delete confined to files is bounded by the files named. A directory is
    not: its size is whatever it happens to hold, and nothing in the command
    says what that is. That is the question, and it is a question rather than
    a wall -- wording that says the operation "is never granted" and then
    offers approval in the same sentence reads as a refusal and is worked
    around as one.

    Recoverable in exactly the sense a file delete is, so it settles the same
    way. Reasoning that untracked work inside a directory is restored by
    nothing holds only of `git stash create`, which is why
    :mod:`lup.devtools.dev.undo` does not use it: that module
    captures tracked content *and* untracked files, and names ``rm -rf src/``
    as the case it exists for. Carrying the purpose and the requirement lets
    the settlement layer discharge this against a capture that completed, and
    keep the question where one did not -- rather than opting out of that
    layer by returning a bare verdict, which would leave a proven capture
    unable to answer the one operation it was built for.

    What stays outside any capture is ignored content, which is not a fact
    about directories: a path rule guards `.env`, and `git clean -fdx` keeps
    its own question for being the command whose whole purpose is destroying
    what this cannot restore.

    A scratch root keeps its own grant, because there the tree is disposable
    by declaration.
    """
    executable = posixpath.basename(words[0])
    if executable not in ("rm", "mv"):
        return None
    operands = path_verb_operands(words)["operands"]
    named = [
        word
        for word in operands
        if word in (directory_targets or [])
        and path_role(word, path_roles) != "scratch"
    ]
    if not named:
        return None
    # `mv` is not a removal and saying it was is how a reader learns to
    # distrust the reason: the directory leaves the path it was at, which is
    # the fact worth stating, and it is the same fact `rm` states in stronger
    # words.
    taken = "deleting" if executable == "rm" else "moving"
    spelled = ", ".join(named)
    directory = (
        f"the whole directories {spelled}"
        if len(named) > 1
        else f"the whole directory {spelled}"
    )
    what = "they hold" if len(named) > 1 else "it holds"
    # Read off the targets rather than asserted, which is the same correction
    # :func:`verb_loss_scope` makes for the row this sits beside and for the
    # same measured reason: a directory outside the checkout is one no capture
    # of it has ever held, and a requirement stated by the rule rather than by
    # the paths settled `rm -rf /etc/ssl` as "captured and restorable".
    return KernelDecision(
        "ask",
        f"{taken} {directory}, and nothing in the command bounds what {what}",
        checkpoint=(
            "unrecoverable"
            if any(
                write_checkpoint(write_scope(target, path_roles)) == "unrecoverable"
                for target in named
            )
            else "boundary_wide"
        ),
        purpose="unrecovered_local_mutation",
    )


def protected_write_target(
    targets: list[str],
    path_rules: list[PathRuleRow],
    path_exists: bool,
    path_roles: list[PathRoleRow],
) -> KernelDecision | None:
    """Ask before granting a write to a path the declared rules protect.

    Every grant below answers "what would destroying this cost" — nothing,
    for a scratch file; a checkout, for one Git can restore. That is the
    wrong question for a file protected by who owns it rather than by what
    it would cost to rebuild, and answering it anyway would let
    ``rm sync.json`` and ``cp x README.md`` past a gate the Edit tool stops.
    The rules are the edit gate's own, so the two cannot come to disagree
    about a path.

    ``path_exists`` is the caller's own established fact, because the rule
    kinds that fire only on a path that is not there yet — a new subtree, a
    new devtools module — mean the opposite thing when it is. A grant over a
    scratch or Git-clean operand has settled that it exists; a redirection
    knows from the targets the host stat'd.
    """
    for word in targets:
        matched = next(
            (
                row
                for row in path_rules
                if path_rule_matches(word, path_exists, row)
                and not yields_to_scratch(word, row, path_roles)
            ),
            None,
        )
        if matched is not None:
            return KernelDecision(
                "ask",
                protected_path_reason(word, matched),
                recovery=matched["recovery"],
            )
    return None


def expands_to(word: str, name: str) -> bool:
    """Whether one name in a word could be this name once the shell expands it.

    A literal name is only itself. A glob reaches what it matches, except a
    leading-dot name, which the shell's expansion skips unless the glob starts
    with a dot too -- so `*` does not reach `.claude` and `.*` does.
    """
    if word == name:
        return True
    if not any(character in word for character in "*?["):
        return False
    if name.startswith(".") and not word.startswith("."):
        return False
    return fnmatchcase(name, word)


def deletes_protected(operand: str, row: PathRuleRow) -> bool:
    """Whether deleting this operand deletes a path the rule protects.

    The rule's own match, and two readings an edit never needs because an
    edit names one file: a directory holding the protected path (`rm -r .`)
    and a glob that could expand to it (`rm *.md`). Both are read only for a
    rule naming a path, since a prefix or a part names no one file to hold.
    """
    if path_rule_matches(operand, True, row):
        return True
    if row["kind"] not in ("exact", "subtree"):
        return False
    held = PurePosixPath(posixpath.normpath(row["value"])).parts
    named = PurePosixPath(posixpath.normpath(operand)).parts
    return len(named) <= len(held) and all(
        expands_to(word, name) for word, name in zip(named, held)
    )


GIT_INIT_GRAMMAR = grammar(
    valued=(
        "-b",
        "--initial-branch",
        "--template",
        "--separate-git-dir",
        "--object-format",
        "--ref-format",
    ),
    flags=("-q", "--quiet", "--bare", "--shared", "--no-template"),
)
"""The options `git init` reads, and which of them consume the next word.

`--shared` takes its permissions only attached, so it consumes nothing."""


class InitOperands(TypedDict):
    """Where one `git init` makes a repository, and whether that could be read."""

    named: list[PathWord]
    """Each directory it names: the work tree's, and a separate git dir's."""

    directories: int
    """How many operands name the work tree; none means wherever git stands."""

    read: bool
    """Whether every option is one the grammar lists and none copies files in.

    `--template` copies a directory into the new repository, which is a read
    of wherever it names, so a reading carrying one is not a plain create."""


def git_init_operands(words: list[str], at: int) -> InitOperands | None:
    """The directories one `git init` makes, or ``None`` where it is not one.

    ``at`` is where the caller reads the subcommand, as for a restore: a grant
    reads it where it is written, since a global such as `--git-dir` moves the
    repository itself, and a reader naming paths reads it past the globals.
    """
    if posixpath.basename(words[0]) != "git" or words[at : at + 1] != ["init"]:
        return None
    named: list[PathWord] = []
    directories = 0
    read = True
    literal = False
    position = at + 1
    while position < len(words):
        word = words[position]
        if literal or word == "-" or not word.startswith("-"):
            named.append(PathWord(at=position, prefix="", path=word))
            directories += 1
            position += 1
            continue
        if word == "--":
            literal = True
            position += 1
            continue
        options = read_options(word, words[position + 1 :], GIT_INIT_GRAMMAR)
        if options is None:
            read = False
            position += 1
            continue
        for option in options["options"]:
            read = read and option["name"] != "--template"
            if option["name"] == "--separate-git-dir" and option["value"]:
                attached = options["width"] == 1
                named.append(
                    PathWord(
                        at=position if attached else position + 1,
                        prefix=f"{option['name']}=" if attached else "",
                        path=option["value"],
                    )
                )
        position += options["width"]
    return InitOperands(named=named, directories=directories, read=read)


def git_init_in_scratch(
    words: list[str], path_roles: list[PathRoleRow], checkout: str = ""
) -> KernelDecision | None:
    """Recognize a `git init` whose repository is made in declared scratch.

    A repository made there is as disposable as the scratch holding it, and a
    project scaffolded under `tmp/` needs one. Every directory it makes has
    to be named and sit under a scratch root this checkout declares: a work
    tree left to wherever git stands is this checkout's own, and a separate
    git dir anywhere else would move the repository out from under it. The
    words are the placed ones, so a `cd` or `git -C` is already in them, and
    a link a directory crosses is the host's to resolve like any write's.
    """
    reading = git_init_operands(words, 1)
    if reading is None or not reading["read"] or reading["directories"] != 1:
        return None
    places = [named["path"] for named in reading["named"]]
    if any(opaque_argument(place) for place in places):
        return None
    if not all(
        declared_scratch(repository_relative(place, checkout), path_roles)
        for place in places
    ):
        return None
    return KernelDecision(
        "allow", "a repository made in scratch is as disposable as the scratch"
    )


def git_rm_operands(
    words: list[str], rows: list[ShellRuleRow]
) -> list[PathWord] | None:
    """The pathspecs a `git rm` removes, or ``None`` where it removes nothing.

    A dry run removes nothing. `--cached` is read like any other flag: it
    leaves the working copy, but the next commit deletes the file from the
    project, which is the change a protected file's owner is asked about.

    Found past git's globals: `git --no-pager rm README.md` and `git -c
    color.ui=false rm README.md` remove what `git rm README.md` removes, and
    read only where `rm` is written second they would reach the capture
    instead. The
    words rather than the paths, so the segment reading places each from the
    directory a `cd` or `git -C` left -- `cd src && git rm ../README.md` is
    the same removal.
    """
    at = global_span(words, rows)
    if posixpath.basename(words[0]) != "git" or words[at : at + 1] != ["rm"]:
        return None
    operands: list[PathWord] = []
    literal = False
    for index, word in enumerate(words[at + 1 :], start=at + 1):
        if literal or not word.startswith("-"):
            operands.append(PathWord(at=index, prefix="", path=word))
            continue
        if word in ("-n", "--dry-run"):
            return None
        literal = word == "--"
    return operands


def deleted_operands(words: list[str], rows: list[ShellRuleRow]) -> list[str]:
    """What `rm` or `git rm` deletes, and nothing for any other command."""
    match posixpath.basename(words[0]):
        case "git":
            return [named["path"] for named in git_rm_operands(words, rows) or []]
        case "rm":
            return path_verb_operands(words)["operands"]
        case _:
            return []


def protected_deletion(
    words: list[str],
    path_rules: list[PathRuleRow],
    rows: list[ShellRuleRow],
    path_roles: list[PathRoleRow],
    checkout: str = "",
) -> KernelDecision | None:
    """Ask before `rm` or `git rm` deletes a path the declared rules protect.

    Both rows answer what a delete costs, and a capture of the checkout
    settles that -- which is the wrong question about a file protected by
    whose it is. The recoverable-roots grant already defers to ownership, but
    only on the path it grants, so a protected file beside an operand it did
    not cover, under a directory, behind a glob, or named through `git rm`
    reached the capture instead. Read here, before any grant, for every
    operand, from the spelling the rules are anchored at.
    """
    for operand in deleted_operands(words, rows):
        spelled = repository_relative(operand, checkout)
        matched = next(
            (
                row
                for row in path_rules
                if deletes_protected(spelled, row)
                and not yields_to_scratch(spelled, row, path_roles)
            ),
            None,
        )
        if matched is None:
            continue
        reason = (
            protected_path_reason(operand, matched)
            if path_rule_matches(spelled, True, matched)
            else f"{operand} would delete {matched['value']}: {matched['reason']}"
        )
        return KernelDecision("ask", reason, recovery=matched["recovery"])
    return None


def protected_placement(
    words: list[str],
    path_rules: list[PathRuleRow],
    rows: list[ShellRuleRow],
    path_roles: list[PathRoleRow],
    existing: list[str] | None = None,
    checkout: str = "",
) -> KernelDecision | None:
    """Ask before a command writes, moves or links onto a path the rules protect.

    :func:`protected_deletion` asks this of `rm`, and the recoverable-roots
    grant asks it of a path Git could restore. A path nothing stood at yet
    reached neither: it went to the verb's own row, and a capture of the
    checkout then settled `cp evil.yml .github/workflows/ci.yml` and `mv x
    .claude/settings.local.json` as restorable, while the same file written
    through `Edit` or a redirection asked. Whether a file may be written there
    is about whose it is, and a file created there is written as surely as one
    replaced.

    Three readings, each from the spelling the rules are anchored at. Every
    path the line writes, as :func:`written_targets` names them -- a path
    verb's destination, an archive's, a declared write flag's -- matched as
    the edit gate matches a path. Every source `mv` takes away, read as a
    delete is, so moving a directory that holds a protected file asks. And
    where each source of a copy, a move or a link lands under a destination
    that is a directory, read the same way: `cp -r /tmp/.claude .` writes
    `.claude` though no word spells it.

    `rm` is :func:`protected_deletion`'s, which words its question as the
    delete it is.
    """
    executable = posixpath.basename(words[0])
    if executable == "rm":
        return None
    write_flags = next(
        (
            row["write_flags"]
            for row in rows
            if row["command"] == executable and not row["subcommand"]
        ),
        [],
    )
    for word in written_targets(words, write_flags) or []:
        spelled = repository_relative(word, checkout)
        present = existing is None or word in existing
        matched = next(
            (
                row
                for row in path_rules
                if path_rule_matches(spelled, present, row)
                and not yields_to_scratch(spelled, row, path_roles)
            ),
            None,
        )
        if matched is not None:
            return KernelDecision(
                "ask",
                protected_path_reason(posixpath.normpath(word), matched),
                recovery=matched["recovery"],
            )
    lands = PATH_VERBS[executable]["lands"] if executable in PATH_VERBS else "each"
    operands = path_verb_operands(words)["operands"] if lands != "each" else []
    sources = operands[:-1]
    reached = [
        *[(source, "move") for source in sources if lands == "moved"],
        *[
            (
                posixpath.join(
                    operands[-1], posixpath.basename(posixpath.normpath(source))
                ),
                "replace",
            )
            for source in sources
        ],
    ]
    for word, verb in reached:
        spelled = repository_relative(word, checkout)
        matched = next(
            (
                row
                for row in path_rules
                if deletes_protected(spelled, row)
                and not yields_to_scratch(spelled, row, path_roles)
            ),
            None,
        )
        if matched is None:
            continue
        shown = posixpath.normpath(word)
        reason = (
            protected_path_reason(shown, matched)
            if path_rule_matches(spelled, True, matched)
            else f"{shown} would {verb} {matched['value']}: {matched['reason']}"
        )
        return KernelDecision("ask", reason, recovery=matched["recovery"])
    return None


def confined_to_recoverable_roots(
    words: list[str],
    path_roles: list[PathRoleRow],
    recoverable_targets: list[str] | None = None,
    recoverable_target_limit: int = 5,
    path_rules: list[PathRuleRow] | None = None,
    existing_targets: list[str] | None = None,
) -> KernelDecision | None:
    """Recognize a path-taking judged-ask verb whose every target is disposable.

    A scratch role names a tree of disposable files, so destroying one is as
    safe as writing it and creating one there settles nothing. A path the host
    reports as recoverable is committed with no uncommitted change, so
    destroying it costs a checkout rather than any information — the host
    establishes that, because the kernel never reads the filesystem. Only the
    operands the verb writes are judged, so copying any source into a scratch
    root is as disposable as the root it lands in. A long flag, an opaque
    word, or a single written target outside those roots falls through to the
    verb's ask, so a mixed command still asks.

    The two grants are bounded differently because what backs them differs. A
    scratch root is disposable by declaration, so emptying one is one act
    whatever it holds. Restoring committed work is instead a repair somebody
    has to know to perform, so that grant is capped: past the limit a delete
    is a sweep, and a sweep is worth a question even when every file in it
    could be brought back.

    A destination nothing occupies yet is neither: it is brought into being,
    and the cap does not reach it because there is nothing there to restore
    — provided the content arriving there already lives in production, where
    the edit gate has read it.

    All three readings are about a path this checkout answers for, so a target
    beyond it gives the line back to the row rather than taking any of them:
    see :func:`written_beyond_the_checkout`.

    A verb that can reach another machine takes none of them: its entry names
    where a local destination is, and nothing here can say where a remote one
    lands.
    """
    executable = posixpath.basename(words[0])
    if executable not in PATH_VERBS or PATH_VERBS[executable]["remote"]:
        return None
    verb = path_verb_operands(words)
    operands = verb["operands"]
    inert = verb["inert"]
    if not inert or not operands:
        return None
    targets = written_operands(executable, operands)
    if written_beyond_the_checkout(targets, path_roles):
        return None
    created = created_destination(executable, operands, existing_targets, path_roles)
    disposable = [word for word in targets if path_role(word, path_roles) == "scratch"]
    restorable = [
        word
        for word in targets
        if word not in disposable and word in (recoverable_targets or [])
    ]
    fresh = [word for word in targets if word == created and word not in disposable]
    if len(disposable) + len(restorable) + len(fresh) != len(targets):
        return None
    if len(restorable) > recoverable_target_limit:
        return None
    protected = protected_write_target(targets, path_rules or [], True, path_roles)
    if protected is not None:
        return protected
    return KernelDecision("allow", "confined to recoverable roots")


def archive_lands_on_nothing(
    words: list[str],
    path_roles: list[PathRoleRow],
    recoverable_targets: list[str] | None = None,
    path_rules: list[PathRuleRow] | None = None,
    existing_targets: list[str] | None = None,
    empty_directories: list[str] | None = None,
) -> KernelDecision | None:
    """Grant an archive or compression verb that would replace nothing.

    These verbs ask because they place content over whatever stands there.
    Where nothing stands there, the ask is answering a question that has no
    second side: unpacking into a directory that does not exist, or into one
    holding nothing, destroys nothing, and neither does authoring an archive
    at a path nothing occupies.

    A named file is judged exactly as a delete's operand is — disposable by
    role, restorable from Git, or not yet there — because the cost of
    replacing it is the same cost whichever verb does the replacing. That is
    what leaves ``gzip`` and ``rm`` agreeing about a committed, unmodified
    file instead of one asking where the other does not.

    A destination directory cannot be judged that way, because what would
    land in it comes from the archive rather than from the command. So it is
    judged by being empty: nothing there is nothing to replace, whatever the
    archive turns out to hold. Everything the extraction then writes is new,
    and a later edit to any of it passes the edit gate on its own path.

    Both readings are about a path this checkout answers for, so a target
    beyond it gives the line back to the row rather than taking either: see
    :func:`written_beyond_the_checkout`.

    ``None`` wherever the answer is not established — an unmodelled line, an
    expansion that names a different path at run time, a caller that resolved
    no filesystem facts — and ``None`` leaves the verb's own ask standing.
    """
    write = archive_write(words)
    if write is None:
        return None
    authored, consumed = write["authored"], write["consumed"]
    directory = write["directory"]
    named = archive_targets(write)
    # An extraction that named no destination unpacks where it stands, which
    # is the repository itself — certain to be occupied, and the one place an
    # unread archive should never land without a question.
    if not named:
        return None
    if any(opaque_argument(word) or word.startswith("~") for word in named):
        return None
    if written_beyond_the_checkout(named, path_roles):
        return None
    for word in authored:
        if path_role(word, path_roles) == "scratch":
            continue
        if existing_targets is not None and word not in existing_targets:
            continue
        if word in (recoverable_targets or []):
            continue
        return None
    # A consumed path is destroyed rather than created, so being absent is
    # not the licence it is above: it is a fact the host could not establish,
    # and for a destructive verb an unestablished fact is a question.
    for word in consumed:
        if path_role(word, path_roles) == "scratch":
            continue
        if word in (recoverable_targets or []):
            continue
        return None
    if directory is not None and path_role(directory, path_roles) != "scratch":
        if existing_targets is None:
            return None
        if directory in existing_targets and directory not in (empty_directories or []):
            return None
    protected = protected_write_target(named, path_rules or [], True, path_roles)
    if protected is not None:
        return protected
    return KernelDecision("allow", "archive lands where nothing stands")


def dangerous_env_name(name: str) -> bool:
    """Recognize an environment variable that can redirect a command's execution."""
    return name in DANGEROUS_ENV_NAMES or any(
        name.startswith(prefix) for prefix in DANGEROUS_ENV_PREFIXES
    )


def dangerous_assignment_reason(verb: str, names: list[str]) -> str:
    """The question a security-sensitive binding earns, naming every name it binds.

    The variable is the fact the classifier matched on, so the reason says it:
    a reviewer reads this sentence and nothing else, and "an environment
    assignment" would tell them only that some sentence could be printed for
    any call. Every name is listed rather than the first, since approving is
    one decision over the whole segment.
    """
    spelled = ", ".join(names)
    variables = f"variables {spelled}" if len(names) > 1 else f"variable {spelled}"
    return (
        f"{verb} the security-sensitive {variables}, which can change how commands run"
    )


def binding_reach(names: list[str]) -> Reach:
    """Where binding these variables reaches, as the question about them weighs it.

    Most of them redirect what runs -- a search path, a preloaded library, an
    interpreter's module path -- and what runs is this process and its
    children, which a container holds. A few swap who a later command acts as
    or where it acts: the repository git touches, the token gh sends, the
    agent ssh asks. Those are the lent credentials, whichever wall stands --
    and so is a name nobody can read, which might be any of them.
    """
    if any(
        not name.isidentifier() or name.startswith(prefix)
        for name in names
        for prefix in OUTWARD_ENV_PREFIXES
    ):
        return "credential"
    return "container"


def bound_names(arguments: list[str]) -> list[str]:
    """The variable names an `export`-like builtin's arguments bind or unbind.

    Options are skipped and a `NAME=value` word is read for its name, which is
    all these builtins take -- so the names are exactly what a later command
    sees changed.
    """
    return [word.partition("=")[0] for word in arguments if not word.startswith("-")]


def dangerous_assignment(verb: str, names: list[str]) -> KernelDecision:
    """The question a security-sensitive binding earns, and where its harm lands."""
    return KernelDecision(
        "ask", dangerous_assignment_reason(verb, names), reach=binding_reach(names)
    )


def flag_matches(word: str, flags: list[str]) -> bool:
    """Match a word against a rule's ask-flags, allowing clusters and ``=`` forms."""
    for flag in flags:
        if word == flag:
            return True
        if flag.startswith("--") and word.startswith(flag + "="):
            return True
        if (
            len(flag) == 2
            and flag.startswith("-")
            and word.startswith("-")
            and not word.startswith("--")
            and flag[1] in word[1:]
        ):
            return True
    return False


class CarriedSetting(TypedDict):
    """The setting a guarded global names, and how many words it took to say it.

    Both halves, because the reader needs both: the value decides whether the
    global is one worth interrupting about, and the count is how far the parse
    advances past it. Deriving the second from the first would be re-doing the
    spelling test that produced it.
    """

    value: str
    words: int


def carried_setting(
    word: str, flags: list[str], following: list[str]
) -> CarriedSetting:
    """The ``key=value`` a settings global carries, in the spellings that read.

    Two of them, and deliberately not the third. A long option carries its
    value after an ``=`` (``--config-env=core.pager=VAR``), and any of them
    carries it in the next word (``git -c core.pager=x``). A short option with
    the value pressed against it — ``git -ccore.pager=x`` — reads as neither:
    the ``=`` in that word separates the *setting's* value rather than the
    flag's, so splitting on it yields ``x``, and a guard reading that would
    compare a value against a list of keys.

    Nothing found is an empty value, which a caller reads as "this is not a
    shape I can judge" and answers as it would have answered without this. The
    failure worth avoiding is the other direction, where a spelling read
    wrongly makes a guarded key look unguarded.
    """
    for flag in flags:
        if flag.startswith("--") and word.startswith(flag + "="):
            return CarriedSetting(value=word[len(flag) + 1 :], words=1)
        if word == flag:
            return CarriedSetting(
                value=following[0] if following else "", words=2 if following else 1
            )
    return CarriedSetting(value="", words=1)


def opaque_argument(word: str) -> bool:
    """A word whose runtime expansion could inject a guarded flag.

    A substitution sentinel anywhere in the word marks it: the substitution's
    output word-splits at expansion, so even a mid-word result can become new
    words. A :class:`~lup.policy.kernel.syntax.VerbatimText` expands into
    nothing, so `'$x'` is the two characters it spells.
    """
    if isinstance(word, VerbatimText):
        return False
    if word.startswith("$") or SUBSTITUTION_SENTINEL in word:
        return True
    return "}" in word and ("{-" in word or ",-" in word)


def unread_prefix(word: str) -> str | None:
    """What a word spells before an expansion takes the rest, or ``None``.

    ``None`` is a word read whole, which is every word nothing in expands. A
    ``$`` opens a parameter, and a command substitution is spliced in as a
    sentinel opening with one; a backtick opens the older spelling of the
    same. What follows is unknown rather than one more piece of this word,
    because an unquoted result splits into further words at run time. A
    :class:`~lup.policy.kernel.syntax.VerbatimText` is read whole: the `$` its
    quotes held is the character it spells, so `rg -e'foo$'` names no flag.
    """
    if isinstance(word, VerbatimText):
        return None
    opened = next(
        (index for index, character in enumerate(word) if character in "$`"), None
    )
    return None if opened is None else word[:opened]


def unread_flags(word: str, flags: list[str]) -> list[str]:
    """The guarded flags a word nobody can read whole could turn out to be.

    Only a word whose legible part already spells a flag: ``--ret$X`` could
    be ``--retire`` and ``-$X`` any of them. A short flag is reached from any
    single-dash prefix, because the expansion can finish a cluster the way
    :func:`flag_matches` reads one -- ``-a$X`` can become ``-af``. A word
    opening on its expansion is :func:`opaque_argument`'s, and names no flag
    here: it could as well be the path or the ref beside it.
    """
    prefix = unread_prefix(word)
    if prefix is None or not prefix.startswith("-"):
        return []
    return [
        flag
        for flag in flags
        if flag.startswith(prefix)
        or (len(flag) == 2 and flag.startswith("-") and not prefix.startswith("--"))
    ]


def key_matches(word: str, patterns: list[str]) -> bool:
    """Match a setting name against a rule's guarded-key globs, case-blind.

    Lowercased on both sides because git resolves a configuration key's
    section and name without regard to case: `core.hooksPath`,
    `CORE.HOOKSPATH` and `Core.hooksPath` are one key, and a guard comparing
    literally would catch the spelling in the pattern and no other. A
    subsection is the one case-sensitive part, and the patterns that reach
    into one match it with `*` rather than by naming it, so nothing is lost
    by folding it too.

    Globbing rather than prefix-matching because the shapes worth guarding
    sit around a subsection the caller chooses -- `merge.<name>.driver` names
    a program, and the name is theirs. `fnmatchcase` lets `*` cross a `.`,
    which is right here: a subsection may contain dots, so `merge.*.driver`
    should still answer for `merge.a.b.driver`.

    A `key=value` word answers to the key's own pattern, the way
    ``flag_matches`` reads `--flag=value`. Some settings arrive joined --
    `git -c core.pager=x` is the shape -- and a guard reading only the whole
    word would compare `core.pager=x` against `core.pager`, find no match,
    and wave through the one spelling that carries its value with it.
    """
    folded = word.lower()
    return any(
        fnmatchcase(folded, pattern) or fnmatchcase(folded, f"{pattern}=*")
        for pattern in patterns
    )


HELP_UNSAFE = set("/=$*?~<>|&;`\\'\" \t\n")


def is_help_probe(arguments: list[str], unsafe: set[str] = HELP_UNSAFE) -> bool:
    """Recognize an invocation that only prints usage.

    ``--help`` is inert wherever it sits among plain subcommand words, so
    an unclassified command is still readable through it. Bare ``-h`` counts
    only when it stands alone, because several commands spend it on a value
    (``mysql -h host``) rather than on help.
    """
    if not arguments:
        return False
    if any(character in unsafe for word in arguments for character in word):
        return False
    if arguments == ["-h"]:
        return True
    return "--help" in arguments


XARGS_GRAMMAR = grammar(
    valued=(
        "-a",
        "--arg-file",
        "-d",
        "--delimiter",
        "-E",
        "-I",
        "-L",
        "--max-lines",
        "-n",
        "--max-args",
        "-P",
        "--max-procs",
        "--process-slot-var",
        "-s",
        "--max-chars",
    ),
    flags=(
        "-0",
        "--null",
        "-o",
        "--open-tty",
        "-p",
        "--interactive",
        "-r",
        "--no-run-if-empty",
        "--show-limits",
        "-t",
        "--verbose",
        "-x",
        "--exit",
        "--eof",
        "--replace",
        "--help",
        "--version",
    ),
    attached=("-e", "-i", "-l"),
)
"""How xargs spells the options that stand before the command it runs.

`-e`, `-i` and `-l` take a value only pressed against them, and their long
forms only after an ``=``, so the word after a bare one is the command; the
rest of the valued ones take the next word when nothing is attached."""


def xargs_payload(words: list[str]) -> list[str] | None:
    """The command xargs would run, or ``None`` where its options are unread.

    Read by xargs's own grammar, clusters included (`-rn 1`), because a
    fixed table of which words to skip misplaces the command both ways: it
    takes `-i` and `-e` to consume the next word, which is the command, and
    `--max-procs 4` and `-rn 1` to consume nothing, so `4` and `1` are judged
    as the command. An option the grammar does not list could
    consume the next word, so it leaves the command unread rather than
    guessed at, the way an interpreter's does.
    """
    position = 1
    while position < len(words):
        word = words[position]
        if word == "--":
            return words[position + 1 :]
        if word == "-" or not word.startswith("-"):
            return words[position:]
        read = read_options(word, words[position + 1 :], XARGS_GRAMMAR)
        if read is None:
            return None
        position += read["width"]
    return []


# lup: ignore[library-default] — each wrapper's own option spellings, which
# no project could choose differently and still read the command it wraps
WRAPPER_GRAMMARS: dict[str, InterpreterGrammar] = {
    "env": grammar(
        valued=(
            "-a",
            "--argv0",
            "-u",
            "--unset",
            "-C",
            "--chdir",
            "-S",
            "--split-string",
        ),
        flags=(
            "-i",
            "--ignore-environment",
            "-0",
            "--null",
            "-v",
            "--debug",
            "--block-signal",
            "--default-signal",
            "--ignore-signal",
            "--list-signal-handling",
            "--help",
            "--version",
        ),
    ),
    "stdbuf": grammar(
        valued=("-i", "--input", "-o", "--output", "-e", "--error"),
        flags=("--help", "--version"),
    ),
    "time": grammar(
        valued=("-o", "--output", "-f", "--format"),
        flags=(
            "-a",
            "--append",
            "-p",
            "--portability",
            "-q",
            "--quiet",
            "-v",
            "--verbose",
            "-V",
            "--version",
            "--help",
        ),
    ),
    "exec": grammar(valued=("-a",), flags=("-c", "-l")),
    "setsid": grammar(
        flags=(
            "-c",
            "--ctty",
            "-f",
            "--fork",
            "-w",
            "--wait",
            "-h",
            "--help",
            "-V",
            "--version",
        )
    ),
    "command": grammar(flags=("-p", "-v", "-V")),
    "nohup": grammar(flags=("--help", "--version")),
    "timeout": grammar(
        valued=("-k", "--kill-after", "-s", "--signal"),
        flags=(
            "-f",
            "--foreground",
            "-p",
            "--preserve-status",
            "-v",
            "--verbose",
            "--help",
            "--version",
        ),
    ),
}
"""Each wrapper's own options, as its `--help` lists them.

Per wrapper because the grammars differ and a single list would consume a
word one of them does not take, and read by the interpreters' option reader
so a cluster is read the way the wrapper reads it: `env -iu NAME cmd` unsets
`NAME`, `exec -cla name cmd` names the process, and `timeout -vk 5 10 cmd`
kills after 5, where a word at a time each made the value the command. An
option the grammar does not list is left where it stands, so the segment
reaches a command word beginning with `-`, which
:func:`lup.policy.kernel.shell.decide_segment_words` refuses rather than
classifies.
"""

# lup: ignore[library-default] — what each wrapper's own option does, which no
# project could choose differently and still read the command it wraps
WRAPPER_JUDGED_OPTIONS: dict[str, tuple[str, ...]] = {
    "time": ("-o", "--output"),
    "env": ("-C", "--chdir"),
}
"""The wrapper options that act on their own, so the wrapper is not stepped over.

A wrapper is transparent only while its options change nothing a reading of
the wrapped command would judge. `time -o <file>` writes its report into the
file, and stepped over it would be the command it timed: `time -o
README.md ls` read as `ls`. `env -C <dir>` moves where every operand resolves,
and stepped over, `env -C /etc rm hosts` would remove `hosts` here. A wrapper
carrying one of these is the segment's command itself, and its own reader
judges what the option does beside what it wraps.
"""

# lup: ignore[library-default] — `env`'s own spelling of the option that
# re-splits its remainder, which is the command's and not a choice
ENV_SPLIT_STRING = ("-S", "--split-string")
"""The `env` options whose operand is a command line this reading cannot split.

`env -S` applies its own quoting rules to the rest of the line, so the words
here are not the words that will run. Named rather than skipped, because a
skip would hand the next word on as though it were the command.
"""


class WrapperReading(TypedDict):
    """One wrapper's options as its grammar reads them, and what follows."""

    options: list[ReadOption]
    payload: int
    """Where the wrapped command starts, or where an unread option stands."""


def read_wrapper(segment: list[str], position: int, wrapper: str) -> WrapperReading:
    """Read one wrapper's own options up to the command it wraps.

    A valued option consumes the next word when it stands alone and nothing
    when its value is attached (``-oL``, ``--unset=NAME``), and a cluster is
    read letter by letter. Where an option is unread, ``payload`` is that
    option's own position, so what the caller takes for the command begins
    with a dash and is refused rather than read one word off.
    """
    rules = WRAPPER_GRAMMARS[wrapper]
    options: list[ReadOption] = []
    while position < len(segment):
        word = segment[position]
        if word == "--":
            return WrapperReading(options=options, payload=position + 1)
        if word == "-" or not word.startswith("-"):
            return WrapperReading(options=options, payload=position)
        read = read_options(word, segment[position + 1 :], rules)
        if read is None:
            return WrapperReading(options=options, payload=position)
        options.extend(read["options"])
        position += read["width"]
    return WrapperReading(options=options, payload=position)


def env_payload(words: list[str]) -> list[str] | None:
    """The command `env` would run, or ``None`` where that cannot be read.

    Three answers rather than two, and the third is the point. A command comes
    back to be judged; an empty list is an `env` that only reports its
    environment; and ``None`` is this reading saying it does not know what will
    run, which a caller has to treat as unjudged rather than as nothing to
    judge. Collapsing the last two is how `env -i <interpreter> <script>` came
    to be read as a command named `-i` and allowed inside the boundary.

    `-S` is found among the options env reads, however it is spelled: matched
    only as a word of its own, `env -S'python3 -c 1' ls` was stepped over as
    an inert option and judged as `ls`.
    """
    reading = read_wrapper(words, 1, "env")
    if any(option["name"] in ENV_SPLIT_STRING for option in reading["options"]):
        return None
    rest = words[reading["payload"] :]
    assigned = 0
    while assigned < len(rest):
        name = rest[assigned].split("=", 1)
        if len(name) != 2 or not name[0].isidentifier():
            break
        assigned += 1
    # Dropped rather than carried, because the assignments are judged where
    # they sit: `effective_command` collects the dangerous ones, and this
    # answers the separate question of which program they are set for.
    return rest[assigned:]


def refspec_effects(word: str) -> list[str]:
    """What one ``git push`` operand does to the ref it names.

    A refspec spells the same two effects the push flags spell. `--delete`
    removes a remote ref and `:<dst>` removes the same ref by giving it no
    source; `--force` replaces one non-fast-forward and `+<src>:<dst>`
    replaces the same ref by prefixing it. A guard written as a list of flag
    spellings therefore holds only half of each effect, and would let
    `git push origin :refs/heads/main` past a table that asks about
    `git push --delete origin main`.

    Read structurally rather than matched against a second list of spellings,
    because a list of spellings misses these. The grammar is small
    and total: `^` opens a negative refspec, which excludes rather than
    writes; a leading `+` forces; and an empty source — everything before the
    first colon — deletes, which `startswith(":")` is the whole of after the
    plus is taken off.

    Every operand is read, the repository among them, because reading only
    the ones past it would need to know where it stopped, and a repository
    that looked like a refspec would cost a question rather than miss one.
    An scp-style remote (`git@host:repo.git`) names a non-empty source and so
    reads as neither effect.
    """
    if word.startswith("^"):
        return []
    forced = word.startswith("+")
    source = word[1:] if forced else word
    effects = ["force"] if forced else []
    return [*effects, "delete"] if source.startswith(":") else effects


def refspec_destination(word: str) -> str:
    """The branch one ``git push`` refspec updates, or ``""`` where it names none.

    The destination is what follows the colon, and without one it is the
    source's own name -- `main` pushes `main` to `main`. Git resolves a short
    destination against `refs/heads/` first, so `refs/heads/main`,
    `heads/main` and `main` are one branch and read as one name here; a ref
    under another namespace (`refs/tags/v1`) keeps its full spelling, which no
    branch name matches.

    Named-nothing is its own answer rather than a guess. `HEAD` and `@` push
    whichever branch the checkout stands on, and a glob pushes whichever
    branches match it, so neither says which branch it rewrites -- and a
    reader that must not run git cannot find out.
    """
    source, colon, destination = word.removeprefix("+").partition(":")
    named = destination if colon else source
    if named in ("", "HEAD", "@") or "*" in named:
        return ""
    for prefix in ("refs/heads/", "heads/"):
        if named.startswith(prefix):
            return named.removeprefix(prefix)
    return named


def operand_words(arguments: list[str], value_flags: list[str]) -> list[str]:
    """The words of a command line that are operands rather than options.

    A flag declared as taking the next word as its value takes that word with
    it, which is what stops `git push -o ci.skip origin` reading `ci.skip` as
    the repository and `origin` as the refspec. Everything after `--` is an
    operand however it is spelled.
    """
    operands: list[str] = []
    consumed = False
    for position, word in enumerate(arguments):
        if consumed:
            consumed = False
            continue
        if word == "--":
            return [*operands, *arguments[position + 1 :]]
        if word.startswith("-"):
            consumed = word in value_flags
            continue
        operands.append(word)
    return operands


def destination_form(word: str) -> str:
    """How one ``git push`` operand names the repository it lands in.

    The destination is the first operand a push takes, and git accepts two
    kinds of word for it: the name of a remote this repository has configured,
    or the repository itself spelled out — a URL in any of git's transports,
    or a path to a checkout on this machine. The first is a destination
    somebody put in the remote table; the second names one inline and reaches
    it without the table having heard of it, which is why the two are told
    apart here at all.

    Read structurally, and that is the whole of what this can do. Whether a
    bare word is a remote this repository holds is a question about the
    repository rather than about the word, and answering it means running
    `git remote` — which this kernel is stdlib-only and hermetic in order not
    to do. So the test is the other half: a word carrying a transport is a
    repository named inline whatever the remote table says, and a bare name
    that is not in the table makes git fail before it reaches anything.

    The url form is every transport in one test, because they agree on where
    the colon falls: `https://host/p`, `ssh://host/p`, `git://host/p`,
    `file:///p` and the scp-style `git@host:p` all put a colon in the word
    with no slash before it, and no remote name may contain a colon at all.
    The path form is what is left that is not a bare name: a slash anywhere,
    or a leading `.` or `~` for the checkout beside this one.

    Returns the empty string for a bare name, which is the operand a push
    normally carries and the reading that leaves `git push origin main` alone.
    """
    if ":" in word and "/" not in word.partition(":")[0]:
        return "url"
    if "/" in word or word.startswith((".", "~")):
        return "path"
    return ""


# lup: ignore[constant-declaration] — sed's own short flags, spelled as sed does
SED_SAFE_SHORT_FLAGS = "nErsuz"
# lup: ignore[library-default] — sed's own long spellings of the short flags above
SED_SAFE_LONG_OPTIONS = (
    "--quiet",
    "--silent",
    "--regexp-extended",
    "--separate",
    "--null-data",
)
# lup: ignore[constant-declaration] — the flag characters sed's own `s///` takes
SED_SUBSTITUTE_FLAG_CHARS = "0123456789gpiImM"


def scan_sed_delimited(script: str, position: int, parts: int) -> int | None:
    """Scan ``parts`` sections after the delimiter at ``position``.

    The delimiter is whatever character sits at ``position``; backslash
    escapes are honored inside sections.
    """
    if position >= len(script):
        return None
    delimiter = script[position]
    if delimiter.isalnum() or delimiter in " \t\n;\\":
        return None
    cursor = position + 1
    seen = 0
    while cursor < len(script) and seen < parts:
        character = script[cursor]
        if character == "\\":
            cursor += 2
            continue
        if character == delimiter:
            seen += 1
        cursor += 1
    return cursor if seen == parts else None


def scan_sed_address(script: str, position: int) -> int | None:
    """Scan one address: a line-number form, ``$``, or a regex form."""
    character = script[position]
    if character == "$":
        return position + 1
    if character.isdigit():
        cursor = position + 1
        while cursor < len(script) and script[cursor].isdigit():
            cursor += 1
        if cursor < len(script) and script[cursor] == "~":
            cursor += 1
            while cursor < len(script) and script[cursor].isdigit():
                cursor += 1
        return cursor
    if character == "+":
        cursor = position + 1
        while cursor < len(script) and script[cursor].isdigit():
            cursor += 1
        return cursor if cursor > position + 1 else None
    end = (
        scan_sed_delimited(script, position, 1)
        if character == "/"
        else scan_sed_delimited(script, position + 1, 1)
        if character == "\\" and position + 1 < len(script)
        else None
    )
    if end is None:
        return None
    while end < len(script) and script[end] in "IM":
        end += 1
    return end


def scan_sed_command(script: str, position: int) -> int | None:
    """Scan one address-guarded command, returning the position after it.

    Accepted commands read the input and write standard output only: print,
    delete, hold-space, branching, labels, blocks, line numbering, text
    insertion, file reading, transliteration, and flag-screened substitution.
    The write and execute forms (``w``, ``W``, ``e``, ``s///e``, ``s///w``)
    fall out as unrecognized trailing characters.
    """
    length = len(script)
    address = scan_sed_address(script, position)
    if address is not None:
        position = address
        while position < length and script[position] in " \t":
            position += 1
        if position < length and script[position] == ",":
            position += 1
            while position < length and script[position] in " \t":
                position += 1
            if position >= length:
                return None
            second = scan_sed_address(script, position)
            if second is None:
                return None
            position = second
    while position < length and script[position] in " \t!":
        position += 1
    if position >= length:
        return None
    command = script[position]
    if command in "pPdDnNgGhHxz=F{}":
        return position + 1
    if command in "qQl":
        cursor = position + 1
        while cursor < length and (script[cursor].isdigit() or script[cursor] == " "):
            cursor += 1
        return cursor
    if command in "btT:":
        cursor = position + 1
        while cursor < length and script[cursor] in " \t":
            cursor += 1
        while cursor < length and (script[cursor].isalnum() or script[cursor] == "_"):
            cursor += 1
        return cursor
    if command in "aicrR":
        newline = script.find("\n", position)
        return length if newline == -1 else newline
    if command == "s":
        end = scan_sed_delimited(script, position + 1, 2)
        if end is None:
            return None
        while end < length and script[end] in SED_SUBSTITUTE_FLAG_CHARS:
            end += 1
        return end
    if command == "y":
        return scan_sed_delimited(script, position + 1, 2)
    return None


def safe_sed_script(script: str) -> bool:
    """Screen a sed script for side effects: whether it only reads and prints."""
    length = len(script)
    position = 0
    while position < length:
        if script[position] in " \t\n;":
            position += 1
            continue
        end = scan_sed_command(script, position)
        if end is None:
            return False
        position = end
    return True


class SedInvocation(TypedDict):
    """One `sed` call read the way sed reads it: scripts, files, and mode.

    Parsed here rather than inside the classifier so that both readers of an
    in-place rewrite work from one answer. The classifier asks what this would
    do to each named file; the host runs these same screened scripts over
    those same files to produce the documents that question is answered from.
    A second parse would be a second answer to "which files does this
    rewrite", and the two would part company the first time one of them
    learned a flag the other had not.
    """

    scripts: list[str]
    scripted: list[int]
    """Where each script was read from: the word holding it, so a reader asking
    which words name a file can leave the program out."""

    options: list[str]
    backup: str
    targets: list[str]
    named: list[PathWord]
    """The same files with the word each was read from, for a caller that has
    to put one back after resolving it against the rewrite's directory."""

    in_place: bool
    screened: bool
    """Whether every script only reads its input and writes standard output.

    ``--sandbox`` makes sed itself refuse the write and execute commands, so a
    call carrying it is screened by sed rather than by the grammar above, and
    reads as screened here without the grammar having to accept it.
    """


def sed_invocation(words: list[str]) -> SedInvocation | KernelDecision:
    """Read one sed call's flags into its scripts, its files, and its mode.

    A refusal rather than an invocation wherever the reading itself fails: a
    script this cannot see is a script nothing screens, and an option nothing
    classified is a mode nobody has decided about. Both are stronger than
    "no files", which is why they come back as decisions instead of as an
    empty parse a caller could mistake for a harmless call.
    """
    scripts: list[str] = []
    scripted: list[int] = []
    options: list[str] = []
    backup = ""
    positional: list[PathWord] = []
    script_expected = False
    script_from_options = False
    sandbox = False
    in_place = False
    end_options = False
    for index, word in enumerate(words[1:], start=1):
        if script_expected:
            scripts.append(word)
            scripted.append(index)
            script_expected = False
            continue
        if end_options:
            positional.append(PathWord(at=index, prefix="", path=word))
            continue
        if word == "--":
            end_options = True
            continue
        if word.startswith("--"):
            name, separator, value = word.partition("=")
            if name == "--in-place":
                in_place = True
                backup = value
                continue
            if name == "--file":
                return KernelDecision(
                    "deny",
                    "a sed script file is run without anything reading it",
                    recovery="Inline the script.",
                )
            if name == "--sandbox" and not separator:
                sandbox = True
                continue
            if name == "--expression":
                if separator:
                    scripts.append(value)
                    scripted.append(index)
                script_expected = not separator
                script_from_options = True
                continue
            if name in SED_SAFE_LONG_OPTIONS and not separator:
                options.append(name)
                continue
            return unjudged(f"sed option {name!r} is not classified")
        if word.startswith("-") and len(word) > 1:
            flags = word[1:]
            if "i" in flags:
                # `-i` takes its backup suffix attached, so everything after
                # it is that suffix rather than more flags — which is also
                # sed's own reading of `-ie`.
                in_place = True
                backup = flags[flags.index("i") + 1 :]
                flags = flags[: flags.index("i")]
            if "f" in flags:
                return KernelDecision(
                    "deny",
                    "a sed script file is run without anything reading it",
                    recovery="Inline the script.",
                )
            if flags.endswith("e"):
                script_expected = True
                script_from_options = True
                flags = flags[:-1]
            if any(flag not in SED_SAFE_SHORT_FLAGS for flag in flags):
                return unjudged(f"sed option {word!r} is not classified")
            if flags:
                options.append("-" + flags)
            continue
        positional.append(PathWord(at=index, prefix="", path=word))
    if script_expected:
        return unjudged("sed expression flag has no script")
    if not script_from_options and positional:
        first = positional.pop(0)
        scripts.append(first["path"])
        scripted.append(first["at"])
    return SedInvocation(
        scripts=scripts,
        scripted=scripted,
        options=options,
        backup=backup,
        targets=[target["path"] for target in positional],
        named=positional,
        in_place=in_place,
        screened=sandbox or all(safe_sed_script(script) for script in scripts),
    )


def sed_rewrite_words(words: list[str]) -> list[PathWord] | None:
    """Which words an in-place sed reads the files it rewrites out of.

    The words rather than the paths, for the reason every path reader here
    hands words back: where an operand sits is what a placement resolves it
    from, and a bare list of paths has thrown that away.
    """
    if posixpath.basename(words[0]) != "sed":
        return None
    invocation = sed_invocation(words)
    if isinstance(invocation, KernelDecision):
        return []
    return invocation["named"] if invocation["in_place"] else []
