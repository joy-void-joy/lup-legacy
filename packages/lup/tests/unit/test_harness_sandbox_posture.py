"""What a launch claims about the boundary it is opening a session behind.

Four failures live here, and every one of them is silent. A probe spelling
one flag for two programs reports a working socat broken on every host. A
launch about to hand the session a container prints a verdict about a sandbox
it is not going to use. A client that answers for one engine while driving
another starts containers that cannot fork and cannot write to the checkout
they are opened on. And an image built for two runtimes carrying one leaves
the second to build a container, start a proxy, and die on `not found`.

None of the four raises anything. That is what these hold still: each asserts
the shape of an argument list or an environment, because that is where the
claim lives before anything has run.
"""

from pathlib import Path

import pytest

import lup.providers.codex.launch as codex_launch
from lup.launch.boundary import apply_sandbox_environment
from lup.launch.declaration import LaunchSandbox
from lup.launch.declaration import NoSandbox, OuterContainer
from lup.providers.claude import Claude
from lup.providers.claude.launch import claude_settings
from lup.providers.codex.launch import codex_envelope
from lup.harness.image import ContainerClient, Image, detected_client
from lup.harness.models import HookSandbox, HookSet
from lup.harness.requirements import LostCapability, Requirement, Run
from lup.harness.toolchain import (
    agent_session_requirement,
    bubblewrap_requirement,
    codex_envelope_requirement,
    socat_requirement,
)
from lup.providers.claude.confinement import CLAUDE_CONFINEMENT, CLAUDE_SANDBOX_OFF
from lup.providers.codex.confinement import CODEX_CONFINEMENT
from lup.types import EnvVars


def confining_hooks() -> HookSet:
    """A policy declaring an OS sandbox, which is what all of this keys off."""
    return HookSet(id="hooks.probe", policy_ids=[], sandbox=HookSandbox())


def test_each_sandbox_dependency_carries_its_own_version_flag() -> None:
    """socat has no `--version`, and a prober that assumed one broke everywhere.

    Asked the long way socat prints `E unknown option "--version"` and exits
    1, so a launcher that spelled one flag for every tool it checked read a
    working socat as a broken one -- on every host, not on an unlucky one.
    The flag belongs to the program, so it is declared beside the program.
    """
    assert socat_requirement().exercise == Run(command=["socat", "-V"])
    assert bubblewrap_requirement().exercise == Run(command=["bwrap", "--version"])


def test_a_contained_launch_probes_nothing_and_claims_nothing() -> None:
    """The container is the boundary, so the flag would change no verdict.

    `LUP_SANDBOX_ACTIVE` says the launcher verified an OS sandbox. A contained
    session has one already and the kernel reads it from the image's own
    `LUP_CONTAINED`, so probing here could only produce a sentence about a
    boundary nothing was going to consult -- and on a failed probe, the
    sentence was `deny lattice stays active` for a session whose lattice was
    about to stand down behind the container.
    """
    environment: EnvVars = {}
    refuses = Requirement(
        capability="never",
        purpose="a probe that cannot pass",
        exercise=Run(command=["definitely-not-a-program-on-this-host"]),
        absence=LostCapability(capability="OS confinement"),
    )

    apply_sandbox_environment(
        confining_hooks(),
        environment,
        "claude",
        [refuses],
        sandbox=LaunchSandbox.OUTER,
    )

    assert "LUP_SANDBOX_ACTIVE" not in environment


def test_an_uncontained_launch_vouches_for_a_boundary_that_answers() -> None:
    """The case this file exists for, stated as the passing case.

    A wrong probe raises nothing: the flag simply never gets set, and every
    session on every host carries the deny lattice for want of a hyphen. So
    the assertion that matters is the positive one: a tool that answers has
    to reach the environment as a boundary the launch vouched for.
    """
    environment: EnvVars = {}
    answers = Requirement(
        capability="always",
        purpose="a probe that passes anywhere",
        exercise=Run(command=["echo", "ok"]),
        absence=LostCapability(capability="OS confinement"),
    )

    apply_sandbox_environment(
        confining_hooks(),
        environment,
        "claude",
        [answers],
        sandbox=LaunchSandbox.INNER,
    )

    assert environment["LUP_SANDBOX_ACTIVE"] == "1"


