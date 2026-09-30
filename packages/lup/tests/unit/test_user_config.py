"""One person's lup config: where it is, what it holds, and what it refuses.

A value read from the wrong place, or silently dropped, is a new project
opening signed out in the runtime's default theme again — the reset this file
exists to end. These pin the XDG location, lup's defaults for a person who
wrote nothing, a file that does not parse being refused rather than ignored,
and a selection written by a command leaving the person's own lines alone.
"""

from pathlib import Path

import pytest

from lup.providers.user_config import UserConfig, UserConfigFile, UserConfigHome


def written(home: Path, content: str) -> UserConfigFile:
    """A config home holding ``content`` as its config file."""
    config = UserConfigFile(home)
    config.path().parent.mkdir(parents=True, exist_ok=True)
    config.path().write_text(content, encoding="utf-8")
    return config


def test_the_config_home_follows_an_absolute_xdg_config_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))

    assert UserConfigHome().directory() == tmp_path / "xdg" / "lup"
    assert UserConfigFile().path() == tmp_path / "xdg" / "lup" / "config.toml"
    assert UserConfigFile().profiles_root() == tmp_path / "xdg" / "lup" / "profiles"


@pytest.mark.parametrize("named", ["", "relative/config"])
def test_an_empty_or_relative_xdg_config_home_falls_back_to_dot_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, named: str
) -> None:
    """The specification's rule: only an absolute path moves configuration."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", named)

    assert UserConfigHome().directory() == tmp_path / ".config" / "lup"


def test_a_person_who_wrote_nothing_gets_lups_defaults(tmp_path: Path) -> None:
    loaded = UserConfigFile(tmp_path / "lup").load()

    assert loaded == UserConfig()
    assert loaded.profile is None
    assert loaded.theme.claude is None
    assert loaded.theme.codex is None
    assert loaded.effort is None
    assert loaded.tier == "strongest"
    assert loaded.dashboard.reopen


def test_reopening_the_dashboard_is_the_persons_to_turn_off(tmp_path: Path) -> None:
    config = written(tmp_path / "lup", "[dashboard]\nreopen = false\n")

    assert not config.load().dashboard.reopen


def test_one_line_changes_one_answer_and_leaves_the_rest_lups(tmp_path: Path) -> None:
    config = written(
        tmp_path / "lup",
        'profile = "work"\neffort = "high"\ntier = "balanced"\n\n[theme]\nclaude = "light"\n',
    )

    loaded = config.load()

    assert (loaded.profile, loaded.effort, loaded.tier) == ("work", "high", "balanced")
    assert loaded.theme.claude == "light"
    assert loaded.theme.codex is None


def test_a_file_that_does_not_parse_is_refused_naming_itself(tmp_path: Path) -> None:
    config = written(tmp_path / "lup", "tier = \n")

    with pytest.raises(ValueError, match=str(config.path())):
        config.load()


@pytest.mark.parametrize(
    "content",
    ['tier = "enormous"\n', 'effort = "extreme"\n', 'colour = "blue"\n'],
    ids=["unknown-tier", "unknown-effort", "unknown-key"],
)
def test_a_setting_lup_cannot_read_is_refused_rather_than_dropped(
    tmp_path: Path, content: str
) -> None:
    config = written(tmp_path / "lup", content)

    with pytest.raises(ValueError, match="holds a setting lup cannot read"):
        config.load()


def test_a_theme_claude_code_does_not_ship_is_refused(tmp_path: Path) -> None:
    config = written(tmp_path / "lup", '[theme]\nclaude = "solarized"\n')

    with pytest.raises(ValueError):
        config.load()
    assert written(tmp_path / "custom", '[theme]\nclaude = "custom:mine"\n').load()


def test_selecting_a_profile_keeps_every_line_the_person_wrote(tmp_path: Path) -> None:
    config = written(
        tmp_path / "lup",
        '# my lup\ntier = "balanced"\n\n[theme]\n# drawn this way\nclaude = "light"\n',
    )

    config.select_profile("work")

    text = config.path().read_text(encoding="utf-8")
    assert "# my lup" in text and "# drawn this way" in text
    assert config.load().profile == "work"
    assert config.load().theme.claude == "light"

    config.select_profile(None)

    assert config.load().profile is None
    assert config.load().tier == "balanced"


def test_selecting_a_profile_starts_the_file_where_there_is_none(
    tmp_path: Path,
) -> None:
    config = UserConfigFile(tmp_path / "lup")

    config.select_profile("work")

    assert config.load() == UserConfig(profile="work")


def test_the_editor_and_each_runtimes_own_settings_are_read(tmp_path: Path) -> None:
    config = written(
        tmp_path / "lup",
        'editor = "vim"\n\n[claude.settings]\nverbose = true\n\n'
        "[codex.settings.tui]\nanimations = false\n",
    )

    loaded = config.load()

    assert loaded.editor == "vim"
    assert loaded.claude.settings == {"verbose": True}
    assert loaded.codex.settings == {"tui": {"animations": False}}


def test_an_editor_mode_no_runtime_takes_is_refused(tmp_path: Path) -> None:
    config = written(tmp_path / "lup", 'editor = "emacs"\n')

    with pytest.raises(ValueError, match="holds a setting lup cannot read"):
        config.load()


def test_recording_values_keeps_the_persons_lines_and_makes_missing_tables(
    tmp_path: Path,
) -> None:
    config = written(tmp_path / "lup", '# mine\ntier = "balanced"\n')

    config.record({("theme", "claude"): "light", ("editor",): "vim"})
    config.record({("claude", "settings", "verbose"): True})

    assert "# mine" in config.path().read_text(encoding="utf-8")
    loaded = config.load()
    assert (loaded.theme.claude, loaded.editor, loaded.tier) == (
        "light",
        "vim",
        "balanced",
    )
    assert loaded.claude.settings == {"verbose": True}

    config.record({("editor",): None, ("codex", "settings", "absent"): None})

    assert config.load().editor is None


def test_a_value_lup_could_not_read_back_is_refused_and_nothing_written(
    tmp_path: Path,
) -> None:
    config = written(tmp_path / "lup", 'tier = "balanced"\n')

    with pytest.raises(ValueError):
        config.record({("theme", "claude"): "not-a-theme", ("editor",): "vim"})

    assert config.path().read_text(encoding="utf-8") == 'tier = "balanced"\n'


def test_a_superseded_volume_is_kept_fourteen_days_unless_the_person_says(
    tmp_path: Path,
) -> None:
    assert (
        UserConfigFile(tmp_path / "unset").load().cleanup.superseded_volumes_after_days
        == 14
    )
    config = written(tmp_path / "lup", "[cleanup]\nsuperseded_volumes_after_days = 3\n")

    assert config.load().cleanup.superseded_volumes_after_days == 3
    with pytest.raises(ValueError):
        written(
            tmp_path / "bad", "[cleanup]\nsuperseded_volumes_after_days = -1\n"
        ).load()
