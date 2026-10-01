"""What a shell command may not reach: withheld paths, and secret variables.

Two kinds of thing are kept from a session by name rather than by what the
command does with them. A private key or a login is reached the moment a
command names it, whichever verb does the reaching -- `cat` prints it, `cp`
copies it somewhere readable, `tar` packs it, `grep -r` walks into it -- and a
list of the verbs that read would be a list of the ones somebody remembered.
So every word is read instead, and a word naming a withheld path refuses the
command it sits in, whatever that command is. The redirections are read the
same way, since `cat < key` names the key to the shell rather than to `cat`.

A variable is the other half. The whole environment printed is already refused
as a dump; one secret printed is the same disclosure narrowed to the variable
that mattered, so the builtins that write their operands are refused a secret's
value, and so is a here-string handing one to any command.

Both answer ``deny`` rather than asking, for the reason the dump does: what is
printed lands in a transcript that outlives the turn, and a question would be
answered yes on the way to something else. The escalation marker still turns a
refusal into that question for a caller who means it.
"""

import posixpath
from fnmatch import fnmatchcase
from pathlib import PurePosixPath
from typing import TypedDict

from .decision import KernelDecision
from .diagnostic import Step, step
from .lex import placed_path, placed_redirects
from .rows import RefusedPathRow, WithheldWalkRow
from .syntax import Redirect, Script, Word, WordPart, word_text
from .walks import grep_split, pattern_positions, placed_root, walked_roots
from .words import expands_to, sed_invocation

# lup: ignore[library-default] — the shell builtins that write their operands to stdout
PRINTING_BUILTINS = ("echo", "printf", "print")
# lup: ignore[library-default] — bash's parameter operators whose expansion never carries the value
UNPRINTED_OPERATORS = ("length", "+", ":+")


def could_name(pattern: str, name: str) -> bool:
    """Whether one word's name could be one a pattern's name matches.

    Either side may be a glob: the pattern's is the declaration, and the
    word's is expanded by the shell before the command sees it. So a globbed
    name reaches whatever the pattern's name spells -- except a leading-dot
    name, which the shell's expansion skips unless the glob starts with a dot
    itself. Without that exception `cat *` in any directory would read as
    naming `.netrc`.
    """
    return fnmatchcase(name, pattern) or expands_to(name, pattern)


def covered_by(pattern: str, name: str) -> bool:
    """Whether a pattern's name holds everything one word's name could expand to.

    The exemption's test, one-directional where :func:`could_name` is not: a
    word escapes a refusal only when every file it could name is exempt, so
    `~/.ssh/*.pub` is exempt and `~/.ssh/*` is not.
    """
    return fnmatchcase(name, pattern)


def spans(
    pattern: tuple[str, ...], names: tuple[str, ...], component: bool = True
) -> bool:
    """Whether a pattern's names match exactly these, ``**`` spanning any run.

    ``component`` picks the test one name is put to: :func:`could_name` for a
    refusal, :func:`covered_by` for an exemption.
    """
    if not pattern:
        return not names
    head, rest = pattern[0], pattern[1:]
    if head == "**":
        return any(
            spans(rest, names[index:], component) for index in range(len(names) + 1)
        )
    if not names:
        return False
    matched = could_name(head, names[0]) if component else covered_by(head, names[0])
    return matched and spans(rest, names[1:], component)


def reaches(pattern: str, path: str, component: bool = True) -> bool:
    """Whether a path is one a declared pattern names.

    A pattern spelled from the root names that one place, and is matched
    against an absolute path from its start. A pattern spelled from ``~`` is
    matched wherever its names end a path, because the kernel knows no home:
    `~/.ssh/id_rsa`, `$HOME/.ssh/id_rsa`, `/home/u/.ssh/id_rsa` and
    `.ssh/id_rsa` read from a `cd` into one are the same file, and a word
    naming some other directory's `.ssh/id_rsa` is key material all the same.

    Matching wherever a path ends has one cost a glob would make everyday:
    `.*` in any directory reaches `~/.netrc`. So a run of names that is all
    glob reaches a pattern spelled from ``~`` only where the word spells the
    home it stands in -- `~/.*` does, `ls -d .*` in a checkout does not.

    A pattern spelled from anywhere has no home to spell: the file it names
    may stand in whichever directory the glob is read in, so a glob reaches it
    wherever it stands -- `cat .env*` in a checkout names its `.env.local`.
    Read the same whether or not the word was placed under the checkout,
    where the checkout's own names are literal ones.
    """
    wanted = PurePosixPath(pattern).parts
    anchored = wanted[:1] == ("/",)
    home = wanted[:1] == ("~",)
    wanted = wanted[1:] if anchored or home else wanted
    absolute = posixpath.isabs(path)
    names = PurePosixPath(posixpath.normpath(path)).parts[1 if absolute else 0 :]
    if anchored:
        return absolute and spans(wanted, names, component)
    return any(
        spans(wanted, names[start:], component)
        and (not home or literal_among(names[start:]) or spelled_home(names[:start]))
        for start in range(len(names))
    )


