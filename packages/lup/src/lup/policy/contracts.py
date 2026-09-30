"""The decision seam: one typed semantic event in, one verdict out.

:class:`DecisionPolicy` is implemented by the validated policies in
:mod:`lup.policy.rules` and :mod:`lup.policy.chain`, and a composition
declares one per family in :class:`DeclaredPolicies`.
"""

from abc import ABC, abstractmethod

from lup.policy.models import (
    Decision,
    EditBatch,
    FetchUrl,
    ShellCommand,
    UnknownTool,
)


class DeclaredPolicies:
    """The policies one composition declared, as a semantic tool finds them.

    A tool asks this for the policy of its own family, so adding a family
    adds a slot here and a class beside the others, rather than an arm to
    every walk that would have to notice it. A family left undeclared is
    ``None`` and asks: a composition that wires fetch and forgets shell must
    stop at a human, not wave the command through.
    """

    def __init__(
        self,
        *,
        unknown: "DecisionPolicy[UnknownTool]",
        fetch: "DecisionPolicy[FetchUrl] | None" = None,
        shell: "DecisionPolicy[ShellCommand] | None" = None,
        edit: "DecisionPolicy[EditBatch] | None" = None,
    ) -> None:
        self.unknown = unknown
        self.fetch = fetch
        self.shell = shell
        self.edit = edit


class DecisionPolicy[E](ABC):
    """Compute a security decision for one typed semantic event."""

    @abstractmethod
    def decide(self, event: E) -> Decision:
        """Return allow, ask, or deny with evidence."""
