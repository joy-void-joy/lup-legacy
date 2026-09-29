"""Actor mail reaches a native turn before its durable receipt advances."""

import asyncio
from pathlib import Path

import pytest

from lup.coordination.mail import ActorMail
from lup.coordination.refs import ActorRef
from lup.coordination.sessions import ActorMailbox, create_mailbox_hooks
from lup.providers.codex.hooks import COMMAND_APPROVAL, CodexApprovalResponder
from lup.resolver.record import Journal
from lup.policy.hooks import LupHookInput, LupHookMatcher, LupHookOutput, LupHooksConfig


@pytest.fixture
def mailbox(tmp_path: Path) -> ActorMailbox:
    actor = ActorRef(kind="worker", id="delivery")
    return ActorMailbox(ActorMail(tmp_path), Journal(tmp_path), actor)


@pytest.mark.parametrize("redirect", [False, True])
async def test_a_native_receipt_delivers_each_kind_of_actor_mail_once(
    mailbox: ActorMailbox, redirect: bool
) -> None:
    mailbox.mail.send(mailbox.actor, "use the repaired tree", redirect=redirect)
    delivered: list[str] = []

    async def receive(text: str) -> None:
        assert len(mailbox.waiting().messages) == 1
        delivered.append(text)

    responder = CodexApprovalResponder(
        hooks=create_mailbox_hooks(mailbox), deliver_context=receive
    )
    decision = await responder.decide(COMMAND_APPROVAL, {"command": "git status"})
    assert decision == "decline"  # Mailbox delivery never grants an approval.
    assert len(delivered) == 1
    assert "use the repaired tree" in delivered[0]
    assert mailbox.waiting().messages == []
    await responder.deliver_pending()
    assert len(delivered) == 1


async def test_context_without_a_native_transport_stays_pending(
    mailbox: ActorMailbox,
) -> None:
    mailbox.mail.send(mailbox.actor, "still needed")
    responder = CodexApprovalResponder(hooks=create_mailbox_hooks(mailbox))
    decision = await responder.decide(COMMAND_APPROVAL, {"command": "git status"})
    assert decision == "decline"
    assert [message.text for message in mailbox.waiting().messages] == ["still needed"]
    assert not Journal(mailbox.mail.root).read()


async def test_a_rejected_native_delivery_leaves_no_false_receipt(
    mailbox: ActorMailbox,
) -> None:
    mailbox.mail.send(mailbox.actor, "retry this delivery")

    async def refused(_text: str) -> None:
        raise RuntimeError("turn already completed")

    responder = CodexApprovalResponder(
        hooks=create_mailbox_hooks(mailbox), deliver_context=refused
    )
    decision = await responder.decide(COMMAND_APPROVAL, {"command": "git status"})
    assert decision == "decline"
    assert len(mailbox.waiting().messages) == 1
    assert not Journal(mailbox.mail.root).read()


async def test_activity_that_needs_no_approval_still_delivers_mail(
    mailbox: ActorMailbox,
) -> None:
    mailbox.mail.send(mailbox.actor, "late review evidence")
    received: list[str] = []

    async def receive(text: str) -> None:
        received.append(text)

    responder = CodexApprovalResponder(
        hooks=create_mailbox_hooks(mailbox), deliver_context=receive
    )
    await responder.deliver_pending()
    assert len(received) == 1
    assert "late review evidence" in received[0]
    assert mailbox.waiting().messages == []


async def test_an_approval_and_activity_share_one_delivery(
    mailbox: ActorMailbox,
) -> None:
    mailbox.mail.send(mailbox.actor, "one delivery")
    entered = asyncio.Event()
    release = asyncio.Event()
    received: list[str] = []

    async def receive(text: str) -> None:
        received.append(text)
        entered.set()
        await release.wait()

    responder = CodexApprovalResponder(
        hooks=create_mailbox_hooks(mailbox), deliver_context=receive
    )
    approval = asyncio.create_task(
        responder.decide(COMMAND_APPROVAL, {"command": "git status"})
    )
    await entered.wait()
    activity = asyncio.create_task(responder.deliver_pending())
    release.set()
    await asyncio.gather(approval, activity)
    assert len(received) == 1
    assert mailbox.waiting().messages == []


async def test_canceling_delivery_keeps_mail_for_the_next_turn(
    mailbox: ActorMailbox,
) -> None:
    mailbox.mail.send(mailbox.actor, "survive cancellation")
    entered = asyncio.Event()
    suspended = asyncio.Event()

    async def receive(_text: str) -> None:
        entered.set()
        await suspended.wait()

    responder = CodexApprovalResponder(
        hooks=create_mailbox_hooks(mailbox), deliver_context=receive
    )
    pending = asyncio.create_task(responder.deliver_pending())
    await entered.wait()
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert len(mailbox.waiting().messages) == 1
    assert not Journal(mailbox.mail.root).read()


@pytest.mark.parametrize("matcher", [None, "", "*", "^item/commandExecution/"])
async def test_approval_callbacks_honor_native_method_matchers(
    matcher: str | None,
) -> None:
    calls: list[str] = []

    async def allowed(event: LupHookInput) -> LupHookOutput:
        calls.append(event.tool_name)
        return LupHookOutput(decision="allow")

    async def unrelated(_event: LupHookInput) -> LupHookOutput:
        raise AssertionError("an unrelated tool matcher must not run")

    responder = CodexApprovalResponder(
        hooks=LupHooksConfig(
            pre_tool_use=[
                LupHookMatcher(matcher=matcher, hook=allowed),
                LupHookMatcher(matcher="^different$", hook=unrelated),
            ]
        )
    )
    assert await responder.decide(COMMAND_APPROVAL, {}) == "accept"
    assert calls == [COMMAND_APPROVAL]


async def test_unmatched_approval_declines() -> None:
    async def unrelated(_event: LupHookInput) -> LupHookOutput:
        raise AssertionError("must not run")

    responder = CodexApprovalResponder(
        hooks=LupHooksConfig(
            pre_tool_use=[
                LupHookMatcher(matcher="^different$", hook=unrelated),
            ]
        )
    )
    assert await responder.decide(COMMAND_APPROVAL, {}) == "decline"


async def test_a_rewrite_never_approves_the_original_command() -> None:
    async def rewritten(_event: LupHookInput) -> LupHookOutput:
        return LupHookOutput(decision="allow", updated_input={"command": "safe"})

    received: list[str] = []

    async def receive(text: str) -> None:
        received.append(text)

    responder = CodexApprovalResponder(
        hooks=LupHooksConfig(pre_tool_use=[LupHookMatcher(hook=rewritten)]),
        deliver_context=receive,
    )
    assert await responder.decide(COMMAND_APPROVAL, {"command": "unsafe"}) == "decline"
    assert "cannot rewrite tool input" in received[0]


def test_receipt_is_private_to_adapter_transport() -> None:
    output = LupHookOutput(delivery_receipt=lambda: None)
    assert "delivery_receipt" not in output.model_dump()
    assert "delivery_receipt" not in LupHookOutput.model_json_schema()["properties"]
