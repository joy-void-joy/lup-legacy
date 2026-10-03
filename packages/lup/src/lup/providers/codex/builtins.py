"""The tools a Codex session is given, and the controls that hold it to them.

Codex's built-ins are facilities rather than a roster: a shell, hosted web
search, and patch application, each switched on or off where the app-server
starts. So a grant names those three and nothing else. Codex has no tool
that reads, writes or fetches without being one of them, and a grant for
one it lacks is refused by the Literal rather than approximated by the
nearest facility, which would hand over more than was asked for.
"""

import json
from pathlib import Path
from typing import TYPE_CHECKING, Self

import sh
from pydantic import BaseModel, TypeAdapter

from lup.tools.builtin import BuiltinPreset
from lup.types import EnvVars, JsonObject, JsonValue

if TYPE_CHECKING:
    from lup.providers.codex import CodexBuiltinTool


class CodexBuiltins(BaseModel, frozen=True):
    """The facilities one session starts with, as the app-server is told them."""

    shell: bool = False
    web: bool = False
    write: bool = False
    images: bool = False
    all_tools: bool = False
    agents: bool = False
    """Whether the session gets Codex's agent tools: spawning a subagent, and
    messaging, waiting on and closing one. Its own switch because a project
    that refuses spawning refuses it here too — a session composed in process
    cannot see a spawn to refuse it whenever no policy plugin is installed —
    while keeping every other tool its grant names."""

    @classmethod
    def compile(cls, builtin: "BuiltinPreset | list[CodexBuiltinTool]") -> Self:
        """The facilities a grant switches on; ``stock`` is every one Codex has."""
        match builtin:
            case "stock":
                return cls(
                    shell=True,
                    web=True,
                    write=True,
                    images=True,
                    all_tools=True,
                    agents=True,
                )
            case "web":
                return cls(web=True)
            case "none":
                return cls()
            case list():
                return cls(
                    shell="Bash" in builtin,
                    web="WebSearch" in builtin,
                    write="apply_patch" in builtin,
                )

    def configuration(self) -> JsonObject:
        """Override every startup facility that can introduce ambient tools."""
        disabled = (
            "shell_snapshot",
            "apps",
            "plugins",
            "remote_plugin",
            "multi_agent",
            "multi_agent_v2",
            "browser_use",
            "browser_use_external",
            "browser_use_full_cdp_access",
            "computer_use",
            "image_generation",
            "hooks",
            "skill_mcp_dependency_install",
            "skill_search",
            "workspace_dependencies",
            "tool_suggest",
            "recommended_plugins",
            "code_mode",
            "code_mode_host",
            "code_mode_only",
            "code_mode_prewarm",
            "goals",
            "sleep_tool",
            "view_image",
            "request_permissions_tool",
            "memories",
            "context_management",
        )
        features: JsonObject = {feature: False for feature in disabled}
        features.update(shell_tool=self.shell, unified_exec=self.shell)
        features["view_image"] = self.images
        features["multi_agent"] = self.agents
        features["multi_agent_v2"] = self.agents
        features["standalone_web_search"] = self.web
        return {
            "features": features,
            "web_search": "live" if self.web else "disabled",
            "notify": [],
            "skills": {"include_instructions": False},
            "tools": {
                "web_search": self.web,
                "view_image": self.images,
                "update_plan": {"enabled": self.all_tools},
                "experimental_request_user_input": {"enabled": False},
            },
        }

    def arguments(self) -> list[str]:
        """Apply the same controls before app-server starts, not just per thread."""
        return self.configuration_arguments() + [
            "--config",
            'sandbox_mode="read-only"',
            "--config",
            'approval_policy="never"',
        ]

    def configuration_arguments(self) -> list[str]:
        """These facilities as the dotted ``--config`` overrides any Codex CLI reads."""

        def leaves(prefix: str, value: JsonValue) -> list[str]:
            if isinstance(value, dict):
                return [
                    argument
                    for name, child in value.items()
                    for argument in leaves(
                        f"{prefix}.{name}" if prefix else name, child
                    )
                ]
            return ["--config", f"{prefix}={json.dumps(value)}"]

        return leaves("", self.configuration())

    def model_catalog(
        self, executable: Path, environment: EnvVars, model: str | None
    ) -> JsonObject:
        """Compile the vendor's model metadata without its implicit tool grants.

        Model metadata can opt into tools independently of features: direct
        tool mode, apply_patch and v2 delegation must be bounded here too.
        Model identities, context limits and reasoning settings remain the
        vendor's values. Unknown models fail before any model request.
        """
        raw = str(
            sh.Command(str(executable))(
                "debug", "models", "--bundled", _env=environment
            )
        )
        catalog = TypeAdapter(JsonObject).validate_json(raw)
        models = catalog["models"] if "models" in catalog else None
        if not isinstance(models, list) or not all(
            isinstance(item, dict) for item in models
        ):
            raise ValueError(
                "Codex did not return a compatible model catalog; update the adapter before opening this session"
            )
        if model is not None and not any(
            isinstance(item, dict) and "slug" in item and item["slug"] == model
            for item in models
        ):
            raise ValueError(
                f"Codex cannot bound tools for unknown model {model!r}; use a model in its installed catalog"
            )
        for item in models:
            if isinstance(item, dict):
                item["tool_mode"] = "direct"
                if not self.all_tools:
                    item.update(
                        experimental_supported_tools=[], node_repl_disabled=True
                    )
                if not self.agents:
                    # The features alone do not take the agent tools away: a
                    # model whose row names `v2` was offered them with both
                    # off (0.159.2, `gpt-5.6-sol`), so the row has to say so.
                    item["multi_agent_version"] = None
                if not self.write:
                    item["apply_patch_tool_type"] = None
        return catalog
