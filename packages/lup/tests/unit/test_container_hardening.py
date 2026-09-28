"""What a session's processes may hold inside their container, and may come to hold.

Measured in a session container this hardening did not yet cover: the agent
ran as the operator's uid with an empty effective set, under the engine's
default bounding set (``00000000800405fb`` on rootless podman) and with
``NoNewPrivs`` off, beside thirteen setuid-root binaries (``su``, ``mount``,
``passwd`` among them). The flags close the way back up; the entrypoint clears
what an engine hands a non-root user in its inheritable and ambient sets, which
is how a capability given back for ``sudo`` reached the agent itself.
"""

import shutil
import stat
from pathlib import Path

import pytest
import sh

import lup.launch.container as container
from lup.harness.image import (
    Capability,
    Docker,
    Image,
    Podman,
    SessionPrivileges,
)
from lup.harness.requirements import Manifest
from lup.launch.declaration import InnerSandbox, NoSandbox, OuterContainer
from lup.launch.refusal import LaunchRefused
from lup.providers.claude.login import CLAUDE_LOGIN


class Reached(Exception):
    """Raised where a launch goes past the step under test."""


def test_every_session_container_drops_every_capability_and_gains_none() -> None:
    arguments = Image().run_arguments(Path("/checkout"), 1000, 1000)

    assert arguments[arguments.index("--cap-drop") + 1] == "ALL"
    assert "--cap-add" not in arguments
    assert arguments[arguments.index("--security-opt") + 1] == "no-new-privileges:true"


def test_a_session_granted_sudo_holds_what_administering_takes_and_may_raise() -> None:
    arguments = Image().run_arguments(
        Path("/checkout"), 1000, 1000, privileges=SessionPrivileges(sudo=True)
    )

    added = [
        arguments[index + 1]
        for index, word in enumerate(arguments)
        if word == "--cap-add"
    ]
    assert arguments[arguments.index("--cap-drop") + 1] == "ALL"
    assert added == [capability.value for capability in Capability]
    assert "no-new-privileges:true" not in arguments


def test_nothing_reaching_past_the_container_can_be_given_back() -> None:
    """A capability no member names is one no declaration can spell."""
    for outward in ("SYS_ADMIN", "NET_ADMIN", "NET_RAW", "SYS_PTRACE", "SYS_MODULE"):
        assert outward not in {capability.value for capability in Capability}


def test_the_session_argv_carries_the_privileges_it_was_given() -> None:
    def opened(privileges: SessionPrivileges) -> list[str]:
        return Image().session_arguments(
            tag="lup-agent:x",
            checkout=Path("/checkout"),
            uid=1000,
            gid=1000,
            writable={},
            read_only={},
            state_volume="lup-claude-x",
            config_home_env="CLAUDE_CONFIG_DIR",
            privileges=privileges,
        )

    assert "no-new-privileges:true" in opened(SessionPrivileges())
    assert "--cap-add" in opened(SessionPrivileges(sudo=True))


def test_sudo_is_in_the_image_only_of_a_session_granted_it() -> None:
    plain = Image().dockerfile(Manifest())
    administered = Image().dockerfile(Manifest(), SessionPrivileges(sudo=True))

    assert "sudoers" not in plain and "--needed sudo" not in plain
    assert "/etc/sudoers.d/lup-session" in administered
    assert container.image_tag(plain) != container.image_tag(administered)


def test_the_entrypoint_hands_the_agent_nothing_to_inherit() -> None:
    image = Image()

    assert (
        image.entrypoint()
        .rstrip()
        .endswith('exec setpriv --inh-caps=-all --ambient-caps=-all -- "$@"')
    )
    assert image.entrypoint() in image.dockerfile(Manifest())
    assert "util-linux" in image.baseline


def capabilities(argv: list[str]) -> dict[str, str]:
    """The capability sets of a process running ``argv``, read from its own status."""
    status = str(sh.Command(argv[0])(*argv[1:], "grep", "^Cap", "/proc/self/status"))
    return dict(line.split(":\t") for line in status.splitlines())


