"""Move a module between the two halves, and repoint every import of it.

Deciding a utility belongs in the library rather than the application is a
one-line judgement and a hundred-line consequence: the module moves, and
every site that named it has to say the new name. Doing that by hand is
where the judgement gets abandoned, so the mechanical half is a command.

Both halves, and that is the correction. Repointing importers while leaving
the file where it was is a tree in which nothing resolves, reported as a
success — the type check names it a step later, as an unresolved import,
detached from the command that produced it.

Tokens decide, text applies. ``tokenize`` reports every token with its exact
position, so a dotted module path in an import statement is located by
grammar rather than by pattern; the replacement is then spliced over that one
span, leaving the spacing, the parenthesized continuation, and the comment
beside it byte for byte untouched. Splicing a span rather than swapping token
for token is what lets a path change depth — a flat module moving under a
subpackage is the ordinary relocation, and three names do not fit where two
were. A module path appearing anywhere else — a log line, a docstring naming
the old home, a path in a comment — is not a module run in an import
statement and is never matched; :func:`surviving_mentions` reports those for
a human to read instead.

``from package import submodule`` is the same tokens as importing a name the
package defines, and the grammar alone cannot tell the two apart. The move
can: it names ``package.submodule`` as a module, and lup's conventions refuse
``__init__.py`` re-exports, so no name the package binds stands between the
statement and the module. It is respelled ``from new.package import newleaf
as submodule`` — bound to the name it had, so every reference in the file
still resolves — and a statement also importing names that stayed is split:
those keep the statement and its layout, and each destination gets a
statement of its own where the old one runs. A name that, joined to its
package, spells no moved module is a symbol, and is left alone.

A splice may cross rows, so a path continued by a backslash is respelled
whole. What the rewrite declines is a statement it could only respell by
dropping a comment — a parenthesized list moving whole to a top-level module,
which bare ``import`` cannot parenthesize. That is named among the surviving
mentions, found by the grammar: the dotted path is never written whole there,
so the text search that finds prose cannot see it.
"""

import ast
import io
import tokenize
from collections.abc import Collection, Iterator
from pathlib import Path
from typing import Self

import sh
from pydantic import BaseModel

from lup.execution.shell import git

SOURCE_SUFFIXES = (".py", ".pyi")
"""Which files a sweep reads, for a caller that does not say.

The rewrite itself is Python's grammar and only ever will be, but what a
repository wants swept is a wider question than that: a tree carrying its
module paths in generated stubs or a sibling language passes the suffixes it
means rather than accepting these two.
"""


def dotted_parts(node: ast.expr) -> list[str] | None:
    """The names an attribute chain spells, outermost last."""
    match node:
        case ast.Name(id=name):
            return [name]
        case ast.Attribute(value=inner, attr=attribute):
            head = dotted_parts(inner)
            return None if head is None else [*head, attribute]
        case _:
            return None


def name_parts(dotted: str) -> list[str] | None:
    """The name tokens a dotted module path is made of, or None if it is not one.

    A module path is an expression in Python's own grammar — a name, or an
    attribute chain over one — so parsing it as that is both how the parts
    are read and how a typo is caught before it is used to rewrite a file.
    """
    try:
        return dotted_parts(ast.parse(dotted, mode="eval").body)
    except SyntaxError:
        return None


class Relocation(BaseModel, frozen=True):
    """One module path that moved, and where it moved to, as name tokens.

    Held as parts rather than as dotted text because that is what the rewrite
    splices: a run of ``NAME`` tokens replaces a run of ``NAME`` tokens, and
    the reading that turns a path into names happens once, at the boundary
    where somebody typed one.
    """

    old: list[str]
    new: list[str]


