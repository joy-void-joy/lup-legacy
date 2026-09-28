"""Rendering a runtime's project settings from what the harness declares.

Everything here is derived rather than written down: the marketplace key, the
enabled plugin, the sandbox's network and filesystem boundaries come off the
``Plugin`` and its ``HookSet``, and the tool grants off the servers every
session carries. A project supplies only what is genuinely its own — which
official plugins it enables, which tools it grants outright, which reads it
refuses — through :class:`Settings`.

The derivation is the point. A settings file written by hand beside a hook
declaration can disagree with it, and the disagreement is invisible until a
session is denied something the policy allows.
"""

from collections.abc import Sequence
from pathlib import PurePosixPath
from urllib.parse import urlsplit

from pydantic import BaseModel, Field

from lup.harness.models import HookSet, HookUrlScope, Plugin
from lup.mcp import ToolServer
from lup.types import EnvVars, JsonObject, JsonValue


class Settings(BaseModel, frozen=True):
    """The half of a settings artifact that is a project's own judgement."""

    base: JsonObject = Field(
        default={},
        description=(
            "Runtime settings that are neither derived from the declaration "
            "nor permissions — the editor integrations and session defaults a "
            "project chooses for itself."
        ),
    )
    env: EnvVars = Field(
        default={},
        description=(
            "Environment the runtime applies to the session, rendered over "
            "what the declaration derives — a variable spelled here is the "
            "project's own judgement and outranks a derived one."
        ),
    )
    official_plugins: JsonObject = Field(
        default={},
        description=(
            "Vendor-published plugins this project decides by name. False is "
            "a decision and not an omission: a project scope outranks the "
            "user's, so it holds against a plugin enabled globally, and it "
            "survives regeneration where an install written into this "
            "artifact would read as drift."
        ),
    )
    allowed: list[JsonValue] = Field(
        default=[],
        description="Tool patterns granted outright, before the served-tool grants.",
    )
    denied: list[JsonValue] = Field(
        default=[],
        description="Tool patterns refused regardless of what else allows them.",
    )


def served_tool_grants(servers: Sequence[ToolServer]) -> list[str]:
    """Grant every tool the servers each session carries serve.

    A declared server is the project's own code, wired in deliberately, so
    asking per call would make the declaration a suggestion. A new group in
    the toolsets registry is granted by being declared, with nothing in the
    settings to extend. Each is granted under the key a launch declares it
    by, which is the one the skills and agents that ask for its tools name.
    """
    return [f"mcp__{server.name}" for server in servers]


def credential_read_denials(hooks: "HookSet | None") -> list[str]:
    """Every path the policy withholds from a command, as rules the file tools obey.

    Compiled from :attr:`~lup.harness.models.HookSet.refused_paths`, the one
    declaration the shell policy refuses a word by. Read, Grep and Glob run in
    the session's own process and never reach the policy hook, so a second
    list declared for them named `~/.ssh` and `~/.aws/credentials` while the
    shell withheld `~/.netrc`, `~/.git-credentials` and each runtime's own
    login -- which the file tools went on reading. Claude Code merges these
    same rules into its sandbox's read restrictions, so they are the OS
    layer's credential denial as well, and nothing else renders one.

    Each anchor keeps its meaning. From a home is `~/`, which the runtime
    knows and the kernel does not; from the root is `//`; from anywhere is
    `//**/`. A pattern ending in `**` names the directory too, as the
    kernel's own match does, so a search rooted at it is refused and not only
    a read beneath it. An exemption cannot be carried: a rule carves nothing
    out of a deny spelled from a home or the root, so the file tools are
    refused the public keys the shell may still read.

    ``Read`` covers the reading tools whole: Claude Code consults file
    permissions against ``Read`` and ``Edit`` rules only, and accepts but never
    consults a ``Grep`` or ``Glob`` path rule — writing one would warn at
    startup and deny nothing.
    """
    if hooks is None:
        return []

    def rules(pattern: str) -> list[str]:
        """One withheld pattern in the rule syntax, with the directory it spans."""
        match PurePosixPath(pattern).parts:
            case ("~", *names):
                anchor = "~/"
            case ("/", *names):
                anchor = "//"
            case ("**", *names):
                anchor = "//**/"
            case _:
                return []
        spanned = names[:-1] if names[-1:] == ["**"] else []
        return [
            f"Read({anchor}{'/'.join(names)})",
            *([f"Read({anchor}{'/'.join(spanned)})"] if spanned else []),
        ]

    return list(
        dict.fromkeys(
            rule
            for refused in hooks.refused_paths
            for pattern in refused.paths
            for rule in rules(pattern)
        )
    )


