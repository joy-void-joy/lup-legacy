"""A session's model and effort, compiled for each CLI and refused where wrong.

The catalog is only worth its types where it is consulted: a tier resolves to
the lineup's model, ``ultra`` becomes each CLI's own spelling of it, and an
effort a model does not take is refused where the session is declared — never
dropped by the CLI, never narrowed to a rung the model has.
"""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from lup.providers.claude.launch import claude_arguments, claude_settings
from lup.coordination.identity import LaunchedMember
from lup.harness.models import HookSandbox, HookSet
from lup.providers.claude.confinement import CLAUDE_SANDBOX_OFF
from lup.providers.claude.model_choice import (
    claude_default_effort,
    claude_effort,
    claude_effort_named,
    claude_model_choice,
    claude_model_id,
    claude_model_name,
)
from lup.launch.declaration import InnerSandbox, OuterContainer
from lup.providers.claude import Claude
from lup.providers.claude.models import ClaudeModel
from lup.providers.claude.runtime import build_claude_options
from lup.providers.codex.model_choice import (
    codex_default_effort,
    codex_effort_arguments,
    codex_effort_named,
    codex_model_choice,
    codex_model_id,
)
from lup.providers.codex import Codex
from lup.providers.codex.subagents import CodexModelTiers
from lup.types import CustomModel

MEMBER = LaunchedMember(member_id="member-1", cli_name="work")


SESSION = "18f5debf-499a-42bb-8856-0b39dd59943d"


def test_a_tier_compiles_to_each_lineups_model() -> None:
    assert claude_model_id("frontier") == "fable"
    assert claude_model_id("strongest") == "opus"
    assert claude_model_id("inherit") is None
    assert claude_model_name("fast") == "haiku"

    tiers = CodexModelTiers()
    assert codex_model_id("frontier", tiers) == "gpt-6-astra"
    assert codex_model_id("strongest", tiers) == "gpt-5.6-sol"
    assert codex_model_id("balanced", tiers) == "gpt-5.6-terra"
    assert codex_model_id("fast", tiers) == "gpt-6-luna"
    assert codex_model_id("inherit", tiers) is None


def test_a_replaced_tier_table_is_the_one_a_session_resolves() -> None:
    endpoint = CodexModelTiers(strongest=CustomModel(id="served-large"))
    config = Codex(model="strongest", model_tiers=endpoint, cwd=Path("."))

    assert config.model_id() == "served-large"
    assert config.model_selection() == {"model": "served-large", "effort": "xhigh"}


def test_a_custom_id_reaches_the_cli_unchanged() -> None:
    assert claude_model_id(CustomModel(id="local-model")) == "local-model"
    assert codex_model_id(CustomModel(id="local"), CodexModelTiers()) == "local"
    assert Claude(model=CustomModel(id="x")).model_id() == "x"


def test_a_misspelt_model_is_refused_where_it_is_written() -> None:
    with pytest.raises(ValidationError):
        Claude.model_validate({"model": "claude-opsu"})
    with pytest.raises(ValidationError):
        Codex.model_validate({"model": "gpt-6-astr", "cwd": "."})


def test_ultra_compiles_to_xhigh_with_ultracode_for_the_sdk() -> None:
    options = build_claude_options(
        Claude(model="opus", effort="ultra"),
        servers={},
        binding=lambda: None,
        resume=None,
        session_id=SESSION,
    )

    assert options.effort == "xhigh"
    assert options.settings is not None
    assert json.loads(options.settings) == {
        "sandbox": {"enabled": False},
        "ultracode": True,
    }


def test_every_other_effort_reaches_the_sdk_unchanged_and_alone() -> None:
    for effort in ("low", "medium", "high", "xhigh", "max"):
        options = build_claude_options(
            Claude(model="opus", effort=effort),
            servers={},
            binding=lambda: None,
            resume=None,
            session_id=SESSION,
        )
        assert options.effort == effort
        assert options.settings is not None
        assert json.loads(options.settings) == {"sandbox": {"enabled": False}}


@pytest.mark.parametrize(
    ("model", "expected"), [("opus", "xhigh"), ("claude-opus-4-6", "high")]
)
def test_an_unnamed_effort_reaches_the_sdk_as_the_models_default(
    model: ClaudeModel, expected: str
) -> None:
    options = build_claude_options(
        Claude(model=model),
        servers={},
        binding=lambda: None,
        resume=None,
        session_id=SESSION,
    )

    assert options.effort == expected


def test_an_ultra_session_keeps_its_sandbox_in_the_same_settings() -> None:
    """The sandbox and the effort's switch travel in one settings document."""
    options = build_claude_options(
        Claude(model="opus", effort="ultra", sandbox=InnerSandbox()),
        servers={},
        binding=lambda: None,
        resume=None,
        session_id=SESSION,
    )

    assert options.settings is not None
    settings = json.loads(options.settings)
    assert settings["ultracode"] is True
    assert settings["sandbox"]["enabled"] is True


