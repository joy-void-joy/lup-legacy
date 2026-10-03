# lup: ignore[empty-collection, string-split]
# The dependency-free runtime deliberately uses primitive rows and stdlib scanners.
"""Shell variable bindings: which literal a name holds, and where it expands.

Every reader of a command asks what its words become once the shell expands
them, and each one answering it apart leaves `S=f.py; sed -i … $S` judged
against `f.py` by the classifier and against `$S` by the host that produces
the rewritten document -- two readings of one line, and the ask that falls
between them. So a command's variables are resolved once, over its
syntax tree, before any reader takes a word from it.

The tree is what makes the expansion exact rather than textual. A `$S` inside
single quotes or a quoted heredoc is not a parameter part at all, so nothing
here can expand it; a loop, a branch, a subshell or a pipe is a node, so
whether an assignment stands for the words after it is read off the shape
rather than guessed from the keywords in front of a segment.
"""

import posixpath
from collections.abc import Callable
from typing import TypedDict

from .roles import spells_its_path
from .syntax import (
    AndOr,
    Arm,
    Clause,
    Command,
    Item,
    Pipeline,
    Redirect,
    Script,
    Word,
    WordPart,
    part,
    verbatim,
    word_text,
)
from .words import effective_command


class ShellBinding(TypedDict):
    """One frozen variable binding: a name, and its literal value or None.

    ``value`` is ``None`` where the word could not be read as a literal, which
    is what makes the binding opaque to every later substitution.
    """

    name: str
    value: str | None


def bind_name(
    bindings: tuple[ShellBinding, ...], name: str, value: str | None
) -> tuple[ShellBinding, ...]:
    """Rebind one name immutably, shadowing any earlier binding of it."""
    kept = tuple(pair for pair in bindings if pair["name"] != name)
    return (*kept, ShellBinding(name=name, value=value))


def literal_loop_word(word: Word) -> bool:
    """A word whose runtime expansion is exactly the text it reads as.

    Quoting is what decides it (:func:`~lup.policy.kernel.syntax.verbatim`):
    `'*.py'` is those five characters, `*.py` is whatever the glob matches,
    `"$f"` is whatever `f` holds, and `a{b,c}` is two words.
    """
    return not word_text(word).startswith("/dev/fd/") and verbatim(word)


def literal_value(word: str) -> bool:
    """Whether an assigned value is a string every reference expands to whole.

    A glob character is stored unexpanded by an assignment but expanded again
    wherever the name is referenced unquoted, whitespace splits it into more
    words than one, and a substitution sentinel or backtick is a value nobody
    here ran. Each leaves a reference that could become a different argument
    list, so only the rest bind.
    """
    return (
        not word.startswith(("~", "/dev/fd/"))
        and not any(character in "$*?[" or character.isspace() for character in word)
        and spells_its_path(word)
    )


def pure_assignment_names(segment: list[str]) -> list[ShellBinding] | None:
    """The bindings of an assignment-only segment."""
    pairs: list[ShellBinding] = []
    for word in segment:
        name, separator, value = word.partition("=")
        if not separator or not name.isidentifier():
            return None
        pairs.append(
            ShellBinding(name=name, value=value if literal_value(value) else None)
        )
    return pairs


def named_parameters(parts: list[WordPart]) -> list[str]:
    """Every parameter these parts expand, outside any substitution in them."""
    return [
        name
        for item in parts
        for name in ([item["name"]] if item["kind"] == "param" and item["name"] else [])
        + named_parameters(item["parts"])
    ]


def references(words: list[Word], name: str) -> bool:
    """Whether any of these words still expands ``name``."""
    return any(name in named_parameters(word["parts"]) for word in words)


# lup: ignore[library-default] — bash's own builtins that assign a named variable
ASSIGNING_BUILTINS = (
    "declare",
    "export",
    "getopts",
    "let",
    "local",
    "mapfile",
    "printf",
    "read",
    "readarray",
    "readonly",
    "typeset",
    "unset",
)
# lup: ignore[library-default] — the shell's builtins that run text as commands
EVALUATING_BUILTINS = ("eval", "source", ".")


