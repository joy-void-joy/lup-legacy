"""Reflection gate abstraction for enforcing reflect-before-output patterns.

Reflect-before-output gates: a flag-based `ReflectionGate` and a verdict-aware
`ReviewGate`.

Agents benefit from structured self-assessment before committing to output.
This module provides the domain-neutral gate mechanism:

- ``ReflectionGate``: Flag-based state tracker for whether the agent
  has reflected in the current cycle.
- ``ReviewVerdict`` / ``ReviewResult``: Structured outcome vocabulary
  for a reviewer (approve / warn / fail) however it is wired — the
  template runs it as a nested agent inside the reflection tool.
- ``ReviewGate``: Verdict-aware ``ReflectionGate`` — the flag opens on
  reviewer approval instead of on the mere act of reflecting, with a
  consecutive-fail escape hatch so a harsh reviewer cannot deadlock.

The reflection *tool* and its input model are domain-specific and belong
in ``agent/tools/``. This module only provides the enforcement mechanism.

Hook factories that enforce the gate live in :mod:`lup.policy.hooks` (SDK-agnostic).
Each adapter converts these to its native hook format.

One-shot agents: gate ``StructuredOutput`` on reflection.
Persistent agents: gate ``sleep`` on reflection (via ``Scheduler.meta_gate``).

Examples:
    Gate ``StructuredOutput`` until the agent has reflected::

        >>> from lup.orchestration.reflection import ReflectionGate
        >>> from lup.policy.hooks import create_reflection_gate, merge_hooks
        >>> gate = ReflectionGate()
        >>> gate_hooks = create_reflection_gate(
        ...     gate=gate,
        ...     gated_tool="StructuredOutput",
        ...     reflection_tool_name="mcp__notes__review",
        ... )
        >>> hooks = merge_hooks(permission_hooks, gate_hooks)

    In the reflection tool handler, mark as reflected::

        >>> gate.mark_reflected()
        >>> gate.reflected
        True

    For persistent agents, reset the gate each cycle::

        >>> gate.reset()
        >>> gate.reflected
        False
"""

from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, Field

from lup.policy.hooks import LupHooksConfig, create_tool_gate


class ReflectionGate:
    """Tracks whether the agent has reflected in the current cycle.

    Used by :func:`~lup.orchestration.reflection.create_reflection_gate` to enforce
    "reflect before X" patterns. The reflection tool handler calls :meth:`mark_reflected`
    after saving reflection data. The orchestration layer calls
    :meth:`reset` when a new cycle begins (e.g., after each agent action
    in persistent mode).

    Supports two modes, so enforcement does not depend on any one
    backend's hook support:
    - In-memory (default): when the gate and the enforcement point share
      a process (in-process hooks read ``reflected`` directly).
    - File-backed: when they do not — the flag is a file, marked and
      read across the process boundary. This is what backends without
      firing hooks rely on: the enforcement point checks the flag
      in-tool (inside submit_output) rather than in a hook.
    """

    def __init__(self, flag_path: Path | None = None) -> None:
        self.memory_flag: bool = False
        self.flag_path = flag_path

    @property
    def reflected(self) -> bool:
        if self.flag_path is not None:
            return self.flag_path.exists()
        return self.memory_flag

    @reflected.setter
    def reflected(self, value: bool) -> None:
        self.memory_flag = value
        if self.flag_path is not None:
            if value:
                self.flag_path.parent.mkdir(parents=True, exist_ok=True)
                self.flag_path.touch()
            else:
                self.flag_path.unlink(missing_ok=True)

    def mark_reflected(self) -> None:
        """Record that reflection has occurred."""
        self.reflected = True

    def reset(self) -> None:
        """Require fresh reflection (start of new cycle)."""
        self.reflected = False


class ReviewVerdict(StrEnum):
    """Outcome of a reviewer's evaluation."""

    approve = "approve"
    warn = "warn"
    fail = "fail"


class ReviewResult(BaseModel):
    """Structured output from a reviewer."""

    verdict: ReviewVerdict = Field(
        description=(
            "approve: no blocking errors. warn: real but non-blocking "
            "issues — the conclusion stands. fail: a concrete error that "
            "would change the output; the agent must revise."
        ),
    )
    assessment: str = Field(
        description=(
            "Full analysis. Explain what you checked, what you found, "
            "and why you reached your verdict."
        ),
    )


