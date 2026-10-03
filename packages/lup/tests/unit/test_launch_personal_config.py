"""A launch in a project it has never seen opens the way the person left it.

The complaint this answers: every new repository reset the account, the
theme and the defaults, because each was kept per checkout or left to a CLI
starting from nothing. A launch reads them from the person's lup config
instead, so a fresh project inherits them; a project's mode and a flag on the
command line still overrule it, in that order. `harness claude|codex` reaches
all of it through the declaration it launches, as a program does.
"""

import json
from pathlib import Path
from unittest.mock import Mock

import pytest
import sh

import lup.devtools.harness.launch as launch
from lup.devtools.utils import Refusal
import lup.providers.claude.launch as claude_launch
import lup.providers.codex.launch as codex_launch
import lup.providers.profile_tree as profile_tree
from lup.launch.declaration import LaunchSandbox
from lup.providers.claude.config_home import (
    ClaudeConfigHome,
    load_document,
    save_document,
    selected_config_home,
)
from lup.providers.claude.login import CLAUDE_CONFIG_DIR, CLAUDE_LOGIN
from lup.providers.claude.usage.reader import ClaudeUsageReader, claude_usage_entry
from lup.providers.codex.home import CodexWorktreeHomeStore
from lup.providers.codex.login import CODEX_LOGIN
from lup.providers.codex.preferences import CodexSettingsReturn
from lup.providers.codex.usage.reader import CodexUsageReader, codex_usage_entry
from lup.providers.profile_tree import profile_directory
from lup.providers.profiles import ProfileDirectory
from lup.providers.user_config import UserConfigFile
from lup.types import EnvVars, JsonValue
from tests.unit.harness_launch import Caught, composition, stub_host


@pytest.fixture
def config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> UserConfigFile:
    """A person's config home, found the way a launch finds one."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    return UserConfigFile()


def writes(config: UserConfigFile, content: str) -> None:
    config.path().parent.mkdir(parents=True, exist_ok=True)
    config.path().write_text(content, encoding="utf-8")


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A project nothing has launched in before."""
    fresh = tmp_path / "fresh-project"
    fresh.mkdir()
    monkeypatch.setattr(profile_tree, "project_root", lambda: fresh)
    return fresh


@pytest.fixture
def launched(project: Path, monkeypatch: pytest.MonkeyPatch) -> Caught:
    """Stub every measurement of the host, and keep what each CLI would be handed."""
    return stub_host(monkeypatch, project)


def flag(arguments: list[str], name: str) -> str | None:
    """The value following ``name`` on a command line, where it appears."""
    return arguments[arguments.index(name) + 1] if name in arguments else None


def settings(arguments: list[str]) -> dict[str, JsonValue]:
    """The one ``--settings`` document a Claude command line carries."""
    return json.loads(arguments[arguments.index("--settings") + 1])


def claude(
    project: Path,
    config: UserConfigFile,
    model: str | None = None,
    effort: str | None = None,
    profile: str | None = None,
    accounts: ProfileDirectory | None = None,
    sandbox: LaunchSandbox = LaunchSandbox.INNER,
) -> None:
    """``harness claude`` in the fresh project, over the person's accounts."""
    launch.launch_claude(
        composition(project, "claude"),
        launch.LaunchArguments(
            model=model, effort=effort, profile=profile, sandbox=sandbox
        ),
        accounts or profile_directory(CLAUDE_LOGIN, config),
        False,
    )


def codex(project: Path) -> None:
    """``harness codex`` in the fresh project."""
    launch.launch_codex(
        composition(project, "codex"),
        launch.LaunchArguments(sandbox=LaunchSandbox.INNER),
        None,
        False,
        False,
    )


def test_a_person_who_wrote_nothing_launches_on_lups_defaults(
    project: Path, config: UserConfigFile, launched: Caught
) -> None:
    claude(project, config)

    assert flag(launched.argv, "--model") == "opus"
    assert flag(launched.argv, "--effort") == "xhigh"
    assert CLAUDE_CONFIG_DIR not in launched.env


def test_a_fresh_project_inherits_the_persons_account_theme_and_defaults(
    project: Path, config: UserConfigFile, launched: Caught
) -> None:
    home = (
        profile_directory(CLAUDE_LOGIN, config).add("work", scope="global").config_dir
    )
    writes(
        config,
        'profile = "work"\ntier = "balanced"\neffort = "high"\n\n'
        '[theme]\nclaude = "light-daltonized"\n',
    )

    claude(project, config)

    assert flag(launched.argv, "--model") == "sonnet"
    assert flag(launched.argv, "--effort") == "high"
    assert load_document(home / ".claude.json")["theme"] == "light-daltonized"
    assert launched.env[CLAUDE_CONFIG_DIR] == str(home)


def test_a_flag_on_the_command_line_overrules_the_person(
    project: Path, config: UserConfigFile, launched: Caught
) -> None:
    writes(config, 'tier = "balanced"\neffort = "high"\n')

    claude(project, config, model="opus", effort="max")

    assert flag(launched.argv, "--model") == "opus"
    assert flag(launched.argv, "--effort") == "max"


