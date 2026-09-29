"""A contained home takes the account's settings and leaves its own trust where it is."""

import asyncio
from pathlib import Path
import shutil
import stat
from unittest.mock import AsyncMock, Mock

import pytest
import sh
import tomlkit
from typer.testing import CliRunner

import lup.devtools.harness.launch as launch
from lup.providers.codex.session import prepare_codex_plugin
from lup.launch.declaration import LaunchSandbox
import lup.providers.codex.install as installation
from lup.providers.codex.account import read_account
from lup.providers.codex.home import CodexHomeSelection
from lup.providers.codex.profile import CodexAccountSettings

capture = CodexAccountSettings.capture
"""The real capture, restored over the host stub for the tests that read it."""


def source_home(root: Path, extra: str = "") -> Path:
    home = root / "source"
    home.mkdir()
    (home / "config.toml").write_text(
        'model="gpt-5.5"\nmodel_reasoning_effort="medium"\n'
        'model_instructions_file="instructions.md"\n'
        f"{extra}"
        '[model_providers.fixture]\nname="Fixture"\nbase_url="http://localhost:9999/v1"\n'
        '[plugins."private@account"]\nenabled=true\n'
        '[projects."/private/project"]\ntrust_level="trusted"\n'
        '[hooks.state.private]\ntrusted_hash="private-hash"\n',
        encoding="utf-8",
    )
    (home / "instructions.md").write_text("A fixture instruction.", encoding="utf-8")
    return home


def test_the_accounts_settings_travel_without_its_home_state(tmp_path: Path) -> None:
    settings = CodexAccountSettings.capture(source_home(tmp_path))
    assert settings.settings["model"] == "gpt-5.5"
    assert settings.settings["model_providers"] == {
        "fixture": {"name": "Fixture", "base_url": "http://localhost:9999/v1"}
    }
    assert "plugins" not in settings.settings
    assert "projects" not in settings.settings
    assert "private-hash" not in settings.model_dump_json()


@pytest.mark.skipif(shutil.which("codex") is None, reason="Codex CLI is not installed")
def test_native_settings_keep_source_paths_and_the_destinations_own_state(
    tmp_path: Path,
) -> None:
    source = source_home(tmp_path)
    settings = CodexAccountSettings.capture(source)
    destination = tmp_path / "destination"
    destination.mkdir()
    (destination / "config.toml").write_text(
        '[plugins."lup@fixture"]\nenabled=true\n[hooks.state.owned]\ntrusted_hash="owned"\n',
        encoding="utf-8",
    )

    settings.install(destination)

    installed = tomlkit.parse((destination / "config.toml").read_text("utf-8"))
    assert installed["model"] == "gpt-5.5"
    assert installed["model_instructions_file"] == str(source / "instructions.md")
    assert installed["plugins"] == {"lup@fixture": {"enabled": True}}
    assert installed["hooks"] == {"state": {"owned": {"trusted_hash": "owned"}}}
    assert not (destination / "auth.json").exists()
    assert stat.S_IMODE((destination / "config.toml").stat().st_mode) == 0o600


@pytest.mark.skipif(shutil.which("codex") is None, reason="Codex CLI is not installed")
def test_the_accounts_settings_cannot_disable_owned_policy(tmp_path: Path) -> None:
    source = source_home(tmp_path, "[features]\nhooks=false\nmulti_agent=false\n")
    settings = CodexAccountSettings.capture(source)
    destination = tmp_path / "destination"
    destination.mkdir()
    (destination / "config.toml").write_text(
        '[plugins."lup@fixture"]\nenabled=true\n[hooks.state.owned]\ntrusted_hash="owned"\n',
        encoding="utf-8",
    )

    settings.install(destination, enforce_policy=True)

    installed = tomlkit.parse((destination / "config.toml").read_text("utf-8"))
    assert installed["features"] == {"hooks": True, "multi_agent": False}
    assert installed["plugins"] == {"lup@fixture": {"enabled": True}}
    assert installed["hooks"] == {"state": {"owned": {"trusted_hash": "owned"}}}


@pytest.mark.skipif(shutil.which("codex") is None, reason="Codex CLI is not installed")
def test_an_unavailable_dependency_refuses_before_install(tmp_path: Path) -> None:
    source = source_home(tmp_path)
    (source / "instructions.md").unlink()
    settings = CodexAccountSettings.capture(source)
    destination = tmp_path / "destination"
    with pytest.raises(ValueError, match="absolute, accessible paths"):
        settings.install(destination)
    assert not (destination / "config.toml").exists()


@pytest.mark.skipif(shutil.which("codex") is None, reason="Codex CLI is not installed")
def test_native_paths_keep_the_source_users_home(tmp_path: Path) -> None:
    original_home = tmp_path / "operator"
    original_home.mkdir()
    (original_home / "instructions.md").write_text("A fixture instruction.")
    snapshot = CodexAccountSettings(
        source_home=tmp_path / "config",
        source_user_home=original_home,
        settings={"model_instructions_file": "~/instructions.md"},
    )
    destination = tmp_path / "contained"
    snapshot.install(destination)
    installed = tomlkit.parse((destination / "config.toml").read_text())
    assert installed["model_instructions_file"] == str(
        original_home / "instructions.md"
    )


