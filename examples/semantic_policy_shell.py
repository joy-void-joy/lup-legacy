"""Refuse a denied command inside a live session — the tool-call path.

The tool-call half of the policy pair; ``semantic_policy`` is the fetch
half. The shell policy consults the same declared origin table, so the URL
refused to a fetch is refused to ``curl`` as well and no command reaches a
shell by taking the other route.

The session runs with permissions bypassed, which is the mode where this
hook is the only gate the call meets: what the policy denies is refused,
and nothing else is left to grant it.
"""

import asyncio

from pydantic import AnyHttpUrl, BaseModel, Field

from lup import Claude, InnerSandbox
from lup.harness.models import HookSet
from lup.providers.claude.hooks import CLAUDE_SEMANTICS
from lup.providers.claude import ClaudeTools
from lup.policy.hooks import LupHooksConfig
from lup.policy.enforcement import SemanticToolPolicy, create_policy_hooks
from lup.policy.models import UrlScope
from lup.policy.rules import ShellPolicy

# lup: ignore[constant-declaration] — the one origin this example allows, which
# is the example's subject rather than a value to pass in
DOCS_ORIGIN = AnyHttpUrl("https://docs.example.com")
# lup: ignore[constant-declaration] — the one command this example demonstrates a
# denial on, which is the example's subject rather than a value to pass in
DENIED_COMMAND = "curl https://docs.example.com/private/token"

SANDBOX = InnerSandbox(escapable=True)
"""The escape this session permits, said once to the policy and the runtime.

A rule may place a call outside the sandbox, but only a session that opened
the channel can carry it there. One object answers both so the two cannot
disagree — unstated, a placement is rendered and silently dropped.
"""


class Summary(BaseModel, frozen=True):
    """A minimal structured result submitted by this example's agent."""

    summary: str = Field(min_length=1)


def declared_hook_set() -> HookSet:
    """This example's policy declaration, taking lup's shell vocabulary as shipped.

    An empty ``shell_rules`` selection is the library's default vocabulary
    unchanged. A project states only where it judges differently — a command
    it adds, one it retires — and the declaration layers that over the
    defaults, the same resolution its generated dispatchers read.
    """
    return HookSet(id="semantic-policy-shell", policy_ids=["shell"])


def policy_hooks() -> LupHooksConfig:
    """Enforce the shell lattice, scoped by the same declared origins.

    The vocabulary is the declaration's: read-only commands allow,
    destructive ones ask, and anything the lattice cannot judge denies with
    the recipe for reshaping or escalating it.
    """
    policy = ShellPolicy(
        declared_hook_set().resolved_shell_rules(),
        allowed_urls=[UrlScope(origin=DOCS_ORIGIN, path_prefix="/api")],
        denied_urls=[UrlScope(origin=DOCS_ORIGIN, path_prefix="/private")],
    )
    return create_policy_hooks(
        SemanticToolPolicy(shell=policy),
        CLAUDE_SEMANTICS,
        sandbox=SANDBOX.enforcement(),
    )


def session_config() -> Claude:
    """Carry the enforcing hooks into every session this agent opens."""
    return Claude(
        model="claude-opus-5",
        tools=ClaudeTools(builtin=["Bash"]),
        system_prompt="Run what you are asked to run and report what happened.",
        hooks=policy_hooks(),
        sandbox=SANDBOX,
    )


async def main() -> None:
    result = await session_config().ask(
        f"Run `{DENIED_COMMAND}` and summarize the output.",
        Summary,
    )
    print(result.output.summary)


if __name__ == "__main__":
    asyncio.run(main())