def literal_among(names: tuple[str, ...]) -> bool:
    """Whether any of these names is spelled out rather than globbed."""
    return any(not any(mark in name for mark in "*?[") for name in names)


def spelled_home(names: tuple[str, ...]) -> bool:
    """Whether a path's leading names end in a spelling of a home directory."""
    return bool(names) and (
        names[-1].startswith("~") or names[-1] in ("$HOME", "${HOME}")
    )


def withheld_row(path: str, rows: list[RefusedPathRow]) -> RefusedPathRow | None:
    """The first declaration refusing this path, where one does."""
    return next(
        (
            row
            for row in rows
            if any(reaches(pattern, path) for pattern in row["paths"])
            and not any(reaches(pattern, path, False) for pattern in row["exempt"])
        ),
        None,
    )


def named_paths(word: str) -> list[str]:
    """Every string one word could name a file by.

    The word itself, and the value an option or an operand attaches after
    `=` or `:` -- `--file=<path>`, `if=<path>`, socat's
    `UNIX-CONNECT:<path>,<options>`, scp's `host:<path>` -- which names a file
    exactly as the word standing alone would, with socat's trailing options
    read off. Reading a word's tail as a path it is not costs nothing unless
    the tail names a withheld one.
    """
    # lup: ignore[string-split] — an argv word's attached value, whose only parser is the program's own
    tails = [tail for tail in (word.partition(mark)[2] for mark in "=:") if tail]
    # lup: ignore[string-split] — socat's own address grammar, options after the first comma
    heads = [tail.partition(",")[0] for tail in tails]
    return list(dict.fromkeys([word, *tails, *heads]))


def withheld_edit(path: str, rows: list[RefusedPathRow]) -> KernelDecision | None:
    """The refusal a file tool earns for writing a withheld path, or nothing.

    The declaration every command's words are read against, read at the path
    a file tool resolved: an edit naming a key or a login names it as surely
    as `cp` would, and one authoring a login file a session could not read is
    planting one no command was allowed to. The exemptions are the reading's
    too, so what a command may name an edit may write.
    """
    row = withheld_row(path, rows)
    if row is None:
        return None
    return KernelDecision(
        "deny",
        row["reason"],
        cause="deliberate",
        recovery=row["recovery"],
        subject=path,
    )


def withheld_path(
    word: str, directory: str | None, checkout_root: str, rows: list[RefusedPathRow]
) -> KernelDecision | None:
    """The refusal one word earns by naming a withheld path, or nothing.

    Read from where the command stands, and from the checkout above that, so
    a relative word reaches a pattern anchored at the root the way the shell
    would resolve it. A directory nothing here can name leaves the word as it
    was spelled, which still reaches every pattern spelled from ``~``.
    """
    for named in named_paths(word):
        placed = placed_path(named, directory) or named
        path = (
            posixpath.join(checkout_root, placed)
            if checkout_root and not posixpath.isabs(placed)
            else placed
        )
        row = withheld_row(path, rows)
        if row is not None:
            return KernelDecision(
                "deny",
                row["reason"],
                cause="deliberate",
                recovery=row["recovery"],
                subject=word,
            )
    return None


