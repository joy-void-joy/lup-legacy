"""What `version bump` records, checked through the option wiring itself.

`test_changelog.py` pins the document model. These go through Typer, because
the faults worth pinning are not in the rendering — they are an option being
a scalar that keeps only the last `--detail`, and that one string being split
on its own commas afterwards. Both live between the command line and the
model, which is where a test has to stand to see them.

Every changelog case runs `--dry-run`: the entry is rendered and shown
without a file being written, a commit made, or a tag created. What a bump
does write is checked against the writer itself, which is the whole of what
it changes in the manifest.
"""

from pathlib import Path

import pytest
import tomlkit
from typer.testing import CliRunner

from lup.devtools.version import app, write_agent_version

runner = CliRunner()

MANIFEST = """\
[project]
name = "example"  # the name this repository publishes under

# Read by lup rather than by the packaging tools.
[tool.lup]
agent_version = "1.2.3"
"""
"""A manifest carrying exactly what rewriting rather than editing would lose."""


def bullets(output: str) -> list[str]:
    """The detail lines of a rendered entry, as a reader would count them."""
    return [line for line in output.splitlines() if line.startswith("- ")]


def dry_bump(*arguments: str) -> str:
    """A bump that shows what it would record and writes nothing."""
    result = runner.invoke(app, ["bump", "patch", *arguments, "--dry-run"])
    assert result.exit_code == 0, result.output
    return result.output


def test_every_detail_reaches_the_entry() -> None:
    """A scalar option kept only the last of these three."""
    output = dry_bump("A summary", "-d", "first", "-d", "second", "-d", "third")

    assert bullets(output) == ["- first", "- second", "- third"]


def test_a_detail_holding_commas_stays_one_bullet() -> None:
    """One sentence stays one bullet, whatever commas it holds."""
    prose = "the settings — the sandbox, the roots, the ceiling — ride a transform"
    output = dry_bump("A summary", "--detail", prose)

    assert bullets(output) == [f"- {prose}"]


def test_the_summary_is_recorded_above_its_details() -> None:
    """A bullet list with no sentence over it is not an entry."""
    output = dry_bump("Govern each session by the fields that bound it", "-d", "one")

    assert "Govern each session by the fields that bound it" in output


def test_a_detail_without_a_summary_is_refused() -> None:
    """Silently dropping it would be the same class of fault as losing a detail."""
    result = runner.invoke(app, ["bump", "patch", "--detail", "orphan", "--dry-run"])

    assert result.exit_code == 1
    assert "summary" in result.output


def test_a_bump_naming_no_summary_records_nothing() -> None:
    """The version alone is a fact the manifest already carries."""
    output = dry_bump()

    assert bullets(output) == []


def test_no_tag_is_reported_rather_than_assumed() -> None:
    """A dry run has to say which of the two it would do."""
    assert "Would not tag" in dry_bump("A summary", "--no-tag")
    assert "Would tag" in dry_bump("A summary")


def test_a_bump_rewrites_only_the_version(tmp_path: Path) -> None:
    """Everything a reviewer wrote around the value survives changing it.

    A bump that reformatted the manifest would put a diff nobody authored in
    front of every reviewer of every release, with the three characters that
    are the release somewhere inside it — and the comments explaining why a
    key is there are the first thing such a rewrite drops.
    """
    manifest = tmp_path / "pyproject.toml"
    manifest.write_text(MANIFEST, encoding="utf-8")

    write_agent_version(manifest, "2.0.0")

    written = manifest.read_text(encoding="utf-8")
    assert written == MANIFEST.replace('"1.2.3"', '"2.0.0"')
    assert tomlkit.parse(written)["tool"]["lup"]["agent_version"] == "2.0.0"


def test_a_manifest_that_declares_no_agent_version_is_refused(tmp_path: Path) -> None:
    """A project that never adopted the version is told, not given a table."""
    manifest = tmp_path / "pyproject.toml"
    manifest.write_text('[project]\nname = "example"\n', encoding="utf-8")

    with pytest.raises((KeyError, TypeError)):
        write_agent_version(manifest, "2.0.0")

    assert manifest.read_text(encoding="utf-8") == '[project]\nname = "example"\n'
