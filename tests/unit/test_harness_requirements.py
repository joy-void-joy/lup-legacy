"""Behavior tests for the declare-and-exercise requirement manifest.

The point of the manifest is that it says something true about the machine it
runs on, so most of these pin the distinctions a presence check cannot make:
installed-but-not-working, one capability spelled several ways, and a
diagnosis that stays quiet about a failure it does not explain.
"""

from pathlib import Path

import pytest
from pydantic import ValidationError

import lup.launch.session as launch_session
from lup.harness.egress import SessionEgress
from lup.harness.image import Podman
from lup.providers.claude.confinement import CLAUDE_CONFINEMENT
from lup.harness.ownership import source_digest
from lup.harness.toolchain import for_host
from lup.policy.survey import allowed_programs
from lup.policy.vocabulary import default_vocabulary
from lup_template.harness.catalog import portable_harness
from lup_template.harness.content.requirements import (
    carried_vocabulary,
    manifest,
)
from lup_template.harness.content.shell_vocabulary import SHELL_RULES
from lup.harness.requirements import (
    Advisory,
    AnyOf,
    EnvironmentRedirect,
    ExerciseOutcome,
    Finding,
    LostCapability,
    Manifest,
    Package,
    RefusedLaunch,
    Requirement,
    Run,
    SupplementaryGroup,
    refused,
)

WORKING = Run(command=["echo", "hello"])
"""A command every host this suite runs on has, exercised for real."""


def requirement(name: str, exercise: Run | AnyOf, **rest: object) -> Requirement:
    """One requirement with the fields a test does not care about filled in."""
    return Requirement.model_validate(
        {
            "capability": name,
            "purpose": f"whatever {name} is for",
            "exercise": exercise,
            "absence": LostCapability(capability=f"the {name} capability"),
            **rest,
        }
    )


def test_a_clean_exit_proves_a_requirement() -> None:
    found = requirement("echo", WORKING).check({})
    assert found.working
    assert [(item.text, item.urgency) for item in found.notices()] == [
        ("echo (host): working", "ready")
    ]


def test_a_roster_exercised_on_the_way_past_names_only_what_failed() -> None:
    """A launch asked nobody, and a command whose job is this answer did.

    One line per healthy requirement is identical every session and is what
    the one absence beside it has to be picked out of. Silence is the wrong
    answer to `harness requirements`, though, so which half is read belongs
    to the caller rather than to the finding.
    """
    working = requirement("echo", WORKING).check({})
    lost = requirement("absent", Run(command=["lup-no-such-program"])).check({})

    assert working.alarms() == []
    assert lost.alarms() == lost.notices()


def test_a_missing_program_is_named_as_missing_rather_than_as_a_failure() -> None:
    """`not on PATH` and `exited 1` send a reader to different places."""
    found = requirement("absent", Run(command=["lup-no-such-program"])).check({})
    assert not found.working
    assert "not on PATH" in found.detail


def test_a_program_that_runs_without_working_is_not_counted_as_working() -> None:
    """The failure a presence check cannot see: installed, and doing nothing.

    `expect` is the whole reason the manifest exercises rather than probes --
    a daemon client that answers while pointed at the wrong endpoint exits
    zero, and every `which` in the world calls that success.
    """
    found = requirement("wrong", Run(command=["echo", "nothing"], expect="banana"))
    checked = found.check({})
    assert not checked.working
    assert "output did not contain 'banana'" in checked.detail
    assert "Output: 'nothing'" in checked.detail


def test_any_of_takes_the_first_spelling_that_works() -> None:
    """A capability several programs provide is held when any one is."""
    exercise = AnyOf(
        alternatives=[Run(command=["lup-no-such-program"]), WORKING],
    )
    assert requirement("clipboard", exercise).check({}).working


def test_any_of_reports_every_spelling_it_tried_when_none_worked() -> None:
    """Naming one absent program would send an operator to install the wrong one."""
    exercise = AnyOf(
        alternatives=[
            Run(command=["lup-no-such-program"]),
            Run(command=["lup-also-absent"]),
        ],
    )
    detail = requirement("clipboard", exercise).check({}).detail
    assert "lup-no-such-program" in detail and "lup-also-absent" in detail


