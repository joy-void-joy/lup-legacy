"""Worktree-scoped Codex home selection and first-use initialization."""

import json
import plistlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import jwt
import tomlkit
from tomlkit.items import Table

from lup.providers.user_config import UserConfigFile
from lup.types import EnvVars
from lup.providers.codex.home import (
    CodexWorktreeHomeStore,
    login_state,
    select_codex_home,
    trust_project,
    seed_hook_trust,
)
from lup.providers.codex.trust import CodexHookReport, hooks_of, skipped
from lup.providers.codex.theme import (
    TextMateStyle,
    TextMateThemeDocument,
    claude_daltonized_theme,
)


ACCOUNT_CONFIG = """\
model = "gpt-personal"

[features]
hooks = true

[hooks]
PreToolUse = []

[hooks.state."lup@account:hooks/hooks.json:pre_tool_use:0:0"]
enabled = true

[marketplaces.account]
source_type = "local"
source = "/account"

[plugins."lup@account"]
enabled = true
"""

# Nanosecond precision, as the runtime actually writes it.
SEEDED_CREDENTIAL = (
    '{"auth_mode": "chatgpt", "tokens": {"refresh_token": "seeded"},'
    ' "last_refresh": "2026-07-23T22:11:59.592425868Z"}'
)
ROTATED_CREDENTIAL = (
    '{"auth_mode": "chatgpt", "tokens": {"refresh_token": "rotated"},'
    ' "last_refresh": "2026-08-05T19:23:03.507636146Z"}'
)


def credential_expiring(expiry: datetime) -> str:
    """An auth record whose access token states one expiry."""
    # Signed only so the token parses; the adapter never verifies it.
    token = jwt.encode({"exp": expiry}, key="x" * 32, algorithm="HS256")
    return json.dumps(
        {
            "auth_mode": "chatgpt",
            "tokens": {"access_token": token},
            "last_refresh": "2026-07-23T22:11:59.592425868Z",
        }
    )


def account_home_with(credential: str, root: Path) -> Path:
    """Build an account home holding one credential."""
    account = root / "account"
    account.mkdir()
    (account / "auth.json").write_text(credential, encoding="utf-8")
    return account


def test_worktree_home_is_stable_and_lives_in_the_checkout(tmp_path: Path) -> None:
    """Two checkouts are two homes, and each sits inside the checkout it serves.

    What a shared root needed a path digest to guarantee, distinct checkouts
    now give for free — so the assertion is where the home is, not what it is
    named."""
    store = CodexWorktreeHomeStore(account_home=tmp_path / "account")
    first = tmp_path / "tree" / "first"
    second = tmp_path / "tree" / "second"
    first.mkdir(parents=True)
    second.mkdir()

    assert store.home_for(first) == store.home_for(first)
    assert store.home_for(first) != store.home_for(second)
    assert store.home_for(first) == first / ".lup" / "codex-home"
    assert first in store.home_for(first).parents


def test_a_worktree_below_a_project_answers_with_the_project_home(
    tmp_path: Path,
) -> None:
    """One checkout keeps one home, reached from anywhere inside it.

    A session opened in a subdirectory must not open a second home beneath
    itself — the credential would be seeded twice and the two would rotate
    independently."""
    root = tmp_path / "project"
    (root / "src" / "deep").mkdir(parents=True)
    (root / "pyproject.toml").write_text(
        '[tool.lup]\nversion = "1.0.0"\n', encoding="utf-8"
    )
    store = CodexWorktreeHomeStore(account_home=tmp_path / "account")

    assert store.home_for(root / "src" / "deep") == root / ".lup" / "codex-home"
    assert store.home_for(root) == store.home_for(root / "src" / "deep")


