"""Small deterministic helpers shared across pipeline stages.

Invocation-argument serialization for the adapter renderers, tree indexing
for reconciliation, and the validation-failure error
:mod:`lup.harness.validation` raises.
"""

import json


from lup.harness.models import Artifact, ArtifactTree
from lup.types import JsonValue


def argument_text(value: JsonValue) -> str:
    """Serialize one semantic invocation argument deterministically."""
    if isinstance(value, str):
        return value
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


class ArtifactValidationError(ValueError):
    """Complete desired output failed validation before materialization."""


def artifact_map(tree: ArtifactTree) -> dict[str, Artifact]:
    """Index an already validated tree by portable path."""
    return {artifact.path.as_posix(): artifact for artifact in tree.artifacts}