def test_a_lost_capability_does_not_stop_a_launch() -> None:
    """Absence is an ordinary answer: no clipboard is still a usable session."""
    found = requirement("clipboard", Run(command=["lup-no-such-program"])).check({})
    assert not found.refuses()
    assert refused([found]) == []


def test_a_refusing_requirement_stops_a_launch() -> None:
    stopped = Requirement(
        capability="uv",
        purpose="every command here",
        exercise=Run(command=["lup-no-such-program"]),
        absence=RefusedLaunch(because="nothing runs without it"),
    ).check({})
    assert stopped.refuses()
    assert refused([stopped]) == [stopped]


def test_an_environment_redirect_names_the_endpoint_rather_than_the_service() -> None:
    """The diagnosis that replaces an evening: the variable, not the daemon."""
    redirect = EnvironmentRedirect(variable="DOCKER_HOST")
    spoken = redirect.cause(
        {"DOCKER_HOST": "unix:///nowhere/at/all.sock"},
        ExerciseOutcome(proved=False, detail="cannot connect"),
    )
    assert "DOCKER_HOST points at /nowhere/at/all.sock" in spoken
    assert "does not exist" in spoken


def test_an_environment_redirect_is_silent_when_the_target_is_there() -> None:
    redirect = EnvironmentRedirect(variable="DOCKER_HOST")
    outcome = ExerciseOutcome(proved=False, detail="cannot connect")
    assert redirect.cause({"DOCKER_HOST": "unix:///"}, outcome) == ""
    assert redirect.cause({}, outcome) == ""


def test_a_remote_endpoint_is_not_diagnosed_as_a_missing_local_socket() -> None:
    redirect = EnvironmentRedirect(variable="DOCKER_HOST")
    outcome = ExerciseOutcome(proved=False, detail="connection failed")
    assert (
        redirect.cause({"DOCKER_HOST": "tcp://container.example:2376"}, outcome) == ""
    )


def test_a_group_diagnosis_stays_quiet_about_a_failure_it_cannot_explain() -> None:
    """The red herring this gate exists for.

    A group difference is nearly always present and nearly never the cause.
    Ungated, this volunteers "this session is not in the docker group" for a
    daemon reached over a socket the user owns outright -- true about
    the groups, unrelated to the failure, and actively costly: a reader who
    acts on it starts a new session and finds nothing changed.
    """
    group = SupplementaryGroup(group="docker")
    unrelated = ExerciseOutcome(proved=False, detail="docker is not on PATH")
    assert group.cause({"USER": "anybody"}, unrelated) == ""


def test_a_group_diagnosis_speaks_when_the_failure_is_a_refusal() -> None:
    """Gated on the failure looking like one a group would cause, not on nothing."""
    group = SupplementaryGroup(group="lup-no-such-group")
    refusal = ExerciseOutcome(proved=False, detail="permission denied")
    assert "no lup-no-such-group group" in group.cause({"USER": "anybody"}, refusal)


def test_a_manifest_collects_the_packages_an_image_installs_without_repeating_one() -> (
    None
):
    """One roster feeds the preflight and the image, so they cannot disagree."""
    manifest = Manifest(
        requirements=[
            requirement("first", WORKING, where="image", install=["shared", "one"]),
            requirement("second", WORKING, where="both", install=["shared", "two"]),
        ]
    )
    assert [item.name for item in manifest.packages()] == ["shared", "one", "two"]


def test_a_host_requirement_never_reaches_the_image() -> None:
    """The container runtime is the case: a socket inside is a full host escape."""
    manifest = Manifest(
        requirements=[requirement("runtime", WORKING, where="host", install=["docker"])]
    )
    assert manifest.packages() == []


