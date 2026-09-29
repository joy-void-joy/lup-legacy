# lup: ignore[set-shape]
# Test fixtures and assertions construct these shapes deliberately.
"""The declared tool groups, and what one session's state makes of them.

The declaration exists to make tool-group drift between backends impossible:
both paths build the same list, and what a subprocess backend is told to serve
is read off it rather than written beside it. These tests are the tripwire —
that a group with nothing to build is neither registered nor served, that the
groups lup ships arrive with their companions, and that a group servable only
by name reaches no default set.
"""

from pathlib import Path

import pytest

from lup.coordination.identity import MEMBER_ENV
from lup.coordination.peer_tools import RosterPulse
from lup.coordination.relay import MailboxRelay
from lup.orchestration.reflection import ReviewGate
from lup.sandbox.container import Sandbox
from lup.tools.policy import BaseToolPolicy
from lup.tools.toolsets import (
    SessionNeeds,
    SessionToolset,
    assembled,
    named_only,
    registered,
    served_names,
    startup_names,
)
from lup_template.agent.toolsets import EXAMPLE_GROUP, declared_tool_groups


def needs(
    base: Path,
    *,
    realtime: bool = False,
    with_sandbox: bool = False,
    member: str = "toolset-test",
) -> SessionNeeds:
    """One session's state, as a tool server or an in-process assembly resolves it.

    A session has an identity — its own runtime gives it one even where no
    launcher minted a durable member id — and the coordination group is built
    against it. Omitting it builds one group fewer than the names served,
    which is the drift these tests exist to catch.
    """
    return SessionNeeds(
        session_dir=base / "session",
        root=base,
        gate=ReviewGate(),
        outputs_dir=base / "outputs",
        sandbox=(
            Sandbox(session_id="toolset-test", shared_dir=base / "shared")
            if with_sandbox
            else None
        ),
        realtime_dir=(base / "realtime") if realtime else None,
        member=member,
    )


def build(
    base: Path,
    *,
    realtime: bool = False,
    with_sandbox: bool = False,
    member: str = "toolset-test",
) -> SessionToolset:
    """The toolset one session carries, from the declaration every backend reads."""
    return assembled(
        declared_tool_groups(),
        needs(base, realtime=realtime, with_sandbox=with_sandbox, member=member),
    )


def test_served_names_are_the_groups_this_session_builds(tmp_path: Path) -> None:
    for realtime in (False, True):
        state = needs(tmp_path / str(realtime), realtime=realtime, with_sandbox=True)
        toolset = assembled(declared_tool_groups(), state)

        served = served_names(declared_tool_groups(), state)

        assert set(served) == set(toolset.groups) - {EXAMPLE_GROUP}


def test_a_group_served_only_by_name_is_in_no_default_set(tmp_path: Path) -> None:
    """The example group ships fabricated data and reaches no live agent by default."""
    toolset = build(tmp_path)
    groups = declared_tool_groups()

    assert EXAMPLE_GROUP in toolset.groups
    assert EXAMPLE_GROUP in named_only(groups)
    assert EXAMPLE_GROUP not in startup_names(groups)
    servers = registered(toolset, groups, BaseToolPolicy())
    assert EXAMPLE_GROUP not in {server.name for server in servers}
    assert toolset.groups[EXAMPLE_GROUP]


def test_a_runtime_starts_no_server_for_a_session_bound_group() -> None:
    """The relay exists once a session has a mailbox, which none has at startup."""
    assert "session" not in startup_names(declared_tool_groups())


def test_the_session_group_waits_on_a_mailbox(tmp_path: Path) -> None:
    without = build(tmp_path / "without")
    with_relay = build(tmp_path / "with", realtime=True)

    assert "session" not in without.groups
    assert with_relay.groups["session"]


def test_the_coordination_server_beats_for_the_session_it_serves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The group's server keeps the session's pulse, under the identity it joined as."""
    # A launched test session carries its own member id, which would win over
    # the fallback this asserts; the unlaunched case is the one under test.
    monkeypatch.delenv(MEMBER_ENV, raising=False)
    toolset = build(tmp_path)

    [companion, relay] = toolset.companions["coordination"]

    assert isinstance(companion, RosterPulse)
    assert isinstance(relay, MailboxRelay)
    assert relay.member_id == "toolset-test"
    assert companion.member_id == "toolset-test"
    assert set(toolset.companions) == {"coordination"}


def test_a_session_with_no_identity_has_neither_group_nor_companion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(MEMBER_ENV, raising=False)
    toolset = build(tmp_path, member="")

    assert "coordination" not in toolset.groups
    assert "ledger" not in toolset.groups
    assert toolset.companions == {}


def test_submit_output_is_owned_by_the_turn_runtime(tmp_path: Path) -> None:
    toolset = build(tmp_path)

    assert "submit_output" not in {tool.name for tool in toolset.groups["notes"]}


def test_in_process_registration_does_not_claim_a_receiver_lifecycle(
    tmp_path: Path,
) -> None:
    servers = registered(build(tmp_path), declared_tool_groups(), BaseToolPolicy())
    [coordination] = [server for server in servers if server.name == "coordination"]
    assert coordination.companions == []