class ReviewGate(ReflectionGate):
    """Verdict-aware :class:`ReflectionGate`: opens on reviewer approval.

    :meth:`record` maps a reviewer's :class:`ReviewResult` onto the flag
    every existing consumer already checks (``reflected``): approve and
    warn open the gate immediately; fail keeps it closed — and re-closes
    it — so the agent must revise and review again. After ``max_fails``
    consecutive fails the gate opens anyway, an escape hatch so a harsh
    reviewer cannot deadlock the session.

    File-backed mode persists the fail counter beside the flag file, so
    enforcement keeps working across the process boundary (the
    tool-serving subprocess) exactly like the base flag does.
    """

    def __init__(self, flag_path: Path | None = None, *, max_fails: int = 3) -> None:
        super().__init__(flag_path)
        self.max_fails = max_fails
        self.memory_fails: int = 0
        self.last_verdict: ReviewVerdict | None = None

    @property
    def fails_path(self) -> Path | None:
        if self.flag_path is None:
            return None
        return self.flag_path.with_name(self.flag_path.name + ".fails")

    @property
    def consecutive_fails(self) -> int:
        if self.fails_path is not None:
            if not self.fails_path.exists():
                return 0
            return int(self.fails_path.read_text(encoding="utf-8"))
        return self.memory_fails

    @consecutive_fails.setter
    def consecutive_fails(self, value: int) -> None:
        self.memory_fails = value
        if self.fails_path is not None:
            if value:
                self.fails_path.parent.mkdir(parents=True, exist_ok=True)
                self.fails_path.write_text(str(value), encoding="utf-8")
            else:
                self.fails_path.unlink(missing_ok=True)

    def record(self, result: ReviewResult) -> None:
        """Record a reviewer verdict and update the gate flag."""
        self.last_verdict = result.verdict
        if result.verdict is ReviewVerdict.fail:
            self.consecutive_fails += 1
            self.reflected = self.consecutive_fails >= self.max_fails
        else:
            self.consecutive_fails = 0
            self.reflected = True

    def reset(self) -> None:
        """Require a fresh review (start of new cycle)."""
        super().reset()
        self.consecutive_fails = 0
        self.last_verdict = None


def create_reflection_gate(
    *,
    gate: ReflectionGate,
    gated_tool: str,
    reflection_tool_name: str = "reflection",
    denial_message: str | None = None,
) -> LupHooksConfig:
    """Create a PreToolUse hook that denies *gated_tool* until reflection.

    **What:** Preset over :func:`lup.policy.hooks.create_tool_gate` — denies
    *gated_tool* with a message naming *reflection_tool_name* while
    ``gate.reflected`` is False, and explicitly allows it afterwards.

    **When:** Wire this for any "reflect before X" pattern: gate
    ``StructuredOutput`` for one-shot agents or ``sleep`` for persistent
    agents. The reflection tool handler calls
    :meth:`ReflectionGate.mark_reflected`; persistent agents reset the
    gate per cycle via :meth:`ReflectionGate.reset`.

    **Why:** The external :class:`ReflectionGate` object (rather than
    the gate primitive's one-shot ``on_unlock_tool`` tracking) supports
    resettable, per-cycle reflection.

    .. note::

        If you also need to rewrite the gated tool's input (e.g., unwrap
        a ``{"parameter": {...}}`` wrapper), combine both checks in a
        single hook to avoid the CLI bug where multiple PreToolUse hooks
        overwrite each other's ``updatedInput`` (the SDK's issue 15897).
        Register this gate as the **last** PreToolUse hook.

    Args:
        gate: The :class:`ReflectionGate` instance tracking status.
        gated_tool: Tool name to block (e.g., ``"StructuredOutput"``).
        reflection_tool_name: Name shown in the denial message.
        denial_message: Custom denial text. Uses a sensible default
            if ``None``.

    Returns:
        SDK-agnostic hooks configuration with a PreToolUse hook.
    """
    default_message = (
        f"You must call {reflection_tool_name}() with your assessment "
        f"BEFORE calling {gated_tool}. Reflect on your work first, "
        f"then try again."
    )

    return create_tool_gate(
        gated_tool=gated_tool,
        message=denial_message or default_message,
        unlocked=lambda _input: gate.reflected,
        allow_when_unlocked=True,
        tag="reflection_gate",
    )