def test_an_image_requirement_is_not_exercised_on_the_host() -> None:
    """A laptop with no TypeScript toolchain is not a laptop with a problem."""
    manifest = Manifest(
        requirements=[
            requirement("bun", Run(command=["lup-no-such-program"]), where="image")
        ]
    )
    assert manifest.check({}) == []
    assert manifest.on_the_host(setting_up=True) == []


def test_an_advisory_is_silent_at_launch_and_spoken_at_setup() -> None:
    """A nicety repeated before every session becomes a line people skip."""
    convenience = Requirement(
        capability="clipboard",
        purpose="pasting the next command",
        checked="setup",
        exercise=Run(command=["lup-no-such-program"]),
        absence=Advisory(improves="copying a command to the clipboard"),
    )
    manifest = Manifest(requirements=[convenience])
    assert manifest.check({}) == []
    spoken = manifest.check({}, setting_up=True)
    assert [item.requirement.capability for item in spoken] == ["clipboard"]
    assert not spoken[0].refuses()
    assert not spoken[0].requirement.absence.costly()


def test_an_expensive_check_of_an_important_thing_is_expressible() -> None:
    """Two axes one field would conflate, and the case that needs both.

    Same-path bind mounting is a prerequisite of the worktree rail -- its
    absence costs a real capability -- and exercising it starts a container,
    which is far too slow to pay before every session. One field could say
    "important" or "cheap to check" but not both.
    """
    expensive = Requirement(
        capability="same-path bind mounts",
        purpose="the worktree rail",
        checked="setup",
        exercise=Run(command=["lup-no-such-program"]),
        absence=LostCapability(capability="multi-worker resolve"),
    )
    manifest = Manifest(requirements=[expensive])
    assert manifest.check({}) == []
    at_setup = manifest.check({}, setting_up=True)
    assert len(at_setup) == 1
    assert at_setup[0].requirement.absence.costly()


def test_an_empty_manifest_checks_nothing_and_installs_nothing() -> None:
    """A project with a pure-Python toolchain must not be told it is missing anything."""
    assert Manifest().check({}) == []
    assert Manifest().packages() == []


def test_a_failing_finding_says_what_was_lost_and_what_needed_it() -> None:
    """Why it failed and what is missing as a result are different questions."""
    lines = requirement("clipboard", Run(command=["lup-no-such-program"])).check({})
    rendered = "\n".join(item.text for item in lines.notices())
    assert "the clipboard capability is unavailable" in rendered
    assert "clipboard (host): check failed" in rendered


def test_a_container_that_never_started_is_not_read_as_an_absent_capability() -> None:
    """An engine that refused has said nothing about what was inside it.

    A bind mount whose source does not exist makes podman refuse the
    container. Read as an absent capability, both at-launch boundary probes
    would announce what neither measured -- an unreachable proxy, an
    untunnelled egress -- under advice to tear down a network that is fine.
    """
    # `sh` stands in for the container engine here. What is being pinned is a
    # *contained* command exiting 125, the code both engines reserve for a run
    # that failed before the container's own command existed.
    engine_refused = Run(command=["-c", "exit 125"]).behind(["sh"])
    found = requirement("proxy", engine_refused).check({})

    assert not found.working
    assert not found.exercised
    rendered = "\n".join(item.text for item in found.notices())
    assert "proxy (host): check could not run" in rendered
    assert "the proxy capability is unavailable" not in rendered
    assert "Result unknown" in rendered


def test_a_probe_that_ran_and_failed_is_still_the_capabilitys_own_answer() -> None:
    """Inside a container that started, a failure is the capability's."""
    probe_said_no = Run(command=["-c", "exit 1"]).behind(["sh"])
    found = requirement("proxy", probe_said_no).check({})

    assert not found.working
    assert found.exercised
    assert "the proxy capability is unavailable" in "\n".join(
        item.text for item in found.notices()
    )


def test_an_unprefixed_exercise_reads_125_as_its_own_exit_code() -> None:
    """Nothing started a container, so 125 is the program's own answer."""
    found = requirement("odd", Run(command=["sh", "-c", "exit 125"])).check({})

    assert not found.working
    assert found.exercised


