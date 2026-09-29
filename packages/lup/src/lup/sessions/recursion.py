"""Framework-owned recursive-agent allowance and its process relay."""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings

from lup.types import EnvVars

# lup: ignore[constant-declaration] — launchers and sessions must share this name
MAX_RECURSIVE_AGENT_ENV = "LUP_MAX_RECURSIVE_AGENT"
"""Remaining child-agent depth, or ``-1`` for no limit."""


class RecursiveAgentSettings(BaseSettings, extra="ignore"):
    """The allowance inherited when no opened session supplies one."""

    remaining: int = Field(
        default=-1,
        ge=-1,
        validation_alias=MAX_RECURSIVE_AGENT_ENV,
    )


class RecursiveAgentAllowance(BaseModel, frozen=True):
    """How many more Lup-created agent levels this context may open."""

    remaining: int = Field(ge=-1)

    def child(self) -> "RecursiveAgentAllowance":
        """Consume one level for a child, refusing when none remain."""
        if self.remaining == 0:
            raise RecursiveAgentLimitError(
                f"recursive agent creation is disabled by {MAX_RECURSIVE_AGENT_ENV}=0"
            )
        return RecursiveAgentAllowance(
            remaining=-1 if self.remaining == -1 else self.remaining - 1
        )

    def environment(self, base: EnvVars | None = None) -> EnvVars:
        """Carry this allowance across a process boundary."""
        return {**(base or {}), MAX_RECURSIVE_AGENT_ENV: str(self.remaining)}


class RecursiveAgentLimitError(RuntimeError):
    """A Lup session was opened after its recursive allowance was spent."""


ACTIVE_RECURSIVE_AGENT_ALLOWANCE: ContextVar[RecursiveAgentAllowance | None] = (
    ContextVar("lup_recursive_agent_allowance", default=None)
)


def recursive_agent_allowance(
    environment: EnvVars | None = None,
) -> RecursiveAgentAllowance:
    """Read the active session allowance, then an explicit or process relay."""
    active = ACTIVE_RECURSIVE_AGENT_ALLOWANCE.get()
    if active is not None:
        return active
    if environment is not None and MAX_RECURSIVE_AGENT_ENV in environment:
        settings = RecursiveAgentSettings.model_validate(environment)
    else:
        settings = RecursiveAgentSettings()
    return RecursiveAgentAllowance(remaining=settings.remaining)


def child_recursive_agent_allowance(
    environment: EnvVars | None = None,
) -> RecursiveAgentAllowance:
    """The decremented allowance a newly opened Lup session receives."""
    return recursive_agent_allowance(environment).child()


@contextmanager
def recursive_agent_scope(
    allowance: RecursiveAgentAllowance,
) -> Iterator[None]:
    """Make one opened session's remaining allowance ambient to its tools."""
    token = ACTIVE_RECURSIVE_AGENT_ALLOWANCE.set(allowance)
    try:
        yield
    finally:
        ACTIVE_RECURSIVE_AGENT_ALLOWANCE.reset(token)
