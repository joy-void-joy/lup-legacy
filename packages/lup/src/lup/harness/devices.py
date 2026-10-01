"""The devices a contained session is granted, resolved the way its mounts are.

A container starts with none of the host's devices, and a GPU is the one a
session would otherwise leave the boundary for: without a grant, an
accelerated run launches on the host behind an escalation, outside
containment and outside the provenance a contained launch records. What keeps
it inside is a grant the
launcher resolves rather than a flag somebody remembers -- a device granted on
the machine, in its gitignored registry, reaches every session and worker
opened there, and a device named on one command line is granted for that
launch. Never a committed declaration: which GPU a machine holds is that
machine's fact, and a repository's code is shared by every machine and every
downstream user that runs it.

A device is named the way the Container Device Interface names it,
``vendor/class=device``, because that is the one spelling both engines take on
``--device`` and the one a vendor's toolkit registers. The nodes under ``/dev``
and the driver libraries beside them are the registered spec's to inject, so
nothing here enumerates either, and a second vendor's accelerator is a second
name rather than a second code path.

The registry is read on the host before any argv names a device, for the
reason every host fact in this package is resolved there: an engine handed a
name no spec answers refuses the whole container, which takes the launch and
every probe behind the same argv down with it. A device nobody registered is
withheld and said, the way a declared root that is not there is skipped rather
than mounted -- and said with the command that registers it, because the
absence is one the operator fixes once on the machine rather than in the tree.
"""

from collections.abc import Sequence
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, ValidationError

from lup.harness.notice import Notice


class Device(BaseModel, frozen=True):
    """One device a session asks for, under the name the engine resolves."""

    name: str = Field(
        pattern=(
            r"^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9_-]*"
            r"=[A-Za-z0-9][A-Za-z0-9._:-]*$"
        ),
        description=(
            "A fully-qualified CDI device name, ``vendor/class=device``: "
            "``nvidia.com/gpu=all`` for every GPU the host has, "
            "``nvidia.com/gpu=0`` for one by index. Validated against the "
            "specification's own grammar rather than parsed, because the "
            "engine looks the whole name up in a registered spec and a name "
            "shaped any other way refuses the container rather than a device"
        ),
    )

    def arguments(self) -> list[str]:
        """This device as both engines take it on the command line."""
        return ["--device", self.name]


class RegisteredDevice(BaseModel, frozen=True, extra="ignore"):
    """One ``devices`` entry of a spec, as much of it as resolving a name reads."""

    name: str


class UnreadableSpecification(BaseModel, frozen=True):
    """A spec file the registry holds and the launcher could not read.

    Reported beside the names rather than skipped, because a file an engine
    cannot read is a file it refuses a device over -- and the refusal names
    the device, not the file.
    """

    path: Path
    reason: str

    def names(self) -> list[str]:
        """Nothing: a spec that did not parse registers no device."""
        return []

    def failures(self) -> list["UnreadableSpecification"]:
        """This one."""
        return [self]


class Specification(BaseModel, frozen=True, extra="ignore"):
    """A CDI specification file, as much of it as resolving a name reads.

    A spec carries the container edits each device makes -- nodes, mounts,
    hooks, environment -- and none of that is read here, because none of it
    is the launcher's to apply: the engine injects what the spec says once
    handed the name. What the launcher has to know is which names the host
    answers for.
    """

    kind: str = Field(description="``vendor/class``, the half every device shares")
    devices: list[RegisteredDevice] = []

    def names(self) -> list[str]:
        """Every fully-qualified name this spec registers."""
        return [f"{self.kind}={device.name}" for device in self.devices]

    def failures(self) -> list[UnreadableSpecification]:
        """None: this spec was read."""
        return []


class DeviceRegistry(BaseModel, frozen=True):
    """What the host's CDI registry answers for, and what it holds that cannot."""

    names: list[str] = []
    unreadable: list[UnreadableSpecification] = []
    searched: list[Path] = Field(
        default=[], description="The directories read, for a notice to name"
    )


