"""The conservative answer for a call no semantic family classifies.

A tool the policy has no semantics for must still stop at a human, and the
question it raises has to name the tool the agent actually called.
"""

from lup.policy.chain import UnknownToolPolicy
from lup.policy.models import ToolIdentity, UnknownTool


def test_unknown_tool_ask_names_the_original_tool() -> None:
    event = UnknownTool(identity=ToolIdentity(original_name="mystery_tool"))

    decision = UnknownToolPolicy().decide(event)

    assert decision.effect == "ask"
    assert "mystery_tool" in decision.reason
