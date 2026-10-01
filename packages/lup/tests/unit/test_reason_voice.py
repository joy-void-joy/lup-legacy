"""A reason is read by the person approving, and says nothing to the agent.

The two audiences must not share one string. What a runtime shows the approver
is `reason` and nothing else, so a sentence spending half its length telling the
agent which tool to reach for instead spends it on somebody who cannot act on it
— and the agent, on the one effect where it reads nothing at all, is handed a
prompt written for a reviewer.

`recovery` is the other half, and this is the gate that keeps it there: an
instruction to the agent belongs in that field, whatever produced it. Both
halves are checked from the shape a verdict actually carries, which is why the
kernel's own literals are read out of the source rather than triggered one rule
at a time — a rule with no fixture is exactly the one whose wording drifts.
"""

import ast
from pathlib import Path

import pytest

from lup.policy.kernel.decision import CONTAINED_ESCAPE_NOTICE, SANDBOX_ESCAPE_NOTICE

KERNEL = Path(__file__).resolve().parents[2] / "src" / "lup" / "policy"

LONGEST_REASON = 200
"""How much of a sentence an approval prompt can carry before it is skimmed.

Generous rather than tight: what this refuses is the essay, not the clause that
names a second path or a second file. A verdict with more to say has somewhere
to say it."""

# lup: ignore[library-default] — the second person, which is what an
# instruction to the agent is written in; a reason states a fact instead
ADDRESSED_TO_THE_AGENT = (
    "instead",
    "prefer ",
    "resubmit",
    "rather than editing",
    "asyouwere",
)
"""Wording that only makes sense said to whoever wrote the call.

Each of these was in a reason before the field split, and each is an
instruction: the approver cannot reshape a command, reach for another tool, or
resubmit anything. They read `recovery` nowhere and answer yes or no."""


def reason_texts(node: ast.expr) -> str | None:
    """The literal text of one reason argument, with its expansions blanked."""
    match node:
        case ast.Constant(value=str() as value):
            return value
        case ast.JoinedStr(values=values):
            return "".join(
                part.value
                if isinstance(part, ast.Constant) and isinstance(part.value, str)
                else "{}"
                for part in values
            )
        case ast.BinOp(left=left, op=ast.Add(), right=right):
            left_text = reason_texts(left)
            right_text = reason_texts(right)
            if left_text is None or right_text is None:
                return None
            return left_text + right_text
    return None


def verdict_reasons(path: Path) -> list[tuple[int, str]]:
    """Every literal reason one policy module hands a verdict, with its line."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        named = node.func.id if isinstance(node.func, ast.Name) else ""
        stated = [
            argument
            for argument in node.args[1:2]
            if named in ("KernelDecision", "Decision")
        ]
        stated += [
            keyword.value for keyword in node.keywords if keyword.arg == "reason"
        ]
        found.extend(
            (node.lineno, text)
            for argument in stated
            if (text := reason_texts(argument)) is not None
        )
    return found


def policy_sources() -> list[Path]:
    found = sorted(KERNEL.rglob("*.py"))
    assert found, "no policy modules found to read"
    return found


@pytest.mark.parametrize("path", policy_sources(), ids=lambda path: path.name)
def test_a_reason_is_one_sentence_the_approver_can_read(path: Path) -> None:
    long = [
        (line, text)
        for line, text in verdict_reasons(path)
        if len(text) > LONGEST_REASON
    ]

    assert not long, (
        f"{path.name}:{long[0][0]} states a reason of {len(long[0][1])} characters:"
        f" {long[0][1]!r}. An approval prompt shows this and nothing else, so it"
        " names the subject and the one fact that stopped it. Whatever the agent"
        " should do about it goes in `recovery`, which rides beside the question"
        " and reaches the agent on a refusal."
    )


@pytest.mark.parametrize("path", policy_sources(), ids=lambda path: path.name)
def test_a_reason_tells_the_agent_nothing(path: Path) -> None:
    addressed = [
        (line, text, word)
        for line, text in verdict_reasons(path)
        for word in ADDRESSED_TO_THE_AGENT
        if word in text.lower()
    ]

    assert not addressed, (
        f"{path.name}:{addressed[0][0]} says {addressed[0][2]!r} in a reason:"
        f" {addressed[0][1]!r}. That is addressed to whoever wrote the call, and"
        " the reason is read by whoever approves it. Move the instruction to"
        " `recovery`."
    )


@pytest.mark.parametrize(
    "notice",
    [SANDBOX_ESCAPE_NOTICE, CONTAINED_ESCAPE_NOTICE],
    ids=["host", "contained"],
)
def test_a_placement_notice_is_a_fact_the_approver_reads(notice: str) -> None:
    """The sentence an approved crossing appends is part of the reason.

    One per boundary, because the crossing differs: on a host the per-call
    sandbox is the only boundary and the call leaves for the launcher's host;
    inside a container that sandbox was never armed and the call stays in the
    mounts. Each is read by whoever approves, so each states where the call
    lands and tells the agent nothing -- what to do about a mount that refuses
    is the guidance's to say, not the prompt's. Named constants rather than
    literals in a call, so the walk above never sees them and they are put to
    the same two gates by name.
    """
    assert len(notice) <= LONGEST_REASON
    assert not [word for word in ADDRESSED_TO_THE_AGENT if word in notice.lower()]
