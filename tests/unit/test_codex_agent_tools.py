"""Codex's agent tools: which a session is given, and whether its spawns pass the policy.

Codex has two spellings of a spawn. `multi_agent_v2`'s `collaboration`
namespace offers one taking a `task_name`, which a hook names
`collaborationspawn_agent`; `multi_agent_v1`'s offers one taking no name at
all, which a hook names bare, `spawn_agent`. Which one a session gets is the
model catalog's `multi_agent_version` and the two features together, measured
on 0.159.2 against a local Responses fixture: `gpt-5.6-sol` (`v2` in its row)
was offered v2's with both features off, and only a model the catalog does not
know, with `multi_agent` on and `multi_agent_v2` off, was offered v1's.

Both are routed through the generated dispatcher, run here as Codex runs it
over a scripted local model. A session composed in process judges a spawn
only where it installs that dispatcher, so where its declared policy refuses
spawning it is given no agent tools at all, measured here at the wire.
"""

import asyncio
import json
import os
import shutil
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock, Thread

import pytest
from pydantic import BaseModel

from lup.harness.models import HookSet
from lup.policy.refused_tools import RefusedTool
from lup.providers.codex import Codex, CodexTools
from lup.providers.codex.app_server import CodexAppServer
from lup.providers.codex.home import seed_hook_trust
from lup.providers.codex.trust import read_hooks
from lup.types import JsonObject, JsonValue
from lup_template.harness.catalog import declared_hook_set
from tests.unit.test_spawn_names import CODEX_DISPATCHER, SPAWN_REFUSALS, refusing

LIVE = pytest.mark.skipif(
    shutil.which("codex") is None, reason="native Codex is not installed"
)
ROOT_MARK = "AGENT-TOOLS-ROOT"


def declared_refusing(tool: str, specifier: str = "") -> HookSet:
    """This project's hook set, refusing one spelling of a spawn."""
    declared = declared_hook_set()
    refusal = RefusedTool(
        tool=tool,
        specifier=specifier,
        reason="this project runs no subagents",
        recovery="Do the work in this conversation.",
    )
    return declared.model_copy(
        update={"refused_tools": [*declared.refused_tools, refusal]}
    )


def test_a_stock_session_is_given_the_agent_tools() -> None:
    granted = Codex(
        cwd=Path("."), tools=CodexTools(builtin="stock"), policy=declared_hook_set()
    ).builtins()

    features = granted.configuration()["features"]
    assert isinstance(features, dict)
    assert granted.agents
    assert features["multi_agent"] is True
    assert features["multi_agent_v2"] is True


@pytest.mark.parametrize("tool", ["collaborationspawn_agent", "spawn_agent"])
def test_a_refused_spawn_takes_the_agent_tools_away_and_nothing_else(tool: str) -> None:
    """Either spelling, because which one a model is offered is its catalog row's."""
    granted = Codex(
        cwd=Path("."),
        tools=CodexTools(builtin="stock"),
        policy=declared_refusing(tool),
    ).builtins()

    features = granted.configuration()["features"]
    assert isinstance(features, dict)
    assert not granted.agents
    assert features["multi_agent"] is False
    assert features["multi_agent_v2"] is False
    assert granted.shell and granted.write and granted.web and granted.all_tools


def test_refusing_one_kind_of_spawn_keeps_the_agent_tools() -> None:
    """A refusal by subject still lets the other spawns through, so the tools stay."""
    granted = Codex(
        cwd=Path("."),
        tools=CodexTools(builtin="stock"),
        policy=declared_refusing("collaborationspawn_agent", "reviewer"),
    ).builtins()

    assert granted.agents


class OfferedTool(BaseModel, extra="ignore"):
    """One tool a Responses request offers, a hosted one carrying a type alone."""

    type: str
    name: str | None = None


class InputItem(BaseModel, extra="ignore"):
    """One item of a request's input, as far as these tests read it."""

    type: str = ""
    tools: list[OfferedTool] = []
    output: JsonValue = None


class ResponsesRequest(BaseModel, extra="ignore"):
    """What Codex sends the model, as far as these tests read it."""

    tools: list[OfferedTool] = []
    input: list[InputItem] = []

    def offered(self) -> list[str]:
        """Every top-level tool offered, by name or, for a hosted one, type.

        A model in code mode is handed its tools as an ``additional_tools``
        input item rather than as ``tools``, measured on 0.159.2, so both are
        read.
        """
        added = [
            tool
            for item in self.input
            if item.type == "additional_tools"
            for tool in item.tools
        ]
        return [tool.name or tool.type for tool in [*self.tools, *added]]


