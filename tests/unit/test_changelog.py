"""A changelog read and written as releases rather than as text.

Details taken as one comma-separated string split a single sentence whose
prose contains commas into bullets, and that sentence publishes as four:

    - agent: the Claude-only settings — the bash sandbox
    - the roots read outside cwd
    - the transport ceiling — ride a ConfigTransform stacked after rendering
    - so the portable request stays one both runtimes accept

That is one sentence with three em-dash clauses. Nothing fails, nothing
warns, and the damage is only visible to somebody reading the rendered file
afterwards — which is the shape of failure worth pinning tests around.
"""

import datetime as dt
from pathlib import Path

from lup.devtools.changelog import Changelog, ReleaseNote

DAY = dt.date(2026, 8, 19)


def note(
    version: str = "1.0.0",
    summary: str = "A summary",
    details: list[str] | None = None,
) -> ReleaseNote:
    """A release note with everything but the field under test defaulted."""
    return ReleaseNote(
        version=version, date=DAY, summary=summary, details=details or []
    )


def bullets(rendered: str) -> list[str]:
    """The detail lines of a rendered note, as a reader would count them."""
    return [line for line in rendered.splitlines() if line.startswith("- ")]


def test_a_detail_containing_commas_stays_one_bullet() -> None:
    """Prose punctuation never decides the bullet count."""
    prose = "the settings — the sandbox, the roots, the ceiling — ride a transform"

    assert bullets(note(details=[prose]).render()) == [f"- {prose}"]


def test_every_detail_survives_rather_than_the_last() -> None:
    """A scalar option keeps only the final `-d`; a list keeps them all."""
    rendered = note(details=["first", "second", "third"]).render()

    assert bullets(rendered) == ["- first", "- second", "- third"]


def test_a_note_round_trips_through_the_document() -> None:
    """Render and read are inverses, or a bump writes what it cannot find."""
    document = Changelog().with_note(note("7.2.0", details=["one", "two"]))
    reparsed = Changelog.parse(document.render())

    assert [section.version for section in reparsed.sections] == ["7.2.0"]
    assert reparsed.sections[0].date == DAY


def test_the_newest_release_is_written_on_top() -> None:
    """A changelog reads newest first, so an appended entry would read as oldest."""
    document = Changelog().with_note(note("1.0.0")).with_note(note("1.1.0"))

    assert [section.version for section in document.sections] == ["1.1.0", "1.0.0"]


def test_bumping_a_version_twice_leaves_one_section() -> None:
    """Re-running a bump after an amended summary must not double the version."""
    document = (
        Changelog()
        .with_note(note("1.0.0", summary="First attempt"))
        .with_note(note("1.0.0", summary="Corrected"))
    )

    assert [section.version for section in document.sections] == ["1.0.0"]
    assert "Corrected" in document.render()
    assert "First attempt" not in document.render()


def test_an_existing_release_passes_through_untouched() -> None:
    """Only the note being written is modelled; the rest is carried verbatim."""
    existing = (
        "# Changelog\n\n"
        "## v1.0.0 (2026-01-01)\n\n"
        "Something\n"
        "- with | a | table\n"
        "- and [a link](https://example.com)\n\n"
    )
    written = Changelog.parse(existing).with_note(note("1.1.0")).render()

    assert "- with | a | table\n" in written
    assert "- and [a link](https://example.com)\n" in written


def test_the_preamble_survives_a_bump() -> None:
    """Whatever opens the document is not a release and must not be rewritten."""
    existing = "# Changelog\n\nA hand-written note about this file.\n\n"
    written = Changelog.parse(existing).with_note(note("1.0.0")).render()

    assert written.startswith(existing)


def test_a_heading_inside_a_fenced_block_is_not_a_release() -> None:
    """A scanner would split here and write the next release into the example."""
    existing = (
        "# Changelog\n\n"
        "## v2.0.0 (2026-02-02)\n\n"
        "Shows the format\n\n"
        "```\n"
        "## v1.0.0 (2020-01-01)\n"
        "```\n\n"
    )
    document = Changelog.parse(existing)

    assert [section.version for section in document.sections] == ["2.0.0"]


def test_a_prose_heading_is_not_read_as_a_release() -> None:
    """A document's own sections must survive being read for versions."""
    document = Changelog.parse("# Changelog\n\n## Conventions\n\nProse.\n")

    assert document.sections == []
    assert "## Conventions" in document.render()


def test_the_document_answers_when_each_release_was_written() -> None:
    """`dates()` is what a report joins a version's scores against."""
    existing = (
        "# Changelog\n\n"
        "## v2.0.0 (2026-02-02)\n\nTwo\n\n"
        "## v1.0.0 (2026-01-01)\n\nOne\n\n"
    )

    assert Changelog.parse(existing).dates() == {
        "2.0.0": dt.date(2026, 2, 2),
        "1.0.0": dt.date(2026, 1, 1),
    }


def test_a_document_that_does_not_exist_yet_reads_as_an_empty_one(
    tmp_path: Path,
) -> None:
    """A first bump creates the file rather than failing to find it."""
    document = Changelog.read(tmp_path / "CHANGELOG.md")

    assert document.sections == []
    assert document.render().startswith("# Changelog")
