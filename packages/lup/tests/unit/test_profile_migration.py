"""Accounts a checkout or the personal ``~/.lup`` registry keeps move to global once.

A checkout's ``.lup/profiles`` keeps working where it is, so moving one is a
choice; an account left in the personal registry is one no launch can select; and
a login copied rather than moved is two chains that diverge on the first
renewal. These pin that a run moves each account, carries one selection
without overriding the person's, never overwrites a name the destination
holds, and that running it again finds nothing left to do.
"""

import json
from pathlib import Path

import pytest

from lup.providers.claude.login import CLAUDE_LOGIN
from lup.providers.codex.login import CODEX_LOGIN
from lup.providers.profile_migration import migrate_profiles
from lup.providers.profile_tree import profile_directory
from lup.providers.user_config import UserConfigFile


@pytest.fixture
def config(tmp_path: Path) -> UserConfigFile:
    return UserConfigFile(tmp_path / "config" / "lup")


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    """A checkout keeping a signed-in Claude profile and a selection."""
    root = tmp_path / "checkout"
    kept = root / ".lup" / "profiles"
    home = kept / "work" / CLAUDE_LOGIN.home_subdir
    home.mkdir(parents=True)
    CLAUDE_LOGIN.credentials_path(home).write_text('{"login": 1}', encoding="utf-8")
    (kept / ".active").write_text("work\n", encoding="utf-8")
    return root


def test_a_checkouts_profiles_move_with_their_selection(
    checkout: Path, config: UserConfigFile, tmp_path: Path
) -> None:
    migration = migrate_profiles(checkout, config, tmp_path / "registry-home")

    home = profile_directory(CLAUDE_LOGIN, config).launch_home(None)
    assert home is not None
    assert home == config.profiles_root() / "work" / CLAUDE_LOGIN.home_subdir
    assert CLAUDE_LOGIN.logged_in(home)
    assert migration.selected == "work"
    assert [move.outcome for move in migration.moves] == ["moved"]
    assert not (checkout / ".lup" / "profiles").exists()


def test_running_it_again_finds_nothing_to_do(
    checkout: Path, config: UserConfigFile, tmp_path: Path
) -> None:
    migrate_profiles(checkout, config, tmp_path / "registry-home")
    settled = sorted(config.home.rglob("*"))

    again = migrate_profiles(checkout, config, tmp_path / "registry-home")

    assert again.lines() == ["nothing to migrate"]
    assert sorted(config.home.rglob("*")) == settled


def test_a_checkouts_profile_works_where_it_is_and_moving_it_is_optional(
    checkout: Path, config: UserConfigFile, tmp_path: Path
) -> None:
    kept = checkout / ".lup" / "profiles" / "work" / CLAUDE_LOGIN.home_subdir

    before = profile_directory(CLAUDE_LOGIN, config, checkout).launch_home(None)
    migrate_profiles(checkout, config, tmp_path / "registry-home")
    after = profile_directory(CLAUDE_LOGIN, config, checkout).launch_home(None)

    assert before == kept
    assert after == config.profiles_root() / "work" / CLAUDE_LOGIN.home_subdir


def test_a_selection_the_person_already_made_is_theirs(
    checkout: Path, config: UserConfigFile, tmp_path: Path
) -> None:
    (config.profiles_root() / "personal").mkdir(parents=True)
    config.select_profile("personal")

    migration = migrate_profiles(checkout, config, tmp_path / "registry-home")

    assert migration.selected is None
    assert config.load().profile == "personal"


def test_a_name_the_destination_holds_keeps_what_it_holds(
    checkout: Path, config: UserConfigFile, tmp_path: Path
) -> None:
    """The same person, signed in to Claude there and Codex here."""
    theirs = config.profiles_root() / "work" / CLAUDE_LOGIN.home_subdir
    theirs.mkdir(parents=True)
    CLAUDE_LOGIN.credentials_path(theirs).write_text('{"mine": 1}', encoding="utf-8")
    codex = checkout / ".lup" / "profiles" / "work" / CODEX_LOGIN.home_subdir
    codex.mkdir()

    migration = migrate_profiles(checkout, config, tmp_path / "registry-home")

    outcomes = {move.source.name: move.outcome for move in migration.moves}
    assert outcomes == {"claude-config": "kept", "codex-home": "moved"}
    assert CLAUDE_LOGIN.credentials_path(theirs).read_text() == '{"mine": 1}'
    assert (config.profiles_root() / "work" / CODEX_LOGIN.home_subdir).is_dir()
    left = checkout / ".lup" / "profiles" / "work" / CLAUDE_LOGIN.home_subdir
    assert CLAUDE_LOGIN.logged_in(left), "a conflict dropped the source"
    assert "merge it by hand" in migration.lines()[0]


def test_the_registry_moves_homes_it_made_and_links_the_rest(
    config: UserConfigFile, tmp_path: Path
) -> None:
    registry_home = tmp_path / "registry-home"
    made = registry_home / "homes" / "work"
    made.mkdir(parents=True)
    elsewhere = tmp_path / "their-own-home"
    elsewhere.mkdir()
    (registry_home / "profiles.json").write_text(
        json.dumps(
            {
                "profiles": {
                    "work": {"config_dir": str(made)},
                    "side": {"config_dir": str(elsewhere)},
                    "main": {"config_dir": str(CLAUDE_LOGIN.ambient_home)},
                },
                "active": "side",
            }
        ),
        encoding="utf-8",
    )

    migration = migrate_profiles(tmp_path / "no-checkout", config, registry_home)

    outcomes = {move.name: move.outcome for move in migration.moves}
    assert outcomes == {"main": "refused", "side": "linked", "work": "moved"}
    directory = profile_directory(CLAUDE_LOGIN, config)
    assert directory.launch_home("work") == (
        config.profiles_root() / "work" / "claude-config"
    )
    selected = directory.launch_home(None)
    assert selected is not None and selected.resolve() == elsewhere.resolve()
    assert elsewhere.is_dir(), "a home of their own choosing was moved"
    assert not (registry_home / "profiles.json").exists()
    assert not (registry_home / "homes").exists()
    assert migrate_profiles(
        tmp_path / "no-checkout", config, registry_home
    ).lines() == ["nothing to migrate"]