def test_the_persons_effort_steps_down_to_one_the_model_takes(
    project: Path, config: UserConfigFile, launched: Caught
) -> None:
    """A default adapts where a named effort would be refused."""
    writes(config, 'effort = "ultra"\n')

    claude(project, config, model="claude-opus-4-6")

    assert flag(launched.argv, "--effort") == "max"


def test_codex_launches_on_the_persons_tier_and_effort(
    project: Path, config: UserConfigFile, launched: Caught
) -> None:
    writes(config, 'tier = "balanced"\neffort = "high"\n')

    codex(project)

    assert flag(launched.argv, "--model") == "gpt-5.6-terra"
    assert 'model_reasoning_effort="high"' in launched.argv


def test_a_config_lup_cannot_read_refuses_the_launch_naming_it(
    project: Path, config: UserConfigFile, launched: Caught
) -> None:
    """Before any of the workflow around the session runs, as a flag is refused."""
    writes(config, 'tier = "enormous"\n')

    with pytest.raises(Refusal) as refused:
        claude(project, config)
    assert str(config.path()) in refused.value.said["why"]
    assert launched.events == []


@pytest.fixture
def account(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ClaudeConfigHome:
    """The operator's default Claude home, for this test alone."""
    home = ClaudeConfigHome(
        directory=tmp_path / "user" / ".claude",
        document=tmp_path / "user" / ".claude.json",
    )

    def selected(environment: EnvVars) -> ClaudeConfigHome:
        if environment.get(CLAUDE_CONFIG_DIR):
            return selected_config_home(environment)
        return home

    monkeypatch.setattr(claude_launch, "selected_config_home", selected)
    return home


def test_claude_fills_lups_theme_only_where_the_account_names_none(
    project: Path, config: UserConfigFile, launched: Caught, account: ClaudeConfigHome
) -> None:
    claude(project, config)

    assert load_document(account.document) == {"theme": "dark-daltonized"}
    assert "theme" not in settings(launched.argv)


def test_claude_leaves_a_theme_the_account_already_has(
    project: Path, config: UserConfigFile, launched: Caught, account: ClaudeConfigHome
) -> None:
    save_document(account.document, {"theme": "light", "editorMode": "vim"})

    claude(project, config)

    assert load_document(account.document) == {"theme": "light", "editorMode": "vim"}


def test_claude_leaves_a_theme_the_accounts_settings_hold(
    project: Path, config: UserConfigFile, launched: Caught, account: ClaudeConfigHome
) -> None:
    """Claude Code reads the settings' theme first, so that one is the account's."""
    held = account.directory / "settings.json"
    save_document(held, {"theme": "light-ansi"})

    claude(project, config)

    assert load_document(held) == {"theme": "light-ansi"}
    assert not account.document.exists()


def test_a_theme_the_config_names_wins_over_the_accounts(
    project: Path, config: UserConfigFile, launched: Caught, account: ClaudeConfigHome
) -> None:
    save_document(account.document, {"theme": "light", "editorMode": "vim"})
    writes(config, '[theme]\nclaude = "dark-ansi"\n')

    claude(project, config)

    assert load_document(account.document) == {
        "theme": "dark-ansi",
        "editorMode": "vim",
    }


def test_a_named_theme_is_written_where_the_account_keeps_its_own(
    project: Path, config: UserConfigFile, launched: Caught, account: ClaudeConfigHome
) -> None:
    held = account.directory / "settings.json"
    save_document(held, {"theme": "light", "model": "opus"})
    writes(config, '[theme]\nclaude = "dark-ansi"\n')

    claude(project, config)

    assert load_document(held) == {"theme": "dark-ansi", "model": "opus"}
    assert not account.document.exists()


def test_a_claude_sessions_theme_change_stays_the_accounts(
    project: Path,
    config: UserConfigFile,
    launched: Caught,
    account: ClaudeConfigHome,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A host session runs in the account's own home, so /theme lands there.

    Nothing after the session writes the document back, so a change the
    account took meanwhile — here another process setting its editor mode —
    survives beside the session's theme, and the next launch keeps both.
    """

    def session(_name: str) -> object:
        def run(*args: object, _env: EnvVars, **kwargs: object) -> None:
            del args, _env, kwargs
            chosen = {**load_document(account.document), "theme": "light"}
            save_document(account.document, {**chosen, "editorMode": "vim"})

        return run

    monkeypatch.setattr(sh, "Command", session)

    claude(project, config)

    assert load_document(account.document) == {"theme": "light", "editorMode": "vim"}
    claude(project, config)
    assert load_document(account.document)["theme"] == "light"


def test_a_contained_claude_launch_leaves_the_accounts_theme_alone(
    project: Path, config: UserConfigFile, launched: Caught, account: ClaudeConfigHome
) -> None:
    """Its session runs in the repository's config volume, not the account."""
    claude(project, config, sandbox=LaunchSandbox.OUTER)

    assert not account.document.exists()
    assert "theme" not in settings(launched.argv)


def recording_stores(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, object]]:
    """Every worktree home store a Codex launch derives, by what it was built from."""
    stores: list[dict[str, object]] = []

    def store(**named: object) -> Mock:
        stores.append(named)
        return Mock(
            spec=CodexWorktreeHomeStore,
            publish=Mock(return_value=False),
            return_settings=Mock(return_value=CodexSettingsReturn()),
        )

    monkeypatch.setattr(codex_launch, "CodexWorktreeHomeStore", store)
    return stores


def test_codex_hands_the_named_theme_to_the_home_not_the_command_line(
    project: Path,
    config: UserConfigFile,
    launched: Caught,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A launch-wide override would outrank the session's own /theme."""
    writes(config, '[theme]\ncodex = "dracula"\n')
    stores = recording_stores(monkeypatch)

    codex(project)

    assert stores[-1] == {
        "account_home": CODEX_LOGIN.ambient_home,
        "theme": "dracula",
        "editor": None,
        "settings": {},
    }
    assert not any("tui.theme" in argument for argument in launched.argv)


def test_codex_derives_its_worktree_home_from_the_selected_account(
    project: Path,
    config: UserConfigFile,
    launched: Caught,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One name, one account: the profile a Claude launch opens, on Codex too."""
    profile_directory(CLAUDE_LOGIN, config).add("work", scope="global")
    stores = recording_stores(monkeypatch)

    codex(project)

    assert stores[-1] == {
        "account_home": config.profiles_root() / "work" / "codex-home",
        "theme": None,
        "editor": None,
        "settings": {},
    }


@pytest.mark.parametrize("named", ["work", None], ids=["named", "selected"])
def test_the_usage_display_reads_the_account_a_launch_opens(
    project: Path, config: UserConfigFile, launched: Caught, named: str | None
) -> None:
    """One resolution for both, so a name cannot read one account and open another."""
    accounts = profile_directory(CLAUDE_LOGIN, config)
    accounts.add("personal", scope="global")
    accounts.add("work", scope="global")
    accounts.use("work", "global")
    claude_reader = claude_usage_entry().open(named)
    codex_reader = codex_usage_entry().open(named)

    claude(project, config, profile=named, accounts=accounts)

    assert isinstance(claude_reader, ClaudeUsageReader)
    assert isinstance(codex_reader, CodexUsageReader)
    assert str(claude_reader.config_dir) == launched.env[CLAUDE_CONFIG_DIR]
    assert codex_reader.home == config.profiles_root() / "work" / "codex-home"


@pytest.mark.parametrize("named", ["work", None], ids=["named", "selected"])
def test_the_usage_display_and_a_launch_agree_on_the_checkouts_own_profile(
    project: Path, config: UserConfigFile, launched: Caught, named: str | None
) -> None:
    """A checkout's profile of a name wins over the global one, for both."""
    accounts = profile_directory(CLAUDE_LOGIN, config)
    accounts.add("work", scope="global")
    accounts.add("work")
    accounts.use("work")
    kept = project / ".lup" / "profiles" / "work"
    claude_reader = claude_usage_entry().open(named)
    codex_reader = codex_usage_entry().open(named)

    claude(project, config, profile=named, accounts=accounts)

    assert isinstance(claude_reader, ClaudeUsageReader)
    assert isinstance(codex_reader, CodexUsageReader)
    assert launched.env[CLAUDE_CONFIG_DIR] == str(kept / "claude-config")
    assert str(claude_reader.config_dir) == launched.env[CLAUDE_CONFIG_DIR]
    assert codex_reader.home == kept / "codex-home"


def test_a_launch_opens_a_checkouts_profile_and_says_nothing_of_moving_it(
    project: Path,
    config: UserConfigFile,
    launched: Caught,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A checkout's own profiles are read where they are, with no notice."""
    kept = project / ".lup" / "profiles"
    (kept / "work" / CLAUDE_LOGIN.home_subdir).mkdir(parents=True)
    (kept / ".active").write_text("work\n", encoding="utf-8")

    claude(project, config)

    said = capsys.readouterr()
    assert launched.env[CLAUDE_CONFIG_DIR] == str(
        kept / "work" / CLAUDE_LOGIN.home_subdir
    )
    assert "migrate" not in said.out + said.err
    assert str(kept) not in said.out + said.err


def test_codex_derives_its_worktree_home_from_the_checkouts_selected_account(
    project: Path,
    config: UserConfigFile,
    launched: Caught,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The checkout's ``.active`` beats the config file's selection on Codex too."""
    accounts = profile_directory(CODEX_LOGIN, config)
    accounts.add("me", scope="global")
    accounts.add("work")
    accounts.use("work")
    kept = project / ".lup" / "profiles" / "work"
    stores = recording_stores(monkeypatch)

    codex(project)

    assert config.load().profile == "me"
    assert stores[-1] == {
        "account_home": kept / "codex-home",
        "theme": None,
        "editor": None,
        "settings": {},
    }
