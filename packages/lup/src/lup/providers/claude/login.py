"""Claude Code's own words for where it keeps a login.

A leaf on purpose: the harness declaration, the usage reader, and the profile
transform all need these spellings, and none of them should pull the session
runtime in to get them.
"""

from pathlib import Path

from lup.providers.login import ProviderLogin

# lup: ignore[constant-declaration] — the environment variable Claude Code reads
CLAUDE_CONFIG_DIR = "CLAUDE_CONFIG_DIR"

CLAUDE_LOGIN = ProviderLogin(
    config_home_env=CLAUDE_CONFIG_DIR,
    credentials_file=".credentials.json",
    credential_fields=["claudeAiOauth"],
    renewable=(
        '(.claudeAiOauth.refreshToken // "") != ""'
        " and .claudeAiOauth.refreshTokenExpiresAt > (now * 1000)"
    ),
    # Measured on Claude Code 2.1.285: a running session moved between a real
    # login, an invalid one and none, turn by turn, as its home's file changed.
    rereads_login=True,
    ambient_home=Path.home() / ".claude",
    ambient_home_nameable=False,
    editor_lockfiles="ide",
    home_subdir="claude-config",
    state_volume="claude",
    trust_document=".claude.json",
    # Read off a home Claude Code 2.1.282 wrote, and off the shared volume a
    # contained session had filled beside Codex. `cache`, `history.jsonl`,
    # `plugins`, `sessions` and `skills` are names both runtimes write.
    home_entries=[
        ".claude.json",
        ".credentials.json",
        ".last-cleanup",
        ".lup-login-handoff.lock",
        ".lup-seeded-.credentials.json.sha256",
        "CLAUDE.md",
        "agents",
        "backups",
        "cache",
        "commands",
        "daemon",
        "daemon.lock",
        "daemon.log",
        "daemon.status.json",
        "debug",
        "editor",
        "file-history",
        "gh-pr-status-cache.json",
        "history.jsonl",
        "ide",
        "jobs",
        "keybindings.json",
        "output-styles",
        "paste-cache",
        "plans",
        "plugins",
        "projects",
        "session-env",
        "sessions",
        "settings.json",
        "shell-snapshots",
        "skills",
        "statsig",
        "telemetry",
        "todos",
    ],
    home_debris=[".claude.json.tmp.*"],
)
"""Where Claude Code stores a completed login, and how to select one.

The file carries two deadlines and the renewal test reads the second.
``claudeAiOauth.expiresAt`` is the access token's, hours away and renewed
without anyone asking; ``refreshTokenExpiresAt`` is the refresh token's, weeks
away, and once it passes there is nothing left to renew with. `jq` states the
comparison in seconds and the file in milliseconds, which is what the factor of
a thousand reconciles.

The deadline is necessary and not sufficient, so the filter asks for the
credential before it asks how long the credential has. A logout, or a refresh
the server refuses, empties both token strings and leaves every other member
of the object untouched -- a file that is a login by every test reading only
the date, and inert by the one test that decides whether a request is
answered. Read as renewable it suppresses the seed, and the session it
suppresses it for opens demanding the one flow a contained session cannot
finish.

The ambient home is the operator's: joined onto this process's home
directory as the module is imported, never onto a ``HOME`` a request hands its
session. A request changes ``HOME`` for a tool its session runs, and that
session must still authenticate as the operator, so the account a derived home
is seeded from and linked back to stays the one this program was launched as.
Claude Code left to choose would join the request's instead — its default is
joined onto ``os.homedir()``, which reads ``HOME`` — and a homed session is
never left that choice, because homing names its configuration home outright.
Every other place asking for the default home reads it here.

The ambient home cannot be named. Claude Code (read from 2.1.282) keeps its
configuration document at ``join(CLAUDE_CONFIG_DIR || homedir(),
".claude.json")``: ``~/.claude.json`` with the variable unset, and
``~/.claude/.claude.json`` with it naming ``~/.claude`` — so a session pointed
at its own default by name starts without the theme, trust records and
projects the account keeps beside it.
"""