def allowed_network_domains(hooks: HookSet) -> list[str]:
    """Collect fetch-scope hostnames and declared extras, first-seen order.

    A scope that includes subdomains contributes both its apex host and the
    ``*.host`` wildcard, so the OS boundary admits exactly what the semantic
    fetch policy already allows.
    """
    if hooks.sandbox is None:
        return []

    def sandbox_domains(scope: HookUrlScope) -> list[str]:
        host = urlsplit(str(scope.origin)).hostname
        if host is None:
            return []
        return [host, f"*.{host}"] if scope.include_subdomains else [host]

    merged = [
        domain for scope in hooks.allowed_fetch for domain in sandbox_domains(scope)
    ]
    merged.extend(hooks.sandbox.extra_domains)
    return list(dict.fromkeys(merged))


def project_settings(
    declared: Settings, plugin: Plugin | None, servers: Sequence[ToolServer] = ()
) -> JsonObject:
    """Render the settings artifact, deriving every block it can.

    ``servers`` are the tool servers every session a launch opens carries,
    whose tools are granted outright.

    The sandbox stays permissive where the semantic policy already judges
    (escapes re-enter the deny lattice) and hardens what shell readers could
    otherwise bypass: every path the policy withholds becomes a `Read` deny
    rule, which the runtime merges into the sandbox's read restrictions, so
    the file tools and a sandboxed command are refused the same set. It is an
    array key the runtime merges across settings scopes, so a repository
    states its own requirement without displacing the user's or the
    organization's.
    """
    settings: JsonObject = dict(declared.base)
    if declared.env:
        settings["env"] = dict(declared.env)
    settings["enabledPlugins"] = dict(declared.official_plugins)
    if plugin is not None:
        # Marketplace names share one global namespace, so this must be the
        # per-project name the declaration carries. A literal here would
        # register every adopter under one key, and whichever repo installed
        # last would serve its plugin to all of them.
        settings["extraKnownMarketplaces"] = {
            str(plugin.marketplace): {
                "source": {"path": "./.claude/plugins", "source": "directory"}
            }
        }
        settings["enabledPlugins"] = {
            **declared.official_plugins,
            f"{plugin.name}@{plugin.marketplace}": True,
        }
    grants: list[JsonValue] = list(served_tool_grants(servers))
    hooks = plugin.hooks if plugin is not None else None
    settings["permissions"] = {
        "allow": [*declared.allowed, *grants],
        "deny": [*declared.denied, *credential_read_denials(hooks)],
    }
    if hooks is None or hooks.sandbox is None:
        return settings
    domains: list[JsonValue] = list(allowed_network_domains(hooks))
    settings["sandbox"] = {
        "enabled": True,
        "excludedCommands": list(hooks.sandbox.excluded_commands),
        "network": {"allowedDomains": domains},
        # No write denials: a human-owned path is judged by the policy, which
        # asks and carries the author's answer, where a denial in the runtime's
        # own sandbox refused the write outright and could put nothing to
        # anybody.
        # No read denials either: the `Read` rules above are merged into this
        # sandbox's read restrictions by the runtime itself, and a second
        # rendering here in the sandbox's own path syntax is a second list to
        # keep in step with the one the file tools obey.
        "filesystem": {"allowWrite": list(hooks.sandbox.writable_paths)},
    }
    return settings
