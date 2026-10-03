"""Codex's own words for where it keeps a login.

A leaf on purpose: the harness declaration, the account-home sync, and the
profile transform all need these spellings, and none of them should pull the
session runtime in to get them.
"""

from pathlib import Path

from lup.providers.login import HomePreparation, ProviderLogin

# lup: ignore[constant-declaration] — the environment variable Codex reads
CODEX_HOME = "CODEX_HOME"

CODEX_LOGIN = ProviderLogin(
    config_home_env=CODEX_HOME,
    credentials_file="auth.json",
    # Codex 0.159.2 holds its login in memory and reloads auth.json only for
    # the account it already runs as (codex-rs login/src/auth/manager.rs,
    # `reload_if_account_id_matches`), so another account reaches a session
    # only when it is opened again.
    rereads_login=False,
    ambient_home=Path.home() / ".codex",
    canonical_home=True,
    home_subdir="codex-home",
    home_preparation=HomePreparation(executable="lup-codex-plugin"),
    state_volume="codex",
    # Read off a home Codex 0.156.1 wrote, and off the shared volume a
    # contained session had filled beside Claude Code. `cache`,
    # `history.jsonl`, `plugins`, `sessions` and `skills` are names both
    # runtimes write.
    home_entries=[
        ".codex-global-state.json",
        ".lup-login-handoff.lock",
        ".lup-seeded-auth.json.sha256",
        ".personality_migration",
        ".sandbox_migration",
        ".tmp",
        "AGENTS.md",
        "archived_sessions",
        "auth.json",
        "cache",
        "config.toml",
        "*.config.toml",
        "goals_*.sqlite*",
        "history.jsonl",
        "installation_id",
        "log",
        "logs_*.sqlite*",
        "memories_*.sqlite*",
        "models_cache.json",
        "plugins",
        "prompts",
        "queue_*.sqlite*",
        "rules",
        "session_index.jsonl",
        "sessions",
        "shell_snapshots",
        "skills",
        "state_*.sqlite*",
        "themes",
        "thread-writer-locks",
        "thread_history_*.sqlite*",
        "tmp",
        "version.json",
    ],
)
"""Where Codex stores a completed login, and how to select one.

No renewal test, because the file states no deadline to read one from: it holds
``auth_mode``, the three tokens, an account id and ``last_refresh``, which
records when a renewal last happened rather than when renewing stops working.
The `id_token` is a JWT and does carry an `exp`, and using it would be worse
than declaring nothing — that claim is the id token's own hour, so every launch
past the first would read a perfectly good login as dead and re-seed over it.

Host-login fingerprints decide whether an explicit host change is applied;
unchanged host credentials leave container renewals intact. The launcher asks
Codex's account API to validate or renew the selected login inside the session
boundary, choosing browser login when its callback can reach the session and
device authentication when the container holds a separate loopback interface.

The ambient home is the operator's account, joined onto this process's home
directory as the module is imported and never onto a ``HOME`` a request hands
its session: that session must still authenticate as the operator, so the
login a worktree home is derived from stays the one this program was launched
as. Codex left to choose would read the request's — its default is ``.codex``
in the effective ``HOME``, as :meth:`ProviderLogin.selected_home` reads it —
and a homed session is never left that choice, because homing names its home
outright. Codex ignores an empty ``CODEX_HOME`` and canonicalises the home it
selects, which is what ``canonical_home`` declares.
"""
