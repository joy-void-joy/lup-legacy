"""A target's own MCP companion queues durable mail, and hands over what its queue took.

What the native queue accepted is on its way into the session as a turn, so
it leaves the mailbox then: the delivery hook at the session's next call
hands over only what no wake carried.
"""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from unittest.mock import Mock

import pytest

import lup.coordination.bare.arrival as arrival
import lup.coordination.wake as routing
from lup.coordination.identity import member_ref
from lup.coordination.relay import MailboxRelay
from lup.coordination.repository import RepositoryPeers
from lup.coordination.wake import WakePath, wake


@pytest.fixture(autouse=True)
def native_queue(monkeypatch: pytest.MonkeyPatch) -> Mock:
    """No test may reach a real native daemon or initiate a model turn."""
    command = Mock(return_value="accepted")
    monkeypatch.setattr(routing, "QUEUE_COMMAND", command)
    monkeypatch.setattr(routing, "execution_scope", lambda: "target-scope")
    monkeypatch.setattr(arrival, "execution_scope", lambda: "target-scope")
    return command


@pytest.fixture
def relay(tmp_path: Path) -> MailboxRelay:
    peers = RepositoryPeers(tmp_path)
    peers.join("recipient", tmp_path, wake=WakePath(runtime="codex"))
    assert arrival.bind(
        peers.root,
        "recipient",
        "codex",
        arrival.Arrival(
            session_id="native-thread",
            hook_event_name="SessionStart",
            cwd=str(tmp_path),
        ),
        ("SessionStart",),
        str(tmp_path / "native-home"),
    )
    return MailboxRelay(root=tmp_path, member_id="recipient", queue_timeout_seconds=0.2)


