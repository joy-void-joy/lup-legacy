# lup

Lup is a typed capability-composition library for Claude Code and Codex
agents: declare an agent, ask it for a Pydantic type, and get back a validated
value. Importing `lup` loads no provider SDK.

It is published as `lup-agents` and imported as `lup`:
`uv add lup-agents`, or `pip install lup-agents`.

```python
import asyncio

from pydantic import BaseModel

from lup import Claude


class Summary(BaseModel):
    title: str
    points: list[str]


async def main() -> None:
    agent = Claude(model="opus", system_prompt="Be concise.")
    result = await agent.ask("Summarize why typed tool boundaries help.", Summary)
    print(result.output.title)
    for point in result.output.points:
        print("-", point)


asyncio.run(main())
```

It needs the `claude` extra (`lup-agents[claude]`) and a logged-in Claude Code
on the machine. `Codex(model="gpt-5.5")` in place of `Claude(...)` drives the Codex
app-server instead, with the `codex` CLI on `PATH`.

## The surface

The package root is deliberately small. `Claude` and `Codex` are frozen
Pydantic models, each declaring one agent whole — model, system prompt, tools,
permissions, workspace, and the layers its sessions are wrapped in — and each
is also what opens those sessions: there is no client to build from one.
Beside them is what a launch adds to a declaration: the wall its session opens
behind, `OuterContainer`, `InnerSandbox` or `NoSandbox`, with the `Mount`s it
reaches; its `Member` identity on the coordination roster; its `Recording`;
and the session it reopens, `Latest()`, `Pick()` or `Reopen(session=...)`.
Everything else at the root is vocabulary to annotate against: `Agent`,
`Conversation`, and `Turn` for code naming neither provider, and
`TurnResult`, `TurnInput`, `TurnMessage`, `SessionId`, `SessionSummary`,
`TurnId`, and `CustomModel`. The agents and the launch fields resolve on
first access, so `import lup` pulls neither adapter nor the launch machinery,
and naming one still loads no SDK: opening a session does.

`ask` is the only verb. `await agent.ask(prompt, Model)` opens a session,
takes one turn, and closes the session however the turn ended; asked without
a model it returns `TurnResult[None]`. For a conversation, open a session and
ask it turn by turn:

```python
from pydantic import BaseModel

from lup import Agent


class Plan(BaseModel):
    steps: list[str]


async def plan(agent: Agent) -> Plan:
    async with agent.open() as session:
        await session.ask("Draft a plan for the migration.")
        turn = session.ask("Now give that plan as steps.", Plan)
        async for block in turn:  # completed blocks, in the order they finished
            if (text := block.text_payload) is not None:
                print(text)
        return (await turn).output  # the same turn's validated result
```

Written against `Agent`, that function takes a `Claude` or a `Codex` alike.

A turn starts the first time anything asks for it — awaiting it, iterating
it, or calling `events()`, `live()`, or `interrupt()` — and it starts once.
`events()` yields its durable events, `live()` adds the deltas between them,
and `interrupt()` stops it. `CodexTurn` also has `steer()`, which adds input
to the running turn; `ClaudeTurn` has no such method, because a capability a
provider lacks is absent from its type rather than present and `None`. The
provider's own session and turn classes are named from `lup.providers.claude`
and `lup.providers.codex`.

A conversation outlives the process: `session.id` resumes it through
`agent.open(resume=...)`, `agent.sessions()` lists the conversations the
provider has on record for the agent's workspace, `session.history()` reads
one back as `TurnMessage`s, and `session.fork(at=...)` branches it at a turn.

`model` takes a name from the runtime's own catalog, a portable tier
(`frontier`, `strongest`, `balanced`, `fast`), or `CustomModel(id=...)` for an
id outside the catalog; `effort` runs from `low` up to `ultra`, and an effort
the model cannot take is refused where the agent is declared.

## Typed output

A typed turn's result carries a validated instance of the model it was asked
for, or the turn raises a typed error — a missing submission is never an empty
success. On Claude the turn binds a fresh `submit_output` tool to the schema
before the prompt is accepted; on Codex the schema rides the turn's own strict
`outputSchema`. Either way the same Pydantic validation and optional
submission gate run before the result is returned.

## Beyond the front door

Other modules provide independently composable runtime decorators, semantic
policy, deterministic harness generation/reconciliation, the persisted
resolver, MCP helpers, workspace/history support, scheduling, telemetry, and
sandboxing. See the repository's
[library guide](../../docs/library.md)
and
[architecture guide](../../docs/architecture.md).
Runnable compositions — layers, background agents, profiles, compatible
endpoints, routes, and policy — live in the repository
[examples](../../examples).