def assigning_operands(words: list[str]) -> list[str]:
    """The names a builtin could assign, read from its operands.

    Over-reads on purpose: `printf -v NAME` and `getopts spec NAME` are named
    by position, and taking every identifier-shaped operand costs only a
    substitution that did not happen, where missing one resolves a reference
    to a value the name no longer holds.
    """
    names: list[str] = []
    for word in words[1:]:
        name = word.partition("=")[0].removesuffix("+").partition("[")[0]
        if name.isidentifier():
            names.append(name)
    return names


def unsettled_assignments(
    words: list[str], executable: list[str], standing: bool
) -> list[str] | None:
    """Names one simple command assigns that a later reference cannot rely on.

    ``words`` is the command as it reads, ``executable`` what runs once
    leading assignments and wrappers are skipped, and ``standing`` whether a
    plain assignment here holds for every later word. ``None`` means an
    ``eval`` or ``source`` could assign any name at all.
    """
    command = posixpath.basename(executable[0]) if executable else ""
    if command in EVALUATING_BUILTINS:
        return None
    names: list[str] = []
    if command in ASSIGNING_BUILTINS:
        names.extend(assigning_operands(executable))
    names.extend(assigning_operands(["", *[word for word in words if "+=" in word]]))
    plain = pure_assignment_names(words)
    if plain is not None and not standing:
        names.extend(pair["name"] for pair in plain)
    return names


def defaulted_names(parts: list[WordPart]) -> list[str]:
    """Names a ``${NAME:=value}`` or ``${NAME=value}`` expansion assigns."""
    return [
        name
        for item in parts
        for name in (
            [item["name"]]
            if item["kind"] == "param" and item["operator"] in (":=", "=")
            else []
        )
        + defaulted_names(item["parts"])
    ]


def command_lists(command: Command) -> list[Script]:
    """The lists a command runs, in the order they appear."""
    return [
        *(
            script
            for clause in command["clauses"]
            for script in (clause["condition"], clause["body"])
        ),
        command["body"],
        *(arm["body"] for arm in command["arms"]),
    ]


def carried_words(command: Command) -> list[Word]:
    """Every word a command carries itself, redirection targets included."""
    return [
        *command["words"],
        *(pattern for arm in command["arms"] for pattern in arm["patterns"]),
        *(target for redirect in command["redirects"] for target in redirect["target"]),
    ]


class Placed(TypedDict):
    """One command a list runs directly, and how far its assignments hold."""

    command: Command
    standing: bool
    """An assignment here holds for every word after it in the list.

    Behind `&&` too, where nothing before it in the chain can fail
    (:func:`certain`): what such a chain holds runs whenever the chain does."""
    chained: bool
    """An assignment here holds for the rest of its `&&` chain, and no further."""
    opens: bool
    """What a chain before this command bound stops holding here."""


def certain(pipeline: Pipeline) -> bool:
    """Whether a `&&` chain member is taken to run and succeed whenever it is reached.

    An assignment of literal values cannot fail. A `cd` naming one literal
    directory is taken to have reached it, which is the reading the placing
    pass gives every literal `cd` a later command is placed behind; a
    `cd` that fails leaves the shell where it was, and that reading is
    already the one judged. Anything else -- `false`, `test -f x`, an
    assignment whose value runs a command -- can fail, and skip what its
    chain holds after it.
    """
    match pipeline["commands"]:
        case [command] if command["kind"] == "simple" and not command["redirects"]:
            texts = [word_text(word) for word in command["words"]]
            match texts:
                case ["cd", operand] if not operand.startswith("-"):
                    return literal_loop_word(command["words"][1])
                case _:
                    assigned = pure_assignment_names(texts)
                    return bool(assigned) and all(
                        pair["value"] is not None for pair in assigned or []
                    )
        case _:
            return False


