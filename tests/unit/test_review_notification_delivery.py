"""Browser decisions reach a requester's own native queue through durable mail."""

from pathlib import Path
from typing import Final
from unittest.mock import Mock

import pytest
import sh
from httpx import ASGITransport, AsyncClient
from pydantic import BaseModel

import lup.coordination.bare.arrival as arrival
import lup.coordination.wake as routing
from lup.channels.models import Door, publish_atomic
from lup.coordination.relay import MailboxRelay, WakeReceipts
from lup.coordination.repository import RepositoryPeers
from lup.coordination.wake import WakePath
from lup.devtools.dashboard.reviews import (
    ReviewDecision,
    ReviewSnapshot,
    dashboard_app,
)
from lup.devtools.review import app as review
from lup.devtools.review.app import ReviewDetail
from lup.policy.operations import Operation
from lup.policy.relay import PersistentQuestion
from tests.unit.native import bound

BASE_URL: Final = "http://127.0.0.1:8765"
TOKEN: Final = "isolated-browser-operator"
MEMBER: Final = "launched-recipient"
THREAD: Final = "native-requester-thread"
NOTE: Final = "Operator decision delivery probe: keep the exact reviewed operation."


@pytest.mark.parametrize("approved", [True, False])
@pytest.mark.parametrize("failed_write", [0, 1, 2])
async def test_browser_decision_relays_from_the_recipient_scope_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    approved: bool,
    failed_write: int,
) -> None:
    writes = 0

    def publish(path: Path, record: BaseModel) -> None:
        nonlocal writes
        writes += 1
        if writes == failed_write:
            raise OSError("Notification diagnostic store unavailable")
        publish_atomic(path, record)

    monkeypatch.setattr("lup.devtools.review.notifications.publish_atomic", publish)
    upstream = tmp_path / "upstream"
    consumer = tmp_path / "consumer"
    for checkout in (upstream, consumer):
        sh.git("init", "--quiet", str(checkout))
    home = tmp_path / "recipient-home"
    native_queue = Mock(return_value="accepted")
    monkeypatch.setattr(routing, "QUEUE_COMMAND", native_queue)
    monkeypatch.setattr(arrival, "execution_scope", lambda: "recipient-scope")
    monkeypatch.setattr(routing, "execution_scope", lambda: "browser-host-scope")
    peers = RepositoryPeers(consumer)
    peers.join(MEMBER, consumer, wake=WakePath(runtime="codex"))
    assert arrival.bind(
        peers.root,
        MEMBER,
        "codex",
        arrival.Arrival(
            session_id=THREAD, hook_event_name="SessionStart", cwd=str(consumer)
        ),
        ("SessionStart",),
        str(home),
    )
    target = upstream / "must-not-execute"
    operation = Operation(
        id="operation-browser-delivery",
        session=THREAD,
        requester=MEMBER,
        tool="Bash",
        payload={"command": f"touch {target}"},
        cwd=upstream,
        worktree=upstream,
    )
    entry = review.relay(upstream).record(
        bound(
            PersistentQuestion(
                id="browser-delivery",
                operation=operation,
                fingerprint="",
                reason="The operator must review this exact command.",
                rule="shell:test",
                eligible=["operator"],
                resumption="native_retry",
            )
        )
    )
    application = dashboard_app(BASE_URL, TOKEN, (upstream, consumer), discover=False)
    async with AsyncClient(
        transport=ASGITransport(app=application), base_url=BASE_URL
    ) as http:
        listed = await http.get(
            "/api/reviews", headers={"Authorization": f"Bearer {TOKEN}"}
        )
        assert listed.status_code == 200
        [summary] = ReviewSnapshot.model_validate(listed.json()).reviews
        response = await http.post(
            f"/api/reviews/{summary.key}/answer",
            headers={"Authorization": f"Bearer {TOKEN}", "Origin": BASE_URL},
            json={
                "approved": approved,
                "note": NOTE,
                "fingerprint": entry.fingerprint,
            },
        )

    reopened = dashboard_app(BASE_URL, TOKEN, (upstream, consumer), discover=False)
    async with AsyncClient(
        transport=ASGITransport(app=reopened), base_url=BASE_URL
    ) as http:
        historical = await http.get(
            f"/api/reviews/{summary.key}", headers={"Authorization": f"Bearer {TOKEN}"}
        )

    assert response.status_code == 200
    decision = ReviewDecision.model_validate(response.json())
    assert historical.status_code == 200
    persisted = ReviewDetail.model_validate(historical.json()).notification
    assert persisted is not None
    if failed_write == 2:
        assert "no confirmed outcome" in persisted.detail
        assert "Notification diagnostic store unavailable" in caplog.text
    else:
        assert persisted.queued and not persisted.woken
    if failed_write == 1:
        assert (
            "Notification diagnostic store unavailable" in decision.notification.detail
        )
        assert "Notification diagnostic store unavailable" in persisted.detail
    settled = review.relay(upstream).find(entry.id)
    assert settled == decision.review.question
    assert settled is not None and settled.answer is not None
    assert settled.state == ("approved" if approved else "rejected")
    assert settled.answer.note == NOTE
    assert not decision.notification.queued
    assert not decision.notification.woken
    assert "no confirmed outcome" in decision.notification.detail
    native_queue.assert_not_called()
    [message] = RepositoryPeers(consumer).waiting(MEMBER).messages
    assert message.door == Door.PAGE
    assert entry.id in message.text
    assert str(upstream) in message.text
    assert NOTE in message.text
    instruction = (
        f"lup-devtools review wait {entry.id}` carries it out"
        if approved
        else "Don't retry the call as it stands"
    )
    assert instruction in message.text

    relay = MailboxRelay(root=consumer, member_id=MEMBER, queue_timeout_seconds=0.2)
    foreign = relay.tick()
    assert foreign is not None and not foreign.reached
    assert not relay.receipt_path.exists()
    native_queue.assert_not_called()
    monkeypatch.setattr(routing, "execution_scope", lambda: "recipient-scope")
    delivered = relay.tick()

    assert delivered is not None and delivered.reached
    native_queue.assert_called_once()
    assert native_queue.call_args.args[:6] == (
        f"CODEX_HOME={home}",
        "codex",
        "queue",
        "--thread",
        THREAD,
        "--message",
    )
    assert message.text in native_queue.call_args.args[6]
    assert native_queue.call_args.kwargs == {"_cwd": str(consumer), "_timeout": 0.2}
    receipt = WakeReceipts.model_validate_json(relay.receipt_path.read_text())
    assert receipt.message_ids == [message.id]
    assert receipt.route.handle == THREAD
    assert receipt.route.home == str(home)
    assert receipt.route.scope == "recipient-scope"
    assert relay.tick() is None
    assert MailboxRelay(root=consumer, member_id=MEMBER).tick() is None
    native_queue.assert_called_once()
    assert RepositoryPeers(consumer).waiting(MEMBER).messages == [message]
    assert review.relay(upstream).find(entry.id) == settled
    assert not target.exists()
