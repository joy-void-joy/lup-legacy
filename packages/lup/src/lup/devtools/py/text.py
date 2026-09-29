"""Literal text search over explicitly scoped Python source paths."""

from collections.abc import Iterable, Iterator
from pathlib import Path

from pydantic import BaseModel

from lup.policy.kernel.edit import prose_spans


class SourceTextMatch(BaseModel, frozen=True):
    """One complete matching source line and where it was found."""

    path: Path
    line_number: int
    text: str


def python_source_paths(roots: Iterable[Path]) -> list[Path]:
    """Enumerate Python files without descending into hidden or cache trees."""

    def selected_paths() -> Iterator[Path]:
        for root in roots:
            if not root.exists():
                raise FileNotFoundError(f"Python source path does not exist: {root}")
            if root.is_file():
                if root.suffix != ".py":
                    raise ValueError(f"Source file is not Python: {root}")
                yield root
                continue
            for directory, directories, files in root.walk():
                directories[:] = [
                    name
                    for name in directories
                    if not name.startswith(".") and name != "__pycache__"
                ]
                yield from (directory / name for name in files if name.endswith(".py"))

    return sorted(dict.fromkeys(selected_paths()))


def source_text_matches(
    pattern: str, roots: Iterable[Path], ignore_case: bool = False, prose: bool = False
) -> list[SourceTextMatch]:
    """Find a literal pattern in complete lines from the selected Python files.

    With ``prose``, only what a person reads as a sentence is searched — a
    docstring whole, a comment from where it opens — which is the reading
    the prose rules judge by, so a candidate for one is found the way the
    rule would find it rather than among identifiers and string data.
    """
    if not pattern or "\n" in pattern or "\r" in pattern:
        raise ValueError("pattern must be one non-empty line")
    needle = pattern.casefold() if ignore_case else pattern

    def matching(line: str) -> bool:
        haystack = line.casefold() if ignore_case else line
        return needle in haystack

    def searched(source: str) -> dict[int, str]:
        if not prose:
            return dict(enumerate(source.splitlines(), start=1))
        return {span["line"]: span["text"] for span in prose_spans(source)}

    def matches_in(path: Path) -> Iterator[SourceTextMatch]:
        source = path.read_text(encoding="utf-8")
        lines = source.splitlines()
        for line_number, text in searched(source).items():
            if matching(text):
                yield SourceTextMatch(
                    path=path, line_number=line_number, text=lines[line_number - 1]
                )

    return [match for path in python_source_paths(roots) for match in matches_in(path)]