def test_only_a_container_prefix_makes_an_exercise_contained() -> None:
    """The flag is `behind`'s to set, because it is the only thing prefixing one."""
    assert not Run(command=["echo", "hello"]).contained
    assert Run(command=["echo", "hello"]).behind(["podman", "run"]).contained


def test_the_declared_manifest_names_only_programs_this_project_invokes() -> None:
    """A manifest that invents a prerequisite refuses machines that were fine.

    Pinned as a count rather than a list so adding a genuine requirement is
    an ordinary edit, while the roster staying small stays deliberate:
    ripgrep, for one, is no prerequisite, because this project never invokes
    it.
    """
    from lup_template.harness.content.requirements import manifest

    MANIFEST = manifest()

    declared = [item.capability for item in MANIFEST.requirements]
    assert "ripgrep" not in declared
    assert all(item.purpose for item in MANIFEST.requirements)
    assert [item for item in MANIFEST.requirements if item.absence.refuses()]


def test_the_offered_container_requirement_defaults_out_of_the_image() -> None:
    """A container holding a runtime socket can mount the whole host into a sibling.

    Asked of the offered default rather than of one composition, because the
    default is what protects every project that never thought about it. A
    project may still overrule it -- that is what the parameter is for -- but
    it has to say so.
    """
    from lup.harness.toolchain import container_requirement, default_manifest

    offered = container_requirement()
    assert offered.where == "host"
    assert offered.install == []
    assert not any(
        "docker" in package.name for package in default_manifest().packages()
    )


def test_every_offered_requirement_lets_a_project_place_and_install_it() -> None:
    """The seams are parameters, so an adopter overrules without forking lup."""
    from lup.harness.toolchain import bun_requirement, container_requirement

    moved = bun_requirement(where="both", install=[Package(name="bun-bin")])
    assert moved.where == "both" and [item.name for item in moved.install] == [
        "bun-bin"
    ]
    inside = container_requirement(where="image", install=[Package(name="docker.io")])
    assert inside.where == "image" and [item.name for item in inside.install] == [
        "docker.io"
    ]


def test_the_declared_manifest_asks_the_host_for_nothing_image_side() -> None:
    """bun and typescript live in the image, so a bare host is never faulted."""
    from lup_template.harness.content.requirements import manifest

    MANIFEST = manifest()

    on_host = [item.capability for item in MANIFEST.on_the_host(setting_up=True)]
    assert "bun" not in on_host and "typescript" not in on_host
    assert {"bun", "typescript"} <= {item.name for item in MANIFEST.packages()}


def test_every_declared_package_is_obtained_by_something_that_verifies_it() -> None:
    """The property the base image was chosen for, pinned so a change is visible.

    Three managers verify their own downloads and the fourth has to say how,
    by pinning the digest its shell line checks against. What this asserts is
    the property rather than the manager: a `script` package with a digest has
    done the verifying, and one without cannot be constructed at all.

    A rule catches the source shape; this catches the roster the image is
    actually built from, including anything a constructor default
    reintroduces.
    """
    from lup.harness.toolchain import default_manifest
    from lup_template.harness.content.requirements import manifest

    for roster in (default_manifest(), manifest()):
        assert not [item for item in roster.packages() if not item.verified()]


def test_a_finding_carries_the_requirement_that_produced_it() -> None:
    """So a caller reporting one can say what it was for without a lookup."""
    declared = requirement("echo", WORKING)
    assert Finding(requirement=declared, working=True).requirement is declared