def test_prepare_seeds_auth_and_sanitized_personal_settings(tmp_path: Path) -> None:
    account = tmp_path / "account"
    account.mkdir()
    auth = account / "auth.json"
    auth.write_text('{"token": "secret"}\n', encoding="utf-8")
    auth.chmod(0o600)
    (account / "config.toml").write_text(ACCOUNT_CONFIG, encoding="utf-8")
    worktree = tmp_path / "tree" / "dev"
    worktree.mkdir(parents=True)
    store = CodexWorktreeHomeStore(account)

    scoped = store.prepare(worktree)

    assert (scoped / "auth.json").read_bytes() == auth.read_bytes()
    assert (scoped / "auth.json").stat().st_mode & 0o777 == 0o600
    assert scoped.stat().st_mode & 0o777 == 0o700
    config = tomlkit.parse((scoped / "config.toml").read_text(encoding="utf-8"))
    assert config["model"] == "gpt-personal"
    assert config["features"]["hooks"] is True
    assert "marketplaces" not in config
    assert "plugins" not in config
    hooks = config.item("hooks")
    assert isinstance(hooks, Table)
    assert "PreToolUse" in hooks
    # Trust is seeded, unlike installed state. The runtime will not run a
    # plugin's hooks until they are reviewed, so a scoped home that dropped
    # this installs the policy plugin and then runs ungoverned — present,
    # never consulted, and silent about it.
    state = hooks.item("state")
    assert isinstance(state, Table)
    assert "lup@account:hooks/hooks.json:pre_tool_use:0:0" in state


def test_claude_daltonized_theme_uses_truecolor_palette() -> None:
    theme = claude_daltonized_theme()
    rules = {
        rule.name: rule.settings
        for rule in theme.document.settings
        if rule.name is not None
    }
    assert theme.document.settings[0].settings.foreground == "#F8F8F2"
    assert rules["Comments"].foreground == "#75715E"
    assert rules["Keywords and operators"].foreground == "#F92672"
    assert rules["Storage"].foreground == "#66D9EF"
    assert rules["Strings"].foreground == "#E6DB74"
    assert rules["Numbers and constants"].foreground == "#BE84FF"
    assert rules["Diff additions"] == TextMateStyle(
        background="#001B29", foreground="#51A0C8"
    )
    assert rules["Diff deletions"] == TextMateStyle(
        background="#3D0100", foreground="#DC5A5A"
    )


def home_theme(home: Path) -> object:
    """The theme a home's configuration draws, or None where it names none."""
    config = home / "config.toml"
    if not config.is_file():
        return None
    return (
        tomlkit.parse(config.read_text(encoding="utf-8"))
        .unwrap()
        .get("tui", {})
        .get("theme")
    )


def worktree_of(tmp_path: Path, name: str = "worktree") -> Path:
    """A checkout for a store to derive a home for."""
    worktree = tmp_path / name
    worktree.mkdir()
    return worktree


def test_lups_theme_is_drawn_only_where_the_account_names_none(
    tmp_path: Path,
) -> None:
    account = tmp_path / "account"
    account.mkdir()
    (account / "config.toml").write_text(ACCOUNT_CONFIG, encoding="utf-8")
    worktree = worktree_of(tmp_path)
    store = CodexWorktreeHomeStore(account)
    theme = claude_daltonized_theme()
    parsed_theme = TextMateThemeDocument.model_validate(
        plistlib.loads(theme.render().encode("utf-8"))
    )
    assert parsed_theme == theme.document

    scoped = store.prepare(worktree)
    generated = scoped / "themes" / "claude-daltonized.tmTheme"

    assert generated.read_text(encoding="utf-8") == theme.render()
    assert home_theme(scoped) == "claude-daltonized"
    assert home_theme(account) is None, "the account was given lup's theme"

    generated.write_text("stale", encoding="utf-8")
    store.prepare(worktree)

    assert generated.read_text(encoding="utf-8") == theme.render()


def test_a_theme_the_account_keeps_is_drawn_with_its_file(tmp_path: Path) -> None:
    account = tmp_path / "account"
    (account / "themes").mkdir(parents=True)
    (account / "themes" / "mine.tmTheme").write_text("mine", encoding="utf-8")
    (account / "config.toml").write_text(
        ACCOUNT_CONFIG + '\n[tui]\ntheme = "mine"\n', encoding="utf-8"
    )

    scoped = CodexWorktreeHomeStore(account).prepare(worktree_of(tmp_path))

    assert home_theme(scoped) == "mine"
    assert (scoped / "themes" / "mine.tmTheme").read_text(encoding="utf-8") == "mine"


