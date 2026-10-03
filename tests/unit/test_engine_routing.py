"""Which engine a model runs on, and which model its auxiliary turns use.

An explicit `agent_sdk` wins; otherwise the model's name routes it, and an
unknown name falls back to an OpenAI-compatible endpoint. The auxiliary
model follows the engine the session routed to unless one is named.
"""

import pytest

from lup_template.agent.config import aux_model, engine_for_settings, settings


def test_aux_model_explicit_override_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "aux_model", "my-reviewer")
    monkeypatch.setattr(settings, "agent_sdk", "codex")
    assert aux_model() == "my-reviewer"


def test_aux_model_claude_defaults_to_opus(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "aux_model", None)
    monkeypatch.setattr(settings, "agent_sdk", "claude")
    monkeypatch.setattr(settings, "openai_base_url", None)
    monkeypatch.setattr(settings, "openrouter_api_key", None)
    assert aux_model() is None  # The provider resolves its strongest alias.


def test_aux_model_compat_endpoint_reuses_session_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "aux_model", None)
    monkeypatch.setattr(settings, "agent_sdk", "claude")
    monkeypatch.setattr(settings, "model", "anthropic/claude-opus-5")
    monkeypatch.setattr(settings, "openai_base_url", None)
    monkeypatch.setattr(settings, "openrouter_api_key", "or-key")
    assert aux_model() == "anthropic/claude-opus-5"


def test_engine_router_explicit_agent_sdk_wins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "agent_sdk", "claude")
    monkeypatch.setattr(settings, "model", "gpt-5.6-sol")
    assert engine_for_settings() == "claude"


def test_engine_router_claude_prefix_runs_native_claude(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "agent_sdk", None)
    monkeypatch.setattr(settings, "model", "claude-fable-5")
    assert engine_for_settings() == "claude"


def test_engine_router_openai_prefixes_run_codex(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "agent_sdk", None)
    for model in ("gpt-5.6-sol", "o4-mini", "codex-mini-latest"):
        monkeypatch.setattr(settings, "model", model)
        assert engine_for_settings() == "codex"


def test_engine_router_openrouter_fallback_runs_claude_compat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "agent_sdk", None)
    monkeypatch.setattr(settings, "model", "anthropic/claude-opus-5")
    monkeypatch.setattr(settings, "openrouter_api_key", "or-key")
    assert engine_for_settings() == "claude-compat"


def test_engine_router_unknown_model_runs_openai_compat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "agent_sdk", None)
    monkeypatch.setattr(settings, "model", "llama-4-scout")
    monkeypatch.setattr(settings, "openrouter_api_key", None)
    assert engine_for_settings() == "openai-compat"


def test_aux_model_follows_the_routed_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "aux_model", None)
    monkeypatch.setattr(settings, "agent_sdk", None)
    monkeypatch.setattr(settings, "model", "gpt-5.6-sol")
    assert aux_model() == "gpt-5.6-sol"


def test_aux_model_codex_reuses_session_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "aux_model", None)
    monkeypatch.setattr(settings, "agent_sdk", "codex")
    monkeypatch.setattr(settings, "model", "gpt-5.5")
    assert aux_model() == "gpt-5.5"
