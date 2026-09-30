"""Real comment markers remain visible behind quoted marker examples."""

from pathlib import Path

import pytest

from lup.devtools.review.app import ReviewFile
from lup.harness.codescan.markers import ScanMode, find_feedback
from lup.policy.review import ReviewedFile


@pytest.mark.parametrize(
    ("filename", "source"),
    [
        (
            "sample.py",
            'message = "# lup: ignore[fake]"  # lup: ignore[real] — actual reason\n',
        ),
        ("sample.py", '"# lup: ignore[fake]"  # lup: ignore[real] — actual reason\n'),
        (
            "sample.py",
            'message = "` # lup: ignore[fake]"  # lup: ignore[real] — actual reason\n',
        ),
        (
            "sample.ts",
            'const message = "// lup: ignore[fake]"; // lup: ignore[real] — actual reason\n',
        ),
        (
            "sample.ts",
            "const message = `// lup: ignore[fake]`; // lup: ignore[real] — actual reason\n",
        ),
        ("sample.ts", "`// lup: ignore[fake]`; // lup: ignore[real] — actual reason\n"),
    ],
)
def test_inline_directive_uses_lexical_site_and_keeps_its_own_reason(
    filename: str, source: str
) -> None:
    comment = "//" if filename.endswith(".ts") else "#"
    after = source + f"{comment} unrelated following comment\n"
    shown = ReviewFile.of(ReviewedFile(path=Path(filename), before="", after=after))

    assert len(shown.suppressions) == 1
    directive = shown.suppressions[0]
    assert directive.line == 1
    assert directive.rule_ids == ["real"]
    assert directive.reason == "— actual reason"
    assert directive.introduced
    assert shown.after == after
    assert [
        line.new_line for hunk in shown.hunks for line in hunk.lines if line.suppression
    ] == [1]


@pytest.mark.parametrize(
    ("filename", "source"),
    [
        ("sample.py", '"""# lup: ignore[fake]\nstill a docstring\n"""\n'),
        (
            "sample.ts",
            "const message = `first line\n// lup: ignore[fake]\nlast line`;\n",
        ),
        ("sample.ts", "const expression = /\\/\\/ lup: ignore[fake]/;\n"),
    ],
)
def test_literal_directives_are_never_presented_as_exceptions(
    filename: str, source: str
) -> None:
    shown = ReviewFile.of(ReviewedFile(path=Path(filename), before="", after=source))

    assert shown.suppressions == []
    assert not any(line.suppression for hunk in shown.hunks for line in hunk.lines)


@pytest.mark.parametrize(
    ("filename", "comment"), [("sample.py", "#"), ("sample.ts", "//")]
)
def test_standalone_directive_keeps_the_complete_continuation_reason(
    filename: str, comment: str
) -> None:
    after = f"{comment} lup: ignore[real] — reason starts\n{comment} and continues here\nvalue = 1\n"
    shown = ReviewFile.of(ReviewedFile(path=Path(filename), before="", after=after))

    assert len(shown.suppressions) == 1
    assert shown.suppressions[0].reason == "— reason starts and continues here"


@pytest.mark.parametrize(
    ("source", "mode"),
    [
        ('message = "# lup: quoted note"  # lup: actual note\n', ScanMode.PYTHON),
        ('message = "` # lup: quoted note"  # lup: actual note\n', ScanMode.PYTHON),
        ('const message = "// lup: quoted note"; // lup: actual note\n', ScanMode.JS),
        ("const message = `// lup: quoted note`; // lup: actual note\n", ScanMode.JS),
    ],
)
def test_feedback_uses_real_comment_after_an_earlier_literal(
    source: str, mode: str
) -> None:
    notes = find_feedback(source, mode)

    assert len(notes) == 1
    assert notes[0].text == "actual note"


def test_multiline_template_string_does_not_create_feedback() -> None:
    source = "const example = `first line\n// lup: quoted note\nlast line`;\n// lup: actual note\n"

    notes = find_feedback(source, ScanMode.JS)

    assert len(notes) == 1
    assert notes[0].start_line == 4
    assert notes[0].text == "actual note"


def test_every_marker_kind_is_located_and_classified_on_its_side() -> None:
    before = "value = 1  # lup: defer: parked before the change\n"
    after = (
        "# lup: open feedback\n"
        "# lup: defer: parked\n"
        "# lup: defer[until the v2 API ships]: gated\n"
        "# lup: solved: answered\n"
        "# lup: template: choose\n"
        "value = []  # lup: ignore[empty-collection] — a fold\n"
    )

    shown = ReviewFile.of(
        ReviewedFile(path=Path("sample.py"), before=before, after=after)
    )

    assert [
        (marker.side, marker.line, marker.kind, marker.condition)
        for marker in shown.markers
    ] == [
        ("before", 1, "defer", None),
        ("after", 1, "note", None),
        ("after", 2, "defer", None),
        ("after", 3, "defer", "until the v2 API ships"),
        ("after", 4, "solved", None),
        ("after", 5, "template", None),
        ("after", 6, "ignore", None),
    ]


@pytest.mark.parametrize(
    ("before", "after", "bounds"),
    [
        (
            "".join(f"line {number}\n" for number in range(1, 21)),
            "".join(
                "line ten\n" if number == 10 else f"line {number}\n"
                for number in range(1, 21)
            ),
            (7, 13, 7, 13),
        ),
        ("one\ntwo\n", "one\ntwo\nthree\n", (1, 2, 1, 3)),
        (None, "one\n", (1, 0, 1, 1)),
    ],
    ids=["changed-line", "appended", "created"],
)
def test_hunks_say_where_they_stand_so_the_whole_file_can_be_shown_around_them(
    before: str | None, after: str, bounds: tuple[int, int, int, int]
) -> None:
    shown = ReviewFile.of(
        ReviewedFile(path=Path("notes.txt"), before=before, after=after)
    )

    (hunk,) = shown.hunks
    assert (hunk.old_start, hunk.old_end, hunk.new_start, hunk.new_end) == bounds
