"""Code-intelligence registration compiled from its agent-facing catalog."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from lup.tools.mcp import ToolError
from lup.tools.lsp.tools import (
    CODEINTEL_TOOL_DECLARATIONS,
    PositionInput,
    create_codeintel_tools,
    pointed,
)


def test_registered_tools_match_their_declarations() -> None:
    registered = create_codeintel_tools(Path("pyright-langserver"), Path("."))

    assert [(tool.name, tool.description) for tool in registered] == [
        (declaration.name, declaration.description)
        for declaration in CODEINTEL_TOOL_DECLARATIONS
    ]


def source(tmp_path: Path) -> Path:
    """One file with a name at the margin and a name behind indentation."""
    written = tmp_path / "subject.py"
    written.write_text("class Held:\n    def kept(self) -> None: ...\n")
    return written


def test_a_column_on_the_name_is_answered(tmp_path: Path) -> None:
    """The guard passes the positions a caller meant, at either depth."""
    pointed(source(tmp_path), PositionInput(path="subject.py", line=1, column=6))
    pointed(source(tmp_path), PositionInput(path="subject.py", line=2, column=8))


def test_the_default_column_on_an_indented_line_is_refused(tmp_path: Path) -> None:
    """The mistake the default invites, and the one that would read as an answer.

    Column zero is the first character of the line, which is indentation for
    everything inside a block. Answered emptily, that reads as a symbol
    nobody references rather than as a position naming no symbol.
    """
    with pytest.raises(ToolError, match="whitespace"):
        pointed(source(tmp_path), PositionInput(path="subject.py", line=2))


def test_a_refusal_shows_the_line_it_could_not_find_a_symbol_on(
    tmp_path: Path,
) -> None:
    """Whole, so the caller counts the column instead of asking again."""
    with pytest.raises(ToolError, match="    def kept\\(self\\) -> None: \\.\\.\\."):
        pointed(source(tmp_path), PositionInput(path="subject.py", line=2))


def test_a_column_past_the_end_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ToolError, match="past the end"):
        pointed(source(tmp_path), PositionInput(path="subject.py", line=1, column=99))


def test_a_line_the_file_does_not_have_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ToolError, match="2 lines"):
        pointed(source(tmp_path), PositionInput(path="subject.py", line=9, column=0))


def test_a_field_no_position_carries_is_refused(tmp_path: Path) -> None:
    """A dropped argument is a question answered about somewhere else.

    `symbol` is the plausible spelling for what this input calls `column`,
    and ignoring it would leave the default column standing while the caller
    believes they named the symbol.
    """
    with pytest.raises(ValidationError):
        PositionInput.model_validate(
            {"path": "subject.py", "line": 2, "symbol": "kept"}
        )