def placed_commands(script: Script, standing: bool) -> list[Placed]:
    """Each command a list runs directly, and how far its assignments hold.

    An assignment stands when nothing between it and the words after it can
    keep it from holding: not beside a pipe or `&`, which put it in a process
    of its own, and not after `&&` or `||`, which may skip it.

    One reached through nothing but `&&` holds for less, and exactly: every
    command joined to it by `&&` alone runs only once everything before it in
    the chain ran and succeeded, so `cd w && F=x && sed -i … $F` rewrites `x`
    whatever `F` held before. An `||` runs what follows where something
    before it failed or was skipped, and the next item runs either way, so
    the chain ends at the first of them, and past it the name holds whichever
    value ran. Where nothing before it in the chain can fail (:func:`certain`)
    it runs whenever the chain does, and stands: `cd w && S=/abs; … $S` reads
    `/abs` past the `;`, while `false && S=/abs; … $S` reads whatever `S`
    already held.
    """
    return [
        Placed(
            command=command,
            standing=standing
            and item["terminator"] != "&"
            and len(pipeline["commands"]) == 1
            and all(operator == "&&" for operator in operators[:index])
            and all(certain(before) for before in pipelines[:index]),
            chained=standing
            and item["terminator"] != "&"
            and index > 0
            and len(pipeline["commands"]) == 1
            and all(operator == "&&" for operator in operators[:index])
            and not all(certain(before) for before in pipelines[:index]),
            opens=position == 0 and (index == 0 or operators[index - 1] == "||"),
        )
        for item in script["items"]
        for operators in [item["andor"]["operators"]]
        for pipelines in [item["andor"]["pipelines"]]
        for index, pipeline in enumerate(pipelines)
        for position, command in enumerate(pipeline["commands"])
    ]


def unsettled_names(script: Script, standing: bool = True) -> list[str] | None:
    """Every name a list assigns where the assignment may not stand.

    Inside a loop, a branch, a case arm or a subshell, beside a pipe, after
    an `||`, through a builtin or a ``${NAME:=…}`` default, what a later
    reference expands to depends on what ran, and nothing here decides that.
    A plain assignment in a `&&` chain is settled for the rest of the chain,
    which :func:`bound_list` binds and releases where the chain ends.
    ``None`` means an ``eval`` or ``source`` could assign any name at all.
    What a substitution assigns stays inside it, so it is not read here.
    """
    names: list[str] = []
    for placed in placed_commands(script, standing):
        command = placed["command"]
        names.extend(
            name
            for word in carried_words(command)
            for name in defaulted_names(word["parts"])
        )
        if command["kind"] == "simple":
            texts = [word_text(word) for word in command["words"]]
            found = unsettled_assignments(
                texts,
                effective_command(texts)["words"],
                (placed["standing"] or placed["chained"]) and not command["redirects"],
            )
            if found is None:
                return None
            names.extend(found)
            continue
        if command["kind"] in ("for", "select"):
            names.append(command["name"])
        for inner in command_lists(command):
            found = unsettled_names(
                inner, placed["standing"] and command["kind"] == "brace"
            )
            if found is None:
                return None
            names.extend(found)
    return names


ListMapping = Callable[[Script], Script]
CommandMapping = Callable[[Command], Command]


def expanded_parts(
    parts: list[WordPart], bindings: tuple[ShellBinding, ...], settle: bool
) -> list[WordPart]:
    """Parts with every literal binding in scope expanded into them.

    A substitution's command is expanded from the same bindings: bound, with
    its own assignments settled, where ``settle`` says the whole line is being
    bound, and substituted only where one name is being instantiated.
    """
    literal = {
        binding["name"]: binding["value"]
        for binding in bindings
        if binding["value"] is not None
    }
    expanded: list[WordPart] = []
    for item in parts:
        value = literal.get(item["name"]) if item["kind"] == "param" else None
        match item["kind"]:
            case "param" if not item["operator"] and value is not None:
                expanded.append(part("literal", value))
            case "double" | "param":
                expanded.append(
                    part(
                        item["kind"],
                        item["text"],
                        item["name"],
                        item["operator"],
                        parts=expanded_parts(item["parts"], bindings, settle),
                    )
                )
            case "command" | "process":
                expanded.append(
                    part(
                        item["kind"],
                        item["text"],
                        script=[
                            bind_script(inner, bindings)
                            if settle
                            else expanded_script(inner, bindings)
                            for inner in item["script"]
                        ],
                    )
                )
            case _:
                expanded.append(item)
    return expanded


def expanded_word(
    word: Word, bindings: tuple[ShellBinding, ...], settle: bool = False
) -> Word:
    """One word with every literal binding in scope expanded into it."""
    return Word(parts=expanded_parts(word["parts"], bindings, settle))


