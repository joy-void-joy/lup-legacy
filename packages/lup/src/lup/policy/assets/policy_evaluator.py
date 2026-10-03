"""Hermetic destination policy evaluator; no native rendering or review queue."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "runtime"))
from decisions import local_edit_decision
from host import worktree_root
from kernel.policy_protocol import decision_wire, read_edit_request
from policy_data import AUTONOMOUS_AGENT_IDENTITIES


def main() -> None:
    """Judge one normalized document under these accepted policy bytes."""
    request = read_edit_request(json.load(sys.stdin))
    path = Path(request["path"])
    owner = Path(request["owner"])
    if not path.is_absolute() or str(owner.resolve()) != worktree_root(str(path)):
        raise ValueError("edit target does not belong to the destination owner")
    autonomous = (
        request["autonomous"]
        and request["agent_identity"] in AUTONOMOUS_AGENT_IDENTITIES
    )
    decision = local_edit_decision(
        str(path),
        request["before"],
        request["after"],
        request["path_exists"],
        autonomous,
        request["operation"],
        owner,
        allowances=[],
        resolve_external=False,
    )
    print(json.dumps({"protocol": 2, "decision": decision_wire(decision)}))
