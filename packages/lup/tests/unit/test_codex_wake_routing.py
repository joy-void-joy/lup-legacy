"""Native queue delivery selects the target home only in its proven scope."""

from pathlib import Path
import os
from unittest.mock import Mock

import pytest

import lup.providers.codex.wake as routing
import lup.coordination.bare.scope as scopes
from lup.coordination.identity import mint_member_id
from lup.coordination.repository import RepositoryPeers
from lup.coordination.wake import WakePath
from lup.providers.wake import wake


def test_roster_preserves_native_home_scope_and_session(tmp_path: Path) -> None:
    peers = RepositoryPeers(tmp_path)
    member = mint_member_id()
    path = WakePath(
        runtime="codex",
        handle="thread",
        session="thread",
        home="/native-home",
        scope="scope",
    )
    peers.join(member, tmp_path, cli_name="peer", wake=path)

    assert next(row.wake for row in peers.present()) == path


def test_queue_selects_target_home_and_preserves_argument_boundaries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    command = Mock()
    monkeypatch.setattr(routing, "QUEUE_COMMAND", command)
    monkeypatch.setattr(routing, "execution_scope", lambda: "same")
    monkeypatch.setenv("CODEX_HOME", "/caller-home")
    path = WakePath(runtime="codex", handle="thread", home="/target home", scope="same")

    result = wake(path, "look at durable mail", tmp_path)

    assert result.reached
    command.assert_called_once_with(
        "CODEX_HOME=/target home",
        "codex",
        "queue",
        "--thread",
        "thread",
        "--message",
        "look at durable mail",
        _cwd=str(tmp_path),
        _timeout=20.0,
    )


@pytest.mark.parametrize(
    ("home", "scope"),
    [("/target", "foreign"), ("/target", ""), ("", "same"), ("relative", "same")],
)
def test_unknown_or_foreign_execution_boundary_never_queues_into_caller_home(
    monkeypatch: pytest.MonkeyPatch, home: str, scope: str
) -> None:
    command = Mock()
    monkeypatch.setattr(routing, "QUEUE_COMMAND", command)
    monkeypatch.setattr(routing, "execution_scope", lambda: "same")

    result = wake(
        WakePath(runtime="codex", handle="thread", home=home, scope=scope), "mail"
    )

    assert not result.reached
    assert "durable mail remains pending" in result.reason
    command.assert_not_called()


def test_native_queue_failure_is_reported_without_acknowledging_mail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        routing, "QUEUE_COMMAND", Mock(side_effect=RuntimeError("daemon unavailable"))
    )
    monkeypatch.setattr(routing, "execution_scope", lambda: "same")

    result = wake(
        WakePath(runtime="codex", handle="thread", home="/target", scope="same"), "mail"
    )

    assert not result.reached
    assert "daemon unavailable" in result.reason


@pytest.fixture
def kernel_evidence(monkeypatch: pytest.MonkeyPatch) -> dict[Path, Mock]:
    paths = [
        Path("/proc/self/root"),
        Path("/proc/sys/kernel/random/boot_id"),
        *[Path("/proc/self/ns", name) for name in ("mnt", "net", "user")],
    ]
    evidence = {path: Mock() for path in paths}
    for index, node in enumerate(evidence.values()):
        node.stat.return_value.st_dev = 1
        node.stat.return_value.st_ino = index + 10
        node.read_text.return_value = "boot-id\n"
    monkeypatch.setattr(scopes, "Path", lambda *parts: evidence[Path(*parts)])
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    return evidence


def test_scope_is_stable_for_identical_kernel_evidence(
    kernel_evidence: dict[Path, Mock],
) -> None:
    first = scopes.execution_scope()
    assert first
    assert scopes.execution_scope() == first


@pytest.mark.parametrize(
    "path",
    [
        Path("/proc/self/root"),
        *[Path("/proc/self/ns", name) for name in ("mnt", "net", "user")],
    ],
)
@pytest.mark.parametrize("identity", ["st_dev", "st_ino"])
def test_changed_root_or_namespace_never_reaches_the_original_daemon(
    kernel_evidence: dict[Path, Mock],
    monkeypatch: pytest.MonkeyPatch,
    path: Path,
    identity: str,
) -> None:
    original = scopes.execution_scope()
    setattr(kernel_evidence[path].stat.return_value, identity, 500)
    assert scopes.execution_scope() != original
    command = Mock()
    monkeypatch.setattr(routing, "QUEUE_COMMAND", command)
    result = wake(
        WakePath(runtime="codex", handle="thread", home="/target", scope=original),
        "mail",
    )
    assert not result.reached
    command.assert_not_called()


def test_changed_effective_user_never_reaches_the_original_daemon(
    kernel_evidence: dict[Path, Mock], monkeypatch: pytest.MonkeyPatch
) -> None:
    original = scopes.execution_scope()
    monkeypatch.setattr(os, "geteuid", lambda: 1001)
    assert scopes.execution_scope() != original
    command = Mock()
    monkeypatch.setattr(routing, "QUEUE_COMMAND", command)
    result = wake(
        WakePath(runtime="codex", handle="thread", home="/target", scope=original),
        "mail",
    )
    assert not result.reached
    command.assert_not_called()


def test_changed_boot_changes_the_scope(kernel_evidence: dict[Path, Mock]) -> None:
    original = scopes.execution_scope()
    kernel_evidence[
        Path("/proc/sys/kernel/random/boot_id")
    ].read_text.return_value = "other-boot"
    assert scopes.execution_scope() != original


@pytest.mark.parametrize(
    "path",
    [
        Path("/proc/self/root"),
        Path("/proc/sys/kernel/random/boot_id"),
        *[Path("/proc/self/ns", name) for name in ("mnt", "net", "user")],
    ],
)
def test_unreadable_kernel_evidence_is_not_a_scope(
    kernel_evidence: dict[Path, Mock], path: Path
) -> None:
    kernel_evidence[path].read_text.side_effect = PermissionError("unreadable")
    kernel_evidence[path].stat.side_effect = PermissionError("unreadable")
    assert scopes.execution_scope() == ""
