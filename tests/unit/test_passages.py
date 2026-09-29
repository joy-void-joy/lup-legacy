"""Prose lives beside its declaration, and its values enter escaped.

What this replaced: prose inside an r-string in Python with every path,
count and description spliced in by f-string. The claim now is narrow and
checkable — a passage names values, a value is a part, and a part spells
itself — so the tests below are that claim put to the shapes that broke it.
"""

from pathlib import Path

import pytest

import lup.harness.models as models
from lup.formats.markdown import ProseCode, ProseStrong
from lup.harness.passages import (
    passage_path,
    passage_sections,
    passage_text,
    prose_without_logic,
    rendered,
)
from lup.providers.harness import claude_prompt_renderer


def test_a_passage_is_the_markdown_beside_its_module() -> None:
    """One file apart, so neither half of a declaration hides from the other."""
    beside = passage_path("lup.harness.content.skills.commit")

    assert beside.name == "commit.passage.md"
    assert beside.parent.name == "skills"
    assert beside.read_text().startswith("# Create Commits")


def test_a_module_keeps_every_passage_it_composes_in_one_file() -> None:
    """The subject is one file, and the marker is its table of contents.

    A module composing several documents marks them off rather than scattering
    them: what its declaration places by naming nothing stands at the top, and
    each further passage is named for what it holds.
    """
    beside = passage_path("lup.harness.content.skills.commit")
    held = passage_sections(beside, beside.read_text(encoding="utf-8"))

    assert [section.name for section in held] == ["", "examples"]
    assert held[0].text.startswith("# Create Commits")
    assert passage_text("lup.harness.content.skills.commit", "examples") == held[1].text
    assert held[1].text.lstrip("\n").startswith("### Examples")


def test_a_marker_inside_a_fence_divides_nothing() -> None:
    """The division comes from the parser, so a code sample stays a sample."""
    written = Path("somewhere.passage.md")
    fenced = f"Shown below.\n\n```\n{'<!-- passage: sample -->'}\n```\n"

    assert [section.name for section in passage_sections(written, fenced)] == [""]


def test_a_section_that_ended_on_a_word_is_read_back_without_a_newline() -> None:
    """A marker costs the section above it a newline, and gives it back.

    Some passages end mid sentence on purpose, joined to the part that follows
    them, so the newline a marker needs cannot be left on the section it was
    taken from.
    """
    written = Path("somewhere.passage.md")
    file = "ends on a word\n<!-- passage: after -->\nand the rest\n"

    assert [section.text for section in passage_sections(written, file)] == [
        "ends on a word",
        "and the rest\n",
    ]


def test_one_name_marks_one_passage() -> None:
    """A file naming a passage twice has no answer for which one is meant."""
    written = Path("somewhere.passage.md")
    twice = "open\n<!-- passage: same -->\none\n<!-- passage: same -->\ntwo\n"

    with pytest.raises(ValueError, match="marks `same` twice"):
        passage_sections(written, twice)


def test_prose_that_holds_logic_is_refused() -> None:
    """A passage names values and nothing else.

    What varies by more than a value is two passages, or a declaration in
    Python where it is typed and reviewed — never a statement inside prose,
    where what the document says would depend on state no reviewer sees.
    """
    written = Path("somewhere.passage.md")

    for logical in ("{% if user %}hello{% endif %}\n", "{# a note #}\n"):
        with pytest.raises(ValueError, match="names values and nothing else"):
            prose_without_logic(written, logical)

    assert (
        prose_without_logic(written, "Plain {{ value }}.\n") == "Plain {{ value }}.\n"
    )


def test_a_name_the_context_never_carried_fails_where_it_is_written() -> None:
    """StrictUndefined: a misspelled value is a failure, never a blank.

    Generation runs inside `dev check`, so the name that never reached the
    context is reported there rather than leaving a hole in a shipped prompt.
    """
    with pytest.raises(Exception, match="undefined"):
        rendered("lup.harness.content.skills.report", "", {})


def test_a_value_enters_escaped_and_cannot_break_its_line() -> None:
    """The whole point: a part spells itself, and a line stays one line."""
    renderer = claude_prompt_renderer()
    hostile = "first line\nsecond line"

    assert models.plain(hostile).spell(renderer) == "first line second line"
    assert models.code(hostile).spell(renderer) == "`first line second line`"


def test_a_bullet_list_escapes_both_halves_of_every_line() -> None:
    """A roster is derived, so each item is a node rather than text."""
    listed = models.BulletList(
        items=[
            models.BulletItem(
                lead=ProseCode(text="trace-explorer"), text="Reads\ntraces"
            ),
            models.BulletItem(lead=ProseStrong(text="Open notes"), text="Still asking"),
        ]
    )

    assert listed.text_payload == (
        "- `trace-explorer` — Reads traces\n- **Open notes** — Still asking\n"
    )


def test_a_passage_shows_the_parts_it_names_to_every_walk() -> None:
    """A skill invoked inside a sentence is invoked.

    The walks that ask a document's parts what they name — which plugin,
    which agent, whether the arguments are reached — have to see the values a
    passage holds, or writing one inside prose would hide it from all of them.
    """
    passage = models.Passage(
        module="lup.harness.content.skills.commit",
        values={"arguments": models.ArgumentsRef()},
    )
    document = models.PromptDocument(parts=[passage])

    assert any(part.references_arguments for part in document.walked())
    assert not any(part.references_arguments for part in document.parts)


def test_a_value_handed_over_and_never_placed_is_refused() -> None:
    """The mirror of a name placed and never handed over.

    A value is a part, and whatever a part declares counts as said by the
    declaration holding it — an invocation nobody renders still ties one
    module to another, which is exactly what a declined module tripped over.
    """
    with pytest.raises(ValueError, match=r"is handed \['stray'\]"):
        rendered("lup.harness.content.skills.commit", "", {"stray": "anything"})
