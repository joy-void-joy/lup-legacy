"""Codex's half of session naming, run as a bare script.

Shipped verbatim into the plugin's ``hooks/runtime/`` beside the coordination
package it imports, and registered under ``UserPromptSubmit`` — and under
``SessionStart`` for a resume. It holds only what Codex spells for itself: the
CLI a name is asked through, the events and their fields, and the app-server
requests that read and set a thread's name. When to ask, what a name may be
and how one is settled on the roster are :mod:`coordination.naming`'s.

Codex takes no name from a hook's answer. The output schema it documents for
this event (https://learn.chatgpt.com/docs/hooks) admits added context, a
block and the common fields, and closes the object to anything else. A thread
is named through its app-server instead, which a process of its own can reach
after the hook has returned — so nothing waits on the model here: the hook
records that an ask started, hands the prompt to a copy of this file in a
session of its own, and lets the prompt go on.

A reopened thread keeps its own name. Codex reports no title to a hook, so
``SessionStart`` with ``source`` ``resume`` records which thread was
reopened, and at the next prompt a copy of this file reads the name that
thread already has through ``thread/read`` and the roster takes it up, in the
roster's shape, leaving the thread's name as it stands. Only a thread with no
name, or one still called after its worktree, is asked for one, the way a new
session is.

Measured on Codex 0.155.1:

- A separate, short-lived ``codex app-server`` answers ``thread/name/set``
  for a thread a live TUI has loaded with ``{}``, and the name lands in the
  home's ``session_index.jsonl``. It outlives the TUI's later turns and its
  exit, and ``codex exec resume <name>`` reopens the thread by it. The live
  TUI's status line goes on showing the title it already has.
- Codex names a thread itself from its first prompt, and that is what the
  session's own status line shows. The two names race, and either order
  settles on this one: a name set after Codex's title replaces it, and a
  thread named before Codex's titler answers is left unnamed by it.
- ``codex exec --ephemeral --skip-git-repo-check --ignore-user-config -C
  <scratch>`` with ``--output-schema`` answers a naming ask in 3.7 seconds
  with JSON meeting the schema, and ``-o`` writes it to a file. With its shell
  on, it meets a prompt asking it to explain something by exploring the
  scratch directory until the deadline passes, so the ask is opened with the
  arguments the generator compiles from the adapter's own list of facilities
  to switch off — every tool, every hook, every write — and the prompt
  arrives quoted, as the thing to name. Killing the npm wrapper at that
  deadline leaves the binary running, which is why an ask runs in a session
  of its own that is killed whole.
- The hook's ``session_id`` is the thread's id, which the arrival binder
  records as the thread ``codex queue`` takes.

Measured on Codex 0.159.2: ``thread/read`` answers a thread's ``name``, and
for a thread nobody named it is the title Codex generated — ``Fix expired MCP
authentication token``, from the thread's first prompt — the name the home's
``session_index.jsonl`` last records for it. So a resumed thread's own title
is what the roster takes up, whichever of the two named it last.

Every failure is silence: the prompt goes on, and the session keeps its name.
"""

import json
import os
import signal

# lup: ignore[subprocess] — `sh` is third-party and this half is shipped into a
# bare script that has no virtual environment to resolve it from
import subprocess
import sys
import tempfile
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TypedDict

# The hook is launched as a bare script, promised no cwd, PYTHONPATH, or
# interpreter environment, and this file sits in the `runtime/` directory
# that holds the coordination package. Naming it as a search path is what
# lets the imports below resolve.
sys.path.insert(0, str(Path(__file__).parent))
from coordination.naming import (
    Arrival,
    Naming,
    answer_schema,
    answered,
    compiled_for,
    concluded,
    detached,
    prompted,
    ran,
    reopened,
    request_for,
    resuming,
    said,
    session_of,
    settled,
    shown_as,
    stopped,
)


class Payload(Arrival, total=False):
    """What Codex hands the hook, under either event it is registered for."""

    hook_event_name: str
    source: str


class ClientInfo(TypedDict):
    name: str
    version: str


