"""The changelog section a release candidate leaves open, and how it closes.

A candidate names the version it is heading for without being it, so its
section is headed by that version and lists every candidate cut toward it,
and stays open: a fix found in a candidate lands under ``## Unreleased`` like
any other, and the next candidate folds it in. Promotion closes the section
as it stood in the candidate being promoted — work that landed after it is
not in the release, and stays open above.
"""

import datetime as dt

import pytest

from lup.devtools.changelog import (
    Candidate,
    Changelog,
    candidate_heading,
    release_heading,
)

FIRST = dt.date(2026, 9, 28)
SECOND = dt.date(2026, 10, 1)
FINAL = dt.date(2026, 10, 5)

OPEN = """# Changelog

## Unreleased

### A capability arrived

It does a thing.

## 0.4.0 — 2026-09-22

- older
"""


def first_candidate(asks: list[str] | None = None) -> Changelog:
    """The document as 0.5.0rc1 left it."""
    return Changelog.parse(OPEN).with_candidate(
        "0.5.0", Candidate(version="0.5.0rc1", date=FIRST), asks or []
    )


def test_a_candidate_heads_the_section_with_its_target_and_lists_itself() -> None:
    rendered = first_candidate().render()

    assert candidate_heading("0.5.0") in rendered
    assert "Candidates: 0.5.0rc1 (2026-09-28)." in rendered
    assert "### A capability arrived" in rendered
    assert "## Unreleased" not in rendered
    assert rendered.index(candidate_heading("0.5.0")) < rendered.index("## 0.4.0")


def test_the_candidate_section_is_neither_a_release_nor_the_preamble() -> None:
    """Read back, it is the open candidate — not a release, not lost above one."""
    reread = Changelog.parse(first_candidate().render())

    assert reread.candidate is not None
    assert reread.candidate.target == "0.5.0"
    assert [c.version for c in reread.candidate.candidates] == ["0.5.0rc1"]
    assert [section.version for section in reread.sections] == ["0.4.0"]
    assert "A capability arrived" not in reread.preamble


def test_a_document_holding_an_open_candidate_round_trips() -> None:
    text = (
        "# Changelog\n\n## Unreleased\n\n- a fix\n\n"
        f"{candidate_heading('0.5.0')}\n\n"
        "Candidates: 0.5.0rc1 (2026-09-28).\n\n- a feature\n\n"
        "## 0.4.0 — 2026-09-22\n\n- older\n"
    )

    assert Changelog.parse(text).render() == text


def test_a_later_candidate_folds_what_landed_since_in_and_lists_itself() -> None:
    fixed = Changelog.parse(
        first_candidate(["gone — a reason"])
        .render()
        .replace("# Changelog\n\n", "# Changelog\n\n## Unreleased\n\n### A fix\n\n", 1)
    )

    second = fixed.with_candidate(
        "0.5.0",
        Candidate(version="0.5.0rc2", date=SECOND),
        ["gone — a reason", "later — another reason"],
    )
    rendered = second.render()

    assert "## Unreleased" not in rendered
    assert "Candidates: 0.5.0rc1 (2026-09-28), 0.5.0rc2 (2026-10-01)." in rendered
    assert rendered.index("### A capability arrived") < rendered.index("### A fix")
    # Rendered from every break still pending, so the second candidate's list
    # replaces the first's rather than repeating it beneath.
    assert rendered.count("### What this release asks of a caller") == 1
    assert rendered.count("- gone — a reason") == 1
    assert rendered.index("### A fix") < rendered.index("- later — another reason")


def test_relevelling_retitles_the_section_and_keeps_its_history() -> None:
    relevelled = Changelog.parse(first_candidate().render()).with_candidate(
        "0.6.0", Candidate(version="0.6.0rc1", date=SECOND), []
    )
    rendered = relevelled.render()

    assert candidate_heading("0.6.0") in rendered
    assert candidate_heading("0.5.0") not in rendered
    assert "Candidates: 0.5.0rc1 (2026-09-28), 0.6.0rc1 (2026-10-01)." in rendered


def test_promotion_closes_the_candidate_section_and_leaves_later_work_open() -> None:
    """What landed after the candidate is not in the release it becomes."""
    later = Changelog.parse(
        first_candidate()
        .render()
        .replace(
            "# Changelog\n\n", "# Changelog\n\n## Unreleased\n\n- after rc1\n\n", 1
        )
    )

    promoted = later.promoted("0.5.0", FINAL)
    rendered = promoted.render()

    assert promoted.candidate is None
    assert [section.version for section in promoted.sections] == ["0.5.0", "0.4.0"]
    assert release_heading("0.5.0", FINAL) in rendered
    assert "Candidates: 0.5.0rc1 (2026-09-28)." in promoted.sections[0].text
    assert "after rc1" not in promoted.sections[0].text
    assert rendered.index("## Unreleased") < rendered.index("- after rc1")
    assert rendered.index("- after rc1") < rendered.index(
        release_heading("0.5.0", FINAL)
    )


def test_a_direct_release_closes_the_candidate_section_with_everything_open() -> None:
    later = Changelog.parse(
        first_candidate(["gone — a reason"])
        .render()
        .replace(
            "# Changelog\n\n", "# Changelog\n\n## Unreleased\n\n- after rc1\n\n", 1
        )
    )

    released = later.released_as("0.5.0", FINAL, ["gone — a reason"])
    closed = released.sections[0]

    assert released.candidate is None
    assert released.unreleased == ""
    assert closed.version == "0.5.0"
    assert "Candidates: 0.5.0rc1 (2026-09-28)." in closed.text
    assert "### A capability arrived" in closed.text
    assert "- after rc1" in closed.text
    assert closed.text.count("- gone — a reason") == 1


def test_a_closed_section_ends_with_a_blank_line_before_the_next() -> None:
    """The asks list used to run straight into the next release's heading."""
    released = Changelog.parse(OPEN).released_as("0.5.0", FINAL, ["gone — a reason"])

    assert "- gone — a reason\n\n## 0.4.0" in released.render()


def test_promoting_with_no_candidate_section_records_only_itself() -> None:
    """A document nobody kept a candidate section in still gets its release."""
    promoted = Changelog.parse(OPEN).promoted("0.5.0", FINAL)

    assert promoted.sections[0].text == f"{release_heading('0.5.0', FINAL)}\n\n"
    assert "### A capability arrived" in promoted.unreleased


def test_an_unreadable_candidate_list_is_refused_rather_than_dropped() -> None:
    text = (
        f"# Changelog\n\n{candidate_heading('0.5.0')}\n\n"
        "Candidates: soon, maybe.\n\n- a feature\n"
    )

    with pytest.raises(ValueError, match="Candidates"):
        Changelog.parse(text)