def test_an_uncontained_launch_still_refuses_to_vouch_for_a_broken_tool() -> None:
    """The probe is not gone, only asked where its answer decides something."""
    environment: EnvVars = {}
    refuses = Requirement(
        capability="never",
        purpose="a probe that cannot pass",
        exercise=Run(command=["definitely-not-a-program-on-this-host"]),
        absence=LostCapability(capability="OS confinement"),
    )

    apply_sandbox_environment(
        confining_hooks(),
        environment,
        "claude",
        [refuses],
        sandbox=LaunchSandbox.INNER,
    )

    assert "LUP_SANDBOX_ACTIVE" not in environment


def test_a_contained_claude_session_turns_its_own_sandbox_off() -> None:
    """Measured: bubblewrap cannot mount a fresh /proc in that container.

    The settings artifact keeps saying `enabled: true`, because that is the
    right answer for the uncontained launch the same file serves. Which one
    this launch is, is the launch's to say.
    """
    document = claude_settings(
        Claude(policy=confining_hooks(), sandbox=OuterContainer())
    )

    assert document == {"sandbox": {"enabled": False}}


def test_a_contained_codex_session_keeps_the_route_to_its_proxy() -> None:
    """`workspace-write` turns network access off, which is fatal here.

    A contained session reaches the world through one HTTP proxy on an
    internal network. An envelope that cut network access would take that
    route away, so the container's own isolation is the boundary and Codex is
    told to stand down -- which is what its documentation prescribes once a
    container provides the isolation.
    """
    environment: EnvVars = {}

    arguments = codex_envelope(
        confining_hooks(), environment, [], sandbox=LaunchSandbox.OUTER
    )

    assert arguments == ["--sandbox", "danger-full-access"]
    assert "LUP_SANDBOX_ACTIVE" not in environment


def test_a_launch_choosing_no_sandbox_stands_both_walls_down_and_vouches_nothing() -> (
    None
):
    """`--sandbox none` is the degraded posture stated rather than assumed.

    Both runtimes spell their own sandbox off, exactly as a contained launch
    does, and neither exports LUP_SANDBOX_ACTIVE -- so the deny lattice stays
    standing and every unjudged command keeps its escalation recipe. A
    posture otherwise reachable only as a broken inner sandbox is a choice
    with a name, and the name buys the strict judgment on purpose.
    """
    environment: EnvVars = {}

    claude = claude_settings(Claude(policy=confining_hooks(), sandbox=NoSandbox()))
    codex = codex_envelope(
        confining_hooks(), environment, [], sandbox=LaunchSandbox.NONE
    )

    assert claude == CLAUDE_SANDBOX_OFF
    assert codex == CODEX_CONFINEMENT.off
    assert "LUP_SANDBOX_ACTIVE" not in environment


