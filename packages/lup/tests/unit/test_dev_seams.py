"""Reading and writing what a project settled about itself.

A default nobody was shown is not a decision, and the seams shipped as
defaults nobody was shown: an initialization walked the customization markers
and never reached the declarations behind them. What is pinned here is the
half that makes putting them to a person possible — that a seam can be read
where it lives, and that an answer lands in the declaration at the depth the
declaration is written at rather than as a stranger spliced into the file.
"""

from pathlib import Path

import pytest

from lup.devtools.dev.seams import Answers, DeclarationSite, Seam, read_seam, survey

CATALOG = '''"""A project's declarations."""

from pathlib import Path


def declared() -> HookSet:
    return HookSet(
        rules=RuleSelection(retired=[]),
        human_owned_files=[Path("README.md")],
        path_roles=[
            # A comment somebody wrote to explain the choice.
            HookPathRole(root=Path("tests"), role="test"),
        ],
    )
'''


@pytest.fixture
def catalog(tmp_path: Path) -> Path:
    written = tmp_path / "catalog.py"
    written.write_text(CATALOG, encoding="utf-8")
    return written


def test_a_seam_is_read_where_it_is_written(catalog: Path) -> None:
    """Located by parsing, so a declaration is found rather than matched."""
    site = read_seam(catalog, "HookSet", "human_owned_files")

    assert isinstance(site, DeclarationSite)
    assert site.entries == ['Path("README.md")']
    assert site.paths() == ["README.md"]


def test_a_seam_reports_what_it_names_rather_than_the_comments_around_it(
    catalog: Path,
) -> None:
    """The source segment of a declaration worth reading aloud is mostly prose.

    A comment explaining a choice belongs in the file. What a person being
    asked about the seam needs is the entries.
    """
    lines = survey(catalog)

    assert any(
        'HookPathRole(root=Path("tests"), role="test")' in line for line in lines
    )
    assert not any("somebody wrote to explain" in line for line in lines)


def test_a_seam_nobody_wrote_down_says_so_and_refuses_to_be_edited(
    catalog: Path,
) -> None:
    """Two different facts, and only one of them is a value to change."""
    absent = read_seam(catalog, "HookSet", "protected_edit_roots")

    assert "left at the library's default" in absent.described("trees")
    with pytest.raises(ValueError, match="not written down"):
        absent.editable()


def test_an_answer_lands_at_the_depth_the_declaration_is_written_at(
    catalog: Path,
) -> None:
    """A literal spliced at a fixed indentation parses and reads as a stranger."""
    Answers(disown=["README.md"], own=["docs/design.md"]).settled(catalog, [])

    written = catalog.read_text(encoding="utf-8")
    assert "        human_owned_files=[\n" in written
    assert '            Path("docs/design.md"),\n' in written
    assert "        ]," in written
    assert "README.md" not in written


def test_retiring_every_rule_names_them_all_rather_than_saying_all(
    catalog: Path,
) -> None:
    """The selection is subtractive, so "all of them" is spelled as all of them.

    A project that drops the family and one that dropped them a denial at a
    time are the same project — and a rule the library adds later is one this
    declaration has visibly not answered for.
    """
    Answers(retire_all=True).settled(catalog, ["dict-get", "own-model-dispatch"])

    site = read_seam(catalog, "RuleSelection", "retired")
    assert isinstance(site, DeclarationSite)
    assert site.strings() == ["dict-get", "own-model-dispatch"]


def test_keeping_a_rule_takes_it_back_out(catalog: Path) -> None:
    Answers(retire=["dict-get", "own-model-dispatch"]).settled(catalog, [])
    Answers(keep=["dict-get"]).settled(catalog, [])

    site = read_seam(catalog, "RuleSelection", "retired")
    assert isinstance(site, DeclarationSite)
    assert site.strings() == ["own-model-dispatch"]


def test_the_file_still_parses_after_every_answer(catalog: Path) -> None:
    """The property a splice has to keep, whatever else it gets right."""
    Answers(own=["a.md"], retire=["dict-get"]).settled(catalog, [])
    Answers(disown=["README.md"], retire_all=True).settled(catalog, ["x", "y"])

    compile(catalog.read_text(encoding="utf-8"), str(catalog), "exec")


def test_a_project_declaring_no_catalog_is_told_rather_than_reported_empty() -> None:
    """An empty survey would read as "you have decided nothing"."""
    assert "declares no catalog path" in survey(None)[0]


def test_a_seam_whose_module_is_gone_says_which_and_where_it_is_declared(
    catalog: Path,
) -> None:
    """What a renamed package leaves: a seam spelled at the old path.

    The survey shows every seam at once, so one whose file moved is a finding
    beside the rest rather than a traceback that hides them all — and writing
    into it is refused with the same words, naming the seam's own catalog.
    """
    moved = Seam(
        call="Image",
        keyword="tooling",
        summary="programs the image carries",
        module=Path("src/renamed_away/harness/content/image.py"),
    )

    lines = survey(catalog, [moved])
    value = moved.read(catalog)

    assert len(lines) == 1
    assert "src/renamed_away/harness/content/image.py does not exist" in lines[0]
    assert f"declared in {catalog}" in lines[0]
    with pytest.raises(ValueError, match="does not exist"):
        value.editable()