def destination(named: list[str], moves: list[Relocation]) -> list[str] | None:
    """Where a module path lands, if one of the moves carries it.

    A move carries the module it names and every module beneath it, so
    relocating a package takes its submodules without each being declared.
    Where two moves match, the longer one decides: a module declared on its own
    has its file carried to the name that move spells, and following its
    package's move instead would point the import at a module that is not
    there. The result may be longer or shorter than what it replaces — a move
    into or out of a subpackage changes the path's depth.
    """
    carrying = [move for move in moves if named[: len(move.old)] == move.old]
    if not carrying:
        return None
    move = max(carrying, key=lambda move: len(move.old))
    return [*move.new, *named[len(move.old) :]]


class ModuleRun(BaseModel, frozen=True):
    """The token span naming one module path, as indexes into the token list."""

    start: int
    end: int

    def named(self, tokens: list[tokenize.TokenInfo]) -> list[str]:
        """The names this run spells, without the dots between them."""
        return [
            token.string
            for token in tokens[self.start : self.end + 1]
            if token.string != "."
        ]


class Point(BaseModel, frozen=True):
    """A place in the source as ``tokenize`` counts it, rows from one."""

    row: int
    column: int

    @classmethod
    def before(cls, token: tokenize.TokenInfo) -> Self:
        """Where ``token`` begins."""
        return cls(row=token.start[0], column=token.start[1])

    @classmethod
    def after(cls, token: tokenize.TokenInfo) -> Self:
        """Where ``token`` ends."""
        return cls(row=token.end[0], column=token.end[1])


class Splice(BaseModel, frozen=True):
    """Text replacing the source between two points, which may be rows apart."""

    start: Point
    end: Point
    text: str

    def covers(self, point: Point) -> bool:
        """Whether ``point`` lies in the span this splice replaces."""
        return (
            (self.start.row, self.start.column)
            <= (point.row, point.column)
            < (self.end.row, self.end.column)
        )


class Respelling(BaseModel, frozen=True):
    """One import statement's splices, and how many module paths they repoint."""

    splices: list[Splice]
    repointed: int


class ImportedName(BaseModel, frozen=True):
    """One name a ``from`` statement imports, as indexes into the token list.

    ``comma`` is the separator following it, where one does. Which side of a
    name its comma sits on decides what leaves with it when it is split off.
    """

    name: str
    alias: str | None
    start: int
    end: int
    comma: int | None

    def spelled(self, leaf: str) -> str:
        """This name imported as ``leaf``, still bound to the local name it had."""
        local = self.alias or self.name
        return leaf if leaf == local else f"{leaf} as {local}"


