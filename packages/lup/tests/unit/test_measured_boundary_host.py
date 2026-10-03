"""What a session may believe about its own boundary, read inside the dispatcher.

The half the whole layer is named for. `contained` is not read off a constant
an image bakes, such as `LUP_CONTAINED` — a constant answers yes for any
container built from that image, for a bare `run` holding none of the lease,
and for an uncontained session whose launcher forwarded a variable the operator
happened to export. Each of those is a session reporting a boundary nothing put
under it.

So the answer comes from a ledger a launch wrote, and only from the one that
launch named. Everything here is a case where the honest answer is "no boundary
was measured", which every caller reads as the fail-closed one.
"""

import json
from pathlib import Path

import pytest

import lup.policy.assets.host as policy_host
from lup.policy.assets.host import (
    contained,
    defers_unjudged,
    delivers,
    execution_write_refusal,
    measured_boundary,
    read_only_mount_points,
    readonly_write_targets,
    record_held,
    unleased_write_targets,
)

MEASURED = {
    "profile": ["contained"],
    "contained": ["yes"],
    "unjudged_ambient": ["ask"],
    "delivered": ["inside_placement", "question_relay"],
    "blocked": ["host_executor"],
}


def written(root: Path, nonce: str, ledger: dict[str, list[str]]) -> None:
    """One launch's measurement, where that launch would have put it."""
    path = root / ".lup" / "preflight" / f"{nonce}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(ledger), encoding="utf-8")


def test_an_inherited_variable_no_longer_grants_containment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reading a measured boundary refuses, stated as the case that fails.

    A launcher forwards its own environment, so an uncontained session started
    from a shell exporting `LUP_CONTAINED=1` would report a boundary with no
    container under it — and place every operation by a wall that is not
    there. Nothing consults that variable, so the claim costs nothing.
    """
    monkeypatch.setenv("LUP_CONTAINED", "1")
    monkeypatch.delenv("LUP_BOUNDARY_NONCE", raising=False)

    assert not contained(measured_boundary(tmp_path))


def test_a_session_believes_only_the_ledger_its_own_launch_named(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, launch_record_held: None
) -> None:
    """A ledger left by another launch is a measurement of another session.

    Which is the same class of wrong answer as the constant, reached from the
    other direction — so the nonce decides, and a session holding none reads
    nothing at all.
    """
    written(tmp_path, "the-launch-that-measured", MEASURED)

    monkeypatch.setenv("LUP_BOUNDARY_NONCE", "some-other-launch")
    assert not contained(measured_boundary(tmp_path))

    monkeypatch.setenv("LUP_BOUNDARY_NONCE", "the-launch-that-measured")
    assert contained(measured_boundary(tmp_path))


def test_each_capability_is_answered_from_what_was_observed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Delivered and blocked are different facts and both are recorded."""
    written(tmp_path, "launch", MEASURED)
    monkeypatch.setenv("LUP_BOUNDARY_NONCE", "launch")

    measured = measured_boundary(tmp_path)

    assert delivers(measured, "inside_placement")
    assert delivers(measured, "question_relay")
    assert not delivers(measured, "host_executor")
    assert not delivers(measured, "checkpoint_store")


def test_the_ambient_policy_is_the_profiles_and_asks_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A session that could not read its profile does not infer a seamless one."""
    written(tmp_path, "asking", MEASURED)
    written(tmp_path, "deferring", {**MEASURED, "unjudged_ambient": ["defer"]})

    monkeypatch.setenv("LUP_BOUNDARY_NONCE", "asking")
    assert not defers_unjudged(measured_boundary(tmp_path))

    monkeypatch.setenv("LUP_BOUNDARY_NONCE", "deferring")
    assert defers_unjudged(measured_boundary(tmp_path))

    monkeypatch.delenv("LUP_BOUNDARY_NONCE")
    assert not defers_unjudged(measured_boundary(tmp_path))


def test_every_way_of_having_no_measurement_reads_the_same(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Absent, unnamed, wrecked, and the wrong shape are one answer.

    A session whose launcher predates this has no ledger and gets exactly what
    a session whose boundary failed to stand gets — which is the fail-closed
    answer, and the only one that cannot be wrong in the dangerous direction.
    """
    monkeypatch.setenv("LUP_BOUNDARY_NONCE", "launch")
    assert measured_boundary(tmp_path) == {}
    assert measured_boundary(None) == {}

    path = tmp_path / ".lup" / "preflight" / "launch.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    assert measured_boundary(tmp_path) == {}

    path.write_text(json.dumps(["not", "a", "mapping"]), encoding="utf-8")
    assert measured_boundary(tmp_path) == {}


def test_a_ledger_carrying_something_other_than_strings_drops_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, launch_record_held: None
) -> None:
    """This half validates by hand, because it may reach no parser but json.

    A capability name that is not a string cannot be compared to one, and
    keeping it would put a value of unknown shape in front of every later
    read. Dropped rather than refused: the rest of the measurement is still
    a measurement.
    """
    written(tmp_path, "launch", MEASURED)
    path = tmp_path / ".lup" / "preflight" / "launch.json"
    path.write_text(
        json.dumps({**MEASURED, "delivered": ["question_relay", 7, None]}),
        encoding="utf-8",
    )
    monkeypatch.setenv("LUP_BOUNDARY_NONCE", "launch")

    measured = measured_boundary(tmp_path)

    assert measured["delivered"] == ["question_relay"]
    assert contained(measured)