def test_a_theme_the_config_names_wins_in_the_home_and_leaves_the_account(
    tmp_path: Path,
) -> None:
    account = tmp_path / "account"
    account.mkdir()
    (account / "config.toml").write_text(
        ACCOUNT_CONFIG + '\n[tui]\ntheme = "zenburn"\n', encoding="utf-8"
    )
    worktree = worktree_of(tmp_path)
    store = CodexWorktreeHomeStore(account, theme="dracula")

    scoped = store.prepare(worktree)

    assert home_theme(scoped) == "dracula"
    assert home_theme(account) == "zenburn"
    assert (
        store.return_settings(worktree, UserConfigFile(tmp_path / "lup")).carried()
        == []
    )
    assert home_theme(account) == "zenburn"


def chosen_in_session(scoped: Path, theme: str) -> None:
    """What a session's /theme writes into the home it runs in."""
    config = tomlkit.parse((scoped / "config.toml").read_text(encoding="utf-8"))
    config.setdefault("tui", tomlkit.table())["theme"] = theme
    (scoped / "config.toml").write_text(tomlkit.dumps(config), encoding="utf-8")


def test_a_sessions_theme_returns_to_the_lup_config_and_its_file_to_the_account(
    tmp_path: Path,
) -> None:
    account = tmp_path / "account"
    account.mkdir()
    (account / "config.toml").write_text(ACCOUNT_CONFIG, encoding="utf-8")
    worktree = worktree_of(tmp_path)
    store = CodexWorktreeHomeStore(account)
    scoped = store.prepare(worktree)
    (scoped / "themes" / "mine.tmTheme").write_text("mine", encoding="utf-8")
    chosen_in_session(scoped, "mine")

    config = UserConfigFile(tmp_path / "lup")
    assert store.return_settings(worktree, config).carried() == ["theme.codex"]
    assert config.load().theme.codex == "mine"
    assert home_theme(account) is None
    assert (account / "themes" / "mine.tmTheme").read_text(encoding="utf-8") == "mine"

    elsewhere = CodexWorktreeHomeStore(
        account, theme=config.load().theme.codex
    ).prepare(worktree_of(tmp_path, "new"))
    assert home_theme(elsewhere) == "mine"
    assert (elsewhere / "themes" / "mine.tmTheme").is_file()


def test_lups_filled_theme_never_returns_to_the_account(tmp_path: Path) -> None:
    """Only what a session chose goes back; what lup drew for it does not."""
    account = tmp_path / "account"
    account.mkdir()
    (account / "config.toml").write_text(ACCOUNT_CONFIG, encoding="utf-8")
    worktree = worktree_of(tmp_path)
    store = CodexWorktreeHomeStore(account)
    scoped = store.prepare(worktree)
    config = tomlkit.parse((scoped / "config.toml").read_text(encoding="utf-8"))
    config["model"] = "gpt-chosen"
    (scoped / "config.toml").write_text(tomlkit.dumps(config), encoding="utf-8")

    returned = store.return_settings(worktree, UserConfigFile(tmp_path / "lup"))
    assert returned.carried() == []
    assert returned.session == ["model"]
    assert home_theme(account) is None


def test_a_sessions_theme_returns_without_undoing_the_accounts_meanwhile(
    tmp_path: Path,
) -> None:
    account = tmp_path / "account"
    account.mkdir()
    (account / "config.toml").write_text(
        ACCOUNT_CONFIG + '\n[tui]\ntheme = "zenburn"\n', encoding="utf-8"
    )
    worktree = worktree_of(tmp_path)
    store = CodexWorktreeHomeStore(account)
    scoped = store.prepare(worktree)
    (account / "config.toml").write_text(
        ACCOUNT_CONFIG.replace("gpt-personal", "gpt-meanwhile")
        + '\n[tui]\ntheme = "zenburn"\n',
        encoding="utf-8",
    )
    chosen_in_session(scoped, "dracula")

    config = UserConfigFile(tmp_path / "lup")
    assert store.return_settings(worktree, config).carried() == ["theme.codex"]
    settings = tomlkit.parse((account / "config.toml").read_text(encoding="utf-8"))
    assert settings["model"] == "gpt-meanwhile"
    assert home_theme(account) == "zenburn"
    assert config.load().theme.codex == "dracula"


