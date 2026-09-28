"""Prepare Codex's own home from a checkout, without opening an agent turn."""

from pathlib import Path
import sys
from typing import Annotated

import typer
from pydantic import ValidationError

from lup.providers.codex.harness_runtime import CodexPluginInstaller, PluginCacheConfig
from lup.providers.codex.home import (
    CodexWorktreeHomeStore,
    install_declared_policy,
    trust_project,
)
from lup.providers.codex.marketplace import CodexMarketplace
from lup.providers.codex.profile import CodexAccountSettings
from lup.providers.codex.theme import claude_daltonized_theme


def install_codex_plugin(
    root: Path, home: Path, force: bool = False, trusted: bool = False
) -> None:
    """Install the checkout's plugin and verify native discovery and hook trust."""
    declared = CodexMarketplace.declared(root)
    if declared is None:
        return
    if trusted:
        home.mkdir(parents=True, exist_ok=True)
        trust_project(home, root)
        claude_daltonized_theme().write(home)
    installer = CodexPluginInstaller(
        PluginCacheConfig(
            codex_home=home, marketplace=declared.name, plugin=declared.plugin
        )
    )
    cache = installer.ensure(declared.source, root, force=force)
    installer.verify(cache, root)
    install_declared_policy(
        home, root, seed=trusted or CodexWorktreeHomeStore().derived(home)
    )
    typer.echo(f"Verified installed Codex plugin in {home}: {cache.installed_root}")


app = typer.Typer()


@app.command()
def prepare(
    root: Annotated[Path, typer.Option("--root")],
    home: Annotated[Path, typer.Option("--home")],
    force: Annotated[bool, typer.Option("--force")] = False,
    trusted: Annotated[bool, typer.Option("--trust-project")] = False,
    settings_stdin: Annotated[bool, typer.Option("--settings-stdin")] = False,
) -> None:
    """Prepare one native home from the installed library's implementation."""
    if settings_stdin:
        try:
            CodexAccountSettings.model_validate_json(sys.stdin.read()).install(
                home, enforce_policy=CodexMarketplace.declared(root) is not None
            )
        except (ValidationError, ValueError):
            raise typer.BadParameter(
                "Cannot prepare the selected Codex profile; its settings were not logged."
            ) from None
    install_codex_plugin(root, home, force, trusted)


if __name__ == "__main__":
    app()