@pytest.mark.parametrize("location", ["subdirectory", "other-checkout", "absent"])
def test_launch_ledger_stays_pinned_when_a_tool_runs_elsewhere(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    location: str,
    launch_record_held: None,
) -> None:
    launch = tmp_path / "launch"
    other = tmp_path / "other"
    readonly = other / "authored"
    measured = {
        **MEASURED,
        "writable_roots": [str(launch), str(other)],
        "read_only_roots": [str(readonly)],
    }
    written(launch, "launch", measured)
    written(other, "launch", {"writable_roots": [str(other)]})
    monkeypatch.setenv("LUP_BOUNDARY_NONCE", "launch")
    monkeypatch.setenv("LUP_BOUNDARY_ROOT", str(launch))
    cwd = {
        "subdirectory": launch / "src",
        "other-checkout": other,
        "absent": None,
    }[location]

    assert measured_boundary(cwd) == measured
    assert execution_write_refusal(str(readonly / "document.md"), cwd)
    assert not execution_write_refusal(str(other / "ordinary.md"), cwd)


@pytest.mark.parametrize("pinned", ["missing", "relative", "symlink"])
def test_a_failed_pinned_ledger_never_uses_the_current_directory_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pinned: str
) -> None:
    written(tmp_path, "launch", MEASURED)
    alias = tmp_path / "alias"
    alias.symlink_to(tmp_path, target_is_directory=True)
    monkeypatch.setenv("LUP_BOUNDARY_NONCE", "launch")
    monkeypatch.setenv(
        "LUP_BOUNDARY_ROOT",
        {"missing": str(tmp_path / "missing"), "relative": ".", "symlink": str(alias)}[
            pinned
        ],
    )

    assert measured_boundary(tmp_path) == {}


def test_a_grant_declared_from_a_home_is_read_in_the_home_reading_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`~/.cache/uv` is recorded as written, and every reader expands it here.

    The declared sandbox grants join the lease spelled as they were declared,
    because they answer for whichever home reads them. Read without expanding,
    `~` would name a directory called `~` under the working directory, and the
    toolchain cache every `uv` command writes would be refused as outside the
    boundary it was granted into.
    """
    home = tmp_path / "home"
    launch = tmp_path / "launch"
    held = home / ".cache" / "uv" / "held"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("LUP_BOUNDARY_NONCE", "launch")
    monkeypatch.delenv("LUP_BOUNDARY_ROOT", raising=False)
    # Uncontained, so the lease alone answers: inside a measured container a
    # path no root names is the container's own, and `~/.bashrc` would be too.
    measured = {
        **MEASURED,
        "contained": [],
        "writable_roots": [str(launch), "~/.cache/uv"],
        "read_only_roots": ["~/.cache/uv/held"],
    }
    written(launch, "launch", measured)

    cached = str(home / ".cache" / "uv" / "probe.txt")
    assert not execution_write_refusal(cached, launch)
    assert execution_write_refusal(str(home / ".bashrc"), launch)
    assert execution_write_refusal(str(held / "x"), launch)
    assert unleased_write_targets([cached, str(home / ".bashrc")], measured) == [
        str(home / ".bashrc")
    ]
    assert readonly_write_targets([cached, str(held / "x")], measured) == [
        str(held / "x")
    ]


MOUNTINFO = (
    "22 1 0:21 / / rw,relatime - overlay overlay rw\n"
    "30 22 0:40 /home/u/w /home/u/w rw,relatime - ext4 /dev/sda1 rw\n"
    "31 30 0:40 /home/u/w/.lup/preflight /home/u/w/.lup/preflight ro,relatime"
    " - ext4 /dev/sda1 rw\n"
    "32 30 0:40 /home/u/a\\040b /home/u/a\\040b/.lup/preflight ro,nosuid"
    " - ext4 /dev/sda1 rw\n"
    "33 22 0:41 / /proc rw,nosuid - proc proc rw\n"
)
"""A container's table: a writable checkout, its ledger held, one escaped path."""


def test_the_read_only_mount_points_are_read_off_the_table() -> None:
    assert read_only_mount_points(MOUNTINFO) == [
        "/home/u/w/.lup/preflight",
        "/home/u/a b/.lup/preflight",
    ]


def test_a_directory_is_held_only_where_it_is_itself_a_read_only_mount(
    tmp_path: Path,
) -> None:
    """Beneath one is not enough: the mount at the ledger's own path is the hold."""
    table = tmp_path / "mountinfo"
    table.write_text(MOUNTINFO, encoding="utf-8")

    assert record_held(Path("/home/u/w/.lup/preflight"), table)
    assert not record_held(Path("/home/u/w/.lup/preflight/launch.json"), table)
    assert not record_held(Path("/home/u/w/.lup"), table)
    assert not record_held(Path("/home/u/w/.lup/preflight"), tmp_path / "unread")


def test_a_ledger_claiming_a_container_nothing_holds_loses_that_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A script on the host writing its own ledger cannot claim a container.

    The directory is writable wherever no container holds it, so a ledger
    read through no read-only mount says what whoever wrote it said. Its
    claim to a container is dropped and the rest stands, as it does for
    every ledger an uncontained launch wrote.
    """
    written(tmp_path, "launch", MEASURED)
    monkeypatch.setenv("LUP_BOUNDARY_NONCE", "launch")
    monkeypatch.delenv("LUP_BOUNDARY_ROOT", raising=False)

    forged = measured_boundary(tmp_path)

    assert not contained(forged)
    assert delivers(forged, "inside_placement")


def test_a_ledger_read_through_its_hold_is_believed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    written(tmp_path, "launch", MEASURED)
    monkeypatch.setenv("LUP_BOUNDARY_NONCE", "launch")
    monkeypatch.delenv("LUP_BOUNDARY_ROOT", raising=False)
    held = (tmp_path / ".lup" / "preflight").resolve()
    monkeypatch.setattr(
        policy_host, "record_held", lambda directory, *_rest: directory == held
    )

    assert contained(measured_boundary(tmp_path))