class Initialize(TypedDict):
    clientInfo: ClientInfo


class SetName(TypedDict):
    threadId: str
    name: str


class ReadThread(TypedDict):
    threadId: str


class Empty(TypedDict):
    pass


class Request(TypedDict):
    """One app-server request, as its protocol spells it on a stdio line."""

    method: str
    id: int
    params: Initialize | SetName | ReadThread


class Notification(TypedDict):
    method: str
    params: Empty


class ThreadRecord(TypedDict, total=False):
    """A thread as ``thread/read`` answers, as far as its name."""

    name: str | None


class Result(TypedDict, total=False):
    """A reply's result, as far as the requests sent here read one."""

    thread: ThreadRecord


class Reply(TypedDict, total=False):
    """One line the app-server writes, as far as waiting on a request reads it."""

    id: int
    result: Result


def asked(prompt: str, naming: Naming) -> str:
    """The name Codex gives the work *prompt* describes, blank for none.

    Asked from a scratch directory, so the project configuration beside the
    session is not the one read, and whole on stdin: a prompt too long for
    the naming model is an ask that fails, not one to cut. The compiled
    arguments name the model and its effort, and are what keep the ask to
    answering — no tool, no hook, nothing written — while the configuration
    naming this project's plugin is not read.
    """
    with tempfile.TemporaryDirectory(prefix="lup-naming-") as scratch:
        schema = Path(scratch) / "schema.json"
        reply = Path(scratch) / "answer.json"
        try:
            schema.write_text(answer_schema(), encoding="utf-8")
        except OSError:
            return ""
        printed = ran(
            [
                "codex",
                "exec",
                "--ephemeral",
                "--skip-git-repo-check",
                "--ignore-user-config",
                "-C",
                scratch,
                *naming["arguments"],
                "--config",
                f"developer_instructions={json.dumps(naming['instruction'])}",
                "--output-schema",
                str(schema),
                "-o",
                str(reply),
                "-",
            ],
            request_for(prompt),
            naming["deadline_seconds"],
            cwd=scratch,
        )
        try:
            return (
                answered(reply.read_text("utf-8"), naming["longest"])
                if printed is not None
                else ""
            )
        except OSError:
            return ""