@LIVE
@pytest.mark.parametrize(
    ("policy", "agents"),
    [
        pytest.param(declared_hook_set(), True, id="allowed"),
        pytest.param(
            declared_refusing("collaborationspawn_agent"), False, id="refused"
        ),
    ],
)
async def test_the_agent_tools_reach_the_model_only_where_spawning_is_allowed(
    tmp_path: Path, policy: HookSet, agents: bool
) -> None:
    """Measured at the wire, for a model whose catalog row names `v2`.

    The features alone do not take the tools away from such a model, so the
    catalog the session hands Codex has to say so as well; this is the whole
    of what an in-process session sends, read back out of the request.
    """
    model = "gpt-5.6-sol"
    loop = asyncio.get_running_loop()
    captured: asyncio.Future[ResponsesRequest] = loop.create_future()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            body = ResponsesRequest.model_validate_json(
                self.rfile.read(int(self.headers["Content-Length"]))
            )
            if not captured.done():
                loop.call_soon_threadsafe(captured.set_result, body)
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(
                b'{"error":{"message":"captured","type":"invalid_request_error"}}'
            )

        def log_message(self, format: str, *args: object) -> None:
            return None

    endpoint = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    Thread(target=endpoint.serve_forever, daemon=True).start()
    (tmp_path / "config.toml").write_text(
        f'model = "{model}"\nmodel_provider = "local_fixture"\n'
        '[model_providers.local_fixture]\nname = "Local fixture"\n'
        f'base_url = "http://127.0.0.1:{endpoint.server_port}/v1"\n'
        'wire_api = "responses"\nrequires_openai_auth = false\n'
    )
    executable = shutil.which("codex")
    assert executable is not None
    environment = {
        "CODEX_HOME": str(tmp_path),
        "OPENAI_API_KEY": "",
        "CODEX_API_KEY": "",
    }
    granted = Codex(
        cwd=tmp_path, tools=CodexTools(builtin="stock"), policy=policy
    ).builtins()
    catalog = tmp_path / "models.json"
    catalog.write_text(
        json.dumps(
            granted.model_catalog(
                Path(executable), {**os.environ, **environment}, model
            )
        )
    )
    server = CodexAppServer(
        Path(executable),
        arguments=[
            *granted.arguments(),
            "--config",
            f"model_catalog_json={json.dumps(str(catalog))}",
        ],
        environment=environment,
    )
    try:
        async with asyncio.timeout(60):
            await server.start()
            started = await server.request(
                "thread/start",
                {
                    "cwd": str(tmp_path),
                    "model": model,
                    "approvalPolicy": "never",
                    "sandbox": "read-only",
                    "config": granted.configuration(),
                },
            )
            assert isinstance(started, dict) and isinstance(started["thread"], dict)
            await server.request(
                "turn/start",
                {
                    "threadId": started["thread"]["id"],
                    "input": [{"type": "text", "text": "Say ok."}],
                },
            )
            request = await captured
    finally:
        await server.close()
        await asyncio.to_thread(endpoint.shutdown)

    assert ("collaboration" in request.offered()) is agents
    assert "multi_agent_v1" not in request.offered()


def completed(response: str) -> JsonObject:
    return {
        "type": "response.completed",
        "response": {
            "id": response,
            "usage": {
                "input_tokens": 1,
                "input_tokens_details": None,
                "output_tokens": 1,
                "output_tokens_details": None,
                "total_tokens": 2,
            },
        },
    }


def streamed(response: str, item: JsonObject) -> bytes:
    """One Responses stream carrying a single output item."""
    events: list[JsonObject] = [
        {"type": "response.created", "response": {"id": response}},
        {"type": "response.output_item.done", "item": item},
        completed(response),
    ]
    return b"".join(
        f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode()
        for event in events
    )


class Scripted:
    """A local model that spawns once from the root conversation, then stops.

    Every request is kept; the root conversation is the one whose input
    carries the probe's own prompt, and a subagent's is answered at once.
    """

    def __init__(self, spawn: JsonObject) -> None:
        self.spawn = spawn
        self.requests: list[ResponsesRequest] = []
        self.spawned = False
        self.subagents: list[str] = []
        self.lock = Lock()

    def answer(self, body: bytes) -> bytes:
        """The stream one request is answered with, the request kept."""
        with self.lock:
            self.requests.append(ResponsesRequest.model_validate_json(body))
            response = f"r{len(self.requests)}"
            if ROOT_MARK.encode() in body and not self.spawned:
                self.spawned = True
                return streamed(
                    response,
                    {
                        "type": "function_call",
                        "id": f"fc_{response}",
                        "call_id": f"call_{response}",
                        **self.spawn,
                    },
                )
        return streamed(
            response,
            {
                "type": "message",
                "role": "assistant",
                "id": f"msg_{response}",
                "content": [{"type": "output_text", "text": "ok"}],
            },
        )

    def outputs(self) -> list[str]:
        """What every function call the root made came back carrying."""
        return [
            str(item.output)
            for request in self.requests
            for item in request.input
            if item.type == "function_call_output"
        ]


