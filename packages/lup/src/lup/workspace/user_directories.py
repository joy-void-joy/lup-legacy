"""Where lup keeps what belongs to the person rather than to any checkout.

Three places, each the XDG base directory specification's, with lup's own
directory beneath: state a program keeps between runs (``$XDG_STATE_HOME``,
``~/.local/state`` where unset) — the repositories it has vouched for, the
shared companions, the operator's answers to parked reviews; configuration a
person writes (``$XDG_CONFIG_HOME``, ``~/.config``) — their accounts and
decisions, the host-only secrets; and caches anything may delete
(``$XDG_CACHE_HOME``, ``~/.cache``) — environments, releases, guidance a
container swaps in.

Each variable is read the specification's way: an absolute path moves every
program's directory of that kind, and an empty or relative one is ignored in
favour of the default rather than resolved against wherever the command
happens to run. Declared once, so the state a launch writes and the state a
cleanup reads are the same directory under every environment.
"""

from pathlib import Path

from pydantic_settings import BaseSettings


class UserDirectories(BaseSettings):
    """The person's state, configuration and cache directories, each lup's own."""

    xdg_state_home: str = ""
    xdg_config_home: str = ""
    xdg_cache_home: str = ""

    def state(self) -> Path:
        """What lup keeps between runs for this person."""
        return self.beneath(self.xdg_state_home, Path(".local") / "state")

    def config(self) -> Path:
        """What this person decided, and the accounts they name."""
        return self.beneath(self.xdg_config_home, Path(".config"))

    def cache(self) -> Path:
        """What lup can rebuild, and so what anything may delete."""
        return self.beneath(self.xdg_cache_home, Path(".cache"))

    def beneath(self, named: str, default: Path) -> Path:
        """lup's directory under the base ``named`` moves it to, else under ``default`` in the home."""
        base = Path(named)
        return (base if base.is_absolute() else Path.home() / default) / "lup"