@contextmanager
def app_server(deadline: float) -> Iterator[subprocess.Popen[str] | None]:
    """A short-lived app-server over the session's own home, initialized, or nothing.

    The home is whatever the environment names, which is the session's: a
    hook inherits it from the runtime that spawned it. In a session of its
    own and killed whole at the deadline, which ends every read below with
    the stream it was waiting on — the wrapper and the binary it starts alike.
    """
    try:
        server = subprocess.Popen(
            ["codex", "app-server"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            start_new_session=True,
        )
    except OSError:
        yield None
        return
    watchdog = threading.Timer(deadline, os.killpg, (server.pid, signal.SIGKILL))
    watchdog.start()
    try:
        greeted = requested(
            server,
            Request(
                method="initialize",
                id=0,
                params=Initialize(clientInfo=ClientInfo(name="lup", version="0")),
            ),
        )
        ready = greeted is not None and notified(
            server, Notification(method="initialized", params=Empty())
        )
        yield server if ready else None
    finally:
        watchdog.cancel()
        stopped(server)


def notified(server: subprocess.Popen[str], notification: Notification) -> bool:
    """Send one notification, which nothing answers."""
    if server.stdin is None:
        return False
    server.stdin.write(json.dumps(notification) + "\n")
    server.stdin.flush()
    return True


def requested(server: subprocess.Popen[str], request: Request) -> Result | None:
    """Send one request and read until its reply: its result, or nothing for an error."""
    if server.stdin is None or server.stdout is None:
        return None
    server.stdin.write(json.dumps(request) + "\n")
    server.stdin.flush()
    for line in server.stdout:
        try:
            reply: Reply = json.loads(line)
        except ValueError:
            continue
        match reply:
            case {"id": answering, "result": result} if answering == request["id"]:
                return result
            case {"id": answering} if answering == request["id"]:
                return None
            case _:
                continue
    return None


def thread_named(thread: str, name: str, deadline: float) -> bool:
    """Give *thread* the user-facing name *name*, saying whether it took."""
    with app_server(deadline) as server:
        return (
            server is not None
            and requested(
                server,
                Request(
                    method="thread/name/set",
                    id=1,
                    params=SetName(threadId=thread, name=name),
                ),
            )
            is not None
        )


def thread_name(thread: str, deadline: float) -> str | None:
    """The user-facing name *thread* already has: blank for none, nothing unanswered."""
    with app_server(deadline) as server:
        result = (
            requested(
                server,
                Request(method="thread/read", id=1, params=ReadThread(threadId=thread)),
            )
            if server is not None
            else None
        )
    match result:
        case {"thread": {"name": str(name)}}:
            return name
        case {"thread": _}:
            return ""
        case _:
            return None


def named_in_background(root: Path, member_id: str, thread: str, prompt: str) -> None:
    """Settle the roster's name for the session, and give the thread the name settled."""
    naming = compiled_for(Path(__file__))
    if naming is None:
        return
    wanted = asked(prompt, naming) if prompt.strip() else ""
    name = settled(root, member_id, wanted) if wanted else ""
    if name and thread and thread_named(thread, name, naming["deadline_seconds"]):
        shown_as(root, member_id, name, name)
    concluded(root, member_id)


def adopted_in_background(root: Path, member_id: str, thread: str, prompt: str) -> None:
    """Take up the name a reopened thread already has, or ask for one where it has none.

    Its own name is kept as it stands: the roster takes it up in its own
    shape, and the thread hears nothing back. A thread the app-server could
    not be asked about is left for the next prompt to try again.
    """
    naming = compiled_for(Path(__file__))
    if naming is None:
        return
    kept = thread_name(thread, naming["deadline_seconds"])
    if kept is None:
        return
    if reopened(root, member_id, kept, naming["longest"]):
        concluded(root, member_id)
        return
    named_in_background(root, member_id, thread, prompt)


def hooked(root: Path, member_id: str, payload: Payload, naming: Naming) -> None:
    """Start what this event calls for, and return without waiting on it.

    A resume is only recorded as it happens: the session may not be on the
    roster yet, and its thread's name is read at the next prompt instead.
    """
    host, thread = Path(__file__), session_of(payload)
    match payload:
        case {"hook_event_name": "SessionStart", "source": "resume"} if (
            thread and "agent_id" not in payload and "agent_type" not in payload
        ):
            resuming(root, member_id, thread)
            return
        case {"hook_event_name": "SessionStart"}:
            return
        case _:
            pass
    step = prompted(root, member_id, payload, naming, None)
    match step["move"]:
        case "ask":
            detached(host, ["ask", str(root), member_id, thread], said(payload))
        case "adopt":
            detached(
                host, ["adopt", str(root), member_id, step["value"]], said(payload)
            )
        case "push" if thread:
            detached(host, ["push", str(root), thread, step["value"]], "")
        case _:
            pass


def main() -> None:
    """Answer the event, or run one detached half of it; say nothing either way.

    As the hook, the store root, the launcher-proven member id (blank where
    nothing launched this session) and the event's name arrive as arguments,
    and the compiled declaration is read under this file's name.
    """
    try:
        match sys.argv[1:]:
            case ["ask", root, member_id, thread]:
                named_in_background(Path(root), member_id, thread, sys.stdin.read())
            case ["adopt", root, member_id, thread]:
                adopted_in_background(Path(root), member_id, thread, sys.stdin.read())
            case ["push", _root, thread, name]:
                naming = compiled_for(Path(__file__))
                if naming is not None:
                    thread_named(thread, name, naming["deadline_seconds"])
            case [root, member_id, _event]:
                payload: Payload = json.load(sys.stdin)
                naming = compiled_for(Path(__file__))
                if naming is not None:
                    hooked(
                        Path(root), member_id or session_of(payload), payload, naming
                    )
            case _:
                pass
    except Exception:
        return


if __name__ == "__main__":
    main()