def judged_spawn(
    tmp_path: Path, model: str, features: list[str], spawn: JsonObject, dispatcher: Path
) -> tuple[list[JsonObject], Scripted]:
    """Run `codex exec` once over a scripted spawn, judged by *dispatcher*.

    The hook is registered the way the generated plugin registers it — its
    own matcher — and runs the dispatcher through a wrapper that keeps each
    payload, so what reached the policy is read back rather than inferred.
    Returns every hook record and the scripted model.
    """
    script = Scripted(spawn)

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            answer = script.answer(self.rfile.read(int(self.headers["Content-Length"])))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(answer)

        def log_message(self, format: str, *args: object) -> None:
            return None

    endpoint = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    Thread(target=endpoint.serve_forever, daemon=True).start()
    home = tmp_path / "home"
    project = tmp_path / "project"
    home.mkdir(parents=True)
    project.mkdir(parents=True)
    events = tmp_path / "events.jsonl"
    wrapper = tmp_path / "judge.sh"
    wrapper.write_text(
        "#!/bin/sh\n"
        "payload=$(cat)\n"
        f"printf '%s\\n' \"$payload\" >> {events}\n"
        f"printf '%s' \"$payload\" | python3 -I -S {dispatcher}\n"
    )
    wrapper.chmod(0o755)
    logger = tmp_path / "start.sh"
    logger.write_text(
        f"#!/bin/sh\ncat >> {tmp_path / 'started.jsonl'}\necho >> {tmp_path / 'started.jsonl'}\n"
    )
    logger.chmod(0o755)
    registered = json.loads(
        (dispatcher.parents[1] / "hooks.json").read_text(encoding="utf-8")
    )["hooks"]
    (home / "hooks.json").write_text(
        json.dumps(
            {
                "hooks": {
                    "PreToolUse": [
                        {
                            "matcher": registered["PreToolUse"][0]["matcher"],
                            "hooks": [{"type": "command", "command": str(wrapper)}],
                        }
                    ],
                    "SubagentStart": [
                        {"hooks": [{"type": "command", "command": str(logger)}]}
                    ],
                }
            }
        )
    )
    (home / "config.toml").write_text(
        f'model = "{model}"\nmodel_provider = "local_fixture"\n'
        "[features]\nhooks = true\n"
        '[model_providers.local_fixture]\nname = "Local fixture"\n'
        f'base_url = "http://127.0.0.1:{endpoint.server_port}/v1"\n'
        'wire_api = "responses"\nrequires_openai_auth = false\n'
        f'[projects."{project}"]\ntrust_level = "trusted"\n'
    )
    environment = {
        **os.environ,
        "CODEX_HOME": str(home),
        "OPENAI_API_KEY": "",
        "CODEX_API_KEY": "",
    }
    seed_hook_trust(home, asyncio.run(read_hooks(home, project)).resolved())
    executable = shutil.which("codex")
    assert executable is not None
    try:
        subprocess.run(
            [
                executable,
                "exec",
                *(word for feature in features for word in ("--enable", feature)),
                "--skip-git-repo-check",
                "-C",
                str(project),
                "-s",
                "read-only",
                f"{ROOT_MARK}: spawn one subagent.",
            ],
            env=environment,
            capture_output=True,
            stdin=subprocess.DEVNULL,
            timeout=180,
            check=True,
        )
    finally:
        endpoint.shutdown()
    records = [
        json.loads(line)
        for line in (events.read_text().splitlines() if events.exists() else [])
        if line.strip()
    ]
    started = tmp_path / "started.jsonl"
    script.subagents = [
        line
        for line in (started.read_text().splitlines() if started.exists() else [])
        if line.strip()
    ]
    return records, script


V1 = (
    "gpt-5.4",
    ["multi_agent"],
    {
        "name": "spawn_agent",
        "namespace": "multi_agent_v1",
        "arguments": json.dumps({"message": "Reply ok."}),
    },
)
V2 = (
    "gpt-5.4",
    ["multi_agent_v2"],
    {
        "name": "spawn_agent",
        "namespace": "collaboration",
        "arguments": json.dumps({"task_name": "probe_child", "message": "Reply ok."}),
    },
)
"""Each spelling as a scripted model sends it.

`gpt-5.4` is absent from 0.159.2's bundled catalog, which is what offers v1's
spawn at all: Codex falls back to metadata naming no version, and the
features decide."""


@LIVE
@pytest.mark.parametrize(
    ("model", "features", "spawn", "hooked"),
    [
        pytest.param(*V1, "spawn_agent", id="v1"),
        pytest.param(*V2, "collaborationspawn_agent", id="v2"),
    ],
)
def test_each_spelling_of_a_spawn_reaches_the_policy_and_goes_out(
    tmp_path: Path, model: str, features: list[str], spawn: JsonObject, hooked: str
) -> None:
    records, script = judged_spawn(
        tmp_path, model, features, spawn, CODEX_DISPATCHER.resolve()
    )

    assert [record["tool_name"] for record in records] == [hooked]
    assert len(script.subagents) == 1


@LIVE
@pytest.mark.parametrize(
    ("model", "features", "spawn"),
    [pytest.param(*V1, id="v1"), pytest.param(*V2, id="v2")],
)
def test_a_refused_spawn_never_goes_out_in_either_spelling(
    tmp_path: Path, model: str, features: list[str], spawn: JsonObject
) -> None:
    _, codex = refusing(SPAWN_REFUSALS, tmp_path / "tree")

    records, script = judged_spawn(tmp_path / "run", model, features, spawn, codex)

    assert len(records) == 1
    assert script.subagents == []
    assert any(
        "this project runs no subagents" in output for output in script.outputs()
    )