def withheld_operand(
    words: list[str],
    directory: str | None,
    checkout_root: str,
    rows: list[RefusedPathRow],
) -> KernelDecision | None:
    """The refusal one command earns for any operand naming a withheld path.

    Every word after the command's own name, because which of them it reads
    is the command's grammar and there is one grammar per program: a flag's
    value, a copy's source, an archive's member, a directory to walk.
    """
    # lup: solved: a recursive reader over an ancestor -- `grep -r x ~`,
    # `tar czf out ~` -- names no withheld path and walks into one anyway;
    # catching it needs which verbs recurse, since `ls ~` and `cd ~` must not
    # be refused for sitting above a key.
    invocation = (
        sed_invocation(words) if posixpath.basename(words[0]) == "sed" else None
    )
    scripted = (
        invocation["scripted"]
        if invocation is not None and not isinstance(invocation, KernelDecision)
        else []
    )
    # A program or a pattern is text the command runs or matches, and names
    # no file whatever it spells: `sed 's/.*/token/' f` opens `f` alone.
    texts = [*scripted, *pattern_positions(words)]
    return next(
        (
            refused
            for index, word in enumerate(words[1:], start=1)
            if index not in texts
            and (refused := withheld_path(word, directory, checkout_root, rows))
            is not None
        ),
        None,
    )


class WithheldNames(TypedDict):
    """The names a withheld path ends on, spelled out and globbed apart."""

    literal: list[str]
    patterned: list[str]


def withheld_names(rows: list[RefusedPathRow]) -> WithheldNames:
    """The name each withheld pattern ends on, which a walk looks for.

    Each pattern ends on a name its files carry, or sit beneath where it ends
    in ``**`` -- `.ssh`, `credentials`, `.env.local`. A walk looking for these
    finds every candidate without reading every pattern at every file, and
    :func:`withheld_row` then decides each candidate it finds. Read once per
    command, and kept apart by whether a name is a glob, because a walk asks
    this of every name it passes.
    """
    endings = list(
        dict.fromkeys(
            next(
                part for part in reversed(PurePosixPath(pattern).parts) if part != "**"
            )
            for row in rows
            for pattern in row["paths"]
        )
    )

    def globbed(name: str) -> bool:
        """Whether a name is a glob rather than the name it spells."""
        return any(mark in name for mark in "*?[")

    return WithheldNames(
        literal=[ending for ending in endings if not globbed(ending)],
        patterned=[ending for ending in endings if globbed(ending)],
    )


def carries_withheld_name(name: str, names: WithheldNames) -> bool:
    """Whether a file or directory by this name could carry a withheld path."""
    return name in names["literal"] or any(
        fnmatchcase(name, pattern) for pattern in names["patterned"]
    )


def withheld_walk(
    words: list[str],
    directory: str | None,
    checkout_root: str,
    walks: list[WithheldWalkRow],
    rows: list[RefusedPathRow],
    listed: bool = False,
) -> KernelDecision | None:
    """The refusal a recursive read earns where its root holds a withheld path.

    Which words the command walks is its grammar, read by
    :func:`~kernel.walks.walked_roots`; what lies beneath each is the host's
    to say, since only a filesystem can, spelled beneath the root the command
    named. A read reaching a key or a login is reading it, whichever word
    named the directory above it, so it meets the refusal naming that path
    would. A walk the host could not finish before the hook's deadline met no
    answer, which is not an answer of none.
    """
    reached = next(
        (
            walk
            for root in walked_roots(words, listed)
            for walk in walks
            if walk["root"] == placed_root(root["path"], directory)
        ),
        None,
    )
    if reached is None:
        return None
    found = reached["found"]
    placed = placed_root(found, directory) or found
    row = withheld_row(
        posixpath.join(checkout_root, placed)
        if checkout_root and not posixpath.isabs(placed)
        else placed,
        rows,
    )
    executable = posixpath.basename(words[0])
    recovery = walk_recovery(words, reached)
    if row is None:
        return KernelDecision(
            "deny",
            f"walks everything beneath `{reached['root']}`, and the walk stopped at"
            f" `{found}` before this hook could show none of it is a key or a login",
            cause="deliberate",
            recovery=recovery,
            subject=executable,
        )
    return KernelDecision(
        "deny",
        f"walks everything beneath `{reached['root']}`, `{found}` among it, which"
        f" {row['reason']}",
        cause="deliberate",
        recovery=recovery,
        subject=executable,
    )


