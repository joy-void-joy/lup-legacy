"""Prepare Codex's own home from a checkout, without opening an agent turn."""

from pathlib import Path
import sys
from typing import Annotated

import typer
from pydantic import BaseModel, ValidationError

from lup.diagnostics import refuse
from lup.providers.codex.harness_runtime import CodexPluginInstaller, PluginCacheConfig
from lup.providers.codex.home import (
    CodexWorktreeHomeStore,
    install_declared_policy,
    trust_project,
)
from lup.providers.codex.marketplace import CodexMarketplace
from lup.providers.codex.profile import CodexAccountSettings
from lup.providers.codex.theme import claude_daltonized_theme


class PreparedPlugin(BaseModel, frozen=True):
    """What preparing a home installed, reported for the launch that asked.

    The revision Codex will run the plugin's hooks from, by the path it has
    in the home. A contained launch reads it to hold that revision still for
    the session: the home is the session's to write, and the revision's name
    is chosen against what the home already caches, so only the preparation
    that installed it knows it.
    """

    installed_root: Path | None = None
    """Where the installed revision sits in the home; none where nothing was."""


def install_codex_plugin(
    root: Path, home: Path, force: bool = False, trusted: bool = False
) -> PreparedPlugin:
    """Install the checkout's plugin and verify native discovery and hook trust."""
    declared = CodexMarketplace.declared(root)
    if declared is None:
        return PreparedPlugin()
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
    typer.echo(
        f"Verified installed Codex plugin in {home}: {cache.installed_root}", err=True
    )
    return PreparedPlugin(installed_root=cache.installed_root)


app = typer.Typer()


@app.command()
def prepare(
    root: Annotated[Path, typer.Option("--root")],
    home: Annotated[Path, typer.Option("--home")],
    force: Annotated[bool, typer.Option("--force")] = False,
    trusted: Annotated[bool, typer.Option("--trust-project")] = False,
    settings_stdin: Annotated[bool, typer.Option("--settings-stdin")] = False,
    report: Annotated[
        bool,
        typer.Option(
            "--report",
            help="Print what was installed as JSON on stdout, and nothing else there",
        ),
    ] = False,
) -> None:
    """Prepare one native home from the installed library's implementation."""

    def settings_installed() -> bool:
        """Whether the settings on stdin validated and installed.

        Answered as a flag rather than refused where it is caught, so the
        refusal carries no context: what failed to validate holds the
        profile's settings, which are never logged.
        """
        try:
            CodexAccountSettings.model_validate_json(sys.stdin.read()).install(
                home, enforce_policy=CodexMarketplace.declared(root) is not None
            )
        except (ValidationError, ValueError):
            return False
        return True

    if settings_stdin and not settings_installed():
        refuse(
            "cannot prepare the selected Codex profile from these settings, "
            "which are not logged",
            what="--settings-stdin",
            code=2,
        )
    prepared = install_codex_plugin(root, home, force, trusted)
    if report:
        typer.echo(prepared.model_dump_json())


if __name__ == "__main__":
    app()