def test_the_declared_client_is_the_portable_one_whatever_this_host_runs() -> None:
    """A container client is a fact about the machine, not about the project.

    The ownership digest hashes the whole declaration, so a probe of this
    host inside it reports generated artifacts as stale on any machine whose
    client differs -- and even on *one* machine it moves between two runs
    minutes apart, when a stale podman pid file is cleaned up between them and
    the resolution flips.
    """
    declared = manifest()
    carried = [item for item in declared.requirements if item.by_client]

    assert carried, "this project declares no client-carried exercise"
    assert all(item.exercise.programs() == ["docker"] for item in carried)
    # Two *different* checkouts, which is the comparison that catches this.
    # Asking the same root twice cannot fail: a host fact resolved from the
    # root gives the same answer both times, so the digest would measure
    # stable against the one input that never varies while moving for every
    # worktree that is not this one.
    assert source_digest(
        portable_harness(root=Path("/tmp/one-checkout"))
    ) == source_digest(portable_harness(root=Path("/tmp/another-checkout")))


def test_the_preflight_points_every_client_exercise_at_what_answered() -> None:
    """The other half: it does matter which client, where they actually run.

    A Docker CLI pointed at a podman socket answers first and is refused as
    undrivable, so exercising it verifies a boundary no session opens.
    `for_host` is the one place that knows, because it is the one place the
    exercises run.
    """
    pointed = for_host(manifest(), Podman(binary="podman"))
    carried = [item for item in pointed.requirements if item.by_client]

    assert carried
    assert all(item.exercise.programs() == ["podman"] for item in carried)


def test_pointing_a_manifest_at_a_host_moves_only_what_a_machine_supplies() -> None:
    """The client and the checkout are filled in; nothing else is touched.

    Two things a machine supplies, and the test has to allow for both. A
    ``Run`` keeps every argument the declaration wrote and moves only its
    program. A ``MountProbe`` has no arguments to keep — it is a shape, and
    resolving it is what turns it into a command — so the assertion for it is
    that the command it resolves to names this checkout rather than any other.
    """
    declared = manifest()
    here = Path("/tmp/some-checkout")
    pointed = for_host(declared, Podman(binary="podman"), here)

    assert [item.capability for item in pointed.requirements] == [
        item.capability for item in declared.requirements
    ]
    # Through `programs`, because the roster holds more than one shape of
    # exercise and only `Run` has a command to index -- the clipboard entry
    # is an `AnyOf` over several spellings.
    assert [item.exercise.programs() for item in pointed.requirements] != [
        item.exercise.programs() for item in declared.requirements
    ]
    spelled = {
        item.capability: item.exercise
        for item in declared.requirements
        if isinstance(item.exercise, Run)
    }
    assert all(
        item.exercise.model_dump(exclude={"command"})
        == spelled[item.capability].model_dump(exclude={"command"})
        for item in pointed.requirements
        if item.capability in spelled
    )


def test_a_mount_probe_is_aimed_at_the_checkout_a_machine_names() -> None:
    """The shape carries no path; the resolution carries this one.

    The declaration cannot name a checkout, because it is hashed into the
    ownership digest and a worktree is where somebody put it. Named there,
    two checkouts of one commit would hash differently, so every one but the
    last to generate would read its own committed tree as stale.
    """
    here = Path("/tmp/some-checkout")
    aimed = for_host(manifest(), Podman(binary="podman"), here).requirements
    probe = next(
        item for item in aimed if item.capability == "same-path bind mounts"
    ).exercise

    assert isinstance(probe, Run)
    assert probe.command[0] == "podman"
    assert f"{here}:{here}:ro" in probe.command
    assert probe.command[-1] == f"cat {here / 'pyproject.toml'} >/dev/null"


def test_nothing_in_a_declared_manifest_names_a_path_or_a_client() -> None:
    """The whole roster, checked for the fact that must not be in it.

    A per-entry assertion would pass for every entry written before the one
    that reintroduces the trap. This asks the question of the manifest itself,
    so a new requirement that bakes a host path fails here rather than at
    whichever adopter cloned to a different directory.
    """
    written = manifest().model_dump_json()

    assert str(Path.cwd()) not in written
    assert str(Path.home()) not in written