def walk_recovery(words: list[str], walk: WithheldWalkRow) -> tuple[Step, ...]:
    """The searches that read the same tree without walking into what was found.

    Named in the command's own words, because the checkout a session works
    in is the tree this refuses most often: `rg` skips hidden and ignored
    paths, and grep leaves out the directory holding what was found -- the
    first name beneath the root -- or the file, where it stands at the top.
    Another walker is told to name the directories below the root it needs.
    """
    depth = len(PurePosixPath(walk["root"]).parts)
    beneath = PurePosixPath(walk["found"]).parts[depth:]
    held = beneath[0] if beneath else walk["found"]
    if posixpath.basename(words[0]) not in ("grep", "egrep", "fgrep"):
        return (
            step(
                f"name the directories below it that the work needs, leaving out"
                f" `{held}`"
            ),
        )
    leaving = f"--exclude-dir={held}" if len(beneath) > 1 else f"--exclude={held}"
    at = 2 if len(words) > 1 and words[1].startswith("-") else 1
    return (
        step(
            "search with rg, which skips hidden and ignored paths",
            ["rg", *grep_split(words[1:])["operands"]],
        ),
        step(f"or leave `{held}` out", [*words[:at], leaving, *words[at:]]),
    )


def redirect_names(redirect: Redirect) -> bool:
    """Whether a redirection's target is a file, rather than text or a descriptor.

    A heredoc and a here-string carry their text where a file would be, and a
    duplication (`2>&1`, `<&-`) names a descriptor, which leaves its target
    empty.
    """
    operator = redirect["operator"]
    return bool(redirect["target"]) and not operator.endswith(("<<", "<<-", "<<<"))


def withheld_redirect(
    script: Script, checkout_root: str, rows: list[RefusedPathRow]
) -> KernelDecision | None:
    """The refusal a command line earns for redirecting to or from a withheld path."""
    return next(
        (
            refused
            for placed in placed_redirects(script)
            if redirect_names(placed["redirect"])
            for word in placed["redirect"]["target"]
            if (
                refused := withheld_path(
                    word_text(word), placed["directory"], checkout_root, rows
                )
            )
            is not None
        ),
        None,
    )


def secret_name(name: str, patterns: list[str]) -> bool:
    """Whether a variable's name is one this project treats as holding a secret.

    Compared without case, because a shell variable holding a token is as
    often spelled `token` as `TOKEN`.
    """
    return any(fnmatchcase(name.upper(), pattern.upper()) for pattern in patterns)


def printed_parameters(parts: list[WordPart]) -> list[str]:
    """Every variable these parts would print the value of.

    A command substitution is its own command and is judged as one. A length
    (`${#X}`) and an alternative (`${X:+set}`) print something about the
    variable rather than its value, which is how a script asks whether a
    secret is set without reading it -- so those name only what their own
    operand prints.
    """
    return [
        name
        for item in parts
        if item["kind"] != "command"
        for name in (
            [item["name"]]
            if item["kind"] == "param"
            and item["name"]
            and item["operator"] not in UNPRINTED_OPERATORS
            else []
        )
        + printed_parameters(item["parts"])
    ]


def printed_secret(
    words: list[Word], redirects: list[Redirect], patterns: list[str]
) -> KernelDecision | None:
    """The refusal one command earns for printing a secret variable's value.

    ``words`` start at the command the shell finally runs, wrappers already
    stepped over. A printing builtin prints every operand; any command reads
    a here-string's text on stdin, and one handed a secret there has it in
    the stream it is about to write.
    """
    printing = (
        bool(words) and posixpath.basename(word_text(words[0])) in PRINTING_BUILTINS
    )
    carried = [
        *(word for word in words[1:] if printing),
        *(
            word
            for redirect in redirects
            if redirect["operator"].endswith("<<<")
            for word in redirect["target"]
        ),
    ]
    named = next(
        (
            name
            for word in carried
            for name in printed_parameters(word["parts"])
            if secret_name(name, patterns)
        ),
        None,
    )
    return None if named is None else secret_refusal(named)


def secret_refusal(name: str) -> KernelDecision:
    """The refusal for writing one secret variable's value into this transcript."""
    return KernelDecision(
        "deny",
        "holds a secret, and printing it writes the secret into this transcript",
        cause="deliberate",
        recovery=(
            step("let the tool that needs it read the variable itself"),
            step(
                f'to learn whether it is set, test it: `[ -n "${name}" ] && echo set`'
            ),
        ),
        subject=f"${name}",
    )
