"""Marketplace naming for the local lup plugin.

Marketplace names live in one global namespace
(``~/.claude/plugins/known_marketplaces.json``), so a shared name like
``lup`` or ``local`` collides across every project and worktree that
registers it — an install from one repo shadows the others. Naming each
repo's marketplace after its own project (while the plugin entry stays
``lup``, so the ``/lup:*`` command namespace is identical everywhere)
keeps installs from clashing.

``set_marketplace_name`` wires one name into the two files that hold it:

- ``.claude/plugins/.claude-plugin/marketplace.json`` — top-level ``name``
- ``.claude/settings.json`` — ``extraKnownMarketplaces`` (self-registration)
  and ``enabledPlugins`` (``lup@<name>``)

Run it via ``lup-devtools dev plugin name``; ``rename_package`` calls it so
``/lup:init`` names the marketplace after the project in the same step.
"""

import json
from pathlib import Path
from typing import TypedDict

import typer
from pydantic import ConfigDict, TypeAdapter, with_config

from lup.diagnostics import refuse
from lup.harness.models import Plugin
from lup.workspace.paths import find_project_root

# lup: ignore[constant-declaration] — the marketplace entry a native host reads,
# spelled the relative way that host requires
SELF_PATH = "./.claude/plugins"


@with_config(ConfigDict(extra="allow"))
class DirectorySource(TypedDict, total=False):
    source: str
    path: str
    url: str


@with_config(ConfigDict(extra="allow"))
class MarketplaceEntry(TypedDict):
    source: DirectorySource


@with_config(ConfigDict(extra="allow"))
class MarketplaceJson(TypedDict, total=False):
    name: str


@with_config(ConfigDict(extra="allow"))
class SettingsJson(TypedDict, total=False):
    extraKnownMarketplaces: dict[str, MarketplaceEntry]
    enabledPlugins: dict[str, bool]  # lup: ignore[dict-str-payload] — settings wire


# Validating readers for the two files this module rewrites. extra="allow" is
# what makes the write-back safe: keys these TypedDicts don't declare (the
# rest of settings.json, marketplace owner/plugins) survive the round-trip.
MARKETPLACE_ADAPTER = TypeAdapter(MarketplaceJson)
SETTINGS_ADAPTER = TypeAdapter(SettingsJson)


def plugin_root(root: Path) -> Path:
    return root / ".claude" / "plugins"


def marketplace_file(root: Path) -> Path:
    return plugin_root(root) / ".claude-plugin" / "marketplace.json"


def settings_file(root: Path) -> Path:
    return root / ".claude" / "settings.json"


def validate_name(name: str) -> str:
    if not name or any(c in name for c in " \t@/"):
        refuse(
            "is not a marketplace name, which holds no spaces, '@' or '/'",
            what=name,
            code=2,
        )
    return name


def write_json(path: Path, data: MarketplaceJson | SettingsJson) -> None:
    path.write_text(json.dumps(data, indent=2) + "\n")


def points_at_self(entry: MarketplaceEntry, root: Path) -> bool:
    """True when a marketplace entry is this repo's own local plugin dir."""
    source = entry.get("source")
    if source is None or source.get("source") != "directory":
        return False
    target = (root / source.get("path", "")).resolve()
    return target in {plugin_root(root).resolve(), root.resolve()}


def apply_marketplace_json(root: Path, name: str, dry_run: bool) -> list[str]:
    path = marketplace_file(root)
    if not path.exists():
        refuse("no marketplace.json is there", what=str(path), code=2)
    try:
        data = MARKETPLACE_ADAPTER.validate_python(json.loads(path.read_text()))
    except json.JSONDecodeError as error:
        refuse(f"is not valid JSON: {error}", what=str(path), code=2)
    old = data.get("name")
    if old == name:
        return []
    if not dry_run:
        data["name"] = name
        write_json(path, data)
    return [f"marketplace.json: name {old!r} -> {name!r}"]


def apply_settings_json(
    root: Path, name: str, plugin_name: str, dry_run: bool
) -> list[str]:
    path = settings_file(root)
    try:
        settings = (
            SETTINGS_ADAPTER.validate_python(json.loads(path.read_text()))
            if path.exists()
            else SettingsJson()
        )
    except json.JSONDecodeError as error:
        refuse(f"is not valid JSON: {error}", what=str(path), code=2)
    changes: list[str] = []  # lup: ignore[empty-collection] — change log

    known = settings.get("extraKnownMarketplaces") or {}
    for key in [k for k, v in known.items() if k != name and points_at_self(v, root)]:
        del known[key]
        changes.append(f"settings.json: drop extraKnownMarketplaces[{key!r}]")
    desired: MarketplaceEntry = {"source": {"source": "directory", "path": SELF_PATH}}
    if known.get(name) != desired:
        known[name] = desired
        changes.append(
            f"settings.json: extraKnownMarketplaces[{name!r}] -> {SELF_PATH}"
        )

    # Exactly one marketplace carrying this plugin is enabled — this repo's
    # own — so stale self-enables (lup@lup, lup@local, …) can't shadow it.
    enabled = settings.get("enabledPlugins") or {}
    target = f"{plugin_name}@{name}"
    for key in [k for k in enabled if k.startswith(f"{plugin_name}@") and k != target]:
        del enabled[key]
        changes.append(f"settings.json: drop enabledPlugins[{key!r}]")
    if enabled.get(target) is not True:
        enabled[target] = True
        changes.append(f"settings.json: enable {target!r}")

    if changes and not dry_run:
        settings["extraKnownMarketplaces"] = known
        settings["enabledPlugins"] = enabled
        write_json(path, settings)
    return changes


def set_marketplace_name(
    root: Path, name: str, plugin_name: str, dry_run: bool = False
) -> list[str]:
    """Point this repo's plugin marketplace at ``name``; the entry keeps its own.

    Idempotent: rewrites marketplace.json and settings.json only where they
    disagree with ``name``. Returns a list of human-readable change lines.
    """
    name = validate_name(name)
    return apply_marketplace_json(root, name, dry_run) + apply_settings_json(
        root, name, plugin_name, dry_run
    )


def name_marketplace(plugin: Plugin, name: str | None, dry_run: bool) -> None:
    """CLI entry for ``lup-devtools dev plugin name`` (see module docstring).

    Both names come off the plugin declaration that generation already
    renders, so this command and ``harness generate`` cannot disagree and
    silently undo each other's naming on the next run — which is how a repo
    ends up registered under a name nobody chose.
    """
    root = find_project_root()
    resolved = name or str(plugin.marketplace)
    changes = set_marketplace_name(root, resolved, plugin.name, dry_run)
    if not changes:
        typer.echo(f"Marketplace already named {resolved!r}; nothing to do.")
        return
    typer.echo(
        f"Dry run — {len(changes)} change(s) for {resolved!r}:"
        if dry_run
        else f"Named marketplace {resolved!r}:"
    )
    for change in changes:
        typer.echo(f"  {change}")