def test_the_image_half_is_exercised_behind_the_argv_a_session_opens() -> None:
    """An image requirement runs inside the container, not beside it.

    Excluded from the host roster and exercised nowhere else, image-side
    entries would leave the whole boundary — proxy, mounts, config home —
    declared and unverified. An entry spelling its own `docker run` hides
    that, because it verifies a container with no network and no mounts.
    """
    opening = ["podman", "run", "--rm", "--network", "lup-net", "lup-agent:abc"]
    inside = manifest().check_inside({}, opening)
    session = next(
        item
        for item in inside
        if item.requirement.capability == "contained agent session"
    )

    assert [item.requirement.capability for item in inside]
    carried = session.requirement.exercise
    assert isinstance(carried, Run)
    assert carried.command[: len(opening)] == opening
    assert carried.command[len(opening) :] == [
        "claude",
        *CLAUDE_CONFINEMENT.off,
        "-p",
        "Reply with exactly: SESSION_OK",
    ]


def test_a_launch_asks_only_the_image_entries_marked_always() -> None:
    """The whole image roster at every launch would cost a container each.

    The axis is doing real work here rather than expressing importance: what
    a launch pays for is the handful whose absence means the session can do
    nothing at all, and a model call and a toolchain version are what
    somebody setting a machine up hears once.

    The criterion is a failure invisible from outside. Without the endpoint a
    session cannot think; without the placement it cannot be said to be
    anywhere, and every operation after is placed by a boundary nothing
    observed. The third is the same criterion reached from the other
    direction: a session missing part of its shell vocabulary is not stopped
    by the absence and is not told about it either, because a program that is
    not there exits 127 and a shell reads 127 as an answer -- so it works on,
    holding conclusions it has no way to doubt. That is the one kind of
    absence a session cannot discover for itself, which is what a launch is
    for. The fourth is the boundary's own: a read-only bind the engine did
    not apply leaves the host's git `config` writable in a session that
    opens looking entirely healthy.
    """
    declared = manifest()
    opening = ["podman", "run", "--rm", "lup-agent:abc"]
    at_launch = {
        item.requirement.capability
        for item in declared.check_inside({}, opening, setting_up=False)
    }
    at_setup = {
        item.requirement.capability for item in declared.check_inside({}, opening)
    }

    assert at_launch == {
        "session reaches the model endpoint",
        "inside placement",
        "read-only binds",
        "shell commands",
    }
    assert "contained agent session" in at_setup - at_launch


def test_a_launch_asks_something_whatever_the_network_posture_is() -> None:
    """Emptying this set is how the verification disappears without failing.

    Were every entry marked always about the proxy, declaring a posture with
    none would leave the launch asking nothing at all -- and nothing asked is
    indistinguishable from everything passing. The property is that
    the set has a member, not which member it has: a posture answers with its
    own vocabulary, and one that answers with silence is the failure.
    """
    opening = ["podman", "run", "--rm", "lup-agent:abc"]
    for boundary in (SessionEgress(), SessionEgress(mode="host")):
        asked = {
            item.requirement.capability
            for item in manifest(boundary).check_inside({}, opening, setting_up=False)
        }

        assert asked, f"a {boundary.mode} launch exercises nothing on the way in"


def test_the_launch_probe_drops_the_terminal_it_cannot_have() -> None:
    """The session's own argv, minus the one flag a captured probe cannot take.

    The same argv rather than a fresh one is the whole point — an exercise
    assembled separately verifies a container no session opens — so the
    difference has to be exactly this and nothing else.
    """
    opening = ["podman", "run", "--rm", "-it", "-v", "vol:/cfg", "lup-agent:abc"]

    assert launch_session.probing(opening) == [
        "podman",
        "run",
        "--rm",
        "-v",
        "vol:/cfg",
        "lup-agent:abc",
    ]


def test_one_host_roster_spans_every_target() -> None:
    """Two targets declare one machine, so its capabilities are asked once.

    The command that spans every target holds a manifest per target, and the
    host halves answer the same question — what this machine carries. Asked
    one manifest at a time, a reader gets the whole roster twice with nothing
    saying the second is a repeat, and `same-path bind mounts` starts its
    probe container twice to establish what the first did.
    """
    declared = manifest()
    spanned = Manifest.across([declared, declared])

    assert [item.capability for item in spanned.requirements] == [
        item.capability for item in declared.requirements
    ]


