"""Behavior tests for the Python source-reading commands."""

from pathlib import Path

from typer.testing import CliRunner

import lup.devtools.py.app as py_app_module
import pytest
from lup.devtools.py.app import app
from lup.devtools.py.search import scan_project_symbols
from lup.devtools.py.text import source_text_matches


runner = CliRunner()


def test_source_is_complete_by_default() -> None:
    result = runner.invoke(app, ["source", "lup.devtools.py.app"])
    assert result.exit_code == 0
    assert '@app.command("search")' in result.output


def test_source_reads_dispatcher_asset_without_importing_it() -> None:
    result = runner.invoke(app, ["source", "lup.policy.assets.decisions", "-n", "60"])
    assert result.exit_code == 0
    assert "from host import" in result.output


def test_source_text_search_keeps_complete_matching_lines(tmp_path: Path) -> None:
    source_root = tmp_path / "source"
    source_root.mkdir()
    source = source_root / "policy.py"
    complete_line = "assignment = " + "literal-value-" * 40
    source.write_text(f"before = 1\n{complete_line}\nafter = 2\n")
    matches = source_text_matches("literal-value", [source_root])

    assert len(matches) == 1
    assert matches[0].line_number == 2
    assert matches[0].text == complete_line


def test_prose_search_reads_docstrings_and_comments_only(tmp_path: Path) -> None:
    """The reading a prose rule judges by: sentences, not identifiers or data."""
    source = tmp_path / "prose.py"
    source.write_text(
        '"""A module that used to do more."""\n'
        'used_to = "used to"\n'
        "value = 1  # it used to be two\n"
    )

    matches = source_text_matches("used to", [source], prose=True)

    assert [match.line_number for match in matches] == [1, 3]
    assert matches[1].text == "value = 1  # it used to be two"


def test_text_command_reports_path_and_line(tmp_path: Path) -> None:
    source = tmp_path / "example.py"
    source.write_text("first = 1\nassignment = 'constant'\n")
    result = runner.invoke(app, ["text", "assignment", str(source)])

    assert result.exit_code == 0
    assert f"{source}:2:" in result.output
    assert "assignment = 'constant'" in result.output


def test_text_command_refuses_a_missing_path(tmp_path: Path) -> None:
    result = runner.invoke(app, ["text", "assignment", str(tmp_path / "missing")])
    assert result.exit_code == 1
    assert "Python source path does not exist" in result.output


def test_text_command_refuses_a_non_python_file(tmp_path: Path) -> None:
    source = tmp_path / "notes.md"
    source.write_text("assignment")
    result = runner.invoke(app, ["text", "assignment", str(source)])
    assert result.exit_code == 1
    assert "Source file is not Python" in result.output


def test_project_symbol_search_finds_package_members(tmp_path: Path) -> None:
    package = tmp_path / "src" / "demo"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    source = (
        "class Builder:\n"
        "    def build_client(self):\n"
        "        local_result = None\n"
        "        return local_result\n"
        "\n"
        "def query():\n"
        "    return None\n"
    )
    (package / "client.py").write_text(source)
    matches = scan_project_symbols(tmp_path, "build")
    paths = {match["import_path"] for match in matches}

    assert paths == {"demo.client.Builder", "demo.client.Builder.build_client"}
    assert all("local_result" not in match["import_path"] for match in matches)


def test_search_command_includes_project_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package = tmp_path / "src" / "demo"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (package / "client.py").write_text("def repository_only_symbol():\n    pass\n")
    monkeypatch.setattr(py_app_module, "find_nearest_pyproject", lambda: tmp_path)
    monkeypatch.setattr(py_app_module, "get_top_level_packages", lambda: [])
    result = runner.invoke(app, ["search", "repository_only"])

    assert result.exit_code == 0
    assert "demo.client.repository_only_symbol" in result.output
    assert "matches in project source and 0 packages" in result.output
