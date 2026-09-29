"""What a container grants its session, stated by the project, the person, a mode and a flag.

Network, memory, sudo, devices, mounts and the generated trees held are all
fields of :class:`~lup.launch.declaration.OuterContainer`, so each layer that
states them states the same model: the command line over a mode, over the
person's config, over the project. A layer says only what it sets, so a
setting nobody above named is the one below's, and a folder or a device named
anywhere is granted.
"""

from pathlib import Path

import pytest
from pydantic import ValidationError

from lup.harness.devices import Device
from lup.harness.image import Image, MemoryLimit, Podman
from lup.harness.models import Harness
from lup.launch.container import held_memory
from lup.launch.declaration import (
    InnerSandbox,
    Mount,
    NoSandbox,
    OuterContainer,
    declared_image,
)
from lup.launch.refusal import LaunchRefused
from lup.providers.user_config import UserConfigFile


def test_a_memory_limit_is_an_amount_of_bytes_or_a_share() -> None:
    assert MemoryLimit.model_validate("8GiB").amount == 8 * 1024**3
    assert MemoryLimit.model_validate("512MiB").amount == 512 * 1024**2
    assert MemoryLimit.model_validate("75%").percent == 75
    assert MemoryLimit.model_validate("4096").amount == 4096


@pytest.mark.parametrize("spelled", ["0", "0%", "101%", "lots", ""])
def test_a_limit_no_engine_would_read_is_refused(spelled: str) -> None:
    with pytest.raises(ValidationError):
        MemoryLimit.model_validate(spelled)


def test_a_share_is_resolved_against_what_the_engine_can_hand_out() -> None:
    limit = MemoryLimit.model_validate("50%")

    assert limit.resolved(16 * 1024**3) == 8 * 1024**3
    assert MemoryLimit.model_validate("2GiB").resolved(16 * 1024**3) == 2 * 1024**3
    assert "50%" in limit.described(16 * 1024**3)


def test_what_a_higher_layer_sets_wins_and_what_it_leaves_is_the_lower_ones() -> None:
    project = OuterContainer(
        network="filtered", sudo=False, memory=MemoryLimit(percent=50)
    )
    person = OuterContainer(memory=MemoryLimit.model_validate("12GiB"))
    mode = OuterContainer(sudo=True)
    flags = OuterContainer(network="host")

    settled = flags.over(mode.over(person.over(project)))

    assert settled.network == "host"
    assert settled.sudo is True
    assert settled.memory == MemoryLimit.model_validate("12GiB")


def test_a_setting_said_as_its_default_still_overrules_the_one_below() -> None:
    """``--no-sudo`` on the command line takes back a mode's sudo."""
    settled = OuterContainer(sudo=False).over(OuterContainer(sudo=True))

    assert settled.sudo is False


def test_folders_and_devices_named_in_any_layer_are_all_granted() -> None:
    gpu = Device(name="nvidia.com/gpu=all")
    camera = Device(name="vendor.example/camera=0")
    project = OuterContainer(mounts=[Mount(path=Path("/data"))], devices=[gpu])
    flags = OuterContainer(
        mounts=[Mount(path=Path("/scratch"), writable=True)], devices=[camera, gpu]
    )

    settled = flags.over(project)

    assert settled.mounts == [
        Mount(path=Path("/scratch"), writable=True),
        Mount(path=Path("/data")),
    ]
    assert settled.devices == [camera, gpu]


def test_a_layered_container_still_knows_what_was_set_for_the_next_layer() -> None:
    settled = OuterContainer(sudo=True).over(OuterContainer())

    assert "sudo" in settled.model_fields_set
    assert "network" not in settled.model_fields_set


def test_the_network_a_container_names_is_the_one_its_image_runs_on() -> None:
    harness = Harness.model_construct(image=Image())

    assert declared_image(harness, OuterContainer(network="host")).egress.mode == "host"
    assert declared_image(harness, OuterContainer()).egress.mode == "filtered"


def test_a_host_wall_leaves_the_image_network_alone() -> None:
    harness = Harness.model_construct(image=Image())

    assert declared_image(harness, InnerSandbox()).egress.mode == "filtered"
    assert declared_image(harness, NoSandbox()).egress.mode == "filtered"


def test_the_run_carries_the_memory_limit_it_was_given() -> None:
    limited = Image().run_arguments(Path("/checkout"), 1000, 1000, memory=2 * 1024**3)
    unlimited = Image().run_arguments(Path("/checkout"), 1000, 1000)

    assert limited[limited.index("--memory") + 1] == str(2 * 1024**3)
    assert "--memory" not in unlimited


def test_a_person_s_config_states_container_settings(tmp_path: Path) -> None:
    config = UserConfigFile(tmp_path)
    config.path().parent.mkdir(parents=True, exist_ok=True)
    config.path().write_text(
        '[container]\nnetwork = "bridge"\nmemory = "75%"\nsudo = true\n',
        encoding="utf-8",
    )

    person = config.load().container

    assert person.network == "bridge"
    assert person.memory == MemoryLimit(percent=75)
    assert person.sudo is True
    assert person.model_fields_set == {"network", "memory", "sudo"}


@pytest.mark.parametrize(
    "table",
    [
        '[container]\nnested_repositories = [{path = "works"}]\n',
        '[container.image]\nbase = "debian"\n',
    ],
)
def test_a_person_s_config_names_no_repository_s_image_or_nested_repositories(
    tmp_path: Path, table: str
) -> None:
    """Both are facts about one repository, and a person's config reaches every one."""
    config = UserConfigFile(tmp_path)
    config.path().parent.mkdir(parents=True, exist_ok=True)
    config.path().write_text(table, encoding="utf-8")

    with pytest.raises(ValueError, match="repository"):
        config.load()


class Answering(Podman, frozen=True):
    """An engine answering one amount of memory, or nothing, to ``info``."""

    answered: int | None = None

    def memory_total(self) -> int | None:
        return self.answered


def test_a_share_is_a_share_of_what_the_engine_answers() -> None:
    held = held_memory(
        MemoryLimit(percent=25), Answering(answered=8 * 1024**3), lambda: 64 * 1024**3
    )

    assert held.limit == 2 * 1024**3
    assert any("25%" in notice.text for notice in held.notices)


def test_the_host_answers_for_an_engine_that_said_nothing() -> None:
    held = held_memory(MemoryLimit(percent=50), Answering(), lambda: 4 * 1024**3)

    assert held.limit == 2 * 1024**3


def test_a_share_nothing_can_resolve_refuses_rather_than_dropping_the_bound() -> None:
    with pytest.raises(LaunchRefused, match="amount"):
        held_memory(MemoryLimit(percent=50), Answering(), lambda: None)


def test_an_amount_needs_nothing_answered() -> None:
    held = held_memory(MemoryLimit.model_validate("1GiB"), Answering(), lambda: None)

    assert held.limit == 1024**3


def test_no_limit_is_no_flag() -> None:
    assert held_memory(None, Answering(), lambda: None).limit is None