def test_spanning_targets_keeps_what_only_one_of_them_declares() -> None:
    """Deduplication is per capability, not a choice between whole manifests.

    A project may hold one target to something the other never needed, and a
    span that took the first manifest whole would drop it — reporting a
    machine ready for a target whose own requirement was never exercised.
    """
    shared = Requirement(
        capability="uv",
        purpose="the environment",
        exercise=Run(command=["uv", "--version"]),
        absence=LostCapability(capability="syncing"),
    )
    only_one = Requirement(
        capability="bun",
        purpose="the javascript toolchain",
        exercise=Run(command=["bun", "--version"]),
        absence=LostCapability(capability="bundling"),
    )
    spanned = Manifest.across(
        [Manifest(requirements=[shared]), Manifest(requirements=[shared, only_one])]
    )

    assert [item.capability for item in spanned.requirements] == ["uv", "bun"]


def test_a_script_package_names_what_it_verifies_against() -> None:
    """A shell line with no digest installs whatever the server answered with.

    The refusal is at construction rather than at render, for the sharper
    version of the reason the missing-command refusal is: an unverified install
    produces an image that is *not* missing anything, so nothing later asks
    what it got.
    """
    with pytest.raises(ValidationError, match="names no digest"):
        Package(name="elan", manager="script", command="curl -fsSL x | sh")


def test_a_managed_package_may_not_restate_an_integrity_claim() -> None:
    """pacman, bun and uv check their own downloads, so a digest beside one is
    a second claim nothing compares against the first."""
    with pytest.raises(ValidationError, match="verifies its own downloads"):
        Package(name="ripgrep", digest="00" * 32)


def test_verification_is_a_property_rather_than_a_manager() -> None:
    """What the roster is asked is whether an install is checked, not how.

    A `script` carrying a digest has done the verifying the other three
    managers do for a caller, and a test spelling `manager != "script"` would
    go on refusing it — which is the shape this method exists to replace.
    """
    verified = Package(
        name="elan",
        manager="script",
        command='curl -fsSL -o /tmp/x https://example.invalid/x && echo "d  /tmp/x" | sha256sum -c -',
        digest="d" * 64,
    )
    assert verified.verified()
    assert Package(name="ripgrep").verified()


def test_every_promised_program_is_measured_or_deliberately_dropped() -> None:
    """Nothing leaves the promise quietly.

    The risk is not a package missing from an image. It is the vocabulary and
    the image being two lists nothing compares, so a word can be declared
    safe for an agent to run unattended and carried nowhere -- and the agent
    that runs it gets 127, which a shell hands to `||` as an ordinary answer.
    Measured inside the agent container, with `cmp` missing:
    `cmp -s A B && echo IDENTICAL || echo DIFFERS` prints DIFFERS for two
    byte-identical files.

    So the two lists are held against each other here, and a word may leave
    the probe only by being named in the subtraction, where it has to give a
    reason.
    """
    promised = allowed_programs(SHELL_RULES.over(default_vocabulary()))
    carried = carried_vocabulary()

    assert set(promised) - set(carried) == {"man"}
    assert set(carried) <= set(promised)


def test_the_comparison_tools_are_promised_and_installed() -> None:
    """The measured pair, pinned on both sides of the seam that separated them."""
    assert {"diff", "cmp"} <= set(carried_vocabulary())
    assert "diffutils" in [item.name for item in manifest().packages()]


def test_the_vocabulary_probe_reads_the_policy_rather_than_a_copy() -> None:
    """A word joins the probe by joining the table, not by being copied here."""
    entry = next(
        item for item in manifest().requirements if item.capability == "shell commands"
    )

    assert entry.exercise.programs() == list(carried_vocabulary())
    assert not entry.absence.refuses()
    assert entry.absence.costly()
