"""The conservative answer for a call no semantic family classifies.

``UnknownToolPolicy`` refuses the calls a project declared against and holds
every other unclassified tool at ``ask``, so a tool with no semantics of its
own still reaches a human rather than running.
"""

from lup.policy.contracts import DecisionPolicy
from lup.policy.kernel.tools import decide_tool
from lup.policy.refused_tools import RefusedTool, erase_refused_tools
from lup.policy.models import Decision, UnknownTool


class UnknownToolPolicy(DecisionPolicy[UnknownTool]):
    """Refuse a declared call, and fail conservatively on the rest.

    A refusal table is the one rule surface a tool with no semantics of its
    own can have, so it is consulted here rather than in a family of its own:
    what is being judged is still the call nothing classified. Everything the
    table does not speak to keeps the conservative ask.
    """

    def __init__(self, refused: list[RefusedTool] | None = None) -> None:
        self.refused = erase_refused_tools(refused or [])

    def decide(self, event: UnknownTool) -> Decision:
        name = event.identity.original_name
        refusal = decide_tool(
            name,
            [value for value in event.input.values() if isinstance(value, str)],
            self.refused,
        )
        if refusal is not None:
            return Decision.of(refusal)
        return Decision(effect="ask", reason=f"unclassified tool {name!r}")
