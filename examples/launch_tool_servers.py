"""Declare the tool servers every session carries, launched or opened here.

``tools.mcp`` is the session's whole roster: a launched Claude Code gets it
as ``--mcp-config`` with ``--strict-mcp-config`` and a launched Codex as
``--config mcp_servers.*``, a session opened here hosts the same servers in
process. ``serve`` says how a launched CLI starts the servers lup hosts, and
how long each has to come up; ``always_load`` exempts a server from Claude
Code's tool search.

    uv run -m examples.launch_tool_servers
"""

from lup import Claude, Codex
from lup.mcp import CodeIntel, Coordination, ServeLaunch
from lup.providers.claude import ClaudeTools
from lup.providers.codex import CodexTools


def main() -> None:
    servers = [Coordination(always_load=True), CodeIntel()]
    serve = ServeLaunch(startup_timeout_seconds=60)
    claude = Claude(tools=ClaudeTools(builtin="stock", mcp=servers, serve=serve))
    codex = Codex(tools=CodexTools(builtin="stock", mcp=servers, serve=serve))
    print(claude.command())
    print(codex.command())


if __name__ == "__main__":
    main()
