"""Serve one hosted tool server over MCP stdio, for a runtime that launched it.

A runtime's own CLI starts every server a session declares before that session
is anything this library holds, so the process started here does two things a
session opened in process is simply handed. It resolves the session: the one
an adapter relayed in the environment, or — for a server a native runtime
starts — one opened under the name its command line gives, whose identity is
the launcher's id or the one the runtime gave the process. And it validates
the server back from the class and fields
:meth:`~lup.mcp.HostedServer.served` wrote, so what it serves is the
declaration the launch compiled rather than a name looked up in a list that
could disagree with it.

What only a project can build — a container its settings start, a delegation
verb its specs define — reaches the session through a needs hook the command
names, which is the one place a project's code runs before its groups do.

Run as ``python -m lup.mcp.serve``, the program a
:class:`~lup.mcp.ServeLaunch` defaults to, or as the ``tools serve``
command a composed CLI mounts.
"""

import os
from typing import Annotated

import typer
from pydantic import ImportString, TypeAdapter

from lup.coordination.bare.runtime import Runtime, runtime_of, stdin_runtime
from lup.coordination.identity import MemberEnv, session_cli_name
from lup.coordination.repository import runtime_member
from lup.coordination.wake import WakePath
from lup.observability.metrics import configure_metrics, metrics_path
from lup.orchestration.reflection import ReviewGate
from lup.providers.identity import native_session_id, native_wake
from lup.tools.mcp import serve_stdio
from lup.mcp import NeedsHook, ServedServer
from lup.tools.toolsets import SessionNeeds
from lup.types import JsonObject
from lup.workspace.context import SessionContext, read_session_context
from lup.workspace.paths import project_root


def harness_session_context(name: str) -> SessionContext:
    """Open the session a natively launched tool server serves.

    An adapter-launched server is handed a session that already exists; a
    server a native runtime starts is the first thing in that session to run,
    so it opens one under the name it was given. The name is the whole
    identity, and every server of one runtime's session is started with the
    same name, so those processes agree on where session state lives without
    a channel between them.
    """
    from lup.workspace.notes import session_gate_flag, setup_notes

    notes = setup_notes(session_id=name, task_id=name, type="harness")
    return SessionContext(
        session_dir=notes.session,
        outputs_dir=notes.output.parent,
        gate_flag=session_gate_flag(name),
        session_id=name,
        task_id=name,
    )


def context_needs(
    context: SessionContext,
    identity: str,
    wake: WakePath = WakePath(),
    runtime: Runtime | None = None,
) -> SessionNeeds:
    """What one opened session gives the groups built for it.

    *identity* is what the roster knows the session by where no launcher
    minted a member id, which the launcher's id outranks — for the runtime it
    was minted for. *runtime* is the process the session is: one that
    inherited the launcher's id from another session's shell serves a member
    of its own, spawned by that session.
    """
    root = project_root()
    served = runtime or runtime_of(os.getpid())
    member = runtime_member(root, MemberEnv().member_id, identity, served)
    return SessionNeeds(
        session_dir=context.session_dir,
        root=root,
        gate=ReviewGate(flag_path=context.gate_flag),
        outputs_dir=context.outputs_dir,
        realtime_dir=context.realtime_dir,
        member=member.member_id,
        spawned_by=member.spawned_by,
        wake=wake,
        runtime=served,
    )


def resolved_needs(session: str | None, runtime: str | None) -> SessionNeeds | None:
    """What the session this process serves gives its server, or nothing.

    The relayed context wins, being a session an adapter already opened. A
    native runtime relays none, so the session opened for it is named for
    where its notes go and not for who it is: what the roster knows it by is
    the launcher's id where one was minted, otherwise the id the runtime gave
    this process, and what would make it look is the runtime's adapter's to
    say. Nothing where neither was given, since every group closes over a
    session's directories.

    Whichever it is, the session is the process feeding this one's input,
    read before anything else reads it: that is what the roster's row answers
    for, where this process only serves it.
    """
    served = stdin_runtime()
    match read_session_context(), session, runtime:
        case SessionContext() as context, _, _:
            needs = context_needs(context, context.session_id or "", runtime=served)
        case None, str(), str():
            context = harness_session_context(session)
            wake = native_wake(runtime, session_cli_name())
            needs = context_needs(
                context, native_session_id(runtime), wake, runtime=served
            )
        case None, str(), None:
            context = harness_session_context(session)
            needs = context_needs(context, "", runtime=served)
        case _:
            return None
    configure_metrics(metrics_path(context.session_dir))
    return needs


def serve(
    served: ServedServer,
    needs: SessionNeeds,
    list_only: bool = False,
) -> None:
    """Serve one server's tools over stdio, or print their names and return.

    A server that builds nothing for this session still serves, empty: the
    runtime started it because the launch declared it, and a process that
    exited instead would read to that runtime as a server that crashed.
    """
    from lup.tools.mcp import create_mcp_server

    server = served.built()
    entry = server.hosted(needs)
    hosted = entry if entry is not None else create_mcp_server(server.name, tools=[])
    if list_only:
        for tool in hosted.tools:
            typer.echo(tool.name)
        return
    serve_stdio(hosted)


def serve_command(
    server: Annotated[
        str,
        typer.Argument(help="The server's class, as module:Class"),
    ],
    fields: Annotated[
        str,
        typer.Argument(help="The server's fields, as a JSON object"),
    ] = "{}",
    session: Annotated[
        str | None,
        typer.Option(
            "--session", help="Open a session under this name when none is relayed"
        ),
    ] = None,
    runtime: Annotated[
        str | None,
        typer.Option("--runtime", help="The runtime serving this session"),
    ] = None,
    needs: Annotated[
        str | None,
        typer.Option(
            "--needs", help="Import path of what the project adds to the session"
        ),
    ] = None,
    list_only: Annotated[
        bool,
        typer.Option("--list", help="Print the served tool names and exit"),
    ] = False,
) -> None:
    """Serve one hosted tool server over MCP stdio, for the session it names."""
    served = ServedServer.model_validate(
        {"server": server, "fields": TypeAdapter(JsonObject).validate_json(fields)}
    )
    resolved = resolved_needs(session, runtime)
    if resolved is None:
        typer.echo(
            "no session context and no --session name, so there is nothing to "
            "serve: an adapter relays a session in the environment and a native "
            "runtime names one on the command line",
            err=True,
        )
        raise typer.Exit(1)
    if needs is not None:
        hook = TypeAdapter(ImportString[NeedsHook]).validate_python(needs)
        resolved = hook(resolved, runtime)
    serve(served, resolved, list_only)


if __name__ == "__main__":
    typer.run(serve_command)
