"""An isolated native stdio fixture serving the production coordination server.

Its accelerated heartbeat exercises an expired roster pulse without waiting
for the production interval. Codex owns and stops this subprocess.
"""

from pathlib import Path

from lup.coordination.identity import session_member_id
from lup.coordination.peer_tools import RosterPulse
from lup.coordination.pulse import Pulse
from lup.coordination.wake import WakePath
from lup.mcp import Coordination
from lup.orchestration.reflection import ReviewGate
from lup.tools.mcp import create_mcp_server, serve_stdio
from lup.tools.toolsets import SessionNeeds

root = Path.cwd()
group = Coordination().group()
needs = SessionNeeds(
    root=root,
    session_dir=root / "session",
    gate=ReviewGate(),
    member=session_member_id(),
    wake=WakePath(runtime="codex"),
)
companions = [
    companion.model_copy(update={"pulse": Pulse(interval_seconds=0.02)})
    if isinstance(companion, RosterPulse)
    else companion
    for companion in group.companions(needs)
]
serve_stdio(
    create_mcp_server(group.name, tools=group.tools(needs), companions=companions)
)
