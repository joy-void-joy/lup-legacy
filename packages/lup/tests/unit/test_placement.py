"""Where a process runs, read once, with each fact its own.

A runtime's own sandbox on the host is the case that made one reading
necessary: it is in no container and is still not the operator's terminal,
which two predicates that were not each other's negation answered as
"neither".
"""

import pytest

from lup.coordination.identity import MEMBER_ENV
from lup.harness.environment import Placement, PlacementEnv
from lup.launch.preflight import NONCE_VARIABLE
from lup.policy.assets.host import sandbox_active
from lup.policy.identity import AGENT_IDENTITY_ENV
from lup.providers.runtime_homes import selected_runtime
from lup.workspace.context import SESSION_DIR_ENV, SESSION_ID_ENV


def test_nothing_set_is_the_operators_terminal() -> None:
    placement = Placement.of({})

    assert placement.host
    assert not placement.in_session


def test_a_sandboxed_session_on_the_host_is_neither_contained_nor_the_host() -> None:
    placement = Placement.of({"LUP_SANDBOX_ACTIVE": "1"})

    assert placement.sandboxed
    assert not placement.contained
    assert not placement.host


@pytest.mark.parametrize(
    ("environment", "contained"),
    [({"LUP_CONTAINED": "1"}, True), ({"LUP_CONTAINED": "0"}, False)],
)
def test_the_image_marker_is_read_as_it_is_baked(
    environment: dict[str, str], contained: bool
) -> None:
    placement = Placement.of(environment)

    assert placement.contained == contained
    assert placement.host == (not contained)


@pytest.mark.parametrize(
    "marker",
    [NONCE_VARIABLE, MEMBER_ENV, AGENT_IDENTITY_ENV, SESSION_DIR_ENV, SESSION_ID_ENV],
)
def test_any_session_marker_puts_a_process_in_a_session(marker: str) -> None:
    placement = Placement.of({marker: "set"})

    assert placement.in_session
    assert not placement.host


def test_an_empty_marker_names_no_session() -> None:
    assert not Placement.of({NONCE_VARIABLE: ""}).in_session


@pytest.mark.parametrize(
    ("environment", "runtime"),
    [
        ({"CLAUDE_CONFIG_DIR": "/home/someone/.claude"}, "claude"),
        ({"CODEX_HOME": "/home/someone/.codex"}, "codex"),
        ({}, None),
    ],
)
def test_the_runtime_is_the_configuration_home_its_launcher_selected(
    environment: dict[str, str], runtime: str | None
) -> None:
    assert selected_runtime(environment) == runtime


def test_a_launchs_environment_is_read_without_this_processs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LUP_CONTAINED", "1")
    monkeypatch.setenv("LUP_SANDBOX_ACTIVE", "1")

    assert Placement.of({}) == Placement()
    assert Placement.here().contained


@pytest.mark.parametrize("value", [None, "", "0", "1", "yes"])
def test_the_policy_hosts_bare_reading_agrees(
    monkeypatch: pytest.MonkeyPatch, value: str | None
) -> None:
    """The dispatcher's copy runs on a bare interpreter, so it is pinned here."""
    for field in PlacementEnv.model_fields.values():
        if isinstance(field.validation_alias, str):
            monkeypatch.delenv(field.validation_alias, raising=False)
    if value is not None:
        monkeypatch.setenv("LUP_SANDBOX_ACTIVE", value)

    assert sandbox_active() == Placement.here().sandboxed
