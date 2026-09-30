"""Reasoning effort, named on one ladder and taken by each runtime as named.

The application's settings name an effort as a
:data:`~lup.types.SessionEffort` and hand it to whichever runtime
the session opens on. The failure this guards against is silent: a rung one
runtime took under another name, or narrowed to one it had, would run the
session at a value only discoverable by reading the provider call. Each rung
is pinned here because a collapse of one rung into another is exactly the
substitution that would otherwise go unnoticed.
"""

from pathlib import Path
from typing import get_args

import pytest

from lup.providers.claude import Claude
from lup.providers.claude.models import ClaudeEffort
from lup.providers.codex import Codex
from lup.providers.codex.models import CodexEffort
from lup.types import SessionEffort

EVERY_DEGREE: list[SessionEffort] = list(get_args(SessionEffort.__value__))


def test_a_session_naming_no_effort_takes_the_models_default() -> None:
    """Absence is the model's default, not whatever a settings file says."""
    assert Claude(cwd=Path(".")).resolved_effort() == "xhigh"
    assert Codex(cwd=Path(".")).resolved_effort() == "xhigh"


def test_the_default_is_clamped_to_what_the_model_takes() -> None:
    """``claude-opus-4-6`` stops at ``max`` with no ``xhigh``; haiku takes none."""
    assert Claude(model="claude-opus-4-6").resolved_effort() == "high"
    assert Claude(model="haiku").resolved_effort() is None


@pytest.mark.parametrize("degree", EVERY_DEGREE)
def test_every_degree_reaches_both_runtimes_under_its_own_name(
    degree: SessionEffort,
) -> None:
    """Both catalogs list every rung of the ladder, so neither may reinterpret one."""
    assert Claude(effort=degree).effort == degree
    assert Codex(cwd=Path("."), effort=degree).effort == degree


def test_the_ladder_is_the_rungs_both_catalogs_share() -> None:
    """A rung only one runtime lists would be narrowed on the other in silence."""
    shared = set(get_args(ClaudeEffort.__value__)) & set(
        get_args(CodexEffort.__value__)
    )
    assert set(EVERY_DEGREE) == shared


def test_nothing_below_low_is_offered() -> None:
    """``none`` and ``minimal`` left Codex's catalog; neither may linger here."""
    assert "none" not in EVERY_DEGREE
    assert "minimal" not in EVERY_DEGREE


def test_ultra_is_the_top_of_the_ladder() -> None:
    """Asking for the ceiling has to land on a ceiling, not near one."""
    assert EVERY_DEGREE[-1] == "ultra"
    assert Claude(effort="ultra").effort == "ultra"
    assert Codex(cwd=Path("."), effort="ultra").effort == "ultra"