class FromImport(BaseModel, frozen=True):
    """One absolute ``from package import names`` statement, read from its tokens.

    ``end`` is its last token: the closing parenthesis, or the last name.
    """

    module: ModuleRun
    names: list[ImportedName]
    end: int

    def home(
        self, tokens: list[tokenize.TokenInfo], moves: list[Relocation]
    ) -> list[str]:
        """Where the package this statement imports from lands."""
        package = self.module.named(tokens)
        return destination(package, moves) or package

    def landed(
        self, tokens: list[tokenize.TokenInfo], moves: list[Relocation]
    ) -> list[list[str]]:
        """The module path each imported name spells once the moves are made.

        A name that joined to its package spells a moved module is taken for
        that module, and lands where its move puts it. A name no move carries
        on its own follows its package, wherever that went.
        """
        package = self.module.named(tokens)
        home = self.home(tokens, moves)
        return [
            destination([*package, name.name], moves) or [*home, name.name]
            for name in self.names
        ]

    def carried(
        self, tokens: list[tokenize.TokenInfo], moves: list[Relocation]
    ) -> list[ImportedName]:
        """The names a move of their own carried away from their package."""
        home = self.home(tokens, moves)
        return [
            name
            for name, target in zip(self.names, self.landed(tokens, moves), strict=True)
            if target != [*home, name.name]
        ]

    def respelled(
        self, tokens: list[tokenize.TokenInfo], moves: list[Relocation]
    ) -> Respelling | None:
        """This statement as the moves leave it, or None where it reads the same.

        The names landing in one package keep the statement: those that
        followed the package where any did, otherwise the first destination
        named. They keep its layout, and a renamed module keeps the local name
        it was bound to, so every reference to it still resolves. Each other
        destination gets a statement of its own, run where this one runs: on
        a row of its own at the same indentation where this statement has
        its logical line to itself, and after a semicolon on the same line
        otherwise, so no code sharing the line runs before the name is bound
        and a one-line block keeps what it held.

        A name leaves with a comma: its own, or, for the last name, the one
        before it, unless that ends a row inside parentheses and may stay as a
        trailing comma. A row left blank by a name that stood alone on it is
        dropped when the splices are applied. Where every name went to a
        top-level module there is no package to keep the statement, and bare
        ``import`` replaces it whole.
        """
        package = self.module.named(tokens)
        home = self.home(tokens, moves)
        landed = self.landed(tokens, moves)
        repointed = int(home != package) + len(self.carried(tokens, moves))
        if not repointed:
            return None
        packages = [tuple(target[:-1]) for target in landed]
        grouped = {
            destination_package: [
                name.spelled(target[-1])
                for name, target, went in zip(self.names, landed, packages, strict=True)
                if went == destination_package
            ]
            for destination_package in dict.fromkeys(packages)
        }
        keeper = next(
            (
                candidate
                for candidate in (tuple(home), *grouped)
                if candidate and candidate in grouped
            ),
            None,
        )
        keyword = tokens[self.module.start - 1]

        def statement(destination_package: tuple[str, ...]) -> str:
            listed = ", ".join(grouped[destination_package])
            if not destination_package:
                return f"import {listed}"
            return f"from {'.'.join(destination_package)} import {listed}"

        if keeper is None:
            replaced = Splice(
                start=Point.before(keyword),
                end=Point.after(tokens[self.end]),
                text=statement(()),
            )
            return Respelling(splices=[replaced], repointed=repointed)
        staying = [went == keeper for went in packages]

        def removal(name: ImportedName) -> Iterator[Splice]:
            first = tokens[name.start]
            if name.comma is not None:
                yield Splice(
                    start=Point.before(first),
                    end=Point.before(tokens[name.comma + 1]),
                    text="",
                )
                return
            # Alone on its row, the name takes the space up to the comment or
            # the row's end, as one with a comma of its own does.
            following = tokens[name.end + 1]
            alone = tokens[name.start - 1].type == tokenize.NL
            reaches = alone and following.type in (tokenize.NL, tokenize.COMMENT)
            yield Splice(
                start=Point.before(first),
                end=Point.before(following)
                if reaches
                else Point.after(tokens[name.end]),
                text="",
            )
            anchor = [
                kept for kept, stays in zip(self.names, staying, strict=True) if stays
            ][-1]
            if anchor.comma is not None and tokens[anchor.comma + 1].type not in (
                tokenize.NL,
                tokenize.COMMENT,
            ):
                yield Splice(
                    start=Point.before(tokens[anchor.comma]),
                    end=Point.before(tokens[anchor.comma + 1]),
                    text="",
                )

        def insertion(added: list[str]) -> Splice:
            preceding = next(
                (
                    tokens[index]
                    for index in range(self.module.start - 2, -1, -1)
                    if tokens[index].type
                    not in (
                        tokenize.NL,
                        tokenize.COMMENT,
                        tokenize.INDENT,
                        tokenize.DEDENT,
                    )
                ),
                None,
            )
            following = next(
                tokens[index]
                for index in range(self.end + 1, len(tokens))
                if tokens[index].type != tokenize.COMMENT
            )
            owns_line = following.type == tokenize.NEWLINE and (
                preceding is None or preceding.type == tokenize.NEWLINE
            )
            if owns_line:
                indent = keyword.line[: keyword.start[1]]
                at = Point.before(following)
                text = "".join(f"\n{indent}{line}" for line in added)
            else:
                at = Point.after(tokens[self.end])
                text = "".join(f"; {line}" for line in added)
            return Splice(start=at, end=at, text=text)

        def splices() -> Iterator[Splice]:
            if keeper != tuple(package):
                yield Splice(
                    start=Point.before(tokens[self.module.start]),
                    end=Point.after(tokens[self.module.end]),
                    text=".".join(keeper),
                )
            for name, target, stays in zip(self.names, landed, staying, strict=True):
                match (stays, target[-1]):
                    case (False, _):
                        yield from removal(name)
                    case (True, leaf) if leaf != name.name:
                        yield Splice(
                            start=Point.before(tokens[name.start]),
                            end=Point.after(tokens[name.end]),
                            text=name.spelled(leaf),
                        )
            added = [statement(other) for other in grouped if other != keeper]
            if added:
                yield insertion(added)

        return Respelling(splices=list(splices()), repointed=repointed)


