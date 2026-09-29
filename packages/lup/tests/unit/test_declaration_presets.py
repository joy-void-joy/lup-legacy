"""A declaration laid over another: a preset stating only what it changes.

A named kind of session is a preset: a ``Claude(...)`` or ``Codex(...)``
stating only its differences, laid over the project's declaration, so what it
left unstated stays the project's and a nested declaration is laid field by
field.
"""

from pathlib import Path

import pytest
from pydantic import ValidationError

from lup.launch.declaration import Member, Recording, laid_over
from lup.providers.claude import Claude


def test_a_preset_replaces_only_what_it_states() -> None:
    project = Claude(model="opus", system_prompt="the project's", max_turns=3)

    moded = laid_over(Claude(permission_mode="auto"), project)

    assert moded.permission_mode == "auto"
    assert moded.model == "opus"
    assert moded.system_prompt == "the project's"


def test_a_nested_declaration_is_laid_field_by_field() -> None:
    """A mode moving its record's root keeps the ledger the project records to."""
    project = Claude(record=Recording(transcript=True, mode="free"))

    moded = laid_over(Claude(record=Recording(root=Path("notes/sessions"))), project)

    assert moded.record == Recording(
        transcript=True, mode="free", root=Path("notes/sessions")
    )


def test_a_list_a_preset_states_is_the_whole_list() -> None:
    project = Claude(identity=Member(name="work"), add_dirs=[Path("/a")])

    moded = laid_over(Claude(add_dirs=[Path("/b")]), project)

    assert moded.add_dirs == [Path("/b")]


def test_a_combination_neither_side_refused_alone_is_refused_where_they_meet() -> None:
    with pytest.raises(ValidationError, match="does not take effort"):
        laid_over(Claude(effort="max"), Claude(model="haiku"))
