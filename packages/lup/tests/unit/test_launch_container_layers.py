"""`harness claude|codex` lays the container's settings the one way: flag, mode, person, project.

Each layer is an ``OuterContainer`` stating only what it sets: the project's
declared one, the ``[container]`` table of the person's lup config, the
selected mode's, and what this command line names. The declaration the
harness launches carries the result, so ``command()`` prints the container
the launch opens.
"""

from pathlib import Path

import pytest

import lup.devtools.harness.launch as launch
from lup.harness.image import MemoryLimit
from lup.launch.declaration import InnerSandbox, LaunchSandbox, OuterContainer
from lup.providers.user_config import UserConfigFile
from tests.unit.harness_launch import checkout, composition, profiles


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return checkout(tmp_path)


@pytest.fixture
def person(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> UserConfigFile:
    """This person's lup config, found the way a launch finds it."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setattr(launch, "accessible_roots", lambda *_a, **_k: [])
    monkeypatch.setattr(launch, "granted_devices", lambda *_a, **_k: [])
    config = UserConfigFile()
    config.path().parent.mkdir(parents=True, exist_ok=True)
    return config


def says(config: UserConfigFile, table: str) -> None:
    config.path().write_text(table, encoding="utf-8")


def contained(root: Path, request: launch.LaunchRequest) -> OuterContainer:
    """The container each runtime's declaration opens, asserted to be the same."""
    claude = launch.claude_declaration(composition(root, "claude"), request, profiles())
    codex = launch.codex_declaration(composition(root, "codex"), request, None)
    assert isinstance(claude.sandbox, OuterContainer)
    assert claude.sandbox == codex.sandbox
    return claude.sandbox


def outer(**named: object) -> launch.LaunchRequest:
    return launch.LaunchRequest.model_validate(
        {"sandbox": LaunchSandbox.OUTER, **named}
    )


def test_the_command_line_grants_sudo(root: Path, person: UserConfigFile) -> None:
    del person
    assert contained(root, outer(sudo=True)).sudo is True
    assert contained(root, outer()).sudo is False


def test_a_person_s_network_holds_until_a_flag_names_another(
    root: Path, person: UserConfigFile
) -> None:
    says(person, '[container]\nnetwork = "bridge"\n')

    assert contained(root, outer()).network == "bridge"
    assert contained(root, outer(network="host")).network == "host"


def test_a_person_s_config_overrules_the_project_and_a_flag_overrules_both(
    root: Path, person: UserConfigFile
) -> None:
    says(person, '[container]\nmemory = "12GiB"\nsudo = true\n')
    project = OuterContainer(memory=MemoryLimit(percent=50), network="bridge")

    settled = contained(root, outer(container=project))
    assert settled.memory == MemoryLimit.model_validate("12GiB")
    assert settled.network == "bridge"
    assert settled.sudo is True
    assert contained(root, outer(container=project, sudo=False)).sudo is False


def test_the_project_s_image_is_the_one_a_layer_names_none_over(
    root: Path, person: UserConfigFile
) -> None:
    del person
    assert (
        contained(root, outer()).image
        == composition(root, "claude").recipe.source.image
    )


def test_a_host_session_says_what_its_flags_asked_of_a_container(
    root: Path, person: UserConfigFile, capsys: pytest.CaptureFixture[str]
) -> None:
    del person
    request = launch.LaunchRequest(
        sandbox=LaunchSandbox.INNER, sudo=True, network="host"
    )

    agent = launch.claude_declaration(composition(root, "claude"), request, profiles())

    assert isinstance(agent.sandbox, InnerSandbox)
    captured = capsys.readouterr()
    said = captured.err + captured.out
    assert "--sudo" in said and "--network" in said and "host" in said