def test_an_account_theme_changed_meanwhile_is_not_undone(tmp_path: Path) -> None:
    account = tmp_path / "account"
    account.mkdir()
    (account / "config.toml").write_text(
        ACCOUNT_CONFIG + '\n[tui]\ntheme = "zenburn"\n', encoding="utf-8"
    )
    worktree = worktree_of(tmp_path)
    store = CodexWorktreeHomeStore(account)
    store.prepare(worktree)
    (account / "config.toml").write_text(
        ACCOUNT_CONFIG + '\n[tui]\ntheme = "monokai"\n', encoding="utf-8"
    )

    assert (
        store.return_settings(worktree, UserConfigFile(tmp_path / "lup")).carried()
        == []
    )
    assert home_theme(account) == "monokai"


def test_prepare_keeps_what_the_home_itself_holds(tmp_path: Path) -> None:
    """A newer login, a transcript and the home's own installs survive a launch."""
    account = tmp_path / "account"
    account.mkdir()
    (account / "auth.json").write_text("account-auth", encoding="utf-8")
    (account / "config.toml").write_text(ACCOUNT_CONFIG, encoding="utf-8")
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    store = CodexWorktreeHomeStore(account)
    scoped = store.prepare(worktree)
    (scoped / "auth.json").write_text("scoped-auth", encoding="utf-8")
    config = tomlkit.parse((scoped / "config.toml").read_text(encoding="utf-8"))
    config["plugins"] = {"lup@worktree": {"enabled": True}}
    (scoped / "config.toml").write_text(tomlkit.dumps(config), encoding="utf-8")
    session = scoped / "sessions" / "saved.jsonl"
    session.parent.mkdir()
    session.write_text("saved\n", encoding="utf-8")
    (account / "auth.json").write_text("different-account-auth", encoding="utf-8")

    assert store.prepare(worktree) == scoped
    assert (scoped / "auth.json").read_text(encoding="utf-8") == "scoped-auth"
    settings = tomlkit.parse((scoped / "config.toml").read_text(encoding="utf-8"))
    assert settings["plugins"] == {"lup@worktree": {"enabled": True}}
    assert session.read_text(encoding="utf-8") == "saved\n"


def test_a_setting_changed_in_the_account_reaches_an_existing_home(
    tmp_path: Path,
) -> None:
    """Seeded once, a home kept the account as it stood the day it was made."""
    account = tmp_path / "account"
    account.mkdir()
    (account / "config.toml").write_text(ACCOUNT_CONFIG, encoding="utf-8")
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    store = CodexWorktreeHomeStore(account)
    scoped = store.prepare(worktree)

    (account / "config.toml").write_text(
        ACCOUNT_CONFIG.replace("gpt-personal", "gpt-since")
        + '\n[tui]\ntheme = "zenburn"\n',
        encoding="utf-8",
    )
    store.prepare(worktree)

    settings = tomlkit.parse((scoped / "config.toml").read_text(encoding="utf-8"))
    assert settings["model"] == "gpt-since"
    assert settings["tui"] == {"theme": "zenburn"}
    assert str(worktree.resolve()) in settings["projects"]


