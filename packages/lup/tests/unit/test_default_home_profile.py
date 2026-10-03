"""No profile names the default home, wherever one is registered or selected.

Claude Code reads its configuration document from ``~/.claude.json`` while
``CLAUDE_CONFIG_DIR`` is unset, and from ``<dir>/.claude.json`` once it names
a directory — ``~/.claude`` included. A profile at the default home therefore
opened every session on a document the account never wrote, and the person's
theme, trust records and projects looked reset. These pin the refusal at
every place a profile can be registered, selected or resolved, and that a
profile already on disk fails naming itself and the way out, while removing
it stays open.

Nothing here writes to the real default home: a profile naming it is refused
before anything is written, and one already on disk is a symlink under the
test's own directory, which resolves onto the default home without touching
it.
"""

import json
import shutil
from pathlib import Path
from unittest.mock import Mock

import pytest
import typer
from pydantic import ValidationError
from typer.testing import CliRunner

import lup.devtools.harness.launch as launch
from lup.devtools.utils import Refusal
from lup.launch.declaration import LaunchSandbox
import lup.providers.claude.usage.reader as claude_usage
from lup.devtools.harness.composition import NativeTargets
from lup.observability.usage.app import create_usage_app
from lup.devtools.harness.profile_app import create_profile_app
from lup.devtools.resolve.app import create_resolve_app
from lup.devtools.setup import create_setup_app
from lup.providers.claude.config import ClaudeProfileRegistry, ClaudeProfileSelection
from lup.providers.claude.login import CLAUDE_CONFIG_DIR, CLAUDE_LOGIN
from lup.providers.codex.login import CODEX_LOGIN
from lup.providers.login import ProviderLogin
from lup.providers.profile_tree import profile_directory
from lup.providers.profiles import DefaultHomeProfile, ProfileDirectory
from lup.providers.user_config import UserConfigFile
from tests.unit.harness_launch import checkout, composition, stub_host

DEFAULT_HOME = CLAUDE_LOGIN.ambient_home

WAY_OUT = "leave the profile unset to use the default account"

WIDE_PLAIN_CONSOLE = {
    "FORCE_COLOR": None,
    "NO_COLOR": "1",
    "TERM": "dumb",
    "COLUMNS": "400",
}

runner = CliRunner(env=WIDE_PLAIN_CONSOLE)


@pytest.fixture
def config(tmp_path: Path) -> UserConfigFile:
    return UserConfigFile(tmp_path / "lup")


@pytest.fixture
def directory(config: UserConfigFile) -> ProfileDirectory:
    return profile_directory(CLAUDE_LOGIN, config)


def symlinked(
    config: UserConfigFile, name: str, login: ProviderLogin, target: Path
) -> Path:
    """A profile whose home is a symlink onto ``target``."""
    home = config.profiles_root() / name / login.home_subdir
    home.parent.mkdir(parents=True)
    home.symlink_to(target, target_is_directory=True)
    return home


@pytest.fixture
def registered(config: UserConfigFile, directory: ProfileDirectory) -> ProfileDirectory:
    """A profile written before the refusal existed: ``main`` is the default home.

    Linked rather than named, which is the one way a directory profile can
    reach it, and selected; ``work`` sits beside it to show the refusal is
    about ``main``.
    """
    symlinked(config, "main", CLAUDE_LOGIN, DEFAULT_HOME)
    (config.profiles_root() / "work" / CLAUDE_LOGIN.home_subdir).mkdir(parents=True)
    config.select_profile("main")
    return directory


def own_default(tmp_path: Path) -> ProviderLogin:
    """Claude's login, declaring a home this test made as its default."""
    home = tmp_path / "user" / ".claude"
    home.mkdir(parents=True)
    return CLAUDE_LOGIN.model_copy(update={"ambient_home": home})


# The login's own answer, which every refusal below asks.


@pytest.mark.parametrize(
    "spelled",
    [
        DEFAULT_HOME,
        Path("~/.claude"),
        DEFAULT_HOME / "nested" / "..",
        Path(f"{DEFAULT_HOME}/"),
    ],
    ids=["absolute", "tilde", "dotdot", "trailing-separator"],
)
def test_claude_cannot_be_pointed_at_its_default_home_by_name(spelled: Path) -> None:
    assert not CLAUDE_LOGIN.nameable(spelled)


def test_a_symlink_onto_the_default_home_is_the_default_home(tmp_path: Path) -> None:
    link = tmp_path / "account"
    link.symlink_to(DEFAULT_HOME, target_is_directory=True)

    assert not CLAUDE_LOGIN.nameable(link)
    assert CLAUDE_LOGIN.nameable(tmp_path / ".claude")