def expanded_command(
    command: Command, bindings: tuple[ShellBinding, ...], settle: bool
) -> Command:
    """A command's own words and redirection targets expanded; its lists untouched."""

    def expand(words: list[Word]) -> list[Word]:
        return [expanded_word(word, bindings, settle) for word in words]

    return Command(
        kind=command["kind"],
        words=expand(command["words"]),
        redirects=[
            Redirect(
                operator=redirect["operator"],
                target=expand(redirect["target"]),
                heredoc=redirect["heredoc"],
            )
            for redirect in command["redirects"]
        ],
        name=command["name"],
        listed=command["listed"],
        clauses=command["clauses"],
        body=command["body"],
        arms=[
            Arm(patterns=expand(arm["patterns"]), body=arm["body"])
            for arm in command["arms"]
        ],
        directory=command["directory"],
    )


def rebuilt_lists(command: Command, rebuild: ListMapping) -> Command:
    """A command whose lists have each been passed through ``rebuild``, in order."""
    clauses = [
        Clause(condition=rebuild(clause["condition"]), body=rebuild(clause["body"]))
        for clause in command["clauses"]
    ]
    body = rebuild(command["body"])
    arms = [
        Arm(patterns=arm["patterns"], body=rebuild(arm["body"]))
        for arm in command["arms"]
    ]
    return Command(
        kind=command["kind"],
        words=command["words"],
        redirects=command["redirects"],
        name=command["name"],
        listed=command["listed"],
        clauses=clauses,
        body=body,
        arms=arms,
        directory=command["directory"],
    )


def mapped_commands(script: Script, mapping: CommandMapping) -> Script:
    """A list with every command it runs directly replaced, in order."""
    return Script(
        items=[
            Item(
                andor=AndOr(
                    pipelines=[
                        Pipeline(
                            commands=[
                                mapping(command) for command in pipeline["commands"]
                            ],
                            operators=pipeline["operators"],
                            negated=pipeline["negated"],
                        )
                        for pipeline in item["andor"]["pipelines"]
                    ],
                    operators=item["andor"]["operators"],
                ),
                terminator=item["terminator"],
            )
            for item in script["items"]
        ]
    )


def expanded_script(script: Script, bindings: tuple[ShellBinding, ...]) -> Script:
    """A whole list with the given bindings substituted wherever they are live.

    No assignment in the list is consulted: this instantiates a loop body
    once per literal loop word, where the word is the value by construction.
    """

    def rebuild(command: Command) -> Command:
        return rebuilt_lists(
            expanded_command(command, bindings, settle=False),
            lambda inner: expanded_script(inner, bindings),
        )

    return mapped_commands(script, rebuild)


def unrollable(command: Command, limit: int = 16) -> bool:
    """Whether every pass of a `for` loop can be read off its literal words.

    A listed loop over one to ``limit`` words, each exactly the text it reads
    as, whose body never assigns the loop's own name: each reference in the
    body is then that word on its pass and nothing else. A body assigning the
    name -- or able to, through `eval` -- makes a later reference some other
    value (`f=README.md; rm $f` removes README.md whatever the list said),
    and a longer list costs more readings than one line is worth.
    """
    words = command["words"]
    if command["kind"] != "for" or not command["listed"]:
        return False
    if not words or len(words) > limit:
        return False
    if not all(literal_loop_word(word) for word in words):
        return False
    assigned = unsettled_names(command["body"], standing=False)
    return assigned is not None and command["name"] not in assigned


def bound_passes(
    command: Command, bindings: tuple[ShellBinding, ...], unsettled: list[str]
) -> Script:
    """A literal loop's passes, each binding what it assigns for the rest of itself.

    A pass runs its body's list in order, so an assignment standing in it --
    `W=tmp/$v; rm -rf $W` -- holds for the rest of that pass, which reads
    `rm -rf tmp/a` on one pass and `rm -rf tmp/b` on the next. What a name
    the body assigns holds as a pass begins is the outer value or whatever an
    earlier pass left, and a `break` or `continue` there decides which, so
    each pass starts with it unread. What stands nowhere in the pass -- an
    assignment in a branch, beside a pipe, after an `||` -- stays unread for
    the whole pass, as it does anywhere. After the loop the names it assigns
    stay unread too: :func:`bind_script` counts them unsettled for the line.

    Sequential rather than side by side, because a pass leaves the shell
    where its `cd` took it for the next one, which is where the placing pass
    reads each copy's words from.
    """
    assigned = unsettled_names(command["body"], standing=False) or []
    outside = [name for name in unsettled if name not in assigned]
    start = bindings
    for name in assigned:
        start = bind_name(start, name, None)
    items: list[Item] = []
    for word in command["words"]:
        body = expanded_script(
            command["body"],
            (ShellBinding(name=command["name"], value=word_text(word)),),
        )
        within = unsettled_names(body)
        if within is None:
            items.extend(bound_list(body, start, unsettled, False)["script"]["items"])
            continue
        items.extend(
            bound_list(body, start, [*outside, *within], True)["script"]["items"]
        )
    return Script(items=items)