def test_the_entrypoint_clears_an_ambient_set_the_engine_handed_the_user(
    tmp_path: Path,
) -> None:
    """The defect, reproduced: a non-root user holding capabilities through ambient.

    Podman places capabilities given back with ``--cap-add`` into a non-root
    ``--user``'s ambient set, so the agent held them without sudo. A user
    namespace keeping its capabilities as ambient is that same shape, and the
    entrypoint the image runs is executed as rendered.
    """
    if shutil.which("unshare") is None:
        pytest.skip("unshare is not on PATH")
    holding = [
        "unshare",
        "--user",
        "--map-user=1000",
        "--map-group=1000",
        "--keep-caps",
        "--",
    ]
    try:
        before = capabilities(holding)
    except sh.ErrorReturnCode as refused:
        pytest.skip(f"this host refuses an unprivileged user namespace: {refused}")
    entry = tmp_path / "lup-entrypoint"
    entry.write_text(Image(config_home=str(tmp_path / "cfg")).entrypoint())
    entry.chmod(entry.stat().st_mode | stat.S_IXUSR)

    # A container's environment rather than this session's, which may carry
    # the variables that send the entrypoint to seed a home.
    bare = ["env", "-i", "PATH=/usr/sbin:/usr/bin:/bin"]

    after = capabilities([*holding, *bare, "sh", str(entry)])

    assert int(before["CapAmb"], 16) != 0
    assert {name: int(after[name], 16) for name in ("CapInh", "CapEff", "CapAmb")} == {
        "CapInh": 0,
        "CapEff": 0,
        "CapAmb": 0,
    }


def test_only_the_container_wall_grants_sudo() -> None:
    assert OuterContainer().privileges() == SessionPrivileges()
    assert OuterContainer(sudo=True).privileges() == SessionPrivileges(sudo=True)
    assert InnerSandbox().privileges() == SessionPrivileges()
    assert NoSandbox().privileges() == SessionPrivileges()


def reporting(tmp_path: Path, name: str, answer: str) -> str:
    """A client that answers ``info`` the way one engine does, and nothing else."""
    script = tmp_path / name
    script.write_text(f"#!/bin/sh\nprintf '%s\\n' '{answer}'\n")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return str(script)


def test_each_engine_says_in_its_own_words_whether_it_runs_rootless(
    tmp_path: Path,
) -> None:
    rootless_docker = reporting(
        tmp_path, "docker-rootless", '["name=seccomp,profile=builtin","name=rootless"]'
    )
    rootful_docker = reporting(tmp_path, "docker-rootful", '["name=seccomp"]')

    assert Docker(binary=rootless_docker).rootless()
    assert not Docker(binary=rootful_docker).rootless()
    assert Podman(binary=reporting(tmp_path, "podman-rootless", "true")).rootless()
    assert not Podman(binary=reporting(tmp_path, "podman-rootful", "false")).rootless()
    assert not Docker(binary=str(tmp_path / "absent")).rootless()


class Rootful(Docker, frozen=True):
    def rootless(self) -> bool:
        return False


class Rootless(Podman, frozen=True):
    def rootless(self) -> bool:
        return True


def test_sudo_is_refused_on_a_rootful_engine_saying_where_packages_belong(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def reached(*_arguments: object, **_keywords: object) -> None:
        raise Reached()

    monkeypatch.setattr(container, "judged_roots", reached)

    def opened(engine: Docker | Podman, privileges: SessionPrivileges) -> None:
        container.contained_argv(
            Image(),
            Manifest(),
            tmp_path,
            None,
            None,
            CLAUDE_LOGIN,
            engine=engine,
            privileges=privileges,
        )

    with pytest.raises(LaunchRefused) as refused:
        opened(Rootful(), SessionPrivileges(sudo=True))
    said = str(refused.value)
    assert "rootless" in said and "host's root" in said
    assert "tooling" in said and "vanish" in said
    with pytest.raises(Reached):
        opened(Rootful(), SessionPrivileges())
    with pytest.raises(Reached):
        opened(Rootless(), SessionPrivileges(sudo=True))