def test_codex_can_be_pointed_at_its_default_home_by_name() -> None:
    """A set ``CODEX_HOME`` of ``~/.codex`` is the unset one: nothing sits beside it."""
    assert CODEX_LOGIN.nameable(CODEX_LOGIN.ambient_home)


# A typed selection, which a programmatic registry is built from.


def test_a_selection_cannot_name_the_default_home() -> None:
    with pytest.raises(ValidationError, match=WAY_OUT):
        ClaudeProfileSelection(config_directory=DEFAULT_HOME)


def test_a_registry_cannot_hold_a_named_profile_at_the_default_home() -> None:
    with pytest.raises(ValidationError) as raised:
        ClaudeProfileRegistry.model_validate(
            {"profiles": {"main": {"config_directory": "~/.claude"}}}
        )

    assert raised.value.errors()[0]["loc"] == ("profiles", "main", "config_directory")


def test_a_registry_cannot_default_to_the_default_home_by_name() -> None:
    """The same trap as a named one, so the same refusal."""
    with pytest.raises(ValidationError, match=WAY_OUT):
        ClaudeProfileRegistry.model_validate(
            {"default": {"config_directory": str(DEFAULT_HOME)}}
        )


# The directory a launcher, a resolver run and the command trees hold.


@pytest.mark.parametrize("spelled", [DEFAULT_HOME, Path("~/.claude")])
def test_adding_the_default_home_is_refused_before_anything_is_written(
    directory: ProfileDirectory, config: UserConfigFile, spelled: Path
) -> None:
    with pytest.raises(DefaultHomeProfile, match=WAY_OUT) as raised:
        directory.add("main", spelled)

    assert "profile 'main' cannot name the default home" in str(raised.value)
    assert "symlink" not in str(raised.value)
    assert not config.profiles_root().exists()
    assert not config.path().exists()


def test_a_stored_default_home_fails_resolution_naming_itself_and_the_way_out(
    registered: ProfileDirectory,
) -> None:
    for name in [None, "main"]:
        with pytest.raises(DefaultHomeProfile) as raised:
            registered.launch_home(name)

        refusal = str(raised.value)
        assert f"profile 'main' names the default home {DEFAULT_HOME.resolve()}" in (
            refusal
        )
        assert CLAUDE_CONFIG_DIR in refusal
        assert f"remove it (`profile remove main`) and {WAY_OUT}" in refusal


def test_a_stored_default_home_is_refused_wherever_it_would_be_launched(
    registered: ProfileDirectory, config: UserConfigFile
) -> None:
    """Named or merely selected, for a launch, a run's account, or a selection.

    Adding it again is refused too, before anything is written: what the name
    already holds is the default home.
    """
    stored = config.path().read_bytes()

    for refused in [
        lambda: registered.launch_home(None),
        lambda: registered.launch_home("main"),
        lambda: registered.account(None),
        lambda: registered.use("main"),
        lambda: registered.add("main", scope="global"),
    ]:
        with pytest.raises(DefaultHomeProfile, match="profile remove main"):
            refused()

    assert config.path().read_bytes() == stored
    assert registered.launch_home("work") == registered.profile("work").config_dir
    assert [entry.name for entry in registered.entries()] == ["main", "work"]


def test_removing_a_stored_default_home_restores_the_default_account(
    registered: ProfileDirectory, config: UserConfigFile
) -> None:
    """The way out the refusal names has to be open, and has to arrive."""
    with pytest.raises(ValueError, match="remove that directory") as said:
        registered.remove("main", "global")
    assert "`profile` line" in str(said.value)

    shutil.rmtree(config.profiles_root() / "main")
    config.select_profile(None)

    assert registered.launch_home(None) is None
    assert registered.account(None).variables == {}
    assert [entry.name for entry in registered.entries()] == ["work"]


def test_replacing_a_linked_default_home_with_its_own_repairs_it(
    registered: ProfileDirectory, config: UserConfigFile
) -> None:
    """The other way out: the name keeps working, on a home of its own."""
    home = config.profiles_root() / "main" / CLAUDE_LOGIN.home_subdir
    home.unlink()
    home.mkdir()

    assert registered.launch_home(None) == home


def test_a_profile_is_refused_the_default_home_rather_than_advised_to_link(
    tmp_path: Path,
) -> None:
    """Its own refusal would say to symlink that path, which is the trap itself."""
    login = own_default(tmp_path)
    config = UserConfigFile(tmp_path / "lup")
    tree = profile_directory(login, config)

    with pytest.raises(DefaultHomeProfile, match=WAY_OUT) as raised:
        tree.add("main", login.ambient_home)

    assert "symlink" not in str(raised.value)
    assert not config.profiles_root().exists()


