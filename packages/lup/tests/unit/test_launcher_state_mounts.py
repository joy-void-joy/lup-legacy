"""No container mounts any part of what the launcher keeps for itself.

The store of trusted repositories and the superseded-volume record under
`$XDG_STATE_HOME/lup`, the person's lup config under `$XDG_CONFIG_HOME/lup`
-- each profile's account and credentials among it -- and the plugin
revisions contained sessions run their hooks from: a container reaching any
of them could rewrite what the next launch trusts. So a mount at one, above
one, or inside one refuses the launch, naming the mount and what moves the
directory -- except what the launcher lends a session read-only, such as the
dashboard's pulse its status line reads.
"""

from pathlib import Path

import pytest

from lup.devtools.dashboard.companion import Dashboard
from lup.launch.companions import CompanionPlace, companions_home
from lup.launch.environments import revisions_home
from lup.launch.pointer_trust import launcher_state_exposure
from lup.sandbox.known import store_directory
from lup.sandbox.rail import Lease, same_path


@pytest.fixture
def homes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """This test's own state, config and home, each where its variable says."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    return tmp_path


@pytest.mark.parametrize(
    ("mounted", "named"),
    [
        ("state/lup", "XDG_STATE_HOME"),
        ("state", "XDG_STATE_HOME"),
        ("state/lup/trust", "XDG_STATE_HOME"),
        ("config/lup", "XDG_CONFIG_HOME"),
        ("config/lup/profiles/work", "XDG_CONFIG_HOME"),
        ("config", "XDG_CONFIG_HOME"),
        ("home/.cache", "HOME"),
        ("home/.cache/lup/codex-revisions/abc", "HOME"),
    ],
    ids=[
        "state-itself",
        "state-enclosed",
        "state-inside",
        "config-itself",
        "a-profile",
        "config-enclosed",
        "revisions-enclosed",
        "a-revision",
    ],
)
def test_a_mount_at_above_or_inside_launcher_state_is_refused(
    homes: Path, mounted: str, named: str
) -> None:
    for lease in (
        Lease(writable=same_path([homes / mounted])),
        Lease(read_only=same_path([homes / mounted])),
    ):
        said = launcher_state_exposure(lease)

        assert str(homes / mounted) in said
        assert named in said


def test_the_whole_home_is_refused_once_for_each_directory_it_carries(
    homes: Path,
) -> None:
    said = launcher_state_exposure(Lease(writable=same_path([homes])))

    assert said.count(str(homes)) >= 3
    assert "store of trusted repositories" in said
    assert "credentials" in said


def test_mounts_beside_launcher_state_pass(homes: Path) -> None:
    beside = [
        homes / "work",
        homes / "config" / "other",
        homes / "state" / "elsewhere",
        homes / "home" / ".cache" / "lup" / "environments" / "dev-0123",
    ]

    assert launcher_state_exposure(Lease(writable=same_path(beside))) == ""
    assert revisions_home() == homes / "home" / ".cache" / "lup" / "codex-revisions"


def test_what_the_dashboard_lends_its_session_passes_read_only(homes: Path) -> None:
    dashboard = Dashboard()
    place = CompanionPlace(state=dashboard.slot(homes).directory, ports={"page": 8766})
    lent = [mount.path for mount in dashboard.contribution(place, homes).mounts]

    assert lent
    assert all(path.is_relative_to(store_directory()) for path in lent)
    assert launcher_state_exposure(Lease(read_only=same_path(lent))) == ""
    assert "XDG_STATE_HOME" in launcher_state_exposure(Lease(writable=same_path(lent)))


@pytest.mark.parametrize(
    "mounted",
    ["", "user", "user/dashboard", "user/dashboard/other", "user/lent", "lent"],
    ids=["home", "a-sharing", "a-companion", "beside-lent", "a-name", "too-shallow"],
)
def test_a_companion_directory_beyond_what_it_lends_is_refused(
    homes: Path, mounted: str
) -> None:
    del homes
    path = companions_home() / mounted

    assert str(path) in launcher_state_exposure(Lease(read_only=same_path([path])))
