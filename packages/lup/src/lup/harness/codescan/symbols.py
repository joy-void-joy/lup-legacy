"""What a Python source defines, by qualified name and where.

A merge asks a question line-level diffing cannot answer: did this join lose
something one side had built? Line presence is the wrong instrument for it in
both directions — renaming a variable makes untouched lines read as missing,
while a deleted function whose body lines happen to recur elsewhere reads as
kept. A definition is the unit the question is actually about.

Names are qualified by the scope that holds them, so a method removed from
one class is not excused by a same-named method on another. The line comes
with the name because whoever is told a definition went missing has to find
it, and the tree it went missing from no longer holds it.
"""

import ast

from pydantic import BaseModel


class DefinedSymbol(BaseModel, frozen=True):
    """One name a source defines, and the line that defines it."""

    name: str
    """Qualified by the scopes that hold it, as ``Outer.inner``."""

    line: int

    bases: list[str] = []
    """The classes a class definition names as its bases, each by its own name.

    Empty for everything but a class. Carried because a member a class
    stopped declaring is still the class's where a base declares it: moved up
    to a parent every subclass shares is not gone, and only the bases say so.
    """

    reachable: bool = True
    """Whether an importer can name it, or only the function holding it can.

    A definition inside a function body is that function's own — a command
    wired onto an app, a helper nested in its single caller — and no importer
    reaches it however it is spelled. The merge-loss check wants it all the
    same, because losing one in a join is still a loss; a surface an adopter
    holds wants only what an adopter could have held.
    """


def symbols_under(node: ast.AST, prefix: str, local: bool) -> list[DefinedSymbol]:
    """Every definition beneath one node, qualified by the scope it sits in."""
    return [
        found
        for child in ast.iter_child_nodes(node)
        for found in symbols_of(child, prefix, local)
    ]


def symbols_of(node: ast.AST, prefix: str, local: bool) -> list[DefinedSymbol]:
    """One node's own definition, if it is one, and everything beneath it.

    ``local`` says the walk is inside a function body, where a bound name is
    a working variable rather than a definition. Recording those would put
    every renamed local on the lost list — the exact noise that makes a
    line-presence pass unreadable, arriving by a different route.

    ``type X = ...`` sits with the assignments rather than with the classes,
    because that is what it is: one name bound at module scope, with nothing
    beneath it to walk into. It parses to its own node rather than to an
    ``Assign``, which is the whole of why it goes missing — and missing
    silently, in the one direction this module exists to make loud. A
    ``type`` alias invisible to the loss check means a
    merge that drops one reports having lost nothing, while the older
    ``Name = Literal[...]`` spelling of the same idea is tracked. Two ways
    to write one declaration, one of them checked.
    """
    match node:
        case (
            ast.FunctionDef(name=name, lineno=line)
            | ast.AsyncFunctionDef(name=name, lineno=line)
        ):
            qualified = f"{prefix}{name}"
            return [
                DefinedSymbol(name=qualified, line=line, reachable=not local),
                *symbols_under(node, f"{qualified}.", True),
            ]
        case ast.ClassDef(name=name, lineno=line, bases=bases):
            qualified = f"{prefix}{name}"
            return [
                DefinedSymbol(
                    name=qualified,
                    line=line,
                    reachable=not local,
                    bases=[named for base in bases if (named := base_name(base))],
                ),
                *symbols_under(node, f"{qualified}.", local),
            ]
        case (
            ast.Assign(targets=[ast.Name(id=name)], lineno=line)
            | ast.AnnAssign(target=ast.Name(id=name), lineno=line)
            | ast.TypeAlias(name=ast.Name(id=name), lineno=line)
        ) if not local:
            return [DefinedSymbol(name=f"{prefix}{name}", line=line, reachable=True)]
    return symbols_under(node, prefix, local)


def base_name(base: ast.expr) -> str | None:
    """The name a class statement gives one base: ``Base``, ``module.Base``, ``Base[T]``.

    Its own name alone, since that is what the class is declared under
    wherever it is imported from; a base built by an expression names none.
    """
    match base:
        case ast.Name(id=name) | ast.Attribute(attr=name):
            return name
        case ast.Subscript(value=value):
            return base_name(value)
        case _:
            return None


def defined_symbols(source: str) -> list[DefinedSymbol]:
    """Every function, class, and bound name this source defines.

    Unparseable text defines nothing rather than raising: this reads whatever
    a commit happens to hold, including a file that is not Python at all and
    a revision from before a syntax error was fixed.
    """
    try:
        return symbols_under(ast.parse(source), "", False)
    except (SyntaxError, ValueError):
        return []


def symbols_lost(before: str, after: str) -> list[DefinedSymbol]:
    """Definitions ``before`` holds that ``after`` no longer does."""
    held = {symbol.name for symbol in defined_symbols(after)}
    return [symbol for symbol in defined_symbols(before) if symbol.name not in held]