def test_a_session_preference_returns_to_the_account_and_reaches_a_new_checkout(
    tmp_path: Path,
) -> None:
    """The reset this ends: a preference set in one project, absent in the next."""
    account = tmp_path / "account"
    account.mkdir()
    (account / "config.toml").write_text("# mine\n" + ACCOUNT_CONFIG, encoding="utf-8")
    first = tmp_path / "first"
    first.mkdir()
    store = CodexWorktreeHomeStore(account)
    scoped = store.prepare(first)
    config = tomlkit.parse((scoped / "config.toml").read_text(encoding="utf-8"))
    config["notice"] = {"hide_full_access_warning": True}
    config["model"] = "gpt-chosen"
    config["file_opener"] = "none"
    config["tui"]["animations"] = False
    config["plugins"] = {"lup@first": {"enabled": True}}
    (scoped / "config.toml").write_text(tomlkit.dumps(config), encoding="utf-8")
    person = UserConfigFile(tmp_path / "lup")

    returned = store.return_settings(first, person)

    assert returned.carried() == ["file_opener", "tui.animations"]
    assert returned.session == ["model"]
    assert returned.withheld == ["notice"]
    kept = (account / "config.toml").read_text(encoding="utf-8")
    assert kept.startswith("# mine")
    account_settings = tomlkit.parse(kept)
    assert account_settings["model"] == "gpt-personal"
    assert account_settings["file_opener"] == "none"
    assert "notice" not in account_settings
    assert "lup@first" not in account_settings["plugins"]
    assert str(first.resolve()) not in account_settings.get("projects", {})
    assert store.return_settings(first, person).carried() == []

    second = tmp_path / "second"
    second.mkdir()
    fresh = tomlkit.parse(
        (store.prepare(second) / "config.toml").read_text(encoding="utf-8")
    )
    assert fresh["model"] == "gpt-personal"
    assert fresh["file_opener"] == "none"
    assert fresh["tui"]["animations"] is False


def test_a_session_leaves_an_account_change_made_meanwhile_alone(
    tmp_path: Path,
) -> None:
    """Measured against the launch, not the account, so it undoes nothing."""
    account = tmp_path / "account"
    account.mkdir()
    (account / "config.toml").write_text(ACCOUNT_CONFIG, encoding="utf-8")
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    store = CodexWorktreeHomeStore(account)
    store.prepare(worktree)
    (account / "config.toml").write_text(
        ACCOUNT_CONFIG.replace("gpt-personal", "gpt-meanwhile"), encoding="utf-8"
    )

    assert (
        store.return_settings(worktree, UserConfigFile(tmp_path / "lup")).carried()
        == []
    )
    settings = tomlkit.parse((account / "config.toml").read_text(encoding="utf-8"))
    assert settings["model"] == "gpt-meanwhile"


def test_a_first_sign_in_under_a_new_profile_reaches_its_account_home(
    tmp_path: Path,
) -> None:
    """A profile's Codex home need not exist until its first login lands in it."""
    account = tmp_path / "profiles" / "work" / "codex-home"
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    store = CodexWorktreeHomeStore(account)
    scoped = store.prepare(worktree)
    (scoped / "auth.json").write_text(SEEDED_CREDENTIAL, encoding="utf-8")

    assert store.publish(worktree)
    assert (account / "auth.json").read_text(encoding="utf-8") == SEEDED_CREDENTIAL


def test_a_scoped_home_trusts_the_checkout_it_was_made_for(tmp_path: Path) -> None:
    """Untrusted, the runtime reads none of a project's own configuration.

    Which is where a generated tree declares its tool servers, so the gap
    shows up as a session with no instruments rather than as an error.
    """
    account = tmp_path / "account"
    account.mkdir()
    (account / "config.toml").write_text(ACCOUNT_CONFIG, encoding="utf-8")
    worktree = tmp_path / "worktree"
    worktree.mkdir()

    scoped = CodexWorktreeHomeStore(account).prepare(worktree)

    config = tomlkit.parse((scoped / "config.toml").read_text(encoding="utf-8"))
    projects = config.item("projects")
    assert isinstance(projects, Table)
    assert projects[str(worktree.resolve())]["trust_level"] == "trusted"
    assert config["model"] == "gpt-personal"


def test_a_home_made_before_the_trust_was_written_gets_it_on_next_use(
    tmp_path: Path,
) -> None:
    """A home that predates this is exactly the one silently serving nothing."""
    account = tmp_path / "account"
    account.mkdir()
    (account / "config.toml").write_text(ACCOUNT_CONFIG, encoding="utf-8")
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    store = CodexWorktreeHomeStore(account)
    scoped = store.prepare(worktree)
    (scoped / "config.toml").write_text('model = "scoped"\n', encoding="utf-8")

    store.prepare(worktree)

    config = tomlkit.parse((scoped / "config.toml").read_text(encoding="utf-8"))
    projects = config.item("projects")
    assert isinstance(projects, Table)
    assert str(worktree.resolve()) in projects


