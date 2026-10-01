"""A directory tree rendered from the tree, and captioned by the tree.

A hand-written layout diagram is a claim about the filesystem that nothing
checks, and it decays the way every uncheckable claim does: it goes on
describing directories under the application package for as long as it takes
somebody to notice that a split moved them into the library and added others
it never mentions. Every name in it exists somewhere, so even a basename
check passes.

Nothing here is declared. The structure is walked, and each caption is the
module's own docstring — a package's from its ``__init__.py``, a module's from
itself. A separate table of descriptions would be one more copy to fall behind
the code, which is the defect a walk avoids rather than a smaller version of it;
this way a module that is renamed, moved, or re-described changes the diagram
by being edited, and one that is deleted leaves it by being deleted.

The docstring is read with :func:`ast.get_docstring` rather than off the first
line, because a module may open with a suppression directive or a licence
header and still have a docstring beneath it.
"""

import ast
from collections.abc import Iterator
from pathlib import Path

from pydantic import BaseModel

DEFAULT_SKIPPED_SUFFIXES = (".pyc", ".egg-info")
"""Build residue, which is present in a checkout but is not part of it."""

DEFAULT_SKIPPED_NAMES = ("__pycache__", "__init__.py")
"""Entries every reader can infer, so listing them spends width on nothing.

``__init__.py`` is here because its docstring is already doing a job — it
captions the directory it sits in — and printing the file as well would say
the same sentence twice at two indents.
"""

DEFAULT_NOTE_COLUMN = 28
"""Where a caption starts, so they read as a column rather than a ragged edge.

A default rather than a constant: how wide the names run is a fact about the
tree being drawn, and a project whose paths are longer should be able to say
so at the call rather than fork the renderer.
"""


def summary(source: Path) -> str | None:
    """A Python file's opening docstring paragraph, as one line.

    The paragraph rather than its first line, because a summary long enough to
    wrap is still one sentence and cutting it at the wrap ends a caption
    mid-clause. Returns ``None`` for a file that cannot be parsed as well as for
    one with no docstring: a caption is a courtesy, and a syntax error is a
    problem for the checks that exist to report it rather than for a diagram to
    raise on.
    """
    try:
        parsed = ast.parse(source.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError, OSError):
        return None
    documented = ast.get_docstring(parsed)
    if documented is None:
        return None
    # lup: ignore[string-split] — a docstring is prose, and the blank line
    # between its summary and its body is a convention no parser owns
    opening = documented.strip().split("\n\n")[0]
    return " ".join(opening.split())


class TopLevelEntry(BaseModel, frozen=True):
    """One importable entry beneath a package, and the file that defines it.

    Both halves come from one walk. The walk has to open the layout anyway to
    decide an entry is importable at all, so it already holds the file, and
    carrying it costs nothing; a caller handed the name alone has to rebuild
    that path from the name, which is a second reading of the same layout and
    answers about a file it never saw.
    """

    name: str
    source: Path


def top_level_entries(
    root: Path,
    subtree: str,
    skipped_names: tuple[str, ...] = DEFAULT_SKIPPED_NAMES,
) -> list[TopLevelEntry]:
    """Every importable entry directly beneath ``subtree``, with its source file.

    A page that lists what a tree contains is making a claim about the tree,
    and prose cannot check it — a roster promising "every remaining top-level
    entry" omits six of them and reads as complete. Walking the entries
    here lets the page hold its own authored description of each one while
    the *set* it describes is the tree's to decide.

    What makes an entry importable is what supplies its source: a module is
    its own file, and a package is the ``__init__.py`` whose presence is the
    reason the directory counts. One test decides both, so there is no filter
    for a second reader to agree with.

    A dotted entry is skipped whatever it holds. Python cannot import one, so
    it is never a name a roster could owe a row — and the tree is read from
    the filesystem rather than from git, which means anything a tool leaves
    beside the source is visible here. A checkout carrying `.claude` or
    `.venv` under the library would otherwise fail generation asking what an
    editor's scratch directory solves.

    A directory holding no ``__init__.py`` is skipped for that same reason: it
    is not a package, so no import name reaches it. Deleting a package leaves
    its directory standing wherever an untracked file still sits inside it, and
    a roster asking what that directory solves would be asking about something
    Python cannot import.
    """
    base = root / subtree
    if not base.is_dir():
        raise ValueError(f"no directory at {subtree!r} beneath {root} to enumerate")

    def importable() -> Iterator[TopLevelEntry]:
        """Each surviving candidate, paired with the file that makes it one."""
        for entry in base.iterdir():
            if (
                entry.name.startswith(".")
                or entry.name in skipped_names
                or entry.name.endswith(DEFAULT_SKIPPED_SUFFIXES)
            ):
                continue
            initializer = entry / "__init__.py"
            if initializer.is_file():
                yield TopLevelEntry(name=entry.name, source=initializer)
                continue
            if entry.is_file() and entry.suffix == ".py":
                yield TopLevelEntry(name=entry.stem, source=entry)

    return sorted(importable(), key=lambda entry: entry.name)


def annotated_tree(
    root: Path,
    subtree: str,
    *,
    note_column: int = DEFAULT_NOTE_COLUMN,
    skipped_names: tuple[str, ...] = DEFAULT_SKIPPED_NAMES,
    skipped_suffixes: tuple[str, ...] = DEFAULT_SKIPPED_SUFFIXES,
) -> str:
    """Render ``subtree`` beneath ``root`` as a self-captioning ASCII tree."""

    def caption(entry: Path) -> str | None:
        """What this entry says about itself — a package through its init."""
        if entry.is_dir():
            initializer = entry / "__init__.py"
            return summary(initializer) if initializer.exists() else None
        return summary(entry) if entry.suffix == ".py" else None

    def labelled(stem: str, entry: Path) -> str:
        """One rendered line: its branch, its name, and what it says it is."""
        label = f"{stem}{entry.name}/" if entry.is_dir() else f"{stem}{entry.name}"
        described = caption(entry)
        if described is None:
            return label
        return f"{label:<{note_column}} # {described}"

    def shown(entry: Path) -> bool:
        """Whether this entry is part of the layout, rather than beside it.

        The tree is read from the filesystem, so everything a tool leaves in
        the checkout is visible here — and a diagram of what a reader would
        import has no room for any of it. A dotted entry is skipped because
        Python cannot import one; a directory holding no source anywhere
        beneath it is skipped because what sits there is bytecode rather than
        modules, and nothing a reader imports.
        """
        if entry.name.startswith("."):
            return False
        if entry.name in skipped_names or entry.name.endswith(skipped_suffixes):
            return False
        if entry.is_dir():
            return any(entry.rglob("*.py"))
        return entry.suffix == ".py"

    def children(directory: Path) -> list[Path]:
        listed = sorted(directory.iterdir(), key=lambda entry: entry.name)
        return [entry for entry in listed if shown(entry)]

    def render(directory: Path, prefix: str) -> list[str]:
        lines: list[str] = []  # lup: ignore[empty-collection] — tree fold
        entries = children(directory)
        for index, entry in enumerate(entries):
            last = index == len(entries) - 1
            lines.append(labelled(f"{prefix}{'└── ' if last else '├── '}", entry))
            if entry.is_dir():
                lines.extend(render(entry, f"{prefix}{'    ' if last else '│   '}"))
        return lines

    base = root / subtree
    if not base.is_dir():
        raise ValueError(
            f"no directory at {subtree!r} beneath {root}, so there is no tree to draw"
        )
    return "\n".join([f"{subtree}/", *render(base, "")])
