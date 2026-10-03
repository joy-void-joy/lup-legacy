"""Two sides' changelogs merged entry by entry, as the changelog's merge driver merges them.

Every branch adds its own entry at the top of ``## Unreleased``, so any two
meet on the same lines. A line-based merge lines their text up: git's union
keeps a line both entries share once, under the second, and runs the first
entry's last line into the second's heading. Merged as whole entries under
their headings, each side's entry stays whole and set off, and only an
entry both sides changed differently is left for somebody to resolve.
"""

from lup.devtools.changelog import merged_changelog

RELEASED = "## 1.0.0 — 2026-01-01\n\n- First.\n"
SHARED = "What changes for a session: retry it once."


def entry(heading: str, body: str) -> str:
    """One entry as an author writes it, set off from whatever follows."""
    return f"### {heading}\n\n{body}\n\n"


def changelog(*entries: str, released: str = RELEASED) -> str:
    """A changelog whose open section holds *entries*, above *released*."""
    return "# Changelog\n\n## Unreleased\n\n" + "".join(entries) + released


OLDER = entry("An older entry", "It was already here.")
BASE = changelog(OLDER)


def test_two_entries_added_at_one_place_both_stay_whole_and_set_off() -> None:
    ours = entry("Ours", f"Our change.\n\n{SHARED}")
    theirs = entry("Theirs", f"Their change.\n\n{SHARED}")

    merged = merged_changelog(BASE, changelog(ours, OLDER), changelog(theirs, OLDER))

    assert not merged.conflicted
    assert merged.text == changelog(theirs, ours, OLDER)


def test_a_document_nobody_changed_merges_to_itself() -> None:
    document = changelog(entry("Ours", "Our change."), OLDER)

    assert merged_changelog(document, document, document).text == document


def test_an_entry_run_into_the_next_heading_is_set_off_again() -> None:
    joined = changelog("### Ours\n\nOur change.\n", OLDER)
    theirs = changelog(entry("Theirs", "Their change."), OLDER)

    merged = merged_changelog(BASE, joined, theirs)

    assert merged.text == changelog(
        entry("Theirs", "Their change."), entry("Ours", "Our change."), OLDER
    )


def test_an_edit_on_one_side_and_an_entry_on_the_other_are_both_kept() -> None:
    clearer = entry("An older entry", "It was already here, and is clearer.")

    merged = merged_changelog(
        BASE, changelog(clearer), changelog(entry("Theirs", "Their change."), OLDER)
    )

    assert not merged.conflicted
    assert merged.text == changelog(entry("Theirs", "Their change."), clearer)


def test_one_entry_both_sides_changed_differently_is_left_marked() -> None:
    merged = merged_changelog(
        BASE,
        changelog(entry("An older entry", "Ours says this.")),
        changelog(entry("An older entry", "Theirs says that.")),
    )

    assert merged.conflicted
    assert merged.text == changelog(
        "<<<<<<< ours\n### An older entry\n\nOurs says this.\n=======\n"
        "### An older entry\n\nTheirs says that.\n>>>>>>> theirs\n\n"
    )


def test_an_entry_on_one_side_survives_a_release_on_the_other() -> None:
    release = f"## 1.1.0 — 2026-02-01\n\n{OLDER}{RELEASED}"
    theirs = entry("Theirs", "Their change.")

    merged = merged_changelog(
        BASE, f"# Changelog\n\n{release}", changelog(theirs, OLDER)
    )

    assert not merged.conflicted
    assert merged.text == changelog(theirs, released=release)


def test_a_heading_quoted_in_a_fenced_example_does_not_split_an_entry() -> None:
    fenced = entry("Ours", "An example:\n\n```markdown\n### Not a heading\n```")
    theirs = entry("Theirs", "Their change.")

    merged = merged_changelog(BASE, changelog(fenced, OLDER), changelog(theirs, OLDER))

    assert not merged.conflicted
    assert merged.text == changelog(theirs, fenced, OLDER)


def test_two_entries_one_side_gave_the_same_heading_are_both_kept() -> None:
    first = entry("Same", "First of two.")
    second = entry("Same", "Second of two.")
    theirs = entry("Theirs", "Their change.")

    merged = merged_changelog(
        BASE, changelog(second, first, OLDER), changelog(theirs, OLDER)
    )

    assert merged.text == changelog(theirs, second, first, OLDER)


def test_a_release_run_into_the_next_heading_is_set_off_again() -> None:
    joined = f"{RELEASED}## 0.9.0 — 2025-12-01\n\n- Earlier.\n"
    ours = entry("Ours", "Our change.")
    theirs = entry("Theirs", "Their change.")

    merged = merged_changelog(
        changelog(OLDER, released=joined),
        changelog(ours, OLDER, released=joined),
        changelog(theirs, OLDER, released=joined),
    )

    assert merged.text == changelog(
        theirs,
        ours,
        OLDER,
        released=f"{RELEASED}\n## 0.9.0 — 2025-12-01\n\n- Earlier.\n",
    )
