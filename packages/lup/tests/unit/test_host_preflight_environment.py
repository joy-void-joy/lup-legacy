"""Host probes cannot turn an operator's launcher into what reads as a launched session."""

import os
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import Mock

import pytest

import lup.launch.session as session
from lup.harness.requirements import (
    SENTINEL_VARIABLE,
    HostFacts,
    Manifest,
    SentinelProbe,
)
from lup.launch.preflight import NONCE_VARIABLE, LaunchSentinels
from lup.sandbox.known import host_side


@pytest.mark.parametrize("fails", [False, True])
def test_host_preflight_leaves_the_launcher_environment_as_it_found_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fails: bool
) -> None:
    monkeypatch.delenv(NONCE_VARIABLE, raising=False)
    monkeypatch.setenv(SENTINEL_VARIABLE, "original-sentinel")
    original = dict(os.environ)
    sentinels = LaunchSentinels()
    probe = Mock()
    probe.check.return_value = []
    if fails:
        probe.check.side_effect = RuntimeError("host probe failed")
    monkeypatch.setattr(session, "for_host", Mock(return_value=probe))
    monkeypatch.setattr(session, "container_client", Mock())

    with (
        pytest.raises(RuntimeError, match="host probe failed")
        if fails
        else nullcontext()
    ):
        session.report_requirements(
            Manifest(), tmp_path, sentinels=sentinels, in_passing=True
        )

    assert dict(os.environ) == original
    assert host_side()
    forwarded = probe.check.call_args.args[0]
    assert forwarded[NONCE_VARIABLE] == sentinels.nonce
    assert forwarded[SENTINEL_VARIABLE] == sentinels.host


def test_the_host_probe_observes_the_host_value_through_its_own_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(SENTINEL_VARIABLE, raising=False)
    facts = HostFacts(
        checkout=tmp_path, host_sentinel="host-value", inside_sentinel="inside-value"
    )

    host = SentinelProbe(side="host").given(facts).run()
    inside = SentinelProbe(side="inside").given(facts).run()

    assert host.proved, host.detail
    assert not inside.proved
    assert SENTINEL_VARIABLE not in os.environ