class RelocationEdit(BaseModel, frozen=True):
    """One file the rewrite changed, and how many imports it repointed."""

    path: Path
    imports: int


def dotted_run(tokens: list[tokenize.TokenInfo], start: int) -> int:
    """The index of the last token in the ``a.b.c`` run beginning at ``start``."""
    position = start
    while position + 2 < len(tokens):
        following = tokens[position + 1]
        if following.type != tokenize.OP or following.string != ".":
            break
        if tokens[position + 2].type != tokenize.NAME:
            break
        position += 2
    return position


def module_runs(tokens: list[tokenize.TokenInfo]) -> list[ModuleRun]:
    """Every module path an ``import a.b, c.d`` statement names, as a token span.

    After ``import`` where no ``from`` preceded it in the statement, and after
    each comma in that form, because ``import a.b, c.d`` names two. A ``from``
    statement is read whole by :func:`from_imports` instead: where the names
    it imports land depends on where its module went, so the two are
    respelled together. A semicolon ends a statement as a line break does, so
    what follows one is read afresh.
    """

    def run_at(index: int) -> Iterator[ModuleRun]:
        """The module run starting just past ``index``, if a name is there."""
        if index + 1 < len(tokens) and tokens[index + 1].type == tokenize.NAME:
            yield ModuleRun(start=index + 1, end=dotted_run(tokens, index + 1))

    def found() -> Iterator[ModuleRun]:
        from_seen = False
        listing = False
        for index, token in enumerate(tokens):
            match (token.type, token.string):
                case (tokenize.NEWLINE | tokenize.NL, _) | (tokenize.OP, ";"):
                    from_seen = listing = False
                case (tokenize.OP, ",") if listing:
                    yield from run_at(index)
                case (tokenize.NAME, "from"):
                    from_seen = True
                case (tokenize.NAME, "import") if not from_seen:
                    listing = True
                    yield from run_at(index)

    return list(found())