def read_specification(path: Path) -> Specification | UnreadableSpecification:
    """One spec file, in either spelling the registry accepts.

    YAML is what a toolkit writes by default and JSON is the other spelling
    the engines read; YAML being a superset of JSON, one loader reads both.
    """
    try:
        return Specification.model_validate(yaml.safe_load(path.read_text()))
    except (OSError, yaml.YAMLError, ValidationError) as failure:
        return UnreadableSpecification(path=path, reason=str(failure))


def registered_devices(
    directories: Sequence[Path] = (Path("/etc/cdi"), Path("/var/run/cdi")),
) -> DeviceRegistry:
    """Every device the host registers, in the order the engines read the specs.

    The two directories are the specification's own defaults and the ones
    both engines read without being configured: the static one an operator
    writes into, and the generated one a toolkit refreshes at boot. A
    directory that is not there is an ordinary answer -- a host with no
    toolkit installed -- and is still named as searched, so a withheld device
    can say where a spec would have been looked for.
    """
    readings = [
        read_specification(path)
        for directory in directories
        if directory.is_dir()
        for path in sorted(directory.iterdir())
        if path.suffix in (".yaml", ".yml", ".json")
    ]
    return DeviceRegistry(
        names=[name for reading in readings for name in reading.names()],
        unreadable=[failure for reading in readings for failure in reading.failures()],
        searched=list(directories),
    )


class Withheld(BaseModel, frozen=True):
    """A declared device this host could not grant, and the reason it could not."""

    device: Device
    reason: str


class DeviceLease(BaseModel, frozen=True):
    """Which declared devices one launch grants, and which it had to withhold.

    Recorded beside the mount table rather than folded into it: a lease of
    mounts is a table of paths and modes the dispatcher attributes refusals
    against, and a device is neither -- it is granted whole or not at all,
    and a refusal it explains is the engine's at start rather than a command's
    later.
    """

    granted: list[Device] = []
    withheld: list[Withheld] = []
    unreadable: list[UnreadableSpecification] = []

    def notices(self) -> list[Notice]:
        """What a launch says about the devices the session is about to get.

        Nothing when nothing was granted: a line saying no device was asked
        for is the block that grows with the roster, which this package
        argues against everywhere else. A grant is one line at boundary
        weight, because it widens what the session reaches. A device
        withheld is one warning line naming it and where a spec was looked
        for; what registers one is reference, pulled from `harness
        requirements` and the contributing page rather than pushed at every
        launch, because a line read at the first launch is skipped by the
        third.
        """
        return [
            *(
                [
                    Notice(
                        text=(
                            "Devices: "
                            + ", ".join(device.name for device in self.granted)
                            + " granted through the host's CDI registry."
                        ),
                        urgency="boundary",
                    )
                ]
                if self.granted
                else []
            ),
            *[
                Notice(
                    text=(
                        f"Device {item.device.name} withheld: {item.reason}; "
                        "`harness requirements` says how to register one."
                    ),
                    urgency="warning",
                )
                for item in self.withheld
            ],
            *[
                Notice(
                    text=f"CDI spec {item.path} could not be read: {item.reason}",
                    urgency="warning",
                )
                for item in self.unreadable
            ],
        ]


def lease_devices(declared: Sequence[Device], registry: DeviceRegistry) -> DeviceLease:
    """Grant each declared device the registry names, once, in declaration order.

    Deduplicated rather than refused, because an image and a command line
    naming the same GPU are two people asking for one thing, and the engine
    would otherwise be handed the flag twice.
    """
    unique = list(dict.fromkeys(declared))
    searched = " or ".join(str(directory) for directory in registry.searched)
    return DeviceLease(
        granted=[device for device in unique if device.name in registry.names],
        withheld=[
            Withheld(device=device, reason=f"no CDI spec under {searched} registers it")
            for device in unique
            if device.name not in registry.names
        ],
        unreadable=registry.unreadable,
    )