def test_cross_container_mail_queues_only_from_the_target_boundary(
    relay: MailboxRelay, native_queue: Mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    peers = RepositoryPeers(relay.root)
    peers.send("recipient", "mail from another container")
    target = peers.row("recipient")
    assert target is not None
    monkeypatch.setattr(routing, "execution_scope", lambda: "sender-scope")
    assert not wake(target.wake, "mail", relay.root).reached
    assert relay.tick() is not None
    native_queue.assert_not_called()

    monkeypatch.setattr(routing, "execution_scope", lambda: "target-scope")
    outcome = relay.tick()

    assert outcome is not None and outcome.reached
    arguments = native_queue.call_args.args
    assert arguments[0] == f"CODEX_HOME={relay.root / 'native-home'}"
    assert arguments[4] == "native-thread"
    assert "mail from another container" in arguments[6]
    assert native_queue.call_args.kwargs["_timeout"] == 0.2
    assert peers.waiting("recipient").messages == []


def test_what_the_queue_accepted_is_handed_over_once_across_a_restart(
    relay: MailboxRelay, native_queue: Mock
) -> None:
    peers = RepositoryPeers(relay.root)
    first = peers.cohort.mail.send(member_ref("recipient"), "identical body")
    second = peers.cohort.mail.send(member_ref("recipient"), "identical body")
    assert first.id != second.id

    assert relay.tick() is not None
    restarted = MailboxRelay(root=relay.root, member_id=relay.member_id)
    assert restarted.tick() is None

    native_queue.assert_called_once()
    assert native_queue.call_args.args[6].count("identical body") == 2
    assert peers.waiting("recipient").messages == []


def test_only_mail_no_wake_carried_is_queued(
    relay: MailboxRelay, native_queue: Mock
) -> None:
    peers = RepositoryPeers(relay.root)
    peers.send("recipient", "first")
    relay.tick()
    peers.send("recipient", "second")

    relay.tick()

    assert native_queue.call_count == 2
    assert "second" in native_queue.call_args.args[6]
    assert "first" not in native_queue.call_args.args[6]
    assert peers.waiting("recipient").messages == []
    assert relay.tick() is None


def test_queue_failure_is_retried_and_leaves_the_mail_waiting(
    relay: MailboxRelay, native_queue: Mock
) -> None:
    peers = RepositoryPeers(relay.root)
    peers.send("recipient", "still pending")
    native_queue.side_effect = [RuntimeError("unavailable"), "accepted"]

    failed = relay.tick()
    assert failed is not None and not failed.reached
    assert len(peers.waiting("recipient").messages) == 1
    succeeded = relay.tick()

    assert succeeded is not None and succeeded.reached
    assert peers.waiting("recipient").messages == []
    assert native_queue.call_count == 2


@pytest.mark.parametrize("changed", ["session", "home", "scope"])
def test_mail_after_a_new_native_route_is_queued_by_that_route(
    relay: MailboxRelay,
    native_queue: Mock,
    monkeypatch: pytest.MonkeyPatch,
    changed: str,
) -> None:
    peers = RepositoryPeers(relay.root)
    peers.send("recipient", "taken by the old route")
    relay.tick()
    peers.send("recipient", "for the new route")
    session = "other-thread" if changed == "session" else "native-thread"
    home = relay.root / ("other-home" if changed == "home" else "native-home")
    if changed == "scope":
        monkeypatch.setattr(arrival, "execution_scope", lambda: "other-scope")
        monkeypatch.setattr(routing, "execution_scope", lambda: "other-scope")
    assert arrival.bind(
        peers.root,
        "recipient",
        "codex",
        arrival.Arrival(
            session_id=session, hook_event_name="SessionStart", cwd=str(relay.root)
        ),
        ("SessionStart",),
        str(home),
    )

    relay.tick()

    assert native_queue.call_count == 2
    arguments = native_queue.call_args.args
    assert arguments[0] == f"CODEX_HOME={home}"
    assert arguments[4] == session
    assert "for the new route" in arguments[6]
    assert "taken by the old route" not in arguments[6]


def test_missing_binding_waits_for_a_native_hook_instead_of_guessing(
    tmp_path: Path, native_queue: Mock
) -> None:
    peers = RepositoryPeers(tmp_path)
    peers.join("recipient", tmp_path, wake=WakePath(runtime="codex"))
    peers.send("recipient", "waiting for native identity")
    relay = MailboxRelay(root=tmp_path, member_id="recipient")
    outcome = relay.tick()
    assert outcome is not None and not outcome.reached
    native_queue.assert_not_called()
    assert arrival.bind(
        peers.root,
        "recipient",
        "codex",
        arrival.Arrival(
            session_id="bound", hook_event_name="SessionStart", cwd=str(tmp_path)
        ),
        ("SessionStart",),
        str(tmp_path / "home"),
    )
    assert relay.tick() is not None
    native_queue.assert_called_once()


def test_only_its_own_pending_mail_is_relayed(
    relay: MailboxRelay, native_queue: Mock
) -> None:
    peers = RepositoryPeers(relay.root)
    peers.join("other", relay.root, wake=WakePath(runtime="codex"))
    peers.send("other", "not for recipient")

    assert relay.tick() is None

    native_queue.assert_not_called()
    assert len(peers.waiting("other").messages) == 1


def test_multiple_servers_share_a_lock_and_queue_once(
    relay: MailboxRelay, native_queue: Mock
) -> None:
    peers = RepositoryPeers(relay.root)
    peers.send("recipient", "once")
    entered = Event()
    release = Event()

    def blocked(*_arguments: str, **_options: object) -> str:
        entered.set()
        assert release.wait(5)
        return "accepted"

    native_queue.side_effect = blocked
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(relay.tick)
        try:
            assert entered.wait(5)
            assert MailboxRelay(root=relay.root, member_id="recipient").tick() is None
        finally:
            release.set()
        assert first.result() is not None

    assert relay.tick() is None
    native_queue.assert_called_once()


def test_a_hand_over_that_fails_after_acceptance_repeats_the_nudge(
    relay: MailboxRelay, native_queue: Mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    peers = RepositoryPeers(relay.root)
    peers.send("recipient", "durable mail survives a failed hand-over")
    with monkeypatch.context() as fail:
        fail.setattr(
            RepositoryPeers, "delivered", Mock(side_effect=OSError("disk unavailable"))
        )
        with pytest.raises(OSError):
            relay.tick()
    assert len(peers.waiting("recipient").messages) == 1
    relay.tick()
    assert native_queue.call_count == 2
    assert peers.waiting("recipient").messages == []


def test_a_redirect_the_queue_took_waits_for_the_hook_and_is_queued_once(
    relay: MailboxRelay, native_queue: Mock
) -> None:
    peers = RepositoryPeers(relay.root)
    peers.send("recipient", "stop: that branch is closed", redirect=True)

    first = relay.tick()
    again = relay.tick()

    assert first is not None and first.reached
    assert again is None
    native_queue.assert_called_once()
    [left] = peers.waiting("recipient").messages
    assert left.redirect and left.carried


async def test_cancellation_joins_the_inflight_queue_before_server_shutdown(
    relay: MailboxRelay, native_queue: Mock
) -> None:
    peers = RepositoryPeers(relay.root)
    peers.send("recipient", "pending")
    entered = Event()
    release = Event()

    def blocked(*_arguments: str, **_options: object) -> str:
        entered.set()
        assert release.wait(5)
        return "accepted"

    native_queue.side_effect = blocked
    serving = asyncio.create_task(relay.run())
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        serving.cancel()
        await asyncio.sleep(0)
        assert not serving.done()
    finally:
        release.set()
        if not entered.is_set():
            serving.cancel()
    with pytest.raises(asyncio.CancelledError):
        await serving
    native_queue.assert_called_once()
    assert peers.waiting("recipient").messages == []


def test_an_unjoined_or_departed_member_never_creates_a_relay(
    tmp_path: Path, native_queue: Mock
) -> None:
    relay = MailboxRelay(root=tmp_path, member_id="missing")
    assert relay.tick() is None
    peers = RepositoryPeers(tmp_path)
    assert not peers.root.exists()
    peers.join("missing", tmp_path, wake=WakePath(runtime="codex"))
    peers.send("missing", "pending")
    peers.leave("missing")
    assert relay.tick() is None
    native_queue.assert_not_called()
    assert not (peers.root / "wake-relay").exists()


async def test_companion_reports_storage_failure_without_payloads_and_retries(
    relay: MailboxRelay,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    retried = asyncio.Event()
    loop = asyncio.get_running_loop()
    attempts = Mock()

    def failing_once(_relay: MailboxRelay) -> None:
        attempts()
        if attempts.call_count == 1:
            raise ValueError("private-message-body")
        loop.call_soon_threadsafe(retried.set)

    monkeypatch.setattr(MailboxRelay, "tick", failing_once)
    serving = asyncio.create_task(
        relay.model_copy(update={"interval_seconds": 0.01}).run()
    )
    try:
        await asyncio.wait_for(retried.wait(), 5)
    finally:
        serving.cancel()
        with pytest.raises(asyncio.CancelledError):
            await serving

    assert attempts.call_count >= 2
    assert "Mail remains pending" in caplog.text
    assert "ValueError" in caplog.text
    assert str(relay.lock_path) in caplog.text
    assert "private-message-body" not in caplog.text