def test_a_trust_the_operator_already_recorded_is_left_alone(tmp_path: Path) -> None:
    """Their decision about their own checkout, whatever they decided."""
    account = tmp_path / "account"
    account.mkdir()
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    scoped = CodexWorktreeHomeStore(account).home_for(worktree)
    scoped.mkdir(parents=True)
    (scoped / "config.toml").write_text(
        f'[projects."{worktree.resolve()}"]\ntrust_level = "on-request"\n',
        encoding="utf-8",
    )

    assert not trust_project(scoped, worktree)
    config = tomlkit.parse((scoped / "config.toml").read_text(encoding="utf-8"))
    projects = config.item("projects")
    assert isinstance(projects, Table)
    assert projects[str(worktree.resolve())]["trust_level"] == "on-request"


def test_an_explicit_home_is_never_written_to(tmp_path: Path) -> None:
    """An operator's own home carries their decisions, not ours."""
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    explicit = tmp_path / "personal"
    explicit.mkdir()

    selection = select_codex_home(explicit, {}, worktree)

    assert selection.path == explicit
    assert not selection.isolated
    assert not (explicit / "config.toml").exists()


def test_explicit_home_and_environment_bypass_scoped_initialization(
    tmp_path: Path,
) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    store = CodexWorktreeHomeStore(account_home=tmp_path / "account")
    configured = tmp_path / "configured"
    environment: EnvVars = {"CODEX_HOME": str(configured)}

    from_environment = select_codex_home(None, environment, worktree, store=store)
    explicit = select_codex_home(
        tmp_path / "explicit", environment, worktree, store=store
    )

    assert from_environment.path == configured
    assert from_environment.isolated is False
    assert explicit.path == tmp_path / "explicit"
    assert explicit.isolated is False
    assert not (worktree / ".lup").exists()


