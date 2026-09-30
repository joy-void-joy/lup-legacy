"""Configuration management using pydantic-settings.

This is a TEMPLATE. Customize for your domain.

Key patterns:
1. Multiple env files (.env, .env.local) - local overrides shared
2. Optional API keys with startup warnings
3. validation_alias for explicit env var names
4. Singleton instance for easy import

Usage:
    from lup_template.agent.config import settings
    print(settings.model)
"""

import logging
import os
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode

from lup.providers.routing import catalog_provider
from lup.types import SessionEffort

logger = logging.getLogger(__name__)

type Engine = Literal["claude", "codex", "openai", "openai-compat", "claude-compat"]
"""The engines the composition root can assemble; ``engine_for_settings``
resolves which one a session runs."""


class Settings(BaseSettings, env_file=(".env", ".env.local"), extra="ignore"):
    """Application settings loaded from environment variables.

    Agent-specific settings use a prefix (e.g., AGENT_MODEL).
    """

    @model_validator(mode="after")
    def warn_missing_optional_keys(self) -> Self:
        """Warn at startup if optional API keys are missing.

        Customize this for your domain's optional dependencies.
        """
        # lup: template: list your domain's optional API keys here, e.g.
        # "EXAMPLE_API_KEY": self.example_api_key,
        optional_keys: dict[str, str | None] = {}  # lup: ignore[dict-str-payload]
        missing = [name for name, value in optional_keys.items() if not value]

        if missing:
            logger.warning(
                "Missing API keys (some tools may fail): %s", ", ".join(missing)
            )
        return self

    # ==========================================================================
    # OPTIONAL API KEYS (tools degrade gracefully without these)
    # ==========================================================================

    # lup: template: replace with your domain's keys, one "requires:" tag per
    # service — tools tagged "requires:example-api" are excluded by
    # ToolPolicy when this key is missing.
    example_api_key: str | None = Field(
        default=None,
        validation_alias="EXAMPLE_API_KEY",
        description="Example service API key (placeholder for your domain)",
    )

    # ==========================================================================
    # LLM ROUTING (optional)
    # ==========================================================================

    openrouter_api_key: str | None = Field(
        default=None,
        validation_alias="OPENROUTER_API_KEY",
        description="OpenRouter API key (enables routing through OpenRouter when set)",
    )

    # ==========================================================================
    # SDK SELECTION
    # ==========================================================================

    agent_sdk: Engine | None = Field(
        default=None,
        validation_alias="AGENT_SDK",
        description=(
            "Which engine runs the agent (claude, codex, openai-compat, "
            "claude-compat; openai also names openai-compat). "
            "Unset routes by the AGENT_MODEL vendor prefix: claude-* runs "
            "claude, gpt-*/o<digit>*/codex* runs codex, and anything else "
            "runs claude-compat when OPENROUTER_API_KEY is set or "
            "openai-compat otherwise. claude-compat keeps the Claude "
            "scaffolding on an Anthropic-protocol endpoint (OPENAI_BASE_URL). "
            "The reviewer and background agents follow AGENT_AUX_MODEL, "
            "which defaults to an engine-native model. Delegated roles "
            "declare portable model tiers resolved by the selected engine."
        ),
    )

    openai_base_url: str | None = Field(
        default=None,
        validation_alias="OPENAI_BASE_URL",
        description="Base URL for OpenAI-compatible API (vLLM, Ollama, TGI, etc.)",
    )

    openai_api_key: str | None = Field(
        default=None,
        validation_alias="OPENAI_API_KEY",
        description="API key for OpenAI-compatible API",
    )

    openai_model_provider: str | None = Field(
        default=None,
        validation_alias="OPENAI_MODEL_PROVIDER",
        description="Codex model_provider for OpenAI-compatible endpoints",
    )

    codex_sandbox: str | None = Field(
        default=None,
        validation_alias="CODEX_SANDBOX",
        description="Codex sandbox mode: read_only, workspace_write, danger_full_access",
    )

    codex_effort: SessionEffort | None = Field(
        default=None,
        validation_alias="CODEX_EFFORT",
        description=(
            "Codex reasoning effort: low, medium, high, xhigh, max or ultra. "
            "Unset, AGENT_REASONING_EFFORT, and unset too, the model's default: "
            "xhigh where its catalog row takes it, its highest rung below "
            "otherwise"
        ),
    )

    codex_approval_policy: str | None = Field(
        default=None,
        validation_alias="CODEX_APPROVAL_POLICY",
        description="Codex approval policy for tool use",
    )

    codex_usd_per_mtok_input: float | None = Field(
        default=None,
        validation_alias="CODEX_USD_PER_MTOK_INPUT",
        description="USD per million input tokens (enables budget enforcement on codex/openai)",
    )

    codex_usd_per_mtok_output: float | None = Field(
        default=None,
        validation_alias="CODEX_USD_PER_MTOK_OUTPUT",
        description="USD per million output tokens (enables budget enforcement on codex/openai)",
    )

    codex_usd_per_mtok_cached_input: float | None = Field(
        default=None,
        validation_alias="CODEX_USD_PER_MTOK_CACHED_INPUT",
        description="USD per million cached input tokens (defaults to the input rate)",
    )

    reasoning_effort: SessionEffort | None = Field(
        default=None,
        validation_alias="AGENT_REASONING_EFFORT",
        description=(
            "Backend-agnostic reasoning effort: low, medium, high, xhigh, "
            "max or ultra, on both backends. Which of them a model takes is "
            "its catalog's answer, and a level the model lacks is refused; "
            "unset, the model's default is xhigh where its row takes it and "
            "its highest rung below otherwise (CODEX_EFFORT overrides this on "
            "Codex/OpenAI backends)."
        ),
    )

    tool_search: str | None = Field(
        default="false",
        validation_alias="AGENT_TOOL_SEARCH",
        description=(
            "Claude tool-schema deferral (ENABLE_TOOL_SEARCH): false loads "
            "every schema upfront, true forces tool search, auto/auto:N "
            "defer past a context threshold, None inherits the harness "
            "default. Defaults to false because the template serves a small "
            "curated surface where a deferred (invisible) tool risks the "
            "agent concluding the capability is missing — see docs/orchestration.md "
            "§ Deferred Tool Schemas. Claude sessions only."
        ),
    )

    permission_mode: (
        Literal["manual", "acceptEdits", "plan", "bypassPermissions"] | None
    ) = Field(
        default=None,
        validation_alias="AGENT_PERMISSION_MODE",
        description=(
            "Claude SDK permission mode for the main agent session "
            "(None = engine default: bypassPermissions on claude, where "
            "enforcement is the hook layer)"
        ),
    )

    # ==========================================================================
    # MODEL SETTINGS
    # ==========================================================================

    model: str | None = Field(
        default=None,
        validation_alias="AGENT_MODEL",
        description="Explicit model identifier; unset selects the native engine's strongest tier",
    )

    max_thinking_tokens: int | None = Field(
        default=None,
        validation_alias="AGENT_MAX_THINKING_TOKENS",
        description=(
            "Max thinking tokens (None = engine default: the API maximum "
            "on claude sessions)"
        ),
    )

    aux_model: str | None = Field(
        default=None,
        validation_alias="AGENT_AUX_MODEL",
        description=(
            "Model for auxiliary agents (reviewer, background agents). "
            "None resolves per backend: an opus-class reviewer on claude, "
            "the session model on codex/openai — so AGENT_SDK=codex/openai "
            "runs without Anthropic credentials."
        ),
    )

    # ==========================================================================
    # PATHS
    # ==========================================================================

    notes_path: str = Field(
        default="./notes",
        validation_alias="AGENT_NOTES_PATH",
        description="Base path for notes folders",
    )

    logs_path: str = Field(
        default="./logs",
        validation_alias="AGENT_LOGS_PATH",
        description="Base path for trace logs",
    )

    extra_dirs: Annotated[list[Path], NoDecode] = Field(
        default=[],
        validation_alias="AGENT_EXTRA_DIRS",
        description=(
            "Additional directories the agent may read beyond the session "
            "workspace, PATH-style (colon-separated) — e.g. a reference-data "
            "or transcript directory."
        ),
    )

    @field_validator("extra_dirs", mode="before")
    @classmethod
    def split_extra_dirs(cls, value: str | list[Path]) -> list[str] | list[Path]:
        """Env values are PATH-style: colon-separated directory names."""
        if isinstance(value, str):
            parts = value.split(os.pathsep)  # lup: ignore[string-split] — PATH format
            return [part for part in parts if part]
        return value

    # ==========================================================================
    # LIMITS
    # ==========================================================================

    max_budget_usd: float | None = Field(
        default=None,
        validation_alias="AGENT_MAX_BUDGET_USD",
        description="Maximum budget per session (None = unlimited)",
    )

    max_turns: int | None = Field(
        default=None,
        validation_alias="AGENT_MAX_TURNS",
        description="Maximum agent turns per session (None = unlimited)",
    )

    turn_timeout_seconds: float | None = Field(
        default=None,
        validation_alias="AGENT_TURN_TIMEOUT_SECONDS",
        description=(
            "Wall-clock cap on a complete logical turn, enforced for every engine. "
            "None = no limit."
        ),
    )

    sandbox_enabled: bool = Field(
        default=True,
        validation_alias="AGENT_SANDBOX_ENABLED",
        description="Run code execution tools in a Docker sandbox "
        "(requires Docker; disable to run the agent without code execution)",
    )

    sandbox_timeout_seconds: int = Field(
        default=30,
        validation_alias="AGENT_SANDBOX_TIMEOUT_SECONDS",
        description="Timeout for sandbox code execution",
    )

    sandbox_allow_shell: bool = Field(
        default=False,
        validation_alias="AGENT_SANDBOX_ALLOW_SHELL",
        description="Grant the raw shell (Bash) builtin. Off by default: host "
        "shell is dropped regardless of the code-execution sandbox "
        "(execute_code is the sanctioned code path), so it is an explicit opt-in.",
    )


