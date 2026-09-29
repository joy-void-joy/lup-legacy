"""Refusing a tool per session, declared on the agent and rendered by its runtime.

Three fields on a declaration read alike and do different things, which is
the whole reason this one is worth pinning. ``tools`` is the roster a session
is given, its built-ins by preset; ``allowed_tools`` is auto-approval within
that roster and its own SDK docs say it restricts nothing; ``disallowed_tools``
is the one the SDK documents as removal — "removed from the model's context
and cannot be used, even if they would otherwise be allowed" — and it is not
confined to built-ins.

Without it a caller wanting "everything except this one" had to enumerate the
complement, which is a roster that has to be restated every time the tool set
grows and silently re-admits whatever was added. The failure being guarded
here is the same shape as the effort field's: a value that reaches nothing
and a session that runs wider than it asked to, with nothing raised.
"""

from pathlib import Path

import pytest
from pydantic import ValidationError

from lup.providers.claude import Claude, ClaudeTools
from lup.providers.claude.runtime import build_claude_options
from lup.providers.codex import Codex


def test_a_declaration_naming_no_refusal_leaves_claude_blocking_nothing() -> None:
    """Absence has to stay absent, or every session gains a block list."""
    assert Claude(cwd=Path(".")).disallowed_tools == []


def test_a_refusal_reaches_the_provider_call() -> None:
    """Declaring it is half the trip; the SDK options are the rest."""
    options = build_claude_options(
        Claude(cwd=Path("."), disallowed_tools=["Bash"]),
        servers={},
        binding=lambda: None,
        resume=None,
        session_id=None,
    )

    assert options.disallowed_tools == ["Bash"]


def test_a_refusal_names_a_tool_no_roster_mentions() -> None:
    """The point of a block list is naming what a roster never enumerated."""
    config = Claude(
        cwd=Path("."),
        tools=ClaudeTools(builtin="web"),
        disallowed_tools=["mcp__research__research"],
    )

    assert config.tools.roster() == ["WebFetch", "WebSearch"]
    assert config.disallowed_tools == ["mcp__research__research"]


def test_the_three_tool_fields_stay_independent() -> None:
    """They read alike, so a rendering that conflated two would look correct."""
    config = Claude(
        cwd=Path("."),
        tools=ClaudeTools(builtin="web"),
        allowed_tools=["WebFetch"],
        disallowed_tools=["WebSearch"],
    )

    assert config.tools.roster() == ["WebFetch", "WebSearch"]
    assert config.allowed_tools == ["WebFetch"]
    assert config.disallowed_tools == ["WebSearch"]


def test_codex_has_no_refusal_it_could_apply_per_session() -> None:
    """Its dispatcher is per harness tree, so honouring this would over-apply it.

    Refused where it is declared, naming every field at once, so a caller
    does not fix two fields in two attempts.
    """
    with pytest.raises(ValidationError) as refusal:
        Codex.model_validate(
            {"cwd": ".", "allowed_tools": ["Read"], "disallowed_tools": ["Bash"]}
        )

    for field in ("allowed_tools", "disallowed_tools"):
        assert field in str(refusal.value)
