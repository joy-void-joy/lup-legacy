"""Real comment markers remain visible behind quoted marker examples."""

from pathlib import Path

import pytest

from lup.devtools.dev.questions import ReviewFile
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