# Singleton instance
settings = Settings()


def engine_for_model(model: str | None) -> Engine:
    """The engine one model id routes to, by catalog and then vendor prefix.

    A name either runtime's own catalog lists runs that runtime natively —
    which is how an alias like ``fable`` routes at all. Otherwise
    ``claude-*`` runs the native Claude engine and ``gpt-*``/``o<digit>``/
    ``codex*`` runs Codex. Anything else runs a compat engine:
    ``claude-compat`` when ``OPENROUTER_API_KEY`` selects OpenRouter's
    Anthropic-protocol endpoint, ``openai-compat`` otherwise.
    """
    if model is None:
        return "claude"
    listed = catalog_provider(model)
    if listed is not None:
        return listed
    if model.startswith("claude-"):
        return "claude"
    if model.startswith(("gpt-", "codex")) or (
        model[:1] == "o" and model[1:2].isdigit()
    ):
        return "codex"
    return "claude-compat" if settings.openrouter_api_key else "openai-compat"


def engine_for_settings(model: str | None = None) -> Engine:
    """The engine the session runs: explicit ``AGENT_SDK``, else routed.

    Unset ``AGENT_SDK`` routes by the requested or configured model's vendor prefix.
    """
    if settings.agent_sdk is not None:
        return settings.agent_sdk
    return engine_for_model(model or settings.model)