def test_neither_runtime_vouches_for_a_boundary_it_did_not_exercise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both runtimes probe before vouching; neither asserts the flag outright.

    A runtime setting it unprobed, on a machine whose envelope does not hold,
    tells every dispatcher downstream to relax into a boundary that is not
    there. Both vouch through one function, and what differs is which tools
    it takes.
    """
    environment: EnvVars = {}
    monkeypatch.setattr(
        codex_launch,
        "codex_envelope_requirement",
        lambda: Requirement(
            capability="codex sandbox envelope",
            purpose="a probe that cannot pass",
            exercise=Run(command=["definitely-not-a-program-on-this-host"]),
            absence=LostCapability(capability="vouching for the Codex envelope"),
        ),
    )

    arguments = codex_envelope(
        confining_hooks(), environment, [], sandbox=LaunchSandbox.INNER
    )

    assert "LUP_SANDBOX_ACTIVE" not in environment
    # The confinement stays on: a probe that could not verify the envelope
    # withdraws the claim, never the wall.
    assert arguments[:2] == ["--sandbox", "workspace-write"]


def test_an_envelope_that_answers_is_vouched_for(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The positive case, which is the one a broken probe silently loses."""
    environment: EnvVars = {}
    monkeypatch.setattr(
        codex_launch,
        "codex_envelope_requirement",
        lambda: Requirement(
            capability="codex sandbox envelope",
            purpose="a probe that passes anywhere",
            exercise=Run(command=["echo", "envelope-holds"], expect="envelope-holds"),
            absence=LostCapability(capability="vouching for the Codex envelope"),
        ),
    )

    codex_envelope(confining_hooks(), environment, [], sandbox=LaunchSandbox.INNER)

    assert environment["LUP_SANDBOX_ACTIVE"] == "1"


def test_the_envelope_probe_tells_a_boundary_from_a_command_that_never_ran() -> None:
    """One witness cannot: a failed command writes nothing outside either.

    Measured with the outer witness alone, the probe reports a working
    envelope for a runtime that does not exist on this machine — the exact
    unmeasured claim this layer refuses. The inner witness is written where
    the envelope permits, so its absence says the command never ran rather
    than that the wall held.
    """
    absent = codex_envelope_requirement(runtime="definitely-not-a-runtime")

    finding = absent.check({})

    assert not finding.working


def test_a_client_that_cannot_drive_its_server_is_passed_over() -> None:
    """A Docker CLI on a podman socket cannot send the flag podman needs.

    Both answer, so taking the first would take the one that cannot start a
    usable session: podman remaps uids without `--userns=keep-id`, and a
    Docker client rejects that flag before the daemon sees it.
    """
    versions = {"docker": "Docker version 29.7.2", "podman": "podman version 6.1.0"}
    servers = {"docker": '[{"Name":"Podman Engine"}]', "podman": ""}

    found = detected_client(ask=versions.__getitem__, ask_server=servers.__getitem__)

    assert found is not None
    assert found.binary == "podman"
    assert found.engine().identity_arguments(1000, 1000)[-1] == "--userns=keep-id"


def test_the_only_client_is_returned_even_when_it_cannot_drive_its_server() -> None:
    """So the refusal names DOCKER_HOST rather than a host with no runtime.

    Reporting absence here would send an operator to install the thing they
    already have, which is the least useful true-sounding sentence available.
    """
    found = detected_client(
        candidates=("docker",),
        ask=lambda _: "Docker version 29.7.2",
        ask_server=lambda _: '[{"Name":"Podman Engine"}]',
    )

    assert found is not None
    assert not found.drives_its_server()
    assert "DOCKER_HOST" in found.consequence()


def test_a_session_container_is_given_a_process_bound_it_can_name() -> None:
    """Absent the flag, that Docker-to-podman path is given `pids.max=1`.

    A container that may hold one process cannot fork, so the entrypoint dies
    on its first `mkdir` with `fork: Resource temporarily unavailable` and
    nothing in the message names a limit.
    """
    arguments = Image().run_arguments(Path("/checkout"), 1000, 1000)

    assert "--pids-limit" in arguments
    assert arguments[arguments.index("--pids-limit") + 1] == "4096"


def test_a_session_container_is_given_something_at_pid_one_that_reaps() -> None:
    """The bound above is only survivable while something drains what fills it.

    Absent the flag, PID 1 is the agent runtime, which does not reap. Every
    orphaned child is reparented to it and stays a zombie for the life of the
    container, so the bound is reached by a session that leaked rather than by
    one doing too much at once -- and what that announces itself as is
    `can't start new thread` across an unrelated suite.
    """
    arguments = Image().run_arguments(Path("/checkout"), 1000, 1000)

    assert "--init" in arguments