def from_imports(tokens: list[tokenize.TokenInfo]) -> list[FromImport]:
    """Every absolute ``from package import names`` statement, read whole.

    A ``from`` followed by no ``import`` — ``yield from``, ``raise ... from``
    — opens no such statement, and a relative import is not read either: its
    package is spelled by where the importing file sits, not by a name a move
    could match. Nor is a list that does not read as names, ``*`` among them,
    since nothing in it is a module a move could name.
    """

    def closes(token: tokenize.TokenInfo, parenthesized: bool) -> bool:
        """Whether ``token`` is where the list of imported names ends."""
        match (token.type, token.string):
            case (tokenize.OP, ")") if parenthesized:
                return True
            case (tokenize.NEWLINE, _) | (tokenize.OP, ";") if not parenthesized:
                return True
            case (tokenize.ENDMARKER, _):
                return True
            case _:
                return False

    def imported(segment: list[int], comma: int | None) -> ImportedName | None:
        """The name one comma-separated segment imports, if it reads as one."""
        match [tokens[index] for index in segment]:
            case [tokenize.TokenInfo(type=tokenize.NAME, string=name)]:
                alias = None
            case [
                tokenize.TokenInfo(type=tokenize.NAME, string=name),
                tokenize.TokenInfo(type=tokenize.NAME, string="as"),
                tokenize.TokenInfo(type=tokenize.NAME, string=str(bound)),
            ]:
                alias = bound
            case _:
                return None
        return ImportedName(
            name=name, alias=alias, start=segment[0], end=segment[-1], comma=comma
        )

    def statement(at: int) -> FromImport | None:
        """The statement a ``from`` at ``at`` opens, if it is one this reads."""
        if tokens[at + 1].type != tokenize.NAME:
            return None
        module = ModuleRun(start=at + 1, end=dotted_run(tokens, at + 1))
        keyword = tokens[module.end + 1]
        if (keyword.type, keyword.string) != (tokenize.NAME, "import"):
            return None
        parenthesized = tokens[module.end + 2].string == "("
        opening = module.end + 2 + parenthesized
        closing = next(
            index
            for index in range(opening, len(tokens))
            if closes(tokens[index], parenthesized)
        )
        if parenthesized and tokens[closing].string != ")":
            return None
        listing = [
            index
            for index in range(opening, closing)
            if tokens[index].type not in (tokenize.NL, tokenize.COMMENT)
        ]
        trailing = (
            listing[-1]
            if parenthesized and listing and tokens[listing[-1]].string == ","
            else None
        )
        items = listing if trailing is None else listing[:-1]
        commas = [
            position
            for position, index in enumerate(items)
            if tokens[index].string == ","
        ]
        read = [
            imported(
                items[low + 1 : high], items[high] if high < len(items) else trailing
            )
            for low, high in zip([-1, *commas], [*commas, len(items)], strict=True)
        ]
        names = [name for name in read if name is not None]
        if not names or len(names) != len(read):
            return None
        return FromImport(
            module=module,
            names=names,
            end=closing if parenthesized else names[-1].end,
        )

    return [
        found
        for index, token in enumerate(tokens)
        if (token.type, token.string) == (tokenize.NAME, "from")
        and (found := statement(index)) is not None
    ]


def respellings(
    tokens: list[tokenize.TokenInfo], moves: list[Relocation]
) -> list[Respelling]:
    """Every import statement one of the moves repoints, as it will read.

    A statement whose rewrite would take a comment with it is declined whole.
    The splices own only the paths and names they respell, and a comment is
    somebody's note about the code beside it — in practice the one such
    statement is a parenthesized list moving whole to a top-level module,
    which bare ``import`` cannot parenthesize. It stays for a human, named
    among the surviving mentions.
    """
    plain = [
        Respelling(
            splices=[
                Splice(
                    start=Point.before(tokens[run.start]),
                    end=Point.after(tokens[run.end]),
                    text=".".join(landed),
                )
            ],
            repointed=1,
        )
        for run in module_runs(tokens)
        if (landed := destination(run.named(tokens), moves)) is not None
    ]
    named = [
        respelled
        for statement in from_imports(tokens)
        if (respelled := statement.respelled(tokens, moves)) is not None
    ]
    comments = [
        Point.before(token) for token in tokens if token.type == tokenize.COMMENT
    ]
    return [
        respelling
        for respelling in [*plain, *named]
        if not any(
            splice.covers(comment)
            for splice in respelling.splices
            for comment in comments
        )
    ]


def apply_splices(text: str, splices: list[Splice]) -> str:
    """Write every splice into the source it was read from.

    Last first, so a splice's recorded points still address the text they
    were read off when two share a row. A splice across rows folds them into
    its first and leaves the others empty rather than gone, so every row
    number read before stays true while the rest are applied; a row a
    removal leaves blank — a name that stood alone on it inside parentheses —
    is dropped. Rows are read by the reader the tokenizer was given, which
    ends a line at a newline alone: ``splitlines`` also ends one at a form
    feed, and a row counted that way is a different line from the one the
    splice was read off.
    """
    lines = io.StringIO(text).readlines()
    for splice in sorted(
        splices,
        key=lambda splice: (splice.start.row, splice.start.column),
        reverse=True,
    ):
        first, last = splice.start.row - 1, splice.end.row - 1
        head = lines[first][: splice.start.column]
        tail = lines[last][splice.end.column :]
        lines[first : last + 1] = [
            f"{head}{splice.text}{tail}",
            *[""] * (last - first),
        ]
    touched = {splice.start.row - 1 for splice in splices}
    return "".join(
        line for row, line in enumerate(lines) if row not in touched or line.strip()
    )


