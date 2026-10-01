"""Every runtime's configuration-home declaration, for whatever has to tell their files apart.

A volume two runtimes once shared is split by what each runtime says it
keeps (:attr:`~lup.providers.login.ProviderLogin.home_entries`), and a split
answers for every runtime at once, not only the one being launched. Each
declaration is imported where it is asked for, as the model catalog's
readers are: it reaches its provider's adapter, which nothing importing this
module for anything else needs.
"""

from typing import Literal

from lup.providers.login import ProviderLogin
from lup.types import StringMap


def runtime_logins() -> list[ProviderLogin]:
    """Every runtime lup launches, as its configuration-home declaration."""
    from lup.providers.claude.login import CLAUDE_LOGIN
    from lup.providers.codex.login import CODEX_LOGIN

    return [CLAUDE_LOGIN, CODEX_LOGIN]


def selected_runtime(environment: StringMap) -> Literal["claude", "codex"] | None:
    """Which runtime's session an environment is, by the home its launcher selected.

    Each launcher exports its own runtime's configuration-home variable, so
    the one set names the runtime; neither set is no runtime's session. The
    variables are each login's own, so nothing outside the providers spells
    them.
    """
    from lup.providers.claude.login import CLAUDE_LOGIN
    from lup.providers.codex.login import CODEX_LOGIN

    if CLAUDE_LOGIN.config_home_env in environment:
        return "claude"
    if CODEX_LOGIN.config_home_env in environment:
        return "codex"
    return None