def aux_model() -> str | None:
    """Backend-coherent model for auxiliary agents (reviewer, backgrounds).

    Explicit ``AGENT_AUX_MODEL`` wins. Otherwise native Claude sessions get
    an opus-class reviewer (best results on a subscription), while Codex/
    OpenAI sessions and compat-endpoint sessions reuse the session model —
    the one model the account accepts.
    """
    if settings.aux_model:
        return settings.aux_model
    if engine_for_settings() == "claude" and compat_base_url() is None:
        return None
    return settings.model


# Route notes/logs paths into lup.workspace.paths so every consumer (history,
# devtools, traces) honors the configured locations
if settings.notes_path != "./notes" or settings.logs_path != "./logs":
    from pathlib import Path

    from lup.workspace.paths import configure

    configure(
        notes_dir=Path(settings.notes_path).resolve(),
        logs_dir=Path(settings.logs_path).resolve(),
    )

# lup: ignore[constant-declaration] — the endpoint OpenRouter publishes
OPENROUTER_BASE_URL = "https://openrouter.ai/api"
"""OpenRouter's Anthropic-protocol endpoint, selected by OPENROUTER_API_KEY."""


def compat_base_url() -> str | None:
    """The compat endpoint settings select, or None for native routing.

    An explicit ``OPENAI_BASE_URL`` wins; otherwise an ``OPENROUTER_API_KEY``
    alone selects OpenRouter. The concrete composition root applies it through
    the provider's typed compatibility transform.
    """
    if settings.openai_base_url:
        return settings.openai_base_url
    if settings.openrouter_api_key:
        return OPENROUTER_BASE_URL
    return None


def compat_api_key() -> str | None:
    """The credential paired with :func:`compat_base_url`."""
    return settings.openai_api_key or settings.openrouter_api_key
