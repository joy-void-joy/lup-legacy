"""What identity buys the always-loaded document, and what it must not cost.

The document is a run of named sections across two packages, where a
hand-ordered splice of constants would render the same and leave nothing able
to name a piece of it. What these pin is that naming the pieces leaves the
output as it is — the same bytes reach a session — and that a name is worth
something, because the same algebra that retires a skill retires a section.
"""

import pytest

import lup.harness.models as models
from lup.harness.content import conventions
from lup.harness.codescan.common import RuleSelection
from lup.providers.harness import claude_prompt_renderer
from lup.seams import Selection
from lup_template.harness.content.catalog import GUIDANCE, GUIDANCE_SECTIONS

SECTIONS = GUIDANCE_SECTIONS
"""This repository's document, resolved against the modules it takes."""


def test_every_section_is_named_once() -> None:
    """Two sections under one id is the ambiguity a selection cannot survive.

    Retiring an id that names two stretches of the document would take out
    whichever the resolution reached, and which that is would depend on the
    order the composition happened to build.
    """
    ids = [section.id for section in SECTIONS]

    assert sorted(ids) == sorted(set(ids))


def test_the_parts_are_the_sections_read_end_to_end() -> None:
    """Identity is a layer over the document, never a second copy of it.

    Every part, the last one included. How many blank lines the document ends
    with is not a section's to decide and is not decided here either: the
    renderer answers it, being the one reader that sees a whole document
    whatever kinds of part composed it. So this splice is exact.
    """
    spliced = [part for section in SECTIONS for part in section.parts]

    assert GUIDANCE.parts == spliced


def test_the_document_ends_in_one_newline_whoever_closes_it() -> None:
    """Which section lands last is a module selection, not a formatting choice.

    A project declining the subject that happened to close the document must
    not thereby produce a malformed artifact, so the normalisation is asserted
    against a document whose final section is one that ends in a blank line.
    Read off the rendering rather than off the parts, because a section whose
    words are a passage carries no text a trim between them could reach.
    """
    doubled = models.GuidanceSection(
        id="doubled", chapter="meta", parts=[models.TextPart(text="Closing.\n\n")]
    )

    document = models.PromptDocument(parts=models.sectioned([doubled]))

    assert claude_prompt_renderer().render(document) == "Closing.\n"


def test_a_retired_section_leaves_the_document() -> None:
    """The point of the name: a project declines a section it does not want.

    Nothing declines one today — every section here is this repository's own
    answer — so what is pinned is that the seat works, against the algebra
    every other table in the repository already resolves through.
    """
    resolved = Selection(retired=["configuration"]).over(SECTIONS)

    assert "configuration" not in [section.id for section in resolved]
    assert len(resolved) == len(SECTIONS) - 1


def test_a_declared_section_replaces_the_one_of_its_id_in_place() -> None:
    """A project rewriting one section says so where it would have added it."""
    mine = models.GuidanceSection(
        id="configuration",
        chapter="tooling",
        parts=[models.TextPart(text="## Configuration\n\nSomewhere else.\n")],
    )

    resolved = Selection(overrides=[mine]).over(SECTIONS)

    assert [section for section in resolved if section.id == "configuration"] == [mine]
    assert len(resolved) == len(SECTIONS)


def test_a_sections_text_is_the_prose_it_carries() -> None:
    """What a weigher reads, without knowing which kinds of part hold words."""
    section = models.GuidanceSection(
        id="worked-example",
        chapter="code",
        parts=[
            models.TextPart(text="before "),
            models.SkillInvocation(plugin="lup", skill="commit"),
            models.TextPart(text=" after"),
        ],
    )

    assert section.text == "before  after"


@pytest.mark.parametrize("retired", [[], ["own-model-dispatch"]])
def test_the_built_section_answers_to_its_id_whatever_it_says(
    retired: list[str],
) -> None:
    """The one section whose contents depend on the reading project's rules.

    Its prose changes with the selection and its name does not, which is what
    lets a project retire or replace it by the same id as every other.
    """
    built = conventions.design_principles(RuleSelection(retired=retired))

    assert built.id == "design-principles"