def relocate_in_file(path: Path, moves: list[Relocation]) -> RelocationEdit | None:
    """Repoint every import in one file, or report that none named a mover."""
    text = path.read_text(encoding="utf-8")
    tokens = list(tokenize.generate_tokens(io.StringIO(text).readline))
    respelled = respellings(tokens, moves)
    if not respelled:
        return None
    splices = [splice for respelling in respelled for splice in respelling.splices]
    path.write_text(apply_splices(text, splices), encoding="utf-8")
    return RelocationEdit(
        path=path, imports=sum(respelling.repointed for respelling in respelled)
    )


def source_files(
    roots: list[Path], suffixes: Collection[str] = SOURCE_SUFFIXES
) -> list[Path]:
    """Every source file beneath the given roots, once each, in a stable order.

    Once each because the roots may nest: a package root a module path
    resolves against sits inside the sweep root that covers the whole
    workspace, and a file reached through both would be read, rewritten and
    reported twice — the second pass finding nothing, and the count telling a
    reader the file had two imports where it had one.
    """
    return sorted(
        {
            path
            for root in roots
            for path in root.rglob("*")
            if path.suffix in suffixes and "__pycache__" not in path.parts
        }
    )


class MovedModule(BaseModel, frozen=True):
    """One module file the relocation carried, and where it landed."""

    old: Path
    new: Path


def planned_move(roots: list[Path], move: Relocation) -> MovedModule | None:
    """Where the module's file is, and where its new name puts it, before either moves.

    ``None`` where no root holds the module, which is a caller who moved the
    file first and is repointing imports after.
    """

    def destination(source_root: Path) -> Path:
        """The root the new name belongs under, which need not be the old one.

        A module crossing packages — an application's becoming the library's —
        spells a top-level name another root already holds, and resolving its
        destination against the root it came from writes a second tree beside
        the first: `src/lup/devtools/...` next to the application, where
        `packages/lup/src/lup/` is the package that owns the name. The root
        declaring the new top-level name is the one that takes it, and a name
        no root declares stays where it was, which is every move within one
        package.

        Declaring it means holding it as a package, which is what the import
        the move rewrites resolves against. A root is also a sweep's, named
        wide so every importer is found — `packages/` holds a directory
        called `lup` that no import has ever reached, and matching the name
        alone would land the module in it.
        """
        return next(
            (root for root in roots if (root / move.new[0] / "__init__.py").is_file()),
            source_root,
        )

    return next(
        (
            MovedModule(
                old=source, new=destination(root).joinpath(*move.new).with_suffix(".py")
            )
            for root in roots
            if (source := root.joinpath(*move.old).with_suffix(".py")).is_file()
        ),
        None,
    )


def occupied(roots: list[Path], moves: list[Relocation]) -> list[MovedModule]:
    """Every move whose module is here to carry and whose destination is taken.

    Asked of every move before anything is touched, because the file and its
    importers are one relocation: a module that cannot land has no business
    having its imports repointed. Doing that anyway -- carrying nothing and
    rewriting everything -- aimed every importer at the module already there,
    and a caller who reads the moved-and-repointed report as done learns
    otherwise from a tree that resolves the old names against the wrong file.
    """
    return [
        plan
        for move in moves
        if (plan := planned_move(roots, move)) is not None and plan.new.exists()
    ]


