"""What `dev modules` counts: each module as this project resolves it.

The table exists to show what a project decided about each module, and its
first total is what that decision costs the always-loaded document. Summed
from the modules as they declare themselves, it reported prose a project had
retired as still carried — every section of `core` gone from the document,
and all of its bytes still on the row — while leaving out the sections the
project wrote itself. These pin the table to what the composition renders,
and the roster's offer to what the modules declare.
"""

from pathlib import Path

import pytest

import lup.harness.models as models
from lup.devtools.dev.modules import report, rows
from lup.harness.modules import (
    Adoption,
    DocumentEntry,
    Module,
    ModuleEntry,
    ModuleSelection,
    ModuleSpec,
    adopted,
    composed_guidance,
)
from lup.seams import Selection


def section(identity: str, words: str) -> models.GuidanceSection:
    """One guidance section carrying the words that make its weight."""
    return models.GuidanceSection(
        id=identity, chapter="code", parts=[models.TextPart(text=words)]
    )


def skill(identity: str, name: str) -> models.Skill:
    """One skill, spelled as briefly as a declaration can be."""
    return models.Skill(
        id=identity,
        name=name,
        description="A worked-example skill.",
        prompt=models.PromptDocument(
            source=__name__, parts=[models.TextPart(text="Do the thing.")]
        ),
    )


def page(semantic_id: str) -> DocumentEntry:
    """One published page, which nothing here renders."""
    return DocumentEntry(
        semantic_id=semantic_id,
        source=f"worked_example.docs.{semantic_id}",
        build=lambda _: models.Document(
            path=Path("docs") / f"{semantic_id}.md",
            semantic_id=semantic_id,
            source="tmp/worked_example.py",
            document=models.PromptDocument(
                source=__name__, parts=[models.TextPart(text="A page.\n")]
            ),
        ),
    )


def weight(sections: list[models.GuidanceSection]) -> int:
    """What these sections cost a session, the way the document is weighed."""
    return sum(models.document_byte_size(one.text) for one in sections)


CORE = Module(
    spec=ModuleSpec(id="core", title="Core", summary="Always there.", default_on=True),
    content=models.ContentRoster(
        skills=[skill("skill.debug", "debug"), skill("skill.report", "report")]
    ),
    guidance=[
        section("gates", "## Gates\nEvery edit is checked where it lands.\n"),
        section("principles", "## Principles\nCompile, do not emit.\n"),
    ],
    documents=[page("docs.rules")],
)

QUIET = Module(
    spec=ModuleSpec(id="quiet", title="Quiet", summary="Taken for its code."),
    guidance=[section("quiet-prose", "## Quiet\nA paragraph nobody loads.\n")],
)

EMPTY = Module(
    spec=ModuleSpec(
        id="empty", title="Empty", summary="Declares no prose.", default_on=True
    ),
)

ENTRIES = [
    ModuleEntry(spec=CORE.spec, build=lambda: CORE),
    ModuleEntry(spec=QUIET.spec, build=lambda: QUIET),
    ModuleEntry(spec=EMPTY.spec, build=lambda: EMPTY),
]


def test_a_module_whose_every_section_is_retired_carries_none_of_them(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The adopter's case: `core`'s prose retired whole, and still reported."""
    selection = ModuleSelection(
        adoptions=[
            Adoption(
                module="core",
                guidance=Selection(retired=["gates", "principles"]),
            )
        ]
    )

    [row] = rows([CORE], selection)
    report([CORE], selection, verbose=False)
    printed = capsys.readouterr().out

    assert row.guidance_resolved == 0
    assert row.guidance_declared == weight(CORE.guidance)
    assert "this document carries 0 prose bytes" in printed
    assert f"(module declares {weight(CORE.guidance)}b)" in printed
    assert f"the roster's own sections come to {weight(CORE.guidance)}" in printed


def test_what_the_table_says_is_carried_is_what_the_document_is_composed_from(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Retired, rewritten, added and kept quiet, all against one composition.

    The total is read off the printed report and checked against the
    document the composition assembles from the same selection, so the
    claim the command makes is held to the thing it claims to describe.
    """
    rewritten = section("gates", "## Gates\nChecked.\n")
    added = section("house-rules", "## House rules\nThis project's own paragraph.\n")
    selection = ModuleSelection(
        adoptions=[
            Adoption(
                module="core",
                guidance=Selection(retired=["principles"], overrides=[rewritten]),
            ),
            Adoption(module="quiet", taken=True, loads_guidance=False),
            Adoption(module="empty", guidance=Selection(overrides=[added])),
        ]
    )
    document = composed_guidance(adopted(ENTRIES, selection), selection)

    report([CORE, QUIET, EMPTY], selection, verbose=False)
    printed = capsys.readouterr().out

    assert [one.id for one in document] == ["gates", "house-rules"]
    assert f"this document carries {weight(document)} prose bytes" in printed
    offered = weight([*CORE.guidance, *QUIET.guidance, *EMPTY.guidance])
    assert f"the roster's own sections come to {offered}" in printed


def test_the_prose_column_names_the_modules_own_only_where_they_differ() -> None:
    """Unchanged prose is one figure; changed prose is both, and why."""
    added = section("house-rules", "## House rules\nThis project's own paragraph.\n")
    selection = ModuleSelection(
        adoptions=[
            Adoption(module="empty", guidance=Selection(overrides=[added])),
            Adoption(module="quiet", taken=True, loads_guidance=False),
        ]
    )

    listed = {row.identity: row for row in rows([CORE, QUIET, EMPTY], selection)}

    assert listed["core"].prose() == f"{weight(CORE.guidance):5d}b"
    assert listed["empty"].prose() == (f"{weight([added]):5d}b  (module declares none)")
    assert listed["quiet"].prose() == f"{weight(QUIET.guidance):5d}b  (not loaded)"


def test_what_a_module_ships_is_counted_after_the_selection_narrows_it() -> None:
    """A retired skill and a declined page are not among what a project ships."""
    selection = ModuleSelection(
        adoptions=[
            Adoption(
                module="core",
                content=models.ContentSelection(retired=["skill.report"]),
                documents=Selection(retired=["docs.rules"]),
            )
        ]
    )

    [row] = rows([CORE], selection)

    assert (row.skills, row.agents, row.pages) == (1, 0, 0)
    assert row.surfaces() == "1 skill(s)"
