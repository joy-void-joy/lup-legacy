"""Versioned edit evaluator transport; carries every semantic decision field."""

from typing import TypedDict, TypeGuard

from .decision import (
    KernelDecision,
    DecisionEffect,
    SandboxPlacement,
    CheckpointRequirement,
)
from .semantics import (
    ReviewerRequirement,
    ReviewPurpose,
    Visibility,
    RefusalCause,
    Capability,
    AbstentionPurpose,
    Reach,
)


type WireValue = (
    str | int | float | bool | None | list[WireValue] | dict[str, WireValue]
)


class DecisionWire(TypedDict):
    """A complete decision, including all contributing findings."""

    effect: DecisionEffect
    reason: str
    sandbox: SandboxPlacement
    escalated: str
    checkpoint: CheckpointRequirement
    unlisted: bool
    reviewer: ReviewerRequirement
    purpose: ReviewPurpose | None
    visibility: Visibility
    cause: RefusalCause | None
    capability: Capability | None
    abstention: AbstentionPurpose | None
    hard: bool
    findings: list["DecisionWire"]
    rule: str
    evaluator: str
    recovery: str
    reach: Reach | None
    unread: bool


class EditRequest(TypedDict):
    """Normalized edit facts; caller and owner are deliberately independent."""

    protocol: int
    path: str
    before: str | None
    after: str | None
    path_exists: bool
    autonomous: bool
    agent_identity: str
    operation: str
    cwd: str
    owner: str


def decision_wire(decision: KernelDecision) -> DecisionWire:
    """Carry semantic verdicts; caller-bound display attribution stays local.

    The origin binds file evidence after receiving this owner's verdict. An
    evaluator cannot substitute the caller's path or image digests through
    advisory metadata, and the transport does not change for that metadata.
    """
    # lup: defer: the protected-path rule an owner's verdict met is not
    # carried, so a file another repository's policy judged names no rule on
    # the origin's row. Both sides read this shape exactly, so carrying it is
    # a protocol version an older origin or owner can still decline.
    return DecisionWire(
        effect=decision.effect,
        reason=decision.reason,
        sandbox=decision.sandbox,
        escalated=decision.escalated,
        checkpoint=decision.checkpoint,
        unlisted=decision.unlisted,
        reviewer=decision.reviewer,
        purpose=decision.purpose,
        visibility=decision.visibility,
        cause=decision.cause,
        capability=decision.capability,
        abstention=decision.abstention,
        hard=decision.hard,
        findings=[decision_wire(part) for part in decision.findings],
        rule=decision.rule,
        evaluator=decision.evaluator,
        recovery=decision.recovery,
        reach=decision.reach,
        unread=decision.unread,
    )


def valid_decision(value: WireValue | DecisionWire) -> TypeGuard[DecisionWire]:
    """Reject incomplete or malformed evaluator responses before using them."""
    if not isinstance(value, dict) or sorted(value) != sorted(
        DecisionWire.__annotations__
    ):
        raise ValueError("destination evaluator returned an incompatible decision")
    for name in ("reason", "escalated", "rule", "evaluator", "recovery"):
        if not isinstance(value[name], str):
            raise ValueError(f"destination decision {name} must be text")
    for name in ("unlisted", "hard", "unread"):
        if not isinstance(value[name], bool):
            raise ValueError(f"destination decision {name} must be boolean")
    for name, choices in (
        ("effect", ("allow", "ask", "deny", "defer")),
        ("sandbox", ("inside", "ambient", "outside")),
        ("checkpoint", ("targeted", "boundary_wide", "unrecoverable")),
        ("reviewer", ("human_only", "supervisor_allowed")),
        (
            "purpose",
            (
                None,
                "quality_review",
                "sensitive_access",
                "external_consequence",
                "policy_override",
                "unrecovered_local_mutation",
                "untrusted_dependency",
            ),
        ),
        ("visibility", ("quiet", "notice")),
        (
            "cause",
            (
                None,
                "deliberate",
                "reviewability",
                "unreadable",
                "unlisted",
                "capability",
            ),
        ),
        (
            "capability",
            (
                None,
                "host_executor",
                "checkpoint_store",
                "question_relay",
                "inside_placement",
            ),
        ),
        ("abstention", (None, "provider_native", "boundary_settle")),
        (
            "reach",
            (
                None,
                "container",
                "mount",
                "dependency",
                "credential",
                "lup",
                "host_later",
            ),
        ),
    ):
        if value[name] not in choices:
            raise ValueError(f"destination decision has invalid {name}")
    if not isinstance(value["findings"], list):
        raise ValueError("destination decision findings must be a list")
    return all(valid_decision(part) for part in value["findings"])


def read_decision(value: WireValue | DecisionWire) -> KernelDecision:
    """Restore a validated complete decision."""
    if not valid_decision(value):
        raise ValueError("malformed destination decision")
    row = value
    findings = tuple(read_decision(part) for part in row["findings"])
    return KernelDecision(
        effect=row["effect"],
        reason=row["reason"],
        sandbox=row["sandbox"],
        escalated=row["escalated"],
        checkpoint=row["checkpoint"],
        unlisted=row["unlisted"],
        reviewer=row["reviewer"],
        purpose=row["purpose"],
        visibility=row["visibility"],
        cause=row["cause"],
        capability=row["capability"],
        abstention=row["abstention"],
        hard=row["hard"],
        findings=findings,
        rule=row["rule"],
        evaluator=row["evaluator"],
        recovery=row["recovery"],
        reach=row["reach"],
        unread=row["unread"],
    )


def routing_failure(reason: str) -> KernelDecision:
    """An unavailable owner is a refusal, never an origin-policy fallback."""
    return KernelDecision(
        "deny",
        f"Destination policy unavailable: {reason}",
        hard=True,
        rule="edit:destination-policy",
        recovery="Regenerate the destination harness and ask the operator to refresh its accepted policy snapshot.",
    )


def read_response(value: WireValue) -> KernelDecision:
    """Read the versioned semantic reply from an accepted evaluator."""
    if not isinstance(value, dict) or sorted(value) != ["decision", "protocol"]:
        raise ValueError("malformed destination evaluator response")
    if type(value["protocol"]) is not int or value["protocol"] != 1:
        raise ValueError("unsupported destination evaluator response")
    return read_decision(value["decision"])


def valid_edit_request(value: WireValue) -> TypeGuard[EditRequest]:
    """Validate the complete protocol before any destination code judges it."""
    if not isinstance(value, dict) or sorted(value) != sorted(
        EditRequest.__annotations__
    ):
        raise ValueError("incompatible destination edit request")
    if type(value["protocol"]) is not int or value["protocol"] != 1:
        raise ValueError("unsupported destination evaluator protocol")
    for name in ("path", "operation", "cwd", "owner", "agent_identity"):
        if not isinstance(value[name], str):
            raise ValueError(f"destination edit {name} must be text")
    for name in ("before", "after"):
        if value[name] is not None and not isinstance(value[name], str):
            raise ValueError(f"destination edit {name} must be text or null")
    for name in ("path_exists", "autonomous"):
        if not isinstance(value[name], bool):
            raise ValueError(f"destination edit {name} must be boolean")
    if value["operation"] not in ("modify", "overwrite", "create", "delete"):
        raise ValueError("unsupported destination edit operation")
    return True


def read_edit_request(value: WireValue) -> EditRequest:
    """Restore normalized edit facts after validating their complete shape."""
    if not valid_edit_request(value):
        raise ValueError("malformed destination edit request")
    return value