def test_ultra_on_the_command_line_is_one_settings_document() -> None:
    """The CLI reads one ``--settings``; a second would replace the first."""
    compiled = claude_effort("ultra")
    agent = Claude(
        model="opus",
        effort="ultra",
        policy=HookSet(id="hooks.probe", policy_ids=[], sandbox=HookSandbox()),
        sandbox=OuterContainer(),
    )
    arguments = claude_arguments(agent, MEMBER, None, [])

    assert compiled.arguments() == ["--effort", "xhigh"]
    assert arguments.count("--settings") == 1
    assert json.loads(arguments[arguments.index("--settings") + 1]) == {
        "sandbox": {"enabled": False},
        "ultracode": True,
    }
    assert (
        claude_settings(agent.model_copy(update={"effort": "xhigh"}))
        == CLAUDE_SANDBOX_OFF
    )


def test_codex_takes_ultra_under_its_own_name() -> None:
    assert codex_effort_arguments("ultra") == [
        "--config",
        'model_reasoning_effort="ultra"',
    ]
    config = Codex(model="gpt-6-astra", effort="ultra", cwd=Path("."))
    assert config.model_selection() == {"model": "gpt-6-astra", "effort": "ultra"}


def test_an_effort_the_model_lacks_is_refused_at_declaration() -> None:
    with pytest.raises(ValidationError, match="does not take effort 'ultra'"):
        Codex(model="gpt-6-luna", effort="ultra", cwd=Path("."))
    with pytest.raises(ValidationError, match="does not take effort 'max'"):
        Codex(model="gpt-5.5", effort="max", cwd=Path("."))
    with pytest.raises(ValidationError, match="does not take effort 'high'"):
        Claude(model="haiku", effort="high")
    with pytest.raises(ValidationError, match="does not take effort 'xhigh'"):
        Claude(model="claude-opus-4-6", effort="xhigh")


def test_a_tier_is_refused_what_its_model_lacks() -> None:
    """``fast`` is luna on Codex, whose catalog row stops below ultra."""
    with pytest.raises(ValidationError, match="gpt-6-luna"):
        Codex(model="fast", effort="ultra", cwd=Path("."))


def test_the_default_effort_is_xhigh_clamped_to_the_models_row() -> None:
    """``claude-opus-4-6`` stops at ``max`` with no ``xhigh``; haiku takes none."""
    assert claude_default_effort("opus") == "xhigh"
    assert claude_default_effort("claude-opus-4-6") == "high"
    assert claude_default_effort("haiku") is None
    assert claude_default_effort("fast") is None
    assert codex_default_effort("gpt-5.6-luna", CodexModelTiers()) == "xhigh"


def test_a_model_without_a_row_defaults_to_xhigh() -> None:
    """Inherited, custom, or a tier resolving to a custom id: no row refuses it."""
    served = CodexModelTiers(strongest=CustomModel(id="served-large"))

    assert claude_default_effort(CustomModel(id="served")) == "xhigh"
    assert claude_default_effort("inherit") == "xhigh"
    assert claude_default_effort(None) == "xhigh"
    assert codex_default_effort(CustomModel(id="local"), CodexModelTiers()) == "xhigh"
    assert codex_default_effort("strongest", served) == "xhigh"
    assert codex_default_effort(None, CodexModelTiers()) == "xhigh"


def test_only_the_default_adapts_to_the_model() -> None:
    """An agent naming no effort takes the default; a named one is still checked."""
    assert Claude(model="opus").resolved_effort() == "xhigh"
    assert Claude(model="claude-opus-4-6").resolved_effort() == "high"
    assert Claude(model="haiku").resolved_effort() is None
    assert Claude(model=CustomModel(id="served")).resolved_effort() == "xhigh"
    assert Claude(model="opus", effort="max").resolved_effort() == "max"
    assert Codex(model="gpt-5.6-luna", cwd=Path(".")).resolved_effort() == "xhigh"
    with pytest.raises(ValidationError, match="does not take effort 'ultra'"):
        Codex(model="gpt-5.6-luna", effort="ultra", cwd=Path("."))


def test_what_the_catalog_cannot_see_is_left_to_the_cli() -> None:
    Claude(model=CustomModel(id="served"), effort="ultra")
    Claude(effort="max")
    Codex(model=CustomModel(id="local"), effort="ultra", cwd=Path("."))
    Codex(model="inherit", effort="ultra", cwd=Path("."))


def test_a_model_the_other_runtime_lists_is_refused_by_this_one() -> None:
    with pytest.raises(ValueError, match="not a model Claude Code lists"):
        claude_model_choice("gpt-6-astra")
    with pytest.raises(ValueError, match="not a model Codex lists"):
        codex_model_choice("opus")


def test_a_portable_tier_reaches_both_runtimes() -> None:
    assert Claude(model="frontier").model_id() == "fable"
    assert Codex(model="frontier", cwd=Path(".")).model_id() == "gpt-6-astra"


def test_text_from_a_launcher_flag_is_read_against_the_catalog() -> None:
    assert claude_effort_named("ultra") == "ultra"
    assert codex_effort_named("max") == "max"
    with pytest.raises(ValueError, match="not an effort Claude Code takes"):
        claude_effort_named("minimal")
    with pytest.raises(ValueError, match="not an effort Codex takes"):
        codex_effort_named("none")