def test_a_profile_symlinked_onto_the_default_home_is_refused(
    tmp_path: Path,
) -> None:
    login = own_default(tmp_path)
    config = UserConfigFile(tmp_path / "lup")
    symlinked(config, "main", login, login.ambient_home)
    tree = profile_directory(login, config)

    for refused in [
        lambda: tree.add("main", scope="global"),
        lambda: tree.use("main"),
        lambda: tree.launch_home("main"),
        lambda: tree.account("main"),
    ]:
        with pytest.raises(DefaultHomeProfile, match="profile 'main' names"):
            refused()

    assert [entry.name for entry in tree.entries()] == ["main"]
    assert tree.active_name() is None, "a refused add selected it anyway"


def test_a_codex_profile_may_link_its_default_home(tmp_path: Path) -> None:
    """Codex keeps nothing beside its home, so naming it is naming nothing."""
    login = CODEX_LOGIN.model_copy(
        update={"ambient_home": tmp_path / "user" / ".codex"}
    )
    login.ambient_home.mkdir(parents=True)
    config = UserConfigFile(tmp_path / "lup")
    home = symlinked(config, "main", login, login.ambient_home)

    assert profile_directory(login, config).launch_home("main") == home


# The command trees and entry points a person reaches all of this through.


def test_the_profile_command_tree_refuses_adding_the_default_home(
    directory: ProfileDirectory, config: UserConfigFile
) -> None:
    result = runner.invoke(
        create_profile_app(directory),
        ["add", "main", "--config-dir", "~/.claude"],
    )

    assert result.exit_code != 0
    assert "profile 'main' cannot name the default home" in result.output
    assert WAY_OUT in result.output
    assert not config.profiles_root().exists()


def test_the_profile_command_tree_refuses_selecting_a_stored_default_home(
    registered: ProfileDirectory,
) -> None:
    result = runner.invoke(create_profile_app(registered), ["use", "main"])

    assert result.exit_code != 0
    assert "profile remove main" in result.output


def test_the_setup_wizard_refuses_adding_the_default_home(
    directory: ProfileDirectory, config: UserConfigFile
) -> None:
    result = runner.invoke(
        create_setup_app([], directory),
        ["profile", "add", "main", "--config-dir", str(DEFAULT_HOME)],
    )

    assert result.exit_code != 0
    assert WAY_OUT in result.output
    assert not config.profiles_root().exists()


def test_a_launch_refuses_a_stored_default_home_as_a_bad_parameter(
    registered: ProfileDirectory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Refused as the declaration is made, before anything around the session runs."""
    root = checkout(tmp_path)
    caught = stub_host(monkeypatch, root)

    with pytest.raises(Refusal) as refused:
        launch.launch_claude(
            composition(root, "claude"),
            launch.LaunchArguments(sandbox=LaunchSandbox.INNER),
            registered,
            False,
        )
    assert "profile remove main" in refused.value.said["why"]
    assert caught.events == []


def test_a_resolver_run_refuses_a_stored_default_home_before_it_starts(
    registered: ProfileDirectory,
) -> None:
    """Refused in this terminal, including for a run that would have detached."""
    app = create_resolve_app(Mock(), NativeTargets(builders={}), profiles=registered)

    for arguments in [["--profile", "main"], ["--detach", "--adapter", "claude"]]:
        result = runner.invoke(app, arguments)

        assert result.exit_code == 2, result.output
        assert "profile remove main" in result.output


def test_the_usage_display_reports_a_stored_default_home_as_a_failed_read(
    registered: ProfileDirectory,
) -> None:
    """Reading an account's usage resolves its profile too, and says so the same way.

    As a failed read rather than a traceback, so ``--json`` still answers a
    parser with an error object on stdout.
    """
    root = typer.Typer()
    root.add_typer(
        create_usage_app([claude_usage.claude_usage_entry(registered)]), name="usage"
    )

    shown = runner.invoke(root, ["usage", "claude", "--profile", "main"])
    emitted = runner.invoke(root, ["usage", "claude", "--json"])
    unknown = runner.invoke(root, ["usage", "claude", "--profile", "ghost"])

    assert shown.exit_code == 1, shown.output
    assert "profile remove main" in shown.output
    assert emitted.exit_code == 1, emitted.output
    assert "profile remove main" in json.loads(emitted.stdout)["error"]
    assert unknown.exit_code == 1, unknown.output
    assert "unknown profile 'ghost'; known: main, work" in unknown.output