def test_an_image_no_longer_installs_a_sandbox_it_cannot_start() -> None:
    """Those two packages bought silence about a boundary that was not there."""
    assert Image().inner_sandbox == []


def test_the_image_carries_every_runtime_a_contained_launch_can_open() -> None:
    """A contained launch runs `<cli>` inside the container, so it has to be there.

    While the install was one hardcoded line naming Claude, a contained
    `harness codex` built the image, started the egress proxy, wrote
    the boundary record, and then failed on `codex: not found` -- every
    expensive step taken before the cheap one that could not work.
    """
    installed = " ".join(item.requested() for item in Image().agent_clis)

    assert "@anthropic-ai/claude-code" in installed
    assert "@openai/codex" in installed


def test_the_declared_runtimes_leave_their_version_to_the_launch() -> None:
    """An empty version is a per-launch resolution, not a declaration.

    The launch pins each one to the registry's current release before
    rendering, so the pin lives in the rendered Dockerfile rather than
    here -- a version declared here would freeze it instead.
    """
    assert all(not item.version for item in Image().agent_clis)


def test_a_client_and_the_engine_behind_it_are_read_separately() -> None:
    """The podman-docker package is why the client is still asked at all."""
    renamed = ContainerClient(binary="docker", client="podman", server="podman")

    assert renamed.drives_its_server()
    assert renamed.engine().identity_arguments(1, 1)[-1] == "--userns=keep-id"


def test_each_runtime_stands_down_by_one_declaration_rather_than_two() -> None:
    """Both launchers read the word the adapter owns, which the probe reads too.

    Spelled at each caller instead, they are three copies of a vendor's flag
    with no way to notice when one falls behind -- and the caller likeliest to
    fall behind is the one nobody watches open a session.
    """
    environment: EnvVars = {}

    assert (
        claude_settings(Claude(policy=confining_hooks(), sandbox=OuterContainer()))
        == CLAUDE_SANDBOX_OFF
    )
    assert (
        codex_envelope(confining_hooks(), environment, [], sandbox=LaunchSandbox.OUTER)
        == CODEX_CONFINEMENT.off
    )


def test_the_probe_opens_the_session_a_launch_opens() -> None:
    """A probe answering about a session nobody runs is this file's whole subject.

    An exercise carrying the mounts, the config home and the network, and not
    the flag standing the runtime's own sandbox down, opens a session whose
    settings still say the sandbox is on, finds no bubblewrap, and refuses
    for a confinement that cannot start in an unprivileged container and that
    no launch asks for. Ordered as well as present: the
    words are the runtime's own and have to reach it before the prompt does.
    """
    probe = agent_session_requirement(arguments=CLAUDE_CONFINEMENT.off)

    assert probe.exercise == Run(
        command=[
            "claude",
            *CLAUDE_CONFINEMENT.off,
            "-p",
            "Reply with exactly: SESSION_OK",
        ],
        expect="SESSION_OK",
    )


def test_a_relay_is_carried_without_reviving_the_sandbox_that_cannot_start() -> None:
    """Two packages once bought silence about a boundary that was not there.

    `socat` earns its place on its own account -- carrying something between
    two things that cannot address each other -- so it lands in the baseline
    rather than beside the confinement whose emptiness is a finding.
    """
    assert "socat" in Image().baseline
    assert Image().inner_sandbox == []


def test_the_image_carries_the_manager_its_own_runtimes_install_through() -> None:
    """A layer every image renders may not stand on a package one declares.

    `agent_clis` installs through `bun` whatever the manifest says, so a
    project with no JavaScript of its own -- and so no reason to declare that
    requirement -- built as far as the layer carrying the runtimes its
    sessions exist to run, and died there at `bun: command not found`.
    """
    image = Image()

    assert {item.manager for item in image.agent_clis} <= set(image.baseline)