class BoundList(TypedDict):
    """A list with its words expanded, and the bindings standing after it."""

    script: Script
    bindings: tuple[ShellBinding, ...]


def bound_list(
    script: Script,
    bindings: tuple[ShellBinding, ...],
    unsettled: list[str],
    standing: bool,
) -> BoundList:
    """Expand one list in order, letting each standing assignment rebind.

    An assignment in a `&&` chain rebinds for the rest of that chain, and
    where the chain ends its name holds whichever value ran, which nothing
    here names.
    """
    placements = iter(placed_commands(script, standing))
    chained: list[str] = []

    def released(held: tuple[ShellBinding, ...]) -> tuple[ShellBinding, ...]:
        for name in chained:
            held = bind_name(held, name, None)
        return held

    def rebuild(command: Command) -> Command:
        nonlocal bindings, chained
        placed = next(placements)
        if placed["opens"]:
            bindings = released(bindings)
            chained = []
        expanded = expanded_command(command, bindings, settle=True)
        match command["kind"]:
            case "simple":
                assigned = pure_assignment_names(
                    [word_text(word) for word in expanded["words"]]
                )
                binds = placed["standing"] or placed["chained"]
                if binds and not command["redirects"] and assigned is not None:
                    for pair in assigned:
                        if pair["name"] not in unsettled:
                            bindings = bind_name(bindings, pair["name"], pair["value"])
                            if placed["chained"]:
                                chained.append(pair["name"])
                return expanded
            case "brace":
                inner = bound_list(
                    command["body"], bindings, unsettled, placed["standing"]
                )
                bindings = inner["bindings"]
                return rebuilt_lists(expanded, lambda _body: inner["script"])
            case "for" if unrollable(expanded):
                # Read once per word here, for every reader of the line: a
                # redirection's target or a `cd`'s directory in the body then
                # names each path a pass reaches, where it named the variable.
                passes = bound_passes(expanded, bindings, unsettled)
                return rebuilt_lists(expanded, lambda _body: passes)
            case _:
                scope = bindings
                return rebuilt_lists(
                    expanded,
                    lambda body: bound_list(body, scope, unsettled, False)["script"],
                )

    rebuilt = mapped_commands(script, rebuild)
    return BoundList(script=rebuilt, bindings=released(bindings))


def bind_script(script: Script, inherited: tuple[ShellBinding, ...] = ()) -> Script:
    """Expand every literal variable binding into the words that reference it.

    The one place a command's variables are resolved, and resolved before
    anything reads a word: the classifier's walk, the redirection rows, and
    the host readers that stat targets and produce rewritten documents all
    take their words from the tree this returns, so none of them can
    disagree about what `$S` names.

    Only a standing assignment binds, and one in a `&&` chain for the rest of
    its chain (:func:`placed_commands`). `VAR=x cmd`
    sets `VAR` for that one command's environment, and the shell expands the
    command's words from the value it already held. A name assigned anywhere
    it may not stand (:func:`unsettled_names`) is left unexpanded for the
    whole list, so a loop's second pass or an untaken branch is never judged
    by another reading's value, and a list that can ``eval`` expands nothing.
    A substitution inherits the bindings in scope where it stands.
    """
    unsettled = unsettled_names(script)
    if unsettled is None:
        return script
    bindings = inherited
    for name in unsettled:
        bindings = bind_name(bindings, name, None)
    return bound_list(script, bindings, unsettled, standing=True)["script"]