def test_default_selection_prepares_the_scoped_home(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    store = CodexWorktreeHomeStore(account_home=tmp_path / "account")

    selection = select_codex_home(None, {}, worktree, store=store)

    assert selection.path == store.home_for(worktree)
    assert selection.isolated is True
    assert selection.path.is_dir()


def test_an_empty_codex_home_names_no_home(tmp_path: Path) -> None:
    """Codex ignores an empty ``CODEX_HOME`` and falls back to its default.

    Read as a path it would be ``.``, and the session routed there would
    have its policy installed into whatever directory the caller stood in.
    """
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    store = CodexWorktreeHomeStore(account_home=tmp_path / "account")

    selection = select_codex_home(None, {"CODEX_HOME": ""}, worktree, store=store)

    assert selection.path == store.home_for(worktree)
    assert selection.isolated is True


def test_a_rotated_account_login_reaches_a_stale_scoped_home(tmp_path: Path) -> None:
    account = account_home_with(ROTATED_CREDENTIAL, tmp_path)
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    store = CodexWorktreeHomeStore(account)
    scoped = store.prepare(worktree)
    (scoped / "auth.json").write_text(SEEDED_CREDENTIAL, encoding="utf-8")

    assert store.prepare(worktree) == scoped
    assert (scoped / "auth.json").read_text(encoding="utf-8") == ROTATED_CREDENTIAL


def test_a_rotation_inside_a_scoped_home_returns_to_the_account(
    tmp_path: Path,
) -> None:
    account = account_home_with(SEEDED_CREDENTIAL, tmp_path)
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    store = CodexWorktreeHomeStore(account)
    scoped = store.prepare(worktree)
    (scoped / "auth.json").write_text(ROTATED_CREDENTIAL, encoding="utf-8")

    assert store.publish(worktree) is True
    assert (account / "auth.json").read_text(encoding="utf-8") == ROTATED_CREDENTIAL


def test_publishing_an_unrotated_login_changes_nothing(tmp_path: Path) -> None:
    account = account_home_with(SEEDED_CREDENTIAL, tmp_path)
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    store = CodexWorktreeHomeStore(account)
    store.prepare(worktree)

    assert store.publish(worktree) is False


def test_an_unreadable_credential_is_never_overwritten(tmp_path: Path) -> None:
    account = account_home_with("not-json", tmp_path)
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    store = CodexWorktreeHomeStore(account)
    scoped = store.prepare(worktree)
    (scoped / "auth.json").write_text(ROTATED_CREDENTIAL, encoding="utf-8")

    assert store.prepare(worktree) == scoped
    assert (scoped / "auth.json").read_text(encoding="utf-8") == ROTATED_CREDENTIAL


def test_a_home_with_no_record_holds_no_login(tmp_path: Path) -> None:
    assert login_state(tmp_path).usable_at(datetime.now(UTC)) is False


def test_a_login_is_usable_until_the_issuer_says_otherwise(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    expiry = now + timedelta(days=5)
    (tmp_path / "auth.json").write_text(credential_expiring(expiry), encoding="utf-8")

    state = login_state(tmp_path)

    assert state.present is True
    assert state.expires_at == expiry.replace(microsecond=0)
    assert state.usable_at(now) is True
    assert state.usable_at(expiry + timedelta(seconds=1)) is False


def test_a_lapsed_login_is_caught_before_a_session_starts(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    lapsed = credential_expiring(now - timedelta(days=7))
    (tmp_path / "auth.json").write_text(lapsed, encoding="utf-8")

    assert login_state(tmp_path).usable_at(now) is False


def test_a_record_stating_no_expiry_is_left_to_the_runtime(tmp_path: Path) -> None:
    (tmp_path / "auth.json").write_text(SEEDED_CREDENTIAL, encoding="utf-8")

    assert login_state(tmp_path).usable_at(datetime.now(UTC)) is True


def reported(hooks: list[dict[str, object]], **listing: object) -> CodexHookReport:
    """One ``hooks/list`` answer, spelled the way the runtime spells one."""
    return CodexHookReport.model_validate(
        {"data": [{"cwd": "/checkout", "hooks": hooks, **listing}]}
    )


def resolved_hook(key: str, **overrides: object) -> dict[str, object]:
    """One hook as the runtime reports it, trusted and enabled by default."""
    return {
        "key": f"lup@proj:hooks/hooks.json:{key}",
        "eventName": "preToolUse",
        "pluginId": "lup@proj",
        "source": "plugin",
        "enabled": True,
        "isManaged": False,
        "currentHash": "sha256:whatever-this-was-when-reviewed",
        "trustStatus": "trusted",
        **overrides,
    }


def test_a_home_trusting_every_reported_hook_would_run_them_all() -> None:
    """The governed reading, so the refusal cannot pass by being unable to fail."""
    report = reported(
        [resolved_hook("pre_tool_use:0:0"), resolved_hook("permission_request:0:0")]
    )

    assert skipped(report, "lup@proj") == []


def test_user_hooks_have_no_plugin_identity() -> None:
    report = reported([resolved_hook("user-hook", pluginId=None, source="user")])
    assert report.resolved()[0].plugin_id is None
    assert hooks_of(report, "lup@proj") == []


def test_a_managed_hook_needs_no_record_of_its_own() -> None:
    """Policy trusted it, which is a state a home's own records cannot show.

    A check reading the home's TOML sees no entry and calls this untrusted,
    refusing a session that is governed — which is the false refusal an
    adopter under managed configuration would meet first.
    """
    report = reported([resolved_hook("pre_tool_use:0:0", trustStatus="managed")])

    assert skipped(report, "lup@proj") == []


def test_a_regenerated_hook_reads_as_modified_rather_than_trusted() -> None:
    """The state a generated plugin reaches constantly, and the one that hid.

    Trust was granted, the declaration was regenerated, and the recorded
    digest now describes bytes that are gone. Codex skips it exactly as it
    skips one never answered for.
    """
    report = reported([resolved_hook("pre_tool_use:0:0", trustStatus="modified")])

    assert [hook.key for hook in skipped(report, "lup@proj")] == [
        "lup@proj:hooks/hooks.json:pre_tool_use:0:0"
    ]


def test_a_home_trusting_one_event_of_three_names_the_other_two() -> None:
    """The measured case, and why a plugin being installed proves nothing.

    An operator's home carried trust for `pre_tool_use` alone. A shell command
    is gated by `permission_request`, so the session ran the command the
    policy refuses — while carrying that policy, enabled, and reading exactly
    like a governed session.
    """
    report = reported(
        [
            resolved_hook("pre_tool_use:0:0"),
            resolved_hook("post_tool_use:0:0", trustStatus="untrusted"),
            resolved_hook("permission_request:0:0", trustStatus="untrusted"),
        ]
    )

    assert [hook.key for hook in skipped(report, "lup@proj")] == [
        "lup@proj:hooks/hooks.json:post_tool_use:0:0",
        "lup@proj:hooks/hooks.json:permission_request:0:0",
    ]


def test_a_hook_trusted_and_then_disabled_is_one_that_will_not_run() -> None:
    """Trust and enablement are two records, and either one off is a skip."""
    report = reported([resolved_hook("pre_tool_use:0:0", enabled=False)])

    assert [hook.key for hook in skipped(report, "lup@proj")] == [
        "lup@proj:hooks/hooks.json:pre_tool_use:0:0"
    ]


def test_another_plugin_s_untrusted_hook_is_not_this_project_s_refusal() -> None:
    """A home carries the operator's own plugins, and those are their decision."""
    report = reported(
        [
            resolved_hook("pre_tool_use:0:0"),
            {
                **resolved_hook("pre_tool_use:0:0", trustStatus="untrusted"),
                "pluginId": "somebody-else@theirs",
            },
        ]
    )

    assert skipped(report, "lup@proj") == []


def test_a_plugin_declaring_no_hooks_has_nothing_to_trust() -> None:
    """A project whose plugin carries only skills is not refused over hooks."""
    assert skipped(reported([]), "lup@proj") == []


def test_what_the_runtime_could_not_resolve_is_carried_rather_than_dropped() -> None:
    """A manifest that would not parse arrives as a directory with no hooks."""
    report = reported([], warnings=["clamping SessionEnd hook timeout to 3s"])

    assert report.unresolved() == ["clamping SessionEnd hook timeout to 3s"]


def test_seeded_trust_records_the_hash_the_runtime_reported(tmp_path: Path) -> None:
    """A record either names the definition Codex holds, or is worth nothing.

    The digest is the runtime's, never recomputed here: one computed over a
    canonical form this does not own would read as `modified` and be skipped
    exactly like a record never written.
    """
    home = tmp_path / "home"
    home.mkdir()
    report = reported(
        [resolved_hook("pre_tool_use:0:0", currentHash="sha256:as-generated")]
    )

    written = seed_hook_trust(home, skipped(report, "lup@proj"))

    assert written == []
    seeded = seed_hook_trust(home, hooks_of(report, "lup@proj"))
    document = tomlkit.parse((home / "config.toml").read_text(encoding="utf-8"))
    state = document["hooks"]["state"]["lup@proj:hooks/hooks.json:pre_tool_use:0:0"]
    assert seeded == ["lup@proj:hooks/hooks.json:pre_tool_use:0:0"]
    assert state["trusted_hash"] == "sha256:as-generated"
    assert state["enabled"] is True


def test_seeding_keeps_what_the_home_already_recorded(tmp_path: Path) -> None:
    """A home's own settings are not collateral of answering for one hook."""
    home = tmp_path / "home"
    home.mkdir()
    trust_project(home, tmp_path / "checkout")

    seed_hook_trust(home, hooks_of(reported([resolved_hook("pre:0:0")]), "lup@proj"))

    document = tomlkit.parse((home / "config.toml").read_text(encoding="utf-8"))
    assert str(tmp_path / "checkout") in document["projects"]


def test_only_a_home_this_store_derived_may_be_written_into(tmp_path: Path) -> None:
    """Whose decisions a home holds is the whole of whether lup may answer."""
    store = CodexWorktreeHomeStore(account_home=tmp_path / "account")

    assert store.derived(store.home_for(tmp_path / "checkout"))
    assert not store.derived(tmp_path / "account")