def test_stdin_payload_is_sanitized_even_without_capture(tmp_path: Path) -> None:
    settings = CodexAccountSettings(
        source_home=tmp_path,
        settings={
            "plugins": {"lup@fixture": {"enabled": False}},
            "hooks": {"state": {"owned": {"enabled": False}}},
            "features": {"hooks": False, "multi_agent": False},
        },
    )
    assert settings.personal_settings(enforce_policy=True) == {
        "hooks": {},
        "features": {"hooks": True, "multi_agent": False},
    }


@pytest.mark.skipif(shutil.which("codex") is None, reason="Codex CLI is not installed")
def test_a_contained_home_is_the_native_account_configuration(tmp_path: Path) -> None:
    source = source_home(tmp_path, 'model_provider="fixture"\n')
    snapshot = CodexAccountSettings.capture(source)
    destination = tmp_path / "destination"
    destination.mkdir()
    (destination / "config.toml").write_text(
        '[plugins."lup@fixture"]\nenabled=true\n'
        '[hooks.state.owned]\ntrusted_hash="owned"\n',
        encoding="utf-8",
    )

    snapshot.install(destination, enforce_policy=True)
    settings = tomlkit.parse((destination / "config.toml").read_text(encoding="utf-8"))
    assert settings["model"] == "gpt-5.5"
    assert settings["model_provider"] == "fixture"
    assert settings["plugins"] == {"lup@fixture": {"enabled": True}}
    assert settings["hooks"] == {"state": {"owned": {"trusted_hash": "owned"}}}
    inode = (destination / "config.toml").stat().st_ino
    snapshot.install(destination, enforce_policy=True)
    assert (destination / "config.toml").stat().st_ino == inode
    state = asyncio.run(read_account(Path("codex"), {"CODEX_HOME": str(destination)}))
    assert state.ready
    assert state.account is None


@pytest.mark.parametrize("sandbox", list(LaunchSandbox))
def test_a_launch_carries_the_accounts_settings_into_a_container_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sandbox: LaunchSandbox
) -> None:
    """A contained session's home starts from the account's whole configuration.

    The home prepared inside the container is handed the account's settings;
    a session on the host runs in a home that already holds them, so none
    are carried.
    """
    import lup.providers.codex.launch as codex_launch
    from tests.unit.harness_launch import checkout, composition, stub_host

    source = source_home(tmp_path)
    root = checkout(tmp_path)
    stub_host(monkeypatch, root)
    monkeypatch.setattr(
        codex_launch,
        "select_codex_home",
        Mock(return_value=CodexHomeSelection(path=source, isolated=False)),
    )
    monkeypatch.setattr(CodexAccountSettings, "capture", classmethod(capture.__func__))
    prepare = Mock()
    monkeypatch.setattr(codex_launch, "prepare_codex_plugin", prepare)
    monkeypatch.setattr(codex_launch, "settled_codex_seed", Mock(return_value={}))

    launch.launch_codex(
        composition(root, "codex"),
        launch.LaunchRequest(sandbox=sandbox),
        source,
        False,
        False,
    )

    snapshot = prepare.call_args.kwargs["settings"]
    if sandbox.contained():
        assert snapshot.settings["model"] == "gpt-5.5"
    else:
        assert snapshot is None


def test_malformed_settings_do_not_expose_their_values(tmp_path: Path) -> None:
    source = source_home(tmp_path)
    (source / "config.toml").write_text('secret="DO-NOT-LOG', encoding="utf-8")
    with pytest.raises(ValueError) as error:
        CodexAccountSettings.capture(source)
    assert "DO-NOT-LOG" not in str(error.value)


def test_invalid_owned_settings_do_not_expose_validation_values(tmp_path: Path) -> None:
    source = source_home(tmp_path)
    (source / "config.toml").write_text('hooks="DO-NOT-LOG"\n')
    with pytest.raises(ValueError) as error:
        CodexAccountSettings.capture(source)
    assert "DO-NOT-LOG" not in str(error.value)


def test_native_configuration_failure_does_not_expose_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = CodexAccountSettings.capture(source_home(tmp_path))
    monkeypatch.setattr(
        CodexAccountSettings,
        "normalized",
        AsyncMock(side_effect=RuntimeError("DO-NOT-LOG secret from native stderr")),
    )
    with pytest.raises(ValueError) as error:
        settings.install(tmp_path / "destination")
    assert "DO-NOT-LOG" not in str(error.value)
    assert "not logged" in str(error.value)


def test_malformed_stdin_payload_does_not_expose_values(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        installation.app,
        ["--root", str(tmp_path), "--home", str(tmp_path / "home"), "--settings-stdin"],
        input='{"source_home":"x","settings":"DO-NOT-LOG"}',
    )
    assert result.exit_code != 0
    assert "DO-NOT-LOG" not in result.output


def test_the_settings_payload_crosses_stdin_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = CodexAccountSettings.capture(source_home(tmp_path))
    command = Mock(return_value="{}")
    monkeypatch.setattr(sh, "Command", Mock(return_value=command))
    prepare_codex_plugin(
        ["podman", "run", "-i", "image"],
        tmp_path / "destination",
        tmp_path,
        {},
        settings=settings,
    )
    assert "--settings-stdin" in command.call_args.args
    assert "localhost:9999" not in " ".join(command.call_args.args)
    assert (
        CodexAccountSettings.model_validate_json(command.call_args.kwargs["_in"])
        == settings
    )