def carry_module(roots: list[Path], move: Relocation) -> MovedModule | None:
    """Move the module's own file to the path its new name spells.

    The half a caller should never have been left holding. Repointing every
    importer and leaving the file where it was produces a tree where nothing
    resolves -- which the type check does catch, but only after this command
    reported success, so the failure arrives detached from what caused it.

    Moved through ``git`` where the file is tracked, so the history follows
    the module instead of reading as a delete beside an unrelated add. An
    untracked file is renamed plainly.

    Two cases are deliberately quiet here. A source that is not there is a
    caller doing the same relocation in the other order -- file first,
    imports after -- and refusing that would punish the tidier sequence. A
    destination that already exists is left alone, because overwriting one
    module with another is not a relocation; the command refuses that case
    whole, through :func:`occupied`, before this or any import rewrite runs.
    """

    def tracked(path: Path) -> bool:
        """Whether git is keeping this file's history, and can be asked to move it.

        A tree that is not a repository at all answers the same way a file git
        has never seen does — plainly rename it — so the exit code and the
        empty listing collapse into one answer here rather than becoming two
        branches at the call.
        """
        try:
            return bool(git.lines("-C", str(path.parent), "ls-files", "--", path.name))
        except sh.ErrorReturnCode:
            return False

    plan = planned_move(roots, move)
    if plan is None or plan.new.exists():
        return None
    plan.new.parent.mkdir(parents=True, exist_ok=True)
    if tracked(plan.old):
        # Asked of the repository holding the file, with both operands
        # resolved: a root is wherever a module path happens to resolve
        # against and need be no repository at all, so `-C <root>` with
        # operands spelled from the caller's directory reads each of them
        # twice — `packages/lup/src/packages/lup/src/...`, which git
        # reports as a bad source rather than as a path it built.
        top = git.out("-C", str(plan.old.parent), "rev-parse", "--show-toplevel")
        git.out("-C", top, "mv", str(plan.old.resolve()), str(plan.new.resolve()))
    else:
        plan.old.rename(plan.new)
    return plan


def relocate(
    roots: list[Path],
    moves: list[Relocation],
    suffixes: Collection[str] = SOURCE_SUFFIXES,
) -> list[RelocationEdit]:
    """Repoint every import beneath ``roots``, reporting each file changed."""
    edits = [relocate_in_file(path, moves) for path in source_files(roots, suffixes)]
    return [edit for edit in edits if edit is not None]


def surviving_mentions(
    roots: list[Path],
    moves: list[Relocation],
    suffixes: Collection[str] = SOURCE_SUFFIXES,
) -> list[str]:
    """Every remaining mention of a moved module, wherever it is not an import.

    Not necessarily wrong — prose about where something used to live is a
    legitimate thing to write — so this reports and the reader decides. A
    moved module still imported by name from its package is reported too,
    though it is always wrong: it is a statement the rewrite declined, and
    the one a search for the dotted path cannot find.
    """

    def mentions(path: Path) -> Iterator[str]:
        text = path.read_text(encoding="utf-8")
        declined = {number for number in submodule_imports(text, moves)}
        for number, line in enumerate(io.StringIO(text).readlines(), start=1):
            if number in declined or any(".".join(move.old) in line for move in moves):
                yield f"{path}:{number}: {line.strip()}"

    return [
        mention for path in source_files(roots, suffixes) for mention in mentions(path)
    ]


def submodule_imports(text: str, moves: list[Relocation]) -> list[int]:
    """The rows importing a moved module by name from the package it left.

    ``from package import submodule`` never writes the module's dotted path
    whole, so it is read by the grammar the rewrite reads it with: an
    imported name a move of its own carried away from its package. A source
    the tokenizer cannot read names nothing here, and its text mentions
    still are.
    """
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(text).readline))
    except (tokenize.TokenError, SyntaxError):
        return []
    return sorted(
        {
            Point.before(tokens[name.start]).row
            for statement in from_imports(tokens)
            for name in statement.carried(tokens, moves)
        }
    )
