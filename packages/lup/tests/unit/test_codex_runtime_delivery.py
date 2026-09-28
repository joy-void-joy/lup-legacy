"""Native app-server activity and approvals transport actor context safely."""

import asyncio
from pathlib import Path

import pytest

from lup.coordination.mail import ActorMail
from lup.coordination.refs import ActorRef
from lup.coordination.sessions import ActorMailbox, create_mailbox_hooks
from lup.policy.hooks import LupHookInput, LupHookMatcher, LupHookOutput, LupHooksConfig
from lup.providers.codex.app_server import CodexAppServer, RpcMessage, RpcNotification
from lup.providers.codex.hooks import COMMAND_APPROVAL
from lup.providers.codex import Codex
from lup.providers.codex.runtime import CodexConversationState, CodexTurnChannel
from lup.resolver.record import Journal
from lup.types import JsonObject, JsonValue


class RecordingServer(CodexAppServer):
    def __init__(self) -> None:
        super().__init__(Path("codex"))
        self.calls: list[tuple[str, JsonObject]] = []
        self.reject = False

    async def request(self, method: str, params: JsonObject) -> JsonValue:
        self.calls.append((method, params))
        if self.reject:
            raise RuntimeError("native turn already ended")
        return {}


def delivery_state(
    tmp_path: Path,
) -> tuple[CodexConversationState, RecordingServer, ActorMailbox]:
    mailbox = ActorMailbox(
        ActorMail(tmp_path), Journal(tmp_path), ActorRef(kind="worker", id="delivery")
    )
    server = RecordingServer()
    config = Codex(cwd=tmp_path, hooks=create_mailbox_hooks(mailbox))
    state = CodexConversationState(config, server, None)
    state.thread_id = "thread"
    state.channel = CodexTurnChannel("thread")
    state.channel.turn_id = "turn"
    return state, server, mailbox


@pytest.mark.parametrize("reject", [False, True])
async def test_runtime_receipt_follows_native_steering_acceptance(
    tmp_path: Path, reject: bool
) -> None:
    state, server, mailbox = delivery_state(tmp_path)
    server.reject = reject
    mailbox.mail.send(mailbox.actor, "review evidence")
    response = await state.resolve_approval(
        RpcMessage(
            id=1,
            method=COMMAND_APPROVAL,
            params={"threadId": "thread", "turnId": "turn", "command": "git status"},
        )
    )
    assert response == {"decision": "decline"}
    assert len(mailbox.waiting().messages) == int(reject)
    assert server.calls[0][0] == "turn/steer"
    assert server.calls[0][1]["threadId"] == "thread"


async def test_completed_activity_schedules_owned_delivery_without_approval(
    tmp_path: Path,
) -> None:
    state, server, mailbox = delivery_state(tmp_path)
    mailbox.mail.send(mailbox.actor, "late mail")
    state.handle_notification(
        RpcNotification(
            method="item/completed",
            params={
                "threadId": "thread",
                "turnId": "turn",
                "item": {"id": "item", "type": "agentMessage", "text": "working"},
            },
        )
    )
    await asyncio.gather(*server.handlers)
    assert mailbox.waiting().messages == []
    assert len(server.calls) == 1


async def test_stale_approval_and_notification_cannot_consume_current_mail(
    tmp_path: Path,
) -> None:
    state, server, mailbox = delivery_state(tmp_path)
    mailbox.mail.send(mailbox.actor, "belongs to current turn")
    for thread, turn in [("other", "turn"), ("thread", "stale")]:
        response = await state.resolve_approval(
            RpcMessage(
                id=1,
                method=COMMAND_APPROVAL,
                params={"threadId": thread, "turnId": turn, "command": "git status"},
            )
        )
        assert response == {"decision": "decline"}
    state.handle_notification(
        RpcNotification(
            method="item/completed",
            params={"turnId": "stale", "item": {}},
        )
    )
    assert not server.handlers
    assert not server.calls
    assert len(mailbox.waiting().messages) == 1


async def test_queued_delivery_cannot_jump_to_a_replacement_turn(
    tmp_path: Path,
) -> None:
    state, server, mailbox = delivery_state(tmp_path)
    mailbox.mail.send(mailbox.actor, "retain through turn replacement")
    state.handle_notification(
        RpcNotification(
            method="item/completed",
            params={"turnId": "turn", "item": {"type": "agentMessage", "text": "work"}},
        )
    )
    old = state.channel
    assert old is not None
    current = CodexTurnChannel("thread")
    current.turn_id = "replacement"
    state.channel = current
    await asyncio.gather(*server.handlers)
    assert not server.calls
    assert len(mailbox.waiting().messages) == 1


@pytest.mark.parametrize("event", ["post_tool_use", "stop"])
def test_lifecycle_hooks_are_accepted(tmp_path: Path, event: str) -> None:
    async def callback(_event: LupHookInput) -> LupHookOutput:
        return LupHookOutput(decision="allow")

    hooks = LupHooksConfig.model_validate({event: [LupHookMatcher(hook=callback)]})
    assert Codex(cwd=tmp_path, hooks=hooks).hooks is not None
